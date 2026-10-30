"""The pipeline events the CLI and the Stage runtime print, checked as printed."""

from __future__ import annotations

import pytest

from sdi_pipeline_integration._pipeline_event_check import (
    ConsoleEvent,
    Projection,
    read_console,
)
from sdi_pipeline_integration._pipeline_event_printer import (
    RUN_ACCEPTED,
    RUN_FINISHED,
    EventPrinter,
)
from sdi_pipeline_integration._stage_runtime import (
    AdapterTimeLimit,
    adapter_time_limit,
)
from tests import _pipeline_event_world as world


def _projections(stderr: bytes) -> list[str]:
    items, violations = read_console(stderr)
    assert violations == []
    return [
        str(Projection.of(item.row)) for item in items if isinstance(item, ConsoleEvent)
    ]


@pytest.mark.parametrize(
    ("work_limit", "deadline", "expected"),
    [
        (600, None, AdapterTimeLimit(600.0, "work-limit-reached")),
        (600, 1000.0 + 600, AdapterTimeLimit(600.0, "work-limit-reached")),
        (600, 1000.0 + 599.5, AdapterTimeLimit(599.5, "run-deadline-reached")),
        (600, 1000.0 - 5, AdapterTimeLimit(0.0, "run-deadline-reached")),
    ],
    ids=["no-deadline", "deadline-equal-to-limit", "deadline-sooner", "expired"],
)
def test_the_adapter_waits_for_the_work_limit_unless_the_deadline_is_sooner(
    work_limit: int, deadline: float | None, expected: AdapterTimeLimit
) -> None:
    assert adapter_time_limit(work_limit, deadline, 1000.0) == expected


@pytest.mark.parametrize(
    ("seconds", "limit_min"),
    [(0.0, 1), (59.9, 1), (60.0, 1), (119.9, 1), (120.0, 2), (2700.0, 45)],
)
def test_the_shown_limit_rounds_down_to_at_least_one_minute(
    seconds: float, limit_min: int
) -> None:
    limit = AdapterTimeLimit(seconds, "work-limit-reached")

    assert limit.limit_min == limit_min
    assert limit.running_line("cv").slots == {"limit_min": limit_min}
    assert limit.reached_line("cv").slots == {"limit_min": limit_min}


def test_each_time_limit_names_its_own_reason_and_line() -> None:
    work = AdapterTimeLimit(60.0, "work-limit-reached")
    run = AdapterTimeLimit(0.0, "run-deadline-reached")

    assert work.reason[0] == "sdi.stage.deadline-exceeded"
    assert run.reason[0] == "sdi.run.deadline-exceeded"
    assert work.reached_line("cv").step == "work-limit-reached"
    assert run.reached_line("cv").step == "run-deadline-reached"
    assert run.reached_line("cv").slots == {}


def test_a_printer_prints_one_line_pair_per_emit_on_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    printer = EventPrinter.for_run(
        world.IDENTIFIED,
        {"target_profile": world.profile_source()},
        scope="run",
    )

    printer.emit(RUN_ACCEPTED)
    printer.with_executions([world.composition_ok()]).emit(RUN_FINISHED)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert _projections(captured.err.encode()) == [
        "RUN run-accepted [P P P P] -",
        "RUN run-finished final [S P P P] -",
    ]
