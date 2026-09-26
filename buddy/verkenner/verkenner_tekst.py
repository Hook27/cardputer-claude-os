"""verkenner_tekst — tekst- en hexviewer.

Er is op dit toestel geen klein monospace-font: ``ASCII7`` gedraagt zich
als DejaVu9, dat 15 px hoog is en per letter 3 (``i``) tot 14 px (``W``)
breed (gemeten 26 sep 2026). Beide viewers rekenen daarom in pixels.

### Tekst

DejaVu9, 7 regels van 16 px. Bij het openen meten we één keer de breedte
van elk ASCII-teken (``maten()``). ``breek()`` telt die op en breekt af
zodra een regel breder dan het scherm zou worden. We onthouden alleen de
byte-positie van de bovenste regel. Terugbladeren zoekt het begin van de
logische regel (de vorige newline, hooguit 2 KB terug) en breekt van
daaruit opnieuw af. Zo werkt het ook voor logbestanden van megabytes
zonder index in het RAM. UTF-8 wordt gedecodeerd; accenten worden vertaald
en de rest wordt '?'.

### Hex

Acht bytes per regel, 7 regels: offset (laagste 4 hexcijfers; de kop toont
de volledige), dan elke byte in een vaste cel van 18 px (gecentreerd, want
de cijfers zijn niet even breed), dan de ASCII. Nullen zijn gedimd.

Toetsen: ``;``/``.`` regel, ``[``/``]`` pagina, ``0``-``9`` naar 0-90%,
``b``/``e`` begin/eind, ``h``/``t`` wissel hex/tekst, q/ESC/del terug.
De infopagina staat apart in verkenner_info, zodat de viewer en de
handtekeningtabel niet tegelijk in het geheugen hoeven.
"""

import gc
import time

import verkenner_ui as ui

_LCD = ui.LCD

_KOP_H = ui.REGEL_H
_Y0 = _KOP_H + 1
_REGEL_H = ui.REGEL_H
_REGELS = (ui.H - _Y0) // _REGEL_H
_X0 = 2
_BREEDTE = ui.W - 2 * _X0
_BLOK = 2048


def _lees(f, pos, n):
    f.seek(pos)
    return f.read(n)


def _kop(titel, rechts):
    _LCD.fillRect(0, 0, ui.W, _KOP_H, ui.DONKER)
    ui.font_prop()
    rw = _LCD.textWidth(rechts)
    ui.tekst(rechts, ui.W - rw - 3, 0, ui.ORANJE)
    ui.tekst(ui.passend(ui.ascii(titel), ui.W - rw - 12), 3, 0, ui.CREME)


def _procent(pos, grootte):
    return "{}%".format(pos * 100 // grootte if grootte else 100)


def maten():
    """Pixelbreedte van elk ASCII-teken in het huidige font (index = code)."""
    ui.font_prop()
    t = bytearray(128)
    for c in range(32, 127):
        t[c] = _LCD.textWidth(chr(c))
    return t


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


def breek(data, max_regels, maat, breedte=_BREEDTE):
    """Breek bytes af in schermregels. -> lijst van (offset, tekst).

    ``maat[c]`` is de breedte in px van ASCII-teken ``c``. Een regel begint
    na een newline, of waar het volgende teken niet meer in ``breedte``
    past. Een tab telt als vier spaties, CR wordt genegeerd, stuurtekens
    worden '.'.
    """
    uit = []
    n = len(data)
    i = 0
    start = 0
    regel = []
    x = 0
    while i < n and len(uit) < max_regels:
        b = data[i]
        if b == 0x0A:
            uit.append((start, "".join(regel)))
            regel = []
            x = 0
            i += 1
            start = i
            continue
        if b == 0x0D:
            i += 1
            continue
        k = 1
        if b == 0x09:
            ch = "    "
            w = 4 * maat[32]
        elif 0x20 <= b < 0x7F:
            ch = chr(b)
            w = maat[b]
        elif b >= 0x80:
            ch, k = _teken_utf8(data, i, n)
            ch = ch[:1]
            w = maat[ord(ch)]
        else:
            ch = "."
            w = maat[46]
        if x + w > breedte and regel:
            uit.append((start, "".join(regel)))
            regel = []
            x = 0
            start = i
            if len(uit) >= max_regels:
                break
        regel.append(ch)
        x += w
        i += k
    if regel and len(uit) < max_regels:
        uit.append((start, "".join(regel)))
    return uit


def vorige_regel(f, p, maat, breedte=_BREEDTE):
    """Byte-offset van de schermregel vóór de regel die op ``p`` begint."""
    if p <= 0:
        return 0
    a = max(0, p - _BLOK)
    data = _lees(f, a, p - a)
    k = data.rfind(b"\n", 0, len(data) - 1)
    begin = k + 1 if k >= 0 else 0
    regels = breek(data[begin:], 10000, maat, breedte)
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
    maat = maten()
    top = 0
    while True:
        data = _lees(f, top, min(_BLOK, max(0, grootte - top)))
        regels = breek(data, _REGELS + 1, maat)
        _LCD.fillScreen(ui.ZWART)
        _kop(titel, _procent(top, grootte))
        for j, (_o, s) in enumerate(regels[:_REGELS]):
            ui.tekst(s, _X0, _Y0 + j * _REGEL_H, ui.CREME)
        if not regels:
            ui.tekst("(leeg)", _X0, _Y0, ui.GRIJS)
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
                top = vorige_regel(f, top, maat)
            elif t == "[":
                for _ in range(_REGELS):
                    top = vorige_regel(f, top, maat)
            elif t == "b":
                top = 0
            elif t == "e":
                top = grootte
                for _ in range(_REGELS):
                    top = vorige_regel(f, top, maat)
            elif "0" <= t <= "9" and len(t) == 1:
                top = _synchroon(f, grootte * int(t) // 10, grootte)
            else:
                continue
            break


# ---- hex --------------------------------------------------------------------

_HEX = "0123456789ABCDEF"
_PER_REGEL = 8
_CEL = 18                                     # px per byte; "DB" is 19 px
_X_HEX = 36                                   # na 4 offsetcijfers (~30 px)
_X_ASC = _X_HEX + _PER_REGEL * _CEL + 2


def hex_regel(off, blok):
    """-> (offset: laagste 4 hexcijfers, [bytepaar per byte], ascii)."""
    paren = [_HEX[b >> 4] + _HEX[b & 15] for b in blok]
    a = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in blok)
    return "{:04X}".format(off & 0xFFFF), paren, a


def toon_hex(kb, f, grootte, titel):
    maat = maten()
    per_scherm = _PER_REGEL * _REGELS
    top = 0
    while True:
        data = _lees(f, top, per_scherm)
        _LCD.fillScreen(ui.ZWART)
        _kop(titel, "0x{:X}  {}".format(top, _procent(top, grootte)))
        for j in range(_REGELS):
            blok = data[j * _PER_REGEL:(j + 1) * _PER_REGEL]
            if not blok:
                break
            y = _Y0 + j * _REGEL_H
            o, paren, a = hex_regel(top + j * _PER_REGEL, blok)
            ui.tekst(o, _X0, y, ui.GRIJS)
            for k, p in enumerate(paren):
                w = maat[ord(p[0])] + maat[ord(p[1])]
                ui.tekst(p, _X_HEX + k * _CEL + (_CEL - w) // 2, y,
                         ui.DIM if p == "00" else ui.CREME)
            ui.tekst(a, _X_ASC, y, ui.ORANJE)
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
