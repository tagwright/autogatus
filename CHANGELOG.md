# Changelog

## Unreleased

A round of naming and behavior cleanup before the label and env interface freezes at 1.0.

- Removed the `gatus.<id>.alert` bool. Use `gatus.<id>.alerts` instead. With no label you still get a single `custom` alert, and `alerts=none` silences it. This is a hard removal, there is no alias.
- `autogatus.check.<id>.interval` now sets how often a check actually runs, defaulting to the resync interval. Before, it only fed the Gatus heartbeat and checks ran every cycle. The endpoint heartbeat is now derived from the run interval, large enough that one skipped cycle does not false-alarm.
- Renamed the env var `AUTOGATUS_EXEC_CHECKS` to `AUTOGATUS_ENABLE_EXEC`. The old name still works through beta and logs a deprecation warning.
- Renamed the label `autogatus.check.<id>.description` to `autogatus.check.<id>.alert-description`, matching the gatus side. The old key still works through beta.
- A container's `autogatus.alerts` now cascades to its `autogatus.check.<id>` checks that do not set their own `.alerts`. Set it once and it covers the container and its checks.
- Added `autogatus.enable=false` to opt one container out of tier-2 auto monitoring, the per-container version of `AUTOGATUS_EXCLUDE`. Any `autogatus.check.<id>.*` checks on that container still run.
- Alerts now accept the full Gatus alert shape. Alongside the `alerts=custom,ntfy` shorthand there is a structured form, `alerts.<n>.type` with `failure-threshold`, `success-threshold`, `send-on-resolved`, and `description`, at all three alert sites. The structured form wins when its keys are present, and the shorthand still expands to a default alert per type. The cascade carries whole alert objects, not just the provider names.
- Every time value takes a Go duration string now (`15s`, `5m`, `1h30m`), matching Gatus. `AUTOGATUS_RESYNC_INTERVAL` and `AUTOGATUS_PUSH_TIMEOUT` moved off bare seconds. A bare number is still read as seconds.

Runtime robustness:

- A Docker listing failure no longer wipes the config. Before, a daemon restart or hiccup made autogatus write an empty file, so Gatus dropped every endpoint and its heartbeat alerts. Now a failed listing skips the cycle and keeps the last-good config. Container listings also pass `ignore_removed`, so a container removed mid-cycle no longer raises.
- Monitor state now survives a Docker reconnect. The store, monitor, and their history are built once, and a reconnect swaps only the client, so a currently-stopped container is not reclassified as never-seen and dropped from the dashboard. The old client is closed on reconnect.
- Clean shutdown works. The loop waits on an event instead of sleeping, so SIGTERM stops it at once rather than waiting out the resync interval and being killed.
- Tier-1 labeled endpoints are kept while the container exists but is stopped, so Gatus fails the probe and alerts, instead of the endpoint quietly vanishing. It drops only when the container is removed.
- The generated file and the push token are written with `fsync`, so a hard power cut cannot leave them truncated or empty.
- A sub-second resync interval is floored at one second instead of busy-looping.

Alerts and checks correctness:

- `minimum-reminder-interval` on a structured alert is now a Gatus duration string, not an int. Before, `10m` was dropped and `600` decoded as 600 nanoseconds.
- A check that inherits its container's `autogatus.alerts` now keeps its own `alert-description`, so the alert names the failing check rather than the container. The inherited providers and thresholds are unchanged, and the container's own declaration is no longer aliased.
- An unconfigured-provider warning fires once per endpoint and provider instead of every cycle, and clears if the provider is later configured.
- A transient failure reading Gatus's config no longer strips every alert. The last good allowlist is cached and reused (with a single warning) rather than falling back and rewriting the config into a reload loop.
- A wedged exec check is not relaunched while its previous run is still going, so a hung command no longer stacks orphan processes in the target container.
- Two group/name inputs that sanitize to the same Gatus key now log a warning instead of silently overwriting each other.

Features and per-container tuning:

- Added `autogatus.mem-threshold` and `autogatus.cpu-threshold` labels to override the global thresholds for one container. A percent, or `none` to disable that threshold for the container.
- Added `autogatus.snooze=<duration>` as a startup grace. While a container's uptime is under the window, its alerts and its checks' alerts are suppressed, so a deploy or restart does not page. Status is still pushed.
- Added `AUTOGATUS_FAILURE_LATCH` (default 3). A restart bump or an OOM kill is a point-in-time event that recovers after one cycle, so on its own it never crossed Gatus's default failure-threshold. autogatus now holds it failing for that many cycles so the alert fires.
- `pyproject.toml` now lists `flask`, `waitress`, and `requests`, which the code imports. A `pip install .` with monitoring on no longer crashes at startup.
- Marked the deprecated aliases as removed at 1.0 in the docs, and the `autogatus.check.<id>.description` label alias now logs a one-time deprecation warning like the env alias already did.

## v00.01.00b1 (beta)

First tagged release of autogatus. It has been running against a homelab of around ninety containers for a while, but this is an early build and the label and env names are not frozen yet.

What it does:

- Reads `gatus.*` labels off your containers and writes a Gatus config file, so a labeled container shows up on the dashboard and a deleted one drops off.
- Monitors every container by reading Docker state and stats, then pushes the result to Gatus as external endpoints grouped by stack. That picks up the workers, crons, and sidecars that have nothing to probe.
- Serves a read-only detail view at `/details`, styled from Gatus's own stylesheet, with search and filtering and sorting by name, group, memory, CPU, or health.
- Routes each endpoint's alerts to the Gatus providers you already have configured, chosen by label, with an allowlist built from Gatus's own config so a typo cannot fire into the void.
- Runs exec and tcp checks against headless or network-isolated containers and pushes the verdict, for the things Gatus cannot reach itself.
- Logs in plain text or json, with a redacted push token and per-request access logs for the detail view.

Licensed under Apache-2.0.

Beta note: this is a `b1` release. The feature set is complete and covered by tests, but label names and environment variables may still change before 1.0, so pin the image tag if you build on it.
