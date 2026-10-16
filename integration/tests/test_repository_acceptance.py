"""Black-box checks for the single repository acceptance entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
VERIFY = REPOSITORY_ROOT / "integration" / "scripts" / "verify"


def _acceptance_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    scripts = checkout / "integration" / "scripts"
    plugins = checkout / "deployment" / "jenkins" / "controller"
    stack = checkout / "deployment" / "jenkins" / "bin" / "stack"
    scripts.mkdir(parents=True)
    plugins.mkdir(parents=True)
    stack.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VERIFY, scripts / "verify")
    (checkout / "deployment" / "installation.env").write_text(
        "SDI_GITHUB_RUNNER_VERSION=2.337.0\n",
        encoding="utf-8",
    )
    (plugins / "plugins.txt").write_text(
        "workflow-cps:4378.v7a_08f1b_b_f8f4\n",
        encoding="utf-8",
    )
    stack.write_text(
        '#!/bin/sh\nprintf \'stack %s\\n\' "$*" >> "$ACCEPTANCE_LOG"\n',
        encoding="utf-8",
    )
    stack.chmod(0o755)
    return checkout, scripts / "verify"


def _fake_tools(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf \'docker %s\\n\' "$*" >> "$ACCEPTANCE_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "${1-}" = --version ]; then\n'
        "  printf '%s\\n' 'uv 0.12.1 (x86_64-unknown-linux-gnu)'\n"
        "  exit 0\n"
        "fi\n"
        'printf \'uv %s\\n\' "$*" >> "$ACCEPTANCE_LOG"\n'
        '[ "$*" != "${FAIL_ACCEPTANCE_COMMAND-}" ]\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return fake_bin


def _environment(tmp_path: Path) -> dict[str, str]:
    return os.environ | {
        "ACCEPTANCE_LOG": str(tmp_path / "acceptance.log"),
        "PATH": f"{_fake_tools(tmp_path)}:{os.environ['PATH']}",
        "SDI_HOST_LOCK": str(tmp_path / "host.lock"),
    }


def _git(checkout: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
        check=True,
        capture_output=True,
        cwd=checkout,
    )


def test_verification_is_the_complete_fail_fast_acceptance_gate(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    log = tmp_path / "acceptance.log"
    environment = _environment(tmp_path)

    completed = subprocess.run(
        [verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    passed_commands = log.read_text(encoding="utf-8").splitlines()
    assert passed_commands == [
        "uv lock --check",
        "uv sync --frozen --all-groups",
        "uv run --frozen sdi-integration schemas --check --directory schemas",
        (
            "uv run --frozen sdi-integration stage-profiles --check "
            "--directory stage-profiles"
        ),
        (
            "uv run --frozen python -m "
            "sdi_pipeline_integration._fixture_regeneration --check"
        ),
        (
            "uv run --frozen check-jsonschema --quiet --builtin-schema "
            "vendor.github-workflows ../.github/workflows/pipeline-integration.yml"
        ),
        "uv run --frozen ruff format --check .",
        "uv run --frozen ruff check .",
        "uv run --frozen basedpyright",
        "uv run --frozen pytest -m not slow -n auto --dist worksteal",
        "uv run --frozen pytest -m slow",
        "uv tree --frozen --all-groups",
        "stack smoke",
    ]
    assert "uv 0.12.1" in completed.stdout
    assert "SDI_GITHUB_RUNNER_VERSION=2.337.0" in completed.stdout
    assert "workflow-cps:4378.v7a_08f1b_b_f8f4" in completed.stdout
    assert (
        "Repository acceptance passed.\n"
        "Passing proves the integration scaffold, the Fixture behavior of the "
        "image-build, CV, and CD Stages, and the composition Stage contract: "
        "outcomes, outputs, evidence, and diagnostics.\n"
        "Live-model composition tests are skipped when their provider keys are "
        "unavailable.\n"
        "Passing proves no real image build, CV verdict, deployment, or KPI "
        "evaluation; product security assurance, availability, and deferred "
        "infrastructure remain out of scope.\n"
    ) in completed.stdout

    failed = subprocess.run(
        [verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=environment | {"FAIL_ACCEPTANCE_COMMAND": "run --frozen basedpyright"},
        text=True,
    )

    assert failed.returncode != 0
    assert not any(
        line.startswith("uv run --frozen pytest")
        for line in log.read_text(encoding="utf-8").splitlines()[len(passed_commands) :]
    )
    assert "Repository acceptance passed." not in failed.stdout


def test_fast_tier_skips_the_slow_tests_and_the_stack_smoke(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)

    completed = subprocess.run(
        [verify, "--fast"],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=_environment(tmp_path),
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = (tmp_path / "acceptance.log").read_text(encoding="utf-8").splitlines()
    assert commands[-1] == "uv run --frozen pytest -m not slow -n auto --dist worksteal"
    assert "uv run --frozen pytest -m slow" not in commands
    assert not any(line.startswith(("stack", "docker")) for line in commands)
    assert completed.stdout.endswith(
        "fast tier passed; run integration/scripts/verify before opening a PR\n"
    )


@pytest.mark.parametrize(
    ("changed_path", "smoke_runs"),
    [
        ("integration/src/sdi_pipeline_integration/changed.py", True),
        ("integration/tests/test_changed.py", False),
    ],
)
def test_stack_smoke_runs_only_when_a_smoke_input_changed(
    tmp_path: Path, *, changed_path: str, smoke_runs: bool
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    _git(checkout, "init", "--quiet")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "--quiet", "--message", "base")
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    changed = checkout / changed_path
    changed.parent.mkdir(parents=True)
    changed.write_text("change = 1\n", encoding="utf-8")
    _git(checkout, "add", changed_path)

    completed = subprocess.run(
        [verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=_environment(tmp_path),
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = (tmp_path / "acceptance.log").read_text(encoding="utf-8").splitlines()
    assert ("stack smoke" in commands) is smoke_runs
    assert (
        "stack smoke skipped: no smoke inputs changed" in completed.stdout
    ) is not smoke_runs


def test_a_caller_holding_the_host_lock_does_not_deadlock_verification(
    tmp_path: Path,
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    environment = _environment(tmp_path)

    completed = subprocess.run(
        ["flock", environment["SDI_HOST_LOCK"], verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "waiting for" not in completed.stdout
    assert "stack smoke" in (tmp_path / "acceptance.log").read_text(encoding="utf-8")
