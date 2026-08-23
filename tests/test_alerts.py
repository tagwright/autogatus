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
