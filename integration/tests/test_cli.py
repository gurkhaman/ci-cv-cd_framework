"""Black-box checks for the installed command-line interface."""

from __future__ import annotations

import json
import subprocess
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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


def test_cli_writes_and_freshness_checks_contract_schemas(tmp_path: Path) -> None:
    write = subprocess.run(
        [
            "sdi-integration",
            "schemas",
            "--write",
            "--directory",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert write.returncode == 0, write.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "mobility-requirements-specification-v1.schema.json",
        "pipeline-integration-run-request-v1.schema.json",
        "target-execution-profile-v1.schema.json",
    ]

    check = subprocess.run(
        [
            "sdi-integration",
            "schemas",
            "--check",
            "--directory",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert check.returncode == 0, check.stderr

    run_schema = json.loads(
        (tmp_path / "pipeline-integration-run-request-v1.schema.json").read_text()
    )
    path_pattern = run_schema["properties"]["requirements_specification"]["pattern"]
    assert "[A-Za-z]:/" in path_pattern

    profile_schema_path = tmp_path / "target-execution-profile-v1.schema.json"
    profile_schema = json.loads(profile_schema_path.read_text())
    resources = profile_schema["$defs"]["ResourceCapacity"]
    assert resources["minProperties"] == 1
    assert profile_schema["$defs"]["NetworkLink"]["anyOf"] == [
        {"required": ["latency_ms"]},
        {"required": ["bandwidth_mbps"]},
    ]
    assert profile_schema["properties"]["network_links"]["minItems"] == 1
    profile_schema_text = profile_schema_path.read_text()
    assert '"type": "null"' not in profile_schema_text
    assert '"exclusiveMinimum": 0' in profile_schema_text
    assert '"minimum": 0' in profile_schema_text
    assert '"gt":' not in profile_schema_text
    assert '"ge":' not in profile_schema_text
