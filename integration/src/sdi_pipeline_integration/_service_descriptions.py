"""Strict reading of SDI service descriptions from a supplied directory."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import ContractModel, NonBlank, Slug, reject_explicit_nulls
from ._yaml_input import MAX_INPUT_BYTES, InputError, parse_yaml

SERVICE_DESCRIPTION_SCHEMA_VERSION = "sdi.service-description/v1"
DESCRIPTION_FILENAME = "SDI.md"
FRONT_MATTER_FENCE = "---\n"

type Architecture = Literal["amd64", "arm64", "noarch"]


class DeclaredProvenance(ContractModel):
    """Facts asserted by an accountable upstream source."""

    basis: Literal["declared"]
    source: NonBlank


class PlaceholderProvenance(ContractModel):
    """Facts that only exercise an interface; the label is carried."""

    basis: Literal["placeholder"]
    note: NonBlank


Provenance = Annotated[
    DeclaredProvenance | PlaceholderProvenance, Field(discriminator="basis")
]


class Implementation(ContractModel):
    """Carried identity of the upstream implementation."""

    name: NonBlank
    version: NonBlank | SkipJsonSchema[None] = None
    repository: NonBlank
    revision: NonBlank

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unknown version to be omitted rather than null."""
        return reject_explicit_nulls(data, ("version",))


class HostRequirement(ContractModel):
    """Baseline an artifact needs on its host."""

    os: NonBlank
    ros_distro: NonBlank
    jetpack: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unknown JetPack version to be omitted rather than null."""
        return reject_explicit_nulls(data, ("jetpack",))


class AptRoute(ContractModel):
    """Debian packages installed with their carried version pins."""

    kind: Literal["apt"]
    packages: Annotated[list[NonBlank], Field(min_length=1)]


class ImageRoute(ContractModel):
    """A container image reference."""

    kind: Literal["image"]
    reference: NonBlank


class SourceRoute(ContractModel):
    """A source checkout built on the host."""

    kind: Literal["source"]
    repository: NonBlank
    revision: NonBlank


Route = Annotated[AptRoute | ImageRoute | SourceRoute, Field(discriminator="kind")]


class Artifact(ContractModel):
    """One fixed deployable variant of a service."""

    architectures: Annotated[list[Architecture], Field(min_length=1)]
    host_requirement: HostRequirement | SkipJsonSchema[None] = None
    route: Route
    invocation: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent optional facts to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host_requirement", "invocation"))


class Endpoint(ContractModel):
    """One ROS interface keyed by its exact name in a role map."""

    type: NonBlank
    reliability: Literal["reliable", "best_effort"] | SkipJsonSchema[None] = None
    durability: Literal["volatile", "transient_local"] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require undeclared QoS to be omitted rather than null."""
        return reject_explicit_nulls(data, ("reliability", "durability"))


class Frames(ContractModel):
    """Carried coordinate frame names."""

    global_frame: NonBlank | SkipJsonSchema[None] = Field(default=None, alias="global")
    odometry: NonBlank | SkipJsonSchema[None] = None
    robot_base: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown frames to be omitted rather than null."""
        return reject_explicit_nulls(data, ("global", "odometry", "robot_base"))


type RoleMap = dict[NonBlank, Endpoint]
ROLE_MAPS = (
    "subscribes",
    "publishes",
    "service_servers",
    "service_clients",
    "action_servers",
    "action_clients",
)


class ServiceDescription(ContractModel):
    """Front matter of one mobility service description."""

    schema_version: Literal["sdi.service-description/v1"]
    service_id: Slug
    title: NonBlank
    provenance: Provenance
    implementation: Implementation | SkipJsonSchema[None] = None
    devices: list[NonBlank] | SkipJsonSchema[None] = None
    depends_on: list[Slug] | SkipJsonSchema[None] = None
    artifacts: (
        Annotated[dict[Slug, Artifact], Field(min_length=1)] | SkipJsonSchema[None]
    ) = None
    subscribes: RoleMap | SkipJsonSchema[None] = None
    publishes: RoleMap | SkipJsonSchema[None] = None
    service_servers: RoleMap | SkipJsonSchema[None] = None
    service_clients: RoleMap | SkipJsonSchema[None] = None
    action_servers: RoleMap | SkipJsonSchema[None] = None
    action_clients: RoleMap | SkipJsonSchema[None] = None
    frames: Frames | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown facts to be omitted rather than null."""
        return reject_explicit_nulls(
            data,
            (
                "implementation",
                "devices",
                "depends_on",
                "artifacts",
                *ROLE_MAPS,
                "frames",
            ),
        )


@dataclass(frozen=True)
class DescriptionFile:
    """One validated description with its exact-byte identity."""

    path: str
    sha256: str
    description: ServiceDescription
    body: str


def _split(raw: bytes, path: str) -> tuple[bytes, str]:
    if len(raw) > MAX_INPUT_BYTES:
        msg = f"{path}: file exceeds {MAX_INPUT_BYTES} bytes"
        raise InputError(msg)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        msg = f"{path}: file is not valid UTF-8"
        raise InputError(msg) from error
    end = text.find(f"\n{FRONT_MATTER_FENCE}", len(FRONT_MATTER_FENCE) - 1)
    if not text.startswith(FRONT_MATTER_FENCE) or end == -1:
        msg = f"{path}: expected YAML front matter between --- lines"
        raise InputError(msg)
    body = text[end + 1 + len(FRONT_MATTER_FENCE) :]
    if not body.strip():
        msg = f"{path}: Markdown body is empty"
        raise InputError(msg)
    return text[len(FRONT_MATTER_FENCE) : end + 1].encode(), body


def _read(root: Path, file: Path) -> DescriptionFile:
    path = file.relative_to(root).as_posix()
    if file.is_symlink():
        msg = f"{path}: symbolic links are not read"
        raise InputError(msg)
    raw = file.read_bytes()
    front_matter, body = _split(raw, path)
    try:
        description = ServiceDescription.model_validate(parse_yaml(front_matter, path))
    except ValidationError as error:
        msg = f"{path}: contract validation failed: {error}"
        raise InputError(msg) from error
    return DescriptionFile(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        description=description,
        body=body,
    )


def _check_cross_file(files: list[DescriptionFile]) -> None:
    by_id: dict[str, DescriptionFile] = {}
    for file in files:
        service_id = file.description.service_id
        if service_id in by_id:
            msg = (
                f"{file.path}: duplicate service_id {service_id} "
                f"(also {by_id[service_id].path})"
            )
            raise InputError(msg)
        by_id[service_id] = file
    graph: TopologicalSorter[str] = TopologicalSorter()
    for file in files:
        dependencies = file.description.depends_on or []
        for dependency in dependencies:
            if dependency not in by_id:
                msg = f"{file.path}: depends_on names unknown service {dependency}"
                raise InputError(msg)
        graph.add(file.description.service_id, *dependencies)
    try:
        graph.prepare()
    except CycleError as error:
        msg = f"depends_on cycle: {' -> '.join(error.args[1])}"
        raise InputError(msg) from error


def read_service_repository(root: Path) -> list[DescriptionFile]:
    """Read and validate every SDI.md under a supplied directory."""
    if not root.is_dir():
        msg = f"{root}: service repository is not a directory"
        raise InputError(msg)
    found = sorted(
        Path(directory) / DESCRIPTION_FILENAME
        for directory, _subdirectories, filenames in os.walk(root)
        if DESCRIPTION_FILENAME in filenames
    )
    if not found:
        msg = f"{root}: no {DESCRIPTION_FILENAME} found"
        raise InputError(msg)
    files = [_read(root, file) for file in found]
    _check_cross_file(files)
    return files
