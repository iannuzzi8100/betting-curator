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
    "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "LALIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    "SERIEA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
    "BUNDESLIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
    "UCL": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"
}

SUMMARY_SPORT_PATHS = {
    "CFB": "football/college-football",
    "NFL": "football/nfl",
    "NBA": "basketball/nba",
    "NCAAM": "basketball/mens-college-basketball",
    "NHL": "hockey/nhl",
    "EPL": "soccer/eng.1",
    "LALIGA": "soccer/esp.1",
    "SERIEA": "soccer/ita.1",
    "BUNDESLIGA": "soccer/ger.1",
    "UCL": "soccer/uefa.champions"
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
        "next_bet_ids": [f"BET-{next_bet_num:03d}", f"BET-{next_bet_num + 1:03d}", f"BET-{next_bet_num + 2:03d}"],
        "unit_tier_rules": rules_rows
    }

# --- 2. DEEP ESPN SLATE SCRAPER (SPLITS, STATS, INJURIES, BPI/FPI) ---
def fetch_event_enrichment(league, event_id):
    path = SUMMARY_SPORT_PATHS.get(league)
    if not path or not event_id:
        return {}
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code != 200:
            return {}
        data = res.json()

        # 1. Extract BPI/FPI Win Projection if available
        predictor = data.get("predictor", {})
        home_proj = predictor.get("homeTeam", {}).get("gameProjection", "N/A")
        away_proj = predictor.get("awayTeam", {}).get("gameProjection", "N/A")

        # 2. Extract Key Injuries (Out / Doubtful / Questionable)
        injuries_summary = []
        for team_inj in data.get("injuries", []):
            t_name = team_inj.get("team", {}).get("abbreviation", "")
            notable = []
            for item in team_inj.get("injuries", [])[:5]:
                athlete = item.get("athlete", {}).get("shortName", "")
                status = item.get("status", "")
                if athlete and status:
                    notable.append(f"{athlete} ({status})")
            if notable:
                injuries_summary.append(f"{t_name}: {', '.join(notable)}")

        return {
            "espn_win_prob": f"Home {home_proj}% / Away {away_proj}%" if home_proj != "N/A" else "N/A",
            "key_injuries": " | ".join(injuries_summary) if injuries_summary else "None listed"
        }
    except Exception:
        return {}

def extract_records(competitor):
    recs = {}
    for r in competitor.get("records", []):
        name = r.get("name") or r.get("type") or "overall"
        recs[name] = r.get("summary", "")
    return recs

def extract_team_stats(competitor):
    stats = {}
    for s in competitor.get("statistics", []):
        name = s.get("abbreviation") or s.get("name")
        val = s.get("displayValue")
        if name and val:
            stats[name] = val
    return stats

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

            event_id = event.get("id", "")
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

            # Only enrich games that have active betting lines or high-profile matchups
            enrichment = fetch_event_enrichment(league, event_id)

            slate.append({
                "league": league,
                "kickoff_utc": event.get("date", ""),
                "matchup": f"{away.get('team', {}).get('displayName', '')} @ {home.get('team', {}).get('displayName', '')}",
                "home_team": home.get("team", {}).get("displayName", ""),
                "home_splits": extract_records(home),
                "home_stats": extract_team_stats(home),
                "away_team": away.get("team", {}).get("displayName", ""),
                "away_splits": extract_records(away),
                "away_stats": extract_team_stats(away),
                "spread_details": odds_info.get("details", "N/A"),
                "over_under": odds_info.get("overUnder", "N/A"),
                "home_ml": odds_info.get("homeTeamOdds", {}).get("moneyLine", "N/A"),
                "away_ml": odds_info.get("awayTeamOdds", {}).get("moneyLine", "N/A"),
                "draw_ml": odds_info.get("drawOdds", {}).get("moneyLine", "N/A"),
                "espn_win_prob": enrichment.get("espn_win_prob", "N/A"),
                "key_injuries": enrichment.get("key_injuries", "None listed")
            })

    return slate

# --- 3. DYNAMIC AI CURATION (WITH GOOGLE SEARCH GROUNDING & FLEXIBLE SLOTS) ---
def run_ai_curation(slate, context):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are the quantitative sports betting curator for a $100-to-$1,000 challenge.
Yesterday's bets have been settled by settler.py, and today's enriched ESPN slate (with home/away splits, team stats, BPI/FPI win probabilities, and injury lists) has been scraped.

USE GOOGLE SEARCH GROUNDING BEFORE FINALIZING PICKS:
Verify rest advantages (back-to-backs), starting lineups/quarterbacks/goalies, pace/efficiency mismatches, and current consensus lines for your top candidates.

OPERATIONAL MANDATES:
1. NO FIXED BET SLOTS (DYNAMIC PORTFOLIO SELECTION):
   - You are NOT forced to output 1 SGP and 1 Parlay.
   - Choose the 1 to 3 plays across today's slate that provide the HIGHEST mathematical expected value (+EV) and win probability based on 'unit_tier_rules':
     * Straight Spreads or Totals (-135 to +110)
     * Correlated 2-Leg SGPs (Favorite ML/Spread + Alternate Total with +6.0 to +7.5 pts cushion in CFB/NFL or +8.0 to +10.0 pts in NBA/NCAAM)
     * High-Floor 2-to-3 Leg Moneyline/Spread Parlays (+105 to +220)
   - If the best board today is two Straight Bets, recommend two Straight Bets. If it is one Straight Bet, one Correlated SGP, and one 0.5U Parlay, recommend those.
2. STRICT TIER 1 UNIT SIZING (Current Bankroll: ${context['current_bankroll']:.2f}, 1U = $4.00, Max Daily Risk = $10.00-$12.00 [2.5U-3.0U]):
   - Core Plays (Straight Bets or High-Correlation 2-Leg SGPs): $6.00 to $8.00 (1.50U to 2.00U).
   - Secondary Floor Parlays / Flyers (Rule 5): Max 1 parlay per day, capped strictly at $2.00 to $3.00 (0.50U to 0.75U).
   - Total combined stake across all recommended plays today MUST NOT exceed $12.00 (3.0U).
3. 30-DAY BASELINE DATA COLLECTION MODE:
   - Do NOT apply arbitrary historical trend bans or home/road biases. Evaluate home and road sides objectively using splits, rest, injuries, and efficiency.
4. SPORT-SPECIFIC MARKET HYGIENE (NHL & EUROPEAN SOCCER):
   - European Soccer (EPL, La Liga, Serie A, Bundesliga, UCL) uses 3-way 90-minute lines where a regulation Draw loses a standard Moneyline. NEVER put a raw 3-way Soccer ML in a parlay unless intentionally pricing the draw risk; instead specify 'Draw No Bet (DNB)', 'Double Chance (Win or Draw)', or 'Over/Under Goals'.
   - For NHL, explicitly state '2-Way ML (Inc. OT/SO)' rather than 60-minute regulation ML.
5. RULE 3 (NO TEAM STACKING):
   - Never include the same team or game across multiple recommended tickets.

POST-SETTLEMENT CONTEXT:
{json.dumps(context, indent=2)}

TODAY'S ENRICHED ESPN SLATE:
{json.dumps(slate, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "recommendations": [
    {{
      "bet_id": "{context['next_bet_ids'][0]}",
      "kickoff_utc": "2026-11-14 00:00 UTC",
      "matchup": "Away Team @ Home Team",
      "play_type": "Core Tier 1 (Straight Spread / Straight Total / Correlated SGP)",
      "consensus_total": "48.5",
      "target_under": "Under 55.5 (or N/A for straight spread/ML)",
      "favorite": "Exact Selection & Line (e.g. Pittsburgh -3.5 or UCLA ML + Alt Under 54.5)",
      "favorite_ml": "-110",
      "target_combined_odds": "-110",
      "rec_stake": 7,
      "rationale": "Cite specific splits, injuries, rest, pace/efficiency edge, and why this bet structure beats alternative structures today."
    }}
  ]
}}
"""

    models_to_try = [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash"
    ]

    response = None
    for attempt, model_name in enumerate(models_to_try, start=1):
        try:
            # First try with live Google Search grounding enabled
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2
                )
            )
            break
        except errors.ServerError as e:
            print(f"Attempt {attempt} ({model_name}) hit 503 overload: {e}. Retrying in 15s...", flush=True)
            time.sleep(15)
        except Exception as e:
            print(f"Attempt {attempt} ({model_name} with Search) fallback due to: {e}", flush=True)
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
            except Exception as e2:
                print(f"Fallback attempt failed: {e2}", flush=True)
                time.sleep(10)

    if response is None:
        raise RuntimeError("All Gemini model attempts failed.")

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    return json.loads(raw_text).get("recommendations", [])

# --- 4. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Step 2: Starting Enriched Dynamic Curation Engine...", flush=True)
    workbook = get_workbook()
    context = load_post_settlement_context(workbook)
    print(f"Verified Post-Settlement Bankroll: ${context['current_bankroll']:.2f} | Pending Bets Left: {context['pending_bets_remaining']}", flush=True)

    slate = fetch_daily_slate()
    print(f"Scraped & enriched {len(slate)} matchups for today from ESPN.", flush=True)

    if not slate:
        print("No pre-game matchups found on ESPN for today's date. Exiting.", flush=True)
        exit(0)

    recommendations = run_ai_curation(slate, context)
    print(f"Generated {len(recommendations)} dynamic play(s) for today.", flush=True)

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
            rec.get("rec_stake", 6),
            "UNEXECUTED",
            rec.get("rationale", "")
        ])

    if rows_to_append:
        feed_ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        print(f"Successfully appended {len(rows_to_append)} row(s) to Curator Feed.", flush=True)
