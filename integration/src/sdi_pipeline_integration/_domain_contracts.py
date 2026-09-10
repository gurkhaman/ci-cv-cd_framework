"""Authoritative composition output contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from ._contracts import CombinationId, ContractModel, ScenarioId, Slug, TestcaseId
from ._stage_contracts import Sha256, SlotName  # noqa: TC001

COMPOSITION_BLUEPRINT_SCHEMA_VERSION = "sdi.composition-blueprint/v1"
DEPLOYMENT_SCHEMA_VERSION = "sdi.deployment-schema/v1"
ServiceVersion = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=63),
]


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


class SourceInputDigest(ContractModel):
    """Stable exact-byte input correlation embedded in Domain outputs."""

    slot: SlotName
    sha256: Sha256


class BlueprintService(ContractModel):
    """One visibly Fixture-owned service in a composition blueprint."""

    service_id: Slug
    version: ServiceVersion
    fixture_source: Slug
    capability: Slug
    depends_on: list[Slug]


class CompositionBlueprint(ContractModel):
    """Target-aware logical service composition without validation claims."""

    schema_version: Literal["sdi.composition-blueprint/v1"]
    evidence_basis: Literal["fixture"]
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]
    services: Annotated[list[BlueprintService], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(self.requirement_ids, "requirement_id")
        service_ids = [item.service_id for item in self.services]
        _require_unique(service_ids, "service_id")
        service_set = set(service_ids)
        dependencies: dict[str, set[str]] = {}
        for service in self.services:
            _require_unique(service.depends_on, "depends_on")
            if service.service_id in service.depends_on:
                msg = f"service {service.service_id} depends on itself"
                raise ValueError(msg)
            unknown = set(service.depends_on) - service_set
            if unknown:
                msg = f"unknown service dependencies: {sorted(unknown)}"
                raise ValueError(msg)
            dependencies[service.service_id] = set(service.depends_on)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(service_id: str) -> None:
            if service_id in visiting:
                msg = "service dependency graph must be acyclic"
                raise ValueError(msg)
            if service_id in visited:
                return
            visiting.add(service_id)
            for dependency in dependencies[service_id]:
                visit(dependency)
            visiting.remove(service_id)
            visited.add(service_id)

        for service_id in service_ids:
            visit(service_id)
        return self


class DeploymentLocation(ContractModel):
    """One intended mobility or SDI execution location."""

    location_id: Slug
    tier: Literal["mobility", "edge", "fog", "cloud"]
    resource_id: Slug


class ServicePlacement(ContractModel):
    """One service-to-location mapping."""

    service_id: Slug
    location_id: Slug


class DeploymentSchema(ContractModel):
    """Intended placement for every service in one accepted blueprint."""

    schema_version: Literal["sdi.deployment-schema/v1"]
    evidence_basis: Literal["fixture"]
    deployment_schema_id: Slug
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    locations: Annotated[list[DeploymentLocation], Field(min_length=1)]
    placements: Annotated[list[ServicePlacement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        location_ids = [item.location_id for item in self.locations]
        _require_unique(location_ids, "location_id")
        placement_services = [item.service_id for item in self.placements]
        _require_unique(placement_services, "placed service_id")
        unknown = {item.location_id for item in self.placements} - set(location_ids)
        if unknown:
            msg = f"unknown placement locations: {sorted(unknown)}"
            raise ValueError(msg)
        return self
