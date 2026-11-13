import os
import re
import json
import time
import requests
import gspread
from datetime import datetime, timezone
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
    # Use EST date so 3:12 AM EST pulls today's US slate accurately
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
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return json.loads(text)

# --- 4. MULTI-WINDOW AI CURATION ENGINE ---
def run_ai_curation(slate, context):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    start_num = context["next_bet_num"]

    prompt = f"""
You are the quantitative sports betting curator for a $100-to-$1,000 challenge.
Yesterday's bets have been settled by settler.py, and today's live ESPN slate (grouped into EST broadcast time windows with splits, stats, rankings, TV networks, and odds) is loaded below.

OPERATIONAL MANDATES:
1. MULTI-WINDOW TIME-SLOT ACTION (NO 2-BET RESTRICTION):
   - You are NOT restricted to 2 bets a day.
   - Spread action across today's available broadcast time windows ('Window 1: Early / Noon', 'Window 2: Afternoon', 'Window 3: Primetime', 'Window 4: Late Night') whenever multiple windows have games on the slate.
   - On full slates (like CFB Saturdays or NFL Sundays), recommend 3 to 5 plays covering each active time window (e.g., Noon, 3:30/4:30 PM, 7:30 PM, 10:00 PM EST). On lighter weekday slates, recommend 2 to 4 plays across the active evening/late windows.
2. BANKROLL & 50% MAX ACTIVE EXPOSURE RULE:
   - Current Settled Bankroll: ${context['current_bankroll']:.2f}
   - Maximum Active Exposure (50% of Bankroll): ${context['max_active_exposure_50pct']:.2f}
   - Total combined 'rec_stake' across all recommended plays today MUST NOT exceed ${context['max_active_exposure_50pct']:.2f}.
   - Individual play sizing in Tier 1 (1U = $4.00):
     * High-Conviction Core Plays (Straight Spreads, Straight Totals, or 2-Leg Correlated SGPs): $6.00 to $12.00 (1.5U to 3.0U) depending on edge and number of windows active today.
     * Multi-Leg Parlays / Window Action Plays: $3.00 to $6.00 (0.75U to 1.5U).
3. DYNAMIC BET STRUCTURES (NEVER FORCE FIXED SLOTS):
   - Choose the highest-edge structure for each time window: Straight Spread, Straight Over/Under, 2-Leg Correlated SGP (Favorite ML/Spread + Alternate Over or Alternate Under with +6.0 to +7.5 pts cushion in CFB/NFL or +8.0 to +10.0 pts in NBA/NCAAM), or 2-to-3 Leg Moneyline/Spread Parlay.
4. WATCHABILITY & OVERS AS SOFT TIEBREAKER (PROFIT IS #1 GOAL):
   - Primary goal is growing the bankroll to $1,000 via objective mathematical edge (records, home/away splits, scoring efficiency, rest, injuries).
   - Soft Tiebreaker: When two plays have similar +EV, prefer games with high watchability (nationally televised ABC/ESPN/FOX/CBS games, ranked matchups, SEC/Florida Gators, marquee NBA/NFL matchups) and look for high-scoring Over setups (or Alternate Over cushions in SGPs). However, NEVER force an Over if the Under or another matchup has a clearly superior edge.
5. 30-DAY BASELINE DATA COLLECTION & MARKET HYGIENE:
   - Do NOT apply arbitrary historical trend bans or home/road biases.
   - Rule 3 (No Team Stacking): Never include the same team across multiple tickets.
   - For Soccer, always specify 'Draw No Bet (DNB)', 'Double Chance', or 'Over/Under Goals' (avoid raw 3-way 90-min ties in parlays). For NHL, specify '2-Way ML (Inc. OT/SO)'.

Number the 'bet_id' field sequentially starting at BET-{start_num:03d} (e.g., BET-{start_num:03d}, BET-{start_num+1:03d}, BET-{start_num+2:03d}...).

POST-SETTLEMENT CONTEXT:
{json.dumps(context, indent=2)}

TODAY'S LIVE ESPN SLATE (WITH EST TIME WINDOWS):
{json.dumps(slate, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "recommendations": [
    {{
      "bet_id": "BET-{start_num:03d}",
      "kickoff_utc": "2026-11-13 19:00 EST (Window 3: Primetime)",
      "matchup": "Away Team @ Home Team (TV Network)",
      "play_type": "Window 3 Core (Straight / SGP / Parlay)",
      "consensus_total": "54.5",
      "target_under": "Alt Over 47.5 or Alt Under 61.5 or N/A",
      "favorite": "Exact Selection & Line",
      "favorite_ml": "-110",
      "target_combined_odds": "-125",
      "rec_stake": 8,
      "rationale": "Time window, TV watchability, objective statistical/injury edge, and why Over/Under/Spread/SGP was chosen."
    }}
  ]
}}
"""

    models_to_try = [
        "gemini-2.5-flash",
        "gemini-2.0-flash"
    ]

    for attempt, model_name in enumerate(models_to_try, start=1):
        # Attempt A: With Google Search grounding for live injury/lineup checks
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2
                )
            )
            parsed = extract_json_payload(response.text)
            if "recommendations" in parsed:
                return parsed["recommendations"]
        except Exception as e:
            print(f"Attempt {attempt} ({model_name} + Search) fallback: {e}", flush=True)

        # Attempt B: Strict JSON MIME mode (Guaranteed valid JSON)
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2
                )
            )
            parsed = extract_json_payload(response.text)
            if "recommendations" in parsed:
                return parsed["recommendations"]
        except Exception as e2:
            print(f"Attempt {attempt} ({model_name} strict JSON) failed: {e2}", flush=True)
            time.sleep(10)

    raise RuntimeError("All Gemini model attempts failed.")

# --- 5. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Step 2: Starting Multi-Window Dynamic Curation Engine...", flush=True)
    workbook = get_workbook()
    context = load_post_settlement_context(workbook)
    print(
        f"Bankroll: ${context['current_bankroll']:.2f} | "
        f"50% Max Exposure Budget: ${context['max_active_exposure_50pct']:.2f} | "
        f"Next ID: BET-{context['next_bet_num']:03d}",
        flush=True
    )

    slate = fetch_daily_slate()
    print(f"Scraped {len(slate)} upcoming matchups for today (EST) from ESPN.", flush=True)

    if not slate:
        print("No pre-game matchups found on ESPN for today's date. Exiting.", flush=True)
        exit(0)

    recommendations = run_ai_curation(slate, context)
    print(f"Generated {len(recommendations)} multi-window play(s) for today.", flush=True)

    feed_ws = workbook.worksheet("Curator Feed")
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

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
            rec.get("rec_stake", 6),
            "UNEXECUTED",
            rec.get("rationale", "")
        ])

    if rows_to_append:
        feed_ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"Successfully appended {len(rows_to_append)} row(s) to Curator Feed.", flush=True)
