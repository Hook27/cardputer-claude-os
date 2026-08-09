"""Houdt het Claude Code OAuth-token in ~/.claude/.credentials.json geldig.

Linux-port van `ververs-claude-token.ps1` (Windows/SP6). Zelfde logica,
zelfde resultaatcodes, zelfde veiligheidskleppen — alleen de paden en het
starten van de CLI verschillen. Bedoeld als systemd-timer op de Pi die de
verbruikserver draait.

### Waarom dit nodig is

Het access-token verloopt 8 uur na de laatste refresh, en de CLI ververst
het alleen als het binnen 5 minuten verloopt (of al verlopen is). Blind om
de 8 uur iets starten helpt dus niet. Dit script kijkt spotgoedkoop of een
refresh nodig is (leest alleen het JSON-bestand) en start alleen dan de
CLI met een lege prompt:

    claude -p --no-session-persistence     (met lege stdin)

Die lege prompt doorloopt de volledige startup-init — en die roept de
refresh-functie aan vóór elke API-call — maar doet GEEN model-call.
Gevolg: geen tokenverbruik, geen nieuw 5-uursvenster, geen transcript.

De CLI eindigt daarbij met exit 1 en "No messages returned from query".
Dat is verwacht en zegt NIETS over de auth-status: bij een leeggemaakte
login is de uitkomst exact hetzelfde. Of het gelukt is blijkt alleen uit
een opgeschoven `expiresAt`; wát er misging, uit de diagnosebestanden.

Het verversen zelf doet altijd de CLI, nooit dit script.

### Veiligheidskleppen (na de mislukking van 2026-07-26)

Toen weigerde de server het refresh-token, bleef het script het elk
kwartier opnieuw proberen, en maakte de CLI uiteindelijk de login leeg.
Daarom:

  - hoogstens MAX_POGINGEN pogingen per expiry-venster; daarna pauzeert
    het script tot er een nieuw token of een nieuwe login is;
  - elke poging schrijft een CLI-debuglog (laatste 5 blijven bewaard);
  - vóór en ná elke poging gaat er een momentopname naar
    momentopnames.jsonl met tijden en VINGERAFDRUKKEN van de tokens —
    geen tokens zelf — zodat te zien is of iets anders het token heeft
    geroteerd;
  - na een mislukte poging bepaalt `claude auth status --json` of de
    login nog staat; dat is het enige betrouwbare signaal.

### Meting naar aanleiding van 2026-08-05

Die dag ging de login op de Pi verloren. De vingerafdrukketen liet zien dat
niets anders het token had geroteerd, en `refreshTokenExpiresAt` had nog 25
dagen te gaan — en tóch antwoordde de server met
`400: OAuth refresh token is no longer valid`.

Het debuglog van de eerste poging wijst naar onszelf: de CLI meldt
`Passes: Cache stale, ... refreshing in background` en het proces was 0,2 s
later al weg, zonder iets weg te schrijven. Bereikte die achtergrondrefresh de
server wél, dan roteerde het token daar en was het onze kopie die dood
achterbleef — waarna de tweede poging de CLI de login liet wissen.

Op 2026-08-09 gebeurde het opnieuw, en toen gaf het volledige debuglog de
doorslag. De CLI meldt letterlijk "Starting background startup prefetches",
"refreshing in background" en "running fully async (nonblocking)", en begon
**68 ms** na het versturen van het verzoek al af te sluiten. De regel
`[claudeai-mcp] Fetching from .../v1/mcp_servers` — die in een geslaagde run
wél staat — ontbrak volledig: er is nooit iets teruggekomen.

Vandaar `start_cli()`: stdin blijft een paar seconden open, zodat het proces
op zijn prompt wacht terwijl die achtergrondrefresh landt. Een run die het
bestand tóch ongemoeid laat krijgt nog steeds de eigen uitkomst `onveranderd`,
en de looptijd wordt nu ook bij een geslaagde refresh gelogd — dat
vergelijkingspunt ontbrak, waardoor de eerste meting stuurloos was.

### Gezondheidscontrole (na de storing van 2026-08-03)

Een vers token is niet hetzelfde als een wérkend token: die dag bleef dit
script uren `[OK] Token ververst` melden terwijl de API de credentials
weigerde (401 met een `expiresAt` ver in de toekomst). We bewaakten dus of
het token ververst werd, niet of het werkte.

Daarom vraagt het script nu bij elke run aan de lokale verbruikserver hoe
het gaat, en zet dat in dezelfde regel die je toch al leest. Gaat er iets
mis, dan wordt het een `[WAARSCHUWING]` met de te nemen actie erbij.

Bewust géén eigen API-call: we lezen de toestand van de server, die de call
toch al doet. Een tweede poller zou het rate-limit-venster kunnen raken —
precies waardoor het die dag misging.

### Resultaat

Laatste regel op stdout is "RESULTAAT: <code>":

    geldig        token nog ruim geldig, niets gedaan            (exit 0)
    refresh-nodig refresh nodig, CLI niet gestart wegens --dry-run (exit 0)
    ververst      refresh gelukt, nieuwe expiry weggeschreven    (exit 0)
    onveranderd   CLI gedraaid maar liet het bestand ongemoeid   (exit 1)
    mislukt       CLI gedraaid, wel iets gewijzigd, geen nieuwe expiry (exit 1)
    gepauzeerd    pogingenlimiet voor dit venster bereikt        (exit 1)
    login-nodig   login weg of geweigerd: claude auth login      (exit 1)
    fout          credentialsbestand ontbreekt of is onleesbaar  (exit 1)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Standaardwaarden; alle paden zijn met vlaggen te overschrijven voor tests.
MARGE_MINUTEN = 4      # bewust NET binnen de 5 min die de CLI zelf aanhoudt
MAX_POGINGEN = 2
TIMEOUT_S = 120
MAX_LOG_BYTES = 200 * 1024
WAARSCHUW_DAGEN = 2
BEWAAR_DEBUG = 5
# Lokale verbruikserver, om te controleren of het token niet alleen vers maar
# ook werkzaam is. Leeg maken (of --server "") schakelt die controle uit.
SERVER_URL = "http://127.0.0.1:8091/verbruik"
# Seconden dat we stdin openhouden nadat de CLI is gestart. Zie start_cli()
# voor waarom dat nodig is; ruim genomen, want de kosten zijn nul.
STDIN_OPEN_S = 5.0
# Een verbindingsfout mag een tweede kans krijgen: vlak na een herstart is de
# poort nog niet open, en een loos alarm ondermijnt de bewaking.
SERVER_POGINGEN = 2
SERVER_PAUZE_S = 3.0

_HOME = os.path.expanduser("~")
_CRED_PAD = os.path.join(_HOME, ".claude", ".credentials.json")
# Bewust in de XDG-state-map: buiten elke cloudmap, en het overleeft een
# reboot (anders dan /tmp), wat nodig is voor de pogingenteller.
_STAAT_MAP = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(_HOME, ".local", "state")),
    "claude-token-refresh")


class Ctx:
    """Bundelt de paden zodat de testvariant ze in één keer kan omzetten."""

    def __init__(self, cred, staat_map, claude=None):
        self.cred = cred
        self.staat_map = staat_map
        self.diagnose = os.path.join(staat_map, "diagnose")
        self.log = os.path.join(staat_map, "ververs-claude-token.log")
        self.status = os.path.join(staat_map, "ververs-status.json")
        self.werkmap = os.path.join(staat_map, "werkmap")
        self.claude = claude


# --- logboek ----------------------------------------------------------


def schrijf_log(ctx, niveau, bericht):
    regel = "{} [{}] {}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), niveau, bericht)
    try:
        os.makedirs(ctx.staat_map, exist_ok=True)
        if os.path.exists(ctx.log) and os.path.getsize(ctx.log) > MAX_LOG_BYTES:
            oud = ctx.log + ".1"
            if os.path.exists(oud):
                os.remove(oud)
            os.replace(ctx.log, oud)
        with open(ctx.log, "a", encoding="utf-8") as f:
            f.write(regel + "\n")
    except OSError:
        pass  # loggen mag de refresh nooit blokkeren
    print(regel, file=sys.stderr)


def heartbeat_nodig(ctx, niveau="OK"):
    """Hoogstens 1x per uur een regel van dit niveau.

    Zonder deze rem staan er 96 identieke OK-regels per dag in de log en zie
    je de interessante regels niet meer staan. Ook waarschuwingen gaan er
    doorheen: een aanhoudend probleem hoort te blijven melden, maar per uur,
    niet per kwartier.

    Zoekt de meest recente regel van dit niveau en negeert wat ertussen staat.
    (De eerdere versie stopte bij de eerste regel van een ánder niveau, wat
    betekende dat één waarschuwing de OK-rem meteen weer vrijgaf.)
    """
    patroon = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[" + re.escape(niveau) + r"\]"
    try:
        with open(ctx.log, "r", encoding="utf-8") as f:
            regels = f.readlines()
    except OSError:
        return True
    for regel in reversed(regels):
        m = re.match(patroon, regel)
        if m:
            try:
                t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return True
            return (datetime.now() - t).total_seconds() >= 3600
    return True


# --- gezondheid van de verbruikserver --------------------------------


def server_toestand(url, timeout=4, pogingen=SERVER_POGINGEN,
                    pauze_s=SERVER_PAUZE_S):
    """Vraag de verbruikserver hoe het met hem gaat.

    Bewust GEEN eigen API-call: we lezen de toestand van de server, die de
    call toch al doet. Een tweede poller zou het rate-limit-venster kunnen
    raken — precies waardoor het op 2026-08-03 misging.

    Een verbindingsfout krijgt een tweede kans na ``pauze_s`` seconden. Een
    herstart van de service is genoeg om de eerste poging te laten stuiten
    (systemd meldt de unit actief zodra het proces draait, niet zodra het de
    poort heeft geopend), en bewaking die af en toe onterecht alarm slaat leer
    je negeren — dan doet ze niet meer waarvoor ze bedoeld is.

    Een server die wél antwoordt maar geen live cijfers heeft, melden we
    meteen: daar is niets tijdelijks aan.

    Geeft ``(gezond, tekst)``. ``gezond`` is None wanneer er niets te zeggen
    valt (controle uitgeschakeld), True bij verse cijfers, en False als de
    server geen live data heeft of na alle pogingen niet reageert.
    """
    if not url:
        return None, ""

    laatste_fout = "verbruikserver reageert niet"
    for poging in range(1, max(1, pogingen) + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            laatste_fout = "verbruikserver reageert niet ({})".format(e)
            if poging < pogingen:
                time.sleep(pauze_s)
            continue

        bron = (data or {}).get("bron") or "?"
        if bron == "live":
            return True, "verbruikserver: live"
        if bron == "geweigerd":
            # Dit is het scenario dat we tot 2026-08-03 niet zagen: het token
            # wordt keurig ververst, maar de API accepteert het niet meer.
            return False, ("verbruikserver krijgt 'geweigerd' -- het token wordt "
                           "wel ververst maar niet geaccepteerd. Nodig: claude auth login")
        return False, "verbruikserver: geen live cijfers (bron '{}')".format(bron)

    return False, laatste_fout


def stop_met(code, exitcode=0):
    print("RESULTAAT: " + code)
    sys.exit(exitcode)


# --- credentials ------------------------------------------------------


def lees_oauth(pad):
    with open(pad, "r", encoding="utf-8") as f:
        return (json.load(f) or {}).get("claudeAiOauth")


def vingerafdruk(waarde):
    """Korte SHA-256: genoeg om een tokenwissel te zien, zonder het token
    zelf ergens vast te leggen."""
    if not waarde:
        return "(leeg)"
    return hashlib.sha256(waarde.encode("utf-8")).hexdigest()[:12]


def schrijf_momentopname(ctx, oauth, fase, pogingnaam):
    try:
        os.makedirs(ctx.diagnose, exist_ok=True)
        regel = {
            "tijd": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "poging": pogingnaam,
            "fase": fase,
            "expiresAt": oauth.get("expiresAt") if oauth else None,
            "refreshTokenExpiresAt": oauth.get("refreshTokenExpiresAt") if oauth else None,
            "accessTokenVa": vingerafdruk(oauth.get("accessToken")) if oauth else "(geen bestand)",
            "refreshTokenVa": vingerafdruk(oauth.get("refreshToken")) if oauth else "(geen bestand)",
        }
        with open(os.path.join(ctx.diagnose, "momentopnames.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(regel) + "\n")
    except OSError:
        pass


def ruim_debuglogs_op(ctx):
    try:
        bestanden = [os.path.join(ctx.diagnose, n)
                     for n in os.listdir(ctx.diagnose)
                     if n.startswith("cli-") and n.endswith(".log")]
        bestanden.sort(key=os.path.getmtime, reverse=True)
        for pad in bestanden[BEWAAR_DEBUG:]:
            os.remove(pad)
    except OSError:
        pass


_DEBUG_PATROON = re.compile(
    r"oauth|invalid_grant|refresh_token|refresh token|token refresh|\b401\b|"
    r"\b403\b|Unauthorized|Forbidden|not logged in|credentials\.json|"
    r"claudeai-mcp|\[Bootstrap\]|Authorization", re.IGNORECASE)


def cli_sporen(pad):
    """Wat het CLI-debuglog zegt over de refreshpoging zelf.

    Geeft ``(duur_s, achtergrond_gestart, oauth_gezien)``.

    De duur is het verschil tussen de eerste en laatste tijdstempel in het log,
    oftewel hoe lang het CLI-proces heeft geleefd. Dat is het getal waar het om
    draait bij het vermoeden van 2026-08-05: de CLI start de refresh op de
    achtergrond ("refreshing in background") en onze lege prompt geeft hem
    verder niets te doen, dus hij kan vertrekken vóór die refresh geland is.
    Bereikte de refresh de server toch, dan is het refresh-token daar geroteerd
    en dus dood, terwijl het nieuwe nooit is opgeslagen — en dan sloopt de
    vólgende poging de login.

    Als stille no-ops samenvallen met een korte looptijd, klopt dat verhaal.
    Zo niet, dan moeten we een andere kant op kijken.
    """
    try:
        with open(pad, "r", encoding="utf-8", errors="replace") as f:
            regels = f.readlines()
    except OSError:
        return None, False, False

    stempels = []
    for regel in regels:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z", regel)
        if m:
            try:
                stempels.append(
                    datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f"))
            except ValueError:
                pass
    duur = (stempels[-1] - stempels[0]).total_seconds() if len(stempels) >= 2 else None

    tekst = "".join(regels).lower()
    achtergrond = "refreshing in background" in tekst
    oauth_gezien = ("oauth refresh" in tekst) or ("[bootstrap] fetching" in tekst)
    return duur, achtergrond, oauth_gezien


def debug_hoogtepunten(pad):
    """Regels uit het CLI-debuglog die iets over auth zeggen.

    De eigen mapnaam wordt eerst weggestreept: "claude-token-refresh"
    bevat het woord refresh en zou anders elke padregel laten meeliften.
    """
    try:
        with open(pad, "r", encoding="utf-8", errors="replace") as f:
            regels = f.readlines()
    except OSError:
        return []
    treffers = []
    for regel in regels:
        kaal = regel.replace("claude-token-refresh", "")
        if _DEBUG_PATROON.search(kaal):
            treffers.append(re.sub(r"\s+", " ", regel.strip())[:180])
    return treffers[-6:]


# --- pogingenteller ---------------------------------------------------


def lees_status(ctx):
    try:
        with open(ctx.status, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def schrijf_status(ctx, venster, pogingen):
    try:
        os.makedirs(ctx.staat_map, exist_ok=True)
        with open(ctx.status, "w", encoding="utf-8") as f:
            json.dump({"venster": venster, "pogingen": pogingen,
                       "bijgewerkt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
    except OSError:
        pass


# --- CLI --------------------------------------------------------------


def vind_claude(expliciet=None):
    if expliciet:
        return expliciet if os.path.exists(expliciet) else None
    gevonden = shutil.which("claude")
    if gevonden:
        return gevonden
    for kandidaat in (
        os.path.join(_HOME, ".local", "bin", "claude"),
        os.path.join(_HOME, ".npm-global", "bin", "claude"),
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ):
        if os.path.exists(kandidaat):
            return kandidaat
    return None


def is_ingelogd(exe, werkmap):
    """True/False, of None als het niet vast te stellen was.

    `claude auth status --json` leest alleen lokale state (ververst dus
    niets) en is het enige betrouwbare signaal of de login nog staat.
    """
    try:
        r = subprocess.run([exe, "auth", "status", "--json"],
                           cwd=werkmap, capture_output=True, timeout=60)
        if not r.stdout:
            return None
        return bool(json.loads(r.stdout.decode("utf-8", "replace")).get("loggedIn"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def start_cli(exe, werkmap, debug_pad, timeout_s, stdin_open_s):
    """Start de CLI met een lege prompt, maar sluit stdin niet meteen.

    De CLI vuurt zijn OAuth-refresh af als achtergrondtaak en wacht daar niet
    op — het debuglog zegt het met zoveel woorden: "Starting background startup
    prefetches", "refreshing in background", "running fully async
    (nonblocking)". Met een stdin die direct sluit heeft hij daarna niets meer
    te doen en vertrekt hij binnen een halve seconde. Op 2026-08-09 begon het
    afsluiten **68 ms** nadat het verzoek de deur uit was, en een retour naar
    api.anthropic.com duurt vanaf een Pi een veelvoud daarvan.

    Landt het antwoord te laat, dan heeft de server het refresh-token wél
    geroteerd terwijl wij het nieuwe nooit opslaan — en dan is onze kopie dood.
    De volgende poging biedt dat dode token aan, krijgt een 400, en de CLI wist
    de login. Dat kostte drie logins in een week.

    Door de pijp een paar seconden open te houden blijft het proces wachten op
    zijn prompt en krijgt die achtergrondrefresh de tijd om te landen en
    weggeschreven te worden. Er gaat nog steeds geen prompt naartoe, dus nog
    steeds geen model-call, geen tokenverbruik en geen nieuw 5-uursvenster.

    Geeft ``(exitcode, looptijd_s)``. Gooit ``subprocess.TimeoutExpired`` door.
    """
    begin = time.monotonic()
    proc = subprocess.Popen(
        [exe, "-p", "--no-session-persistence", "--debug-file", debug_pad],
        cwd=werkmap, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # In stapjes wachten, zodat we niet nodeloos blijven zitten als de CLI
        # om een andere reden al klaar is.
        einde = time.monotonic() + stdin_open_s
        while time.monotonic() < einde and proc.poll() is None:
            time.sleep(0.1)
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # proces was er al niet meer
        proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate()
        except Exception:
            pass
        raise
    return proc.returncode, time.monotonic() - begin


def duur(minuten):
    if minuten < 0:
        return "{:.0f} min geleden verlopen".format(abs(minuten))
    if minuten < 90:
        return "nog {:.0f} min geldig".format(minuten)
    return "nog {:.1f} uur geldig".format(minuten / 60)


def klok(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


# --- hoofdstroom ------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--marge", type=int, default=MARGE_MINUTEN,
                   help="ververs zodra het token nog minder dan dit aantal minuten geldig is")
    p.add_argument("--max-pogingen", type=int, default=MAX_POGINGEN)
    p.add_argument("--dry-run", action="store_true",
                   help="bepaal wel de actie, start de CLI niet")
    p.add_argument("--credentials", default=_CRED_PAD)
    p.add_argument("--staat-map", default=_STAAT_MAP)
    p.add_argument("--claude", default=None, help="pad naar de claude-binary")
    p.add_argument("--timeout", type=int, default=TIMEOUT_S)
    p.add_argument("--stdin-open", type=float, default=STDIN_OPEN_S,
                   help="seconden dat stdin openblijft, zodat de CLI niet "
                        "vertrekt voor zijn achtergrondrefresh geland is")
    p.add_argument("--server", default=SERVER_URL,
                   help="verbruikserver om de gezondheid bij op te vragen; "
                        "leeg laten schakelt die controle uit")
    p.add_argument("--server-pogingen", type=int, default=SERVER_POGINGEN,
                   help="aantal pogingen voor de gezondheidscontrole")
    p.add_argument("--server-pauze", type=float, default=SERVER_PAUZE_S,
                   help="seconden tussen die pogingen")
    args = p.parse_args()

    ctx = Ctx(args.credentials, args.staat_map, args.claude)

    # --- 1. Credentials inlezen ---------------------------------------
    if not os.path.exists(ctx.cred):
        schrijf_log(ctx, "FOUT", "Credentialsbestand niet gevonden: {} -- log "
                    "eenmalig in met: claude auth login".format(ctx.cred))
        stop_met("fout", 1)
    try:
        oauth = lees_oauth(ctx.cred)
    except (OSError, ValueError) as e:
        schrijf_log(ctx, "FOUT", "Kan credentialsbestand niet lezen of parsen: {}".format(e))
        stop_met("fout", 1)
    if not oauth or oauth.get("expiresAt") is None:
        schrijf_log(ctx, "FOUT", "Geen claudeAiOauth/expiresAt in {} -- log "
                    "eenmalig in met: claude auth login".format(ctx.cred))
        stop_met("fout", 1)

    nu_ms = int(time.time() * 1000)
    expires_at = int(oauth["expiresAt"])
    resterend_min = (expires_at - nu_ms) / 60000.0

    rt_ms = oauth.get("refreshTokenExpiresAt")
    rt_ms = int(rt_ms) if rt_ms else None
    rt_dagen = (rt_ms - nu_ms) / 86400000.0 if rt_ms else None

    # Puur informatief: dit veld bleek geen betrouwbare voorspeller — op
    # 2026-07-26 was het token al geweigerd terwijl hier nog acht dagen stond.
    if rt_dagen is not None and 0 < rt_dagen < WAARSCHUW_DAGEN:
        schrijf_log(ctx, "WAARSCHUWING", "Refresh-token verloopt volgens het "
                    "bestand over {:.1f} dag(en) ({}).".format(rt_dagen, klok(rt_ms)))

    # --- 2. Is een refresh nodig? -------------------------------------
    if resterend_min > args.marge:
        # Een geldig token is niet hetzelfde als een wérkend token. Vraag de
        # verbruikserver of hij er nog cijfers mee ophaalt; anders blijft dit
        # script "niets te doen" melden terwijl het scherm al uren hangt.
        gezond, gezondheidstekst = server_toestand(
            args.server, pogingen=args.server_pogingen, pauze_s=args.server_pauze)
        if gezond is False:
            if heartbeat_nodig(ctx, "WAARSCHUWING"):
                schrijf_log(ctx, "WAARSCHUWING", "Token {} (tot {}), maar {}.".format(
                    duur(resterend_min), klok(expires_at), gezondheidstekst))
        elif heartbeat_nodig(ctx):
            achtervoegsel = "; " + gezondheidstekst if gezondheidstekst else ""
            schrijf_log(ctx, "OK", "Token {} (tot {}); niets te doen{}.".format(
                duur(resterend_min), klok(expires_at), achtervoegsel))
        stop_met("geldig", 0)

    # --- 3. Kan de CLI überhaupt verversen? ---------------------------
    if not oauth.get("refreshToken"):
        schrijf_log(ctx, "FOUT", "Geen refresh-token aanwezig (login is "
                    "leeggemaakt). Nodig: claude auth login")
        stop_met("login-nodig", 1)
    if rt_ms is not None and rt_ms <= nu_ms:
        schrijf_log(ctx, "FOUT", "Refresh-token is verlopen op {}. Nodig: "
                    "claude auth login".format(klok(rt_ms)))
        stop_met("login-nodig", 1)

    # --- 4. Pogingenlimiet voor dit expiry-venster --------------------
    status = lees_status(ctx)
    al_geprobeerd = 0
    if status and int(status.get("venster", -1)) == expires_at:
        al_geprobeerd = int(status.get("pogingen", 0))

    if al_geprobeerd >= args.max_pogingen:
        if heartbeat_nodig(ctx):
            schrijf_log(ctx, "OK", "Gepauzeerd: {} pogingen voor dit token gedaan, "
                        "geen daarvan hielp. Wacht op een nieuw token of een nieuwe "
                        "login. Diagnose: {}".format(al_geprobeerd, ctx.diagnose))
        stop_met("gepauzeerd", 1)

    poging = al_geprobeerd + 1
    schrijf_log(ctx, "INFO", "Token {} -- refreshpoging {}/{} via Claude Code CLI.".format(
        duur(resterend_min), poging, args.max_pogingen))

    if args.dry_run:
        schrijf_log(ctx, "INFO", "dry-run: CLI niet gestart.")
        stop_met("refresh-nodig", 0)

    # --- 5. CLI starten met lege prompt -------------------------------
    exe = vind_claude(ctx.claude)
    if not exe:
        schrijf_log(ctx, "FOUT", "claude niet gevonden. Geef het pad op met --claude.")
        stop_met("mislukt", 1)

    # Eigen, lege werkmap: geen project-CLAUDE.md, hooks of local settings
    # die per ongeluk meedoen of aangepast worden.
    os.makedirs(ctx.werkmap, exist_ok=True)
    os.makedirs(ctx.diagnose, exist_ok=True)

    pogingnaam = "{}-p{}".format(datetime.now().strftime("%Y%m%d-%H%M%S"), poging)
    debug_pad = os.path.join(ctx.diagnose, "cli-{}.log".format(pogingnaam))

    schrijf_momentopname(ctx, oauth, "voor", pogingnaam)

    try:
        exitcode, cli_looptijd = start_cli(
            exe, ctx.werkmap, debug_pad, args.timeout, args.stdin_open)
    except subprocess.TimeoutExpired:
        schrijf_log(ctx, "FOUT", "CLI reageerde niet binnen {} s en is "
                    "afgebroken.".format(args.timeout))
        schrijf_status(ctx, expires_at, poging)
        stop_met("mislukt", 1)
    except OSError as e:
        schrijf_log(ctx, "FOUT", "Kan claude niet starten: {}".format(e))
        stop_met("mislukt", 1)

    # Exit 1 + "No messages returned from query" is hier de normale
    # uitkomst: de lege prompt levert bewust geen model-antwoord op. Die
    # uitkomst is identiek bij een kapotte login, dus er valt niets uit
    # af te leiden — vandaar de expiry-controle hieronder.

    # --- 6. Is de expiry echt opgeschoven? ----------------------------
    time.sleep(0.3)
    try:
        na = lees_oauth(ctx.cred)
    except (OSError, ValueError):
        na = None
    schrijf_momentopname(ctx, na, "na", pogingnaam)
    ruim_debuglogs_op(ctx)

    if na and na.get("expiresAt") and int(na["expiresAt"]) > expires_at:
        # Looptijd meeloggen, ook bij succes: zonder dat vergelijkingspunt zegt
        # het getal bij een mislukking niets. Dat gat zat in de meting van
        # 2026-08-05 en maakte die stuurloos.
        schrijf_log(ctx, "OK", "Token ververst; nu geldig tot {} (CLI {:.2f} s).".format(
            klok(int(na["expiresAt"])), cli_looptijd))
        schrijf_status(ctx, int(na["expiresAt"]), 0)
        stop_met("ververst", 0)

    # Geen fout: de CLI vond het (nog) niet nodig omdat het token buiten
    # haar eigen 5-minutenvenster valt. Kan alleen bij een verhoogde --marge.
    if na and na.get("expiresAt"):
        resterend_na = (int(na["expiresAt"]) - int(time.time() * 1000)) / 60000.0
        if resterend_na > 5:
            schrijf_log(ctx, "OK", "CLI vond verversen nog niet nodig; token {}.".format(
                duur(resterend_na)))
            stop_met("geldig", 0)

    # --- 7. Uitzoeken WAAROM het niet lukte ---------------------------
    schrijf_status(ctx, expires_at, poging)

    for regel in debug_hoogtepunten(debug_pad):
        schrijf_log(ctx, "DEBUG", regel)

    # Bleef het bestand volledig ongemoeid, dan is dat een eigen verhaal en
    # geen gewone mislukking: de CLI heeft dan niets weggeschreven, maar kan de
    # server wél bereikt hebben. Zie cli_sporen() voor waarom dat gevaarlijk
    # is. Bewust nog GEEN ander gedrag — eerst meten hoe vaak dit gebeurt en
    # of het samenvalt met een korte looptijd, anders weten we straks niet
    # welke ingreep hielp.
    if (na is not None
            and na.get("expiresAt") == oauth.get("expiresAt")
            and na.get("accessToken") == oauth.get("accessToken")
            and na.get("refreshToken") == oauth.get("refreshToken")):
        # NB: niet 'duur' als naam gebruiken -- dat is de functie hierboven, en
        # een gelijknamige lokale variabele maakt die onbereikbaar in de hele
        # functie (Python bepaalt dat bij het compileren, niet bij het uitvoeren).
        logspan, achtergrond, oauth_gezien = cli_sporen(debug_pad)
        schrijf_log(ctx, "FOUT", (
            "Poging {}/{}: de CLI draaide maar veranderde niets -- expiry en "
            "beide tokens identiek. Proces leefde {:.2f} s, logde {}; "
            "achtergrondrefresh gestart: {}; OAuth-antwoord gezien: {}. "
            "Bereikte die refresh de server wel, dan is het token nu dood en "
            "sloopt de volgende poging de login. Debuglog: {}").format(
                poging, args.max_pogingen, cli_looptijd,
                "{:.2f} s".format(logspan) if logspan is not None else "onbekend",
                "ja" if achtergrond else "nee",
                "ja" if oauth_gezien else "nee",
                debug_pad))
        stop_met("onveranderd", 1)

    if na and (not na.get("refreshToken") or not na.get("accessToken")):
        schrijf_log(ctx, "FOUT", "De CLI heeft de login leeggemaakt (refresh-token "
                    "geweigerd). Nodig: claude auth login. Debuglog: {}".format(debug_pad))
        stop_met("login-nodig", 1)

    ingelogd = is_ingelogd(exe, ctx.werkmap)
    if ingelogd is False:
        schrijf_log(ctx, "FOUT", "Auth-controle: niet meer ingelogd. Nodig: "
                    "claude auth login. Debuglog: {}".format(debug_pad))
        stop_met("login-nodig", 1)

    auth_tekst = "auth-status onbekend" if ingelogd is None else "login staat nog"
    schrijf_log(ctx, "FOUT", "Poging {}/{} mislukt: expiry niet opgeschoven "
                "({}, CLI exit {}). Debuglog: {}".format(
                    poging, args.max_pogingen, auth_tekst, exitcode, debug_pad))
    stop_met("mislukt", 1)


if __name__ == "__main__":
    main()
