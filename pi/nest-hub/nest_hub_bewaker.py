"""Nest Hub-bewaker — zet je Claude-verbruik op de Hub zolang je ermee bezig bent.

Draait op de Pi, naast de verbruikserver, en laat die ongemoeid. Normaal toont
de Google Nest Hub zijn fotolijst. Ben je thuis met Claude bezig, dan schakelt
deze bewaker de Hub over naar een groot statusscherm met je 5-uurs- en
weeklimiet; na een stilte zonder verbruik gaat hij terug naar de fotolijst.

    GET  /            bedieningspagina voor telefoon of laptop
    GET  /scherm      statuspagina voor de Hub (1024x600, leesbaar op 3 m)
    GET  /verbruik    doorgegeven van de verbruikserver, zodat de pagina zijn
                      cijfers van dezelfde origin haalt
    GET  /status      wat de bewaker nu doet, als JSON
    POST /toon        handmatig: nu tonen
    POST /fotolijst   handmatig: terug naar de fotolijst

### Hoe hij "bezig met Claude" herkent

Aan de verbruikcijfers zelf. Die zijn accountbreed, dus elk gebruik telt mee:
Claude Code, de desktop-app, claude.ai en de telefoon. Stijgt het 5-uurs- of
weekpercentage ten opzichte van de vorige verse meting, dan is er gewerkt.

Twee dingen maken dat grof. De percentages zijn hele getallen, en de
verbruikserver haalt ze hoogstens elke 5 minuten op (zijn cache, vanwege de
rate limit van het usage-endpoint). In de eigen historie van september 2026
(598 metingen per kwartier) steeg het 5-uurscijfer tijdens gebruik vrijwel elk
kwartier, meestal met 2 à 5% of meer. Daarom: aan bij de eerste stijging, uit
na STILTE_S zonder stijging. Een korter venster laat het scherm tijdens rustig
werk heen en weer springen. Een kort vraagje dat onder de 1% blijft, zie je
simpelweg niet — daarvoor is de knop.

Alleen verse cijfers tellen (`bron` "live", of "nep" van de testserver). Bij
"gemeten", "geweigerd" of "geen-token" ziet de bewaker niets en blijft hij van
de Hub af, en een tussenliggende oude meting verschuift de vergelijking niet.

### Alleen als je thuis bent

Omdat het verbruik accountbreed is, stijgen de cijfers ook als je buitenshuis
werkt. Automatisch overschakelen gebeurt daarom alleen als een van de opgegeven
Tailscale-apparaten (bedoeld: de laptop) *direct* met de Pi verbonden is via
het thuisnetwerk. `tailscale status --json` geeft per apparaat het huidige pad
(`CurAddr`); ligt dat adres in een netwerk van de Pi zelf (het IPv4-subnet of
het IPv6-prefix van de provider), dan zit het apparaat thuis. Dat pad blijft
alleen 'warm' zolang er verkeer is (ruwweg een minuut); daarna is `CurAddr`
leeg, ook als de laptop gewoon thuis staat. De aanname dat de laptop-widget
het warm houdt, bleek op 2026-09-25 niet te kloppen. Daarom laat de bewaker
Tailscale het pad vaststellen met `tailscale ping` zolang er verbruik op
'thuis' wacht, en blijft een stijging STILTE_S geldig: lukt de vaststelling
pas een ronde later, dan springt het scherm dan alsnog aan.

Handmatig tonen werkt altijd, ook als de thuis-check "nee" zegt, en binnen
zo'n handmatige sessie houdt elke stijging het scherm aan.

### Wat hij met de Hub doet — en wat niet

- Hij schakelt alleen over vanaf de fotolijst. Speelt de Hub muziek of een
  video, dan blijft hij eraf; alleen de knop "Toon op de Hub" gaat daaroverheen.
- Hij haalt alleen een DashCast-scherm weg, want daarmee toont hij zijn pagina
  (net als `catt cast_site`). Let op: een site die je zelf met catt cast, is
  ook DashCast en verdwijnt dus zodra de bewaker het scherm niet wil.
- Na "Terug naar fotolijst" blijft de automaat eraf tot je sessie voorbij is,
  dus tot er STILTE_S geen stijging meer is geweest.
- Na een herstart neemt hij een scherm dat er al staat over, in plaats van het
  weg te halen; zonder verbruik gaat het na STILTE_S alsnog weg.
- Per ronde maakt hij een verse verbinding met de Hub en verbreekt die weer.
  Eenvoudiger dan een blijvende verbinding, en de pagina blijft gewoon staan
  als de afzender weg is. De Hub laat zo'n cast ook zelf niet vallen: de test
  met catt stond op 2026-09-25 na 40 minuten nog, tot Jörg hem stopte. Terug
  naar de fotolijst is dus helemaal het werk van deze bewaker.

### Wat de Hub meldt

pychromecast's `is_idle` gebruiken we bewust niet: die telt een toestel van
het type "cast" met `is_active_input is False` als rust, ook als er een app
draait. We kijken naar `app_id`: leeg of Backdrop = fotolijst, DashCast = ons
scherm, al het andere = de Hub is ergens mee bezig. Elke verandering komt in
de journal, zodat te zien is wat de Hub werkelijk meldt.

Gebruik:

    python3 nest_hub_bewaker.py --hub 192.168.1.50 --thuis-apparaat mijn-laptop
    python3 nest_hub_bewaker.py ... --eenmalig   # 1x alles meten en tonen, niets casten
    python3 nest_hub_bewaker.py ... --droog      # beslissen en loggen, niets casten

    # lokaal proberen, zonder Hub en zonder Tailscale:
    python fake_verbruik_server.py --adres 127.0.0.1
    python nest_hub_bewaker.py --nep-hub --altijd-thuis --adres 127.0.0.1
"""

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


VERBRUIK_URL = "http://127.0.0.1:8091/verbruik"
POORT = 8092

INTERVAL_S = 60        # zo vaak kijken we naar cijfers, thuis en Hub
STILTE_S = 20 * 60     # zo lang zonder stijging -> terug naar de fotolijst
HTTP_TIMEOUT_S = 4
HUB_TIMEOUT_S = 10
TS_TIMEOUT_S = 8

# `tailscale ping`: na een stille periode gaat de eerste pong vrijwel altijd
# via een relay (DERP), terwijl het directe pad nog wordt opgezet; pas de
# volgende komen direct. Met één ping (de eerste versie, 2026-09-25) zag de
# bewaker daardoor nooit "thuis". Tailscale stopt zelf bij de eerste directe
# pong, dus bij een warm pad blijft het bij één rondje.
PING_AANTAL = 5
PING_TIMEOUT_S = 3
PING_PROCES_S = 25      # bovengrens voor het hele ping-commando

# Zo lang geldt een vastgestelde thuis-toestand nog voor de weergave als het
# pad intussen weer 'koud' is (geen verkeer = geen CurAddr).
THUIS_ONTHOUD_S = 15 * 60

# Alleen op deze bronnen vertrouwen we voor de activiteitsdetectie; "nep" is
# de offline testserver, die eerlijk zegt dat hij verzint.
BETROUWBAAR = ("live", "nep")

# Dezelfde waarden als pychromecast.config.APP_DASHCAST en
# pychromecast.IDLE_APP_ID. Hier los gezet, zodat de beslisregels (en hun
# test) zonder pychromecast kunnen.
APP_DASHCAST = "84912283"
APP_BACKDROP = "E8C28D3C"

# Tailscale-adressen tellen nooit als thuisnetwerk.
_TS_NETTEN = (ipaddress.ip_network("100.64.0.0/10"),
              ipaddress.ip_network("fd7a:115c:a1e0::/48"))

_HIER = os.path.dirname(os.path.abspath(__file__))
_PAGINAS = {"/": "bediening.html", "/bediening": "bediening.html",
            "/scherm": "scherm.html"}


def _log(*args):
    """Regel naar stdout met tijdstempel; systemd vangt dit op in de journal."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


# ---- verbruik -------------------------------------------------------


def _haal_ruw(url, timeout=HTTP_TIMEOUT_S):
    """GET ``url``: ``(status, bytes, None)``, of ``(status, None, fout)``."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, None, "http {}".format(e.code)
    except (urllib.error.URLError, OSError) as e:
        return None, None, "onbereikbaar: {}".format(getattr(e, "reason", e))


def haal_verbruik(url):
    """Stand van de verbruikserver: ``(dict, None)`` of ``(None, fout)``."""
    _status, ruw, fout = _haal_ruw(url)
    if fout:
        return None, "verbruikserver " + fout
    try:
        return json.loads(ruw.decode("utf-8")), None
    except ValueError as e:
        return None, "verbruikserver antwoord onleesbaar: {}".format(e)


class Activiteit:
    """Ziet of er gewerkt is: stijgt een percentage t.o.v. de vorige verse meting?

    Alleen betrouwbare metingen tellen, en alleen die schuiven de basis op —
    een tussenliggende "gemeten" verandert dus niets. Een daling is een reset
    van het venster, geen activiteit; de eerste stijging daarna wel.
    """

    def __init__(self):
        self._vorige = None

    def verwerk(self, stand):
        if not stand or stand.get("bron") not in BETROUWBAAR:
            return False
        fh, sd = stand.get("five_hour"), stand.get("seven_day")
        if not isinstance(fh, (int, float)) or not isinstance(sd, (int, float)):
            return False
        vorige, self._vorige = self._vorige, (fh, sd)
        if vorige is None:
            return False
        return fh > vorige[0] or sd > vorige[1]


# ---- thuis ----------------------------------------------------------


def thuisnetten(ip_json):
    """De netwerken van deze machine, uit de uitvoer van ``ip -j addr show``.

    Loopback, link-local en Tailscale tellen niet mee. Wat overblijft is het
    LAN (IPv4-subnet) en het IPv6-prefix van de provider, meestal een /64.
    """
    try:
        interfaces = json.loads(ip_json) if ip_json else []
    except ValueError:
        return []
    netten = []
    for itf in interfaces or []:
        naam = str(itf.get("ifname") or "")
        if naam == "lo" or naam.startswith("tailscale"):
            continue
        for a in itf.get("addr_info") or []:
            if a.get("scope") != "global":
                continue
            try:
                net = ipaddress.ip_interface(
                    "{}/{}".format(a["local"], a["prefixlen"])).network
            except (KeyError, ValueError):
                continue
            if any(net.version == t.version and net.subnet_of(t) for t in _TS_NETTEN):
                continue
            if net not in netten:
                netten.append(net)
    return netten


def adres_uit_pad(pad):
    """IP-adres uit een Tailscale-pad ('1.2.3.4:41641' of '[2001:db8::1]:41641'), of None."""
    tekst = (pad or "").strip()
    if not tekst:
        return None
    if tekst.startswith("["):
        host = tekst[1:tekst.find("]")] if "]" in tekst else ""
    else:
        host = tekst.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


_PONG = re.compile(r"\bvia (\S+) in\b")


def pad_uit_ping(uitvoer):
    """Het directe pad uit de uitvoer van `tailscale ping`, of None bij een relay (DERP)."""
    for m in _PONG.finditer(uitvoer or ""):
        via = m.group(1)
        if not via.upper().startswith("DERP"):
            return via
    return None


def _hoort_bij(peer, namen):
    """Past de peer bij een van de namen? HostName of eerste DNS-label, hoofdletterongevoelig."""
    host = (peer.get("HostName") or "").lower()
    label = (peer.get("DNSName") or "").split(".")[0].lower()
    return any(n.lower() in (host, label) for n in namen)


def beoordeel_thuis(status, namen, netten):
    """``(thuis, reden, te_pingen)`` uit de JSON van ``tailscale status``.

    thuis is True of False, of None als een apparaat online is maar zijn pad
    onbekend (geen recent verkeer); dan staat in te_pingen welk adres een ping
    verdient om dat pad te laten vaststellen.
    """
    peers = list(((status or {}).get("Peer") or {}).values())
    gevonden = [p for p in peers if _hoort_bij(p, namen)]
    if not gevonden:
        return False, "{} niet gevonden in tailscale".format(", ".join(namen)), None
    zonder_pad = None
    for p in gevonden:
        if not p.get("Online"):
            continue
        adres = adres_uit_pad(p.get("CurAddr"))
        if adres is None:
            zonder_pad = zonder_pad or p
        elif any(adres in n for n in netten):
            naam = p.get("HostName") or p.get("DNSName")
            return True, "{} direct via het thuisnetwerk ({})".format(naam, adres), None
    if zonder_pad is not None:
        dns = (zonder_pad.get("DNSName") or "").rstrip(".")
        naam = zonder_pad.get("HostName") or dns
        return None, "{} online, pad onbekend (geen recent verkeer)".format(naam), dns or naam
    return False, "{} niet thuis (offline of via internet)".format(
        ", ".join(p.get("HostName") or "?" for p in gevonden)), None


class ThuisCheck:
    """Zegt of een van de thuis-apparaten nu direct via het thuisnetwerk verbonden is.

    Geeft ``(thuis, reden)`` met thuis = True, False, of None (onbekend). De
    bewaker behandelt onbekend als "niet thuis": liever een scherm dat niet
    aanspringt dan een woonkamer die oplicht terwijl je elders werkt.
    """

    def __init__(self, namen, altijd=False, draai=subprocess.run, klok=time.time):
        self.namen = [n for n in namen if n]
        self.altijd = altijd
        self._draai = draai
        self._klok = klok
        self._ping_mag = True   # uit zodra tailscale laat weten dat ping niet mag
        self._laatst = None     # (tijd, thuis, reden) van de laatste vaststelling

    def _voer_uit(self, cmd, timeout=TS_TIMEOUT_S):
        """``(stdout, stderr, exitcode)``; kan het niet starten: ``(None, fout, None)``."""
        try:
            r = self._draai(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e), None
        return r.stdout or "", r.stderr or "", r.returncode

    def __call__(self, mag_pingen=False):
        if self.altijd:
            return True, "altijd thuis (testvlag --altijd-thuis)"
        if not self.namen:
            return False, "geen thuis-apparaat opgegeven"
        uit, err, code = self._voer_uit(["tailscale", "status", "--json"])
        if uit is None or code != 0:
            return False, "tailscale status mislukt: {}".format((err or "").strip() or code)
        try:
            status = json.loads(uit)
        except ValueError:
            return False, "tailscale status onleesbaar"
        ip_uit, ip_err, _ = self._voer_uit(["ip", "-j", "addr", "show"])
        netten = thuisnetten(ip_uit)
        if not netten:
            return False, "eigen netwerk onbekend ({})".format(
                (ip_err or "").strip() or "geen adressen")

        thuis, reden, te_pingen = beoordeel_thuis(status, self.namen, netten)
        # Pad onbekend (geen recent verkeer). Alleen pingen als het ertoe doet —
        # de bewaker vraagt erom zolang er verbruik ligt dat op 'thuis' wacht —
        # niet elke minuut voor de statuspagina.
        if thuis is None and te_pingen and mag_pingen and self._ping_mag:
            thuis, reden = self._ping(te_pingen, netten)
        if thuis is not None:
            self._laatst = (self._klok(), thuis, reden)
            return thuis, reden
        # Nog steeds onbekend: geef de laatste vaststelling als die recent is,
        # zodat de status niet naar "onbekend" springt telkens als het pad afkoelt.
        if self._laatst and self._klok() - self._laatst[0] < THUIS_ONTHOUD_S:
            t, oud, oude_reden = self._laatst
            return oud, "{} (vastgesteld om {})".format(
                oude_reden, time.strftime("%H:%M", time.localtime(t)))
        return None, reden

    def _ping(self, doel, netten):
        """Laat Tailscale het pad vaststellen: ``(thuis, reden)``; thuis kan None zijn."""
        uit, err, _ = self._voer_uit(
            ["tailscale", "ping", "-c", str(PING_AANTAL),
             "--timeout", "{}s".format(PING_TIMEOUT_S), doel],
            timeout=PING_PROCES_S)
        tekst = "{}\n{}".format(uit or "", err or "")
        if "denied" in tekst.lower() or "permission" in tekst.lower():
            self._ping_mag = False
            _log("tailscale ping mag niet voor deze gebruiker; de thuis-check leunt "
                 "voortaan alleen op tailscale status (zie LEESMIJ: --operator)")
            return None, "{}: ping niet toegestaan".format(doel)
        adres = adres_uit_pad(pad_uit_ping(tekst))
        if adres is None:
            return False, "{}: geen direct pad na {} pings (relay of geen antwoord)".format(
                doel, PING_AANTAL)
        if any(adres in n for n in netten):
            return True, "{} direct via het thuisnetwerk ({}, na ping)".format(doel, adres)
        return False, "{} direct verbonden, maar niet via het thuisnetwerk ({})".format(
            doel, adres)


# ---- Hub ------------------------------------------------------------


def duid_app(app_id):
    """Wat de Hub doet, afgeleid uit zijn app_id: fotolijst, eigen of anders."""
    if app_id in (None, "", APP_BACKDROP):
        return "fotolijst"
    if app_id == APP_DASHCAST:
        return "eigen"
    return "anders"


def beslis(gewenst, toestand, forceer=False):
    """'toon', 'stop' of None — de hele regelset in één oogopslag.

    - scherm gewenst en de Hub toont de fotolijst            -> toon
    - scherm gewenst, Hub bezig, maar handmatig gevraagd      -> toon
    - scherm niet gewenst en ons scherm staat erop            -> stop
    - al het andere, ook een onbereikbare Hub                 -> niets
    """
    if gewenst and (toestand == "fotolijst" or (forceer and toestand == "anders")):
        return "toon"
    if not gewenst and toestand == "eigen":
        return "stop"
    return None


def _verbreek(cast):
    if cast is None:
        return
    try:
        cast.disconnect(timeout=5)
    except Exception:
        pass


class Hub:
    """De echte Nest Hub, via pychromecast. Elke aanroep maakt een verse verbinding.

    pychromecast wordt pas bij het eerste gebruik geladen, zodat de test en
    --nep-hub zonder kunnen. Reageert het bekende adres niet, dan zoeken we de
    Hub via mDNS terug op zijn uuid (of, vóór de eerste verbinding, op naam):
    een vast adres in de router is dan niet nodig.
    """

    def __init__(self, ip, naam=None):
        self.ip = ip or None
        self.naam = naam
        self._uuid = None
        self._pc = None

    def _laad(self):
        if self._pc is None:
            import pychromecast
            from pychromecast.controllers.dashcast import DashCastController
            from pychromecast.dial import get_device_info
            from pychromecast.discovery import discover_listed_chromecasts

            class Dash(DashCastController):
                """DashCast die laat weten wanneer het laadbericht verstuurd is.

                load_url() start eerst de app en stuurt daarna via send_message
                het adres; dát moment willen we weten voordat we de verbinding
                verbreken, en de ontvanger zelf bevestigt niets (zie toon()).
                """
                verstuurd = None

                def send_message(self, *args, **kwargs):
                    try:
                        return super().send_message(*args, **kwargs)
                    finally:
                        if self.verstuurd is not None:
                            self.verstuurd.set()

            self._pc = {"pychromecast": pychromecast, "dash": Dash,
                        "info": get_device_info, "zoek": discover_listed_chromecasts}
        return self._pc

    def _apparaatinfo(self):
        if not self.ip:
            return None
        try:
            return self._laad()["info"](self.ip, timeout=HUB_TIMEOUT_S)
        except Exception as e:   # pychromecast gooit hier van alles, per soort fout
            _log("Hub op {} geeft geen apparaatinfo: {!r}".format(self.ip, e))
            return None

    def _zoek_opnieuw(self):
        """Zoek de Hub via mDNS en onthoud zijn adres. True als dat lukte."""
        if not self._uuid and not self.naam:
            return False
        zoek = self._laad()["zoek"]
        try:
            if self._uuid:
                infos, browser = zoek(uuids=[self._uuid], discovery_timeout=HUB_TIMEOUT_S)
            else:
                infos, browser = zoek(friendly_names=[self.naam],
                                      discovery_timeout=HUB_TIMEOUT_S)
        except Exception as e:
            _log("zoeken naar de Hub mislukt: {!r}".format(e))
            return False
        try:
            browser.stop_discovery()
        except Exception:
            pass
        for info in infos:
            if info.host:
                if info.host != self.ip:
                    _log("Hub gevonden op {} (was {}); zet dat adres in nest-hub.env".format(
                        info.host, self.ip))
                self.ip = info.host
                return True
        return False

    def _verbind(self):
        """Verbonden pychromecast-object, of None als de Hub niet te bereiken is."""
        info = self._apparaatinfo()
        if info is None and self._zoek_opnieuw():
            info = self._apparaatinfo()
        if info is None:
            return None
        if self._uuid is None:
            _log("Hub: {} ({}) op {}, uuid {}".format(
                info.friendly_name, info.model_name, self.ip, info.uuid))
        self._uuid = info.uuid or self._uuid
        cast = None
        try:
            cast = self._laad()["pychromecast"].get_chromecast_from_host(
                (self.ip, 8009, info.uuid, info.model_name, info.friendly_name),
                tries=1, timeout=HUB_TIMEOUT_S)
            cast.wait(timeout=HUB_TIMEOUT_S)
            return cast
        except Exception as e:
            _log("verbinden met de Hub mislukt: {!r}".format(e))
            _verbreek(cast)
            return None

    def toestand(self):
        """``(toestand, app_id, app-naam)``; toestand is fotolijst/eigen/anders/onbereikbaar."""
        cast = self._verbind()
        if cast is None:
            return "onbereikbaar", None, None
        try:
            return duid_app(cast.app_id), cast.app_id, cast.app_display_name
        finally:
            _verbreek(cast)

    def toon(self, url):
        cast = self._verbind()
        if cast is None:
            return False
        verstuurd = threading.Event()
        try:
            dash = self._laad()["dash"]()
            dash.verstuurd = verstuurd
            cast.register_handler(dash)
            # force=True: de Hub navigeert echt naar onze pagina in plaats van
            # hem in een iframe te laden. Zo doet catt het ook, en zo werkte de
            # http-pagina van de Pi in de test van 2026-09-25.
            dash.load_url(url, force=True)
            # Een bevestiging van de ontvanger komt er met force=True niet: hij
            # navigeert weg voordat hij antwoordt. De eerste versie wachtte daar
            # telkens 20 s vergeefs op. Nu wachten we tot het laadbericht de deur
            # uit is (dat meldt Dash), en als dat signaal onverhoopt uitblijft,
            # tot DashCast een paar tellen draait.
            eind = time.time() + HUB_TIMEOUT_S * 1.5
            draait_sinds = None
            while time.time() < eind and not verstuurd.is_set():
                if cast.app_id == APP_DASHCAST:
                    draait_sinds = draait_sinds or time.time()
                    if time.time() - draait_sinds >= 3:
                        break
                time.sleep(0.2)
            time.sleep(0.5)   # het bericht is geschreven; even laten landen
            return cast.app_id == APP_DASHCAST
        except Exception as e:
            _log("tonen mislukt: {!r}".format(e))
            return False
        finally:
            _verbreek(cast)

    def stop(self):
        cast = self._verbind()
        if cast is None:
            return False
        try:
            # Vlak voor het ingrijpen nog één keer kijken: is er intussen iets
            # anders gestart (muziek, een video), dan laten we dat staan.
            if cast.app_id != APP_DASHCAST:
                return True
            cast.quit_app(timeout=HUB_TIMEOUT_S)
            return True
        except Exception as e:
            _log("weghalen mislukt: {!r}".format(e))
            return False
        finally:
            _verbreek(cast)


class NepHub:
    """Hub zonder Hub: voor de offline test en om lokaal te proberen (--nep-hub)."""

    _NAMEN = {APP_BACKDROP: "Backdrop", APP_DASHCAST: "DashCast"}

    def __init__(self, app=None):
        self.app = app
        self.bereikbaar = True
        self.acties = []

    def toestand(self):
        if not self.bereikbaar:
            return "onbereikbaar", None, None
        naam = self._NAMEN.get(self.app, "nep-app") if self.app else None
        return duid_app(self.app), self.app, naam

    def toon(self, url):
        self.acties.append(("toon", url))
        if not self.bereikbaar:
            return False
        self.app = APP_DASHCAST
        return True

    def stop(self):
        self.acties.append(("stop",))
        if self.app == APP_DASHCAST:
            self.app = None
        return True


# ---- bewaker --------------------------------------------------------


class Bewaker:
    """Houdt de sessie bij en zet het scherm op de Hub of haalt het weg.

    Alle toestand staat onder één slot. De Hub wordt alleen vanuit de rondes
    aangestuurd, in één draad, zodat twee knopdrukken nooit tegelijk met de Hub
    praten: een knop zet alleen de wens en maakt de volgende ronde meteen wakker.
    """

    def __init__(self, hub, verbruik, thuis, scherm_url, stilte_s=STILTE_S,
                 droog=False, klok=time.time):
        self.hub = hub
        self.scherm_url = scherm_url
        self.stilte_s = stilte_s
        self.droog = droog
        self.wek = threading.Event()
        self._verbruik = verbruik     # () -> (stand, fout)
        self._thuis = thuis           # (mag_pingen) -> (thuis, reden)
        self._klok = klok
        self._activiteit = Activiteit()
        self._slot = threading.Lock()
        self._actief_tot = None       # tot wanneer het scherm gewenst is
        self._handmatig = False       # de huidige sessie begon met "Toon"
        self._uit_sinds = None        # moment van "Terug naar fotolijst"
        self._forceer = False         # volgende ronde ook over een andere app heen
        self._laatste_stijging = None
        self._thuis_nu = None
        self._thuis_reden = "nog niet gecontroleerd"
        self._hub = ("onbekend", None, None)
        self._verbruik_fout = None
        self._eerste_ronde = True     # nog geen Hub-toestand gezien sinds de start
        self._sleutels = {}           # laatst gelogde toestand per onderwerp
        self._geschiedenis = []       # (tijd, tekst) voor de bedieningspagina

    # -- logboek --

    def _noteer(self, tekst):
        _log(tekst)
        self._geschiedenis.append((self._klok(), tekst))
        del self._geschiedenis[:-12]

    def _meld(self, onderwerp, sleutel, tekst):
        """Noteer alleen als de toestand van dit onderwerp veranderde.

        Zo levert een Hub die urenlang de fotolijst toont één regel op, en
        niet één per minuut.
        """
        if self._sleutels.get(onderwerp) != sleutel:
            self._sleutels[onderwerp] = sleutel
            self._noteer(tekst)

    # -- knoppen --

    def toon(self):
        with self._slot:
            self._actief_tot = self._klok() + self.stilte_s
            self._handmatig = True
            self._uit_sinds = None
            self._forceer = True
            self._noteer("knop: tonen")
        self.wek.set()

    def fotolijst(self):
        with self._slot:
            self._actief_tot = None
            self._handmatig = False
            self._uit_sinds = self._klok()
            self._forceer = False
            self._noteer("knop: terug naar fotolijst; automaat wacht tot de sessie voorbij is")
        self.wek.set()

    # -- rondes --

    def _werk_sessie_bij(self, nu, gestegen, thuis):
        """Werk de sessie bij en zeg of het scherm op de Hub hoort. Onder het slot."""
        if gestegen:
            self._laatste_stijging = nu
        if self._uit_sinds is not None:
            # "Terug naar fotolijst" geldt tot er een volle stilte is geweest,
            # gerekend vanaf de knop of de laatste stijging, wat later is.
            rust_sinds = max(self._uit_sinds, self._laatste_stijging or 0)
            if nu - rust_sinds >= self.stilte_s:
                self._uit_sinds = None
                self._noteer("sessie voorbij: de automaat mag weer")
        # Een stijging blijft STILTE_S geldig. Kon 'thuis' op dat moment niet
        # worden vastgesteld, dan springt het scherm alsnog aan zodra dat lukt,
        # in plaats van te wachten op de volgende stijging (dat kan een kwartier
        # duren). Een lopende sessie verlengen kan alleen een nieuwe stijging.
        open_stijging = (self._laatste_stijging is not None
                         and nu - self._laatste_stijging < self.stilte_s)
        if open_stijging and self._uit_sinds is None and (gestegen or self._actief_tot is None):
            if thuis or self._handmatig:
                if self._actief_tot is None:
                    self._noteer("verbruik gestegen: scherm gewenst" if gestegen else
                                 "thuis bevestigd, verbruik van {} min geleden: scherm gewenst"
                                 .format(int(nu - self._laatste_stijging) // 60))
                self._actief_tot = self._laatste_stijging + self.stilte_s
                self._sleutels.pop("weg", None)
            elif gestegen:
                self._meld("weg", "gemeld",
                           "verbruik gestegen, maar niet thuis ({})".format(self._thuis_reden))
        if self._actief_tot is not None and nu >= self._actief_tot:
            self._actief_tot = None
            self._handmatig = False
            self._noteer("{} min zonder stijging: terug naar de fotolijst".format(
                self.stilte_s // 60))
        return self._actief_tot is not None and self._uit_sinds is None

    def stap(self):
        """Eén ronde: meten, beslissen, eventueel ingrijpen. Geeft de actie terug."""
        stand, fout = self._verbruik()
        gestegen = self._activiteit.verwerk(stand)
        with self._slot:
            # Een ping alleen als het ertoe doet: er is net verbruik gezien, of
            # er ligt nog een stijging klaar waarvoor 'thuis' niet bevestigd is.
            nu = self._klok()
            wacht_op_thuis = (self._laatste_stijging is not None
                              and nu - self._laatste_stijging < self.stilte_s
                              and self._actief_tot is None and self._uit_sinds is None)
        thuis, reden = self._thuis(gestegen or wacht_op_thuis)
        with self._slot:
            self._verbruik_fout = fout
            self._meld("verbruik", fout, "verbruik: {}".format(fout or "bereikbaar"))
            self._thuis_nu, self._thuis_reden = thuis, reden
            self._meld("thuis", thuis is True, "thuis: {} ({})".format(
                "ja" if thuis else "nee of onbekend", reden))
            gewenst = self._werk_sessie_bij(self._klok(), gestegen, thuis)
            forceer, self._forceer = self._forceer, False

        toestand, app, naam = self.hub.toestand()
        with self._slot:
            self._hub = (toestand, app, naam)
            self._meld("hub", (toestand, app), "Hub: {} (app_id={}, naam={})".format(
                toestand, app, naam))
            if toestand == "onbereikbaar" and forceer:
                self._forceer = True   # de knop geldt nog zodra de Hub terug is
            if self._eerste_ronde and toestand != "onbereikbaar":
                self._eerste_ronde = False
                if toestand == "eigen" and not gewenst and self._uit_sinds is None:
                    # Na een herstart (uitrol, reboot) staat ons scherm er soms
                    # nog. Niet weghalen maar overnemen: zonder verbruik gaat het
                    # na STILTE_S alsnog weg. Weghalen liet het scherm op
                    # 2026-09-25 verdwijnen terwijl Jörg gewoon zat te werken.
                    self._actief_tot = self._klok() + self.stilte_s
                    gewenst = True
                    self._noteer("scherm stond er al (na herstart): overgenomen")

        actie = beslis(gewenst, toestand, forceer)
        if actie is None:
            return None
        if self.droog:
            with self._slot:
                self._noteer("droog: zou het scherm {}".format(
                    "tonen" if actie == "toon" else "weghalen"))
            return actie
        gelukt = self.hub.toon(self.scherm_url) if actie == "toon" else self.hub.stop()
        with self._slot:
            if gelukt:
                # Wat de Hub nu hoort te doen, zodat de bedieningspagina niet een
                # ronde achterloopt; de volgende ronde meet het echt (en logt
                # het, want de gelogde sleutel is nog die van vóór de actie).
                self._hub = (("eigen", APP_DASHCAST, "DashCast") if actie == "toon"
                             else ("fotolijst", None, None))
            self._noteer("scherm {}{}".format(
                "getoond" if actie == "toon" else "weggehaald",
                "" if gelukt else " — MISLUKT, volgende ronde opnieuw"))
        return actie

    def draai(self, interval_s=INTERVAL_S):
        while True:
            self.wek.clear()
            try:
                self.stap()
            except Exception as e:   # één kapotte ronde mag de bewaker niet stoppen
                _log("ronde mislukt: {!r}".format(e))
            self.wek.wait(interval_s)

    def status(self):
        with self._slot:
            nu = self._klok()
            if self._uit_sinds is not None:
                modus = "uitgezet tot je sessie voorbij is"
            elif self._actief_tot is not None:
                modus = "handmatig" if self._handmatig else "automatisch"
            else:
                modus = "wacht op verbruik"
            toestand, app, naam = self._hub
            return {
                "scherm_gewenst": self._actief_tot is not None and self._uit_sinds is None,
                "modus": modus,
                "actief_nog_s": (int(self._actief_tot - nu)
                                 if self._actief_tot is not None else None),
                "thuis": self._thuis_nu,
                "thuis_reden": self._thuis_reden,
                "hub": toestand,
                "hub_app": naam or app,
                "laatste_stijging_s": (int(nu - self._laatste_stijging)
                                       if self._laatste_stijging is not None else None),
                "verbruik_fout": self._verbruik_fout,
                "droog": self.droog,
                "geschiedenis": [[int(nu - t), tekst]
                                 for t, tekst in reversed(self._geschiedenis)],
            }


# ---- HTTP -----------------------------------------------------------


def _maak_handler(bewaker, verbruik_url):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return  # de journal krijgt alleen wat de bewaker zelf meldt

        def _stuur(self, code, body, soort):
            self.send_response(code)
            self.send_header("Content-Type", soort)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, data):
            self._stuur(code, json.dumps(data).encode("utf-8"), "application/json")

        def do_GET(self):
            pad = self.path.split("?")[0].rstrip("/") or "/"
            if pad in _PAGINAS:
                # Bij elk verzoek van schijf: een aangepaste pagina is dan
                # zonder herstart zichtbaar.
                try:
                    with open(os.path.join(_HIER, _PAGINAS[pad]), "rb") as f:
                        body = f.read()
                except OSError:
                    self.send_error(500, "pagina ontbreekt")
                    return
                self._stuur(200, body, "text/html; charset=utf-8")
            elif pad == "/verbruik":
                status, ruw, fout = _haal_ruw(verbruik_url)
                if status == 200 and ruw is not None:
                    self._stuur(200, ruw, "application/json")
                else:
                    self._json(502, {"fout": fout or "http {}".format(status)})
            elif pad == "/status":
                self._json(200, bewaker.status())
            else:
                self.send_error(404, "onbekend pad")

        def do_POST(self):
            pad = self.path.split("?")[0].rstrip("/")
            # Een eigen header dwingt bij een verzoek vanaf een andere site een
            # CORS-preflight af, en die beantwoorden we niet. Zo kan een
            # willekeurige webpagina in je thuisnet de Hub niet omzetten; de
            # bedieningspagina stuurt de header gewoon mee.
            if self.headers.get("X-Nest-Hub") != "1":
                self.send_error(403, "header X-Nest-Hub ontbreekt")
                return
            if pad == "/toon":
                bewaker.toon()
            elif pad == "/fotolijst":
                bewaker.fotolijst()
            else:
                self.send_error(404, "onbekend pad")
                return
            self._json(200, bewaker.status())

    return Handler


def _lan_ip(richting=None):
    """Het adres waarop de Hub deze machine bereikt.

    Een UDP-socket 'verbinden' met de Hub verstuurt niets, maar laat de kernel
    wel de uitgaande interface kiezen — en dat is precies het adres dat de Hub
    moet gebruiken, ook als Tailscale draait. Zonder Hub-adres nemen we een
    documentatieadres (RFC 5737), net als fake_verbruik_server.py.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((richting or "192.0.2.1", 8009))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _eenmalig(bewaker, thuis, verbruik_url):
    """Meet alles één keer en toon het. Cast niets."""
    print("Scherm-URL voor de Hub : {}".format(bewaker.scherm_url))
    stand, fout = haal_verbruik(verbruik_url)
    if fout:
        print("Verbruik               : FOUT — {}".format(fout))
    else:
        print("Verbruik               : 5u {}%  week {}%  bron {}".format(
            stand.get("five_hour"), stand.get("seven_day"), stand.get("bron")))
    t, reden = thuis(mag_pingen=True)
    print("Thuis                  : {} — {}".format(
        {True: "ja", False: "nee"}.get(t, "onbekend"), reden))
    toestand, app, naam = bewaker.hub.toestand()
    print("Hub                    : {} (app_id={}, naam={})".format(toestand, app, naam))
    if isinstance(bewaker.hub, Hub):
        print("Hub-adres              : {}".format(bewaker.hub.ip))
    print("\nEr is niets gecast; dit was alleen meten.")
    return 0 if (not fout and toestand != "onbereikbaar") else 1


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hub", help="IP-adres van de Nest Hub (uit `catt scan`)")
    p.add_argument("--hub-naam",
                   help="naam uit `catt scan`; om de Hub terug te vinden als zijn adres verandert")
    p.add_argument("--thuis-apparaat", action="append", default=[],
                   help="Tailscale-naam van een apparaat dat 'thuis' betekent; "
                        "mag vaker, of komma-gescheiden")
    p.add_argument("--verbruik", default=VERBRUIK_URL, help="adres van de verbruikserver")
    p.add_argument("--poort", type=int, default=POORT)
    p.add_argument("--adres", default="0.0.0.0",
                   help="bindadres; 0.0.0.0 = bereikbaar vanaf het LAN en Tailscale")
    p.add_argument("--scherm-url",
                   help="adres waarop de Hub de pagina ophaalt (standaard: automatisch)")
    p.add_argument("--stilte", type=int, default=STILTE_S // 60,
                   help="minuten zonder stijging voordat het scherm weggaat")
    p.add_argument("--interval", type=int, default=INTERVAL_S,
                   help="seconden tussen twee rondes")
    p.add_argument("--droog", action="store_true", help="beslissen en loggen, niets casten")
    p.add_argument("--eenmalig", action="store_true",
                   help="1x alles meten, tonen en stoppen (cast niets)")
    p.add_argument("--nep-hub", action="store_true", help="testen zonder echte Hub")
    p.add_argument("--altijd-thuis", action="store_true",
                   help="thuis-check overslaan (om lokaal te testen)")
    args = p.parse_args()

    namen = [n.strip() for arg in args.thuis_apparaat for n in arg.split(",") if n.strip()]
    hub_naam = (args.hub_naam or "").strip() or None
    if args.nep_hub:
        hub = NepHub()
    elif not args.hub and not hub_naam:
        p.error("geef --hub (IP-adres) en/of --hub-naam op, of --nep-hub")
    else:
        # Meteen duidelijk falen als pychromecast ontbreekt, in plaats van
        # elke minuut een Hub die "onbereikbaar" lijkt.
        try:
            import pychromecast  # noqa: F401
        except ImportError:
            p.error("pychromecast ontbreekt; start met de venv-python "
                    "(~/nest-hub/venv/bin/python) of zie LEESMIJ.md")
        hub = Hub(args.hub, hub_naam)

    scherm_url = args.scherm_url or "http://{}:{}/scherm".format(_lan_ip(args.hub), args.poort)
    thuis = ThuisCheck(namen, altijd=args.altijd_thuis)
    bewaker = Bewaker(hub, lambda: haal_verbruik(args.verbruik), thuis, scherm_url,
                      stilte_s=args.stilte * 60, droog=args.droog)

    if args.eenmalig:
        return _eenmalig(bewaker, thuis, args.verbruik)

    server = ThreadingHTTPServer((args.adres, args.poort), _maak_handler(bewaker, args.verbruik))
    _log("nest-hub-bewaker op http://{}:{}/ — scherm voor de Hub: {}".format(
        args.adres, args.poort, scherm_url))
    _log("thuis-apparaten: {}; stilte {} min; {}".format(
        ", ".join(namen) or ("(altijd thuis)" if args.altijd_thuis else "GEEN"),
        args.stilte, "DROOG (cast niets)" if args.droog else "cast echt"))
    threading.Thread(target=bewaker.draai, args=(args.interval,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("gestopt")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
