"""claude_verbruik — Claude-limieten op de Cardputer-Adv.

Toont je 5-uursvenster en je weekvenster als balk + percentage, met een
lopende aftelklok tot de eerstvolgende reset. Hetzelfde beeld als de
verbruik-widget op de laptop, maar dan glanceable naast je toetsenbord.

### Waar de cijfers vandaan komen

Dit toestel praat NIET met api.anthropic.com en draagt GEEN token. Een
kleine server op de Pi (`pi/verbruik/claude_verbruik_server.py`) heeft de
Claude Code-login, haalt daar de cijfers op en serveert ze kaal op het
LAN. Wij pollen dat endpoint — precies zoals pi_dashboard de Pi's pollt.
Zo blijft het accounttoken op één machine en ziet het toestel alleen
percentages.

Endpoint in `apps/config.py` als ``VERBRUIK_ENDPOINT``, en het antwoord
ziet er zo uit:

    {"five_hour": 42.0, "seven_day": 18.0,
     "fh_reset_in_s": 4680, "sd_reset_in_s": 275000,
     "bron": "live", "ts": 1753...}

``bron`` is "live" (vers van de API), "gemeten"/"geen-token" (de server
kon niet verversen en geeft z'n laatst bekende cijfers) of "nep" (de
offline testserver). We tonen dat in de kop, zodat je nooit een oud
getal voor vers aanziet.

De server rekent de resettijd om naar **seconden vanaf nu**, zodat wij
geen ISO-datums hoeven te parseren en de klok van het toestel er niet toe
doet — we tellen lokaal af vanaf het moment van ontvangst.

### Layout

Zelfde chrome als de rest van de bundel: 20 px DARK-kop met ORANJE
hairline, inhoud daaronder, hintstrip onderaan. Per venster een rij met
label + percentage (size 2), een balk, en de aftelklok eronder. Kleur
loopt groen -> geel -> rood mee met het percentage.

### Bediening

  R          nu verversen (in plaats van wachten op de volgende poll)
  Q / ESC    terug naar het launcher-menu

### Port notes

- **Netwerk.** WiFi via ``network.WLAN(STA_IF)`` + de ``wifi_event``
  helper, net als pi_dashboard; de launcher heeft meestal al verbonden.
  HTTP met de firmware-``requests``.
- **Cadans.** Pollen elke 45 s (de server cachet zelf ~90 s, vaker heeft
  dus geen zin), aftelklok elke seconde bijgewerkt, toetsen elke 40 ms —
  dus nooit slapen tot de volgende poll.
- **Font.** DejaVu9, size 1 voor labels, size 2 voor de percentages.
"""

import sys
import time

import M5
import network
from hardware import MatrixKeyboard


# Palet gelijk aan de rest van de bundel.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xCC4444
_GREEN = 0x4CAF50
_YELLOW = 0xE0B341

_LCD = M5.Lcd

_W = 240
_H = 135

# Endpoint uit config.py (gitignored, zoals PI_ENDPOINTS).
try:
    from apps.config import VERBRUIK_ENDPOINT as _URL
except Exception:
    _URL = ""

# Cadans, in milliseconden.
_POLL_MS = 45000   # netwerk-poll
_KLOK_MS = 1000    # aftelklok bijwerken
_TICK_MS = 40      # toetsenbord

# Kleurdrempels voor de balken. Onder 70% groen, tot 90% geel, daarboven
# rood — gelijk aan DrempelLet/DrempelAlarm van de laptop-widget en aan het
# Nest Hub-scherm, zodat alle schermen hetzelfde "hoe sta ik ervoor" signaal
# geven. Tot 2026-09-25 stond hier 60/85, terwijl de widget al op 70/90 zat.
_DREMPEL_GEEL = 70.0
_DREMPEL_ROOD = 90.0

# Verticale posities. Kop 0..20, hintstrip vanaf _H-18 (=117); alles
# daartussen is van ons.
_Y_FH_LABEL = 24
_Y_FH_PCT = 22
_Y_FH_BALK = 44
_Y_FH_RESET = 56
_Y_SD_LABEL = 72
_Y_SD_PCT = 70
_Y_SD_BALK = 90
_Y_SD_RESET = 102

_BALK_X = 6
# De balk stopt vóór de percentagekolom rechts. De percentages staan op
# size 2, en die glyphs lopen op hardware net wat verder door dan hun
# nominale 20 px; met een balk die tot de rechterrand liep sneden de
# cijfers er zichtbaar dwars doorheen. Door beide een eigen kolom te
# geven kan dat niet meer gebeuren, ongeacht de exacte fontmetriek.
_PCT_KOLOM_W = 74          # ruim genoeg voor "100%" op size 2
_BALK_W = _W - 12 - _PCT_KOLOM_W
_BALK_H = 10


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("verbruik: setFont fallback:", e)


def _kleur(pct):
    """Balk-/cijferkleur voor een percentage."""
    if pct >= _DREMPEL_ROOD:
        return _RED
    if pct >= _DREMPEL_GEEL:
        return _YELLOW
    return _GREEN


def _fmt_duur(secs):
    """Seconden -> korte leesbare aftelklok.

    Boven een dag tonen we dagen+uren (het weekvenster), daaronder
    H:MM:SS, en onder het uur M:SS — zo blijft het altijd binnen de
    breedte van een regel en zie je bij een bijna-reset de seconden echt
    lopen.
    """
    if secs is None:
        return None
    if secs < 0:
        secs = 0
    if secs >= 86400:
        d = secs // 86400
        u = (secs % 86400) // 3600
        return "{}d {}u".format(d, u)
    u = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if u:
        return "{}:{:02d}:{:02d}".format(u, m, s)
    return "{}:{:02d}".format(m, s)


def _now_hm():
    """Lokale klok als HH:MM voor het bron-stempel in de kop.

    Zelfde UTC+2-correctie als pi_dashboard: de device-klok komt van NTP
    in UTC.
    """
    t = time.localtime(time.time() + 7200)
    return "{:02d}:{:02d}".format(t[3], t[4])


# ---- chrome ---------------------------------------------------------


def _draw_chrome():
    """Vaste meubels: kop, hairline, hintstrip. Eenmalig bij start."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Claude verbruik", 6, 5)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "R ververs   Q/ESC menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _draw_bron(staat):
    """Bron-stempel rechts in de kop.

    Dit is het eerlijkheidsvenster van het scherm: het zegt of je naar
    verse cijfers kijkt of naar iets ouds. Daarom krijgt elke bron een
    eigen kleur in plaats van allemaal hetzelfde grijs.
    """
    # Alleen het rechterdeel van de kopbalk wissen, niet de titel.
    _LCD.fillRect(_W // 2, 0, _W // 2, 20, _DARK)
    _LCD.setTextSize(1)
    if staat["fout"]:
        tekst, kleur = staat["fout"], _RED
    else:
        bron = staat["bron"] or "?"
        stempel = staat["stempel"] or "--:--"
        if bron == "live":
            tekst, kleur = "live " + stempel, _GREEN
        elif bron == "nep":
            tekst, kleur = "NEP " + stempel, _YELLOW
        else:
            # "gemeten" / "geen-token": de server serveert oude cijfers.
            tekst, kleur = bron + " " + stempel, _ORANGE
    _LCD.setTextColor(kleur, _DARK)
    while _LCD.textWidth(tekst) > _W // 2 - 8 and len(tekst) > 1:
        tekst = tekst[:-1]
    _LCD.drawString(tekst, _W - _LCD.textWidth(tekst) - 6, 5)


def _draw_balk(y, pct):
    """Balk met achtergrond, vulling naar rato en een dunne rand."""
    _LCD.fillRect(_BALK_X, y, _BALK_W, _BALK_H, _DARK)
    if pct is not None and pct > 0:
        vul = int(_BALK_W * min(pct, 100.0) / 100.0)
        if vul > 0:
            _LCD.fillRect(_BALK_X, y, vul, _BALK_H, _kleur(pct))
    _LCD.drawRect(_BALK_X, y, _BALK_W, _BALK_H, _GRAY_MID)


def _draw_venster(label, y_label, y_pct, y_balk, pct, oud):
    """Eén venster: label links, percentage rechts, balk eronder.

    ``oud`` dimt het percentage wanneer de laatste poll faalde — de
    cijfers kloppen dan mogelijk niet meer, maar ze weghalen zou nog
    verwarrender zijn dan ze grijs tonen.
    """
    # Iets ruimer wissen dan de nominale 20 px van size 2: de glyphs
    # lopen wat door, en anders blijven er resten van een vorig, breder
    # getal staan (99% -> 9%). De balk wordt hieronder toch opnieuw
    # getekend, dus dat dit vlak de bovenrand ervan raakt is prima.
    _LCD.fillRect(0, y_pct, _W, 26, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString(label, 6, y_label)

    _LCD.setTextSize(2)
    if pct is None:
        tekst, kleur = "--", _GRAY_MID
    else:
        tekst, kleur = "{:.0f}%".format(pct), (_GRAY_MID if oud else _kleur(pct))
    _LCD.setTextColor(kleur, _BLACK)
    _LCD.drawString(tekst, _W - _LCD.textWidth(tekst) - 6, y_pct)
    _LCD.setTextSize(1)

    _draw_balk(y_balk, pct)


def _draw_reset(y, secs, prefix="reset over "):
    """Aftelklok-regel. Wordt elke seconde apart hertekend."""
    _LCD.fillRect(0, y, _W, 11, _BLACK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    duur = _fmt_duur(secs)
    if duur is None:
        tekst = "reset onbekend"
    elif secs <= 0:
        # De server heeft nog niet gepolld sinds de reset; zeg dat
        # eerlijk in plaats van 0:00 te blijven tonen.
        tekst = "reset nu"
    else:
        tekst = prefix + duur
    _LCD.drawString(tekst, 6, y)


def _draw_alles(staat):
    _draw_bron(staat)
    oud = bool(staat["fout"])
    _draw_venster("5-UUR", _Y_FH_LABEL, _Y_FH_PCT, _Y_FH_BALK,
                  staat["five_hour"], oud)
    _draw_reset(_Y_FH_RESET, _resterend(staat, "fh"))
    _draw_venster("WEEK", _Y_SD_LABEL, _Y_SD_PCT, _Y_SD_BALK,
                  staat["seven_day"], oud)
    _draw_reset(_Y_SD_RESET, _resterend(staat, "sd"))


# ---- netwerk --------------------------------------------------------


def _ensure_wifi():
    """Station-interface omhoog en verbinden. True bij succes.

    De launcher verbindt normaal al bij boot, dus dit is meestal een
    no-op; de wifi_event-fallback dekt de start-zonder-launcher.
    """
    try:
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if sta.isconnected():
            return True
        try:
            import wifi_event
            res = wifi_event.connect()
            return bool(res.get("ok"))
        except Exception as e:
            print("verbruik: wifi_event err:", e)
            return sta.isconnected()
    except Exception as e:
        print("verbruik: ensure_wifi err:", e)
        return False


def _poll(url):
    """GET + JSON. ``(data, None)`` of ``(None, foutlabel)``."""
    import requests
    r = None
    try:
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            return None, "HTTP {}".format(r.status_code)
        return r.json(), None
    except Exception as e:
        print("verbruik: poll", url, "err:", e)
        return None, "geen data"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _getal(v):
    """Naar float, of None als het veld ontbreekt/onbruikbaar is."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _secs(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ververs(staat):
    """Poll het endpoint en werk ``staat`` bij.

    Bij een fout houden we de vorige cijfers vast en zetten alleen
    ``fout``; het scherm dimt ze dan. Zo blijf je zien wat je laatst
    wist in plaats van een leeg scherm te krijgen als de Pi even weg is.
    """
    if not _URL:
        staat["fout"] = "geen config"
        return
    data, fout = _poll(_URL)
    if fout is not None:
        staat["fout"] = fout
        return
    try:
        staat["five_hour"] = _getal(data.get("five_hour"))
        staat["seven_day"] = _getal(data.get("seven_day"))
        staat["fh_reset_s"] = _secs(data.get("fh_reset_in_s"))
        staat["sd_reset_s"] = _secs(data.get("sd_reset_in_s"))
        staat["bron"] = data.get("bron") or "?"
        staat["stempel"] = _now_hm()
        # IJkpunt voor de lokale aftelklok: vanaf hier tellen we zelf af
        # zodat de klok tussen twee polls door blijft lopen.
        staat["ijk_ms"] = time.ticks_ms()
        staat["fout"] = None
    except (TypeError, ValueError, AttributeError) as e:
        print("verbruik: parse err:", e)
        staat["fout"] = "rare data"


def _resterend(staat, welk):
    """Resterende seconden nu, afgeteld vanaf het laatste ijkpunt."""
    basis = staat["fh_reset_s"] if welk == "fh" else staat["sd_reset_s"]
    if basis is None or staat["ijk_ms"] is None:
        return None
    verstreken = time.ticks_diff(time.ticks_ms(), staat["ijk_ms"]) // 1000
    return basis - verstreken


# ---- invoer ---------------------------------------------------------


def _intent(k):
    """MatrixKeyboard-toets -> 'ververs' / 'exit' / None."""
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:  # ESC
            return "exit"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch == "q":
        return "exit"
    if ch == "r":
        return "ververs"
    return None


# ---- hoofdlus -------------------------------------------------------


def _nieuwe_staat():
    return {
        "five_hour": None,
        "seven_day": None,
        "fh_reset_s": None,
        "sd_reset_s": None,
        "bron": None,
        "stempel": None,
        "ijk_ms": None,
        "fout": "...",
    }


def run():
    _set_font()

    staat = _nieuwe_staat()
    _draw_chrome()
    _draw_alles(staat)

    kb = MatrixKeyboard()
    # De Enter waarmee de launcher ons startte niet als invoer tellen.
    time.sleep_ms(400)

    _ensure_wifi()
    # Meteen één poll, anders staar je 45 s naar streepjes.
    _ververs(staat)
    _draw_alles(staat)

    now = time.ticks_ms()
    volgende_poll = time.ticks_add(now, _POLL_MS)
    volgende_klok = time.ticks_add(now, _KLOK_MS)

    while True:
        kb.tick()
        intent = _intent(kb.get_key())
        if intent == "exit":
            return
        if intent == "ververs":
            _ververs(staat)
            _draw_alles(staat)
            volgende_poll = time.ticks_add(time.ticks_ms(), _POLL_MS)

        now = time.ticks_ms()

        # Pollen op de deadline in plaats van slapen, zodat R en Q/ESC
        # de hele tijd reageren.
        if time.ticks_diff(now, volgende_poll) >= 0:
            _ververs(staat)
            _draw_alles(staat)
            volgende_poll = time.ticks_add(time.ticks_ms(), _POLL_MS)

        # Alleen de twee klokregels hertekenen: een volledige repaint
        # elke seconde geeft zichtbaar geflikker op dit paneel.
        if time.ticks_diff(time.ticks_ms(), volgende_klok) >= 0:
            _draw_reset(_Y_FH_RESET, _resterend(staat, "fh"))
            _draw_reset(_Y_SD_RESET, _resterend(staat, "sd"))
            volgende_klok = time.ticks_add(time.ticks_ms(), _KLOK_MS)

        time.sleep_ms(_TICK_MS)


# Draaien en onszelf daarna uit sys.modules halen, zodat de launcher ons
# opnieuw kan importeren (en dus opnieuw draaien) — zonder machine.reset().
try:
    run()
finally:
    try:
        M5.Lcd.fillScreen(_BLACK)
    except Exception as e:
        print("verbruik: scherm wissen warning:", e)
    sys.modules.pop(__name__, None)
