import json
import logging
import os
import re
import subprocess
import time

from PIL import Image, ImageDraw, ImageOps

import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.hw.whisplay import Whisplay

# Same physical board as pwnagotchi's stock `whisplay` driver (PiSugar
# Whisplay HAT, ST7789 240x280 SPI) and the same board barthal(omeus) speaks
# to from its own repo (github.com/xxbrunnenxx/barthalomeus). 5teve and
# barthal never run at the same time (systemd Conflicts=), so there's
# exactly one consumer of the panel at any moment — this driver only adds
# 5teve's own skin on top of the stock driver's hardware talk.
#
# pwnagotchi's core renders every display in 1-bit black/white
# (pwnagotchi/ui/view.py, `Image.new('1', ...)`) — there is no per-pixel
# color in the canvas `render()` receives. Coloring happens once, here, at
# the driver boundary: `render()` colorizes the finished mono canvas
# (white -> ACCENT, black -> GROUND), then hands the result to the stock
# `Whisplay.render()` for the actual SPI transfer.
#
# Deliberately plain (27.08.2026, owner call after a long debugging night
# with the earlier decorated version — corner brackets/scanlines/dot-grid/
# a separate post-colorize text layer for BAKED/IP): form follows function.
# Every piece of info gets its own non-overlapping row, drawn the exact
# same way (plain d.text() straight onto the mono canvas, same as
# pwnagotchi's own widgets) — no separate drawing path for anything, so
# there's nothing left that could behave differently between elements.
GROUND = (8, 3, 14)
ACCENT = (220, 60, 255)

# View-orientation dims (landscape) — same trick as barthal: draw in the
# orientation you look at, rotate into the panel's native portrait frame
# only at the very end.
VIEW_WIDTH, VIEW_HEIGHT = 280, 240

# render() runs on every real state change (view.py's _refresh_handler, or
# any on_state_change during active scanning) — IP and battery estimate
# don't change fast enough to justify a fresh subprocess/file read every
# single time, so both are throttled to this interval.
_MESS_INTERVALL = 5.0


def _rotation() -> int:
    """Degrees clockwise into the panel frame. Same env var as barthal
    (`BARTHAL_DREHUNG`) would collide if both ran at once, but they never
    do — Conflicts= guarantees that — so 5teve reads its own.

    Env var names can't start with a digit (invalid in POSIX shells), so
    this is `STEVE_DREHUNG`, not `5TEVE_DREHUNG`.

    Called once from `__init__` and cached — `render()` runs in
    pwnagotchi's renderer thread with no exception handling around it
    (`ui/display.py`'s `_render_thread`), so raising from inside render()
    on every single frame would permanently kill the display after the
    first bad value instead of failing once, loudly, at startup."""
    deg = int(os.environ.get("STEVE_DREHUNG", 90))
    if deg not in (90, 270):
        raise ValueError(
            f"STEVE_DREHUNG={deg} not supported — layout is landscape, "
            "so only 90 or 270 make sense."
        )
    return deg


# Owner requirement (kraken-arche/gedaechtnis.md, 27.08.2026): "der
# Akku-Lerner bleibt eigenstaendig bestehen, samt seiner Anzeige" - the
# same barthal-akku-lerner.service (bob's own systemd unit, unrelated to
# 5teve/pwnagotchi) keeps writing this file regardless of which persona
# owns the display; 5teve just needs to read and show it too, so that
# switching personas doesn't lose the battery-cycle learning progress
# from view. Overridable the same way barthal's BARTHAL_AKKU_DATEI is.
_AKKU_DATEI = os.environ.get(
    "STEVE_AKKU_DATEI", "/home/bob/barthalomeus/akku_lernen.json")


def _akku_geschaetzt(pfad: str = _AKKU_DATEI):
    """Port of barthal/akku_lernen.py's geschaetzte_energie() - same file,
    same math (0..100 from the learned cycle history, or None before the
    first completed cycle). Not imported directly: barthal lives in a
    separate repo, this is the whole function, small enough to duplicate
    rather than couple the two repos at runtime."""
    try:
        with open(pfad) as f:
            stand = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    zyklen = stand.get("zyklen") or []
    if not zyklen:
        return None
    erwartet = sum(zyklen) / len(zyklen)
    if erwartet <= 0:
        return None
    with open("/proc/uptime") as f:
        jetzt = float(f.readline().split()[0])
    anteil = 1.0 - (jetzt / erwartet)
    return max(0.0, min(100.0, anteil * 100.0))


# Owner requirement (kraken-arche/gedaechtnis.md, 27.08.2026): "im
# Managed-Mode muss wie bei barthalomeus die IP stehen, damit man weiss,
# dass er connected ist" - port of barthal/wlan_modus.py's ip_adresse():
# a single glance at `ip -4 -o addr show`, no DHCP wait, since this
# renders every frame. Empty in monitor mode by design - wlan0 itself
# has no IP once it's unmanaged and pulled into wlan0mon.
_IP_MUSTER = re.compile(r"inet (\d+\.\d+\.\d+\.\d+)")


def _ip_adresse(iface: str = "wlan0"):
    try:
        ausgabe = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", iface],
            capture_output=True, text=True, timeout=1,
        ).stdout
    except Exception:
        return None
    treffer = _IP_MUSTER.search(ausgabe)
    return treffer.group(1) if treffer else None


class Whisplay5teve(Whisplay):
    def __init__(self, config):
        super(Whisplay5teve, self).__init__(config)
        self.name = 'whisplay_5teve'
        # Validate/cache once at startup, not on every render() call — see
        # _rotation()'s docstring.
        self._rotation_deg = _rotation()
        # Throttle-Cache fuer IP/Akku, siehe _MESS_INTERVALL.
        self._akku_gemessen = 0.0
        self._akku_wert = None
        self._ip_gemessen = 0.0
        self._ip_wert = None
        # Setting self._layout['face'] does nothing: view.py builds the
        # face widget from config['ui']['faces']['position_x'/'position_y']
        # directly, not from the display's layout dict. Moving it off the
        # 280x240 canvas is the only way to actually suppress it.
        config['ui']['faces']['position_x'] = -1000
        config['ui']['faces']['position_y'] = -1000

    def layout(self):
        fonts.setup(10, 9, 10, 18, 20, 9)
        self._layout['width'] = VIEW_WIDTH
        self._layout['height'] = VIEW_HEIGHT
        # No face — 5teve's HUD reads as a dashboard, not a mascot. `face`
        # position is unused by pwnagotchi core when no face widget is
        # added, but the base layout dict expects a value.
        self._layout['face'] = (0, 0)
        # Sechs Zeilen, grosszuegig gestaffelt (240px Canvashoehe / 6 =
        # 40px pro Zeile), damit nichts mehr ineinanderlaeuft (siehe
        # 27.08.2026: BAKED/IP kollidierten vorher mit PWND/AUTO, weil
        # beide praktisch dieselbe y-Koordinate hatten). Jede Zeile hat
        # jetzt klar eigenen Raum, keine Linien/Texturen dazwischen.
        # x=20 links, nicht x=4: Eichbild (kraken-arche/gedaechtnis.md)
        # zeigt runde Ecken am Panel - Eck-Farbfelder kamen als Viertel-
        # kreis an, nicht als volles Quadrat. x=20 ist der Wert, der in
        # jedem bisherigen Foto nachweislich nie beschnitten war (alte
        # Eckklammern nutzten dieselbe margin=20).
        self._layout['name'] = (20, 4)
        self._layout['uptime'] = (180, 4)
        self._layout['line1'] = [0, 0, 0, 0]  # kein sichtbarer Strich mehr
        self._layout['channel'] = (20, 40)
        self._layout['aps'] = (100, 40)
        self._layout['status'] = {
            'pos': (20, 76),
            'font': fonts.status_font(fonts.Medium),
            'max': 24,
        }
        self._layout['line2'] = [0, 0, 0, 0]
        self._layout['friend_face'] = (0, 92)
        self._layout['friend_name'] = (40, 94)
        self._layout['shakes'] = (20, 160)
        self._layout['mode'] = (180, 160)
        self._layout['akku'] = (20, 200)
        self._layout['ip'] = (130, 200)
        return self._layout

    def _akku_gecacht(self):
        jetzt = time.monotonic()
        if jetzt - self._akku_gemessen >= _MESS_INTERVALL:
            self._akku_wert = _akku_geschaetzt()
            self._akku_gemessen = jetzt
        return self._akku_wert

    def _ip_gecacht(self):
        jetzt = time.monotonic()
        if jetzt - self._ip_gemessen >= _MESS_INTERVALL:
            self._ip_wert = _ip_adresse()
            self._ip_gemessen = jetzt
        return self._ip_wert

    def render(self, canvas):
        # Akku/IP direkt auf denselben Mono-Canvas gezeichnet, auf dem
        # pwnagotchi selbst "5teve"/"PWND"/"AUTO" zeichnet - kein separater
        # Zeichenweg mehr, der sich anders verhalten koennte. Simple
        # d.text() wie ueberall sonst, fill=255 wie pwnagotchi's eigenes
        # BLACK (view.py, kein invert konfiguriert).
        d = ImageDraw.Draw(canvas)
        akku = self._akku_gecacht()
        akku_text = f"BAKED {akku:.0f}%" if akku is not None else "BAKED n/a"
        d.text(self._layout['akku'], akku_text, font=fonts.Small, fill=255)
        ip = self._ip_gecacht()
        if ip:
            d.text(self._layout['ip'], ip, font=fonts.Small, fill=255)

        colored = ImageOps.colorize(
            canvas.convert('L'), black=GROUND, white=ACCENT
        ).convert('RGB')
        rotated = colored.transpose(
            Image.ROTATE_270 if self._rotation_deg == 90 else Image.ROTATE_90
        )
        super(Whisplay5teve, self).render(rotated)
