import os
import sys
import json
import atexit
import signal

from functools import partial

from collections import deque

libdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib')
if os.path.exists(libdir):
   sys.path.append(libdir)
from waveshare_epd import epd7in5_V2

from gpiozero import Button

from dotenv import load_dotenv

import time
import caldav
from datetime import datetime, timedelta
from dateutil import tz

from PIL import Image,ImageFont,ImageDraw

import netifaces

import vlc

load_dotenv()

subscribers = {}
class MsgQueue:
    def __init__(self, name):
        self.queue = deque()

    def sub(self, signal_name):
        global subscribers
        if signal_name not in subscribers:
            subscribers[signal_name] = set()
        subscribers[signal_name].add(self)

    def pub(self, signal_name, value):
        global subscribers
        if signal_name not in subscribers:
            subscribers[signal_name] = set()
            return

        for subscriber in subscribers[signal_name]:
            subscriber.enqueue(signal_name, value)

    def enqueue(self, name, value):
        self.queue.append([name, value])

    def dequeue(self):
        return self.queue.popleft()

    def len(self):
        return len(self.queue)

class Calender:
    updateDelta = timedelta(hours=2)
    updateDeltaFailed = timedelta(minutes=15)

    def __init__(self, calendarName, delta):        
        self.firstBatch = True
        self.logged_in = False

        self.calendarName = calendarName

        self.calendar = None

        self.delta = delta
        self.events = set()
        self.nextUpdateTime = datetime.now().astimezone()

        self.eventQueue = MsgQueue("Calender")
        self.eventQueue.sub("RequestCalendarUpdate")

    def tryLogin(self):
        try:
            if not self.logged_in:
                cal_user=os.getenv('OWNCLOUD_USERNAME')
                cal_pass=os.getenv('OWNCLOUD_PASSWORD')
                cal_url=os.getenv('OWNCLOUD_URL')
                cal_client=caldav.DAVClient(cal_url, username=cal_user, password=cal_pass, ssl_verify_cert=True)
                cal_principal=cal_client.principal()
                calendars = cal_principal.calendars()

                for calendar in calendars:
                    if calendar.name == self.calendarName:
                        self.calendar = calendar
                        break
                
                if self.calendar == None:
                    self.eventQueue.pub("CalendarError", f'No calendar named "{self.calendarName}"')
                    return False

                self.logged_in = True
                print("logged in")
            return True
        except Exception as e:
            self.eventQueue.pub("CalendarError", f'Failed to login')
            self.logged_in = False
            return False

    def isValid(self):
        return self.calendar != None

    def updateEventTimes(self, truncatePastEvents = True):
        try:
            if not self.tryLogin():
                return False
            
            start = datetime.now().astimezone()
            end = start+self.delta

            events = self.calendar.search(
                start=start,
                end=end,
                event=True,
                expand=True,
            )

            from_zone = tz.gettz('UTC')

            # HACK: this should be bigger than 0, but if a calendar update happens cleared events will be re-added
            eventTooOverdue = timedelta(hours=1)

            times = []
            for event in events:
                dtstart = event.icalendar_component.get("dtstart").dt
                dtstart = dtstart.replace(tzinfo=from_zone)
                localtime = dtstart.astimezone()
                if truncatePastEvents:
                    if localtime > start:
                        times.append(localtime)
                else:
                    if localtime + eventTooOverdue > start:
                        times.append(localtime)

            self.events = set(times)
            return True
        except Exception as e:
            print(e)
            print("Error: failed to update calendar")
            self.eventQueue.pub("CalendarError", f'Failed to get new events')
            return False

    def update(self):
        try:
            now = datetime.now().astimezone()
            forceUpdate = False
            while self.eventQueue.len() > 0:
                event = self.eventQueue.dequeue()
                print(event)
                if(event[0] == "RequestCalendarUpdate" and event[1] == "Pressed"):
                    print("forcing an update!")
                    forceUpdate = True

            if self.nextUpdateTime <= now or forceUpdate:
                self.eventQueue.pub("CalendarError", "")
                if self.updateEventTimes(not self.firstBatch):
                    self.firstBatch = False
                    self.nextUpdateTime = now + self.updateDelta
                    self.eventQueue.pub("EventNext", min(self.events))
                else:
                    self.nextUpdateTime = now + self.updateDeltaFailed

            pastEvents = []
            for event in self.events:
                if now >= event:
                    self.eventQueue.pub("EventStarted", event)
                    pastEvents.append(event)
            
            for pastEvent in pastEvents:
                self.events.remove(pastEvent)
            
            if len(pastEvents) > 0:
                self.eventQueue.pub("EventNext", min(self.events))

            return False
        except Exception as e:
            print(e)
            return True

def CheckForWifi():
    netifcases2 = netifaces.interfaces()
    for x in netifcases2:
        if x == "lo":
            continue
        return True
    return False

class AlarmManager:
    def __init__(self):
        self.sourceURL = os.getenv('STREAM_URL')
        self.instance = vlc.Instance(["--network-caching=1000"])
        self.mediaplayer = self.instance.media_player_new()
        self.media = self.instance.media_new(self.sourceURL)
        self.mediaplayer.set_media(self.media)
        self.current_volume = 0
        self.mediaplayer.audio_set_volume(self.current_volume)

        self.playing = False
        self.cancelButtonPressed = False
        self.songName = None

        self.expireTime = None

        self.eventQueue = MsgQueue("AlarmManager")
        self.eventQueue.sub("EventStarted")
        self.eventQueue.sub("SilenceAlarm")
        
    def metadata(self):
        self.media.parse_with_options(vlc.MediaParseFlag.network, 2)
        newSongName = self.media.get_meta(0)
        newArtistName = self.media.get_meta(1)
        if newSongName == "stream" and newArtistName == None:
            newSongName = "[Unknown]"
            newArtistName = "[Unknown]"
        if newSongName != self.songName:
            self.eventQueue.pub("SongName", f"{newArtistName} - {newSongName}")
            self.songName = newSongName

    def start(self):
        self.playing = True
        self.eventQueue.pub("Radio", "On")
        self.current_volume = 0
        self.mediaplayer.pause()
        self.mediaplayer.audio_set_volume(self.current_volume)
        self.mediaplayer.play()

    def stop(self):
        self.playing = False
        self.songName = None
        self.eventQueue.pub("Radio", "Off")
        self.mediaplayer.stop()

    def update(self):
        now = datetime.now().astimezone()

        if self.current_volume < 100:
            self.current_volume += 1
            self.mediaplayer.audio_set_volume(self.current_volume)
        
        button_pressed = False

        while self.eventQueue.len() > 0:
            event = self.eventQueue.dequeue()
            if event[0] == "EventStarted":
                self.start()
                self.expireTime = now + timedelta(minutes=30)
            elif event[0] == "SilenceAlarm" and event[1] == "Pressed":
                button_pressed = True

        if self.expireTime is not None: 
            if self.expireTime < now:
                self.stop()
                self.expireTime = None
                self.eventQueue.pub("Alarm", "Expired")
            elif button_pressed:
                self.stop()
                self.expireTime = None
                self.eventQueue.pub("Alarm", "Canceled")
        elif button_pressed:
            if self.playing:
                self.stop()
            else:
                self.start()
                self.current_volume = 100
                self.mediaplayer.audio_set_volume(self.current_volume)
                
        if self.playing:
            self.metadata()

        return False

class ButtonManager:
    eventQueue = None

    def __init__(self):
        self.button26 = Button(26, bounce_time=0.1)
        self.button26.when_pressed = partial(self.on_press, "SilenceAlarm")
        self.button26.when_released = partial(self.on_release, "SilenceAlarm")

        self.button13 = Button(13, bounce_time=0.1)
        self.button13.when_pressed = partial(self.on_press, "RequestCalendarUpdate")
        self.button13.when_released = partial(self.on_release, "RequestCalendarUpdate")
        
        self.eventQueue = MsgQueue("ButtonManager")

    def on_press(self, event_name, button):
        if event_name == None:
            event_name = f'Buttton#{button.pin}'
        self.eventQueue.pub(event_name, "Pressed")
        return
    
    def on_release(self, event_name, button):
        if event_name == None:
            event_name = f'Buttton#{button.pin}'
        self.eventQueue.pub(event_name, "Released")
        return

    def update(self):
        return False

class ScreenManager:
    wifiImg = Image.open("images/wifi.bmp")
    bellImg = Image.open("images/bell.bmp")
    warnImg = Image.open("images/warn.bmp")
    backgroundImg = Image.open("images/background.bmp")
    font = ImageFont.truetype("OverpassMono-Regular.ttf", 240)
    fontSmall = ImageFont.truetype("OverpassMono-Regular.ttf", 32)
    fontSong = ImageFont.truetype("NotoSansJP-Regular.ttf", 32)
    size = (800, 480)
    timeDisplayIncrement = timedelta(minutes=1)
    nextEventTime = timedelta(hours=12)

    def __init__(self):
        self.epd = epd7in5_V2.EPD()
        self.epd.init()
        self.epd.Clear()
        self.epd.sleep()

        self.lastDisplayedTime = datetime.now().replace(second=0).replace(microsecond=0).astimezone()
        self.nextDisplayedTime = self.lastDisplayedTime
        
        self.songName = None
        self.partialUpdateCount = 10

        self.nextEvent = None
        self.calendarError = None

        self.eventQueue = MsgQueue("ScreenManager")
        self.eventQueue.sub("Radio")
        self.eventQueue.sub("SongName")
        self.eventQueue.sub("EventNext")
        self.eventQueue.sub("CalendarError")

    def DrawText(self, draw, text, x, y, w, h, font, centerVert=False, centerHorz=False):
        l, t, r, b = draw.textbbox((x, y), text, font=font)
        width = r-l
        height = b-t

        tx = x + (x - l)
        ty = y + (y - t)

        if(centerHorz):
            tx += (w - width)//2
            x += (w - width)//2
        
        if(centerVert):
            ty += (h - height)//2
            y += (h - height)//2

        draw.text((tx, ty), text, font=font, fill=(0))
        #draw.rectangle([x, y, x+width, y+height])

    def ConstructClockFace(self, time, show_wifi):
        img = self.backgroundImg.copy()
        W, H = self.size

        message = time

        draw = ImageDraw.Draw(img)
        self.DrawText(draw, message, 0, 0, W, H, self.font, True, True)

        if(self.songName != None):
            self.DrawText(draw, self.songName, 0, 442, 800, 38, self.fontSong, True, False)

        if show_wifi:
            img.paste(self.wifiImg, (800-45,2))

        if self.nextEvent is not None and self.nextEvent < datetime.now().astimezone() + self.nextEventTime:
            img.paste(self.bellImg, (1,1))
            eventTime = self.nextEvent.strftime('%H%M')
            self.DrawText(draw, eventTime, 42, 0, 800-42-45, 40, self.fontSmall, True, False)
        elif self.nextEvent is None and self.calendarError is not None:
            img.paste(self.warnImg, (1,2))
            self.DrawText(draw, self.calendarError, 42, 0, 800-42-45, 40, self.fontSmall, True, False)

        return img

    def update(self):

        asyncUpdate = False

        while self.eventQueue.len() > 0:
            event = self.eventQueue.dequeue()
            if event[0] == "SongName":
                self.songName = event[1]
                asyncUpdate = True
            elif event[0] == "Radio" and event[1] == "Off":
                self.songName = None
                asyncUpdate = True
            elif event[0] == "EventNext":
                self.nextEvent = event[1]
                asyncUpdate = True
            elif event[0] == "CalendarError":
                self.calendarError = event[1]
                asyncUpdate = True

        now = datetime.now().astimezone()
        if now >= self.nextDisplayedTime or asyncUpdate:
            show_wifi = CheckForWifi()
            #show_bell = True #lastDisplayedTime + bellDisplayIncrement > events[0].astimezone()
            
            img = self.ConstructClockFace(now.strftime('%H%M'), show_wifi)

            #DrawToEInk(img)
            self.partialUpdateCount
            if self.partialUpdateCount < 9 or asyncUpdate:
                self.epd.init_part()
                self.epd.display_Partial(self.epd.getbuffer(img), 0, 0, self.epd.width, self.epd.height)
                if not asyncUpdate:
                    self.partialUpdateCount += 1
            else:
                self.epd.init_fast()
                self.epd.display(self.epd.getbuffer(img))
                self.partialUpdateCount = 0
            self.epd.sleep()

            if now >= self.nextDisplayedTime:
                self.lastDisplayedTime = self.nextDisplayedTime
                self.nextDisplayedTime += self.timeDisplayIncrement
        
        return False

def main():
    tasks = deque()

    tasks.append(Calender("work alarm", timedelta(days=14)))
    tasks.append(AlarmManager())
    tasks.append(ButtonManager())
    tasks.append(ScreenManager())

    while(len(tasks) > 0):
        for i in range(len(tasks)):
            task = tasks.popleft()
            requestedTime = task.update()
            if requestedTime is False:
                tasks.append(task)

        time.sleep(1)

def exit_handler():
    epd = epd7in5_V2.EPD()
    epd.init()
    epd.Clear()
    epd.sleep()

atexit.register(exit_handler)

def handle_signal(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

if __name__ == "__main__":
    main()
