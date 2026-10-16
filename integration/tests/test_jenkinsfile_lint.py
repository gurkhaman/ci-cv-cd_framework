"""Catch Jenkinsfile mistakes that Jenkins reports only when a run reaches them."""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from sdi_pipeline_integration._cli import main

SOURCE_ROOT = Path(__file__).resolve().parents[2]
JENKINSFILE = SOURCE_ROOT / "Jenkinsfile"
SCM_TOKEN = re.compile(r"\bscm\b")
OPERATION_CALL = re.compile(r"\bsdi-integration\s+([a-z][a-z0-9-]*)")


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
