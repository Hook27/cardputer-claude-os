"""verkenner_bron — waar de verkenner zijn bestanden vandaan haalt.

Eén interface voor drie soorten bronnen:

- **VfsBron** — een gewoon MicroPython-bestandssysteem: ``/flash`` en
  ``/system`` (LittleFS) of een FAT-kaart op ``/sd``. Lijsten via
  ``os.ilistdir``, lezen via ``open()``. De tekenfuncties van de firmware
  kunnen hier rechtstreeks van een pad lezen.
- **FatBron** — de SD-kaart als hij FAT12/16/32 is. Wordt alleen-lezen
  gemount. Alleen voor volle-resolutie-tekenen mounten we heel even
  read-write: UIFlow's tekenfuncties openen bestanden met FA_WRITE, en een
  readonly FatFs-volume weigert dat (FR_WRITE_PROTECTED). Er wordt ook
  dan niets geschreven.
- **ExfatBron** — de SD-kaart als hij exFAT is: via onze eigen lezer
  (verkenner_exfat) op ruwe sectoren, zonder mount. Om een foto in volle
  resolutie te tekenen, zet verkenner_fatvenster heel even één bestand als
  mini-FAT16-schijf op ``/sd``, zodat de firmware hem via een pad leest.

Tekenen gaat altijd in drie stappen: ``teken_pad(map, item)`` (zonder
bijwerkingen: kan dit, en via welk pad?), ``voor_tekenen(map, item)``
(mount/hermount) en ``na_tekenen()`` (terug naar alleen-lezen / venster
weg).

Een map is voor de verkenner een ondoorzichtige *descriptor* (een pad
voor VFS, ``(cluster, aaneen, lengte)`` voor exFAT). Een item is een tuple
``(positie, naam, vlag, grootte, cluster)``. ``positie`` is het
hervat-punt voor ``lijst(map, vanaf=positie)``.

### SD-kaart op de Cardputer-Adv

Het scherm zit op SPI3. UIFlow's ``machine.SDCard`` nummert de slots op de
S3 omgekeerd (slot=2 → SPI3!), dus de kaart gaat via **slot=3** (SPI2),
met pins 40/39/14/12. Gemeten 2026-09-25.
"""

import os

V_MAP = 0x01
V_AANEEN = 0x02        # exFAT: clusters liggen aaneen (NoFatChain)
V_VERBORGEN = 0x04
V_SYSTEEM = 0x08
V_ALLEEN_LEZEN = 0x10

SD_SLOT = 3
SD_FREQ = 20000000
SD_MOUNT = "/sd"


def _u32(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def _soort_vbr(b):
    """Wat voor volume-bootsector is dit? 'exfat', 'ntfs', 'fat' of None."""
    if b[510] != 0x55 or b[511] != 0xAA:
        return None
    oem = bytes(b[3:11])
    if oem == b"EXFAT   ":
        return "exfat"
    if oem == b"NTFS    ":
        return "ntfs"
    bps = b[11] | (b[12] << 8)
    spc = b[13]
    if (b[0] in (0xEB, 0xE9) and bps in (512, 1024, 2048, 4096)
            and spc and not spc & (spc - 1) and b[16] in (1, 2)):
        return "fat"
    return None


def herken(dev):
    """Lees sector 0 (en zo nodig de partitie) -> ``(soort, startsector)``.

    ``soort``: 'exfat', 'fat', 'ntfs', 'gpt' of 'onbekend'. Werkt voor een
    kaart met MBR én voor een 'super floppy' zonder partitietabel.
    """
    b = bytearray(512)
    dev.readblocks(0, b)
    s = _soort_vbr(b)
    if s:
        return s, 0
    if b[510] != 0x55 or b[511] != 0xAA:
        return "onbekend", 0
    for i in range(4):
        o = 446 + 16 * i
        if b[o + 4] == 0xEE:
            return "gpt", 0
    for i in range(4):
        o = 446 + 16 * i
        lba = _u32(b, o + 8)
        if b[o + 4] and lba:
            v = bytearray(512)
            dev.readblocks(lba, v)
            s = _soort_vbr(v)
            if s:
                return s, lba
    return "onbekend", 0


def veilig_tekenpad(pad):
    """Mag dit pad naar M5.Lcd.drawJpg/drawBmp/drawPng?

    UIFlow's ``vfs_stream_open`` kiest het bestandssysteem op een substring
    van het hele pad: eerst "flash", dan "system", dan "sd", en anders
    LittleFS. Een SD-pad met "flash" of "system" erin wordt als LittleFS
    gelezen, en dat crasht het toestel. Paden van 128 tekens of meer ziet de
    binding als beeldbuffer in plaats van pad.
    """
    if len(pad) >= 128 or not pad.startswith("/"):
        return False
    if pad.startswith(SD_MOUNT + "/") and ("flash" in pad or "system" in pad):
        return False
    return True


def _join(map_, naam):
    return (map_ if map_ != "/" else "") + "/" + naam


def _venster_ext(naam):
    """Extensie (hoofdletters, max 3) voor de 8.3-naam in het FAT-venster."""
    p = naam.rfind(".")
    ext = naam[p + 1:p + 4].upper() if p > 0 else ""
    for c in ext:
        if c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            return "BIN"
    return ext or "BIN"


# ---- bronnen -------------------------------------------------------------


class VfsBron:
    soort = "vfs"
    kan_deel = False        # de firmware leest een pad altijd vanaf byte 0

    def __init__(self, naam, wortel):
        self.naam = naam
        self._wortel = wortel

    def wortel(self):
        return self._wortel

    def kind(self, map_, e):
        return _join(map_, e[1])

    def lijst(self, map_, vanaf=0):
        i = 0
        for t in os.ilistdir(map_):
            if i >= vanaf:
                is_map = t[1] == 0x4000
                vlag = V_MAP if is_map else 0
                if t[0].startswith("."):
                    vlag |= V_VERBORGEN
                grootte = t[3] if len(t) > 3 and not is_map else 0
                yield (i, t[0], vlag, max(0, grootte), 0)
            i += 1

    def open(self, map_, e):
        return open(_join(map_, e[1]), "rb")

    def pad(self, map_, e):
        return _join(map_, e[1])

    def teken_pad(self, map_, e):
        """Pad voor de tekenfuncties van de firmware, of None."""
        p = _join(map_, e[1])
        return p if veilig_tekenpad(p) else None

    def voor_tekenen(self, map_, e, begin=0, deel=None, voor=b""):
        pass

    def na_tekenen(self):
        pass

    def details(self, map_, e):
        import time
        st = os.stat(_join(map_, e[1]))
        t = time.localtime(st[8])
        return {"gewijzigd": tuple(t[:6]) + (None,)}

    def vrij(self):
        try:
            st = os.statvfs(self._wortel)
            return st[0] * st[3], st[0] * st[2]
        except OSError:
            return None

    def sluit(self):
        pass


class FatBron(VfsBron):
    soort = "fat"

    def __init__(self, kaart):
        VfsBron.__init__(self, "SD FAT", SD_MOUNT)
        self.kaart = kaart
        self._mount(True)

    def _mount(self, alleen_lezen):
        try:
            os.umount(SD_MOUNT)
        except OSError:
            pass
        os.mount(self.kaart.sd, SD_MOUNT, readonly=alleen_lezen)
        self.kaart.gemount = True

    def voor_tekenen(self, map_, e, begin=0, deel=None, voor=b""):
        self._mount(False)

    def na_tekenen(self):
        self._mount(True)


class ExfatBron:
    soort = "exfat"
    kan_deel = True         # het FAT-venster kan ook een stuk van een bestand tonen

    def __init__(self, kaart):
        from verkenner_exfat import ExFat
        self.kaart = kaart
        self.fs = ExFat(kaart.sd, kaart.start)
        self.naam = "SD exFAT"

    def wortel(self):
        return (self.fs.root, False, None)

    def kind(self, map_, e):
        return (e[4], bool(e[2] & V_AANEEN), e[3])

    def lijst(self, map_, vanaf=0):
        eerste, aaneen, lengte = map_
        for pos, naam, attr, c, n, an, _x in self.fs.lijst(eerste, aaneen, lengte, vanaf):
            vlag = 0
            if attr & 0x10:
                vlag |= V_MAP
            if an:
                vlag |= V_AANEEN
            if attr & 0x02:
                vlag |= V_VERBORGEN
            if attr & 0x04:
                vlag |= V_SYSTEEM
            if attr & 0x01:
                vlag |= V_ALLEEN_LEZEN
            yield (pos, naam, vlag, n, c)

    def _entry(self, map_, e):
        eerste, aaneen, lengte = map_
        for it in self.fs.lijst(eerste, aaneen, lengte, e[0], True):
            return it
        return None

    def open(self, map_, e):
        it = self._entry(map_, e)
        geldig = it[6][-1] if it else None
        return self.fs.open(e[4], bool(e[2] & V_AANEEN), e[3], geldig)

    def pad(self, map_, e):
        return None

    def teken_pad(self, map_, e):
        """Het pad waaronder verkenner_fatvenster dit bestand laat zien."""
        if e[2] & V_MAP or not e[3]:
            return None
        return SD_MOUNT + "/BEELD." + _venster_ext(e[1])

    def voor_tekenen(self, map_, e, begin=0, deel=None, voor=b""):
        import verkenner_fatvenster as fv
        fv.koppel(self.fs, e[4], bool(e[2] & V_AANEEN), e[3], _venster_ext(e[1]),
                  begin, deel, voor)

    def na_tekenen(self):
        import verkenner_fatvenster as fv
        fv.ontkoppel()

    def details(self, map_, e):
        from verkenner_exfat import tijdstempel
        it = self._entry(map_, e)
        if not it:
            return {}
        x = it[6]
        return {
            "gemaakt": tijdstempel(x[0], x[1], x[2]),
            "gewijzigd": tijdstempel(x[3], x[4], x[5]),
            "geopend": tijdstempel(x[6], 0, x[7]),
            "attr": it[2],
            "cluster": it[3],
            "aaneen": it[5],
            "geldig": x[8],
        }

    def vrij(self):
        p = self.fs.procent_gebruikt
        if p is None:
            return None
        return self.fs.grootte * (100 - p) // 100, self.fs.grootte

    def sluit(self):
        pass


# ---- de SD-sleuf ------------------------------------------------------------


class Kaart:
    """De SD-sleuf: initialiseren, herkennen, bron maken, netjes loslaten."""

    def __init__(self):
        self.sd = None
        self.soort = None
        self.start = 0
        self.grootte = 0
        self.fout = None
        self.gemount = False
        self.label = ""

    def open(self):
        self.sluit()
        self.fout = None
        self.soort = None
        try:
            import machine
            self.sd = machine.SDCard(slot=SD_SLOT, width=1, sck=40, miso=39,
                                     mosi=14, cs=12, freq=SD_FREQ)
            self.grootte = self.sd.info()[0]
        except Exception as e:
            print("verkenner: geen SD-kaart:", repr(e))
            self.fout = "geen kaart"
            self.sluit()
            return False
        try:
            self.soort, self.start = herken(self.sd)
        except Exception as e:
            print("verkenner: kaart onleesbaar:", repr(e))
            self.fout = "kaart onleesbaar"
            return False
        return True

    def bron(self):
        """Maak de juiste bron voor deze kaart (of ValueError met reden)."""
        if self.soort == "exfat":
            b = ExfatBron(self)
            self.label = b.fs.label()
            return b
        if self.soort == "fat":
            return FatBron(self)
        if self.soort == "ntfs":
            raise ValueError("NTFS wordt niet ondersteund")
        if self.soort == "gpt":
            raise ValueError("GPT-partitietabel niet ondersteund")
        raise ValueError(self.fout or "onbekend bestandssysteem")

    def sluit(self):
        if self.gemount:
            try:
                os.umount(SD_MOUNT)
            except OSError:
                pass
            self.gemount = False
        if self.sd is not None:
            try:
                self.sd.deinit()
            except Exception:
                pass
            self.sd = None
