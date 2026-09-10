"""Command-line entry point for the pipeline integration scaffold."""

from __future__ import annotations

import argparse
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdi-integration",
        description="Validate and execute SDI pipeline integration requests.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sdi-pipeline-integration')}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the public command-line interface."""
    parser = _parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
