#!/usr/bin/env python3
"""
YouTube Cockpit — lokale Pipeline.

Zieht für die in channels.json hinterlegten Kanäle:
  1. Channel-ID aus dem @handle.
  2. Aktuelle Videos aus dem RSS-Feed.
  3. Auto-Subs (de/en) via yt-dlp für neue Videos.
  4. Zusammenfassung (Hook + 3-5 Bullets) via lokales Ollama-Modell.
  5. Rendert ein Dark-Mode-Cockpit als output/cockpit.html.

Gecacht wird in cache/videos.json. Subs landen in subs/. Re-Runs sind
inkrementell: bereits zusammengefasste Videos werden nicht neu verarbeitet.
"""

import argparse
import html as html_mod
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _yt_api = YouTubeTranscriptApi()
except ImportError:
    _yt_api = None

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
SUBS_DIR = ROOT / "subs"
OUTPUT_DIR = ROOT / "output"
CONFIG_PATH = ROOT / "channels.json"
VIDEOS_DB = CACHE_DIR / "videos.json"
CHANNEL_DB = CACHE_DIR / "channels.json"
HISTORY_DB = CACHE_DIR / "history.json"
LAST_RUN_PATH = CACHE_DIR / "last_run.txt"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:14b"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CONSENT_COOKIE = "SOCS=CAI; CONSENT=YES+1"

MAX_VIDEOS_PER_CHANNEL = 15
MAX_TRANSCRIPT_CHARS = 18000


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": CONSENT_COOKIE,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  [warn] cache {path.name} unlesbar ({e}), starte leer")
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_channel_id(handle: str, channel_cache: dict) -> str:
    """Auflösen via yt-dlp (zuverlässiger als HTML-Regex, da Pages
    fremde channelIds in Embeds enthalten können)."""
    handle_clean = handle.lstrip("@")
    cached = channel_cache.get(handle_clean)
    if cached and cached.get("channel_id"):
        return cached["channel_id"]

    url = f"https://www.youtube.com/@{handle_clean}"
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings",
             "--playlist-items", "1", "--print", "%(channel_id)s", url],
            capture_output=True, text=True, timeout=90,
        )
        for line in reversed(out.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("UC"):
                channel_cache[handle_clean] = {
                    "channel_id": line,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                return line
    except Exception as e:
        log(f"  [warn] yt-dlp-Auflösung von @{handle_clean} fehlgeschlagen: {e}")

    raise RuntimeError(f"Channel-ID für @{handle_clean} konnte nicht ermittelt werden")


def fetch_rss(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = http_get(url)
    root = ET.fromstring(xml)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    videos = []
    for entry in root.findall("atom:entry", ns):
        vid_el = entry.find("yt:videoId", ns)
        title_el = entry.find("atom:title", ns)
        pub_el = entry.find("atom:published", ns)
        if vid_el is None or title_el is None:
            continue
        thumb_el = entry.find(".//media:thumbnail", ns)
        desc_el = entry.find(".//media:description", ns)
        videos.append({
            "video_id": vid_el.text,
            "title": (title_el.text or "").strip(),
            "published": pub_el.text if pub_el is not None else "",
            "thumbnail": thumb_el.attrib.get("url", "") if thumb_el is not None else "",
            "description": ((desc_el.text or "")[:600] if desc_el is not None else ""),
        })
    return videos


def fetch_transcript(video_id: str, max_chars: int = MAX_TRANSCRIPT_CHARS):
    """
    Direkter Transkript-Fetch über youtube-transcript-api.
    Zwischenspeichert das Roh-Transkript als JSON in subs/<vid>.json.
    Rückgabe: (text, language_code) oder ('', None).
    """
    if _yt_api is None:
        return "", None

    cache_path = SUBS_DIR / f"{video_id}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return cached.get("text", "")[:max_chars], cached.get("language")
        except Exception:
            pass

    fetched = None
    last_err = None
    for attempt in range(3):
        try:
            fetched = _yt_api.fetch(video_id, languages=["de", "en"])
            break
        except Exception as e:
            last_err = e
            err_name = type(e).__name__
            if err_name in ("IpBlocked", "TooManyRequests", "YouTubeRequestFailed"):
                wait = 8 * (attempt + 1)
                log(f"     transcript: {err_name}, warte {wait}s und retry…")
                time.sleep(wait)
                continue
            break
    if fetched is None:
        log(f"     transcript: nicht verfügbar ({type(last_err).__name__ if last_err else 'unknown'})")
        return "", None

    snippets = getattr(fetched, "snippets", []) or []
    parts = []
    for snip in snippets:
        t = (snip.text or "").strip()
        t = re.sub(r"\s+", " ", t)
        if t and (not parts or parts[-1] != t):
            parts.append(t)
    text = " ".join(parts)
    text = re.sub(r"\s+", " ", text).strip()
    lang = getattr(fetched, "language_code", None)

    try:
        cache_path.write_text(
            json.dumps({"text": text, "language": lang}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

    return text[:max_chars], lang


def get_duration(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings",
             "--print", "%(duration)s", url],
            capture_output=True, text=True, timeout=60,
        )
        for line in reversed(out.stdout.strip().splitlines()):
            line = line.strip()
            if line and line != "NA" and line.isdigit():
                secs = int(line)
                h, rem = divmod(secs, 3600)
                m, s = divmod(rem, 60)
                if h:
                    return f"{h}:{m:02d}:{s:02d}"
                return f"{m}:{s:02d}"
    except Exception as e:
        log(f"  [warn] duration-Abfrage fehlgeschlagen {video_id}: {e}")
    return ""


def summarize(transcript: str, title: str, channel_name: str) -> dict:
    if not transcript or len(transcript) < 80:
        return {
            "hook": title,
            "bullets": ["Auto-Transkript noch nicht verfügbar — wird beim nächsten Lauf erneut versucht."],
            "model": None,
        }

    prompt = (
        "Du bist ein präziser deutscher Video-Zusammenfasser.\n"
        f"Kanal: {channel_name}\n"
        f"Videotitel: {title}\n\n"
        "Erzeuge aus dem folgenden Transkript:\n"
        "- 'hook': ein einzelner deutscher Satz mit max. 18 Wörtern, der den Kern des Videos auf den Punkt bringt.\n"
        "- 'bullets': 3 bis 5 prägnante deutsche Bullet-Aussagen (jede max. 22 Wörter), die die wichtigsten Erkenntnisse oder Schritte aus dem Video festhalten.\n\n"
        "Antworte ausschließlich mit JSON in exakt diesem Schema:\n"
        '{"hook": "...", "bullets": ["...", "...", "..."]}\n\n'
        "Transkript:\n"
        f"{transcript}\n"
    )

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.25, "num_ctx": 16384},
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read().decode("utf-8"))
        result = json.loads(data.get("response", "{}"))
        bullets = result.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        bullets = [str(b).strip() for b in bullets if str(b).strip()][:5]
        hook = str(result.get("hook", "")).strip()
        if not hook and bullets:
            hook = bullets[0]
        if not bullets:
            bullets = ["Zusammenfassung konnte nicht extrahiert werden."]
        return {"hook": hook or "—", "bullets": bullets, "model": OLLAMA_MODEL}
    except Exception as e:
        log(f"  [warn] Ollama-Zusammenfassung fehlgeschlagen: {e}")
        return {
            "hook": "Zusammenfassung lokal nicht erzeugbar.",
            "bullets": [f"Ollama-Fehler: {e}"],
            "model": None,
        }


def relative_date(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return "gerade eben"
        if s < 3600:
            m = s // 60
            return f"vor {m} Min"
        if s < 86400:
            h = s // 3600
            return f"vor {h} Std"
        days = s // 86400
        if days < 7:
            return f"vor {days} {'Tag' if days == 1 else 'Tagen'}"
        if days < 30:
            w = days // 7
            return f"vor {w} {'Woche' if w == 1 else 'Wochen'}"
        if days < 365:
            mo = days // 30
            return f"vor {mo} {'Monat' if mo == 1 else 'Monaten'}"
        y = days // 365
        return f"vor {y} {'Jahr' if y == 1 else 'Jahren'}"
    except Exception:
        return iso_str[:10]


def svg_cover(title: str, color: str, channel_name: str, duration: str, is_new: bool) -> str:
    safe_title = title if len(title) <= 70 else title[:68] + "…"
    safe_title = html_mod.escape(safe_title)
    safe_channel = html_mod.escape(channel_name)
    grad_id = f"g{abs(hash(title)) % 1000000}"
    badges = []
    if is_new:
        badges.append(
            '<g transform="translate(20 20)">'
            '<rect rx="4" ry="4" width="56" height="22" fill="#ef4444"/>'
            '<text x="28" y="15" text-anchor="middle" '
            'font-family="Inter,system-ui,sans-serif" font-size="12" '
            'font-weight="700" fill="white">NEU</text></g>'
        )
    channel_w = max(72, len(channel_name) * 7 + 16)
    badges.append(
        f'<g transform="translate(20 {52 if is_new else 20})">'
        f'<rect rx="4" ry="4" width="{channel_w}" height="22" '
        f'fill="rgba(0,0,0,0.55)" stroke="{color}" stroke-width="1"/>'
        f'<text x="{channel_w//2}" y="15" text-anchor="middle" '
        f'font-family="Inter,system-ui,sans-serif" font-size="11" '
        f'font-weight="600" fill="white">{safe_channel}</text></g>'
    )
    if duration:
        dur_safe = html_mod.escape(duration)
        dur_w = max(48, len(duration) * 8 + 16)
        badges.append(
            f'<g transform="translate({620 - dur_w} 318)">'
            f'<rect rx="4" ry="4" width="{dur_w}" height="22" fill="rgba(0,0,0,0.7)"/>'
            f'<text x="{dur_w//2}" y="15" text-anchor="middle" '
            f'font-family="Inter,system-ui,sans-serif" font-size="11" '
            f'font-weight="600" fill="white">{dur_safe}</text></g>'
        )
    badges_svg = "\n".join(badges)

    return f'''<svg viewBox="0 0 640 360" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice" class="cover-svg">
  <defs>
    <linearGradient id="{grad_id}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{color}" stop-opacity="0.92"/>
      <stop offset="65%" stop-color="#1a1a1a" stop-opacity="1"/>
      <stop offset="100%" stop-color="#0a0a0a" stop-opacity="1"/>
    </linearGradient>
  </defs>
  <rect width="640" height="360" fill="url(#{grad_id})"/>
  <foreignObject x="32" y="92" width="576" height="180">
    <div xmlns="http://www.w3.org/1999/xhtml" style="font-family:Inter,system-ui,-apple-system,sans-serif;font-size:30px;font-weight:700;color:white;line-height:1.2;text-shadow:0 2px 14px rgba(0,0,0,0.7);">{safe_title}</div>
  </foreignObject>
  <g transform="translate(320 230)">
    <circle r="42" fill="rgba(0,0,0,0.55)" stroke="white" stroke-width="3"/>
    <polygon points="-14,-19 -14,19 21,0" fill="white"/>
  </g>
  {badges_svg}
</svg>'''


def render_html(channels_cfg, videos_db, history, last_run_iso) -> str:
    rows = []
    for v in videos_db.values():
        rows.append(v)
    rows.sort(key=lambda v: v.get("published", ""), reverse=True)

    tiles_html = []
    for v in rows:
        cfg = next((c for c in channels_cfg if c["handle"].lower() == v["channel_handle"].lower()), None)
        color = cfg["color"] if cfg else "#888"
        ch_name = cfg["name"] if cfg else v["channel_handle"]
        is_new = bool(v.get("is_new"))
        bullets_html = "".join(
            f'<li>{html_mod.escape(b)}</li>' for b in (v.get("summary", {}).get("bullets") or [])
        )
        hook = html_mod.escape(v.get("summary", {}).get("hook") or "")
        title = html_mod.escape(v.get("title", ""))
        published_iso = v.get("published", "")
        rel = relative_date(published_iso)
        duration = v.get("duration", "")
        cover = svg_cover(v["title"], color, ch_name, duration, is_new)
        embed_url = f'https://www.youtube-nocookie.com/embed/{v["video_id"]}?rel=0'
        watch_url = f'https://www.youtube.com/watch?v={v["video_id"]}'

        tile = f'''
<article class="tile" data-channel="{html_mod.escape(v["channel_handle"])}" style="--accent:{color}">
  <button class="cover-wrap" type="button" aria-label="Video abspielen: {title}">
    {cover}
  </button>
  <div class="tile-body">
    <h2 class="tile-title">{title}</h2>
    <div class="tile-meta">
      <span class="meta-channel" style="color:{color}">●&nbsp;{html_mod.escape(ch_name)}</span>
      <span class="meta-sep">·</span>
      <time class="rel-time" datetime="{html_mod.escape(published_iso)}" title="{html_mod.escape(published_iso)}">{html_mod.escape(rel)}</time>
      {f'<span class="meta-sep">·</span><span>{html_mod.escape(duration)}</span>' if duration else ''}
      <a class="meta-link" href="{watch_url}" target="_blank" rel="noopener">YouTube ↗</a>
    </div>
    {f'<p class="tile-hook">{hook}</p>' if hook else ''}
    <ul class="tile-bullets">{bullets_html}</ul>
  </div>
  <template class="embed-template">
    <iframe src="{embed_url}" loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
  </template>
</article>'''
        tiles_html.append(tile)

    by_channel = {}
    for v in videos_db.values():
        by_channel.setdefault(v["channel_handle"], 0)
        by_channel[v["channel_handle"]] += 1
    channel_chips = "".join(
        f'<span class="chip" style="--accent:{c["color"]}">●&nbsp;{html_mod.escape(c["name"])} '
        f'<b>{by_channel.get(c["handle"], 0)}</b></span>'
        for c in channels_cfg
    )

    history_items = "".join(
        f'<li><time>{html_mod.escape(h["at"][:16].replace("T", " "))}</time> '
        f'<span>{h["new_count"]} neue Videos</span>'
        f'{" — " + html_mod.escape(", ".join(h["new_titles"][:3])) + ("…" if len(h["new_titles"]) > 3 else "") if h.get("new_titles") else ""}</li>'
        for h in reversed(history[-20:])
    ) or "<li>Noch keine vorherigen Läufe.</li>"

    last_run_rel = relative_date(last_run_iso) if last_run_iso else "—"
    last_run_iso_safe = html_mod.escape(last_run_iso or "")
    total = len(videos_db)

    return f'''<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Cockpit · Lokal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  :root {{
    --bg: #0a0a0c;
    --bg-2: #131318;
    --bg-3: #1c1c24;
    --border: #2a2a36;
    --text: #f1f1f4;
    --muted: #9b9bab;
    --hook: #ffd166;
  }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  body {{ min-height: 100vh; }}
  .wrap {{ max-width: 1280px; margin: 0 auto; padding: 32px 24px 80px; }}
  header.cockpit {{ display: flex; flex-direction: column; gap: 16px; padding-bottom: 28px; border-bottom: 1px solid var(--border); margin-bottom: 32px; }}
  header.cockpit h1 {{ margin: 0; font-size: 28px; font-weight: 800; letter-spacing: -0.02em; }}
  header.cockpit h1 .accent {{ background: linear-gradient(90deg, #ff5b5b, #a855f7, #0ea5e9, #f59e0b, #10b981); -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .stat-row {{ display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; font-size: 13px; color: var(--muted); }}
  .chip {{ display: inline-flex; gap: 6px; align-items: center; padding: 5px 10px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 999px; font-size: 12px; color: var(--text); }}
  .chip b {{ color: var(--accent, var(--text)); font-weight: 700; }}
  .chip span:first-child {{ color: var(--accent); }}
  .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; }}
  @media (max-width: 880px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .tile {{ background: var(--bg-2); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease; display: flex; flex-direction: column; }}
  .tile:hover {{ transform: translateY(-2px); border-color: var(--accent); box-shadow: 0 12px 36px -16px var(--accent); }}
  .cover-wrap {{ all: unset; cursor: pointer; display: block; aspect-ratio: 16/9; background: #000; position: relative; }}
  .cover-svg {{ width: 100%; height: 100%; display: block; }}
  .cover-wrap iframe {{ width: 100%; height: 100%; border: 0; display: block; }}
  .tile-body {{ padding: 18px 20px 22px; display: flex; flex-direction: column; gap: 10px; }}
  .tile-title {{ margin: 0; font-size: 18px; font-weight: 700; line-height: 1.32; letter-spacing: -0.01em; }}
  .tile-meta {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; color: var(--muted); }}
  .meta-sep {{ opacity: 0.5; }}
  .meta-channel {{ font-weight: 600; }}
  .meta-link {{ margin-left: auto; color: var(--muted); text-decoration: none; }}
  .meta-link:hover {{ color: var(--accent); }}
  .tile-hook {{ margin: 4px 0 2px; padding: 10px 12px; background: rgba(255, 209, 102, 0.08); border-left: 3px solid var(--hook); border-radius: 4px; font-size: 14px; font-weight: 500; color: var(--hook); line-height: 1.4; }}
  .tile-bullets {{ margin: 4px 0 0; padding: 0 0 0 20px; color: var(--muted); font-size: 13.5px; line-height: 1.5; display: flex; flex-direction: column; gap: 4px; }}
  .tile-bullets li::marker {{ color: var(--accent); }}
  details.history {{ margin-top: 48px; padding: 16px 20px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 12px; }}
  details.history summary {{ cursor: pointer; font-weight: 600; font-size: 14px; color: var(--text); }}
  details.history ul {{ margin: 12px 0 0; padding: 0 0 0 20px; color: var(--muted); font-size: 13px; line-height: 1.6; }}
  details.history time {{ color: var(--text); font-variant-numeric: tabular-nums; margin-right: 6px; }}
  footer {{ margin-top: 48px; font-size: 12px; color: var(--muted); text-align: center; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="cockpit">
    <h1><span class="accent">YouTube Cockpit</span> · 5 Kanäle</h1>
    <div class="stat-row">
      {channel_chips}
      <span style="margin-left:auto">{total} Videos · letzter Refresh: <time class="rel-time" datetime="{last_run_iso_safe}" title="{last_run_iso_safe}" style="color:var(--text);font-weight:600">{html_mod.escape(last_run_rel)}</time></span>
    </div>
  </header>
  <main class="grid">
    {''.join(tiles_html)}
  </main>
  <details class="history">
    <summary>Versionshistorie · letzte Läufe</summary>
    <ul>{history_items}</ul>
  </details>
  <footer>Lokal generiert via cockpit.py · Ollama-Modell: {OLLAMA_MODEL}</footer>
</div>
<script>
  document.querySelectorAll('.cover-wrap').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const tile = btn.closest('.tile');
      const tpl = tile.querySelector('.embed-template');
      if (!tpl) return;
      const iframe = tpl.content.firstElementChild.cloneNode(true);
      btn.replaceWith(iframe);
    }});
  }});

  // Relative Zeiten clientseitig — bleiben auch zwischen Server-Builds aktuell.
  function fmtRel(iso) {{
    const t = new Date(iso);
    if (isNaN(t)) return null;
    const s = Math.max(0, Math.floor((Date.now() - t.getTime()) / 1000));
    if (s < 60) return 'gerade eben';
    if (s < 3600) {{ const m = Math.floor(s/60); return 'vor ' + m + ' Min'; }}
    if (s < 86400) {{ const h = Math.floor(s/3600); return 'vor ' + h + ' Std'; }}
    const d = Math.floor(s/86400);
    if (d < 7) return 'vor ' + d + (d === 1 ? ' Tag' : ' Tagen');
    if (d < 30) {{ const w = Math.floor(d/7); return 'vor ' + w + (w === 1 ? ' Woche' : ' Wochen'); }}
    if (d < 365) {{ const mo = Math.floor(d/30); return 'vor ' + mo + (mo === 1 ? ' Monat' : ' Monaten'); }}
    const y = Math.floor(d/365);
    return 'vor ' + y + (y === 1 ? ' Jahr' : ' Jahren');
  }}
  function refreshTimes() {{
    document.querySelectorAll('time.rel-time').forEach(el => {{
      const iso = el.getAttribute('datetime');
      if (!iso) return;
      const txt = fmtRel(iso);
      if (txt) el.textContent = txt;
    }});
  }}
  refreshTimes();
  setInterval(refreshTimes, 60000);
</script>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description="YouTube Cockpit Pipeline")
    parser.add_argument("--no-summary", action="store_true",
                        help="Überspringe Ollama-Zusammenfassungen (nur RSS+Transkript)")
    parser.add_argument("--limit", type=int, default=MAX_VIDEOS_PER_CHANNEL,
                        help=f"Max. Videos pro Kanal (Default {MAX_VIDEOS_PER_CHANNEL})")
    parser.add_argument("--channels", nargs="*",
                        help="Optional: nur bestimmte Handles aktualisieren")
    parser.add_argument("--render-only", action="store_true",
                        help="HTML aus Cache neu rendern, kein Netzwerk-/AI-Zugriff")
    args = parser.parse_args()

    for d in (CACHE_DIR, SUBS_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    cfg = load_json(CONFIG_PATH, {"channels": []})
    channels_cfg = cfg.get("channels", [])
    if args.channels:
        wanted = {c.lower().lstrip("@") for c in args.channels}
        channels_cfg = [c for c in channels_cfg if c["handle"].lower() in wanted]

    videos_db = load_json(VIDEOS_DB, {})
    channel_cache = load_json(CHANNEL_DB, {})
    history = load_json(HISTORY_DB, [])

    previous_ids = set(videos_db.keys())
    new_titles_this_run = []

    if args.render_only:
        log("→ render-only: kein Netzwerk-/AI-Zugriff")
        for v in videos_db.values():
            v["is_new"] = False
        last_run_iso = datetime.now(timezone.utc).isoformat()
        html_out = render_html(channels_cfg, videos_db, history, last_run_iso)
        out_path = OUTPUT_DIR / "cockpit.html"
        out_path.write_text(html_out, encoding="utf-8")
        log(f"✓ Cockpit gerendert: {out_path}")
        return

    for ch in channels_cfg:
        log(f"→ Kanal: {ch['name']} (@{ch['handle']})")
        try:
            cid = resolve_channel_id(ch["handle"], channel_cache)
            log(f"   channel_id: {cid}")
        except Exception as e:
            log(f"   [error] {e}, überspringe")
            continue
        save_json(CHANNEL_DB, channel_cache)

        try:
            entries = fetch_rss(cid)
        except Exception as e:
            log(f"   [error] RSS fehlgeschlagen: {e}")
            continue
        log(f"   {len(entries)} Einträge im RSS-Feed")

        new_in_channel = 0
        for entry in entries[: args.limit]:
            vid = entry["video_id"]
            existing = videos_db.get(vid)
            if existing:
                # Lokaler Lauf: nur skippen, wenn echte Ollama-Summary + echtes Transkript da sind.
                # CI-Lauf (--no-summary): existierende Videos nie erneut anfassen, egal in welchem Zustand.
                if args.no_summary:
                    videos_db[vid]["is_new"] = False
                    # Published-Datum aus RSS aktualisieren, falls YouTube es korrigiert hat
                    if entry.get("published"):
                        videos_db[vid]["published"] = entry["published"]
                    continue
                already_full = (
                    existing.get("summary", {}).get("bullets")
                    and existing.get("summary", {}).get("model")
                    and existing.get("transcript_lang") not in (None, "rss-desc")
                )
                if already_full:
                    videos_db[vid]["is_new"] = False
                    continue
            if new_in_channel > 0:
                time.sleep(2.5)
            new_in_channel += 1

            log(f"   • {vid} — {entry['title'][:70]}")

            if args.no_summary:
                # CI-Pfad: kein Transkript-Fetch (Ollama läuft hier eh nicht), kein yt-dlp-Aufruf
                transcript = entry.get("description", "")
                lang = "rss-desc"
                duration = ""
            else:
                transcript, lang = fetch_transcript(vid)
                if transcript:
                    log(f"     transcript: {lang}, {len(transcript)} chars")
                elif entry.get("description"):
                    log("     fallback: nutze RSS-Beschreibung statt Transkript")
                    transcript = entry["description"]
                    lang = "rss-desc"
                duration = get_duration(vid)

            if args.no_summary:
                desc = (entry.get("description") or "").strip()
                summary = {
                    "hook": "Zusammenfassung folgt beim nächsten lokalen Lauf.",
                    "bullets": [desc[:240] + ("…" if len(desc) > 240 else "")] if desc
                               else ["Beschreibung vom Kanal noch nicht verfügbar."],
                    "model": None,
                }
            elif not transcript:
                summary = {
                    "hook": "Kein Auto-Transkript verfügbar.",
                    "bullets": [entry["description"][:200] + "…"] if entry.get("description") else ["—"],
                    "model": None,
                }
            else:
                log(f"     summarisiere via {OLLAMA_MODEL} ({len(transcript)} Zeichen)…")
                t0 = time.time()
                summary = summarize(transcript, entry["title"], ch["name"])
                log(f"     summary: {len(summary['bullets'])} bullets in {time.time()-t0:.1f}s")

            videos_db[vid] = {
                **entry,
                "channel_handle": ch["handle"],
                "channel_id": cid,
                "duration": duration,
                "transcript_lang": lang,
                "summary": summary,
                "first_seen": videos_db.get(vid, {}).get(
                    "first_seen", datetime.now(timezone.utc).isoformat()
                ),
                "is_new": vid not in previous_ids,
            }
            if vid not in previous_ids:
                new_titles_this_run.append(entry["title"])
            save_json(VIDEOS_DB, videos_db)

    last_run_iso = datetime.now(timezone.utc).isoformat()
    LAST_RUN_PATH.write_text(last_run_iso, encoding="utf-8")

    if new_titles_this_run or not history:
        history.append({
            "at": last_run_iso,
            "new_count": len(new_titles_this_run),
            "new_titles": new_titles_this_run,
        })
        save_json(HISTORY_DB, history)

    html_out = render_html(channels_cfg, videos_db, history, last_run_iso)
    out_path = OUTPUT_DIR / "cockpit.html"
    out_path.write_text(html_out, encoding="utf-8")
    log(f"✓ Cockpit gerendert: {out_path}")
    log(f"  Videos: {len(videos_db)} · neu in diesem Lauf: {len(new_titles_this_run)}")


if __name__ == "__main__":
    main()
