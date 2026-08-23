# Changelog

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
