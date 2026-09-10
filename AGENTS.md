# Repository Guide

## Boundaries

- `integration/` is the supported Python project and owns the repository verification command. Run its commands from the directory stated below.
- `CI/` is an explicitly unsupported workspace reserved for a future composition adapter. The architecture in `CI/APSEC_2025_Tools.pdf` is design context, not implemented code.
- The Year-1 plugin and prototype were retired. Git history is their archive; do not restore their interfaces or artifact formats.
- Read `CONTEXT.md` and `docs/project/PROJECT-CONTEXT.md` for project terminology and the cached synthesis of the stable project PDFs. Do not reread those PDFs unless exact source wording, a diagram, compliance evidence, or an uncaptured detail is required.
- Most tracked files are Gazebo assets under `CV/gazebo/models/`; avoid repository-wide formatting or generated-file rewrites there.

## Engineering Choices

- Prefer mature, actively maintained, purpose-built libraries for standard capabilities such as parsing and validation when they satisfy the project contract. Assess compatibility, security posture, licensing, and maintenance, then lock the selected version exactly.
- Build custom infrastructure only when no suitable library exists or project-specific semantics require it, and record that rationale in the implementing issue or code review.

## Pipeline Integration Scaffold

- `integration/` requires Python `>=3.12,<3.13`, pins CPython 3.12.13 for development, and uses its own exact `uv.lock`.
- Run the deterministic repository checks from any directory with `integration/scripts/verify`. It performs frozen environment setup, Ruff formatting and linting, Basedpyright, and pytest.
- The public entry point is `uv run --project integration sdi-integration`. The CLI, committed file contracts, and future language-neutral Stage-adapter process interface are the supported boundaries; internal Python modules are not compatibility contracts.
- The current scaffold proves packaging and baseline verification only. Real Domain adapters, Stage execution, Jenkins deployment, and the GitHub workflow arrive in their dedicated implementation tickets.

## Issue And Review Workflow

- Treat one implementation issue as one review unit. Planning and human-acceptance issues need a pull request only when they change repository files.
- Start an unblocked issue from the latest merged `main`, move it to In Progress, and use its Linear-generated branch name.
- Prefix commit and pull-request titles with the Linear identifier, for example `GUR-33 Validate and identify one committed run`, so Linear can associate the code automatically.
- Implement and verify only that issue, review the complete diff, push the branch, and open one pull request against `main`.
- Attach the commit URL and pull request to the Linear issue, then move it to In Review.
- After approval, merge the pull request and move the issue to Done. Begin the next blocked issue only after its blocker is merged and Done.

## CV Assets

- Gazebo files are source assets for an external TurtleBot3 workspace. Copy the world, model directories, and launch file into `turtlebot3_gazebo`, then run `colcon build --symlink-install` from `turtlebot3_ws` before `ros2 launch turtlebot3_gazebo office.launch.py`.
- The Gazebo assets are not wired into the pipeline integration scaffold and provide no automated scenario, oracle, or Validation evidence.
- `CV/gazebo/world_parser.py` executes on import. Run it only from `CV/gazebo`; it copies model trees into the ignored `_models/` directory.

## Generated And Local Files

- `.gitignore` excludes Python caches and environments, generated artifacts, workspaces, recovery artifacts, local deployment configuration, runtime secrets, and Gazebo parser output without hiding committed schemas or Fixture cases.
- No active root `Jenkinsfile` or GitHub workflow exists until its implementation ticket. The README files under `deployment/jenkins/` and `.github/workflows/` reserve those owned areas without providing runnable placeholders.
- Check `git status` after commands and keep machine-specific values, secrets, generated evidence, and recovery state out of commits.

## Agent skills

### Issue tracker

Issues are tracked in Linear under the Gurkhaman team's SDI project. See `docs/agents/issue-tracker.md`.

### Triage labels

The tracker uses the five canonical triage label names. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain documentation layout. See `docs/agents/domain.md`.
