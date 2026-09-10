"""Deep module for one transactional Stage-adapter attempt."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._domain_contracts import CompositionBlueprint, DeploymentSchema
from ._git_input import CommittedBlob, GitRepository, validate_repository_path
from ._json_input import parse_json
from ._run_input import PROTECTED_MAIN_REF, identify_committed_run
from ._stage_contracts import (
    AcceptedAttemptEnvelope,
    AdapterDescriptor,
    AdapterRequest,
    AdapterResponse,
    StageProfile,
)
from ._yaml_input import InputError, parse_yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_RESPONSE_BYTES = 64 * 1024
MAX_CANDIDATE_ENTRIES = 32
MAX_CANDIDATE_DEPTH = 4
ASCII_CONTROL_LIMIT = 32
ADAPTER_SHUTDOWN_GRACE_SECONDS = 10


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _validate_yaml_model[ModelT: BaseModel](
    model: type[ModelT], blob: CommittedBlob
) -> ModelT:
    try:
        return model.model_validate(
            parse_yaml(blob.content, blob.path), strict=True, extra="forbid"
        )
    except ValidationError as error:
        msg = f"{blob.path}: contract validation failed: {error}"
        raise InputError(msg) from error


def _write_file(root: Path, relative_path: str, content: bytes) -> None:
    validate_repository_path(relative_path)
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _capture_tree(  # noqa: C901, PLR0915
    root: Path,
    expected_root_identity: tuple[int, int],
    grants: Mapping[str, int],
) -> tuple[set[str], dict[str, bytes]]:
    paths: set[str] = set()
    folded_paths: set[str] = set()
    captured: dict[str, bytes] = {}
    flags = os.O_RDONLY | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | os.O_DIRECTORY | no_follow
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as error:
        msg = "candidate bundle root was replaced"
        raise InputError(msg) from error
    root_metadata = os.fstat(root_descriptor)
    if (root_metadata.st_dev, root_metadata.st_ino) != expected_root_identity:
        os.close(root_descriptor)
        msg = "candidate bundle root was replaced"
        raise InputError(msg)

    def walk(  # noqa: C901, PLR0912, PLR0915
        directory_descriptor: int, prefix: str, depth: int
    ) -> None:
        if depth > MAX_CANDIDATE_DEPTH:
            msg = "candidate bundle directory depth exceeds its limit"
            raise InputError(msg)
        for name in os.listdir(directory_descriptor):
            relative = f"{prefix}/{name}" if prefix else name
            validate_repository_path(relative)
            folded = relative.casefold()
            if folded in folded_paths:
                msg = "candidate bundle contains case-colliding paths"
                raise InputError(msg)
            folded_paths.add(folded)
            paths.add(relative)
            if len(paths) > MAX_CANDIDATE_ENTRIES:
                msg = "candidate bundle contains too many entries"
                raise InputError(msg)
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if metadata.st_dev != root_metadata.st_dev:
                msg = "candidate bundle crosses a filesystem device"
                raise InputError(msg)
            if stat.S_ISLNK(metadata.st_mode):
                msg = "candidate bundle contains a symbolic link"
                raise InputError(msg)
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = os.open(
                    name,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        msg = "candidate bundle changed during traversal"
                        raise InputError(msg)
                    walk(child_descriptor, relative, depth + 1)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                msg = "candidate bundle contains a non-regular file"
                raise InputError(msg)
            if metadata.st_nlink != 1:
                msg = "candidate bundle contains a hard-linked file"
                raise InputError(msg)
            limit = grants.get(relative)
            if limit is None:
                msg = "candidate bundle contains undeclared files"
                raise InputError(msg)
            if metadata.st_size > limit:
                msg = f"candidate bundle file exceeds its {limit}-byte grant"
                raise InputError(msg)
            file_descriptor = os.open(
                name,
                flags | no_follow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
            try:
                before = os.fstat(file_descriptor)
                if (before.st_dev, before.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    msg = "candidate bundle changed during capture"
                    raise InputError(msg)
                content = b""
                while len(content) <= limit:
                    chunk = os.read(
                        file_descriptor,
                        min(65536, limit + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content += chunk
                after = os.fstat(file_descriptor)
            finally:
                os.close(file_descriptor)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            ):
                msg = "candidate bundle changed while being captured"
                raise InputError(msg)
            if len(content) != before.st_size or len(content) > limit:
                msg = "candidate bundle file changed size while being captured"
                raise InputError(msg)
            captured[relative] = content

    try:
        walk(root_descriptor, "", 1)
    finally:
        os.close(root_descriptor)
    return paths, captured


def _expected_directories(files: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in files:
        parent = Path(path).parent
        while parent != Path():
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _capture_candidate(  # noqa: C901, PLR0912, PLR0915
    output_root: Path,
    output_root_identity: tuple[int, int],
    request: AdapterRequest,
    implementation_mode: str,
    identified: Mapping[str, Any],
) -> tuple[AdapterResponse, dict[str, bytes]]:
    grants = {item.path: item.max_bytes for item in request.outputs}
    grants[request.diagnostic.path] = request.diagnostic.max_bytes
    grants["response.json"] = MAX_RESPONSE_BYTES
    inventory_paths, captured = _capture_tree(
        output_root,
        output_root_identity,
        grants,
    )
    if "response.json" not in captured:
        msg = "candidate bundle did not publish response.json"
        raise InputError(msg)
    try:
        response = AdapterResponse.model_validate(
            parse_json(
                captured["response.json"],
                "response.json",
                max_bytes=MAX_RESPONSE_BYTES,
            ),
            strict=True,
            extra="forbid",
        )
    except ValidationError as error:
        msg = f"candidate response contract validation failed: {error}"
        raise InputError(msg) from error
    if response.correlation != request.correlation:
        msg = "candidate response correlation does not match the request"
        raise InputError(msg)
    if implementation_mode == "fixture" and (
        response.domain_outcome != "not_evaluated"
        or response.reason is None
        or response.reason.category != "fixture"
    ):
        msg = "Fixture response must preserve its evidence limits"
        raise InputError(msg)

    input_slots = [item.slot for item in request.inputs]
    output_by_slot = {item.slot: item for item in request.outputs}
    if not set(response.consumed_inputs) <= set(input_slots):
        msg = "candidate response names an undeclared input slot"
        raise InputError(msg)
    if not set(response.produced_outputs) <= set(output_by_slot):
        msg = "candidate response names an ungranted output slot"
        raise InputError(msg)
    if response.execution_conclusion == "succeeded":
        if set(response.consumed_inputs) != set(input_slots):
            msg = "candidate response did not consume every declared input"
            raise InputError(msg)
        required_outputs = {item.slot for item in request.outputs if item.required}
        if not required_outputs <= set(response.produced_outputs):
            msg = "candidate response did not produce every required output"
            raise InputError(msg)
    expected_files = {
        "response.json",
        *(output_by_slot[slot].path for slot in response.produced_outputs),
    }
    if response.diagnostic.present:
        expected_files.add(request.diagnostic.path)
    expected_paths = expected_files | _expected_directories(expected_files)
    if inventory_paths != expected_paths:
        msg = "candidate bundle inventory does not match its response"
        raise InputError(msg)

    validated_outputs: dict[str, BaseModel] = {}
    output_models: dict[str, type[BaseModel]] = {
        "sdi.composition-blueprint/v1": CompositionBlueprint,
        "sdi.deployment-schema/v1": DeploymentSchema,
    }
    for slot in response.produced_outputs:
        grant = output_by_slot[slot]
        model = output_models.get(grant.schema_version)
        if model is None:
            msg = "candidate output uses an unsupported schema version"
            raise InputError(msg)
        try:
            validated_outputs[slot] = model.model_validate(
                parse_json(captured[grant.path], grant.path, max_bytes=grant.max_bytes),
                strict=True,
                extra="forbid",
            )
        except ValidationError as error:
            msg = f"candidate output contract validation failed: {slot}: {error}"
            raise InputError(msg) from error

    if response.diagnostic.present:
        try:
            diagnostic = captured[request.diagnostic.path].decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as error:
            msg = "candidate diagnostic is not valid UTF-8"
            raise InputError(msg) from error
        if "\0" in diagnostic or any(
            ord(character) < ASCII_CONTROL_LIMIT and character not in "\t\r\n"
            for character in diagnostic
        ):
            msg = "candidate diagnostic contains unsanitized control characters"
            raise InputError(msg)

    if response.execution_conclusion == "succeeded":
        _validate_composition_outputs(validated_outputs, request, identified)
    second_paths, second_capture = _capture_tree(
        output_root,
        output_root_identity,
        grants,
    )
    if second_paths != inventory_paths or second_capture != captured:
        msg = "candidate bundle changed after capture"
        raise InputError(msg)
    return response, captured


def _validate_composition_outputs(
    outputs: Mapping[str, BaseModel],
    request: AdapterRequest,
    identified: Mapping[str, Any],
) -> None:
    blueprint = outputs.get("composition_blueprint")
    deployment = outputs.get("deployment_schema")
    if not isinstance(blueprint, CompositionBlueprint) or not isinstance(
        deployment, DeploymentSchema
    ):
        msg = "composition candidate must contain both Domain outputs"
        raise InputError(msg)
    expected_identity = (
        identified["scenario_id"],
        identified["testcase_id"],
        identified["combination_id"],
        identified["profile_id"],
    )
    for output in (blueprint, deployment):
        actual_identity = (
            output.scenario_id,
            output.testcase_id,
            output.combination_id,
            output.profile_id,
        )
        if actual_identity != expected_identity:
            msg = "candidate Domain output correlation does not match accepted inputs"
            raise InputError(msg)
        if {item.slot: item.sha256 for item in output.source_inputs} != {
            item.slot: item.sha256 for item in request.inputs
        }:
            msg = "candidate Domain output input digests do not match the request"
            raise InputError(msg)
    if deployment.blueprint_id != blueprint.blueprint_id:
        msg = "deployment schema does not reference the accepted blueprint"
        raise InputError(msg)
    if {item.service_id for item in deployment.placements} != {
        item.service_id for item in blueprint.services
    }:
        msg = "deployment schema must place every accepted blueprint service once"
        raise InputError(msg)


def _accepted_envelope(  # noqa: PLR0913
    *,
    descriptor: AdapterDescriptor,
    request: AdapterRequest,
    response: AdapterResponse,
    identified: Mapping[str, Any],
    captured: Mapping[str, bytes],
    started_at: datetime,
    finished_at: datetime,
) -> AcceptedAttemptEnvelope:
    output_by_slot = {item.slot: item for item in request.outputs}
    accepted_files: list[dict[str, object]] = []
    for slot in response.produced_outputs:
        grant = output_by_slot[slot]
        content = captured[grant.path]
        accepted_files.append(
            {
                "role": "domain_output",
                "slot": slot,
                "path": grant.path,
                "media_type": grant.media_type,
                "schema_version": grant.schema_version,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if response.diagnostic.present:
        content = captured[request.diagnostic.path]
        accepted_files.append(
            {
                "role": "diagnostic",
                "slot": "diagnostic",
                "path": request.diagnostic.path,
                "media_type": request.diagnostic.media_type,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "truncated": response.diagnostic.truncated,
            }
        )
    provenance = {item["schema_version"]: item for item in identified["inputs"]}
    accepted_inputs: list[dict[str, object]] = []
    for declared in request.inputs:
        source = provenance[declared.schema_version]
        accepted_inputs.append(
            {
                "slot": declared.slot,
                "source_path": source["path"],
                "schema_version": source["schema_version"],
                "byte_size": source["byte_size"],
                "sha256": source["sha256"],
            }
        )
    envelope_data: dict[str, object] = {
        "schema_version": "sdi.accepted-attempt-envelope/v1",
        "correlation": request.correlation.model_dump(mode="json"),
        "lifecycle_state": "completed",
        "execution_conclusion": response.execution_conclusion,
        "implementation_mode": descriptor.implementation_mode,
        "domain_outcome": response.domain_outcome,
        "process": {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
            "exit_code": 0,
        },
        "accepted_inputs": accepted_inputs,
        "accepted_files": accepted_files,
    }
    if response.reason is not None:
        envelope_data["reason"] = response.reason.model_dump(mode="json")
    return AcceptedAttemptEnvelope.model_validate(
        envelope_data,
        strict=True,
        extra="forbid",
    )


def _stage_inputs(
    repository: GitRepository,
    identified: Mapping[str, Any],
    profile: StageProfile,
    input_root: Path,
) -> list[dict[str, object]]:
    provenance = identified["inputs"]
    if len(profile.inputs) != len(provenance):
        msg = "Stage profile input grants do not match accepted run inputs"
        raise InputError(msg)
    declared_inputs: list[dict[str, object]] = []
    input_models: dict[str, type[BaseModel]] = {
        "sdi.pipeline-integration-run-request/v1": RunRequest,
        "sdi.mobility-requirements-specification/v1": (
            MobilityRequirementsSpecification
        ),
        "sdi.target-execution-profile/v1": TargetExecutionProfile,
    }
    provenance_by_schema = {item["schema_version"]: item for item in provenance}
    for grant in profile.inputs:
        source = provenance_by_schema.get(grant.schema_version)
        model = input_models.get(grant.schema_version)
        if source is None or model is None:
            msg = "Stage input grant does not match an accepted input slot"
            raise InputError(msg)
        blob = repository.read_regular_file(source["path"])
        _validate_yaml_model(model, blob)
        digest = hashlib.sha256(blob.content).hexdigest()
        if len(blob.content) != source["byte_size"] or digest != source["sha256"]:
            msg = "accepted input provenance changed before Stage execution"
            raise InputError(msg)
        _write_file(input_root, grant.path, blob.content)
        declared_inputs.append(
            {
                **grant.model_dump(mode="json"),
                "byte_size": len(blob.content),
                "sha256": digest,
            }
        )
    return declared_inputs


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_process_group(
    process: subprocess.Popen[bytes], process_group: int
) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + ADAPTER_SHUTDOWN_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(process_group):
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def execute_stage(  # noqa: C901, PLR0913, PLR0915
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    descriptor_path: str,
    attempt_root: Path,
) -> dict[str, Any]:
    """Execute and transactionally accept one descriptor-selected Stage attempt."""
    if requested_ref != PROTECTED_MAIN_REF:
        msg = f"requested ref must be {PROTECTED_MAIN_REF}"
        raise InputError(msg)
    validate_repository_path(descriptor_path)
    if attempt_root.exists():
        msg = "attempt root must not already exist"
        raise InputError(msg)
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
    )
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)
    descriptor_blob = repository.read_regular_file(descriptor_path)
    descriptor = _validate_yaml_model(AdapterDescriptor, descriptor_blob)
    profile_blob = repository.read_regular_file(descriptor.stage_profile)
    if (
        hashlib.sha256(profile_blob.content).hexdigest()
        != descriptor.stage_profile_sha256
    ):
        msg = "Stage profile bytes do not match the reviewed descriptor digest"
        raise InputError(msg)
    profile = _validate_yaml_model(StageProfile, profile_blob)
    if (
        profile.stage != descriptor.stage
        or profile.profile_version != descriptor.stage_profile_version
    ):
        msg = "Stage profile identity does not match the reviewed descriptor"
        raise InputError(msg)

    attempt_root.parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(
        tempfile.mkdtemp(prefix=f".{attempt_root.name}.work-", dir=attempt_root.parent)
    )
    accepted_staging: Path | None = None
    try:
        input_root = work_root / "inputs"
        output_root = work_root / "candidate"
        input_root.mkdir()
        output_root.mkdir()
        output_root_metadata = output_root.lstat()
        output_root_identity = (
            output_root_metadata.st_dev,
            output_root_metadata.st_ino,
        )
        declared_inputs = _stage_inputs(repository, identified, profile, input_root)
        request = AdapterRequest.model_validate(
            {
                "schema_version": "sdi.stage-adapter-request/v1",
                "correlation": {
                    "execution_id": identified["execution_id"],
                    "stage": descriptor.stage,
                    "attempt_number": 1,
                },
                "work_limit_seconds": profile.work_limit_seconds,
                "inputs": declared_inputs,
                "outputs": [item.model_dump(mode="json") for item in profile.outputs],
                "diagnostic": profile.diagnostic.model_dump(mode="json"),
            },
            strict=True,
            extra="forbid",
        )
        request_path = work_root / "request.json"
        _write_file(
            work_root,
            "request.json",
            _canonical_json(request.model_dump(mode="json")),
        )
        started_at = datetime.now(UTC)
        process = subprocess.Popen(  # noqa: S603
            [
                descriptor.entrypoint,
                "run",
                "--request",
                str(request_path),
                "--input-root",
                str(input_root),
                "--output-root",
                str(output_root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LC_ALL": "C.UTF-8",
            },
            start_new_session=True,
        )
        process_group = process.pid
        try:
            return_code = process.wait(timeout=profile.work_limit_seconds)
        except subprocess.TimeoutExpired as error:
            _terminate_process_group(process, process_group)
            msg = "Stage adapter exceeded its work limit"
            raise InputError(msg) from error
        finished_at = datetime.now(UTC)
        if _process_group_exists(process_group):
            _terminate_process_group(process, process_group)
            msg = "Stage adapter left descendant processes after exit"
            raise InputError(msg)
        if return_code != 0:
            msg = "Stage adapter did not publish a candidate response successfully"
            raise InputError(msg)
        response, captured = _capture_candidate(
            output_root,
            output_root_identity,
            request,
            descriptor.implementation_mode,
            identified,
        )
        envelope = _accepted_envelope(
            descriptor=descriptor,
            request=request,
            response=response,
            identified=identified,
            captured=captured,
            started_at=started_at,
            finished_at=finished_at,
        )

        accepted_staging = Path(
            tempfile.mkdtemp(
                prefix=f".{attempt_root.name}.accepted-", dir=attempt_root.parent
            )
        )
        output_by_slot = {item.slot: item for item in request.outputs}
        for slot in response.produced_outputs:
            grant = output_by_slot[slot]
            _write_file(accepted_staging, grant.path, captured[grant.path])
        if response.diagnostic.present:
            _write_file(
                accepted_staging,
                request.diagnostic.path,
                captured[request.diagnostic.path],
            )
        _write_file(
            accepted_staging,
            "accepted-attempt.json",
            _canonical_json(envelope.model_dump(mode="json")),
        )
        accepted_staging.replace(attempt_root)
        accepted_staging = None
        return envelope.model_dump(mode="json")
    finally:
        if accepted_staging is not None:
            shutil.rmtree(accepted_staging, ignore_errors=True)
        shutil.rmtree(work_root, ignore_errors=True)
