"""load_stack_map: parse a map, and fall back cleanly on bad input (gate-proofs)."""

import os
import tempfile

from autogatus.stackmap import load_stack_map


def _write(content):
    fd, path = tempfile.mkstemp(suffix=".yml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_empty_path_returns_empty():
    assert load_stack_map("") == {}


def test_missing_file_falls_back_to_empty():
    # Gate: an unmounted/absent map must not crash; group-by-name fallback.
    assert load_stack_map("/no/such/stack-map.yml") == {}


def test_flat_mapping():
    p = _write("adguard: ad_blocker\nblocky: ad_blocker\n")
    assert load_stack_map(p) == {"adguard": "ad_blocker", "blocky": "ad_blocker"}
    os.unlink(p)


def test_stacks_wrapper_key():
    p = _write("stacks:\n  authentik-db: authentik\n")
    assert load_stack_map(p) == {"authentik-db": "authentik"}
    os.unlink(p)


def test_garbage_content_falls_back_to_empty():
    # Gate: a non-mapping document degrades to {} rather than throwing.
    p = _write("- just\n- a\n- list\n")
    assert load_stack_map(p) == {}
    os.unlink(p)
