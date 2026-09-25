"""verkenner_beeld — foto's tonen op het 240×135-scherm.

### Wat de firmware kan (gemeten 2026-09-25, UIFlow 2.4.5)

- **JPEG**: alleen baseline (SOF0). TJpgDec decodeert 240×135 in ~22 ms
  en schaalt grote foto's bij het decoderen al 1/2–1/8. Een progressieve
  JPEG tekent stil niets, dus die herkennen we vooraf.
- **BMP**: ja.
- **PNG**: nee. pngle wil ~43 KB aaneengesloten IDF-geheugen en met WiFi +
  BLE actief is het grootste vrije blok ~31 KB. We proberen het alleen als
  er genoeg vrij is.
- ``drawJpg``/``drawBmp``/``drawPng`` geven altijd None terug. Of er iets
  getekend is, leiden we af uit de tijd: onder ~3 ms is er niets gebeurd.

### Miniatuur eerst

Camerafoto's zijn megabytes groot. Die passen niet in het RAM (~62 KB
vrij), en een volledige decode kost seconden. De meeste camera's en
telefoons stoppen een EXIF-miniatuur van 160×120 in de APP1-marker. Die
laden we (een paar KB) en tekenen we meteen; op dit scherm is dat bijna
de volle resolutie. **Enter** decodeert de echte foto.

Zonder miniatuur (veel Lumia-foto's bijvoorbeeld) moet de hele foto door
de decoder. De firmware leest het bestand dan zelf via een pad: op flash
of een FAT-kaart rechtstreeks, op een exFAT-kaart via verkenner_fatvenster.
Dat kost ~3-3,5 s per MB (gemeten: 16 MP, 4 MB = ~13 s) en kan niet worden
onderbroken. Kleine foto's (tot 300 KB) tonen we meteen. Bij grotere
eerst een kaartje met afmetingen, camera en datum: **Enter** decodeert,
bladeren blijft direct. Tijdens het decoderen staat er een laadmelding.

### EXIF-oriëntatie

Staande telefoonfoto's liggen op schijf plat met Orientation 6 of 8. We
draaien dan het assenstelsel van het LCD (setRotation) en tekenen de foto
passend in 135×240. Welke kant op, staat in ``_ROTATIE``.
"""

import gc
import time

import verkenner_exif as ex
import verkenner_ui as ui
from verkenner_bron import V_MAP

_LCD = ui.LCD

_MAX_BUF = len(ui.WERK)   # grootste miniatuur/bestand dat we in RAM laden
_VOL_DIRECT = 300 * 1024  # kleinere JPEG's meteen in volle resolutie
_PNG_NODIG = 48 * 1024    # aaneengesloten IDF-heap die pngle nodig heeft
_MIN_MS = 2               # sneller dan dit = de firmware tekende niets

JPEG_EXT = ui.JPEG_EXT
TOONBAAR = ui.TOONBAAR
extensie = ui.extensie

# EXIF-oriëntatie -> stappen van 90° die we bij de LCD-rotatie optellen.
# 6 = foto moet 90° met de klok mee, 8 = tegen de klok in, 3 = 180°.
# De spiegelvarianten (2/4/5/7) zijn zeldzaam; die benaderen we.
_ROTATIE = {1: 0, 2: 0, 3: 2, 4: 2, 5: 3, 6: 1, 7: 1, 8: 3}


# ---- tekenen ----------------------------------------------------------------


def passend(w, h, bw, bh, max_zoom=2.0):
    """Schaal en positie om w×h gecentreerd in bw×bh te passen.

    -> (schaal, x, y, breedte, hoogte). Vergroten doen we hooguit 2×,
    anders wordt een piepklein plaatje één grote blokkenbrij.
    """
    s = min(bw / w, bh / h, max_zoom)
    dw = max(1, int(w * s + 0.5))
    dh = max(1, int(h * s + 0.5))
    return s, (bw - dw) // 2, (bh - dh) // 2, dw, dh


def _grootste_vrije_blok():
    try:
        import esp32
        return max(r[2] for r in esp32.idf_heap_info(esp32.HEAP_DATA))
    except Exception:
        return 0


def _teken(soort, bron_img, w, h, orient=1, melding=None):
    """Teken beeld (pad of buffer) passend en gecentreerd. -> ms.

    ``melding`` blijft zichtbaar zolang de decoder bezig is (die tekent er
    overheen). Daarna wissen we de randen buiten het beeld, zodat er geen
    resten van de melding blijven staan.
    """
    basis = _LCD.getRotation()
    stap = _ROTATIE.get(orient, 0)
    _LCD.fillScreen(ui.ZWART)
    if melding:
        ui.font_prop()
        _LCD.setTextColor(ui.GRIJS, ui.ZWART)
        _LCD.drawString(melding, (ui.W - _LCD.textWidth(melding)) // 2, ui.H // 2 - 5)
    bw, bh = (ui.H, ui.W) if stap in (1, 3) else (ui.W, ui.H)
    s, x, y, dw, dh = passend(w, h, bw, bh)
    try:
        if stap:
            _LCD.setRotation((basis + stap) % 4)
        t0 = time.ticks_ms()
        if soort == "jpg":
            _LCD.drawJpg(bron_img, x, y, dw, dh, 0, 0, s, s)
        elif soort == "bmp":
            _LCD.drawBmp(bron_img, x, y, dw, dh, 0, 0, s, s)
        else:
            _LCD.drawPng(bron_img, x, y, dw, dh, 0, 0, s, s)
        ms = time.ticks_diff(time.ticks_ms(), t0)
    finally:
        if stap:
            _LCD.setRotation(basis)
    if melding and ms >= _MIN_MS:
        # Beeld staat gecentreerd; bij 90/270 graden zijn breedte en hoogte
        # op het fysieke scherm omgewisseld.
        pw, ph = (dh, dw) if stap in (1, 3) else (dw, dh)
        px = (ui.W - pw) // 2
        py = (ui.H - ph) // 2
        _LCD.fillRect(0, 0, ui.W, py, ui.ZWART)
        _LCD.fillRect(0, py + ph, ui.W, ui.H - py - ph, ui.ZWART)
        _LCD.fillRect(0, py, px, ph, ui.ZWART)
        _LCD.fillRect(px + pw, py, ui.W - px - pw, ph, ui.ZWART)
    return ms


def _laad_buffer(f, pos, n, soi=False):
    """Lees n bytes in de vaste werkbuffer. -> memoryview, of None als het
    niet past.

    Geen nieuwe allocatie: na wat bladeren is er vaak geen groot blok meer
    aaneen vrij (zie ui.WERK). ``soi``: zet er een SOI-marker (FF D8) voor,
    voor miniaturen waar de camera die heeft weggelaten.
    """
    extra = 2 if soi else 0
    if n + extra > _MAX_BUF:
        return None
    buf = memoryview(ui.WERK)[0:n + extra]
    if soi:
        buf[0] = 0xFF
        buf[1] = 0xD8
    f.seek(pos)
    if f.readinto(buf[extra:]) != n:
        return None
    return buf


def _balk(naam, rechts, mini):
    """Info-overlay onderaan: naam links, positie rechts, 'MINI'-label."""
    y = ui.H - 12
    _LCD.fillRect(0, y, ui.W, 12, ui.DONKER)
    ui.font_mono()
    _LCD.setTextColor(ui.GRIJS, ui.DONKER)
    rw = len(rechts) * ui.MONO_W
    _LCD.drawString(rechts, ui.W - rw - 3, y + 2)
    ui.font_prop()
    _LCD.setTextColor(ui.CREME, ui.DONKER)
    _LCD.drawString(ui.passend(ui.ascii(naam), ui.W - rw - 10), 3, y + 1)
    if mini:
        ui.font_mono()
        _LCD.fillRect(ui.W - 30, 0, 30, 11, ui.DONKER)
        _LCD.setTextColor(ui.ORANJE, ui.DONKER)
        _LCD.drawString("MINI", ui.W - 27, 2)


def _bericht(naam, regels):
    _LCD.fillScreen(ui.ZWART)
    ui.font_prop()
    _LCD.setTextColor(ui.CREME, ui.ZWART)
    _LCD.drawString(ui.passend(ui.ascii(naam), ui.W - 10), 5, 8)
    _LCD.fillRect(0, 22, ui.W, 1, ui.ORANJE)
    y = 34
    for i, r in enumerate(regels):
        _LCD.setTextColor(ui.GEEL if i == 0 else ui.GRIJS, ui.ZWART)
        _LCD.drawString(ui.passend(ui.ascii(r), ui.W - 10), 5, y)
        y += 13


def toon_een(src, map_, e, volledig=False):
    """Toon item ``e``. -> (gelukt, miniatuur, afmetingen, reden).

    ``miniatuur``: True als de EXIF-miniatuur getekend is, False als het
    de hele foto is of niets, en ``"aanbod"`` als er alleen een kaartje staat
    met de keuze om de grote foto volledig te decoderen.
    """
    ext = extensie(e[1])
    f = src.open(map_, e)
    try:
        if ext in JPEG_EXT:
            return _toon_jpeg(src, map_, e, f, volledig)
        if ext == "bmp":
            info = ex.bmp_info(f)
            if not info:
                return False, False, None, ["Geen geldige BMP"]
            return _toon_via_pad_of_buffer("bmp", src, map_, e, f, info, 16 * 16)
        if ext == "png":
            info = ex.png_info(f)
            if not info:
                return False, False, None, ["Geen geldige PNG"]
            vrij = _grootste_vrije_blok()
            if vrij < _PNG_NODIG:
                return False, False, (info["w"], info["h"]), [
                    "PNG {}x{} kan niet".format(info["w"], info["h"]),
                    "De PNG-decoder wil ~43 KB aaneen-",
                    "gesloten geheugen; vrij: {} KB.".format(vrij // 1024),
                ]
            return _toon_via_pad_of_buffer("png", src, map_, e, f, info, 0)
        if ext == "gif":
            info = ex.gif_info(f)
            afm = (info["w"], info["h"]) if info else None
            return False, False, afm, ["GIF wordt niet ondersteund",
                                       "{}x{}".format(*afm) if afm else ""]
        return False, False, None, ["Dit formaat kan het", "toestel niet tonen ({})".format(ext.upper())]
    finally:
        f.close()


def _toon_jpeg(src, map_, e, f, volledig):
    info = ex.jpeg_info(f, e[3])
    if info is None:
        return False, False, None, ["Geen geldige JPEG", "(handtekening klopt niet)"]
    afm = (info["w"], info["h"]) if info["w"] else None
    pad = src.teken_pad(map_, e)
    mini = info["mini"]
    if not volledig and mini and (not pad or e[3] > _VOL_DIRECT):
        buf = _laad_buffer(f, mini[0], mini[1], mini[2])
        if buf is not None:
            wh = ex.jpeg_afm(buf) or (160, 120)
            ms = _teken("jpg", buf, wh[0], wh[1], info["orient"])
            if ms >= _MIN_MS:
                return True, True, afm, None
        elif pad and src.kan_deel:
            # Te groot voor het RAM: de firmware leest alleen het stukje met
            # de miniatuur via het FAT-venster (met FF D8 ervoor als die mist).
            wh = ex.mini_afm(f, mini)
            f.close()
            src.voor_tekenen(map_, e, mini[0], mini[1], b"\xFF\xD8" if mini[2] else b"")
            try:
                ms = _teken("jpg", pad, wh[0], wh[1], info["orient"])
            finally:
                src.na_tekenen()
            if ms >= _MIN_MS:
                return True, True, afm, None
    if info["sof"] != 0xC0:
        soort = "progressieve" if info["prog"] else "deze soort"
        return False, False, afm, ["Kan {} JPEG niet".format(soort),
                                   "decoderen (alleen baseline)."]
    if not afm:
        return False, False, None, ["JPEG zonder afmetingen?"]
    if pad and not volledig and e[3] > _VOL_DIRECT:
        # Groot en zonder bruikbare miniatuur: niet meteen ~3,5 s per MB
        # blokkeren (dat kan niet worden onderbroken). Laat zien wat we
        # weten en laat de gebruiker met Enter kiezen.
        return False, "aanbod", afm, _aanbod(info, e)
    if pad:
        f.close()               # de firmware leest zelf; ons handvat mag dicht
        melding = "foto laden... " + ui.grootte_kort(e[3]) + "B"
        src.voor_tekenen(map_, e)
        try:
            ms = _teken("jpg", pad, afm[0], afm[1], info["orient"], melding)
        finally:
            src.na_tekenen()
        if ms >= _MIN_MS:
            return True, False, afm, None
        return False, False, afm, ["Decoderen mislukt", "(firmware gaf niets terug)"]
    buf = _laad_buffer(f, 0, e[3])
    if buf is not None:
        ms = _teken("jpg", buf, afm[0], afm[1], info["orient"])
        if ms >= _MIN_MS:
            return True, False, afm, None
    return False, False, afm, _geen_pad_reden(src, map_, e)


def _aanbod(info, e):
    """Kaartje voor een grote foto zonder miniatuur: gegevens + keuze."""
    sec = int(e[3] * 3.5 / 1048576 + 1)       # gemeten: ~3-3,5 s per MB
    regels = ["Geen miniatuur in deze foto.",
              "Enter: volledig decoderen (~{} s)".format(sec), "",
              "{}x{}  {}B".format(info["w"], info["h"], ui.grootte_kort(e[3]))]
    camera = " ".join(x for x in (info["merk"], info["model"]) if x)
    if camera:
        regels.append(camera)
    if info["datum"]:
        regels.append(info["datum"])
    return regels


def _geen_pad_reden(src, map_, e):
    """Uitleg waarom de firmware dit bestand niet via een pad kan lezen."""
    pad = src.pad(map_, e)
    if pad and len(pad) >= 128:
        return ["Pad te lang voor de firmware", "(max 127 tekens)."]
    if pad:
        return ["Firmware leest dit pad niet", "('flash'/'system' in het pad", "crasht de tekenfunctie)."]
    return ["Te groot voor het geheugen", "({} KB).".format(e[3] // 1024)]


def _toon_via_pad_of_buffer(soort, src, map_, e, f, info, klein):
    afm = (info["w"], info["h"])
    pad = src.teken_pad(map_, e)
    if pad:
        f.close()
        src.voor_tekenen(map_, e)
        try:
            ms = _teken(soort, pad, afm[0], afm[1], 1, "laden... " + ui.grootte_kort(e[3]) + "B")
        finally:
            src.na_tekenen()
    else:
        buf = _laad_buffer(f, 0, e[3])
        if buf is None:
            return False, False, afm, _geen_pad_reden(src, map_, e)
        ms = _teken(soort, buf, afm[0], afm[1])
    if ms >= _MIN_MS or afm[0] * afm[1] <= klein:
        return True, False, afm, None
    return False, False, afm, ["Tekenen mislukt", "(firmware gaf niets terug)"]


# ---- de viewer ---------------------------------------------------------------


def _is_beeld(e):
    return not e[2] & V_MAP and extensie(e[1]) in TOONBAAR


def kan_volledig(src, map_, e):
    """Kan de echte foto (niet de miniatuur) getekend worden?"""
    return bool(src.teken_pad(map_, e)) or e[3] <= _MAX_BUF


def _zoek(m, i, stap):
    """Volgende/vorige toonbare foto vanaf index i, of None."""
    n = m.aantal()
    j = i + stap
    gezocht = 0
    while 0 <= j < n and gezocht < 500:
        e = m.item(j)
        if e and _is_beeld(e):
            return j
        j += stap
        gezocht += 1
    return None


def toon(kb, src, m, i):
    """Bekijk foto ``i`` van map ``m``. -> index van de laatst getoonde foto.

    links/rechts (of op/neer) = vorige/volgende foto, Enter = volle
    resolutie, i = info-balk aan/uit, q/ESC/del = terug naar de lijst.
    """
    balk = True
    volledig = False
    while True:
        e = m.item(i)
        ok, mini, afm, reden = toon_een(src, m.desc, e, volledig)
        if not ok:
            _bericht(e[1], reden or ["Kan niet tonen"])
        elif balk:
            pos = "{}/{}".format(i + 1, m.aantal())
            if afm:
                pos = "{}x{} {}".format(afm[0], afm[1], pos)
            _balk(e[1], pos, mini is True)
        gc.collect()
        while True:
            t = ui.toets(kb)
            if t is None:
                time.sleep_ms(30)
                continue
            if t in ("esc", "q", "del"):
                return i
            if t in ("rechts", "neer", " "):
                j = _zoek(m, i, 1)
            elif t in ("links", "op"):
                j = _zoek(m, i, -1)
            elif t == "enter":
                if not mini:            # True (miniatuur) of "aanbod": vol kan
                    continue
                if kan_volledig(src, m.desc, e):
                    volledig = True
                    break
                # De firmware kan dit pad niet lezen (zie _geen_pad_reden)
                # en in het RAM past het niet: miniatuur laten staan.
                _balk("volle resolutie kan hier niet", "", False)
                continue
            elif t == "i":
                balk = not balk
                break
            else:
                continue
            if j is not None:
                i = j
                volledig = False
                break
