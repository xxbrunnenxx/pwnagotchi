import logging
import subprocess
import threading
import time

import pwnagotchi.plugins as plugins

# bettercap.service + pwngrid-peer.service gehoeren als Paar zusammen:
# sobald bettercap erfolgreich mit pwnagotchi verbunden ist, ruft
# agent.start() -> start_monitor_mode() -> start_advertising() (agent.py)
# ungeschuetzt (kein try/except, anders als die bettercap-REST-Verbindung
# selbst, siehe pwnagotchi/bettercap.py Client.run()) pwngrids REST-API
# auf 127.0.0.1:8666 an (pwnagotchi/grid.py set_advertisement_data()).
# Laeuft pwngrid-peer.service nicht mit, wirft das eine unbehandelte
# ConnectionError, die den ganzen pwnagotchi-Prozess crasht - am echten
# Geraet gefunden (27.08.2026, Absturz-Zyklus alle ~30-40s, Display ging
# im Rhythmus aus/an). Beide Dienste werden deshalb hier immer zusammen
# umgeschaltet, nie nur bettercap allein.
_MITGESCHALTETE_DIENSTE = ("bettercap.service", "pwngrid-peer.service")

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
        self._board = None
        board = getattr(display, '_display', None)
        if board is None or not hasattr(board, 'on_button_press'):
            logging.warning("[steve_taster] kein Taster am Display-Treiber gefunden - Umschalten per Taster bleibt aus")
            return
        self._board = board
        board.on_button_press(self._umschalten)
        logging.info("[steve_taster] Taster verdrahtet - Kurzdruck schaltet Managed/Monitor-Mode um")

    def _blitz(self):
        """Sofort-Rueckmeldung, dass der Druck angekommen ist - der
        eigentliche Dienst-Wechsel braucht mehrere Sekunden (reload_brcm,
        _sendebereit-Probe), ohne dieses Blitzen wirkt der Taster wie
        "nix passiert" (Besitzer-Feedback 27.08.2026), obwohl er laengst
        gegriffen hat. Laeuft in einem eigenen Thread, damit der
        Button-Callback selbst sofort zurueckkehrt."""
        if self._board is None:
            return
        try:
            self._board.set_rgb(220, 60, 255)
            time.sleep(0.2)
            self._board.set_rgb(0, 0, 0)
        except Exception as e:
            logging.debug("[steve_taster] LED-Blitz fehlgeschlagen: %s", e)

    def _umschalten(self):
        threading.Thread(target=self._blitz, daemon=True).start()
        try:
            aktiv = subprocess.run(
                ["systemctl", "is-active", "--quiet", "bettercap.service"]
            ).returncode == 0
        except Exception as e:
            logging.warning("[steve_taster] konnte bettercap-Status nicht pruefen: %s", e)
            return

        kommando = "stop" if aktiv else "start"
        logging.info("[steve_taster] Taster gedrueckt - %s %s", kommando, " ".join(_MITGESCHALTETE_DIENSTE))
        subprocess.Popen(["systemctl", kommando, *_MITGESCHALTETE_DIENSTE])
