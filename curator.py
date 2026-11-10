import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# --- CONFIGURATION (TIER 1 PROFILE) ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
FEED_TAB_NAME = "Curator Feed"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00
CORE_STAKE = round(BASE_UNIT * 1.75, 2)       # $7.00 (1.75U)
SECONDARY_STAKE = round(BASE_UNIT * 0.75, 2)  # $3.00 (0.75U)

# ESPN Public Scoreboard Endpoints (Free, No API Key Required)
ESPN_LEAGUES = {
    "CFB": "football/college-football",
    "NFL": "football/nfl",
    "NBA": "basketball/nba",
    "NCAAM": "basketball/mens-college-basketball",
    "NHL": "hockey/nhl",
    "MLB": "baseball/mlb"
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
            "Sport / League",
            "Bet Type & Tier",
            "Curated Selections / Legs",
            "Target Odds",
            "Rec Stake ($)",
            "Allocation",
            "Status",
            "Strategy Rationale"
        ])
    return sheet

# --- 2. ESPN PUBLIC SCRAPER ---
def fetch_espn_events(league_path):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league_path}/scoreboard"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("events", [])
    except Exception as e:
        print(f"Error fetching {league_path}: {e}")
    return []

def parse_game_odds(competition):
    odds_list = competition.get("odds", [])
    if not odds_list:
        return None

    odds_data = odds_list[0]
    total = odds_data.get("overUnder")
    details = odds_data.get("details", "")

    home_ml = odds_data.get("homeTeamOdds", {}).get("moneyLine")
    away_ml = odds_data.get("awayTeamOdds", {}).get("moneyLine")

    return {
        "total": float(total) if total is not None else None,
        "details": details,
        "home_ml": home_ml,
        "away_ml": away_ml
    }

# --- 3. CURATION LOGIC ---
def curate_card():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    used_teams = set()
    core_candidates = []
    parlay_candidates = []

    for league_name, league_path in ESPN_LEAGUES.items():
        events = fetch_espn_events(league_path)

        for event in events:
            date_str = event.get("date")
            if not date_str:
                continue

            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if not (now <= event_time <= cutoff):
                continue

            comps
