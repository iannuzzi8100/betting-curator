import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
FEED_TAB_NAME = "Curator Feed"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00  # Tier 1 ($100-$249)

# --- 1. SPREADSHEET CONNECTION & TAB INITIALIZATION ---
def get_curator_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if GCP_SA_KEY:
        service_account_info = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    
    client = gspread.authorize(creds)
    workbook = client.open(SHEET_NAME)

    # Get or automatically create the Curator Feed worksheet
    try:
        sheet = workbook.worksheet(FEED_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=FEED_TAB_NAME, rows=500, cols=12)
        # Establish header row
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
            "Status"
        ])
    return sheet

# --- 2. ODDS INGESTION ---
def fetch_upcoming_odds(sport="americanfootball_ncaaf"):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,totals",
        "oddsFormat": "american"
    }
    response = requests.get(url, params=params)
    if response.status_code != 200:
        print(f"Failed to fetch odds: {response.text}")
        return []
    return response.json()

# --- 3. FILTERING & CURATION ---
def curate_todays_core_play(games):
    candidate_plays = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    for game in games:
        commence_time = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        if not (now <= commence_time <= cutoff):
            continue

        home = game.get("home_team", "")
        away = game.get("away_team", "")
        matchup_str = f"{away} @ {home}".lower()

        if any(keyword in matchup_str for keyword in EXCLUDED_KEYWORDS):
            continue

        bookmakers = game.get("bookmakers", [])
        if not bookmakers:
            continue

        markets = bookmakers[0].get("markets", [])
        h2h = next((m for m in markets if m["key"] == "h2h"), None)
        totals = next((m for m in markets if m["key"] == "totals"), None)

        if not h2h or not totals:
            continue

        favorite = None
        fav_price = None
        for outcome in h2h["outcomes"]:
            price = outcome["price"]
            if -600 <= price <= -250:
                favorite = outcome["name"]
                fav_price = price
                break

        if not favorite:
            continue

        consensus_total = totals["outcomes"][0]["point"]
        padded_under = consensus_total + 7.0

        candidate_plays.append({
            "scraped_date": datetime.now().strftime("%Y-%m-%d"),
            "kickoff": commence_time.strftime("%Y-%m-%d %H:%M UTC"),
            "game": f"{away} @ {home}",
            "type": "Core Tier A (SGP)",
            "consensus_total": consensus_total,
            "padded_under": padded_under,
            "favorite": favorite,
            "fav_price": fav_price,
            "target_odds": "-130 to -115",
            "wager": round(BASE_UNIT * 1.75, 2),
            "status": "UNEXECUTED"
        })

    return candidate_plays[:1]

# --- 4. EXECUTION ---
if __name__ == "__main__":
    sheet = get_curator_sheet()
    games = fetch_upcoming_odds("americanfootball_ncaaf")
    recommendations = curate_todays_core_play(games)

    if not recommendations:
        print("No qualifying plays found in the next 24 hours.")
    else:
        for rec in recommendations:
            print(f"Curated: {rec['game']} | {rec['favorite']} ML + Under {rec['padded_under']}")
            sheet.append_row([
                rec["scraped_date"],
                rec["kickoff"],
                rec["game"],
                rec["type"],
                rec["consensus_total"],
                rec["padded_under"],
                rec["favorite"],
                rec["fav_price"],
                rec["target_odds"],
                rec["wager"],
                rec["status"]
            ])
            print(f"Logged to '{FEED_TAB_NAME}' tab successfully.")
