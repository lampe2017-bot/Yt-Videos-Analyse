# YouTube Cockpit

Lokales, automatisiertes Dashboard für 5 YouTube-Kanäle:

- **Everlast AI** (`@everlastai`) — rot
- **Leonard Schmedding** (`@LeonardSchmedding`) — lila
- **Dan Martell** (`@danmartell`) — cyan
- **Sebiforce** (`@sebiforce`) — amber
- **Niklas Steenfatt** (`@niklassteenfatt`) — grün

## Was es tut

Pro Lauf:

1. Löst den `@handle` jedes Kanals zur Channel-ID auf (yt-dlp, einmalig gecacht).
2. Holt den RSS-Feed (`feeds/videos.xml?channel_id=…`) jedes Kanals.
3. Erkennt neue Videos durch Diff gegen den lokalen Cache.
4. Zieht für jedes neue Video das Auto-Transkript via `youtube-transcript-api` (Deutsch bevorzugt, Englisch als Fallback).
5. Falls kein Transkript verfügbar (z.B. Rate-Limit oder deaktivierte Subs): nutzt die RSS-Beschreibung als Fallback und markiert das Video für späteren Retry.
6. Lässt das **lokale Ollama-Modell `qwen2.5:14b`** für jedes neue Video einen 1-Satz-Hook + 3-5 Bullets erzeugen.
7. Rendert ein dunkles Dashboard mit Inline-SVG-Covers (Kanalfarbe, Titel, Play-Button, NEU-Badge, Dauer).
8. Klick auf das Cover lädt das YouTube-Embed (`youtube-nocookie`) inline in der Kachel.

Alle Daten landen lokal in `cache/`, `subs/` und `output/`.
**Keine Auth-APIs, keine Cloud, keine personenbezogenen Daten.**

## Voraussetzungen

- Python 3.10+
- Ollama mit Modell `qwen2.5:14b` (`ollama pull qwen2.5:14b`)
- Python-Pakete: `yt-dlp`, `youtube-transcript-api`, `requests`

```powershell
pip install yt-dlp youtube-transcript-api requests
ollama pull qwen2.5:14b
```

## Bedienung

### Manuell

Doppelklick auf `run.bat` — die Pipeline läuft, anschließend öffnet sich das Dashboard im Browser.

Oder per CLI:

```powershell
python cockpit.py
```

### Optionen

```text
python cockpit.py --no-summary           # Nur RSS+Transkripte, ohne Ollama
python cockpit.py --limit 5              # Max. 5 Videos pro Kanal (Default 15)
python cockpit.py --channels everlastai  # Nur einen Kanal aktualisieren
```

## Automatisiert: Windows Task Scheduler

Damit das Cockpit z.B. alle 6 Stunden im Hintergrund neue Videos zieht und summarisiert:

1. **Win+R** → `taskschd.msc` → **"Task erstellen…"**
2. Reiter **Allgemein**:
   - Name: `YouTube Cockpit Refresh`
   - Anhaken: "Mit höchsten Privilegien ausführen" (nicht zwingend, aber stabil)
   - "Unabhängig von der Benutzeranmeldung ausführen"
3. Reiter **Trigger** → **Neu…**:
   - Beginnen: Heute, z.B. 08:00 Uhr
   - Wiederholen alle: 6 Stunden, für die Dauer von "Unbegrenzt"
4. Reiter **Aktionen** → **Neu…**:
   - Aktion: "Programm starten"
   - Programm/Skript: `python.exe`
   - Argumente: `D:\Claude\YT-Cockpit\cockpit.py`
   - Starten in: `D:\Claude\YT-Cockpit`
5. Reiter **Bedingungen** → "Nur starten, wenn folgende Netzwerkverbindung verfügbar ist: Beliebige Verbindung" anhaken.
6. Reiter **Einstellungen**:
   - "Aufgabe so schnell wie möglich nach einem ausgelassenen Start neu ausführen" anhaken.
   - "Aufgabe beenden, falls sie länger ausgeführt wird als" auf 30 Minuten setzen.

Beim ersten Lauf werden alle Kanäle initial gefüllt (~5-15 Min, je nach Modell-Geschwindigkeit). Danach sind Folgeläufe schnell, da nur noch neue Videos verarbeitet werden.

## Dateistruktur

```
YT-Cockpit/
├── channels.json          # Kanäle + Farben
├── cockpit.py             # Pipeline
├── run.bat                # Windows-Runner
├── cache/
│   ├── videos.json        # Alle Videos + Summaries (Hauptcache)
│   ├── channels.json      # @handle → channel_id
│   ├── history.json       # Versionshistorie
│   └── last_run.txt       # Timestamp letzter Refresh
├── subs/
│   └── <video_id>.json    # Roh-Transkript-Cache
└── output/
    └── cockpit.html       # Generiertes Dashboard
```

## Bekannte Eigenheiten

- **YouTube IP-Rate-Limit**: Bei Initialläufen mit vielen Videos kann YouTube unsere IP für 30-60 Min temporär blockieren. Die Pipeline fängt das ab und nutzt RSS-Beschreibungen als Fallback. Beim nächsten Lauf werden die betroffenen Videos automatisch noch einmal mit Transkript versucht.
- **Modellwahl**: `qwen2.5:14b` ist auf Deutsch sehr stark, braucht ~20-40s pro Video auf RTX-GPUs, auf CPU deutlich länger. Wer schnell will: in `cockpit.py` auf `llama3.1:8b` oder `mistral:7b` umstellen (Variable `OLLAMA_MODEL`).
- **Versionshistorie**: Im Dashboard unten als ausklappbares "Versionshistorie · letzte Läufe" — zeigt pro Lauf, welche Videos neu hinzugekommen sind.

## Erweitern

Weiteren Kanal hinzufügen: in `channels.json` einen Eintrag ergänzen, fertig — die Pipeline holt beim nächsten Lauf automatisch alles.

```json
{
  "handle": "neuerkanal",
  "name": "Neuer Kanal",
  "color": "#fb7185",
  "color_label": "rosa"
}
```
