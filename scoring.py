"""
scoring.py

Heuristics for scoring Ticketmaster events for InYourBones show radar.

The goal is to assign each event a base numeric score in [0, 1] that reflects
how compelling it is for coverage, before any AI reranking happens.

This module is intentionally self-contained and does NOT call any external APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# ---------------------------
# Venue tiers & weights
# ---------------------------

# Rough tiers for common Bay Area venues. 
VENUE_TIERS: Dict[str, float] = {
    # --- ARENAS / STADIUMS ---
    "chase center": 1.00,
    "oakland arena": 0.98,
    "sap center": 0.97,
    "sap center at san jose": 0.97,
    "levis stadium": 0.97,
    "levi's stadium": 0.97,

    # --- LARGE AMPHITHEATERS / OUTDOOR ---
    "shoreline amphitheatre": 0.96,
    "greek theatre-uc berkeley": 0.96,
    "greek theatre": 0.96,
    "frost amphitheater": 0.94,
    "mountain winery": 0.93,
    "concord pavilion": 0.93,
    "toyota pavilion at concord": 0.93,

    # --- LARGE THEATERS / CIVIC ---
    "bill graham civic auditorium": 0.94,
    "san jose civic": 0.92,
    "san jose center for the performing arts": 0.91,
    "paramount theatre oakland": 0.91,
    "palace of fine arts": 0.90,
    "davies symphony hall": 0.90,
    "war memorial opera house": 0.90,

    # --- MARQUEE CLUBS / A-TIER CLUBS ---
    "the fillmore": 0.93,
    "warfield": 0.93,
    "the warfield": 0.93,
    "fox theater - oakland": 0.93,
    "fox theatre - oakland": 0.93,
    "fox theater": 0.93,
    "the masonic": 0.92,
    "the regency ballroom": 0.91,
    "regency ballroom": 0.91,
    "uc theatre": 0.89,
    "the uc theatre": 0.89,
    "great american music hall": 0.89,
    "gamh": 0.89,
    "august hall": 0.88,
    "bimbo's 365 club": 0.88,
    "bimbos 365 club": 0.88,
    "bimbo's": 0.88,

    # --- STRONG CLUBS / B-TIER CLUBS ---
    "the independent": 0.88,
    "independent": 0.88,
    "the chapel": 0.85,
    "the new parish": 0.84,
    "new parish": 0.84,
    "sweetwater music hall": 0.84,
    "sweetwater": 0.84,
    "cornerstone berkeley": 0.83,
    "cornerstone": 0.83,

    # High-cred but small
    "bottom of the hill": 0.82,

    # --- INTIMATE / SMALL MUSIC ROOMS ---
    "rickshaw stop": 0.82,
    "cafe du nord": 0.81,
    "brick & mortar music hall": 0.80,
    "brick and mortar music hall": 0.80,
    "neck of the woods": 0.79,
    "the lost church - san francisco": 0.79,
    "the lost church": 0.79,
    "boom boom room": 0.78,
    "music city san francisco": 0.78,
    "the make-out room": 0.77,
    "make-out room": 0.77,

    # --- NAPA / NORTH BAY ---
    "uptown theatre napa": 0.86,
    "blue note napa": 0.84,
}


DEFAULT_VENUE_WEIGHT = 0.75


def _normalize_key(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().lower()


def venue_weight(name: Optional[str]) -> float:
    key = _normalize_key(name)
    if not key:
        return DEFAULT_VENUE_WEIGHT
    for known, w in VENUE_TIERS.items():
        if known in key:
            return w
    return DEFAULT_VENUE_WEIGHT


# ---------------------------
# Genre / classification fit
# ---------------------------

GENRE_HINTS = {
    "pop": 1.0,
    "rock": 1.0,
    "indie": 1.0,
    "alternative": 1.0,
    "alt": 1.0,
    "hip hop": 0.9,
    "hip-hop": 0.9,
    "rap": 0.9,
    "electronic": 0.85,
    "edm": 0.85,
    "reggae": 0.85,
    "country": 0.80,
    "latin": 0.80,
    "metal": 0.75,
    "comedy": 0.3,   # de-prioritize non-music
}


def genre_fit(event: Dict[str, Any]) -> float:
    """
    Very lightweight genre fit based on Ticketmaster 'classifications'.
    """
    classes = event.get("classifications") or []
    texts = []
    for c in classes:
        for key in ("genre", "subGenre", "segment", "subType", "type"):
            obj = c.get(key)
            if isinstance(obj, dict):
                name = obj.get("name")
                if name:
                    texts.append(str(name).lower())

    if not texts:
        return 0.8  # neutral if unknown

    best = 0.7
    for text in texts:
        for hint, weight in GENRE_HINTS.items():
            if hint in text:
                best = max(best, weight)
    return best


# ---------------------------
# Date proximity bonus
# ---------------------------

def date_proximity_bonus(dt: Optional[datetime], now: Optional[datetime] = None) -> float:
    """
    Gives a small bonus for nearer-term shows.
    0.0 for >= 365 days away, ~0.1 for very soon.
    """
    if dt is None:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)

    delta_days = (dt - now).days
    if delta_days < 0:
        return -0.2  # in the past, strongly down-weight
    if delta_days <= 7:
        return 0.10
    if delta_days <= 30:
        return 0.08
    if delta_days <= 120:
        return 0.05
    if delta_days <= 365:
        return 0.02
    return 0.0


# ---------------------------
# Public API
# ---------------------------

@dataclass
class ScoreResult:
    score: float
    components: Dict[str, float]


def score_event(event: Dict[str, Any]) -> ScoreResult:
    """
    Compute a base numeric score in [0, 1] for an event.

    We combine:
      - venue weight
      - genre fit
      - date proximity
    """
    # Venue
    v_name = event.get("venue_name") or (
        (event.get("_embedded") or {}).get("venues", [{}])[0].get("name")
        if isinstance(event.get("_embedded"), dict)
        else None
    )
    v_weight = venue_weight(v_name)

    # Genre
    g_weight = genre_fit(event)

    # Date
    dt = event.get("start_datetime") or event.get("date")
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            dt = None
    d_bonus = date_proximity_bonus(dt)

    # Combine
    base = 0.5
    score = base
    score += 0.3 * (v_weight - 0.75)  # venue swings ±0.075
    score += 0.3 * (g_weight - 0.8)   # genre swings ±0.06
    score += d_bonus                  # date can add up to 0.1

    score = max(0.0, min(1.0, score))

    components = {
        "venue_weight": v_weight,
        "genre_fit": g_weight,
        "date_bonus": d_bonus,
        "base": base,
    }
    return ScoreResult(score=score, components=components)
    