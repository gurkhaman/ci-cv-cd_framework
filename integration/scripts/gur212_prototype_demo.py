"""THROWAWAY PROTOTYPE (GUR-212): run every progress-channel case and save its output.

usage: uv run python scripts/gur212_prototype_demo.py PROOF_DIR [CASE ...]
Cases: 1-composition 2-burst 3-hostile 4-timeout 5-stalled-stderr 6-sigterm
       7-descendants 8-never-reports 9-cv-hop
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTEGRATION))
RUNNER = INTEGRATION / "scripts/gur212_prototype_runner.py"
PYTHON = sys.executable
F_SETPIPE_SZ = 1031

PREAMBLE = """#!/usr/bin/env python3
import os, sys, time, json, subprocess
from pathlib import Path
from sdi_pipeline_integration._progress_channel_prototype import Reporter
WORK = Path(__file__).parent
"""


class Proof:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = []
        self.failed = False

    def say(self, text: str = "") -> None:
        self.lines.append(text)
        print(text, flush=True)

    def check(self, ok: bool, what: str) -> None:  # noqa: FBT001
        self.failed |= not ok
        self.say(f"CHECK {'PASS' if ok else 'FAIL'}: {what}")

    def save(self) -> None:
        self.say(f"RESULT: {'FAIL' if self.failed else 'PASS'}")
        self.path.write_text("\n".join(self.lines) + "\n")


def adapter(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(PREAMBLE + body)
    path.chmod(0o755)
    return path


def run_streaming(  # noqa: PLR0913
    proof: Proof,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    stall_seconds: float = 0.0,
    pipe_bytes: int | None = None,
    sigterm_at: float | None = None,
    timeout: float = 120.0,
) -> tuple[int, list[str], str, float]:
    """Run argv; stderr lines get a read-time stamp. Optionally stall the reader."""
    read_fd, write_fd = os.pipe()
    if pipe_bytes is not None:
        fcntl.fcntl(write_fd, F_SETPIPE_SZ, pipe_bytes)
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=write_fd, env=env, cwd=INTEGRATION
    )
    os.close(write_fd)
    lines: list[str] = []

    def reader() -> None:
        if stall_seconds:
            time.sleep(stall_seconds)
            proof.say(f"[demo] stderr reader resumes at +{time.monotonic() - started:.2f}s")
        with os.fdopen(read_fd, "rb") as stream:
            for raw in stream:
                text = raw.decode("utf-8", "replace").rstrip("\n")
                stamp = f"+{time.monotonic() - started:6.2f}s"
                lines.append(text)
                proof.say(f"{stamp} | {text}")

    thread = threading.Thread(target=reader)
    thread.start()
    if sigterm_at is not None:
        time.sleep(sigterm_at)
        wchan = Path(f"/proc/{process.pid}/wchan").read_text().strip()
        proof.say(f"[demo] SIGTERM -> runtime pid {process.pid} at +{time.monotonic() - started:.2f}s "
                  f"(runtime kernel wait channel: {wchan or 'running'})")
        process.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        pid_file = Path(argv[3]).parent / "adapter.pid" if len(argv) > 3 else None
        if pid_file is not None and pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 0)
                state = "STILL ALIVE"
            except ProcessLookupError:
                state = "gone"
            proof.say(f"[demo] +{time.monotonic() - started:.2f}s: adapter {state}; runtime "
                      f"{'exited' if process.poll() is not None else 'still running (flushing stderr?)'}, "
                      f"wait channel {Path(f'/proc/{process.pid}/wchan').read_text().strip() if process.poll() is None else '-'}")
    stdout, _ = process.communicate(timeout=timeout)
    elapsed = time.monotonic() - started
    thread.join(timeout=timeout)
    return process.returncode, lines, stdout.decode(), elapsed


def runner_env(heartbeat: float = 180) -> dict[str, str]:
    return os.environ | {"SDI_PROGRESS_HEARTBEAT_SECONDS": str(heartbeat)}


def run_runner(proof: Proof, work: Path, script: Path, limit: int, services: int, **kw: object):  # noqa: ANN201
    heartbeat = kw.pop("heartbeat", 180)
    argv = [PYTHON, str(RUNNER), str(work / "w"), str(script), str(limit), str(services)]
    proof.say(f"$ {' '.join(argv)}   (heartbeat={heartbeat}s)")
    rc, lines, stdout, elapsed = run_streaming(proof, argv, env=runner_env(heartbeat), **kw)  # type: ignore[arg-type]
    proof.say(f"[demo] runtime exit code {rc} after {elapsed:.2f}s; stdout: {stdout.strip()}")
    return rc, lines, stdout, elapsed


def sdi(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("[sdi]")]


def steps(lines: list[str]) -> list[str]:
    return [line.split(" › ")[1].split()[0] for line in sdi(lines)]


def prism(lines: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("PRISM_EVENT ")) for line in lines if line.startswith("PRISM_EVENT ")]


def terminal_last(lines: list[str]) -> bool:
    events = [line for line in lines if line.startswith(("[sdi]", "PRISM_EVENT"))]
    return len(events) >= 2 and "stage-finished" in events[-2] and '"step":"stage-finished"' in events[-1]


# ------------------------------------------------------------------ cases


def case_composition(proof: Proof, work: Path) -> None:
    from tests._proposals import COMPLETE
    from tests._stub_provider import STUB_KEY, _Handler, proposals_body, stub_config
    from tests.test_composition_stage import CONFIG_PATH, RUN_REQUEST_PATH, SERVICE_REPOSITORY, _repository

    slow = 5.0

    def do_post(self) -> None:  # noqa: ANN001
        if self.path.endswith("/responses"):
            time.sleep(slow)
        _Handler.do_POST(self)

    handler = type("Slow", (_Handler,), {
        "body": json.dumps(proposals_body([COMPLETE])).encode(), "received": [], "do_POST": do_post,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    commit = _repository(work, {CONFIG_PATH: stub_config(base_url)})
    argv = [
        str(Path(PYTHON).parent / "sdi-integration"), "dispatch-local",
        "--repository", str(work / "repository"), "--requested-ref", "refs/heads/main",
        "--resolved-commit", commit, "--run-request-path", RUN_REQUEST_PATH,
        "--service-repository", str(SERVICE_REPOSITORY), "--bundle-root", str(work / "bundle"),
    ]
    proof.say(f"Real Composition adapter, real runtime (dispatch-local), stub provider answers after {slow}s, heartbeat 1.5s")
    proof.say("$ " + " ".join(argv))
    env = {k: v for k, v in runner_env(1.5).items() if k not in {"OPENAI_API_KEY", "VLLM_KEY"}}
    rc, lines, stdout, elapsed = run_streaming(proof, argv, env=env | {"VLLM_KEY": STUB_KEY})
    server.shutdown()
    result = json.loads(stdout)
    composition = result["attempts"][0]
    proof.say(f"[demo] exit {rc} after {elapsed:.1f}s; composition attempt: "
              f"{composition['execution_conclusion']}/{composition['domain_outcome']} {composition.get('reason', {}).get('code')}")
    got = steps(lines)
    proof.check(rc == 0, "dispatch-local exit 0")
    order = list(dict.fromkeys(s for s in got if s in {"discovery", "generation", "assessment", "stage-finished"}))
    proof.check(order == ["discovery", "generation", "assessment", "stage-finished"], f"adapter steps in order: {order}")
    proof.check(got.count("generation") >= 3, f"generation line + heartbeats ({got.count('generation') - 1} heartbeats)")
    proof.check(any("Still waiting for stub" in line for line in sdi(lines)), "heartbeat uses the fixed template")
    proof.check(terminal_last(lines), "terminal line is the last event line")
    proof.check(STUB_KEY not in "\n".join(lines), "API key never on stderr")


def case_burst(proof: Proof, work: Path) -> None:
    script = adapter(work, "burst.py", """r = Reporter.from_environment()
r.report("builder-check")
r.report("building", n_started=48)
for k in range(1, 49):
    r.report("image-failed", service_id=f"svc-{k:02d}", image_reason="work-limit")
sys.exit(0)
""")
    proof.say("Scripted adapter writes 48 image-failed lines then exits at once (48 services)")
    rc, lines, _, _ = run_runner(proof, work, script, 60, 48)
    failed = [line for line in sdi(lines) if "image-failed" in line]
    proof.check(len(failed) == 48, f"48 image-failed lines accepted (got {len(failed)})")
    proof.check(failed[-1].endswith("(48/48)") if failed else False, "runtime-computed counter reaches 48/48")
    final = prism(lines)[-1]
    proof.check(len(final.get("image_failures") or []) == 48, "terminal row snapshot lists 48 image_failures")
    proof.check(terminal_last(lines), "terminal line is last")
    proof.check(max(len(line.encode()) for line in lines) < 4096, f"longest line {max(len(line.encode()) for line in lines)} B < 4 KiB")


def case_hostile(proof: Proof, work: Path) -> None:
    script = adapter(work, "hostile.py", """fd = int(os.environ["SDI_PROGRESS_FD"])
def w(data): os.write(fd, data)
line = lambda step, **slots: (json.dumps({"step": step, "slots": slots}) + "\\n").encode()
w(line("builder-check"))                                   # ok
w(line("teleport"))                                        # unknown step
w(line("stage-finished"))                                  # runtime-only step
w(line("image-built", service_id="evil-svc-SECRET"))      # foreign service id
w(line("image-built", service_id="svc-01"))                # ok item
w(line("image-failed", service_id="svc-01", image_reason="build-failed"))  # duplicate item (same family)
w(line("builder-check"))                                   # second report of a non-item step
w(line("building", n_started=2, note="api key sk-LEAK-123"))  # free-text slot
w(line("building", n_started=999))                         # count out of range
w(line("builder-check", x="A" * 10240))                    # 10 KiB line
w(b"\\xff\\xfe\\x00garbage TOKEN-should-not-echo\\n")          # garbage bytes
w(b"[1,2,3]\\n")                                            # wrong shape
w(line("building", n_started=2))                           # ok
w(b'{"step":"image-built","slots":{"service_id":"svc-02"')  # partial last line, no newline
sys.exit(0)
""")
    proof.say("Hostile adapter writes raw bytes to the progress fd (2 services)")
    rc, lines, _, _ = run_runner(proof, work, script, 60, 2)
    text = "\n".join(lines)
    got = steps(lines)
    adapter_steps = [s for s in got if s in {"builder-check", "image-built", "image-failed", "building", "teleport"}]
    proof.check(adapter_steps == ["builder-check", "image-built", "building"], f"only the 3 valid reports accepted: {adapter_steps}")
    notes = [line for line in lines if line.startswith("sdi-integration: dropped")]
    proof.check(len(notes) == 1, "exactly one internal counts-only line")
    for leak in ("SECRET", "sk-LEAK", "TOKEN-should", "teleport", "AAAA", "evil"):
        proof.check(leak not in text, f"rejected text {leak!r} never echoed")
    proof.check(terminal_last(lines), "terminal line is last")


def case_timeout(proof: Proof, work: Path) -> None:
    script = adapter(work, "blocks.py", """import signal
r = Reporter.from_environment()
def late(*_):
    r.report("image-failed", service_id="svc-01", image_reason="work-limit")  # after the runtime closed
    r.report("image-failed", service_id="svc-02", image_reason="work-limit")
    sys.exit(1)
signal.signal(signal.SIGTERM, late)
r.report("builder-check")
r.report("building", n_started=2)
while True:
    time.sleep(3600)
""")
    proof.say("Adapter blocks forever; work limit 4 s; heartbeat 1 s; on SIGTERM it reports two late items")
    rc, lines, stdout, elapsed = run_runner(proof, work, script, 4, 2, heartbeat=1)
    reason = json.loads(stdout.strip().splitlines()[-1])["reason"]
    proof.check(reason == "sdi.stage.deadline-exceeded", f"same failure code as today: {reason}")
    proof.check("work-limit-reached" in steps(lines), "work-limit-reached runtime line")
    proof.check(not any("image-failed" in line for line in sdi(lines)), "late adapter reports (after close) dropped")
    proof.check(terminal_last(lines), "terminal line is last; nothing after it")
    proof.check(steps(lines).count("building") >= 3, "heartbeats repeated 'building' while blocked")


def case_stalled(proof: Proof, work: Path) -> None:
    script = adapter(work, "stalls.py", """r = Reporter.from_environment()
r.report("builder-check")
r.report("building", n_started=12)
for k in range(1, 13):
    r.report("image-built", service_id=f"svc-{k:02d}")
    time.sleep(0.05)
while True:
    time.sleep(3600)
""")
    proof.say("stderr is a 4 KiB pipe whose reader pauses 8 s; adapter reports 14 lines then blocks; work limit 3 s; heartbeat 0.5 s")
    rc, lines, stdout, elapsed = run_runner(proof, work, script, 3, 12, heartbeat=0.5, stall_seconds=8, pipe_bytes=4096)
    reason = json.loads(stdout.strip().splitlines()[-1])["reason"]
    proof.check(reason == "sdi.stage.deadline-exceeded", f"timeout path still taken: {reason}")
    proof.check(len([s for s in steps(lines) if s == "image-built"]) == 12, "all 12 items reached the console")
    proof.check(terminal_last(lines), "terminal line is last for the stage")
    proof.say(f"[demo] NOTE: run took {elapsed:.1f}s for a 3 s limit: the runtime's blocking stderr writes waited for the paused reader")


def case_sigterm(proof: Proof, work: Path) -> None:
    script = adapter(work, "sigterm.py", """(WORK / "adapter.pid").write_text(str(os.getpid()))
r = Reporter.from_environment()
r.report("builder-check")
r.report("building", n_started=12)
for k in range(1, 13):
    r.report("image-built", service_id=f"svc-{k:02d}")
    time.sleep(0.2)
while True:
    time.sleep(3600)
""")
    for label, stall in (("plain stderr", 0.0), ("stalled 4 KiB stderr (reader pauses 6 s), heartbeat 0.1 s to fill it", 6.0)):
        proof.say(f"--- SIGTERM at +3 s, {label}")
        sub = work / ("stall" if stall else "plain")
        sub.mkdir()
        sub_script = sub / script.name
        sub_script.write_text(script.read_text())
        sub_script.chmod(0o755)
        rc, lines, stdout, elapsed = run_runner(
            proof, sub, sub_script, 60, 12, heartbeat=0.1 if stall else 0.5, sigterm_at=3.0,
            stall_seconds=stall, pipe_bytes=4096 if stall else None,
        )
        text = "\n".join(lines)
        proof.check(rc == 143, f"exit 143 (128+SIGTERM) as today: {rc}")
        proof.check(stdout == "", "no envelope on stdout")
        proof.check("Fatal Python error" not in text and "Traceback" not in text, "no Fatal Python error / traceback")
        proof.check("stage-finished" not in text, "no terminal line on cancellation (by design)")
        proof.check(not (sub / "w/attempt").exists(), "attempt root removed")
        pid = int((sub / "adapter.pid").read_text())
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        proof.check(not alive, f"adapter pid {pid} gone")
        tail = [line for line in lines if line.startswith(("[sdi]", "PRISM_EVENT"))]
        proof.say(f"[demo] last event line: {tail[-1][:120] if tail else None}")


def case_descendants(proof: Proof, work: Path) -> None:
    grandchild = """import os, sys, time, json
from pathlib import Path
log = Path(sys.argv[1]); fd = int(sys.argv[2]) if len(sys.argv) > 2 else None
if sys.argv[3:] == ["setsid"]:
    os.setsid()
time.sleep(1.5)
for k in range(4):
    try:
        os.write(fd, (json.dumps({"step": "image-built", "slots": {"service_id": "svc-02"}}) + "\\n").encode())
        log.open("a").write(f"write {k}: ok\\n")
    except OSError as e:
        log.open("a").write(f"write {k}: {type(e).__name__}\\n")
    time.sleep(0.5)
time.sleep(6)
"""
    (work / "grandchild.py").write_text(grandchild)
    variants = {
        "a-close_fds-False": """fd = int(os.environ["SDI_PROGRESS_FD"])
os.write(fd, b'{"step":"builder-check","slots":{}}\\n')
subprocess.Popen([sys.executable, str(WORK.parent / "grandchild.py"), str(WORK / "gc.log"), str(fd)], close_fds=False)
sys.exit(0)
""",
        "b-setsid-keeps-running": """fd = int(os.environ["SDI_PROGRESS_FD"])
os.write(fd, b'{"step":"builder-check","slots":{}}\\n')
p = subprocess.Popen([sys.executable, str(WORK.parent / "grandchild.py"), str(WORK / "gc.log"), str(fd), "setsid"], close_fds=False)
(WORK / "gc.pid").write_text(str(p.pid))
time.sleep(0.3)
sys.exit(0)
""",
        "c-env-var-only": """os.write(int(os.environ["SDI_PROGRESS_FD"]), b'{"step":"builder-check","slots":{}}\\n')
code = '''
import os, sys
from pathlib import Path
f = open(os.devnull)  # likely takes the freed fd number
from sdi_pipeline_integration._progress_channel_prototype import Reporter
env_fd = os.environ.get("SDI_PROGRESS_FD")
r = Reporter.from_environment()
r.report("image-built", service_id="svc-02")
Path(sys.argv[1]).write_text(f"env SDI_PROGRESS_FD={env_fd}, devnull fd={f.fileno()}, reporter fd={r.fd}\\\\n")
'''
subprocess.run([sys.executable, "-c", code, str(WORK / "gc.log")])
sys.exit(0)
""",
    }
    for name, body in variants.items():
        proof.say(f"--- {name}")
        sub = work / name
        sub.mkdir()
        script = adapter(sub, "adapter.py", body)
        rc, lines, stdout, elapsed = run_runner(proof, sub, script, 60, 2)
        time.sleep(3.5 if name.startswith("b") else 0.5)  # let grandchild attempt its writes
        log = (sub / "gc.log").read_text() if (sub / "gc.log").exists() else "(no log: grandchild killed before writing)"
        proof.say(f"[demo] grandchild log: {log.strip()}")
        reason = json.loads(stdout.strip().splitlines()[-1])["reason"]
        proof.say(f"[demo] reason {reason}")
        proof.check(elapsed < 15, f"runtime did not hang ({elapsed:.1f}s)")
        proof.check(not any("image-built" in line for line in sdi(lines)), "no grandchild report reached the console")
        proof.check(terminal_last(lines), "terminal line is last")
        if name.startswith("a"):
            proof.check(reason == "sdi.adapter.descendants-survived", "descendants-survived classification unchanged")
        if name.startswith("b"):
            proof.check(reason == "sdi.adapter.response-absent", "setsid grandchild escapes the group: classification as today (response-absent)")
            proof.check("BrokenPipeError" in log, "grandchild writes after close got EPIPE")
            pid = int((sub / "gc.pid").read_text()) if (sub / "gc.pid").exists() else None
            if pid:
                with __import__("contextlib").suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        if name.startswith("c"):
            proof.check("reporter fd=None" in log, "env var alone gives a no-op reporter")


def case_never(proof: Proof, work: Path) -> None:
    from tests._fixture_inputs import COMMITTED_FILES, SUPPLIED_INPUT_ARGUMENTS, commit_fixture_repository
    del COMMITTED_FILES
    repository, commit = commit_fixture_repository(work)
    argv = [
        str(Path(PYTHON).parent / "sdi-integration"), "execute-stage", "--repository", str(repository),
        "--requested-ref", "refs/heads/main", "--resolved-commit", commit,
        "--run-request-path", "runs/s-04/s-04-tc-03-c-05-fixture.yaml",
        "--descriptor-path", "deployment/jenkins/adapters/composition-v1.yaml",
        "--attempt-root", str(work / "attempt"), *SUPPLIED_INPUT_ARGUMENTS,
    ]
    proof.say("Fixture composition adapter (never reports) through execute-stage")
    proof.say("$ " + " ".join(argv))
    rc, lines, stdout, _ = run_streaming(proof, argv)
    envelope = json.loads(stdout)
    proof.say(f"[demo] envelope: {envelope['execution_conclusion']} / {envelope['domain_outcome']} / {envelope['reason']['code']}")
    proof.check(rc == 0 and envelope["execution_conclusion"] == "succeeded", "attempt accepted as today")
    proof.check(steps(lines) == ["stage-started", "adapter-running", "checking-output", "stage-finished"], f"runtime lines only: {steps(lines)}")
    probe = subprocess.run(  # noqa: S603
        [PYTHON, "-c", """
import os, tempfile
from sdi_pipeline_integration._progress_channel_prototype import Reporter
os.environ.pop("SDI_PROGRESS_FD", None)
a = Reporter.from_environment(); a.report("builder-check")
f = tempfile.TemporaryFile(); os.environ["SDI_PROGRESS_FD"] = str(f.fileno())
b = Reporter.from_environment(); b.report("builder-check")
os.environ["SDI_PROGRESS_FD"] = "99"
c = Reporter.from_environment(); c.report("builder-check")
print(f"no env -> fd {a.fd}; regular file -> fd {b.fd}, file size {os.fstat(f.fileno()).st_size}; closed fd -> fd {c.fd}")
"""], capture_output=True, text=True, check=False)
    proof.say(f"[demo] kit without a channel: {probe.stdout.strip()} rc={probe.returncode}")
    proof.check(probe.returncode == 0 and probe.stdout.count("fd None") == 3, "Reporter is a no-op without a FIFO fd")


def case_cv(proof: Proof, work: Path) -> None:  # noqa: PLR0915
    import docker

    from sdi_pipeline_integration import _progress_channel_prototype as progress

    client = docker.from_env()
    name = f"gur212-cv-{os.getpid()}"
    script = r"""
mkdir -p /cv
step() { echo "{\"kind\":\"step\",\"id\":\"$1\",\"status\":\"$2\",\"s\":$3}" >> /cv/progress.jsonl; }
sleep 4
echo '{"kind":"setup","status":"passed","s":4}' >> /cv/progress.jsonl
sleep 2; step secure-book scripted 0
sleep 2; step accept-spoken-request passed 2
sleep 1; step resolve-rendezvous scripted 0
sleep 2; step navigate-to-rendezvous passed 3
printf '{"kind":"step","id":"verify-recip' >> /cv/progress.jsonl
sleep 6; printf 'ient-face","status":"passed","s":4}\n' >> /cv/progress.jsonl
sleep 1; step transfer-book scripted 0
sleep 2; step acknowledge-receipt failed 2
echo '{"setup":"passed","note":"toy result"}' > /cv/.result.tmp && mv /cv/.result.tmp /cv/result.json
"""
    container = client.containers.run("busybox:1.37", ["sh", "-c", script], name=name, detach=True,
                                      labels={"gur212": "prototype"})
    proof.say(f"Toy orchestrator container {name} ({container.short_id}) on local docker, ~17 s; return-to-origin never reported (not_reached)")
    adapter_code = f"""
import socket, threading, time, sys, json
import docker
from sdi_pipeline_integration._progress_channel_prototype import Reporter
from sdi_pipeline_integration import _cv_run_progress_prototype as cv
t0 = time.monotonic()
log = lambda m: print(f"+{{time.monotonic()-t0:6.2f}}s adapter: {{m}}", file=open({str(work / 'adapter.log')!r}, "a"), flush=True)
r = Reporter.from_environment()
short = docker.APIClient(**docker.utils.kwargs_from_env(), timeout=cv.SHORT_TIMEOUT_SECONDS)
hang = socket.socket(); hang.bind(("127.0.0.1", 0)); hang.listen(8)   # accepts, never answers
hung = docker.APIClient(base_url=f"tcp://127.0.0.1:{{hang.getsockname()[1]}}", timeout=2, version="1.44")
refused = docker.APIClient(base_url="tcp://127.0.0.1:1", timeout=2, version="1.44")
faults = {{3: hung, 5: refused}}
r.report("mission")
exited = cv.follow(short_api=short, container_id={container.id!r}, deadline=time.monotonic() + 60,
                   report=r.report, log=log, fetch_api=lambda n: faults.get(n, short))
r.report("collecting-result")
stream, _ = short.get_archive({container.id!r}, "/cv/result.json")
import tarfile, io
raw = tarfile.open(fileobj=io.BytesIO(b"".join(stream))).extractfile("result.json").read()
log(f"exited={{exited}} result={{raw.decode().strip()}}")
sys.exit(0 if exited and json.loads(raw)["setup"] == "passed" else 1)
"""
    channel = progress.Channel("cv", progress.Context(), limit_seconds=2700)
    channel.heartbeat_seconds = float(os.environ.get("SDI_PROGRESS_HEARTBEAT_SECONDS", "5"))
    started = time.monotonic()
    events: list[str] = []
    original_emit = channel.emitter.emit

    def stamped(step: str, message: str, status: str = "RUNNING") -> None:
        line = f"[sdi] C-01 CV › {step}  {status}  {message}"
        events.append(line)
        proof.say(f"+{time.monotonic() - started:6.2f}s | {line}")
        proof.say(f"         | PRISM_EVENT ... mission_setup={channel.emitter.row.get('mission_setup')} "
                  f"mission_steps={[s['status'] for s in channel.emitter.row.get('mission_steps') or []]}")
        with open(os.devnull, "w") as sink:  # keep the real emitter path exercised
            saved, sys.stderr = sys.stderr, sink
            try:
                original_emit(step, message, status)
            finally:
                sys.stderr = saved

    channel.emitter.emit = stamped  # type: ignore[method-assign]
    try:
        channel.runtime("stage-started")
        channel.runtime("adapter-running")
        process = subprocess.Popen(  # noqa: S603
            [PYTHON, "-c", adapter_code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=channel.child_environment(dict(os.environ)),
            pass_fds=(channel.write_fd,), start_new_session=True,
        )
        channel.after_spawn()
        rc = progress.wait_with_progress(process, channel, 90)
        channel.runtime("checking-output")
        channel.runtime("stage-finished", "SUCCEEDED" if rc == 0 else "FAILED",
                        outcome="succeeded" if rc == 0 else f"adapter exit {rc}")
        channel.abort()
        proof.say("--- adapter-side trace (adapter stderr is DEVNULL in the real runtime; this is a demo log file)")
        for line in (work / "adapter.log").read_text().splitlines():
            proof.say(line)
        received = [e for e in events if "mission-step" in e]
        proof.check(rc == 0, "adapter read the result after the loop: outcome unaffected by the failed fetches")
        proof.check(len(received) == 7, f"7 mission-step lines (not_reached omitted), got {len(received)}")
        proof.check(any("Setup passed" in e for e in events), "setup line arrived")
        proof.check(any("verify-recipient-face" in e for e in received), "partial line completed and reported once")
        log = (work / "adapter.log").read_text()
        proof.check("poll 1: fetched 0 B" in log, "file absent at first: NotFound treated as empty")
        proof.check("poll 3: fetch failed" in log and "poll 5: fetch failed" in log, "hung (ReadTimeout) and refused fetches ignored")
        proof.check("partial line pending" in log, "partial line held until complete")
        proof.check("final fetch" in log, "final fetch after exit")
        proof.check(events[-1].split(" › ")[1].startswith("stage-finished"), "terminal line last")
    finally:
        container.remove(force=True)
        left = client.containers.list(all=True, filters={"label": "gur212=prototype"})
        proof.say(f"[demo] containers left with label gur212=prototype: {len(left)}")


CASES = {
    "1-composition": case_composition,
    "2-burst": case_burst,
    "3-hostile": case_hostile,
    "4-timeout": case_timeout,
    "5-stalled-stderr": case_stalled,
    "6-sigterm": case_sigterm,
    "7-descendants": case_descendants,
    "8-never-reports": case_never,
    "9-cv-hop": case_cv,
}


def main() -> int:
    proof_dir = Path(sys.argv[1])
    proof_dir.mkdir(parents=True, exist_ok=True)
    selected = sys.argv[2:] or list(CASES)
    failed = []
    for name in selected:
        proof = Proof(proof_dir / f"{name}.txt")
        proof.say(f"=== GUR-212 prototype case {name}  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
        with tempfile.TemporaryDirectory(prefix=f"gur212-{name}-") as tmp:
            try:
                CASES[name](proof, Path(tmp))
            except Exception as error:  # noqa: BLE001
                proof.check(False, f"case raised {error!r}")  # noqa: FBT003
        proof.save()
        if proof.failed:
            failed.append(name)
    print(f"failed cases: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
