"""HyperPixel 2.1 Round output: backlight, framebuffer and drawing."""

import fcntl
import io
import logging
import math
import mmap
import os
import struct
import threading
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

log = logging.getLogger(__name__)

SIZE = 480
CENTRE = SIZE // 2
ASSETS = os.path.join(os.path.dirname(__file__), "assets")
BACKLIGHT = "/sys/class/backlight/rpi_backlight/brightness"
KDSETMODE, KD_TEXT, KD_GRAPHICS = 0x4B3A, 0, 1
FBIO_WAITFORVSYNC = 0x40044620
FB_MASKS = {32: (0xFF0000, 0x00FF00, 0x0000FF), 16: (0xF800, 0x07E0, 0x001F)}

# Nudge everything by a pixel or two every few minutes so no pixel holds
# exactly the same value for the whole wake period.
ORBIT = [(0, 0), (2, 1), (1, -2), (-2, -1), (-1, 2)]
ORBIT_SECONDS = 120

# The panel shows banding on anything that isn't a fully-lit pixel, so draw
# everything in exactly one colour with no anti-aliasing or dimming.
ERROR_COLOUR = (255, 0, 0)
# The ring is 60 dot positions, one per minute. Dots are told apart by size,
# not brightness. Dots rather than a line: a thin circle without
# anti-aliasing looks jagged, and dots match the dot-matrix font.
RING_RADIUS = 231
DOT_SMALL, DOT_MARK, DOT_LEAD, DOT_HOUR = 2, 4, 3, 7

TICKER_TOP, TICKER_HEIGHT = 338, 40
TICKER_STEP = 2  # px per frame; at 30 fps that's 60 px/s
TICKER_LEFT, TICKER_RIGHT = 60, 420  # visible chord of the circle at ticker height

# Shorter names so destinations fit at a readable size.
SHORT_NAMES = {
    "Manchester Airport": "Airport",
    "Deansgate-Castlefield": "Deansgate",
    "Ashton-under-Lyne": "Ashton",
    "Rochdale Town Centre": "Rochdale",
    "Victoria Station": "Victoria",
    "Piccadilly Station": "Piccadilly",
    "The Trafford Centre": "Trafford Ctr",
    "Exchange Square": "Exchange Sq",
    "Wythenshawe Town Centre": "Wythenshawe",
}


class Panel:
    """The physical display: backlight switch plus a memory-mapped framebuffer."""

    def __init__(self, fbdev="/dev/fb0", tty="/dev/tty1"):
        name = os.path.basename(fbdev)
        with open("/sys/class/graphics/{}/bits_per_pixel".format(name)) as f:
            self.bpp = int(f.read())
        with open("/sys/class/graphics/{}/stride".format(name)) as f:
            self.stride = int(f.read())
        if self.bpp not in (16, 32):
            raise RuntimeError("Unsupported framebuffer depth {}".format(self.bpp))
        self._fb = open(fbdev, "r+b")
        self._mem = mmap.mmap(self._fb.fileno(), self.stride * SIZE)
        self._tty = None
        try:
            # Stop the text console (login prompt, cursor, kernel messages)
            # drawing over us.
            self._tty = open(tty, "w")
            fcntl.ioctl(self._tty, KDSETMODE, KD_GRAPHICS)
        except OSError as e:
            log.warning("Could not put %s into graphics mode: %s", tty, e)
        self.backlight = None
        self._vsync = True

    def wait_vsync(self):
        """Block until the next vertical blank, so writes don't tear mid-scan."""
        if not self._vsync:
            return
        try:
            fcntl.ioctl(self._fb, FBIO_WAITFORVSYNC, struct.pack("I", 0))
        except OSError as e:
            log.warning("vsync unavailable, updates may tear: %s", e)
            self._vsync = False

    def set_backlight(self, on):
        if on == self.backlight:
            return
        try:
            with open(BACKLIGHT, "w") as f:
                f.write("1" if on else "0")
        except OSError as e:
            log.error("Backlight control failed: %s", e)
        self.backlight = on
        log.info("Backlight %s", "on" if on else "off")

    def _pixels(self, surface):
        """Raw bytes in the framebuffer's layout (XRGB8888 or RGB565) and the row length."""
        masks = FB_MASKS[self.bpp]
        if surface.get_bitsize() != self.bpp or surface.get_masks()[:3] != masks:
            native = pygame.Surface(surface.get_size(), 0, self.bpp, masks + (0,))
            native.blit(surface, (0, 0))
            surface = native
        return surface.get_buffer().raw, surface.get_pitch()

    def show(self, surface, top=0):
        """Copy surface onto the framebuffer starting at row `top`."""
        data, pitch = self._pixels(surface)
        row = surface.get_width() * self.bpp // 8
        if pitch != row:
            data = b"".join(data[y * pitch:y * pitch + row] for y in range(surface.get_height()))
        # Copying takes well under a scan-out, so starting at vblank keeps
        # us ahead of the beam.
        self.wait_vsync()
        if row == self.stride:
            self._mem[top * self.stride:top * self.stride + len(data)] = data
        else:
            for y in range(surface.get_height()):
                at = (top + y) * self.stride
                self._mem[at:at + row] = data[y * row:(y + 1) * row]

    def close(self):
        self._mem.close()
        self._fb.close()
        if self._tty:
            try:
                fcntl.ioctl(self._tty, KDSETMODE, KD_TEXT)
            except OSError:
                pass
            self._tty.close()


def _hex(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


class Renderer:
    """Draws a 480x480 frame. Knows nothing about hardware or timing."""

    def __init__(self):
        pygame.font.init()
        self._dot_cache = {}
        self._dot_path = os.path.join(ASSETS, "train-font.ttf")
        self.colour = _hex("#FFB000")
        self._ticker_text = None
        self._ticker_surface = None
        self._ticker_pos = 0

    def dot(self, size):
        if size not in self._dot_cache:
            self._dot_cache[size] = pygame.font.Font(self._dot_path, size)
        return self._dot_cache[size]

    def set_colour(self, colour):
        rgb = _hex(colour)
        if rgb != self.colour:
            self.colour = rgb
            self._ticker_text = None  # re-render the ticker in the new colour

    # -- helpers -----------------------------------------------------------

    def _fit(self, text, max_width, sizes=(46, 42, 38, 34), colour=None):
        """Render text in the largest size that fits, else truncate."""
        return self._fit_sized(text, max_width, sizes, colour)[0]

    def _fit_sized(self, text, max_width, sizes=(46, 42, 38, 34), colour=None):
        """Like _fit, but also returns the font used."""
        for size in sizes:
            surf = self._text(text, size, colour)
            if surf.get_width() <= max_width:
                return surf, self.dot(size)
        font = self.dot(sizes[-1])
        while len(text) > 1 and font.size(text + "..")[0] > max_width:
            text = text[:-1]
        return font.render(text.rstrip() + "..", False, colour or self.colour), font

    def _text(self, text, size, colour=None):
        return self.dot(size).render(text, False, colour or self.colour)

    def _centred(self, surf, target, y, dx):
        target.blit(surf, (CENTRE - surf.get_width() // 2 + dx, y))

    @staticmethod
    def _carriages(target, x, top, height, count, colour):
        """One bar per carriage, sitting on the baseline."""
        for i in range(count):
            pygame.draw.rect(target, colour, (x + i * 8, top, 5, height))

    @staticmethod
    def _dot(target, colour, minute, size, dx, dy):
        """Dot at `minute` (0-60, may be fractional) clockwise from 12 o'clock."""
        a = 2 * math.pi * minute / 60.0
        centre = (round(CENTRE + dx + RING_RADIUS * math.sin(a)), round(CENTRE + dy - RING_RADIUS * math.cos(a)))
        pygame.draw.circle(target, colour, centre, size)

    def _ring_clock(self, target, now, dx, dy):
        """Analogue clock: hour markers, minutes filling clockwise, a big hour dot."""
        t = time.localtime(now)
        for m in range(1, t.tm_min + 1):
            if m % 5:
                self._dot(target, self.colour, m, DOT_LEAD if m == t.tm_min else DOT_SMALL, dx, dy)
        for m in range(0, 60, 5):
            self._dot(target, self.colour, m, DOT_MARK, dx, dy)
        hour = (t.tm_hour % 12 + t.tm_min / 60.0) * 5
        self._dot(target, self.colour, hour, DOT_HOUR, dx, dy)

    def _ring_next_tram(self, target, board, dx, dy):
        """One big dot per minute until the next tram, over small 5-minute ticks."""
        wait = board.departures[0].wait if board and board.departures else 0
        for m in range(0, 60, 5):
            self._dot(target, self.colour, m, DOT_SMALL, dx, dy)
        for m in range(min(wait, 60)):
            self._dot(target, self.colour, m, DOT_MARK, dx, dy)

    # -- frame -------------------------------------------------------------

    def frame(self, state, now=None):
        """state: dict with view_name, board, error, stale, remaining, total, now."""
        now = now or time.time()
        dx, dy = ORBIT[int(now // ORBIT_SECONDS) % len(ORBIT)]
        surf = pygame.Surface((SIZE, SIZE))
        surf.fill((0, 0, 0))
        clock = self._text(time.strftime("%H:%M", time.localtime(now)), 40)
        self._centred(clock, surf, 30 + dy, dx)
        self._centred(self._fit(state["view_name"].upper(), 340, (30, 26, 22)), surf, 80 + dy, dx)
        pygame.draw.line(surf, self.colour, (70 + dx, 122 + dy), (410 + dx, 122 + dy), 2)

        board = state.get("board")
        if board and board.departures:
            for i, dep in enumerate(board.departures[:3]):
                y = 138 + i * 66 + dy
                wait = "Now" if dep.wait == 0 else "{} min".format(dep.wait)
                wait_surf = self.dot(46).render(wait, False, self.colour)
                surf.blit(wait_surf, (440 - wait_surf.get_width() + dx, y))
                cars = 2 if dep.carriages == "Double" else 1
                # Bars span the x-height: from the top of lowercase letters to the baseline.
                row_font = self.dot(46)
                x_top = row_font.render("x", False, self.colour).get_bounding_rect().top
                self._carriages(surf, 40 + dx, y + x_top, row_font.get_ascent() - x_top, cars, self.colour)
                dest, font = self._fit_sized(SHORT_NAMES.get(dep.dest, dep.dest), 440 - wait_surf.get_width() - 24 - 66)
                # Shrunk names share the baseline with the minutes.
                surf.blit(dest, (66 + dx, y + row_font.get_ascent() - font.get_ascent()))
        else:
            if board is not None:
                msg = "No trams due"
            elif state.get("error"):
                msg = "No data"
            else:
                msg = "Loading..."
            self._centred(self.dot(46).render(msg, False, self.colour), surf, 190 + dy, dx)

        if state.get("error"):
            err = self._fit(state["error"], 380, (24, 20), ERROR_COLOUR)
            self._centred(err, surf, 300 + dy, dx)

        if state.get("stale"):
            self._centred(self._text("data may be out of date", 24), surf, 398 + dy, dx)
        elif state.get("remaining") is not None:
            mins = int(math.ceil(state["remaining"] / 60.0))
            label = "screen off in {} min".format(mins) if not state.get("scheduled") else "scheduled"
            self._centred(self._text(label, 24), surf, 398 + dy, dx)

        ring = state.get("ring", "clock")
        if ring == "clock":
            self._ring_clock(surf, now, dx, dy)
        elif ring == "next_tram":
            self._ring_next_tram(surf, board, dx, dy)

        return surf

    # -- message ticker ----------------------------------------------------

    def set_ticker(self, text):
        text = text or None
        if text == self._ticker_text:
            return
        self._ticker_text = text
        self._ticker_pos = 0
        self._ticker_surface = (
            self._text(text, 28) if text else None
        )

    @property
    def has_ticker(self):
        return self._ticker_surface is not None

    def draw_ticker(self, target, advance=False):
        """Draw the ticker onto target, touching only its own strip.

        Advances by a fixed step per frame (not by wall time) so the motion
        stays even even when a frame is late.
        """
        if not self._ticker_surface:
            return
        target.set_clip(pygame.Rect(TICKER_LEFT, TICKER_TOP, TICKER_RIGHT - TICKER_LEFT, TICKER_HEIGHT))
        target.fill((0, 0, 0))
        lap = self._ticker_surface.get_width() + TICKER_RIGHT - TICKER_LEFT
        if advance:
            self._ticker_pos = (self._ticker_pos + TICKER_STEP) % lap
        x = TICKER_RIGHT - self._ticker_pos
        target.blit(self._ticker_surface, (x, TICKER_TOP + (TICKER_HEIGHT - self._ticker_surface.get_height()) // 2))
        target.set_clip(None)


class Screen:
    """Owns the panel and renderer and remembers the last frame for previews."""

    def __init__(self, panel=None):
        self.panel = panel
        self.renderer = Renderer()
        self._lock = threading.Lock()
        self._frame = pygame.Surface((SIZE, SIZE))
        self._frame.fill((0, 0, 0))

    def draw(self, state, now):
        surf = self.renderer.frame(state, now)
        self.renderer.draw_ticker(surf)
        with self._lock:
            self._frame = surf
        if self.panel:
            self.panel.show(surf)

    def draw_ticker(self):
        with self._lock:
            self.renderer.draw_ticker(self._frame, advance=True)
            band = self._frame.subsurface((0, TICKER_TOP, SIZE, TICKER_HEIGHT)).copy()
        if self.panel:
            self.panel.show(band, TICKER_TOP)

    def skip_frame(self):
        """Let one vblank pass, so ticker frames land on every second one (30 fps)."""
        if self.panel:
            self.panel.wait_vsync()
        else:
            time.sleep(1 / 60.0)

    def blank(self):
        if self.panel:
            black = pygame.Surface((SIZE, SIZE))
            black.fill((0, 0, 0))
            self.panel.show(black)

    def set_backlight(self, on):
        if self.panel:
            self.panel.set_backlight(on)

    def png(self):
        with self._lock:
            surf = self._frame.copy()
        buf = io.BytesIO()
        pygame.image.save(surf, buf, "preview.png")
        return buf.getvalue()
