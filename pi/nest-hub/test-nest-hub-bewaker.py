"""Offline test voor de beslisregels van nest_hub_bewaker.

Geen Hub, geen Tailscale en geen netwerk naar buiten: de Hub is een NepHub,
de klok een nepklok, het verbruik een waarde die de test zelf zet, en de
thuis-check krijgt vaste uitvoer van `tailscale` en `ip`. Alleen het laatste
blok start echte HTTP-servers, en die luisteren op 127.0.0.1.

Het gaat om de regels die in de woonkamer merkbaar zijn: springt het scherm
aan als je werkt, gaat het weg als je stopt, blijft het van muziek af, en
luistert het naar de knoppen.

    python3 test-nest-hub-bewaker.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

_HIER = os.path.dirname(os.path.abspath(__file__))


def _laad():
    pad = os.path.join(_HIER, "nest_hub_bewaker.py")
    spec = importlib.util.spec_from_file_location("nest_hub_bewaker", pad)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nh = _laad()
nh._log = lambda *a: None   # stil; de uitslagen zeggen genoeg

SPOTIFY = "CC32E753"        # een willekeurige andere app op de Hub


class Uitslag:
    def __init__(self):
        self.goed = 0
        self.fout = 0

    def check(self, naam, gekregen, verwacht):
        if gekregen == verwacht:
            self.goed += 1
            print("  OK    {:<52} -> {}".format(naam, gekregen))
        else:
            self.fout += 1
            print("  FOUT  {:<52} -> {} (verwacht: {})".format(naam, gekregen, verwacht))


class NepKlok:
    def __init__(self):
        self.t = 1800000000.0

    def __call__(self):
        return self.t

    def later(self, minuten):
        self.t += minuten * 60


def meting(fh, sd=10, bron="live"):
    return {"five_hour": float(fh), "seven_day": float(sd), "fh_reset_in_s": 3600,
            "sd_reset_in_s": 86400, "bron": bron, "ts": 0}


class Opstelling:
    """Bewaker met NepHub, nepklok, en verbruik en thuis-status die de test zet."""

    def __init__(self, thuis=True, droog=False, app=None):
        self.klok = NepKlok()
        self.hub = nh.NepHub(app=app)
        self.stand = meting(5)
        self.thuis = thuis
        self.ping_vragen = []   # per ronde: mocht de thuis-check pingen?
        self.b = nh.Bewaker(self.hub, lambda: (self.stand, None), self._thuis_check,
                            "http://pi:8092/scherm", stilte_s=20 * 60,
                            droog=droog, klok=self.klok)

    def _thuis_check(self, mag_pingen=False):
        self.ping_vragen.append(mag_pingen)
        return self.thuis, "test"

    def ronde(self, fh=None, na_min=0, bron="live"):
        self.klok.later(na_min)
        if fh is not None:
            self.stand = meting(fh, bron=bron)
        return self.b.stap()


# Vaste uitvoer van `ip -j addr show`: een LAN, een IPv6-prefix, en de dingen
# die níet als thuis mogen tellen (loopback, link-local, Tailscale).
IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [
        {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}]},
    {"ifname": "eth0", "addr_info": [
        {"family": "inet", "local": "192.168.1.10", "prefixlen": 24, "scope": "global"},
        {"family": "inet6", "local": "2001:db8:1:2::10", "prefixlen": 64, "scope": "global"},
        {"family": "inet6", "local": "fe80::10", "prefixlen": 64, "scope": "link"}]},
    {"ifname": "tailscale0", "addr_info": [
        {"family": "inet", "local": "100.64.0.10", "prefixlen": 32, "scope": "global"},
        {"family": "inet6", "local": "fd7a:115c:a1e0::10", "prefixlen": 128,
         "scope": "global"}]},
])


def peer(host, pad="", online=True, dns=None):
    return {"HostName": host, "DNSName": (dns or host.lower()) + ".tailnet.ts.net.",
            "Online": online, "CurAddr": pad}


def ts_status(*peers):
    return {"Peer": {"nodekey:{}".format(i): p for i, p in enumerate(peers)}}


class NepRun:
    """Speelt `tailscale` en `ip` na met vaste uitvoer, en telt de pings."""

    def __init__(self, status, ping_uit="", ping_err=""):
        self.status = status
        self.ping_uit = ping_uit
        self.ping_err = ping_err
        self.pings = 0
        self.cmds = []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        if cmd[:2] == ["tailscale", "status"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.status), "")
        if cmd[:2] == ["tailscale", "ping"]:
            self.pings += 1
            code = 0 if nh.pad_uit_ping(self.ping_uit) else 1
            return subprocess.CompletedProcess(cmd, code, self.ping_uit, self.ping_err)
        if cmd[0] == "ip":
            return subprocess.CompletedProcess(cmd, 0, IP_JSON, "")
        raise OSError("onbekend commando: {}".format(cmd))


def main():
    u = Uitslag()
    print("Nepomgeving: NepHub, nepklok, geen Tailscale, geen netwerk naar buiten\n")

    # --- activiteit: wanneer telt iets als 'bezig met Claude' ---------------
    a = nh.Activiteit()
    u.check("activiteit: eerste meting is alleen de basis", a.verwerk(meting(5)), False)
    u.check("activiteit: gelijk -> niets", a.verwerk(meting(5)), False)
    u.check("activiteit: 5-uurs +1 -> gewerkt", a.verwerk(meting(6)), True)
    u.check("activiteit: daling (venster reset) -> niets", a.verwerk(meting(0)), False)
    u.check("activiteit: na reset 0 -> 4 -> gewerkt", a.verwerk(meting(4)), True)
    u.check("activiteit: week +1 -> gewerkt", a.verwerk(meting(4, sd=11)), True)
    u.check("activiteit: 'gemeten' telt niet", a.verwerk(meting(9, sd=11, bron="gemeten")), False)
    u.check("activiteit: ... en schuift de basis niet op", a.verwerk(meting(9, sd=11)), True)
    u.check("activiteit: lege cijfers -> niets",
            a.verwerk({"five_hour": None, "seven_day": None, "bron": "live"}), False)
    u.check("activiteit: geen antwoord -> niets", a.verwerk(None), False)

    # --- de regelset zelf ---------------------------------------------------
    for args, verwacht in [
        ((True, "fotolijst", False), "toon"),
        ((True, "anders", False), None),
        ((True, "anders", True), "toon"),
        ((True, "eigen", False), None),
        ((True, "onbereikbaar", True), None),
        ((False, "eigen", False), "stop"),
        ((False, "anders", False), None),
        ((False, "fotolijst", False), None),
    ]:
        u.check("beslis{}".format(args), nh.beslis(*args), verwacht)
    u.check("duid: geen app = fotolijst", nh.duid_app(None), "fotolijst")
    u.check("duid: Backdrop = fotolijst", nh.duid_app(nh.APP_BACKDROP), "fotolijst")
    u.check("duid: DashCast = ons scherm", nh.duid_app(nh.APP_DASHCAST), "eigen")
    u.check("duid: Spotify = iets anders", nh.duid_app(SPOTIFY), "anders")

    # --- 1. automatisch aan en weer uit -------------------------------------
    o = Opstelling()
    u.check("auto: eerste ronde = basis, niets", o.ronde(fh=5), None)
    u.check("auto: zonder stijging niets", o.ronde(na_min=5), None)
    u.check("auto: stijging -> tonen", o.ronde(fh=6, na_min=5), "toon")
    u.check("auto: met het juiste adres", o.hub.acties[-1], ("toon", "http://pi:8092/scherm"))
    u.check("auto: status loopt niet achter na tonen", o.b.status()["hub"], "eigen")
    u.check("auto: 19 min stil -> blijft staan", o.ronde(na_min=19), None)
    u.check("auto: 20 min stil -> weg", o.ronde(na_min=1), "stop")
    u.check("auto: terug op de fotolijst", o.hub.app, None)
    u.check("auto: status loopt niet achter na weghalen", o.b.status()["hub"], "fotolijst")

    # --- 2. valt de Hub zelf terug, dan komt het scherm terug ---------------
    o = Opstelling()
    o.ronde(fh=5)
    o.ronde(fh=6, na_min=5)
    o.hub.app = None
    u.check("terugval: Hub zelf weg tijdens sessie -> opnieuw", o.ronde(na_min=1), "toon")

    # --- 3. muziek of video blijft ongemoeid, behalve met de knop -----------
    o = Opstelling(app=SPOTIFY)
    o.ronde(fh=5)
    u.check("storen: muziek + stijging -> niets", o.ronde(fh=6, na_min=5), None)
    u.check("storen: de muziek speelt door", o.hub.app, SPOTIFY)
    o.b.toon()
    u.check("storen: knop Toon gaat er wel overheen", o.ronde(), "toon")
    o.hub.app = SPOTIFY                     # iemand start weer muziek
    u.check("storen: daarna niet opnieuw overheen", o.ronde(na_min=1), None)
    u.check("storen: na de sessie niets weghalen", o.ronde(na_min=25), None)
    u.check("storen: de muziek staat er nog", o.hub.app, SPOTIFY)

    # --- 4. knop 'Terug naar fotolijst' geldt tot de sessie voorbij is ------
    o = Opstelling()
    o.ronde(fh=5)
    o.ronde(fh=6, na_min=5)
    o.b.fotolijst()
    u.check("uit: knop Fotolijst -> weg", o.ronde(), "stop")
    doorwerken = [o.ronde(fh=fh, na_min=5) for fh in range(7, 13)]
    u.check("uit: 30 min doorwerken zet het niet terug", doorwerken, [None] * 6)
    u.check("uit: status zegt waarom", o.b.status()["modus"], "uitgezet tot je sessie voorbij is")
    u.check("uit: 20 min stil -> automaat mag weer", o.ronde(na_min=20), None)
    u.check("uit: status wacht weer", o.b.status()["modus"], "wacht op verbruik")
    u.check("uit: volgende stijging -> tonen", o.ronde(fh=13, na_min=5), "toon")

    # --- 5. buitenshuis: automaat niet, knop wel ------------------------------
    o = Opstelling(thuis=False)
    o.ronde(fh=5)
    u.check("weg: stijging buitenshuis -> niets", o.ronde(fh=6, na_min=5), None)
    o.b.toon()
    u.check("weg: knop Toon werkt altijd", o.ronde(), "toon")
    o.ronde(fh=7, na_min=15)
    u.check("weg: stijging houdt handmatige sessie aan", o.ronde(na_min=10), None)
    u.check("weg: 20 min na de laatste stijging -> weg", o.ronde(na_min=10), "stop")

    # --- 5b. 'thuis' pas een ronde later vastgesteld (de storing van 11:38) --
    # Toen koelde het pad af, faalde de ene ping, en was de stijging verloren.
    o = Opstelling(thuis=False)
    o.ronde(fh=5)
    u.check("later thuis: stijging, thuis nog onbekend -> niets", o.ronde(fh=6, na_min=5), None)
    o.thuis = True
    u.check("later thuis: volgende ronde bevestigd -> alsnog", o.ronde(na_min=1), "toon")
    u.check("later thuis: in die ronde om een ping gevraagd", o.ping_vragen[-1], True)
    u.check("later thuis: sessie telt vanaf de stijging", o.ronde(na_min=19), "stop")

    o = Opstelling(thuis=False)
    o.ronde(fh=5)
    o.ronde(fh=6, na_min=5)
    o.thuis = True
    u.check("te laat thuis: na 20 min telt de stijging niet", o.ronde(na_min=20), None)

    o = Opstelling()
    o.ronde(fh=5)
    o.ronde(na_min=5)
    o.ronde(fh=6, na_min=5)
    o.ronde(na_min=1)
    u.check("ping: alleen bij verbruik dat op thuis wacht", o.ping_vragen,
            [False, False, True, False])

    # --- 5c. na een herstart: een scherm dat er al staat overnemen ------------
    # Op 2026-09-25 haalde een herstart het scherm weg terwijl Jörg werkte.
    o = Opstelling(app=nh.APP_DASHCAST)
    u.check("herstart: scherm staat er al -> niet weghalen", o.ronde(fh=5), None)
    u.check("herstart: ... maar overnemen", o.b.status()["modus"], "automatisch")
    u.check("herstart: 19 min zonder verbruik -> blijft", o.ronde(na_min=19), None)
    u.check("herstart: 20 min zonder verbruik -> weg", o.ronde(na_min=1), "stop")

    o = Opstelling(app=nh.APP_DASHCAST)
    o.hub.bereikbaar = False
    o.ronde(fh=5)
    o.hub.bereikbaar = True
    u.check("herstart: Hub eerst onbereikbaar -> later overnemen", o.ronde(na_min=1), None)
    u.check("herstart: ... en dan wel in een sessie", o.b.status()["scherm_gewenst"], True)

    o = Opstelling()
    o.ronde(fh=5)
    o.hub.app = nh.APP_DASHCAST              # later iemands eigen catt cast_site
    u.check("herstart: overnemen alleen in de eerste ronde", o.ronde(na_min=1), "stop")

    # --- 6. onbereikbare Hub, droog, Backdrop, oude cijfers -------------------
    o = Opstelling()
    o.ronde(fh=5)
    o.hub.bereikbaar = False
    u.check("onbereikbaar: geen actie, geen crash", o.ronde(fh=6, na_min=5), None)
    u.check("onbereikbaar: status zegt het", o.b.status()["hub"], "onbereikbaar")
    o.hub.bereikbaar = True
    u.check("onbereikbaar: weer terug -> alsnog tonen", o.ronde(na_min=1), "toon")

    o = Opstelling(droog=True)
    o.ronde(fh=5)
    u.check("droog: beslist wel", o.ronde(fh=6, na_min=5), "toon")
    u.check("droog: maar raakt de Hub niet", (o.hub.acties, o.hub.app), ([], None))

    o = Opstelling(app=nh.APP_BACKDROP)
    o.ronde(fh=5)
    u.check("Backdrop telt als fotolijst", o.ronde(fh=6, na_min=5), "toon")

    o = Opstelling()
    o.ronde(fh=5)
    u.check("oude cijfers ('gemeten') -> niets", o.ronde(fh=9, na_min=5, bron="gemeten"), None)
    u.check("geen token -> niets", o.ronde(fh=9, na_min=5, bron="geen-token"), None)
    u.check("weer live en hoger -> tonen", o.ronde(fh=9, na_min=5), "toon")

    # --- 7. thuis-check ------------------------------------------------------
    netten = nh.thuisnetten(IP_JSON)
    u.check("netten: LAN + IPv6-prefix, geen lo/link/tailscale",
            [str(n) for n in netten], ["192.168.1.0/24", "2001:db8:1:2::/64"])

    def oordeel(*peers):
        return nh.beoordeel_thuis(ts_status(*peers), ["Mijn-Laptop"], netten)

    u.check("thuis: direct via het LAN", oordeel(peer("Mijn-Laptop", "192.168.1.50:41641"))[0], True)
    u.check("thuis: direct via het IPv6-prefix",
            oordeel(peer("Mijn-Laptop", "[2001:db8:1:2::5]:41641"))[0], True)
    u.check("thuis: direct via internet -> nee",
            oordeel(peer("Mijn-Laptop", "203.0.113.30:41641"))[0], False)
    u.check("thuis: offline -> nee",
            oordeel(peer("Mijn-Laptop", "192.168.1.50:41641", online=False))[0], False)
    r = oordeel(peer("Mijn-Laptop", ""))
    u.check("thuis: online zonder pad -> onbekend + ping", (r[0], r[2]),
            (None, "mijn-laptop.tailnet.ts.net"))
    u.check("thuis: naam via het DNS-label",
            nh.beoordeel_thuis(ts_status(peer("Laptop van mij", "192.168.1.50:41641",
                                              dns="mijn-laptop")),
                               ["mijn-laptop"], netten)[0], True)
    u.check("thuis: onbekend apparaat -> nee", oordeel(peer("Telefoon", "192.168.1.60:41641"))[0], False)
    u.check("ping: direct pad",
            nh.pad_uit_ping("pong from mijn-laptop (100.1.2.3) via 192.168.1.50:41641 in 3ms"),
            "192.168.1.50:41641")
    u.check("ping: via relay -> geen pad",
            nh.pad_uit_ping("pong from mijn-laptop (100.1.2.3) via DERP(ams) in 25ms\n"
                            "direct connection not established"), None)
    u.check("pad: IPv6 tussen haken", str(nh.adres_uit_pad("[2001:db8::5]:41641")), "2001:db8::5")
    u.check("pad: leeg -> geen adres", nh.adres_uit_pad(""), None)

    zonder_pad = ts_status(peer("Mijn-Laptop", ""))
    run = NepRun(zonder_pad, ping_uit="pong from mijn-laptop (100.1.2.3) via 192.168.1.50:41641 in 3ms")
    tc = nh.ThuisCheck(["Mijn-Laptop"], draai=run)
    u.check("check: zonder stijging geen ping -> onbekend", (tc(False)[0], run.pings), (None, 0))
    u.check("check: bij stijging ping -> thuis", (tc(True)[0], run.pings), (True, 1))
    run = NepRun(zonder_pad, ping_uit="pong from mijn-laptop (100.1.2.3) via DERP(ams) in 25ms")
    u.check("check: ping via relay -> niet thuis", nh.ThuisCheck(["Mijn-Laptop"], draai=run)(True)[0], False)
    # Zo ziet het er na een stilte echt uit: eerst via de relay, dan direct.
    run = NepRun(zonder_pad, ping_uit=(
        "pong from mijn-laptop (100.1.2.3) via DERP(ams) in 25ms\n"
        "pong from mijn-laptop (100.1.2.3) via 192.168.1.50:41641 in 3ms"))
    u.check("check: eerst relay, dan direct -> thuis",
            nh.ThuisCheck(["Mijn-Laptop"], draai=run)(True)[0], True)
    ping = [c for c in run.cmds if c[:2] == ["tailscale", "ping"]][-1]
    u.check("check: pingt meer dan één keer", int(ping[ping.index("-c") + 1]) >= 2, True)

    klok = NepKlok()
    run = NepRun(ts_status(peer("Mijn-Laptop", "192.168.1.50:41641")))
    tc = nh.ThuisCheck(["Mijn-Laptop"], draai=run, klok=klok)
    u.check("onthoud: direct pad -> thuis", tc()[0], True)
    run.status = ts_status(peer("Mijn-Laptop", ""))
    klok.later(10)
    r = tc()
    u.check("onthoud: pad afgekoeld, 10 min later -> nog thuis",
            (r[0], "vastgesteld om" in r[1]), (True, True))
    klok.later(6)
    u.check("onthoud: na 16 min -> weer onbekend", tc()[0], None)
    run = NepRun(zonder_pad, ping_err="Access denied: ping access denied")
    tc = nh.ThuisCheck(["Mijn-Laptop"], draai=run)
    tc(True)
    tc(True)
    u.check("check: ping geweigerd -> niet blijven proberen", run.pings, 1)

    def geen_tailscale(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    u.check("check: geen tailscale -> nee, geen crash",
            nh.ThuisCheck(["x"], draai=geen_tailscale)()[0], False)
    u.check("check: testvlag altijd thuis", nh.ThuisCheck([], altijd=True)()[0], True)

    # --- 8. HTTP: pagina's, doorgeven, knoppen, afscherming ------------------
    class NepVerbruik(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            body = json.dumps(meting(42)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    vsrv = HTTPServer(("127.0.0.1", 0), NepVerbruik)
    threading.Thread(target=vsrv.serve_forever, daemon=True).start()
    verbruik_url = "http://127.0.0.1:{}/verbruik".format(vsrv.server_address[1])

    o = Opstelling()
    srv = nh.ThreadingHTTPServer(("127.0.0.1", 0), nh._maak_handler(o.b, verbruik_url))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    basis = "http://127.0.0.1:{}".format(srv.server_address[1])

    def get(pad):
        try:
            with urllib.request.urlopen(basis + pad, timeout=5) as r:
                return r.status, r.headers.get("Content-Type") or "", r.read()
        except urllib.error.HTTPError as e:
            return e.code, "", b""

    def post(pad, kop=True):
        req = urllib.request.Request(basis + pad, data=b"", method="POST",
                                     headers={"X-Nest-Hub": "1"} if kop else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None

    code, soort, body = get("/scherm")
    u.check("http: /scherm is html", (code, soort.split(";")[0], b"<title>" in body),
            (200, "text/html", True))
    code, soort, body = get("/")
    u.check("http: / is de bediening", (code, b"Terug naar fotolijst" in body), (200, True))
    code, soort, body = get("/verbruik")
    u.check("http: /verbruik wordt doorgegeven", (code, json.loads(body)["five_hour"]), (200, 42.0))
    code, soort, body = get("/status")
    u.check("http: /status geeft de modus", (code, json.loads(body)["modus"]),
            (200, "wacht op verbruik"))
    u.check("http: POST zonder header -> geweigerd", post("/toon", kop=False)[0], 403)
    code, st = post("/toon")
    u.check("http: POST /toon -> handmatig", (code, st["modus"]), (200, "handmatig"))
    u.check("http: ... en de ronde is gewekt", o.b.wek.is_set(), True)
    code, st = post("/fotolijst")
    u.check("http: POST /fotolijst -> uit tot sessie voorbij", (code, st["modus"]),
            (200, "uitgezet tot je sessie voorbij is"))
    u.check("http: onbekend pad -> 404", get("/nergens")[0], 404)
    vsrv.shutdown()
    vsrv.server_close()
    u.check("http: verbruikserver weg -> 502", get("/verbruik")[0], 502)
    srv.shutdown()
    srv.server_close()

    # --- 9. Hub.toon(): wachten op het laadbericht, niet op een bevestiging ---
    # De eerste versie wachtte telkens 20 s op een antwoord dat de ontvanger met
    # force=True nooit stuurt. pychromecast is hier nagebootst.
    class NepCast:
        def __init__(self):
            self.app_id = nh.APP_BACKDROP

        def register_handler(self, handler):
            return

        def disconnect(self, timeout=None):
            return

    def meet_toon(start_app, meldt_verstuurd):
        cast = NepCast()

        class Dash:
            verstuurd = None

            def load_url(self, url, force=False):
                if start_app:
                    cast.app_id = nh.APP_DASHCAST
                if meldt_verstuurd and self.verstuurd is not None:
                    self.verstuurd.set()

        hub = nh.Hub("192.0.2.10")
        hub._pc = {"dash": Dash}
        hub._verbind = lambda: cast
        begin = time.time()
        gelukt = hub.toon("http://pi:8092/scherm")
        return gelukt, time.time() - begin

    gelukt, duur = meet_toon(True, True)
    u.check("toon: bericht verstuurd -> klaar binnen 2 s", (gelukt, duur < 2), (True, True))
    gelukt, duur = meet_toon(True, False)
    u.check("toon: geen signaal, wel DashCast -> klaar na ~3 s", (gelukt, duur < 5), (True, True))
    oud = nh.HUB_TIMEOUT_S
    nh.HUB_TIMEOUT_S = 1
    try:
        gelukt, duur = meet_toon(False, False)
    finally:
        nh.HUB_TIMEOUT_S = oud
    u.check("toon: app start niet -> mislukt, niet eindeloos", (gelukt, duur < 3), (False, True))

    print("\n{} goed, {} fout".format(u.goed, u.fout))
    return 0 if u.fout == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
