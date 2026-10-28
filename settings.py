"""Trading settings that can be viewed and changed from the web UI.

Values live in a JSON file so they survive restarts. On Railway, point
SETTINGS_PATH at a mounted volume (e.g. /data/settings.json) so they also
survive redeploys; locally the file defaults to settings.json next to this
module.

Anything not in the file falls back to its default. TAKE_PROFIT_PERCENT and
SELL_LIMIT_PERCENT still honour their environment variables as defaults, so
existing deployments behave exactly as before until something is saved.
"""

import json
import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    help: str
    default: float
    minimum: float
    maximum: float
    # "percent" values are stored as fractions (0.05) and shown as 5 in the UI.
    kind: str = "number"  # "percent", "dollars", "seconds"
    integer: bool = False


SETTINGS = [
    Setting(
        "TRADE_PERCENT", "Trade size",
        "Share of buying power to spend on each buy.",
        0.01, 0.0001, 1.0, kind="percent",
    ),
    Setting(
        "MIN_NOTIONAL", "Minimum order",
        "Skip a buy if the trade size works out below this amount.",
        1.00, 0.0, 1_000_000.0, kind="dollars",
    ),
    Setting(
        "LIMIT_PRICE_BUFFER", "Extended-hours limit buffer",
        "Outside market hours, buys are placed this far above the alert price "
        "and sells this far below it, to help them fill.",
        0.05, 0.0, 0.5, kind="percent",
    ),
    Setting(
        "TAKE_PROFIT_PERCENT", "Take-profit",
        "After a buy, place a sell limit this far above the fill price. "
        "0 turns take-profit off.",
        0.0, 0.0, 10.0, kind="percent",
    ),
    Setting(
        "SELL_LIMIT_PERCENT", "Sell-limit offset",
        "For sell_limit alerts, sell this far above the alert price. "
        "0 sells at exactly the alert price.",
        0.0, 0.0, 10.0, kind="percent",
    ),
    Setting(
        "POLL_INTERVAL", "Fill check interval",
        "After an extended-hours buy, how often to check whether it filled "
        "(to place the take-profit).",
        5, 1, 300, kind="seconds", integer=True,
    ),
    Setting(
        "POLL_TIMEOUT", "Fill check timeout",
        "Stop checking for an extended-hours fill after this long.",
        3600, 10, 86_400, kind="seconds", integer=True,
    ),
]

_BY_KEY = {setting.key: setting for setting in SETTINGS}

# Environment variables that set a default, for deployments that predate the UI.
_ENV_DEFAULTS = ("TAKE_PROFIT_PERCENT", "SELL_LIMIT_PERCENT")


class SettingsError(ValueError):
    """A submitted value is missing, not a number, or out of range."""


class SettingsStore:
    def __init__(self, path=None):
        self.path = path or os.getenv(
            "SETTINGS_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json"),
        )
        self._lock = threading.Lock()
        self._values = self._load()

    def defaults(self):
        values = {setting.key: setting.default for setting in SETTINGS}
        for key in _ENV_DEFAULTS:
            raw = os.getenv(key)
            if raw not in (None, ""):
                try:
                    values[key] = self._validate(_BY_KEY[key], raw)
                except SettingsError as error:
                    print(f"WARNING: ignoring {key} environment variable: {error}")
        return values

    def _load(self):
        values = self.defaults()
        try:
            with open(self.path, encoding="utf-8") as file:
                saved = json.load(file)
        except FileNotFoundError:
            return values
        except (OSError, json.JSONDecodeError) as error:
            print(f"WARNING: could not read settings from {self.path}, using defaults: {error}")
            return values
        for key, raw in saved.items():
            if key in _BY_KEY:
                try:
                    values[key] = self._validate(_BY_KEY[key], raw)
                except SettingsError as error:
                    print(f"WARNING: ignoring saved {key}: {error}")
        return values

    @staticmethod
    def _validate(setting, raw):
        if isinstance(raw, bool):
            raise SettingsError(f"{setting.label} must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SettingsError(f"{setting.label} must be a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise SettingsError(f"{setting.label} must be a number")
        if setting.integer:
            if value != int(value):
                raise SettingsError(f"{setting.label} must be a whole number")
            value = int(value)
        if not setting.minimum <= value <= setting.maximum:
            raise SettingsError(
                f"{setting.label} must be between {setting.minimum} and {setting.maximum}"
            )
        return value

    def get(self, key):
        with self._lock:
            return self._values[key]

    def all(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        """Validates every change first, so a bad value saves nothing."""
        unknown = sorted(set(changes) - set(_BY_KEY))
        if unknown:
            raise SettingsError(f"Unknown setting(s): {', '.join(unknown)}")
        validated = {key: self._validate(_BY_KEY[key], raw) for key, raw in changes.items()}
        with self._lock:
            updated = {**self._values, **validated}
            self._write(updated)
            self._values = updated
            return dict(updated)

    def _write(self, values):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(values, file, indent=2, sort_keys=True)
        os.replace(temporary, self.path)  # atomic: never leaves a half-written file
