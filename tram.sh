#!/bin/sh
cd /home/pi/tfgm-tram-clock
sudo SDL_FBDEV=/dev/fb0 python3 tram_time.py &
