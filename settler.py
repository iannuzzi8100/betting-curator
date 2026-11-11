import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

# --- CONFIGURATION ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
TRACKER_TAB_NAME = "Challenge Tracker ($100-$1k)"

ESPN_LEAGUES = {
    "CFB": {"path": "football/college-football", "groups": "80"},
    "NFL": {"path": "football/nfl", "groups": None},
    "NBA": {"path": "basketball/nba", "groups": None},
    "NCAAM": {"path": "basketball/mens-college-basketball", "groups": "50"},
    "NHL": {"path": "hockey/nhl", "groups": None},
    "MLB": {"path": "baseball/mlb", "groups": None}
}

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
    workbook = client.open(SHEET_NAME)
    return workbook.worksheet(TRACKER_TAB_NAME)

# --- 2. FETCH FINAL SCORES FROM ESPN ---
def fetch_final_scores(target_dates):
    final_games = []

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
                home_score = int(home.get("score", 0) or 0)
                away_score = int(away.get("score", 0) or 0)

                final_games.append({
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
                })

    return final_games

# --- 3. GRADE PENDING TICKETS WITH GEMINI ---
def grade_pending_bets(pending_bets, final_games):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are a deterministic sportsbook settlement auditor.
Verify each pending bet against the official ESPN scoreboard results provided below.

RULES:
1. Check EVERY leg of a parlay or Same Game Parlay (SGP).
2. A parlay/SGP is "Won" ONLY if EVERY leg has completed AND won.
3. A parlay/SGP is "Lost" if ANY single leg has completed AND lost (even if other legs are still in progress).
4. Keep the status "Pending" if any required game is not yet completed ("completed": false) and no leg has lost yet, or if a game cannot be matched in the scoreboard data.
5. For totals (e.g., "Under 57.5"), compare "total_points" (away_score + home_score) strictly against the line.
6. For moneylines (e.g., "Toledo ML", "Pennsylvania ML"), verify the selected team's score is strictly greater than the opponent's score.

PENDING BETS:
{json.dumps(pending_bets, indent=2)}

ESPN SCOREBOARD DATA:
{json.dumps(final_games, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "settlements": [
    {{
      "row_index": 17,
      "bet_id": "BET-003",
      "result": "Won",
      "verification_note": "Toledo defeated UMass 38-14 (52 total pts < 57.5)."
    }}
  ]
}}
"""

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0
        )
    )

    try:
        return json.loads(response.text).get("settlements", [])
    except Exception as e:
        print(f"Error parsing Gemini settlement response: {e}", flush=True)
        print("Raw model response:", response.text, flush=True)
        return []

# --- 4. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Starting Nightly Settlement Engine...", flush=True)
    sheet = get_tracker_sheet()
    all_values = sheet.get_all_values()

    # Locate the header row dynamically ("Bet ID")
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
                # Also check next UTC day for late ET night games
                dt_obj = datetime.strptime(clean_date, "%Y%m%d")
                dates_needed.add((dt_obj + timedelta(days=1)).strftime("%Y%m%d"))

            pending_bets.append({
                "row_index": r_idx + 1,  # 1-indexed for gspread
                "bet_id": bet_id,
                "date": raw_date,
                "sport": row[col_sport] if col_sport is not None else "",
                "bet_type": row[col_type] if col_type is not None else "",
                "selections": row[col_selections]
            })

    if not pending_bets:
        print("No pending bets found to settle. Exiting cleanly.", flush=True)
        exit(0)

    # Always include today and yesterday in UTC as fallback
    now_utc = datetime.now(timezone.utc)
    dates_needed.add(now_utc.strftime("%Y%m%d"))
    dates_needed.add((now_utc - timedelta(days=1)).strftime("%Y%m%d"))

    print(f"Found {len(pending_bets)} pending bet(s). Fetching ESPN scores for {sorted(dates_needed)}...", flush=True)
    final_games = fetch_final_scores(sorted(dates_needed))
    print(f"Retrieved {len(final_games)} scoreboard records from ESPN.", flush=True)

    settlements = grade_pending_bets(pending_bets, final_games)

    updated_count = 0
    for s in settlements:
        row_num = s.get("row_index")
        bet_id = s.get("bet_id")
        new_result = s.get("result", "Pending").strip()
        note = s.get("verification_note", "")

        print(f"Audit -> {bet_id} (Row {row_num}): {new_result} | {note}", flush=True)

        if new_result in ["Won", "Lost", "Push"]:
            sheet.update_cell(row_num, col_result + 1, new_result)
            if col_notes is not None and note:
                existing_note = all_values[row_num - 1][col_notes] if len(all_values[row_num - 1]) > col_notes else ""
                updated_note = f"{existing_note} | [Settled: {note}]".strip(" |")
                sheet.update_cell(row_num, col_notes + 1, updated_note)
            updated_count += 1

    print(f"Settlement run complete. Updated {updated_count} ticket(s).", flush=True)
