import os
import json
import time
import requests
import gspread
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types, errors

load_dotenv()

# --- CONFIGURATION ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SHEET_ID = (
    os.environ.get("SHEET_ID")
    or os.environ.get("MASTER_SHEET_ID")
    or os.environ.get("SPREADSHEET_ID")
    or os.environ.get("TRACKER_SHEET_ID")
    or os.environ.get("GOOGLE_SHEET_ID")
    or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
)
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
TRACKER_TAB_NAME = "Challenge Tracker ($100-$1k)"

ESPN_LEAGUES = {
    "CFB": {"path": "football/college-football", "groups": "80"},
    "NFL": {"path": "football/nfl", "groups": None},
    "NBA": {"path": "basketball/nba", "groups": None},
    "NCAAM": {"path": "basketball/mens-college-basketball", "groups": "50"},
    "NHL": {"path": "hockey/nhl", "groups": None},
    "MLB": {"path": "baseball/mlb", "groups": None},
    "EPL": {"path": "soccer/eng.1", "groups": None},
    "UCL": {"path": "soccer/uefa.champions", "groups": None},
}

ALLOWED_EDGE_TAGS = [
    "WIN: Correlated Home Favorite + Alt Total Cushion",
    "WIN: Fatigue / Back-to-Back Fade",
    "WIN: Trench / Pace Mismatch",
    "WIN: Disciplined Straight / Floor Play",
    "FAIL: Totals Blowout / Late Garbage Time",
    "FAIL: Road Favorite Upset (Parlay Bust)",
    "FAIL: Hook / Half-Point Variance",
    "FAIL: Juice Parlay / Multi-Leg Bust"
]

# --- 1. GOOGLE SHEETS CONNECTION ---
def get_tracker_sheet():
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
    try:
        workbook = client.open(SHEET_NAME)
    except Exception:
        workbook = client.open_by_key(SHEET_ID)
    return workbook.worksheet(TRACKER_TAB_NAME)

# --- 2. FETCH FINAL SCORES & PLAYER BOX SCORES FROM ESPN ---
def fetch_boxscore_players(path, event_id, league_name):
    """Fetches player box-score stats for completed games to grade player props (NHL G/A/PTS/SOG, NBA PTS/REB/AST/PRA)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"
    players_out = {}
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return players_out
        box = res.json().get("boxscore", {})
        for team_block in box.get("players", []):
            for stat_group in team_block.get("statistics", []):
                labels = [lbl.upper() for lbl in stat_group.get("labels", [])]
                for ath_entry in stat_group.get("athletes", []):
                    ath_name = ath_entry.get("athlete", {}).get("displayName", "")
                    stats_vals = ath_entry.get("stats", [])
                    if not ath_name or not stats_vals or len(stats_vals) != len(labels):
                        continue
                    stat_map = dict(zip(labels, stats_vals))
                    if league_name == "NHL" and ("G" in stat_map or "S" in stat_map):
                        try:
                            g = int(stat_map.get("G", 0) or 0)
                            a = int(stat_map.get("A", 0) or 0)
                            sog = int(stat_map.get("S", stat_map.get("SOG", 0)) or 0)
                            players_out[ath_name] = {"G": g, "A": a, "PTS": g + a, "SOG": sog}
                        except Exception:
                            pass
                    elif league_name in ("NBA", "NCAAM") and "PTS" in stat_map:
                        try:
                            pts = int(stat_map.get("PTS", 0) or 0)
                            reb = int(stat_map.get("REB", 0) or 0)
                            ast = int(stat_map.get("AST", 0) or 0)
                            players_out[ath_name] = {
                                "PTS": pts,
                                "REB": reb,
                                "AST": ast,
                                "PRA": pts + reb + ast,
                            }
                        except Exception:
                            pass
    except Exception:
        pass
    return players_out


def fetch_final_scores(target_dates, pending_bets=None):
    final_games = []
    pending_text = " ".join(
        f"{b.get('sport', '')} {b.get('selections', '')} {b.get('notes', '')}"
        for b in (pending_bets or [])
    ).lower()

    for league_name, league_info in ESPN_LEAGUES.items():
        path = league_info["path"]
        for d in target_dates:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d}"
            if league_info.get("groups"):
                url += f"&groups={league_info['groups']}"

            try:
                res = requests.get(url, timeout=10)
                if res.status_code != 200:
                    continue
                events = res.json().get("events", [])
            except Exception as e:
                print(f"Error fetching {path} for {d}: {e}", flush=True)
                continue

            for event in events:
                event_id = event.get("id", "")
                status_type = event.get("status", {}).get("type", {})
                is_completed = status_type.get("completed", False)
                state = status_type.get("state", "")

                comps = event.get("competitions", [])
                if not comps:
                    continue
                competitors = comps[0].get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = next((c for c in competitors if c.get("homeAway") == "home"), None)
                away = next((c for c in competitors if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue

                home_name = home.get("team", {}).get("displayName", "")
                away_name = away.get("team", {}).get("displayName", "")
                home_short = home.get("team", {}).get("shortDisplayName", "").lower()
                away_short = away.get("team", {}).get("shortDisplayName", "").lower()
                home_score = int(float(home.get("score", 0) or 0))
                away_score = int(float(away.get("score", 0) or 0))

                game_record = {
                    "league": league_name,
                    "date": d,
                    "matchup": f"{away_name} @ {home_name}",
                    "away_team": away_name,
                    "away_score": away_score,
                    "home_team": home_name,
                    "home_score": home_score,
                    "total_points": home_score + away_score,
                    "completed": is_completed,
                    "status": status_type.get("description", state)
                }

                # Attach player box-score stats for completed NBA/NHL/NCAAM games mentioned in pending bets
                if (
                    is_completed
                    and event_id
                    and league_name in ("NHL", "NBA", "NCAAM")
                    and (
                        (home_short and home_short in pending_text)
                        or (away_short and away_short in pending_text)
                        or league_name.lower() in pending_text
                    )
                ):
                    player_stats = fetch_boxscore_players(path, event_id, league_name)
                    if player_stats:
                        game_record["player_boxscore"] = player_stats

                final_games.append(game_record)

    return final_games

# --- 3. GRADE & AUTO-TAG PENDING TICKETS WITH GEMINI ---
def grade_pending_bets(pending_bets, final_games):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are a deterministic sportsbook settlement and post-mortem auditor.
Verify each pending bet against the official ESPN scoreboard results provided below, and classify the root cause or winning edge using the allowed tags.

GRADING RULES:
1. Check EVERY leg of a parlay or Same Game Parlay (SGP).
2. A parlay/SGP is "Won" ONLY if EVERY leg has completed AND won.
3. A parlay/SGP is "Lost" if ANY single leg has completed AND lost (even if other legs are still in progress).
4. Keep the status "Pending" if any required game is not yet completed ("completed": false) and no leg has lost yet, or if a game cannot be matched in the scoreboard data.
5. Match short team names (e.g., "Celtics", "Thunder", "Lakers", "Magic", "Pennsylvania") to ESPN full display names (e.g., "Boston Celtics", "Oklahoma City Thunder", "Los Angeles Lakers", "Orlando Magic", "Pennsylvania Quakers").
6. For totals (e.g., "Under 57.5"), compare "total_points" (away_score + home_score) strictly against the line.
7. For moneylines (e.g., "Toledo ML", "Thunder ML"), verify the selected team's score is strictly greater than the opponent's score.
8. For player props (e.g., "Over 0.5 Points", "Under 0.5 Points", "Over 1.5 Shots", "Over 22.5 Points"), check the player's entry in "player_boxscore" on the completed game (NHL: PTS = G + A, Shots = SOG; NBA/NCAAM: PTS, REB, AST, PRA). If the game is completed and the player is in "player_boxscore", grade strictly against the prop line.

TAGGING RULES:
- If result is "Won", select the single best matching "WIN: ..." tag from ALLOWED_EDGE_TAGS based on the bet's notes and structure.
- If result is "Lost", select the single best matching "FAIL: ..." tag from ALLOWED_EDGE_TAGS based on which leg failed and why.
- If result is "Pending" or "Push", set "edge_tag" to "".

ALLOWED_EDGE_TAGS:
{json.dumps(ALLOWED_EDGE_TAGS, indent=2)}

PENDING BETS:
{json.dumps(pending_bets, indent=2)}

ESPN SCOREBOARD DATA:
{json.dumps(final_games, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "settlements": [
    {{
      "row_index": 21,
      "bet_id": "BET-003",
      "result": "Won",
      "edge_tag": "WIN: Correlated Home Favorite + Alt Total Cushion",
      "verification_note": "Toledo defeated UMass 38-14 (52 total pts < 57.5)."
    }}
  ]
}}
"""

    models_to_try = [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ]

    response = None
    for attempt, model_name in enumerate(models_to_try, start=1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            if response and response.text:
                break
        except Exception as e:
            print(f"Attempt {attempt} ({model_name}) failed: {e}. Trying next model...", flush=True)
            time.sleep(2)

    if response is None:
        print("All model attempts experienced 503 overload. Skipping settlement until next run.", flush=True)
        return []

    try:
        return json.loads(response.text).get("settlements", [])
    except Exception as e:
        print(f"Error parsing Gemini settlement response: {e}", flush=True)
        print("Raw model response:", response.text, flush=True)
        return []

# --- 4. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Starting Nightly Settlement & Edge Audit Engine...", flush=True)
    sheet = get_tracker_sheet()
    all_values = sheet.get_all_values()

    header_row_idx = None
    headers = []
    for idx, row in enumerate(all_values):
        if "Bet ID" in row and "Result" in row:
            header_row_idx = idx
            headers = row
            break

    if header_row_idx is None:
        raise ValueError("Could not locate 'Bet ID' and 'Result' columns in tracker sheet.")

    col_bet_id = headers.index("Bet ID")
    col_date = headers.index("Date")
    col_sport = headers.index("Sport/League") if "Sport/League" in headers else None
    col_type = headers.index("Bet Type") if "Bet Type" in headers else None
    col_selections = headers.index("Selections / Legs")
    col_result = headers.index("Result")
    col_notes = headers.index("Notes / Game Script Analysis") if "Notes / Game Script Analysis" in headers else None
    col_tag = headers.index("Outcome / Edge Tag") if "Outcome / Edge Tag" in headers else None

    pending_bets = []
    dates_needed = set()

    for r_idx in range(header_row_idx + 1, len(all_values)):
        row = all_values[r_idx]
        if len(row) <= col_result:
            continue

        bet_id = row[col_bet_id].strip()
        result_val = row[col_result].strip().lower()

        if bet_id and result_val == "pending":
            raw_date = row[col_date].strip()
            clean_date = raw_date.replace("-", "")
            if len(clean_date) == 8 and clean_date.isdigit():
                dates_needed.add(clean_date)
                dt_obj = datetime.strptime(clean_date, "%Y%m%d")
                dates_needed.add((dt_obj + timedelta(days=1)).strftime("%Y%m%d"))

            pending_bets.append({
                "row_index": r_idx + 1,
                "bet_id": bet_id,
                "date": raw_date,
                "sport": row[col_sport] if col_sport is not None else "",
                "bet_type": row[col_type] if col_type is not None else "",
                "selections": row[col_selections],
                "notes": row[col_notes] if (col_notes is not None and len(row) > col_notes) else ""
            })

    if not pending_bets:
        print("No pending bets found to settle. Exiting cleanly.", flush=True)
        exit(0)

    now_utc = datetime.now(timezone.utc)
    dates_needed.add(now_utc.strftime("%Y%m%d"))
    dates_needed.add((now_utc - timedelta(days=1)).strftime("%Y%m%d"))

    print(f"Found {len(pending_bets)} pending bet(s). Fetching ESPN scores for {sorted(dates_needed)}...", flush=True)
    final_games = fetch_final_scores(sorted(dates_needed), pending_bets=pending_bets)
    print(f"Retrieved {len(final_games)} scoreboard records from ESPN.", flush=True)

    settlements = grade_pending_bets(pending_bets, final_games)

    updated_count = 0
    for s in settlements:
        row_num = s.get("row_index")
        bet_id = s.get("bet_id")
        new_result = s.get("result", "Pending").strip()
        edge_tag = s.get("edge_tag", "").strip()
        note = s.get("verification_note", "")

        print(f"Audit -> {bet_id} (Row {row_num}): {new_result} | Tag: {edge_tag} | {note}", flush=True)

        if new_result in ["Won", "Lost", "Push"]:
            sheet.update_cell(row_num, col_result + 1, new_result)

            if col_notes is not None and note:
                existing_note = all_values[row_num - 1][col_notes] if len(all_values[row_num - 1]) > col_notes else ""
                updated_note = f"{existing_note} | [Settled: {note}]".strip(" |")
                sheet.update_cell(row_num, col_notes + 1, updated_note)

            if col_tag is not None and edge_tag in ALLOWED_EDGE_TAGS:
                sheet.update_cell(row_num, col_tag + 1, edge_tag)

            updated_count += 1

    print(f"Settlement run complete. Updated {updated_count} ticket(s).", flush=True)
