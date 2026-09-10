# SDI Pipeline Integration Scaffold

This repository's supported entry point is the production integration scaffold
under `integration/`. It will coordinate the CI, image-build, CV, and CD Stage
interfaces for Software-Defined Mobility while keeping those Domain capabilities
independently replaceable.

The current scaffold validates and identifies committed inputs, executes one
deterministic four-Stage Fixture run through the permanent Stage-adapter process
interface, and validates its complete local archive candidate. Fixture output is
not Validation evidence, Domain success, KPI evidence, or proof that a real
composition, build, simulation, or deployment capability exists.

The repository also reconstructs an on-demand, zero-executor Jenkins controller
from an immutable image digest, an exact plugin lock, Configuration as Code, and
Job DSL. The controller smoke check does not require or imply that the future
inbound agents or Jenkins pipeline are implemented.

## Development

Install [`uv`](https://docs.astral.sh/uv/) and run the deterministic baseline
verification command from any directory:

```sh
integration/scripts/verify
```

The command installs the exact locked Python 3.12 environment; checks schemas,
the public CLI, Ruff, and Basedpyright; and runs the ephemeral Jenkins controller
smoke check on the supported Docker host. The installed command is:

```sh
uv run --project integration sdi-integration --help
```

## Repository Areas

- `integration/`: the independently owned Python package, contracts, Stage
  profiles, Fixtures, tests, and verification entry point.
- `requirements/`, `profiles/`, and `runs/`: version-controlled, dispatchable
  inputs introduced by the run-contract implementation.
- `deployment/jenkins/`: the pinned zero-executor Jenkins controller deployment,
  exact plugin lock, configuration, fixed job, and lifecycle interface. Agents
  arrive in later implementation issues.
- `.github/workflows/`: protected workflow configuration added by the handoff
  implementation.
- `docs/`: role-neutral setup, operation, maintenance, and readiness guidance.
- `CV/gazebo/`: retained Gazebo source assets. They are not connected to the
  integration scaffold.
- `CI/`: an unsupported workspace reserved for a possible future composition
  adapter. It is not a supported entry point.

The Year-1 implementation and its artifact formats were retired rather than
carried forward. Git history remains the archive.
