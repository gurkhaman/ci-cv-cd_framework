"""Strict reading of SDI service descriptions from a supplied directory."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    Baseline,
    ContractModel,
    NonBlank,
    RoleMaps,
    Slug,
    reject_explicit_nulls,
)
from ._stage_contracts import Sha256  # noqa: TC001
from ._yaml_input import InputError, parse_front_matter

SERVICE_DESCRIPTION_SCHEMA_VERSION = "sdi.service-description/v1"
# The directory under a Stage's input root that holds the copied descriptions.
SERVICE_REPOSITORY_ROOT = "service-repository"
DESCRIPTION_FILENAME = "SDI.md"
MAX_DESCRIPTION_FILES = 256
MAX_DESCRIPTION_BYTES = 1024 * 1024

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
    host_requirement: Baseline | SkipJsonSchema[None] = None
    route: Route
    invocation: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent optional facts to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host_requirement", "invocation"))


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


class ServiceDescription(RoleMaps):
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


def _read(root: Path, file: Path) -> DescriptionFile:
    path = file.relative_to(root).as_posix()
    if file.is_symlink():
        msg = f"{path}: symbolic links are not read"
        raise InputError(msg)
    raw = file.read_bytes()
    front_matter, body = parse_front_matter(raw, path)
    try:
        description = ServiceDescription.model_validate(front_matter)
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


class ManifestFile(ContractModel):
    """One description file copied into a Stage's inputs."""

    path: Annotated[
        str, Field(strict=True, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*SDI\.md$")
    ]
    byte_size: NonNegativeInt
    sha256: Sha256


class ServiceRepositoryManifest(ContractModel):
    """The exact description files a Stage receives from the service repository."""

    schema_version: Literal["sdi.service-repository-manifest/v1"]
    files: Annotated[
        list[ManifestFile], Field(min_length=1, max_length=MAX_DESCRIPTION_FILES)
    ]

    @model_validator(mode="after")
    def validate_files(self) -> ServiceRepositoryManifest:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)):
            msg = "manifest paths must be unique and sorted"
            raise ValueError(msg)
        if sum(item.byte_size for item in self.files) > MAX_DESCRIPTION_BYTES:
            msg = "manifest files exceed the service repository byte limit"
            raise ValueError(msg)
        return self
