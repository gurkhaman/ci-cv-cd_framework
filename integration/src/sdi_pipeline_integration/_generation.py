"""Model generation of composition proposals through the OpenAI Responses API.

A generation config file selects the model, its endpoint and the environment
variable holding its key. Use the self-hosted Qwen config while its endpoint is
up and rerun with the OpenAI luna config when it is not. Each endpoint reads
only its own key, so the OpenAI key never reaches another server.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Annotated, Literal, cast

import openai
from pydantic import Field, PositiveInt, ValidationError, create_model

from ._assessment import (
    AssessmentInputs,
    CompositionProposals,
    CoverageClaim,
    Proposal,
    ServicePlacement,
    load_assessment_inputs,
)
from ._contracts import ContractModel

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

MAX_RETRIES = 0

INSTRUCTIONS = """\
You compose ROS 2 mobility services for one mission on one target.

The input gives the target execution profile, the mission's requirements
specification and every service description in the service repository. Take
interfaces, artifacts, devices and depends_on only from each description's JSON
front matter; the Markdown body says what the service does and its limits. Use
only facts stated in the input. Placeholder provenance does not exclude a
service.

Return one to three proposals, best first: fewest gaps, then fewest requirements
left "missing". Add a proposal only when it changes a choice you are unsure of
(a service, artifact or host); never repeat one.

In each proposal, choose each service_id at most once, and only to cover a
requirement or to provide an input or depends_on of another chosen service.
For each chosen service give:
- artifact_id: one of its artifact keys;
- host: a profile host whose architecture is in the artifact's architectures
  (or noarch), whose baseline agrees with the artifact's host_requirement on
  fields both declare, and whose devices include every device the service
  declares;
- capability: a short label.
A placement that does not fit removes the service and everything it provides;
use null for artifact_id or host instead when nothing fits.

Each of these is a gap:
- a null artifact_id or host;
- a service input (subscribes, service_clients, action_clients) or an
  orchestrator_provides input with no provider. Providers match by exact ROS
  name and type among the chosen services and the orchestrator's publishes and
  servers; a best_effort or volatile publisher does not provide to a subscriber
  requiring reliable or transient_local;
- a binding between services on two different hosts whose pair is not in
  connections;
- a depends_on service that is not chosen.

Give every requirement_id exactly one coverage entry, citing only services
chosen in that proposal:
- "outside_composition" when the specification allocates it only to the
  orchestrator or to people; cite none;
- "supported" when chosen services provide it; cite all of them;
- "uncertain" when a chosen service may provide it but the input leaves it in
  doubt; cite those services;
- "missing" otherwise; cite none.

In the rationale, state the main choices and every gap you could not avoid.
"""

type Outcome = Literal["generated", "exhausted", "refused", "invalid", "provider_error"]


class GenerationConfig(ContractModel):
    """The model, limits and endpoint for one generation request."""

    schema_version: Literal["sdi.generation-config/v1"]
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    max_output_tokens: PositiveInt
    timeout_seconds: PositiveInt
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)


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


def _json(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True), indent=2)


def _prompt_input(inputs: AssessmentInputs) -> str:
    requirement_ids = ", ".join(inputs.requirements.requirement_ids)
    parts = [
        f"<profile>\n{_json(inputs.profile)}\n</profile>",
        (
            f'<requirements ids="{requirement_ids}">\n'
            f"{inputs.requirements_body.strip()}\n</requirements>"
        ),
    ]
    parts.extend(
        f'<service id="{file.description.service_id}" path="{file.path}">\n'
        f"{_json(file.description)}\n\n{file.body.strip()}\n</service>"
        for file in inputs.files
    )
    return "\n\n".join(parts) + "\n"


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
    config: GenerationConfig,
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
    evidence["settings"] = config.model_dump() | {"max_retries": MAX_RETRIES}
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        msg = f"{config.api_key_env} is not set"
        raise GenerationError(msg, outcome="provider_error")
    try:
        client = openai.OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=MAX_RETRIES,
        )
        raw = client.responses.with_raw_response.parse(
            model=config.model,
            instructions=INSTRUCTIONS,
            input=_prompt_input(inputs),
            reasoning={"effort": config.reasoning_effort},
            max_output_tokens=config.max_output_tokens,
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
        msg = f"output reached max_output_tokens={config.max_output_tokens}"
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
