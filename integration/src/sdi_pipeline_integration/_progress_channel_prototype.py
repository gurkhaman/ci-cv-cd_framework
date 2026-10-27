"""THROWAWAY PROTOTYPE (GUR-212). Not for merge.

Adapter progress channel: the adapter writes step codes and typed slots as one
JSON line per report to an inherited pipe fd; the runtime reads it on its main
thread, checks each report against a mini step catalogue, and is the only
process that prints `[sdi]` and `PRISM_EVENT` lines.
"""

from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

FD_ENV = "SDI_PROGRESS_FD"
HEARTBEAT_ENV = "SDI_PROGRESS_HEARTBEAT_SECONDS"
COMPOSITION_ID = "C-01"  # fake row key for the prototype
LINE_MAX = 1024
ATTEMPT_MAX = 64 * 1024
POLL_SECONDS = 0.2
READS_PER_TICK = 16

MISSION_STEPS = (
    "secure-book",
    "accept-spoken-request",
    "resolve-rendezvous",
    "navigate-to-rendezvous",
    "verify-recipient-face",
    "transfer-book",
    "acknowledge-receipt",
    "return-to-origin",
)
STAGE_LABEL = {
    "composition": "COMPOSITION",
    "image_build": "IMAGE BUILD",
    "cv": "CV",
    "cd": "CD",
}


# ---------------------------------------------------------------- catalogue


@dataclass(frozen=True)
class Step:
    stage: str
    code: str
    template: str
    slots: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    family: str | None = None  # item steps share one family per item kind
    heartbeat: str | None = None
    adapter: bool = True


INT = "int"
ENUM = "enum"
SERVICE = "service"
PROVIDER = "provider"
MISSION = "mission"

_STEPS = (
    Step("composition", "discovery", "Finding candidate services ({n} in repository, {search})",
         {"n": (INT, 0, 4096), "search": (ENUM, "lexical", "lexical+embeddings")}),
    Step("composition", "generation", "Asking {provider} for proposals",
         {"provider": (PROVIDER,)}, heartbeat="Still waiting for {provider}, {m} min"),
    Step("composition", "assessment", "Assessing {n} proposals", {"n": (INT, 0, 64)}),
    Step("image_build", "builder-check", "Checking builder and registry"),
    Step("image_build", "building", "Building {n_started} images (amd64+arm64)",
         {"n_started": (INT, 0, 48)}, heartbeat="Still building {n_left} images, {m} min"),
    Step("image_build", "image-built", "{service_id} built ({i}/{n})",
         {"service_id": (SERVICE,)}, family="image"),
    Step("image_build", "image-failed", "{service_id} failed: {image_reason} ({i}/{n})",
         {"service_id": (SERVICE,),
          "image_reason": (ENUM, "unresolved-placement", "no-recipe", "build-failed", "work-limit")},
         family="image"),
    Step("cv", "mission", "Mission setup (face, localization, navigation)",
         heartbeat="Mission still running, {m} min"),
    Step("cv", "mission-setup-done", "Setup {setup_status} after {s} s",
         {"setup_status": (ENUM, "passed", "timed_out"), "s": (INT, 0, 86400)}),
    Step("cv", "mission-step", "Step {mission_step}: {mission_status} after {s} s ({i}/{n})",
         {"mission_step": (MISSION,),
          "mission_status": (ENUM, "passed", "failed", "timed_out", "scripted"),
          "s": (INT, 0, 86400)}, family="mission"),
    Step("cv", "collecting-result", "Reading the mission result"),
)
_RUNTIME = (
    ("stage-started", "{Stage} started"),
    ("adapter-running", "{Stage} work started (limit {limit_min} min)"),
    ("work-limit-reached", "{Stage} hit its {limit_min} min limit, stopping"),
    ("checking-output", "Checking {Stage} output"),
    ("stage-finished", "{Stage} finished: {outcome}"),
)
CATALOGUE: dict[tuple[str, str], Step] = {(s.stage, s.code): s for s in _STEPS}
for _stage in STAGE_LABEL:
    for _code, _template in _RUNTIME:
        CATALOGUE[(_stage, _code)] = Step(_stage, _code, _template, adapter=False)


# ---------------------------------------------------------------- writer (adapter side)


class Reporter:
    """Best-effort writer. Never blocks, never raises, never sends free text."""

    def __init__(self, fd: int | None) -> None:
        self.fd = fd

    @classmethod
    def from_environment(cls) -> Reporter:
        raw = os.environ.pop(FD_ENV, None)
        if raw is None or not raw.isdigit():
            return cls(None)
        fd = int(raw)
        try:
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                return cls(None)
            os.set_inheritable(fd, False)
        except OSError:
            return cls(None)
        return cls(fd)

    def report(self, step: str, **slots: object) -> None:
        if self.fd is None:
            return
        line = json.dumps({"step": step, "slots": slots}, separators=(",", ":"))
        data = line.encode() + b"\n"
        if len(data) > LINE_MAX:
            return
        try:
            os.write(self.fd, data)
        except OSError:  # EAGAIN (pipe full), EPIPE (runtime gone), EBADF
            return


# ---------------------------------------------------------------- emitter (runtime only)


class Emitter:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.row: dict[str, Any] = {
            "composition_id": COMPOSITION_ID,
            "stage": stage,
            "step": None,
            "message": None,
            f"{stage}_status": "RUNNING",
        }
        if stage == "image_build":
            self.row["image_failures"] = None
        if stage == "cv":
            self.row["mission_setup"] = None
            self.row["mission_steps"] = None

    def emit(self, step: str, message: str, status: str = "RUNNING") -> None:
        self.row.update(step=step, message=message)
        self.row[f"{self.stage}_status"] = status
        label = STAGE_LABEL[self.stage]
        text = (
            f"[sdi] {COMPOSITION_ID} {label} › {step}  {status}  {message}\n"
            f"PRISM_EVENT {json.dumps(self.row, separators=(',', ':'))}\n"
        )
        sys.stderr.write(text)
        sys.stderr.flush()

    def note(self, text: str) -> None:
        sys.stderr.write(f"sdi-integration: {text}\n")
        sys.stderr.flush()


# ---------------------------------------------------------------- reader (runtime main thread)


@dataclass
class Context:
    """What the runtime gave the adapter, against which reports are checked."""

    services: frozenset[str] = frozenset()
    providers: frozenset[str] = frozenset()


def context_from_inputs(stage: str, input_models: dict[str, Any]) -> Context:
    services: frozenset[str] = frozenset()
    schema = input_models.get("deployment_schema")
    if schema is not None:
        services = frozenset(item.service_id for item in schema.placements)
    providers: frozenset[str] = frozenset()
    settings = input_models.get("composition_settings")
    if settings is not None:
        providers = frozenset({settings.generator})
    return Context(services, providers)


class Channel:
    def __init__(self, stage: str, context: Context, *, limit_seconds: float) -> None:
        self.stage = stage
        self.context = context
        self.emitter = Emitter(stage)
        self.limit_min = max(1, round(limit_seconds / 60))
        self.heartbeat_seconds = float(os.environ.get(HEARTBEAT_ENV, "180"))
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)  # shared description: adapter writes never block
        self.buffer = b""
        self.discarding = False
        self.total = 0
        self.reading = True  # False after EOF, budget exhaustion, or close
        self.closed = False  # set before the terminal status; later reports are dropped
        self.dropped: Counter[str] = Counter()
        self.items: dict[str, set[str]] = {}
        self.seen_steps: set[str] = set()
        self.long_step: Step | None = None
        self.long_slots: dict[str, Any] = {}
        self.long_started = 0.0
        self.last_line = time.monotonic()

    # -- lifecycle

    def child_environment(self, environment: dict[str, str]) -> dict[str, str]:
        return environment | {FD_ENV: str(self.write_fd)}

    def after_spawn(self) -> None:
        self._close_write()

    def _close_write(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            self.write_fd = -1

    def _close_read(self) -> None:
        self.reading = False
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1

    def close_input(self, *, report: bool = True) -> None:
        """Stop accepting reports. Called before any terminal line."""
        if self.closed:
            return
        self.closed = True
        if self.buffer or self.discarding:
            self.dropped["partial-line"] += 1
            self.buffer = b""
        self._close_read()
        if report and self.dropped:
            counts = ", ".join(f"{k}={v}" for k, v in sorted(self.dropped.items()))
            self.emitter.note(f"dropped {sum(self.dropped.values())} progress reports ({counts})")

    def abort(self) -> None:
        self.closed = True
        self._close_write()
        self._close_read()

    # -- runtime lines

    def runtime(self, code: str, status: str = "RUNNING", **slots: object) -> None:
        step = CATALOGUE[(self.stage, code)]
        message = step.template.format(
            Stage=STAGE_LABEL[self.stage].title() if self.stage != "cv" else "CV",
            limit_min=self.limit_min,
            **slots,
        )
        self.emitter.emit(code, message, status)
        self.long_step = None
        self.last_line = time.monotonic()

    # -- reading

    def read_available(self) -> None:
        for _ in range(READS_PER_TICK):
            if not self.reading:
                return
            try:
                chunk = os.read(self.read_fd, 65536)
            except BlockingIOError:
                return
            if not chunk:
                self.reading = False
                return
            self._feed(chunk)

    def _feed(self, chunk: bytes) -> None:
        if self.closed:
            return
        room = ATTEMPT_MAX - self.total
        if len(chunk) > room:
            chunk = chunk[:room]
            self.dropped["attempt-budget"] += 1
            self._close_read()  # adapter writes now fail with EPIPE and are dropped
        self.total += len(chunk)
        self.buffer += chunk
        while (cut := self.buffer.find(b"\n")) >= 0:
            line, self.buffer = self.buffer[:cut], self.buffer[cut + 1 :]
            if self.discarding:
                self.discarding = False
                self.dropped["oversize-line"] += 1
                continue
            self._handle(line)
        if len(self.buffer) >= LINE_MAX:
            self.discarding = True
            self.buffer = b""

    def _handle(self, line: bytes) -> None:  # noqa: C901, PLR0911, PLR0912
        if len(line) >= LINE_MAX:
            self.dropped["oversize-line"] += 1
            return
        try:
            report = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.dropped["malformed"] += 1
            return
        if (
            not isinstance(report, dict)
            or set(report) != {"step", "slots"}
            or not isinstance(report["step"], str)
            or not isinstance(report["slots"], dict)
        ):
            self.dropped["malformed"] += 1
            return
        step = CATALOGUE.get((self.stage, report["step"]))
        if step is None:
            self.dropped["unknown-step"] += 1
            return
        if not step.adapter:
            self.dropped["runtime-step"] += 1
            return
        slots = report["slots"]
        if set(slots) != set(step.slots):
            self.dropped["bad-slots"] += 1
            return
        for name, spec in step.slots.items():
            reason = self._check(spec, slots[name])
            if reason is not None:
                self.dropped[reason] += 1
                return
        values = dict(slots)
        if step.family is not None:
            key = slots.get("service_id") or slots.get("mission_step")
            accepted = self.items.setdefault(step.family, set())
            if key in accepted:
                self.dropped["duplicate-item"] += 1
                return
            accepted.add(key)
            values["i"] = len(accepted)
            values["n"] = len(MISSION_STEPS) if step.family == "mission" else len(self.context.services)
        else:
            if step.code in self.seen_steps:
                self.dropped["repeated-step"] += 1
                return
            self.seen_steps.add(step.code)
        self._accept(step, values)

    def _check(self, spec: tuple[Any, ...], value: object) -> str | None:
        kind = spec[0]
        if kind == INT:
            ok = isinstance(value, int) and not isinstance(value, bool) and spec[1] <= value <= spec[2]
            return None if ok else "bad-slots"
        if kind == ENUM:
            return None if value in spec[1:] else "bad-slots"
        if kind == SERVICE:
            return None if value in self.context.services else "foreign-service"
        if kind == PROVIDER:
            return None if value in self.context.providers else "bad-slots"
        if kind == MISSION:
            return None if value in MISSION_STEPS else "bad-slots"
        return "bad-slots"

    def _accept(self, step: Step, values: dict[str, Any]) -> None:
        row = self.emitter.row
        if step.code == "image-failed":
            row["image_failures"] = [
                *(row["image_failures"] or []),
                {"service_id": values["service_id"], "reason": values["image_reason"]},
            ]
        if step.code == "mission-setup-done":
            row["mission_setup"] = values["setup_status"]
        if step.code == "mission-step":
            steps = row["mission_steps"] or [{"id": s, "status": None} for s in MISSION_STEPS]
            row["mission_steps"] = [
                {"id": s["id"], "status": values["mission_status"]}
                if s["id"] == values["mission_step"]
                else s
                for s in steps
            ]
        template = step.template
        if step.code == "mission-step" and values["mission_status"] == "scripted":
            template = "Step {mission_step}: scripted ({i}/{n})"
        self.emitter.emit(step.code, template.format(**values))
        now = time.monotonic()
        self.last_line = now
        if step.family is None:
            self.long_step = step if step.heartbeat else None
            self.long_slots = values
            self.long_started = now

    # -- heartbeat

    def next_heartbeat(self) -> float:
        if self.long_step is None:
            return float("inf")
        return self.last_line + self.heartbeat_seconds

    def tick(self, now: float) -> None:
        if self.closed or self.long_step is None or now < self.next_heartbeat():
            return
        values = dict(self.long_slots)
        values["m"] = int((now - self.long_started) // 60)
        if self.long_step.code == "building":
            values["n_left"] = values["n_started"] - len(self.items.get("image", ()))
        self.emitter.emit(self.long_step.code, self.long_step.heartbeat.format(**values))  # type: ignore[union-attr]
        self.last_line = now


def wait_with_progress(
    process: subprocess.Popen[bytes], channel: Channel, timeout_seconds: float
) -> int:
    """Replace process.wait(timeout): select on the progress pipe, poll the child.

    ExternalCancellation from the signal handler surfaces from select/sleep on
    this (main) thread. Raises TimeoutExpired past the deadline.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        return_code = process.poll()
        if return_code is not None:
            channel.read_available()  # drain what the adapter wrote before exit
            channel.close_input()
            return return_code
        now = time.monotonic()
        if now >= deadline:
            channel.close_input()
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        wait = max(0.0, min(POLL_SECONDS, deadline - now, channel.next_heartbeat() - now))
        if channel.reading:
            ready, _, _ = select.select([channel.read_fd], [], [], wait)
            if ready:
                channel.read_available()
        else:
            time.sleep(wait)
        channel.tick(time.monotonic())


def finish(channel: Channel, envelope: Any) -> None:  # noqa: ANN401
    """The runtime's terminal line; always last for the Stage."""
    channel.close_input()
    if envelope.domain_outcome == "succeeded":
        status = "SUCCEEDED"
    elif envelope.domain_outcome == "not_evaluated" and envelope.execution_conclusion == "succeeded":
        status = "NOT_EVALUATED"
    else:
        status = "FAILED"
    outcome = envelope.reason.code if envelope.reason is not None else "succeeded"
    channel.runtime("stage-finished", status, outcome=outcome)
    channel.abort()
