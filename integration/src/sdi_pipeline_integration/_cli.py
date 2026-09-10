"""Command-line entry point for the pipeline integration scaffold."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ._run_input import identify_committed_run
from ._schemas import check_schemas, write_schemas
from ._stage_runtime import execute_stage
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Sequence


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911
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
    if arguments.command == "execute-stage":
        try:
            envelope = execute_stage(
                repository_path=arguments.repository,
                requested_ref=arguments.requested_ref,
                resolved_commit=arguments.resolved_commit,
                run_request_path=arguments.run_request_path,
                descriptor_path=arguments.descriptor_path,
                attempt_root=arguments.attempt_root,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(envelope, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    parser.print_help()
    return 0
