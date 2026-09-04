import logging
import subprocess
import threading
import time

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import Text
from pwnagotchi.ui.view import BLACK
from pwnagotchi.ui.hw.whisplay_5teve import STATUS_POSITION

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

# Ein-Taster-Menue (27.08.2026, spaete Sitzung) - Hardware hat genau einen
# Knopf (whisplaydriver.py BUTTON_PIN=11), kein zweiter, kein Touch. Muss
# also komplett ueber Druckdauer/-anzahl laufen:
#   kurzer Druck  = Auswahl weiterschalten (MANAGED -> GOOD-ACTIVE ->
#                   BAD-ACTIVE -> zurueck, im Kreis)
#   langer Druck  = markierte Auswahl aktivieren
#   BAD-ACTIVE braucht zusaetzlich einen zweiten langen Druck zum
#   Bestaetigen (Besitzer-Vorgabe: schwerer zugaenglich als die anderen
#   beiden, weil es ohne Allowlist alles ausser den geschuetzten Netzen
#   angreift)
#   ~5s ohne Tastendruck = Menue schliesst sich wieder ohne zu aktivieren
#
# WICHTIG, noch NICHT umgesetzt: GOOD-ACTIVE und BAD-ACTIVE starten
# aktuell beide dieselben Dienste mit der bestehenden config.toml
# (main.target_whitelist bleibt Allowlist wie bisher) - das Umschalten
# der eigentlichen Angriffs-Erlaubnis (Allowlist fuer GOOD-ACTIVE vs.
# Pwnagotchis eigene Exclude-Liste main.whitelist=["heiopaiscastle",
# "heiopaiscastle5"] fuer BAD-ACTIVE) ist eine separate, noch offene
# Aufgabe - hier nur das Menue/die Taster-Logik.
_MODI = ("MANAGED", "GOOD-ACTIVE", "BAD-ACTIVE")
_LANG_SCHWELLE_S = 1.5
_MENU_TIMEOUT_S = 5.0


class SteveTaster(plugins.Plugin):
    __author__ = 'kraken-arche'
    __version__ = '2.0.0'
    __license__ = 'GPL3'
    __description__ = 'Taster des Whisplay-HAT: Ein-Knopf-Menue zum Umschalten zwischen Managed/Good-Active/Bad-Active.'

    def __init__(self):
        self._board = None
        self._druck_start = None
        self._ausgewaehlt = 0
        self._bestaetigung_ausstehend = False
        self._menu_offen_bis = 0.0
        self._menu_war_offen = False
        self._status_vorher = None
        # Zustandsmaschine wird sowohl vom GPIO-Interrupt-Thread des
        # Whisplay-Treibers (_auf_druck/_auf_loslassen) als auch vom
        # Display-Render-Thread (on_ui_update) angefasst - Review-Fund
        # 04.09.2026: ohne Lock kann ein Tastendruck genau zwischen
        # on_ui_update's Timeout-Check und dem Zuruecksetzen landen und
        # einen inkonsistenten Zwischenzustand sehen (z.B. BAD-ACTIVE-
        # Bestaetigung wird uebersprungen oder faelschlich neu bewaffnet).
        self._lock = threading.Lock()

    def on_display_setup(self, display):
        board = getattr(display, '_display', None)
        if board is None or not hasattr(board, 'on_button_press'):
            logging.warning("[steve_taster] kein Taster am Display-Treiber gefunden - Menue bleibt aus")
            return
        self._board = board
        board.on_button_press(self._auf_druck)
        board.on_button_release(self._auf_loslassen)
        logging.info("[steve_taster] Taster verdrahtet - Ein-Knopf-Menue aktiv")

    def on_ui_setup(self, ui):
        # Eigenes Element statt einen bestehenden Zeichenweg zweitzuverwenden
        # oder gar (wie in der Nacht davor) einen eigenen Compositing-Layer
        # zu bauen - laeuft ueber denselben Text-Widget-Mechanismus wie
        # 'name'/'shakes'/etc., dieselbe Stelle wie 'status' (geteilte
        # Konstante, siehe whisplay_5teve.STATUS_POSITION - Review-Fund
        # 04.09.2026: vorher zwei getrennte Hardcodierungen, konnten
        # stillschweigend auseinanderlaufen).
        ui.add_element('steve_menu', Text(value='', position=STATUS_POSITION, font=fonts.Small, color=BLACK))

    def on_ui_update(self, ui):
        jetzt = time.time()
        with self._lock:
            if jetzt >= self._menu_offen_bis:
                if not self._menu_war_offen:
                    return
                # Timeout ohne Aktivierung - Menue schliessen UND Zustand
                # zuruecksetzen, sonst wuerde der naechste Tastendruck auf
                # einer laengst nicht mehr sichtbaren BAD-ACTIVE-Bestaetigung
                # aufsetzen.
                self._bestaetigung_ausstehend = False
                self._ausgewaehlt = 0
                self._menu_war_offen = False
                wiederherstellen = self._status_vorher
                self._status_vorher = None
                ui.set('steve_menu', '')
                # Fund 04.09.2026 (Review): 'status' wurde beim Oeffnen
                # geleert, aber nie wiederhergestellt - blieb bis zum
                # naechsten zufaelligen Voice-Update leer, im schlimmsten
                # Fall lange. Jetzt: den Wert von vor dem Oeffnen merken
                # und beim Schliessen aktiv zurueckschreiben.
                ui.set('status', wiederherstellen or '')
                return

            if not self._menu_war_offen:
                # Erster Tick, in dem das Menue sichtbar wird - jetzigen
                # Status-Text sichern, bevor er ueberschrieben wird.
                self._status_vorher = ui.get('status')
            self._menu_war_offen = True
            if self._bestaetigung_ausstehend:
                text = "BAD-ACTIVE?\nalles ausser Whitelist\n\nnochmal halten\nzum Bestaetigen"
            else:
                zeilen = ["MODUS WAEHLEN", ""]
                for i, modus in enumerate(_MODI):
                    praefix = "> " if i == self._ausgewaehlt else "  "
                    zeilen.append(praefix + modus)
                zeilen += ["", "kurz=weiter  lang=waehlen"]
                text = "\n".join(zeilen)
        # 'status' waere an derselben Stelle sonst gleichzeitig sichtbar
        # (dieselbe Flaeche, fuer genau sowas gedacht) - waehrend das Menue
        # offen ist, hat es Vorrang. Ausserhalb des Locks: ui.set() selbst
        # braucht ihn nicht, und die naechste on_ui_update-Runde soll nicht
        # darauf warten.
        ui.set('status', '')
        ui.set('steve_menu', text)

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

    def _auf_druck(self):
        with self._lock:
            self._druck_start = time.time()
        threading.Thread(target=self._blitz, daemon=True).start()

    def _auf_loslassen(self):
        aktivieren_als = None
        with self._lock:
            if self._druck_start is None:
                return
            dauer = time.time() - self._druck_start
            self._druck_start = None
            lang = dauer >= _LANG_SCHWELLE_S
            self._menu_offen_bis = time.time() + _MENU_TIMEOUT_S

            if self._bestaetigung_ausstehend:
                if lang:
                    aktivieren_als = _MODI[self._ausgewaehlt]
                    self._menu_offen_bis = 0.0
                self._bestaetigung_ausstehend = False
            elif lang:
                gewaehlt = _MODI[self._ausgewaehlt]
                if gewaehlt == "BAD-ACTIVE":
                    self._bestaetigung_ausstehend = True
                else:
                    aktivieren_als = gewaehlt
                    self._menu_offen_bis = 0.0
            else:
                self._ausgewaehlt = (self._ausgewaehlt + 1) % len(_MODI)
        # Ausserhalb des Locks: _aktivieren() startet nur Subprozesse
        # (Popen, kein Warten), muss den Lock fuer on_ui_update/den naechsten
        # Tastendruck nicht blockieren.
        if aktivieren_als is not None:
            self._aktivieren(aktivieren_als)

    def _aktivieren(self, modus):
        logging.info("[steve_taster] Modus aktiviert: %s", modus)
        if modus == "MANAGED":
            subprocess.Popen(["systemctl", "stop", *_MITGESCHALTETE_DIENSTE])
        else:
            # GOOD-ACTIVE und BAD-ACTIVE starten aktuell identisch - siehe
            # Kommentar oben, Allowlist/Exclude-Umschaltung fehlt noch.
            subprocess.Popen(["systemctl", "start", *_MITGESCHALTETE_DIENSTE])
