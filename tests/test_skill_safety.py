"""Tests for rumil_skills._safety.assert_local_ok."""

import pytest
from rumil_skills._safety import assert_local_ok


def test_returns_when_not_prod(monkeypatch):
    monkeypatch.delenv("RUMIL_ALLOW_PROD", raising=False)
    assert_local_ok(prod=False)


def test_returns_when_prod_and_env_var_set(monkeypatch):
    monkeypatch.setenv("RUMIL_ALLOW_PROD", "1")
    assert_local_ok(prod=True)


def test_exits_when_prod_without_env_var(monkeypatch, capsys):
    monkeypatch.delenv("RUMIL_ALLOW_PROD", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        assert_local_ok(prod=True)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "RUMIL_ALLOW_PROD" in err
    assert "--prod" in err


def test_exits_when_prod_and_env_var_not_equal_to_one(monkeypatch, capsys):
    monkeypatch.setenv("RUMIL_ALLOW_PROD", "yes")
    with pytest.raises(SystemExit) as excinfo:
        assert_local_ok(prod=True)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "RUMIL_ALLOW_PROD" in err
