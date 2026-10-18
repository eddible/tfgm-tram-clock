"""Reads a single numeric sensor (e.g. car state of charge) from Home Assistant."""

import requests

TIMEOUT = 10


class HomeAssistantError(Exception):
    pass


def read_percent(url, token, entity):
    """Current value of `entity` as a float clamped to 0-100."""
    if not (url and token and entity):
        raise HomeAssistantError("Home Assistant not configured")
    try:
        r = requests.get(
            "{}/api/states/{}".format(url.rstrip("/"), entity),
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise HomeAssistantError("Can't reach Home Assistant: {}".format(e.__class__.__name__))
    if r.status_code == 401:
        raise HomeAssistantError("Home Assistant token rejected")
    if r.status_code == 404:
        raise HomeAssistantError("Home Assistant has no entity {}".format(entity))
    if not r.ok:
        raise HomeAssistantError("Home Assistant returned HTTP {}".format(r.status_code))
    try:
        value = float(r.json()["state"])
    except (ValueError, KeyError, TypeError):
        # "unavailable" / "unknown" while the car is offline
        raise HomeAssistantError("{} has no numeric value".format(entity))
    return max(0.0, min(100.0, value))
