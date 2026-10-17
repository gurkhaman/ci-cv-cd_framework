"""The process shell every Domain adapter shares. Copy _image_build_adapter.py.

A Domain adapter is its outcome constants and one body:

    VALIDATED = Outcome("validated", "succeeded", "succeeded", "domain", "...")

    def _validate(invocation: Invocation) -> Verdict:
        invocation.diagnostic.fact("scenarios", 3)
        return Verdict(VALIDATED, {"validation_evidence": evidence})

    main = serve("cv", _validate)

`serve` owns the agent-label check, the arguments, the bounded request read,
the input digests, the deadline, canonical writes, the response and the exit
code. The Fixture adapter replays recorded bytes for any Stage, so it uses
only `load_request` and `atomic_write`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import canonical_json, parse_json
from ._stage_contracts import (
    AdapterExecutionConclusion,
    AdapterRequest,
    AdapterResponse,
    DomainOutcome,
    ReasonCategory,
    contains_sensitive_material,
)
from ._stage_registry import STAGES
from ._yaml_input import InputError, validate_contract

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ._stage_registry import StageName

MAX_REQUEST_BYTES = 128 * 1024
# Model names may carry an organization prefix, as in Qwen/Qwen3.8-27B.
DIAGNOSTIC_FACT = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")


@dataclass(frozen=True)
class Outcome:
    """One way a run ends. Its reason code is `sdi.<stage-slug>.<code>`."""

    code: str
    conclusion: AdapterExecutionConclusion
    domain_outcome: DomainOutcome
    category: ReasonCategory
    summary: str


@dataclass(frozen=True)
class Verdict:
    """What a body returns. Each document key is a granted output slot."""

    outcome: Outcome
    documents: Mapping[str, BaseModel] = field(default_factory=dict[str, BaseModel])


class Diagnostic:
    """The bounded diagnostic text: the outcome, facts, lines, then blocks."""

    def __init__(self, prefix: str, max_bytes: int) -> None:
        self._prefix = prefix
        self._max_bytes = max_bytes
        self._facts: dict[str, str | None] = {}
        self._lines: list[str] = []
        self._blocks: list[str] = []
        self.truncated = False

    def fact(self, name: str, value: object) -> None:
        """Record a text or count value alone, or `unrecorded` if it fails screening.

        Other values record nothing, and a later value replaces an earlier one.
        """
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            self._facts[name] = None
            return
        text = str(value)
        if DIAGNOSTIC_FACT.fullmatch(text) is None or contains_sensitive_material(text):
            text = "unrecorded"
        self._facts[name] = text

    def facts(self, values: Mapping[str, object]) -> None:
        """Record each value as `fact` does."""
        for name, value in values.items():
            self.fact(name, value)

    def line(self, text: str) -> None:
        """Add a line the adapter built from fixed text and checked values."""
        self._lines.append(text)

    def block(self, title: str, lines: Sequence[str], *, cut: bool) -> None:
        """Add a titled block, such as a log tail, kept only if everything fits."""
        self._blocks.append(f"--- {title}\n" + "".join(f"{line}\n" for line in lines))
        self.truncated = self.truncated or cut

    def remaining(self, outcome: Outcome) -> int:
        """Return the bytes left for blocks once `outcome`'s lines are written."""
        return self._max_bytes - len(self._header(outcome))

    def render(self, outcome: Outcome) -> tuple[bytes, bool]:
        """Return the diagnostic bytes and whether anything was cut."""
        header = self._header(outcome)
        if len(header) > self._max_bytes:
            return header[: header.rfind(b"\n", 0, self._max_bytes) + 1], True
        content = header + "".join(self._blocks).encode()
        # Every fact was screened alone; this screens what joining them made.
        if len(content) > self._max_bytes or contains_sensitive_material(
            content.decode()
        ):
            return header, bool(self._blocks)
        return content, self.truncated

    def _header(self, outcome: Outcome) -> bytes:
        lines = [
            f"outcome: {self._prefix}.{outcome.code}",
            *(
                f"{name}: {text}"
                for name, text in self._facts.items()
                if text is not None
            ),
            *self._lines,
        ]
        return "".join(f"{line}\n" for line in lines).encode()


@dataclass(frozen=True)
class Invocation:
    """One adapter run as its body sees it."""

    request: AdapterRequest
    input_root: Path
    # Every granted input, already checked against its declared size and digest.
    inputs: Mapping[str, bytes]
    # The monotonic time at which the Stage's work limit ends.
    deadline: float
    diagnostic: Diagnostic


@dataclass(frozen=True)
class DomainAdapter:
    """The console-script `main` that `serve` builds for one Stage."""

    stage: StageName
    body: Callable[[Invocation], Verdict]

    def __call__(self, argv: Sequence[str] | None = None) -> int:
        """Run the one-shot Stage-adapter operation; exit 2 before publishing."""
        started = time.monotonic()
        slug = self.stage.replace("_", "-")
        try:
            enforce_domain_execution_boundary(
                expected_label=STAGES[self.stage].agent.label
            )
            arguments = _parser(f"sdi-{slug}-adapter").parse_args(argv)
            output_root: Path = arguments.output_root
            request = load_request(
                arguments.request,
                stage=self.stage,
                input_root=arguments.input_root,
                output_root=output_root,
            )
            invocation = Invocation(
                request,
                arguments.input_root,
                _read_inputs(request, arguments.input_root),
                started + request.work_limit_seconds,
                Diagnostic(f"sdi.{slug}", request.diagnostic.max_bytes),
            )
            _publish(output_root, invocation, self.body(invocation))
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-{slug}-adapter: {type(error).__name__}\n")
            return 2
        return 0


def serve(stage: StageName, body: Callable[[Invocation], Verdict]) -> DomainAdapter:
    """Return the console-script `main` of the Domain adapter for `stage`."""
    return DomainAdapter(stage, body)


def encode_document(document: BaseModel) -> bytes:
    """Return the exact bytes an adapter writes for a Domain output."""
    return canonical_json(document.model_dump(mode="json", exclude_none=True))


def load_request(
    path: Path, *, stage: StageName | None, input_root: Path, output_root: Path
) -> AdapterRequest:
    """Read the bounded request for `stage`, or any Stage, and check both roots."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    except OSError as error:
        msg = "adapter request cannot be read"
        raise InputError(msg) from error
    request = validate_contract(
        AdapterRequest,
        parse_json(raw, str(path), max_bytes=MAX_REQUEST_BYTES),
        str(path),
    )
    if stage is not None and request.correlation.stage != stage:
        msg = f"the {stage} adapter serves only the {stage} Stage"
        raise InputError(msg)
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)
    return request


def atomic_write(root: Path, relative_path: str, content: bytes) -> None:
    """Write a file so the runtime never captures it partially written."""
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def _read_inputs(request: AdapterRequest, input_root: Path) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for item in request.inputs:
        path = input_root / item.path
        if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
            msg = "adapter input is not a single regular file"
            raise InputError(msg)
        content = path.read_bytes()
        if (
            len(content) != item.byte_size
            or hashlib.sha256(content).hexdigest() != item.sha256
        ):
            msg = "adapter input bytes do not match the request"
            raise InputError(msg)
        contents[item.slot] = content
    return contents


def _publish(output_root: Path, invocation: Invocation, verdict: Verdict) -> None:
    """Publish every Domain output, then the diagnostic, then the response."""
    request = invocation.request
    outcome = verdict.outcome
    outputs = {item.slot: item.path for item in request.outputs}
    for slot, document in verdict.documents.items():
        atomic_write(output_root, outputs[slot], encode_document(document))
    diagnostic, truncated = invocation.diagnostic.render(outcome)
    atomic_write(output_root, request.diagnostic.path, diagnostic)
    response = AdapterResponse.model_validate(
        {
            "schema_version": "sdi.stage-adapter-response/v1",
            "correlation": request.correlation.model_dump(mode="json"),
            "execution_conclusion": outcome.conclusion,
            "domain_outcome": outcome.domain_outcome,
            "reason": {
                "category": outcome.category,
                "code": f"sdi.{request.correlation.stage.replace('_', '-')}."
                f"{outcome.code}",
                "summary": outcome.summary,
            },
            "consumed_inputs": [item.slot for item in request.inputs],
            "produced_outputs": list(verdict.documents),
            "diagnostic": {"present": True, "truncated": truncated},
        },
        strict=True,
        extra="forbid",
    )
    atomic_write(
        output_root, "response.json", canonical_json(response.model_dump(mode="json"))
    )
