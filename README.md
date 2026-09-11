# SDI Pipeline Integration Scaffold

This repository's supported entry point is the production integration scaffold
under `integration/`. It will coordinate the CI, image-build, CV, and CD Stage
interfaces for Software-Defined Mobility while keeping those Domain capabilities
independently replaceable.

The current scaffold validates and identifies committed inputs and executes one
deterministic four-Stage Fixture run locally or through the protected GitHub
workflow and private five-agent Jenkins pipeline. GitHub is the external
trigger, progress, summary, and artifact-retrieval surface. Both execution paths
use the permanent Stage-adapter process interface and
validate the complete archive candidate. Fixture output is not Validation
evidence, Domain success, KPI evidence, or proof that a real composition, build,
simulation, or deployment capability exists.

The repository also reconstructs an on-demand, zero-executor Jenkins controller
and five isolated one-executor inbound agents from immutable image inputs,
Configuration as Code, and Job DSL. The live smoke check proves their WebSocket
connections, labels, isolation, disposable workspaces, and configuration
convergence. The root `Jenkinsfile` is a thin scheduler over public
`sdi-integration` operations; Python owns all contract construction and
interpretation.

## Development

Install [`uv`](https://docs.astral.sh/uv/) and run the deterministic baseline
verification command from any directory:

```sh
integration/scripts/verify
```

The command installs the exact locked Python 3.12 environment; checks schemas,
the public CLI, Ruff, and Basedpyright; and runs the ephemeral Jenkins stack
smoke check on the supported Docker host. The installed command is:

```sh
uv run --project integration sdi-integration --help
```

## Repository Areas

- `integration/`: the independently owned Python package, contracts, Stage
  profiles, Fixtures, tests, and verification entry point.
- `requirements/`, `profiles/`, and `runs/`: version-controlled, dispatchable
  inputs introduced by the run-contract implementation.
- `deployment/jenkins/`: the pinned zero-executor Jenkins controller, five
  isolated inbound agents, exact plugin lock, configuration, fixed job, and
  lifecycle interface.
- `deployment/github-runner/`: the pinned low-privilege user-service interface
  for the handoff-only self-hosted runner.
- `.github/workflows/`: the protected path-only Pipeline integration workflow.
- `docs/`: role-neutral setup, operation, maintenance, and readiness guidance.
- `CV/gazebo/`: retained Gazebo source assets. They are not connected to the
  integration scaffold.
- `CI/`: an unsupported workspace reserved for a possible future composition
  adapter. It is not a supported entry point.

The Year-1 implementation and its artifact formats were retired rather than
carried forward. Git history remains the archive.
