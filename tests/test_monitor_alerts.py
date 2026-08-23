from autogatus.monitor import ContainerMonitor


class FakeContainer:
    def __init__(self, labels):
        self.labels = labels
        self.name = "c"


def _monitor(default_alert_types):
    return ContainerMonitor(
        client=None,
        pusher=None,
        token="t",
        stack_map={},
        thresholds=None,
        excludes=[],
        default_alert_types=default_alert_types,
    )


def test_default_alert_types_when_no_label():
    m = _monitor(["custom", "ntfy"])
    assert m._alert_types_for(FakeContainer({})) == ["custom", "ntfy"]


def test_label_overrides_default():
    m = _monitor(["custom"])
    assert m._alert_types_for(FakeContainer({"autogatus.alerts": "ntfy,discord"})) == [
        "ntfy",
        "discord",
    ]


def test_label_none_disables():
    m = _monitor(["custom"])
    assert m._alert_types_for(FakeContainer({"autogatus.alerts": "none"})) == []


def test_default_falls_back_to_custom():
    m = _monitor(None)
    assert m._alert_types_for(FakeContainer({})) == ["custom"]
