import os
import tempfile

from autogatus.alerts import (
    build_alerts,
    configured_providers,
    filter_alerts,
    parse_type_list,
    resolve_allowlist,
    sanitize_description,
)


def test_parse_type_list():
    assert parse_type_list(None) is None  # absent -> caller default
    assert parse_type_list("none") == []
    assert parse_type_list("") == []
    assert parse_type_list("false") == []
    assert parse_type_list("custom,ntfy") == ["custom", "ntfy"]
    assert parse_type_list(" custom , ntfy , custom ") == ["custom", "ntfy"]  # trim + dedup


def test_sanitize_description_strips_quote_and_backslash():
    assert sanitize_description('a"b\\c') == "abc"
    assert sanitize_description(None) == ""


def test_configured_providers_single_file():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("alerting:\n  custom:\n    url: x\n  ntfy:\n    url: y\nendpoints: []\n")
        path = f.name
    try:
        assert configured_providers(path) == {"custom", "ntfy"}
    finally:
        os.unlink(path)


def test_configured_providers_directory_union_and_keys_only():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "00-base.yaml"), "w") as f:
        f.write("alerting:\n  custom:\n    url: SECRET_URL\n")
    with open(os.path.join(d, "10-more.yml"), "w") as f:
        f.write("alerting:\n  slack:\n    webhook-url: SECRET\n")
    providers = configured_providers(d)
    assert providers == {"custom", "slack"}


def test_configured_providers_missing_path():
    assert configured_providers("") == set()
    assert configured_providers("/no/such/path") == set()


def test_configured_providers_skips_basename():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "autogatus.yaml"), "w") as f:
        f.write("alerting:\n  should_be_skipped:\n    x: 1\n")
    with open(os.path.join(d, "base.yaml"), "w") as f:
        f.write("alerting:\n  custom:\n    url: x\n")
    assert configured_providers(d, skip_basename="autogatus.yaml") == {"custom"}


def test_resolve_allowlist_precedence():
    # config primary
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("alerting:\n  custom: {}\n")
        path = f.name
    try:
        allow, src = resolve_allowlist(path, ["ntfy"])
        assert allow == {"custom"} and "gatus-config" in src
    finally:
        os.unlink(path)
    # env fallback when no config
    allow, src = resolve_allowlist("", ["ntfy", "discord"])
    assert allow == {"ntfy", "discord"} and src == "AUTOGATUS_ALERT_TYPES"
    # last resort
    allow, src = resolve_allowlist("", [])
    assert allow == {"custom"} and "default" in src


def test_filter_alerts_drops_unconfigured():
    alerts = build_alerts(["custom", "ntfy"], "x is down")
    kept = filter_alerts(alerts, {"custom"}, "grp/x")
    assert [a["type"] for a in kept] == ["custom"]


def test_filter_alerts_all_kept():
    alerts = build_alerts(["custom"], "d")
    assert filter_alerts(alerts, {"custom", "ntfy"}, "ctx") == alerts


def test_build_alerts_sanitizes():
    a = build_alerts(["custom"], 'we"ird\\name')
    assert a == [{"type": "custom", "description": "weirdname"}]


# ── parse_alerts: shorthand, structured, precedence ───────────────────────────

from autogatus.alerts import parse_alerts  # noqa: E402


def test_parse_alerts_absent_and_silenced():
    assert parse_alerts({}, "d") is None
    assert parse_alerts({"alerts": "none"}, "d") == []


def test_parse_alerts_shorthand_expands():
    got = parse_alerts({"alerts": "custom,ntfy"}, "x is down")
    assert got == [
        {"type": "custom", "description": "x is down"},
        {"type": "ntfy", "description": "x is down"},
    ]


def test_parse_alerts_shorthand_uses_alert_description():
    got = parse_alerts({"alerts": "custom", "alert-description": "boom"}, "d")
    assert got[0]["description"] == "boom"


def test_parse_alerts_structured_all_fields():
    got = parse_alerts(
        {
            "alerts.0.type": "ntfy",
            "alerts.0.failure-threshold": "5",
            "alerts.0.success-threshold": "2",
            "alerts.0.send-on-resolved": "true",
            "alerts.0.description": "db down",
        },
        "unused",
    )
    assert got == [
        {
            "type": "ntfy",
            "failure-threshold": 5,
            "success-threshold": 2,
            "send-on-resolved": True,
            "description": "db down",
        }
    ]


def test_parse_alerts_structured_wins_over_scalar():
    got = parse_alerts({"alerts": "custom", "alerts.0.type": "ntfy"}, "d")
    assert got == [{"type": "ntfy", "description": "d"}]


def test_parse_alerts_bad_int_field_ignored_alert_kept():
    got = parse_alerts({"alerts.0.type": "ntfy", "alerts.0.failure-threshold": "abc"}, "d")
    assert got == [{"type": "ntfy", "description": "d"}]


def test_parse_alerts_bool_variants():
    assert (
        parse_alerts({"alerts.0.type": "c", "alerts.0.send-on-resolved": "no"}, "d")[0][
            "send-on-resolved"
        ]
        is False
    )


def test_parse_alerts_missing_type_skips_that_alert():
    assert parse_alerts({"alerts.0.failure-threshold": "5"}, "d") == []


def test_parse_alerts_structured_description_sanitized():
    got = parse_alerts({"alerts.0.type": "c", "alerts.0.description": 'we"ird\\x'}, "d")
    assert got[0]["description"] == "weirdx"


def test_filter_alerts_keeps_structured_options():
    from autogatus.alerts import filter_alerts

    alerts = [{"type": "ntfy", "failure-threshold": 5, "description": "d"}]
    kept = filter_alerts(alerts, {"ntfy"}, "ctx")
    assert kept == alerts  # structured options survive the allowlist filter
