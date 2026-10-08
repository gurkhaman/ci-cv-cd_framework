"""Checking and ranking of model-generated composition proposals."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import BaseModel, Field, ValidationError

from ._contracts import (
    Baseline,
    ContractModel,
    Endpoint,
    Host,
    MobilityRequirementsSpecification,
    NonBlank,
    RoleMaps,
    TargetExecutionProfile,
)
from ._domain_contracts import Capability  # noqa: TC001
from ._json_input import parse_json
from ._service_descriptions import read_service_repository
from ._yaml_input import InputError, parse_front_matter, parse_yaml

if TYPE_CHECKING:
    from pathlib import Path

    from ._service_descriptions import Artifact, DescriptionFile, ServiceDescription

MAX_PROPOSALS_BYTES = 1024 * 1024
ROLE_PAIRS = (
    ("subscribes", "publishes"),
    ("service_clients", "service_servers"),
    ("action_clients", "action_servers"),
)
GAP_KINDS = frozenset(
    {
        "missing-provider",
        "missing-artifact",
        "unmet-dependency",
        "unresolved-placement",
        "unresolved-connection",
    }
)

type CoverageStatus = Literal[
    "supported", "missing", "uncertain", "outside_composition"
]


class ServicePlacement(ContractModel):
    """One chosen service, its artifact and its profile host."""

    service_id: NonBlank
    artifact_id: NonBlank | None = None
    host: NonBlank | None = None
    capability: Capability


class CoverageClaim(ContractModel):
    """The model's claim for one requirement obligation."""

    requirement_id: NonBlank
    status: CoverageStatus
    services: list[NonBlank]
    reason: NonBlank


class Proposal(ContractModel):
    """One joint services, artifacts and placement proposal."""

    rationale: NonBlank
    services: Annotated[list[ServicePlacement], Field(min_length=1)]
    coverage: list[CoverageClaim]


class CompositionProposals(ContractModel):
    """The model's alternatives, listed best-first."""

    proposals: Annotated[list[Proposal], Field(min_length=1, max_length=3)]


@dataclass(frozen=True)
class _Participant:
    """A chosen service, or the orchestrator when service_id is None."""

    service_id: str | None
    host: str | None
    roles: RoleMaps

    @property
    def label(self) -> str:
        return self.service_id or "orchestrator"


def _finding(
    kind: str,
    message: str,
    *,
    service_id: str | None = None,
    requirement_id: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "gap": kind in GAP_KINDS,
        "message": message,
        "service_id": service_id,
        "requirement_id": requirement_id,
    }


def _ineligibility(
    proposal: Proposal,
    services: dict[str, ServiceDescription],
    requirement_ids: list[str],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for placement in proposal.services:
        service_id = placement.service_id
        description = services.get(service_id)
        if description is None:
            findings.append(
                _finding("ineligible", "unknown service_id", service_id=service_id)
            )
        elif (
            placement.artifact_id is not None
            and description.artifacts is not None
            and placement.artifact_id not in description.artifacts
        ):
            findings.append(
                _finding(
                    "ineligible",
                    f"unknown artifact_id {placement.artifact_id}",
                    service_id=service_id,
                )
            )
    counts = Counter(placement.service_id for placement in proposal.services)
    findings.extend(
        _finding("ineligible", "duplicate service_id", service_id=service_id)
        for service_id, count in counts.items()
        if count > 1
    )
    claimed = Counter(entry.requirement_id for entry in proposal.coverage)
    if claimed != Counter(requirement_ids):
        findings.append(
            _finding(
                "ineligible", "coverage does not name each requirement_id exactly once"
            )
        )
    findings.extend(
        _finding(
            "ineligible",
            f"{entry.status} coverage cites services",
            requirement_id=entry.requirement_id,
        )
        for entry in proposal.coverage
        if entry.status in {"missing", "outside_composition"} and entry.services
    )
    return findings


def _baseline_conflict(required: Baseline, actual: Baseline) -> str | None:
    for field in ("os", "ros_distro", "jetpack"):
        wanted, offered = getattr(required, field), getattr(actual, field)
        if wanted is not None and offered is not None and wanted != offered:
            return f"requires {field} {wanted}, host has {offered}"
    return None


def _artifact(
    placement: ServicePlacement, description: ServiceDescription
) -> tuple[Artifact | None, list[dict[str, object]]]:
    service_id = placement.service_id
    if description.artifacts is None:
        message = "declares no artifacts"
    elif placement.artifact_id is None:
        message = "no artifact chosen"
    else:
        return description.artifacts[placement.artifact_id], []
    return None, [_finding("missing-artifact", message, service_id=service_id)]


def _place(
    placement: ServicePlacement,
    description: ServiceDescription,
    hosts: dict[str, Host],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Return the placement outcome for one service and its findings."""
    service_id = placement.service_id
    artifact, findings = _artifact(placement, description)
    host = hosts.get(placement.host) if placement.host is not None else None
    outcome: dict[str, object] = {
        "service_id": service_id,
        "artifact_id": placement.artifact_id if artifact is not None else None,
        "host": placement.host if host is not None else None,
        "placement": "resolved",
    }
    if host is None:
        findings.append(
            _finding(
                "unresolved-placement",
                "no host chosen"
                if placement.host is None
                else f"host {placement.host} is not a profile host",
                service_id=service_id,
            )
        )
        return {**outcome, "placement": "unresolved"}, findings
    conflicts: list[str] = []
    if artifact is not None:
        if not {"noarch", host.architecture} & set(artifact.architectures):
            conflicts.append(f"no {host.architecture} artifact")
        if artifact.host_requirement is not None and host.baseline is not None:
            conflict = _baseline_conflict(artifact.host_requirement, host.baseline)
            if conflict is not None:
                conflicts.append(conflict)
    if description.devices:
        if host.devices is None:
            findings.append(
                _finding(
                    "unresolved-placement",
                    f"host {placement.host} declares no device list",
                    service_id=service_id,
                )
            )
        else:
            conflicts.extend(
                f"host lacks device {device}"
                for device in description.devices
                if device not in host.devices
            )
    if conflicts:
        rejection = _finding(
            "placement-rejected",
            f"on {placement.host}: {'; '.join(conflicts)}; service removed",
            service_id=service_id,
        )
        return {**outcome, "placement": "rejected"}, [rejection]
    if findings:
        return {**outcome, "placement": "unresolved"}, findings
    return outcome, findings


def _binding_conflict(
    consumer_role: str, wanted: Endpoint, offered: Endpoint
) -> str | None:
    if wanted.type != offered.type:
        return f"type {wanted.type} != {offered.type}"
    if consumer_role == "subscribes":
        if wanted.reliability == "reliable" and offered.reliability == "best_effort":
            return "reliable requested from a best_effort publisher"
        if wanted.durability == "transient_local" and offered.durability == "volatile":
            return "transient_local requested from a volatile publisher"
    return None


def _role(roles: RoleMaps, role: str) -> dict[str, Endpoint]:
    return cast("dict[str, Endpoint] | None", getattr(roles, role)) or {}


def _bind(
    participants: list[_Participant],
    connections: set[frozenset[str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    bindings: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    unconnected: dict[tuple[str, str], list[str]] = {}
    for consumer in participants:
        for consumer_role, provider_role in ROLE_PAIRS:
            for name, wanted in _role(consumer.roles, consumer_role).items():
                provided = False
                for provider in participants:
                    offered = _role(provider.roles, provider_role).get(name)
                    if provider is consumer or offered is None:
                        continue
                    edge = f"{name} {provider.label} -> {consumer.label}"
                    conflict = _binding_conflict(consumer_role, wanted, offered)
                    if conflict is not None:
                        findings.append(
                            _finding(
                                "binding-rejected",
                                f"{edge}: {conflict}",
                                service_id=consumer.service_id,
                            )
                        )
                        continue
                    provided = True
                    bindings.append(
                        {
                            "name": name,
                            "type": offered.type,
                            "consumer": {
                                "service_id": consumer.service_id,
                                "role": consumer_role,
                            },
                            "provider": {
                                "service_id": provider.service_id,
                                "role": provider_role,
                            },
                        }
                    )
                    if (
                        consumer.host is not None
                        and provider.host is not None
                        and consumer.host != provider.host
                        and frozenset((consumer.host, provider.host)) not in connections
                    ):
                        pair = (
                            min(consumer.host, provider.host),
                            max(consumer.host, provider.host),
                        )
                        unconnected.setdefault(pair, []).append(edge)
                if not provided:
                    findings.append(
                        _finding(
                            "missing-provider",
                            f"no provider for {consumer_role} {name}",
                            service_id=consumer.service_id,
                        )
                    )
    findings.extend(
        _finding(
            "unresolved-connection",
            f"no connection between {first} and {second}: {', '.join(edges)}",
        )
        for (first, second), edges in sorted(unconnected.items())
    )
    return bindings, findings


def _final_coverage(
    proposal: Proposal, present: set[str]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    coverage: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    for entry in proposal.coverage:
        status, reason = entry.status, entry.reason
        cited = set(entry.services)
        if status == "supported" and not (cited and cited <= present):
            status, reason = "missing", "a cited service is not in the proposal"
        elif status == "uncertain" and not cited & present:
            status, reason = "missing", "no cited service is in the proposal"
        if status != entry.status:
            findings.append(
                _finding(
                    "coverage-downgraded",
                    f"{entry.status} -> missing: {reason}",
                    requirement_id=entry.requirement_id,
                )
            )
        coverage.append(
            {
                "requirement_id": entry.requirement_id,
                "status": status,
                "services": list(entry.services),
                "reason": reason,
            }
        )
    return coverage, findings


def _check(
    proposal: Proposal,
    services: dict[str, ServiceDescription],
    profile: TargetExecutionProfile,
) -> tuple[tuple[int, int] | None, dict[str, object]]:
    findings: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    participants: list[_Participant] = []
    for placement in proposal.services:
        description = services[placement.service_id]
        outcome, placed = _place(placement, description, profile.hosts)
        outcomes.append(outcome)
        findings.extend(placed)
        if outcome["placement"] == "rejected":
            continue
        host = cast("str | None", outcome["host"])
        participants.append(_Participant(placement.service_id, host, description))
        if not any(
            getattr(description, role) is not None
            for pair in ROLE_PAIRS
            for role in pair
        ):
            findings.append(
                _finding(
                    "endpoints-unverified",
                    "declares no role maps",
                    service_id=placement.service_id,
                )
            )
    if not participants:
        findings.append(_finding("ineligible", "no service remains after removals"))
        return None, {
            "eligible": False,
            "findings": findings,
            "services": outcomes,
            "bindings": None,
            "coverage": None,
            "score": None,
        }
    present = {participant.label for participant in participants}
    for participant in participants:
        findings.extend(
            _finding(
                "unmet-dependency",
                f"depends_on {dependency} is not in the proposal",
                service_id=participant.service_id,
            )
            for dependency in services[participant.label].depends_on or []
            if dependency not in present
        )
    orchestrator = _Participant(None, None, profile.orchestrator_provides or RoleMaps())
    bindings, bound = _bind(
        [*participants, orchestrator],
        {frozenset(pair) for pair in profile.connections or []},
    )
    findings.extend(bound)
    coverage, downgraded = _final_coverage(proposal, present)
    findings.extend(downgraded)
    gaps = sum(1 for finding in findings if finding["gap"])
    missing = sum(1 for entry in coverage if entry["status"] == "missing")
    return (gaps, missing), {
        "eligible": True,
        "findings": findings,
        "services": outcomes,
        "bindings": bindings,
        "coverage": coverage,
        "score": {"gaps": gaps, "missing": missing},
    }


def assess_proposals(
    proposals: CompositionProposals,
    raw_proposals: list[object],
    services: dict[str, ServiceDescription],
    profile: TargetExecutionProfile,
    requirement_ids: list[str],
) -> dict[str, object]:
    """Check every proposal and prefer the eligible one with fewest gaps."""
    assessed: list[dict[str, object]] = []
    ranked: list[tuple[int, int, int]] = []
    for index, proposal in enumerate(proposals.proposals):
        ineligible = _ineligibility(proposal, services, requirement_ids)
        if ineligible:
            result: dict[str, object] = {
                "eligible": False,
                "findings": ineligible,
                "services": None,
                "bindings": None,
                "coverage": None,
                "score": None,
            }
        else:
            score, result = _check(proposal, services, profile)
            if score is not None:
                ranked.append((*score, index))
        assessed.append(
            {
                "proposal_id": f"proposal-{index + 1}",
                **result,
                "proposal": raw_proposals[index],
            }
        )
    ranked.sort()
    if not ranked:
        return {
            "preferred": None,
            "tie_broken_by_model_order": False,
            "reason": "no proposal is eligible",
            "proposals": assessed,
        }
    best = ranked[0]
    return {
        "preferred": f"proposal-{best[2] + 1}",
        "tie_broken_by_model_order": len(ranked) > 1 and ranked[1][:2] == best[:2],
        "reason": None,
        "proposals": assessed,
    }


def _validate[ModelT: BaseModel](
    model: type[ModelT], document: dict[str, object], path: Path
) -> ModelT:
    try:
        return model.model_validate(document)
    except ValidationError as error:
        msg = f"{path}: contract validation failed: {error}"
        raise InputError(msg) from error


@dataclass(frozen=True)
class AssessmentInputs:
    """The validated service repository, profile and requirements."""

    files: list[DescriptionFile]
    profile: TargetExecutionProfile
    requirements: MobilityRequirementsSpecification
    requirements_body: str


def load_assessment_inputs(
    *,
    service_repository: Path,
    target_profile: Path,
    requirements_specification: Path,
) -> AssessmentInputs:
    """Read and validate the inputs every proposal is generated and checked against."""
    files = read_service_repository(service_repository)
    profile = _validate(
        TargetExecutionProfile,
        parse_yaml(target_profile.read_bytes(), str(target_profile)),
        target_profile,
    )
    front_matter, body = parse_front_matter(
        requirements_specification.read_bytes(), str(requirements_specification)
    )
    requirements = _validate(
        MobilityRequirementsSpecification, front_matter, requirements_specification
    )
    return AssessmentInputs(files, profile, requirements, body)


def assess_proposal_files(
    *,
    service_repository: Path,
    target_profile: Path,
    requirements_specification: Path,
    proposals: Path,
) -> dict[str, object]:
    """Read the supplied inputs and assess the proposals they hold."""
    inputs = load_assessment_inputs(
        service_repository=service_repository,
        target_profile=target_profile,
        requirements_specification=requirements_specification,
    )
    document = parse_json(
        proposals.read_bytes(), str(proposals), max_bytes=MAX_PROPOSALS_BYTES
    )
    return assess_proposals(
        _validate(CompositionProposals, document, proposals),
        document["proposals"],
        {file.description.service_id: file.description for file in inputs.files},
        inputs.profile,
        inputs.requirements.requirement_ids,
    )
