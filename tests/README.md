# autogatus tests

Run the suite (matches the container pattern used in development):

    docker run --rm -v "$PWD":/app -w /app python:3.12-slim sh -c \
      "pip install -q -r requirements.txt -r requirements-dev.txt && python -m pytest -q"

Coverage:

    python -m pytest --cov=autogatus --cov-report=term-missing

Coverage config is in `.coveragerc`. `__main__.py` (process wiring) and
`__init__.py` are omitted from the percentage since they are glue, not logic.

## Gate-proofs

Each guard / fail-closed behavior has an explicit negative test that proves it
catches a violation. Cross-module ones are aggregated in `test_negative.py`;
the rest live beside their module.

| Gate | Proving test |
|------|--------------|
| Unknown/unconfigured alert provider dropped + warned | `test_alerts.py::test_filter_alerts_drops_unconfigured` |
| Alert `none`/empty disables alerting | `test_alerts.py::test_alerts_none_disables`, `test_monitor_alerts.py::test_label_none_disables` |
| Alert description strips `"` and `\` | `test_alerts.py::test_build_alerts_sanitizes`, `test_sanitize_description_strips_quote_and_backslash` |
| Gatus-config allowlist: missing path handled | `test_alerts.py::test_configured_providers_missing_path` |
| exec checks ignored when `AUTOGATUS_EXEC_CHECKS` off | `test_checks.py::test_exec_gated_off_by_default` |
| Malformed / missing check target skipped | `test_checks.py::test_tcp_malformed_skipped`, `test_check_missing_target_skipped` |
| Non-zero exec exit = down | `test_checks.py::test_evaluate_exec_failure` |
| Closed TCP port = down | `test_checks.py::test_evaluate_tcp_failure_on_closed_port` |
| Endpoint label missing url is not fatal (skipped) | `test_negative.py::test_gather_endpoints_skips_malformed_container` |
| One-shot exited container not monitored | `test_negative.py::test_monitor_skips_one_shot_exited_container` |
| Seen-then-exited container still reported | `test_negative.py::test_monitor_keeps_container_seen_then_exited` |
| Removed container pruned from store | `test_negative.py::test_monitor_prune_drops_removed_from_store`, `test_store.py::test_prune_drops_absent_keys` |
| push 404 (not registered) handled | `test_push.py::test_push_404_not_registered_returns_false` |
| push 401 (bad token) handled | `test_push.py::test_push_401_bad_token_returns_false` |
| push connection error handled | `test_push.py::test_push_connection_exception_returns_false` |
| stack map missing/garbage falls back | `test_stackmap.py::test_missing_file_falls_back_to_empty`, `test_garbage_content_falls_back_to_empty` |
| Partial stats payload returns None | `test_stats.py::test_cpu_percent_missing_fields`, `test_memory_missing` |
| `autogatus.offline`: a non-truthy value never silences a container (guard) | `test_offline_guards.py::test_guard_invalid_never_silences_shipped`, negative fixture `test_guard_invalid_never_silences_catches_any_nonempty_parser` |
| `autogatus.offline`: a container that has run, or tried to, is never parked (guard) | `test_offline_guards.py::test_guard_ran_is_never_parked_shipped`, negative fixture `test_guard_ran_is_never_parked_catches_quiet_when_not_live` |
| `autogatus.offline`: a declared container is declared on a fresh monitor's first cycle, in every class (guard) | `test_offline_guards.py::test_guard_declared_always_declared_shipped`, negative fixture `test_guard_declared_always_declared_catches_todays_gate` |
| `autogatus.offline`: `enabled: false` only on a parked container's liveness and check endpoints, never tier-1 (guard) | `test_offline_guards.py::test_guard_disabled_only_when_parked_shipped`, negative fixture `test_guard_disabled_only_when_parked_catches_disable_all_declared` |
| `autogatus.offline`: no key pushed in a cycle is declared `enabled: false` in that cycle's file (guard) | `test_offline_guards.py::test_guard_frozen_never_pushed_shipped`, negative fixture `test_guard_frozen_never_pushed_catches_pushes_parked` |
| F1: a Docker listing failure leaves the last file untouched, frozen endpoint and all, and warns | `test_offline_wiring.py::test_list_failure_keeps_frozen_endpoint` |
| F2: the cycle summary counts no push or failure for a frozen key, and a failed drift push counts as failed | `test_offline_wiring.py::test_cycle_summary_skips_frozen_and_counts_drift_failure` |

The five guards' negative fixtures are committed known-bad variants in
`tests/offline_negative.py`. Each guard runs on the shipped code and passes, and
on its fixture and must go red.

## The offline label, by level

How the `autogatus.offline` paths are covered, labeled per the tagwright Testing
Standard.

| Level | What | Status |
|-------|------|--------|
| 1, pure | `autogatus/offline.py`: value grammar, the parked/live/ran classifier over every Docker status times StartedAt times `State.Error`, the verdict texts (`tests/test_offline.py`) | proven |
| 2, wiring | Through `__main__._tick` with a real `ContainerMonitor`, a real `Writer` on a temp path, a scripted fake Docker client and a recording pusher (`tests/fakes.py`, `tests/test_offline_wiring.py`). An autogatus restart is simulated as `run()` starts up, with a new monitor and writer. The listing and the push each have a fault knob and a surfacing test (F1, F2). The no-label output is checked against a golden recorded from the code before the label existed (`tests/golden/no_label_fleet.json`) | proven |
| 3, guards | The five guards above, each with its committed negative fixture (`tests/test_offline_guards.py`) | proven |
| Live harness | H1 to H11 against real Gatus v5.36.0 in a throwaway dind (`test/integration/`) | operator-run, last run at sha PENDING on date PENDING |

Not driven at Level 2: `run()`'s reconnect loop. The tests call `_tick` directly,
so a Docker reconnect (which keeps the monitor and swaps only the client) is not
exercised here.

Premises the build relied on, and where each stands:

- How the Gatus UI draws a frozen row (stale last result, aged, or greyed, and
  its uptime figures with no new results): untested. The harness checks results
  and keys through Gatus's API, not the rendering.
- `docker compose up --no-start --force-recreate` on a running labeled container
  parks it: operator-run, harness scenario H4.
- docker-py's `containers.list(all=True)` without `sparse` inspects each
  container, so `attrs["State"]` carries `StartedAt` and `Error`: checked in the
  source of docker 7.0.0 (the pin floor) and 7.2.0, where `list` calls `get` per
  container, and operator-run end to end, since every harness scenario depends on
  it.
