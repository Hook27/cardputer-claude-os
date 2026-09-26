# Verkenner — bestanden en foto's op de Cardputer

Een bestandsverkenner voor de Cardputer-Adv. Je bladert door een
geheugenkaartje (exFAT of FAT) of door het interne flash, bekijkt foto's,
leest tekst en hex, en krijgt per bestand een forensische infopagina.

In het launcher-menu heet hij **verkenner**.

## Wat hij doet

- **Bladeren** door de SD-kaart, `/flash` en `/system`. Mappen staan
  bovenaan, daarna de bestanden in de volgorde waarin ze op de kaart staan
  (on-disk, zoals een forensische tool). Per bestand zie je een typelabel,
  de grootte en verborgen/systeembestanden gedimd. Ook mappen met duizenden
  foto's werken; een map van 1129 foto's is in ~1,5 s geteld.
- **Foto's**: JPEG (baseline) en BMP, passend op het scherm, met
  vorige/volgende. Staat er een EXIF-miniatuur in, dan zie je die meteen
  (~0,1 s); **Enter** decodeert de echte foto. Zonder miniatuur krijg je een
  kaartje met afmetingen, camera en datum, en decodeert Enter de hele foto
  (~3–3,5 s per MB; een 16 MP-foto van 4 MB ~13 s).
- **Tekst** (txt, log, csv, json, py, ...) en **hex** voor al het andere,
  ook voor bestanden van megabytes.
- **Infopagina** (`i`): exacte grootte, tijden (gemaakt/gewijzigd/geopend,
  met UTC-afwijking), attributen, cluster en fragmentatie, en een
  **handtekeningcontrole**: de eerste bytes tegen de extensie. Een `.jpg`
  die eigenlijk een ZIP is, staat er in rood. Bij foto's komt de EXIF erbij
  (camera, opnamedatum, GPS); bij video de melding dat afspelen op de
  Cardputer niet kan.
- **Kaartinfo** (`i` op het startscherm): bestandssysteem, capaciteit,
  label, serienummer, clustergrootte, percentage in gebruik.

## Alleen lezen

De verkenner schrijft nooit. Een **exFAT**-kaart wordt niet eens gemount:
de firmware kan dat niet, dus leest `verkenner_exfat` de ruwe sectoren
zelf. Een **FAT**-kaart wordt alleen-lezen gemount. Alleen tijdens het
tekenen van een foto in volle resolutie gaat hij heel even read-write, want
de firmware opent bestanden met schrijfrechten. Ook dan wordt er niets
geschreven.

## Bediening

| Waar | Toets | Doet |
|---|---|---|
| overal | `;` `.` | op / neer |
| lijst | Enter of `/` | openen (map, foto, tekst, anders info/hex) |
| lijst | `,` of del | map omhoog; in de hoofdmap terug naar het startscherm |
| lijst | `[` `]` | bladzijde op / neer |
| lijst | `b` `e` / `0`–`9` | begin / eind / naar 0–90% |
| lijst | `i` / `h` / `t` | infopagina / hex / tekst |
| lijst | `r` | map opnieuw lezen |
| startscherm | `r` | kaart opnieuw zoeken (na wisselen) |
| foto | `,` `/` | vorige / volgende foto |
| foto | Enter | volle resolutie |
| foto | `i` | infobalk aan/uit |
| overal | `q` of ESC | terug (in de lijst: uit de verkenner) |

## Wat niet kan, en waarom

- **Video afspelen**: bewust niet gebouwd. De verkenner herkent video en
  zegt dat hij niet af te spelen is.
- **PNG**: de PNG-decoder van de firmware wil ~43 KB aaneengesloten
  geheugen, en met WiFi + BLE is het grootste vrije blok ~31 KB.
- **Progressieve JPEG en HEIC**: de decoder van de firmware kan alleen
  baseline JPEG.
- **NTFS en GPT-kaarten**: niet ondersteund; de verkenner zegt dat.

## Installeren

De bronnen staan in `buddy/verkenner/` en **niet** in `buddy/device/`.
`install_apps.py` zet elke `.py` uit `buddy/device` op het toestel, en een
`.py` gaat bij importeren vóór een `.mpy`. De verkenner compileert op het
toestel niet (MemoryError), dus hij wordt voorgecompileerd:

```bash
python -m pip install --user mpy-cross==1.27.0.post2
python buddy/scripts/push_verkenner.py --port COM5
```

Het script compileert met `-march=xtensawin`, uploadt de `.mpy`'s, haalt
oude `.py`-versies weg, controleert alles met SHA-256 en reset het
toestel. Hardware Buddy los en de Cardputer op het launcher-menu, anders is
de poort bezet.

Tests (op de pc, geen toestel nodig):

```bash
python buddy/tests/test_verkenner_logic.py
```

## Hoe het werkt (voor wie eraan gaat sleutelen)

| Module | Wat |
|---|---|
| `verkenner.py` | de app: startscherm, lijst, navigatie |
| `verkenner_ui.py` | palet, kop/hint, toetsen, en de vaste werkbuffer |
| `verkenner_bron.py` | bronnen (flash, FAT, exFAT) en kaartherkenning |
| `verkenner_exfat.py` | alleen-lezen exFAT-lezer op ruwe sectoren |
| `verkenner_fatvenster.py` | toont één exFAT-bestand als mini-FAT16-schijf |
| `verkenner_exif.py` | JPEG/PNG/BMP/GIF-headers en EXIF |
| `verkenner_beeld.py` | fotoviewer |
| `verkenner_tekst.py` | tekst- en hexviewer |
| `verkenner_info.py` | infopagina en handtekeningen |

De belangrijkste lessen, allemaal gemeten op het toestel (september 2026):

- **SD-kaart op `slot=3`**. Het scherm zit op SPI3, en UIFlow nummert de
  slots omgekeerd: `slot=2` is de LCD-bus.
- **Grote mappen**: nooit de hele lijst in het RAM. De map wordt één keer
  geteld; elke 32e positie wordt onthouden, en alleen het venster rond de
  cursor wordt opnieuw van de kaart gelezen.
- **Foto's van exFAT in volle resolutie**: de firmware kan een foto alleen
  via een pad op FAT of LittleFS lezen. `verkenner_fatvenster` verzint
  daarom een FAT16-schijfje met precies één bestand erop, en vertaalt elke
  sector die FatFs opvraagt naar de juiste sector op de exFAT-kaart. Met
  16–24 sectoren vooruit lezen gaat dat ~5× sneller dan sector voor sector.
- **Lumia-miniaturen**: de Lumia laat bij de EXIF-miniatuur de SOI-marker
  (`FF D8`) weg; de viewer zet die er zelf voor.
- **Leesbaarheid**: DejaVu9 is 15 px hoog en proportioneel; het
  "ASCII7"-font meet op deze firmware precies hetzelfde, dus een klein
  monospace-font bestaat niet. Alle regels staan 16 px uit elkaar, en de
  hexviewer zet elke byte in een vaste cel. De firmware tekent tekst altijd
  met een achtergrondvakje; daardoor wiste een `j` de `i` ervoor. Alle tekst
  loopt daarom via `ui.tekst()`, die met gelijke voor- en achtergrondkleur
  doorzichtig tekent.
- **Geheugen**: ~62 KB vrij. De modules zijn voorgecompileerd, viewers
  worden na gebruik uit het geheugen gehaald, en grote buffers komen uit
  één vaste werkbuffer van 12 KB (na wat bladeren is er vaak geen groot blok
  meer aaneen vrij).
- **WiFi staat uit** zolang de verkenner draait, en gaat daarna weer aan.
  De tekenfuncties van de firmware en WiFi delen een krappe heap. Met WiFi
  aan viel het toestel twee van de drie keer om bij het openen van een foto;
  met WiFi uit nooit. `claude_buddy` zet WiFi om dezelfde soort reden uit.
