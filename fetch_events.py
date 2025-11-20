# fetch_events.py
import os
import json
import hashlib
from datetime import datetime, timedelta, timezone, time

import requests
from dotenv import load_dotenv

from scoring import (
    score_event,
    get_venue_tier,
    get_genre_fit,
    get_promoter_weight,
    editorial_fit_score,
)
from ai_filter import refine_top_events_with_ai  # AI reranking

# ---------------------------
# Environment & config
# ---------------------------

load_dotenv()

TM_API_KEY = os.getenv("TM_API_KEY")
if not TM_API_KEY:
    raise ValueError("TM_API_KEY not found in environment variables. Check your .env file.")

GOOGLE_CREDS_RAW = os.getenv("GOOGLE_CREDS_JSON")
if GOOGLE_CREDS_RAW:
    try:
        json.loads(GOOGLE_CREDS_RAW)
        print("✓ Loaded GOOGLE_CREDS_JSON successfully")
    except Exception as e:
        print("⚠ Failed to parse GOOGLE_CREDS_JSON:", e)
else:
    print("⚠ GOOGLE_CREDS_JSON not found (this is OK for now)")

OPENAI_PRESENT = bool(os.getenv("OPENAI_API_KEY"))

TM_BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

BAY_CITIES = [
    "San Francisco",
    "Oakland",
    "Berkeley",
    "San Jose",
    "Santa Cruz",
    "Mountain View",
    "Santa Clara",
    "Napa",
    "Concord",
]

WINDOWS = {
    "short_term": {
        "start_days": 14,
        "end_days": 120,
    },
    "far_out": {
        "start_days": 120,
        "end_days": 365,
    },
}

# ---------------------------
# Simple on-disk cache
# ---------------------------

CACHE_ENABLED = True
CACHE_DIR = ".tm_cache"
CACHE_TTL_HOURS = 12  # how long a cached response is considered fresh

os.makedirs(CACHE_DIR, exist_ok=True)


def _make_cache_key(params: dict) -> str:
    """
    Build a stable cache key from request params. We:
    - Drop the API key
    - Sort params to keep order stable
    - Hash the resulting string
    """
    items = sorted((k, v) for k, v in params.items() if k.lower() != "apikey")
    s = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _get_cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _load_from_cache(key: str):
    path = _get_cache_path(key)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None

    ts_str = payload.get("timestamp")
    if not ts_str:
        return None

    try:
        ts = datetime.fromisoformat(ts_str)
    except Exception:
        return None

    if CACHE_TTL_HOURS is not None:
        if datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc) > timedelta(hours=CACHE_TTL_HOURS):
            # expired
            return None

    return payload.get("data")


def _save_to_cache(key: str, data: dict, params: dict):
    path = _get_cache_path(key)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "params": {k: v for k, v in params.items() if k.lower() != "apikey"},
        "data": data,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"⚠ Failed to write cache file {path}: {e}")


# ---------------------------
# HTTP helper
# ---------------------------

def tm_request(params):
    """
    Make request to Ticketmaster Discovery API with basic error handling
    and simple on-disk caching keyed by params.
    """
    # Build cache key (without apikey)
    cache_key = _make_cache_key(params)

    if CACHE_ENABLED:
        cached = _load_from_cache(cache_key)
        if cached is not None:
            print(f"→ [cache hit] params={cache_key}")
            return cached

    params["apikey"] = TM_API_KEY

    try:
        response = requests.get(TM_BASE_URL, params=params, timeout=15)
        print(f"→ [live request] {response.url}")
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        print("❌ HTTP error:", e)
        try:
            print("Response body:", response.text[:500])
        except Exception:
            pass
        return None
    except Exception as e:
        print("❌ Request failed:", e)
        return None

    if CACHE_ENABLED:
        _save_to_cache(cache_key, data, params)

    return data


# ---------------------------
# Fetching & normalization
# ---------------------------

def fetch_events_for_city(city, days_from_now_start, days_from_now_end, max_events=200):
    # Use date-based windows so caching works across runs on the same day
    today_utc = datetime.now(timezone.utc).date()

    start_date = today_utc + timedelta(days=days_from_now_start)
    end_date = today_utc + timedelta(days=days_from_now_end)

    # Start and end at midnight UTC for stability
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date, time.min, tzinfo=timezone.utc)

    start_iso = start.isoformat(timespec="seconds").replace("+00:00", "Z")
    end_iso = end.isoformat(timespec="seconds").replace("+00:00", "Z")

    print(f"\n===== Fetching events for {city} =====")
    print(f"  Date range: {start_iso} → {end_iso}")

    all_events = []
    page = 0
    page_size = 100

    while True:
        params = {
            "city": city,
            "stateCode": "CA",
            "countryCode": "US",
            "classificationName": "music",
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "size": page_size,
            "page": page,
            "sort": "date,asc",
        }

        data = tm_request(params)
        if not data:
            print(f"⚠ No data returned for {city} page {page}")
            break

        embedded = data.get("_embedded", {})
        events = embedded.get("events", [])
        if not events:
            print(f"⚠ No events found for {city} on page {page}")
            break

        all_events.extend(events)
        print(f"  ✓ Page {page}: {len(events)} events (total so far: {len(all_events)})")

        if len(all_events) >= max_events:
            print(f"  → Reached max_events={max_events} for {city}, stopping pagination.")
            break

        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        if page >= total_pages - 1:
            print(f"  → Reached last page ({page}/{total_pages - 1}) for {city}.")
            break

        page += 1

    print(f"✓ Found {len(all_events)} events in {city} in total")
    return all_events


def parse_date(date_raw: str):
    if not date_raw:
        return None
    try:
        return datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_primary_genre(raw_event):
    classifications = raw_event.get("classifications") or []
    if not classifications:
        return None

    c = classifications[0] or {}
    for key in ("subGenre", "genre", "subType", "type", "segment"):
        part = c.get(key) or {}
        name = part.get("name")
        if name and name != "Undefined":
            return name
    return None


def extract_promoter_name(raw_event):
    """
    Ticketmaster sometimes uses 'promoter' or 'promoters'.
    We grab the first recognizable name if present.
    """
    promoter = raw_event.get("promoter") or {}
    name = promoter.get("name")
    if name:
        return name

    promoters = raw_event.get("promoters") or []
    if promoters:
        first = promoters[0] or {}
        if first.get("name"):
            return first["name"]

    return None


def normalize_event(raw, window_name: str):
    venues = (raw.get("_embedded") or {}).get("venues") or [{}]
    venue = venues[0] or {}
    venue_name = venue.get("name", "Unknown Venue")
    city_name = (venue.get("city") or {}).get("name", "")

    attractions = (raw.get("_embedded") or {}).get("attractions") or []
    primary_artist = attractions[0]["name"] if attractions else None

    date_raw = (raw.get("dates") or {}).get("start", {}).get("dateTime")
    date = parse_date(date_raw)

    genre_name = extract_primary_genre(raw)
    promoter_name = extract_promoter_name(raw)

    normalized = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "artist_primary": primary_artist,
        "venue": venue_name,
        "city": city_name,
        "date": date,
        "window": window_name,
        "genre_primary": genre_name,
        "promoter_name": promoter_name,
    }

    # Attach per-dimension scores and flags
    venue_tier = get_venue_tier(venue_name)
    genre_fit = get_genre_fit(genre_name)
    promoter_weight = get_promoter_weight(promoter_name, venue_name)
    ed_fit = editorial_fit_score(normalized)

    normalized["venue_tier"] = venue_tier
    normalized["genre_fit"] = genre_fit
    normalized["promoter_weight"] = promoter_weight
    normalized["editorial_fit"] = ed_fit

    # A rough guess at 'press eligibility'
    normalized["press_eligible"] = bool(
        (venue_tier >= 0.75 and genre_fit >= 0.7) or promoter_weight >= 0.7
    )

    normalized["score"] = score_event(normalized)
    return normalized


def dedupe_events(events):
    """
    Dedupe by (artist_primary or name, venue, window), keeping the earliest
    date + best scoring 'canonical' entry, and tracking multi-night runs.

    For each (artist, venue, window) key we aggregate:
    - canonical event (used for metadata and score)
    - date_first (earliest date)
    - date_last  (latest date)
    - num_dates  (how many dates were seen)
    - multi_day  (bool)
    """
    seen = {}

    for e in events:
        artist_key = e.get("artist_primary") or e.get("name")
        key = (artist_key, e.get("venue"), e.get("window"))

        d_new = e.get("date")

        if key not in seen:
            seen[key] = {
                "canonical": e,
                "date_first": d_new,
                "date_last": d_new,
                "num_dates": 1 if d_new else 0,
            }
            continue

        agg = seen[key]

        # Update date range + count
        if d_new:
            if agg["date_first"] is None or d_new < agg["date_first"]:
                agg["date_first"] = d_new
            if agg["date_last"] is None or d_new > agg["date_last"]:
                agg["date_last"] = d_new
            agg["num_dates"] += 1

        # Decide if this new event should replace the canonical representative
        c = agg["canonical"]
        d_old = c.get("date")

        if d_new and d_old:
            if d_new < d_old:
                better = True
            elif d_new > d_old:
                better = False
            else:
                better = e.get("score", 0) > c.get("score", 0)
        elif d_new and not d_old:
            better = True
        elif not d_new and d_old:
            better = False
        else:
            better = e.get("score", 0) > c.get("score", 0)

        if better:
            agg["canonical"] = e

    # Flatten aggregated structures into final events
    result = []
    for agg in seen.values():
        c = agg["canonical"].copy()
        c["date_first"] = agg["date_first"]
        c["date_last"] = agg["date_last"]
        c["num_dates"] = agg["num_dates"]
        c["multi_day"] = bool(agg["num_dates"] and agg["num_dates"] > 1)
        result.append(c)

    return result


def _format_date_range(e):
    """
    Helper to show a nicer date string in CLI output if multi-day.
    """
    d_first = e.get("date_first") or e.get("date")
    d_last = e.get("date_last") or e.get("date")

    if not d_first:
        return "Unknown date"

    if not d_last or d_last == d_first:
        return d_first.strftime("%Y-%m-%d %H:%M")

    # Multi-day span
    if d_first.date() == d_last.date():
        # Same calendar day, different times
        return f"{d_first.strftime('%Y-%m-%d')} ({d_first.strftime('%H:%M')}–{d_last.strftime('%H:%M')})"

    # Different days
    # Example: 2026-05-08 → 2026-05-10
    return f"{d_first.strftime('%Y-%m-%d')} → {d_last.strftime('%Y-%m-%d')}"


def summarize_normalized_event(e):
    date_s = _format_date_range(e)
    artist_or_name = e["artist_primary"] or e["name"]
    ai_part = f" | AI {e['ai_priority']:.1f}" if "ai_priority" in e else ""
    multi_tag = ""
    if e.get("multi_day") and e.get("num_dates", 1) > 1:
        multi_tag = f" [multi-night x{e['num_dates']}]"

    print(
        f" • [{e['score']:.3f}{ai_part}] {date_s} — {artist_or_name} "
        f"@ {e['venue']} ({e['city']}){multi_tag} [{e['window']}]"
    )


# ---------------------------
# Main
# ---------------------------

def main():
    print("\n==============================")
    print("InYourBones — Ticketmaster Radar (Scored + AI Refined + Cached)")
    print("==============================\n")

    print("Environment check:")
    print("✓ TM_API_KEY loaded:", "Yes" if TM_API_KEY else "NO!")
    print("✓ OPENAI_API_KEY present:", "Yes" if OPENAI_PRESENT else "No (AI step will be skipped)")
    print(f"✓ Caching enabled: {CACHE_ENABLED} (TTL={CACHE_TTL_HOURS}h, dir={CACHE_DIR})")
    print()

    normalized_events = []

    # Fetch + normalize
    for window_name, cfg in WINDOWS.items():
        print(
            f"\n==============================\n"
            f"Window: {window_name} "
            f"({cfg['start_days']}–{cfg['end_days']} days from now)\n"
            f"=============================="
        )
        for city in BAY_CITIES:
            raw_events = fetch_events_for_city(
                city,
                days_from_now_start=cfg["start_days"],
                days_from_now_end=cfg["end_days"],
                max_events=200,
            )
            for raw in raw_events:
                ev = normalize_event(raw, window_name)
                normalized_events.append(ev)

    # Dedupe across all windows (we keep window info in each event)
    deduped_events = dedupe_events(normalized_events)
    print(f"\n✓ After deduplication, events count: {len(deduped_events)}")

    # Per window: sort by score, take top N, run AI refinement
    for window_name in WINDOWS.keys():
        window_events = [e for e in deduped_events if e["window"] == window_name]
        window_events_sorted = sorted(
            window_events,
            key=lambda e: e["score"],
            reverse=True
        )

        pre_top_n = 200  # expanded AI input pool
        candidates = window_events_sorted[:pre_top_n]

        print(
            f"\n==============================\n"
            f"AI-refined top events for window '{window_name}' "
            f"(from {len(window_events)} events, taking first {len(candidates)} for AI)\n"
            f"=============================="
        )

        refined = refine_top_events_with_ai(candidates, window_name, top_k=20)

        for e in refined:
            summarize_normalized_event(e)

    print("\n==============================")
    print(f"Total normalized events before dedupe: {len(normalized_events)}")
    print(f"Total normalized events after dedupe: {len(deduped_events)}")
    print("==============================\n")
    print("Test complete.\n")


if __name__ == "__main__":
    main()
