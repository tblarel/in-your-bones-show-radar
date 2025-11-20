# 🎸 InYourBones Show Radar

AI-curated Bay Area music show recommendations, automatically updated weekly.

[![Update Radar](https://github.com/tblarel/in-your-bones-show-radar/actions/workflows/radar.yml/badge.svg)](https://github.com/tblarel/in-your-bones-show-radar/actions/workflows/radar.yml)

## 📡 Live RSS Feeds

**[View All Feeds →](https://tblarel.github.io/in-your-bones-show-radar/)**

### Recommended for Squarespace
- **Short-Term (New)**: `https://tblarel.github.io/in-your-bones-show-radar/output/radar_short_term_new.rss`
  - Only shows added this week (14-120 days out)
  - Perfect for weekly "What's New" widgets

### All Available Feeds
- **Short-Term Shows** (2 weeks - 4 months out)
  - [New RSS](https://tblarel.github.io/in-your-bones-show-radar/output/radar_short_term_new.rss) | [All RSS](https://tblarel.github.io/in-your-bones-show-radar/output/radar_short_term.rss)
  - [New JSON](https://tblarel.github.io/in-your-bones-show-radar/output/radar_short_term_new.json) | [All JSON](https://tblarel.github.io/in-your-bones-show-radar/output/radar_short_term.json)

- **Far-Out Shows** (4-12 months out)
  - [New RSS](https://tblarel.github.io/in-your-bones-show-radar/output/radar_far_out_new.rss) | [All RSS](https://tblarel.github.io/in-your-bones-show-radar/output/radar_far_out.rss)
  - [New JSON](https://tblarel.github.io/in-your-bones-show-radar/output/radar_far_out_new.json) | [All JSON](https://tblarel.github.io/in-your-bones-show-radar/output/radar_far_out.json)

## 🎯 What It Does

This system automatically finds and curates live music shows in the Bay Area that match InYourBones editorial priorities:

1. **Fetch** - Pulls upcoming shows from Ticketmaster Discovery API
2. **Score** - Applies heuristic scoring based on venue prestige, genre fit, and timing
3. **Refine** - Uses GPT-4o-mini to editorially curate the top candidates
4. **Track** - Maintains state to identify newly announced shows each week
5. **Export** - Generates RSS, JSON, CSV, and plain-text digests
6. **Publish** - Auto-commits to GitHub and serves via GitHub Pages

## 🏙️ Coverage Areas

- San Francisco
- Oakland
- Berkeley
- San Jose
- Santa Cruz
- Mountain View
- Santa Clara
- Napa
- Concord

## 📅 Update Schedule

**Every Monday at 7-8am Pacific** (15:00 UTC)
- Automated via GitHub Actions
- Can also be triggered manually from Actions tab

## 🔧 How It Works

### Architecture

```
┌─────────────────┐
│ Ticketmaster API│
└────────┬────────┘
         │ fetch_events.py
         ▼
┌─────────────────┐
│ Normalize Events│ (collapse multi-night shows)
└────────┬────────┘
         │ scoring.py
         ▼
┌─────────────────┐
│ Heuristic Score │ (venue + genre + timing)
└────────┬────────┘
         │ ai_filter.py
         ▼
┌─────────────────┐
│ AI Refinement   │ (GPT-4o-mini editorial curation)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Export Feeds    │ (RSS, JSON, CSV, digests)
└─────────────────┘
```

### Scoring Components

**Heuristic Score (0-1 scale)**
- `venue_weight` (0.75-1.0): Prestige of venue
  - Chase Center, Bill Graham: 1.0
  - Fillmore, Fox Theater: 0.92
  - Independent, Chapel: 0.85
- `genre_fit` (0.3-1.0): Music genre relevance
  - Pop/Rock/Indie/Alternative: 0.7-1.0
  - Comedy: 0.3 (deprioritized)
- `date_proximity_bonus` (-0.2 to +0.1): Timing incentive
  - ≤7 days: +0.10
  - ≤30 days: +0.08
  - Past events: -0.20

**AI Refinement**
- Takes top 200 heuristic candidates
- GPT-4o-mini applies editorial judgment based on:
  - Emerging/trending artists
  - Genre diversity
  - Special events (album releases, reunion shows)
  - Avoiding over-saturation of similar acts
- Returns top 20 with AI priority (1-10) and reasoning

### State Tracking

The system maintains `state/known_events.json` to track which events have been seen before:

```json
{
  "short_term": {
    "G5vYZbVivluSD": "2025-11-20",
    "vvG1zZp0k3kDdA|vvG1zZp0k3kDdB": "2025-11-20"
  },
  "far_out": { ... }
}
```

- **Single-night shows**: Tracked by Ticketmaster event ID
- **Multi-night runs**: Tracked by pipe-delimited sorted IDs
- **Automatic cleanup**: Removes events no longer in current window

### Multi-Night Collapsing

Consecutive shows by the same artist at the same venue are collapsed:
- "Ariana Grande" (Dec 18, 19, 20) → "Ariana Grande x3 nights"
- First/last dates shown in description
- Scoring uses average of all nights

## 🚀 Local Development

### Setup

```bash
# Clone the repo
git clone https://github.com/tblarel/in-your-bones-show-radar.git
cd in-your-bones-show-radar

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env with your API keys
```

### Environment Variables

```env
# Ticketmaster Discovery API
TM_API_KEY=your_ticketmaster_key

# OpenAI API (for AI refinement)
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-4o-mini  # optional, defaults to gpt-4o-mini

# Google Sheets API (optional, for sheet export)
GOOGLE_CREDS_JSON='{"type": "service_account", ...}'
```

### Run Locally

```bash
# Full run with AI refinement
python fetch_events.py

# Outputs to:
# - output/radar_short_term.rss
# - output/radar_short_term_new.rss
# - output/radar_short_term.json
# - output/radar_short_term_new.json
# - output/radar_short_term.csv
# - output/radar_short_term_digest.txt
# - output/radar_short_term_new_digest.txt
# (+ same for far_out window)
```

## 📦 Dependencies

```
requests          # HTTP client for Ticketmaster API
python-dateutil   # Date parsing and manipulation
gspread           # Google Sheets integration
google-auth       # Google authentication
PyYAML            # Config file parsing
openai            # GPT-4o-mini API client
python-dotenv     # Environment variable management
```

## 🔒 GitHub Secrets

For GitHub Actions automation, configure these secrets in repo settings:

- `TM_API_KEY` - Ticketmaster Discovery API key
- `OPENAI_API_KEY` - OpenAI API key
- `GOOGLE_CREDS_JSON` - Google service account credentials (as JSON string)

## 📊 Output Formats

### RSS 2.0
- Standard podcast/feed format
- Compatible with Squarespace, WordPress, feed readers
- Item title: "Artist @ Venue"
- Item description: Full event details + AI reasoning

### JSON
- Structured data with all fields
- Includes `ai_priority`, `ai_reason`, `is_new`, score components
- Suitable for custom integrations

### CSV
- Spreadsheet-compatible
- All events with scores and metadata
- Great for analysis or Google Sheets import

### Plain-Text Digest
- Human-readable summary
- Sorted by AI priority
- Includes show counts and window info

## 🎨 Customization

### Adjust Scoring Weights

Edit `scoring.py`:
```python
VENUE_WEIGHTS = {
    "Your Favorite Venue": 1.0,
    # ...
}

GENRE_KEYWORDS = {
    "your-preferred-genre": 1.0,
    # ...
}
```

### Modify AI Instructions

Edit `ai_filter.py`:
```python
SYSTEM_PROMPT = """
You are an editorial assistant for [Your Publication].

Your priorities:
- [Custom priority 1]
- [Custom priority 2]
...
"""
```

### Change Time Windows

Edit `fetch_events.py`:
```python
WINDOWS = {
    "custom_window": (30, 90),  # 30-90 days out
    # ...
}
```

## 📝 File Structure

```
in-your-bones-show-radar/
├── fetch_events.py       # Main orchestrator
├── scoring.py            # Heuristic scoring logic
├── ai_filter.py          # GPT-4o-mini refinement
├── sheets_client.py      # Google Sheets export (optional)
├── requirements.txt      # Python dependencies
├── .env                  # Local environment variables (not committed)
├── .gitignore
├── index.html            # GitHub Pages landing page
│
├── .github/
│   └── workflows/
│       └── radar.yml     # GitHub Actions automation
│
├── output/               # Generated feeds (committed by workflow)
│   ├── radar_short_term.rss
│   ├── radar_short_term_new.rss
│   ├── radar_short_term.json
│   ├── radar_short_term_new.json
│   ├── radar_short_term.csv
│   ├── radar_short_term_digest.txt
│   ├── radar_short_term_new_digest.txt
│   └── (same for far_out)
│
├── state/                # Event tracking state (committed by workflow)
│   └── known_events.json
│
└── .tm_cache/            # Ticketmaster API cache (not committed)
```

## 🤝 Contributing

This is a personal project for InYourBones.live, but feel free to fork and adapt for your own music publication or city!

## 📄 License

MIT License - feel free to use and modify for your own projects.

## 🔗 Links

- **Live Feeds**: https://tblarel.github.io/in-your-bones-show-radar/
- **InYourBones**: https://inyourbones.live/
- **Ticketmaster Discovery API**: https://developer.ticketmaster.com/
- **OpenAI API**: https://platform.openai.com/

---

Built with ❤️ for the Bay Area music scene
