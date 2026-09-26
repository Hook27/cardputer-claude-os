"""verkenner_ui — gedeelde meubels voor de verkenner en zijn viewers.

Palet, font, kop- en hintbalk, tekst inkorten, groottes opmaken, een
scrollbare label/waarde-pagina en de toetsenbord-normalisatie. Staat als
peer-module in /flash, zodat verkenner.py (de app) en de viewers er
allemaal uit kunnen putten zonder elk hun eigen kopie.

Zelfde chrome als de rest van de bundel: 20 px DONKER-kop met ORANJE
hairline op y=20, inhoud daaronder, 18 px hintstrip onderaan.

### Leesbaarheid (gemeten op het toestel, 26 sep 2026)

- Er is maar één bruikbaar font: **DejaVu9**, en dat is **15 px hoog**
  (``fontHeight()``), met letters van 3 px (``i``) tot 14 px (``W``) breed.
  ``FONTS.ASCII7`` gedraagt zich op deze firmware precies hetzelfde; een
  klein monospace-font bestaat hier dus niet. Alle regels staan daarom
  ``REGEL_H`` = 16 px uit elkaar. Met 12 px vielen de staarten van
  g/j/p/q/y en de underscore weg.
- De UIFlow-binding tekent tekst altijd met een achtergrondvakje. Daarmee
  wist een ``j`` de ``i`` ervoor ("vrij" werd "vr j"). LovyanGFX slaat de
  achtergrond over als voor- en achtergrondkleur gelijk zijn. ``tekst()``
  tekent daarom altijd zo, op een vlak dat de aanroeper eerst leegmaakt.
"""

import M5

LCD = M5.Lcd
W = 240
H = 135

# Palet van de bundel, plus drie gedempte tinten voor de icoontjes. Bewust
# geen fleurige kleuren: donker, forensisch, alleen accenten in oranje.
ZWART = 0x000000
ORANJE = 0xCC785C
CREME = 0xF0EEE6
DONKER = 0x1F1F1F
GRIJS = 0x777777
DIM = 0x4A4A4A
ROOD = 0xCC4444
GROEN = 0x4CAF50
GEEL = 0xE0B341
BEELDKLEUR = 0x9DB39D   # gedempt groen voor foto's
VIDEOKLEUR = 0x8FA3BF   # gedempt blauwgrijs voor video

KOP_H = 20
HINT_H = 18
INHOUD_Y = KOP_H + 1
INHOUD_H = H - HINT_H - INHOUD_Y

FONT_H = 15             # DejaVu9, gemeten met fontHeight()
REGEL_H = FONT_H + 1    # regelafstand overal in de verkenner

# Eén vaste werkbuffer, gereserveerd zodra de app start, als de heap nog
# schoon is. Gemeten 2026-09-25: na wat bladeren was er 36 KB vrij, maar
# geen aaneengesloten blok van 8 KB meer. De heap van MicroPython schuift
# niets op. Het FAT-venster gebruikt hem als vooruit-lees-buffer, de
# fotoviewer voor een EXIF-miniatuur. Die twee lopen nooit tegelijk.
WERK = bytearray(12 * 1024)

# Extensies per soort, gedeeld door de lijst en de viewers.
JPEG_EXT = ("jpg", "jpeg", "jpe", "jfif")
TOONBAAR = JPEG_EXT + ("bmp", "png")
BEELD_EXT = TOONBAAR + ("gif", "heic", "heif", "hif", "webp", "avif", "tif",
                        "tiff", "dng", "cr2", "cr3", "nef", "arw", "raf",
                        "orf", "rw2")
VIDEO_EXT = ("mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "mts", "m2ts",
             "ts", "wmv", "flv", "mpg", "mpeg", "vob", "lrv", "insv", "360")
TEKST_EXT = ("txt", "log", "csv", "tsv", "json", "py", "md", "htm", "html",
             "xml", "ini", "cfg", "conf", "yaml", "yml", "toml", "js", "css",
             "c", "h", "cpp", "hpp", "sh", "bat", "ps1", "srt", "vtt", "gpx",
             "kml", "nmea", "url", "inf", "rtf", "svg", "sql", "java", "rs",
             "go", "lua", "properties", "nfo", "me", "readme")


def extensie(naam):
    p = naam.rfind(".")
    return naam[p + 1:].lower() if p > 0 else ""


def font_prop():
    try:
        LCD.setFont(LCD.FONTS.DejaVu9)
    except Exception as e:
        print("verkenner: setFont fallback:", e)
    LCD.setTextSize(1)


def tekst(s, x, y, kleur):
    """Teken ``s`` zonder achtergrondvakje (voor- = achtergrondkleur).

    De ondergrond moet de aanroeper zelf leegmaken; zo kan een letter nooit
    meer een buurletter of de staart van de regel erboven wissen.
    """
    LCD.setTextColor(kleur, kleur)
    LCD.drawString(s, x, y)


# ---- tekst ------------------------------------------------------------

# DejaVu9 heeft alleen glyphs voor ASCII. Veelvoorkomende Latijnse letters
# met accent vertalen we, de rest wordt '?'. Twee strings in plaats van een
# dict: dat scheelt een paar KB RAM.
_VAN = "àáâãäåèéêëìíîïòóôõöøùúûüçñýÿÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖØÙÚÛÜÇÑÝæÆ"
_NAAR = "aaaaaaeeeeiiiioooooouuuucnyyAAAAAAEEEEIIIIOOOOOOUUUUCNYaA"


def ascii(s):
    """Maak ``s`` tekenbaar met het ASCII-font van het toestel."""
    for c in s:
        if ord(c) > 0x7E or ord(c) < 0x20:
            break
    else:
        return s
    uit = []
    for c in s:
        o = ord(c)
        if 0x20 <= o <= 0x7E:
            uit.append(c)
        elif c == "ß":
            uit.append("ss")
        else:
            i = _VAN.find(c)
            uit.append(_NAAR[i] if i >= 0 else "?")
    return "".join(uit)


def passend(s, max_px):
    """Kort ``s`` rechts in tot hij in ``max_px`` past (met '..')."""
    if LCD.textWidth(s) <= max_px:
        return s
    while s and LCD.textWidth(s + "..") > max_px:
        s = s[:-1]
    return s + ".."


def passend_links(s, max_px):
    """Kort ``s`` links in (voor paden: het einde is het belangrijkst)."""
    if LCD.textWidth(s) <= max_px:
        return s
    while s and LCD.textWidth(".." + s) > max_px:
        s = s[1:]
    return ".." + s


def grootte_kort(n):
    """Bytes -> hooguit 5 tekens, voor de groottekolom ('999', '12K', '1.4G')."""
    if n < 1000:
        return str(n)
    for eenheid in "KMGT":
        n /= 1024
        if n < 9.95:
            return "{:.1f}{}".format(n, eenheid)
        if n < 999.5:
            return "{}{}".format(int(n + 0.5), eenheid)
    return "{}P".format(int(n / 1024 + 0.5))


def duizendtallen(n):
    """3145728 -> '3.145.728'."""
    s = str(n)
    delen = []
    while len(s) > 3:
        delen.insert(0, s[-3:])
        s = s[:-3]
    delen.insert(0, s)
    return ".".join(delen)


def grootte_mens(n):
    """Bytes -> '3,0 MB' (of '512 bytes')."""
    if n < 1024:
        return "{} bytes".format(n)
    for eenheid in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or eenheid == "TB":
            return "{} {}".format("{:.1f}".format(n).replace(".", ","), eenheid)


def grootte_lang(n):
    """Bytes -> '3.145.728 bytes (3,0 MB)'."""
    exact = duizendtallen(n) + " bytes"
    if n < 1024:
        return exact
    return "{} ({})".format(exact, grootte_mens(n))


# ---- chrome -------------------------------------------------------------


def kop(links, rechts=None, rechts_kleur=GRIJS):
    """Kopbalk: ``links`` in crème (links ingekort), ``rechts`` rechts."""
    LCD.fillRect(0, 0, W, KOP_H, DONKER)
    LCD.fillRect(0, KOP_H, W, 1, ORANJE)
    font_prop()
    rw = 0
    if rechts:
        rw = LCD.textWidth(rechts)
        tekst(rechts, W - rw - 5, 3, rechts_kleur)
        rw += 10
    tekst(passend_links(ascii(links), W - 10 - rw), 5, 3, CREME)


def _strip(s, kleur):
    LCD.fillRect(0, H - HINT_H, W, HINT_H, DONKER)
    font_prop()
    s = passend(ascii(s), W - 8)
    tekst(s, (W - LCD.textWidth(s)) // 2, H - HINT_H + 2, kleur)


def hint(s):
    _strip(s, GRIJS)


def bezig(s):
    """Kort 'bezig'-bericht in de hintstrip terwijl iets traag loopt."""
    _strip(s, ORANJE)


def wis_inhoud():
    LCD.fillRect(0, INHOUD_Y, W, INHOUD_H, ZWART)


def melding(regels, kleur=CREME, y=None):
    """Een paar gecentreerde regels midden in het inhoudsvlak."""
    wis_inhoud()
    font_prop()
    if y is None:
        y = INHOUD_Y + (INHOUD_H - len(regels) * REGEL_H) // 2
    for i, r in enumerate(regels):
        r = passend(ascii(r), W - 10)
        tekst(r, (W - LCD.textWidth(r)) // 2, y + i * REGEL_H, kleur if i == 0 else GRIJS)


def regels_scherm(kb, titel, regels, extra=(), extra_hint=""):
    """Scrollbare pagina met ``(label, waarde, kleur)``-regels.

    De labelkolom is zo breed als het langste label (hooguit 120 px), zodat
    korte labels geen ruimte van de waarden afsnoepen. -> None bij terug
    (q/ESC/del/Enter/i/links), of de toets uit ``extra`` die de gebruiker
    koos.
    """
    font_prop()
    lw = 0
    for r in regels:
        lw = max(lw, LCD.textWidth(r[0]))
    lw = min(lw, 120) + 8
    zichtbaar = INHOUD_H // REGEL_H
    boven = 0
    while True:
        kop(titel)
        wis_inhoud()
        for j, (label, waarde, kleur) in enumerate(regels[boven:boven + zichtbaar]):
            y = INHOUD_Y + j * REGEL_H
            tekst(passend(label, lw - 6), 4, y, GRIJS)
            tekst(passend(ascii(str(waarde)), W - lw - 6), lw, y, kleur)
        # Oranje streepjes rechts: er staat nog meer boven/onder.
        if boven:
            LCD.fillRect(W - 3, INHOUD_Y, 2, 6, ORANJE)
        if boven + zichtbaar < len(regels):
            LCD.fillRect(W - 3, INHOUD_Y + INHOUD_H - 6, 2, 6, ORANJE)
        delen = []
        if len(regels) > zichtbaar:
            delen.append("; . scrol")
        if extra_hint:
            delen.append(extra_hint)
        delen.append("q terug")
        hint("   ".join(delen))
        while True:
            t = wacht_toets(kb)
            if t in ("q", "esc", "del", "enter", "i", "links"):
                return None
            if t in extra:
                return t
            if t == "neer" and boven + zichtbaar < len(regels):
                boven += 1
            elif t == "op" and boven > 0:
                boven -= 1
            else:
                continue
            break


# ---- toetsenbord --------------------------------------------------------


def toets(kb):
    """MatrixKeyboard-toets -> intentie.

    De pijltjes van de Cardputer-Adv geven hun ongeshifte ASCII door:
    ``;`` op, ``.`` neer, ``,`` links, ``/`` rechts. Enter komt als 0x0A
    (LF) op deze firmware, ESC als 0x1B, de del-toets als 0x08. Alle andere
    printbare tekens gaan ongewijzigd terug ('i', '[', '5', ...).
    """
    kb.tick()
    k = kb.get_key()
    if k is None:
        return None
    if isinstance(k, str):
        if not k:
            return None
        k = ord(k[0])
    if k in (0x0A, 0x0D):
        return "enter"
    if k == 0x1B:
        return "esc"
    if k in (0x08, 0x7F):
        return "del"
    if 0x20 <= k <= 0x7E:
        c = chr(k)
        if c == ";":
            return "op"
        if c == ".":
            return "neer"
        if c == ",":
            return "links"
        if c == "/":
            return "rechts"
        return c
    return None


def wacht_toets(kb):
    """Blokkeer tot er een toets komt en geef die terug."""
    import time
    while True:
        t = toets(kb)
        if t is not None:
            return t
        time.sleep_ms(30)
