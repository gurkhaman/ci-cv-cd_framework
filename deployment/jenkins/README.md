# Jenkins Deployment

This directory owns the reconstructable Jenkins control plane and five inbound
agents. The controller runs on supported Ubuntu LTS x86_64, has no executors,
and is started only on demand. One dedicated `integration` agent performs only
integration work; four independently replaceable agents provide the
`composition`, `image-build`, `cv`, and `cd` scheduling labels. The root pipeline
executes one submitted Fixture chain across those five agents.

The tracked authority is:

- `controller/Dockerfile`: Jenkins `2.568.3` on JDK 21, pinned to the reviewed
  Linux/amd64 image digest.
- `controller/plugins.txt`: the complete 47-plugin runtime set, including every
  transitive dependency at an exact version.
- `agents/Dockerfile`: the exact Jenkins Remoting, Python 3.12.13, `uv`, and
  locked production integration package used to construct each minimal agent
  image without inherited volume metadata.
- `casc/jenkins.yaml`: global security, zero-executor, five-node, retention, and
  Job DSL configuration.
- `jobs/pipeline-integration.groovy`: the fixed parameterized Pipeline SCM job.
- `compose.yaml`: loopback-only controller publication, named controller state,
  five agent services, and a separate egress-capable network for each agent.
- `Jenkinsfile`: the thin repository-owned scheduler over public integration CLI
  operations.
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
- `uv` for strict adapter-descriptor validation and runtime-image selection.
- Outbound HTTPS access to Docker Hub, GHCR, the Jenkins update service during a
  clean image build, PyPI during a clean agent build, and GitHub for each
  agent's independent immutable checkout.

`bin/stack validate` verifies the live Docker platform, secret ownership and
permissions, supplied values, Dockerfile syntax, and the fully interpolated
Compose model. It intentionally does not install Docker, alter the firewall, or
enable a host-boot service.

## Local Configuration

Copy `config/controller.env.example` to the ignored
`config/local/controller.env` and set every value. The repository URL must be an
uncredentialed HTTPS GitHub clone URL. `JENKINS_AGENT_CONTROLLER_URL` is
exactly `http://controller:8080` for local Compose agents and an uncredentialed
HTTPS URL for a relocated agent. Persisted container IPs and machine names are
not configuration. The host port, user IDs, endpoint, repository identity, and
secret locations are local deployment settings rather than tracked topology.
The five `*_LIMIT_SECONDS` settings are trusted deployment overrides for the
tracked 90-minute run and 10, 30, 45, and 15 minute Stage defaults. They must be
positive integers no greater than one day; the run limit must leave at least two
minutes for finalization. Workflow dispatch parameters cannot change them.

Create the administrator and machine-user password files outside the checkout.
Each file must be nonempty, owned by the operator, and inaccessible to group and
other users, for example mode `0600`. Compose mounts them through `/run/secrets`;
secret values are never placed in tracked files or Compose environment values.

JCasC creates the two local identities from those bootstrap files. The
administrator has `Overall/Administer`. The handoff machine user has only
`Overall/Read` globally and `Job/Read`, `Job/Build`, and `Job/Cancel` on the fixed
job. Anonymous users receive no permissions. The administrator must later issue
the named machine-user API token and place it only in the protected
`pipeline-integration-jenkins` GitHub environment. Password authentication is
not the GitHub-to-Jenkins contract.

The dedicated GitHub runner uses the host interface under
`deployment/github-runner/`. It is a separate low-privilege user service with no
Docker socket or Jenkins-state access. The service environment never persists
this API token; GitHub releases it only to the protected handoff job.

JCasC owns one reviewed script-sandbox approval: reading
`FlowInterruptedException.getCauses()` lets the thin scheduler distinguish a
Jenkins timeout from external cancellation. No other custom signature is
approved.

JCasC creates five permanent inbound node identities. Their physical names and
pipeline-facing labels are:

| Node | Explicit label | Executors | Responsibility |
| --- | --- | ---: | --- |
| `integration` | `integration` | 1 | Revalidation, preflight, assembly, bundle validation, and archive preparation |
| `ci` | `composition` | 1 | Composition Stage adapter |
| `image-build` | `image-build` | 1 | Image-build Stage adapter |
| `cv` | `cv` | 1 | CV Stage adapter |
| `cd` | `cd` | 1 | CD Stage adapter |

Each node has its own controller-generated registration secret. For a new or
destroyed controller volume:

1. Configure five distinct future secret-file paths outside the checkout.
2. Run `deployment/jenkins/bin/stack start-controller`.
3. In **Manage Jenkins > Nodes**, open each node and write its displayed inbound
   registration secret to the corresponding configured file with mode `0600`.
4. Run `deployment/jenkins/bin/stack start` to validate the distinct secrets,
   build the agents, and connect all five over WebSocket.

Never copy one registration secret between nodes. Destroying `jenkins-home`
invalidates the old files; remove them and repeat provisioning. The integration
agent receives no Domain credential and has no Domain label. The four Fixture
descriptors currently declare no Domain secret binding, so every agent container
sees only its own registration file and no controller password.

Each image contains an immutable role and label identity under `/etc/sdi` on its
read-only root filesystem. The supported Stage execution commands refuse every
Domain adapter on the `integration` image and require a Domain image's label to
match the selected descriptor before starting a child process. Integration-only
preflight and finalization likewise reject a Domain image. Job environment
changes cannot alter that boundary. Local execution outside an agent image has no
deployment role and remains available for development.

## Operation

Run from any directory:

```sh
deployment/jenkins/bin/stack validate
deployment/jenkins/bin/stack start
deployment/jenkins/bin/stack status
deployment/jenkins/bin/stack logs
deployment/jenkins/bin/stack stop
```

`start` builds the exact controller and five agent images, starts all six
containers, rejects an agent image that declares any volume, and waits for
process health. Jenkins is available only at
`http://127.0.0.1:<configured-port>`. No inbound-agent TCP port is published;
all agents connect over WebSocket. `stop` removes all containers and networks
but retains only the `jenkins-home` named volume. Agent workspaces are separate
tmpfs mounts, have no shared or persistent filesystem, and disappear whenever
their container is replaced. Their five separate networks allow GitHub checkout
without making peer agents reachable.

The root pipeline deletes every allocated workspace before checkout and in a
`finally` block. Each agent independently checks out and verifies the submitted
protected-main commit. Accepted envelopes and their declared bounded files are
the only cross-agent Stage data, transferred through Execution-ID-qualified
Jenkins stashes. The integration agent validates the complete bundle before
archiving all and only its declared files from the current build.

The pipeline defaults to 10, 30, 45, and 15 minute Python-owned Stage work
limits, a 90-minute Jenkins run limit, and a two-minute finalization limit. The
validated local deployment settings may override the Stage and run limits. A
handled machinery failure archives its structured result before Jenkins ends in
`FAILURE`; a contract-valid negative Domain outcome can remain `SUCCESS`.
External cancellation propagates immediately, cleans active workspaces and
process trees, ends as `ABORTED`, and does not manufacture a result.

Each Domain service owns its descriptor-selected `RUNTIME_IMAGE` build input and
local image tag. `bin/stack` loads each image through the integration CLI's strict
descriptor model and injects it only into the matching Compose service. A
reviewed descriptor image replacement followed by
`bin/stack reconcile-agent <ci|image-build|cv|cd>` builds and replaces only the
affected Domain agent without starting or rebuilding the controller. The node
identity and scheduling label remain unchanged. Only the selected agent's
registration secret is validated or mounted during reconciliation.

`bin/stack destroy` is the explicit destructive operation for reconstruction. It
removes the controller and its named volume. Back up operational state first if
it must be retained.

## Verification

Run the empty-state runtime acceptance check with:

```sh
deployment/jenkins/bin/stack smoke
```

The smoke check uses an isolated Compose project, owner-only temporary bootstrap
files, five distinct temporary registration secrets, a random loopback port,
and a fresh named volume. It verifies the exact Jenkins, plugin, Remoting,
Python, and `uv` inputs; native JCasC validation; controller and agent executor
counts; labels and identities; WebSocket connectivity; the fixed job; and the
handoff authorization boundary. Temporary jobs clean before and after each
allocation and execute twice on every label to prove correct node allocation,
clean workspaces, the integration/Domain role boundary, one-secret visibility,
missing Docker access, and runtime network isolation. The checker also
distinguishes missing labels, invalid mappings, incorrect executor counts,
unavailable agents, and admissible busy agents.

The smoke then mutates controller and node configuration, deletes one managed
node and the fixed job, restarts all six containers while preserving the
controller volume, and proves JCasC and Job DSL restore repository state. Agent
tmpfs workspaces are recreated empty. Cleanup removes the isolated smoke volume.

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

The agent build combines the descriptor-selected CPython 3.12.13 Fixture runtime
with the official Jenkins inbound-agent `3391.va_37fa_a_305d6d-2` JDK 21 image
and official `uv` 0.12.1 image. The production integration package and its exact
dependencies are installed from `integration/uv.lock` while the image has build
network access, so agents need no runtime package download.
All three image inputs are immutable multi-platform digests, and the deployment
selects Linux/amd64. Remoting files are copied into the descriptor runtime rather
than inherited as the final image, preventing the upstream anonymous volume
declarations from persisting agent state. The custom entrypoint exists only to
read the externally mounted registration file without placing its value in
Compose environment configuration.

Controller health and Fixture execution prove pipeline machinery only. They are
not Domain success, Validation evidence, deployment evidence, or KPI evidence.
