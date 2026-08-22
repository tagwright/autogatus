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
| `gatus.<id>.alert` | `true` | Attaches a `custom` alert; set `false` to skip |
| `gatus.<id>.alert-description` | `<name> is down` | Alert description |

Conditions use [Gatus's condition syntax](https://github.com/TwiN/gatus#conditions)
verbatim, so anything Gatus supports (`[BODY].x`, `[CERTIFICATE_EXPIRATION]`, …) works.

The `custom` alert this attaches must be configured once in your Gatus base config
(`alerting.custom` with a `default-alert`). autogatus references it; it does not define it.

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `AUTOGATUS_OUTPUT` | `/output/autogatus.yaml` | Where the generated file is written |
| `AUTOGATUS_DEFAULT_GROUP` | *(empty)* | Fallback group; empty means "group by container name" |
| `AUTOGATUS_RESYNC_INTERVAL` | `15` | Seconds between reconciles / event backstop |
| `AUTOGATUS_LOG_LEVEL` | `INFO` | `DEBUG` for per-container detail |

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

## Prior art

- [Autokuma](https://github.com/BigBoot/AutoKuma) — the same idea for Uptime Kuma, via
  Kuma's socket API.
- [home-operations/gatus-sidecar](https://github.com/home-operations/gatus-sidecar) —
  the same idea for Gatus but keyed off Kubernetes resources. autogatus is the Docker
  (Compose / plain Docker) counterpart.

## License

MIT
