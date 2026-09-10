"""Command-line entry point for the pipeline integration scaffold."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from ._local_dispatch import dispatch_local, validate_bundle
from ._run_input import identify_committed_run
from ._schemas import check_schemas, write_schemas
from ._stage_contracts import AdapterDescriptor
from ._stage_runtime import ExternalCancellation, execute_stage
from ._yaml_input import InputError, parse_yaml

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence


@contextmanager
def _external_cancellation() -> Generator[None, None, None]:
    previous: dict[signal.Signals, Any] = {}  # pyright: ignore[reportExplicitAny]

    def cancel(signal_number: int, _frame: object) -> None:
        raise ExternalCancellation(signal_number)

    for cancellation_signal in (signal.SIGINT, signal.SIGTERM):
        previous[cancellation_signal] = signal.signal(cancellation_signal, cancel)
    try:
        yield
    finally:
        for cancellation_signal, handler in previous.items():
            signal.signal(cancellation_signal, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdi-integration",
        description="Validate and execute Pipeline integration run requests.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sdi-pipeline-integration')}",
    )
    commands = parser.add_subparsers(dest="command")
    identify = commands.add_parser(
        "identify-run",
        help="validate committed inputs and assign an Execution ID",
    )
    identify.add_argument("--repository", type=Path, required=True)
    identify.add_argument("--requested-ref", required=True)
    identify.add_argument("--resolved-commit", required=True)
    identify.add_argument("--run-request-path", required=True)
    schemas = commands.add_parser(
        "schemas",
        help="write or check generated input contract schemas",
    )
    schema_action = schemas.add_mutually_exclusive_group(required=True)
    schema_action.add_argument("--check", action="store_true")
    schema_action.add_argument("--write", action="store_true")
    schemas.add_argument("--directory", type=Path, default=Path("schemas"))
    adapter_image = commands.add_parser(
        "adapter-image",
        help="print the immutable runtime image selected by an adapter descriptor",
    )
    adapter_image.add_argument("--descriptor", type=Path, required=True)
    execute = commands.add_parser(
        "execute-stage",
        help="execute and transactionally accept one trusted Stage adapter",
    )
    execute.add_argument("--repository", type=Path, required=True)
    execute.add_argument("--requested-ref", required=True)
    execute.add_argument("--resolved-commit", required=True)
    execute.add_argument("--run-request-path", required=True)
    execute.add_argument("--descriptor-path", required=True)
    execute.add_argument("--attempt-root", type=Path, required=True)
    dispatch = commands.add_parser(
        "dispatch-local",
        help="execute and assemble one local four-Stage run",
    )
    dispatch.add_argument("--repository", type=Path, required=True)
    dispatch.add_argument("--requested-ref", required=True)
    dispatch.add_argument("--resolved-commit", required=True)
    dispatch.add_argument("--run-request-path", required=True)
    dispatch.add_argument("--bundle-root", type=Path, required=True)
    validate = commands.add_parser(
        "validate-bundle",
        help="validate one complete Pipeline integration archive candidate",
    )
    validate.add_argument("--bundle-root", type=Path, required=True)
    return parser


def _has_machinery_failure(result: dict[str, object]) -> bool:
    attempts = result.get("attempts")
    if not isinstance(attempts, list):
        return True
    for raw_attempt in cast("list[object]", attempts):
        if not isinstance(raw_attempt, dict):
            return True
        attempt = cast("dict[str, object]", raw_attempt)
        if attempt.get("execution_conclusion") in {"failed", "timed_out"}:
            return True
        raw_reason = attempt.get("reason")
        if isinstance(raw_reason, dict):
            reason = cast("dict[str, object]", raw_reason)
            if reason.get("code") in {
                "sdi.stage.not-implemented",
                "sdi.dependency.fixture-evidence",
            }:
                return True
    return False


def main(  # noqa: C901, PLR0911, PLR0912, PLR0915
    argv: Sequence[str] | None = None,
) -> int:
    """Run the public command-line interface."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "identify-run":
        try:
            identified = identify_committed_run(
                repository_path=arguments.repository,
                requested_ref=arguments.requested_ref,
                resolved_commit=arguments.resolved_commit,
                run_request_path=arguments.run_request_path,
            )
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(identified, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "schemas":
        try:
            if arguments.write:
                write_schemas(arguments.directory)
            else:
                check_schemas(arguments.directory)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "adapter-image":
        try:
            document = parse_yaml(
                arguments.descriptor.read_bytes(), str(arguments.descriptor)
            )
            descriptor = AdapterDescriptor.model_validate(document)
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(f"{descriptor.image}\n")
        return 0
    if arguments.command == "execute-stage":
        attempt_existed = arguments.attempt_root.exists() or (
            arguments.attempt_root.is_symlink()
        )
        try:
            with _external_cancellation():
                envelope = execute_stage(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    descriptor_path=arguments.descriptor_path,
                    attempt_root=arguments.attempt_root,
                )
        except ExternalCancellation as cancellation:
            if not attempt_existed:
                if arguments.attempt_root.is_symlink():
                    arguments.attempt_root.unlink(missing_ok=True)
                else:
                    shutil.rmtree(arguments.attempt_root, ignore_errors=True)
            return 128 + cancellation.signal_number
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(envelope, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "dispatch-local":
        bundle_existed = arguments.bundle_root.exists() or (
            arguments.bundle_root.is_symlink()
        )
        try:
            with _external_cancellation():
                result = dispatch_local(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    bundle_root=arguments.bundle_root,
                )
        except ExternalCancellation as cancellation:
            if not bundle_existed:
                if arguments.bundle_root.is_symlink():
                    arguments.bundle_root.unlink(missing_ok=True)
                else:
                    shutil.rmtree(arguments.bundle_root, ignore_errors=True)
            return 128 + cancellation.signal_number
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(result, sort_keys=True, separators=(',', ':'))}\n"
        )
        if _has_machinery_failure(cast("dict[str, object]", result)):
            return 1
        return 0
    if arguments.command == "validate-bundle":
        try:
            validate_bundle(arguments.bundle_root)
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    parser.print_help()
    return 0
