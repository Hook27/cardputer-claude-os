"""Nep-verbruikserver — offline test voor de Cardputer-app claude_verbruik.

Serveert dezelfde JSON-vorm als de echte `claude_verbruik_server.py`, maar
met VERZONNEN cijfers. Dit script:

  * leest GEEN credentials,
  * belt GEEN api.anthropic.com,
  * heeft geen enkele netwerk-uitgang.

Daardoor kun je de weergave op het toestel (balken, kleuren, aftelklok,
degradatie bij fouten) volledig testen zonder je echte token aan te raken.
Draai dit op de SP6 of op de Pi, richt de Cardputer erop, en pas als het
beeld klopt zetten we de echte server ernaast.

Gebruik:

    python fake_verbruik_server.py                     # scenario 'oplopend'
    python fake_verbruik_server.py --scenario kritiek
    python fake_verbruik_server.py --poort 8091 --lijst

Scenario's (--scenario):

    oplopend    percentages lopen langzaam op, groen -> oranje -> rood,
                zodat je de kleurovergangen live ziet gebeuren
    normaal     rustig gebruik (22% / 14%)
    hoog        tegen de grens aan (78% / 61%) -> oranje
    kritiek     bijna op (96% / 91%) -> rood
    reset-bijna 5-uursvenster reset over 45 seconden (aftelklok naar 0)
    geen-token  server draait, maar heeft geen geldig token: 'bron' is
                "geen-token" met laatst bekende cijfers -> het toestel
                hoort te degraderen, niet leeg te gaan
    stuk        HTTP 500, om de foutafhandeling op het toestel te zien

Het antwoord is bewust klein (een paar honderd bytes): de Cardputer
parseert dit met de firmware-`requests` en heeft geen zin in een lijst
van duizenden samples.
"""

import argparse
import json
import random
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


# Vaste scenario's: (five_hour, seven_day, fh_reset_in_s, bron).
_SCENARIOS = {
    "normaal":     (22.0, 14.0, 3 * 3600 + 12 * 60, "nep"),
    "hoog":        (78.0, 61.0, 1 * 3600 + 5 * 60, "nep"),
    "kritiek":     (96.0, 91.0, 22 * 60, "nep"),
    "reset-bijna": (61.0, 44.0, 45, "nep"),
    "geen-token":  (37.0, 25.0, None, "geen-token"),
}

# Het weekvenster resetten we in de nepdata op een vast moment verderop;
# de echte server rekent dit uit het API-antwoord.
_SD_RESET_S = 3 * 86400 + 4 * 3600


class _Staat:
    """Draagt het 'oplopend'-scenario tussen requests door.

    We laten de percentages met de kloktijd meelopen in plaats van per
    request op te hogen, anders bepaalt de pollfrequentie van het toestel
    hoe snel de balk stijgt — en dan test je de verkeerde variabele.
    """

    def __init__(self, scenario, start_pct=8.0, per_minuut=6.0):
        self.scenario = scenario
        self.start_pct = start_pct
        self.per_minuut = per_minuut
        self.t0 = time.time()

    def waarden(self):
        if self.scenario != "oplopend":
            fh, sd, fh_reset, bron = _SCENARIOS[self.scenario]
            return fh, sd, fh_reset, bron
        verstreken_min = (time.time() - self.t0) / 60.0
        fh = self.start_pct + verstreken_min * self.per_minuut
        # Rond de 100 blijven hangen in plaats van doorschieten, en het
        # weekvenster loopt bewust trager op dan het 5-uursvenster.
        fh = min(fh, 100.0)
        sd = min(self.start_pct * 0.5 + verstreken_min * (self.per_minuut / 3.0), 100.0)
        # Aftelklok loopt echt af, zodat de klok op het toestel beweegt.
        fh_reset = max(int(5 * 3600 - (time.time() - self.t0)), 0)
        return round(fh, 1), round(sd, 1), fh_reset, "nep"


def _maak_handler(staat, vertraag_s):
    class Handler(BaseHTTPRequestHandler):
        # Standaard logt BaseHTTPRequestHandler naar stderr met een
        # nogal luidruchtig formaat; we willen juist kort zien wat er
        # is uitgeserveerd zodat je het naast het scherm kunt leggen.
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            pad = self.path.split("?")[0].rstrip("/") or "/"
            if pad not in ("/verbruik", "/"):
                self.send_error(404, "alleen /verbruik")
                return

            if vertraag_s:
                # Traag antwoord: test of het toestel netjes in z'n
                # timeout loopt in plaats van vast te lopen.
                time.sleep(vertraag_s)

            if staat.scenario == "stuk":
                self.send_error(500, "nep-storing")
                print("  -> 500 (scenario 'stuk')")
                return

            fh, sd, fh_reset, bron = staat.waarden()
            payload = {
                "five_hour": fh,
                "seven_day": sd,
                "fh_reset_in_s": fh_reset,
                "sd_reset_in_s": _SD_RESET_S,
                "bron": bron,
                "ts": int(time.time()),
                "nep": True,  # zodat niemand dit ooit voor echt aanziet
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # Geen caching: het toestel moet elke poll verse cijfers zien.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            print("  -> 5u {:.1f}%  week {:.1f}%  reset_in {}  bron {}".format(
                fh, sd, fh_reset, bron))

    return Handler


def _lan_ip():
    """Beste gok voor het LAN-adres van deze machine.

    We openen een UDP-socket naar een adres in het eigen subnet; er gaat
    geen pakket uit, maar de kernel kiest wel de uitgaande interface en
    dat adres is precies wat de Cardputer moet benaderen. Betrouwbaarder
    dan gethostbyname(hostname), dat op Windows vaak 127.0.0.1 geeft.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.178.1", 9))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", default="oplopend",
                   choices=sorted(list(_SCENARIOS) + ["oplopend", "stuk"]))
    p.add_argument("--poort", type=int, default=8091)
    p.add_argument("--adres", default="0.0.0.0",
                   help="bindadres; 0.0.0.0 = bereikbaar vanaf het LAN")
    p.add_argument("--vertraag", type=float, default=0.0,
                   help="seconden wachten voor antwoord (timeout-test)")
    p.add_argument("--lijst", action="store_true",
                   help="toon de scenario's en stop")
    args = p.parse_args()

    if args.lijst:
        print("Scenario's:")
        print("  oplopend     percentages lopen op; kleurovergangen live")
        for naam, (fh, sd, r, bron) in sorted(_SCENARIOS.items()):
            print("  {:<12} 5u {:.0f}%  week {:.0f}%  bron {}".format(
                naam, fh, sd, bron))
        print("  stuk         HTTP 500 (foutafhandeling testen)")
        return 0

    staat = _Staat(args.scenario)
    server = HTTPServer((args.adres, args.poort), _maak_handler(staat, args.vertraag))

    ip = _lan_ip()
    print("NEP-verbruikserver — verzonnen cijfers, geen token, geen API-call.")
    print("Scenario : {}".format(args.scenario))
    print("Luistert : http://{}:{}/verbruik".format(args.adres, args.poort))
    print("Vanaf LAN: http://{}:{}/verbruik   <- dit in config.py".format(ip, args.poort))
    print("Stoppen  : Ctrl-C")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ngestopt")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
