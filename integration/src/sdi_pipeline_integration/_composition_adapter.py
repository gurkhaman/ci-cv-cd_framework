"""Implemented composition Stage adapter: generate, assess and publish.

The adapter asks the configured model for proposals over the supplied service
descriptions, assesses them, and publishes the preferred proposal as the
composition blueprint and deployment schema. Every successful execution also
publishes evidence of the exact request, response and assessment. Reason
summaries and the diagnostic come only from fixed templates, so provider text
never reaches durable output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ValidationError

from ._assessment import CompositionProposals, Proposal, assess_proposals
from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._domain_contracts import (
    CompositionBlueprint,
    CompositionEvidence,
    DeploymentSchema,
)
from ._generation import (
    MAX_RETRIES,
    GenerationConfig,
    GenerationError,
    generate_proposals,
)
from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    read_service_repository,
)
from ._stage_contracts import AdapterRequest, AdapterResponse
from ._yaml_input import InputError, parse_front_matter, parse_yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._service_descriptions import ServiceDescription

MAX_REQUEST_BYTES = 128 * 1024
# Leave the runtime time to capture the candidate before its work limit expires.
TIMEOUT_RESERVE_SECONDS = 30
PROVIDER_FACT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

OUTCOMES: dict[str, tuple[str, str, str, str]] = {
    "preferred-proposal": (
        "succeeded",
        "succeeded",
        "domain",
        "The assessment preferred one eligible generated proposal.",
    ),
    "bounded-no-result": (
        "succeeded",
        "failed",
        "domain",
        "Generation reached its output limit before returning proposals.",
    ),
    "scoped-rejection": (
        "succeeded",
        "failed",
        "domain",
        "Every proposal lost all of its services to placement removals.",
    ),
    "insufficient-proposals": (
        "succeeded",
        "failed",
        "domain",
        "No generated proposal was eligible.",
    ),
    "invalid-service-description": (
        "failed",
        "not_evaluated",
        "input",
        "A supplied service description is invalid.",
    ),
    "generation-invalid": (
        "failed",
        "not_evaluated",
        "tool",
        "The model returned output outside the proposal contract.",
    ),
    "generation-refused": (
        "failed",
        "not_evaluated",
        "tool",
        "The model refused to generate proposals.",
    ),
    "provider-error": (
        "failed",
        "not_evaluated",
        "tool",
        "The model provider did not complete the request.",
    ),
}
GENERATION_FAILURES = {
    "invalid": "generation-invalid",
    "refused": "generation-refused",
    "provider_error": "provider-error",
}


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


def _atomic_write(root: Path, relative_path: str, content: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)


def _validated[ModelT: BaseModel](model: type[ModelT], document: object) -> ModelT:
    try:
        return model.model_validate(document, strict=True, extra="forbid")
    except ValidationError as error:
        msg = f"{model.__name__} contract validation failed: {error}"
        raise InputError(msg) from error


def _read_input(path: Path, size: int, sha256: str) -> bytes:
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        msg = "adapter input is not a single regular file"
        raise InputError(msg)
    content = path.read_bytes()
    if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
        msg = "adapter input bytes do not match the request"
        raise InputError(msg)
    return content


def _check_descriptions(input_root: Path, manifest: ServiceRepositoryManifest) -> None:
    """Require the copied descriptions to be exactly the manifest's files."""
    root = input_root / SERVICE_REPOSITORY_ROOT
    found = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not path.is_dir()
    )
    if found != [item.path for item in manifest.files]:
        msg = "copied descriptions do not match the service-repository manifest"
        raise InputError(msg)
    for item in manifest.files:
        _read_input(root / item.path, item.byte_size, item.sha256)


class _Run:
    """One adapter invocation and the candidate files it publishes."""

    def __init__(self, request: AdapterRequest, output_root: Path) -> None:
        self.request = request
        self.output_root = output_root
        self.outputs = {item.slot: item.path for item in request.outputs}
        self.facts: dict[str, object] = {}

    def publish(self, code: str, documents: dict[str, BaseModel]) -> None:
        """Publish every candidate file, then the response."""
        conclusion, domain_outcome, category, summary = OUTCOMES[code]
        for slot, document in documents.items():
            _atomic_write(
                self.output_root,
                self.outputs[slot],
                _canonical_json(document.model_dump(mode="json", exclude_none=True)),
            )
        _atomic_write(
            self.output_root, self.request.diagnostic.path, self._diagnostic(code)
        )
        response = AdapterResponse.model_validate(
            {
                "schema_version": "sdi.stage-adapter-response/v1",
                "correlation": self.request.correlation.model_dump(mode="json"),
                "execution_conclusion": conclusion,
                "domain_outcome": domain_outcome,
                "reason": {
                    "category": category,
                    "code": f"sdi.composition.{code}",
                    "summary": summary,
                },
                "consumed_inputs": [item.slot for item in self.request.inputs],
                "produced_outputs": list(documents),
                "diagnostic": {"present": True, "truncated": False},
            },
            strict=True,
            extra="forbid",
        )
        _atomic_write(
            self.output_root,
            "response.json",
            _canonical_json(response.model_dump(mode="json")),
        )

    def _diagnostic(self, code: str) -> bytes:
        """Fill the fixed diagnostic template with checked provider facts only."""
        lines = [f"outcome: sdi.composition.{code}"]
        for name, value in self.facts.items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value)
            if PROVIDER_FACT.fullmatch(text) is None:
                text = "unrecorded"
            lines.append(f"{name}: {text}")
        content = "\n".join(lines) + "\n"
        return content.encode()[: self.request.diagnostic.max_bytes]


def _record_provider_facts(run: _Run, evidence: dict[str, object]) -> None:
    error = cast("dict[str, object]", evidence.get("error") or {})
    response = cast("dict[str, object]", evidence.get("response") or {})
    usage = cast("dict[str, object]", response.get("usage") or {})
    run.facts.update(
        {
            "error_type": error.get("type"),
            "status_code": error.get("status_code"),
            "request_id": error.get("request_id"),
            "response_id": response.get("id"),
            "response_status": response.get("status"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
    )
    if "request" in evidence:
        run.facts["request_sha256"] = hashlib.sha256(
            _canonical_json(evidence["request"])
        ).hexdigest()


def _blueprint_documents(
    *,
    assessment: dict[str, object],
    proposals: CompositionProposals,
    services: dict[str, ServiceDescription],
    identity: dict[str, object],
    requirement_ids: list[str],
) -> dict[str, BaseModel]:
    """Turn the preferred proposal's checked outcome into the Domain outputs."""
    proposal_id = cast("str", assessment["preferred"])
    # The assessment lists proposals in the model's order, one per proposal.
    index, assessed = next(
        (index, item)
        for index, item in enumerate(
            cast("list[dict[str, object]]", assessment["proposals"])
        )
        if item["proposal_id"] == proposal_id
    )
    proposal: Proposal = proposals.proposals[index]
    capabilities = {item.service_id: item.capability for item in proposal.services}
    kept = [
        cast("dict[str, object]", item)
        for item in cast("list[object]", assessed["services"])
        if cast("dict[str, object]", item)["placement"] != "rejected"
    ]
    kept_ids = [cast("str", item["service_id"]) for item in kept]
    coverage = {
        cast("str", entry["requirement_id"]): entry
        for entry in cast("list[dict[str, object]]", assessed["coverage"])
    }
    blueprint_id = (
        f"{identity['scenario_id']}-{identity['testcase_id']}-"
        f"{identity['combination_id']}-{proposal_id}"
    ).lower()
    blueprint = _validated(
        CompositionBlueprint,
        identity
        | {
            "schema_version": "sdi.composition-blueprint/v1",
            "blueprint_id": blueprint_id,
            "proposal_id": proposal_id,
            "requirement_ids": requirement_ids,
            "services": [
                {
                    "service_id": service_id,
                    "capability": capabilities[service_id],
                    "depends_on": services[service_id].depends_on or [],
                }
                for service_id in kept_ids
            ],
            "coverage": [
                {
                    "requirement_id": requirement_id,
                    "status": coverage[requirement_id]["status"],
                    "services": [
                        service_id
                        for service_id in cast(
                            "list[str]", coverage[requirement_id]["services"]
                        )
                        if service_id in kept_ids
                    ],
                }
                for requirement_id in requirement_ids
            ],
        },
    )
    placements: list[dict[str, object]] = []
    for item in kept:
        placement: dict[str, object] = {
            "service_id": item["service_id"],
            "status": "resolved"
            if item["placement"] == "resolved"
            and item["artifact_id"] is not None
            and item["host"] is not None
            else "unresolved",
        }
        placement.update(
            {
                name: item[name]
                for name in ("artifact_id", "host")
                if item[name] is not None
            }
        )
        placements.append(placement)
    deployment = _validated(
        DeploymentSchema,
        identity
        | {
            "schema_version": "sdi.deployment-schema/v1",
            "deployment_schema_id": f"{blueprint_id}-deployment",
            "blueprint_id": blueprint_id,
            "placements": placements,
        },
    )
    return {"composition_blueprint": blueprint, "deployment_schema": deployment}


type Outcome = tuple[str, dict[str, BaseModel]]


def _compose(run: _Run, input_root: Path) -> Outcome:
    """Return the outcome code and the documents the run publishes."""
    request = run.request
    declared = {item.slot: item for item in request.inputs}
    contents = {
        slot: _read_input(input_root / item.path, item.byte_size, item.sha256)
        for slot, item in declared.items()
    }
    manifest = _validated(
        ServiceRepositoryManifest,
        parse_json(
            contents["service_repository"],
            "service-repository.json",
            max_bytes=len(contents["service_repository"]),
        ),
    )
    _check_descriptions(input_root, manifest)
    run_request = _validated(
        RunRequest, parse_yaml(contents["run_request"], "run-request.yaml")
    )
    profile = _validated(
        TargetExecutionProfile,
        parse_yaml(contents["target_profile"], "target-profile.yaml"),
    )
    front_matter, _body = parse_front_matter(
        contents["requirements_specification"], "requirements-specification.md"
    )
    requirements = _validated(MobilityRequirementsSpecification, front_matter)
    config = _validated(
        GenerationConfig,
        parse_yaml(contents["generation_config"], "generation-config.yaml"),
    )
    config = config.model_copy(
        update={
            "timeout_seconds": max(
                1,
                min(
                    config.timeout_seconds,
                    request.work_limit_seconds - TIMEOUT_RESERVE_SECONDS,
                ),
            )
        }
    )
    config_sha256 = declared["generation_config"].sha256
    run.facts["config_sha256"] = config_sha256
    identity: dict[str, object] = {
        "evidence_basis": "implemented",
        "scenario_id": run_request.scenario_id,
        "testcase_id": run_request.testcase_id,
        "combination_id": run_request.combination.combination_id,
        "profile_id": profile.profile_id,
        "source_inputs": [
            {"slot": item.slot, "sha256": item.sha256} for item in request.inputs
        ],
    }

    descriptions_root = input_root / SERVICE_REPOSITORY_ROOT
    try:
        files = read_service_repository(descriptions_root)
    except InputError:
        return "invalid-service-description", {}
    services = {file.description.service_id: file.description for file in files}

    generation: dict[str, object] = {}
    record: dict[str, object] = {
        "config_sha256": config_sha256,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "max_output_tokens": config.max_output_tokens,
        "timeout_seconds": config.timeout_seconds,
        "max_retries": MAX_RETRIES,
    }
    evidence: dict[str, object] = identity | {
        "schema_version": "sdi.composition-evidence/v1",
        "descriptions": [
            {"path": item.path, "sha256": item.sha256} for item in manifest.files
        ],
    }
    try:
        generated = generate_proposals(
            service_repository=descriptions_root,
            target_profile=input_root / declared["target_profile"].path,
            requirements_specification=(
                input_root / declared["requirements_specification"].path
            ),
            config=config,
            evidence=generation,
        )
    except GenerationError as error:
        _record_provider_facts(run, generation)
        run.facts["generation_outcome"] = error.outcome
        if error.outcome != "exhausted":
            return GENERATION_FAILURES[error.outcome], {}
        record |= {
            "outcome": "exhausted",
            "request": generation["request"],
            "response": generation["response"],
        }
        return "bounded-no-result", {
            "composition_evidence": _validated(
                CompositionEvidence, evidence | {"generation": record}
            )
        }
    _record_provider_facts(run, generation)
    run.facts["generation_outcome"] = "generated"
    record |= {
        "outcome": "generated",
        "request": generation["request"],
        "response": generation["response"],
    }
    proposals = _validated(CompositionProposals, generated)
    assessment = assess_proposals(
        proposals,
        cast("list[object]", generated["proposals"]),
        services,
        profile,
        requirements.requirement_ids,
    )
    documents: dict[str, BaseModel] = {
        "composition_evidence": _validated(
            CompositionEvidence,
            evidence | {"generation": record, "assessment": assessment},
        )
    }
    if assessment["preferred"] is None:
        assessed = cast("list[dict[str, object]]", assessment["proposals"])
        # A proposal that passed the ineligibility checks reaches placement;
        # it can then only become ineligible by losing every service there.
        scoped = all(item["services"] is not None for item in assessed)
        return (
            "scoped-rejection" if scoped else "insufficient-proposals",
            documents,
        )
    run.facts["preferred"] = assessment["preferred"]
    return "preferred-proposal", _blueprint_documents(
        assessment=assessment,
        proposals=proposals,
        services=services,
        identity=identity,
        requirement_ids=requirements.requirement_ids,
    ) | documents


def run_adapter(*, request_path: Path, input_root: Path, output_root: Path) -> None:
    """Publish one complete candidate response after all candidate files."""
    enforce_domain_execution_boundary(expected_label="composition")
    try:
        parsed = parse_json(
            request_path.read_bytes(), str(request_path), max_bytes=MAX_REQUEST_BYTES
        )
    except OSError as error:
        msg = "adapter request cannot be read"
        raise InputError(msg) from error
    request = _validated(AdapterRequest, parsed)
    if request.correlation.stage != "composition":
        msg = "the composition adapter serves only the composition Stage"
        raise InputError(msg)
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)
    run = _Run(request, output_root)
    run.publish(*_compose(run, input_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdi-composition-adapter")
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
        sys.stderr.write(f"sdi-composition-adapter: {type(error).__name__}\n")
        return 2
    return 0
