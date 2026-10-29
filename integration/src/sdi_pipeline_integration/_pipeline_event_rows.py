"""Pipeline events: build one row from a run's identity, profile and attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from ._pipeline_event_catalogue import SlotName, SlotValue, StepCode
    from ._pipeline_events import EventScope, PipelineEventRow
    from ._stage_registry import StageName
    from ._stage_runtime import StageExecution, StageInputSource


@dataclass(frozen=True)
class EventLine:
    """A line head other than `stage-finished`: Stage, step, and the caller's slots.

    It is also an accepted adapter report. Building one checks it against the
    catalogue and raises ValueError when the step does not appear on the
    Stage's lines, when `slots` names a slot the row fills, when no form (or
    the heartbeat template) takes exactly `slots`, or when a value does not fit
    its slot type.
    """

    stage: StageName | None
    step: StepCode
    slots: Mapping[SlotName, SlotValue] = field(
        default_factory=lambda: MappingProxyType({})
    )
    heartbeat: bool = False


@dataclass(frozen=True)
class StageFinished:
    """The `stage-finished` head, carrying the attempt its Stage just published."""

    execution: StageExecution

    @property
    def stage(self) -> StageName:
        """Return the Stage the attempt belongs to."""
        return self.execution.envelope.correlation.stage

    @property
    def step(self) -> StepCode:
        """Return the head's step code."""
        return "stage-finished"


type LineHead = EventLine | StageFinished


def build_row(  # noqa: PLR0913
    identified: Mapping[str, Any],
    target_profile_source: StageInputSource,
    executions: Sequence[StageExecution],
    *,
    line: LineHead,
    live_items: Sequence[EventLine],
    scope: EventScope,
    emitted_at: datetime,
) -> PipelineEventRow:
    """Build the full row one pipeline event prints."""
    raise NotImplementedError
