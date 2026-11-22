import os
import re
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
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
FEED_TAB_NAME = "Curator Feed"

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
    try:
        return client.open(SHEET_NAME)
    except Exception:
        return client.open_by_key(SHEET_ID)


def get_tracker_sheet(workbook=None):
    wb = workbook or get_workbook()
    return wb.worksheet(TRACKER_TAB_NAME)


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

                # Attach player box-score stats ONLY for completed NBA/NHL/NCAAM games that match a pending bet team (or prop)
                has_prop_kw = any(kw in pending_text for kw in ("rebound", "assist", "point", "shot", "sog", "goal", "pra", "over ", "under "))
                if (
                    is_completed
                    and event_id
                    and league_name in ("NHL", "NBA", "NCAAM")
                    and (
                        (home_short and home_short in pending_text)
                        or (away_short and away_short in pending_text)
                        or (has_prop_kw and league_name.lower() in pending_text)
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
6. For totals (e.g., "Under 57.5", "Over 216.0"), compare "total_points" (away_score + home_score) strictly against the line; if total_points equals the exact whole-number line, grade "Push".
7. For moneylines (e.g., "Toledo ML", "Tennessee TO WIN"), verify the selected team's score is strictly greater than the opponent's score. For Soccer/EPL/UCL 3-way moneylines (team to win), a draw is "Lost" (not Push).
8. For point spreads (e.g., "Middle Tennessee -7.5", "Kentucky +4.5", "Oregon State -7.0"), compute (selected_team_score + spread) vs opponent_score: > opponent is "Won", < opponent is "Lost", exact equality is "Push".
9. For player props (e.g., "Over 0.5 Points", "Under 0.5 Points", "Over 1.5 Shots", "Over 22.5 Points"), check the player's entry in "player_boxscore" on the completed game (NHL: PTS = G + A, Shots = SOG; NBA/NCAAM: PTS, REB, AST, PRA). If the game is completed and the player is in "player_boxscore", grade strictly against the prop line.

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
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
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

# --- 4. BANKROLL & REMAINING BET WAGER RECALCULATION ---
def _parse_num(val):
    if val is None:
        return 0.0
    s = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except Exception:
        return 0.0


def _bet_num(bid):
    try:
        return int(str(bid).strip().split("-")[1])
    except Exception:
        return 0


def _extract_selection_core(text):
    s = str(text or "").strip()
    s = re.sub(r"^\d+\.\s*", "", s).replace("*", "").strip()
    s = re.sub(r"^\[[^\]]+\]\s*", "", s).strip()
    core = re.split(
        r"(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)",
        s,
        flags=re.I,
    )[0]
    return re.sub(r"[^a-z0-9\s]", "", core.lower()).strip()


def compute_ledger_state(all_values, today_str=None):
    if not today_str:
        est_now = datetime.now(timezone(timedelta(hours=-5)))
        today_str = est_now.strftime("%Y-%m-%d")

    header_idx = next((i for i, r in enumerate(all_values) if "Bet ID" in r and "Result" in r), 15)
    settled_bankroll = _parse_num(all_values[4][0]) if len(all_values) > 4 and all_values[4] else 100.0
    total_net_pl = 0.0
    pending_total = 0.0
    pending_tickets = []
    today_tickets = []

    bet_entries = []
    for idx_row, r in enumerate(all_values[header_idx + 1 :], start=header_idx + 2):
        if not r or not str(r[0]).strip().startswith("BET-"):
            continue
        bet_id = str(r[0]).strip()
        stake = _parse_num(r[8]) if len(r) > 8 else 0.0
        payout = _parse_num(r[9]) if len(r) > 9 else 0.0
        odds_raw = str(r[6]).strip() if len(r) > 6 else ""
        res = str(r[10]).strip() if len(r) > 10 else ""
        net_pl = _parse_num(r[12]) if (len(r) > 12 and str(r[12]).strip()) else None
        run_br = _parse_num(r[13]) if (len(r) > 13 and str(r[13]).strip()) else 0.0
        res_l = res.lower()

        if net_pl is None or (net_pl == 0.0 and res_l in ("won", "lost") and stake > 0):
            if res_l == "lost":
                net_pl = -stake
            elif res_l == "won":
                if payout > stake:
                    net_pl = round(payout - stake, 2)
                else:
                    odds_val = _parse_num(odds_raw.replace("+", ""))
                    if odds_val > 0:
                        net_pl = round(stake * (odds_val / 100.0), 2)
                    elif odds_val < 0:
                        net_pl = round(stake * (100.0 / abs(odds_val)), 2)
                    else:
                        net_pl = 0.0
            else:
                net_pl = 0.0

        bet_entries.append({
            "row_index": idx_row,
            "bet_num": _bet_num(bet_id),
            "bet_id": bet_id,
            "b_date": str(r[1]).strip() if len(r) > 1 else "",
            "sport": str(r[2]).strip() if len(r) > 2 else "",
            "b_type": str(r[3]).strip() if len(r) > 3 else "",
            "sel": str(r[5]).strip() if len(r) > 5 else "",
            "odds": odds_raw,
            "stake": round(stake, 2),
            "payout": round(payout, 2),
            "res": res,
            "net_pl": round(net_pl or 0.0, 2),
            "run_br": run_br,
        })

    for e in sorted(bet_entries, key=lambda x: x["bet_num"]):
        rl = e["res"].lower()
        if rl in ("won", "lost"):
            total_net_pl += e["net_pl"]
            settled_bankroll = e["run_br"] if e["run_br"] > 0 else round(100.0 + total_net_pl, 2)

    for e in sorted(bet_entries, key=lambda x: x["bet_num"], reverse=True):
        rl = e["res"].lower()
        if rl == "pending":
            pending_total += e["stake"]
            pending_tickets.append({
                "row_index": e["row_index"],
                "bet_id": e["bet_id"],
                "date": e["b_date"],
                "sport": e["sport"],
                "bet_type": e["b_type"],
                "selections": e["sel"],
                "odds": e["odds"],
                "stake": e["stake"],
                "payout": e["payout"],
            })
        elif e["b_date"] == today_str and rl in ("won", "lost", "push"):
            today_tickets.append({
                "bet_id": e["bet_id"],
                "sport": e["sport"],
                "selections": e["sel"],
                "odds": e["odds"],
                "stake": e["stake"],
                "result": e["res"],
                "net_pl": e["net_pl"],
            })

    settled_bankroll = round(settled_bankroll, 2)
    pending_total = round(pending_total, 2)
    available_cash = round(max(0.0, settled_bankroll - pending_total), 2)
    return {
        "settled_bankroll": settled_bankroll,
        "pending_total": pending_total,
        "available_cash": available_cash,
        "pending_tickets": pending_tickets,
        "today_tickets": today_tickets,
    }


def recalculate_remaining_curated_bets(workbook, all_values=None):
    """
    When a bet settles and the bankroll changes, recalculates the wager amounts
    for all remaining (unplaced) curated bets for today in the 'Curator Feed' tab
    and updates the sheet in-place.
    """
    est_now = datetime.now(timezone(timedelta(hours=-5)))
    today_str = est_now.strftime("%Y-%m-%d")

    if all_values is None:
        tracker_ws = get_tracker_sheet(workbook)
        all_values = tracker_ws.get_all_values()

    ledger = compute_ledger_state(all_values, today_str=today_str)
    settled_br = ledger["settled_bankroll"]
    pending_total = ledger["pending_total"]
    avail_cash = ledger["available_cash"]
    pending_tickets = ledger["pending_tickets"]
    today_tickets = ledger["today_tickets"]

    try:
        feed_ws = workbook.worksheet(FEED_TAB_NAME)
        feed_vals = feed_ws.get_all_values()
    except Exception as exc:
        print(f"Could not open {FEED_TAB_NAME} for wager recalculation: {exc}", flush=True)
        return {"feed_updated": False, "adjustments": [], "settled_bankroll": settled_br}

    target_row_idx = None
    raw_text = ""
    row_date = ""
    for i in range(len(feed_vals) - 1, -1, -1):
        r = feed_vals[i]
        if len(r) >= 12 and str(r[11]).strip():
            txt = str(r[11]).strip()
            if re.search(r"^\s*1\.\s+", txt, flags=re.M):
                target_row_idx = i + 1
                raw_text = txt
                row_date = str(r[0]).strip() or today_str
                break

    if not target_row_idx or not raw_text:
        return {"feed_updated": False, "adjustments": [], "settled_bankroll": settled_br}

    m_br = re.search(r"Bankroll:\s*\$(\d+(?:\.\d{1,2})?)", raw_text)
    feed_br = float(m_br.group(1)) if m_br else settled_br

    lines = raw_text.splitlines()
    # First pass: collect numbered play line indices, window labels, and confidence %
    play_records = []
    for idx, ln in enumerate(lines):
        m_play = re.match(r"^(\s*\d+\.\s*)(.+)$", ln)
        if not m_play:
            continue
        body = m_play.group(2)
        clean_body = body.replace("*", "").strip()
        is_pass = ("⛔" in clean_body) or bool(re.search(r"\bPASS\b", clean_body))
        win_label = ""
        m_win = re.match(r"^\[([^\]]+)\]", clean_body)
        if m_win:
            win_label = m_win.group(1).split("—")[0].split("-")[0].strip()

        conf_pct = None
        for look_idx in range(idx + 1, min(len(lines), idx + 5)):
            nxt = lines[look_idx].strip()
            if re.match(r"^\d+\.\s+", nxt):
                break
            if "confidence:" in nxt.lower():
                m_cp = re.search(r"(\d{2,3})\s*%", nxt)
                if m_cp:
                    conf_pct = int(m_cp.group(1))
                break

        play_records.append({
            "line_idx": idx,
            "prefix": m_play.group(1),
            "body": body,
            "clean_body": clean_body,
            "is_pass": is_pass,
            "win_label": win_label,
            "conf_pct": conf_pct,
        })

    win_counts = {}
    for pr in play_records:
        if not pr["is_pass"] and pr["win_label"]:
            win_counts[pr["win_label"]] = win_counts.get(pr["win_label"], 0) + 1

    adjustments = []
    for pr in play_records:
        if pr["is_pass"]:
            continue
        core = _extract_selection_core(pr["clean_body"])
        if not core or len(core) < 3:
            continue

        is_pending = any(core in str(t.get("selections", "")).lower() for t in pending_tickets)
        is_settled_today = any(core in str(t.get("selections", "")).lower() for t in today_tickets)
        if is_pending or is_settled_today:
            continue

        # Determine wager % of bankroll for this remaining bet
        wager_pct = None
        m_pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s+(?:Resulting\s+)?)?(?:BR|Bankroll)", pr["clean_body"], flags=re.I)
        if m_pct:
            wager_pct = float(m_pct.group(1))
        elif pr["conf_pct"] is not None:
            cp = pr["conf_pct"]
            dual = win_counts.get(pr["win_label"], 1) >= 2
            if cp >= 88:
                wager_pct = 35.0 if dual else 50.0
            elif cp >= 78:
                wager_pct = 25.0 if dual else 35.0
            elif cp >= 68:
                wager_pct = 18.0 if dual else 20.0

        m_stakes = list(re.finditer(r"\$(\d+(?:\.\d{1,2})?)", pr["body"]))
        old_stake = float(m_stakes[-1].group(1)) if m_stakes else 0.0
        if wager_pct is None and feed_br > 0 and old_stake > 0:
            wager_pct = round((old_stake / feed_br) * 100.0, 1)

        if not wager_pct or wager_pct <= 0 or settled_br <= 0:
            continue

        new_stake = round(max(2.00, settled_br * (wager_pct / 100.0)), 2) if settled_br >= 2.0 else round(settled_br, 2)
        if m_stakes:
            last_m = m_stakes[-1]
            new_body = pr["body"][: last_m.start()] + f"${new_stake:.2f}" + pr["body"][last_m.end() :]
            lines[pr["line_idx"]] = pr["prefix"] + new_body

        pct_display = int(wager_pct) if float(wager_pct).is_integer() else wager_pct
        pick_label = re.split(r"\s+—\s+", pr["clean_body"])[0].strip()
        adjustments.append({
            "selection": pick_label,
            "wager_pct": pct_display,
            "old_stake": round(old_stake, 2),
            "new_stake": new_stake,
            "changed": abs(new_stake - old_stake) >= 0.01,
        })

    # Update header line Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX
    updated_text = "\n".join(lines)
    updated_text = re.sub(
        r"Bankroll:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Pending:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Available:\s*\$\d+(?:\.\d{1,2})?",
        f"Bankroll: ${settled_br:.2f} | Pending: ${pending_total:.2f} | Available: ${avail_cash:.2f}",
        updated_text,
    )

    feed_updated = False
    if updated_text != raw_text:
        try:
            feed_ws.update_acell(f"L{target_row_idx}", updated_text[:4500])
            feed_updated = True
            print(
                f"Updated Curator Feed (Row {target_row_idx}) remaining bet wagers for ${settled_br:.2f} bankroll (was ${feed_br:.2f}).",
                flush=True,
            )
        except Exception as exc:
            print(f"Warning: Could not update Curator Feed row {target_row_idx}: {exc}", flush=True)

    return {
        "feed_updated": feed_updated,
        "adjustments": adjustments,
        "settled_bankroll": settled_br,
        "prev_feed_bankroll": feed_br,
        "pending_count": len(pending_tickets),
        "curated_date": row_date,
    }


def _check_static_data_drift(settled_br, pending_count):
    """Checks if docs/data.json has a stale bankroll or pending count so static build gets triggered."""
    try:
        if not os.path.exists("docs/data.json"):
            return False
        with open("docs/data.json", "r", encoding="utf-8") as f:
            d = json.load(f)
        old_br = float(d.get("bankroll", 0.0))
        old_pend_cnt = len(d.get("pending_tickets", []))
        if abs(old_br - settled_br) >= 0.01 or old_pend_cnt != pending_count:
            return True
        for p in d.get("curated_plays", []):
            title = str(p.get("title", ""))
            m_pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s+(?:Resulting\s+)?)?(?:BR|Bankroll)", title, flags=re.I)
            if m_pct:
                pct = float(m_pct.group(1))
                core = _extract_selection_core(title)
                in_pend = any(core in str(t.get("selections", "")).lower() for t in d.get("pending_tickets", []))
                in_today = any(core in str(t.get("selections", "")).lower() for t in d.get("today_stats", {}).get("tickets", []))
                if not in_pend and not in_today:
                    expected_stake = round(max(2.00, settled_br * (pct / 100.0)), 2)
                    if abs(float(p.get("stake", 0.0)) - expected_stake) >= 0.01:
                        return True
    except Exception:
        pass
    return False


# --- 5. EXECUTION ENTRYPOINT ---
def run_settlement(notify_telegram=True):
    print("Starting Settlement & Edge Audit Engine...", flush=True)
    workbook = get_workbook()
    sheet = get_tracker_sheet(workbook)
    all_values = sheet.get_all_values()

    est_now = datetime.now(timezone(timedelta(hours=-5)))
    today_str = est_now.strftime("%Y-%m-%d")
    before_ledger = compute_ledger_state(all_values, today_str=today_str)
    prev_bankroll = before_ledger["settled_bankroll"]

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

    updated_count = 0
    settled_lines = []
    wins_cnt = 0
    losses_cnt = 0
    pushes_cnt = 0

    if not pending_bets:
        print("No pending bets found to settle. Checking remaining bet wager sync...", flush=True)
    else:
        now_utc = datetime.now(timezone.utc)
        dates_needed.add(now_utc.strftime("%Y%m%d"))
        dates_needed.add((now_utc - timedelta(days=1)).strftime("%Y%m%d"))

        print(f"Found {len(pending_bets)} pending bet(s). Fetching ESPN scores for {sorted(dates_needed)}...", flush=True)
        final_games = fetch_final_scores(sorted(dates_needed), pending_bets=pending_bets)
        print(f"Retrieved {len(final_games)} scoreboard records from ESPN.", flush=True)

        settlements = grade_pending_bets(pending_bets, final_games)

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

                if new_result == "Won":
                    wins_cnt += 1
                    icon = "🟢"
                elif new_result == "Lost":
                    losses_cnt += 1
                    icon = "🔴"
                else:
                    pushes_cnt += 1
                    icon = "⚪"

                settled_lines.append(f"{icon} *{bet_id}* ({new_result}) — {note}")
                updated_count += 1

    print(f"Settlement run complete. Updated {updated_count} ticket(s).", flush=True)

    refreshed = sheet.get_all_values() if updated_count > 0 else all_values
    recalc = recalculate_remaining_curated_bets(workbook, all_values=refreshed)
    new_bankroll_val = recalc["settled_bankroll"]
    still_pending = recalc["pending_count"]
    adjustments = recalc["adjustments"]
    bankroll_changed = abs(new_bankroll_val - prev_bankroll) >= 0.01 or recalc["feed_updated"]

    if updated_count > 0 or recalc["feed_updated"] or _check_static_data_drift(new_bankroll_val, still_pending):
        try:
            with open(".tg_changed", "w", encoding="utf-8") as f:
                f.write(str(max(1, updated_count)))
            if updated_count > 0 or recalc["feed_updated"]:
                with open(".settled_changed", "w", encoding="utf-8") as f:
                    f.write(str(max(1, updated_count)))
        except Exception:
            pass

    if updated_count > 0 and notify_telegram and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            br_str = f"${new_bankroll_val:.2f}"
            if abs(new_bankroll_val - prev_bankroll) >= 0.01:
                br_str += f" (was ${prev_bankroll:.2f})"
            receipt_msg = (
                f"✅ *Settlement & Edge Audit Complete*\n"
                f"Graded: {updated_count} ticket(s) ({wins_cnt}W - {losses_cnt}L"
                + (f" - {pushes_cnt}P" if pushes_cnt else "")
                + ")\n\n"
                + "\n".join(settled_lines)
                + f"\n\n💰 *Settled Bankroll:* {br_str} | *Remaining Pending:* {still_pending}"
            )
            if adjustments:
                adj_lines = []
                for a in adjustments:
                    was_str = f" (was ${a['old_stake']:.2f})" if a.get("changed") and a.get("old_stake") else ""
                    adj_lines.append(f"• *{a['selection']}* — *${a['new_stake']:.2f}* ({a['wager_pct']}% BR{was_str})")
                receipt_msg += (
                    f"\n\n🔄 *Adjusted Wagers for Remaining Bets Today (${new_bankroll_val:.2f} BR):*\n"
                    + "\n".join(adj_lines)
                )
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": receipt_msg,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            print("Delivered Telegram settlement receipt.", flush=True)
        except Exception as tg_exc:
            print(f"Warning: Could not send Telegram settlement receipt: {tg_exc}", flush=True)

    return {
        "ok": True,
        "settled_count": updated_count,
        "settled_lines": settled_lines,
        "prev_bankroll": prev_bankroll,
        "new_bankroll": new_bankroll_val,
        "bankroll_changed": bankroll_changed,
        "adjusted_plays": adjustments,
    }


if __name__ == "__main__":
    run_settlement()

