"""Store: read/write, history cap, and the prune gate."""

from collections import namedtuple

from autogatus.store import Store

V = namedtuple("V", "success")


def test_update_then_get_returns_copy_with_history_list():
    s = Store()
    s.update("k", "stack", "name", health="H", verdict=V(True), ts=1.0)
    got = s.get("k")
    assert got["stack"] == "stack" and got["name"] == "name"
    assert got["health"] == "H" and got["updated"] == 1.0
    assert got["history"] == [(True, 1.0)]


def test_get_missing_returns_none():
    assert Store().get("nope") is None


def test_all_returns_every_key():
    s = Store()
    s.update("a", "s", "a", None, V(True), 1.0)
    s.update("b", "s", "b", None, V(False), 2.0)
    assert set(s.all()) == {"a", "b"}


def test_prune_drops_absent_keys():
    # Gate: keys no longer present in the reconcile are removed from the store.
    s = Store()
    s.update("a", "s", "a", None, V(True), 1.0)
    s.update("b", "s", "b", None, V(True), 1.0)
    s.prune({"a"})
    assert set(s.all()) == {"a"}


def test_history_is_capped_at_maxlen():
    s = Store(history=3)
    for i in range(5):
        s.update("k", "s", "k", None, V(True), float(i))
    assert len(s.get("k")["history"]) == 3
    assert s.get("k")["history"][0] == (True, 2.0)
