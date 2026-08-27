import logging
import os

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
# only at the very end. Not yet confirmed against a real photo (see
# gedaechtnis.md) — barthal needed three passes against real hardware
# before corner brackets/scanlines sat right; expect the same here.
VIEW_WIDTH, VIEW_HEIGHT = 280, 240


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
        ).convert('RGB')
        d = ImageDraw.Draw(colored)
        _corner_brackets(d, VIEW_WIDTH, VIEW_HEIGHT)
        rotated = colored.transpose(
            Image.ROTATE_270 if self._rotation_deg == 90 else Image.ROTATE_90
        )
        super(Whisplay5teve, self).render(rotated)
