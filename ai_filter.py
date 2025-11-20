"""
ai_filter.py

LLM-powered refinement of scored Ticketmaster events for InYourBones.
Takes a list of pre-scored events and:
  - applies an "editorial fit" judgment
  - optionally re-prioritizes them
  - returns a smaller, ordered subset with inline AI metadata
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI


def _get_openai_client() -> OpenAI | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("⚠  OPENAI_API_KEY not set, skipping AI refinement.")
        return None
    return OpenAI(api_key=api_key)


def _summarize_event_for_ai(ev: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strip the event down to the essentials so the model can reason
    without us blowing through context.
    """
    return {
        "id": ev.get("id"),
        "name": ev.get("name"),
        "primary_artist": ev.get("primary_artist") or ev.get("name"),
        "venue_name": ev.get("venue_name"),
        "city": ev.get("city"),
        "state": ev.get("state"),
        "country": ev.get("country"),
        "local_date": ev.get("local_date"),
        "window": ev.get("window"),
        "score": round(float(ev.get("score", 0.0)), 3),
    }


SYSTEM_PROMPT = """
You are the lead editor for a Bay Area live-music publication called "InYourBones".
Your job is to curate the most editorially interesting shows from a pre-scored list
of Ticketmaster events in the San Francisco Bay Area.

Editorial priorities (rough):
- Strongest interest: indie / alt / pop / rock / electronic / hip-hop acts with a story,
  buzz, or strong live reputation.
- Medium interest: legacy or nostalgia acts, mid-tier touring artists, niche but cool genres.
- Lower interest: tribute bands, generic cover bands, casino / cruise-ship-style shows,
  very small bar gigs with no clear hook.
- Ignore anything that is clearly *not* a music event (e.g. pure comedy, sports).

You will be given:
- a time window label (e.g. "short_term" or "far_out")
- a list of candidate events, already scored from 0–1 by a heuristic
- a desired top_k size

You must return a JSON object of the form:
{
  "selections": [
    {
      "id": "<Ticketmaster event id>",
      "keep": true,
      "priority": <integer 1-10>,
      "reason": "<1-2 sentence editorial justification>"
    },
    ...
  ]
}

Rules:
- ALWAYS respond with valid JSON matching the schema above. No extra keys, no comments.
- The selections array MUST be sorted from highest to lowest priority.
- "priority" should roughly reflect both the heuristics score and editorial excitement.
- You MAY drop events entirely by setting "keep": false or by omitting them,
  but the caller will cap the final list at top_k anyway.
- Favor a varied mix of genres and venues when possible, not 20 shows at the same arena.
- Keep reasons short, concrete, and specific (no generic "great show" fluff).
""".strip()


def refine_top_events_with_ai(
    events: List[Dict[str, Any]],
    window_label: str,
    top_k: int = 20,
    max_items: int = 200,
    debug: bool = False,
) -> List[Dict[str, Any]]:
    """
    Given a list of normalized + scored events for a single window,
    call the LLM to re-rank and optionally filter them.

    Returns a list of events (subset of the input) with added fields:
        - ai_priority: int | None
        - ai_reason: str | None
    """
    if not events:
        return []

    client = _get_openai_client()
    if client is None:
        # No key: just fall back to heuristic top_k
        return events[:top_k]

    # Truncate for context safety
    slice_events = events[: max_items]
    payload = {
        "window": window_label,
        "top_k": top_k,
        "candidates": [_summarize_event_for_ai(e) for e in slice_events],
    }

    if debug:
        print(f"→ Sending {len(slice_events)} events to AI for window='{window_label}', top_k={top_k}")

    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            temperature=0.3,
            timeout=60.0,  # 60 second timeout
        )
        raw = resp.choices[0].message.content
        if debug:
            print("✓ AI response received.")
    except Exception as e:
        print(f"⚠ AI refinement failed: {e}")
        print(f"  Falling back to top {top_k} by heuristic score only")
        return events[:top_k]

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"⚠ Failed to parse AI JSON: {e}")
        if debug:
            print("⚠ Raw AI response content:")
            print(raw)
        return events[:top_k]

    selections = data.get("selections") or []
    if not isinstance(selections, list):
        print("⚠ AI JSON missing 'selections' list, falling back.")
        return events[:top_k]

    # Build lookup from event id -> original event
    by_id: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        eid = ev.get("id")
        if eid:
            by_id[eid] = ev

    chosen: List[Dict[str, Any]] = []
    seen_ids = set()

    for item in selections:
        if not isinstance(item, dict):
            continue
        eid = item.get("id")
        if not eid or eid in seen_ids:
            continue
        keep = item.get("keep", True)
        if not keep:
            continue
        src = by_id.get(eid)
        if not src:
            continue
        ev = dict(src)  # shallow copy
        ev["ai_priority"] = item.get("priority")
        ev["ai_reason"] = item.get("reason")
        chosen.append(ev)
        seen_ids.add(eid)
        if len(chosen) >= top_k:
            break

    # If AI returned too few, top up with heuristic-ordered leftovers
    if len(chosen) < top_k:
        already = set(seen_ids)
        for ev in events:
            eid = ev.get("id")
            if not eid or eid in already:
                continue
            chosen.append(ev)
            already.add(eid)
            if len(chosen) >= top_k:
                break

    # Ensure stable ordering: sort by (ai_priority desc, score desc)
    def sort_key(ev: Dict[str, Any]):
        return (
            ev.get("ai_priority") or 0,
            ev.get("score") or 0.0,
        )

    chosen.sort(key=sort_key, reverse=True)
    return chosen[:top_k]
