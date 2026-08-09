"""Offline test voor ververs-claude-token.py — met NEP-credentials.

Draait de refreshlogica langs alle uitkomsten zonder ook maar iets van je
echte omgeving aan te raken:

  * gebruikt een tijdelijke map met een VERZONNEN .credentials.json,
  * vervangt de Claude CLI door een nepscript dat we zelf sturen,
  * raakt ~/.claude/ nooit aan en gaat het netwerk niet op.

Dit is de test die je draait vóór je de systemd-timer op de Pi aanzet: de
gevaarlijke paden (pogingenlimiet, leeggemaakte login, verlopen
refresh-token) worden hier uitgelokt in plaats van in productie.

    python3 test-ververs-claude-token.py
    python3 test-ververs-claude-token.py --houd   # temp-map laten staan

Elke test controleert de RESULTAAT-code die het script als laatste regel
op stdout zet.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_HIER = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HIER, "ververs-claude-token.py")

# De nep-CLI. Leest zijn gedrag uit FAKE_MODE en doet net genoeg om de
# echte CLI na te bootsen: bij 'ververs' schuift hij expiresAt op (dat is
# het enige signaal waar het echte script op afgaat), bij 'wis' maakt hij
# de login leeg zoals de echte CLI op 2026-07-26 deed.
_FAKE_CLAUDE = r'''
import json, os, sys, threading, time

mode = os.environ.get("FAKE_MODE", "niets")
cred = os.environ["FAKE_CRED"]

if "auth" in sys.argv and "status" in sys.argv:
    ingelogd = mode != "wis"
    print(json.dumps({"loggedIn": ingelogd}))
    sys.exit(0)

def lees():
    with open(cred, encoding="utf-8") as f:
        return json.load(f)

def schrijf(d):
    with open(cred, "w", encoding="utf-8") as f:
        json.dump(d, f)

if mode == "ververs":
    d = lees()
    o = d["claudeAiOauth"]
    o["expiresAt"] = o["expiresAt"] + 8 * 3600 * 1000
    o["accessToken"] = o["accessToken"] + "-nieuw"
    o["refreshToken"] = o["refreshToken"] + "-nieuw"   # roteert, net als echt
    schrijf(d)
elif mode == "async-refresh":
    # Bootst de echte CLI na: de refresh landt pas na 0,4 s, en wordt alleen
    # weggeschreven als het proces dan nog leeft. Sluit de aanroeper stdin
    # meteen, dan is dat niet zo en verandert er niets -- precies de race die
    # op 2026-08-09 de login kostte.
    geland = {"ok": False}

    def _landt():
        time.sleep(0.4)
        geland["ok"] = True

    threading.Thread(target=_landt, daemon=True).start()
    try:
        sys.stdin.read()      # wacht tot de pijp sluit, net als bij -p
    except Exception:
        pass
    if geland["ok"]:
        d = lees()
        o = d["claudeAiOauth"]
        o["expiresAt"] = o["expiresAt"] + 8 * 3600 * 1000
        o["accessToken"] = o["accessToken"] + "-nieuw"
        o["refreshToken"] = o["refreshToken"] + "-nieuw"
        schrijf(d)
elif mode == "rommelt":
    # Wel iets aanraken, maar geen nieuwe expiry: dat is een echte mislukking
    # en moet te onderscheiden zijn van een run die niets deed.
    d = lees()
    d["claudeAiOauth"]["accessToken"] += "-anders"
    schrijf(d)
elif mode == "wis":
    d = lees()
    d["claudeAiOauth"] = {"accessToken": "", "refreshToken": "",
                          "expiresAt": 0, "refreshTokenExpiresAt": 0}
    schrijf(d)

# De echte CLI eindigt bij een lege prompt met exit 1 en deze melding.
print("No messages returned from query", file=sys.stderr)
sys.exit(1)
'''


def _maak_nep_claude(map_):
    """Schrijf de nep-CLI + een starter die op dit platform uitvoerbaar is."""
    py = os.path.join(map_, "fake_claude.py")
    with open(py, "w", encoding="utf-8") as f:
        f.write(_FAKE_CLAUDE)
    if os.name == "nt":
        # Bewust `python` van de PATH en niet sys.executable: bij de
        # Microsoft Store-Python wijst sys.executable naar
        # Program Files\WindowsApps\..., en dat pad is ACL-beschermd —
        # cmd.exe krijgt daar "Het systeem kan het opgegeven pad niet
        # vinden". Op de Pi speelt dit niet; daar is de starter een
        # gewoon sh-script met sys.executable.
        starter = os.path.join(map_, "claude.cmd")
        with open(starter, "w", encoding="utf-8") as f:
            f.write('@python "{}" %*\n'.format(py))
    else:
        starter = os.path.join(map_, "claude")
        with open(starter, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, py))
        os.chmod(starter, 0o755)
    return starter


def _schrijf_creds(pad, resterend_min, refresh_token="rt-abc",
                   rt_dagen=20, access_token="at-abc"):
    """Nep-credentialsbestand met een expiry `resterend_min` vanaf nu."""
    nu_ms = int(time.time() * 1000)
    oauth = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": nu_ms + int(resterend_min * 60000),
    }
    if rt_dagen is not None:
        oauth["refreshTokenExpiresAt"] = nu_ms + int(rt_dagen * 86400000)
    with open(pad, "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": oauth}, f)


def _draai(cred, staat, claude, mode="niets", extra=None):
    """Start het echte script en geef de RESULTAAT-code terug."""
    omgeving = dict(os.environ, FAKE_MODE=mode, FAKE_CRED=cred)
    # --server "" houdt deze tests hermetisch: zonder dat zou het script de
    # echte verbruikserver op poort 8091 van de testmachine proberen.
    # --stdin-open 0 houdt de suite snel; de tests die de wachttijd zélf
    # onderzoeken zetten hem expliciet hoger.
    cmd = [sys.executable, _SCRIPT, "--credentials", cred,
           "--staat-map", staat, "--claude", claude, "--timeout", "60",
           "--server", "", "--stdin-open", "0"]
    if extra:
        cmd += extra
    r = subprocess.run(cmd, capture_output=True, env=omgeving, timeout=180)
    uit = r.stdout.decode("utf-8", "replace")
    for regel in uit.splitlines():
        if regel.startswith("RESULTAAT:"):
            return regel.split(":", 1)[1].strip(), r.returncode
    return "(geen resultaat: {})".format(uit.strip()[:120]), r.returncode


def _start_nepserver(payload):
    """Piepklein HTTP-servertje dat één vaste JSON teruggeeft.

    Staat de gezondheidscontrole toe zonder de echte verbruikserver, en zonder
    het netwerk op te gaan. Poort 0 laat het OS er een vrije kiezen, zodat
    parallelle runs elkaar niet in de weg zitten.
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:{}/verbruik".format(srv.server_port)


def _start_hakkelende_server(payload, keer_falen=1):
    """Nepserver die de eerste ``keer_falen`` aanvragen weigert.

    Bootst na wat er bij een herstart gebeurt: de eerste poging stuit, de
    volgende lukt. Geeft ook de teller terug zodat de test kan vaststellen dat
    er daadwerkelijk opnieuw geprobeerd is.
    """
    teller = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            teller["n"] += 1
            if teller["n"] <= keer_falen:
                self.send_error(503, "nog niet zover")
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:{}/verbruik".format(srv.server_port), teller


def _leeslog(staat):
    try:
        with open(os.path.join(staat, "ververs-claude-token.log"),
                  encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


class Uitslag:
    def __init__(self):
        self.goed = 0
        self.fout = 0

    def check(self, naam, gekregen, verwacht):
        if gekregen == verwacht:
            self.goed += 1
            print("  OK    {:<34} -> {}".format(naam, gekregen))
        else:
            self.fout += 1
            print("  FOUT  {:<34} -> {} (verwacht: {})".format(
                naam, gekregen, verwacht))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--houd", action="store_true",
                   help="tijdelijke map niet opruimen (om logs te bekijken)")
    args = p.parse_args()

    if not os.path.exists(_SCRIPT):
        print("Niet gevonden: {}".format(_SCRIPT))
        return 2

    basis = tempfile.mkdtemp(prefix="test-ververs-")
    print("Nep-omgeving: {}".format(basis))
    print("(geen echte credentials, geen netwerk)\n")
    u = Uitslag()
    try:
        claude = _maak_nep_claude(basis)

        def verse_omgeving(naam):
            """Iedere test zijn eigen staat-map, anders lekt de
            pogingenteller van de ene test naar de andere."""
            staat = os.path.join(basis, "staat-" + naam)
            cred = os.path.join(basis, "cred-" + naam + ".json")
            return cred, staat

        # 1. Token nog ruim geldig -> niets doen.
        cred, staat = verse_omgeving("geldig")
        _schrijf_creds(cred, resterend_min=300)
        u.check("token ruim geldig", _draai(cred, staat, claude)[0], "geldig")

        # 2. Bijna verlopen + --dry-run -> wel signaleren, niet starten.
        cred, staat = verse_omgeving("dryrun")
        _schrijf_creds(cred, resterend_min=2)
        u.check("bijna verlopen, dry-run",
                _draai(cred, staat, claude, extra=["--dry-run"])[0], "refresh-nodig")

        # 3. Bijna verlopen + CLI ververst -> gelukt.
        cred, staat = verse_omgeving("ververst")
        _schrijf_creds(cred, resterend_min=2)
        u.check("CLI ververst het token",
                _draai(cred, staat, claude, mode="ververs")[0], "ververst")

        # 4. Al verlopen + CLI ververst -> ook dan moet het lukken (dat is
        #    empirisch zo gebleken op 2026-07-27: 129 min verlopen, toch goed).
        cred, staat = verse_omgeving("verlopen")
        _schrijf_creds(cred, resterend_min=-129)
        u.check("al verlopen, CLI ververst",
                _draai(cred, staat, claude, mode="ververs")[0], "ververst")

        # 5. CLI raakt niets aan -> 'onveranderd', tweemaal, dan gepauzeerd.
        #    De pogingenklep van 26 juli werkt onveranderd; alleen de uitkomst
        #    heeft sinds 5 augustus een eigen naam, omdat een run die niets
        #    wegschreef iets anders betekent dan een mislukte refresh.
        cred, staat = verse_omgeving("pauze")
        _schrijf_creds(cred, resterend_min=2)
        u.check("poging 1 raakt niets aan", _draai(cred, staat, claude)[0], "onveranderd")
        u.check("poging 2 raakt niets aan", _draai(cred, staat, claude)[0], "onveranderd")
        u.check("poging 3 -> gepauzeerd", _draai(cred, staat, claude)[0], "gepauzeerd")

        # 5b. Wel iets gewijzigd maar geen nieuwe expiry -> echte mislukking.
        cred, staat = verse_omgeving("rommel")
        _schrijf_creds(cred, resterend_min=2)
        u.check("wel gewijzigd, geen nieuwe expiry",
                _draai(cred, staat, claude, mode="rommelt")[0], "mislukt")

        # 5d. De kern van de fix van 2026-08-09. De nep-CLI schrijft het nieuwe
        #     token pas na 0,4 s weg, en alleen als hij dan nog leeft. Sluiten
        #     we stdin meteen, dan mislukt dat -- houden we hem open, dan lukt
        #     het. Dat is precies de race die drie logins kostte.
        cred, staat = verse_omgeving("racekwijt")
        _schrijf_creds(cred, resterend_min=2)
        u.check("stdin meteen dicht -> refresh gaat verloren",
                _draai(cred, staat, claude, mode="async-refresh")[0], "onveranderd")

        cred, staat = verse_omgeving("racegewonnen")
        _schrijf_creds(cred, resterend_min=2)
        u.check("stdin openhouden -> refresh landt",
                _draai(cred, staat, claude, mode="async-refresh",
                       extra=["--stdin-open", "2"])[0], "ververst")

        # 5c. De no-op-regel moet de meetgegevens bevatten waarvoor hij bestaat.
        cred, staat = verse_omgeving("meting")
        _schrijf_creds(cred, resterend_min=2)
        _draai(cred, staat, claude)
        log = _leeslog(staat)
        u.check("no-op logt de looptijd", "Proces leefde" in log, True)
        u.check("no-op logt de achtergrondrefresh",
                "achtergrondrefresh gestart:" in log, True)

        # 6. CLI maakt de login leeg -> login-nodig (het scenario van 26 juli).
        cred, staat = verse_omgeving("wis")
        _schrijf_creds(cred, resterend_min=2)
        u.check("CLI wist de login",
                _draai(cred, staat, claude, mode="wis")[0], "login-nodig")

        # 7. Geen refresh-token -> meteen login-nodig, CLI niet starten.
        cred, staat = verse_omgeving("geenrt")
        _schrijf_creds(cred, resterend_min=2, refresh_token="")
        u.check("geen refresh-token", _draai(cred, staat, claude)[0], "login-nodig")

        # 8. Refresh-token verlopen -> login-nodig.
        cred, staat = verse_omgeving("rtverlopen")
        _schrijf_creds(cred, resterend_min=2, rt_dagen=-1)
        u.check("refresh-token verlopen", _draai(cred, staat, claude)[0], "login-nodig")

        # 9. Geen credentialsbestand -> fout.
        cred, staat = verse_omgeving("geenbestand")
        u.check("credentialsbestand ontbreekt", _draai(cred, staat, claude)[0], "fout")

        # 10. Onleesbare JSON -> fout (en geen stacktrace).
        cred, staat = verse_omgeving("kapot")
        with open(cred, "w", encoding="utf-8") as f:
            f.write("{dit is geen json")
        u.check("credentials onleesbaar", _draai(cred, staat, claude)[0], "fout")

        # 11. Token blijft ongemoeid: het script mag zelf nooit schrijven.
        cred, staat = verse_omgeving("readonly")
        _schrijf_creds(cred, resterend_min=300, access_token="at-onaangeroerd")
        _draai(cred, staat, claude)
        with open(cred, encoding="utf-8") as f:
            na = json.load(f)["claudeAiOauth"]["accessToken"]
        u.check("script schrijft niet zelf in creds", na, "at-onaangeroerd")

        # --- gezondheidscontrole (het gat van 2026-08-03) ----------------
        # Een geldig token terwijl de server er geen cijfers mee ophaalt moet
        # een WAARSCHUWING opleveren, niet het geruststellende "niets te doen".

        # 12. Server meldt live -> gewone OK-regel, geen waarschuwing.
        cred, staat = verse_omgeving("gezond")
        _schrijf_creds(cred, resterend_min=300)
        srv, url = _start_nepserver({"bron": "live", "five_hour": 12.0})
        try:
            _draai(cred, staat, claude, extra=["--server", url])
        finally:
            srv.shutdown()
        log = _leeslog(staat)
        u.check("server live -> OK-regel", "[OK]" in log and "live" in log, True)
        u.check("server live -> geen waarschuwing", "[WAARSCHUWING]" in log, False)

        # 13. Server meldt 'geweigerd' -> waarschuwing met de te nemen actie.
        cred, staat = verse_omgeving("geweigerd")
        _schrijf_creds(cred, resterend_min=300)
        srv, url = _start_nepserver({"bron": "geweigerd", "five_hour": None})
        try:
            _draai(cred, staat, claude, extra=["--server", url])
        finally:
            srv.shutdown()
        log = _leeslog(staat)
        u.check("geweigerd -> WAARSCHUWING", "[WAARSCHUWING]" in log, True)
        u.check("geweigerd -> noemt de oplossing", "claude auth login" in log, True)

        # 14. Server onbereikbaar -> ook een waarschuwing, maar pas na alle
        #     pogingen. (Korte pauze zodat de test niet gaat zitten wachten.)
        cred, staat = verse_omgeving("serverweg")
        _schrijf_creds(cred, resterend_min=300)
        _draai(cred, staat, claude,
               extra=["--server", "http://127.0.0.1:9/verbruik",
                      "--server-pauze", "0.1"])
        u.check("server onbereikbaar -> WAARSCHUWING",
                "[WAARSCHUWING]" in _leeslog(staat), True)

        # 14b. Eén mislukte poging is géén alarm: vlak na een herstart is de
        #      poort nog niet open, en loos alarm ondermijnt de bewaking.
        cred, staat = verse_omgeving("hakkelt")
        _schrijf_creds(cred, resterend_min=300)
        srv, url, teller = _start_hakkelende_server({"bron": "live"}, keer_falen=1)
        try:
            _draai(cred, staat, claude,
                   extra=["--server", url, "--server-pauze", "0.1"])
        finally:
            srv.shutdown()
        log = _leeslog(staat)
        u.check("1e poging faalt, 2e lukt -> geen alarm",
                "[WAARSCHUWING]" in log, False)
        u.check("1e poging faalt, 2e lukt -> meldt live", "live" in log, True)
        u.check("er is echt opnieuw geprobeerd", teller["n"], 2)

        # 14c. Blijft hij weigeren, dan komt de waarschuwing alsnog.
        cred, staat = verse_omgeving("blijfthakkelen")
        _schrijf_creds(cred, resterend_min=300)
        srv, url, teller = _start_hakkelende_server({"bron": "live"}, keer_falen=5)
        try:
            _draai(cred, staat, claude,
                   extra=["--server", url, "--server-pauze", "0.1"])
        finally:
            srv.shutdown()
        u.check("beide pogingen falen -> WAARSCHUWING",
                "[WAARSCHUWING]" in _leeslog(staat), True)

        # 15. Controle uitgeschakeld -> gedraagt zich als voorheen.
        cred, staat = verse_omgeving("geencheck")
        _schrijf_creds(cred, resterend_min=300)
        _draai(cred, staat, claude)   # _draai geeft standaard --server ""
        log = _leeslog(staat)
        u.check("zonder server -> gewoon OK", "[OK]" in log, True)
        u.check("zonder server -> geen waarschuwing", "[WAARSCHUWING]" in log, False)

    finally:
        if args.houd:
            print("\nTemp-map blijft staan: {}".format(basis))
        else:
            shutil.rmtree(basis, ignore_errors=True)

    print("\n{} goed, {} fout".format(u.goed, u.fout))
    return 0 if u.fout == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
