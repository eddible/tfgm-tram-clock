"""Wake/sleep logic, touch handling and the departure fetcher."""

import datetime
import logging
import threading
import time

from . import homeassistant
from .tfgm import TfGM, TfGMError

log = logging.getLogger(__name__)

LONG_PRESS = 1.5     # seconds held to put the screen to sleep
DEBOUNCE = 0.3       # ignore taps closer together than this
STALE_AFTER = 300    # seconds before cached departures are flagged as old
ERROR_RETRY = 15     # seconds between retries after a failed fetch
MIN_REFETCH = 10     # don't refetch a view more often than this
CHARGE_REFRESH = 300  # seconds between Home Assistant reads
CHARGE_STALE = 3600   # stop showing a charge reading older than this
HA_KEYS = ("ha_url", "ha_token", "ha_entity")
SLEEP_COOLDOWN = 120  # ignore "at least" wakes (e.g. motion) this long after a long-press sleep


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def schedule_end(schedule, now):
    """If `now` falls inside a schedule window, return when it ends (epoch secs)."""
    dt = datetime.datetime.fromtimestamp(now)
    mins = dt.hour * 60 + dt.minute
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for w in schedule:
        start, end = _minutes(w["start"]), _minutes(w["end"])
        if start < end:
            if dt.weekday() in w["days"] and start <= mins < end:
                return (midnight + datetime.timedelta(minutes=end)).timestamp()
        else:  # window crosses midnight
            if dt.weekday() in w["days"] and mins >= start:
                return (midnight + datetime.timedelta(days=1, minutes=end)).timestamp()
            if (dt.weekday() - 1) % 7 in w["days"] and mins < end:
                return (midnight + datetime.timedelta(minutes=end)).timestamp()
    return None


class Controller:
    def __init__(self, config):
        self.config = config
        self._cfg = config.get()
        self.api = TfGM(self._cfg["api_key"])
        self._lock = threading.Lock()
        self._changed = threading.Event()   # wakes the display loop
        self._fetch_now = threading.Event()  # wakes the fetcher
        self.awake_until = 0.0
        self.awake_total = 0.0
        self.snoozed_until = 0.0   # manual sleep during a schedule window
        self.view = 0
        self._boards = {}          # view index -> (Board, fetched_at)
        self.error = None
        self._charge_now = threading.Event()
        self.charge = None          # (percent, read_at)
        self.charge_error = None
        self._touch_down = None
        self._last_tap = 0.0
        self._slept_at = 0.0
        config.on_change(self._config_changed)

    # -- config ------------------------------------------------------------

    def _config_changed(self, cfg):
        with self._lock:
            old = self._cfg
            key_changed = cfg["api_key"] != self._cfg["api_key"]
            views_changed = cfg["views"] != self._cfg["views"]
            self._cfg = cfg
            if key_changed:
                self.api.set_key(cfg["api_key"])
            if key_changed or views_changed:
                self._boards.clear()
                self.error = None
            if self.view >= len(cfg["views"]):
                self.view = 0
            if any(cfg[k] != old[k] for k in HA_KEYS):
                self.charge = None
                self.charge_error = None
        self._charge_now.set()
        self._fetch_now.set()
        self._changed.set()

    @property
    def cfg(self):
        return self._cfg

    # -- wake / sleep ------------------------------------------------------

    def wake(self, minutes=None, at_least=False):
        """Turn the screen on for `minutes`.

        With at_least (used for motion sensors), never shorten a longer wake
        already in progress, and don't undo a long-press sleep from just now.
        """
        minutes = minutes or self._cfg["wake_minutes"]
        now = time.time()
        with self._lock:
            was_awake = self._awake_locked(now)
            if at_least:
                if now - self._slept_at < SLEEP_COOLDOWN:
                    return
                if self.awake_until >= now + minutes * 60.0:
                    return
            self.awake_total = minutes * 60.0
            self.awake_until = now + self.awake_total
            self.snoozed_until = 0.0
        if not was_awake:
            log.info("Waking for %g min%s", minutes, " (at least)" if at_least else "")
            self._fetch_now.set()
            self._charge_now.set()
        self._changed.set()

    def sleep(self, cooldown=False):
        """Turn the screen off. cooldown: briefly ignore motion wakes, for when
        the person is standing in front of the clock (long press)."""
        now = time.time()
        with self._lock:
            self.awake_until = 0.0
            self._slept_at = now if cooldown else 0.0
            end = schedule_end(self._cfg["schedule"], now)
            self.snoozed_until = end or 0.0
        log.info("Sleeping")
        self._changed.set()

    def _awake_locked(self, now):
        if now < self.awake_until:
            return True
        return now >= self.snoozed_until and schedule_end(self._cfg["schedule"], now) is not None

    def is_awake(self, now=None):
        with self._lock:
            return self._awake_locked(now or time.time())

    def remaining(self, now):
        """(seconds left, total seconds, scheduled?) for the current wake period."""
        with self._lock:
            sched = None
            if now >= self.snoozed_until:
                sched = schedule_end(self._cfg["schedule"], now)
            manual = self.awake_until - now if now < self.awake_until else None
            if sched and (manual is None or sched - now > manual):
                return sched - now, None, True
            if manual is not None:
                return manual, self.awake_total, False
            return None, None, False

    def set_view(self, index):
        with self._lock:
            if not self._cfg["views"]:
                return
            self.view = index % len(self._cfg["views"])
        self._fetch_now.set()
        self._changed.set()

    def next_view(self):
        self.set_view(self.view + 1)

    # -- touch -------------------------------------------------------------

    def on_touch(self, touch_id, x, y, down):
        """Called from the GPIO interrupt thread."""
        if touch_id != 0:
            return
        now = time.time()
        if down:
            if self._touch_down is not None:
                return  # finger moving, not a new press
            self._touch_down = now
            if not self.is_awake(now):
                self._touch_down = -1  # this press only wakes; ignore its release
                self.wake()
            return

        pressed, self._touch_down = self._touch_down, None
        if pressed is None or pressed < 0:
            return
        if now - pressed >= LONG_PRESS:
            self.sleep(cooldown=True)
        elif now - self._last_tap >= DEBOUNCE:
            self._last_tap = now
            if len(self._cfg["views"]) > 1:
                self.next_view()
            self.wake()  # extend the timer

    # -- data --------------------------------------------------------------

    def board(self, index=None):
        """(Board or None, age in seconds or None) for a view."""
        with self._lock:
            entry = self._boards.get(self.view if index is None else index)
        if not entry:
            return None, None
        return entry[0], time.time() - entry[1]

    def fetch(self, index):
        views = self._cfg["views"]
        if not self._cfg["api_key"]:
            raise TfGMError("No API key set")
        if not views:
            raise TfGMError("No stops configured")
        board = self.api.board(views[index]["pids"])
        with self._lock:
            self._boards[index] = (board, time.time())
        return board

    def fetcher(self, stop):
        """Thread: keep the visible view fresh while the screen is awake."""
        while not stop.is_set():
            wait = self._cfg["refresh_seconds"]
            _, age = self.board()
            if self.is_awake() and (age is None or age >= MIN_REFETCH):
                try:
                    self.fetch(self.view)
                    if self.error:
                        log.info("Fetching recovered")
                    self.error = None
                except TfGMError as e:
                    if str(e) != self.error:
                        log.warning("Fetch failed: %s", e)
                    self.error = str(e)
                    wait = ERROR_RETRY
                except Exception:
                    log.exception("Unexpected fetch error")
                    self.error = "Internal error"
                    wait = ERROR_RETRY
                self._changed.set()
            elif self.is_awake():
                wait = max(1, self._cfg["refresh_seconds"] - age)
            else:
                wait = 3600  # nothing to do until woken
            self._fetch_now.wait(wait)
            self._fetch_now.clear()

    def charge_fetcher(self, stop):
        """Thread: read the Home Assistant sensor while it's on screen."""
        while not stop.is_set():
            cfg = self._cfg
            wait = 3600
            if self.is_awake() and cfg["ring"] == "tram_charge":
                age = time.time() - self.charge[1] if self.charge else None
                if age is None or age >= CHARGE_REFRESH:
                    try:
                        value = homeassistant.read_percent(cfg["ha_url"], cfg["ha_token"], cfg["ha_entity"])
                        self.charge = (value, time.time())
                        self.charge_error = None
                    except homeassistant.HomeAssistantError as e:
                        if str(e) != self.charge_error:
                            log.warning("Charge read failed: %s", e)
                        self.charge_error = str(e)
                    except Exception:
                        log.exception("Unexpected Home Assistant error")
                        self.charge_error = "Internal error"
                    self._changed.set()
                    wait = CHARGE_REFRESH if self.charge_error is None else ERROR_RETRY * 4
                else:
                    wait = CHARGE_REFRESH - age
            self._charge_now.wait(wait)
            self._charge_now.clear()

    def current_charge(self):
        """Latest charge percentage, or None if unknown or too old."""
        reading = self.charge
        if reading and time.time() - reading[1] < CHARGE_STALE:
            return reading[0]
        return None

    def poke(self):
        """Wake the fetchers and display loop (e.g. on shutdown)."""
        self._charge_now.set()
        self._fetch_now.set()
        self._changed.set()

    def wait_for_change(self, timeout):
        """Block until something changes or timeout; returns True if changed."""
        changed = self._changed.wait(timeout)
        self._changed.clear()
        return changed

    def display_state(self, now):
        board, age = self.board()
        remaining, total, scheduled = self.remaining(now)
        views = self._cfg["views"]
        return {
            "view_name": views[self.view]["name"] if views else "Tram Clock",
            "board": board,
            "error": self.error,
            "stale": age is not None and age > STALE_AFTER,
            "remaining": remaining,
            "total": total,
            "scheduled": scheduled,
            "ring": self._cfg["ring"],
            "charge": self.current_charge(),
        }

    def status(self):
        now = time.time()
        board, age = self.board()
        remaining, total, scheduled = self.remaining(now)
        return {
            "awake": self.is_awake(now),
            "remaining": remaining,
            "scheduled": scheduled,
            "view": self.view,
            "views": [v["name"] for v in self._cfg["views"]],
            "error": self.error,
            "age": age,
            "departures": [d._asdict() for d in board.departures] if board else None,
            "messages": board.messages if board else [],
            "updated": board.updated if board else None,
            "charge": self.current_charge() if self._cfg["ring"] == "tram_charge" else None,
            "charge_error": self.charge_error if self._cfg["ring"] == "tram_charge" else None,
        }
