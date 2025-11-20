# sheets_client.py
import os
import json
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["SHEET_ID"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_CREDS_JSON"])

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet():
    creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1  # or worksheet("Radar")

def upsert_events(events):
    sheet = get_sheet()
    rows = sheet.get_all_records()
    existing_index = {(r["source_event_id"], r["window"]): i+2 for i, r in enumerate(rows)}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    header = [
        "source_event_id", "source", "event_name", "artist_primary", "artist_all",
        "venue", "city", "date", "weekday", "is_weekend", "genre_primary",
        "onsale_start", "tm_popularity_raw", "venue_tier", "genre_fit",
        "score_total", "window", "press_contact_name", "press_contact_url",
        "press_contact_email", "recommended", "last_seen",
    ]
    if not rows:
        sheet.insert_row(header, 1)

    for e in events:
        key = (e["source_event_id"], e["window"])
        recommended = "TRUE" if e["score_total"] >= 0.8 else "FALSE"
        row = [
            e["source_event_id"],
            e["source"],
            e["event_name"],
            e["artist_primary"],
            e["artist_all"],
            e["venue"],
            e["city"],
            e["date"].isoformat() if e["date"] else "",
            e["weekday"],
            e["is_weekend"],
            e["genre_primary"],
            e["onsale_start"],
            e["tm_popularity_raw"],
            e["venue_tier"],
            e["genre_fit"],
            e["score_total"],
            e["window"],
            e["press_contact_name"],
            e["press_contact_url"],
            e["press_contact_email"],
            recommended,
            now,
        ]

        if key in existing_index:
            sheet.update(f"A{existing_index[key]}:W{existing_index[key]}", [row])
        else:
            sheet.append_row(row, value_input_option="RAW")
