"""Black-box checks for the implemented composition Stage in a local run.

The composition adapter calls a real model. These tests assert the run's
contract, never which services the model chose or how it worded them.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from tests._live_model import REPOSITORY_ROOT, live_config, openai_key
from tests._proposals import COMPLETE
from tests._stub_provider import (
    BODIES,
    STUB_KEY,
    proposals_body,
    stub_config,
    stub_provider,
)

if TYPE_CHECKING:
    from pathlib import Path

RUN_REQUEST_PATH = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
COMMITTED_FILES = (
    RUN_REQUEST_PATH,
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    "deployment/jenkins/adapters/composition-v1.yaml",
    "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    "deployment/jenkins/adapters/cv-fixture-v1.yaml",
    "deployment/jenkins/adapters/cd-fixture-v1.yaml",
    "integration/generation-configs/luna.yaml",
    "integration/generation-configs/qwen.yaml",
)
LUNA = "integration/generation-configs/luna.yaml"
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
KEY_NAMES = ("OPENAI_API_KEY", "VLLM_KEY")
NO_RESULT_CODES = {
    "sdi.composition.bounded-no-result",
    "sdi.composition.scoped-rejection",
    "sdi.composition.insufficient-proposals",
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, extra: dict[str, str] | None = None) -> str:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.test")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example/sdi-composition.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPOSITORY_ROOT / relative_path).read_bytes())
    for relative_path, content in (extra or {}).items():
        (repository / relative_path).write_text(content)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add composition inputs")
    return _git(repository, "rev-parse", "HEAD")


def _dispatch(
    tmp_path: Path,
    commit_sha: str,
    *,
    generation_config: str = LUNA,
    service_repository: Path = SERVICE_REPOSITORY,
    keys: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value for name, value in os.environ.items() if name not in KEY_NAMES
    }
    return subprocess.run(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(tmp_path / "repository"),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            RUN_REQUEST_PATH,
            "--service-repository",
            str(service_repository),
            "--generation-config",
            generation_config,
            "--bundle-root",
            str(tmp_path / "bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment | (keys or {}),
    )


def _dispatch_stub(
    tmp_path: Path, body: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    with stub_provider(body) as base_url:
        commit_sha = _repository(
            tmp_path,
            {"integration/generation-configs/stub.yaml": stub_config(base_url)},
        )
        return _dispatch(
            tmp_path,
            commit_sha,
            generation_config="integration/generation-configs/stub.yaml",
            keys={"VLLM_KEY": STUB_KEY},
        )


def _composition_files(result: dict[str, object]) -> set[str]:
    artifacts = cast("list[dict[str, str]]", result["artifacts"])
    return {item["slot"] for item in artifacts if item["stage"] == "composition"}


def _bundle_text(tmp_path: Path) -> str:
    return "".join(
        path.read_text()
        for path in sorted((tmp_path / "bundle").rglob("*"))
        if path.is_file()
    )


def _validate_bundle(tmp_path: Path) -> None:
    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(tmp_path / "bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


def test_composes_the_waffle_orin_run_with_a_real_model(tmp_path: Path) -> None:
    config, key_name = live_config()
    api_key = os.environ.get(key_name) or openai_key()
    if api_key is None:
        pytest.skip("neither the Qwen endpoint nor an OpenAI key is available")
    commit_sha = _repository(tmp_path)

    completed = _dispatch(
        tmp_path,
        commit_sha,
        generation_config=f"integration/generation-configs/{config.name}",
        keys={key_name: api_key},
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, cv, cd = result["attempts"]
    assert composition["implementation_mode"] == "implemented"
    assert composition["execution_conclusion"] == "succeeded"
    if composition["domain_outcome"] == "succeeded":
        assert composition["reason"]["code"] == "sdi.composition.preferred-proposal"
        assert _composition_files(result) == {
            "composition_blueprint",
            "deployment_schema",
            "composition_evidence",
            "diagnostic",
        }
        assert image_build["reason"]["code"] == "sdi.dependency.implemented-evidence"
    else:
        assert composition["domain_outcome"] == "failed"
        assert composition["reason"]["code"] in NO_RESULT_CODES
        assert _composition_files(result) == {"composition_evidence", "diagnostic"}
        assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    assert image_build["lifecycle_state"] == "skipped"
    assert [cv["lifecycle_state"], cd["lifecycle_state"]] == ["skipped", "skipped"]
    _validate_bundle(tmp_path)
    assert api_key not in _bundle_text(tmp_path) + completed.stdout + completed.stderr


def test_exhausted_output_is_a_bounded_domain_failure(tmp_path: Path) -> None:
    api_key = openai_key()
    if api_key is None:
        pytest.skip("no OpenAI key is available")
    bounded = (
        (REPOSITORY_ROOT / LUNA)
        .read_text()
        .replace("max_output_tokens: 32000", "max_output_tokens: 16")
    )
    commit_sha = _repository(
        tmp_path, {"integration/generation-configs/bounded.yaml": bounded}
    )

    completed = _dispatch(
        tmp_path,
        commit_sha,
        generation_config="integration/generation-configs/bounded.yaml",
        keys={"OPENAI_API_KEY": api_key},
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition = result["attempts"][0]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "failed"
    assert composition["reason"]["code"] == "sdi.composition.bounded-no-result"
    assert _composition_files(result) == {"composition_evidence", "diagnostic"}
    evidence = json.loads(
        (tmp_path / "bundle/stages/composition/composition-evidence.json").read_text()
    )
    assert evidence["generation"]["outcome"] == "exhausted"
    assert evidence["generation"]["max_output_tokens"] == 16
    _validate_bundle(tmp_path)
    assert api_key not in _bundle_text(tmp_path)


def test_a_rejected_key_fails_execution_without_outputs(tmp_path: Path) -> None:
    api_key = "sk-test-only-invalid"
    commit_sha = _repository(tmp_path)

    completed = _dispatch(tmp_path, commit_sha, keys={"OPENAI_API_KEY": api_key})

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "failed"
    assert composition["domain_outcome"] == "not_evaluated"
    assert composition["reason"]["code"] == "sdi.composition.provider-error"
    assert _composition_files(result) == {"diagnostic"}
    assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    _validate_bundle(tmp_path)
    assert api_key not in _bundle_text(tmp_path) + completed.stdout


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("refusal", "sdi.composition.generation-refused"),
        ("off-schema", "sdi.composition.generation-invalid"),
    ],
)
def test_an_answer_without_proposals_fails_execution(
    tmp_path: Path, body: str, code: str
) -> None:
    completed = _dispatch_stub(tmp_path, BODIES[body])

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    composition = result["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["domain_outcome"] == "not_evaluated"
    assert composition["reason"]["code"] == code
    assert _composition_files(result) == {"diagnostic"}
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_publishes_the_preferred_stubbed_proposal(tmp_path: Path) -> None:
    completed = _dispatch_stub(tmp_path, proposals_body([COMPLETE]))

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "succeeded"
    assert composition["reason"]["code"] == "sdi.composition.preferred-proposal"
    assert _composition_files(result) == {
        "composition_blueprint",
        "deployment_schema",
        "composition_evidence",
        "diagnostic",
    }
    assert image_build["reason"]["code"] == "sdi.dependency.implemented-evidence"
    blueprint = json.loads(
        (tmp_path / "bundle/stages/composition/composition-blueprint.json").read_text()
    )
    assert {item["service_id"]: item["basis"] for item in blueprint["services"]} == {
        item["service_id"]: (
            "placeholder"
            if "basis: placeholder"
            in (SERVICE_REPOSITORY / item["service_id"] / "SDI.md").read_text()
            else "declared"
        )
        for item in COMPLETE["services"]
    }
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_publishes_only_evidence_without_an_eligible_proposal(
    tmp_path: Path,
) -> None:
    ineligible = copy.deepcopy(COMPLETE)
    ineligible["coverage"][0] |= {"status": "missing", "services": ["v4l2-camera"]}

    completed = _dispatch_stub(tmp_path, proposals_body([ineligible]))

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "failed"
    assert composition["reason"]["code"] == "sdi.composition.insufficient-proposals"
    assert _composition_files(result) == {"composition_evidence", "diagnostic"}
    assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_an_invalid_description_fails_before_generation(tmp_path: Path) -> None:
    service_repository = tmp_path / "service-repository"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    (service_repository / "broken").mkdir()
    (service_repository / "broken/SDI.md").write_text(
        "---\nschema_version: sdi.service-description/v1\nservice_id: broken\n---\n"
    )
    commit_sha = _repository(tmp_path)

    completed = _dispatch(tmp_path, commit_sha, service_repository=service_repository)

    assert completed.returncode == 1, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["reason"]["code"] == (
        "sdi.composition.invalid-service-description"
    )


@pytest.mark.parametrize("defect", ["symlinked-description", "unbound-key"])
def test_rejects_unsafe_composition_inputs_before_launch(
    tmp_path: Path, defect: str
) -> None:
    service_repository = tmp_path / "service-repository"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    extra: dict[str, str] = {}
    generation_config = LUNA
    if defect == "symlinked-description":
        (service_repository / "linked").mkdir()
        (service_repository / "linked/SDI.md").symlink_to(
            service_repository / "nav2-navigation/SDI.md"
        )
    else:
        extra["integration/generation-configs/other.yaml"] = (
            (REPOSITORY_ROOT / LUNA)
            .read_text()
            .replace("api_key_env: OPENAI_API_KEY", "api_key_env: OTHER_KEY")
        )
        generation_config = "integration/generation-configs/other.yaml"
    commit_sha = _repository(tmp_path, extra)

    completed = _dispatch(
        tmp_path,
        commit_sha,
        generation_config=generation_config,
        service_repository=service_repository,
        keys={"OTHER_KEY": "sk-test-only-other"},
    )

    assert completed.returncode == 2
    assert not (tmp_path / "bundle").exists()
