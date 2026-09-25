# Claude-verbruik op de Nest Hub

Toont je Claude-limieten (5-uursvenster en weekvenster) groot op een Google
Nest Hub, maar alleen zolang je er thuis mee bezig bent. De rest van de tijd
blijft de Hub gewoon zijn fotolijst.

## Hoe het in elkaar zit

```
Pi (TaSc-Pi5)                                            Nest Hub (LAN)
─────────────────────────────────────────                ──────────────
claude-verbruik  :8091/verbruik   (ongewijzigd)
      ▲
      │ elke minuut
nest-hub-bewaker :8092
  ├─ /scherm    ◀───────────────────────────────────────  haalt de pagina zelf op
  │                                                       (LAN-adres, force-DashCast)
  ├─ /          ◀── telefoon of laptop: twee knoppen
  ├─ tailscale status  ── is de laptop thuis?
  └─ pychromecast ── "open /scherm" of "stop" ─────────▶
```

De verbruikserver blijft onaangeroerd; de widget en de Cardputer merken hier
niets van. De Hub zit niet in Tailscale, dus hij haalt de pagina op via het
**LAN-adres** van de Pi. De bewaker zoekt dat adres zelf op.

## Hoe het beslist

| situatie | wat er gebeurt |
|---|---|
| je bent thuis en het 5-uurs- of weekpercentage stijgt | scherm op de Hub |
| 20 minuten geen stijging | terug naar de fotolijst |
| de Hub speelt muziek of een video | niets; de automaat stoort niet |
| knop **Toon op de Hub** | meteen tonen, ook over muziek heen en ook als de thuis-check "nee" zegt |
| knop **Terug naar fotolijst** | meteen terug; de automaat wacht tot er 20 minuten geen stijging is geweest |
| de cijfers zijn niet vers (`gemeten`, `geen-token`, ...) | niets; de bewaker ziet dan geen verbruik |
| na een herstart staat het scherm er nog | overgenomen; zonder verbruik na 20 minuten weg |

De Hub laat een cast zelf niet vallen (getest: na 40 minuten stond hij er nog).
Terug naar de fotolijst is dus helemaal het werk van de bewaker; die 20 minuten
zijn zijn instelling (`--stilte`, in minuten), gerekend vanaf de laatste
stijging.

**Waarom zo grof.** De percentages zijn hele getallen, en de verbruikserver
haalt ze hoogstens elke 5 minuten op. Tijdens gebruik steeg het 5-uurscijfer
in de eigen historie (september 2026) vrijwel elk kwartier, meestal met 2 à 5%
of meer. Het scherm springt dus aan binnen een paar minuten nadat je echt aan
het werk bent. Een kort vraagje dat onder de 1% blijft, zie je niet; daarvoor
is de knop.

**Waarom "thuis".** Het verbruik geldt voor je hele account, dus het stijgt ook
als je buitenshuis werkt. De bewaker kijkt daarom in `tailscale status` of het
opgegeven apparaat (de laptop) een *direct* pad naar de Pi heeft via het
thuisnetwerk: hetzelfde IPv4-subnet of hetzelfde IPv6-prefix als de Pi zelf.
Dat pad is alleen zichtbaar zolang er verkeer is (ruwweg een minuut), dus meestal
is het leeg, ook als de laptop gewoon thuis staat. Zolang er verbruik op "thuis"
wacht, laat de bewaker Tailscale het pad daarom vaststellen met `tailscale ping`
(tot 5 pings: na een stilte gaat de eerste pong vrijwel altijd via een relay).
Een stijging blijft 20 minuten geldig, dus lukt dat pas een ronde later, dan
springt het scherm dan alsnog aan.

Dat de eerste versie maar één keer pingde, was de reden dat het scherm op
2026-09-25 niet aansprong terwijl Jörg gewoon thuis zat: "geen direct pad".

## Bestanden

| bestand | rol |
|---|---|
| `nest_hub_bewaker.py` | de dienst: rondes, thuis-check, Hub aansturen, HTTP op :8092 |
| `scherm.html` | statuspagina voor de Hub, 1024×600, leesbaar op 3 m |
| `bediening.html` | knoppen en status, voor telefoon of laptop |
| `test-nest-hub-bewaker.py` | offline test van alle regels, met een nagebootste Hub |
| `nest-hub.env.voorbeeld` | voorbeeld van je instellingen (het echte bestand staat niet in git) |
| `systemd/nest-hub-bewaker.service` | user-unit |

## Installatie op de Pi

Vanaf de laptop de map naar de Pi kopiëren (PowerShell):

```
scp -r "<pad naar de repo>\pi\nest-hub" <gebruiker>@<pi>:~/
```

Op de Pi:

```bash
python3 -m venv ~/nest-hub/venv
~/nest-hub/venv/bin/pip install "pychromecast>=14,<15"
~/nest-hub/venv/bin/python ~/nest-hub/test-nest-hub-bewaker.py     # alles OK?
cp ~/nest-hub/nest-hub.env.voorbeeld ~/nest-hub/nest-hub.env        # en invullen
```

Eerst één keer meten, zonder iets te casten:

```bash
~/nest-hub/venv/bin/python ~/nest-hub/nest_hub_bewaker.py --hub <ip> --hub-naam "<naam>" --thuis-apparaat <laptop> --eenmalig
```

Verwacht: verbruik met `bron live`, thuis `ja`, en de Hub op `fotolijst`. Staat
de Hub op iets anders terwijl hij de fotolijst toont, dan herkent de bewaker
die toestand niet — zie hieronder.

Daarna de dienst:

```bash
mkdir -p ~/.config/systemd/user
cp ~/nest-hub/systemd/nest-hub-bewaker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nest-hub-bewaker
journalctl --user -u nest-hub-bewaker -f
```

### Bediening

Thuis: `http://<LAN-adres van de Pi>:<poort>/`. De poort is 8092, of wat je in
`POORT=` hebt gezet; op TaSc-Pi5 is dat **8093**, omdat 8092 daar al bezet is
door een andere dienst (alleen op 127.0.0.1).

Op je telefoon via Tailscale werkt `http://<pi>.<tailnet>.ts.net:<poort>/` vaak
níet: de browser weigert gewone http voor die naam ("verbinding niet
beveiligd"). Het verkeer ís via Tailscale versleuteld, maar dat ziet de browser
niet. Laat Tailscale er daarom HTTPS voor zetten, met een echt certificaat voor
de ts.net-naam (HTTPS-certificaten moeten aanstaan in de Tailscale-beheeromgeving):

```bash
tailscale serve status                                         # wat staat er al?
sudo tailscale serve --https=8443 --bg http://127.0.0.1:<poort>
```

Daarna: `https://<pi>.<tailnet>.ts.net:8443/`, met slotje — zo in gebruik sinds
2026-09-25. Serve houdt per poort een eigen regel bij: een bestaande regel op
443 (op TaSc-Pi5 een ander dashboard) bleef gewoon staan, en `tailscale serve
status` toont daarna beide. **Vergeet `--https=8443` niet**: zonder die optie
gaat het commando over poort 443 en overschrijft het wat daar staat.
Terugdraaien, alleen voor deze poort: `sudo tailscale serve --https=8443 off`.

Zonder serve kan het ook via het Tailscale-IP van de Pi,
`http://100.x.y.z:<poort>/`: geen slotje, en mogelijk eerst een waarschuwing
met "Doorgaan".

## Lokaal proberen, zonder Hub

Op de laptop, twee vensters:

```
python ..\verbruik\fake_verbruik_server.py --adres 127.0.0.1 --poort 18091
python nest_hub_bewaker.py --nep-hub --altijd-thuis --adres 127.0.0.1 --poort 18092 --verbruik http://127.0.0.1:18091/verbruik --interval 10
```

Open `http://127.0.0.1:18092/scherm` (zet het browservenster op 1024×600) en
`http://127.0.0.1:18092/` voor de knoppen. Het scenario `oplopend` laat de
cijfers stijgen, dus na één ronde "toont" de nep-Hub het scherm.

## Problemen

**`OSError: [Errno 98] Address already in use`.** Poort 8092 is op de Pi al
bezet. Kijk met `pgrep -af nest_hub_bewaker` of er nog een met de hand gestarte
bewaker draait — twee bewakers tegelijk vechten om de Hub, dus stop die dan.
Is het een andere dienst (`sudo ss -ltnp` laat zien welke), zet dan
`POORT=<vrije poort>` in `nest-hub.env` en herstart de dienst. Na vijf mislukte
starts binnen vijf minuten geeft systemd het op; `systemctl --user reset-failed
nest-hub-bewaker` maakt de teller weer leeg.

**Hub onbereikbaar.** Het adres kan veranderd zijn. De bewaker zoekt de Hub
dan zelf terug via mDNS (op uuid, of op `HUB_NAAM`) en logt het nieuwe adres;
zet dat daarna in `nest-hub.env`. Zoeken op naam bleek bij `catt` wisselvallig
(hetzelfde commando faalde en werkte daarna), vandaar het IP-adres als eerste
keus.

**Thuis blijft "nee" of "onbekend".** Draai `tailscale status` als dezelfde
gebruiker. Staat de laptop er als `active; direct 192.168.x.x:41641`, dan hoort
de check "ja" te zeggen; controleer dan de naam in `THUIS_APPARAAT`. Meldt de
journal dat `tailscale ping` niet mag, dan kan een beheerder dat toestaan met
`sudo tailscale set --operator=$USER` — niet nodig zolang de widget draait.

**De Hub meldt op de fotolijst een onbekende app.** De bewaker telt een lege
`app_id` en Backdrop (`E8C28D3C`) als fotolijst. Meldt jouw Hub iets anders,
dan staat dat in de journal (`Hub: anders (app_id=..., naam=...)`) en schakelt
de automaat nooit over. Voeg die app_id dan toe in `duid_app()`.

**Telefoon: "verbinding niet beveiligd".** Zie *Bediening* hierboven:
`tailscale serve --https=8443` geeft de pagina een echt certificaat.

**Een eigen `catt cast_site` verdwijnt.** Klopt: dat is ook DashCast, en de
bewaker haalt DashCast weg als hij het scherm niet wil.

## Wat de Hub meldt

Gemeten met `--eenmalig` op 2026-09-25, Nest Hub 1e generatie (model H1A):

| toestand | app_id | naam |
|---|---|---|
| fotolijst | `E8C28D3C` | Backdrop |
| ons scherm | `84912283` | DashCast |

Staat er bij een andere Hub iets anders bij de fotolijst, vul het dan hier aan
(en in `duid_app()`).
