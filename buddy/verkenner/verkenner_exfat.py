"""verkenner_exfat — alleen-lezen exFAT-lezer bovenop ruwe SD-sectoren.

### Waarom

De UIFlow-firmware (MicroPython 1.27, FatFs zonder exFAT) kan exFAT niet
mounten: ``os.mount`` geeft ENODEV. Vrijwel elke SD-kaart boven 32 GB, en
dus de meeste camerakaarten, is exFAT. Daarom lezen we de structuren zelf
via ``machine.SDCard.readblocks()``. Er wordt niets gemount en er bestaat
geen schrijfpad: een software-write-blocker.

### Wat we lezen (Microsoft "exFAT file system specification")

- **Bootsector:** geometrie (sector- en clustergrootte), waar de FAT en de
  cluster heap beginnen, en de eerste cluster van de hoofdmap.
- **Mappen:** reeksen van 32-byte entries. Een bestand is een *entry set*:
  een File-entry (0x85: attributen, tijden) gevolgd door een Stream
  Extension (0xC0: eerste cluster, lengte, NoFatChain) en 1..17
  File Name-entries (0xC1, elk 15 UTF-16-tekens). Een entry met het
  InUse-bit uit is verwijderd; 0x00 betekent einde van de map.
- **Bestanden:** met NoFatChain liggen de clusters aaneen; anders volgen we
  de keten in de FAT (4 bytes per cluster).

### Hervatten

``lijst()`` geeft bij elk bestand de byte-positie van zijn File-entry in de
map mee. Met ``vanaf=`` begin je daar weer te lezen. Zo hoeft de verkenner
nooit een hele map in het geheugen te houden (zie verkenner.py).
"""

import micropython

_BLOK = 512   # blokgrootte van machine.SDCard.readblocks

# Entry-types (met InUse-bit 0x80 aan).
_T_LABEL = 0x83
_T_BESTAND = 0x85
_T_STREAM = 0xC0
_T_NAAM = 0xC1

# FileAttributes.
ATTR_ALLEEN_LEZEN = 0x01
ATTR_VERBORGEN = 0x02
ATTR_SYSTEEM = 0x04
ATTR_MAP = 0x10
ATTR_ARCHIEF = 0x20


def _u16(b, o):
    return b[o] | (b[o + 1] << 8)


def _u32(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def _u64(b, o):
    return _u32(b, o) | (_u32(b, o + 4) << 32)


@micropython.viper
def _laag_ascii(src: ptr8, n: int, dst: ptr8) -> int:
    """Kopieer de lage bytes van ``n`` UTF-16LE-tekens naar ``dst``.

    Geeft ``n`` terug als de naam puur printbaar ASCII is, anders -1 (dan
    decodeert ``_utf16`` hem langzaam maar volledig). Het snelle pad scheelt
    veel bij mappen met duizenden camerabestanden.
    """
    i = 0
    while i < n:
        lo = src[2 * i]
        if src[2 * i + 1] != 0 or lo < 0x20 or lo > 0x7E:
            return -1
        dst[i] = lo
        i += 1
    return n


def _utf16(buf, n):
    """``n`` UTF-16LE-code-units uit ``buf`` -> str (met surrogaatparen)."""
    uit = []
    i = 0
    while i < n:
        c = buf[2 * i] | (buf[2 * i + 1] << 8)
        i += 1
        if 0xD800 <= c < 0xDC00 and i < n:
            c2 = buf[2 * i] | (buf[2 * i + 1] << 8)
            if 0xDC00 <= c2 < 0xE000:
                c = 0x10000 + ((c - 0xD800) << 10) + (c2 - 0xDC00)
                i += 1
        if c == 0:
            break
        uit.append(chr(c))
    return "".join(uit)


def tijdstempel(ts, ms10=0, utc=0):
    """exFAT-tijdstempel -> (jaar, maand, dag, uur, minuut, seconde, utc_min).

    ``utc_min`` is de UTC-afwijking in minuten, of None als de schrijver hem
    niet heeft ingevuld (bit 7 van het UtcOffset-veld). De tijd zelf is de
    lokale kloktijd van het apparaat dat het bestand schreef.
    """
    if not ts:
        return None
    sec = (ts & 0x1F) * 2 + ms10 // 100
    off = None
    if utc & 0x80:
        q = utc & 0x7F
        if q & 0x40:
            q -= 0x80
        off = q * 15
    return (1980 + (ts >> 25), (ts >> 21) & 0x0F, (ts >> 16) & 0x1F,
            (ts >> 11) & 0x1F, (ts >> 5) & 0x3F, sec, off)


class ExFat:
    """Eén exFAT-volume op een blokapparaat met ``readblocks(nr, buf)``."""

    def __init__(self, dev, start=0):
        self.dev = dev
        self.start = start
        b = bytearray(_BLOK)
        dev.readblocks(start, b)
        if bytes(b[3:11]) != b"EXFAT   " or b[510] != 0x55 or b[511] != 0xAA:
            raise ValueError("geen exFAT-bootsector")
        bps_shift = b[108]
        spc_shift = b[109]
        if not 9 <= bps_shift <= 12 or bps_shift + spc_shift > 25:
            raise ValueError("onmogelijke exFAT-geometrie")
        self.sector = 1 << bps_shift
        blk_per_sec = self.sector // _BLOK
        self.clus_bytes = self.sector << spc_shift
        self.blk_per_clus = self.clus_bytes // _BLOK
        fat_offset = _u32(b, 80)
        fat_lengte = _u32(b, 84)
        # Met twee FAT's (TexFAT) zegt VolumeFlags bit 0 welke actief is.
        if b[110] == 2 and _u16(b, 106) & 1:
            fat_offset += fat_lengte
        self.fat_blk = start + fat_offset * blk_per_sec
        self.heap_blk = start + _u32(b, 88) * blk_per_sec
        self.n_clus = _u32(b, 92)
        self.root = _u32(b, 96)
        self.serie = _u32(b, 100)
        self.grootte = _u64(b, 72) * self.sector
        self.procent_gebruikt = b[112] if b[112] <= 100 else None
        self._fat = bytearray(_BLOK)
        self._fat_nr = -1
        self._label = None

    # ---- clusters --------------------------------------------------------

    def geldig(self, c):
        return 2 <= c <= self.n_clus + 1

    def clus_blok(self, c):
        return self.heap_blk + (c - 2) * self.blk_per_clus

    def volgende(self, c):
        """Volgende cluster in de FAT-keten, of 0 aan het eind van de keten."""
        o = c * 4
        blk = self.fat_blk + o // _BLOK
        if blk != self._fat_nr:
            self.dev.readblocks(blk, self._fat)
            self._fat_nr = blk
        n = _u32(self._fat, o % _BLOK)
        return n if self.geldig(n) else 0

    def _map_blokken(self, eerste, aaneen, lengte, vanaf):
        """Yield ``(blok, positie)`` voor de blokken van een map, vanaf byte
        ``vanaf`` (afgerond op een blok). ``lengte`` None = volg de FAT tot
        het eind (de hoofdmap heeft geen lengteveld)."""
        cb = self.clus_bytes
        c = eerste
        for _ in range(vanaf // cb):
            c = c + 1 if aaneen else self.volgende(c)
            if not self.geldig(c):
                return
        pos = (vanaf // cb) * cb
        binnen = (vanaf % cb) // _BLOK
        stappen = 0
        while self.geldig(c):
            b0 = self.clus_blok(c)
            for i in range(binnen, self.blk_per_clus):
                p = pos + i * _BLOK
                if lengte is not None and p >= lengte:
                    return
                yield b0 + i, p
            binnen = 0
            pos += cb
            stappen += 1
            if stappen > self.n_clus:   # kringloop in een kapotte FAT
                return
            c = c + 1 if aaneen else self.volgende(c)

    # ---- mappen ----------------------------------------------------------

    def lijst(self, eerste, aaneen=False, lengte=None, vanaf=0, details=False):
        """Loop de bestanden van een map af.

        Yield per bestand ``(positie, naam, attr, eerste_cluster, lengte,
        aaneen, extra)``. ``positie`` is de byte-offset van de File-entry
        (bruikbaar als ``vanaf``). ``extra`` is None, of met ``details=True``
        een tuple ``(ctime, c10, cutc, mtime, m10, mutc, atime, autc,
        valid_data_length)`` met de ruwe tijdvelden.
        """
        # Eigen buffers per aanroep: zo mag een tweede lijst() (bijvoorbeeld
        # label()) lopen terwijl een eerste nog openstaat.
        buf = bytearray(_BLOK)
        naam = bytearray(_BLOK)
        asc = bytearray(256)
        rest = 0
        set_pos = attr = nlen = n_units = 0
        f_eerste = f_lengte = f_geldig = 0
        f_aaneen = stream = False
        tijden = None
        for blk, bpos in self._map_blokken(eerste, aaneen, lengte, vanaf):
            self.dev.readblocks(blk, buf)
            o = vanaf - bpos if bpos < vanaf else 0
            while o < _BLOK:
                t = buf[o]
                if t == 0:
                    return
                if not t & 0x80:
                    rest = 0                    # verwijderd of ongebruikt
                elif t & 0x40:                  # secundaire entry
                    if rest:
                        if t == _T_STREAM:
                            f_aaneen = bool(buf[o + 1] & 0x02)
                            nlen = buf[o + 3]
                            f_geldig = _u64(buf, o + 8)
                            f_eerste = _u32(buf, o + 20)
                            f_lengte = _u64(buf, o + 24)
                            stream = True
                        elif t == _T_NAAM and n_units < 255:
                            naam[2 * n_units:2 * n_units + 30] = buf[o + 2:o + 32]
                            n_units += 15
                        rest -= 1
                        if rest == 0 and stream:
                            n = min(nlen, n_units)
                            k = _laag_ascii(naam, n, asc)
                            if k >= 0:
                                s = bytes(asc[:k]).decode()
                            else:
                                s = _utf16(naam, n)
                            extra = None
                            if details:
                                extra = tijden + (f_geldig,)
                            yield (set_pos, s, attr, f_eerste, f_lengte,
                                   f_aaneen, extra)
                else:                           # primaire entry
                    rest = 0
                    if t == _T_BESTAND:
                        rest = buf[o + 1]
                        set_pos = bpos + o
                        attr = _u16(buf, o + 4)
                        n_units = 0
                        stream = False
                        if details:
                            tijden = (_u32(buf, o + 8), buf[o + 20], buf[o + 22],
                                      _u32(buf, o + 12), buf[o + 21], buf[o + 23],
                                      _u32(buf, o + 16), buf[o + 24])
                    elif t == _T_LABEL and self._label is None:
                        self._label = _utf16(buf[o + 2:o + 24], min(buf[o + 1], 11))
                o += 32

    def label(self):
        """Volumelabel uit de hoofdmap ('' als er geen is)."""
        if self._label is None:
            for _ in self.lijst(self.root):
                if self._label is not None:
                    break
            if self._label is None:
                self._label = ""
        return self._label

    # ---- bestanden -------------------------------------------------------

    def open(self, eerste, aaneen, lengte, geldig=None):
        return Bestand(self, eerste, aaneen, lengte, geldig)


class Bestand:
    """Leesbaar bestand op een exFAT-volume (read/readinto/seek/tell)."""

    def __init__(self, fs, eerste, aaneen, lengte, geldig=None):
        self.fs = fs
        self.eerste = eerste
        self.aaneen = aaneen
        self.lengte = lengte
        self.geldig = lengte if geldig is None else min(geldig, lengte)
        self.pos = 0
        self._c_idx = 0
        self._c = eerste
        self._blk = bytearray(_BLOK)
        self._blk_nr = -1

    def _cluster(self, idx):
        if self.aaneen:
            return self.eerste + idx
        if idx < self._c_idx:
            self._c_idx = 0
            self._c = self.eerste
        while self._c_idx < idx:
            self._c = self.fs.volgende(self._c)
            if not self._c:
                raise OSError(5)    # EIO: keten korter dan het bestand
            self._c_idx += 1
        return self._c

    def readinto(self, buf, n=None):
        if n is None:
            n = len(buf)
        n = min(n, self.lengte - self.pos)
        if n <= 0:
            return 0
        fs = self.fs
        mv = memoryview(buf)
        gedaan = 0
        while gedaan < n:
            if self.pos >= self.geldig:
                # Voorbij ValidDataLength leest exFAT nullen.
                for i in range(gedaan, n):
                    buf[i] = 0
                self.pos += n - gedaan
                return n
            stop = min(n, gedaan + self.geldig - self.pos)
            idx, binnen = divmod(self.pos, fs.clus_bytes)
            blk = fs.clus_blok(self._cluster(idx)) + binnen // _BLOK
            off = binnen % _BLOK
            if off == 0 and stop - gedaan >= _BLOK:
                k = min((stop - gedaan) // _BLOK, fs.blk_per_clus - binnen // _BLOK)
                fs.dev.readblocks(blk, mv[gedaan:gedaan + k * _BLOK])
                stap = k * _BLOK
            else:
                if blk != self._blk_nr:
                    fs.dev.readblocks(blk, self._blk)
                    self._blk_nr = blk
                stap = min(_BLOK - off, stop - gedaan)
                mv[gedaan:gedaan + stap] = memoryview(self._blk)[off:off + stap]
            gedaan += stap
            self.pos += stap
        return n

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.lengte - self.pos
        n = max(0, min(n, self.lengte - self.pos))
        buf = bytearray(n)
        k = self.readinto(buf)
        return bytes(buf[:k]) if k < n else bytes(buf)

    def seek(self, pos, waar=0):
        if waar == 1:
            pos += self.pos
        elif waar == 2:
            pos += self.lengte
        self.pos = max(0, pos)
        return self.pos

    def tell(self):
        return self.pos

    def close(self):
        self._blk = None
