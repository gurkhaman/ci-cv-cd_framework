# GitHub Handoff Runner

This directory owns the host interface for the one self-hosted runner permitted
to hand a Pipeline integration run to private Jenkins. The runner is pinned to
GitHub Actions runner `2.337.0` for Linux x86_64 and the upstream archive's
published SHA-256 digest. It is configured with automatic binary updates
disabled so a reviewed pin cannot drift.

## Account Boundary

Create a dedicated unprivileged account on the Jenkins host. The account must:

- not be `root` and not belong to `sudo`, `wheel`, or `docker`;
- have no readable or writable Docker socket;
- have no access to Jenkins controller state, bootstrap secrets, agent
  registration secrets, or backup files;
- own only its home, runner installation, and runner work directories;
- have outbound HTTPS access to GitHub, Python downloads, and PyPI;
- reach Jenkins only through the configured loopback endpoint.

Install the runner's documented native dependencies and enable lingering for
this account as a separate host-administrator operation. The tracked interface
does not elevate privileges. All installation and service commands below run as
the dedicated account and use `systemctl --user`.

## Installation

From the repository checkout, run:

```sh
deployment/github-runner/bin/runner install
```

`install` downloads exactly
`actions-runner-linux-x64-2.337.0.tar.gz`, verifies digest
`70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`,
and installs into `~/.local/share/sdi-github-runner`.

Create a short-lived repository runner registration token, then configure the
installation once:

```sh
SDI_GITHUB_RUNNER_REPOSITORY=https://github.com/gurkhaman/ci-cv-cd_framework \
SDI_GITHUB_RUNNER_NAME=jenkins-handoff-host \
SDI_GITHUB_RUNNER_REGISTRATION_TOKEN='<short-lived-registration-token>' \
deployment/github-runner/bin/runner configure

deployment/github-runner/bin/runner install-service
deployment/github-runner/bin/runner start
deployment/github-runner/bin/runner status
```

The registration token is configuration-time input, not a service environment
value. The runner receives only the custom label `sdi-jenkins-handoff`; default
labels are disabled. Automatic updates are disabled. The user service enables
`NoNewPrivileges`, a read-only host filesystem with only runner diagnostics and
job work paths writable, private devices and temporary files, and explicit
removal of Jenkins credential and state variables from its persistent
environment. The workflow places uv's cache and managed Python installation
under the writable runner temporary directory.

GitHub personal accounts do not provide workflow-restricted runner groups. This
repository therefore treats the runner label as an operational selector, not an
authorization control. The supported threat model requires trusted write
collaborators, reviewed protected-main changes, approval for every external fork
workflow, no approval of untrusted workflow code, and a repository rule that no
other workflow may target `sdi-jenkins-handoff`. Use an organization
workflow-restricted runner group instead if those assumptions change.

Stop the service without removing registration or work state with:

```sh
deployment/github-runner/bin/runner stop
```

The runner software must be reviewed and repinned within 30 days of a new
GitHub Actions runner release. A critical security update can require an
immediate replacement before GitHub will schedule more work.

Jenkins's machine-user API token is never configured on this host interface.
Store it only in the protected GitHub environment; GitHub injects it into the
one approved job for that job's lifetime.
