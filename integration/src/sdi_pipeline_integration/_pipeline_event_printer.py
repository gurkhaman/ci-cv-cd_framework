"""The one way a command or the Stage runtime prints a pipeline event."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ._pipeline_event_rows import EventLine, build_row
from ._pipeline_events import write_event

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._pipeline_event_rows import LineHead
    from ._pipeline_events import EventScope
    from ._stage_runtime import StageExecution, StageInputSource

RUN_ACCEPTED: Final = EventLine(None, "run-accepted")
RUN_FINISHED: Final = EventLine(None, "run-finished")


@dataclass(frozen=True)
class EventPrinter:
    """Prints one run's pipeline events on stderr.

    It holds what every row needs apart from its head: the run's identity, its
    committed target profile, its scope, and the accepted attempts its holder
    already has. A holder that learns of more attempts derives a new printer.
    A failed write is ignored and `build_row` never raises, so printing never
    changes a result.
    """

    identified: Mapping[str, Any]
    target_profile: StageInputSource
    scope: EventScope
    executions: tuple[StageExecution, ...] = ()

    @classmethod
    def for_run(
        cls,
        identified: Mapping[str, Any],
        committed: Mapping[str, StageInputSource],
        *,
        scope: EventScope,
        executions: Sequence[StageExecution] = (),
    ) -> EventPrinter:
        """Build a printer from `committed_stage_sources` and earlier attempts."""
        return cls(identified, committed["target_profile"], scope, tuple(executions))

    def with_executions(self, executions: Sequence[StageExecution]) -> EventPrinter:
        """Return this printer holding `executions` as the run's attempts."""
        return replace(self, executions=tuple(executions))

    def emit(self, head: LineHead) -> None:
        """Print one event line pair now."""
        write_event(
            build_row(
                self.identified,
                self.target_profile,
                self.executions,
                line=head,
                live_items=(),
                scope=self.scope,
                emitted_at=datetime.now(UTC),
            )
        )
