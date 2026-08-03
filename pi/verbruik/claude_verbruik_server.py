"""Verbruikserver — serveert je Claude-limieten kaal op het LAN.

Draait op de machine die de Claude Code-login heeft (bij Jörg: TaSc-Pi5),
leest daar het token en vraagt de actuele limietstand op bij Anthropic.
De Cardputer-app `claude_verbruik` pollt dit endpoint; die draagt zelf
géén token en praat niet met api.anthropic.com.

    GET /verbruik  ->  {"five_hour": 42.0, "seven_day": 18.0,
                        "fh_reset_in_s": 4680, "sd_reset_in_s": 275000,
                        "bron": "live", "ts": 1785261682}

### Wat dit script wel en niet doet

WEL   het token uit ~/.claude/.credentials.json lezen (platte tekst, door
      Claude Code zelf geschreven — geen ontsleuteling van een kluis) en
      daarmee GET /api/oauth/usage aanroepen.
NIET  het token verversen. Dat roteert het refresh-token en kan de login
      slopen; het hoort thuis in `ververs-claude-token.py`, dat als eigen
      systemd-timer draait. Deze server is read-only op de credentials.
NIET  het token uitserveren of loggen. Alleen percentages gaan het net op.

### Resettijd als seconden, niet als datum

De API geeft `resets_at` als ISO-tijdstip. Wij rekenen dat hier om naar
**seconden vanaf nu**, zodat de Cardputer geen ISO hoeft te parseren en
zijn eigen klok (die van NTP komt en er soms naast zit) er niet toe doet.
Het toestel telt lokaal verder af tussen twee polls.

### Cache en rate limit

De API heeft een eigen request-limiet, dus we halen hoogstens elke
CACHE_S seconden verse cijfers op; alle pollende clients binnen dat
venster krijgen dezelfde waarden. Het toestel pollt elke 45 s, wat dus
ruim binnen de cache valt — precies de bedoeling.

Op 2026-08-03 liep dit toch tegen een HTTP 429 aan, en de server bleef
daarna in hetzelfde tempo doorvragen. Dat helpt niet en kan de blokkade in
stand houden, want een limiet telt geweigerde verzoeken vaak gewoon mee.
Daarom nu twee dingen: de cache staat standaard op 5 minuten (~288 in
plaats van ~960 verzoeken per dag, en voor een venster van 5 uur is dat
ruim vers genoeg), en bij een 429 wachten we — zo lang als de server zelf
in `Retry-After` aangeeft, en anders oplopend van 5 minuten tot maximaal
een uur. Na een geslaagde poging valt alles terug op het normale ritme.

### Terugval

Anders dan de laptop-widget is er hier GEEN tweede bron: de lokale cache
van de Claude-desktopapp (`plan-usage-history.json`) bestaat alleen op
Windows. Lukt het ophalen niet, dan serveren we de laatst bekende cijfers
met een afwijkende `bron`, zodat het toestel kan tonen dat het om oude
gegevens gaat. Vlak na een herstart zonder geldig token is er niets te
tonen en blijven de velden leeg (het toestel toont dan "--").

`bron` zegt hoe vers de cijfers zijn, en waarom niet:

    live        vers van de API
    gemeten     ophalen mislukt, dit zijn de laatst bekende cijfers
    geweigerd   er is een token, maar de API accepteert het niet (401/403)
                -> `claude auth login` op deze machine
    geen-token  geen bruikbaar credentialsbestand gevonden

"geweigerd" en "geen-token" staan er bewust apart in. Een token met een
expiry ver in de toekomst kan tóch geweigerd worden, en dan helpt afwachten
niet — dat onderscheid ontbrak tot 2026-08-03 en maakte de diagnose
onnodig lastig.

Gebruik:

    python3 claude_verbruik_server.py                 # 0.0.0.0:8091
    python3 claude_verbruik_server.py --poort 9000
    python3 claude_verbruik_server.py --eenmalig      # 1x ophalen en tonen
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Zelfde headers als de Claude Code CLI; zonder de beta-header antwoordt
# het endpoint niet. De User-Agent houden we gelijk aan wat de widget op
# de laptop stuurt.
UA = "claude-code/2.1.217"
BETA = "oauth-2025-04-20"

CACHE_S = 300         # niet vaker dan dit bij Anthropic langs
HTTP_TIMEOUT_S = 10

# Wachttijden na een HTTP 429, als de server zelf geen Retry-After meegeeft.
# Verdubbelt per mislukte poging tot het maximum; na succes weer op nul.
BACKOFF_START_S = 300
BACKOFF_MAX_S = 3600

_CRED_PAD = os.path.expanduser("~/.claude/.credentials.json")


def _log(*args):
    """Regel naar stdout met tijdstempel; systemd vangt dit op in de journal.

    Bewust geen token, geen headers en geen ruwe response — alleen wat je
    nodig hebt om te zien of het werkt.
    """
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


# ---- token ----------------------------------------------------------


def _lees_token(pad):
    """Access-token uit het credentialsbestand, of None.

    Geeft ook expiresAt terug zodat we een verlopen token kunnen melden
    zonder er eerst een mislukte API-call tegenaan te gooien.
    """
    try:
        with open(pad, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None, "geen credentialsbestand"
    except (OSError, ValueError) as e:
        return None, None, "credentials onleesbaar: {}".format(e)
    oauth = (data or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if not token:
        return None, expires, "geen accessToken (log in met: claude auth login)"
    return token, expires, None


# ---- API ------------------------------------------------------------


def _naar_epoch(waarde):
    """ISO-tijdstip of epoch -> epoch-seconden (float), of None.

    `resets_at` komt als ISO 8601 binnen, meestal met een 'Z'. Oudere
    Python-versies struikelen over die Z in fromisoformat, dus die
    vervangen we zelf. Een kaal getal accepteren we ook, voor het geval
    het formaat ooit verandert.
    """
    if waarde is None:
        return None
    if isinstance(waarde, (int, float)):
        return float(waarde)
    tekst = str(waarde).strip()
    if not tekst:
        return None
    try:
        return float(tekst)
    except ValueError:
        pass
    try:
        if tekst.endswith("Z"):
            tekst = tekst[:-1] + "+00:00"
        dt = datetime.fromisoformat(tekst)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _resterend(waarde):
    """Seconden tot `waarde`, afgerond, nooit negatief. None blijft None."""
    epoch = _naar_epoch(waarde)
    if epoch is None:
        return None
    return max(int(epoch - time.time()), 0)


def _api_melding(e):
    """Korte foutbeschrijving uit het JSON-antwoord van de API.

    Bij een geweigerd token staat daar bijvoorbeeld
    `authentication_error: Invalid authentication credentials` — precies wat je
    in de journal wilt zien, want "geweigerd" alleen zegt niet waarom.
    """
    try:
        data = json.loads(e.read().decode("utf-8", "replace"))
        f = (data or {}).get("error") or {}
        soort = f.get("type") or "?"
        tekst = f.get("message") or ""
        return "{}: {}".format(soort, tekst).strip(": ")
    except (ValueError, OSError, AttributeError):
        return "geen leesbare foutmelding"


def _retry_after(e):
    """Seconden uit een Retry-After-header, of None als die er niet bruikbaar is.

    De header mag twee vormen hebben: een aantal seconden, of een HTTP-datum.
    We accepteren beide en negeren onzin — dan valt de aanroeper terug op zijn
    eigen oplopende wachttijd, wat altijd een veilige ondergrens is.
    """
    ruw = e.headers.get("Retry-After") if e.headers else None
    if not ruw:
        return None
    ruw = str(ruw).strip()
    try:
        return max(int(float(ruw)), 1)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(ruw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(int(dt.timestamp() - time.time()), 1)
    except (TypeError, ValueError, IndexError):
        pass
    return None


def _haal_verbruik(token):
    """Roep het usage-endpoint aan. ``(stand, None, None)`` of ``(None, fout, wacht)``.

    ``wacht`` is alleen gevuld bij een 429 waarbij de server een Retry-After
    meegaf; anders None.

    `utilization` is al een percentage (0-100) — zo gebruikt de widget op
    de laptop het ook, dus we schalen niets.
    """
    req = urllib.request.Request(USAGE_URL, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("anthropic-beta", BETA)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            ruw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "http 429", _retry_after(e)
        if e.code in (401, 403):
            # Credentials geweigerd. Dit is NIET hetzelfde als "geen token":
            # het bestand kan een token bevatten met een expiry ver in de
            # toekomst en tóch geweigerd worden (zo ging het op 2026-08-03).
            # Die twee eerder op één hoop gooien maakte de diagnose lastiger,
            # want het scherm zei "geen-token" terwijl er wél een token was.
            # Niet zelf verversen — dat is het werk van ververs-claude-token.py.
            return None, "http {}: {}".format(e.code, _api_melding(e)), None
        return None, "http {}".format(e.code), None
    except (urllib.error.URLError, OSError) as e:
        return None, "netwerk: {}".format(e), None

    try:
        data = json.loads(ruw)
        vh = data.get("five_hour") or {}
        wk = data.get("seven_day") or {}
        return {
            "five_hour": float(vh.get("utilization")),
            "seven_day": float(wk.get("utilization")),
            "fh_reset_in_s": _resterend(vh.get("resets_at")),
            "sd_reset_in_s": _resterend(wk.get("resets_at")),
        }, None, None
    except (ValueError, TypeError, AttributeError) as e:
        return None, "antwoord onbegrijpelijk: {}".format(e), None


# ---- cache ----------------------------------------------------------


def _is_geweigerd(fout):
    """True bij een 401/403 — token aanwezig maar niet geaccepteerd."""
    return bool(fout) and (fout.startswith("http 401") or fout.startswith("http 403"))


class Verbruik:
    """Haalt op met een cache en onthoudt de laatst gelukte stand.

    De laatst bekende cijfers blijven staan als het ophalen faalt; dan
    verandert alleen `bron`, zodat het toestel oude gegevens als oud kan
    tonen in plaats van leeg te vallen.
    """

    def __init__(self, cred_pad=_CRED_PAD, cache_s=CACHE_S):
        self.cred_pad = cred_pad
        self.cache_s = cache_s
        self._slot = threading.Lock()
        self._stand = None       # laatste gelukte meting
        self._stand_ts = 0.0     # wanneer die gemeten is
        self._laatste_poging = 0.0
        self._bron = "geen-data"
        # Backoff na een 429: vóór _pauze_tot doen we geen poging, en
        # _backoff_s is de wachttijd die we bij een volgende weigering
        # verdubbelen. Beide terug op nul zodra er weer iets lukt.
        self._pauze_tot = 0.0
        self._backoff_s = 0
        # Sleutel van de laatst gelogde toestand, zodat een aanhoudende fout
        # één regel oplevert in plaats van één per cyclus.
        self._laatste_melding = None

    def stand(self):
        with self._slot:
            nu = time.time()
            # Twee remmen: het normale cache-interval, en een eventuele
            # backoff-pauze na een rate limit. Die tweede overrulet de eerste.
            if nu - self._laatste_poging >= self.cache_s and nu >= self._pauze_tot:
                self._laatste_poging = nu
                self._verzamel()
            return self._payload()

    def _meld(self, tekst, sleutel):
        """Log alleen wanneer de toestand verandert.

        De vorige versie vergeleek de foutmelding met ``self._bron``, en die
        twee zijn nooit gelijk ("http 429" tegen "fout") — daardoor liep de
        journal vol met een identieke regel per cyclus. De sleutel staat nu
        los van de bron, zodat ook een oplopende backoff netjes één regel per
        stap geeft.
        """
        if self._laatste_melding != sleutel:
            _log(tekst)
        self._laatste_melding = sleutel

    def _plan_backoff(self, wacht):
        """Bepaal hoe lang we na een 429 niets proberen."""
        if wacht is not None:
            pauze = wacht          # de server weet het beter dan wij
        elif self._backoff_s <= 0:
            pauze = BACKOFF_START_S
        else:
            pauze = self._backoff_s * 2
        self._backoff_s = min(int(pauze), BACKOFF_MAX_S)
        self._pauze_tot = time.time() + self._backoff_s

    def _reset_backoff(self):
        self._backoff_s = 0
        self._pauze_tot = 0.0

    def _verzamel(self):
        token, _expires, fout = _lees_token(self.cred_pad)
        if token is None:
            self._meld("token niet bruikbaar: {}".format(fout), "geen-token")
            self._bron = "geen-token"
            return

        stand, fout, wacht = _haal_verbruik(token)
        if stand is None:
            if fout == "http 429":
                # De API zegt expliciet dat we te vaak vragen. In hetzelfde
                # tempo doorgaan lost niets op en houdt de blokkade mogelijk in
                # stand, dus we wachten eerst — en loggen hoe lang, zodat in de
                # journal te zien is wanneer hij het opnieuw probeert.
                self._plan_backoff(wacht)
                bron_van_de_wachttijd = "server" if wacht is not None else "oplopend"
                self._meld(
                    "ophalen mislukt: rate limit (429); volgende poging over "
                    "{} min ({})".format(max(1, self._backoff_s // 60),
                                         bron_van_de_wachttijd),
                    "429:{}".format(self._backoff_s))
            else:
                # Andere fouten komen niet door ons tempo, dus daar blijven we
                # gewoon op het cache-ritme opnieuw proberen.
                self._reset_backoff()
                self._meld("ophalen mislukt: {}".format(fout), fout)
            # "geweigerd" is bewust een eigen bron: er ís een token, het wordt
            # alleen niet geaccepteerd. Dat vraagt om `claude auth login`, niet
            # om afwachten — en dat verschil hoort op het scherm te staan.
            self._bron = "geweigerd" if _is_geweigerd(fout) else "fout"
            return

        self._reset_backoff()
        self._meld("live cijfers opgehaald", "live")
        self._stand = stand
        self._stand_ts = time.time()
        self._bron = "live"

    def _payload(self):
        if self._stand is None:
            # Nog nooit iets gelukt: lege velden, het toestel toont "--".
            return {
                "five_hour": None, "seven_day": None,
                "fh_reset_in_s": None, "sd_reset_in_s": None,
                "bron": self._bron, "ts": int(time.time()),
            }
        # De aftelling is gemeten op _stand_ts; corrigeer voor de tijd die
        # sindsdien verstreken is, anders loopt de klok op het toestel na
        # bij een cache-hit.
        verstreken = int(time.time() - self._stand_ts)

        def rest(v):
            return None if v is None else max(v - verstreken, 0)

        return {
            "five_hour": self._stand["five_hour"],
            "seven_day": self._stand["seven_day"],
            "fh_reset_in_s": rest(self._stand["fh_reset_in_s"]),
            "sd_reset_in_s": rest(self._stand["sd_reset_in_s"]),
            # "live" alleen als de laatste poging ook echt lukte; anders
            # "gemeten" (oude cijfers) of "geen-token".
            "bron": self._bron if self._bron != "fout" else "gemeten",
            "ts": int(self._stand_ts),
        }


# ---- HTTP -----------------------------------------------------------


def _maak_handler(verbruik):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return  # de journal is al gevuld door _log

        def do_GET(self):
            pad = self.path.split("?")[0].rstrip("/") or "/"
            if pad not in ("/verbruik", "/"):
                self.send_error(404, "alleen /verbruik")
                return
            body = json.dumps(verbruik.stand()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--poort", type=int, default=8091)
    p.add_argument("--adres", default="0.0.0.0",
                   help="bindadres; 0.0.0.0 = bereikbaar vanaf het LAN")
    p.add_argument("--credentials", default=_CRED_PAD)
    p.add_argument("--cache", type=int, default=CACHE_S,
                   help="seconden tussen twee API-calls")
    p.add_argument("--eenmalig", action="store_true",
                   help="1x ophalen, tonen en stoppen (voor een snelle test)")
    args = p.parse_args()

    verbruik = Verbruik(cred_pad=args.credentials, cache_s=args.cache)

    if args.eenmalig:
        print(json.dumps(verbruik.stand(), indent=2))
        # Exitcode zegt of het echt live was, zodat een testscript erop
        # kan sturen zonder de JSON te parsen.
        return 0 if verbruik._bron == "live" else 1

    server = ThreadingHTTPServer((args.adres, args.poort), _maak_handler(verbruik))
    _log("verbruikserver op http://{}:{}/verbruik (cache {}s)".format(
        args.adres, args.poort, args.cache))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("gestopt")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
