"""Real S-04 compositions in the session's CV engine pair."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import docker
import pytest

from sdi_pipeline_integration import _cv_run as run
from sdi_pipeline_integration._cv_run import (
    ContainerState,
    CountingOutput,
    Errored,
    FailedToStart,
    Finished,
    Placement,
    Robot,
    RunOutcome,
    RunRequest,
    run_composition,
)

from ._cv_images import amd64_digest

if TYPE_CHECKING:
    from sdi_pipeline_integration._cv_result import Mission

pytestmark = pytest.mark.cv_engine

REPOSITORY = Path(__file__).parents[2]
WORK_LIMIT_SECONDS = 2700
NAVIGATION = ("nav2-localization", "nav2-navigation")
SPEECH = ("vosk-speech",)
FACE = ("opencv-face-recognition",)
OUTPUTS = {
    "nav2-localization": ("/amcl_pose", "geometry_msgs/msg/PoseWithCovarianceStamped"),
    "nav2-navigation": ("/cmd_vel", "geometry_msgs/msg/TwistStamped"),
    "vosk-speech": ("/speech/text", "std_msgs/msg/String"),
    "opencv-face-recognition": ("/face_recognition/name", "std_msgs/msg/String"),
}
SILENT = CountingOutput(
    topic="/cv_probe/silent", type="std_msgs/msg/String", service_id="probe"
)
WRONG_TYPE = CountingOutput(
    topic="/odom", type="std_msgs/msg/String", service_id="probe"
)
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
TIMEOUTS = {
    "accept-spoken-request": 60,
    "navigate-to-rendezvous": 480,
    "verify-recipient-face": 120,
    "acknowledge-receipt": 60,
    "return-to-origin": 480,
}
SETUP_TIMEOUT = 300


@dataclass(frozen=True)
class Composition:
    """A robot and the services composed for it, plus probe counting entries."""

    robot: Robot
    services: tuple[str, ...]
    probes: tuple[CountingOutput, ...] = ()


COMPOSITIONS = {
    "waffle-pass": Composition("waffle_pi", NAVIGATION + SPEECH + FACE),
    "burger-pass": Composition("burger_cam", NAVIGATION + SPEECH + FACE),
    "no-face": Composition("waffle_pi", NAVIGATION + SPEECH, (SILENT, WRONG_TYPE)),
    "waffle-repeat-1": Composition("waffle_pi", NAVIGATION + SPEECH + FACE),
    "waffle-repeat-2": Composition("waffle_pi", NAVIGATION + SPEECH + FACE),
    "waffle-repeat-3": Composition("waffle_pi", NAVIGATION + SPEECH + FACE),
}


@dataclass
class Engine:
    """The session's engine pair, its pushed placements, and the runs so far."""

    url: str
    registry_url: str
    root: Path
    placements: dict[str, Placement] = field(default_factory=dict[str, Placement])
    runs: dict[str, RunOutcome] = field(default_factory=dict[str, RunOutcome])

    def placement(self, service_id: str) -> Placement:
        """Build and push a service on first use."""
        if service_id not in self.placements:
            self.placements[service_id] = self._push(service_id)
        return self.placements[service_id]

    def _push(self, service_id: str) -> Placement:
        folder = REPOSITORY / "service-repository" / service_id
        tag = f"{run.REGISTRY}/sdi/{service_id}:cv-engine"
        log = self.root / "images" / f"{service_id}.log"
        log.parent.mkdir(exist_ok=True)
        with log.open("wb") as stream:
            for command in (
                [
                    "build",
                    "--platform",
                    "linux/amd64",
                    "-f",
                    str(folder / "Dockerfile.jazzy-debs"),
                    "-t",
                    tag,
                    str(folder),
                ],
                ["push", tag],
            ):
                subprocess.run(
                    ["docker", "-H", self.url, *command],
                    check=True,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
        return Placement(
            service_id,
            amd64_digest(self.registry_url, f"sdi/{service_id}", "cv-engine"),
        )

    def outcome(self, name: str) -> RunOutcome:
        """Run a composition on first use and record what happened."""
        if name not in self.runs:
            composition = COMPOSITIONS[name]
            request = RunRequest(
                execution_id=str(uuid.uuid4()),
                robot=composition.robot,
                placements=tuple(self.placement(item) for item in composition.services),
                counting=(
                    *(
                        CountingOutput(topic=topic, type=kind, service_id=item)
                        for item in composition.services
                        for topic, kind in [OUTPUTS[item]]
                    ),
                    *composition.probes,
                ),
                deadline_monotonic=time.monotonic() + WORK_LIMIT_SECONDS,
            )
            self.runs[name] = run_composition(request, self.url)
            _record(self.root / name, request, self.runs[name])
        return self.runs[name]

    def remaining(self) -> list[str]:
        """Name every container that still carries the CV execution label."""
        client = docker.DockerClient(base_url=self.url)
        try:
            return [
                str(item.name)
                for item in client.containers.list(
                    all=True, filters={"label": run.EXECUTION_LABEL}
                )
            ]
        finally:
            client.close()


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    return Engine(
        os.environ["SDI_CV_ENGINE_URL"],
        os.environ["SDI_CV_REGISTRY_URL"],
        tmp_path_factory.mktemp("cv-engine", numbered=False),
    )


def _record(directory: Path, request: RunRequest, outcome: RunOutcome) -> None:
    directory.mkdir()
    lines = [f"execution: {request.execution_id} robot {request.robot}"]
    lines += [f"placement: {item.reference}" for item in request.placements]
    lines += [f"outcome: {type(outcome).__name__}"]
    if isinstance(outcome, Errored):
        lines.append(f"error: {outcome.error}: {outcome.detail}")
    lines += [f"phase {name}: {seconds} s" for name, seconds in outcome.phases.items()]
    if isinstance(outcome, Finished):
        result = outcome.result
        (directory / "result.json").write_text(
            result.model_dump_json(exclude_none=True, indent=2) + "\n"
        )
        if result.mission is not None:
            lines += _mission_lines(result.mission)
        lines += [f"count {topic}: {count}" for topic, count in result.counts.items()]
        lines += [
            f"graph {item.topic} [{', '.join(item.types)}] from "
            f"{', '.join(item.publishers) or 'no publisher'}"
            for item in result.graph
        ]
    if not isinstance(outcome, (Finished, Errored)) or outcome.snapshot is None:
        (directory / "diagnostic.txt").write_text("\n".join(lines) + "\n")
        return
    snapshot = outcome.snapshot
    containers: dict[str, ContainerState | FailedToStart] = {
        "sim": snapshot.sim,
        "orchestrator": snapshot.orchestrator,
        **snapshot.placements,
    }
    for name, state in containers.items():
        if isinstance(state, ContainerState):
            lines.append(f"container {name}: {state.status} exit {state.exit_code}")
            lines += [f"--- log tail {name}", *state.log_tail]
        else:
            lines.append(f"container {name}: failed to start: {state.detail}")
    (directory / "diagnostic.txt").write_text("\n".join(lines) + "\n")
    (directory / "run.json").write_text(
        json.dumps({"phases": outcome.phases, "snapshot": asdict(snapshot)}, indent=2)
    )


def _mission_lines(mission: Mission) -> list[str]:
    setup = mission.setup
    lines = [f"setup: {setup.status} {setup.elapsed_s} s {setup.waited_on or ''}"]
    lines += [
        f"step {item.requirement_id}: {item.status} "
        f"{item.waited_on or ''} {'' if item.elapsed_s is None else item.elapsed_s}"
        for item in mission.steps
    ]
    return lines


def _passed(outcome: RunOutcome) -> Finished:
    assert isinstance(outcome, Finished), outcome
    mission = outcome.result.mission
    assert mission is not None
    assert mission.setup.status == "passed"
    assert tuple(item.status for item in mission.steps) == PASS
    assert mission.setup.elapsed_s <= SETUP_TIMEOUT / 2
    for item in mission.steps:
        if item.status == "passed":
            assert item.elapsed_s is not None
            assert item.elapsed_s <= TIMEOUTS[item.requirement_id] / 2, item
    assert all(count > 0 for count in outcome.result.counts.values())
    assert outcome.snapshot.sim.running
    for state in outcome.snapshot.placements.values():
        assert isinstance(state, ContainerState)
        assert state.running
    return outcome


def test_cv_engine_waffle_composition_passes(engine: Engine) -> None:
    _passed(engine.outcome("waffle-pass"))
    assert engine.remaining() == []


def test_cv_engine_burger_composition_passes(engine: Engine) -> None:
    _passed(engine.outcome("burger-pass"))
    assert engine.remaining() == []


def test_cv_engine_without_face_times_out_at_the_face_step(engine: Engine) -> None:
    outcome = engine.outcome("no-face")

    assert isinstance(outcome, Finished), outcome
    mission = outcome.result.mission
    assert mission is not None
    assert tuple(item.status for item in mission.steps) == NO_FACE
    assert outcome.result.counts["/speech/text"] > 0
    assert engine.remaining() == []


def test_cv_engine_silent_and_wrong_type_outputs_count_zero(engine: Engine) -> None:
    outcome = engine.outcome("no-face")

    assert isinstance(outcome, Finished), outcome
    assert outcome.result.counts[SILENT.topic] == 0
    assert outcome.result.counts[WRONG_TYPE.topic] == 0
    assert outcome.snapshot.orchestrator.exit_code == 0
    published = {item.topic for item in outcome.result.graph if item.publishers}
    assert WRONG_TYPE.topic in published


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_cv_engine_waffle_composition_passes_again(
    engine: Engine, attempt: int
) -> None:
    _passed(engine.outcome(f"waffle-repeat-{attempt}"))
    assert engine.remaining() == []


def test_cv_engine_every_run_dumps_the_ros_graph(engine: Engine) -> None:
    for name in COMPOSITIONS:
        outcome = engine.outcome(name)
        assert isinstance(outcome, Finished), (name, outcome)
        topics = {item.topic: item for item in outcome.result.graph}
        assert "/clock" in topics
        assert topics["/clock"].publishers
        diagnostic = (engine.root / name / "diagnostic.txt").read_text()
        assert "graph /clock [rosgraph_msgs/msg/Clock] from " in diagnostic
