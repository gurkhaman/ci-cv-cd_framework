"""Authoritative contracts for the language-neutral Stage-adapter seam."""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    ContractModel,
    NonBlank,
    RepositoryPath,
    Slug,
)
from ._git_input import validate_repository_path

ADAPTER_DESCRIPTOR_SCHEMA_VERSION = "sdi.adapter-descriptor/v1"
STAGE_PROFILE_SCHEMA_VERSION = "sdi.stage-profile/v1"
ADAPTER_REQUEST_SCHEMA_VERSION = "sdi.stage-adapter-request/v1"
ADAPTER_RESPONSE_SCHEMA_VERSION = "sdi.stage-adapter-response/v1"
ACCEPTED_ATTEMPT_SCHEMA_VERSION = "sdi.accepted-attempt-envelope/v1"
FIXTURE_CASE_SCHEMA_VERSION = "sdi.fixture-case/v1"
PROCESS_CONTRACT_VERSION = "sdi.stage-adapter-process/v1"
COMPOSITION_PROFILE_VERSION = "sdi.composition-stage-profile/v1"

type StageName = Literal["composition", "image_build", "cv", "cd"]
type ImplementationMode = Literal["fixture", "implemented"]
type ExecutionConclusion = Literal["succeeded", "failed"]
type DomainOutcome = Literal["succeeded", "failed", "not_evaluated"]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
SlotName = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        max_length=63,
    ),
]
ExecutionId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ),
]
BoundedText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
ReasonCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^sdi\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$",
        max_length=127,
    ),
]
ImageReference = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$",
        max_length=255,
    ),
]

COMPOSITION_INPUT_SLOTS = {
    "run_request",
    "requirements_specification",
    "target_profile",
}
COMPOSITION_OUTPUT_SLOTS = {"composition_blueprint", "deployment_schema"}
RESERVED_RESPONSE_PATH = "response.json"
RESERVED_ACCEPTED_ENVELOPE_PATH = "accepted-attempt.json"
EXPECTED_COMPOSITION_INPUTS = {
    "run_request": (
        "run-request.yaml",
        "application/yaml",
        "sdi.pipeline-integration-run-request/v1",
    ),
    "requirements_specification": (
        "requirements-specification.yaml",
        "application/yaml",
        "sdi.mobility-requirements-specification/v1",
    ),
    "target_profile": (
        "target-profile.yaml",
        "application/yaml",
        "sdi.target-execution-profile/v1",
    ),
}
EXPECTED_COMPOSITION_OUTPUTS = {
    "composition_blueprint": (
        "outputs/composition-blueprint.json",
        "application/json",
        "sdi.composition-blueprint/v1",
    ),
    "deployment_schema": (
        "outputs/deployment-schema.json",
        "application/json",
        "sdi.deployment-schema/v1",
    ),
}
MAX_ARTIFACT_PATH_BYTES = 512
MAX_ARTIFACT_COMPONENT_BYTES = 255
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127


def _validate_confined_path(path: str) -> str:
    validate_repository_path(path)
    if len(path.encode()) > MAX_ARTIFACT_PATH_BYTES or any(
        len(part.encode()) > MAX_ARTIFACT_COMPONENT_BYTES for part in path.split("/")
    ):
        msg = "artifact path exceeds its bounded length"
        raise ValueError(msg)
    return path


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


def _reject_null_reason(data: object) -> object:
    if isinstance(data, dict):
        values = cast("dict[object, object]", data)
        if "reason" in values and values["reason"] is None:
            msg = "reason must be omitted when absent"
            raise ValueError(msg)
    return cast("object", data)


def _require_unique_paths(paths: list[str]) -> None:
    _require_unique(paths, "path")
    folded = [path.casefold() for path in paths]
    if len(folded) != len(set(folded)):
        msg = "artifact paths must not collide by case"
        raise ValueError(msg)
    parts = [path.split("/") for path in paths]
    for index, left in enumerate(parts):
        for right in parts[index + 1 :]:
            shorter, longer = sorted((left, right), key=len)
            if longer[: len(shorter)] == shorter:
                msg = "an artifact file path cannot contain another artifact file"
                raise ValueError(msg)


class Correlation(ContractModel):
    """Stable project correlation carried across one Stage attempt."""

    execution_id: ExecutionId
    stage: StageName
    attempt_number: PositiveInt


class Reason(ContractModel):
    """Bounded, sanitized process or Domain reason."""

    category: Literal["fixture", "input", "dependency", "tool", "adapter", "domain"]
    code: ReasonCode
    summary: BoundedText

    @field_validator("summary")
    @classmethod
    def reject_control_characters(cls, summary: str) -> str:
        if any(
            ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
            for character in summary
        ):
            msg = "reason summary must not contain control characters"
            raise ValueError(msg)
        return summary


class AdapterDescriptor(ContractModel):
    """Reviewed authority selecting one trusted Stage adapter."""

    schema_version: Literal["sdi.adapter-descriptor/v1"]
    stage: StageName
    implementation_mode: ImplementationMode
    image: ImageReference
    entrypoint: NonBlank
    process_contract_version: Literal["sdi.stage-adapter-process/v1"]
    stage_profile: RepositoryPath
    stage_profile_version: NonBlank
    stage_profile_sha256: Sha256
    agent_label: Slug
    secret_bindings: list[Slug]

    @field_validator("stage_profile")
    @classmethod
    def validate_profile_path(cls, path: str) -> str:
        return _validate_confined_path(path)

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        _require_unique(self.secret_bindings, "secret binding")
        if self.implementation_mode == "fixture" and self.secret_bindings:
            msg = "Fixture adapters cannot receive secret bindings"
            raise ValueError(msg)
        if (
            self.stage == "composition"
            and self.stage_profile_version != COMPOSITION_PROFILE_VERSION
        ):
            msg = "composition descriptor must select the composition profile version"
            raise ValueError(msg)
        return self


class InputGrant(ContractModel):
    """One input file granted to an adapter."""

    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class OutputGrant(ContractModel):
    """One bounded output file an adapter may publish."""

    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    required: bool
    max_bytes: PositiveInt

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class DiagnosticGrant(ContractModel):
    """The sole bounded diagnostic file grant."""

    path: RepositoryPath
    media_type: Literal["text/plain; charset=utf-8"]
    max_bytes: PositiveInt

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class StageProfile(ContractModel):
    """Strict least-privilege file and work grants for one Stage."""

    schema_version: Literal["sdi.stage-profile/v1"]
    profile_version: NonBlank
    stage: StageName
    work_limit_seconds: PositiveInt
    inputs: Annotated[list[InputGrant], Field(min_length=1)]
    outputs: Annotated[list[OutputGrant], Field(min_length=1)]
    diagnostic: DiagnosticGrant

    @model_validator(mode="after")
    def validate_grants(self) -> Self:
        input_slots = [item.slot for item in self.inputs]
        output_slots = [item.slot for item in self.outputs]
        _require_unique(input_slots, "input slot")
        _require_unique(output_slots, "output slot")
        _require_unique_paths(
            [
                *(item.path for item in self.inputs),
                *(item.path for item in self.outputs),
                self.diagnostic.path,
                RESERVED_RESPONSE_PATH,
                RESERVED_ACCEPTED_ENVELOPE_PATH,
            ]
        )
        if self.stage == "composition":
            if self.profile_version != COMPOSITION_PROFILE_VERSION:
                msg = "composition profile has an unsupported profile version"
                raise ValueError(msg)
            if set(input_slots) != COMPOSITION_INPUT_SLOTS:
                msg = "composition profile must grant exactly its three input slots"
                raise ValueError(msg)
            if set(output_slots) != COMPOSITION_OUTPUT_SLOTS:
                msg = "composition profile must grant exactly its two output slots"
                raise ValueError(msg)
            if not all(output.required for output in self.outputs):
                msg = "both composition outputs are required"
                raise ValueError(msg)
            actual_inputs = {
                item.slot: (item.path, item.media_type, item.schema_version)
                for item in self.inputs
            }
            actual_outputs = {
                item.slot: (item.path, item.media_type, item.schema_version)
                for item in self.outputs
            }
            if actual_inputs != EXPECTED_COMPOSITION_INPUTS:
                msg = "composition input grants do not match the reviewed profile"
                raise ValueError(msg)
            if actual_outputs != EXPECTED_COMPOSITION_OUTPUTS:
                msg = "composition output grants do not match the reviewed profile"
                raise ValueError(msg)
        return self


class DeclaredInput(InputGrant):
    """An input grant bound to exact accepted bytes."""

    byte_size: NonNegativeInt
    sha256: Sha256


class AdapterRequest(ContractModel):
    """Complete least-privilege request for one adapter invocation."""

    schema_version: Literal["sdi.stage-adapter-request/v1"]
    correlation: Correlation
    work_limit_seconds: PositiveInt
    inputs: Annotated[list[DeclaredInput], Field(min_length=1)]
    outputs: Annotated[list[OutputGrant], Field(min_length=1)]
    diagnostic: DiagnosticGrant

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_unique([item.slot for item in self.inputs], "input slot")
        _require_unique([item.slot for item in self.outputs], "output slot")
        _require_unique_paths(
            [
                *(item.path for item in self.inputs),
                *(item.path for item in self.outputs),
                self.diagnostic.path,
                RESERVED_RESPONSE_PATH,
                RESERVED_ACCEPTED_ENVELOPE_PATH,
            ]
        )
        return self


class DiagnosticResponse(ContractModel):
    """Adapter-reported diagnostic publication facts."""

    present: bool
    truncated: bool

    @model_validator(mode="after")
    def validate_truncation(self) -> Self:
        if self.truncated and not self.present:
            msg = "an absent diagnostic cannot be truncated"
            raise ValueError(msg)
        return self


class AdapterResponse(ContractModel):
    """Untrusted candidate response published by an adapter."""

    schema_version: Literal["sdi.stage-adapter-response/v1"]
    correlation: Correlation
    execution_conclusion: ExecutionConclusion
    domain_outcome: DomainOutcome
    reason: Reason | SkipJsonSchema[None] = None
    consumed_inputs: list[SlotName]
    produced_outputs: list[SlotName]
    diagnostic: DiagnosticResponse

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_absent_reason(cls, data: object) -> object:
        return _reject_null_reason(data)

    @model_validator(mode="after")
    def validate_slots(self) -> Self:
        _require_unique(self.consumed_inputs, "consumed input slot")
        _require_unique(self.produced_outputs, "produced output slot")
        if self.execution_conclusion == "failed" and (
            self.domain_outcome != "not_evaluated" or self.produced_outputs
        ):
            msg = "failed adapter execution cannot claim Domain output"
            raise ValueError(msg)
        if self.execution_conclusion == "failed" and self.reason is None:
            msg = "failed adapter execution requires a typed reason"
            raise ValueError(msg)
        return self


class AcceptedInput(ContractModel):
    """Exact declared input accepted for this attempt."""

    slot: SlotName
    source_path: RepositoryPath
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256


class AcceptedDomainFile(ContractModel):
    """One schema-valid Domain file accepted by the integration runtime."""

    role: Literal["domain_output"]
    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256


class AcceptedDiagnosticFile(ContractModel):
    """One bounded sanitized diagnostic accepted by the integration runtime."""

    role: Literal["diagnostic"]
    slot: Literal["diagnostic"]
    path: RepositoryPath
    media_type: Literal["text/plain; charset=utf-8"]
    byte_size: NonNegativeInt
    sha256: Sha256
    truncated: bool


type AcceptedFile = Annotated[
    AcceptedDomainFile | AcceptedDiagnosticFile, Field(discriminator="role")
]


class ProcessFacts(ContractModel):
    """Authoritative process observations recorded by the runtime."""

    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: NonNegativeInt
    exit_code: Literal[0]


class AcceptedAttemptEnvelope(ContractModel):
    """Transactional record created only after whole-bundle acceptance."""

    schema_version: Literal["sdi.accepted-attempt-envelope/v1"]
    correlation: Correlation
    lifecycle_state: Literal["completed"]
    execution_conclusion: ExecutionConclusion
    implementation_mode: ImplementationMode
    domain_outcome: DomainOutcome
    reason: Reason | SkipJsonSchema[None] = None
    process: ProcessFacts
    accepted_inputs: list[AcceptedInput]
    accepted_files: list[AcceptedFile]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_absent_reason(cls, data: object) -> object:
        return _reject_null_reason(data)


class FixtureInputMatch(ContractModel):
    """One exact input digest participating in Fixture selection."""

    slot: SlotName
    byte_size: NonNegativeInt
    sha256: Sha256


class FixtureOutput(ContractModel):
    """One immutable output payload owned by a Fixture case."""

    slot: SlotName
    source_path: RepositoryPath
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class FixtureDiagnostic(ContractModel):
    """One immutable sanitized Fixture diagnostic."""

    source_path: RepositoryPath
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class FixtureCase(ContractModel):
    """Exact-digest-selected deterministic pipeline-interface behavior."""

    schema_version: Literal["sdi.fixture-case/v1"]
    case_id: Slug
    stage: StageName
    match_inputs: Annotated[list[FixtureInputMatch], Field(min_length=1)]
    execution_conclusion: ExecutionConclusion
    domain_outcome: Literal["not_evaluated"]
    reason: Reason
    outputs: list[FixtureOutput]
    diagnostic: FixtureDiagnostic

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        _require_unique([item.slot for item in self.match_inputs], "Fixture input slot")
        _require_unique([item.slot for item in self.outputs], "Fixture output slot")
        if self.execution_conclusion == "succeeded" and not self.outputs:
            msg = "a successful Fixture case must declare outputs"
            raise ValueError(msg)
        return self
