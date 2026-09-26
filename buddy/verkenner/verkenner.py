"""verkenner — bestandsverkenner voor SD-kaart en flash op de Cardputer-Adv.

Blader door een geheugenkaartje (exFAT of FAT) of door het interne flash,
bekijk foto's, lees tekst en hex, en zie per bestand een forensische
infopagina met tijden, attributen en een handtekeningcontrole.

### Alleen lezen

De verkenner schrijft nooit iets. Een **exFAT**-kaart (vrijwel alles boven
32 GB) wordt niet eens gemount: de firmware kan dat niet, dus lezen we de
ruwe sectoren met onze eigen lezer (verkenner_exfat). Een **FAT**-kaart
mounten we alleen-lezen. Alleen tijdens het tekenen van een foto in volle
resolutie gaat hij heel even read-write, omdat de firmware bestanden met
schrijfrechten opent (zie verkenner_bron). Ook dan wordt er niets
geschreven.

### Grote mappen, klein geheugen

Er is ~62 KB RAM vrij. Een camerafolder met duizenden foto's past daar
niet in als lijst. De verkenner telt een map daarom één keer en onthoudt
elke 32e positie (``_K``). Daarna leest hij alleen het venster rond de
cursor opnieuw van de kaart. Mappen staan bovenaan; binnen mappen en
bestanden is de volgorde die van de kaart zelf (on-disk, zoals een
forensische tool hem toont). Alfabetisch sorteren zou alle namen in het
RAM vergen, en ``sort(key=str.lower)`` duurt op dit toestel 18 s per 1000
namen.

### WiFi staat uit zolang de verkenner draait

WiFi, BLE en de tekenfuncties van de firmware delen dezelfde ESP-IDF-heap,
en die is op dit toestel krap: sinds de boot was er al eens maar 1,8 KB
vrij. Een foto via het FAT-venster tekenen vraagt ~8 KB van die heap. Met
WiFi aan liep het toestel daarbij twee van de drie keer vast (hard reset);
met WiFi uit ging het elke keer goed (gemeten 2026-09-25). De verkenner
heeft geen netwerk nodig, dus hij zet WiFi uit. Bij het afsluiten gaat het
weer aan, en de verbinding komt op de achtergrond terug.

### Bediening

Bronnen (startscherm)
  ; .        kies SD-kaart / flash / systeem
  Enter      openen
  i          kaartinfo (bestandssysteem, label, serienummer, clusters)
  r          kaart opnieuw zoeken (na wisselen)
  q / ESC    terug naar het launcher-menu

Lijst
  ; .        op / neer            [ ]        bladzijde op / neer
  b e        begin / eind         0-9        naar 0-90% van de lijst
  Enter /    openen: map, foto, tekst, of anders info/hex
  , / del    map omhoog (in de hoofdmap: terug naar bronnen)
  i          infopagina           h / t      forceer hex / tekst
  r          map opnieuw lezen    q / ESC    uit de verkenner

### Port notes

- **SD-kaart:** ``machine.SDCard(slot=3, ...)``. Het LCD zit op SPI3, en
  UIFlow's slot=2 wijst juist daarheen. Zie verkenner_bron.
- **Tekenen:** ``M5.Lcd.drawJpg`` geeft altijd None terug; verkenner_beeld
  leidt het resultaat af uit headers vooraf en de tijd achteraf.
- **Geheugen:** viewers worden pas bij gebruik geïmporteerd. Bij het
  afsluiten gaan alle verkenner-modules uit ``sys.modules``, zodat de
  volgende app hun geheugen terugkrijgt.
"""

import gc
import sys
import time

import M5
from hardware import MatrixKeyboard

import verkenner_ui as ui
import verkenner_bron as bron

_LCD = ui.LCD

_K = 32                       # onthoud elke 32e map/bestandspositie
_VENSTER = 24                 # items in het geheugen rond de cursor
_RIJ_H = ui.REGEL_H           # 16 px: DejaVu9 is 15 px hoog, staarten incluis
_RIJEN = ui.INHOUD_H // _RIJ_H
_Y0 = ui.INHOUD_Y
_X_ICOON = 4
_X_NAAM = 19
_LIJST_W = ui.W - 4           # rechts 2 px ruimte + 2 px scrollbalk

_BRON_HINT = "enter open   i info   r kaart   q uit"
_LIJST_HINT = "enter open   del terug   i info   q uit"


# ---- een map als venster op de kaart ------------------------------------------


class Map:
    """Een geopende map: tellingen, controlepunten en een klein venster.

    ``scan()`` loopt de map één keer af en onthoudt elke ``_K``-de positie,
    apart voor mappen en bestanden. ``item(i)`` levert item ``i`` in de
    volgorde mappen-dan-bestanden, en haalt zo nodig een nieuw venster van
    ``_VENSTER`` items vanaf het dichtstbijzijnde controlepunt.
    """

    def __init__(self, src, desc, pad):
        self.src = src
        self.desc = desc
        self.pad = pad
        self.n_map = 0
        self.n_best = 0
        self.cp_map = []
        self.cp_best = []
        self.venster = []
        self.v0 = 0
        self.volledig = True

    def aantal(self):
        return self.n_map + self.n_best

    def scan(self, kb=None):
        n = 0
        for e in self.src.lijst(self.desc):
            if e[2] & bron.V_MAP:
                if self.n_map % _K == 0:
                    self.cp_map.append(e[0])
                self.n_map += 1
            else:
                if self.n_best % _K == 0:
                    self.cp_best.append(e[0])
                self.n_best += 1
            n += 1
            if kb is not None and n % 256 == 0:
                ui.bezig("lezen... {}   (q = stoppen)".format(n))
                if ui.toets(kb) in ("q", "esc"):
                    self.volledig = False
                    break
        return self.volledig

    def item(self, i):
        if not self.v0 <= i < self.v0 + len(self.venster):
            self._vul(max(0, i - _VENSTER // 2))
        j = i - self.v0
        return self.venster[j] if 0 <= j < len(self.venster) else None

    def vergeet(self):
        self.venster = []
        self.v0 = 0

    def _vul(self, a):
        self.venster = []
        gc.collect()
        b = min(self.aantal(), a + _VENSTER)
        items = []
        if a < self.n_map:
            items += self._haal(True, a, min(b, self.n_map) - a)
        if b > self.n_map:
            j0 = max(a, self.n_map) - self.n_map
            items += self._haal(False, j0, b - self.n_map - j0)
        self.venster = items
        self.v0 = a

    def _haal(self, is_map, j0, n):
        cps = self.cp_map if is_map else self.cp_best
        k = j0 // _K
        if n <= 0 or k >= len(cps):
            return []
        j = k * _K
        uit = []
        for e in self.src.lijst(self.desc, cps[k]):
            if bool(e[2] & bron.V_MAP) != is_map:
                continue
            if j >= j0:
                uit.append(e)
                if len(uit) >= n:
                    break
            j += 1
        return uit


# ---- lijst tekenen -------------------------------------------------------------


def _type(e):
    """-> (icoon, kleur, wat Enter doet)."""
    if e[2] & bron.V_MAP:
        return "map", ui.ORANJE, "map"
    ext = ui.extensie(e[1])
    if ext in ui.TOONBAAR:
        return "beeld", ui.BEELDKLEUR, "beeld"
    if ext in ui.BEELD_EXT:
        return "beeld", ui.BEELDKLEUR, "info"
    if ext in ui.VIDEO_EXT:
        return "video", ui.VIDEOKLEUR, "info"
    if ext in ui.TEKST_EXT:
        return "tekst", ui.CREME, "tekst"
    return "overig", ui.GRIJS, "hex"


def _icoon(soort, x, y, c):
    """Icoontje van 10×9 px. Leesbaarder dan een typelabel in tekst, en de
    extensie staat toch al in de naam."""
    if soort == "map":
        _LCD.fillRect(x, y, 4, 2, c)
        _LCD.fillRect(x, y + 2, 10, 7, c)
    elif soort == "beeld":
        _LCD.drawRect(x, y, 10, 9, c)
        _LCD.fillTriangle(x + 1, y + 7, x + 4, y + 3, x + 7, y + 7, c)
        _LCD.fillRect(x + 6, y + 2, 2, 2, c)
    elif soort == "video":
        _LCD.drawRect(x, y, 10, 9, c)
        _LCD.fillTriangle(x + 3, y + 2, x + 3, y + 6, x + 7, y + 4, c)
    elif soort == "tekst":
        _LCD.fillRect(x, y + 1, 9, 1, c)
        _LCD.fillRect(x, y + 4, 9, 1, c)
        _LCD.fillRect(x, y + 7, 6, 1, c)
    else:
        _LCD.drawRect(x + 1, y, 8, 9, c)


def _teken_rij(m, i, top, cursor):
    y = _Y0 + (i - top) * _RIJ_H
    sel = i == cursor
    _LCD.fillRect(0, y, _LIJST_W, _RIJ_H, ui.ORANJE if sel else ui.ZWART)
    e = m.item(i)
    if e is None:
        return
    soort, kleur, _wat = _type(e)
    _icoon(soort, _X_ICOON, y + 3, ui.ZWART if sel else kleur)
    ui.font_prop()
    rechts = "" if e[2] & bron.V_MAP else ui.grootte_kort(e[3])
    rw = _LCD.textWidth(rechts) if rechts else 0
    if rechts:
        ui.tekst(rechts, _LIJST_W - 3 - rw, y + 1, ui.ZWART if sel else ui.GRIJS)
    dim = e[2] & (bron.V_VERBORGEN | bron.V_SYSTEEM)
    kleur = ui.ZWART if sel else (ui.GRIJS if dim else ui.CREME)
    ruimte = _LIJST_W - 3 - rw - 6 - _X_NAAM
    ui.tekst(ui.passend(ui.ascii(e[1]), ruimte), _X_NAAM, y + 1, kleur)


def _teken_balk(n, top):
    if n <= _RIJEN:
        return
    spoor = _RIJEN * _RIJ_H
    x = ui.W - 2
    _LCD.fillRect(x, _Y0, 2, spoor, ui.DONKER)
    duim = max(6, spoor * _RIJEN // n)
    y = _Y0 + (spoor - duim) * top // (n - _RIJEN)
    _LCD.fillRect(x, y, 2, duim, ui.ORANJE)


def _kop(m, cursor):
    n = m.aantal()
    teller = "{}/{}".format(cursor + 1 if n else 0, n)
    if not m.volledig:
        teller += "+"
    ui.kop(m.pad, teller)


def _scherm(m, top, cursor):
    _kop(m, cursor)
    ui.wis_inhoud()
    n = m.aantal()
    if n == 0:
        ui.melding(["(lege map)"], ui.GRIJS)
    for r in range(min(_RIJEN, n - top)):
        _teken_rij(m, top + r, top, cursor)
    _teken_balk(n, top)
    ui.hint(_LIJST_HINT)


# ---- openen -------------------------------------------------------------------

_VIEWERS = ("verkenner_beeld", "verkenner_tekst", "verkenner_info",
            "verkenner_exif", "verkenner_fatvenster")


def _ruim_op():
    """Viewers uit het geheugen: de volgende foto heeft elke KB nodig."""
    for mod in _VIEWERS:
        sys.modules.pop(mod, None)
    gc.collect()


def _nieuwe_map(kb, src, desc, pad):
    ui.kop(pad)
    ui.melding(["lezen..."], ui.GRIJS)
    m = Map(src, desc, pad)
    try:
        m.scan(kb)
    except Exception as ex:
        print("verkenner: map lezen:", repr(ex))
        ui.melding(["Kan map niet lezen", repr(ex)], ui.ROOD)
        ui.hint("druk op een toets")
        ui.wacht_toets(kb)
        return None
    gc.collect()
    return m


def _bekijk(kb, src, m, e, modus):
    """Tekst- of hexviewer voor item e."""
    import verkenner_tekst as vt
    f = src.open(m.desc, e)
    try:
        vt.toon_bestand(kb, f, e[3], e[1], modus)
    finally:
        f.close()
        _ruim_op()


def _info(kb, src, m, e):
    import verkenner_info as vi
    try:
        modus = vi.toon_info(kb, src, m.desc, e, m.pad)
    finally:
        _ruim_op()
    if modus:
        _bekijk(kb, src, m, e, modus)


def _open(kb, src, m, i):
    """Open bestand i. -> nieuwe cursorpositie (de fotoviewer kan bladeren)."""
    e = m.item(i)
    _label, _kleur, wat = _type(e)
    try:
        if wat == "beeld":
            import verkenner_beeld as vb
            i = vb.toon(kb, src, m, i)
        elif wat == "tekst":
            _bekijk(kb, src, m, e, "tekst")
        elif wat == "hex":
            _bekijk(kb, src, m, e, "hex")
        else:
            _info(kb, src, m, e)
    except Exception as ex:
        print("verkenner: openen:", repr(ex))
        _LCD.fillScreen(ui.ZWART)
        ui.kop(m.pad + "/" + e[1])
        ui.melding(["Openen mislukt", repr(ex)], ui.ROOD)
        ui.hint("druk op een toets")
        ui.wacht_toets(kb)
    _ruim_op()
    return i


# ---- door een bron bladeren ------------------------------------------------------


def _blader(kb, src, naam):
    """Bladeren in één bron. -> 'bronnen' (terug naar start) of 'uit'."""
    m = _nieuwe_map(kb, src, src.wortel(), naam)
    if m is None:
        return "bronnen"
    stapel = []
    cursor = top = 0
    alles = True
    while True:
        if alles:
            _scherm(m, top, cursor)
            alles = False
        t = ui.toets(kb)
        if t is None:
            time.sleep_ms(30)
            continue
        n = m.aantal()
        oud = cursor
        if t == "op":
            cursor = max(0, cursor - 1)
        elif t == "neer":
            cursor = max(0, min(n - 1, cursor + 1))
        elif t == "[":
            cursor = max(0, cursor - _RIJEN)
        elif t == "]":
            cursor = max(0, min(n - 1, cursor + _RIJEN))
        elif t == "b":
            cursor = 0
        elif t == "e":
            cursor = max(0, n - 1)
        elif len(t) == 1 and "0" <= t <= "9":
            cursor = min(max(0, n - 1), n * int(t) // 10)
        elif t in ("links", "del"):
            m.vergeet()
            if not stapel:
                return "bronnen"
            m, cursor, top = stapel.pop()
            alles = True
            continue
        elif t in ("q", "esc"):
            return "uit"
        elif t == "r":
            m = _nieuwe_map(kb, src, m.desc, m.pad) or m
            cursor = min(cursor, max(0, m.aantal() - 1))
            top = min(top, cursor)
            alles = True
            continue
        elif n and t in ("rechts", "enter"):
            e = m.item(cursor)
            if e is not None and e[2] & bron.V_MAP:
                nieuw = _nieuwe_map(kb, src, src.kind(m.desc, e), m.pad + "/" + e[1])
                if nieuw is not None:
                    m.vergeet()
                    stapel.append((m, cursor, top))
                    m = nieuw
                    cursor = top = 0
            elif e is not None:
                cursor = _open(kb, src, m, cursor)
            alles = True
        elif n and t == "i":
            e = m.item(cursor)
            if e is not None:
                _info(kb, src, m, e)
            alles = True
        elif n and t in ("h", "t"):
            e = m.item(cursor)
            if e is not None and not e[2] & bron.V_MAP:
                _bekijk(kb, src, m, e, "hex" if t == "h" else "tekst")
            alles = True
        else:
            continue
        if cursor < top:
            top = cursor
            alles = True
        elif cursor >= top + _RIJEN:
            top = cursor - _RIJEN + 1
            alles = True
        if not alles and cursor != oud:
            _teken_rij(m, oud, top, cursor)
            _teken_rij(m, cursor, top, cursor)
            _kop(m, cursor)


# ---- bronnen (startscherm) --------------------------------------------------------


def _bron_items(kaart):
    """-> lijst van (titel, detail, kleur, sleutel)."""
    if kaart.sd is None:
        sd = ("SD-kaart", "geen kaart gevonden (r = opnieuw)", ui.GRIJS, None)
    elif kaart.soort in ("exfat", "fat"):
        detail = "{} - {}".format("exFAT" if kaart.soort == "exfat" else "FAT",
                                  ui.grootte_kort(kaart.grootte) + "B")
        if kaart.label:
            detail += " - " + kaart.label
        sd = ("SD-kaart", detail, ui.CREME, "sd")
    else:
        reden = {"ntfs": "NTFS: niet ondersteund", "gpt": "GPT: niet ondersteund"}
        sd = ("SD-kaart", reden.get(kaart.soort, kaart.fout or "onbekend bestandssysteem"), ui.ROOD, None)
    uit = [sd]
    for titel, pad in (("Flash", "/flash"), ("Systeem", "/system")):
        detail = pad
        try:
            import os
            st = os.statvfs(pad)
            detail = "{} - {}B vrij".format(pad, ui.grootte_kort(st[0] * st[3]))
        except Exception:
            pass
        uit.append((titel, detail, ui.CREME, pad))
    return uit


def _teken_bronnen(items, keuze):
    """Startscherm: per bron een titel en een detailregel, 32 px per bron
    (twee regels van 16 px), zodat ook hier geen staarten wegvallen."""
    ui.kop("Verkenner", "alleen lezen", ui.GROEN)
    ui.wis_inhoud()
    ui.font_prop()
    for i, (titel, detail, kleur, _s) in enumerate(items):
        y = _Y0 + i * 2 * ui.REGEL_H
        sel = i == keuze
        if sel:
            _LCD.fillRect(0, y, ui.W, 2 * ui.REGEL_H, ui.ORANJE)
        ui.tekst(titel, 8, y, ui.ZWART if sel else ui.CREME)
        ui.tekst(ui.passend(ui.ascii(detail), ui.W - 16), 8, y + ui.REGEL_H,
                 ui.ZWART if sel else kleur)
    ui.hint(_BRON_HINT)


def _kaart_info(kb, kaart, src):
    # Korte labels: de labelkolom is zo breed als het langste label, en
    # elke px daarvan gaat van de waarden af.
    regels = [("Systeem", "exFAT" if kaart.soort == "exfat" else "FAT")]
    regels.append(("Grootte", ui.grootte_mens(kaart.grootte)))
    regels.append(("Bytes", ui.duizendtallen(kaart.grootte)))
    if kaart.start:
        regels.append(("Partitie", "vanaf sector {}".format(kaart.start)))
    if kaart.soort == "exfat":
        fs = src.fs
        regels.append(("Label", kaart.label or "(geen)"))
        regels.append(("Serienr.", "{:04X}-{:04X}".format(fs.serie >> 16, fs.serie & 0xFFFF)))
        regels.append(("Cluster", ui.grootte_mens(fs.clus_bytes)))
        regels.append(("Clusters", ui.duizendtallen(fs.n_clus)))
        if fs.procent_gebruikt is not None:
            regels.append(("In gebruik", "{}%".format(fs.procent_gebruikt)))
    else:
        v = src.vrij()
        if v:
            regels.append(("Vrij", ui.grootte_mens(v[0])))
    ui.regels_scherm(kb, "SD-kaart", [(l, w, ui.CREME) for l, w in regels])


def _wifi_uit():
    """WiFi uit zolang we draaien. -> (was actief, was verbonden)."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        toestand = (sta.active(), sta.isconnected())
        if toestand[0]:
            sta.active(False)
        return toestand
    except Exception as e:
        print("verkenner: wifi uit:", repr(e))
        return (False, False)


def _wifi_terug(toestand):
    """WiFi terug zoals het was. Niet wachten op de verbinding: die komt op
    de achtergrond, en apps die hem nodig hebben verbinden zelf opnieuw."""
    actief, verbonden = toestand
    if not actief:
        return
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        if verbonden:
            import wifi_event
            sta.connect(wifi_event.SSID, wifi_event.PASSWORD)
    except Exception as e:
        print("verkenner: wifi terug:", repr(e))


def _maak_bron(kb, kaart, sleutel, sd_bron):
    """-> (bron, naam) of (None, None) na een foutmelding."""
    if sleutel != "sd":
        naam = "flash:" if sleutel == "/flash" else "systeem:"
        return bron.VfsBron(naam, sleutel), naam
    if sd_bron is not None:
        return sd_bron, "SD:"
    try:
        ui.bezig("kaart openen...")
        return kaart.bron(), "SD:"
    except Exception as ex:
        print("verkenner: kaart:", repr(ex))
        ui.melding(["Kaart openen mislukt", str(ex)], ui.ROOD)
        ui.hint("druk op een toets")
        ui.wacht_toets(kb)
        return None, None


def run():
    ui.font_prop()
    _LCD.fillScreen(ui.ZWART)
    ui.kop("Verkenner")
    ui.melding(["kaart zoeken..."], ui.GRIJS)
    kb = MatrixKeyboard()
    wifi = _wifi_uit()
    # De Enter waarmee de launcher ons startte niet als invoer tellen.
    time.sleep_ms(400)
    ui.toets(kb)
    kaart = bron.Kaart()
    kaart.open()
    sd_bron = None
    keuze = 0
    try:
        while True:
            items = _bron_items(kaart)
            _teken_bronnen(items, keuze)
            t = ui.wacht_toets(kb)
            if t in ("q", "esc"):
                return
            if t == "op":
                keuze = (keuze - 1) % len(items)
            elif t == "neer":
                keuze = (keuze + 1) % len(items)
            elif t == "r":
                sd_bron = None
                ui.bezig("kaart zoeken...")
                kaart.open()
            elif t == "i" and items[keuze][3] == "sd":
                sd_bron, _n = _maak_bron(kb, kaart, "sd", sd_bron)
                if sd_bron is not None:
                    _kaart_info(kb, kaart, sd_bron)
            elif t in ("enter", "rechts") and items[keuze][3]:
                src, naam = _maak_bron(kb, kaart, items[keuze][3], sd_bron)
                if src is None:
                    continue
                if items[keuze][3] == "sd":
                    sd_bron = src
                if _blader(kb, src, naam) == "uit":
                    return
            gc.collect()
    finally:
        kaart.sluit()
        _wifi_terug(wifi)


# Draaien en daarna opruimen: kaart los (in run), scherm leeg, en alle
# verkenner-modules uit sys.modules zodat de launcher ons opnieuw kan
# importeren en de volgende app het geheugen terugkrijgt.
try:
    run()
finally:
    try:
        M5.Lcd.fillScreen(0x000000)
    except Exception as e:
        print("verkenner: scherm wissen warning:", e)
    for _mod in ("verkenner_beeld", "verkenner_tekst", "verkenner_info",
                 "verkenner_exif", "verkenner_fatvenster", "verkenner_exfat",
                 "verkenner_bron", "verkenner_ui"):
        sys.modules.pop(_mod, None)
    sys.modules.pop(__name__, None)
