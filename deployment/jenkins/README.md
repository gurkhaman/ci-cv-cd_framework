# Jenkins Deployment

This directory owns the reconstructable Jenkins control plane. The controller
runs on supported Ubuntu LTS x86_64, has no executors, and is started only on
demand. It does not run Domain work. The five inbound agents and the root
pipeline arrive in later implementation issues.

The tracked authority is:

- `controller/Dockerfile`: Jenkins `2.568.3` on JDK 21, pinned to the reviewed
  Linux/amd64 image digest.
- `controller/plugins.txt`: the complete 47-plugin runtime set, including every
  transitive dependency at an exact version.
- `casc/jenkins.yaml`: global security, zero-executor, retention, and Job DSL
  configuration.
- `jobs/pipeline-integration.groovy`: the fixed parameterized Pipeline SCM job.
- `compose.yaml`: loopback-only controller publication, named state volume, and
  isolated controller-agent network.
- `bin/stack`: the supported validation and lifecycle interface.

Jenkins Configuration as Code and Job DSL run on every controller startup. A
restart therefore restores tracked global and job configuration after mutable UI
drift. Runtime history and Jenkins-generated state persist in the named volume,
but they are not configuration authority.

## Host Requirements

- Ubuntu LTS on x86_64.
- Docker Engine with the Compose v2 plugin, usable by the operator without
  privilege escalation.
- Python 3 for the isolated smoke-check client.
- Outbound HTTPS access to Docker Hub, the Jenkins update service during a clean
  image build, and GitHub when the job eventually checks out a revision.

`bin/stack validate` verifies the live Docker platform, secret ownership and
permissions, supplied values, Dockerfile syntax, and the fully interpolated
Compose model. It intentionally does not install Docker, alter the firewall, or
enable a host-boot service.

## Local Configuration

Copy `config/controller.env.example` to the ignored
`config/local/controller.env` and set every value. The repository URL must be an
uncredentialed HTTPS GitHub clone URL. The host port, user IDs, repository
identity, and secret locations are local deployment configuration rather than
tracked topology.

Create the administrator and machine-user password files outside the checkout.
Each file must be nonempty, owned by the operator, and inaccessible to group and
other users, for example mode `0600`. Compose mounts them through `/run/secrets`;
secret values are never placed in environment variables, command arguments, or
tracked files.

JCasC creates the two local identities from those bootstrap files. The
administrator has `Overall/Administer`. The handoff machine user has only
`Overall/Read` globally and `Job/Read`, `Job/Build`, and `Job/Cancel` on the fixed
job. Anonymous users receive no permissions. The administrator must later issue
the named machine-user API token and place it in the protected GitHub environment
defined by the handoff implementation. Password authentication is not the
GitHub-to-Jenkins contract.

## Operation

Run from any directory:

```sh
deployment/jenkins/bin/stack validate
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack status
deployment/jenkins/bin/stack logs
deployment/jenkins/bin/stack stop
```

`start` builds the exact controller and waits for Jenkins health. Jenkins is
available only at `http://127.0.0.1:<configured-port>`. No inbound-agent TCP port
is published; later agents connect over WebSocket. `stop` removes the containers
and networks but retains the `jenkins-home` named volume.

`bin/stack destroy` is the explicit destructive operation for reconstruction. It
removes the controller and its named volume. Back up operational state first if
it must be retained.

## Verification

Run the empty-state runtime acceptance check with:

```sh
deployment/jenkins/bin/stack smoke
```

The smoke check uses an isolated Compose project, owner-only temporary bootstrap
files, a random loopback port, and a fresh named volume. It verifies startup,
the exact Jenkins and plugin versions, JCasC validation through the plugin's
native check endpoint, zero executors, the disabled inbound-agent port, the
fixed job, and the handoff authorization boundary. It then introduces executor
and job drift, restarts while preserving the volume, and proves JCasC and Job DSL
restore the repository state. Cleanup removes the isolated smoke volume.

`integration/scripts/verify` includes this smoke check after the Python package
checks. It therefore requires the supported Docker host and network access when
the controller image is not already cached.

## Pin Maintenance

The selected controller is the current weekly-reviewed Jenkins LTS available for
this implementation and uses the official immutable Linux/amd64 image manifest.
The plugin closure was resolved with the controller image's maintained Jenkins
Plugin Installation Manager using `--latest=false`; startup and the smoke check
prove that the runtime set equals the lock exactly. Jenkins core provides the
global simple build discarder, so no extra build-discarder plugin is needed.

Review Jenkins security advisories, plugin health and licenses, minimum-core
requirements, and upstream release notes before changing any pin. Refresh the
complete closure as one review unit, run the empty-state smoke check, and retain
the previous image reference for operational rollback. Primary maintenance
sources are the Jenkins LTS changelog, official Jenkins Docker image, Jenkins
plugin index, Configuration as Code plugin, Job DSL plugin, and Matrix
Authorization plugin.

Controller health and Fixture execution prove pipeline machinery only. They are
not Domain success, Validation evidence, deployment evidence, or KPI evidence.
