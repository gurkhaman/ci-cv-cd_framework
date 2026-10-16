"""Implemented image-build Stage adapter: build each placement's recipe.

For every placement in the deployment schema, the adapter builds the Dockerfile
the placed artifact names, for every architecture the artifact declares,
against the stack's one BuildKit daemon, and pushes one image per placement to
the stack-local pipeline registry. It records each placement built, with its
image digests, or failed, with one reason from a closed set. The Domain outcome
is succeeded whenever every placement is recorded, even when nothing built.

The adapter runs on the image-build agent with only PATH and LC_ALL in its
environment, so the builder and registry addresses are constants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import requests
from pydantic import BaseModel, ValidationError

from ._adapter_output import atomic_write
from ._contracts import TargetExecutionProfile
from ._domain_contracts import (
    CompositionBlueprint,
    DeploymentSchema,
    ImageBuildResult,
)
from ._image_build_redaction import failure_detail, log_tail
from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import canonical_json, parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
    parse_service_repository,
)
from ._stage_contracts import (
    AdapterRequest,
    AdapterResponse,
    contains_sensitive_material,
)
from ._yaml_input import InputError, parse_yaml, validate_contract

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._domain_contracts import ServicePlacement
    from ._service_descriptions import Artifact

MAX_REQUEST_BYTES = 128 * 1024
BUILDKIT_ADDRESS = "tcp://buildkitd:1234"
REGISTRY = "registry:5000"
REGISTRY_URL = f"http://{REGISTRY}"
# More placements than this could exceed the 32 KiB result limit.
MAX_PLACEMENTS = 48
# Builds need time to cancel before the runtime's work limit kills the group.
DEADLINE_RESERVE_SECONDS = 60
CANCEL_GRACE_SECONDS = 15
# Registry reads stop this long before the reserve ends, leaving time to publish.
PUBLISH_RESERVE_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 30
REGISTRY_TIMEOUT_SECONDS = 30
POLL_SECONDS = 0.2
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)
ARCHITECTURES = {"amd64": ("amd64",), "arm64": ("arm64",), "noarch": ("amd64", "arm64")}

OUTCOMES: dict[str, tuple[str, str, str, str]] = {
    "recorded": (
        "succeeded",
        "succeeded",
        "domain",
        "Every placement was recorded as built or failed.",
    ),
    "input-mismatch": (
        "failed",
        "not_evaluated",
        "input",
        "The service repository differs from the one composition read.",
    ),
    "too-many-placements": (
        "failed",
        "not_evaluated",
        "input",
        f"The deployment schema has more than {MAX_PLACEMENTS} placements.",
    ),
    "builder-unreachable": (
        "failed",
        "not_evaluated",
        "dependency",
        "The image builder did not answer.",
    ),
    "registry-unreachable": (
        "failed",
        "not_evaluated",
        "dependency",
        "The pipeline registry did not answer.",
    ),
    "arm64-unavailable": (
        "failed",
        "not_evaluated",
        "dependency",
        "The image builder cannot build linux/arm64 images.",
    ),
}
# Fixed detail texts, keyed by the cause behind a failure reason.
DETAILS = {
    "unresolved-placement": "composition left the placement unresolved",
    "no-artifact": "the placed artifact is not in the service description",
    "no-recipe": "the artifact names no recipe in the service folder",
    "uncaptured-recipe": "the artifact recipe is not a file in the service folder",
    "build-failed": "the build failed",
    "no-digest": "the build reported no image digest",
    "unreadable-index": "the pushed image index could not be read back",
    "unread-index": "the work limit came before the pushed index was read back",
    "platform-mismatch": "the pushed image platforms differ from the requested ones",
    "work-limit": "the build was still running at the work limit",
}


@dataclass(frozen=True)
class Build:
    """One placement to build from its recipe."""

    service_id: str
    artifact_id: str
    recipe: PurePosixPath
    architectures: tuple[str, ...]


@dataclass
class Running:
    """One started `buildctl` client and the files it writes."""

    build: Build
    process: subprocess.Popen[bytes]
    log: Path
    metadata: Path


def encode_document(document: BaseModel) -> bytes:
    """Return the exact bytes the adapter writes for a Domain output."""
    return canonical_json(document.model_dump(mode="json", exclude_none=True))


def _read_input(path: Path, size: int, sha256: str) -> bytes:
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        msg = "adapter input is not a single regular file"
        raise InputError(msg)
    content = path.read_bytes()
    if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
        msg = "adapter input bytes do not match the request"
        raise InputError(msg)
    return content


def _failed(service_id: str, reason: str, detail: str) -> dict[str, object]:
    return {
        "service_id": service_id,
        "status": "failed",
        "reason": reason,
        "detail": detail,
    }


def _plan(
    placement: ServicePlacement,
    artifacts: dict[str, dict[str, Artifact]],
    captured: set[str],
) -> Build | dict[str, object]:
    """Return the build for a placement, or its failed record."""
    service_id = placement.service_id
    if placement.status == "unresolved" or placement.artifact_id is None:
        return _failed(
            service_id, "unresolved-placement", DETAILS["unresolved-placement"]
        )
    artifact = artifacts.get(service_id, {}).get(placement.artifact_id)
    if artifact is None:
        return _failed(service_id, "no-recipe", DETAILS["no-artifact"])
    if artifact.dockerfile is None:
        return _failed(service_id, "no-recipe", DETAILS["no-recipe"])
    if f"{service_id}/{artifact.dockerfile}" not in captured:
        return _failed(service_id, "no-recipe", DETAILS["uncaptured-recipe"])
    architectures = sorted(
        {
            name
            for declared in artifact.architectures
            for name in ARCHITECTURES[declared]
        }
    )
    return Build(
        service_id,
        placement.artifact_id,
        PurePosixPath(artifact.dockerfile),
        tuple(architectures),
    )


def _builder_workers(buildctl: str, environment: dict[str, str]) -> str | None:
    """Return the builder's worker listing, or None when it does not answer."""
    try:
        completed = subprocess.run(  # noqa: S603
            [buildctl, "--addr", BUILDKIT_ADDRESS, "debug", "workers"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _registry_answers() -> bool:
    try:
        response = requests.get(f"{REGISTRY_URL}/v2/", timeout=REGISTRY_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False
    return response.status_code == requests.codes.ok


def _build_services_failure(
    builds: list[Build], buildctl: str, environment: dict[str, str]
) -> str | None:
    """Return the outcome code of the first builder or registry check that fails."""
    workers = _builder_workers(buildctl, environment)
    if workers is None:
        return "builder-unreachable"
    if not _registry_answers():
        return "registry-unreachable"
    needs_arm64 = any("arm64" in build.architectures for build in builds)
    if needs_arm64 and "linux/arm64" not in workers:
        return "arm64-unavailable"
    return None


@dataclass(frozen=True)
class Builder:
    """What every `buildctl` client of one run shares."""

    buildctl: str
    repository: Path
    scratch: Path
    execution_id: str
    environment: dict[str, str]

    def start(self, build: Build) -> Running:
        """Start one placement's build, logging to the scratch directory."""
        log = self.scratch / f"{build.service_id}.log"
        metadata = self.scratch / f"{build.service_id}.metadata.json"
        with log.open("wb") as stream:
            # Same process group as the adapter, so the runtime's group kill
            # still reaches every client on a hard timeout.
            process = subprocess.Popen(  # noqa: S603
                self._command(build, metadata),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=self.environment,
                cwd=self.scratch,
            )
        return Running(build, process, log, metadata)

    def _command(self, build: Build, metadata: Path) -> list[str]:
        name = (
            f"{REGISTRY}/sdi/{build.service_id}:{build.artifact_id}-{self.execution_id}"
        )
        platforms = ",".join(f"linux/{item}" for item in build.architectures)
        # The service folder is the build context; the recipe may sit below it.
        context = self.repository / build.service_id
        return [
            self.buildctl,
            "--addr",
            BUILDKIT_ADDRESS,
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            f"context={context}",
            "--local",
            f"dockerfile={context / build.recipe.parent}",
            "--opt",
            f"filename={build.recipe.name}",
            "--opt",
            f"platform={platforms}",
            "--output",
            f"type=image,name={name},push=true,registry.insecure=true",
            "--metadata-file",
            str(metadata),
            "--progress=plain",
        ]


def _built_digest(running: Running) -> str | None:
    try:
        metadata: object = json.loads(running.metadata.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    digest = cast("dict[str, object]", metadata).get("containerimage.digest")
    return digest if isinstance(digest, str) else None


def _index_platforms(
    service_id: str, digest: str, timeout: float
) -> dict[str, str] | None:
    """Read a pushed index back and return its digest per architecture."""
    try:
        response = requests.get(
            f"{REGISTRY_URL}/v2/sdi/{service_id}/manifests/{digest}",
            headers={"Accept": MANIFEST_ACCEPT},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    body = response.content
    if (
        response.status_code != requests.codes.ok
        or f"sha256:{hashlib.sha256(body).hexdigest()}" != digest
    ):
        return None
    try:
        manifests = json.loads(body)["manifests"]
        platforms: dict[str, str] = {}
        for entry in manifests:
            platform = entry["platform"]
            # BuildKit may list attestation manifests as unknown/unknown.
            if platform["os"] == "linux":
                platforms[platform["architecture"]] = entry["digest"]
    except (ValueError, KeyError, TypeError):
        return None
    return platforms


def _finished_record(running: Running, read_cutoff: float) -> dict[str, object]:
    """Return the record of a build whose client has exited."""
    build = running.build
    if running.process.returncode != 0:
        return _failed(
            build.service_id,
            "build-failed",
            failure_detail(running.log, fallback=DETAILS["build-failed"]),
        )
    digest = _built_digest(running)
    if digest is None:
        return _failed(build.service_id, "build-failed", DETAILS["no-digest"])
    if len(build.architectures) == 1:
        platforms = {build.architectures[0]: digest}
    else:
        timeout = min(REGISTRY_TIMEOUT_SECONDS, read_cutoff - time.monotonic())
        if timeout <= 0:
            return _failed(build.service_id, "work-limit", DETAILS["unread-index"])
        indexed = _index_platforms(build.service_id, digest, timeout)
        if indexed is None:
            return _failed(
                build.service_id, "build-failed", DETAILS["unreadable-index"]
            )
        platforms = indexed
    if sorted(platforms) != list(build.architectures):
        return _failed(build.service_id, "build-failed", DETAILS["platform-mismatch"])
    return {
        "service_id": build.service_id,
        "status": "built",
        "repository": f"sdi/{build.service_id}",
        "digest": digest,
        "platforms": [
            {"architecture": name, "digest": platforms[name]}
            for name in build.architectures
        ],
    }


def _stop(running: list[Running]) -> None:
    """Signal every client still running, then reap them all."""
    for item in running:
        if item.process.poll() is None:
            item.process.send_signal(signal.SIGTERM)
    grace_ends = time.monotonic() + CANCEL_GRACE_SECONDS
    for item in running:
        try:
            item.process.wait(timeout=max(0.0, grace_ends - time.monotonic()))
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait()


def _run_builds(
    builds: list[Build],
    *,
    deadline: float,
    builder: Builder,
) -> tuple[dict[str, dict[str, object]], list[Running]]:
    """Run every build at once and record each until the deadline."""
    records: dict[str, dict[str, object]] = {}
    running: list[Running] = []
    # Reads may run past the deadline, but never into the cancel grace.
    read_cutoff = (
        deadline
        + DEADLINE_RESERVE_SECONDS
        - CANCEL_GRACE_SECONDS
        - PUBLISH_RESERVE_SECONDS
    )
    if time.monotonic() >= deadline:
        # A work limit inside the reserve leaves no time to build at all.
        for build in builds:
            records[build.service_id] = _failed(
                build.service_id, "work-limit", DETAILS["work-limit"]
            )
        return records, running
    try:
        # One at a time, so a failed start still stops the ones before it.
        for build in builds:
            running.append(builder.start(build))  # noqa: PERF401
        pending = list(running)
        while pending and time.monotonic() < deadline:
            for item in list(pending):
                if item.process.poll() is not None:
                    pending.remove(item)
                    records[item.build.service_id] = _finished_record(item, read_cutoff)
            if pending:
                time.sleep(POLL_SECONDS)
        # A client that finished before it was signalled still counts.
        finished = [item for item in pending if item.process.poll() is not None]
        unfinished = [item for item in pending if item not in finished]
        _stop(unfinished)
        for item in finished:
            records[item.build.service_id] = _finished_record(item, read_cutoff)
        for item in unfinished:
            records[item.build.service_id] = _failed(
                item.build.service_id, "work-limit", DETAILS["work-limit"]
            )
    finally:
        _stop(running)
    return records, running


class _Run:
    """One adapter invocation and the candidate files it publishes."""

    def __init__(self, request: AdapterRequest, output_root: Path) -> None:
        self.request = request
        self.output_root = output_root
        self.outputs = {item.slot: item.path for item in request.outputs}
        self.records: list[dict[str, object]] = []
        # Redacted build-log tails, read before the scratch directory goes.
        self.tails: list[bytes] = []
        self.tails_cut = False

    def publish(self, code: str, result: ImageBuildResult | None) -> None:
        """Publish the result, the diagnostic, then the response."""
        conclusion, domain_outcome, category, summary = OUTCOMES[code]
        if result is not None:
            atomic_write(
                self.output_root,
                self.outputs["image_build_result"],
                encode_document(result),
            )
        diagnostic, truncated = self._diagnostic(code)
        atomic_write(self.output_root, self.request.diagnostic.path, diagnostic)
        response = AdapterResponse.model_validate(
            {
                "schema_version": "sdi.stage-adapter-response/v1",
                "correlation": self.request.correlation.model_dump(mode="json"),
                "execution_conclusion": conclusion,
                "domain_outcome": domain_outcome,
                "reason": {
                    "category": category,
                    "code": f"sdi.image-build.{code}",
                    "summary": summary,
                },
                "consumed_inputs": [item.slot for item in self.request.inputs],
                "produced_outputs": [] if result is None else ["image_build_result"],
                "diagnostic": {"present": True, "truncated": truncated},
            },
            strict=True,
            extra="forbid",
        )
        atomic_write(
            self.output_root,
            "response.json",
            canonical_json(response.model_dump(mode="json")),
        )

    def _header(self, code: str) -> bytes:
        lines = [f"outcome: sdi.image-build.{code}"]
        for record in self.records:
            if record["status"] == "built":
                lines.append(f"{record['service_id']}: built {record['digest']}")
            else:
                lines.append(
                    f"{record['service_id']}: {record['reason']}: {record['detail']}"
                )
        return "".join(f"{line}\n" for line in lines).encode()

    def read_tails(self, logs: dict[str, Path]) -> None:
        """Keep a fair share of each failed build's log tail."""
        failed = [
            (str(record["service_id"]), logs[str(record["service_id"])])
            for record in self.records
            if record["status"] == "failed" and record["service_id"] in logs
        ]
        if not failed:
            return
        room = self.request.diagnostic.max_bytes - len(self._header("recorded"))
        share = room // len(failed)
        for service_id, log in failed:
            title = f"--- {service_id} build log tail\n".encode()
            if share <= len(title):
                self.tails_cut = True
                continue
            tail = log_tail(log, max_bytes=share - len(title))
            self.tails_cut = self.tails_cut or tail.cut
            self.tails.append(
                title + "".join(f"{line}\n" for line in tail.lines).encode()
            )

    def _diagnostic(self, code: str) -> tuple[bytes, bool]:
        """Return each record, then the failed builds' log tails."""
        limit = self.request.diagnostic.max_bytes
        header = self._header(code)
        if len(header) > limit:
            return header[: header.rfind(b"\n", 0, limit) + 1], True
        content = header + b"".join(self.tails)
        # Every line was checked alone; this checks what joining them made.
        if len(content) > limit or contains_sensitive_material(content.decode()):
            return header, bool(self.tails)
        return content, self.tails_cut


def _merge(
    plans: list[Build | dict[str, object]], built: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Return one record per placement, in deployment-schema order."""
    return [
        built[plan.service_id] if isinstance(plan, Build) else plan for plan in plans
    ]


def _build_environment(scratch: Path) -> dict[str, str]:
    """Return buildctl's whole environment: no inherited credentials."""
    config = scratch / "docker-config"
    config.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": str(scratch),
        "DOCKER_CONFIG": str(config),
    }


def _image_build(
    run: _Run, input_root: Path, deadline: float
) -> tuple[str, ImageBuildResult | None]:
    """Return the outcome code and the result the run publishes."""
    request = run.request
    declared = {item.slot: item for item in request.inputs}
    contents = {
        slot: _read_input(input_root / item.path, item.byte_size, item.sha256)
        for slot, item in declared.items()
    }
    paths = {slot: item.path for slot, item in declared.items()}
    manifest = validate_contract(
        ServiceRepositoryManifest,
        parse_json(
            contents["service_repository"],
            paths["service_repository"],
            max_bytes=len(contents["service_repository"]),
        ),
        paths["service_repository"],
    )
    repository_root = input_root / SERVICE_REPOSITORY_ROOT
    snapshot = capture_service_repository(repository_root)
    if snapshot.manifest != manifest:
        msg = "copied service folders do not match the service-repository manifest"
        raise InputError(msg)
    validate_contract(
        TargetExecutionProfile,
        parse_yaml(contents["target_profile"], paths["target_profile"]),
        paths["target_profile"],
    )
    validate_contract(
        CompositionBlueprint,
        parse_json(
            contents["composition_blueprint"],
            paths["composition_blueprint"],
            max_bytes=len(contents["composition_blueprint"]),
        ),
        paths["composition_blueprint"],
    )
    deployment = validate_contract(
        DeploymentSchema,
        parse_json(
            contents["deployment_schema"],
            paths["deployment_schema"],
            max_bytes=len(contents["deployment_schema"]),
        ),
        paths["deployment_schema"],
    )
    composed = {item.slot: item.sha256 for item in deployment.source_inputs}
    if composed.get("service_repository") != declared["service_repository"].sha256:
        return "input-mismatch", None
    if len(deployment.placements) > MAX_PLACEMENTS:
        return "too-many-placements", None

    artifacts = {
        file.description.service_id: file.description.artifacts or {}
        for file in parse_service_repository(snapshot.files)
    }
    plans = [
        _plan(placement, artifacts, set(snapshot.files))
        for placement in deployment.placements
    ]
    builds = [plan for plan in plans if isinstance(plan, Build)]
    built: dict[str, dict[str, object]] = {}
    if builds:
        with tempfile.TemporaryDirectory(prefix="sdi-image-build-") as directory:
            scratch = Path(directory)
            environment = _build_environment(scratch)
            buildctl = shutil.which("buildctl", path=environment["PATH"])
            if buildctl is None:
                return "builder-unreachable", None
            failure = _build_services_failure(builds, buildctl, environment)
            if failure is not None:
                return failure, None
            built, running = _run_builds(
                builds,
                deadline=deadline,
                builder=Builder(
                    buildctl,
                    repository_root,
                    scratch,
                    request.correlation.execution_id,
                    environment,
                ),
            )
            run.records = _merge(plans, built)
            run.read_tails({item.build.service_id: item.log for item in running})
    else:
        run.records = _merge(plans, {})
    identifier = (
        f"{deployment.scenario_id}-{deployment.testcase_id}-"
        f"{deployment.combination_id}-image-build"
    ).lower()
    result = validate_contract(
        ImageBuildResult,
        {
            "schema_version": "sdi.image-build-result/v1",
            "evidence_basis": "implemented",
            "image_build_result_id": identifier,
            "blueprint_id": deployment.blueprint_id,
            "deployment_schema_id": deployment.deployment_schema_id,
            "scenario_id": deployment.scenario_id,
            "testcase_id": deployment.testcase_id,
            "combination_id": deployment.combination_id,
            "profile_id": deployment.profile_id,
            "source_inputs": [
                {"slot": item.slot, "sha256": item.sha256} for item in request.inputs
            ],
            "registry": REGISTRY,
            "images": run.records,
        },
        "image-build-result.json",
    )
    return "recorded", result


def run_adapter(*, request_path: Path, input_root: Path, output_root: Path) -> None:
    """Publish one complete candidate response after all candidate files."""
    started = time.monotonic()
    enforce_domain_execution_boundary(expected_label="image-build")
    try:
        parsed = parse_json(
            request_path.read_bytes(), str(request_path), max_bytes=MAX_REQUEST_BYTES
        )
    except OSError as error:
        msg = "adapter request cannot be read"
        raise InputError(msg) from error
    request = validate_contract(AdapterRequest, parsed, str(request_path))
    if request.correlation.stage != "image_build":
        msg = "the image-build adapter serves only the image_build Stage"
        raise InputError(msg)
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)
    deadline = started + request.work_limit_seconds - DEADLINE_RESERVE_SECONDS
    run = _Run(request, output_root)
    run.publish(*_image_build(run, input_root, deadline))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdi-image-build-adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Expose only the stable one-shot Stage-adapter operation."""
    try:
        enforce_domain_execution_boundary()
        arguments = _parser().parse_args(argv)
        run_adapter(
            request_path=arguments.request,
            input_root=arguments.input_root,
            output_root=arguments.output_root,
        )
    except (InputError, OSError, ValidationError) as error:
        sys.stderr.write(f"sdi-image-build-adapter: {type(error).__name__}\n")
        return 2
    return 0
