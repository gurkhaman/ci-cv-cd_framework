"""Choice of the real model endpoint used by the live tests."""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

from sdi_pipeline_integration._yaml_input import parse_yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
CONFIGS = REPOSITORY_ROOT / "integration/generation-configs"


def openai_key() -> str | None:
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
    config = parse_yaml((CONFIGS / "qwen.yaml").read_bytes(), "qwen.yaml")
    health = f"{str(config['base_url']).removesuffix('/v1')}/health"
    try:
        with urllib.request.urlopen(health, timeout=5) as response:  # noqa: S310
            return response.status == 200
    except OSError:
        return False


def live_config() -> tuple[Path, str]:
    """Pick Qwen while its endpoint answers, else luna; return config, key name."""
    if os.environ.get("VLLM_KEY") and _qwen_answers():
        return CONFIGS / "qwen.yaml", "VLLM_KEY"
    return CONFIGS / "luna.yaml", "OPENAI_API_KEY"
