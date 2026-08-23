from autogatus import web
from autogatus.health import ContainerHealth, Verdict


class FakeStore:
    def __init__(self):
        h = ContainerHealth(
            name="firefly",
            stack="firefly",
            state="running",
            cpu_percent=2.2,
            mem_used=96_000_000,
            mem_limit=2_000_000_000,
            mem_percent=4.8,
            restart_count=0,
            net_rx=1000,
            net_tx=2000,
        )
        self._items = {
            "firefly_firefly": {
                "stack": "firefly",
                "name": "firefly",
                "health": h,
                "verdict": Verdict(True, "state=running cpu=2.2%", 96.0),
                "history": [(True, 1.0), (True, 2.0)],
                "updated": 2.0,
            }
        }

    def all(self):
        return self._items

    def get(self, key):
        return self._items.get(key)


def _client():
    app = web.init(FakeStore())
    app.config["TESTING"] = True
    return app.test_client()


def test_index_has_controls_and_data_attributes():
    r = _client().get("/details")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    for marker in [
        'id="ag-q"',
        'id="ag-group"',
        'id="ag-sort"',
        'id="ag-order"',
        'data-mem="96000000"',
        'data-cpu="2.2"',
        'data-status="healthy"',
        "Sort: Memory",
        "autogatus.filters",
    ]:
        assert marker in body, marker


def test_detail_page_renders_stats():
    r = _client().get("/details/firefly_firefly")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    for marker in ["Current Status", "Memory", "CPU", "Network", "Block I/O", "/css/app.css"]:
        assert marker in body, marker


def test_detail_404_for_unknown():
    assert _client().get("/details/nope_nope").status_code == 404
