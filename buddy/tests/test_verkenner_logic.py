"""Hardware-vrije tests voor de verkenner (buddy/verkenner/: de app
verkenner.py en de verkenner_*-modules).

De device-code is MicroPython, maar de logica eronder (exFAT lezen,
kaarten herkennen, JPEG/EXIF ontleden, tekst ombreken, de lijstvensters)
is gewone Python. We stubben de MicroPython/M5-modules (``micropython``,
``M5``, ``hardware``, ``machine``, ``esp32``) en een MicroPython-achtige
``time`` en ``gc``, en draaien de modules dan onder CPython.

Het exFAT-image wordt hier zelf opgebouwd volgens de Microsoft-spec, met
de lastige gevallen erin: lange en niet-ASCII-namen, een emoji
(surrogaatpaar), verwijderde entries, aaneengesloten bestanden en
bestanden met een FAT-keten, en entry sets die over een clustergrens
lopen. Het blokapparaat heeft bewust geen ``writeblocks``: elke
schrijfpoging zou crashen.

De rookproef draait de hele app met gescripte toetsen over zo'n nep-kaart.
Raakt het script leeg voordat de app zelf stopt, dan faalt de test.

Pixels controleren we niet; dat kan alleen op het toestel. De JPEG-tests
hebben Pillow nodig; zonder Pillow worden ze overgeslagen.

Draaien:   python buddy/tests/test_verkenner_logic.py
Of:        pytest buddy/tests/
"""

import builtins
import gc
import io
import os
import struct
import sys
import tempfile
import time
import types

_HIER = os.path.dirname(os.path.abspath(__file__))
_BRON = os.path.join(_HIER, "..", "verkenner")

try:
    from PIL import Image
except ImportError:          # pragma: no cover
    Image = None


# ---- MicroPython/M5-stubs --------------------------------------------------


class ToetsenOp(Exception):
    """Het toetsenscript is op terwijl de app nog wacht."""


class NepToetsenbord:
    rij = []

    def tick(self):
        pass

    def get_key(self):
        if not NepToetsenbord.rij:
            raise ToetsenOp()
        k = NepToetsenbord.rij.pop(0)
        return None if k is None else (ord(k) if isinstance(k, str) and len(k) == 1 else k)


_MOUNTS = {}


def _nep_mount(dev, pad, readonly=False):
    if pad in _MOUNTS:
        raise OSError(1)                # MicroPython: EPERM als al gemount
    _MOUNTS[pad] = dev


def _nep_umount(pad):
    if pad not in _MOUNTS:
        raise OSError(22)
    del _MOUNTS[pad]


def _u16(b, o):
    return b[o] | (b[o + 1] << 8)


def _u32(b, o):
    return _u16(b, o) | (_u16(b, o + 2) << 16)


def lees_fat16(dev, naam):
    """Lees een bestand van een FAT16-blokapparaat zoals FatFs dat doet.

    Valideert de bootsector met dezelfde regels als FatFs' find_volume
    (BytsPerSec, NumFATs, SecPerClus macht van 2, RootEntCnt, clustertelling
    -> FAT16, FAT groot genoeg), zoekt de 8.3-naam in de hoofdmap en volgt
    de FAT-keten.
    """
    b = bytearray(512)
    dev.readblocks(0, b)
    assert b[510:512] == b"\x55\xAA" and b[0] in (0xEB, 0xE9) and b[54:57] == b"FAT"
    assert _u16(b, 11) == 512
    csize = b[13]
    assert csize and not csize & (csize - 1)
    nrsv, nfats, nroot = _u16(b, 14), b[16], _u16(b, 17)
    assert nrsv >= 1 and nfats in (1, 2) and nroot % 16 == 0
    tsect = _u16(b, 19) or _u32(b, 32)
    fasize = _u16(b, 22)
    sysect = nrsv + fasize * nfats + nroot // 16
    nclst = (tsect - sysect) // csize
    assert 4085 < nclst <= 65525, "geen FAT16: {} clusters".format(nclst)
    assert fasize * 512 >= (nclst + 2) * 2
    rd = bytearray(nroot * 32)
    dev.readblocks(nrsv + fasize * nfats, rd)
    doel = naam.upper().split(".")
    doel = (doel[0] + " " * 8)[:8] + (doel[1] + "   ")[:3]
    for o in range(0, len(rd), 32):
        if bytes(rd[o:o + 11]).decode() == doel:
            assert not rd[o + 11] & 0x01, "alleen-lezen: FA_WRITE zou falen"
            c, grootte = _u16(rd, o + 26), _u32(rd, o + 28)
            break
    else:
        raise AssertionError("niet in hoofdmap: " + naam)
    data = bytearray()
    while len(data) < grootte:
        stuk = bytearray(csize * 512)
        dev.readblocks(sysect + (c - 2) * csize, stuk)
        data += stuk
        fs = bytearray(512)
        dev.readblocks(nrsv + (c * 2) // 512, fs)
        c = _u16(fs, (c * 2) % 512)
        if c >= 0xFFF8:
            break
    return bytes(data[:grootte])


class NepLcd:
    class FONTS:
        DejaVu9 = "DejaVu9"
        ASCII7 = "ASCII7"

    def __init__(self):
        self.font = "DejaVu9"
        self.teksten = []
        self.tekeningen = []
        self.via_venster = []
        self.buffers = []
        self.rotatie = 1

    def setFont(self, f):
        self.font = f

    def textWidth(self, s):
        return len(s) * (6 if self.font == "ASCII7" else 5)

    def drawString(self, s, x, y):
        self.teksten.append((s, x, y))

    def getRotation(self):
        return self.rotatie

    def setRotation(self, r):
        self.rotatie = r

    def _beeld(self, soort, img, *a):
        self.tekeningen.append((soort, img if isinstance(img, str) else len(img), a))
        if not isinstance(img, str):
            self.buffers.append(bytes(img))
        if isinstance(img, str) and img.startswith("/sd/") and "/sd" in _MOUNTS:
            # Zoals de firmware: het bestand via FatFs van de mount lezen.
            self.via_venster.append((img, lees_fat16(_MOUNTS["/sd"], img[4:])))
        time.sleep(0.003)       # 'geslaagd' volgens de tijdmeting

    def drawJpg(self, img, *a):
        self._beeld("jpg", img, *a)

    def drawBmp(self, img, *a):
        self._beeld("bmp", img, *a)

    def drawPng(self, img, *a):
        self._beeld("png", img, *a)

    def __getattr__(self, naam):
        return lambda *a, **k: None


class NepWlan:
    """network.WLAN met een logboek van wat de app ermee doet."""
    staat = {"actief": True, "verbonden": True, "log": []}

    def __init__(self, _interface):
        pass

    def active(self, *a):
        if a:
            NepWlan.staat["actief"] = bool(a[0])
            if not a[0]:
                NepWlan.staat["verbonden"] = False
            NepWlan.staat["log"].append(("active", bool(a[0])))
            return None
        return NepWlan.staat["actief"]

    def isconnected(self):
        return NepWlan.staat["verbonden"]

    def connect(self, ssid, _wachtwoord):
        NepWlan.staat["log"].append(("connect", ssid))


def _installeer_nep():
    mp = types.ModuleType("micropython")
    mp.viper = lambda f: f
    mp.native = lambda f: f
    mp.const = lambda x: x
    sys.modules["micropython"] = mp
    for naam in ("ptr8", "ptr16", "ptr32"):
        setattr(builtins, naam, lambda x: x)

    time.ticks_ms = lambda: int(time.perf_counter() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b
    time.sleep_ms = lambda ms: None
    gc.mem_free = lambda: 60000
    os.mount = _nep_mount
    os.umount = _nep_umount

    m5 = types.ModuleType("M5")
    m5.Lcd = NepLcd()
    m5.begin = lambda: None
    sys.modules["M5"] = m5

    hw = types.ModuleType("hardware")
    hw.MatrixKeyboard = NepToetsenbord
    sys.modules["hardware"] = hw

    esp = types.ModuleType("esp32")
    esp.HEAP_DATA = 0
    esp.idf_heap_info = lambda _t: [(32768, 32000, 31744, 32000)]
    sys.modules["esp32"] = esp

    machine = types.ModuleType("machine")
    machine.SDCard = None          # per test ingevuld
    sys.modules["machine"] = machine

    net = types.ModuleType("network")
    net.STA_IF = 0
    net.WLAN = NepWlan
    sys.modules["network"] = net

    # wifi_event.py (met de echte WiFi-gegevens) staat in .gitignore; de app
    # leest er alleen SSID/PASSWORD uit bij het afsluiten.
    wifi = types.ModuleType("wifi_event")
    wifi.SSID = "testnet"
    wifi.PASSWORD = "geheim"
    sys.modules["wifi_event"] = wifi

    if _BRON not in sys.path:
        sys.path.insert(0, _BRON)


_installeer_nep()


# ---- exFAT-image bouwen -----------------------------------------------------

_SEC = 512


def _utf16(s):
    return s.encode("utf-16-le")


def _checksum_set(entries):
    c = 0
    for i, b in enumerate(entries):
        if i in (2, 3):
            continue
        c = (((c << 15) | (c >> 1)) + b) & 0xFFFF
    return c


def _ts(j, mnd, d, u, m, s):
    return ((j - 1980) << 25) | (mnd << 21) | (d << 16) | (u << 11) | (m << 5) | (s // 2)


class ExfatBouwer:
    """Minimale exFAT-bouwer: net genoeg voor wat verkenner_exfat leest."""

    def __init__(self, n_clus=256, spc_shift=3, partitie=64):
        self.spc = 1 << spc_shift
        self.spc_shift = spc_shift
        self.cb = _SEC * self.spc
        self.n_clus = n_clus
        self.part = partitie
        self.fat_off = 24
        self.fat_len = ((n_clus + 2) * 4 + _SEC - 1) // _SEC
        self.heap_off = ((self.fat_off + self.fat_len + self.spc - 1) // self.spc) * self.spc
        vol_sec = self.heap_off + n_clus * self.spc
        self.img = bytearray((partitie + vol_sec) * _SEC)
        self.fat = [0] * (n_clus + 2)
        self.fat[0] = 0xFFFFFFF8
        self.fat[1] = 0xFFFFFFFF
        self.vrij = 2
        self.vol_sec = vol_sec

    def neem(self, n):
        c = self.vrij
        self.vrij += n
        return c

    def keten(self, clusters):
        for a, b in zip(clusters, clusters[1:]):
            self.fat[a] = b
        self.fat[clusters[-1]] = 0xFFFFFFFF

    def clus_off(self, c):
        return (self.part + self.heap_off + (c - 2) * self.spc) * _SEC

    def schrijf_data(self, clusters, data):
        for i, c in enumerate(clusters):
            stuk = data[i * self.cb:(i + 1) * self.cb]
            o = self.clus_off(c)
            self.img[o:o + len(stuk)] = stuk

    def bestand(self, naam, data, **kw):
        """Leg data aaneen neer en geef de entry set terug."""
        if not data:
            return ExfatBouwer.set_bestand(naam, eerste=0, lengte=0, **kw)
        n = (len(data) + self.cb - 1) // self.cb
        c = self.neem(n)
        self.schrijf_data(list(range(c, c + n)), data)
        return ExfatBouwer.set_bestand(naam, eerste=c, lengte=len(data), aaneen=True, **kw)

    @staticmethod
    def set_bestand(naam, attr=0x20, eerste=0, lengte=0, aaneen=False,
                    mtime=0, ctime=0, verwijderd=False, geldig=None, mutc=0):
        n_naam = (len(_utf16(naam)) // 2 + 14) // 15
        f = bytearray(32)
        f[0] = 0x85
        f[1] = 1 + n_naam
        struct.pack_into("<H", f, 4, attr)
        struct.pack_into("<III", f, 8, ctime, mtime, mtime)
        f[23] = mutc
        s = bytearray(32)
        s[0] = 0xC0
        s[1] = 0x01 | (0x02 if aaneen else 0)
        s[3] = len(_utf16(naam)) // 2
        struct.pack_into("<Q", s, 8, lengte if geldig is None else geldig)
        struct.pack_into("<I", s, 20, eerste)
        struct.pack_into("<Q", s, 24, lengte)
        delen = [f, s]
        u = _utf16(naam)
        for i in range(n_naam):
            e = bytearray(32)
            e[0] = 0xC1
            stuk = u[i * 30:(i + 1) * 30]
            e[2:2 + len(stuk)] = stuk
            delen.append(e)
        alles = bytearray(b"".join(delen))
        struct.pack_into("<H", alles, 2, _checksum_set(alles))
        if verwijderd:
            for i in range(0, len(alles), 32):
                alles[i] &= 0x7F
        return bytes(alles)

    def maak_map(self, entries_bytes, clusters, aaneen=False):
        data = b"".join(entries_bytes)
        assert len(data) <= len(clusters) * self.cb, "map te klein"
        if not aaneen:
            self.keten(clusters)
        self.schrijf_data(clusters, data)
        return len(clusters) * self.cb

    def afronden(self, root):
        o = self.part * _SEC
        b = bytearray(_SEC)
        b[0:3] = b"\xEB\x76\x90"
        b[3:11] = b"EXFAT   "
        struct.pack_into("<QQIIIIIIHH", b, 64, self.part, self.vol_sec,
                         self.fat_off, self.fat_len, self.heap_off,
                         self.n_clus, root, 0x1234ABCD, 0x0100, 0)
        b[108] = 9
        b[109] = self.spc_shift
        b[110] = 1
        b[112] = 7
        b[510:512] = b"\x55\xAA"
        self.img[o:o + _SEC] = b
        fo = (self.part + self.fat_off) * _SEC
        for i, v in enumerate(self.fat):
            struct.pack_into("<I", self.img, fo + 4 * i, v)
        m = bytearray(_SEC)             # MBR met één exFAT-partitie
        m[446 + 4] = 0x07
        struct.pack_into("<II", m, 446 + 8, self.part, self.vol_sec)
        m[510:512] = b"\x55\xAA"
        self.img[0:_SEC] = m
        return self.img


class NepKaart:
    """Blokapparaat zonder writeblocks; telt het aantal leesacties."""

    def __init__(self, img):
        self.img = img
        self.leesacties = 0

    def readblocks(self, nr, buf):
        self.leesacties += 1
        n = len(buf)
        buf[0:n] = self.img[nr * _SEC:nr * _SEC + n]


def _label_entry(tekst):
    e = bytearray(32)
    e[0] = 0x83
    e[1] = len(tekst)
    u = _utf16(tekst)
    e[2:2 + len(u)] = u
    return bytes(e)


def _meta_entries():
    bm = bytearray(32)
    bm[0] = 0x81
    struct.pack_into("<IQ", bm, 20, 2, 64)
    up = bytearray(32)
    up[0] = 0x82
    struct.pack_into("<IQ", up, 20, 3, 64)
    return [bytes(bm), bytes(up)]


# ---- JPEG met EXIF bouwen --------------------------------------------------


def _jpeg(w, h, progressief=False, kwaliteit=80, ruis=False):
    if ruis:
        # Ruis comprimeert slecht: zo wordt de foto groter dan de 24 KB die
        # de viewer in het RAM wil laden, net als een echte camerafoto.
        im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    else:
        im = Image.new("RGB", (w, h))
        for x in range(0, w, 8):
            for y in range(0, h, 8):
                im.putpixel((x, y), ((x * 3) & 255, (y * 5) & 255, 128))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=kwaliteit, progressive=progressief)
    return buf.getvalue()


def _exif_app1(le=True, orient=6, thumb=b"", gps=True):
    E = "<" if le else ">"

    def ent(tag, typ, cnt, val4):
        return struct.pack(E + "HHI", tag, typ, cnt) + val4

    def v_short(v):
        return struct.pack(E + "H", v) + b"\0\0"

    def v_long(v):
        return struct.pack(E + "I", v)

    make = b"Canon\0"
    model = b"Canon EOS R6\0"
    dt = b"2024:06:01 12:30:44\0"
    dto = b"2024:06:01 12:30:40\0"
    n0 = 6 if gps else 5

    def maat(n):
        return 2 + 12 * n + 4

    off_exif = 8 + maat(n0)
    off_gps = off_exif + maat(1)
    off_ifd1 = off_gps + (maat(4) if gps else 0)
    off_data = off_ifd1 + maat(3)
    data = bytearray()

    def plaats(b):
        o = off_data + len(data)
        data.extend(b)
        if len(data) % 2:
            data.append(0)
        return o

    o_make, o_model, o_dt, o_dto = plaats(make), plaats(model), plaats(dt), plaats(dto)
    o_lat = plaats(struct.pack(E + "IIIIII", 52, 1, 22, 1, 1234, 100))
    o_lon = plaats(struct.pack(E + "IIIIII", 4, 1, 53, 1, 3012, 100))
    o_thumb = plaats(thumb)
    ifd0 = [ent(0x010F, 2, len(make), v_long(o_make)),
            ent(0x0110, 2, len(model), v_long(o_model)),
            ent(0x0112, 3, 1, v_short(orient)),
            ent(0x0132, 2, len(dt), v_long(o_dt)),
            ent(0x8769, 4, 1, v_long(off_exif))]
    if gps:
        ifd0.append(ent(0x8825, 4, 1, v_long(off_gps)))
    tiff = bytearray(b"II*\0" if le else b"MM\0*") + struct.pack(E + "I", 8)
    tiff += struct.pack(E + "H", len(ifd0)) + b"".join(ifd0) + struct.pack(E + "I", off_ifd1)
    tiff += struct.pack(E + "H", 1) + ent(0x9003, 2, len(dto), v_long(o_dto)) + struct.pack(E + "I", 0)
    if gps:
        g = [ent(1, 2, 2, b"N\0\0\0"), ent(2, 5, 3, v_long(o_lat)),
             ent(3, 2, 2, b"E\0\0\0"), ent(4, 5, 3, v_long(o_lon))]
        tiff += struct.pack(E + "H", 4) + b"".join(g) + struct.pack(E + "I", 0)
    ifd1 = [ent(0x0103, 3, 1, v_short(6)), ent(0x0201, 4, 1, v_long(o_thumb)),
            ent(0x0202, 4, 1, v_long(len(thumb)))]
    tiff += struct.pack(E + "H", 3) + b"".join(ifd1) + struct.pack(E + "I", 0)
    assert len(tiff) == off_data
    tiff += data
    payload = b"Exif\0\0" + bytes(tiff)
    return b"\xFF\xE1" + struct.pack(">H", len(payload) + 2) + payload


def _camerafoto(le=True, gps=True, soi=True, grote_mini=False):
    """Camerafoto met EXIF-miniatuur. ``soi=False`` doet een Lumia na: de
    miniatuur mist zijn FF D8-marker en begint meteen met DQT.
    ``grote_mini``: een miniatuur van ~38 KB, te groot voor het RAM."""
    # Groter dan _VOL_DIRECT (300 KB), zodat de viewer eerst de miniatuur
    # toont, zoals bij een echte camerafoto.
    hoofd = _jpeg(1000, 700, ruis=True)
    mini = _jpeg(280, 210, ruis=True) if grote_mini else _jpeg(160, 120, kwaliteit=60)
    opgeslagen = mini if soi else mini[2:]
    return hoofd[:2] + _exif_app1(le=le, thumb=opgeslagen, gps=gps) + hoofd[2:], mini


# ---- de nep-kaart ----------------------------------------------------------------


def _bouw_kaart():
    """Kaart met een hoofdmap over drie (verspreide) clusters en een submap."""
    b = ExfatBouwer(n_clus=1100, spc_shift=3)
    b.neem(2)                                  # 2 = bitmap, 3 = upcase
    root = [b.neem(1)]                         # hoofdmap begint op cluster 4
    klein = bytes(range(256)) * 20             # 5120 B: > 1 cluster (4096)
    c_klein = b.neem(2)
    b.schrijf_data([c_klein, c_klein + 1], klein)
    groot = bytes((i * 7 + 3) & 0xFF for i in range(3 * 4096 - 100))
    frag = []                                  # clusters met gaten, FAT-keten
    for _ in range(3):
        frag.append(b.neem(1))
        b.neem(1)
    b.keten(frag)
    b.schrijf_data(frag, groot)
    sub_c = b.neem(1)
    tekst = "regel een\nregel twee\n".encode() * 3
    b.fotos = {}
    b.groot_bin = os.urandom(200 * 1024 + 123)
    sub_entries = [
        b.bestand("notities.txt", tekst),
        b.bestand("leeg.bin", b""),
    ]
    if Image is not None:
        b.fotos["IMG_0001.JPG"], _mini = _camerafoto()
        b.fotos["kaal.jpg"] = _jpeg(400, 300, ruis=True)     # geen EXIF
        b.fotos["WP_lumia.jpg"], b.lumia_mini = _camerafoto(le=False, soi=False)
        b.fotos["WP_groot.jpg"], b.groot_mini = _camerafoto(le=False, soi=False, grote_mini=True)
        sub_entries.append(b.bestand("IMG_0001.JPG", b.fotos["IMG_0001.JPG"]))
        sub_entries.append(b.bestand("prog.jpg", _jpeg(64, 48, progressief=True)))
        sub_entries.append(b.bestand("kaal.jpg", b.fotos["kaal.jpg"]))
        sub_entries.append(b.bestand("WP_lumia.jpg", b.fotos["WP_lumia.jpg"]))
        sub_entries.append(b.bestand("WP_groot.jpg", b.fotos["WP_groot.jpg"]))
        b.fotos["groot_kaal.jpg"] = _jpeg(1000, 700, ruis=True)   # >300 KB, geen EXIF
        sub_entries.append(b.bestand("groot_kaal.jpg", b.fotos["groot_kaal.jpg"]))
    sub_entries.append(b.bestand("groot.bin", b.groot_bin))
    b.maak_map(sub_entries, [sub_c], aaneen=True)
    root.append(b.neem(1))
    b.neem(1)
    root.append(b.neem(1))
    entries = [_label_entry("MIJNKAART")] + _meta_entries()
    entries.append(ExfatBouwer.set_bestand(
        "DCIM", attr=0x10, eerste=sub_c, lengte=b.cb, aaneen=True,
        mtime=_ts(2024, 6, 1, 12, 30, 44), mutc=0x80 | 8))
    entries.append(ExfatBouwer.set_bestand("klein.bin", eerste=c_klein, lengte=len(klein), aaneen=True))
    entries.append(ExfatBouwer.set_bestand("gefragmenteerd.dat", eerste=frag[0], lengte=len(groot)))
    entries.append(ExfatBouwer.set_bestand("weg.txt", verwijderd=True))
    entries.append(ExfatBouwer.set_bestand("Jörg-ß.txt"))
    entries.append(ExfatBouwer.set_bestand("日本語.txt"))
    entries.append(ExfatBouwer.set_bestand("\U0001F600.png"))
    entries.append(ExfatBouwer.set_bestand("x" * 15))
    entries.append(ExfatBouwer.set_bestand("een heel lange bestandsnaam die over meerdere entries loopt.jpeg"))
    entries.append(ExfatBouwer.set_bestand("VDL.bin", eerste=c_klein, lengte=1000, aaneen=True, geldig=600))
    for i in range(70):
        entries.append(ExfatBouwer.set_bestand("IMG_{:04d}_met_lange_naam.JPG".format(i)))
    b.maak_map(entries, root)
    img = b.afronden(root[0])
    return img, b, klein, groot, tekst


def _fs():
    import verkenner_exfat as ex
    img, b, klein, groot, tekst = _bouw_kaart()
    kaart = NepKaart(img)
    return ex, ex.ExFat(kaart, b.part), kaart, klein, groot, tekst


def _nep_sdcard(img):
    class NepSDCard:
        def __init__(self, **kw):
            assert kw.get("slot") == 3, "SD moet op slot=3 (SPI2)"
            self.kaart = NepKaart(img)

        def info(self):
            return (len(img), 512)

        def readblocks(self, nr, buf):
            self.kaart.readblocks(nr, buf)

        def deinit(self):
            pass

    return NepSDCard


# ---- tests: exFAT ------------------------------------------------------------


def test_exfat_hoofdmap_en_namen():
    ex, fs, _k, klein, groot, _t = _fs()
    items = list(fs.lijst(fs.root))
    namen = [it[1] for it in items]
    assert namen[0] == "DCIM"
    assert "weg.txt" not in namen, "verwijderde entry mag niet verschijnen"
    for verwacht in ("Jörg-ß.txt", "日本語.txt", "\U0001F600.png", "x" * 15,
                     "een heel lange bestandsnaam die over meerdere entries loopt.jpeg"):
        assert verwacht in namen, verwacht
    assert namen.count("IMG_0069_met_lange_naam.JPG") == 1
    assert len(namen) == 9 + 70
    assert items[0][2] & ex.ATTR_MAP
    assert fs.label() == "MIJNKAART"
    klein_it = items[namen.index("klein.bin")]
    assert klein_it[4] == len(klein) and klein_it[5] is True
    frag_it = items[namen.index("gefragmenteerd.dat")]
    assert frag_it[4] == len(groot) and frag_it[5] is False


def test_exfat_hervatten_op_positie():
    _ex, fs, _k, _kl, _gr, _t = _fs()
    items = list(fs.lijst(fs.root))
    for it in items[::7] + items[-3:]:
        eerste = next(fs.lijst(fs.root, vanaf=it[0]))
        assert eerste[1] == it[1], (it[1], eerste[1])


def test_exfat_bestanden_lezen():
    _ex, fs, _k, klein, groot, tekst = _fs()
    items = {it[1]: it for it in fs.lijst(fs.root)}
    for naam, inhoud in (("klein.bin", klein), ("gefragmenteerd.dat", groot)):
        it = items[naam]
        f = fs.open(it[3], it[5], it[4])
        assert f.read() == inhoud, naam
        for pos, n in ((0, 10), (500, 30), (4090, 20), (4096, 513), (len(inhoud) - 5, 50), (8190, 4100)):
            f.seek(pos)
            assert f.read(n) == inhoud[pos:pos + n], (naam, pos, n)
        buf = bytearray(1500)
        f.seek(100)
        assert f.readinto(buf) == 1500 and bytes(buf) == inhoud[100:1600]
    sub = items["DCIM"]
    sub_items = {it[1]: it for it in fs.lijst(sub[3], sub[5], sub[4])}
    it = sub_items["notities.txt"]
    assert fs.open(it[3], it[5], it[4]).read() == tekst
    assert fs.open(0, False, 0).read() == b""


def test_exfat_valid_data_length_leest_nullen():
    _ex, fs, _k, klein, _g, _t = _fs()
    it = [x for x in fs.lijst(fs.root, details=True) if x[1] == "VDL.bin"][0]
    geldig = it[6][-1]
    assert geldig == 600
    data = fs.open(it[3], it[5], it[4], geldig).read()
    assert data == klein[:600] + bytes(400)


def test_exfat_tijdstempel():
    ex, fs, _k, _kl, _gr, _t = _fs()
    dcim = next(fs.lijst(fs.root, details=True))
    _c, _c10, _cutc, mtime, m10, mutc, _a, _autc, _vdl = dcim[6]
    assert ex.tijdstempel(mtime, m10, mutc) == (2024, 6, 1, 12, 30, 44, 120)
    assert ex.tijdstempel(0) is None
    assert ex.tijdstempel(_ts(2023, 1, 2, 3, 4, 6), 150, 0x80 | 0x7C)[5:] == (7, -60)


def test_exfat_geen_schrijfpad():
    _ex, fs, kaart, _kl, _gr, _t = _fs()
    assert not hasattr(kaart, "writeblocks")
    list(fs.lijst(fs.root))


# ---- tests: bronnen ------------------------------------------------------------


def test_herken_kaarten():
    import verkenner_bron as vb
    img, b, *_ = _bouw_kaart()
    assert vb.herken(NepKaart(img)) == ("exfat", b.part)
    # super floppy: exFAT-bootsector direct op sector 0
    sf = bytearray(img[b.part * _SEC:])
    assert vb.herken(NepKaart(sf)) == ("exfat", 0)
    # FAT32 op een partitie
    fat = bytearray(_SEC * 80)
    fat[510:512] = b"\x55\xAA"
    fat[446 + 4] = 0x0C
    struct.pack_into("<I", fat, 446 + 8, 40)
    v = 40 * _SEC
    fat[v:v + 3] = b"\xEB\x58\x90"
    struct.pack_into("<HB", fat, v + 11, 512, 8)
    fat[v + 16] = 2
    fat[v + 510:v + 512] = b"\x55\xAA"
    assert vb.herken(NepKaart(fat)) == ("fat", 40)
    # NTFS, GPT, leeg
    ntfs = bytearray(fat)
    ntfs[v + 3:v + 11] = b"NTFS    "
    assert vb.herken(NepKaart(ntfs)) == ("ntfs", 40)
    gpt = bytearray(_SEC * 4)
    gpt[446 + 4] = 0xEE
    gpt[510:512] = b"\x55\xAA"
    assert vb.herken(NepKaart(gpt))[0] == "gpt"
    assert vb.herken(NepKaart(bytearray(_SEC * 4)))[0] == "onbekend"


def test_veilig_tekenpad():
    import verkenner_bron as vb
    assert vb.veilig_tekenpad("/sd/DCIM/100CANON/IMG_0001.JPG")
    assert vb.veilig_tekenpad("/flash/res/logo.jpg")
    assert vb.veilig_tekenpad("/system/common/img/x.png")
    assert not vb.veilig_tekenpad("/sd/backup/flash_dump.jpg"), "zou als LittleFS gelezen worden"
    assert not vb.veilig_tekenpad("/sd/system/foto.jpg")
    assert vb.veilig_tekenpad("/sd/System Volume Information/x.jpg"), "hoofdletters zijn veilig"
    assert not vb.veilig_tekenpad("/sd/" + "a" * 130 + ".jpg")
    assert not vb.veilig_tekenpad("relatief.jpg")


def test_kaart_via_nep_sdcard():
    import verkenner_bron as vb
    img, *_ = _bouw_kaart()
    sys.modules["machine"].SDCard = _nep_sdcard(img)
    k = vb.Kaart()
    assert k.open() and k.soort == "exfat"
    src = k.bron()
    assert k.label == "MIJNKAART"
    items = list(src.lijst(src.wortel()))
    assert items[0][1] == "DCIM" and items[0][2] & vb.V_MAP
    sub = list(src.lijst(src.kind(src.wortel(), items[0])))
    assert [e[1] for e in sub][:2] == ["notities.txt", "leeg.bin"]
    d = src.details(src.wortel(), items[0])
    assert d["gewijzigd"] == (2024, 6, 1, 12, 30, 44, 120)
    k.sluit()
    assert k.sd is None


def test_vfs_bron_via_ilistdir_shim():
    import verkenner_bron as vb
    with tempfile.TemporaryDirectory() as tmp:
        os.mkdir(os.path.join(tmp, "sub"))
        with open(os.path.join(tmp, "a.txt"), "wb") as f:
            f.write(b"hallo")

        def ilistdir(p):
            for e in os.scandir(p):
                yield (e.name, 0x4000 if e.is_dir() else 0x8000, 0,
                       0 if e.is_dir() else e.stat().st_size)

        os.ilistdir = ilistdir
        try:
            src = vb.VfsBron("test:", tmp)
            items = list(src.lijst(src.wortel()))
            per_naam = {e[1]: e for e in items}
            assert per_naam["sub"][2] & vb.V_MAP
            assert per_naam["a.txt"][3] == 5
            with src.open(src.wortel(), per_naam["a.txt"]) as f:
                assert f.read() == b"hallo"
            assert list(src.lijst(src.wortel(), vanaf=1)) == items[1:]
        finally:
            del os.ilistdir


# ---- tests: FAT16-venster ------------------------------------------------------


def test_fatvenster_leest_als_fatfs():
    import verkenner_fatvenster as fv
    _ex, fs, _k, klein, groot, _t = _fs()
    img, b, *_ = _bouw_kaart()
    items = {it[1]: it for it in fs.lijst(fs.root)}
    gevallen = (
        (items["klein.bin"], klein),              # aaneen, < 1 FAT-cluster
        (items["gefragmenteerd.dat"], groot),     # exFAT-keten met gaten
    )
    for it, inhoud in gevallen:
        v = fv.FatVenster(fs, it[3], it[5], it[4], "BIN")
        assert lees_fat16(v, "BEELD.BIN") == inhoud
    # Groter dan één FAT-cluster (64 KB): opnieuw bouwen met dezelfde inhoud
    # als deze kaart, want _fs() en _bouw_kaart() maken verse ruis.
    fs2 = __import__("verkenner_exfat").ExFat(NepKaart(img), b.part)
    it2 = {x[1]: x for x in fs2.lijst(fs2.root)}["DCIM"]
    g = {x[1]: x for x in fs2.lijst(it2[3], it2[5], it2[4])}["groot.bin"]
    v = fv.FatVenster(fs2, g[3], g[5], g[4], "BIN")
    assert lees_fat16(v, "BEELD.BIN") == b.groot_bin
    # 'Extended' lezen zoals de VFS-autodetectie (littlefs-magic op offset 8).
    ext = bytearray(44)
    v.readblocks(0, ext, 8)
    blk = bytearray(512)
    v.readblocks(0, blk)
    assert bytes(ext) == bytes(blk[8:52])
    try:
        v.writeblocks(0, bytearray(512))
        raise AssertionError("writeblocks mag nooit lukken")
    except OSError as e:
        assert e.args[0] == 30
    assert v.ioctl(4, 0) == v.totaal and v.ioctl(5, 0) == 512


def test_fatvenster_deel_met_voorvoegsel():
    import verkenner_fatvenster as fv
    _ex, fs, _k, klein, groot, _t = _fs()
    items = {x[1]: x for x in fs.lijst(fs.root)}
    frag = items["gefragmenteerd.dat"]
    # Ongelijk uitgelijnd, over een exFAT-clustergrens met gat, met SOI ervoor.
    for begin, deel, voor in ((4000, 5000, b"\xFF\xD8"), (513, 700, b""),
                              (0, len(groot), b""), (len(groot) - 3, 3, b"XY")):
        v = fv.FatVenster(fs, frag[3], frag[5], frag[4], "JPG", begin, deel, voor)
        assert lees_fat16(v, "BEELD.JPG") == voor + groot[begin:begin + deel], (begin, deel)
    try:
        fv.FatVenster(fs, frag[3], frag[5], frag[4], "JPG", 12000, 500)
        raise AssertionError("deel buiten het bestand moet falen")
    except ValueError:
        pass


def test_fatvenster_koppel_en_ontkoppel():
    import verkenner_fatvenster as fv
    _ex, fs, _k, klein, _g, _t = _fs()
    it = {x[1]: x for x in fs.lijst(fs.root)}["klein.bin"]
    pad = fv.koppel(fs, it[3], it[5], it[4], "JPG")
    assert pad == "/sd/BEELD.JPG" and "/sd" in _MOUNTS
    import verkenner_bron as vb
    assert vb.veilig_tekenpad(pad)
    assert lees_fat16(_MOUNTS["/sd"], "BEELD.JPG") == klein
    pad = fv.koppel(fs, it[3], it[5], it[4], "JPG")      # opnieuw: eerst los
    fv.ontkoppel()
    fv.ontkoppel()                                         # dubbel mag
    assert "/sd" not in _MOUNTS


# ---- tests: lijstvensters -------------------------------------------------------


class _NepBron:
    """Bron met n items; elk 7e is een map. Positie = index."""

    def __init__(self, n):
        self.items = []
        for i in range(n):
            is_map = i % 7 == 3
            self.items.append((i, "{}{:04d}".format("map" if is_map else "f", i),
                               1 if is_map else 0, i * 10, 0))
        self.lijst_aanroepen = 0

    def lijst(self, desc, vanaf=0):
        self.lijst_aanroepen += 1
        for e in self.items[vanaf:]:
            yield e


def _laad_app():
    """Laad apps/verkenner.py zonder de module-body (run()) uit te voeren."""
    pad = os.path.join(_BRON, "verkenner.py")
    bron = open(pad, encoding="utf-8").read()
    kop = bron.split("\ntry:\n    run()")[0]
    mod = types.ModuleType("verkenner_test")
    exec(compile(kop, pad, "exec"), mod.__dict__)
    return mod


def test_map_vensters():
    app = _laad_app()
    for n in (0, 1, 5, 31, 32, 33, 200, 1001):
        src = _NepBron(n)
        m = app.Map(src, None, "test:")
        m.scan()
        mappen = [e for e in src.items if e[2] & 1]
        bestanden = [e for e in src.items if not e[2] & 1]
        verwacht = mappen + bestanden
        assert m.aantal() == n
        # Vooruit, achteruit en springend: altijd het juiste item.
        volgorde = list(range(n)) + list(reversed(range(n))) + [(k * 37) % max(n, 1) for k in range(50)]
        for i in volgorde:
            if n:
                assert m.item(i) == verwacht[i], (n, i, m.item(i), verwacht[i])
    # Een venster verversen mag niet de hele map opnieuw lezen: vanaf het
    # dichtstbijzijnde controlepunt.
    src = _NepBron(5000)
    m = app.Map(src, None, "test:")
    m.scan()
    src.lijst_aanroepen = 0
    m.item(4000)
    assert src.lijst_aanroepen <= 2


# ---- tests: beeld ----------------------------------------------------------------


def test_passend_en_rotatie():
    import verkenner_beeld as vb
    s, x, y, w, h = vb.passend(4032, 3024, 240, 135)
    assert h == 135 and w == 180 and x == 30 and y == 0
    s, x, y, w, h = vb.passend(160, 120, 240, 135)
    assert (w, h) == (180, 135)
    s, x, y, w, h = vb.passend(32, 18, 240, 135)
    assert s == 2.0, "hooguit 2x vergroten"
    s, x, y, w, h = vb.passend(4032, 3024, 135, 240)
    assert w == 135 and y > 0


def _alleen_met_pillow(f):
    def omhulsel():
        if Image is None:
            print("      (overgeslagen: geen Pillow)")
            return
        f()
    omhulsel.__name__ = f.__name__
    return omhulsel


@_alleen_met_pillow
def test_jpeg_exif_ii_en_mm():
    import verkenner_exif as vb
    for le in (True, False):
        foto, mini = _camerafoto(le=le)
        info = vb.jpeg_info(io.BytesIO(foto), len(foto))
        assert (info["w"], info["h"]) == (1000, 700)
        assert info["sof"] == 0xC0 and not info["prog"]
        assert info["orient"] == 6
        assert info["merk"] == "Canon" and info["model"] == "Canon EOS R6"
        assert info["datum"] == "2024:06:01 12:30:40", "DateTimeOriginal gaat voor"
        lat, lon = info["gps"]
        assert abs(lat - 52.3700944) < 1e-5 and abs(lon - 4.8917) < 1e-5
        off, n, zonder_soi = info["mini"]
        assert foto[off:off + n] == mini and not zonder_soi
        assert vb.jpeg_afm(bytearray(mini)) == (160, 120)


@_alleen_met_pillow
def test_jpeg_miniatuur_zonder_soi_zoals_lumia():
    import verkenner_beeld as vb
    import verkenner_exif as vx
    foto, mini = _camerafoto(le=False, soi=False)
    f = io.BytesIO(foto)
    info = vx.jpeg_info(f, len(foto))
    off, n, zonder_soi = info["mini"]
    assert zonder_soi and foto[off:off + n] == mini[2:]
    buf = vb._laad_buffer(f, off, n, zonder_soi)
    assert bytes(buf) == mini, "FF D8 ervoor gezet: weer een geldige JPEG"
    assert vx.jpeg_afm(buf) == (160, 120)
    assert vx.mini_afm(f, info["mini"]) == (160, 120)


@_alleen_met_pillow
def test_jpeg_zonder_exif_en_progressief():
    import verkenner_exif as vb
    kaal = _jpeg(100, 50)
    info = vb.jpeg_info(io.BytesIO(kaal), len(kaal))
    assert (info["w"], info["h"], info["mini"], info["gps"]) == (100, 50, None, None)
    prog = _jpeg(100, 50, progressief=True)
    info = vb.jpeg_info(io.BytesIO(prog), len(prog))
    assert info["prog"] and info["sof"] == 0xC2
    assert vb.jpeg_info(io.BytesIO(b"geen jpeg"), 9) is None


@_alleen_met_pillow
def test_png_bmp_gif_headers():
    import verkenner_exif as vb
    for fmt, fn in (("PNG", vb.png_info), ("BMP", vb.bmp_info), ("GIF", vb.gif_info)):
        buf = io.BytesIO()
        Image.new("RGB", (123, 45)).save(buf, fmt)
        buf.seek(0)
        info = fn(buf)
        assert (info["w"], info["h"]) == (123, 45), fmt


# ---- tests: tekst en hex -------------------------------------------------------


def test_breek_regels():
    import verkenner_tekst as vt
    assert vt.breek(b"hallo\nwereld", 10) == [(0, "hallo"), (6, "wereld")]
    assert vt.breek(b"a" * 80, 10) == [(0, "a" * 39), (39, "a" * 39), (78, "aa")]
    assert vt.breek(b"\tx", 5) == [(0, "    x")]
    assert vt.breek(b"ab\r\ncd", 5) == [(0, "ab"), (4, "cd")]
    assert vt.breek("é日".encode(), 5) == [(0, "e?")]
    assert vt.breek(b"\xff\x01z", 5) == [(0, "?.z")]
    assert vt.breek(b"a" * 39 + b"\nb", 5) == [(0, "a" * 39), (40, "b")], "geen lege regel na precies vol"


def test_terugbladeren_is_spiegel_van_vooruit():
    import verkenner_tekst as vt
    stukken = []
    for i in range(60):
        stukken.append(("regel {} ".format(i) + "x" * (i * 7 % 95)).encode())
        if i % 9 == 0:
            stukken.append(b"")
    data = b"\n".join(stukken) + b"\n\ttab\n" + "slot met ümlaut".encode()
    f = io.BytesIO(data)
    tops = [0]
    while True:
        regels = vt.breek(data[tops[-1]:tops[-1] + 2048], 2)
        if len(regels) < 2:
            break
        tops.append(tops[-1] + regels[1][0])
    terug = [tops[-1]]
    while terug[-1] > 0:
        terug.append(vt.vorige_regel(f, terug[-1]))
    assert list(reversed(terug)) == tops


def test_hex_regel():
    import verkenner_tekst as vt
    assert vt.hex_regel(0x123456789, b"\x00AB\xff") == ("456789", "00 41 42 FF", ".AB.")


def test_handtekeningen():
    import verkenner_info as vt
    import verkenner_ui as ui
    jpeg = b"\xFF\xD8\xFF\xE0" + bytes(20)
    png = b"\x89PNG\r\n\x1a\n" + bytes(20)
    mp4 = b"\x00\x00\x00\x18ftypisom" + bytes(20)
    heic = b"\x00\x00\x00\x18ftypheic" + bytes(20)
    avi = b"RIFF\x00\x00\x00\x00AVI LIST"
    assert vt.oordeel("foto.jpg", jpeg)[2] == ui.GROEN
    soort, tekst, kleur = vt.oordeel("foto.jpg", png)
    assert soort == "PNG" and kleur == ui.ROOD and "AFWIJKING" in tekst
    assert vt.oordeel("clip.mp4", mp4)[:1] == ("MP4-video",)
    assert vt.oordeel("clip.MP4", mp4)[2] == ui.GROEN
    assert vt.oordeel("IMG_1.HEIC", heic)[0] == "HEIC-foto"
    assert vt.oordeel("film.avi", avi)[0] == "AVI-video"
    assert vt.oordeel("notes.txt", b"hallo daar\n")[2] == ui.GROEN
    assert vt.oordeel("data.xyz", b"hallo daar\n")[2] == ui.GEEL
    assert vt.oordeel("blob.bin", bytes(range(256)))[0] == "onbekend"


def test_ui_opmaak():
    import verkenner_ui as ui
    assert ui.grootte_kort(999) == "999"
    assert ui.grootte_kort(1500) == "1.5K"
    assert ui.grootte_kort(3 * 1024 * 1024) == "3.0M"
    assert ui.grootte_kort(64054362112) == "60G"
    assert ui.grootte_lang(3145728) == "3.145.728 bytes (3,0 MB)"
    assert ui.ascii("Jörg-ß 日") == "Jorg-ss ?"
    assert len(ui._VAN) == len(ui._NAAR)
    assert ui.ascii("àÉïõÜçÑæ") == "aEioUcNa"
    assert all(len(ui.grootte_kort(n)) <= 5 for n in (0, 1023, 10 ** 6, 10 ** 9, 10 ** 12, 2 ** 50))


# ---- rookproef: de hele app met gescripte toetsen ------------------------------------


_ALLE_MODULES = ("verkenner", "verkenner_ui", "verkenner_bron", "verkenner_exfat",
                 "verkenner_fatvenster", "verkenner_exif", "verkenner_beeld",
                 "verkenner_tekst", "verkenner_info")


def _draai_app(toetsen, kaart=None):
    img, b, *_ = kaart or _bouw_kaart()
    sys.modules["machine"].SDCard = _nep_sdcard(img)
    NepToetsenbord.rij = list(toetsen)
    for mod in _ALLE_MODULES:
        sys.modules.pop(mod, None)
    lcd = sys.modules["M5"].Lcd
    lcd.teksten.clear()
    lcd.tekeningen.clear()
    lcd.via_venster.clear()
    lcd.buffers.clear()
    # Net als de launcher: de module-body draaien (run() + opruimen). CPython's
    # import struikelt erover dat de app zichzelf uit sys.modules haalt, dus
    # exec'en we hem zelf in een geregistreerde module.
    pad = os.path.join(_BRON, "verkenner.py")
    mod_obj = types.ModuleType("verkenner")
    mod_obj.__file__ = pad
    sys.modules["verkenner"] = mod_obj
    exec(compile(open(pad, encoding="utf-8").read(), pad, "exec"), mod_obj.__dict__)
    for mod in _ALLE_MODULES:
        assert mod not in sys.modules, "{} niet opgeruimd".format(mod)
    assert NepToetsenbord.rij == [], "app stopte eerder dan verwacht: {}".format(NepToetsenbord.rij)
    assert not _MOUNTS, "mount blijft hangen: {}".format(list(_MOUNTS))
    return lcd, b


ENTER, DEL, ESC = 0x0A, 0x08, 0x1B


def test_wifi_uit_tijdens_sessie_en_daarna_terug():
    NepWlan.staat.update(actief=True, verbonden=True, log=[])
    _draai_app([None, "q"])
    assert NepWlan.staat["log"] == [("active", False), ("active", True),
                                    ("connect", "testnet")]
    # Stond WiFi al uit, dan laten we het uit.
    NepWlan.staat.update(actief=False, verbonden=False, log=[])
    _draai_app([None, "q"])
    assert NepWlan.staat["log"] == []


def test_rookproef_bladeren_tekst_hex_info():
    toetsen = [None,                      # debounce-lezing na de start
               ENTER,                     # SD-kaart openen
               ENTER,                     # DCIM in
               ENTER, "q",                # notities.txt: tekstviewer, terug
               "i", "q",                  # infopagina, terug
               ".", "h", "t", "h", "q",   # leeg.bin: hex, tekst, hex, terug
               ",",                       # terug naar de hoofdmap
               "]", "]", "e", "i", ".", "q",   # bladeren, eind, info (scrollen)
               "5", "b", "r",             # springen, begin, opnieuw lezen
               DEL,                       # terug naar bronnen
               "i", "q",                  # kaartinfo
               ".", ENTER, "q",           # flash: os.ilistdir bestaat niet onder
                                          # CPython -> nette foutmelding, weg
               "q"]                       # uit de verkenner
    lcd, _b = _draai_app(toetsen)
    alles = " ".join(t[0] for t in lcd.teksten)
    for verwacht in ("DCIM", "notities.txt", "regel een", "Grootte", "klopt"):
        assert verwacht in alles, verwacht


@_alleen_met_pillow
def test_rookproef_fotos():
    toetsen = [None, ENTER, ENTER,        # SD, DCIM
               ".", ".",                  # naar IMG_0001.JPG
               ENTER,                     # fotoviewer: miniatuur uit het RAM
               ENTER,                     # volle resolutie via het FAT-venster
               "/",                       # prog.jpg: progressief -> melding
               "/",                       # kaal.jpg: geen EXIF -> meteen vol
               "i",                       # balk uit -> opnieuw tekenen
               "/",                       # WP_lumia.jpg: miniatuur zonder SOI
               "/",                       # WP_groot.jpg: miniatuur > RAM -> venster
               "/",                       # groot_kaal.jpg: kaartje met aanbod
               ENTER,                     # ... en dan toch volledig decoderen
               "q",                       # terug naar de lijst
               "q"]                       # uit de verkenner
    lcd, b = _draai_app(toetsen)
    assert len(b.fotos["IMG_0001.JPG"]) > 300 * 1024
    jpgs = [t for t in lcd.tekeningen if t[0] == "jpg"]
    assert isinstance(jpgs[0][1], int) and jpgs[0][1] < 24 * 1024, "eerst de miniatuur, als buffer"
    alles = " ".join(t[0] for t in lcd.teksten)
    assert "MINI" in alles
    assert "Kan progressieve JPEG niet" in alles
    assert "foto laden..." in " ".join(t[0] for t in lcd.teksten)
    gelezen = [(p, d) for p, d in lcd.via_venster]
    assert [p for p, _d in gelezen] == ["/sd/BEELD.JPG"] * 5
    assert gelezen[0][1] == b.fotos["IMG_0001.JPG"], "volle foto byte-voor-byte via het venster"
    assert gelezen[1][1] == b.fotos["kaal.jpg"]
    assert gelezen[2][1] == b.fotos["kaal.jpg"]
    assert lcd.buffers[-1] == b.lumia_mini, "Lumia-miniatuur met FF D8 ervoor"
    assert len(b.groot_mini) > 24 * 1024
    assert gelezen[3][1] == b.groot_mini, "grote miniatuur als deelbestand, met FF D8"
    assert len(b.fotos["groot_kaal.jpg"]) > 300 * 1024
    assert "Geen miniatuur in deze foto." in alles, "eerst het kaartje, niet meteen decoderen"
    assert gelezen[4][1] == b.fotos["groot_kaal.jpg"], "Enter: volledig via het venster"


# ---- hoofdprogramma ----------------------------------------------------------


def _alle_tests():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    fout = 0
    for naam, f in _alle_tests():
        try:
            f()
            print("ok   ", naam)
        except Exception:
            fout += 1
            import traceback
            print("FOUT ", naam)
            traceback.print_exc()
    print("\n{} tests, {} fout".format(len(_alle_tests()), fout))
    sys.exit(1 if fout else 0)
