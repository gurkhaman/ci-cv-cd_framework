"""Public-boundary checks for model-generated composition proposals.

These call the real OpenAI API. They assert the command's contract, never what
the model chose or how it worded it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"
INPUTS = [
    "--service-repository",
    str(SERVICE_REPOSITORY),
    "--target-profile",
    str(PROFILE),
    "--requirements-specification",
    str(REQUIREMENTS),
]


def _api_key() -> str | None:
    """Read the key from the environment, else from the repository's .env."""
    if key := os.environ.get("OPENAI_API_KEY"):
        return key
    dotenv = REPOSITORY_ROOT / ".env"
    if not dotenv.is_file():
        return None
    for line in dotenv.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "OPENAI_API_KEY":
            return value.strip().strip("\"'") or None
    return None


def _generate(tmp_path: Path, api_key: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "generate-proposals",
            *INPUTS,
            "--output",
            str(tmp_path / "proposals.json"),
            "--evidence",
            str(tmp_path / "evidence.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"OPENAI_API_KEY": api_key},
    )


def test_generates_proposals_the_assessment_accepts(tmp_path: Path) -> None:
    api_key = _api_key()
    if api_key is None:
        pytest.skip("OPENAI_API_KEY is not set and the repository has no .env")

    completed = _generate(tmp_path, api_key)

    assert completed.returncode == 0, completed.stderr
    proposals = json.loads((tmp_path / "proposals.json").read_text())
    assert 1 <= len(proposals["proposals"]) <= 3
    assessed = subprocess.run(
        [
            "sdi-integration",
            "assess-proposals",
            *INPUTS,
            "--proposals",
            str(tmp_path / "proposals.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert assessed.returncode == 0, assessed.stderr
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "generated"
    assert evidence["request"]["model"] == "gpt-6-luna"
    assert evidence["request"]["text"]["format"]["strict"] is True
    assert evidence["response"]["status"] == "completed"
    assert api_key not in evidence_text + completed.stdout + completed.stderr


def test_provider_error_writes_no_proposals(tmp_path: Path) -> None:
    api_key = "sk-test-only-invalid"
    completed = _generate(tmp_path, api_key)

    assert completed.returncode == 1, completed.stderr
    assert "provider_error" in completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "provider_error"
    assert evidence["error"]["status_code"] == 401
    assert api_key not in evidence_text + completed.stdout + completed.stderr
