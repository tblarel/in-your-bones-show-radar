"""
fetch_events.py

End-to-end "InYourBones — Ticketmaster Radar" script.

Features:
- Fetch Ticketmaster events for a set of Bay Area cities and time windows
- Basic on-disk caching to avoid hammering the API
- Heuristic scoring via scoring.score_event
- AI refinement / editorial ranking via ai_filter.refine_top_events_with_ai
- Multi-night show collapsing (e.g. 3-night Ariana run → one row with [multi-night x3])
- Exports:
    * JSON files per window
    * CSV files per window
    * RSS feed per window
    * Plain-text digest per window (for potential email use)

You can run this directly:
    python fetch_events.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests
from dotenv import load_dotenv

from scoring import score_event
from ai_filter import refine_top_events_with_ai


# ---------------------------
# Config
# ---------------------------

CITIES = [
    ("San Francisco", "CA"),
    ("Oakland", "CA"),
    ("Berkeley", "CA"),
    ("San Jose", "CA"),
    ("Santa Cruz", "CA"),
    ("Mountain View", "CA"),
    ("Santa Clara", "CA"),
    ("Napa", "CA"),
    ("Concord", "CA"),
]

COUNTRY_CODE = "US"

# Time windows relative to "now"
WINDOW_DEFS = {
    "short_term": (14, 120),   # days from now
    "far_out": (120, 365),
}

CACHE_DIR = Path(".tm_cache")
CACHE_TTL_SECONDS = 12 * 3600  # 12 hours

EXPORT_DIR = Path("output")
EXPORT_DIR.mkdir(exist_ok=True, parents=True)

STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True, parents=True)
KNOWN_EVENTS_FILE = STATE_DIR / "known_events.json"


# ---------------------------
# Helpers
# ---------------------------

@dataclass
class NormalizedEvent:
    id: str
    name: str
    primary_artist: str
    url: str | None
    city: str | None
    state: str | None
    country: str | None
    venue_name: str | None
    local_date: str | None
    start_datetime: datetime | None
    promoter_name: str | None
    window: str
    score: float
    raw: Dict[str, Any]


def _tm_iso(dt: datetime) -> str:
    # Ticketmaster expects UTC ISO8601 with 'Z'
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_cache_key(params: Dict[str, Any]) -> str:
    """
    Produce a deterministic hash for a Ticketmaster params dict,
    ignoring the API key.
    """
    safe = {k: v for k, v in params.items() if k != "apikey"}
    blob = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.md5(blob).hexdigest()


def _params_to_query(params: Dict[str, Any]) -> str:
    parts = []
    for k, v in params.items():
        parts.append(f"{k}={v}")
    return "&".join(parts)


def _tm_get_with_cache(params: Dict[str, Any], use_cache: bool = True) -> Dict[str, Any]:
    """
    Fetch Ticketmaster discovery endpoint with basic file cache.
    """
    base_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    CACHE_DIR.mkdir(exist_ok=True, parents=True)

    cache_key = _make_cache_key(params)
    cache_path = CACHE_DIR / f"{cache_key}.json"

    if use_cache and cache_path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            cache_path.stat().st_mtime, tz=timezone.utc
        )
        if age.total_seconds() <= CACHE_TTL_SECONDS:
            print(f"→ [cache hit] params={cache_key}")
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)

    print(f"→ [live request] {base_url}?{_params_to_query(params)}")
    resp = requests.get(base_url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    return data


def _extract_promoter_name(ev: Dict[str, Any]) -> str | None:
    """
    Try a few places Ticketmaster might store a promoter / presenter name.
    """
    promotions = ev.get("promoters") or []
    if isinstance(promotions, list) and promotions:
        name = promotions[0].get("name")
        if name:
            return name

    promoter = ev.get("promoter") or {}
    if isinstance(promoter, dict):
        name = promoter.get("name") or promoter.get("description")
        if name:
            return name

    return None


def _normalize_tm_event(ev: Dict[str, Any], window: str) -> NormalizedEvent:
    dates = ev.get("dates", {}) or {}
    start = dates.get("start", {}) or {}

    # Start date / time
    local_date = start.get("localDate")
    date_time = start.get("dateTime")  # ISO8601
    dt = None
    if date_time:
        try:
            dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        except Exception:
            dt = None

    embedded = ev.get("_embedded") or {}
    venues = embedded.get("venues") or []
    venue = venues[0] if venues else {}

    city = (venue.get("city") or {}).get("name")
    state = (venue.get("state") or {}).get("stateCode")
    country = (venue.get("country") or {}).get("countryCode")
    venue_name = venue.get("name")

    # Artists / attractions
    attractions = embedded.get("attractions") or []
    primary_artist = None
    if attractions:
        primary_artist = attractions[0].get("name")

    promoter_name = _extract_promoter_name(ev)

    # We don't yet have a score; placeholder 0.0, actual score added later.
    return NormalizedEvent(
        id=str(ev.get("id")),
        name=str(ev.get("name")),
        primary_artist=primary_artist or str(ev.get("name")),
        url=ev.get("url"),
        city=city,
        state=state,
        country=country,
        venue_name=venue_name,
        local_date=local_date,
        start_datetime=dt,
        promoter_name=promoter_name,
        window=window,
        score=0.0,
        raw=ev,
    )


def _fetch_city_for_window(
    city: str,
    state: str,
    window_name: str,
    start_dt: datetime,
    end_dt: datetime,
    max_events: int = 200,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """
    Fetch up to max_events Ticketmaster events for a single city + window.
    Returns raw Ticketmaster event objects.
    """
    all_events: List[Dict[str, Any]] = []
    page = 0
    total_pages = None

    while True:
        params: Dict[str, Any] = {
            "city": city,
            "stateCode": state,
            "countryCode": COUNTRY_CODE,
            "classificationName": "music",
            "startDateTime": _tm_iso(start_dt),
            "endDateTime": _tm_iso(end_dt),
            "size": 100,
            "page": page,
            "sort": "date,asc",
            "apikey": os.getenv("TM_API_KEY") or "",
        }

        data = _tm_get_with_cache(params, use_cache=use_cache)

        page_events = data.get("_embedded", {}).get("events") or []
        if not page_events:
            print(f"⚠ No events found for {city} on page {page}")
            break

        all_events.extend(page_events)
        print(f"  ✓ Page {page}: {len(page_events)} events (total so far: {len(all_events)})")

        # Pagination metadata
        page_info = data.get("page") or {}
        total_pages = page_info.get("totalPages")

        if len(all_events) >= max_events:
            print(f"  → Reached max_events={max_events} for {city}, stopping pagination.")
            break

        if total_pages is not None and page >= total_pages - 1:
            print(f"  → Reached last page ({page}/{total_pages - 1}) for {city}.")
            break

        page += 1

    print(f"✓ Found {len(all_events)} events in {city} in total\n")
    return all_events


def _dedupe_events(events: Iterable[NormalizedEvent]) -> List[NormalizedEvent]:
    """
    De-duplicate by Ticketmaster event id, keeping the highest score.
    """
    best_by_id: Dict[str, NormalizedEvent] = {}
    for ev in events:
        if not ev.id:
            continue
        existing = best_by_id.get(ev.id)
        if existing is None or ev.score > existing.score:
            best_by_id[ev.id] = ev
    return list(best_by_id.values())


def _collapse_multi_night(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse multi-night runs into a single representative row.

    Grouping key:
      (primary_artist or name, venue_name, window)

    Within each group, we:
      - track earliest & latest start_datetime
      - count number of nights
      - attach list of all underlying event ids in 'ids'
    """
    from collections import defaultdict

    if not events:
        return []

    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)

    for ev in events:
        artist = (ev.get("primary_artist") or ev.get("name") or "").strip().lower()
        venue = (ev.get("venue_name") or "").strip().lower()
        window = ev.get("window") or ""
        key = (artist, venue, window)
        groups[key].append(ev)

    collapsed: List[Dict[str, Any]] = []

    for key, items in groups.items():
        if len(items) == 1:
            ev = dict(items[0])
            ev.setdefault("multi_night", False)
            ev.setdefault("night_count", 1)
            ev.setdefault("ids", [ev.get("id")])
            ev.setdefault("date_start", ev.get("start_datetime"))
            ev.setdefault("date_end", ev.get("start_datetime"))
            collapsed.append(ev)
            continue

        # Multi-night run
        sorted_items = sorted(
            items,
            key=lambda e: e.get("start_datetime") or datetime.max.replace(tzinfo=timezone.utc),
        )
        first = dict(sorted_items[0])

        dates = [
            e.get("start_datetime")
            for e in sorted_items
            if isinstance(e.get("start_datetime"), datetime)
        ]
        if dates:
            date_start = min(dates)
            date_end = max(dates)
        else:
            date_start = first.get("start_datetime")
            date_end = first.get("start_datetime")

        first["multi_night"] = True
        first["night_count"] = len(sorted_items)
        first["ids"] = [e.get("id") for e in sorted_items if e.get("id")]
        first["date_start"] = date_start
        first["date_end"] = date_end

        # Aggregate AI priority as max, keep reason from first
        priorities = [e.get("ai_priority") for e in sorted_items if e.get("ai_priority") is not None]
        if priorities:
            first["ai_priority"] = max(priorities)

        collapsed.append(first)

    # Sort collapsed list by (ai_priority desc, score desc)
    def sort_key(ev: Dict[str, Any]):
        return (
            ev.get("ai_priority") or 0,
            ev.get("score") or 0.0,
        )

    collapsed.sort(key=sort_key, reverse=True)
    return collapsed


def _format_date_range(ev: Dict[str, Any]) -> str:
    """
    Human-friendly date range like:
      2026-01-06 → 2026-01-08
    or single date if not multi-night.
    """
    def fmt(dt: datetime | None) -> str:
        if not dt:
            return "?"
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")

    if ev.get("multi_night"):
        return f"{fmt(ev.get('date_start'))} → {fmt(ev.get('date_end'))}"
    dt = ev.get("start_datetime")
    if isinstance(dt, datetime):
        return fmt(dt)
    # fallback to local_date
    ld = ev.get("local_date")
    return ld or "?"


def _serialize_for_export(ev: Dict[str, Any]) -> Dict[str, Any]:
    def to_iso(x):
        if isinstance(x, datetime):
            return x.astimezone(timezone.utc).isoformat()
        return x

    return {
        "id": ev.get("id"),
        "ids": ev.get("ids"),
        "name": ev.get("name"),
        "primary_artist": ev.get("primary_artist"),
        "venue_name": ev.get("venue_name"),
        "city": ev.get("city"),
        "state": ev.get("state"),
        "country": ev.get("country"),
        "url": ev.get("url"),
        "window": ev.get("window"),
        "local_date": ev.get("local_date"),
        "start_datetime": to_iso(ev.get("start_datetime")),
        "date_start": to_iso(ev.get("date_start")),
        "date_end": to_iso(ev.get("date_end")),
        "multi_night": ev.get("multi_night", False),
        "night_count": ev.get("night_count", 1),
        "score": ev.get("score"),
        "ai_priority": ev.get("ai_priority"),
        "ai_reason": ev.get("ai_reason"),
    }


def _export_json(window: str, events: List[Dict[str, Any]]) -> None:
    path = EXPORT_DIR / f"radar_{window}.json"
    payload = [_serialize_for_export(ev) for ev in events]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"✓ JSON exported → {path}")


def _export_csv(window: str, events: List[Dict[str, Any]]) -> None:
    path = EXPORT_DIR / f"radar_{window}.csv"
    if not events:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("")  # empty
        print(f"✓ CSV exported (empty) → {path}")
        return

    fieldnames = list(_serialize_for_export(events[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ev in events:
            writer.writerow(_serialize_for_export(ev))
    print(f"✓ CSV exported → {path}")


def _export_rss(window: str, events: List[Dict[str, Any]]) -> None:
    """
    Minimal RSS 2.0 feed so you can plug this into a widget or RSS reader.
    """
    from xml.etree.ElementTree import Element, SubElement, ElementTree
    from email.utils import format_datetime

    path = EXPORT_DIR / f"radar_{window}.rss"

    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")

    title = SubElement(channel, "title")
    title.text = f"InYourBones — Bay Area Radar ({window})"

    link = SubElement(channel, "link")
    link.text = "https://inyourbones.live"

    desc = SubElement(channel, "description")
    desc.text = "Curated upcoming Bay Area shows from InYourBones."

    now = datetime.now(timezone.utc)
    last_build = SubElement(channel, "lastBuildDate")
    last_build.text = format_datetime(now)

    for ev in events:
        item = SubElement(channel, "item")
        title = SubElement(item, "title")
        artist = ev.get("primary_artist") or ev.get("name") or "Unknown artist"
        venue = ev.get("venue_name") or "Unknown venue"
        title.text = f"{artist} @ {venue}"

        link_el = SubElement(item, "link")
        link_el.text = ev.get("url") or "https://inyourbones.live"

        guid = SubElement(item, "guid")
        guid.text = (ev.get("id") or "") + f"-{window}"

        pub = SubElement(item, "pubDate")
        dt = ev.get("start_datetime") or ev.get("date_start") or now
        if isinstance(dt, datetime):
            pub.text = format_datetime(dt)
        else:
            pub.text = format_datetime(now)

        desc_el = SubElement(item, "description")
        date_str = _format_date_range(ev)
        reason = ev.get("ai_reason") or ""
        desc_el.text = f"{date_str} — {artist} at {venue}. {reason}".strip()

    tree = ElementTree(rss)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    print(f"✓ RSS exported → {path}")


def _export_digest(window: str, events: List[Dict[str, Any]]) -> None:
    """
    Simple plain-text digest that could be dropped into a weekly email.
    """
    path = EXPORT_DIR / f"radar_{window}_digest.txt"
    lines: List[str] = []
    header = f"InYourBones — Bay Area Radar ({window})"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")

    for ev in events:
        date_str = _format_date_range(ev)
        artist = ev.get("primary_artist") or ev.get("name")
        venue = ev.get("venue_name") or "Unknown venue"
        city = ev.get("city") or ""
        reason = ev.get("ai_reason") or ""
        multi = ""
        if ev.get("multi_night"):
            multi = f" [multi-night x{ev.get('night_count')}]"
        lines.append(f"- {date_str} — {artist} @ {venue} ({city}){multi}")
        if reason:
            lines.append(f"    ▹ {reason}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✓ Digest exported → {path}")


# ---------------------------
# State management for tracking new events
# ---------------------------

def _load_known_events() -> Dict[str, Dict[str, str]]:
    """
    Load the known events state file.
    Returns a dict like:
    {
      "short_term": {"event_key": "2025-11-20", ...},
      "far_out": {"event_key": "2025-11-20", ...}
    }
    """
    if not KNOWN_EVENTS_FILE.exists():
        return {}
    
    try:
        with open(KNOWN_EVENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure both windows exist
        if not isinstance(data, dict):
            return {}
        for window in WINDOW_DEFS.keys():
            if window not in data:
                data[window] = {}
        return data
    except Exception as e:
        print(f"⚠ Failed to load known events: {e}")
        return {}


def _save_known_events(known: Dict[str, Dict[str, str]]) -> None:
    """
    Save the known events state file.
    """
    try:
        with open(KNOWN_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=2, ensure_ascii=False)
        print(f"✓ Known events state saved → {KNOWN_EVENTS_FILE}")
    except Exception as e:
        print(f"⚠ Failed to save known events: {e}")


def _event_key(ev: Dict[str, Any]) -> str:
    """
    Generate a stable key for an event.
    For single-night: just the TM id
    For multi-night: primary id + date range to track the run
    """
    if ev.get("multi_night") and ev.get("ids"):
        # Use sorted IDs to handle any ordering issues
        ids = sorted(ev.get("ids", []))
        return "|".join(ids)
    # Single night: just use the primary ID
    return str(ev.get("id", ""))


def _export_new_only_json(window: str, events: List[Dict[str, Any]]) -> None:
    """
    Export JSON file containing only new events.
    """
    path = EXPORT_DIR / f"radar_{window}_new.json"
    payload = [_serialize_for_export(ev) for ev in events]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"✓ New events JSON exported → {path}")


def _export_new_only_digest(window: str, events: List[Dict[str, Any]]) -> None:
    """
    Plain-text digest of only new events (ideal for email alerts).
    """
    path = EXPORT_DIR / f"radar_{window}_new_digest.txt"
    lines: List[str] = []
    header = f"InYourBones — NEW Bay Area Shows ({window})"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    
    if not events:
        lines.append("No new shows since last run.")
        lines.append("")
    else:
        lines.append(f"Found {len(events)} new show(s) since last run:")
        lines.append("")

        for ev in events:
            date_str = _format_date_range(ev)
            artist = ev.get("primary_artist") or ev.get("name")
            venue = ev.get("venue_name") or "Unknown venue"
            city = ev.get("city") or ""
            reason = ev.get("ai_reason") or ""
            multi = ""
            if ev.get("multi_night"):
                multi = f" [multi-night x{ev.get('night_count')}]"
            lines.append(f"- {date_str} — {artist} @ {venue} ({city}){multi}")
            if reason:
                lines.append(f"    ▹ {reason}")
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✓ New events digest exported → {path}")


# ---------------------------
# Main orchestration
# ---------------------------

def main() -> None:
    load_dotenv()

    print("\n==============================")
    print("InYourBones — Ticketmaster Radar (Scored + AI Refined + Cached)")
    print("==============================\n")

    # Environment check
    tm_key = os.getenv("TM_API_KEY")
    if not tm_key:
        print("❌ TM_API_KEY not set in environment.")
        return

    openai_key = os.getenv("OPENAI_API_KEY")
    print("Environment check:")
    print(f"✓ TM_API_KEY loaded: {'Yes' if tm_key else 'No'}")
    print(f"✓ OPENAI_API_KEY present: {'Yes' if openai_key else 'No'}")
    print(f"✓ Caching enabled: True (TTL={CACHE_TTL_SECONDS/3600:.0f}h, dir={CACHE_DIR})")
    print()

    # Optional: GOOGLE_CREDS_JSON just to sanity-check env
    google_creds_raw = os.getenv("GOOGLE_CREDS_JSON")
    if google_creds_raw:
        try:
            json.loads(google_creds_raw)
            print("✓ Loaded GOOGLE_CREDS_JSON successfully\n")
        except json.JSONDecodeError:
            print("⚠ GOOGLE_CREDS_JSON is set but not valid JSON\n")
    else:
        print("ℹ GOOGLE_CREDS_JSON not set (ok if you don't need Sheets/BigQuery)\n")

    now = datetime.now(timezone.utc)

    all_normalized: List[NormalizedEvent] = []

    # Fetch + normalize for each window & city
    for window_name, (start_offset, end_offset) in WINDOW_DEFS.items():
        print("\n==============================")
        print(f"Window: {window_name} ({start_offset}–{end_offset} days from now)")
        print("==============================\n")

        start_dt = datetime.combine(
            (now + timedelta(days=start_offset)).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        end_dt = datetime.combine(
            (now + timedelta(days=end_offset)).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        for city, state in CITIES:
            print(f"===== Fetching events for {city} =====")
            print(f"  Date range: {_tm_iso(start_dt)} → {_tm_iso(end_dt)}")
            raw_events = _fetch_city_for_window(
                city=city,
                state=state,
                window_name=window_name,
                start_dt=start_dt,
                end_dt=end_dt,
                max_events=200,
                use_cache=True,
            )
            for ev in raw_events:
                norm = _normalize_tm_event(ev, window_name)
                all_normalized.append(norm)

    # Convert NormalizedEvent dataclasses to dicts and score them
    enriched: List[Dict[str, Any]] = []
    for ne in all_normalized:
        ev_dict = {
            "id": ne.id,
            "name": ne.name,
            "primary_artist": ne.primary_artist,
            "url": ne.url,
            "city": ne.city,
            "state": ne.state,
            "country": ne.country,
            "venue_name": ne.venue_name,
            "local_date": ne.local_date,
            "start_datetime": ne.start_datetime,
            "promoter_name": ne.promoter_name,
            "window": ne.window,
        }
        sres = score_event(
            {
                **ev_dict,
                "_embedded": ne.raw.get("_embedded") if isinstance(ne.raw, dict) else None,
                "classifications": ne.raw.get("classifications") if isinstance(ne.raw, dict) else [],
                "start_datetime": ne.start_datetime,
            }
        )
        ev_dict["score"] = sres.score
        enriched.append(ev_dict)

    # De-duplicate by TM id
    deduped = _dedupe_events(
        [
            NormalizedEvent(
                id=e["id"],
                name=e["name"],
                primary_artist=e["primary_artist"],
                url=e["url"],
                city=e["city"],
                state=e["state"],
                country=e["country"],
                venue_name=e["venue_name"],
                local_date=e["local_date"],
                start_datetime=e["start_datetime"],
                promoter_name=e["promoter_name"],
                window=e["window"],
                score=e["score"],
                raw={},
            )
            for e in enriched
        ]
    )

    # Convert back to dicts with scores
    deduped_dicts: List[Dict[str, Any]] = [
        {
            "id": ev.id,
            "name": ev.name,
            "primary_artist": ev.primary_artist,
            "url": ev.url,
            "city": ev.city,
            "state": ev.state,
            "country": ev.country,
            "venue_name": ev.venue_name,
            "local_date": ev.local_date,
            "start_datetime": ev.start_datetime,
            "promoter_name": ev.promoter_name,
            "window": ev.window,
            "score": ev.score,
        }
        for ev in deduped
    ]

    print(f"✓ After deduplication, events count: {len(deduped_dicts)}\n")

    # Per-window AI refinement & collapsing
    final_by_window: Dict[str, List[Dict[str, Any]]] = {}

    for window_name in WINDOW_DEFS.keys():
        window_events = [e for e in deduped_dicts if e["window"] == window_name]
        window_events.sort(key=lambda e: e["score"], reverse=True)

        pre_top_n = 200
        candidates = window_events[:pre_top_n]
        top_k = 20  # final number of rows we care about per window

        print("==============================")
        print(
            f"AI-refined top events for window '{window_name}' "
            f"(from {len(window_events)} events, taking first {len(candidates)} for AI)"
        )
        print("==============================")

        refined = refine_top_events_with_ai(
            candidates,
            window_name,
            top_k=top_k,
            max_items=pre_top_n,
            debug=False,
        )

        collapsed = _collapse_multi_night(refined)
        final_by_window[window_name] = collapsed

        # Print to console
        for ev in collapsed:
            date_str = _format_date_range(ev)
            venue = ev.get("venue_name") or "Unknown venue"
            city = ev.get("city") or ""
            ai = ev.get("ai_priority")
            multi = ""
            if ev.get("multi_night"):
                multi = f" [multi-night x{ev.get('night_count')}]"
            ai_str = f" | AI {ai:.1f}" if isinstance(ai, (int, float)) and ai is not None else ""
            print(
                f" • [{ev.get('score'):.3f}{ai_str}] {date_str} — "
                f"{ev.get('name')} @ {venue} ({city}){multi} [{window_name}]"
            )

        print()

    total_before = len(all_normalized)
    total_after = sum(len(v) for v in final_by_window.values())
    print("==============================")
    print(f"Total normalized events before dedupe: {total_before}")
    print(f"Total collapsed editorial picks after AI: {total_after}")
    print("==============================\n")

    # Load known events from previous runs
    print("Loading known events state...")
    known_events = _load_known_events()
    for window in WINDOW_DEFS.keys():
        if window not in known_events:
            known_events[window] = {}
    print(f"✓ Loaded {sum(len(v) for v in known_events.values())} known events from previous runs\n")

    # Track new events and update state
    new_by_window: Dict[str, List[Dict[str, Any]]] = {}
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("Identifying new events...")
    for window_name, events in final_by_window.items():
        # Build set of current event keys in this window
        current_keys = {_event_key(ev) for ev in events if _event_key(ev)}
        
        # Remove events that are no longer in the current window (cleanup old entries)
        old_keys = set(known_events[window_name].keys())
        removed_keys = old_keys - current_keys
        if removed_keys:
            for key in removed_keys:
                del known_events[window_name][key]
            print(f"  {window_name}: Removed {len(removed_keys)} events that are no longer in window")
        
        # Identify new events
        new_events = []
        for ev in events:
            key = _event_key(ev)
            if key and key not in known_events[window_name]:
                new_events.append(ev)
                # Add to known events with current run date
                known_events[window_name][key] = run_date
        
        new_by_window[window_name] = new_events
        print(f"  {window_name}: {len(new_events)} new out of {len(events)} total")
    
    print()

    # Save updated known events state
    _save_known_events(known_events)
    print()

    # Exports - Full feeds
    print("Exporting full feeds...")
    for window_name, events in final_by_window.items():
        _export_json(window_name, events)
        _export_csv(window_name, events)
        _export_rss(window_name, events)
        _export_digest(window_name, events)
    print()

    # Exports - New-only feeds
    print("Exporting new-only feeds...")
    for window_name, new_events in new_by_window.items():
        _export_new_only_json(window_name, new_events)
        _export_new_only_digest(window_name, new_events)
    print()


if __name__ == "__main__":
    main()
