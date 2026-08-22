import yaml

from autogatus.render import render, render_stable


def _sample():
    return [
        {"name": "b", "group": "z", "url": "http://b/", "interval": "60s",
         "conditions": ["[STATUS] == 200"]},
        {"name": "a", "group": "a", "url": "http://a/", "interval": "60s",
         "conditions": ["[STATUS] == 200"]},
    ]


def test_render_is_valid_yaml_with_endpoints():
    doc = yaml.safe_load(render(_sample()))
    assert "endpoints" in doc
    assert len(doc["endpoints"]) == 2


def test_render_sorts_by_group_then_name():
    doc = yaml.safe_load(render(_sample()))
    keys = [(e["group"], e["name"]) for e in doc["endpoints"]]
    assert keys == sorted(keys)


def test_empty_render():
    doc = yaml.safe_load(render([]))
    assert doc["endpoints"] == []


def test_stable_ignores_timestamp():
    # render() embeds a timestamp; render_stable() must not, so change detection
    # is not fooled into rewriting an unchanged cluster.
    s1 = render_stable(_sample())
    s2 = render_stable(list(reversed(_sample())))
    assert s1 == s2  # order-independent


def test_stable_reflects_real_change():
    base = _sample()
    changed = _sample()
    changed[0]["interval"] = "120s"
    assert render_stable(base) != render_stable(changed)
