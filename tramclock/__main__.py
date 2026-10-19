"""Entry point: python3 -m tramclock --config /path/to/config.json"""

import argparse
import collections
import logging
import signal
import threading
import time

from .config import Config
from .controller import Controller
from .screen import Panel, Screen
from . import web

log = logging.getLogger("tramclock")



class RingHandler(logging.Handler):
    """Keeps recent log lines in memory for the web UI."""

    def __init__(self, size=200):
        super().__init__()
        self.lines = collections.deque(maxlen=size)

    def emit(self, record):
        self.lines.append(self.format(record))


def setup_logging(verbose):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    ring = RingHandler()
    ring.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(ring)
    root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return ring.lines


def start_touch(controller):
    try:
        from hyperpixel2r import Touch
    except ImportError:
        log.warning("hyperpixel2r library not installed; touch disabled")
        return None
    try:
        touch = Touch(bus=11, i2c_addr=0x15, interrupt_pin=27)
    except Exception as e:
        log.error("Touch init failed: %s", e)
        return None
    touch.on_touch(controller.on_touch)
    log.info("Touch ready")
    return touch


def display_loop(controller, screen, stop):
    """Redraw only when something visible changes; animate the ticker when awake."""
    awake = None
    last_key = None
    while not stop.is_set():
        now = time.time()
        is_awake = controller.is_awake(now)

        if is_awake != awake:
            awake = is_awake
            last_key = None
            if awake:
                controller.screen_turned_on()
            else:
                screen.blank()
                screen.set_backlight(False)

        if awake:
            cfg = controller.cfg
            screen.renderer.set_colour(cfg["colour"], cfg["ring_colours"][:cfg["ring_trams"]], cfg["charge_colour"])
            state = controller.display_state(now)
            board = state["board"]
            screen.renderer.set_ticker(
                "   •   ".join(board.messages) if board and cfg["show_messages"] else None
            )
            remaining = state["remaining"]
            key = (
                time.strftime("%H:%M", time.localtime(now)), int(now // 120),
                state["view_name"], board, state["error"], state["stale"], cfg["colour"], cfg["ring_colours"], cfg["ring_trams"], cfg["ring"],
                cfg["charge_colour"], state["charge"],
                None if remaining is None else int(remaining // 10),
            )
            if key != last_key:
                screen.draw(state, now)
                last_key = key
                screen.set_backlight(True)  # after the first frame is up
            elif screen.renderer.has_ticker:
                screen.draw_ticker()

            if screen.renderer.has_ticker:
                # Panel is 60 Hz: skip one vblank here, and show() waits for
                # the next, so the ticker moves an even 2 px every 33 ms.
                screen.skip_frame()
                controller.wait_for_change(0)
            else:
                controller.wait_for_change(1.0)
        else:
            controller.wait_for_change(5.0)


def main():
    parser = argparse.ArgumentParser(description="TfGM tram clock for HyperPixel 2.1 Round")
    parser.add_argument("--config", default="/home/pi/tram-clock/config.json")
    parser.add_argument("--port", type=int, help="override the web UI port")
    parser.add_argument("--no-panel", action="store_true", help="run without touching the display")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_lines = setup_logging(args.verbose)
    config = Config(args.config)
    controller = Controller(config)
    panel = None if args.no_panel else Panel()
    screen = Screen(panel)

    stop = threading.Event()

    def shutdown(signum, frame):
        log.info("Shutting down (signal %d)", signum)
        stop.set()
        controller.poke()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    web.serve(controller, screen, log_lines, args.port or config.get()["web_port"])
    threading.Thread(target=controller.fetcher, args=(stop,), name="fetcher", daemon=True).start()
    threading.Thread(target=controller.charge_fetcher, args=(stop,), name="charge", daemon=True).start()
    touch = start_touch(controller)  # noqa: F841 (keep a reference)

    # Flash the clock up on boot so it's obvious it started.
    controller.wake(1)
    try:
        display_loop(controller, screen, stop)
    finally:
        screen.blank()
        screen.set_backlight(False)
        if panel:
            panel.close()


if __name__ == "__main__":
    main()
