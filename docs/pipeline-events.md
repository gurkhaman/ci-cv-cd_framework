# Pipeline Events

While a Jenkins build runs one combination through Composition, Image build,
CV and CD, the `sdi-integration` CLI and the Stage runtime print pipeline
events on stderr. This page is the reference for their lines and fields, and
for the checker that proves a console follows them.

A pipeline event is a pair of lines, written together in one `write`:

```text
[sdi] C-05 CV > stage-started  RUNNING  CV started
PRISM_EVENT {"schema_version":"sdi.pipeline-event/v1",...}
```

- The `PRISM_EVENT` line is the contract. It is a full snapshot of the
  combination's row.
- The `[sdi]` line is for people. Nothing parses it.
- Events are live and best-effort. The Pipeline integration result wins on any
  disagreement.
- The line with `"final":true` is the only authoritative line.

The row model is `PipelineEventRow` in
`integration/src/sdi_pipeline_integration/_pipeline_events.py`. The step
catalogue is in
`integration/src/sdi_pipeline_integration/_pipeline_event_catalogue.py`. The
generated schema is `integration/schemas/pipeline-event-v1.schema.json`.

## Line Grammar

### The `PRISM_EVENT` Line

- It starts at column 0 with `PRISM_EVENT `, followed by compact JSON on the
  same line: no spaces between tokens, and ASCII only.
- Keys come in the model's field order, the order of the tables below. Do not
  depend on that order.
- Ignore fields you do not know. Version 1 only adds fields; no field is
  renamed, and no field changes meaning.
- The generated schema allows unknown properties. The model itself forbids
  them, so the CLI's own checks are stricter than the schema.

### The `[sdi]` Line

- Stage level: `[sdi] {composition_id} {STAGE} > {step}  {status}  {message}`.
  `{STAGE}` is `COMPOSITION`, `IMAGE-BUILD`, `CV` or `CD`, and `{status}` is
  that Stage's own status field.
- Run level: `[sdi] {composition_id} RUN > {step}  {message}`, with no status
  column.
- The separator is the ASCII `>`. Two spaces separate the step, the status and
  the message.

### Characters and Size

Both lines are pure ASCII. Apart from the terminating LF, neither contains a
control character, DEL or the sequence `##[`.

| Character | `PRISM_EVENT` line | `[sdi]` line |
| -- | -- | -- |
| A line break in a slot value | removed from the message | removed from the message |
| Any other character outside 0x20 to 0x7E | `\uXXXX`, as JSON writes it | `\uXXXX`, the same escape |
| A character above U+FFFF | a `\uXXXX\uXXXX` surrogate pair | the same surrogate pair |
| A backslash | `\\`, as JSON writes it | `\\`, so every escape can be read back |
| The `[` of `##[` | `\u005b` | `\u005b` |

`\u005b` is a valid JSON escape, so a JSON parser reads `##[` back.

- A `PRISM_EVENT` line is at most 4096 bytes, counting the prefix and not the
  LF. An over-long line sets whole fields to null, one at a time, and keeps
  their keys: `image_build_result_ref`, `validation_evidence_ref`,
  `deployment_result_ref`, `image_failures`, `placement_issues`,
  `cv_service_issues`, `mission_steps`, `image_digest`, then `image_ref`.
  It skips a field that is already null or an empty list. A value is never
  cut. The writer adds a `sdi-integration: internal error:` line that names
  the nulled fields.
- An `[sdi]` line is at most 1024 bytes. Every template fits with
  maximum-length slots. If a message is still too long, its identifier slots
  (Combination ID, profile id, generator, service id, host and Reason code) are
  shown as `-`, and an internal error line is printed.

A failed write to stderr is ignored. It never raises, and it never changes a
result or an exit code.

## Fields

Every field is present on every line, and is `null` until it is known. A
closed field is an enum the schema enforces. Every other code-like value is a
string, and the schema lists its known values as `examples`.

### Envelope

| Field | Value |
| -- | -- |
| `schema_version` | `"sdi.pipeline-event/v1"` |
| `emitted_at` | UTC, RFC 3339, milliseconds, `Z` suffix |
| `execution_id` | the run's Execution ID |
| `final` | `true` only on the `run-finished` line |
| `stage` | closed: `composition`, `image_build`, `cv`, `cd`, or null on run-level lines |
| `step` | a step code from the catalogue below |
| `message` | rendered from the step's template, never free text |

### Row Identity

| Field | Value |
| -- | -- |
| `composition_id` | the Combination ID, such as `"C-05"` |
| `scenario_id`, `testcase_id`, `profile_id` | from the run request |
| `target` | closed: `"SDV"`, `"SDR"`, or null |
| `architecture` | closed: `"amd64"`, `"arm64"`, or null |

### Statuses

A status is one of `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED` and
`NOT_EVALUATED`.

| Field | Value |
| -- | -- |
| `composition_status`, `image_build_status` | each CI Stage's own status |
| `ci_status` | derived from the two CI halves |
| `cv_status` | CV's status |
| `cd_status` | CD's status |
| `failure_reason` | a Reason code, or null |

`ci_status` is `PENDING` while both halves are `PENDING` and `RUNNING` while
either half runs. When both halves are terminal, it is the worse of the two,
ranked `FAILED`, `SKIPPED`, `NOT_EVALUATED`, `SUCCEEDED`. When one half is
terminal and the other is `PENDING`, it is `RUNNING` during a run, and the
terminal half's status in an `execute-stage` row.

A Stage's own status is `RUNNING` on each of its runtime lines before its
`stage-finished` line, and terminal on that line. It reads `SUCCEEDED` first
on its own `stage-finished` line, and it stays `SUCCEEDED` on every later line
except possibly the final one.

### CI Results

| Field | Value |
| -- | -- |
| `ci_duration_ms` | Composition's and Image build's process durations, summed |
| `blueprint_ref` | `stages/composition/composition-blueprint.json` |
| `deployment_schema_ref` | `stages/composition/deployment-schema.json` |
| `image_build_result_ref` | `stages/image-build/image-build-result.json` |
| `image_ref` | one tag per built image, sorted by service id |
| `image_digest` | each built image's digest, in the same order |
| `image_archive` | always null |
| `images_built`, `images_total` | built images, and every service in the deployment schema |
| `image_failures` | `{"service_id", "reason"}` for each failed image |
| `platform_status` | closed: `"MATCH"`, `"MISMATCH"`, or null |
| `detected_platform` | `linux/<architecture>` for the row's architecture, or null |

### CV Results

| Field | Value |
| -- | -- |
| `cv_duration_ms` | CV's process duration |
| `validation_evidence_ref` | `stages/cv/validation-evidence.json` |
| `cv_platform` | the platform the evidence records, such as `"linux/amd64"` |
| `cv_services_participating`, `cv_services_blocked`, `cv_services_total` | service counts |
| `cv_service_issues` | `{"service_id", "participation", "reason"}` for each service that did not participate or was not evaluated |
| `mission_setup` | `passed`, `timed_out`, or null |
| `mission_steps` | the eight S-04 steps in order, each `{"id", "status"}` |

### CD Results

| Field | Value |
| -- | -- |
| `cd_duration_ms` | CD's process duration |
| `deployment_result_ref` | `stages/cd/deployment-result.json` |
| `cd_placements_deployed`, `cd_placements_blocked`, `cd_placements_total` | placement counts |
| `placement_issues` | `{"service_id", "host", "status", "reason"}` for each placement that is neither deployed nor blocked |

A failed placement's reason is `CrashLoopBackOff`, `ErrImagePull`,
`ImagePullBackOff`, `Unschedulable` or `other`.

## Steps

The runtime prints the run-level and every-Stage steps. A Stage's adapter
reports the Stage-specific steps through its progress channel. `{Stage}` is
`Composition`, `Image build`, `CV` or `CD`.

| Scope | Steps |
| -- | -- |
| Run level | `run-accepted`, `run-finished` |
| Every Stage | `stage-started`, `adapter-running`, `work-limit-reached`, `run-deadline-reached`, `checking-output`, `publishing`, `stage-finished` |
| Composition | `discovery`, `generation`, `assessment` |
| Image build | `builder-check`, `building`, `image-built`, `image-failed` |
| CV | `screening`, `service-unfit`, `leftover-cleanup`, `sim-cached`, `sim-build`, `pull`, `pull-blocked`, `start`, `mission`, `mission-setup-done`, `mission-step`, `collecting-result`, `teardown` |
| CD | `nodes-wait`, `nodes-ready`, `applying`, `pod-running`, `pod-not-running`, `settling`, `judging`, `cleanup` |

While `generation`, `sim-build`, `building` or `mission` is a Stage's last
step, the runtime repeats it every three minutes with its heartbeat message
and the same step code.

One Stage's runtime lines follow one of these paths:

| Case | Steps |
| -- | -- |
| Normal, or rejected output | `stage-started`, `adapter-running`, `checking-output`, `publishing`, `stage-finished` |
| The adapter cannot start | `stage-started`, `publishing`, `stage-finished` |
| Nonzero exit, signal, or surviving descendants | `stage-started`, `adapter-running`, `publishing`, `stage-finished` |
| Work limit or run deadline | `stage-started`, `adapter-running`, `work-limit-reached` or `run-deadline-reached`, `publishing`, `stage-finished` |
| Skipped before the adapter starts | `stage-finished` |

A Stage that is killed stops after any step of a path, before
`stage-finished`. Only the last Stage that printed can stop early.

## Checking a Console

`check-events` checks the events in a console file, such as a test's captured
stderr or a Jenkins `consoleText`. Run it from `integration/`:

```sh
uv run --frozen sdi-integration check-events consoleText --real-run
uv run --frozen sdi-integration check-events stderr.txt --golden g9-race
```

- `--real-run` checks the line rules, the Stage paths and the final line.
- `--golden NAME` also compares the runtime lines with the golden file
  `NAME.txt` in `integration/tests/data/pipeline-events/`, and checks its extra
  field assertions. `--goldens DIRECTORY` reads goldens from another directory.

Exit code 0 means the console conforms. Exit code 1 means it does not, and each
violation is one stdout line in the form `<file>:<line>: [<rule>] <detail>`.
Exit code 2 means the file or the golden could not be read.

The checker reads only lines that start at column 0 with `[sdi] ` or
`PRISM_EVENT `. Shell trace lines and other output are skipped, but each
`PRISM_EVENT` line needs its `[sdi]` line directly before it. A console
decorator such as a timestamp prefix fails the check. Each pair must be
byte-equal to the renderer's output for the row the `PRISM_EVENT` line holds.

The final line's statuses equal those of the last `stage-finished` line, or
all `PENDING` when there is none. When the final `failure_reason` is
`sdi.run.deadline-exceeded`, one Stage may instead become `FAILED` and every
later Stage `SKIPPED`. That Stage is the first Stage still `PENDING`, or the
Stage of the last `stage-finished` line, whatever status that line shows.

### Golden Files

A golden file holds one projection per runtime line, in order:

```text
<stage or RUN> <step> [final] [<composition> <image build> <cv> <cd>] <failure_reason or ->
```

The status letters are `P` (`PENDING`), `R` (`RUNNING`), `S` (`SUCCEEDED`),
`F` (`FAILED`), `K` (`SKIPPED`) and `N` (`NOT_EVALUATED`). Lines that start
with `#` are comments. An indented line asserts a field of the line above it:

| Assertion | Meaning |
| -- | -- |
| `<field> == <json>` | equals a JSON value |
| `<field> != null` | is set |
| `<field> == @<field>` | equals another field of the same line |
| `<field> == @<field> - <n>` | equals another integer field minus `n` |
| `<field> contains <json object>` | a list with an element that has these keys and values |
| `<field>[*].<key> != null` | a list whose elements all set this key |
