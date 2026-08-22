import os, tempfile
from autogatus.reconcile import Writer


def test_writer_only_writes_on_change():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "out.yaml")
    w = Writer(path)
    eps = [{"name": "a", "group": "g", "url": "http://a/", "interval": "60s",
            "conditions": ["[STATUS] == 200"]}]
    assert w.reconcile(eps) is True          # first write
    assert w.reconcile(eps) is False         # unchanged -> no write
    eps2 = eps + [{"name": "b", "group": "g", "url": "http://b/", "interval": "60s",
                   "conditions": ["[STATUS] == 200"]}]
    assert w.reconcile(eps2) is True         # changed -> write


def test_writer_external_endpoints_change_detected():
    d = tempfile.mkdtemp()
    w = Writer(os.path.join(d, "out.yaml"))
    ext = [{"name": "c1", "group": "s", "token": "t", "heartbeat": {"interval": "90s"}}]
    assert w.reconcile([], ext) is True
    assert w.reconcile([], ext) is False
    ext2 = [{"name": "c1", "group": "s", "token": "t2", "heartbeat": {"interval": "90s"}}]
    assert w.reconcile([], ext2) is True     # token change detected
