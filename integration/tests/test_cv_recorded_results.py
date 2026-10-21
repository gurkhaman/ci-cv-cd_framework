"""Result files recorded from real cv_engine runs, read-only under a MANIFEST."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sdi_pipeline_integration._cv_result import ScenarioResult

RECORDED = Path(__file__).parent / "data" / "cv-results"
PASS = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "passed",
    "scripted",
    "passed",
    "passed",
)
NO_FACE = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "timed_out",
    *("not_reached",) * 3,
)


def _manifest() -> dict[str, str]:
    lines = (RECORDED / "MANIFEST").read_text().splitlines()
    assert lines[0].startswith("# recorded by cv_engine session ")
    return {
        name: digest
        for digest, name in (line.split("  ", 1) for line in lines[1:] if line)
    }


def test_cv_recordings_match_their_manifest() -> None:
    manifest = _manifest()

    assert manifest.keys() == {path.name for path in RECORDED.glob("*.json")}
    for name, digest in manifest.items():
        assert hashlib.sha256((RECORDED / name).read_bytes()).hexdigest() == digest


def _steps(name: str) -> tuple[str, ...]:
    result = ScenarioResult.model_validate_json((RECORDED / name).read_bytes())
    assert result.mission is not None
    assert all(count >= 0 for count in result.counts.values())
    return tuple(item.status for item in result.mission.steps)


def test_cv_recorded_passes_complete_the_mission() -> None:
    assert _steps("waffle-pass.json") == PASS
    assert _steps("burger-pass.json") == PASS


def test_cv_recorded_no_face_run_times_out_at_the_face_step() -> None:
    assert _steps("no-face.json") == NO_FACE
