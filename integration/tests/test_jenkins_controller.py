"""Black-box checks for the repository-owned Jenkins controller deployment."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
EXPECTED_CONTROLLER_IMAGE = (
    "docker.io/jenkins/jenkins:2.568.3-jdk21@"
    "sha256:68964c38dbb70c0bf6adff480d6dda2e1191d2d07e076c9fd2996345303eb999"
)
EXPECTED_HANDOFF_PARAMETERS = {
    "HANDOFF_CONTRACT_VERSION",
    "EXECUTION_ID",
    "REQUESTED_GIT_REF",
    "RESOLVED_COMMIT_SHA",
    "RUN_REQUEST_PATH",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
}
FAKE_AGENT_IMAGES = (
    "docker.io/example/composition@sha256:" + "1" * 64,
    "docker.io/example/image-build@sha256:" + "2" * 64,
    "docker.io/example/cv@sha256:" + "3" * 64,
    "docker.io/example/cd@sha256:" + "4" * 64,
)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    yaml = YAML(typ="safe")
    loaded = yaml.load(path)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(loaded, dict)
    return cast("Mapping[str, Any]", loaded)


def _write_fake_deployment_tools(fake_bin: Path) -> None:
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        "printf 'images=%s|%s|%s|%s\\n' "
        '"${JENKINS_COMPOSITION_AGENT_IMAGE:-}" '
        '"${JENKINS_IMAGE_BUILD_AGENT_IMAGE:-}" '
        '"${JENKINS_CV_AGENT_IMAGE:-}" '
        '"${JENKINS_CD_AGENT_IMAGE:-}" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        "  'version --format {{.Server.Os}}/{{.Server.Arch}}') "
        "printf 'linux/amd64\\n' ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)

    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *composition-fixture-v1.yaml) "
        f"printf '{FAKE_AGENT_IMAGES[0]}\\n' ;;\n"
        "  *image-build-fixture-v1.yaml) "
        f"printf '{FAKE_AGENT_IMAGES[1]}\\n' ;;\n"
        f"  *cv-fixture-v1.yaml) printf '{FAKE_AGENT_IMAGES[2]}\\n' ;;\n"
        f"  *cd-fixture-v1.yaml) printf '{FAKE_AGENT_IMAGES[3]}\\n' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)


def _assert_plaintext_relocated_agent_url_is_rejected(
    config: Path, fake_bin: Path, docker_log: Path
) -> None:
    valid_config = config.read_text()
    config.write_text(
        valid_config.replace(
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080",
            "JENKINS_AGENT_CONTROLLER_URL=http://remote.example.test:8080",
        )
    )
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    config.write_text(valid_config)

    assert completed.returncode == 2
    assert "internal Compose URL or an uncredentialed HTTPS URL" in completed.stderr


def test_controller_image_and_complete_plugin_set_are_exactly_pinned() -> None:
    dockerfile = (JENKINS_ROOT / "controller" / "Dockerfile").read_text()
    assert f"FROM {EXPECTED_CONTROLLER_IMAGE}" in dockerfile
    assert "jenkins-plugin-cli --latest=false" in dockerfile

    plugin_lines = [
        line
        for line in (JENKINS_ROOT / "controller" / "plugins.txt")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    ]
    assert len(plugin_lines) == 47
    assert len(plugin_lines) == len(set(plugin_lines))
    assert all(
        re.fullmatch(r"[a-z0-9-]+:[A-Za-z0-9_.+-]+", line) for line in plugin_lines
    )
    assert "configuration-as-code:2121.v86fe99d4b_b_a_b_" in plugin_lines
    assert "job-dsl:3732.v9a_c49a_61a_313" in plugin_lines
    assert "git:5.10.1" in plugin_lines
    assert "matrix-auth:3.3" in plugin_lines


def test_compose_exposes_only_loopback_http_and_persists_only_controller_state() -> (
    None
):
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    controller = compose["services"]["controller"]

    assert controller["platform"] == "linux/amd64"
    assert controller["ports"] == ["127.0.0.1:${JENKINS_HTTP_PORT}:8080"]
    assert controller["restart"] == "no"
    assert controller["volumes"] == ["jenkins-home:/var/jenkins_home"]
    assert set(controller["networks"]) == {
        "controller-egress",
        "controller-integration",
        "controller-ci",
        "controller-image-build",
        "controller-cv",
        "controller-cd",
    }
    assert set(compose["volumes"]) == {"jenkins-home"}
    assert "50000" not in (JENKINS_ROOT / "compose.yaml").read_text()


def test_jcasc_owns_zero_executor_security_and_retention_defaults() -> None:
    casc = _load_yaml(JENKINS_ROOT / "casc" / "jenkins.yaml")
    jenkins = casc["jenkins"]

    assert jenkins["numExecutors"] == 0
    assert jenkins["slaveAgentPort"] == -1
    assert jenkins["disableRememberMe"] is True
    assert jenkins["noUsageStatistics"] is True
    assert jenkins["scmCheckoutRetryCount"] == 0
    assert jenkins["securityRealm"]["local"]["allowsSignup"] is False
    assert jenkins["authorizationStrategy"]["projectMatrix"]["entries"] == [
        {
            "user": {
                "name": "${JENKINS_ADMIN_ID}",
                "permissions": ["Overall/Administer"],
            }
        },
        {
            "user": {
                "name": "${JENKINS_HANDOFF_ID}",
                "permissions": ["Overall/Read"],
            }
        },
    ]
    configured_discarders = casc["unclassified"]["buildDiscarders"][
        "configuredBuildDiscarders"
    ]
    assert configured_discarders[1]["simpleBuildDiscarder"]["discarder"][
        "logRotator"
    ] == {"artifactDaysToKeepStr": "90", "daysToKeepStr": "90"}
    assert casc["jobs"] == [
        {
            "providedEnv": {
                "HANDOFF_USER": "${JENKINS_HANDOFF_ID}",
                "REPOSITORY_URL": "${JENKINS_REPOSITORY_URL}",
            }
        },
        {"file": "/usr/local/share/jenkins/job-dsl/pipeline-integration.groovy"},
    ]
    assert casc["security"]["apiToken"] == {
        "creationOfLegacyTokenEnabled": False,
        "tokenGenerationOnCreationEnabled": False,
    }

    casc_text = (JENKINS_ROOT / "casc" / "jenkins.yaml").read_text()
    assert "jenkins-admin-password" in casc_text
    assert "jenkins-handoff-password" in casc_text
    assert "http://" not in casc_text
    assert "https://" not in casc_text


def test_job_dsl_defines_the_fixed_non_secret_handoff_contract() -> None:
    job_dsl = (JENKINS_ROOT / "jobs" / "pipeline-integration.groovy").read_text()

    parameter_names = set(re.findall(r"stringParam\('([A-Z_]+)'", job_dsl))
    assert parameter_names == EXPECTED_HANDOFF_PARAMETERS
    assert "pipelineJob('pipeline-integration')" in job_dsl
    assert "disableConcurrentBuilds()" in job_dsl
    assert "artifactDaysToKeep(90)" in job_dsl
    assert "daysToKeep(90)" in job_dsl
    assert "url(REPOSITORY_URL)" in job_dsl
    assert "branch('${RESOLVED_COMMIT_SHA}')" in job_dsl
    assert "scriptPath('Jenkinsfile')" in job_dsl
    assert "hudson.model.Item.Read" in job_dsl
    assert "hudson.model.Item.Build" in job_dsl
    assert "hudson.model.Item.Cancel" in job_dsl
    assert "trigger" not in job_dsl.lower()
    assert "credential" not in job_dsl.lower()


def test_stack_validate_uses_compose_and_rejects_insecure_secret_files(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_fake_deployment_tools(fake_bin)

    admin_secret = tmp_path / "admin-password"
    handoff_secret = tmp_path / "handoff-password"
    admin_secret.write_text("admin-secret\n")
    handoff_secret.write_text("handoff-secret\n")
    admin_secret.chmod(0o600)
    handoff_secret.chmod(0o644)
    agent_secrets = {
        name: tmp_path / f"{name}-agent-secret"
        for name in ("integration", "ci", "image-build", "cv", "cd")
    }
    for index, secret in enumerate(agent_secrets.values(), start=10):
        secret.write_text(f"{index:x}" * 64)
        secret.chmod(0o600)
    config = tmp_path / "controller.env"
    config.write_text(
        "JENKINS_HTTP_PORT=8080\n"
        "JENKINS_ADMIN_ID=administrator\n"
        "JENKINS_HANDOFF_ID=github-handoff\n"
        "JENKINS_REPOSITORY_URL=https://github.com/example/repository.git\n"
        "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080\n"
        f"JENKINS_ADMIN_PASSWORD_FILE={admin_secret}\n"
        f"JENKINS_HANDOFF_PASSWORD_FILE={handoff_secret}\n"
        f"JENKINS_INTEGRATION_AGENT_SECRET_FILE={agent_secrets['integration']}\n"
        f"JENKINS_CI_AGENT_SECRET_FILE={agent_secrets['ci']}\n"
        f"JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE={agent_secrets['image-build']}\n"
        f"JENKINS_CV_AGENT_SECRET_FILE={agent_secrets['cv']}\n"
        f"JENKINS_CD_AGENT_SECRET_FILE={agent_secrets['cd']}\n"
    )

    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "must not be accessible by group or other users" in completed.stderr

    handoff_secret.chmod(0o200)
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "must be owner-readable" in completed.stderr

    handoff_secret.chmod(0o600)
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    validation_log = docker_log.read_text()
    assert "compose" in validation_log
    assert "config --quiet" in validation_log
    assert f"images={'|'.join(FAKE_AGENT_IMAGES)}" in validation_log

    _assert_plaintext_relocated_agent_url_is_rejected(config, fake_bin, docker_log)

    agent_secrets["ci"].write_text(agent_secrets["integration"].read_text())
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "validate"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 2
    assert "registration secrets must not be shared" in completed.stderr
    agent_secrets["ci"].write_text("b" * 64)

    subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "start"],
        check=True,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=True,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    lifecycle_log = docker_log.read_text()
    assert "compose" in lifecycle_log
    assert "build integration ci image-build cv cd" in lifecycle_log
    assert "up --no-build --detach --wait --wait-timeout 240" in lifecycle_log
    assert "down" in lifecycle_log
    assert "down --volumes" not in lifecycle_log

    config.write_text("NOT_A_CONTROLLER_SETTING=value\n")
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    missing_config = tmp_path / "missing.env"
    completed = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", missing_config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
