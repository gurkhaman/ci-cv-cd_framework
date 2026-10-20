"""Run one S-04 composition in the CV engine and report what happened.

The run loop removes leftover CV containers, builds the sim image on a miss,
pulls every placement by its linux/amd64 digest, rewrites each image's command
for sim time, and starts the sim, the placements and the orchestrator in the
sim's network and IPC namespaces. It then waits for the orchestrator, takes a
snapshot of every container, reads the orchestrator's result file, and removes
everything it started. The verdict is the caller's.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import requests
from pydantic import ValidationError

from ._contracts import ContractModel, NonBlank
from ._cv_result import ScenarioResult

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from docker import DockerClient
    from docker.models.containers import Container

ENGINE_URL = "tcp://cv-engine:2375"
REGISTRY = "registry:5000"
EXECUTION_LABEL = "org.sdi.cv-execution"
SIM_CONTEXT = Path(__file__).parent / "_cv_sim" / "s-04"
SIM_REPOSITORY = "sdi-cv-sim"
SIM_SHM_SIZE = "1g"
ORCHESTRATOR_COMMAND = ("cv-orchestrator",)
RESULT_PATH = "/cv/result.json"
RESULT_MAX_BYTES = 64 * 1024
TEARDOWN_RESERVE_SECONDS = 120
# Pulls and the sim build stream progress, so a long silence means a stuck engine.
CLIENT_TIMEOUT_SECONDS = 600
LOG_TAIL_LINES = 40

Robot = Literal["waffle_pi", "burger_cam"]
BlockingCause = Literal["image-unavailable", "unsupported-command"]
RunError = Literal[
    "sim-build-failed",
    "orchestrator-timeout",
    "sim-not-running",
    "no-result",
    "result-too-large",
    "invalid-result",
]
Phase = Literal["leftover-cleanup", "sim-build", "pulls", "start", "orchestrator-wait"]


class RunOptions(TypedDict, total=False):
    image: str
    command: list[str]
    name: str
    labels: dict[str, str]
    environment: dict[str, str]
    network_mode: str
    ipc_mode: str
    shm_size: str
    detach: Literal[True]


class CountingOutput(ContractModel):
    """One `CV_COUNTING` entry; service_id is for the orchestrator's logs only."""

    topic: NonBlank
    type: NonBlank
    service_id: NonBlank


@dataclass(frozen=True)
class Placement:
    service_id: str
    digest: str

    @property
    def reference(self) -> str:
        return f"{REGISTRY}/sdi/{self.service_id}@{self.digest}"


@dataclass(frozen=True)
class RunRequest:
    execution_id: str
    robot: Robot
    placements: tuple[Placement, ...]
    counting: tuple[CountingOutput, ...]
    deadline_monotonic: float


@dataclass(frozen=True)
class Plan:
    sim: RunOptions
    placements: Mapping[str, RunOptions]
    orchestrator: RunOptions


@dataclass(frozen=True)
class ContainerState:
    status: str
    exit_code: int
    log_tail: tuple[str, ...]

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True)
class FailedToStart:
    detail: str


@dataclass(frozen=True)
class Snapshot:
    sim: ContainerState
    orchestrator: ContainerState
    placements: Mapping[str, ContainerState | FailedToStart]


@dataclass(frozen=True)
class Blocked:
    """A placement could not be pulled or run in sim time; nothing started."""

    causes: Mapping[str, BlockingCause]
    phases: Mapping[Phase, float]


@dataclass(frozen=True)
class Finished:
    result: ScenarioResult
    snapshot: Snapshot
    phases: Mapping[Phase, float]


@dataclass(frozen=True)
class Failed:
    """A CV error: the run gave no result to judge."""

    error: RunError
    detail: str
    snapshot: Snapshot | None
    phases: Mapping[Phase, float]


RunOutcome = Blocked | Finished | Failed


def sim_time_command(command: Sequence[str]) -> tuple[str, ...] | None:
    """Return the command with sim time switched on, or None for another shape."""
    match list(command):
        case ["ros2", "launch", _, *_]:
            return (*command, "use_sim_time:=true")
        case ["ros2", "run", _, _, *_]:
            return (*command, "--ros-args", "-p", "use_sim_time:=true")
        case _:
            return None


def sim_image_tag(context: Path) -> str:
    """Tag the sim image by the content of its build context."""
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in context.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(context).parts
    )
    for path in files:
        content = path.read_bytes()
        for part in (
            path.relative_to(context).as_posix().encode(),
            str(len(content)).encode(),
            content,
        ):
            digest.update(part + b"\0")
    return f"{SIM_REPOSITORY}:{digest.hexdigest()}"


def plan(
    request: RunRequest, sim_image: str, commands: Mapping[str, tuple[str, ...]]
) -> Plan:
    """Return the run options of the sim, every placement and the orchestrator."""
    labels = {EXECUTION_LABEL: request.execution_id}
    sim = f"cv-{request.execution_id}-sim"
    counting = json.dumps(
        [item.model_dump(mode="json") for item in request.counting],
        separators=(",", ":"),
    )
    return Plan(
        sim={
            "image": sim_image,
            "name": sim,
            "labels": labels,
            "environment": {"CV_ROBOT": request.robot},
            "ipc_mode": "shareable",
            "shm_size": SIM_SHM_SIZE,
            "detach": True,
        },
        placements={
            item.service_id: {
                "image": item.reference,
                "command": list(commands[item.service_id]),
                "name": f"cv-{request.execution_id}-{item.service_id}",
                "labels": labels,
                "network_mode": f"container:{sim}",
                "ipc_mode": f"container:{sim}",
                "detach": True,
            }
            for item in request.placements
        },
        orchestrator={
            "image": sim_image,
            "command": list(ORCHESTRATOR_COMMAND),
            "name": f"cv-{request.execution_id}-orchestrator",
            "labels": labels,
            "environment": {"CV_COUNTING": counting},
            "network_mode": f"container:{sim}",
            "ipc_mode": f"container:{sim}",
            "detach": True,
        },
    )


def run_composition(request: RunRequest, engine_url: str = ENGINE_URL) -> RunOutcome:
    """Run one composition in the engine and remove everything it started."""
    import docker  # noqa: PLC0415

    client = docker.DockerClient(base_url=engine_url, timeout=CLIENT_TIMEOUT_SECONDS)
    try:
        return _Run(client, request).outcome()
    finally:
        for container in _labelled(client, f"{EXECUTION_LABEL}={request.execution_id}"):
            container.remove(force=True)
        client.close()


class _Run:
    def __init__(self, client: DockerClient, request: RunRequest) -> None:
        self.client = client
        self.request = request
        self.phases: dict[Phase, float] = {}

    def outcome(self) -> RunOutcome:
        with self.timed("leftover-cleanup"):
            for container in _labelled(self.client, EXECUTION_LABEL):
                container.remove(force=True)
        with self.timed("sim-build"):
            try:
                sim_image = self.sim_image()
            except _SimBuildError as error:
                return Failed("sim-build-failed", str(error), None, self.phases)
        with self.timed("pulls"):
            commands, causes = self.pull()
        if causes:
            return Blocked(causes, self.phases)
        containers = plan(self.request, sim_image, commands)
        with self.timed("start"):
            sim = self.client.containers.run(**containers.sim)
            placements = {
                service_id: self.start(options)
                for service_id, options in containers.placements.items()
            }
            orchestrator = self.client.containers.run(**containers.orchestrator)
        with self.timed("orchestrator-wait"):
            exited = self.wait(orchestrator)
        snapshot = Snapshot(
            _state(sim),
            _state(orchestrator),
            {
                service_id: item if isinstance(item, FailedToStart) else _state(item)
                for service_id, item in placements.items()
            },
        )
        if not exited:
            return Failed(
                "orchestrator-timeout",
                "the orchestrator was still running at the wait timeout",
                snapshot,
                self.phases,
            )
        if not snapshot.sim.running:
            return Failed(
                "sim-not-running",
                f"the sim was {snapshot.sim.status} when the orchestrator exited",
                snapshot,
                self.phases,
            )
        return self.read_result(orchestrator, snapshot)

    @contextmanager
    def timed(self, phase: Phase) -> Generator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.phases[phase] = round(time.monotonic() - started, 1)

    def sim_image(self) -> str:
        from docker.errors import ImageNotFound  # noqa: PLC0415

        tag = sim_image_tag(SIM_CONTEXT)
        try:
            self.client.images.get(tag)
        except ImageNotFound:
            for chunk in self.client.api.build(
                path=str(SIM_CONTEXT), tag=tag, rm=True, forcerm=True, decode=True
            ):
                entry = cast("dict[str, object]", chunk)
                if "error" in entry:
                    raise _SimBuildError(str(entry["error"])) from None
        return tag

    def pull(self) -> tuple[dict[str, tuple[str, ...]], dict[str, BlockingCause]]:
        from docker.errors import DockerException  # noqa: PLC0415

        commands: dict[str, tuple[str, ...]] = {}
        causes: dict[str, BlockingCause] = {}
        for item in self.request.placements:
            try:
                image = self.client.images.pull(item.reference)
            except DockerException:
                causes[item.service_id] = "image-unavailable"
                continue
            config = cast("dict[str, object]", image.attrs.get("Config") or {})
            command = sim_time_command(cast("list[str]", config.get("Cmd") or []))
            if command is None:
                causes[item.service_id] = "unsupported-command"
            else:
                commands[item.service_id] = command
        return commands, causes

    def start(self, options: RunOptions) -> Container | FailedToStart:
        from docker.errors import APIError  # noqa: PLC0415

        try:
            return self.client.containers.run(**options)
        except APIError as error:
            return FailedToStart(str(error.explanation or error))

    def wait(self, orchestrator: Container) -> bool:
        remaining = (
            self.request.deadline_monotonic
            - TEARDOWN_RESERVE_SECONDS
            - time.monotonic()
        )
        if remaining <= 0:
            return False
        try:
            orchestrator.wait(timeout=remaining)
        except (requests.ConnectionError, requests.ReadTimeout):
            return False
        return True

    def read_result(self, orchestrator: Container, snapshot: Snapshot) -> RunOutcome:
        from docker.errors import NotFound  # noqa: PLC0415

        try:
            stream, stat = orchestrator.get_archive(RESULT_PATH)
        except NotFound:
            return Failed(
                "no-result",
                f"the orchestrator wrote no {RESULT_PATH}",
                snapshot,
                self.phases,
            )
        size = cast("int", cast("dict[str, object]", stat)["size"])
        if size > RESULT_MAX_BYTES:
            return Failed(
                "result-too-large",
                f"{RESULT_PATH} is {size} bytes, over {RESULT_MAX_BYTES}",
                snapshot,
                self.phases,
            )
        archive = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.next()
            content = tar.extractfile(member) if member is not None else None
            raw = content.read(RESULT_MAX_BYTES + 1) if content is not None else b""
        try:
            result = ScenarioResult.model_validate_json(raw)
        except ValidationError as error:
            return Failed(
                "invalid-result",
                f"{RESULT_PATH} is not a valid result: {error.error_count()} errors",
                snapshot,
                self.phases,
            )
        expected = {item.topic for item in self.request.counting}
        if not result.unloadable_types and set(result.counts) != expected:
            return Failed(
                "invalid-result",
                f"{RESULT_PATH} does not count exactly the counting topics",
                snapshot,
                self.phases,
            )
        return Finished(result, snapshot, self.phases)


class _SimBuildError(Exception):
    pass


def _labelled(client: DockerClient, label: str) -> list[Container]:
    return client.containers.list(all=True, filters={"label": label})


def _state(container: Container) -> ContainerState:
    container.reload()
    state = cast("dict[str, object]", container.attrs["State"])
    logs = container.logs(tail=LOG_TAIL_LINES)
    return ContainerState(
        str(state["Status"]),
        cast("int", state["ExitCode"]),
        tuple(logs.decode(errors="replace").splitlines()),
    )
