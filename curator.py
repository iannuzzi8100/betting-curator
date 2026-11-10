import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# --- CONFIGURATION VIA ENVIRONMENT VARIABLES ---
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00  # Tier 1 ($100-$249)

# --- 1. SPREADSHEET CONNECTION ---
def get_tracker_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if GCP_SA_KEY:
        service_account_info = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1

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
        matchup_str = f"{away} vs {home}".lower()

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
            "game": f"{away} @ {home}",
            "type": "Core Tier A (SGP)",
            "leg1": f"{favorite} ML ({fav_price})",
            "leg2": f"Padded Under {padded_under}",
            "wager": round(BASE_UNIT * 1.75, 2),
            "target_odds": "-130 to -115"
        })

    return candidate_plays[:1]

# --- 4. EXECUTION ---
if __name__ == "__main__":
    sheet = get_tracker_sheet()
    games = fetch_upcoming_odds("americanfootball_ncaaf")
    recommendations = curate_todays_core_play(games)

    if not recommendations:
        print("No qualifying plays found in the next 24 hours.")
    else:
        for rec in recommendations:
            print(f"Curated Play: {rec['game']} | {rec['leg1']} + {rec['leg2']} | Stake: ${rec['wager']}")
            sheet.append_row([
                datetime.now().strftime("%Y-%m-%d"),
                rec["game"],
                rec["type"],
                f"{rec['leg1']} / {rec['leg2']}",
                rec["target_odds"],
                rec["wager"],
                "PENDING"
            ])
            print("Successfully logged to Google Sheet!")