"""Persistent JSON settings, shared between the display loop and the web UI."""

import copy
import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

DEFAULTS = {
    "api_key": "",
    # Each view is one screenful; tapping the screen cycles through them.
    # A view can merge several platforms (PIDREFs) at the same station.
    "views": [],
    "wake_minutes": 30,
    "refresh_seconds": 30,
    "colour": "#FFB000",
    "show_messages": True,
    # Outer ring: "clock" (analogue time), "next_tram" (minutes to the next
    # departure) or "off".
    "ring": "clock",
    # Windows when the screen stays on without a tap, e.g.
    # {"days": [0, 1, 2, 3, 4], "start": "07:30", "end": "08:30"}  (0 = Monday)
    "schedule": [],
    "web_port": 80,
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RING_MODES = ("clock", "next_tram", "off")


class ConfigError(ValueError):
    pass


def validate(cfg):
    """Return a cleaned copy of cfg, raising ConfigError on bad input."""
    out = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in cfg:
            out[key] = cfg[key]

    out["api_key"] = str(out["api_key"]).strip()

    views = []
    for v in out["views"]:
        pids = [str(p).strip() for p in v.get("pids", []) if str(p).strip()]
        if not pids:
            raise ConfigError("Every view needs at least one platform")
        name = str(v.get("name", "")).strip() or pids[0]
        views.append({"name": name[:40], "pids": pids})
    out["views"] = views

    for key, lo, hi in (("wake_minutes", 1, 24 * 60), ("refresh_seconds", 10, 600), ("web_port", 1, 65535)):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            raise ConfigError("{} must be a number".format(key))
        if not lo <= out[key] <= hi:
            raise ConfigError("{} must be between {} and {}".format(key, lo, hi))

    if not _COLOUR_RE.match(str(out["colour"])):
        raise ConfigError("colour must look like #RRGGBB")
    out["show_messages"] = bool(out["show_messages"])
    if out["ring"] not in RING_MODES:
        raise ConfigError("ring must be one of: " + ", ".join(RING_MODES))

    schedule = []
    for w in out["schedule"]:
        start, end = str(w.get("start", "")), str(w.get("end", ""))
        if not (_TIME_RE.match(start) and _TIME_RE.match(end)):
            raise ConfigError("Schedule times must be HH:MM")
        days = sorted({int(d) for d in w.get("days", []) if 0 <= int(d) <= 6})
        if not days:
            raise ConfigError("Each schedule window needs at least one day")
        schedule.append({"days": days, "start": start, "end": end})
    out["schedule"] = schedule
    return out


class Config:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._listeners = []
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return validate(json.load(f))
        except FileNotFoundError:
            log.warning("No config at %s, using defaults", self.path)
        except (ValueError, ConfigError) as e:
            log.error("Ignoring invalid config %s: %s", self.path, e)
        return copy.deepcopy(DEFAULTS)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, new):
        """Validate, persist atomically and notify listeners. Returns the saved config."""
        merged = self.get()
        merged.update(new)
        cleaned = validate(merged)
        tmp = self.path + ".tmp"
        # Owner-only: the file holds the API key.
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(cleaned, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        with self._lock:
            self._data = cleaned
        for fn in self._listeners:
            fn(cleaned)
        return copy.deepcopy(cleaned)

    def on_change(self, fn):
        self._listeners.append(fn)
