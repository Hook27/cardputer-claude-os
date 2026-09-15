# Claude verbruik op de Cardputer

Toont je Claude-limieten (5-uursvenster en weekvenster) op de
Cardputer-Adv, als balk + percentage met een lopende aftelklok.

## Waarom een aparte app en niet in Claude Buddy

`claude_buddy` krijgt zijn data van Claude Desktop's Hardware Buddy over
BLE. Die heartbeat bevat queue- en tokencijfers, maar geen limieten, en
het is een dichte app — we kunnen er geen velden aan toevoegen. Bovendien
zet `claude_buddy` **WiFi bewust uit** voordat het BLE aanzet (ESP32 deelt
één radio; dat is de fix die de app stabiel maakt). Verbruik ophalen heeft
juist WiFi nodig. Die twee horen dus niet in één app.

Daarom: een losse app die WiFi gebruikt en geen BLE, precies zoals
`pi_dashboard`.

## Hoe de data bij het toestel komt

```
Pi (heeft de Claude Code-login)          Cardputer (LAN)
────────────────────────────────         ─────────────────
token -> GET /api/oauth/usage
  cache 5 min (+ backoff bij 429)
  ▼
:8091/verbruik  ◀──────────────────────  elke 45 s: requests.get(...)
  {"five_hour":42, ...}                    -> balken + aftelklok
```

Het toestel draagt **geen token** en praat niet met api.anthropic.com; het
ziet alleen kale percentages. Zo blijft het accounttoken op één machine —
zie ook de waarschuwing in `LEESMIJ.txt` van de laptop-widget: kopieer
nooit credentials tussen machines, laat elke machine zelf inloggen.

De server rekent de resettijd om naar **seconden vanaf nu**
(`fh_reset_in_s`), zodat het toestel geen ISO-datums hoeft te parseren en
de klok van de Cardputer er niet toe doet.

### Antwoordvorm

```json
{"five_hour": 42.0, "seven_day": 18.0,
 "fh_reset_in_s": 4680, "sd_reset_in_s": 275000,
 "bron": "live", "ts": 1785261682}
```

`bron` zegt hoe vers de cijfers zijn, en waarom niet:

| waarde | betekenis | actie |
|---|---|---|
| `live` | vers van de API | — |
| `gemeten` | ophalen mislukt; dit zijn de laatst bekende cijfers | afwachten |
| `geweigerd` | er ís een token, maar de API accepteert het niet (401/403) | **`claude auth login` op de Pi** |
| `geen-token` | geen bruikbaar credentialsbestand | inloggen |
| `nep` | de offline testserver | — |

Het toestel toont dit in de kop, zodat je oude cijfers nooit voor verse
aanziet. `geweigerd` en `geen-token` staan er bewust apart in: een token met
een expiry ver in de toekomst kan tóch geweigerd worden, en dan helpt
afwachten niet.

## Fase 0 — offline testen (geen token nodig)

`fake_verbruik_server.py` serveert dezelfde JSON met verzonnen cijfers. Het
leest geen credentials en heeft geen netwerk-uitgang, dus je kunt de
weergave volledig testen zonder je echte token aan te raken.

```
python fake_verbruik_server.py --lijst              # scenario's tonen
python fake_verbruik_server.py --scenario oplopend  # balken lopen op
python fake_verbruik_server.py --scenario kritiek   # rood
python fake_verbruik_server.py --scenario stuk      # HTTP 500
```

Zet `VERBRUIK_ENDPOINT` in `buddy/device/apps/config.py` op het adres dat
de server bij het starten toont, push `claude_verbruik.py` + `config.py`
naar het toestel en open de app.

Op Windows moet poort 8091 inkomend open staan (Wi-Fi-profiel is vaak
"Public", dat blokkeert standaard). Beperk de regel tot je eigen subnet:

```
New-NetFirewallRule -DisplayName "Claude verbruik 8091" -Direction Inbound `
  -Protocol TCP -LocalPort 8091 -Action Allow -Profile Any -RemoteAddress LocalSubnet
```

## Fase 1 — de echte server op de Pi (TaSc-Pi5)

De Pi krijgt een **eigen** Claude Code-login. De limieten zijn accountbreed,
dus de cijfers zijn dezelfde als op de laptop. Kopieer nooit credentials
tussen machines — laat elke machine zelf inloggen.

Bestanden:

| bestand | rol |
|---|---|
| `claude_verbruik_server.py` | haalt op + serveert `:8091/verbruik` |
| `ververs-claude-token.py` | houdt het token geldig (Linux-port van de `.ps1`) |
| `test-ververs-claude-token.py` | offline test van de refresh, met nep-credentials |
| `test-verbruik-server.py` | offline test van cache + rate-limit-backoff |
| `systemd/*.service`, `*.timer` | user-units voor beide |

### Rate limiting

Het usage-endpoint heeft een eigen request-limiet. Op 2026-08-03 liep de
server daar tegenaan (HTTP 429) en bleef hij in hetzelfde tempo doorvragen,
waardoor toestel en widget een halve dag `gemeten` toonden in plaats van
`live`. Sindsdien:

- de cache staat op **5 minuten** (~288 in plaats van ~960 verzoeken per dag);
  voor een venster van 5 uur is dat ruim vers genoeg, en de Cardputer merkt er
  niets van omdat die de cache leest;
- bij een **429 wacht de server** — zo lang als `Retry-After` aangeeft, en
  anders oplopend van 5 minuten tot maximaal een uur, met herstel na de eerste
  geslaagde poging;
- andere fouten (netwerk, 5xx) pauzeren níet: die komen niet door ons tempo.

Zie je in de journal `rate limit (429); volgende poging over N min`, dan werkt
dit zoals bedoeld. Blijft dat uren aanhouden, dan vraagt iets anders ook met
dit token — controleer of er niet nog een tweede verbruikserver of widget op de
API zelf pollt.

### Stap 0 — kan Claude Code hier draaien?

Eerst vaststellen, niet aannemen (de Pi is ARM64):

```bash
uname -m && python3 --version && (command -v claude && claude --version || echo "claude nog niet geinstalleerd")
```

### Stap 1 — Claude Code + login

ARM64 wordt officieel ondersteund (eis: 4 GB+ RAM, Debian 10+). Gebruik op een
always-on Pi de **apt-repository** en niet de curl-installer: die laatste
werkt met een achtergrond-auto-updater, terwijl apt meegaat met je normale
`apt upgrade` — voor een machine waar een unattended timer op draait wil je
niet dat de CLI-versie 's nachts verschuift. Het `stable`-kanaal loopt bewust
ongeveer een week achter en slaat releases met grote regressies over.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc
gpg --show-keys /etc/apt/keyrings/claude-code.asc
```

Controleer dat de fingerprint exact `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF
1A7E CACE` is vóór je verder gaat. Daarna:

```bash
echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" | sudo tee /etc/apt/sources.list.d/claude-code.list
sudo apt update && sudo apt install claude-code
claude --version && claude doctor
```

Log daarna eenmalig in (de enige interactieve stap):

```bash
claude
```

Bijwerken gaat later met `sudo apt update && sudo apt upgrade claude-code`.
Claude Code meldt soms een update vóór die in de repository staat — dat is een
bekend gedrag van het package-manager-pad, geen fout.

Controleer daarna dat het credentialsbestand er is:

```bash
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude/.credentials.json')));o=d['claudeAiOauth'];print('token aanwezig:',bool(o.get('accessToken')),'| verloopt:',o.get('expiresAt'))"
```

### Stap 2 — bestanden neerzetten

```bash
mkdir -p ~/claude-verbruik && cd ~/claude-verbruik
BASE=https://raw.githubusercontent.com/Hook27/cardputer-claude-os/main/pi/verbruik
for f in claude_verbruik_server.py ververs-claude-token.py \
         test-ververs-claude-token.py test-verbruik-server.py; do
  curl -fsSL "$BASE/$f" -o "$f"
done
```

Ditzelfde blok haalt later ook updates op; alleen het opnieuw starten van de
service is dan nog nodig.

### Stap 3 — eerst testen, dan pas aanzetten

Beide tests raken je echte token niet aan (nep-credentials, nep-CLI,
nep-server, geen netwerk) en lokken juist de gevaarlijke paden uit:
wachtschema na mislukkingen, een refresh die te traag terugkomt, leeggemaakte
login, verlopen refresh-token, rate-limit-backoff, een nieuwe login tijdens een
wachttijd en een server die niet reageert. Draai ze **hier op de Pi**, niet op
Windows: subprocess-gedrag verschilt, en dat heeft het refresh-script al eens
lamgelegd terwijl de suite op Windows slaagde.

```bash
python3 ~/claude-verbruik/test-ververs-claude-token.py
python3 ~/claude-verbruik/test-verbruik-server.py
```

Verwacht: `41 goed, 0 fout` en `21 goed, 0 fout`. De eerste duurt ruim een
halve minuut, omdat de trage-refreshtest echt acht seconden wacht. Daarna één echte,
ongevaarlijke controle —
`--dry-run` bepaalt wel de actie maar start de CLI niet:

```bash
python3 ~/claude-verbruik/ververs-claude-token.py --dry-run
```

En de server één keer handmatig (`--eenmalig` haalt op, toont en stopt):

```bash
python3 ~/claude-verbruik/claude_verbruik_server.py --eenmalig
```

Verwacht `"bron": "live"` met je echte percentages. Staat er `geen-token`,
dan is de login nog niet gelukt.

### Stap 4 — units installeren

```bash
mkdir -p ~/.config/systemd/user
# kopieer systemd/claude-verbruik.service, claude-token-ververs.service
# en claude-token-ververs.timer naar ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-verbruik.service
systemctl --user enable --now claude-token-ververs.timer
```

User-units draaien normaal alleen tijdens een sessie. Zodat ze ook draaien
als je niet ingelogd bent (dat is het hele punt van een always-on Pi):

```bash
sudo loginctl enable-linger $USER
```

Controleren:

```bash
systemctl --user status claude-verbruik.service --no-pager
systemctl --user list-timers claude-token-ververs.timer --no-pager
curl -s http://localhost:8091/verbruik
```

### Stap 5 — de Cardputer erop richten

Zet in `buddy/device/apps/config.py` (gitignored, dus daar mag het echte
adres wél in staan):

```python
VERBRUIK_ENDPOINT = "http://<pi-lan-ip>:8091/verbruik"
```

en push `config.py` naar het toestel.

### Onderhoud

- Het refresh-token is ~30 dagen houdbaar **vanaf de laatste login** en
  schuift niet mee met refreshes. Ongeveer maandelijks is dus een
  handmatige `claude auth login` op de Pi nodig.
- Wat de timer deed staat in
  `~/.local/state/claude-token-refresh/ververs-claude-token.log`.
  Zie je daar `Nodig: claude auth login`, dan is dat het signaal.
- **Die log bewaakt nu ook of het token wérkt, niet alleen of het vers is.**
  Elke run vraagt de verbruikserver naar zijn toestand en zet dat in dezelfde
  regel: `... niets te doen; verbruikserver: live`. Haalt de server geen
  cijfers op, dan wordt het een `[WAARSCHUWING]` met de te nemen actie erbij.
  Dat gat kostte op 2026-08-03 een halve dag: het token werd keurig ververst
  terwijl de API het weigerde, en de log bleef `[OK]` melden. De controle
  doet géén eigen API-call — hij leest de server, die de call toch al doet.
  Een tweede poller zou namelijk het rate-limit-venster kunnen raken, en dat
  was nu juist de oorzaak van die storing.
- **Een verbindingsfout krijgt een tweede kans**, standaard na 3 seconden.
  `systemctl --user restart` meldt de unit actief zodra het proces draait,
  niet zodra het de poort heeft geopend — een controle die er meteen
  achteraan komt krijgt dus `Connection refused` terwijl er niets aan de hand
  is. Bewaking die af en toe onterecht alarm slaat leer je negeren, en dan
  doet ze niet meer waarvoor ze bedoeld is. Een server die wél antwoordt maar
  geen live cijfers heeft, meldt onveranderd meteen; daar is niets tijdelijks
  aan. Instelbaar met `--server-pogingen` en `--server-pauze`.
- De controle richt zich standaard op `http://127.0.0.1:8091/verbruik`. Een
  ander adres geef je met `--server`; een lege waarde (`--server ""`) schakelt
  hem uit, wat de tests ook gebruiken om hermetisch te blijven.

  Wil je de regel meteen zien in plaats van te wachten op de uurrem — die
  onderdrukt herhaling, dus vaak schrijft de timer hem al — draai dan:

  ```bash
  python3 ~/claude-verbruik/ververs-claude-token.py --server-pauze 0.1 2>&1 | tail -2
  ```

  De logregels gaan ook naar stderr, dus die zie je zo altijd.
- Diagnose per poging (CLI-debuglogs, momentopnames met vingerafdrukken —
  geen tokens) staat in `~/.local/state/claude-token-refresh/diagnose/`.
- `exit 1` van de refresh-unit is normaal: het script gebruikt die code ook
  voor "wacht" en "login-nodig". Vandaar `SuccessExitStatus=0 1`.

### Een refresh die niet terugkomt (storing 2026-09-14)

Na twee weken foutloos ververste de timer op 14 september om 15:48 en 16:04
niets: de CLI wachtte vijf seconden, maar het antwoord op de refresh kwam niet
binnen die tijd terug. De login bleef heel. Het script ging daarna op pauze tot
een nieuwe login, en zo werd een hapering een storing van dertig uur. Waarom het
antwoord uitbleef is niet vastgesteld; de verbinding was achteraf gezond.

Sindsdien:

- **Wachten op het token, niet op de klok.** stdin blijft open tot het nieuwe
  token daadwerkelijk in het credentialsbestand staat, met een maximum van
  30 s (`--stdin-open`). De OK-regel zegt hoe lang dat duurde:
  `Token ververst; nu geldig tot … (nieuw token na 1.52 s, CLI 2.61 s)`.
  Gezond is rond de anderhalve seconde; kruipt dat getal omhoog, dan hapert
  het endpoint voordat het echt misgaat.
- **Nooit meer opgeven zolang de login heel is.** Na een mislukte poging wacht
  het script 15 min, dan 1 uur, 2 uur en daarna elke 4 uur (`--wachtschema`).
  In de log: `Wacht: … Volgende poging om …`. Het stopt pas bij
  `Nodig: claude auth login`. De oude klep (twee pogingen, dan pauze) was
  bedoeld tegen de race van augustus, waarin een stille rotatie de volgende
  poging de login liet wissen — maar als de login na een poging nog heel is,
  heeft die poging niets geroteerd, en is doorproberen niet riskanter.
- **Markers die zeggen wat ze bewijzen.** De foutregel meldt nu
  `claude.ai-connectors opgehaald: ja/nee` en `bootstrap geslaagd: ja/nee`.
  Twee keer "nee" betekent: de CLI kwam niet voorbij de tokenstap. De oude
  markers ("achtergrondrefresh gestart", "OAuth-antwoord gezien") keken naar
  de Passes-cache en naar het versturen van een verzoek, en logden daardoor
  "ja" terwijl er niets was teruggekomen.
- **De server merkt een nieuwe login zelf op.** Verandert het
  credentialsbestand terwijl er geen live cijfers zijn, dan vervalt een
  lopende 429-wachttijd en haalt de server één keer opnieuw op — nooit binnen
  3 minuten na de vorige poging. Na `claude auth login` is een herstart dus
  niet meer nodig. In de journal: `credentialsbestand gewijzigd; wachttijd
  vervalt, nu opnieuw ophalen`.
