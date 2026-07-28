"""scanner — bedien de CanoScan N656U (op NC-Pi5) vanaf de Cardputer.

De scanner hangt fysiek aan NC-Pi5 en wordt aangeboden door
``scanservjs`` (een SANE-webfrontend) op poort 8090. Deze app geeft
scanopdrachten via de REST-API van scanservjs, zodat je niet bij de
(onlogisch geplaatste) scanner of een browser hoeft te zijn.

De Pi doet al het zware werk: scannen, de pagina's tot één OCR-PDF
assembleren, en die via een ``afterScan``-hook naar Nextcloud uploaden.
Het device raakt het bestand nooit aan — het stuurt alleen een paar
HTTP-calls. De PDF landt vanzelf in de Nextcloud-map ``Scans``.

### Twee modi

- **Losse scan** (``S``) — één A4 → één PDF. Eén POST en klaar.
- **Batch** (``B``) — meerdere A4's → één PDF. Je legt per pagina een
  nieuw vel op de glasplaat en drukt ``SPACE``; ``ENTER`` rondt af.

### Hoe de batch op de API werkt

``POST /api/v1/scan`` met een ``batch``/``index``-veld, precies zoals de
webfrontend het doet (geverifieerd in ``scan-controller.js``):

    index 1   -> wist temp, scant pagina 1
    index 2.. -> scant elke volgende pagina (papier wisselen ertussen)
    index -1  -> scant niet, maar assembleert ALLE pagina's -> 1 PDF
                 en uploadt die (afterScan) — exact één keer.

Er staat geen timer op de batch: de Pi bewaart de gescande pagina's tot
je afrondt, dus je mag rustig de tijd nemen om papier te wisselen.

### Netwerk

scanservjs draait op platte HTTP en de Cardputer zit niet op het
Tailscale-mesh, dus we bereiken NC-Pi5 via het **LAN-IP** (zelfde
thuisnetwerk), niet via de ``*.ts.net``-naam of de HTTPS-serve-URL. Het
basis-URL komt uit ``apps/config.py`` (``SCANNER_BASE``, gitignored),
net als de Pi-dashboard-endpoints. Dat dit alleen thuis werkt is geen
beperking: je moet sowieso fysiek bij de scanner staan om papier te
leggen.

### Belangrijk: scannen blokkeert

De N656U is een trage USB-1.1-scanner; één A4 in grijswaarden op 150 dpi
duurt ~55 s. De ``POST /scan`` blokkeert tot de pagina binnen is, dus
tijdens een scan reageert het toetsenbord niet — daarom een expliciet
"scannen…"-scherm. De scandefaults (Gray / 150 dpi / A4 met 1 mm marge /
OCR-PDF) worden server-side bepaald; we halen ze bij start op uit
``/context`` zodat het device klopt met wat er op de Pi is ingesteld.

### Exit

``Q`` of ``ESC`` in het menu keert terug naar de launcher. We laten
``run()`` terugkeren en droppen de module uit ``sys.modules`` — geen
``machine.reset()`` — zodat de launcher zijn menu herschildert.
"""

import sys
import time

import M5
import network
from hardware import MatrixKeyboard


# Palette — identiek aan de rest van de bundle (hello/snake/pi_dashboard)
# zodat de apps visueel samenhangen.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xCC4444
_GREEN = 0x4CAF50

_LCD = M5.Lcd

_W = 240
_H = 135

# Content-zone zit tussen de header-hairline en de hint-strip.
_TOP = 24
_BOTTOM = _H - 18

# Basis-URL van scanservjs op NC-Pi5, uit config.py (gitignored). Bijv.
# "http://<pi-lan-ip>:8090". Trailing slash wordt afgekapt.
try:
    from apps.config import SCANNER_BASE as _BASE
except Exception:
    _BASE = ""
_BASE = (_BASE or "").rstrip("/")

# Timeouts in seconden. De context-call is licht; een scan blokkeert ~55 s
# dus die krijgt ruim de tijd.
_CTX_TIMEOUT = 8
_SCAN_TIMEOUT = 180

_TICK_MS = 40  # toetsenbord-pollinterval

# Fallback-pipeline als /context er geen aanlevert (zou niet moeten
# gebeuren — server-default is de OCR-PDF).
_FALLBACK_PIPELINE = "@:pipeline.ocr | PDF (JPG | @:pipeline.high-quality)"


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        # Niet crashen op een build zonder FONTS.
        print("scanner: setFont fallback:", e)


def _beep(ok=True):
    """Korte chirp: oplopend bij succes, aflopend bij fout. Defensief —
    M5.Speaker is niet op elke build gegarandeerd en de API varieert, dus
    elke fout valt stil door (het scherm is het primaire kanaal)."""
    try:
        spk = M5.Speaker
    except Exception:
        return
    try:
        if ok:
            spk.tone(880, 90)
            time.sleep_ms(60)
            spk.tone(1175, 110)
        else:
            spk.tone(440, 120)
            time.sleep_ms(60)
            spk.tone(330, 140)
    except Exception as e:
        print("scanner: beep skipped:", e)


# ---- chrome ---------------------------------------------------------


def _screen(title, lines, hint):
    """Volledige repaint: header + verticaal gecentreerde body + hint.

    ``lines`` is een lijst van ``(tekst, kleur)``-tuples; elke regel
    wordt horizontaal gecentreerd. We tekenen alles op zwart, met de
    header-band en hint-strip in _DARK plus de oranje hairline — exact
    de drie-zone chrome van de andere apps.
    """
    _LCD.fillScreen(_BLACK)

    # Header-band met de oranje hairline eronder.
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)

    # Body — verticaal gecentreerd in de content-zone.
    _LCD.setTextSize(1)
    line_h = 16
    block_h = len(lines) * line_h
    y = _TOP + max(0, ((_BOTTOM - _TOP) - block_h) // 2)
    for text, color in lines:
        _LCD.setTextColor(color, _BLACK)
        _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, y)
        y += line_h

    # Hint-strip onderaan.
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


# ---- network --------------------------------------------------------


def _ensure_wifi():
    """Breng de station-interface op en verbind. True bij succes.

    De launcher verbindt WiFi normaliter al bij boot, dus dit is meestal
    een snelle no-op; de wifi_event-fallback dekt het geval dat we zonder
    die verbinding zijn gestart."""
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
            print("scanner: wifi_event err:", e)
            return sta.isconnected()
    except Exception as e:
        print("scanner: ensure_wifi err:", e)
        return False


def _get_json(path):
    """GET ``_BASE + path`` en parse JSON. (data, None) of (None, error)."""
    import requests
    r = None
    try:
        r = requests.get(_BASE + path, timeout=_CTX_TIMEOUT)
        if r.status_code != 200:
            return None, "HTTP {}".format(r.status_code)
        return r.json(), None
    except Exception as e:
        print("scanner: GET", path, "err:", e)
        return None, "geen verbinding"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _post_scan(body):
    """POST een ScanRequest naar /api/v1/scan. (True, None) of (None, error).

    We serialiseren zelf naar JSON en zetten de Content-Type expliciet —
    dat is de meest compatibele vorm voor de firmware-``requests``. De
    timeout is ruim omdat de call blokkeert tot de pagina gescand is.

    Belangrijk: we lezen het antwoord BEWUST niet uit. Bij een
    tussenliggende batch-pagina stuurt scanservjs een grote base64-
    preview mee in de JSON; die parsen (``r.json()``) vrat zoveel RAM dat
    de volgende POST op het device omviel (de losse scan werkt wel, want
    daar is het antwoord klein). We hebben de body niet nodig — alleen de
    statuscode. Een ``gc.collect()`` voor en na houdt het geheugen schoon
    tussen pagina's (zie ook de cardputer geheugen-notitie over RAM)."""
    import requests
    import json
    import gc
    gc.collect()
    payload = json.dumps(body)
    headers = {"Content-Type": "application/json"}
    r = None
    try:
        r = requests.post(_BASE + "/api/v1/scan", data=payload,
                          headers=headers, timeout=_SCAN_TIMEOUT)
        code = r.status_code
        if code != 200:
            return None, "HTTP {}".format(code)
        return True, None
    except Exception as e:
        print("scanner: scan err:", e)
        return None, "scan mislukt"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()


def _load_device():
    """Haal /context en bouw de scan-params uit de server-side defaults.

    Returns (params, pipeline, None) of (None, None, error). Door de
    defaults uit /context te lezen erft het device automatisch de op de
    Pi ingestelde waarden (Gray / 150 dpi / marges / OCR-PDF), en pakt
    het de actuele deviceId (de libusb bus:device kan na herstart van de
    Pi wijzigen)."""
    ctx, err = _get_json("/api/v1/context")
    if err:
        return None, None, err
    try:
        dev = ctx["devices"][0]
    except (KeyError, IndexError, TypeError):
        return None, None, "geen scanner"

    feats = dev.get("features", {})

    def fdef(key, fallback):
        try:
            return feats[key]["default"]
        except Exception:
            return fallback

    params = {
        "deviceId": dev.get("id"),
        "top": fdef("-t", 1),
        "left": fdef("-l", 1),
        "width": fdef("-x", 208),
        "height": fdef("-y", 295),
        "pageWidth": 215,
        "pageHeight": 297,
        "resolution": fdef("--resolution", 150),
        "mode": fdef("--mode", "Gray"),
        "brightness": 0,
        "contrast": 0,
    }
    try:
        pipeline = dev["settings"]["pipeline"]["default"]
    except Exception:
        pipeline = _FALLBACK_PIPELINE
    return params, pipeline, None


def _scan(params, pipeline, batch, index):
    """Stuur één scan-call (één pagina, of index -1 om af te ronden)."""
    return _post_scan({
        "params": params,
        "filters": [],
        "pipeline": pipeline,
        "batch": batch,
        "index": index,
    })


# ---- input ----------------------------------------------------------


def _to_char(k):
    """MatrixKeyboard-return → los teken (str) of None."""
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and k:
        return k
    return None


def _is_enter(k):
    if isinstance(k, int) and k in (0x0A, 0x0D):
        return True
    return isinstance(k, str) and k in ("\r", "\n")


def _is_esc(k):
    return isinstance(k, int) and k == 0x1B


def _wait_key(kb):
    """Wacht op een willekeurige toets. Eerst even pauzeren zodat de
    toets die ons hier bracht al losgelaten is en niet meteen meetelt."""
    time.sleep_ms(250)
    while True:
        kb.tick()
        if kb.get_key() is not None:
            return
        time.sleep_ms(_TICK_MS)


# ---- flows ----------------------------------------------------------


def _fail(kb, err):
    """Toon een foutscherm en wacht op een toets."""
    _beep(ok=False)
    _screen("fout", [("scan mislukt", _RED), (err, _GRAY_MID)],
            "toets = terug")
    _wait_key(kb)


def _do_single(kb, params, pipeline):
    """Losse scan: één pagina → één PDF → Nextcloud."""
    _screen("losse scan",
            [("scannen...", _ORANGE),
             ("gray 150dpi A4", _GRAY_MID),
             ("~55 sec", _GRAY_MID)],
            "even geduld")
    _data, err = _scan(params, pipeline, "none", 1)
    if err:
        _fail(kb, err)
        return
    _beep(ok=True)
    _screen("klaar",
            [("naar Nextcloud", _GREEN),
             ("map: Scans", _CREAM),
             ("1 pagina (OCR)", _GRAY_MID)],
            "toets = terug")
    _wait_key(kb)


def _do_batch(kb, params, pipeline):
    """Batch: leg per pagina een nieuw vel, SPACE scant, ENTER rondt af.

    Elke SPACE doet een blokkerende scan van wat er op dat moment ligt;
    de teller groeit per pagina. ENTER (bij minstens één pagina) stuurt
    index -1 zodat de Pi alles tot één PDF assembleert en uploadt. ESC/Q
    breekt af: de tot dan toe gescande pagina's worden niet bewaard (de
    Pi wist ze bij de eerstvolgende scan vanzelf)."""
    pages = 0
    while True:
        body = [("leg pagina {}".format(pages + 1), _CREAM),
                ("op de glasplaat", _GRAY_MID),
                ("SPACE = scan", _ORANGE)]
        if pages > 0:
            body.append(("ENTER = klaar ({})".format(pages), _GREEN))
            title = "batch - {} pag.".format(pages)
            hint = "SPACE scan  ENTER klaar  ESC"
        else:
            title = "batch"
            hint = "SPACE scan   ESC stop"
        _screen(title, body, hint)

        # Wacht op een betekenisvolle toets.
        action = None
        while action is None:
            kb.tick()
            k = kb.get_key()
            if _is_esc(k):
                action = "esc"
            elif _is_enter(k) and pages > 0:
                action = "finish"
            else:
                ch = _to_char(k)
                if ch == " ":
                    action = "scan"
                elif ch and ch.lower() == "q":
                    action = "esc"
            time.sleep_ms(_TICK_MS)

        if action == "esc":
            if pages > 0:
                _screen("batch gestopt",
                        [("{} pagina's".format(pages), _CREAM),
                         ("niet bewaard", _GRAY_MID)],
                        "toets = terug")
                _wait_key(kb)
            return

        if action == "scan":
            idx = pages + 1
            _screen("scannen pagina {}".format(idx),
                    [("scannen...", _ORANGE), ("~55 sec", _GRAY_MID)],
                    "even geduld")
            _data, err = _scan(params, pipeline, "manual", idx)
            if err:
                _fail(kb, err)
                return
            pages = idx
            _beep(ok=True)
            # terug naar het leg-pagina-scherm voor de volgende

        elif action == "finish":
            _screen("afronden...",
                    [("{} pagina's -> 1 pdf".format(pages), _CREAM),
                     ("OCR + upload...", _GRAY_MID)],
                    "even geduld")
            _data, err = _scan(params, pipeline, "manual", -1)
            if err:
                _fail(kb, err)
                return
            _beep(ok=True)
            _screen("klaar",
                    [("naar Nextcloud", _GREEN),
                     ("map: Scans", _CREAM),
                     ("1 pdf, {} pagina's".format(pages), _GRAY_MID)],
                    "toets = terug")
            _wait_key(kb)
            return


def _menu(kb):
    """Toon het hoofdmenu; return 's', 'b' of 'exit'."""
    _screen("scanner - nc-pi5",
            [("[S]  losse scan", _CREAM),
             ("[B]  batch -> 1 pdf", _CREAM)],
            "S / B kiezen   Q terug")
    while True:
        kb.tick()
        k = kb.get_key()
        if _is_esc(k):
            return "exit"
        ch = _to_char(k)
        if ch:
            ch = ch.lower()
            if ch == "q":
                return "exit"
            if ch == "s":
                return "s"
            if ch == "b":
                return "b"
        time.sleep_ms(_TICK_MS)


# ---- main -----------------------------------------------------------


def run():
    _set_font()
    kb = MatrixKeyboard()
    # Debounce de launch-toets (Enter uit de launcher), zelfde 400 ms als
    # de andere apps.
    time.sleep_ms(400)

    if not _BASE:
        _screen("scanner",
                [("config ontbreekt", _RED),
                 ("zet SCANNER_BASE", _CREAM),
                 ("in apps/config.py", _GRAY_MID)],
                "toets = terug")
        _wait_key(kb)
        return

    _screen("scanner", [("verbinden...", _CREAM)], "even geduld")
    if not _ensure_wifi():
        _screen("scanner", [("geen wifi", _RED)], "toets = terug")
        _wait_key(kb)
        return

    params, pipeline, err = _load_device()
    if err:
        _screen("scanner",
                [("scanner niet gevonden", _RED), (err, _GRAY_MID)],
                "toets = terug")
        _wait_key(kb)
        return

    while True:
        choice = _menu(kb)
        if choice == "exit":
            return
        if choice == "s":
            _do_single(kb, params, pipeline)
        elif choice == "b":
            _do_batch(kb, params, pipeline)


# Run, en drop onszelf daarna uit sys.modules zodat de launcher ons
# opnieuw kan importeren (en draaien) bij de volgende keuze — zonder
# machine.reset(). De launcher houdt zelf een referentie vast, dus
# poppen is veilig; als run() terugkeert herschildert _launch het menu.
try:
    run()
finally:
    try:
        _LCD.fillScreen(_BLACK)
    except Exception:
        pass
    sys.modules.pop(__name__, None)
