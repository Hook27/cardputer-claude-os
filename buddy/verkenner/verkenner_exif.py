"""verkenner_exif — beeldheaders en EXIF lezen, zonder iets te tekenen.

Losse module, zodat de infopagina de EXIF kan tonen zonder de hele
fotoviewer in het geheugen te laden (dat scheelt KB's op een toestel met
~62 KB vrij).

- **JPEG**: markers aflopen tot de scan: afmetingen en soort frame
  (baseline SOF0 kan de firmware, progressief niet), en uit de EXIF
  (APP1) camera, opnamedatum, oriëntatie, GPS en de ingebouwde miniatuur.
- **PNG/BMP/GIF**: alleen de afmetingen uit de header.

Een paar camera's (Lumia's bijvoorbeeld) laten bij de EXIF-miniatuur de
SOI-marker weg: die begint dan meteen met DQT. ``mini`` zegt dat
(``zonder_soi``) en de viewer zet FF D8 er zelf voor.
"""


def _lees(f, pos, n):
    f.seek(pos)
    return f.read(n)


class _Tiff:
    """Kleine TIFF-lezer voor EXIF: IFD's lezen met de juiste bytevolgorde."""

    _MAAT = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

    def __init__(self, f, basis, eind):
        self.f = f
        self.basis = basis
        self.eind = eind
        kop = _lees(f, basis, 8)
        if len(kop) < 8 or kop[:2] not in (b"II", b"MM"):
            raise ValueError("geen TIFF")
        self.le = kop[:2] == b"II"
        if self.u16(kop, 2) != 42:
            raise ValueError("geen TIFF")
        self.ifd0 = self.u32(kop, 4)

    def u16(self, b, o):
        return b[o] | (b[o + 1] << 8) if self.le else (b[o] << 8) | b[o + 1]

    def u32(self, b, o):
        if self.le:
            return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)
        return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]

    def ifd(self, off):
        """-> ({tag: (type, count, ruwe 4 bytes)}, offset volgende IFD)."""
        if not off or self.basis + off + 2 > self.eind:
            return {}, 0
        n = self.u16(_lees(self.f, self.basis + off, 2), 0)
        n = min(n, 64)
        blok = _lees(self.f, self.basis + off + 2, n * 12 + 4)
        tags = {}
        for i in range(n):
            o = i * 12
            if o + 12 > len(blok):
                break
            tags[self.u16(blok, o)] = (self.u16(blok, o + 2), self.u32(blok, o + 4),
                                       bytes(blok[o + 8:o + 12]))
        volgende = self.u32(blok, n * 12) if len(blok) >= n * 12 + 4 else 0
        return tags, volgende

    def waarde_bytes(self, entry):
        typ, cnt, ruw = entry
        lengte = self._MAAT.get(typ, 1) * cnt
        if lengte <= 4:
            return ruw[:lengte]
        off = self.u32(ruw, 0)
        if self.basis + off + lengte > self.eind or lengte > 256:
            return b""
        return _lees(self.f, self.basis + off, lengte)

    def tekst(self, tags, tag):
        if tag not in tags:
            return None
        b = self.waarde_bytes(tags[tag])
        s = "".join(chr(c) for c in b if 0x20 <= c < 0x7F)
        return s.strip() or None

    def getal(self, tags, tag):
        if tag not in tags:
            return None
        typ, _cnt, ruw = tags[tag]
        return self.u16(ruw, 0) if typ == 3 else self.u32(ruw, 0)

    def rationals(self, tags, tag):
        if tag not in tags:
            return None
        b = self.waarde_bytes(tags[tag])
        uit = []
        for i in range(0, len(b) - 7, 8):
            noemer = self.u32(b, i + 4)
            uit.append(self.u32(b, i) / noemer if noemer else 0.0)
        return uit


def _gps(t, tags):
    lat = t.rationals(tags, 2)
    lon = t.rationals(tags, 4)
    if not lat or not lon or len(lat) < 3 or len(lon) < 3:
        return None
    b = lat[0] + lat[1] / 60 + lat[2] / 3600
    l = lon[0] + lon[1] / 60 + lon[2] / 3600
    if (t.tekst(tags, 1) or "N").upper().startswith("S"):
        b = -b
    if (t.tekst(tags, 3) or "E").upper().startswith("W"):
        l = -l
    return b, l


def _exif(f, seg, seglen, info):
    """Ontleed een APP1-segment met EXIF; vult ``info`` aan."""
    if seglen < 16 or _lees(f, seg + 4, 6) != b"Exif\x00\x00":
        return
    t = _Tiff(f, seg + 10, seg + 2 + seglen)
    ifd0, ifd1_off = t.ifd(t.ifd0)
    info["merk"] = t.tekst(ifd0, 0x010F)
    info["model"] = t.tekst(ifd0, 0x0110)
    info["datum"] = t.tekst(ifd0, 0x0132)
    info["orient"] = t.getal(ifd0, 0x0112) or 1
    if 0x8769 in ifd0:
        sub, _ = t.ifd(t.getal(ifd0, 0x8769))
        info["datum"] = t.tekst(sub, 0x9003) or info["datum"]
    if 0x8825 in ifd0:
        gps, _ = t.ifd(t.getal(ifd0, 0x8825))
        info["gps"] = _gps(t, gps)
    ifd1, _ = t.ifd(ifd1_off)
    off = t.getal(ifd1, 0x0201)
    n = t.getal(ifd1, 0x0202)
    if off and n and t.basis + off + n <= t.eind:
        pos = t.basis + off
        kop = _lees(f, pos, 2)
        if kop == b"\xFF\xD8":
            info["mini"] = (pos, n, False)
        elif _lees(f, pos - 2, 2) == b"\xFF\xD8":
            info["mini"] = (pos - 2, n + 2, False)
        elif len(kop) == 2 and kop[0] == 0xFF and (kop[1] in (0xDB, 0xC4, 0xC0, 0xDD)
                                                    or 0xE0 <= kop[1] <= 0xEF):
            # Lumia's (en een paar andere camera's) laten de SOI-marker weg:
            # de miniatuur begint meteen met DQT. Bij het laden zetten we
            # FF D8 er zelf voor.
            info["mini"] = (pos, n, True)


def jpeg_info(f, grootte):
    """Loop de JPEG-markers af tot de scan begint.

    -> dict met w, h, sof (marker van het frame), prog, mini (offset,
    lengte, zonder_soi) of None, orient, merk, model, datum, gps. None als
    het geen JPEG is.
    """
    if _lees(f, 0, 3) != b"\xFF\xD8\xFF":
        return None
    info = {"w": 0, "h": 0, "sof": None, "prog": False, "mini": None,
            "orient": 1, "merk": None, "model": None, "datum": None, "gps": None}
    pos = 2
    exif_gehad = False
    while pos + 4 <= grootte:
        kop = _lees(f, pos, 4)
        if len(kop) < 4 or kop[0] != 0xFF:
            break
        m = kop[1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0x01 or 0xD0 <= m <= 0xD8:
            pos += 2
            continue
        seglen = (kop[2] << 8) | kop[3]
        if m == 0xE1 and not exif_gehad:
            exif_gehad = True
            try:
                _exif(f, pos, seglen, info)
            except (ValueError, IndexError, OSError) as e:
                print("verkenner: exif:", repr(e))
        elif 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            sof = _lees(f, pos + 4, 5)
            if len(sof) == 5:
                info["h"] = (sof[1] << 8) | sof[2]
                info["w"] = (sof[3] << 8) | sof[4]
            info["sof"] = m
            info["prog"] = m in (0xC2, 0xC6, 0xCA, 0xCE)
            break
        elif m in (0xDA, 0xD9):
            break
        pos += 2 + seglen
    return info


def mini_afm(f, mini):
    """Afmetingen van de miniatuur uit zijn eerste 2 KB (SOF staat vooraan)."""
    pos, n, zonder_soi = mini
    kop = _lees(f, pos, min(n, 2048))
    if zonder_soi:
        kop = b"\xFF\xD8" + kop
    return jpeg_afm(kop) or (160, 120)


def jpeg_afm(buf):
    """Afmetingen uit een JPEG in het geheugen (voor de miniatuur)."""
    pos = 2
    n = len(buf)
    while pos + 9 <= n:
        if buf[pos] != 0xFF:
            return None
        m = buf[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if 0xD0 <= m <= 0xD8 or m == 0x01:
            pos += 2
            continue
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return (buf[pos + 7] << 8) | buf[pos + 8], (buf[pos + 5] << 8) | buf[pos + 6]
        pos += 2 + ((buf[pos + 2] << 8) | buf[pos + 3])
    return None


def png_info(f):
    b = _lees(f, 0, 29)
    if len(b) < 29 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    w = (b[16] << 24) | (b[17] << 16) | (b[18] << 8) | b[19]
    h = (b[20] << 24) | (b[21] << 16) | (b[22] << 8) | b[23]
    return {"w": w, "h": h, "bits": b[24], "kleur": b[25], "interlace": b[28]}


def bmp_info(f):
    b = _lees(f, 0, 34)
    if len(b) < 30 or b[:2] != b"BM":
        return None
    w = b[18] | (b[19] << 8) | (b[20] << 16) | (b[21] << 24)
    h = b[22] | (b[23] << 8) | (b[24] << 16) | (b[25] << 24)
    if h & 0x80000000:
        h = (1 << 32) - h            # negatieve hoogte = top-down BMP
    comp = b[30] if len(b) > 30 else 0
    return {"w": w, "h": h, "bits": b[28] | (b[29] << 8), "comp": comp}


def gif_info(f):
    b = _lees(f, 0, 10)
    if len(b) < 10 or b[:4] != b"GIF8":
        return None
    return {"w": b[6] | (b[7] << 8), "h": b[8] | (b[9] << 8)}
