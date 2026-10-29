"""Large rows under the 4096-byte guard, and the committed example lines."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._pipeline_event_rows import EventLine
from sdi_pipeline_integration._pipeline_events import (
    PRISM_LINE_MAX_BYTES,
    PRISM_PREFIX,
    encode_event,
    render_prism_line,
)
from sdi_pipeline_integration._service_descriptions import read_service_repository
from tests._pipeline_event_examples import EXAMPLE_DIRECTORY, EXAMPLES
from tests._pipeline_event_world import (
    SERVICE_REPOSITORY,
    Placement,
    build,
    cd_deployed,
    composition_ok,
    cv_passed,
    images_built,
)
from tests._pipeline_events import written
from tests._stage_attempts import finalized

if TYPE_CHECKING:
    from sdi_pipeline_integration._pipeline_events import PipelineEventRow

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


def _repository_placements(count: int) -> tuple[Placement, ...]:
    """Every repository service, then replicas of them, alternating hosts."""
    services: list[tuple[str, str]] = []
    for item in read_service_repository(SERVICE_REPOSITORY):
        artifacts = item.description.artifacts or {}
        services.append((item.description.service_id, min(artifacts)))
    services += [(f"{service}-replica", artifact) for service, artifact in services]
    return tuple(
        Placement(service, artifact, ("waffle", "orin")[index % 2])
        for index, (service, artifact) in enumerate(services[:count])
    )


def _g1_final(placements: tuple[Placement, ...]) -> PipelineEventRow:
    held = [
        composition_ok(placements),
        images_built(placements),
        cv_passed(placements),
        cd_deployed(placements),
    ]
    return build(
        finalized(held, run_deadline_expired=False),
        line=EventLine(None, "run-finished"),
    )


def test_an_eighteen_image_final_line_drops_digests_and_keeps_image_names() -> None:
    row = _g1_final(_repository_placements(18))

    encoded = encode_event(row)

    assert len(render_prism_line(row)) > PRISM_LINE_MAX_BYTES
    assert encoded.nulled == (
        "image_build_result_ref",
        "validation_evidence_ref",
        "deployment_result_ref",
        "mission_steps",
        "image_digest",
    )
    prism = encoded.data.splitlines()[1]
    assert len(prism) <= PRISM_LINE_MAX_BYTES
    kept = json.loads(prism.removeprefix(PRISM_PREFIX.encode()))
    assert len(kept["image_ref"]) == 18


def test_a_final_line_with_every_repository_service_fits_unchanged() -> None:
    services = [
        path for path in SERVICE_REPOSITORY.iterdir() if (path / "SDI.md").is_file()
    ]
    row = _g1_final(_repository_placements(len(services)))

    encoded = encode_event(row)

    assert encoded.nulled == ()
    assert len(render_prism_line(row)) <= PRISM_LINE_MAX_BYTES
    assert row.image_ref is not None
    assert len(row.image_ref) == len(services)


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_lines_are_current_and_fit(name: str) -> None:
    row = EXAMPLES[name]()
    committed = (EXAMPLE_DIRECTORY / f"{name}.txt").read_bytes()

    assert committed == written(row), f"{name}.txt is stale; run `{REGENERATE}`"
    assert encode_event(row).nulled == ()
    sdi_line, prism_line = committed.splitlines()
    assert len(prism_line) <= PRISM_LINE_MAX_BYTES
    assert sdi_line.startswith(b"[sdi] ")
    fields = json.loads(prism_line.removeprefix(PRISM_PREFIX.encode()))
    assert {key for key, value in fields.items() if value is None} == EXAMPLE_NULLS[
        name
    ]
