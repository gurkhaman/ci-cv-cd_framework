"""Catch Jenkinsfile mistakes that Jenkins reports only when a run reaches them."""

from __future__ import annotations

import contextlib
import io
import re
import shutil
from pathlib import Path

from sdi_pipeline_integration._cli import main
from sdi_pipeline_integration._jenkins_pipeline import (
    STAGE_WIRING_BEGIN,
    STAGE_WIRING_END,
    WRITE_STAGE_WIRING_COMMAND,
)
from sdi_pipeline_integration._stage_registry import AGENTS, STAGES

SOURCE_ROOT = Path(__file__).resolve().parents[2]
JENKINSFILE = SOURCE_ROOT / "Jenkinsfile"
SCM_TOKEN = re.compile(r"\bscm\b")
OPERATION_CALL = re.compile(r"\bsdi-integration\s+([a-z][a-z0-9-]*)")
STAGE_FACT = re.compile(
    r"adapters/|(?<![\w-])(?:"
    + "|".join(sorted({*STAGES, *(agent.label for agent in AGENTS)}))
    + r")(?![\w-])"
)
PRODUCT_NAME = "Pipeline integration"


def code_lines(jenkinsfile: str) -> list[tuple[int, str]]:
    return [
        (number, line)
        for number, line in enumerate(jenkinsfile.splitlines(), start=1)
        if not line.lstrip().startswith("//")
    ]


def scm_failures(jenkinsfile: str) -> list[str]:
    return [
        f"Jenkinsfile line {number}: scm is not bound on this installation; select"
        " the repository by env.JOB_NAME as checkoutSubmittedCommit does"
        for number, line in code_lines(jenkinsfile)
        if SCM_TOKEN.search(line)
    ]


def cli_defines(operation: str) -> bool:
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        try:
            main([operation, "--help"])
        except SystemExit as exited:
            return exited.code == 0
    return True


def undefined_operation_failures(jenkinsfile: str) -> list[str]:
    return [
        f"Jenkinsfile line {number}: sdi-integration does not define operation"
        f" {operation}"
        for number, line in code_lines(jenkinsfile)
        for operation in OPERATION_CALL.findall(line)
        if not cli_defines(operation)
    ]


def hand_written_lines(jenkinsfile: str) -> list[tuple[int, str]]:
    lines = jenkinsfile.splitlines()
    begin = lines.index(STAGE_WIRING_BEGIN) + 1
    end = lines.index(STAGE_WIRING_END) + 1
    return [(n, line) for n, line in code_lines(jenkinsfile) if not begin < n < end]


def stage_fact_failures(jenkinsfile: str) -> list[str]:
    return [
        f"Jenkinsfile line {number}: hand-written Groovy names {fact.group()!r};"
        " Stage facts belong in _stage_registry.STAGES and reach Groovy as"
        " definition.<field> from the generated Stage wiring block"
        for number, line in hand_written_lines(jenkinsfile)
        for fact in STAGE_FACT.finditer(line.replace(PRODUCT_NAME, ""))
    ]


def check_stage_wiring(jenkinsfile: Path) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        status = main(["jenkinsfile", "--check", "--jenkinsfile", str(jenkinsfile)])
    return status, stderr.getvalue()


def test_committed_jenkinsfile_does_not_use_scm() -> None:
    failures = scm_failures(JENKINSFILE.read_text(encoding="utf-8"))
    assert not failures, "\n".join(failures)


def test_committed_jenkinsfile_calls_only_defined_operations() -> None:
    jenkinsfile = JENKINSFILE.read_text(encoding="utf-8")
    assert OPERATION_CALL.search(jenkinsfile)
    failures = undefined_operation_failures(jenkinsfile)
    assert not failures, "\n".join(failures)


def test_checkout_scm_fails_but_a_comment_naming_scm_passes() -> None:
    jenkinsfile = """\
// This installation does not bind the scm variable.
def checkoutSubmittedCommit = {
    checkout scm
}
"""
    assert scm_failures(jenkinsfile) == [
        (
            "Jenkinsfile line 3: scm is not bound on this installation; select the"
            " repository by env.JOB_NAME as checkoutSubmittedCommit does"
        )
    ]


def test_undefined_operation_fails_with_its_name() -> None:
    jenkinsfile = """\
sh '''sdi-integration finalize-jenkins-run \\
  --bundle-root bundle
sdi-integration finalise-jenkins-run --bundle-root bundle
'''
"""
    assert undefined_operation_failures(jenkinsfile) == [
        (
            "Jenkinsfile line 3: sdi-integration does not define operation"
            " finalise-jenkins-run"
        )
    ]


def test_committed_stage_wiring_matches_the_registry() -> None:
    status, stderr = check_stage_wiring(JENKINSFILE)
    assert status == 0, stderr


def test_stale_stage_wiring_fails_with_the_write_command(tmp_path: Path) -> None:
    jenkinsfile = tmp_path / "Jenkinsfile"
    committed = JENKINSFILE.read_text(encoding="utf-8")
    jenkinsfile.write_text(
        committed.replace("safetyTimeoutSeconds: 660", "safetyTimeoutSeconds: 600"),
        encoding="utf-8",
    )

    assert check_stage_wiring(jenkinsfile) == (
        2,
        (
            "sdi-integration: Jenkinsfile Stage wiring is stale; run from the"
            f" repository root: {WRITE_STAGE_WIRING_COMMAND}\n"
        ),
    )
    assert main(["jenkinsfile", "--write", "--jenkinsfile", str(jenkinsfile)]) == 0
    assert jenkinsfile.read_text(encoding="utf-8") == committed


def test_jenkinsfile_without_the_wiring_markers_fails(tmp_path: Path) -> None:
    jenkinsfile = tmp_path / "Jenkinsfile"
    shutil.copyfile(JENKINSFILE, jenkinsfile)
    jenkinsfile.write_text(
        jenkinsfile.read_text(encoding="utf-8").replace(STAGE_WIRING_END, ""),
        encoding="utf-8",
    )

    status, stderr = check_stage_wiring(jenkinsfile)

    assert status == 2
    assert STAGE_WIRING_END in stderr


def test_hand_written_groovy_names_no_stage_fact() -> None:
    failures = stage_fact_failures(JENKINSFILE.read_text(encoding="utf-8"))
    assert not failures, "\n".join(failures)


def test_stage_facts_outside_the_generated_block_fail_with_their_line() -> None:
    jenkinsfile = f"""\
{STAGE_WIRING_BEGIN}
def stages = [[stage: 'cv', label: 'cv']]
{STAGE_WIRING_END}
// The composition Stage binds its credential.
sh 'sdi-integration execute-jenkins-stage'
def digest = 'cat /etc/sdi/integration-code-digest'
def job = 'pipeline-integration'
error('Pipeline integration bundle validation failed')
node('integration') {{
    if (definition.stage == 'composition') {{
        echo resourceNodes.image_build
        sh "--descriptor-path deployment/jenkins/adapters/cv-v1.yaml"
    }}
}}
"""
    assert stage_fact_failures(jenkinsfile) == [
        f"Jenkinsfile line {number}: hand-written Groovy names {fact!r}; Stage facts"
        " belong in _stage_registry.STAGES and reach Groovy as definition.<field>"
        " from the generated Stage wiring block"
        for number, fact in (
            (9, "integration"),
            (10, "composition"),
            (11, "image_build"),
            (12, "adapters/"),
        )
    ]
