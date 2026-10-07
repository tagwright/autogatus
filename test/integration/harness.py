#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Live harness for the autogatus.offline label, scenarios H1 to H11.

Operator-run. Starts one throwaway privileged docker:dind container named
autogatus-itest-dind (labeled autogatus.enable=false, so an autogatus on the
host ignores it) and runs everything inside that daemon, never on the host's:
real Gatus v5.36.0 with sqlite storage and a custom alert provider pointed at a
request-recording sink, the autogatus image built from the commit under test
(git archive HEAD) and started through the shipped examples/docker-compose.yml
with test/integration/compose.itest.yml on top, and target containers named
autogatus-itest-*. Every object lives inside the dind, which is removed with
its volumes at the end and on any failure.

Assertions go through Gatus's API (/api/v1/endpoints/<key>/statuses), the
generated file, autogatus's log, Gatus's log, the dind daemon's debug log and
the sink's request log. Each scenario waits for Gatus's reload after any
autogatus write before it asserts.

Needs python3 (standard library only), git and a docker CLI that may start a
privileged container. Usage:

    python3 test/integration/harness.py [--log PATH] [--no-last-run] [--allow-dirty]

On completion it writes test/integration/LAST-RUN with the sha, date and a
result per scenario, unless --no-last-run is given.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

DIND = "autogatus-itest-dind"
DIND_IMAGE = os.environ.get("AUTOGATUS_ITEST_DIND_IMAGE", "docker:29.8.0-dind")
GATUS_IMAGE = "ghcr.io/twin/gatus:v5.36.0"
BASE_IMAGES = ["python:3.12-slim", "alpine:3.22"]
PROJECT = "autogatus-itest"
TPROJECT = "autogatus-itest-t"
AG = "autogatus-itest-ag"
GATUS = "autogatus-itest-gatus"
GATUS_PORT = 18080
SINK_PORT = 18081
RESYNC = 2
HEARTBEAT = 30

# Compose service names, and the container names they run under.
S0, S1, S2, S3 = ("t0", "t1", "t2", "t3")
T0, T1, T2, T3 = (f"autogatus-itest-{s}" for s in (S0, S1, S2, S3))


def key_of(group: str, name: str) -> str:
    sub = re.compile(r"[^a-zA-Z0-9-]+")
    return f"{sub.sub('-', group)}_{sub.sub('-', name)}"


K0, K1, K2 = (key_of(n, n) for n in (T0, T1, T2))
K3_CHECK = key_of(T3, f"{T3}-alive")
K3_PROBE = key_of(T3, "probe")

DRIFT = "drift: declared offline (autogatus.offline=true) but the container is running"
HINT = (
    "not parked: started since it was created, "
    "re-park with docker compose up --no-start --force-recreate"
)
ZERO = "0001-01-01T00:00:00Z"


class HarnessFailure(AssertionError):
    pass


# --- plumbing ----------------------------------------------------------------

_LOG = None


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {msg}"
    print(line, flush=True)
    if _LOG is not None:
        _LOG.write(line + "\n")
        _LOG.flush()


def check(cond, msg: str) -> None:
    if not cond:
        raise HarnessFailure(msg)
    log(f"  ok: {msg}")


def run(args, *, input=None, check_rc=True, timeout=900) -> subprocess.CompletedProcess:
    r = subprocess.run(
        args,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check_rc and r.returncode != 0:
        raise HarnessFailure(
            f"command failed ({r.returncode}): {' '.join(args[:6])}...\n{r.stdout[-2000:]}"
            f"\n{r.stderr[-2000:]}"
        )
    return r


def dx(*cmd, **kw) -> subprocess.CompletedProcess:
    """Run a command inside the dind container."""
    return run(["docker", "exec", "-i", DIND, *cmd], **kw)


def dd(*args, **kw) -> subprocess.CompletedProcess:
    """Run a docker command against the dind's own daemon."""
    return dx("docker", *args, **kw)


def write_in_dind(path: str, content: str) -> None:
    dx("sh", "-c", f"mkdir -p \"$(dirname '{path}')\" && cat > '{path}'", input=content)


def wait_until(fn, timeout: float, what: str, every: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise HarnessFailure(f"timed out after {timeout:.0f}s waiting for {what}")


def hold(seconds: float, why: str) -> None:
    log(f"  hold {seconds:.0f}s: {why}")
    time.sleep(seconds)


# --- observation -------------------------------------------------------------


def _parse_ts(stamp: str) -> float:
    s = stamp.strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if not m:
        raise ValueError(stamp)
    frac = (m.group(2) or ".0")[:7]
    return dt.datetime.fromisoformat(f"{m.group(1)}{frac}{m.group(3) or '+00:00'}").timestamp()


def container_logs(name: str, since: float) -> list[tuple[float, str]]:
    r = dd("logs", "--timestamps", "--since", f"{since:.3f}", name, check_rc=False)
    out = []
    for line in (r.stdout + r.stderr).splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            out.append((_parse_ts(stamp), rest))
        except ValueError:
            continue
    out.sort(key=lambda t: t[0])
    return out


def dind_daemon_log(since: float) -> list[str]:
    r = run(["docker", "logs", "--since", f"{since:.3f}", DIND], check_rc=False)
    return (r.stdout + r.stderr).splitlines()


def http(path: str, port: int = GATUS_PORT):
    r = dx("wget", "-qO-", f"http://127.0.0.1:{port}{path}", check_rc=False)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout) if r.stdout.strip() else None


def results(key: str):
    """Gatus's results for a key, oldest first, or None if the key is unknown."""
    st = http(f"/api/v1/endpoints/{key}/statuses?page=1&pageSize=5000")
    if st is None:
        return None
    res = st.get("results") or []
    return sorted(res, key=lambda r: _parse_ts(r["timestamp"]))


def listed_keys() -> set[str]:
    sts = http("/api/v1/endpoints/statuses?page=1&pageSize=1") or []
    return {s["key"] for s in sts}


def count_newest(key: str) -> tuple[int, str]:
    res = results(key)
    if res is None:
        raise HarnessFailure(f"{key} is not known to Gatus")
    return len(res), (res[-1]["timestamp"] if res else "")


def generated() -> dict:
    code = (
        "import json,yaml;print(json.dumps(yaml.safe_load(open('/output/autogatus.yaml')) or {}))"
    )
    r = dd("exec", AG, "python", "-c", code)
    return json.loads(r.stdout)


def ext_decl(name: str):
    for d in generated().get("external-endpoints") or []:
        if d["name"] == name:
            return d
    return None


def tier1_decl(group: str, name: str):
    for d in generated().get("endpoints") or []:
        if d["name"] == name and d["group"] == group:
            return d
    return None


def sink_alerts(since: float = 0.0, name: str | None = None, state: str | None = None) -> list:
    alerts = http("/_alerts", port=SINK_PORT) or []
    out = []
    for a in alerts:
        q = a.get("query") or {}
        if a["ts"] < since:
            continue
        if name is not None and q.get("name") != name:
            continue
        if state is not None and q.get("state") != state:
            continue
        out.append(a)
    return out


def inspect_state(name: str) -> dict:
    r = dd("inspect", name, "--format", "{{json .}}")
    return json.loads(r.stdout)


def wait_write(since: float, timeout: float = 30) -> float:
    """Wait for autogatus to write the generated file after ``since``."""

    def found():
        for ts, line in container_logs(AG, since):
            if " wrote " in line and "autogatus.yaml" in line:
                return ts
        return None

    ts = wait_until(found, timeout, "autogatus to write the generated file")
    log(f"  autogatus wrote the file at +{ts - since:.1f}s")
    return ts


def last_write(since: float) -> float:
    writes = [
        ts
        for ts, line in container_logs(AG, since)
        if " wrote " in line and "autogatus.yaml" in line
    ]
    if not writes:
        raise HarnessFailure("autogatus logged no write")
    return writes[-1]


def wait_decl(fetch, pred, since: float, what: str, timeout: float = 30):
    """Poll the generated file until ``fetch()`` returns a declaration that
    satisfies ``pred``. Returns ``(time of the write that produced it, decl)``.
    Polling, rather than reading the first write after an action, rides out the
    one-cycle phantom a compose recreate can cause."""

    def ok():
        d = fetch()
        return d if (d is not None and pred(d)) else None

    d = wait_until(ok, timeout, what)
    t_write = last_write(since)
    log(f"  file shows {what} (write at +{t_write - since:.1f}s)")
    return t_write, d


def frozen(d) -> bool:
    return d.get("enabled") is False and "alerts" not in d


def enabled_with_alerts(d) -> bool:
    return "enabled" not in d and bool(d.get("alerts"))


def wait_reload(after: float, timeout: float = 80) -> float:
    """Wait for Gatus to notice a file change made after ``after`` and finish
    reloading it. Returns the time the reload finished."""

    def found():
        modified = None
        for ts, line in container_logs(GATUS, after - 1):
            if modified is None and ts >= after and "Configuration file has been modified" in line:
                modified = ts
            elif modified is not None and "Total endpoint keys to preserve" in line:
                return ts
        return None

    ts = wait_until(found, timeout, "Gatus to reload its config")
    log(f"  Gatus reloaded at +{ts - after:.1f}s after the write")
    return ts


def gatus_lines(since: float, needle: str) -> list[str]:
    return [line for ts, line in container_logs(GATUS, since) if needle in line]


def gatus_push_lines(since: float, key: str) -> list[str]:
    return gatus_lines(since, f"external endpoint with key={key} and success=")


def ag_push_lines(since: float, key: str) -> list[str]:
    """autogatus's own log of push attempts for a key: failures, HTTP errors and
    not-registered retries (logged at DEBUG, which the harness runs at)."""
    pats = (f"push failed for {key}:", f"push {key} -> HTTP", f"endpoint {key} not registered")
    return [line for _, line in container_logs(AG, since) if any(p in line for p in pats)]


# --- targets -----------------------------------------------------------------


def write_targets(t1_off=False, t2_off=True, t3_off=False) -> None:
    def svc(name, labels, command):
        lab = "".join(f'      {k}: "{v}"\n' for k, v in labels.items())
        return (
            f"  {name.rsplit('-', 1)[1]}:\n"
            f"    image: alpine:3.22\n"
            f"    pull_policy: never\n"
            f"    container_name: {name}\n"
            f"    network_mode: none\n"
            f"    restart: unless-stopped\n"
            f"    command: {json.dumps(command)}\n" + (f"    labels:\n{lab}" if labels else "")
        )

    off = {"autogatus.offline": "true"}
    t3_labels = {
        "autogatus.check.alive.exec": "true",
        "gatus.enable": "true",
        "gatus.probe.url": "tcp://127.0.0.1:9",
        "gatus.probe.interval": "5s",
        "gatus.probe.alerts": "custom",
    }
    doc = (
        "services:\n"
        + svc(T0, {"autogatus.alerts": "none"}, ["sleep", "infinity"])
        + svc(T1, off if t1_off else {}, ["sleep", "infinity"])
        + svc(T2, off if t2_off else {}, ["/nonexistent"])
        + svc(T3, {**t3_labels, **(off if t3_off else {})}, ["sleep", "infinity"])
    )
    write_in_dind("/work/targets.yml", doc)


def tup(*args, check_rc=True) -> subprocess.CompletedProcess:
    return dd("compose", "-p", TPROJECT, "-f", "/work/targets.yml", *args, check_rc=check_rc)


# --- setup and teardown --------------------------------------------------------


def cleanup() -> None:
    r = run(["docker", "rm", "-f", "-v", DIND], check_rc=False)
    if r.returncode == 0:
        log(f"removed {DIND} and its volumes")


def setup(sha: str, token: str) -> None:
    cleanup()
    log(f"starting {DIND} ({DIND_IMAGE}), unix socket only, tmpfs store, daemon debug log on")
    run(
        [
            "docker",
            "run",
            "-d",
            "--privileged",
            "--name",
            DIND,
            "--label",
            "autogatus.enable=false",
            # The dind's image and container store lives in memory, so a run is
            # not held up by slow disks and leaves nothing on them.
            "--tmpfs",
            "/var/lib/docker:rw,exec,size=6g",
            DIND_IMAGE,
            "dockerd",
            "--host=unix:///var/run/docker.sock",
            "--debug",
        ]
    )
    wait_until(lambda: dd("info", check_rc=False).returncode == 0, 90, "the dind daemon")
    log(f"dind engine {dd('version', '--format', '{{.Server.Version}}').stdout.strip()}")
    log(f"dind compose {dd('compose', 'version', '--short').stdout.strip()}")

    for image in BASE_IMAGES:
        have = run(["docker", "image", "inspect", image], check_rc=False).returncode == 0
        if have:
            saved = subprocess.run(["docker", "save", image], capture_output=True, check=True)
            subprocess.run(
                ["docker", "exec", "-i", DIND, "docker", "load"],
                input=saved.stdout,
                capture_output=True,
                check=True,
            )
            log(f"loaded {image} from the host")
        else:
            dd("pull", "-q", image)
            log(f"pulled {image}")
    dd("pull", "-q", GATUS_IMAGE)
    log(f"pulled {GATUS_IMAGE}")

    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", sha],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-i", DIND, "sh", "-c", "mkdir -p /src && tar -x -C /src"],
        input=archive.stdout,
        capture_output=True,
        check=True,
    )
    log(f"shipped git archive {sha[:12]} to /src")
    # The harness's own assets come from beside this script, so they always
    # match the driver. On an attested run the tree is clean, so they are also
    # the committed ones.
    assets = subprocess.run(
        ["tar", "-C", str(HERE), "-cf", "-", "gatus-config.yaml", "compose.itest.yml", "sink.py"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            DIND,
            "sh",
            "-c",
            "mkdir -p /src/test/integration && tar -x -C /src/test/integration",
        ],
        input=assets.stdout,
        capture_output=True,
        check=True,
    )
    tag = sha[:12]
    dd("build", "-q", "-t", f"autogatus-itest-ag:{tag}", "/src", timeout=1200)
    log(f"built autogatus-itest-ag:{tag} from the repo Dockerfile")

    dx(
        "sh",
        "-c",
        # The example compose mounts the config dir read-only and the generated
        # volume inside it, so the generated/ mountpoint has to exist already.
        "mkdir -p /work/gatus/generated && cp /src/test/integration/gatus-config.yaml /work/gatus/",
    )
    write_in_dind("/work/itest.env", f"ITEST_TAG={tag}\nITEST_TOKEN={token}\n")
    dx("chmod", "600", "/work/itest.env")
    dx(
        "sh",
        "-c",
        f"cd /src/examples && docker compose -p {PROJECT} --env-file /work/itest.env "
        "-f docker-compose.yml -f /src/test/integration/compose.itest.yml "
        "up -d gatus sink autogatus",
    )
    wait_until(lambda: http("/health") is not None, 90, "Gatus to answer /health")
    log("Gatus, sink and autogatus are up (shipped example compose plus override)")


# --- scenarios ------------------------------------------------------------------


class Run:
    def __init__(self):
        self.t1_seen: set[str] = set()  # every result timestamp recorded for K1

    def remember(self) -> None:
        for r in results(K1) or []:
            self.t1_seen.add(r["timestamp"])


def _new_results(key: str, after: float) -> list:
    return [r for r in results(key) or [] if _parse_ts(r["timestamp"]) > after]


def _first_error(r) -> str:
    return (r.get("errors") or [""])[0]


def h1(ctx: Run) -> None:
    write_targets()
    t = time.time()
    tup("up", "-d", S0, S1)
    t_write, _ = wait_decl(lambda: ext_decl(T1), enabled_with_alerts, t, "t1 declared")
    wait_reload(t_write)
    wait_until(lambda: results(K1), 30, f"results for {K1}")
    hold(5 * RESYNC, "five unlabeled cycles")
    n0, newest0 = count_newest(K1)
    log(f"  unlabeled: {n0} results, newest {newest0}")
    check(n0 >= 3, f"{K1} gathered results while unlabeled ({n0})")

    write_targets(t1_off=True)
    t_park = time.time()
    tup("up", "--no-start", S1)
    st = inspect_state(T1)
    check(st["State"]["Status"] == "created", "up --no-start left the container created")
    check(st["State"]["StartedAt"].startswith("0001-01-01"), "StartedAt is the zero time")
    check(st["Config"]["Labels"].get("autogatus.offline") == "true", "the label is on it")
    t_write, d = wait_decl(lambda: ext_decl(T1), frozen, t_park, "t1 frozen")
    check(d.get("enabled") is False, "file declares the key enabled: false")
    check("alerts" not in d, "file declares the key with no alerts")
    t_reload = wait_reload(t_write)
    hold(3, "settle after the reload")
    n1, newest1 = count_newest(K1)
    ctx.remember()
    log(f"  frozen reference after the reload: {n1} results, newest {newest1}")
    hold(10 * RESYNC + HEARTBEAT + 5, "ten cycles plus one full heartbeat interval")
    n2, newest2 = count_newest(K1)
    check(K1 in listed_keys(), f"{K1} is still listed by Gatus")
    check((n2, newest2) == (n1, newest1), f"count and newest unchanged ({n1} -> {n2})")
    check(n1 >= n0, "no result was lost by parking")
    check(gatus_push_lines(t_write + 0.5, K1) == [], f"Gatus received no push for {K1}")
    check(ag_push_lines(t_write, K1) == [], f"autogatus logged no push attempt for {K1}")
    beats_t1 = gatus_lines(t_reload + 1, f"Checked heartbeat for group={T1}; endpoint={T1}")
    beats_t0 = gatus_lines(t_reload + 1, f"Checked heartbeat for group={T0}; endpoint={T0}")
    check(beats_t0 != [], "control: Gatus ran heartbeat checks on the enabled t0")
    check(beats_t1 == [], "Gatus ran no heartbeat check on the frozen key")
    check(sink_alerts() == [], "the sink is empty")


def h2(ctx: Run) -> None:
    # Control: t0 is unlabeled and stopped, so a restarted autogatus has never
    # seen it running and drops it. Gatus must log that deletion, which proves
    # the absence check below would catch the parked key being dropped.
    tup("stop", S0)
    hold(3 * RESYNC, "autogatus sees t0 stopped")
    n1, newest1 = count_newest(K1)
    t = time.time()
    dd("restart", AG)
    t_write = wait_write(t)
    wait_reload(t_write)
    hold(3, "settle after the reload")
    deletions = gatus_lines(t, "Deleting endpoints with keys")
    log(f"  deletion lines: {[x.strip()[:200] for x in deletions]}")
    check(any(K0 in line for line in deletions), "control: Gatus deleted the never-seen t0")
    check(not any(K1 in line for line in deletions), f"Gatus did not delete {K1}")
    check(K1 in listed_keys(), f"{K1} still listed after the autogatus restart")
    check(count_newest(K1) == (n1, newest1), "count and newest unchanged across the restart")
    check(frozen(ext_decl(T1)), "still declared frozen after the restart")
    ctx.remember()


def h3(ctx: Run) -> None:
    _, newest = count_newest(K1)
    t = time.time()
    tup("up", "-d", S1)
    check(inspect_state(T1)["State"]["Status"] == "running", "up -d started the parked container")
    t_write, _ = wait_decl(lambda: ext_decl(T1), enabled_with_alerts, t, "t1 re-enabled")
    wait_reload(t_write)

    def drift_results():
        new = _new_results(K1, _parse_ts(newest))
        return [r for r in new if not r["success"] and _first_error(r).startswith(DRIFT)]

    got = wait_until(drift_results, 30, "failing results with the drift text")
    log(f"  drift result: {_first_error(got[-1])[:160]}")
    wait_until(lambda: sink_alerts(t, T1, "TRIGGERED"), 45, "a triggered alert")
    hold(4 * RESYNC, "make sure no second alert follows")
    check(len(sink_alerts(t, T1, "TRIGGERED")) == 1, "the sink got exactly one triggered alert")
    ctx.remember()


def h4(ctx: Run) -> None:
    t = time.time()
    tup("up", "--no-start", "--force-recreate", S1)
    st = inspect_state(T1)
    check(st["State"]["Status"] == "created", "force-recreate on a running container: created")
    check(st["State"]["StartedAt"].startswith("0001-01-01"), "and never started (parked)")
    t_write, _ = wait_decl(lambda: ext_decl(T1), frozen, t, "t1 frozen again")
    wait_reload(t_write)
    hold(3, "settle after the reload")
    n1, newest1 = count_newest(K1)
    hold(10 * RESYNC, "ten cycles")
    check(count_newest(K1) == (n1, newest1), "the row froze again after the reload")
    check(sink_alerts(t, T1, "RESOLVED") == [], "no resolved alert (Gatus dropped the record)")
    ctx.remember()


def h5(ctx: Run) -> None:
    t = time.time()
    tup("up", "-d", S1)
    t_write, _ = wait_decl(lambda: ext_decl(T1), enabled_with_alerts, t, "t1 re-enabled")
    wait_reload(t_write)
    wait_until(lambda: sink_alerts(t, T1, "TRIGGERED"), 45, "the drift alert")
    check(len(sink_alerts(t, T1, "TRIGGERED")) == 1, "one triggered alert on drift")
    write_targets(t1_off=False)
    t2 = time.time()
    tup("up", "-d", S1)
    st = inspect_state(T1)
    check(st["State"]["Status"] == "running", "unlabeled and running")
    check("autogatus.offline" not in st["Config"]["Labels"], "the recreate removed the label")
    wait_until(lambda: sink_alerts(t2, T1, "RESOLVED"), 45, "the resolved alert")
    hold(3 * RESYNC, "make sure no second resolved alert follows")
    check(len(sink_alerts(t2, T1, "RESOLVED")) == 1, "the sink got exactly one resolved alert")
    check([r for r in _new_results(K1, t2) if r["success"]] != [], "healthy results arrive")
    ctx.remember()


def h6(ctx: Run) -> None:
    # Unlabeled running and live declare the same thing, so no write and no
    # reload here: the drift verdict lands on an endpoint that has alerts.
    write_targets(t1_off=True)
    t = time.time()
    tup("up", "-d", S1)
    check(inspect_state(T1)["Config"]["Labels"].get("autogatus.offline") == "true", "labeled")
    wait_until(lambda: sink_alerts(t, T1, "TRIGGERED"), 45, "the drift alert")
    check(len(sink_alerts(t, T1, "TRIGGERED")) == 1, "one triggered alert on drift")
    t_stop = time.time()
    tup("stop", S1)
    check(inspect_state(T1)["State"]["Status"] == "exited", "stopped: exited")

    def hinted():
        new = _new_results(K1, t_stop)
        return [r for r in new if not r["success"] and HINT in " ".join(r.get("errors") or [])]

    got = wait_until(hinted, 20, "failing results with the re-park hint")
    log(f"  ran result: {_first_error(got[-1])[:200]}")
    n_a, _ = count_newest(K1)
    for _ in range(5):
        d = ext_decl(T1)
        check(enabled_with_alerts(d), "ran is never frozen")
        time.sleep(2 * RESYNC)
    n_b, _ = count_newest(K1)
    check(n_b > n_a, f"the row keeps receiving results ({n_a} -> {n_b})")
    ctx.remember()


def h7(ctx: Run) -> None:
    t = time.time()
    tup("up", "--no-start", "--force-recreate", S1)
    check(inspect_state(T1)["State"]["Status"] == "created", "force-recreate on exited: created")
    t_write, _ = wait_decl(lambda: ext_decl(T1), frozen, t, "t1 frozen")
    wait_reload(t_write)
    hold(3, "settle after the reload")
    n1, newest1 = count_newest(K1)
    hold(10 * RESYNC, "ten cycles")
    check(count_newest(K1) == (n1, newest1), "the row froze")
    now = {r["timestamp"] for r in results(K1) or []}
    missing = ctx.t1_seen - now
    check(not missing, f"every one of the {len(ctx.t1_seen)} results recorded since H1 is kept")


def h8(ctx: Run) -> None:
    t = time.time()
    tup("up", "--no-start", S2)
    st = inspect_state(T2)
    check(st["State"]["Status"] == "created" and not st["State"]["Error"], "t2 parked")
    t_write, _ = wait_decl(lambda: ext_decl(T2), frozen, t, "t2 frozen")
    wait_reload(t_write)
    hold(2, "settle")
    t_start = time.time()
    r = tup("up", "-d", S2, check_rc=False)
    log(f"  up -d exit {r.returncode} (a failed start is expected)")
    st = inspect_state(T2)["State"]
    log(f"  state {st['Status']} exit {st['ExitCode']} error {st['Error'][:90]!r}")
    check(st["Status"] == "created" and st["Error"], "the start failed and left State.Error set")
    check(st["StartedAt"].startswith("0001-01-01"), "StartedAt is still the zero time")
    t_write, _ = wait_decl(lambda: ext_decl(T2), enabled_with_alerts, t_start, "t2 re-enabled")
    wait_reload(t_write)

    def hinted():
        res = results(K2) or []
        return [r for r in res if not r["success"] and HINT in " ".join(r.get("errors") or [])]

    got = wait_until(hinted, 30, "a failing result with the hint")
    log(f"  t2 result: {_first_error(got[-1])[:200]}")
    got = wait_until(lambda: sink_alerts(t_start, T2, "TRIGGERED"), 45, "a triggered alert for t2")
    check(len(got) == 1, "the sink got a triggered alert for t2")


def _exec_calls(cid: str, since: float) -> list[str]:
    return [
        line
        for line in dind_daemon_log(since)
        if f"/containers/{cid}/exec" in line and "POST" in line
    ]


H9_NAMES = (T3, f"{T3}-alive", "probe")


def h9(ctx: Run) -> None:
    # Control first: t3 unlabeled and running, so its exec check runs and the
    # dind daemon's debug log shows the exec calls. That proves the channel the
    # parked assertion below reads from.
    write_targets(t1_off=True, t3_off=False)
    t = time.time()
    tup("up", "-d", S3)
    cid = inspect_state(T3)["Id"]
    wait_until(lambda: _exec_calls(cid, t), 30, "exec calls on the running t3 (control)")
    check(True, "control: the daemon log shows autogatus's exec into a running t3")
    t_write, _ = wait_decl(lambda: ext_decl(f"{T3}-alive"), enabled_with_alerts, t, "check")
    wait_reload(t_write)
    wait_until(lambda: results(K3_CHECK), 30, "check results for the running t3")

    write_targets(t1_off=True, t3_off=True)
    t_park = time.time()
    tup("up", "--no-start", S3)
    st = inspect_state(T3)
    cid2 = st["Id"]
    check(st["State"]["Status"] == "created", "t3 parked")
    t_write, _ = wait_decl(lambda: ext_decl(f"{T3}-alive"), frozen, t_park, "check frozen")
    probe = tier1_decl(T3, "probe")
    check(probe is not None and "alerts" not in probe, "tier-1 endpoint has no alerts")
    check("enabled" not in probe, "tier-1 endpoint is not disabled")
    wait_reload(t_write)
    hold(3, "settle after the reload")
    t_ref = time.time()
    c1 = count_newest(K3_CHECK)
    p1, _ = count_newest(K3_PROBE)
    hold(10 * RESYNC + 5, "ten cycles")
    check(count_newest(K3_CHECK) == c1, "the check row is frozen (count and newest unchanged)")
    check(gatus_push_lines(t_ref, K3_CHECK) == [], "no push reached Gatus for the check")
    check(ag_push_lines(t_write, K3_CHECK) == [], "autogatus logged no push attempt for it")
    check(_exec_calls(cid2, t_park) == [], "autogatus ran no exec in the parked t3")
    p2, _ = count_newest(K3_PROBE)
    probe_new = _new_results(K3_PROBE, t_ref)
    check(p2 > p1 and all(not r["success"] for r in probe_new), "tier-1 keeps failing probes")
    quiet = [a for n in H9_NAMES for a in sink_alerts(t_ref, n)]
    check(quiet == [], "the sink is empty for t3 while it is parked")


def h10(ctx: Run) -> None:
    check(frozen(ext_decl(T1)), "t1 is frozen")
    n1, _ = count_newest(K1)
    t = time.time()
    url = (
        f"http://127.0.0.1:{GATUS_PORT}/api/v1/endpoints/{K1}/external"
        "?success=false&error=stray-push-from-the-harness"
    )
    # The token is read from the env file inside the dind, never passed here.
    r = dx(
        "sh",
        "-c",
        ". /work/itest.env && wget -qO- --post-data= "
        f'--header "Authorization: Bearer $ITEST_TOKEN" "{url}"',
        check_rc=False,
    )
    check(r.returncode == 0, "Gatus answered 200 to a push on the frozen key")
    hold(3, "let any alert fire")
    n2, _ = count_newest(K1)
    check(n2 == n1 + 1, f"the count rose by one ({n1} -> {n2})")
    check(sink_alerts(t, T1) == [], "the sink stays empty")


def h11(ctx: Run) -> None:
    check(frozen(ext_decl(T1)), "t1 is parked")
    t = time.time()
    dd("stop", GATUS)
    tup("up", "-d", S1)
    check(inspect_state(T1)["State"]["Status"] == "running", "t1 live while Gatus is down")

    # Wait for t1's own failed push. t2 is still pushing too, so the first
    # failed cycle after Gatus stops may be t2's alone.
    def t1_failed():
        for ts, x in container_logs(AG, t):
            if f"push failed for {K1}:" in x:
                return ts, x
        return None

    t_fail, fail_line = wait_until(t1_failed, 30, "a failed push for t1")
    check("error=drift" in fail_line, "autogatus logged the failed drift push for t1")

    def failed_summary():
        for ts, line in container_logs(AG, t_fail - 1):
            m = re.search(r"reconcile: (\d+) monitored, (\d+) pushed, (\d+) failed", line)
            if ts >= t_fail and m and int(m.group(3)) > 0:
                return line
        return None

    line = wait_until(failed_summary, 15, "the cycle summary counting it")
    log(f"  {line.strip()[:160]}")
    check(True, "the cycle summary counts the failed drift push")
    hold(3 * RESYNC, "a few failing cycles")
    t_up = time.time()
    dd("start", GATUS)
    wait_until(lambda: http("/health") is not None, 60, "Gatus back")

    def drift_after():
        return [r for r in _new_results(K1, t_up) if _first_error(r).startswith(DRIFT)]

    wait_until(drift_after, 30, "drift results after Gatus returns")
    check(True, "autogatus recovered and pushes the drift verdict again")
    wait_until(lambda: sink_alerts(t_up, T1, "TRIGGERED"), 60, "the drift alert after recovery")
    check(True, "the drift alert arrived after recovery")


SCENARIOS = [
    ("H1", "park and freeze", h1),
    ("H2", "autogatus restart while parked", h2),
    ("H3", "parked to live", h3),
    ("H4", "live to parked on a running container", h4),
    ("H5", "drift resolved by unlabeling", h5),
    ("H6", "live to ran", h6),
    ("H7", "ran to parked", h7),
    ("H8", "parked to ran by a failed start", h8),
    ("H9", "checks and tier-1 while parked", h9),
    ("H10", "stray push", h10),
    ("H11", "failure path, Gatus down while parked to live", h11),
]


def write_last_run(sha: str, results_by_id: dict, versions: dict) -> None:
    lines = [
        "# Live integration harness attestation (tagwright Testing Standard, task #548).",
        "#",
        "# autogatus's harness is operator-run (CI is GitHub-hosted, with no Docker",
        "# socket), so every scenario below is in the operator-run bucket and is only",
        "# valid for the sha it was produced at. Written by test/integration/harness.py",
        "# at the end of a run, against git archive of that sha.",
        "",
        f"sha: {sha}",
        f"date: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}",
        "harness: test/integration/harness.py",
        f"dind: {versions.get('dind', '?')}",
        f"gatus: {GATUS_IMAGE}",
        "",
        "operator_run:",
    ]
    for sid, name, _ in SCENARIOS:
        lines.append(f"  {sid}: {results_by_id.get(sid, 'not run')}  # {name}")
    (HERE / "LAST-RUN").write_text("\n".join(lines) + "\n")
    log(f"wrote {HERE / 'LAST-RUN'}")


def main() -> int:
    global _LOG
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", default=None, help="log file (default: a temp file)")
    ap.add_argument("--no-last-run", action="store_true", help="do not write LAST-RUN")
    ap.add_argument("--allow-dirty", action="store_true", help="run with uncommitted changes")
    ap.add_argument("--keep", action="store_true", help="leave the dind up (debugging only)")
    args = ap.parse_args()

    log_path = args.log or f"/tmp/autogatus-itest-{int(time.time())}.log"
    _LOG = open(log_path, "w")
    log(f"log: {log_path}")

    sha = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
    dirty = run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"]).stdout
    log(f"sha under test: {sha}")
    if dirty.strip() and not args.allow_dirty:
        log("tracked files have uncommitted changes, refusing (the image builds from HEAD)")
        return 2

    token = secrets.token_urlsafe(24)
    outcome: dict[str, str] = {}
    versions: dict[str, str] = {}
    failed = False
    try:
        setup(sha, token)
        versions["dind"] = (
            f"{DIND_IMAGE} engine "
            + dd("version", "--format", "{{.Server.Version}}").stdout.strip()
            + " compose "
            + dd("compose", "version", "--short").stdout.strip()
        )
        ctx = Run()
        for sid, name, fn in SCENARIOS:
            log(f"=== {sid} {name}")
            started = time.time()
            try:
                fn(ctx)
                outcome[sid] = "pass"
                log(f"=== {sid} pass ({time.time() - started:.0f}s)")
            except Exception as e:
                outcome[sid] = "FAIL"
                failed = True
                log(f"=== {sid} FAIL: {e}")
                log(traceback.format_exc())
                break
    except Exception as e:
        failed = True
        log(f"setup or harness error: {e}")
        log(traceback.format_exc())
    finally:
        if failed:
            try:
                ps = dd("ps", "-a", "--format", "{{.Names}} {{.Status}}", check_rc=False)
                log(f"--- containers in the dind\n{ps.stdout}")
                gen = dd("exec", AG, "cat", "/output/autogatus.yaml", check_rc=False)
                log(f"--- generated file\n{gen.stdout}")
                for name in (AG, GATUS):
                    tail = dd("logs", "--tail", "80", name, check_rc=False)
                    log(f"--- last lines of {name}\n{tail.stdout}{tail.stderr}")
            except Exception as e:
                log(f"could not collect failure state: {e}")
        if args.keep:
            log(f"--keep: leaving {DIND} up, remove it with docker rm -f -v {DIND}")
        else:
            cleanup()
        if not args.no_last_run:
            write_last_run(sha, outcome, versions)
        log(f"result: {'FAIL' if failed else 'pass'} {outcome}")
        _LOG.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
