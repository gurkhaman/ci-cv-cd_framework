"""Black-box checks for the installed command-line interface."""

from __future__ import annotations

import subprocess
from importlib.metadata import version


def test_installed_cli_reports_distribution_version() -> None:
    completed = subprocess.run(
        ["sdi-integration", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    distribution_version = version("sdi-pipeline-integration")
    assert completed.stdout == f"sdi-integration {distribution_version}\n"


def test_installed_cli_exposes_help() -> None:
    completed = subprocess.run(
        ["sdi-integration", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.startswith("usage: sdi-integration")
