"""Client for the TfGM Metrolink departures API (https://developer.tfgm.com)."""

import time
from collections import namedtuple

import requests

BASE_URL = "https://api.tfgm.com/odata/Metrolinks"
TIMEOUT = 10

Departure = namedtuple("Departure", "dest wait carriages status")
Board = namedtuple("Board", "departures messages updated")
Platform = namedtuple("Platform", "pid station direction line dests")


class TfGMError(Exception):
    pass


class TfGM:
    def __init__(self, api_key):
        self._session = requests.Session()
        self.set_key(api_key)
        self._platforms = None
        self._platforms_at = 0

    def set_key(self, api_key):
        self._session.headers["Ocp-Apim-Subscription-Key"] = api_key
        self._platforms = None

    def _get(self, params=None):
        try:
            r = self._session.get(BASE_URL, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise TfGMError("Network error: {}".format(e.__class__.__name__))
        if r.status_code in (401, 403):
            raise TfGMError("API key rejected")
        if r.status_code == 429:
            raise TfGMError("Rate limited by TfGM")
        if not r.ok:
            raise TfGMError("TfGM returned HTTP {}".format(r.status_code))
        try:
            return r.json()["value"]
        except (ValueError, KeyError):
            raise TfGMError("Unexpected response from TfGM")

    def board(self, pids):
        """Departures across one or more platforms, soonest first."""
        if not pids:
            return Board([], [], None)
        query = " or ".join("PIDREF eq '{}'".format(p.replace("'", "''")) for p in pids)
        rows = self._get({"$filter": query})
        if not rows:
            raise TfGMError("No data for {}".format(", ".join(pids)))

        departures, messages, updated = [], [], None
        for row in rows:
            for i in range(4):
                dest = (row.get("Dest%d" % i) or "").strip()
                wait = (row.get("Wait%d" % i) or "").strip()
                status = row.get("Status%d" % i) or ""
                # A departing tram can't be caught, and during closures the
                # signs leave a phantom "Departing" entry up all day.
                if not dest or not wait.isdigit() or status == "Departing":
                    continue
                departures.append(Departure(
                    dest=dest,
                    wait=int(wait),
                    carriages=row.get("Carriages%d" % i) or "",
                    status=status,
                ))
            msg = (row.get("MessageBoard") or "").strip()
            if msg and msg not in messages and not msg.startswith("<no message>"):
                messages.append(msg)
            updated = max(updated or "", row.get("LastUpdated") or "") or None
        departures.sort(key=lambda d: d.wait)
        return Board(departures, messages, updated)

    def platforms(self, max_age=3600):
        """Every platform on the network, for the stop picker. Cached."""
        if self._platforms is None or time.time() - self._platforms_at > max_age:
            rows = self._get()
            self._platforms = sorted(
                (Platform(
                    pid=r["PIDREF"],
                    station=r.get("StationLocation", ""),
                    direction=r.get("Direction", ""),
                    line=r.get("Line", ""),
                    dests=sorted({r.get("Dest%d" % i) for i in range(4)} - {"", None}),
                ) for r in rows if r.get("PIDREF")),
                key=lambda p: (p.station, p.pid),
            )
            self._platforms_at = time.time()
        return self._platforms
