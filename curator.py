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

# --- 1. GOOGLE SHEETS CONNECTION & CONTEXT LOADER ---
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

def load_strategy_context(workbook):
    # 1. Read Current Bankroll & Recent Bets
    tracker_ws = workbook.worksheet("Challenge Tracker ($100-$1k)")
    tracker_rows = tracker_ws.get_all_values()

    current_bankroll = 100.0
    recent_bets = []
    for row in tracker_rows:
        if len(row) >= 14 and row[0].strip() in ["START"] or (len(row) > 0 and row[0].strip().startswith("BET-")):
            if row[1].strip():  # Has a date
                try:
                    if row[13].strip():
                        current_bankroll = float(row[13].replace("$", "").replace(",", ""))
                except ValueError:
                    pass
                recent_bets.append({
                    "bet_id": row[0],
                    "date": row[1],
                    "sport": row[2],
                    "type": row[3],
                    "selections": row[5],
                    "odds": row[6],
                    "stake": row[8],
                    "result": row[10],
                    "edge_tag": row[15] if len(row) > 15 else ""
                })

    # 2. Read Strategy Edge & Mistake Audit
    try:
        audit_ws = workbook.worksheet("Strategy Edge & Mistake Audit")
        audit_rows = [r for r in audit_ws.get_all_values() if any(cell.strip() for cell in r)]
    except Exception as e:
        print(f"Warning: Could not read Strategy Edge & Mistake Audit tab: {e}", flush=True)
        audit_rows = []

    # 3. Read Unit & Tier Rules
    try:
        rules_ws = workbook.worksheet("Unit & Tier Rules")
        rules_rows = [r for r in rules_ws.get_all_values() if any(cell.strip() for cell in r)]
    except Exception as e:
        print(f"Warning: Could not read Unit & Tier Rules tab: {e}", flush=True)
        rules_rows = []

    return {
        "current_bankroll": current_bankroll,
        "recent_bets": recent_bets[-10:],
        "edge_and_mistake_audit": audit_rows,
        "unit_tier_rules": rules_rows
    }

# --- 2. SCRAPE LIVE SLATE & ODDS FROM ESPN ---
def fetch_daily_slate():
    slate = []
    for league, url in ESPN_ENDPOINTS.items():
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

# --- 3. CURATE PLAYS USING FEEDBACK LOOP ---
def run_ai_curation(slate, strategy_context):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are a disciplined sports betting quantitative curator managing a $100-to-$1,000 bankroll challenge.
Analyze today's ESPN slate using the user's live bankroll, Unit & Tier Rules, and Strategy Edge & Mistake Audit.

FEEDBACK LOOP MANDATES:
1. DOUBLE DOWN ON WHAT WORKS: Inspect 'edge_and_mistake_audit' for 'WIN:' patterns with positive ROI and prioritize setups matching those exact game scripts (e.g., Correlated Home Favorite + Alt Total Cushion, Fatigue / Back-to-Back Fade, Trench / Pace Mismatch).
2. ELIMINATE REPEAT MISTAKES: Inspect 'edge_and_mistake_audit' for 'FAIL:' patterns and enforce their Guardrail Adjustment Directives:
   - Enforce a minimum +6.0 to +7.5 point alternate total cushion on any SGP Under; reject high-pace blowout risks.
   - Ban road favorites worse than -150 in multi-window parlays unless verified rest/venue edge exists.
   - Never stack high-variance garbage-time spreads or unbuffered totals.
   - Never stack the same team across multiple recommended tickets (Rule 3: No Team Stacking).
3. SIZING & TIER COMPLIANCE:
   - Current Bankroll: ${strategy_context['current_bankroll']:.2f} (Tier 1: 1U = $4.00, Core Tier 1 = $7.00 [1.75U], Multi-Window Tier 2 = $3.00 [0.75U], Max Daily Volume = $10.00-$12.00).
   - Output 2 high-conviction plays:
     * Play 1: Core Tier 1 ($7.00 stake) — e.g., Correlated Home Favorite ML + Padded Alt Under SGP (target combined odds -190 to -115) or high-edge straight play.
     * Play 2: Multi-Window Tier 2 ($3.00 stake) — 2-to-3 leg cross-sport/cross-window floor parlay (target combined odds +120 to +240) built strictly from home anchors or verified rest mismatches.

STRATEGY & AUDIT CONTEXT:
{json.dumps(strategy_context, indent=2)}

TODAY'S LIVE ESPN SLATE:
{json.dumps(slate, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "recommendations": [
    {{
      "kickoff_utc": "2026-11-12 00:30 UTC",
      "matchup": "Away Team @ Home Team",
      "play_type": "Core Tier 1 (SGP)",
      "consensus_total": "51.5",
      "target_under": "Under 58.5",
      "favorite": "Home Team",
      "favorite_ml": "-450",
      "target_combined_odds": "-125",
      "rec_stake": 7,
      "rationale": "Detailed analytical breakdown citing edge tags, cushion math, and mistake guardrails."
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
    print("Starting Daily Curation Engine with Edge & Mistake Feedback Loop...", flush=True)
    workbook = get_workbook()
    strategy_context = load_strategy_context(workbook)
    print(f"Loaded strategy context. Current Bankroll: ${strategy_context['current_bankroll']:.2f}", flush=True)

    slate = fetch_daily_slate()
    print(f"Scraped {len(slate)} upcoming matchups from ESPN.", flush=True)

    if not slate:
        print("No pre-game matchups found on ESPN today. Exiting.", flush=True)
        exit(0)

    recommendations = run_ai_curation(slate, strategy_context)
    print(f"Generated {len(recommendations)} curated plays.", flush=True)

    feed_ws = workbook.worksheet("Curator Feed")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows_to_append = []
    for rec in recommendations:
        rows_to_append.append([
            today_str,
            rec.get("kickoff_utc", ""),
            rec.get("matchup", ""),
            rec.get("play_type", ""),
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
