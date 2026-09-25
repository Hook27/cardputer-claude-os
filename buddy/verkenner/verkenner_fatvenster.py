"""verkenner_fatvenster — één exFAT-bestand tonen als een mini-FAT16-schijf.

### Waarom

UIFlow's tekenfuncties (``drawJpg``/``drawBmp``) lezen een bestand alleen
via een pad op een FAT- of LittleFS-mount, en exFAT kan de firmware niet
mounten. Een foto van een paar MB past ook niet in het RAM. Voor foto's
zonder EXIF-miniatuur (zoals die van een Lumia) zou volle resolutie op
een exFAT-kaart dus onmogelijk zijn.

### Hoe

``FatVenster`` is een blokapparaat in Python dat een FAT16-volume
*verzint* met precies één bestand erop, ``BEELD.<EXT>``. De bootsector,
FAT en hoofdmap maken we ter plekke. Elke datasector wijst naar de juiste
sector van het echte bestand op de exFAT-kaart. MicroPython's VfsFat
mount dit apparaat op ``/sd``, de firmware opent ``/sd/BEELD.JPG`` en
FatFs vraagt sectoren op die wij doorvertalen. De kaart zelf wordt nooit
gemount, en ``writeblocks`` weigert alles: naar de kaart schrijven kán
dit apparaat niet.

Waarom read-write mounten? UIFlow's tekenfuncties openen bestanden met
FA_WRITE, en een readonly volume weigert dat (FR_WRITE_PROTECTED). FatFs
schrijft zelf pas bij f_write/f_sync van een gewijzigd bestand, en dat
gebeurt hier niet.

### Layout (sectoren van 512 bytes)

  0            bootsector (BPB, FAT16, clusters van 64 KB)
  1 .. F       FAT (één kopie): het bestand is één keten vanaf cluster 2
  F+1          hoofdmap: één entry, BEELD.<EXT>
  F+2 ..       data

FAT16 wil minstens 4086 clusters. We maken het volume dus altijd groot
genoeg (virtueel ≥256 MB); de rest is 'vrije ruimte' die niemand leest.
Met clusters van 64 KB hoeft FatFs maar zelden in de FAT te kijken, en
dat scheelt Python-aanroepen per foto.

### Vooruit lezen

De JPEG-decoder vraagt kleine stukjes, dus FatFs vraagt ons één sector per
keer. Eén sector van de kaart lezen kost ~2,1 ms, maar 16 sectoren in één
multi-block-read ~0,4 ms per sector (gemeten 2026-09-25). Bij een losse
datasector halen we daarom ``_VOORUIT`` sectoren tegelijk op en bedienen
we de volgende verzoeken uit die buffer. Een foto van 3 MB ging daarmee
van ~11 s leestijd naar een paar seconden.
"""

import os

import verkenner_ui as ui

_SEC = 512
_CSIZE = 128                 # sectoren per cluster (64 KB, het FAT-maximum)
_CLUS = _SEC * _CSIZE
_MIN_CLUS = 4200             # boven de FAT12-grens van 4085
_MAX_CLUS = 65500            # onder de FAT16-grens van 65525
_VOORUIT = 24                # sectoren per leesactie op de kaart (12 KB = ui.WERK)
MOUNT = "/sd"


def _w16(b, o, v):
    b[o] = v & 0xFF
    b[o + 1] = (v >> 8) & 0xFF


def _w32(b, o, v):
    _w16(b, o, v & 0xFFFF)
    _w16(b, o + 2, (v >> 16) & 0xFFFF)


class FatVenster:
    """Blokapparaat (readblocks/writeblocks/ioctl) over één exFAT-bestand.

    Standaard is BEELD.<EXT> het hele bestand. Met ``begin``/``deel`` wordt
    het een stuk ervan (bijvoorbeeld de EXIF-miniatuur binnen een foto), en
    ``voor`` zet er bytes voor (een ontbrekende SOI-marker). Zo tekent de
    firmware ook een miniatuur die te groot is voor het RAM.
    """

    def __init__(self, fs, eerste, aaneen, bron_lengte, ext="JPG",
                 begin=0, deel=None, voor=b""):
        self.fs = fs
        self.eerste = eerste
        self.aaneen = aaneen
        self.bron_lengte = bron_lengte
        if deel is None:
            deel = bron_lengte - begin
        if begin < 0 or deel < 0 or begin + deel > bron_lengte:
            raise ValueError("deel valt buiten het bestand")
        self.begin = begin
        self.voor = bytes(voor)
        self.lengte = len(self.voor) + deel      # grootte van BEELD.<EXT>
        self._snel = not self.voor and begin % _SEC == 0
        lengte = self.lengte
        self.f_clus = max(1, (lengte + _CLUS - 1) // _CLUS)
        if self.f_clus > _MAX_CLUS - 16:
            raise ValueError("bestand te groot voor het FAT16-venster")
        n = max(_MIN_CLUS, self.f_clus + 16)
        self.fat_sec = ((n + 2) * 2 + _SEC - 1) // _SEC
        self.root_sec = 1 + self.fat_sec
        self.data_sec = self.root_sec + 1
        self.totaal = self.data_sec + n * _CSIZE
        self.ext = (ext.upper() + "   ")[:3]
        self._nul = bytes(_SEC)
        self._boot = self._maak_boot()
        self._root = self._maak_root()
        self._c_idx = 0
        self._c = eerste
        # Vooruit-lees-buffer: een stuk van de vaste werkbuffer (verkenner_ui),
        # want een verse 8 KB kan na wat bladeren niet meer aaneen vrij zijn.
        self._buf = memoryview(ui.WERK)[0:_VOORUIT * _SEC]
        self._b0 = -1                # echte byte-offset van _buf[0]
        self._bn = 0                 # aantal geldige bytes in _buf

    # ---- de verzonnen metadata ------------------------------------------------

    def _maak_boot(self):
        b = bytearray(_SEC)
        b[0:3] = b"\xEB\x3C\x90"
        b[3:11] = b"VERKENNR"
        _w16(b, 11, _SEC)
        b[13] = _CSIZE
        _w16(b, 14, 1)               # gereserveerde sectoren: alleen de boot
        b[16] = 1                    # één FAT
        _w16(b, 17, 16)              # 16 entries in de hoofdmap = 1 sector
        if self.totaal < 0x10000:
            _w16(b, 19, self.totaal)
        else:
            _w32(b, 32, self.totaal)
        b[21] = 0xF8
        _w16(b, 22, self.fat_sec)
        _w16(b, 24, 63)
        _w16(b, 26, 255)
        b[36] = 0x80
        b[38] = 0x29
        _w32(b, 39, 0x5645524B)
        b[43:54] = b"VERKENNER  "
        b[54:62] = b"FAT16   "
        b[510] = 0x55
        b[511] = 0xAA
        return bytes(b)

    def _maak_root(self):
        r = bytearray(_SEC)
        r[0:8] = b"BEELD   "
        r[8:11] = self.ext.encode()
        r[11] = 0x20                 # archief; NIET alleen-lezen (FA_WRITE!)
        datum = ((2026 - 1980) << 9) | (1 << 5) | 1
        _w16(r, 16, datum)
        _w16(r, 18, datum)
        _w16(r, 24, datum)
        _w16(r, 26, 2)               # eerste cluster
        _w32(r, 28, self.lengte)
        return bytes(r)

    def _fat(self, k, out):
        """FAT-sector k (0-based): het bestand is de keten 2 -> 3 -> ... -> EOC."""
        out[0:_SEC] = self._nul
        eind = 2 + self.f_clus - 1
        c = k * 256
        for j in range(256):
            if c == 0:
                v = 0xFFF8
            elif c == 1 or c == eind:
                v = 0xFFFF
            elif 2 <= c < eind:
                v = c + 1
            else:
                break
            out[2 * j] = v & 0xFF
            out[2 * j + 1] = v >> 8
            c += 1

    # ---- data: FAT-sector -> exFAT-sector ---------------------------------------

    def _cluster(self, idx):
        if self.aaneen:
            return self.eerste + idx
        if idx < self._c_idx:
            self._c_idx = 0
            self._c = self.eerste
        while self._c_idx < idx:
            self._c = self.fs.volgende(self._c)
            if not self._c:
                raise OSError(5)
            self._c_idx += 1
        return self._c

    def _blok(self, r):
        """(kaartblok, sectoren tot het eind van de cluster) voor echte offset r."""
        fs = self.fs
        idx, binnen = divmod(r, fs.clus_bytes)
        return (fs.clus_blok(self._cluster(idx)) + binnen // _SEC,
                (fs.clus_bytes - binnen) // _SEC)

    def _echt(self, r, out, n):
        """Kopieer n bytes vanaf echte offset r naar ``out``, via de
        vooruit-lees-buffer (die altijd op een sectorgrens begint)."""
        i = 0
        mv = self._buf
        while i < n:
            j = r - self._b0
            if not 0 <= j < self._bn:
                r0 = r - r % _SEC
                blk, rest = self._blok(r0)
                k = min(_VOORUIT, rest, (self.bron_lengte - r0 + _SEC - 1) // _SEC)
                self.fs.dev.readblocks(blk, mv[0:k * _SEC])
                self._b0 = r0
                self._bn = k * _SEC
                j = r - r0
            k = min(n - i, self._bn - j)
            out[i:i + k] = mv[j:j + k]
            i += k
            r += k

    def _data(self, s, out, max_n):
        """Lees tot ``max_n`` datasectoren vanaf virtuele sector s. -> aantal."""
        o = (s - self.data_sec) * _SEC
        if o >= self.lengte:
            out[0:_SEC] = self._nul
            return 1
        if max_n > 1 and self._snel:
            # Groot, uitgelijnd verzoek: rechtstreeks in de buffer van FatFs.
            r = self.begin + o
            blk, rest = self._blok(r)
            k = min(max_n, rest, (self.lengte - o + _SEC - 1) // _SEC)
            self.fs.dev.readblocks(blk, out[0:k * _SEC])
            return k
        # Eén sector: voorvoegsel + echte bytes via de buffer, rest nullen.
        n = min(_SEC, self.lengte - o)
        p = len(self.voor)
        i = 0
        if o < p:
            i = min(p, o + n) - o
            out[0:i] = self.voor[o:o + i]
        if i < n:
            self._echt(self.begin + o + i - p, out[i:n], n - i)
        if n < _SEC:
            out[n:_SEC] = self._nul[n:]
        return 1

    # ---- blokapparaat-protocol van MicroPython ----------------------------------

    def _sector(self, s, out):
        if s >= self.data_sec:
            self._data(s, out, 1)
        elif s == 0:
            out[0:_SEC] = self._boot
        elif s <= self.fat_sec:
            self._fat(s - 1, out)
        elif s == self.root_sec:
            out[0:_SEC] = self._root
        else:
            out[0:_SEC] = self._nul

    def readblocks(self, nr, buf, off=0):
        # Veruit het vaakst: FatFs vraagt één datasector die al in de
        # vooruit-lees-buffer staat. Die zo kort mogelijk afhandelen, want
        # een foto van 4 MB is ~8000 van deze aanroepen.
        if self._snel and not off and nr >= self.data_sec and len(buf) == _SEC:
            j = self.begin + (nr - self.data_sec) * _SEC - self._b0
            if 0 <= j < self._bn:
                buf[0:_SEC] = self._buf[j:j + _SEC]
                return
        mv = memoryview(buf)
        if off:
            # 'Extended' lezen (VFS-autodetectie zoekt naar littlefs).
            tmp = bytearray(_SEC)
            self._sector(nr, memoryview(tmp))
            n = min(len(buf), _SEC - off)
            mv[0:n] = memoryview(tmp)[off:off + n]
            return
        n = len(buf) // _SEC
        i = 0
        while i < n:
            s = nr + i
            if s >= self.data_sec:
                i += self._data(s, mv[i * _SEC:], n - i)
            else:
                self._sector(s, mv[i * _SEC:(i + 1) * _SEC])
                i += 1

    def writeblocks(self, nr, buf, off=0):
        raise OSError(30)            # EROFS: dit venster schrijft nooit

    def ioctl(self, op, arg):
        if op == 4:
            return self.totaal
        if op == 5:
            return _SEC
        return 0


def koppel(fs, eerste, aaneen, lengte, ext="JPG", begin=0, deel=None, voor=b""):
    """Mount een venster op /sd voor (een deel van) dit bestand.

    -> pad voor drawJpg/drawBmp.
    """
    ontkoppel()
    v = FatVenster(fs, eerste, aaneen, lengte, ext, begin, deel, voor)
    os.mount(v, MOUNT)
    return MOUNT + "/BEELD." + v.ext.strip()


def ontkoppel():
    try:
        os.umount(MOUNT)
    except OSError:
        pass
