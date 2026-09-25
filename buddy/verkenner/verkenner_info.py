"""verkenner_info — infopagina met handtekeningcontrole.

Grootte, tijden, attributen, cluster, en een **handtekeningcontrole**: de
eerste bytes worden vergeleken met bekende formaten. Wijkt de extensie af
van wat de inhoud zegt (een ``.jpg`` die eigenlijk een ZIP is), dan staat
dat er in rood. Bij foto's komt de EXIF erbij (camera, opnamedatum, GPS,
miniatuur, via verkenner_exif), bij video de melding dat afspelen op de
Cardputer niet kan.

Vanaf de infopagina kun je met ``h``/``t`` door naar de hex- of
tekstviewer (verkenner_tekst); de app regelt dat, zodat deze module en de
viewer niet tegelijk geladen zijn.
"""

import time

import verkenner_ui as ui
from verkenner_bron import V_MAP, V_AANEEN, V_VERBORGEN, V_SYSTEEM, V_ALLEEN_LEZEN

_LCD = ui.LCD

TEKST_EXT = ui.TEKST_EXT
VIDEO_EXT = ui.VIDEO_EXT
extensie = ui.extensie


def _lees(f, pos, n):
    f.seek(pos)
    return f.read(n)


# ---- handtekeningen -----------------------------------------------------------

_HANDTEKENINGEN = (
    (b"\xFF\xD8\xFF", "JPEG", ("jpg", "jpeg", "jpe", "jfif", "thm")),
    (b"\x89PNG\r\n\x1a\n", "PNG", ("png",)),
    (b"GIF87a", "GIF", ("gif",)),
    (b"GIF89a", "GIF", ("gif",)),
    (b"%PDF-", "PDF", ("pdf",)),
    (b"PK\x03\x04", "ZIP-container", ("zip", "docx", "xlsx", "pptx", "odt", "ods",
                                      "odp", "jar", "apk", "epub", "kmz", "3mf")),
    (b"PK\x05\x06", "ZIP (leeg)", ("zip",)),
    (b"Rar!\x1a\x07", "RAR", ("rar",)),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip", ("7z",)),
    (b"\x1f\x8b", "GZIP", ("gz", "tgz")),
    (b"BZh", "BZIP2", ("bz2",)),
    (b"\xfd7zXZ\x00", "XZ", ("xz",)),
    (b"\x7fELF", "ELF-programma", ("elf", "so", "o", "bin", "")),
    (b"MZ", "Windows-programma", ("exe", "dll", "sys", "com", "scr", "efi", "ocx")),
    (b"ID3", "MP3", ("mp3",)),
    (b"\xff\xfb", "MP3", ("mp3",)),
    (b"OggS", "Ogg", ("ogg", "oga", "ogv", "opus")),
    (b"fLaC", "FLAC", ("flac",)),
    (b"\x1aE\xdf\xa3", "Matroska/WebM", ("mkv", "webm", "mka")),
    (b"0&\xb2u\x8ef\xcf\x11", "ASF/WMV", ("wmv", "wma", "asf")),
    (b"FLV", "FLV", ("flv",)),
    (b"\x00\x00\x01\xba", "MPEG-PS", ("mpg", "mpeg", "vob")),
    (b"SQLite format 3\x00", "SQLite", ("db", "sqlite", "sqlite3", "db3")),
    (b"II*\x00", "TIFF", ("tif", "tiff", "dng", "cr2", "nef", "arw", "orf", "rw2")),
    (b"MM\x00*", "TIFF", ("tif", "tiff", "nef", "dng", "3fr")),
    (b"FUJIFILMCCD-RAW", "Fuji RAW", ("raf",)),
    (b"{\\rtf", "RTF", ("rtf",)),
    (b"<?xml", "XML", ("xml", "svg", "gpx", "kml", "plist", "xmp")),
    (b"BM", "BMP", ("bmp", "dib")),
)

_VIDEO_SOORT = ("MP4-video", "QuickTime-video", "AVI-video", "Matroska/WebM",
                "ASF/WMV", "FLV", "MPEG-PS", "MPEG-TS", "3GP-video")


def herken_inhoud(kop):
    """Eerste bytes -> (soort, verwachte extensies) of (None, ()).

    ``verwachte extensies`` None betekent: tekst, de extensie is vrij.
    """
    for sig, soort, exts in _HANDTEKENINGEN:
        if kop[:len(sig)] == sig:
            return soort, exts
    if kop[:4] == b"RIFF" and len(kop) >= 12:
        vorm = kop[8:12]
        if vorm == b"WAVE":
            return "WAV", ("wav",)
        if vorm == b"AVI ":
            return "AVI-video", ("avi",)
        if vorm == b"WEBP":
            return "WebP", ("webp",)
        return "RIFF", ()
    if kop[4:8] == b"ftyp" and len(kop) >= 12:
        merk = kop[8:12]
        if merk in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            return "HEIC-foto", ("heic", "heif", "hif")
        if merk == b"avif":
            return "AVIF-foto", ("avif",)
        if merk == b"crx ":
            return "Canon CR3", ("cr3",)
        if merk == b"qt  ":
            return "QuickTime-video", ("mov", "qt")
        if merk in (b"M4A ", b"M4B "):
            return "M4A-audio", ("m4a", "m4b")
        if merk[:3] == b"3gp":
            return "3GP-video", ("3gp", "3g2")
        return "MP4-video", ("mp4", "m4v", "mov", "3gp", "lrv", "insv")
    if len(kop) > 188 and kop[0] == 0x47 and kop[188] == 0x47:
        return "MPEG-TS", ("ts", "mts", "m2ts")
    if kop[:3] == b"\xef\xbb\xbf":
        return "tekst (UTF-8)", None
    if kop and _lijkt_tekst(kop):
        return "tekst", None
    return None, ()


def _lijkt_tekst(b):
    goed = 0
    for c in b:
        if c == 0:
            return False
        if c >= 0x20 or c in (0x09, 0x0A, 0x0D):
            goed += 1
    return goed * 100 >= len(b) * 95


def oordeel(naam, kop):
    """-> (soort, oordeeltekst, kleur) voor de infopagina."""
    soort, exts = herken_inhoud(kop)
    ext = extensie(naam)
    if soort is None:
        return "onbekend", "geen bekende handtekening", ui.GRIJS
    if exts is None:
        if ext in TEKST_EXT or not ext:
            return soort, "klopt", ui.GROEN
        return soort, "tekst met extensie .{}".format(ext), ui.GEEL
    if ext in exts:
        return soort, "klopt met .{}".format(ext), ui.GROEN
    if not exts:
        return soort, "", ui.GRIJS
    return soort, "AFWIJKING: extensie .{}".format(ext or "(geen)"), ui.ROOD


# ---- info ------------------------------------------------------------------


def _tijd(t):
    if not t:
        return None
    s = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*t[:6])
    if len(t) > 6 and t[6] is not None:
        u, m = divmod(abs(t[6]), 60)
        s += " UTC{}{}{}".format("-" if t[6] < 0 else "+", u, ":{:02d}".format(m) if m else "")
    return s


def info_regels(src, map_, e):
    """Bouw de regels van de infopagina: lijst van (label, waarde, kleur)."""
    regels = []
    is_map = e[2] & V_MAP
    regels.append(("Soort", "map" if is_map else "bestand", ui.CREME))
    if not is_map:
        regels.append(("Grootte", ui.grootte_lang(e[3]), ui.CREME))
    try:
        d = src.details(map_, e)
    except Exception as ex:
        print("verkenner: details:", repr(ex))
        d = {}
    for sleutel, label in (("gewijzigd", "Gewijzigd"), ("gemaakt", "Gemaakt"), ("geopend", "Geopend")):
        s = _tijd(d.get(sleutel))
        if s:
            regels.append((label, s, ui.CREME))
    vl = []
    if e[2] & V_ALLEEN_LEZEN:
        vl.append("alleen-lezen")
    if e[2] & V_VERBORGEN:
        vl.append("verborgen")
    if e[2] & V_SYSTEEM:
        vl.append("systeem")
    if vl:
        regels.append(("Attribuut", ", ".join(vl), ui.GEEL))
    if "cluster" in d and not is_map:
        regels.append(("Cluster", "{} ({})".format(
            d["cluster"], "aaneen" if e[2] & V_AANEEN else "FAT-keten"), ui.GRIJS))
        if d.get("geldig") is not None and d["geldig"] < e[3]:
            regels.append(("Geldig", "{} bytes, rest leest 0".format(d["geldig"]), ui.GEEL))
    if is_map:
        return regels
    f = src.open(map_, e)
    try:
        kop = _lees(f, 0, 256)
        soort, tekst, kleur = oordeel(e[1], kop)
        regels.append(("Inhoud", soort, ui.CREME))
        if tekst:
            regels.append(("Controle", tekst, kleur))
        ext = extensie(e[1])
        if soort in _VIDEO_SOORT or ext in VIDEO_EXT:
            regels.append(("Video", "afspelen kan niet op de Cardputer", ui.VIDEOKLEUR))
        if soort == "JPEG":
            _jpeg_regels(f, e, regels)
        elif soort in ("PNG", "BMP", "GIF"):
            import verkenner_exif as vx
            info = {"PNG": vx.png_info, "BMP": vx.bmp_info, "GIF": vx.gif_info}[soort](f)
            if info:
                regels.append(("Beeld", "{}x{}".format(info["w"], info["h"]), ui.CREME))
    finally:
        f.close()
    return regels


def _jpeg_regels(f, e, regels):
    import verkenner_exif as vx
    info = vx.jpeg_info(f, e[3])
    if not info:
        return
    if info["w"]:
        wat = "baseline" if info["sof"] == 0xC0 else ("progressief" if info["prog"] else "SOF {:02X}".format(info["sof"] or 0))
        regels.append(("Beeld", "{}x{} {}".format(info["w"], info["h"], wat),
                       ui.CREME if info["sof"] == 0xC0 else ui.GEEL))
    if info["mini"]:
        regels.append(("Miniatuur", "{} bytes EXIF".format(info["mini"][1]), ui.GRIJS))
    camera = " ".join(x for x in (info["merk"], info["model"]) if x)
    if camera:
        regels.append(("Camera", camera, ui.CREME))
    if info["datum"]:
        regels.append(("Opname", info["datum"], ui.CREME))
    if info["orient"] not in (None, 1):
        regels.append(("Orientatie", str(info["orient"]), ui.GRIJS))
    if info["gps"]:
        regels.append(("GPS", "{:.5f}, {:.5f}".format(*info["gps"]), ui.ORANJE))


def toon_info(kb, src, map_, e, pad):
    """Infopagina voor item ``e``. -> None, of 'hex'/'tekst' als de
    gebruiker vanaf hier naar een viewer wil."""
    ui.bezig("lezen...")
    regels = info_regels(src, map_, e)
    boven = 0
    zichtbaar = (ui.INHOUD_H - 4) // 11
    while True:
        ui.kop(pad + "/" + e[1])
        ui.wis_inhoud()
        for j, (label, waarde, kleur) in enumerate(regels[boven:boven + zichtbaar]):
            y = ui.INHOUD_Y + 3 + j * 11
            ui.font_prop()
            _LCD.setTextColor(ui.GRIJS, ui.ZWART)
            _LCD.drawString(label, 4, y)
            _LCD.setTextColor(kleur, ui.ZWART)
            _LCD.drawString(ui.passend(ui.ascii(str(waarde)), ui.W - 68), 64, y)
        meer = len(regels) > zichtbaar
        ui.hint("; . scrol   h hex   t tekst   q terug" if meer else "h hex   t tekst   q terug")
        while True:
            t = ui.toets(kb)
            if t is None:
                time.sleep_ms(30)
                continue
            if t in ("q", "esc", "del", "enter", "i", "links"):
                return None
            if t in ("h", "t") and not e[2] & V_MAP:
                return "hex" if t == "h" else "tekst"
            if t == "neer" and boven + zichtbaar < len(regels):
                boven += 1
            elif t == "op" and boven > 0:
                boven -= 1
            else:
                continue
            break
