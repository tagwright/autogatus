import pytest

from autogatus.labels import is_enabled, parse_container


def test_disabled_container_yields_nothing():
    assert parse_container({}, "svc") == []
    assert parse_container({"gatus.web.url": "http://x/"}, "svc") == []  # no enable


def test_enable_variants():
    for v in ("true", "True", "1", "yes", "on"):
        assert is_enabled({"gatus.enable": v})
    for v in ("false", "0", "no", ""):
        assert not is_enabled({"gatus.enable": v})


def test_minimal_http_endpoint_defaults():
    labels = {"gatus.enable": "true", "gatus.web.url": "http://svc:3000/"}
    eps = parse_container(labels, container_name="geoducking")
    assert eps == [
        {
            "name": "web",
            "group": "geoducking",  # defaults to container name
            "url": "http://svc:3000/",
            "interval": "60s",
            "conditions": ["[STATUS] == 200"],  # http default
            "alerts": [{"type": "custom", "description": "web is down"}],
        }
    ]


def test_tcp_default_condition():
    labels = {"gatus.enable": "true", "gatus.db.url": "tcp://postgres:5432"}
    ep = parse_container(labels, "core")[0]
    assert ep["conditions"] == ["[CONNECTED] == true"]


def test_indexed_conditions_are_ordered():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "gatus.web.conditions.1": "[RESPONSE_TIME] < 500",
        "gatus.web.conditions.0": "[STATUS] == 200",
        "gatus.web.conditions.2": "[BODY].status == UP",
    }
    ep = parse_container(labels, "svc")[0]
    assert ep["conditions"] == [
        "[STATUS] == 200",
        "[RESPONSE_TIME] < 500",
        "[BODY].status == UP",
    ]


def test_name_group_interval_overrides():
    labels = {
        "gatus.enable": "true",
        "gatus.api.url": "http://svc/health",
        "gatus.api.name": "My API",
        "gatus.api.group": "public",
        "gatus.api.interval": "120s",
    }
    ep = parse_container(labels, "svc")[0]
    assert ep["name"] == "My API"
    assert ep["group"] == "public"
    assert ep["interval"] == "120s"


def test_headers_and_method_and_body():
    labels = {
        "gatus.enable": "true",
        "gatus.api.url": "http://svc/",
        "gatus.api.method": "POST",
        "gatus.api.body": '{"ping":1}',
        "gatus.api.headers.X-Api-Key": "secret",
        "gatus.api.headers.Accept": "application/json",
    }
    ep = parse_container(labels, "svc")[0]
    assert ep["method"] == "POST"
    assert ep["body"] == '{"ping":1}'
    assert ep["headers"] == {"X-Api-Key": "secret", "Accept": "application/json"}


def test_alert_can_be_disabled():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "gatus.web.alerts": "none",
    }
    ep = parse_container(labels, "svc")[0]
    assert "alerts" not in ep


def test_custom_alert_description():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "gatus.web.alert-description": "the website fell over",
    }
    ep = parse_container(labels, "svc")[0]
    assert ep["alerts"][0]["description"] == "the website fell over"


def test_multiple_endpoints_per_container():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc:3000/",
        "gatus.metrics.url": "http://svc:9090/metrics",
    }
    eps = parse_container(labels, "svc")
    assert {e["name"] for e in eps} == {"web", "metrics"}


def test_default_group_env_override():
    labels = {"gatus.enable": "true", "gatus.web.url": "http://svc/"}
    ep = parse_container(labels, "svc", default_group="homelab")[0]
    assert ep["group"] == "homelab"


def test_missing_url_raises():
    labels = {"gatus.enable": "true", "gatus.web.interval": "60s"}
    with pytest.raises(ValueError):
        parse_container(labels, "svc")


def test_unrelated_labels_ignored():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "traefik.enable": "true",
        "com.docker.compose.project": "docker",
    }
    eps = parse_container(labels, "svc")
    assert len(eps) == 1


def test_alerts_list_label_overrides_bool():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "gatus.web.alerts": "custom,ntfy",
    }
    ep = parse_container(labels, "svc")[0]
    assert [a["type"] for a in ep["alerts"]] == ["custom", "ntfy"]


def test_alerts_none_disables():
    labels = {
        "gatus.enable": "true",
        "gatus.web.url": "http://svc/",
        "gatus.web.alerts": "none",
    }
    ep = parse_container(labels, "svc")[0]
    assert "alerts" not in ep


def test_alert_bool_backcompat_default_custom():
    # No alerts field, no alert field -> single custom alert (unchanged behavior)
    labels = {"gatus.enable": "true", "gatus.web.url": "http://svc/"}
    ep = parse_container(labels, "svc")[0]
    assert ep["alerts"] == [{"type": "custom", "description": "web is down"}]


def test_legacy_alert_bool_is_ignored():
    # gatus.<id>.alert (the old bool) was removed. It is now an unknown field, so
    # the default single custom alert still applies. Silence with alerts=none.
    labels = {"gatus.enable": "true", "gatus.web.url": "http://svc/", "gatus.web.alert": "false"}
    ep = parse_container(labels, "svc")[0]
    assert ep["alerts"] == [{"type": "custom", "description": "web is down"}]
