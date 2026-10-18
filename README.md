# Tram Clock

Live Manchester Metrolink departures on a Raspberry Pi Zero W with a
Pimoroni HyperPixel 2.1 Round touchscreen.

The screen is **off by default** (backlight off, black frame) to avoid the
image retention / ghosting you get from leaving an LCD on a static image.

## Using it

| On the screen        | Does                                         |
|----------------------|----------------------------------------------|
| Tap while off        | Turn on for `wake_minutes` (default 30)      |
| Tap while on         | Next stop, and restart the timer             |
| Hold 1.5 s           | Turn off now                                 |

The ring around the edge shrinks as the on-time runs out. Departure data is
only fetched while the screen is on.

**Web UI:** `http://<pi-address>/` lets you turn the screen on or off,
switch stops, see a live preview, pick stops by searching station names, set
an auto-on schedule (e.g. weekdays 07:30–08:30), change the colour and
timings, and read the recent log.

## Layout

```
tramclock/
  __main__.py    entry point, display loop
  controller.py  wake/sleep state, schedule, touch gestures, fetcher thread
  screen.py      backlight, framebuffer, drawing
  tfgm.py        TfGM API client
  web.py         web UI + JSON API
  config.py      config.json load/validate/save
  static/        web UI page
  assets/        fonts
```

Settings live in `config.json` (not in git, holds the API key). Get a key from
<https://developer.tfgm.com>.

## Install / update

```sh
./install.sh                       # installs + starts tramclock.service
sudo journalctl -u tramclock -f    # logs
python3 -m tramclock --no-panel --port 8080 --config config.json  # dev run
```

`install.sh` also disables the tty1 login prompt (it draws over the
framebuffer) and comments out the old `tram.sh` line in `/etc/rc.local`.
SSH is unaffected.
