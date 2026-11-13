import os
import re
import json
import time
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types, errors

# --- CONFIGURATION ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"

ESPN_ENDPOINTS = {
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "LALIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    "SERIEA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
    "BUNDESLIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
    "UCL": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"
}

# --- 1. GOOGLE SHEETS CONNECTION & POST-SETTLEMENT LOADER ---
def get_workbook():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if GCP_SA_KEY:
        service_account_info = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)

    client = gspread.authorize(creds)
    return client.open(SHEET_NAME)

def load_post_settlement_context(workbook):
    tracker_ws = workbook.worksheet("Challenge Tracker ($100-$1k)")
    tracker_rows = tracker_ws.get_all_values()

    current_bankroll = 100.0
    next_bet_num = 1
    pending_count = 0

    for row in tracker_rows:
        if len(row) >= 14:
            bet_id = row[0].strip()
            has_date = bool(row[1].strip())
            result_status = row[10].strip() if len(row) > 10 else ""

            if (bet_id == "START" or bet_id.startswith("BET-")) and has_date:
                if row[13].strip():
                    try:
                        current_bankroll = float(row[13].replace("$", "").replace(",", ""))
                    except ValueError:
                        pass
                if result_status.lower() == "pending":
                    pending_count += 1
                if bet_id.startswith("BET-"):
                    try:
                        num = int(bet_id.split("-")[1])
                        next_bet_num = max(next_bet_num, num + 1)
                    except ValueError:
                        pass

    try:
        rules_ws = workbook.worksheet("Unit & Tier Rules")
        rules_rows = [r for r in rules_ws.get_all_values() if any(cell.strip() for cell in r)]
    except Exception as e:
        print(f"Warning: Could not read Unit & Tier Rules tab: {e}", flush=True)
        rules_rows = []

    max_active_budget = round(current_bankroll * 0.50, 2)

    return {
        "current_bankroll": current_bankroll,
        "max_active_exposure_50pct": max_active_budget,
        "pending_bets_remaining": pending_count,
        "next_bet_num": next_bet_num,
        "unit_tier_rules": rules_rows
    }

# --- 2. FAST SINGLE-PASS ESPN SLATE SCRAPER (WITH EST TIME SLOTS & SPLITS) ---
def classify_est_window(iso_date_str):
    try:
        dt_utc = datetime.fromisoformat(iso_date_str.replace("Z", "+00:00"))
        dt_est = dt_utc.astimezone(ZoneInfo("America/New_York"))
        est_str = dt_est.strftime("%Y-%m-%d %I:%M %p EST")
        hour = dt_est.hour + (dt_est.minute / 60.0)
        if hour < 15.0:
            slot = "Window 1: Early / Noon (11:00 AM - 2:59 PM EST)"
        elif hour < 18.5:
            slot = "Window 2: Afternoon (3:00 PM - 6:29 PM EST)"
        elif hour < 21.5:
            slot = "Window 3: Primetime (6:30 PM -
