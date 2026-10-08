"""Model generation of composition proposals through the OpenAI Responses API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Literal, cast

import openai
from pydantic import Field, ValidationError, create_model

from ._assessment import (
    AssessmentInputs,
    CompositionProposals,
    CoverageClaim,
    Proposal,
    ServicePlacement,
    load_assessment_inputs,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

MODEL = "gpt-6-luna"
REASONING_EFFORT = "high"
MAX_OUTPUT_TOKENS = 32_000
TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 0

INSTRUCTIONS = """\
You compose ROS 2 mobility services for one mission on one target.

The input gives every service description in the service repository (validated
front matter plus its Markdown body), the target execution profile and the
mission's requirements specification.

Return one to three alternative proposals, best first. Each proposal:
- chooses services by service_id, each with one of its artifact_ids and one
  profile host; use null for artifact_id or host only when none fits;
- gives every requirement_id exactly one coverage entry: "supported" or
  "uncertain" citing the chosen services that provide it, "missing" when no
  chosen service provides it, or "outside_composition" when the requirement is
  allocated to people or systems outside the composed services; "missing" and
  "outside_composition" cite no services;
- assigns each chosen service a short capability label;
- explains the choice in the rationale.

Use only facts stated in the input. Choose services whose artifact
architectures, host baseline and devices fit the host, whose inputs are
provided by another chosen service or by the profile's orchestrator_provides
with the same type and compatible QoS, whose depends_on services are also
chosen, and whose cross-host bindings follow the profile's connections. The
orchestrator's own inputs need providers among the chosen services.
"""

type Outcome = Literal["generated", "exhausted", "refused", "invalid", "provider_error"]


class GenerationError(Exception):
    """Generation ended without proposals; the outcome names why."""

    def __init__(self, message: str, *, outcome: Outcome) -> None:
        super().__init__(f"{outcome}: {message}")
        self.outcome: Outcome = outcome


def _generation_model(
    service_ids: list[str], requirement_ids: list[str]
) -> type[CompositionProposals]:
    """Restrict proposal IDs to the supplied services and requirements."""
    service_id = cast("type[str]", Literal[tuple(service_ids)])
    requirement_id = cast("type[str]", Literal[tuple(requirement_ids)])
    placement = create_model(
        "ServicePlacement", __base__=ServicePlacement, service_id=(service_id, ...)
    )
    claim = create_model(
        "CoverageClaim",
        __base__=CoverageClaim,
        requirement_id=(requirement_id, ...),
    )
    proposal = create_model(
        "Proposal",
        __base__=Proposal,
        services=(Annotated[list[placement], Field(min_length=1)], ...),
        coverage=(list[claim], ...),
    )
    return create_model(
        "CompositionProposals",
        __base__=CompositionProposals,
        proposals=(Annotated[list[proposal], Field(min_length=1, max_length=3)], ...),
    )


def _json_block(model: BaseModel) -> str:
    facts = json.dumps(model.model_dump(mode="json", exclude_none=True), indent=2)
    return f"```json\n{facts}\n```\n"


def _prompt_input(inputs: AssessmentInputs) -> str:
    sections = [
        f"## Service description {file.path}\n\n"
        f"{_json_block(file.description)}\n{file.body.strip()}\n"
        for file in inputs.files
    ]
    sections.append(f"## Target execution profile\n\n{_json_block(inputs.profile)}")
    sections.append(
        f"## Requirements specification\n\n"
        f"requirement_ids: {', '.join(inputs.requirements.requirement_ids)}\n\n"
        f"{inputs.requirements_body.strip()}\n"
    )
    return "\n".join(sections)


def _response_record(body: dict[str, object]) -> dict[str, object]:
    return {
        key: body.get(key)
        for key in ("id", "model", "status", "incomplete_details", "usage")
    }


def generate_proposals(
    *,
    service_repository: Path,
    target_profile: Path,
    requirements_specification: Path,
    evidence: dict[str, object],
) -> dict[str, object]:
    """Ask the configured model for proposals, recording everything in evidence."""
    inputs = load_assessment_inputs(
        service_repository=service_repository,
        target_profile=target_profile,
        requirements_specification=requirements_specification,
    )
    text_format: type[BaseModel] = _generation_model(
        [file.description.service_id for file in inputs.files],
        inputs.requirements.requirement_ids,
    )
    evidence["settings"] = {
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
    }
    try:
        client = openai.OpenAI(timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
        raw = client.responses.with_raw_response.parse(
            model=MODEL,
            instructions=INSTRUCTIONS,
            input=_prompt_input(inputs),
            reasoning={"effort": REASONING_EFFORT},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            text_format=text_format,
            store=False,
        )
    except openai.OpenAIError as error:
        if isinstance(error, openai.APIError):
            evidence["request"] = json.loads(error.request.content)
        evidence["error"] = {
            "type": type(error).__name__,
            "status_code": getattr(error, "status_code", None),
            "request_id": getattr(error, "request_id", None),
        }
        raise GenerationError(str(error), outcome="provider_error") from error
    evidence["request"] = json.loads(raw.http_response.request.content)
    body = cast("dict[str, object]", raw.http_response.json())
    evidence["response"] = _response_record(body)
    status = body.get("status")
    details = cast("dict[str, object]", body.get("incomplete_details") or {})
    if status == "incomplete" and details.get("reason") == "max_output_tokens":
        msg = f"output reached max_output_tokens={MAX_OUTPUT_TOKENS}"
        raise GenerationError(msg, outcome="exhausted")
    if status == "incomplete" and details.get("reason") == "content_filter":
        msg = "output stopped by the content filter"
        raise GenerationError(msg, outcome="refused")
    if status != "completed":
        msg = f"response status {status}: {body.get('error')}"
        raise GenerationError(msg, outcome="provider_error")
    try:
        response = raw.parse()
    except ValidationError as error:
        raise GenerationError(str(error), outcome="invalid") from error
    refusals = [
        item.refusal
        for output in response.output
        if output.type == "message"
        for item in output.content
        if item.type == "refusal"
    ]
    if refusals:
        evidence["refusal"] = refusals
        raise GenerationError(" ".join(refusals), outcome="refused")
    if response.output_parsed is None:
        msg = "the response holds no proposals"
        raise GenerationError(msg, outcome="invalid")
    return response.output_parsed.model_dump(mode="json")
