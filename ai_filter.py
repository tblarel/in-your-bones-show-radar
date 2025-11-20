# ai_filter.py
import os
import json
from typing import List, Dict

from openai import OpenAI

# You can change the model if you like
OPENAI_MODEL = "gpt-4o-mini"


def _get_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def refine_top_events_with_ai(events: List[Dict], window_name: str, top_k: int = 20) -> List[Dict]:
    """
    Given a list of already-scored events (dicts with keys like
    id, name, artist_primary, venue, city, date, genre_primary, score, venue_tier,
    editorial_fit, promoter_weight, press_eligible, multi_day, etc.),
    ask an LLM to pick the best subset for InYourBones.

    If OPENAI_API_KEY is missing or anything fails, just return the first top_k events.
    """
    client = _get_client()
    if client is None:
        print("⚠ No OPENAI_API_KEY set; skipping AI refinement and returning first top_k.")
        return events[:top_k]

    # Prepare compact JSON for the model
    serializable_events = []
    for e in events:
        serializable_events.append({
            "id": e.get("id"),
            "artist": e.get("artist_primary") or e.get("name"),
            "event_name": e.get("name"),
            "venue": e.get("venue"),
            "city": e.get("city"),
            "date": e.get("date").strftime("%Y-%m-%d %H:%M") if e.get("date") else None,
            "date_first": e.get("date_first").strftime("%Y-%m-%d %H:%M") if e.get("date_first") else None,
            "date_last": e.get("date_last").strftime("%Y-%m-%d %H:%M") if e.get("date_last") else None,
            "num_dates": e.get("num_dates", 1),
            "multi_day": e.get("multi_day", False),
            "genre": e.get("genre_primary"),
            "score": e.get("score"),
            "venue_tier": e.get("venue_tier"),
            "genre_fit": e.get("genre_fit"),
            "promoter": e.get("promoter_name"),
            "promoter_weight": e.get("promoter_weight"),
            "editorial_fit": e.get("editorial_fit"),
            "press_eligible": e.get("press_eligible", None),
            "window": e.get("window"),
        })

    events_json = json.dumps(serializable_events, ensure_ascii=False)

    system_msg = (
        "You are helping a small independent online music publication called InYourBones.live "
        "decide which shows to cover in the San Francisco Bay Area.\n\n"
        "They mainly care about:\n"
        "- Touring or notable artists (indie, rock, alternative, pop, electronic, hip-hop, reggae, etc.).\n"
        "- Recognizable or exciting support acts.\n"
        "- Solid venues (The Warfield, Fox Oakland, Fillmore, Independent, Greek, Bill Graham, Chase Center, etc.).\n"
        "- Shows that will be visually interesting and relevant for live photo galleries / reviews.\n\n"
        "You are given some useful numeric heuristics:\n"
        "- `score`: overall heuristic score (0–1+) based on venue, genre, editorial fit, promoter, and timing.\n"
        "- `venue_tier`: 0–1 (arena/stadium/major theatre near 1.0).\n"
        "- `genre_fit`: 0–1 (how aligned the genre is with the publication).\n"
        "- `editorial_fit`: 0–1 (how good a fit the event is for IYB editorially).\n"
        "- `promoter_weight`: 0–1 (Goldenvoice/Live Nation/Another Planet etc. get higher values).\n"
        "- `press_eligible`: boolean when True, it’s more likely the site can realistically get photo passes.\n"
        "- `multi_day` and `num_dates`: true/count when the artist is doing a multi-night run at the same venue.\n\n"
        "Heuristics should strongly influence you, but you can override them if the metadata suggests "
        "a show is obviously more/less interesting than the raw score implies.\n\n"
        "They care less about:\n"
        "- Generic bar nights, local DJ or open mic nights, tribute bands unless it's a big production.\n"
        "- Very small/local acts with little broader appeal unless the show itself is special.\n\n"
        "Your job is to look at the candidate events and select the best subset "
        f"(around {top_k} events) that are the most worth covering for the site in this planning window.\n"
        "Prefer:\n"
        "- High `score` and `editorial_fit`.\n"
        "- High `venue_tier` and `promoter_weight`.\n"
        "- `press_eligible = true` (only rarely pick non-eligible if it’s huge or uniquely interesting).\n"
        "- Multi-night runs can be treated as a single strong opportunity if other factors look good."
    )

    # IMPORTANT: ask for a JSON OBJECT with `items`, so we can use response_format=json_object safely
    user_msg = (
        f"Planning window: {window_name}.\n\n"
        "Here is a JSON array of candidate events, already pre-scored by another heuristic:\n\n"
        f"{events_json}\n\n"
        "Return STRICTLY a JSON object with a single key `items`, whose value is an array of objects.\n"
        "Each item in `items` must have exactly these keys:\n"
        '{ "id": string, "keep": boolean, "priority": number, "reason": string }.\n\n'
        f"- Mark only the best ~{top_k} events as keep=true.\n"
        "- `priority` should be 1–10 (10 = absolute must-cover headliner, 7–9 = strong candidate, 5–6 = maybe).\n"
        "- `reason` should briefly explain why it is or isn't a good fit (headliner relevance, venue, genre, "
        "promoter, press eligibility, multi-night run, etc.).\n"
        "- Include ALL input events in `items`, but only mark the best set as keep=true."
    )

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},  # force valid JSON
        )
        content = resp.choices[0].message.content
        # content is guaranteed to be valid JSON representing an object
        parsed = json.loads(content)
        data = parsed.get("items", [])
        if not isinstance(data, list):
            raise ValueError("AI response 'items' is not a list")
    except Exception as e:
        print("⚠ AI refinement failed:", e)
        preview = locals().get("content", "")
        if isinstance(preview, str):
            print("⚠ Raw AI response content (first 500 chars):", preview[:500])
        print("⚠ Falling back to first top_k.")
        return events[:top_k]

    # Build a map id -> (keep, priority, reason)
    decisions = {}
    for item in data:
        _id = item.get("id")
        if not _id:
            continue
        decisions[_id] = {
            "keep": bool(item.get("keep", False)),
            "priority": float(item.get("priority", 0)),
            "reason": item.get("reason", ""),
        }

    # Apply decisions: ONLY keep those AI explicitly marked keep=True
    kept = []
    for e in events:
        d = decisions.get(e.get("id"))
        if not d or not d["keep"]:
            continue
        e = e.copy()
        e["ai_priority"] = d["priority"]
        e["ai_reason"] = d["reason"]
        kept.append(e)

    # Sort kept events by ai_priority then original score
    kept = sorted(
        kept,
        key=lambda x: (x.get("ai_priority", 0), x.get("score", 0)),
        reverse=True,
    )

    # Cap at top_k if AI returned more
    if len(kept) > top_k:
        kept = kept[:top_k]

    # If AI was too stingy and kept nothing, fall back to original top_k
    if not kept:
        print("⚠ AI returned no keep=true events; falling back to first top_k.")
        return events[:top_k]

    return kept
