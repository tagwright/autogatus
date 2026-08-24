from autogatus.duration import parse_duration_seconds


def test_bare_number_is_seconds():
    assert parse_duration_seconds("15", 9) == 15
    assert parse_duration_seconds(20, 9) == 20


def test_units():
    assert parse_duration_seconds("30s", 9) == 30
    assert parse_duration_seconds("5m", 9) == 300
    assert parse_duration_seconds("2h", 9) == 7200
    assert parse_duration_seconds("500ms", 9) == 0.5


def test_compound():
    assert parse_duration_seconds("1h30m", 9) == 5400


def test_unparseable_returns_default():
    assert parse_duration_seconds("", 9) == 9
    assert parse_duration_seconds(None, 9) == 9
    assert parse_duration_seconds("junk", 9) == 9
    assert parse_duration_seconds("15x", 9) == 9
    assert parse_duration_seconds("1h 30m", 9) == 9  # go durations have no spaces
