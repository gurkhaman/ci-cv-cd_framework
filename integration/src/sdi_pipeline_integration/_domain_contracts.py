"""Authoritative Domain output contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    CombinationId,
    ContractModel,
    NonBlank,
    ScenarioId,
    Slug,
    TestcaseId,
    reject_explicit_nulls,
)
from ._stage_contracts import ImageReference, Sha256, SlotName  # noqa: TC001

COMPOSITION_BLUEPRINT_SCHEMA_VERSION = "sdi.composition-blueprint/v1"
DEPLOYMENT_SCHEMA_VERSION = "sdi.deployment-schema/v1"
IMAGE_BUILD_RESULT_SCHEMA_VERSION = "sdi.image-build-result/v1"
VALIDATION_EVIDENCE_SCHEMA_VERSION = "sdi.validation-evidence/v1"
DEPLOYMENT_RESULT_SCHEMA_VERSION = "sdi.deployment-result/v1"


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


class SourceInputDigest(ContractModel):
    """Stable exact-byte input correlation embedded in Domain outputs."""

    slot: SlotName
    sha256: Sha256


class BlueprintService(ContractModel):
    """One selected service; capability is an unchecked assigned label."""

    service_id: Slug
    capability: NonBlank
    depends_on: list[Slug]


class CompositionBlueprint(ContractModel):
    """Target-aware logical service composition without validation claims."""

    schema_version: Literal["sdi.composition-blueprint/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]
    services: Annotated[list[BlueprintService], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_services(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(self.requirement_ids, "requirement_id")
        _require_unique([item.service_id for item in self.services], "service_id")
        for service in self.services:
            _require_unique(service.depends_on, "depends_on")
        return self


class ServicePlacement(ContractModel):
    """One service placed as a chosen artifact on a profile host."""

    service_id: Slug
    artifact_id: Slug | SkipJsonSchema[None] = None
    host: Slug | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unresolved artifact or host to be omitted rather than null."""
        return reject_explicit_nulls(data, ("artifact_id", "host"))


class DeploymentSchema(ContractModel):
    """Intended placement for every service in one accepted blueprint."""

    schema_version: Literal["sdi.deployment-schema/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    deployment_schema_id: Slug
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    placements: Annotated[list[ServicePlacement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.service_id for item in self.placements], "placed service_id"
        )
        return self


class FixtureImageRecord(ContractModel):
    """One deterministic image reference without a build-success claim."""

    service_id: Slug
    architecture: Literal["arm64", "amd64"]
    image: ImageReference
    build_status: Literal["not_evaluated"]


class ImageBuildResult(ContractModel):
    """Fixture-qualified image-build handoff for one composition."""

    schema_version: Literal["sdi.image-build-result/v1"]
    evidence_basis: Literal["fixture"]
    image_build_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    images: Annotated[list[FixtureImageRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique([item.service_id for item in self.images], "image service_id")
        return self


class FixtureValidationRecord(ContractModel):
    """One declared validation case that was deliberately not evaluated."""

    validation_case_id: Slug
    description: NonBlank
    evaluation_status: Literal["not_evaluated"]


class ValidationEvidence(ContractModel):
    """Fixture-qualified CV handoff that contains no Validation verdict."""

    schema_version: Literal["sdi.validation-evidence/v1"]
    evidence_basis: Literal["fixture"]
    validation_evidence_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    domain_outcome: Literal["not_evaluated"]
    kpi_evaluation: Literal["not_evaluated"]
    validation_records: Annotated[list[FixtureValidationRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.validation_case_id for item in self.validation_records],
            "validation_case_id",
        )
        return self


class FixtureDeploymentRecord(ContractModel):
    """One intended placement that was deliberately not applied."""

    service_id: Slug
    host: Slug | SkipJsonSchema[None] = None
    application_status: Literal["not_evaluated"]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unresolved host to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host",))


class DeploymentResult(ContractModel):
    """Fixture-qualified CD handoff that contains no deployment claim."""

    schema_version: Literal["sdi.deployment-result/v1"]
    evidence_basis: Literal["fixture"]
    deployment_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    validation_evidence_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    domain_outcome: Literal["not_evaluated"]
    application_status: Literal["not_evaluated"]
    deployment_records: Annotated[list[FixtureDeploymentRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.service_id for item in self.deployment_records],
            "deployment service_id",
        )
        return self
