# autogatus

**Docker-label service discovery for [Gatus](https://github.com/TwiN/gatus). Autokuma, but for Gatus.**

Gatus is a wonderful config-as-code health monitor, but its config is one central
YAML file. autogatus lets you define monitors the Traefik way: as labels on the
containers themselves. It watches the Docker daemon, compiles `gatus.*` labels into
a Gatus endpoint file, and writes it into the Gatus config directory. Gatus
hot-reloads. Add a service, it starts being monitored; remove it, it stops. No
central file to edit.

It does not fork or patch Gatus. It sits beside it and writes a config file Gatus
already knows how to read.

## How it works

```
  containers with gatus.* labels
            │
            ▼
      autogatus  ──reads──►  Docker socket (read-only)
            │
            └──writes──►  /config/generated/autogatus.yaml
                                    │
                                    ▼
                              Gatus (GATUS_CONFIG_PATH=/config, merges + hot-reloads)
```

Point Gatus's `GATUS_CONFIG_PATH` at a **directory**. Your hand-written base config
(storage, alerting, UI) lives there; autogatus drops a generated file beside it.
Gatus merges every `*.yaml` in the directory.

## Quick start

See [`examples/docker-compose.yml`](examples/docker-compose.yml). The short version:

```yaml
autogatus:
  image: ghcr.io/techgaud/autogatus:latest
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
    - gatus-generated:/output          # Gatus mounts the same volume at /config/generated
```

Then label any container you want monitored:

```yaml
labels:
  gatus.enable: "true"
  gatus.web.url: "http://myapp:3000/health"
  gatus.web.conditions.0: "[STATUS] == 200"
```

## Label reference

Discovery is **opt-in**: a container is ignored unless it sets `gatus.enable=true`.

Each endpoint is keyed by an id segment (`<id>`), so one container can expose many.
`gatus.<id>.url` is the only required field.

| Label | Default | Notes |
|-------|---------|-------|
| `gatus.enable` | — | `true` to opt the container in (required) |
| `gatus.<id>.url` | — | **Required.** `http(s)://`, `tcp://host:port`, `icmp://host` |
| `gatus.<id>.name` | `<id>` | Endpoint display name |
| `gatus.<id>.group` | container name | Dashboard group. Overridable; global default via `AUTOGATUS_DEFAULT_GROUP` |
| `gatus.<id>.interval` | `60s` | Check interval |
| `gatus.<id>.conditions.<n>` | by scheme | Indexed. HTTP → `[STATUS] == 200`, TCP/ICMP → `[CONNECTED] == true` |
| `gatus.<id>.method` | — | HTTP method |
| `gatus.<id>.body` | — | HTTP request body |
| `gatus.<id>.headers.<Name>` | — | HTTP header, repeatable |
| `gatus.<id>.alert` | `true` | Legacy on/off; attaches a single `custom` alert |
| `gatus.<id>.alerts` | *(from `.alert`)* | Comma list of Gatus providers, e.g. `custom,ntfy`; wins over `.alert` |
| `gatus.<id>.alert-description` | `<name> is down` | Alert description |

Conditions use [Gatus's condition syntax](https://github.com/TwiN/gatus#conditions)
verbatim, so anything Gatus supports (`[BODY].x`, `[CERTIFICATE_EXPIRATION]`, …) works.

See **Alert routing** below for how providers are chosen and validated.

## Container monitoring (every container, no labels)

Label discovery covers services you probe. But most containers have no useful
HTTP/TCP endpoint (workers, crons, sidecars, daemons), and the only signal that
exists for *all* of them is Docker state. Set `AUTOGATUS_MONITOR_CONTAINERS=true`
and autogatus monitors **every** container by reading Docker directly and
**pushing** to Gatus [external endpoints](https://github.com/TwiN/gatus#external-endpoints).
No compose edits, no recreates.

For each container it evaluates a composite verdict and pushes:

- **success** = running AND not OOMKilled AND healthcheck not failing AND memory
  under threshold AND (optionally) CPU under threshold AND no new restarts
- **error** = a live status line carrying the real numbers, e.g.
  `mem 96.0%>=95%; restarted (2->3) | state=running cpu=12.0% mem=1.9GB/2.0GB(96%) restarts=3 health=unhealthy`
- **duration** = one headline metric graphed over time (`mem_used_mb` by default,
  or `cpu_percent` / `mem_percent`)

Gatus's per-endpoint `heartbeat` is set automatically, so if autogatus itself
stops pushing, every container flips to down — the monitor is monitored.

**Stack grouping.** Because a Compose project can hold many logical stacks, pass a
`{service: stack}` map via `AUTOGATUS_STACK_MAP` (a YAML file) to group endpoints
by stack. Anything unmapped falls back to the container name.

**What is and isn't monitored.** A container is monitored once it has been seen
running, so a crash or stop flips it to failing (with the heartbeat backstop),
while a one-shot that merely exited is ignored. Removed containers drop off.
Exclude noise with `AUTOGATUS_EXCLUDE` (default `autogatus,claude-code`).

**Thresholds.** Memory pressure and crashloops fail by default because they mean
"about to fall over." CPU is reported but not a failure unless you set
`AUTOGATUS_CPU_THRESHOLD` — a busy container is not a broken one.

> This is the ceiling of what Gatus can hold: availability history, one graphed
> number, and a status string. For true CPU/memory/network time-series and
> dashboards, point Prometheus at a metrics exporter instead — the same stats
> autogatus already collects. That exporter is a planned addition; Gatus stays the
> status layer.

## Detail view (Gatus-styled)

With `AUTOGATUS_WEB=true` (default), autogatus serves a read-only detail view of
everything it monitors, styled to match Gatus by loading Gatus's own stylesheet.
Serve it on the same host under `/details` (path-prefix route to autogatus) so it
sits behind the same auth and mirrors Gatus's URLs:

- `/details` — every container, with client-side search, filter (by group / health)
  and sort (name / group / memory / CPU / status); choices persist in localStorage
- `/details/<key>` — one container (mirrors Gatus's `/endpoints/<key>`), with CPU,
  memory, network, block I/O, restarts, health, uptime, and status history

It reuses Gatus's live `/css/app.css`, so the theme tracks Gatus across versions.
A structural redesign of Gatus's own components would need the markup refreshed;
color/theme changes track automatically. Add a link from the Gatus dashboard with
a `ui.buttons` entry pointing at `/details`.

## Alert routing

autogatus never sends notifications itself. It writes the `alerts:` block of each
endpoint, naming Gatus alert providers; **Gatus does the sending**. The routing:

- **Tier 1** (labeled endpoints): `gatus.<id>.alerts=custom,ntfy` picks providers
  per endpoint. The legacy `gatus.<id>.alert=true|false` still works (true -> a
  single `custom` alert). A list wins over the bool; `none` disables.
- **Tier 2** (monitored containers): the global default is `AUTOGATUS_ALERT_TYPES`
  (or `custom` if unset), overridable per container with the label
  `autogatus.alerts=ntfy,custom` (`none` to silence a container).

**The boundary — read this.** A named provider only fires if it is **configured in
Gatus's own `alerting:` section**. autogatus references providers; it does not define
them. To avoid silent dead alerts, autogatus derives the set of *configured* providers
and drops anything else with a warning:

- Point `AUTOGATUS_GATUS_CONFIG` at Gatus's config file or directory (mount it
  read-only). autogatus reads only the KEYS under `alerting:` — never their values, so
  no secrets are touched — and re-reads each cycle, so adding a provider to Gatus starts
  working with no restart.
- If that is unset, `AUTOGATUS_ALERT_TYPES` is used as the allowlist; if that is unset
  too, the allowlist is just `custom`.

A label naming a provider that is not in the allowlist is dropped and logged at WARN,
naming the endpoint and the provider.

## Checks (reach what Gatus cannot)

Gatus probes from its own vantage point, so it cannot check a headless container
(no HTTP endpoint) or one on a network it cannot reach. autogatus already holds the
Docker socket, so it can run the check itself and push the verdict as an external
endpoint. This is the use case in Gatus issue #647 (monitoring things like
`cloudflare-ddns` that have no web server), solved without Gatus needing shell
support. Checks require container monitoring (`AUTOGATUS_MONITOR_CONTAINERS=true`).

Add one or more checks per container with `autogatus.check.<id>.*` labels:

| Label | Meaning |
|-------|---------|
| `autogatus.check.<id>.exec` | Command to run inside the container (via `/bin/sh -c`); exit 0 = healthy |
| `autogatus.check.<id>.tcp` | `host:port` (or just `port`, host defaults to the container name) to TCP-connect |
| `autogatus.check.<id>.name` | Endpoint name (default `<container>-<id>`) |
| `autogatus.check.<id>.group` | Dashboard group (default the container's stack) |
| `autogatus.check.<id>.interval` | Heartbeat interval for the endpoint |
| `autogatus.check.<id>.timeout` | Check timeout in seconds (default 5) |
| `autogatus.check.<id>.description` | Alert description |
| `autogatus.check.<id>.alerts` | Comma list of Gatus providers (same routing as everything else) |

Worked example (issue #647): a headless `cloudflare-ddns` container with no port to
probe, checked by a command run inside it:

```yaml
services:
  cloudflare-ddns:
    image: someuser/cloudflare-ddns
    labels:
      autogatus.check.alive.exec: "pgrep -f ddns"   # up while the process runs
      autogatus.check.alive.alerts: "custom"
```

Or a TCP check against a service on a network autogatus shares:

```yaml
    labels:
      autogatus.check.api.tcp: "8080"
```

**Security (exec).** `exec` runs arbitrary commands inside containers. Note that a
`:ro` Docker socket mount does NOT prevent this: the exec API is available regardless.
So exec is gated twice: the per-container label AND a global opt-in
`AUTOGATUS_EXEC_CHECKS=true` (default off). With it off, `exec` labels are ignored
with a one-time warning; `tcp` checks are unaffected. Only enable it if you trust the
labels on your containers.

**TCP reachability.** A `tcp` check connects from autogatus's own network position, so
autogatus must share a Docker network with the target. `exec` has no such limitation,
since it goes through the Docker API rather than the network.

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `AUTOGATUS_OUTPUT` | `/output/autogatus.yaml` | Where the generated file is written |
| `AUTOGATUS_DEFAULT_GROUP` | *(empty)* | Fallback group; empty means "group by container name" |
| `AUTOGATUS_RESYNC_INTERVAL` | `15` | Seconds between reconciles / event backstop |
| `AUTOGATUS_LOG_LEVEL` | `INFO` | `DEBUG` for per-container detail |
| `AUTOGATUS_LOG_FORMAT` | `text` | `text` (human) or `json` (one object per line, for aggregators) |
| `AUTOGATUS_ACCESS_LOG_LEVEL` | `INFO` | Level for `/details` request logs; `OFF` to suppress |
| `AUTOGATUS_MONITOR_CONTAINERS` | `false` | Enable container monitoring (Tier 2) |
| `AUTOGATUS_GATUS_URL` | `http://gatus:8080` | Gatus base URL for pushes |
| `AUTOGATUS_PUSH_TOKEN` | *(generated)* | Bearer token; auto-generated and persisted if unset |
| `AUTOGATUS_PUSH_TIMEOUT` | `15` | Seconds to wait on a single push before giving up |
| `AUTOGATUS_STACK_MAP` | *(none)* | Path to a `{service: stack}` YAML for grouping |
| `AUTOGATUS_HEADLINE_METRIC` | `mem_used_mb` | Graphed metric: `mem_used_mb`, `mem_percent`, `cpu_percent` |
| `AUTOGATUS_HEARTBEAT_INTERVAL` | `90s` | No push within this -> Gatus marks the container down |
| `AUTOGATUS_MEM_THRESHOLD` | `95` | Fail over this % of memory limit (blank/`none` disables) |
| `AUTOGATUS_CPU_THRESHOLD` | *(disabled)* | Fail over this CPU % if set |
| `AUTOGATUS_EXCLUDE` | `autogatus,claude-code` | Comma-separated name substrings to skip |
| `AUTOGATUS_EXEC_CHECKS` | `false` | Global opt-in for `autogatus.check.<id>.exec` (runs commands in containers) |
| `AUTOGATUS_GATUS_CONFIG` | *(none)* | Path to Gatus's config (file or dir); derives the alert-provider allowlist |
| `AUTOGATUS_ALERT_TYPES` | `custom` | Default alert channels for monitored containers, and allowlist fallback |
| `AUTOGATUS_WEB` | `true` | Serve the `/details` detail view |
| `AUTOGATUS_WEB_PORT` | `8080` | Port for the detail view |

## Design notes

- **Read-only socket.** autogatus only lists containers and reads labels. Mount the
  socket `:ro`.
- **No churn.** The generated file is rewritten only when the meaningful content
  changes (timestamp header excluded from the comparison), so an idle cluster never
  triggers a Gatus reload.
- **Atomic writes.** The file is written to a temp file and renamed, so Gatus never
  reads a half-written config.
- **One bad label doesn't break the world.** A container with a malformed endpoint is
  logged and skipped; the rest still reconcile.

## Logging

One shared `autogatus` logger, two formats via `AUTOGATUS_LOG_FORMAT`:

- **text** (default): `2026-01-01T00:00:00.000+00:00 autogatus INFO message`
- **json**: one JSON object per line with `timestamp`, `level`, `logger`, `message`,
  plus any structured extras (e.g. `http_method`, `http_status`, `duration_ms` on
  access logs). Point it at any log aggregator.

Both honor `AUTOGATUS_LOG_LEVEL`. Third-party libraries stay at WARNING so they do
not drown out autogatus output.

A healthy INFO run is quiet: a per-cycle summary (`reconcile: N monitored, P pushed,
F failed`) is logged only when the generated config changes or a push fails; otherwise
the cycle detail stays at DEBUG. Per-container detail is DEBUG throughout.

The `/details` web view logs each request (method, path, status, duration) through the
same logger and format. Set `AUTOGATUS_ACCESS_LOG_LEVEL=OFF` to silence it.

**Secret hygiene.** The push token is registered as a redacted value, so it is replaced
with `***REDACTED***` in every log line, in either format, even if it reaches a message.

When running under Docker, cap log growth with a `logging:` block on the service
(json-file, `max-size`, `max-file`); the example compose does this.

## Prior art

- [Autokuma](https://github.com/BigBoot/AutoKuma) — the same idea for Uptime Kuma, via
  Kuma's socket API.
- [home-operations/gatus-sidecar](https://github.com/home-operations/gatus-sidecar) —
  the same idea for Gatus but keyed off Kubernetes resources. autogatus is the Docker
  (Compose / plain Docker) counterpart.

## License

MIT
