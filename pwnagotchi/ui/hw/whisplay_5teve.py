import json
import logging
import os
import re
import subprocess

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
# (white -> ACCENT, black -> GROUND), adds a thin HUD frame, then hands the
# result to the stock `Whisplay.render()` for the actual SPI transfer. None
# of pwnagotchi's own Text/Rect/Line widgets need to change.
#
# Deliberately similar to barthal's Cyberdeck skin (same genre: dark HUD
# panel, corner brackets, scanlines, monospace) but recognizably different
# (magenta accent instead of cyan, own wordmark via `main.name` in
# config.toml) — design vow from kraken-arche/gedaechtnis.md, 27.08.2026.

GROUND = (8, 3, 14)
ACCENT = (220, 60, 255)
LINE = (40, 15, 55)

# View-orientation dims (landscape) — same trick as barthal: draw in the
# orientation you look at, rotate into the panel's native portrait frame
# only at the very end. First real-hardware pass (27.08.2026, photo in
# kraken-arche/gedaechtnis.md) confirmed the layout — no collisions, face
# stayed hidden — but the panel looked flat/empty compared to barthal's
# Cyberdeck skin. This pass adds the texture barthal has (scanlines, dot
# grid) that was missing from the first cut.
VIEW_WIDTH, VIEW_HEIGHT = 280, 240

# Precomputed once at import time, not per frame — same reasoning as
# barthal's lcd_bild.py: the pattern never changes, no need to redraw it
# on every render() call.
_SCANLINES = Image.new("RGBA", (VIEW_WIDTH, VIEW_HEIGHT), (0, 0, 0, 0))
_scanlines_draw = ImageDraw.Draw(_SCANLINES)
for _y in range(0, VIEW_HEIGHT, 2):
    _scanlines_draw.line([(0, _y), (VIEW_WIDTH, _y)], fill=(0, 0, 0, 28))

_RASTER = Image.new("RGBA", (VIEW_WIDTH, VIEW_HEIGHT), (0, 0, 0, 0))
_raster_draw = ImageDraw.Draw(_RASTER)
for _x in range(0, VIEW_WIDTH, 10):
    for _y in range(0, VIEW_HEIGHT, 10):
        _raster_draw.point((_x, _y), fill=(220, 60, 255, 30))


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


def _kopfueber_kompensiert(zielbild, xy, text, font, fill):
    """Zeichnet Text vorab um 180 Grad gedreht in ein kleines Hilfsbild und
    fuegt es an `xy` ein - kompensiert einen nicht verstandenen Effekt, der
    ausgerechnet die BAKED/IP-Textausgaben (nicht aber pwnagotchi's eigene
    Widgets: 5teve, UP, PWND, AUTO) auf dem echten Geraet kopfueber
    erscheinen laesst, obwohl exakt derselbe Rotations-/Farb-Code fuer alle
    gilt. Empirisch ermittelt (27.08.2026, Fotobeleg neu2.jpg und
    immernochnicht.jpg in kraken-arche/gedaechtnis.md), Ursache ungeklaert -
    siehe Kommentar in render(). Analog zur Y-Koordinaten-Spiegelung dort:
    Symptom kompensiert, nicht die Ursache behoben."""
    bbox = font.getbbox(text)
    breite = bbox[2] - bbox[0] + 2
    hoehe = bbox[3] - bbox[1] + 2
    hilfsbild = Image.new("RGBA", (breite, hoehe), (0, 0, 0, 0))
    ImageDraw.Draw(hilfsbild).text((-bbox[0], -bbox[1]), text, font=font, fill=fill)
    hilfsbild = hilfsbild.rotate(180)
    zielbild.paste(hilfsbild, xy, hilfsbild)


def _corner_brackets(d, width, height, length=14, thickness=2, margin=20):
    for x, y, dx, dy in (
        (margin, margin, 1, 1),
        (width - margin, margin, -1, 1),
        (margin, height - margin, 1, -1),
        (width - margin, height - margin, -1, -1),
    ):
        d.line([(x, y), (x + dx * length, y)], fill=ACCENT, width=thickness)
        d.line([(x, y), (x, y + dy * length)], fill=ACCENT, width=thickness)


class Whisplay5teve(Whisplay):
    def __init__(self, config):
        super(Whisplay5teve, self).__init__(config)
        self.name = 'whisplay_5teve'
        # Validate/cache once at startup, not on every render() call — see
        # _rotation()'s docstring.
        self._rotation_deg = _rotation()
        # Setting self._layout['face'] does nothing: view.py builds the
        # face widget from config['ui']['faces']['position_x'/'position_y']
        # directly, not from the display's layout dict. Moving it off the
        # 280x240 canvas is the only way to actually suppress it — 5teve's
        # HUD has no room for pwnagotchi's ASCII mascot, it would overlap
        # the channel/aps labels at y=34.
        config['ui']['faces']['position_x'] = -1000
        config['ui']['faces']['position_y'] = -1000

    def layout(self):
        fonts.setup(10, 9, 10, 18, 20, 9)
        self._layout['width'] = VIEW_WIDTH
        self._layout['height'] = VIEW_HEIGHT
        # No face — same choice barthal made for its infopanel; 5teve's
        # HUD reads as a dashboard, not a mascot. `face` position is
        # unused by pwnagotchi core when no face widget is added, but the
        # base layout dict expects a value.
        self._layout['face'] = (0, 0)
        self._layout['name'] = (38, 4)
        self._layout['channel'] = (38, 34)
        self._layout['aps'] = (90, 34)
        self._layout['uptime'] = (200, 4)
        self._layout['line1'] = [0, 26, VIEW_WIDTH, 26]
        self._layout['line2'] = [0, 196, VIEW_WIDTH, 196]
        self._layout['friend_face'] = (0, 92)
        self._layout['friend_name'] = (40, 94)
        self._layout['shakes'] = (38, 210)
        self._layout['mode'] = (200, 210)
        self._layout['status'] = {
            'pos': (38, 60),
            'font': fonts.status_font(fonts.Medium),
            'max': 24,
        }
        return self._layout

    def render(self, canvas):
        colored = ImageOps.colorize(
            canvas.convert('L'), black=GROUND, white=ACCENT
        ).convert('RGBA')
        # Raster first (under the text/brackets, dim enough to stay a
        # texture and not a distraction), scanlines last (like a pane of
        # glass over everything else) — same layering barthal uses.
        colored = Image.alpha_composite(colored, _RASTER)
        d = ImageDraw.Draw(colored)
        _corner_brackets(d, VIEW_WIDTH, VIEW_HEIGHT)
        # Y-Koordinaten empirisch an der Bildmitte gespiegelt (27.08.2026,
        # spaete Sitzung). Belegfoto (kraken-arche/gedaechtnis.md, neu2.jpg
        # vom echten Dienst, nicht vom Diagnoseskript): Kopfzeile/Fusszeile/
        # Eckklammern sitzen exakt da, wo layout() sie hinlegt - nur diese
        # zwei d.text()-Aufrufe erschienen oben/kopfueber statt unten,
        # ziemlich genau bei (VIEW_HEIGHT - y) statt y. Ursache dafuer NICHT
        # gefunden (Rotationsformel, MADCTL, STEVE_DREHUNG sind fuer alle
        # Elemente identisch) - das hier behebt das Symptom, nicht die
        # Ursache. Falls der Fehler in anderer Form wiederkommt (z.B. nach
        # Layout-Aenderung), nochmal von vorn pruefen statt dieser Zeile
        # blind vertrauen.
        akku = _akku_geschaetzt()
        akku_text = f"BAKED {akku:.0f}%" if akku is not None else "BAKED n/a"
        _kopfueber_kompensiert(colored, (130, VIEW_HEIGHT - 212), akku_text, fonts.Small, ACCENT)
        ip = _ip_adresse()
        if ip:
            _kopfueber_kompensiert(colored, (130, VIEW_HEIGHT - 223), ip, fonts.Small, ACCENT)
        colored = Image.alpha_composite(colored, _SCANLINES).convert('RGB')
        rotated = colored.transpose(
            Image.ROTATE_270 if self._rotation_deg == 90 else Image.ROTATE_90
        )
        super(Whisplay5teve, self).render(rotated)
