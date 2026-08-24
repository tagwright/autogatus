# autogatus

Docker-label service discovery for [Gatus](https://github.com/TwiN/gatus). Label a container and it shows up on your Gatus dashboard. Autokuma, but for Gatus.

Gatus keeps its endpoints in one config file. autogatus lets you put them on the containers instead, the way Traefik does routes. It watches the Docker daemon, turns `gatus.*` labels into a Gatus config file, and writes that file into the directory Gatus already loads from. Gatus hot-reloads it, so a container joins the dashboard when you label it and drops off when you delete it.

Point Gatus's `GATUS_CONFIG_PATH` at a directory instead of a single file. Your hand-written base config (storage, alerting, UI) lives there, and autogatus writes a generated file next to it. Gatus merges every `*.yaml` it finds.

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

## Quick start

Full example in [`examples/docker-compose.yml`](examples/docker-compose.yml). The core of it:

```yaml
autogatus:
  image: ghcr.io/techgaud/autogatus:latest
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
    - gatus-generated:/output          # Gatus mounts the same volume at /config/generated
```

Then label anything you want watched:

```yaml
labels:
  gatus.enable: "true"
  gatus.web.url: "http://myapp:3000/health"
  gatus.web.conditions.0: "[STATUS] == 200"
```

## Labels

A container is ignored unless it sets `gatus.enable=true`. Each endpoint gets an id segment (`<id>`), so one container can expose several. The only required field is `gatus.<id>.url`.

| Label | Default | Notes |
|-------|---------|-------|
| `gatus.enable` | - | `true` to opt the container in (required) |
| `gatus.<id>.url` | - | Required. `http(s)://`, `tcp://host:port`, or `icmp://host` |
| `gatus.<id>.name` | `<id>` | Endpoint display name |
| `gatus.<id>.group` | container name | Dashboard group, overridable, with a global default in `AUTOGATUS_DEFAULT_GROUP` |
| `gatus.<id>.interval` | `60s` | Check interval |
| `gatus.<id>.conditions.<n>` | by scheme | Indexed. HTTP defaults to `[STATUS] == 200`, TCP and ICMP to `[CONNECTED] == true` |
| `gatus.<id>.method` | - | HTTP method |
| `gatus.<id>.body` | - | HTTP request body |
| `gatus.<id>.headers.<Name>` | - | HTTP header, repeatable |
| `gatus.<id>.alerts` | `custom` | Comma list of Gatus providers like `custom,ntfy`. `none` silences it |
| `gatus.<id>.alert-description` | `<name> is down` | Alert description |

Conditions pass through to Gatus untouched, so anything Gatus understands (`[BODY].x`, `[CERTIFICATE_EXPIRATION]`, and the rest) works. Provider selection is covered under Alert routing.

## Monitoring every container

Labels cover the services you can probe. Most containers have nothing to probe, though: workers, cron jobs, sidecars, DNS helpers. The one signal they all share is their Docker state. Set `AUTOGATUS_MONITOR_CONTAINERS=true` and autogatus reads that state for every container and pushes it to Gatus as an [external endpoint](https://github.com/TwiN/gatus#external-endpoints). Nothing to label, no container to recreate.

Each container gets a composite verdict:

- **success**: running, not OOM-killed, healthcheck passing, memory under threshold, no fresh restarts, and CPU under threshold if you set one
- **error**: a line with the actual numbers, for example `mem 96.0%>=95% | state=running cpu=12.0% mem=1.9GB/2.0GB(96%) restarts=3 health=unhealthy`
- **duration**: one number graphed over time, `mem_used_mb` by default, or `cpu_percent` or `mem_percent`

autogatus sets a Gatus heartbeat on each endpoint. If autogatus stops pushing, those endpoints go down, which catches an autogatus outage too.

**Grouping.** A Compose project holds many logical stacks under one project name, so pass a `{service: stack}` map with `AUTOGATUS_STACK_MAP` to group endpoints by stack. Unmapped services fall back to the container name.

**What gets monitored.** autogatus starts watching a container once it has seen it running, so a crash or a stop becomes a failure (the heartbeat backs this up). A one-shot that ran and exited before autogatus ever saw it is left alone, and a removed container drops off. Skip noise globally with `AUTOGATUS_EXCLUDE` (default `autogatus,claude-code`), or opt a single container out with the label `autogatus.enable=false`. Watch the polarity: `gatus.enable=true` opts a container *into* tier-1 probed endpoints, `autogatus.enable=false` opts it *out* of tier-2 auto monitoring. A container you opt out can still declare `autogatus.check.<id>.*` checks, since those are a separate, deliberate opt-in and keep running.

**Thresholds.** Memory pressure and restart loops fail by default, since both mean the container is about to fall over. CPU is reported but never fails on its own unless you set `AUTOGATUS_CPU_THRESHOLD`. A container can run hot for a while without being broken.

Gatus holds three things per endpoint: availability history, one graphed number, and a status string. That is the ceiling here. For real CPU, memory, and network time series with graphs, put Prometheus in front of a metrics exporter fed by the same stats autogatus already collects. That exporter is planned. Gatus stays the status layer.

## Detail view

`AUTOGATUS_WEB=true` (the default) serves a read-only view of everything autogatus monitors, styled from Gatus's own stylesheet. Route it on the same host under `/details` so it lands behind the same auth and its URLs line up with Gatus's.

- `/details`: every container, with client-side search, a filter by group or health, and a sort by name, group, memory, CPU, or status. Your choices persist in localStorage.
- `/details/<key>`: one container, the counterpart to Gatus's `/endpoints/<key>`, showing CPU, memory, network, block I/O, restarts, health, uptime, and recent status history.

It loads Gatus's live `/css/app.css`, so color and theme changes ride along on a Gatus upgrade. A structural redesign of Gatus's components would need the markup refreshed. Link to it from the Gatus dashboard with a `ui.buttons` entry pointing at `/details`.

## Alert routing

autogatus writes the `alerts:` block on each endpoint and names Gatus providers in it. Gatus does the sending.

- **Tier 1** (labeled endpoints): `gatus.<id>.alerts=custom,ntfy` sets the providers for that endpoint. Leave it off and you get a single `custom` alert. `none` turns alerts off.
- **Tier 2** (monitored containers): the default comes from `AUTOGATUS_ALERT_TYPES` (or `custom` if you leave it unset), overridable per container with `autogatus.alerts=ntfy,custom` (`none` silences one container). A container's `autogatus.alerts` also cascades to its `autogatus.check.<id>` checks that do not set their own `.alerts`, so setting it once covers the container and its checks.

A provider only fires if it is configured in Gatus's own `alerting:` section. autogatus names providers, it does not define them, so a label pointing at a provider Gatus has never heard of would be a dead alert. To catch that, autogatus builds an allowlist of the providers Gatus actually has configured and drops anything outside it:

- Set `AUTOGATUS_GATUS_CONFIG` to Gatus's config file or directory, mounted read-only. autogatus reads the keys under `alerting:` and nothing else (no values, so no secrets), and re-reads them each cycle, so a provider you add to Gatus starts working without a restart.
- With that unset, `AUTOGATUS_ALERT_TYPES` becomes the allowlist. With both unset, the allowlist is just `custom`.

A label naming a provider outside the allowlist is dropped and logged at WARN, with the endpoint and provider named.

## Checks

Gatus probes from where it runs, so it cannot see a headless container with no HTTP endpoint, or one on a network it cannot reach. autogatus is already on the Docker socket, so it can run the check itself and push the result as an external endpoint. This is the ask in Gatus issue [#647](https://github.com/TwiN/gatus/issues/647): monitoring something like `cloudflare-ddns` that serves no port, without Gatus needing shell support. Checks need container monitoring on (`AUTOGATUS_MONITOR_CONTAINERS=true`).

Add checks to a container with `autogatus.check.<id>.*` labels:

| Label | Meaning |
|-------|---------|
| `autogatus.check.<id>.exec` | Command to run inside the container (via `/bin/sh -c`), exit 0 means healthy |
| `autogatus.check.<id>.tcp` | `host:port` (or just `port`, host defaults to the container name) to TCP-connect |
| `autogatus.check.<id>.name` | Endpoint name (default `<container>-<id>`) |
| `autogatus.check.<id>.group` | Dashboard group (default the container's stack) |
| `autogatus.check.<id>.interval` | How often the check runs (default the resync interval). The endpoint heartbeat is derived from it |
| `autogatus.check.<id>.timeout` | Check timeout in seconds (default 5) |
| `autogatus.check.<id>.alert-description` | Alert description (old key `description` still works) |
| `autogatus.check.<id>.alerts` | Comma list of Gatus providers, same routing as everything else |

A headless `cloudflare-ddns` container, checked by a command run inside it:

```yaml
services:
  cloudflare-ddns:
    image: someuser/cloudflare-ddns
    labels:
      autogatus.check.alive.exec: "pgrep -f ddns"   # up while the process runs
      autogatus.check.alive.alerts: "custom"
```

A TCP check against a service on a network autogatus shares:

```yaml
    labels:
      autogatus.check.api.tcp: "8080"
```

Exec runs commands inside your containers, and a `:ro` socket mount does not stop it, because the exec API ignores the mount flag. So exec stays off until you turn it on in two places: the per-container label and a global `AUTOGATUS_ENABLE_EXEC=true` (default off). With the global flag off, exec labels are ignored and logged once, while tcp checks keep working. Turn it on only if you trust the labels on your containers.

A tcp check dials from autogatus's own network position, so autogatus has to share a Docker network with the target. Exec has no such constraint, since it goes over the Docker API rather than the network.

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `AUTOGATUS_OUTPUT` | `/output/autogatus.yaml` | Where the generated file is written |
| `AUTOGATUS_DEFAULT_GROUP` | *(empty)* | Fallback group, empty means group by container name |
| `AUTOGATUS_RESYNC_INTERVAL` | `15` | Seconds between reconciles |
| `AUTOGATUS_LOG_LEVEL` | `INFO` | `DEBUG` for per-container detail |
| `AUTOGATUS_LOG_FORMAT` | `text` | `text` (human) or `json` (one object per line, for aggregators) |
| `AUTOGATUS_ACCESS_LOG_LEVEL` | `INFO` | Level for `/details` request logs, `OFF` to suppress |
| `AUTOGATUS_MONITOR_CONTAINERS` | `false` | Enable container monitoring (Tier 2) |
| `AUTOGATUS_GATUS_URL` | `http://gatus:8080` | Gatus base URL for pushes |
| `AUTOGATUS_PUSH_TOKEN` | *(generated)* | Bearer token, auto-generated and persisted if unset |
| `AUTOGATUS_PUSH_TIMEOUT` | `15` | Seconds to wait on a single push before giving up |
| `AUTOGATUS_STACK_MAP` | *(none)* | Path to a `{service: stack}` YAML for grouping |
| `AUTOGATUS_HEADLINE_METRIC` | `mem_used_mb` | Graphed metric: `mem_used_mb`, `mem_percent`, or `cpu_percent` |
| `AUTOGATUS_HEARTBEAT_INTERVAL` | `90s` | No push within this and Gatus marks the container down |
| `AUTOGATUS_MEM_THRESHOLD` | `95` | Fail over this % of memory limit (blank or `none` disables) |
| `AUTOGATUS_CPU_THRESHOLD` | *(disabled)* | Fail over this CPU % if set |
| `AUTOGATUS_EXCLUDE` | `autogatus,claude-code` | Comma-separated name substrings to skip |
| `AUTOGATUS_ENABLE_EXEC` | `false` | Global opt-in for `autogatus.check.<id>.exec` (runs commands in containers). Old name `AUTOGATUS_EXEC_CHECKS` still works |
| `AUTOGATUS_GATUS_CONFIG` | *(none)* | Path to Gatus's config (file or dir), used to build the alert-provider allowlist |
| `AUTOGATUS_ALERT_TYPES` | `custom` | Default alert channels for monitored containers, and the allowlist fallback |
| `AUTOGATUS_WEB` | `true` | Serve the `/details` detail view |
| `AUTOGATUS_WEB_PORT` | `8080` | Port for the detail view |

## Behavior notes

autogatus only lists containers and reads their labels and stats, so mount the socket read-only. The generated file is rewritten only when its meaningful content changes, since the timestamp header is left out of the comparison, so an idle cluster never makes Gatus reload. Writes go to a temp file and are renamed into place, so Gatus never reads a half-written config. A container with a malformed label is logged and skipped, and the rest of the reconcile carries on.

## Logging

One `autogatus` logger, two formats set by `AUTOGATUS_LOG_FORMAT`:

- **text** (default): `2026-01-01T00:00:00.000+00:00 autogatus INFO message`
- **json**: one object per line with `timestamp`, `level`, `logger`, `message`, and any extras (access logs add `http_method`, `http_status`, `duration_ms`). Send it to any aggregator.

Both respect `AUTOGATUS_LOG_LEVEL`, and third-party libraries are held at WARNING so they do not bury autogatus's own output. A healthy run at INFO stays quiet: the per-cycle line (`reconcile: N monitored, P pushed, F failed`) prints only when the config changes or a push fails, and the rest of the cycle detail sits at DEBUG.

The `/details` view logs each request (method, path, status, duration) through the same logger, and `AUTOGATUS_ACCESS_LOG_LEVEL=OFF` silences it. The push token is registered as a redacted value, so it shows up as `***REDACTED***` in every log line even if it reaches a message. Under Docker, cap the log size with a `logging:` block on the service (json-file, `max-size`, `max-file`), which the example compose does.

## Prior art

- [Autokuma](https://github.com/BigBoot/AutoKuma), the same idea for Uptime Kuma, through Kuma's socket API.
- [home-operations/gatus-sidecar](https://github.com/home-operations/gatus-sidecar), the same idea for Gatus but driven by Kubernetes resources. autogatus is the plain-Docker and Compose counterpart.

## License

[Apache-2.0](LICENSE), matching Gatus. Permissive, with a patent grant.
