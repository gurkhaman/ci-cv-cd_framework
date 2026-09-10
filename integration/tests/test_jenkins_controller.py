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


def _load_yaml(path: Path) -> Mapping[str, Any]:
    yaml = YAML(typ="safe")
    loaded = yaml.load(path)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(loaded, dict)
    return cast("Mapping[str, Any]", loaded)


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
    assert set(controller["networks"]) == {"controller-agents", "controller-egress"}
    assert compose["networks"]["controller-agents"]["internal"] is True
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
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        "  'version --format {{.Server.Os}}/{{.Server.Arch}}') "
        "printf 'linux/amd64\\n' ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)

    admin_secret = tmp_path / "admin-password"
    handoff_secret = tmp_path / "handoff-password"
    admin_secret.write_text("admin-secret\n")
    handoff_secret.write_text("handoff-secret\n")
    admin_secret.chmod(0o600)
    handoff_secret.chmod(0o644)
    config = tmp_path / "controller.env"
    config.write_text(
        "JENKINS_HTTP_PORT=8080\n"
        "JENKINS_ADMIN_ID=administrator\n"
        "JENKINS_HANDOFF_ID=github-handoff\n"
        "JENKINS_REPOSITORY_URL=https://github.com/example/repository.git\n"
        f"JENKINS_ADMIN_PASSWORD_FILE={admin_secret}\n"
        f"JENKINS_HANDOFF_PASSWORD_FILE={handoff_secret}\n"
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
    assert "compose" in docker_log.read_text()
    assert "config --quiet" in docker_log.read_text()

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
    assert (
        "up --no-build --detach --wait --wait-timeout 240 controller" in lifecycle_log
    )
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
