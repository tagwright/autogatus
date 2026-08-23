"""GatusPusher: HTTP status handling must never crash the loop (gate-proofs)."""

from unittest.mock import MagicMock

from autogatus.push import GatusPusher


def _pusher_with_response(status_code, text=""):
    p = GatusPusher("http://gatus:8080", "tok")
    p._session = MagicMock()
    p._session.post.return_value = MagicMock(status_code=status_code, text=text)
    return p


def test_push_success_returns_true():
    assert _pusher_with_response(200).push("k", True) is True


def test_push_404_not_registered_returns_false():
    # Gate: a freshly-declared endpoint Gatus hasn't loaded yet -> retry, don't crash.
    assert _pusher_with_response(404).push("k", True) is False


def test_push_401_bad_token_returns_false():
    # Gate: bad/rotated token -> false, logged, no crash.
    assert _pusher_with_response(401, "invalid token").push("k", True) is False


def test_push_500_returns_false():
    assert _pusher_with_response(500, "boom").push("k", False) is False


def test_push_connection_exception_returns_false():
    # Gate: Gatus unreachable -> false, not an exception.
    p = GatusPusher("http://gatus:8080", "tok")
    p._session = MagicMock()
    p._session.post.side_effect = OSError("connection refused")
    assert p.push("k", True) is False


def test_push_formats_duration_and_truncates_error():
    p = _pusher_with_response(200)
    p.push("k", False, error="x" * 900, duration=42.7)
    _, kwargs = p._session.post.call_args
    params = kwargs["params"]
    assert params["duration"] == "43ms"
    assert len(params["error"]) == 500
    assert params["success"] == "false"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
