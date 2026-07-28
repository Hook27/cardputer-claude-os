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

## Fase 1 — de echte server op de Pi

Nog te bouwen: `claude_verbruik_server.py` (systemd-service) plus een
Linux-tegenhanger van `ververs-claude-token.ps1` als systemd-timer. De Pi
krijgt daarvoor een **eigen** Claude Code-login; de limieten zijn
accountbreed, dus de cijfers zijn dezelfde als op de laptop.

Let op het onderhoud dat daarbij hoort: het refresh-token is ~30 dagen
houdbaar vanaf de laatste login en schuift niet mee met refreshes, dus
ongeveer maandelijks is een handmatige `claude auth login` op de Pi nodig.
