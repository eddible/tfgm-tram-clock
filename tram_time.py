#!/usr/bin/env python3
import configparser
import logging
import os
import sys
import signal
import pygame
import time

from tfgm import TFGM

WAIT = "Wait"
CARRIAGES = "Carriages"
DEST = "Dest"


# Credit to Pimoroni for the Hyperpixel2r class
class Hyperpixel2r:
    screen = None

    def __init__(self):
        self._init_display()

        self.screen.fill((0, 0, 0))
        if self._rawfb:
            self._updatefb()
        else:
            pygame.display.update()

        # For some reason the canvas needs a 7px vertical offset
        # circular screens are weird...
        self.center = (240, 247)
        self._radius = 240

        # Distance of hour marks from center
        # self._marks = 220

        self._running = False
        self._origin = pygame.math.Vector2(*self.center)
        # self._clock = pygame.time.Clock()
        self._colour = (255, 0, 255)

    def _exit(self, sig, frame):
        self._running = False
        print("\nExiting!...\n")

    def _init_display(self):
        self._rawfb = False
        # Based on "Python GUI in Linux frame buffer"
        # http://www.karoltomala.com/blog/?p=679
        DISPLAY = os.getenv("DISPLAY")
        if DISPLAY:
            print("Display: {0}".format(DISPLAY))

        if os.getenv("SDL_VIDEODRIVER"):
            print(
                "Using driver specified by SDL_VIDEODRIVER: {}".format(
                    os.getenv("SDL_VIDEODRIVER")
                )
            )
            pygame.display.init()
            size = (pygame.display.Info().current_w, pygame.display.Info().current_h)
            if size == (480, 480):  # Fix for 480x480 mode offset
                size = (640, 480)
            self.screen = pygame.display.set_mode(
                size,
                pygame.FULLSCREEN
                | pygame.DOUBLEBUF
                | pygame.NOFRAME
                | pygame.HWSURFACE,
            )
            return

        else:
            # Iterate through drivers and attempt to init/set_mode
            for driver in ["rpi", "kmsdrm", "fbcon", "directfb", "svgalib"]:
                os.putenv("SDL_VIDEODRIVER", driver)
                try:
                    pygame.display.init()
                    size = (
                        pygame.display.Info().current_w,
                        pygame.display.Info().current_h,
                    )
                    if size == (480, 480):  # Fix for 480x480 mode offset
                        size = (640, 480)
                    self.screen = pygame.display.set_mode(
                        size,
                        pygame.FULLSCREEN
                        | pygame.DOUBLEBUF
                        | pygame.NOFRAME
                        | pygame.HWSURFACE,
                    )
                    print(
                        "Using driver: {0}, Framebuffer size: {1:d} x {2:d}".format(
                            driver, *size
                        )
                    )
                    return
                except pygame.error as e:
                    print('Driver "{0}" failed: {1}'.format(driver, e))
                    continue
                break

        print("All SDL drivers failed, falling back to raw framebuffer access.")
        self._rawfb = True
        os.putenv("SDL_VIDEODRIVER", "dummy")
        pygame.display.init()  # Need to init for .convert() to work
        self.screen = pygame.Surface((480, 480))

    def __del__(self):
        "Destructor to make sure pygame shuts down, etc."

    def _updatefb(self):
        fbdev = os.getenv("SDL_FBDEV", "/dev/fb0")
        with open(fbdev, "wb") as fb:
            fb.write(self.screen.convert(16, 0).get_buffer())

    def blit_screen(self, items):
        spacer = 70
        top_row = 120
        l = 50
        r = top_row
        row = 0
        for item in items:
            if item["item"]:
                self.screen.blit(item["item"], (l, r))
                r += spacer
                row += 1
                if row == 4:
                    row = 0
                    l = 270
                    r = top_row


    def setup_fonts(self):
        pygame.font.init()
        # Credit for this font: https://github.com/chrisys/train-departure-display/tree/main/src/fonts

        # The number here will change the font size
        game_font = pygame.font.Font("fonts/train-font.ttf", 60)
        font_colour = (250, 250, 0)

        return game_font, font_colour

    def run(self):
        # Read the config file to get your API token and tram line
        config = get_config()
        ml = TFGM(config['api_key'], config['tram_line'])

        # Configure the font and colour
        game_font, font_colour = self.setup_fonts()

        self._running = True
        signal.signal(signal.SIGINT, self._exit)

        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False
                    break
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self._running = False
                        break
            display_times(ml, game_font, font_colour)
            if self._rawfb:
                self._updatefb()
            else:
                pygame.display.flip()
                # Time to wait until checking for the Metrolink API again
                time.sleep(30)
        pygame.quit()
        sys.exit(0)


def display_times(ml, game_font, font_colour):
    display.screen.fill((0, 0, 0))
    # Change this to your Metrolink line number
    statuses = ml.get_tram_status()

    if statuses:
        next_tram_wait = check_for_value(statuses[0], WAIT)
        second_tram_wait = check_for_value(statuses[1], WAIT)

        next_tram_size = check_for_value(statuses[0], CARRIAGES)
        second_tram_size = check_for_value(statuses[1], CARRIAGES)

        next_tram_dest = check_for_value(statuses[0], DEST)
        second_tram_dest = check_for_value(statuses[1], DEST)

        # This is how long the strings are shortened to so they fit on the screen. 7 is a reasonable compromise.
        # It would be great to make them scroll in the allocated space, but I haven't figured out how to do this yet!
        truncate = 7

        if len(second_tram_dest) > truncate:
            second_tram_dest = second_tram_dest[:truncate] + ".."
        if len(next_tram_dest) > truncate:
            print(len(next_tram_dest))
            print(next_tram_dest)
            next_tram_dest = next_tram_dest[:truncate] + ".."

        next_tram_header = render_font(game_font, "Next", font_colour)
        second_tram_header = render_font(game_font, "2nd", font_colour)

        next_tram_dest = render_font(game_font, next_tram_dest, font_colour)
        second_tram_dest = render_font(game_font, second_tram_dest, font_colour)

        next_tram = render_font(
            game_font, next_tram_wait + min_or_mins(next_tram_wait), font_colour
        )
        second_tram = render_font(
            game_font, second_tram_wait + min_or_mins(second_tram_wait), font_colour
        )

        # This reduces the size of the tram icon to fit nicely.
        scale_factor = 8

        # Credit for the icon source: https://www.flaticon.com/free-icons/train
        single_tram_img = pygame.image.load("imgs/single-tram.png")
        single_tram_img = pygame.transform.scale(
            single_tram_img, (512 / scale_factor, 367 / scale_factor)
        )

        double_tram_img = pygame.image.load("imgs/double-tram.png")
        double_tram_img = pygame.transform.scale(
            double_tram_img, (1050 / scale_factor, 367 / scale_factor)
        )

        if next_tram_size == "Single":
            next_tram_image = single_tram_img
        elif next_tram_size == "Double":
            next_tram_image = double_tram_img
        else:
            next_tram_image = None

        if second_tram_size == "Single":
            second_tram_image = single_tram_img
        elif second_tram_size == "Double":
            second_tram_image = double_tram_img
        else:
            second_tram_image = None

        display.blit_screen(
            [
                {"item": next_tram_header, "type": "text"},
                {"item": next_tram_dest, "type": "text"},
                {"item": next_tram, "type": "text"},
                {"item": next_tram_image, "type": "image"},
                {"item": second_tram_header, "type": "text"},
                {"item": second_tram_dest, "type": "text"},
                {"item": second_tram, "type": "text"},
                {"item": second_tram_image, "type": "image"},
            ]
        )

        return True
    else:
        return False


def min_or_mins(wait_time):
    if wait_time:
        if int(wait_time) > 1:
            return " mins"
        else:
            return " min"
    else:
        return wait_time


def check_for_value(obj, key):
    return obj[key] if key in obj else "?"


def render_font(font, text, font_colour, bold=False):
    return font.render(text, bold, font_colour)


def get_config():
    config = configparser.ConfigParser()
    config.read("config.ini")
    if "TFGM-CONFIG" not in config:
        logging.info("No config file found, or badly formatted.")
        return {}
    config_options = ["api_key", "tram_line"]
    configured_values = {}
    for option in config_options:
        configured_value = config["TFGM-CONFIG"][f"{option}"]
        if not configured_value:
            logging.error(f"Missing {option} from config file, but it is required.")
            sys.exit()
        else:
            configured_values[option] = configured_value.strip()

    return configured_values


display = Hyperpixel2r()
display.run()
