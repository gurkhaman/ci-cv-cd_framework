"""Public-boundary checks for model-generated composition proposals.

These call a real model: the self-hosted Qwen endpoint while it answers, else
OpenAI's luna. They assert the command's contract, never what the model chose
or how it worded it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests._live_model import CONFIGS, REPOSITORY_ROOT, live_config, openai_key

if TYPE_CHECKING:
    from pathlib import Path

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


def _generate(
    tmp_path: Path, config: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "generate-proposals",
            *INPUTS,
            "--generation-config",
            str(config),
            "--output",
            str(tmp_path / "proposals.json"),
            "--evidence",
            str(tmp_path / "evidence.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | env,
    )


def test_generates_proposals_the_assessment_accepts(tmp_path: Path) -> None:
    config, key_name = live_config()
    api_key = os.environ.get(key_name) or openai_key()
    if api_key is None:
        pytest.skip("neither the Qwen endpoint nor an OpenAI key is available")

    completed = _generate(tmp_path, config, {key_name: api_key})

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
    assert evidence["request"]["model"] == evidence["settings"]["model"]
    assert evidence["request"]["text"]["format"]["strict"] is True
    assert evidence["response"]["status"] == "completed"
    assert api_key not in evidence_text + completed.stdout + completed.stderr


def test_provider_error_writes_no_proposals(tmp_path: Path) -> None:
    config, key_name = live_config()
    api_key = "sk-test-only-invalid"
    completed = _generate(tmp_path, config, {key_name: api_key})

    assert completed.returncode == 1, completed.stderr
    assert "provider_error" in completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "provider_error"
    assert evidence["error"]["status_code"] == 401
    assert api_key not in evidence_text + completed.stdout + completed.stderr


def test_rejects_an_unknown_generation_setting(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"{(CONFIGS / 'luna.yaml').read_text()}temperature: 0\n")

    completed = _generate(tmp_path, config, {})

    assert completed.returncode == 2
    assert "temperature" in completed.stderr
    assert not (tmp_path / "evidence.json").exists()
