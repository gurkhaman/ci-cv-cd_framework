"""Black-box checks for the repository-owned Jenkins controller deployment."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        "printf 'network-internal=%s\\n' "
        '"${JENKINS_AGENT_NETWORK_INTERNAL:-}" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        "  'version --format {{.Server.Os}}/{{.Server.Arch}}') "
        "printf 'linux/amd64\\n' ;;\n"
        "  'version --format {{.Server.Version}}') printf '29.7.2\\n' ;;\n"
        "  'compose version --short') printf '5.5.1\\n' ;;\n"
        "  'volume inspect sdi-jenkins-'*) exit 1 ;;\n"
        "  'network inspect sdi-jenkins-'*) exit 1 ;;\n"
        "  'volume inspect --format '*' sdi-jenkins-'*) "
        "for probe do :; done; printf '%s\\n' \"${probe#sdi-jenkins-}\" ;;\n"
        "  'network create --label org.sdi.preflight='*) "
        'for argument do case "$argument" in org.sdi.preflight=*) '
        'printf \'%s\\n\' "${argument#*=}" > "$DOCKER_LOG.network-owner" ;; '
        "esac; done; "
        f"printf '%s\\n' '{'a' * 64}' ;;\n"
        "  'network inspect --format '*) cat \"$DOCKER_LOG.network-owner\" ;;\n"
        "  *' ps --all --quiet') "
        "[ \"${FORCE_KILL:-}\" != 1 ] || printf 'forced-container\\n' ;;\n"
        "  'inspect --format {{.State.ExitCode}} forced-container') "
        "printf '137\\n' ;;\n"
        "  'image inspect --format {{json .Config.Volumes}}'*) "
        "printf 'null\\n' ;;\n"
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
        "  *sdi_pipeline_integration._recovery_contracts*authority*) exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)

    for name, output in (("curl", ""), ("systemctl", "systemd 259")):
        command = fake_bin / name
        command.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
        command.chmod(0o755)
    git = fake_bin / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  --version) printf '%s\\n' 'git version 2.53.0' ;;\n"
        "  *'status --porcelain=v1 --untracked-files=all') exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)


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

    config.write_text(
        valid_config.replace(
            "JENKINS_AGENT_CONTROLLER_URL=http://controller:8080",
            "JENKINS_AGENT_CONTROLLER_URL=https://jenkins.example.test",
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

    assert completed.returncode == 0, completed.stderr
    assert "network-internal=false" in docker_log.read_text()


def _assert_single_agent_reconciliation(
    config: Path,
    fake_bin: Path,
    docker_log: Path,
    unrelated_agent_secret: Path,
) -> None:
    class IdleJenkinsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/crumbIssuer/api/json":
                body = b'{"crumbRequestField":"Jenkins-Crumb","crumb":"test"}'
            elif self.path.startswith("/queue/api/json"):
                body = b'{"items":[]}'
            elif self.path.startswith("/job/pipeline-integration/api/json"):
                body = b'{"builds":[]}'
            elif self.path.startswith("/api/json"):
                body = b'{"quietingDown":false}'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *arguments: object) -> None:  # noqa: A002
            del format, arguments

    docker_log.write_text("")
    unrelated_agent_secret.chmod(0o000)
    server = ThreadingHTTPServer(("127.0.0.1", 0), IdleJenkinsHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    original_config = config.read_text()
    config.write_text(
        original_config.replace(
            "JENKINS_HTTP_PORT=8080", f"JENKINS_HTTP_PORT={server.server_port}"
        )
    )
    try:
        subprocess.run(
            [
                JENKINS_ROOT / "bin" / "stack",
                "--config",
                config,
                "reconcile-agent",
                "cv",
            ],
            check=True,
            capture_output=True,
            env={
                "DOCKER_LOG": str(docker_log),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            },
            text=True,
        )
    finally:
        config.write_text(original_config)
        server.shutdown()
        thread.join()
        server.server_close()
    reconcile_log = docker_log.read_text()

    assert "build cv" in reconcile_log
    assert (
        "up --no-build --no-deps --detach --wait --wait-timeout 240 cv" in reconcile_log
    )
    assert "--tag sdi-jenkins-controller" not in reconcile_log
    assert reconcile_log.count("image inspect --format {{json .Config.Volumes}}") == 1


def _assert_stack_lifecycle_log(lifecycle_log: str) -> None:
    assert "compose" in lifecycle_log
    assert "build integration ci image-build cv cd" in lifecycle_log
    assert "up --no-build --detach --wait --wait-timeout 240" in lifecycle_log
    assert "image inspect --format {{json .Config.Volumes}}" in lifecycle_log
    assert "down" in lifecycle_log
    assert "down --volumes" not in lifecycle_log


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
    assert set(controller["environment"]) >= {
        "JENKINS_PIPELINE_RUN_LIMIT_SECONDS",
        "JENKINS_COMPOSITION_WORK_LIMIT_SECONDS",
        "JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS",
        "JENKINS_CV_WORK_LIMIT_SECONDS",
        "JENKINS_CD_WORK_LIMIT_SECONDS",
    }
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
    assert jenkins["globalNodeProperties"] == [
        {
            "envVars": {
                "env": [
                    {
                        "key": "JENKINS_PIPELINE_RUN_LIMIT_SECONDS",
                        "value": "${JENKINS_PIPELINE_RUN_LIMIT_SECONDS}",
                    },
                    {
                        "key": "JENKINS_COMPOSITION_WORK_LIMIT_SECONDS",
                        "value": "${JENKINS_COMPOSITION_WORK_LIMIT_SECONDS}",
                    },
                    {
                        "key": "JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS",
                        "value": "${JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS}",
                    },
                    {
                        "key": "JENKINS_CV_WORK_LIMIT_SECONDS",
                        "value": "${JENKINS_CV_WORK_LIMIT_SECONDS}",
                    },
                    {
                        "key": "JENKINS_CD_WORK_LIMIT_SECONDS",
                        "value": "${JENKINS_CD_WORK_LIMIT_SECONDS}",
                    },
                ]
            }
        }
    ]
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
    approved_signature = (
        "method org.jenkinsci.plugins.workflow.steps.FlowInterruptedException getCauses"
    )
    assert casc["security"]["scriptApproval"] == {
        "approvedSignatures": [approved_signature]
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


def test_root_pipeline_is_a_thin_scheduler_over_public_cli_operations() -> None:
    pipeline = (REPOSITORY_ROOT / "Jenkinsfile").read_text()

    assert "timeout(time: runLimitSeconds, unit: 'SECONDS')" in pipeline
    assert "timeout(time: schedulingLimitSeconds, unit: 'SECONDS')" in pipeline
    assert "timeout(time: 120, unit: 'SECONDS')" in pipeline
    assert (
        "timeout(time: definition.workLimitSeconds + 60, unit: 'SECONDS')" in pipeline
    )
    assert "--work-limit-seconds '${definition.workLimitSeconds}'" in pipeline
    assert "stageExecutionStatus == 3" in pipeline
    assert "System.currentTimeMillis() < schedulingDeadlineEpochMillis" in pipeline
    assert "TimeoutStepExecution.ExceededTimeout" in pipeline
    assert "throw interruption" in pipeline
    assert "refs/remotes/origin/sdi-protected-main" in pipeline
    assert "git merge-base --is-ancestor" in pipeline
    assert "System.currentTimeMillis() >= runDeadlineEpochMillis" in pipeline
    assert len(re.findall(r"try \{\n\s+deleteDir\(\)", pipeline)) == 4
    assert [
        (label, pipeline.count(f"label: '{label}'"))
        for label in ("composition", "image-build", "cv", "cd")
    ] == [
        ("composition", 1),
        ("image-build", 1),
        ("cv", 1),
        ("cd", 1),
    ]
    assert pipeline.count("execute-jenkins-stage") == 1
    assert "preflight-jenkins-run" in pipeline
    assert "attempt-allows-continuation" in pipeline
    assert "finalize-jenkins-run" in pipeline
    assert "--run-deadline-expired" in pipeline
    assert "bundle-conclusion" in pipeline
    assert "stash(" in pipeline
    assert "unstash(" in pipeline
    assert "archiveArtifacts(" in pipeline
    assert "deleteDir()" in pipeline
    assert "retry(" not in pipeline
    assert "readJSON" not in pipeline
    assert '"execution_conclusion"' not in pipeline
    assert '"domain_outcome"' not in pipeline
    assert "lastBuild" not in pipeline


def test_stack_validate_uses_compose_and_rejects_insecure_secret_files(  # noqa: PLR0915
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
        "JENKINS_PIPELINE_RUN_LIMIT_SECONDS=5400\n"
        "JENKINS_COMPOSITION_WORK_LIMIT_SECONDS=600\n"
        "JENKINS_IMAGE_BUILD_WORK_LIMIT_SECONDS=1800\n"
        "JENKINS_CV_WORK_LIMIT_SECONDS=2700\n"
        "JENKINS_CD_WORK_LIMIT_SECONDS=900\n"
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

    agent_secrets["cd"].unlink()
    preflight = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "preflight"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert preflight.returncode == 0, preflight.stderr
    agent_secrets["cd"].write_text("e" * 64)
    agent_secrets["cd"].chmod(0o600)

    valid_config = config.read_text()
    config.write_text(
        valid_config.replace(
            "JENKINS_PIPELINE_RUN_LIMIT_SECONDS=5400",
            "JENKINS_PIPELINE_RUN_LIMIT_SECONDS=120",
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
    assert completed.returncode == 2
    assert "must reserve at least two minutes" in completed.stderr
    config.write_text(valid_config)

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
    _assert_stack_lifecycle_log(lifecycle_log)

    forced_stop = subprocess.run(
        [JENKINS_ROOT / "bin" / "stack", "--config", config, "stop"],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "FORCE_KILL": "1",
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert forced_stop.returncode == 2
    assert "required forced termination" in forced_stop.stderr

    invalid_project = subprocess.run(
        [
            JENKINS_ROOT / "bin" / "stack",
            "--project-name",
            "INVALID",
            "--config",
            config,
            "validate",
        ],
        check=False,
        capture_output=True,
        env={
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        text=True,
    )
    assert invalid_project.returncode == 2
    assert "project name must start" in invalid_project.stderr

    _assert_single_agent_reconciliation(
        config,
        fake_bin,
        docker_log,
        agent_secrets["ci"],
    )

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
