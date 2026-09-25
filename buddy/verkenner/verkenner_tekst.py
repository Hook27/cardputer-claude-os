"""verkenner_tekst — tekst- en hexviewer.

### Tekst

ASCII7 (6×8 monospace), 39 kolommen × 13 regels. We breken af op
schermbreedte en onthouden alleen de byte-positie van de bovenste regel.
Terugbladeren zoekt het begin van de logische regel (de vorige newline,
hooguit 2 KB terug) en breekt van daaruit opnieuw af. Zo werkt het ook
voor logbestanden van megabytes zonder index in het RAM. UTF-8 wordt
gedecodeerd; accenten worden vertaald en de rest wordt '?'.

### Hex

Acht bytes per regel: offset (laagste 6 hexcijfers), bytes, ASCII.
Nullen zijn gedimd. De kop toont de volledige offset. Zo lees je
headers, handtekeningen en slack zonder iets te hoeven interpreteren.

Toetsen: ``;``/``.`` regel, ``[``/``]`` pagina, ``0``-``9`` naar 0-90%,
``b``/``e`` begin/eind, ``h``/``t`` wissel hex/tekst, q/ESC/del terug.
De infopagina staat apart in verkenner_info, zodat de viewer en de
handtekeningtabel niet tegelijk in het geheugen hoeven.
"""

import gc
import time

import verkenner_ui as ui

_LCD = ui.LCD

_KOLOM = 39
_REGELS = 13
_REGEL_H = 9
_Y0 = 14
_X0 = 2
_BLOK = 2048

def _lees(f, pos, n):
    f.seek(pos)
    return f.read(n)


def _kop(titel, rechts):
    _LCD.fillRect(0, 0, ui.W, 12, ui.DONKER)
    ui.font_mono()
    rw = len(rechts) * ui.MONO_W
    _LCD.setTextColor(ui.ORANJE, ui.DONKER)
    _LCD.drawString(rechts, ui.W - rw - 2, 2)
    ui.font_prop()
    _LCD.setTextColor(ui.CREME, ui.DONKER)
    _LCD.drawString(ui.passend(ui.ascii(titel), ui.W - rw - 8), 2, 1)


def _procent(pos, grootte):
    return "{}%".format(pos * 100 // grootte if grootte else 100)


# ---- tekst ------------------------------------------------------------------


def _teken_utf8(data, i, n):
    """Decodeer één UTF-8-teken vanaf data[i]. -> (tekst, aantal bytes)."""
    b = data[i]
    if b >= 0xF0:
        k, cp = 4, b & 0x07
    elif b >= 0xE0:
        k, cp = 3, b & 0x0F
    elif b >= 0xC0:
        k, cp = 2, b & 0x1F
    else:
        return ".", 1
    if i + k > n:
        return "?", 1
    for j in range(1, k):
        c = data[i + j]
        if c & 0xC0 != 0x80:
            return "?", 1
        cp = (cp << 6) | (c & 0x3F)
    return ui.ascii(chr(cp)), k


def breek(data, max_regels, kolommen=_KOLOM):
    """Breek bytes af in schermregels. -> lijst van (offset, tekst).

    Een regel begint na een newline of op het afbreekpunt na ``kolommen``
    tekens. Tabs springen naar een veelvoud van 4, CR wordt genegeerd,
    stuurtekens worden '.'.
    """
    uit = []
    n = len(data)
    i = 0
    start = 0
    regel = []
    kol = 0
    while i < n and len(uit) < max_regels:
        b = data[i]
        if b == 0x0A:
            uit.append((start, "".join(regel)))
            regel = []
            kol = 0
            i += 1
            start = i
            continue
        if b == 0x0D:
            i += 1
            continue
        if kol >= kolommen:
            uit.append((start, "".join(regel)))
            regel = []
            kol = 0
            start = i
            if len(uit) >= max_regels:
                break
        if b == 0x09:
            spaties = min(4 - kol % 4, kolommen - kol)
            regel.append(" " * spaties)
            kol += spaties
            i += 1
            continue
        if 0x20 <= b < 0x7F:
            regel.append(chr(b))
            i += 1
        elif b >= 0x80:
            ch, k = _teken_utf8(data, i, n)
            regel.append(ch[:1])
            i += k
        else:
            regel.append(".")
            i += 1
        kol += 1
    if regel and len(uit) < max_regels:
        uit.append((start, "".join(regel)))
    return uit


def vorige_regel(f, p):
    """Byte-offset van de schermregel vóór de regel die op ``p`` begint."""
    if p <= 0:
        return 0
    a = max(0, p - _BLOK)
    data = _lees(f, a, p - a)
    k = data.rfind(b"\n", 0, len(data) - 1)
    begin = k + 1 if k >= 0 else 0
    regels = breek(data[begin:], 10000)
    if not regels:
        return a + begin
    return a + begin + regels[-1][0]


def _synchroon(f, pos, grootte):
    """Eerste regelbegin op of na ``pos`` (na de eerstvolgende newline)."""
    if pos <= 0:
        return 0
    data = _lees(f, pos - 1, min(_BLOK, grootte - pos + 1))
    k = data.find(b"\n")
    return pos + k if k >= 0 else pos


def toon_tekst(kb, f, grootte, titel):
    top = 0
    while True:
        data = _lees(f, top, min(_BLOK, max(0, grootte - top)))
        regels = breek(data, _REGELS + 1)
        _LCD.fillScreen(ui.ZWART)
        _kop(titel, _procent(top, grootte))
        ui.font_mono()
        _LCD.setTextColor(ui.CREME, ui.ZWART)
        for j, (_o, tekst) in enumerate(regels[:_REGELS]):
            _LCD.drawString(tekst, _X0, _Y0 + j * _REGEL_H)
        if not regels:
            _LCD.setTextColor(ui.GRIJS, ui.ZWART)
            _LCD.drawString("(leeg)", _X0, _Y0)
        volgende = top + regels[1][0] if len(regels) > 1 else top
        pagina = top + regels[_REGELS][0] if len(regels) > _REGELS else top
        while True:
            t = ui.toets(kb)
            if t is None:
                time.sleep_ms(30)
                continue
            if t in ("q", "esc", "del", "links"):
                return None
            if t == "h":
                return "hex"
            if t == "neer":
                top = volgende
            elif t in ("]", " ", "rechts"):
                top = pagina
            elif t == "op":
                top = vorige_regel(f, top)
            elif t == "[":
                for _ in range(_REGELS):
                    top = vorige_regel(f, top)
            elif t == "b":
                top = 0
            elif t == "e":
                top = grootte
                for _ in range(_REGELS):
                    top = vorige_regel(f, top)
            elif "0" <= t <= "9" and len(t) == 1:
                top = _synchroon(f, grootte * int(t) // 10, grootte)
            else:
                continue
            break


# ---- hex --------------------------------------------------------------------

_HEX = "0123456789ABCDEF"
_PER_REGEL = 8


def hex_regel(off, blok):
    """-> (offsettekst, hextekst, asciitekst) voor één regel."""
    h = []
    a = []
    for b in blok:
        h.append(_HEX[b >> 4] + _HEX[b & 15])
        a.append(chr(b) if 0x20 <= b < 0x7F else ".")
    return "{:06X}".format(off & 0xFFFFFF), " ".join(h), "".join(a)


def toon_hex(kb, f, grootte, titel):
    per_scherm = _PER_REGEL * _REGELS
    top = 0
    x_hex = _X0 + 7 * ui.MONO_W
    x_asc = x_hex + 24 * ui.MONO_W
    while True:
        data = _lees(f, top, per_scherm)
        _LCD.fillScreen(ui.ZWART)
        _kop(titel, "0x{:X}".format(top))
        ui.font_mono()
        for j in range(_REGELS):
            blok = data[j * _PER_REGEL:(j + 1) * _PER_REGEL]
            if not blok:
                break
            y = _Y0 + j * _REGEL_H
            o, h, a = hex_regel(top + j * _PER_REGEL, blok)
            _LCD.setTextColor(ui.GRIJS, ui.ZWART)
            _LCD.drawString(o, _X0, y)
            _LCD.setTextColor(ui.CREME, ui.ZWART)
            _LCD.drawString(h, x_hex, y)
            _LCD.setTextColor(ui.DIM, ui.ZWART)
            for k, b in enumerate(blok):
                if b == 0:
                    _LCD.drawString("00", x_hex + k * 3 * ui.MONO_W, y)
            _LCD.setTextColor(ui.ORANJE, ui.ZWART)
            _LCD.drawString(a, x_asc, y)
        eind = max(0, (grootte - 1) // _PER_REGEL * _PER_REGEL - (_REGELS - 1) * _PER_REGEL)
        while True:
            t = ui.toets(kb)
            if t is None:
                time.sleep_ms(30)
                continue
            if t in ("q", "esc", "del"):
                return None
            if t == "t":
                return "tekst"
            if t == "neer":
                top = min(top + _PER_REGEL, eind)
            elif t == "op":
                top = max(0, top - _PER_REGEL)
            elif t in ("]", " "):
                top = min(top + per_scherm, eind)
            elif t == "[":
                top = max(0, top - per_scherm)
            elif t == "rechts":
                top = min(top + 1, max(eind, 0))
            elif t == "links":
                top = max(0, top - 1)
            elif t == "b":
                top = 0
            elif t == "e":
                top = eind
            elif "0" <= t <= "9" and len(t) == 1:
                top = min(grootte * int(t) // 10 // _PER_REGEL * _PER_REGEL, eind)
            else:
                continue
            break


def toon_bestand(kb, f, grootte, titel, modus):
    """Tekst- of hexviewer, met wisselen via h/t tot de gebruiker terug wil."""
    while modus:
        if modus == "hex":
            modus = toon_hex(kb, f, grootte, titel)
        else:
            modus = toon_tekst(kb, f, grootte, titel)
        gc.collect()
