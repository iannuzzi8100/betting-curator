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
            slot = "Window 3: Primetime (6:30 PM - 9:29 PM EST)"
        else:
            slot = "Window 4: Late Night (9:30 PM+ EST)"
        return est_str, slot
    except Exception:
        return iso_date_str, "Unclassified Window"

def extract_records(competitor):
    recs = {}
    for r in competitor.get("records", []):
        name = r.get("name") or r.get("type") or "overall"
        recs[name] = r.get("summary", "")
    return recs

def extract_stats(competitor):
    stats = {}
    for s in competitor.get("statistics", []):
        name = s.get("abbreviation") or s.get("name")
        val = s.get("displayValue")
        if name and val:
            stats[name] = val
    return stats

def fetch_daily_slate():
    slate = []
    today_est = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")

    for league, base_url in ESPN_ENDPOINTS.items():
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}dates={today_est}"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                continue
            events = res.json().get("events", [])
        except Exception as e:
            print(f"Error fetching {league} slate: {e}", flush=True)
            continue

        for event in events:
            status_state = event.get("status", {}).get("type", {}).get("state", "")
            if status_state != "pre":
                continue

            comps = event.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            odds_list = comp.get("odds", [])
            odds_info = odds_list[0] if odds_list else {}

            broadcasts = []
            for b in comp.get("broadcasts", []):
                broadcasts.extend(b.get("names", []))

            est_time_str, time_window = classify_est_window(event.get("date", ""))

            slate.append({
                "league": league,
                "kickoff_utc": event.get("date", ""),
                "kickoff_est": est_time_str,
                "time_window": time_window,
                "tv_broadcast": ", ".join(broadcasts) if broadcasts else "Streaming",
                "matchup": f"{away.get('team', {}).get('displayName', '')} @ {home.get('team', {}).get('displayName', '')}",
                "home_team": home.get("team", {}).get("displayName", ""),
                "home_rank": home.get("curatedRank", {}).get("current", 99),
                "home_records": extract_records(home),
                "home_stats": extract_stats(home),
                "away_team": away.get("team", {}).get("displayName", ""),
                "away_rank": away.get("curatedRank", {}).get("current", 99),
                "away_records": extract_records(away),
                "away_stats": extract_stats(away),
                "spread_details": odds_info.get("details", "N/A"),
                "over_under": odds_info.get("overUnder", "N/A"),
                "home_ml": odds_info.get("homeTeamOdds", {}).get("moneyLine", "N/A"),
                "away_ml": odds_info.get("awayTeamOdds", {}).get("moneyLine", "N/A"),
                "draw_ml": odds_info.get("drawOdds", {}).get("moneyLine", "N/A")
            })

    return slate

# --- 3. ROBUST JSON EXTRACTOR ---
def extract_json_payload(raw_text):
    text = raw_text.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        if blocks:
            text = blocks[0]
