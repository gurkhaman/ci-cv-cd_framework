"""Public-boundary checks for model-generated composition proposals.

These call a real model: the self-hosted Qwen endpoint while it answers, else
OpenAI's luna. They assert the command's contract, never what the model chose
or how it worded it.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"
CONFIGS = REPOSITORY_ROOT / "integration/generation-configs"
INPUTS = [
    "--service-repository",
    str(SERVICE_REPOSITORY),
    "--target-profile",
    str(PROFILE),
    "--requirements-specification",
    str(REQUIREMENTS),
]


def _openai_key() -> str | None:
    """Read the OpenAI key from the environment, else from the repository's .env."""
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


def _qwen_answers() -> bool:
    config = (CONFIGS / "qwen.yaml").read_text()
    base_url = next(
        line.partition(":")[2].strip()
        for line in config.splitlines()
        if line.startswith("base_url:")
    )
    health = f"{base_url.removesuffix('/v1')}/health"
    try:
        with urllib.request.urlopen(health, timeout=5) as response:  # noqa: S310
            return response.status == 200
    except OSError:
        return False


def _live_config() -> tuple[Path, str]:
    """Pick Qwen while its endpoint answers, else luna; return config, key name."""
    if os.environ.get("VLLM_KEY") and _qwen_answers():
        return CONFIGS / "qwen.yaml", "VLLM_KEY"
    return CONFIGS / "luna.yaml", "OPENAI_API_KEY"


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
    config, key_name = _live_config()
    api_key = os.environ.get(key_name) or _openai_key()
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
    config, key_name = _live_config()
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
