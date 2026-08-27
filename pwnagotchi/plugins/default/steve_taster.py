import logging
import subprocess

import pwnagotchi.plugins as plugins

# Koppelt den physischen Taster des Whisplay-HATs an den Wechsel
# Managed<->Monitor-Mode - genau der Baustein, den barthal fuer sich
# schon lange hat (barthal/wlan_modus.py, herzschlag._wlan_umschalten()),
# 5teve aber noch fehlte. Architektur-Vorgabe vom 27.08.2026 (siehe
# kraken-arche/gedaechtnis.md): Display laeuft dauerhaft im Managed-
# Modus, der Wechsel in den Monitor-Mode passiert nur auf bewussten
# Anstoss - bisher nur von Hand per SSH (`systemctl start bettercap`),
# ab jetzt auch per Taster.
#
# Der Taster selbst ist keine rohe GPIO-Leitung, die ein generisches
# Plugin wie gpio_buttons.py lesen koennte - er haengt am WhisPlayBoard-
# Treiber (pwnagotchi/ui/hw/libs/whisplay/whisplaydriver.py), der eigene
# GPIO-Interrupts intern verwaltet und `on_button_press()` anbietet.
# Der `display_setup`-Hook (pwnagotchi/ui/display.py:314) liefert genau
# das Objekt, dessen `_display`-Attribut die WhisPlayBoard-Instanz ist -
# `initialize()` (das `self._display` setzt) ist zu diesem Zeitpunkt
# schon gelaufen.
#
# Kein Halten-fuer-Reset wie bei barthal (10s) - bewusst weggelassen,
# noch keine Anforderung dafuer genannt worden. Ein Druck schaltet um,
# fertig.


class SteveTaster(plugins.Plugin):
    __author__ = 'kraken-arche'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = 'Taster des Whisplay-HAT schaltet zwischen Managed und Monitor-Mode um (startet/stoppt bettercap.service).'

    def on_display_setup(self, display):
        board = getattr(display, '_display', None)
        if board is None or not hasattr(board, 'on_button_press'):
            logging.warning("[steve_taster] kein Taster am Display-Treiber gefunden - Umschalten per Taster bleibt aus")
            return
        board.on_button_press(self._umschalten)
        logging.info("[steve_taster] Taster verdrahtet - Kurzdruck schaltet Managed/Monitor-Mode um")

    def _umschalten(self):
        try:
            aktiv = subprocess.run(
                ["systemctl", "is-active", "--quiet", "bettercap.service"]
            ).returncode == 0
        except Exception as e:
            logging.warning("[steve_taster] konnte bettercap-Status nicht pruefen: %s", e)
            return

        kommando = "stop" if aktiv else "start"
        logging.info("[steve_taster] Taster gedrueckt - %s bettercap.service", kommando)
        subprocess.Popen(["systemctl", kommando, "bettercap.service"])
