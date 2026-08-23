from autogatus.health import ContainerHealth, Thresholds, evaluate


def _h(**kw):
    base = {
        "name": "x",
        "stack": "s",
        "state": "running",
        "restart_count": 0,
        "cpu_percent": 5.0,
        "mem_used": 100,
        "mem_limit": 1000,
        "mem_percent": 10.0,
    }
    base.update(kw)
    return ContainerHealth(**base)


def test_healthy_running_is_success():
    v = evaluate(_h(), Thresholds())
    assert v.success
    assert v.headline == 10.0  # mem_percent default headline
    assert "state=running" in v.error


def test_exited_is_failure_with_code():
    v = evaluate(_h(state="exited", exit_code=137), Thresholds())
    assert not v.success
    assert "exited (137)" in v.error


def test_oomkilled_fails():
    v = evaluate(_h(oom_killed=True), Thresholds())
    assert not v.success
    assert "OOMKilled" in v.error


def test_unhealthy_surfaces_output():
    v = evaluate(_h(health_status="unhealthy", health_output="conn refused\n"), Thresholds())
    assert not v.success
    assert "unhealthy: conn refused" in v.error


def test_mem_threshold_breach_fails():
    v = evaluate(_h(mem_percent=97.0), Thresholds(mem_percent=95.0))
    assert not v.success
    assert "mem 97.0%>=95.0%" in v.error


def test_mem_threshold_disabled():
    v = evaluate(_h(mem_percent=99.0), Thresholds(mem_percent=None))
    assert v.success


def test_cpu_not_a_failure_by_default():
    v = evaluate(_h(cpu_percent=350.0), Thresholds())  # cpu_percent threshold None
    assert v.success


def test_cpu_threshold_when_enabled():
    v = evaluate(_h(cpu_percent=350.0), Thresholds(cpu_percent=300.0))
    assert not v.success
    assert "cpu 350.0%>=300.0%" in v.error


def test_restart_increase_fails():
    v = evaluate(_h(restart_count=5), Thresholds(), prev_restart_count=3)
    assert not v.success
    assert "restarted (3->5)" in v.error


def test_restart_same_count_ok():
    v = evaluate(_h(restart_count=5), Thresholds(), prev_restart_count=5)
    assert v.success


def test_headline_metric_selectable():
    v = evaluate(_h(cpu_percent=42.0), Thresholds(), headline_metric="cpu_percent")
    assert v.headline == 42.0


def test_gatus_key_matches_gatus_sanitization():
    from autogatus.monitor import _gatus_key

    # underscores in the group become hyphens in the key (Gatus behaviour)
    assert _gatus_key("ad_blocker", "adguard") == "ad-blocker_adguard"
    assert (
        _gatus_key("photo_processing", "photo_processing_ui")
        == "photo-processing_photo-processing-ui"
    )
    assert _gatus_key("authentik", "authentik-db") == "authentik_authentik-db"


def test_mem_used_mb_property_and_headline():
    h = _h(mem_used=52428800)  # 50 MB
    assert h.mem_used_mb == 50.0
    v = evaluate(h, Thresholds(), headline_metric="mem_used_mb")
    assert v.headline == 50.0
