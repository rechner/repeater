#!/usr/bin/env python
#*-* coding: utf-8 *-*
#*-* vim: set ts=4,expandtab,autoindent *-*

import os
import time
import tempfile
import ConfigParser
import json
import RPi.GPIO as GPIO
import subprocess
from fcntl import fcntl, F_GETFL, F_SETFL

import festival
import morsewav
import pygame
from pygame.locals import *
import pyotp

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
ID_PERIOD = config.getint('repeater', 'id-period')
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
pygame.init()

# Generate and cache the audio files:
fd, cw_id_file = tempfile.mkstemp()
os.close(fd)
# FIXME: Why did this stop working? Worked on my laptop?
#morsewav.generate(cw_id_file, CALLSIGN)
import subprocess
subprocess.call(['python', 'morsewav.py', '-a', '15000', '-o', cw_id_file, CALLSIGN])

cw_id = pygame.mixer.Sound(cw_id_file)

if os.path.exists(COURTESY_TONE_FILE):
    courtesy_tone = pygame.mixer.Sound(COURTESY_TONE_FILE)
    repeater_down_tone = pygame.mixer.Sound('sounds/3down.wav')
    repeater_up_tone = pygame.mixer.Sound('sounds/3up.wav')
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

Repeater_enabled = True  # Disabled during time-out conditions
PTT_state = 0
PTT_timer = -1
PTT_recovery_timer = -1
PTT_hanging = False  # True when COR is inactive but repeater is still keyed up
ID_wait_flag = False # Set when the repeater gets used and reset after ID'ing
Last_ID_time = -1 # UNIX timestamp of last time repeater ID'ed

# Set up the GPIO's (BCM pinout):
COR_PIN = 27
PTT_PIN = 22
RICK_KNOCKDOWN_PIN = 17
POWER_SENSE_PIN = 16
RX_MUTE_PIN = 24

GPIO.setmode(GPIO.BCM)
GPIO.setup(PTT_PIN, GPIO.OUT)
GPIO.setup(RX_MUTE_PIN, GPIO.OUT)
GPIO.setup(RICK_KNOCKDOWN_PIN, GPIO.OUT)
GPIO.setup(COR_PIN, GPIO.IN, GPIO.PUD_UP)
GPIO.setup(POWER_SENSE_PIN, GPIO.IN, GPIO.PUD_UP)

GPIO.output(PTT_PIN, 0)
GPIO.output(RX_MUTE_PIN, 0)
GPIO.output(RICK_KNOCKDOWN_PIN, 0)

# PyGame events:
TIMEOUT_TIMER_EVENT = USEREVENT+1
TIMEOUT_RECOVERY_EVENT = USEREVENT+2
HANGTIME_RELEASE_PTT_EVENT = USEREVENT+3
CW_ID_EVENT = USEREVENT+4

multimon_process = None
dtmf_queue = []

def main():
    global PTT_timer
    global Repeater_enabled

    global PTT_state
    global PTT_timer
    global PTT_recovery_timer
    global ID_wait_flag
    global PTT_hanging
    global dtmf_queue
    
    setup_multimon()
    totp = pyotp.TOTP('5ZEAFI6HRJZOQ52T')

    print "Repeater started"
    courtesy_tone.play()

    # Event loop
    while True:
        for event in pygame.event.get():
            if event.type == TIMEOUT_RECOVERY_EVENT:
                # Check that noone is transmitting
                if GPIO.input(COR_PIN) == 1:
                    pygame.time.set_timer(TIMEOUT_RECOVERY_EVENT, 0)
                    print "Timeout cleared"
                    ptt(True)
                    festival.say("Time out clear")
                    if not check_id_period():
                        ptt(False)
                    Repeater_enabled = True
                else:
                    print "WARN: COR still active, skipping until fault is cleared"

            # Skip the rest of event processing if repeater is timed-out
            if not Repeater_enabled:
                continue
            # Events below here are repeater-only
            # and can be disabled under certain conditions.
            # ============================================

            # Timing events fire once per second (1000 ms)
            if event.type == TIMEOUT_TIMER_EVENT:
                PTT_timer -= 1

            if event.type == HANGTIME_RELEASE_PTT_EVENT:
                pygame.time.set_timer(HANGTIME_RELEASE_PTT_EVENT, 0)
                PTT_hanging = False
                ptt(False)
            if event.type == CW_ID_EVENT:
                if DEBUG: print "CW_ID_EVENT triggered"
                pygame.time.set_timer(CW_ID_EVENT, 0)
                # Only play courtesy tone and release if no RX
                if GPIO.input(COR_PIN) == 1:
                    if DEBUG: print "COR is inactive, playing courtesy tone"
                    courtesy_tone.play()
                    pygame.time.delay(int(courtesy_tone.get_length()) * 1000)
                    # Set hangtime and event to disable PTT:
                    pygame.time.set_timer(HANGTIME_RELEASE_PTT_EVENT, HANGTIME*1000)

        COR_State = GPIO.input(COR_PIN)

        if Repeater_enabled:
            if COR_State == 0 and not PTT_state: # Active low after JFET
                if DEBUG: print "RX active, start TOT timer"
                # Engage PTT
                ptt(True)
                PTT_hanging = False

                # Start TOT timer
                PTT_timer = TIMEOUT_TIMER
                pygame.time.set_timer(TIMEOUT_TIMER_EVENT, 1000)

                # Reset activity flag
                ID_wait_flag = True

            # User has stopped transmitting:
            if COR_State == 1 and PTT_state and not PTT_hanging:
                if DEBUG: print "RX release, TOT timer reset: {0}s was remaining".format(PTT_timer)
                PTT_hanging = True

                # Stop TOT timer event and reset timer
                pygame.time.set_timer(TIMEOUT_TIMER_EVENT, 0)
                PTT_timer = -1

                # Polite CW-ID if needed
                ID_played = check_id_period()
                if not ID_played:
                    # Play courtesy tone (otherwise set in CW_ID_EVENT)
                    courtesy_tone.play()
                    # Block for courtesy tone duration
                    pygame.time.delay(int(courtesy_tone.get_length()) * 1000)

                    # Set timer for hangtime duration and to release PTT
                    pygame.time.set_timer(HANGTIME_RELEASE_PTT_EVENT, HANGTIME*1000)


            if PTT_timer == 0:
                # Stop the TOT timer event and reset timer
                pygame.time.set_timer(TIMEOUT_TIMER_EVENT, 0)
                PTT_timer = -1

                # Mute RX audio and play time-out message
                RX_audio_enable(False)
                festival.say("Time out")
                RX_audio_enable(True)

                # Release PTT
                ptt(False)

                # Start time-out recovery timer
                pygame.time.set_timer(TIMEOUT_RECOVERY_EVENT, TIMEOUT_DURATION*1000)

                # Disable repeater
                Repeater_enabled = False

            if COR_State == 1 and not PTT_state and not PTT_hanging:
                check_id_period()

        #pygame.time.tick(30)
        dtmf_digits = process_dtmf()
        if dtmf_digits is not None:
            dtmf_queue.extend(dtmf_digits)

        if ''.join(dtmf_queue[-8:]) == '**{0}'.format(totp.now()):
            print "Access code entered correctly"
            dtmf_queue = []

            # Toggle repeater state
            ptt(True)
            if Repeater_enabled: 
                festival.say(CALLSIGN + " REPEATER DISABLED")
                repeater_down_tone.play()
                pygame.time.delay(int((repeater_down_tone.get_length()) * 1000))
                Repeater_enabled = False
            else:
                festival.say(CALLSIGN + " REPEATER ENABLED")
                repeater_up_tone.play()
                pygame.time.delay(int((repeater_up_tone.get_length()) * 1000))
                Repeater_enabled = True
                
            ptt(False)

        print dtmf_queue
        pygame.time.delay(100)

# The with PTT() while nice, is blocking and won't work if we want to stop a
# voice ID and replace it with a CW-ID.  Instead we can make an event that carries
# the PTT release time and just keep putting it back in the event queue until that
# time is reached.

def ptt(state):
    assert type(state) is bool, "parameter must be boolean type"
    global PTT_state
    PTT_state = state
    GPIO.output(PTT_PIN, state)

# Mute or unmute the incoming RX audio
def RX_audio_enable(state):
    assert type(state) is bool, "parameter must be boolean type"
    GPIO.output(RX_MUTE_PIN, not state)

def check_id_period():
    global Last_ID_time
    global ID_wait_flag
    now = time.time()
    if (now - ID_PERIOD) >= Last_ID_time:
        if DEBUG: print "ID period exceeded, last ID was {0}".format( Last_ID_time)
        # ID period exceeded, we probably need to do it.
        # Assert PTT if needed
        old_PTT_state = PTT_state
        if PTT_state == False:
            ptt(True)

        # Has the repeater been used since the last ID?
        if ID_wait_flag:
            # Use a polite CW-id
            cw_id.play()

            # Wait for ID to play and release PTT if needed:
            if old_PTT_state:
                # Let caller handle courtesy tone and PTT
                pygame.time.set_timer(CW_ID_EVENT, (int(cw_id.get_length()+1) * 1000))
            else:
                # This is a tail ID: block for duration and stop TX
                pygame.time.delay(int(cw_id.get_length()+HANGTIME) * 1000)
                ptt(False)
        else:
            # Use a short voice ID This blocks so we can't interrupt it
            festival.say(CALLSIGN)  # Maybe cache to .wav instead?

            # Release PTT if needed:
            if not old_PTT_state:
                ptt(False)

        # Reset ID flag and timestamp
        ID_wait_flag = False
        Last_ID_time = now

        return True
    return False


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
    time_string = getTimeString()
    if DEBUG: print "TTS: " + time_string
    festival.say(time_string)

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
        return "The time is {0}:{1} {2}".format(hour, minute, timezone)

def setup_multimon():
    global multimon_process
    global audio_fifo
	
    # Create FIFO
    audio_fifo = '/tmp/repeater_audio.fifo'
    os.mkfifo(audio_fifo)
    
    # Run aplay to fill FIFO with PCM sound from the soundcard
    subprocess.Popen("arecord -r 22050 -t wav > {0}".format(audio_fifo), shell=True)
    
    # Start multimon in a non-blocking fashion
    # (Consult http://eyalarubas.com/python-subproc-nonblock.html)
    multimon_process = subprocess.Popen(["multimon-ng", "-a", "DTMF", "-t", "wav", audio_fifo], 
        shell=False,
        stdin = subprocess.PIPE,
        stdout = subprocess.PIPE,
        stderr = subprocess.PIPE)
		
    flags = fcntl(multimon_process.stdout,F_GETFL)
    fcntl(multimon_process.stdout,F_SETFL,flags|os.O_NONBLOCK)

def process_dtmf():
    # Fetch any decoded values from the multimon process:
    raw_out = None
    try:
        raw_out = os.read(multimon_process.stdout.fileno(), 1024)
    except OSError as e:
        #print "WARN: No more data from multimon"
        #print e
        return None

    digits = []
    if raw_out is not None:
        # Parse the decoded digits
        lines = raw_out.split('\n')
        for packet in lines:
            # Grab each line
            if packet[0:6] == 'DTMF: ':
                digits.append(packet[6])

    return digits


def cleanup():
    # Cleanup temporary files
    os.unlink(cw_id_file)
    multimon_process.kill()
    multimon_process.wait()
    os.unlink(audio_fifo)
    ptt(False)
    GPIO.cleanup()

class PTT(object):
    def __init__(self):
        pass

    def __enter__(self):
        if DEBUG: print("Debug: PTT on")
        GPIO.output(PTT_PIN, 1)


    def __exit__(self, *excinfo):
        if DEBUG: print("Debug: PTT off")
        GPIO.output(PTT_PIN, 0)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        cleanup()
        exit()
    #voiceid()
    #saytime()
    cleanup()
    exit()
