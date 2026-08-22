from autogatus.stats import cpu_percent, memory


def _stats(cpu_now, cpu_pre, sys_now, sys_pre, cpus, usage, limit, cache=0):
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu_now, "percpu_usage": [0] * cpus},
            "system_cpu_usage": sys_now,
            "online_cpus": cpus,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": cpu_pre},
            "system_cpu_usage": sys_pre,
        },
        "memory_stats": {"usage": usage, "limit": limit, "stats": {"inactive_file": cache}},
    }


def test_cpu_percent_one_full_core():
    # cpu_delta == system_delta, 1 core -> 100%
    s = _stats(200, 100, 1100, 1000, 1, 0, 1)
    assert cpu_percent(s) == 100.0


def test_cpu_percent_scaled_by_cores():
    # cpu_delta == system_delta, 4 cores -> 400%
    s = _stats(200, 100, 1100, 1000, 4, 0, 1)
    assert cpu_percent(s) == 400.0


def test_cpu_percent_missing_fields():
    assert cpu_percent({}) is None
    assert cpu_percent({"cpu_stats": {}, "precpu_stats": {}}) is None


def test_cpu_percent_zero_system_delta():
    s = _stats(200, 100, 1000, 1000, 1, 0, 1)  # no system delta
    assert cpu_percent(s) is None


def test_memory_subtracts_cache():
    used, limit, pct = memory(_stats(0, 0, 1, 1, 1, usage=600, limit=1000, cache=100))
    assert used == 500
    assert limit == 1000
    assert pct == 50.0


def test_memory_no_limit_returns_none_percent():
    used, limit, pct = memory(_stats(0, 0, 1, 1, 1, usage=500, limit=0, cache=0))
    assert used == 500
    assert pct is None


def test_memory_missing():
    assert memory({}) is None


def test_network_totals():
    from autogatus.stats import network
    s = {"networks": {"eth0": {"rx_bytes": 100, "tx_bytes": 200},
                        "eth1": {"rx_bytes": 50, "tx_bytes": 5}}}
    assert network(s) == (150, 205)
    assert network({}) is None


def test_block_io_totals():
    from autogatus.stats import block_io
    s = {"blkio_stats": {"io_service_bytes_recursive": [
        {"op": "Read", "value": 1000}, {"op": "Write", "value": 500},
        {"op": "Read", "value": 200}]}}
    assert block_io(s) == (1200, 500)
    assert block_io({}) is None
