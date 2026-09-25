"""verkenner_ui — gedeelde meubels voor de verkenner en zijn viewers.

Palet, fonts, kop- en hintbalk, tekst inkorten, groottes opmaken en de
toetsenbord-normalisatie. Staat als peer-module in /flash zodat
verkenner.py (de app) en de viewers (verkenner_beeld, verkenner_tekst) er
allemaal uit kunnen putten zonder elk hun eigen kopie.

Zelfde chrome als de rest van de bundel: 20 px DONKER-kop met ORANJE
hairline op y=20, inhoud daaronder, 18 px hintstrip onderaan. Cijfers,
groottes en hex staan in ASCII7 (6×8 monospace); namen en uitleg in
DejaVu9.
"""

import M5

LCD = M5.Lcd
W = 240
H = 135

# Palet van de bundel, plus drie gedempte tinten voor typelabels. Bewust
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

MONO_W = 6   # ASCII7 is 6 px breed per teken
MONO_H = 8

# Eén vaste werkbuffer, gereserveerd zodra de app start, als de heap nog
# schoon is. Gemeten 2026-09-25: na wat bladeren was er 36 KB vrij, maar
# geen aaneengesloten blok van 8 KB meer. De heap van MicroPython schuift
# niets op. Het FAT-venster gebruikt de eerste 8 KB als vooruit-lees-buffer,
# de fotoviewer de hele buffer voor een EXIF-miniatuur. Die twee lopen
# nooit tegelijk.
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


def font_mono():
    try:
        LCD.setFont(LCD.FONTS.ASCII7)
    except Exception as e:
        print("verkenner: mono-font fallback:", e)
    LCD.setTextSize(1)


# ---- tekst ------------------------------------------------------------

# DejaVu9 en ASCII7 hebben alleen glyphs voor ASCII. Veelvoorkomende
# Latijnse letters met accent vertalen we, de rest wordt '?'. Twee strings
# in plaats van een dict: dat scheelt een paar KB RAM.
_VAN = "àáâãäåèéêëìíîïòóôõöøùúûüçñýÿÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖØÙÚÛÜÇÑÝæÆ"
_NAAR = "aaaaaaeeeeiiiioooooouuuucnyyAAAAAAEEEEIIIIOOOOOOUUUUCNYaA"


def ascii(tekst):
    """Maak ``tekst`` tekenbaar met de ASCII-fonts van het toestel."""
    for c in tekst:
        if ord(c) > 0x7E or ord(c) < 0x20:
            break
    else:
        return tekst
    uit = []
    for c in tekst:
        o = ord(c)
        if 0x20 <= o <= 0x7E:
            uit.append(c)
        elif c == "ß":
            uit.append("ss")
        else:
            i = _VAN.find(c)
            uit.append(_NAAR[i] if i >= 0 else "?")
    return "".join(uit)


def passend(tekst, max_px):
    """Kort ``tekst`` rechts in tot hij in ``max_px`` past (met '..')."""
    if LCD.textWidth(tekst) <= max_px:
        return tekst
    while tekst and LCD.textWidth(tekst + "..") > max_px:
        tekst = tekst[:-1]
    return tekst + ".."


def passend_links(tekst, max_px):
    """Kort ``tekst`` links in (voor paden: het einde is het belangrijkst)."""
    if LCD.textWidth(tekst) <= max_px:
        return tekst
    while tekst and LCD.textWidth(".." + tekst) > max_px:
        tekst = tekst[1:]
    return ".." + tekst


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


def grootte_lang(n):
    """Bytes -> '3.145.728 bytes (3,0 MB)'."""
    s = str(n)
    delen = []
    while len(s) > 3:
        delen.insert(0, s[-3:])
        s = s[:-3]
    delen.insert(0, s)
    exact = ".".join(delen) + " bytes"
    if n < 1024:
        return exact
    for eenheid in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or eenheid == "TB":
            return "{} ({} {})".format(exact, "{:.1f}".format(n).replace(".", ","), eenheid)
    return exact


# ---- chrome -------------------------------------------------------------


def kop(links, rechts=None, rechts_kleur=GRIJS):
    """Kopbalk: ``links`` in crème (ingekort), ``rechts`` in mono."""
    LCD.fillRect(0, 0, W, KOP_H, DONKER)
    LCD.fillRect(0, KOP_H, W, 1, ORANJE)
    rw = 0
    if rechts:
        font_mono()
        rw = len(rechts) * MONO_W
        LCD.setTextColor(rechts_kleur, DONKER)
        LCD.drawString(rechts, W - rw - 5, 6)
        rw += 8
    font_prop()
    LCD.setTextColor(CREME, DONKER)
    LCD.drawString(passend_links(ascii(links), W - 10 - rw), 5, 5)


def hint(tekst):
    LCD.fillRect(0, H - HINT_H, W, HINT_H, DONKER)
    font_prop()
    LCD.setTextColor(GRIJS, DONKER)
    tekst = passend(tekst, W - 8)
    LCD.drawString(tekst, (W - LCD.textWidth(tekst)) // 2, H - 14)


def wis_inhoud():
    LCD.fillRect(0, INHOUD_Y, W, INHOUD_H, ZWART)


def melding(regels, kleur=CREME, y=None):
    """Een paar gecentreerde regels midden in het inhoudsvlak."""
    wis_inhoud()
    font_prop()
    if y is None:
        y = INHOUD_Y + (INHOUD_H - len(regels) * 13) // 2
    for i, r in enumerate(regels):
        r = passend(ascii(r), W - 10)
        LCD.setTextColor(kleur if i == 0 else GRIJS, ZWART)
        LCD.drawString(r, (W - LCD.textWidth(r)) // 2, y + i * 13)


def bezig(tekst):
    """Kort 'bezig'-bericht in de hintstrip terwijl iets traag loopt."""
    LCD.fillRect(0, H - HINT_H, W, HINT_H, DONKER)
    font_prop()
    LCD.setTextColor(ORANJE, DONKER)
    tekst = passend(ascii(tekst), W - 8)
    LCD.drawString(tekst, (W - LCD.textWidth(tekst)) // 2, H - 14)


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
