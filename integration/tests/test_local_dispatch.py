"""Black-box checks for one complete local Pipeline integration run."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
RUN_REQUEST_PATH = "runs/s-04/s-04-tc-03-c-01-fixture.yaml"
STAGES = ["composition", "image_build", "cv", "cd"]
COMMITTED_FILES = (
    RUN_REQUEST_PATH,
    "requirements/s-04/deliver-book-to-joe.yaml",
    "profiles/s-04/waffle-native-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    "deployment/jenkins/adapters/composition-fixture-v1.yaml",
    "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    "deployment/jenkins/adapters/cv-fixture-v1.yaml",
    "deployment/jenkins/adapters/cd-fixture-v1.yaml",
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_fixture_repository(tmp_path: Path) -> tuple[Path, str]:
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
        "https://github.com/example/sdi-fixture.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SOURCE_ROOT / relative_path).read_bytes())
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add four-Stage Fixture inputs")
    return repository, _git(repository, "rev-parse", "HEAD")


def _dispatch(
    repository: Path, commit_sha: str, bundle_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            RUN_REQUEST_PATH,
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_dispatches_a_contract_valid_deterministic_four_stage_fixture(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    first_root = tmp_path / "first-bundle"
    second_root = tmp_path / "second-bundle"

    first = _dispatch(repository, commit_sha, first_root)
    second = _dispatch(repository, commit_sha, second_root)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result == json.loads(
        (first_root / "pipeline-integration-result.json").read_text()
    )
    assert first_result["execution_id"] != second_result["execution_id"]
    assert first_result["kpi_evaluation"] == "not_evaluated"
    assert "domain_outcome" not in first_result
    assert "overall_verdict" not in first_result
    assert [
        attempt["correlation"]["stage"] for attempt in first_result["attempts"]
    ] == (STAGES)
    assert all(
        attempt["correlation"]["execution_id"] == first_result["execution_id"]
        and attempt["correlation"]["attempt_number"] == 1
        and attempt["lifecycle_state"] == "completed"
        and attempt["execution_conclusion"] == "succeeded"
        and attempt["implementation_mode"] == "fixture"
        and attempt["domain_outcome"] == "not_evaluated"
        for attempt in first_result["attempts"]
    )
    assert [
        [item["slot"] for item in attempt["accepted_inputs"]]
        for attempt in first_result["attempts"]
    ] == [
        ["run_request", "requirements_specification", "target_profile"],
        ["target_profile", "composition_blueprint", "deployment_schema"],
        [
            "requirements_specification",
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
        ],
        [
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
            "validation_evidence",
        ],
    ]

    expected_paths = {
        "pipeline-integration-result.json",
        *(artifact["path"] for artifact in first_result["artifacts"]),
    }
    actual_paths = {
        path.relative_to(first_root).as_posix()
        for path in first_root.rglob("*")
        if path.is_file()
    }
    assert actual_paths == expected_paths
    assert len(first_result["artifacts"]) == 9
    for artifact in first_result["artifacts"]:
        first_bytes = (first_root / artifact["path"]).read_bytes()
        second_bytes = (second_root / artifact["path"]).read_bytes()
        assert first_bytes == second_bytes
        assert len(first_bytes) == artifact["byte_size"]
        assert hashlib.sha256(first_bytes).hexdigest() == artifact["sha256"]

    for bundle_root in (first_root, second_root):
        validated = subprocess.run(
            [
                "sdi-integration",
                "validate-bundle",
                "--bundle-root",
                str(bundle_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert validated.returncode == 0, validated.stderr
        assert validated.stdout == ""


def test_bundle_validation_rejects_tampered_artifact_bytes(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result = json.loads(dispatched.stdout)
    artifact_path = bundle_root / result["artifacts"][0]["path"]
    artifact_path.write_bytes(b"tampered\n")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""
    assert "artifact" in validated.stderr.lower()


def test_bundle_validation_rejects_unbounded_artifact_grants(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][0]["accepted_files"][0]["byte_size"] = 32769
    result["artifacts"][0]["byte_size"] = 32769
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "less than or equal to 32768" in validated.stderr


def test_bundle_validation_rejects_a_stage_output_under_the_wrong_schema(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][1]["accepted_files"][0]["schema_version"] = (
        "sdi.deployment-result/v1"
    )
    result["artifacts"][3]["schema_version"] = "sdi.deployment-result/v1"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "reviewed profile" in validated.stderr


def test_bundle_validation_rechecks_cross_stage_domain_correlation(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item for item in result["artifacts"] if item["slot"] == "deployment_result"
    )
    domain_path = bundle_root / artifact["path"]
    domain_output = json.loads(domain_path.read_text())
    domain_output["profile_id"] = "another-profile"
    changed_bytes = json.dumps(domain_output).encode()
    domain_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][3]["accepted_files"][0]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "correlation" in validated.stderr.lower()


def test_bundle_validation_rejects_ascii_delete_in_diagnostics(tmp_path: Path) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = _dispatch(repository, commit_sha, bundle_root)
    assert dispatched.returncode == 0, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item
        for item in result["artifacts"]
        if item["stage"] == "composition" and item["role"] == "diagnostic"
    )
    diagnostic_path = bundle_root / artifact["path"]
    changed_bytes = diagnostic_path.read_bytes() + b"\x7f"
    diagnostic_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][0]["accepted_files"][2]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "sanitized" in validated.stderr.lower()
