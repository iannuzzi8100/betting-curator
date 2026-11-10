import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

# --- CONFIGURATION (TIER 1 PROFILE) ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
FEED_TAB_NAME = "Curator Feed"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00
CORE_STAKE = round(BASE_UNIT * 1.75, 2)       # $7.00 (1.75U)
SECONDARY_STAKE = round(BASE_UNIT * 0.75, 2)  # $3.00 (0.75U)

ESPN_LEAGUES = {
    "CFB": {"path": "football/college-football", "groups": "80"},
    "NFL": {"path": "football/nfl", "groups": None},
    "NBA": {"path": "basketball/nba", "groups": None},
    "NCAAM": {"path": "basketball/mens-college-basketball", "groups": "50"},
    "NHL": {"path": "hockey/nhl", "groups": None},
    "MLB": {"path": "baseball/mlb", "groups": None}
}

# --- 1. GOOGLE SHEETS CONNECTION ---
def get_curator_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if GCP_SA_KEY:
        service_account_info = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    
    client = gspread.authorize(creds)
    workbook = client.open(SHEET_NAME)

    try:
        sheet = workbook.worksheet(FEED_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=FEED_TAB_NAME, rows=500, cols=12)
        sheet.append_row([
            "Scraped Date",
            "Kickoff (UTC)",
            "Matchup",
            "Play Type",
            "Consensus Total",
            "Target Under",
            "Favorite",
            "Favorite ML",
            "Target Combined Odds",
            "Rec Stake ($)",
            "Status",
            "AI Deep Dive & Edge Rationale"
        ])
    return sheet

# --- 2. ESPN SLATE INGESTION ---
def fetch_espn_events_for_date(league_info, yyyymmdd):
    path = league_info["path"]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={yyyymmdd}"
    if league_info.get("groups"):
        url += f"&groups={league_info['groups']}"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("events", [])
    except Exception as e:
        print(f"Error fetching {path} for {yyyymmdd}: {e}", flush=True)
    return []

def extract_slate_candidates():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=36)
    dates_to_check = [now.strftime("%Y%m%d"), (now + timedelta(days=1)).strftime("%Y%m%d")]

    board = []

    for league_name, league_info in ESPN_LEAGUES.items():
        all_events = []
        for d in dates_to_check:
            all_events.extend(fetch_espn_events_for_date(league_info, d))

        seen_ids = set()
        for event in all_events:
            event_id = event.get("id")
            if not event_id or event_id in seen_ids:
                continue
            seen_ids.add(event_id)

            date_str = event.get("date")
            if not date_str:
                continue
            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if not (now <= event_time <= cutoff):
                continue

            comps = event.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home_comp = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_comp = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home_comp or not away_comp:
                continue

            home_team = home_comp.get("team", {}).get("displayName", "")
            away_team = away_comp.get("team", {}).get("displayName", "")
            matchup = f"{away_team} @ {home_team}"

            if any(kw in matchup.lower() for kw in EXCLUDED_KEYWORDS):
                continue

            odds_list = comp.get("odds", [])
            odds_info = {}
            if odds_list:
                o = odds_list[0]
                odds_info = {
                    "overUnder": o.get("overUnder"),
                    "details": o.get("details"),
                    "homeML": o.get("homeTeamOdds", {}).get("moneyLine"),
                    "awayML": o.get("awayTeamOdds", {}).get("moneyLine"),
                    "provider": o.get("provider", {}).get("name")
                }

            board.append({
                "league": league_name,
                "game": matchup,
                "home": home_team,
                "away": away_team,
                "start_utc": event_time.strftime("%Y-%m-%d %H:%M UTC"),
                "odds": odds_info
            })

    return board

# --- 3. GEMINI REASONING & RESEARCH ENGINE ---
def run_ai_curation(board):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are an expert sports quantitative analyst managing a strict bankroll challenge from $100 to $1,000.
Current Bankroll Tier: Tier 1 ($100 - $249.99).
Base Unit (1U): ${BASE_UNIT:.2f}

STRATEGY CONSTRAINTS:
1. Play 1 (Core Tier 1): Stake = ${CORE_STAKE:.2f} (1.75U). Target Odds: -135 to -110.
   - Primary high-probability bankroll builder.
   - Correlated SGP (Heavy ML Favorite -250 to -600 + padded alternate under +7 to +10 pts) OR an elite high-floor player prop / alt line with a proven 75%+ hit rate and dominant matchup advantage.
2. Play 2 (Multi-Window Tier 2): Stake = ${SECONDARY_STAKE:.2f} (0.75U). Target Odds: +160 to +250.
   - 2-to-3 leg parlay across different games or sports (can include moneylines or props).
3. ABSOLUTE EXCLUSION: Never bet on or against Kentucky/Wildcats.
4. Total Daily Risk: Max ${CORE_STAKE + SECONDARY_STAKE:.2f} across both plays combined.

Here is the upcoming 36-hour slate scraped from ESPN:
{json.dumps(board, indent=2)}

TASK:
Analyze the board. Leverage current team injuries, pace, defense vs position (DvP), and matchup trends.
Select the optimal Play 1 (Core Tier 1) and Play 2 (Tier 2 Parlay).

Respond ONLY with valid JSON matching this exact structure:
{{
  "plays": [
    {{
      "play_type": "Core Tier 1 (SGP)",
      "matchup": "Away @ Home",
      "kickoff": "YYYY-MM-DD HH:MM UTC",
      "consensus_total": "49.5",
      "target_under": "Under 56.5",
      "favorite": "Team Name",
      "favorite_ml": "-300",
      "target_combined_odds": "-120",
      "rec_stake": {CORE_STAKE},
      "deep_dive_analysis": "Comprehensive statistical reasoning, matchup trench analysis, pace, and hit rate justification."
    }},
    {{
      "play_type": "Multi-Window Tier 2 (Cross-Sport Parlay)",
      "matchup": "Multiple Slates",
      "kickoff": "YYYY-MM-DD HH:MM UTC",
      "consensus_total": "N/A",
      "target_under": "N/A",
      "favorite": "Leg 1 + Leg 2",
      "favorite_ml": "Multiple",
      "target_combined_odds": "+185",
      "rec_stake": {SECONDARY_STAKE},
      "deep_dive_analysis": "Detailed rationale for each leg, usage trends, and why this parlay holds an edge."
    }}
  ]
}}
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )

    try:
        return json.loads(response.text).get("plays", [])
    except Exception as e:
        print(f"Error parsing Gemini JSON response: {e}", flush=True)
        print("Raw model response:", response.text, flush=True)
        return []

# --- 4. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Starting Curator Engine...", flush=True)
    sheet = get_curator_sheet()

    print("Ingesting live slates from ESPN...", flush=True)
    board = extract_slate_candidates()
    print(f"Ingested {len(board)} upcoming matchups within 36-hour window.", flush=True)

    print("Submitting candidate board to Gemini for quantitative analysis...", flush=True)
    ai_plays = run_ai_curation(board)

    if not ai_plays:
        print("No qualifying plays finalized today.", flush=True)
    else:
        now_str = datetime.now().strftime("%Y-%m-%d")
        for p in ai_plays:
            print(f"Curated: [{p.get('play_type')}] {p.get('matchup')} | Stake: ${p.get('rec_stake')}", flush=True)
            sheet.append_row([
                now_str,
                p.get("kickoff", "TBD"),
                p.get("matchup", "N/A"),
                p.get("play_type", "Tier 1"),
                str(p.get("consensus_total", "N/A")),
                str(p.get("target_under", "N/A")),
                p.get("favorite", "N/A"),
                str(p.get("favorite_ml", "N/A")),
                p.get("target_combined_odds", "-110"),
                p.get("rec_stake", CORE_STAKE),
                "UNEXECUTED",
                p.get("deep_dive_analysis", "")
            ])
        print("Deep dive card successfully logged to Google Sheets.", flush=True)
