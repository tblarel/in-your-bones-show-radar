# scoring.py
from datetime import datetime, timezone

# Rough venue tiers (0–1.0) based on size / prestige / how interesting they are for you.
VENUE_TIERS = {
    # SF
    "Chase Center": 1.0,
    "Chase Center - Thrive City": 0.9,
    "Bill Graham Civic Auditorium": 0.9,
    "Warfield": 0.9,
    "The Warfield": 0.9,
    "The Masonic": 0.85,
    "The Fillmore": 0.85,
    "The Independent": 0.8,
    "Great American Music Hall": 0.8,
    "Bimbo's 365 Club": 0.8,
    "The Regency Ballroom": 0.8,
    "The UC Theatre": 0.8,
    "Rickshaw Stop": 0.7,
    "Neck of the Woods": 0.6,
    "Brick and Mortar Music Hall": 0.6,
    "Cafe Du Nord": 0.7,
    "The Chapel": 0.7,

    # Oakland
    "Fox Theater - Oakland": 0.9,
    "Oakland Arena": 0.95,
    "Paramount Theatre Oakland": 0.85,
    "Crybaby": 0.7,
    "Yoshi's Oakland": 0.7,

    # Berkeley
    "Greek Theatre-U.C. Berkeley": 0.9,
    "Cornerstone - CA": 0.7,

    # South Bay / SC
    "SAP Center at San Jose": 0.95,
    "San Jose Civic": 0.8,
    "San Jose Center for the Performing Arts": 0.75,
    "The Ritz": 0.7,
    "Levi's® Stadium": 1.0,

    # Shoreline / Napa / Concord
    "Shoreline Amphitheatre": 0.9,
    "Blue Note Napa": 0.7,
    "Uptown Theatre Napa": 0.8,
    "Toyota Pavilion at Concord": 0.9,

    # Fallback
    "Unknown Venue": 0.4,
}

# Genre preference weights (0–1.0). Tune to your taste.
GENRE_BOOST = {
    "Rock": 1.0,
    "Alternative": 0.90,
    "Indie": 0.85,
    "Indie Rock": 0.90,
    "Pop": 0.95,
    "Pop Rock": 0.95,
    "Hip-Hop/Rap": 0.95,
    "Hip-Hop": 0.95,
    "Rap": 0.95,
    "R&B": 0.8,
    "Electronic": 0.95,
    "Dance/Electronic": 0.95,
    "Reggae": 0.8,
    "Punk": 0.8,

    # Stuff you likely care less about for IYB
    "Country": 0.7,
    "Classical": 0.1,
    "Jazz": 0.1,
    "Comedy": 0.0,
}

# Very rough promoter “weights” based on how good they are for press / access / relevance.
# This is substring-based, so "Goldenvoice Presents" etc will match.
PROMOTER_KEYWORDS = {
    "goldenvoice": 1.0,
    "live nation": 0.95,
    "another planet": 0.9,
    "ape": 0.9,  # Another Planet shorthand sometimes
    "chase center": 0.85,
    "oakland arena": 0.85,
    "sap center": 0.85,
}


def get_venue_tier(name: str) -> float:
    if not name:
        return VENUE_TIERS["Unknown Venue"]
    return VENUE_TIERS.get(name, VENUE_TIERS["Unknown Venue"])


def get_genre_fit(name: str | None) -> float:
    if not name:
        return 0.6  # neutral-ish default
    return GENRE_BOOST.get(name, 0.6)


def get_promoter_weight(promoter_name: str | None, venue_name: str | None = None) -> float:
    """
    Crude guess at how 'good' the promoter is for you.

    - Boost Goldenvoice / Live Nation / Another Planet.
    - If promoter is unknown but the venue is very big (Chase, Oakland Arena, SAP),
      assume a decent promoter weight.
    """
    base = 0.5  # neutral default

    if promoter_name:
        p = promoter_name.lower()
        best = base
        for kw, w in PROMOTER_KEYWORDS.items():
            if kw in p:
                best = max(best, w)
        if best != base:
            return best

    # No known promoter string – infer from venue
    venue_tier = get_venue_tier(venue_name or "")
    if venue_tier >= 0.95:
        return 0.85
    if venue_tier >= 0.9:
        return 0.8
    if venue_tier >= 0.8:
        return 0.7

    return base


def date_bonus(event) -> float:
    """
    Reward events that are in a "sweet spot" depending on window.
    Returns 0–1.
    """
    date = event.get("date")
    if not date:
        return 0.0

    now = datetime.now(timezone.utc)
    days = (date - now).days
    window = event.get("window")

    if window == "short_term":
        # Sweet spot: ~30–90 days from now
        if 30 <= days <= 90:
            return 1.0
        # 14–30 days: still okay, smaller bonus
        if 14 <= days < 30:
            return 0.7
        # 90–120 days: trailing off
        if 90 < days <= 120:
            return 0.6
        return 0.3  # still something, but less ideal

    if window == "far_out":
        # Sweet spot: closer to the start of the far-out window (big tours just announced)
        if 120 <= days <= 200:
            return 1.0
        if 200 < days <= 280:
            return 0.7
        if 280 < days <= 365:
            return 0.5
        return 0.2

    # Fallback
    return 0.5


def editorial_fit_score(event) -> float:
    """
    Heuristic "editorial fit" for InYourBones.live – 0–1.

    Things that help:
    - Strong venue tier (warfield/fox/fillmore/independent/arenas/stadiums/etc).
    - Genres you like (rock/alt/pop/electronic/hip-hop/etc).
    - Core cities (SF / Oakland / Berkeley).
    - Festivals / obviously photogenic shows.
    """
    venue_tier = get_venue_tier(event.get("venue"))
    genre_fit = get_genre_fit(event.get("genre_primary"))
    city = (event.get("city") or "").lower()
    name = (event.get("artist_primary") or event.get("name") or "").lower()

    score = 0.4  # base

    # Venue impact
    if venue_tier >= 0.9:
        score += 0.25
    elif venue_tier >= 0.8:
        score += 0.18
    elif venue_tier >= 0.7:
        score += 0.1

    # Genre impact
    if genre_fit >= 0.9:
        score += 0.25
    elif genre_fit >= 0.75:
        score += 0.15

    # Core city: SF/Oakland/Berkeley
    if city in ("san francisco", "oakland", "berkeley"):
        score += 0.05

    # Festivals / big productions
    if "festival" in name or "fest " in name or "fest" == name.strip():
        score += 0.1

    # Clamp
    return float(max(0.0, min(1.0, score)))


def score_event(event) -> float:
    """
    Compute a 0–1+ score combining:
    - venue tier
    - genre fit
    - editorial fit
    - promoter weight
    - date bonus for the given window

    This is intentionally simple & tweakable.
    """
    venue_tier = get_venue_tier(event.get("venue"))
    genre_fit = get_genre_fit(event.get("genre_primary"))
    d_bonus = date_bonus(event)
    promoter_w = get_promoter_weight(event.get("promoter_name"), event.get("venue"))
    ed_fit = editorial_fit_score(event)

    # Weighting – adjust as you like
    score = (
        0.30 * venue_tier +
        0.20 * genre_fit +
        0.20 * ed_fit +
        0.15 * promoter_w +
        0.15 * d_bonus
    )

    return float(round(score, 3))
