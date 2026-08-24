from autogatus.__main__ import _bool


def test_enable_exec_reads_new_name(monkeypatch):
    monkeypatch.delenv("AUTOGATUS_EXEC_CHECKS", raising=False)
    monkeypatch.setenv("AUTOGATUS_ENABLE_EXEC", "true")
    assert _bool("AUTOGATUS_ENABLE_EXEC", _bool("AUTOGATUS_EXEC_CHECKS", False)) is True


def test_enable_exec_falls_back_to_legacy(monkeypatch):
    monkeypatch.delenv("AUTOGATUS_ENABLE_EXEC", raising=False)
    monkeypatch.setenv("AUTOGATUS_EXEC_CHECKS", "true")
    assert _bool("AUTOGATUS_ENABLE_EXEC", _bool("AUTOGATUS_EXEC_CHECKS", False)) is True


def test_new_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("AUTOGATUS_ENABLE_EXEC", "false")
    monkeypatch.setenv("AUTOGATUS_EXEC_CHECKS", "true")
    assert _bool("AUTOGATUS_ENABLE_EXEC", _bool("AUTOGATUS_EXEC_CHECKS", False)) is False
