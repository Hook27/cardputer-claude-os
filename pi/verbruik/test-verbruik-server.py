"""Offline test voor de cache- en backoff-logica van claude_verbruik_server.

Vervangt het lezen van het token en de API-call door nepfuncties, zodat de
regels rond rate limiting te testen zijn zonder credentials, zonder netwerk en
zonder op een echte 429 te hoeven wachten. Precies daar zat de storing van
2026-08-03: de server bleef bij een 429 in hetzelfde tempo doorvragen, en de
logrem vergeleek de verkeerde velden waardoor de journal volliep.

    python3 test-verbruik-server.py
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time

_HIER = os.path.dirname(os.path.abspath(__file__))


def _laad_server():
    pad = os.path.join(_HIER, "claude_verbruik_server.py")
    spec = importlib.util.spec_from_file_location("verbruikserver", pad)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = _laad_server()


class Uitslag:
    def __init__(self):
        self.goed = 0
        self.fout = 0

    def check(self, naam, gekregen, verwacht):
        if gekregen == verwacht:
            self.goed += 1
            print("  OK    {:<44} -> {}".format(naam, gekregen))
        else:
            self.fout += 1
            print("  FOUT  {:<44} -> {} (verwacht: {})".format(naam, gekregen, verwacht))


class NepAntwoord:
    """Stuurt bij elke aanroep het volgende geprogrammeerde resultaat terug."""

    def __init__(self, reeks):
        self.reeks = list(reeks)
        self.aanroepen = 0

    def __call__(self, token):
        self.aanroepen += 1
        return self.reeks[min(self.aanroepen - 1, len(self.reeks) - 1)]


def _bouw(reeks, cache_s=300, cred_pad="/bestaat/niet"):
    """Verbruik-instantie met nep-token en nep-API, plus de opgevangen logregels.

    Het token komt altijd uit de nepfunctie; ``cred_pad`` doet alleen mee voor
    de test die kijkt of de server een gewijzigd bestand opmerkt.
    """
    srv._lees_token = lambda pad: ("nep-token", None, None)
    nep = NepAntwoord(reeks)
    srv._haal_verbruik = nep
    regels = []
    srv._log = lambda *a: regels.append(" ".join(str(x) for x in a))
    return srv.Verbruik(cred_pad=cred_pad, cache_s=cache_s), nep, regels


def _schrijf(pad, inhoud):
    """Nep-credentialsbestand wijzigen. Andere lengte = gegarandeerd andere
    vingerafdruk, ook als mtime op dit bestandssysteem grof is."""
    with open(pad, "w", encoding="utf-8") as f:
        f.write(inhoud)


def _forceer_nieuwe_poging(v, negeer_pauze=False):
    """Doe alsof het cache-venster voorbij is (en eventueel ook de pauze)."""
    v._laatste_poging = 0.0
    if negeer_pauze:
        v._pauze_tot = 0.0


GOED = ({"five_hour": 10.0, "seven_day": 20.0,
         "fh_reset_in_s": 100, "sd_reset_in_s": 200}, None, None)
R429 = (None, "http 429", None)
NETFOUT = (None, "netwerk: kapot", None)


def main():
    u = Uitslag()
    print("Nepomgeving: geen credentials, geen netwerk\n")

    # 1. De cache remt: twee keer vragen binnen het venster = één API-call.
    v, nep, _ = _bouw([GOED])
    v.stand(); v.stand()
    u.check("cache: 2 aanvragen -> 1 API-call", nep.aanroepen, 1)

    # 2. Een 429 zet de backoff op de startwaarde.
    v, nep, _ = _bouw([R429])
    v.stand()
    u.check("429 -> backoff start", v._backoff_s, srv.BACKOFF_START_S)

    # 3. En die pauze blokkeert een nieuwe poging, ook als de cache al verlopen is.
    #    Dit is de kern van de storing: hier bleef hij eerder doorvragen.
    _forceer_nieuwe_poging(v)
    v.stand()
    u.check("429: pauze blokkeert volgende poging", nep.aanroepen, 1)

    # 4. Bij aanhoudende 429 verdubbelt de wachttijd, tot het maximum.
    v, nep, _ = _bouw([R429])
    reeks = []
    for _ in range(7):
        _forceer_nieuwe_poging(v, negeer_pauze=True)
        v.stand()
        reeks.append(v._backoff_s)
    u.check("429 blijft: verdubbelt tot plafond", reeks,
            [300, 600, 1200, 2400, 3600, 3600, 3600])

    # 5. Geeft de server zelf een Retry-After, dan volgen we die.
    v, nep, _ = _bouw([(None, "http 429", 42)])
    v.stand()
    u.check("Retry-After van de server wint", v._backoff_s, 42)

    # 6. Zodra het weer lukt, is de rem eraf.
    v, nep, _ = _bouw([R429, GOED])
    v.stand()
    _forceer_nieuwe_poging(v, negeer_pauze=True)
    v.stand()
    u.check("herstel: backoff terug op nul", (v._backoff_s, v._pauze_tot), (0, 0.0))
    u.check("herstel: bron weer live", v._bron, "live")

    # 7. Een netwerkfout komt niet door ons tempo, dus die pauzeert niet.
    v, nep, _ = _bouw([NETFOUT])
    v.stand()
    u.check("netwerkfout -> geen backoff", v._backoff_s, 0)
    _forceer_nieuwe_poging(v)
    v.stand()
    u.check("netwerkfout: probeert gewoon opnieuw", nep.aanroepen, 2)

    # 8. De logrem: elke escalatie één regel, daarna stil op het plafond.
    v, nep, regels = _bouw([R429])
    for _ in range(9):
        _forceer_nieuwe_poging(v, negeer_pauze=True)
        v.stand()
    # 300/600/1200/2400/3600 = vijf verschillende wachttijden, daarna gelijk.
    u.check("logrem: 9 mislukkingen -> 5 regels", len(regels), 5)

    # 9. Bij een gelijkblijvende fout zonder backoff blijft het bij één regel.
    v, nep, regels = _bouw([NETFOUT])
    for _ in range(5):
        _forceer_nieuwe_poging(v)
        v.stand()
    u.check("logrem: 5x dezelfde netwerkfout -> 1 regel", len(regels), 1)

    # 10. Retry-After mag ook een HTTP-datum zijn.
    class NepFout:
        def __init__(self, waarde):
            self.headers = {"Retry-After": waarde}

    u.check("Retry-After als seconden", srv._retry_after(NepFout("120")), 120)
    u.check("Retry-After onzin -> None", srv._retry_after(NepFout("later")), None)
    u.check("Retry-After ontbreekt -> None", srv._retry_after(NepFout(None)), None)
    datum = srv._retry_after(NepFout("Wed, 21 Oct 2099 07:28:00 GMT"))
    u.check("Retry-After als datum -> getal", isinstance(datum, int) and datum > 0, True)

    # --- een nieuwe login oppikken (het herstartgedoe van 2026-09-15) ----
    # Toen zat de server in een 429-wachttijd van een uur, logde je opnieuw in,
    # en gebeurde er niets tot je hem herstartte.
    map_ = tempfile.mkdtemp(prefix="test-verbruikserver-")
    try:
        cred = os.path.join(map_, "credentials.json")

        # 11. Controle: zonder wijziging blijft de wachttijd gewoon staan.
        _schrijf(cred, "oud")
        v, nep, _ = _bouw([R429, GOED], cred_pad=cred)
        v.stand()
        v._laatste_poging -= 1000          # lang geleden, maar pauze loopt nog
        v.stand()
        u.check("geen wijziging -> wachttijd blijft", nep.aanroepen, 1)

        # 12. Gewijzigd bestand -> wachttijd vervalt, meteen opnieuw ophalen.
        _schrijf(cred, "nieuwe login")
        v.stand()
        u.check("nieuwe login -> meteen opnieuw ophalen", nep.aanroepen, 2)
        u.check("nieuwe login -> weer live", v._bron, "live")
        u.check("nieuwe login -> backoff eraf", (v._backoff_s, v._pauze_tot), (0, 0.0))

        # 13. Maar niet binnen MIN_NA_WIJZIGING_S na de vorige poging: een
        #     reeks schrijfacties mag het rate-limit-venster niet raken.
        _schrijf(cred, "oud")
        v, nep, _ = _bouw([R429, GOED], cred_pad=cred)
        v.stand()                           # 429, poging is 'nu'
        _schrijf(cred, "direct daarna")
        v.stand()
        u.check("wijziging binnen 3 min -> nog niet", nep.aanroepen, 1)

        # 14. Met live cijfers is een gewijzigd bestand de gewone refresh van
        #     elke acht uur: geen extra API-call.
        _schrijf(cred, "oud")
        v, nep, _ = _bouw([GOED], cred_pad=cred)
        v.stand()
        # Voorbij de 3-minutenrem maar binnen de 5-minutencache: zo kan alléén
        # de live-controle voorkomen dat er een extra call komt.
        v._laatste_poging = time.time() - 200
        _schrijf(cred, "gewone refresh")
        v.stand()
        u.check("live + gewijzigd bestand -> geen extra call", nep.aanroepen, 1)
    finally:
        shutil.rmtree(map_, ignore_errors=True)

    print("\n{} goed, {} fout".format(u.goed, u.fout))
    return 0 if u.fout == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
