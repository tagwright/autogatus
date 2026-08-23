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
