import os
import json
import time
import requests
import gspread
from datetime import datetime, timezone
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
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
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

    return {
        "current_bankroll": current_bankroll,
        "pending_bets_remaining": pending_count,
        "next_bet_ids": [f"BET-{next_bet_num:03d}", f"BET-{next_bet_num + 1:03d}"],
        "unit_tier_rules": rules_rows
    }

# --- 2. SCRAPE TODAY'S LIVE SLATE FROM ESPN ---
def fetch_daily_slate():
    slate = []
    today_utc = datetime.now(timezone.utc).strftime("%Y%m%d")

    for league, base_url in ESPN_ENDPOINTS.items():
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}dates={today_utc}"
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

            slate.append({
                "league": league,
                "kickoff_utc": event.get("date", ""),
                "matchup": f"{away.get('team', {}).get('displayName', '')} @ {home.get('team', {}).get('displayName', '')}",
                "home_team": home.get("team", {}).get("displayName", ""),
                "home_record": home.get("records", [{}])[0].get("summary", "") if home.get("records") else "",
                "away_team": away.get("team", {}).get("displayName", ""),
                "away_record": away.get("records", [{}])[0].get("summary", "") if away.get("records") else "",
                "spread_details": odds_info.get("details", "N/A"),
                "over_under": odds_info.get("overUnder", "N/A"),
                "home_ml": odds_info.get("homeTeamOdds", {}).get("moneyLine", "N/A"),
                "away_ml": odds_info.get("awayTeamOdds", {}).get("moneyLine", "N/A")
            })

    return slate

# --- 3. CURATE TODAY'S BETS (STRICTLY POST-SETTLEMENT & POST-SCRAPE) ---
def run_ai_curation(slate, context):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are the quantitative sports betting curator for a $100-to-$1,000 challenge.
Yesterday's bets have just been settled by settler.py, and today's live ESPN slate has just been scraped.

USER SYSTEM & PREFERENCES PROFILE:
1. Active Bankroll: ${context['current_bankroll']:.2f} (Next Bet IDs: {context['next_bet_ids'][0]} and {context['next_bet_ids'][1]}).
2. Tier & Unit Rules: Follow 'unit_tier_rules' strictly. In Tier 1 ($100-$249.99 / sub-$100 foundation), 1U = $4.00, Core Play = 1.75U ($7.00), Secondary Multi-Window Play = 0.75U ($3.00), Max Daily Exposure = $10.00-$12.00.
3. 30-Day Baseline Data Collection Mode: Do NOT apply arbitrary historical trend bans or home/road biases. Evaluate every game objectively using current season records, matchup efficiency, pace, and line value.
4. Bet Structure Preferences:
   - Play 1 (Core Tier 1 - $7.00): 2-leg Correlated SGP (Favorite ML + Alternate Total Under with +6.0 to +7.5 points of cushion above the consensus total) OR a high-conviction straight play (target combined odds -190 to -115).
   - Play 2 (Multi-Window Tier 2 - $3.00): 2-to-3 leg floor parlay across NBA, NCAAM, CFB, NFL, or NHL (target combined odds +120 to +240) with ZERO team overlap with Play 1 (Rule 3: No Team Stacking).
5. Today's Slate Only: Only select games from TODAY'S LIVE ESPN SLATE below.

POST-SETTLEMENT CONTEXT:
{json.dumps(context, indent=2)}

TODAY'S LIVE ESPN SLATE:
{json.dumps(slate, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "recommendations": [
    {{
      "bet_id": "{context['next_bet_ids'][0]}",
      "kickoff_utc": "2026-11-14 02:00 UTC",
      "matchup": "Away Team @ Home Team",
      "play_type": "Core Tier 1 (SGP)",
      "consensus_total": "48.5",
      "target_under": "Under 55.5",
      "favorite": "Favorite Team ML (-380) + Alt Under 55.5",
      "favorite_ml": "-380",
      "target_combined_odds": "-145",
      "rec_stake": 7,
      "rationale": "Objective matchup, pace, and line cushion breakdown."
    }}
  ]
}}
"""

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash"
    ]

    response = None
    for attempt, model_name in enumerate(models_to_try, start=1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2
                )
            )
            break
        except errors.ServerError as e:
            print(f"Attempt {attempt} ({model_name}) hit 503 overload: {e}. Retrying in 15s...", flush=True)
            time.sleep(15)

    if response is None:
        raise RuntimeError("All Gemini model attempts failed due to 503 overload.")

    return json.loads(response.text).get("recommendations", [])

# --- 4. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Step 2: Starting Post-Settlement Daily Curation Engine...", flush=True)
    workbook = get_workbook()
    context = load_post_settlement_context(workbook)
    print(f"Verified Post-Settlement Bankroll: ${context['current_bankroll']:.2f} | Pending Bets Left: {context['pending_bets_remaining']}", flush=True)

    slate = fetch_daily_slate()
    print(f"Scraped {len(slate)} upcoming matchups for today from ESPN.", flush=True)

    if not slate:
        print("No pre-game matchups found on ESPN for today's date. Exiting.", flush=True)
        exit(0)

    recommendations = run_ai_curation(slate, context)
    print(f"Generated {len(recommendations)} curated plays for today.", flush=True)

    feed_ws = workbook.worksheet("Curator Feed")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows_to_append = []
    for rec in recommendations:
        rows_to_append.append([
            today_str,
            rec.get("kickoff_utc", ""),
            rec.get("matchup", ""),
            f"{rec.get('bet_id', '')} | {rec.get('play_type', '')}".strip(" |"),
            rec.get("consensus_total", "N/A"),
            rec.get("target_under", "N/A"),
            rec.get("favorite", ""),
            rec.get("favorite_ml", ""),
            rec.get("target_combined_odds", ""),
            rec.get("rec_stake", 7),
            "UNEXECUTED",
            rec.get("rationale", "")
        ])

    if rows_to_append:
        feed_ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"Successfully appended {len(rows_to_append)} row(s) to Curator Feed.", flush=True)
