"""Prober-that-pushes: run exec/tcp checks autogatus can reach and push their
verdict to Gatus as external endpoints.

Gatus probes from its own vantage point, so it cannot check a headless container
(no HTTP endpoint) or one on a network it cannot reach. autogatus already holds
the Docker socket, so it can run a command inside a container (exec) or a TCP
connect from its own network position (tcp) and push the result. This satisfies
the use case in Gatus issue #647 without Gatus needing shell support.

Label schema, per container, one external endpoint per check:

    autogatus.check.<id>.exec=<command>     # run in the container; exit 0 = up
    autogatus.check.<id>.tcp=<host:port>    # TCP connect; host defaults to the
                                            # container name, so tcp=<port> works
    autogatus.check.<id>.name=<name>        # default: <container>-<id>
    autogatus.check.<id>.group=<group>      # default: the container's stack
    autogatus.check.<id>.interval=<dur>     # how often the check runs; default
                                            # the resync interval
    autogatus.check.<id>.timeout=<dur>      # check timeout (default 5s)
    autogatus.check.<id>.alert-description=<text>
    autogatus.check.<id>.alerts=<types>     # Gatus providers (shorthand), or the
    autogatus.check.<id>.alerts.0.type=...  #   structured form with per-alert options

exec runs arbitrary commands in containers, so it is gated behind the global
AUTOGATUS_ENABLE_EXEC opt-in as well as the per-container label.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass

from .alerts import parse_alerts
from .duration import parse_duration_seconds
from .health import Verdict

logger = logging.getLogger("autogatus")

PREFIX = "autogatus.check."
DEFAULT_TIMEOUT = 5.0


@dataclass
class Check:
    id: str
    kind: str  # "exec" | "tcp"
    target: str  # command, or "host:port"
    name: str
    group: str
    interval: str
    description: str
    alerts: list | None = None
    timeout: float = DEFAULT_TIMEOUT
    tcp_host: str = ""
    tcp_port: int = 0


def _bucket(labels: dict) -> dict:
    """Group ``autogatus.check.<id>.<field>`` labels by id."""
    out: dict[str, dict] = {}
    for k, v in (labels or {}).items():
        if not k.startswith(PREFIX):
            continue
        rest = k[len(PREFIX) :]
        if "." not in rest:
            continue
        cid, field_name = rest.split(".", 1)
        if cid:
            out.setdefault(cid, {})[field_name] = v
    return out


def _parse_tcp(target: str, container_name: str):
    """Return ``(host, port)`` or ``None`` if the target is malformed."""
    target = (target or "").strip()
    if not target:
        return None
    if ":" in target:
        host, _, port = target.rpartition(":")
        host = host or container_name
    else:
        host, port = container_name, target
    try:
        return host, int(port)
    except ValueError:
        return None


def parse_container_checks(
    labels: dict,
    container_name: str,
    stack: str,
    default_alerts=None,
    default_interval: str = "90s",
    exec_enabled: bool = False,
) -> tuple[list[Check], bool]:
    """Compile a container's ``autogatus.check.*`` labels into Check objects.

    exec checks are dropped (returned in a separate flag via logging) when
    ``exec_enabled`` is False. Malformed checks are skipped with a warning.
    Returns ``(checks, saw_disabled_exec)``.
    """
    checks = []
    saw_disabled_exec = False
    for cid, fields in sorted(_bucket(labels).items()):
        name = (fields.get("name") or f"{container_name}-{cid}").strip()
        group = (fields.get("group") or stack).strip() or stack
        interval = (fields.get("interval") or default_interval).strip() or default_interval
        # `alert-description` matches the gatus.<id>.alert-description name. The
        # older `description` key stays as a deprecated alias through beta.
        description = (
            fields.get("alert-description") or fields.get("description") or f"{name} check"
        ).strip()
        timeout = parse_duration_seconds(fields.get("timeout"), DEFAULT_TIMEOUT)
        # A check's own alerts (shorthand or structured), else it inherits the
        # container's autogatus.alerts, else the global default. `none` on the
        # check keeps it silenced and does not cascade.
        alerts = parse_alerts(
            fields, default_description=description, context=f"check {cid} on {container_name}"
        )
        if alerts is None:
            alerts = list(default_alerts) if default_alerts else []

        if "exec" in fields and str(fields.get("exec")).strip():
            if not exec_enabled:
                saw_disabled_exec = True
                continue
            checks.append(
                Check(
                    id=cid,
                    kind="exec",
                    target=str(fields["exec"]).strip(),
                    name=name,
                    group=group,
                    interval=interval,
                    description=description,
                    alerts=alerts,
                    timeout=timeout,
                )
            )
        elif "tcp" in fields and str(fields.get("tcp")).strip():
            parsed = _parse_tcp(str(fields["tcp"]), container_name)
            if parsed is None:
                logger.warning(
                    "check %s on %s: malformed tcp target %r, skipping",
                    cid,
                    container_name,
                    fields.get("tcp"),
                )
                continue
            host, port = parsed
            checks.append(
                Check(
                    id=cid,
                    kind="tcp",
                    target=f"{host}:{port}",
                    name=name,
                    group=group,
                    interval=interval,
                    description=description,
                    alerts=alerts,
                    timeout=timeout,
                    tcp_host=host,
                    tcp_port=port,
                )
            )
        else:
            logger.warning(
                "check %s on %s: no exec or tcp target, skipping",
                cid,
                container_name,
            )
    return checks, saw_disabled_exec


def _with_timeout(fn, timeout):
    """Run ``fn`` in a daemon thread, returning ``(value, error)``. A run that
    outlives the timeout returns a TimeoutError (the thread is left to finish)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - surfaced as the check error
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, TimeoutError(f"timed out after {timeout}s")
    if "error" in box:
        return None, box["error"]
    return box.get("value"), None


def evaluate_exec(container, command: str, timeout: float):
    """Run ``command`` in ``container`` via /bin/sh. exit 0 = success. The
    container needs a shell for this (most non-scratch images have one)."""

    def _do():
        return container.exec_run(["/bin/sh", "-c", command], demux=False)

    res, err = _with_timeout(_do, timeout)
    if err is not None:
        return False, f"exec error: {err}"
    exit_code = getattr(res, "exit_code", None)
    output = getattr(res, "output", b"") or b""
    if isinstance(output, tuple):  # demux fallback
        output = b"".join(p for p in output if p)
    out = output.decode("utf-8", "replace").strip().replace("\n", " ")
    detail = f"exit={exit_code}"
    if out:
        detail += f" out={out[:200]}"
    return exit_code == 0, detail


def evaluate_tcp(host: str, port: int, timeout: float):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"connected {host}:{port}"
    except Exception as e:  # noqa: BLE001 - surfaced as the check error
        return False, f"tcp {host}:{port}: {e}"


def run_check(check: Check, container) -> Verdict:
    """Execute one check and return a Verdict (no headline metric)."""
    if check.kind == "exec":
        success, detail = evaluate_exec(container, check.target, check.timeout)
    else:
        success, detail = evaluate_tcp(check.tcp_host, check.tcp_port, check.timeout)
    prefix = "" if success else "check failed: "
    return Verdict(success, f"{prefix}{check.kind} {detail}", None)
