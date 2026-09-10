# SDI Pipeline Integration Scaffold

This repository's supported entry point is the production integration scaffold
under `integration/`. It will coordinate the CI, image-build, CV, and CD Stage
interfaces for Software-Defined Mobility while keeping those Domain capabilities
independently replaceable.

The current scaffold proves packaging and repository verification only. Later
slices add deterministic Fixtures that can prove pipeline integration behavior.
Fixture output is not Validation evidence, Domain success, KPI evidence, or proof
that a real composition, build, simulation, or deployment capability exists.

## Development

Install [`uv`](https://docs.astral.sh/uv/) and run the deterministic baseline
verification command from any directory:

```sh
integration/scripts/verify
```

The command installs the exact locked Python 3.12 environment and checks the
public CLI with pytest, Ruff, and Basedpyright. The installed command is:

```sh
uv run --project integration sdi-integration --help
```

## Repository Areas

- `integration/`: the independently owned Python package, contracts, Stage
  profiles, Fixtures, tests, and verification entry point.
- `requirements/`, `profiles/`, and `runs/`: version-controlled, dispatchable
  inputs introduced by the run-contract implementation.
- `deployment/jenkins/`: reproducible Jenkins deployment configuration added by
  the deployment implementation.
- `.github/workflows/`: protected workflow configuration added by the handoff
  implementation.
- `docs/`: role-neutral setup, operation, maintenance, and readiness guidance.
- `CV/gazebo/`: retained Gazebo source assets. They are not connected to the
  integration scaffold.
- `CI/`: an unsupported workspace reserved for a possible future composition
  adapter. It is not a supported entry point.

The Year-1 implementation and its artifact formats were retired rather than
carried forward. Git history remains the archive.
