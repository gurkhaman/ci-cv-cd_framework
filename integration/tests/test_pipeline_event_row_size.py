"""The committed example lines."""

from __future__ import annotations

import json

import pytest

from sdi_pipeline_integration._pipeline_events import PRISM_PREFIX
from tests._pipeline_event_examples import EXAMPLE_DIRECTORY, EXAMPLES
from tests._pipeline_events import written

REGENERATE = "uv run --frozen python -m tests._pipeline_event_examples"
EXAMPLE_NULLS = {
    "mid-cv": {
        "failure_reason",
        "image_archive",
        "cv_duration_ms",
        "validation_evidence_ref",
        "cv_platform",
        "cv_services_participating",
        "cv_services_blocked",
        "cv_services_total",
        "cv_service_issues",
        "cd_duration_ms",
        "deployment_result_ref",
        "cd_placements_deployed",
        "cd_placements_blocked",
        "cd_placements_total",
        "placement_issues",
    },
    "g1-final": {"stage", "failure_reason", "image_archive"},
}


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_lines_are_current(name: str) -> None:
    row = EXAMPLES[name]()
    committed = (EXAMPLE_DIRECTORY / f"{name}.txt").read_bytes()

    assert committed == written(row), f"{name}.txt is stale; run `{REGENERATE}`"
    sdi_line, prism_line = committed.splitlines()
    assert sdi_line.startswith(b"[sdi] ")
    fields = json.loads(prism_line.removeprefix(PRISM_PREFIX.encode()))
    assert {key for key, value in fields.items() if value is None} == EXAMPLE_NULLS[
        name
    ]
