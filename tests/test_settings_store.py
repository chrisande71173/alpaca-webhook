import json

import pytest

from settings import SETTINGS, SettingsError, SettingsStore


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "settings.json")


@pytest.fixture(autouse=True)
def no_env_defaults(monkeypatch):
    monkeypatch.delenv("TAKE_PROFIT_PERCENT", raising=False)
    monkeypatch.delenv("SELL_LIMIT_PERCENT", raising=False)


def test_starts_with_the_defaults_the_code_used_to_hardcode(path):
    store = SettingsStore(path)

    assert store.all() == {
        "TRADE_PERCENT": 0.01,
        "MIN_NOTIONAL": 1.00,
        "LIMIT_PRICE_BUFFER": 0.05,
        "TAKE_PROFIT_PERCENT": 0.0,
        "SELL_LIMIT_PERCENT": 0.0,
        "POLL_INTERVAL": 5,
        "POLL_TIMEOUT": 3600,
    }


def test_existing_env_vars_still_set_the_defaults(path, monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_PERCENT", "0.05")
    monkeypatch.setenv("SELL_LIMIT_PERCENT", "0.1")

    store = SettingsStore(path)

    assert store.get("TAKE_PROFIT_PERCENT") == 0.05
    assert store.get("SELL_LIMIT_PERCENT") == 0.1


def test_an_invalid_env_var_is_ignored(path, monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_PERCENT", "lots")

    assert SettingsStore(path).get("TAKE_PROFIT_PERCENT") == 0.0


def test_saved_values_survive_a_restart(path):
    SettingsStore(path).update({"TRADE_PERCENT": 0.02, "POLL_INTERVAL": 10})

    restarted = SettingsStore(path)

    assert restarted.get("TRADE_PERCENT") == 0.02
    assert restarted.get("POLL_INTERVAL") == 10


def test_saved_values_beat_env_defaults(path, monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_PERCENT", "0.05")
    SettingsStore(path).update({"TAKE_PROFIT_PERCENT": 0.08})

    assert SettingsStore(path).get("TAKE_PROFIT_PERCENT") == 0.08


def test_numbers_sent_as_strings_are_accepted(path):
    store = SettingsStore(path)

    store.update({"MIN_NOTIONAL": "2.5"})

    assert store.get("MIN_NOTIONAL") == 2.5


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"TRADE_PERCENT": 2}, "between"),
        ({"TRADE_PERCENT": 0}, "between"),
        ({"MIN_NOTIONAL": -1}, "between"),
        ({"POLL_INTERVAL": 2.5}, "whole number"),
        ({"POLL_TIMEOUT": "soon"}, "must be a number"),
        ({"LIMIT_PRICE_BUFFER": None}, "must be a number"),
        ({"LIMIT_PRICE_BUFFER": True}, "must be a number"),
        ({"TRADE_PERCENT": float("nan")}, "must be a number"),
        ({"NOT_A_SETTING": 1}, "Unknown setting"),
    ],
)
def test_rejects_bad_values(path, changes, message):
    with pytest.raises(SettingsError, match=message):
        SettingsStore(path).update(changes)


def test_one_bad_value_saves_nothing(path):
    store = SettingsStore(path)

    with pytest.raises(SettingsError):
        store.update({"TRADE_PERCENT": 0.02, "POLL_INTERVAL": 0})

    assert store.get("TRADE_PERCENT") == 0.01
    assert SettingsStore(path).get("TRADE_PERCENT") == 0.01


def test_an_unreadable_file_falls_back_to_defaults(path):
    with open(path, "w", encoding="utf-8") as file:
        file.write("{not json")

    assert SettingsStore(path).get("TRADE_PERCENT") == 0.01


def test_an_out_of_range_saved_value_is_ignored(path):
    with open(path, "w", encoding="utf-8") as file:
        json.dump({"TRADE_PERCENT": 5, "MIN_NOTIONAL": 3}, file)

    store = SettingsStore(path)

    assert store.get("TRADE_PERCENT") == 0.01
    assert store.get("MIN_NOTIONAL") == 3


def test_creates_the_folder_for_a_volume_path(tmp_path):
    path = str(tmp_path / "data" / "settings.json")

    SettingsStore(path).update({"MIN_NOTIONAL": 2})

    assert SettingsStore(path).get("MIN_NOTIONAL") == 2


def test_every_default_is_within_its_own_range():
    for setting in SETTINGS:
        assert setting.minimum <= setting.default <= setting.maximum, setting.key
