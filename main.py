#!/usr/bin/env python
#*-* coding: utf-8 *-*
#*-* vim: set ts=4,expandtab,autoindent *-*

import os
import time
import tempfile
import ConfigParser
import json
#import RPi.GPIO as GPIO

import festival
import morsewav
import pygame

os.environ["SDL_VIDEODRIVER"] = "dummy"

# FIXME: Add the rest of them. This was unavoidable
TIMEZONE_TAB = {
    'EST' : 'Eastern Standrd Time',
    'EDT' : 'Eastern Daylight Time',
    'UTC' : 'Universal Coordinated Time'
}

# Wait for COR input to assert:
# Assert PTT, start time-out timer, add interrupt for COR unasserted

# Play courtesy tone, reset time-out timer, wait hangtime before releasing PTT
#   ELSE
# If timer exceeded, time-out repeater and wait for timeout-duration before
# enabling again

# Set configuration values
config = ConfigParser.RawConfigParser()
config.read('repeater.cfg')

DEBUG = config.getboolean('repeater', 'debug')
COR_DIRECTION = config.get('repeater', 'cor-direction')
CALLSIGN = config.get('repeater', 'callsign')
CW_SPEED = config.get('repeater', 'cw-speed')
CW_FREQUENCY = config.get('repeater', 'cw-frequency')
VOICE_ID_ENABLED = config.getboolean('repeater', 'voice-id')
VOICE_ID_MESSAGE = config.get('repeater', 'voice-message')
ANNOUNCE_TIME_ENABLED = config.getboolean('repeater', 'announce-time')
ANNOUNCE_HOURS = json.loads(config.get('repeater', 'announce-hours'))
TIMEOUT_TIMER = config.getint('repeater', 'timeout-timer')
TIMEOUT_DURATION = config.getint('repeater', 'timeout-duration')
HANGTIME = config.getint('repeater', 'hangtime')
COURTESY_TONE_FILE = config.get('repeater', 'courtesy-tone')

# Temporary files to clean up afterwords:
cw_id_file = None

# Pygame sound clips
cw_id = None
courtesy_tone = None


# We use pygame for the sound mixer, timing functions, and event system
pygame.mixer.init()

# Generate and cache the audio files:
fd, cw_id_file = tempfile.mkstemp()
os.close(fd)
morsewav.generate(cw_id_file, CALLSIGN)
cw_id = pygame.mixer.Sound(cw_id_file)

if os.path.exists(COURTESY_TONE_FILE): 
    courtesy_tone = pygame.mixer.Sound(COURTESY_TONE_FILE)
elif os.path.exists('sounds/Beep.wav'):
    print("Warn: Courtesy tone file {0} not found, using Beep.wav instead" 
      .format(COURTESY_TONE_FILE))
    courtesy_tone = pygame.mixer.Sound('sounds/Beep.wav')
else:
    print("Error: Courtesy tone file {0} not found."
      .format(COURTESY_TONE_FILE))
    print("Additionally, sounds/Beep.wav was not found or is inaccessable.  Aborting.")
    cleanup()
    exit()

def main():
    # Event loop
    while True:
        for event in pygame.event.get():
            pass

# The with PTT() while nice, is blocking and won't work if we want to stop a 
# voice ID and replace it with a CW-ID.  Instead we can make an event that carries
# the PTT release time and just keep putting it back in the event queue until that
# time is reached.
def voiceid():
    # NOTE: festival.say() blocks, but pygame sounds do not
    with PTT():
       if VOICE_ID_ENABLED:
           festival.say(VOICE_ID_MESSAGE)
       if ANNOUNCE_TIME_ENABLED:
           saytime()
       courtesy_tone.play()
       pygame.time.delay(int((courtesy_tone.get_length() + HANGTIME) * 1000))

def saytime():
    festival.say(getTimeString())

def getTimeString():
    # Format time in a way the TTS engine understands better
    now = time.localtime()
    hour = now[3]
    minute = now[4]
    
    # Append timezone
    try:
        timezone = TIMEZONE_TAB[time.tzname[time.daylight]]
    except KeyError:
        # What is this witchcraft?
        timezone = TIMEZONE_TAB['UTC']

    if minute == 0:
        return "The time is {0} hours {1}".format(hour, timezone)
    else:
        return "The time is {0} {1} {2}".format(hour, minute, timezone)

def cleanup():
    # Cleanup temporary files
    os.unlink(cw_id_file)

class PTT(object):
    def __init__(self):
        pass

    def __enter__(self):
        if DEBUG: print("Debug: PTT on")
        # FIXME Engage PTT here

    def __exit__(self, *excinfo):
        if DEBUG: print("Debug: PTT off")
        # FIXME Disengage PTT here

if __name__ == '__main__':
    main()
