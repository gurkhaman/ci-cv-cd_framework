"""Public-boundary checks for model-generated composition proposals.

These call a real model: the self-hosted Qwen endpoint while it answers, else
OpenAI's luna. They assert the command's contract, never what the model chose
or how it worded it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests._live_model import CONFIGS, REPOSITORY_ROOT, live_config, openai_key
from tests._proposals import COMPLETE
from tests._stub_provider import (
    BODIES,
    STUB_KEY,
    proposals_body,
    stub_config,
    stub_embeddings,
    stub_provider,
)

if TYPE_CHECKING:
    from pathlib import Path

SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"
INPUTS = [
    "--target-profile",
    str(PROFILE),
    "--requirements-specification",
    str(REQUIREMENTS),
]


COVERAGE_IDS = [entry["requirement_id"] for entry in COMPLETE["coverage"]]
UNRELATED = """\
---
schema_version: sdi.service-description/v1
service_id: fake-irrigation
title: Lawn sprinkler valve scheduler
provenance:
  basis: placeholder
  note: Hypothetical placeholder for discovery checks.
artifacts:
  vendor-image:
    architectures: [amd64]
    route: {kind: image, reference: example.invalid/sprinkler:v1}
---

## Purpose

Opens garden sprinkler valves on a watering timetable, reading soil moisture
probes and rainfall forecasts to skip irrigation during heavy rain.
"""


def _generate(
    tmp_path: Path,
    config: Path,
    env: dict[str, str],
    service_repository: Path = SERVICE_REPOSITORY,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "generate-proposals",
            "--service-repository",
            str(service_repository),
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
            "--service-repository",
            str(SERVICE_REPOSITORY),
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


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("content-filter", "refused"),
        ("refusal", "refused"),
        ("off-schema", "invalid"),
        ("failed", "provider_error"),
    ],
)
def test_reports_why_the_provider_returned_no_proposals(
    tmp_path: Path, body: str, outcome: str
) -> None:
    config = tmp_path / "stub.yaml"
    with stub_provider(BODIES[body]) as base_url:
        config.write_text(stub_config(base_url))
        completed = _generate(tmp_path, config, {"VLLM_KEY": STUB_KEY})

    assert completed.returncode == 1, completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == outcome
    assert evidence["request"]["model"] == "stub/model"
    assert evidence["response"]["id"] == "resp_stub"
    assert STUB_KEY not in evidence_text + completed.stdout + completed.stderr


def test_rejects_an_unknown_generation_setting(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"{(CONFIGS / 'luna.yaml').read_text()}temperature: 0\n")

    completed = _generate(tmp_path, config, {})

    assert completed.returncode == 2
    assert "temperature" in completed.stderr
    assert not (tmp_path / "evidence.json").exists()


def _stub_generate(
    tmp_path: Path,
    body: dict[str, object],
    extra: str = "",
    service_repository: Path = SERVICE_REPOSITORY,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    config = tmp_path / "stub.yaml"
    with stub_provider(body) as base_url:
        config.write_text(stub_config(base_url, extra.format(base_url=base_url)))
        completed = _generate(
            tmp_path, config, {"VLLM_KEY": STUB_KEY}, service_repository
        )
    evidence_text = (tmp_path / "evidence.json").read_text()
    assert STUB_KEY not in evidence_text + completed.stdout + completed.stderr
    return completed, json.loads(evidence_text)


def test_the_model_sees_and_may_choose_only_discovered_services(
    tmp_path: Path,
) -> None:
    service_repository = tmp_path / "services"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    (service_repository / "fake-irrigation").mkdir()
    (service_repository / "fake-irrigation/SDI.md").write_text(UNRELATED)
    choosing_the_omitted = copy.deepcopy(COMPLETE)
    choosing_the_omitted["services"][0]["service_id"] = "fake-irrigation"

    completed, evidence = _stub_generate(
        tmp_path,
        proposals_body([choosing_the_omitted]),
        service_repository=service_repository,
    )

    assert completed.returncode == 1
    assert evidence["outcome"] == "invalid"
    discovery = evidence["discovery"]
    assert {"service_id": "fake-irrigation", "reason": "not retrieved"} in (
        discovery["omitted"]
    )
    assert discovery["complete"] is False
    assert discovery["dense"] == {"status": "not-configured"}
    assert [query["query_id"] for query in discovery["queries"]] == [
        *COVERAGE_IDS,
        "profile",
    ]
    prompt = evidence["request"]["input"]
    omitted = prompt.split("<omitted>")[1].split("</omitted>")[0]
    assert "fake-irrigation" in omitted.split(", ")
    assert 'id="fake-irrigation"' not in prompt
    assert "fake-irrigation" not in json.dumps(evidence["request"]["text"])


def test_the_context_budget_bounds_discovery_and_says_what_it_left_out(
    tmp_path: Path,
) -> None:
    completed, evidence = _stub_generate(
        tmp_path, BODIES["off-schema"], "max_context_bytes: 4000\n"
    )

    assert completed.returncode == 1
    assert evidence["settings"]["max_context_bytes"] == 4000
    discovery = evidence["discovery"]
    # The orchestrator's navigation goal admits navigation; its inputs no longer fit.
    assert [item["service_id"] for item in discovery["selected"]] == ["nav2-navigation"]
    assert discovery["selected"][0]["reason"] == (
        "provides action_clients /navigate_to_pose of orchestrator"
    )
    assert discovery["budget"]["used"] <= 4000
    assert {"service_id": "nav2-localization", "reason": "budget"} in (
        discovery["omitted"]
    )
    assert {
        "service_id": "nav2-navigation",
        "reference": "subscribes /map",
        "providers": ["nav2-localization"],
    } in discovery["unexpanded_references"]
    assert discovery["complete"] is False


def test_dense_ranking_joins_lexical_ranking_when_embeddings_are_configured(
    tmp_path: Path,
) -> None:
    completed, evidence = _stub_generate(
        tmp_path, BODIES["off-schema"], stub_embeddings("{base_url}")
    )

    assert completed.returncode == 1
    discovery = evidence["discovery"]
    assert discovery["dense"]["status"] == "used"
    assert discovery["dense"]["model"] == "stub/embeddings"
    for query in discovery["queries"]:
        assert all(isinstance(item["dense"], int) for item in query["ranked"])
