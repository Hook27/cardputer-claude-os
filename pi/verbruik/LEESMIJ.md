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
  cache ~90 s
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

`bron` is `live` (vers van de API), `gemeten`/`geen-token` (server kon niet
verversen, dit zijn de laatst bekende cijfers) of `nep` (testserver). Het
toestel toont dat in de kop, zodat je oude cijfers nooit voor verse aanziet.

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
| `test-ververs-claude-token.py` | offline test met nep-credentials |
| `systemd/*.service`, `*.timer` | user-units voor beide |

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
# kopieer hierheen: claude_verbruik_server.py, ververs-claude-token.py,
# test-ververs-claude-token.py
```

### Stap 3 — eerst testen, dan pas aanzetten

De offline test raakt je echte token niet aan (nep-credentials, nep-CLI,
geen netwerk) en lokt juist de gevaarlijke paden uit: pogingenlimiet,
leeggemaakte login, verlopen refresh-token.

```bash
python3 ~/claude-verbruik/test-ververs-claude-token.py
```

Verwacht: `13 goed, 0 fout`. Daarna één echte, ongevaarlijke controle —
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
- Diagnose per poging (CLI-debuglogs, momentopnames met vingerafdrukken —
  geen tokens) staat in `~/.local/state/claude-token-refresh/diagnose/`.
- `exit 1` van de refresh-unit is normaal: het script gebruikt die code ook
  voor "gepauzeerd" en "login-nodig". Vandaar `SuccessExitStatus=0 1`.
