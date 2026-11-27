import os
import re
import json
import time
import unicodedata
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
SHADOW_TAB_NAME = "Shadow Grading Log"
SHADOW_HEADERS = [
    "Date",
    "Sport",
    "Market",
    "Selection",
    "Odds",
    "Confidence %",
    "Pillars",
    "Execution",
    "Result",
    "Flat 1U P/L",
    "Score Note",
    "Graded At",
]

ESPN_LEAGUES = {
    "CFB": {"path": "football/college-football", "groups": "80"},
    "NFL": {"path": "football/nfl", "groups": None},
    "NBA": {"path": "basketball/nba", "groups": None},
    "NCAAM": {"path": "basketball/mens-college-basketball", "groups": "50"},
    "NHL": {"path": "hockey/nhl", "groups": None},
    "MLB": {"path": "baseball/mlb", "groups": None},
    "EPL": {"path": "soccer/eng.1", "groups": None},
    "UCL": {"path": "soccer/uefa.champions", "groups": None},
    "LALIGA": {"path": "soccer/esp.1", "groups": None},
}

_ASCII_TRANS = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ß": "ss",
    "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D",
})


def _normalize_ascii_text(text):
    s = str(text or "").translate(_ASCII_TRANS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s.lower())).strip()


def _canonical_sport(raw_sport):
    sp = str(raw_sport or "").strip().upper()
    if sp in ("CBB", "NCAAB", "MEN'S COLLEGE BASKETBALL", "COLLEGE BASKETBALL"):
        return "NCAAM"
    if sp in ("LA LIGA", "LA_LIGA", "SPANISH LA LIGA"):
        return "LALIGA"
    if sp in ("CHAMPIONS LEAGUE", "UEFA CHAMPIONS LEAGUE"):
        return "UCL"
    if sp in ("PREMIER LEAGUE", "ENGLISH PREMIER LEAGUE"):
        return "EPL"
    if sp == "NCAAF":
        return "CFB"
    return sp

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
    """Fetches player box-score stats for completed games to grade player props (NHL G/A/PTS/SOG, NBA PTS/REB/AST/PRA, NFL/CFB passing/rushing/receiving)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"
    players_out = {}
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return players_out
        box = res.json().get("boxscore", {})
        for team_block in box.get("players", []):
            for stat_group in team_block.get("statistics", []):
                group_name = str(stat_group.get("name", "") or "").strip().lower()
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
                    elif league_name in ("NFL", "CFB"):
                        p_dict = players_out.setdefault(ath_name, {})
                        try:
                            if group_name == "passing" and "YDS" in stat_map:
                                p_dict["PASS_YDS"] = int(float(stat_map.get("YDS", 0) or 0))
                                p_dict["PASS_TD"] = int(float(stat_map.get("TD", 0) or 0))
                                c_att = str(stat_map.get("C/ATT", "") or "")
                                if "/" in c_att:
                                    cp_s, att_s = c_att.split("/", 1)
                                    p_dict["COMP"] = int(float(cp_s or 0))
                                    p_dict["ATT"] = int(float(att_s or 0))
                            elif group_name == "rushing" and "YDS" in stat_map:
                                p_dict["RUSH_YDS"] = int(float(stat_map.get("YDS", 0) or 0))
                                p_dict["CAR"] = int(float(stat_map.get("CAR", 0) or 0))
                                p_dict["RUSH_TD"] = int(float(stat_map.get("TD", 0) or 0))
                            elif group_name == "receiving" and "YDS" in stat_map:
                                p_dict["REC_YDS"] = int(float(stat_map.get("YDS", 0) or 0))
                                p_dict["REC"] = int(float(stat_map.get("REC", 0) or 0))
                                p_dict["REC_TD"] = int(float(stat_map.get("TD", 0) or 0))
                        except Exception:
                            pass
    except Exception:
        pass
    return {k: v for k, v in players_out.items() if v}


def fetch_final_scores(target_dates, pending_bets=None):
    final_games = []
    pending_text = " ".join(
        f"{b.get('sport', '')} {b.get('selections', '')} {b.get('notes', '')}"
        for b in (pending_bets or [])
    ).lower()
    pending_text_norm = _normalize_ascii_text(pending_text)

    needed_leagues = set()
    prop_leagues = set()
    has_unscoped_sport = False
    for b in (pending_bets or []):
        sp_c = _canonical_sport(b.get("sport", ""))
        b_txt = f"{b.get('selections', '')} {b.get('notes', '')}".lower()
        is_prop = bool(
            re.search(
                r"\b(?:rebounds?|assists?|points?|shots?|sog|goals?|pra|pts|reb|ast|yards?|passing|rushing|receiving|receptions?|carries|completions?|attempts?|touchdowns?)\b",
                b_txt,
            )
        )
        if not sp_c or sp_c in ("MULTI-SPORT", "SOCCER", "OTHER"):
            has_unscoped_sport = True
            if is_prop:
                prop_leagues.update(("NHL", "NBA", "NFL"))
        else:
            needed_leagues.add(sp_c)
            if is_prop:
                prop_leagues.add(sp_c)

    for league_name, league_info in ESPN_LEAGUES.items():
        if pending_bets and not has_unscoped_sport and league_name not in needed_leagues:
            continue
        path = league_info["path"]
        for d in target_dates:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={d}"
            if league_info.get("groups"):
                url += f"&groups={league_info['groups']}&limit=500"

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

                home_team_obj = home.get("team", {})
                away_team_obj = away.get("team", {})
                home_name = home_team_obj.get("displayName", "")
                away_name = away_team_obj.get("displayName", "")
                home_short = home_team_obj.get("shortDisplayName", "").lower()
                away_short = away_team_obj.get("shortDisplayName", "").lower()
                home_nick = home_team_obj.get("name", "").lower()
                away_nick = away_team_obj.get("name", "").lower()
                home_abbr = home_team_obj.get("abbreviation", "").lower()
                away_abbr = away_team_obj.get("abbreviation", "").lower()
                home_score = int(float(home.get("score", 0) or 0))
                away_score = int(float(away.get("score", 0) or 0))

                team_matched = False
                if pending_bets:
                    match_tokens = [
                        tok for tok in (
                            home_name.lower(), away_name.lower(),
                            home_short, away_short,
                            home_nick, away_nick,
                        )
                        if tok and len(tok) >= 3
                    ]
                    norm_tokens = [_normalize_ascii_text(tok) for tok in match_tokens]
                    team_matched = (
                        any(tok in pending_text for tok in match_tokens)
                        or any(nt and len(nt) >= 3 and nt in pending_text_norm for nt in norm_tokens)
                        or any(
                            abbr and len(abbr) >= 3 and re.search(rf"\b{re.escape(abbr)}\b", pending_text)
                            for abbr in (home_abbr, away_abbr)
                        )
                    )
                    # Keep completed NHL/NBA/NFL games when there is a prop bet in that league (player props may omit team name)
                    is_relevant = team_matched or (is_completed and league_name in ("NHL", "NBA", "NFL") and league_name in prop_leagues)
                    if not is_relevant:
                        continue

                game_record = {
                    "league": league_name,
                    "date": d,
                    "matchup": f"{away_name} @ {home_name}",
                    "away_team": away_name,
                    "away_short": away_short,
                    "away_score": away_score,
                    "home_team": home_name,
                    "home_short": home_short,
                    "home_score": home_score,
                    "total_points": home_score + away_score,
                    "completed": is_completed,
                    "status": status_type.get("description", state)
                }

                # Attach player box-score stats ONLY for completed NBA/NHL/NCAAM/NFL/CFB games with a matching prop bet
                if (
                    is_completed
                    and event_id
                    and league_name in ("NHL", "NBA", "NCAAM", "NFL", "CFB")
                    and league_name in prop_leagues
                    and (team_matched or league_name in ("NHL", "NBA", "NFL"))
                ):
                    player_stats = fetch_boxscore_players(path, event_id, league_name)
                    if player_stats:
                        game_record["player_boxscore"] = player_stats

                final_games.append(game_record)

    return final_games


def _grade_straight_bet_deterministic(bet, final_games):
    """
    Deterministic fallback grader for Straight Spread / Moneyline / Total bets against completed ESPN games.
    Ensures completed straight bets settle reliably even if Gemini is unavailable or misses a game.
    """
    b_type = str(bet.get("bet_type", "") or "").strip().lower()
    if "parlay" in b_type or "sgp" in b_type:
        return None
    sel = str(bet.get("selections", "") or "").strip()
    if not sel or ("+" in sel and " + " in sel):
        return None
    if re.search(r"\b(?:1h|1st\s*half|first\s*half|rebounds?|assists?|shots?|sog|goals?|pra|yards|touchdowns?)\b", sel, flags=re.I):
        return None
    # Distinguish player points props (e.g. "Ryan Nugent-Hopkins Total Points Over 0.5") from game totals ("Over 216.5 Total Points")
    if re.search(r"\bpoints?\b", sel, flags=re.I):
        sel_after_dash = sel.split("—")[-1].strip() if "—" in sel else sel
        if not re.search(r"^(?:over|under)\s+\d{2,3}(?:\.\d+)?\b", sel_after_dash, flags=re.I) and not re.search(
            r"\b(?:over|under)\s+\d{2,3}(?:\.\d+)?\s+total\s+points?\b", sel_after_dash, flags=re.I
        ):
            return None

    sel_clean = re.sub(r"\([^)]*\)", "", sel).strip()
    if "—" in sel_clean:
        sel_clean = sel_clean.split("—")[-1].strip()
    elif "–" in sel_clean and "@" in sel_clean.split("–")[0]:
        sel_clean = sel_clean.split("–")[-1].strip()
    full_sel_l = _normalize_ascii_text(sel)
    sel_l = _normalize_ascii_text(sel_clean)
    sp = _canonical_sport(bet.get("sport", ""))

    _DIR_PREFIXES = ("western", "eastern", "central", "northern", "southern", "north", "south", "east", "west", "middle", "new", "coastal")
    has_explicit_matchup = ("@" in sel) or bool(re.search(r"\bvs\.?\b", sel, flags=re.I))

    def _alias_score(text_l, full_name, short_name):
        sc = 0
        fn = _normalize_ascii_text(full_name)
        sn = _normalize_ascii_text(short_name)
        if fn and re.search(rf"\b{re.escape(fn)}\b", text_l):
            sc = max(sc, 100 + len(fn))
        if len(sn) >= 3:
            m_sn = re.search(rf"\b{re.escape(sn)}\b", text_l)
            if m_sn:
                # Reject directional school collisions (e.g. "michigan" matching "western michigan" or "eastern michigan")
                prefix_words = text_l[: m_sn.start()].strip().split()
                prev_word = prefix_words[-1] if prefix_words else ""
                if prev_word in _DIR_PREFIXES and prev_word not in fn.split():
                    pass
                else:
                    sc = max(sc, 50 + len(sn))
        return sc

    matched_game = None
    picked_side = None
    best_total_score = 0
    for g in final_games:
        if not g.get("completed"):
            continue
        g_league = _canonical_sport(g.get("league", ""))
        if sp and sp != "MULTI-SPORT" and g_league and g_league != sp:
            continue
        h_name = str(g.get("home_team", ""))
        a_name = str(g.get("away_team", ""))
        h_short = str(g.get("home_short", ""))
        a_short = str(g.get("away_short", ""))

        h_sc_pick = _alias_score(sel_l, h_name, h_short)
        a_sc_pick = _alias_score(sel_l, a_name, a_short)
        if h_sc_pick == 0 and a_sc_pick == 0:
            # For Game Totals ("Team A @ Team B — Under 44.5"), match both teams in full_sel_l
            h_sc_full = _alias_score(full_sel_l, h_name, h_short)
            a_sc_full = _alias_score(full_sel_l, a_name, a_short)
            if h_sc_full > 0 and a_sc_full > 0 and re.search(r"\b(over|under)\s+\d+(?:\.\d+)?\b", sel_clean, flags=re.I):
                tot_sc = h_sc_full + a_sc_full
                if tot_sc > best_total_score:
                    best_total_score = tot_sc
                    matched_game = g
                    picked_side = None
            continue

        # Bonus (and requirement when full "Away @ Home" matchup is given) that the opponent team also matches
        opp_bonus = _alias_score(full_sel_l, a_name, a_short) if h_sc_pick >= a_sc_pick else _alias_score(full_sel_l, h_name, h_short)
        if has_explicit_matchup and opp_bonus == 0:
            continue
        total_score = max(h_sc_pick, a_sc_pick) + opp_bonus
        if total_score > best_total_score:
            best_total_score = total_score
            matched_game = g
            if h_sc_pick > a_sc_pick:
                picked_side = "home"
            elif a_sc_pick > h_sc_pick:
                picked_side = "away"
            else:
                picked_side = None

    if not matched_game:
        return None

    h_team = matched_game["home_team"]
    a_team = matched_game["away_team"]
    h_sc = int(matched_game["home_score"])
    a_sc = int(matched_game["away_score"])
    tot = h_sc + a_sc
    score_str = f"{a_team} {a_sc} - {h_team} {h_sc}"

    # 1. Total (Over / Under)
    m_ou = re.search(r"\b(over|under)\s+(\d+(?:\.\d+)?)\b", sel_clean, flags=re.I)
    if m_ou:
        side = m_ou.group(1).lower()
        line = float(m_ou.group(2))
        if abs(tot - line) < 1e-9:
            res = "Push"
        elif (side == "over" and tot > line) or (side == "under" and tot < line):
            res = "Won"
        else:
            res = "Lost"
        tag = "WIN: Disciplined Straight / Floor Play" if res == "Won" else ("FAIL: Totals Blowout / Late Garbage Time" if res == "Lost" else "")
        return {
            "row_index": bet.get("row_index"),
            "bet_id": bet.get("bet_id", ""),
            "result": res,
            "edge_tag": tag,
            "verification_note": f"{score_str} (Total {tot} vs {side.title()} {line:g}).",
        }

    if not picked_side:
        return None

    my_sc = h_sc if picked_side == "home" else a_sc
    opp_sc = a_sc if picked_side == "home" else h_sc
    my_team = h_team if picked_side == "home" else a_team

    # 2. Moneyline / PK
    if re.search(r"\b(?:ml|moneyline|to\s+win)\b", sel_clean, flags=re.I):
        is_soccer = sp in ("EPL", "UCL", "LALIGA", "SOCCER") or matched_game.get("league") in ("EPL", "UCL", "LALIGA")
        if my_sc > opp_sc:
            res = "Won"
        elif my_sc == opp_sc and not is_soccer:
            res = "Push"
        else:
            res = "Lost"
        tag = "WIN: Disciplined Straight / Floor Play" if res == "Won" else ("FAIL: Hook / Half-Point Variance" if res == "Lost" else "")
        return {
            "row_index": bet.get("row_index"),
            "bet_id": bet.get("bet_id", ""),
            "result": res,
            "edge_tag": tag,
            "verification_note": f"{score_str} ({my_team} ML).",
        }

    # 3. Point Spread (e.g. -5.5, +3.5, -6, PK)
    m_sp = re.search(r"(?:^|\s)([+-]\d+(?:\.\d+)?)(?:\s|$)", sel_clean)
    if m_sp or re.search(r"\bpk\b", sel_clean, flags=re.I):
        spread = float(m_sp.group(1)) if m_sp else 0.0
        adj = my_sc + spread
        if abs(adj - opp_sc) < 1e-9:
            res = "Push"
        elif adj > opp_sc:
            res = "Won"
        else:
            res = "Lost"
        tag = "WIN: Disciplined Straight / Floor Play" if res == "Won" else ("FAIL: Hook / Half-Point Variance" if res == "Lost" else "")
        return {
            "row_index": bet.get("row_index"),
            "bet_id": bet.get("bet_id", ""),
            "result": res,
            "edge_tag": tag,
            "verification_note": f"{score_str} ({my_team} {spread:+g}: {adj:g} vs {opp_sc}).",
        }

    return None


def _grade_player_prop_deterministic(bet, final_games):
    """
    Deterministic grader for Straight Player Props (NBA/NCAAM PTS/REB/AST/PRA, NHL PTS/G/A/SOG)
    against completed ESPN player_boxscore records. Returns None if player is absent (DNP) or not matched.
    """
    b_type = str(bet.get("bet_type", "") or "").strip().lower()
    if "parlay" in b_type or "sgp" in b_type:
        return None
    sel = str(bet.get("selections", "") or "").strip()
    if not sel or " + " in sel:
        return None

    pick = re.sub(r"\([^)]*\)", "", sel).strip()
    if "—" in pick:
        pick = pick.split("—")[-1].strip()
    elif "–" in pick and "@" in pick.split("–")[0]:
        pick = pick.split("–")[-1].strip()

    # Must have Over/Under + numeric line
    m_ou = re.search(r"\b(over|under|o|u)\s*(\d+(?:\.\d+)?)\b", pick, flags=re.I)
    if not m_ou:
        return None
    raw_side = m_ou.group(1).lower()
    side = "over" if raw_side in ("over", "o") else "under"
    line = float(m_ou.group(2))

    # Identify stat category
    stat_patterns = [
        ("PASS_YDS", r"\b(?:passing\s+yards?|pass\s+yards?|pass\s+yds)\b"),
        ("RUSH_YDS", r"\b(?:rushing\s+yards?|rush\s+yards?|rush\s+yds)\b"),
        ("REC_YDS", r"\b(?:receiving\s+yards?|rec\s+yards?|rec\s+yds)\b"),
        ("CAR", r"\b(?:rushing\s+attempts?|rush\s+attempts?|carries)\b"),
        ("REC", r"\b(?:receptions?|catches)\b"),
        ("COMP", r"\b(?:completions?|pass\s+completions?)\b"),
        ("PRA", r"\b(?:pts\s*\+\s*reb\s*\+\s*ast|points\s*\+\s*rebounds\s*\+\s*assists|pra)\b"),
        ("SOG", r"\b(?:total\s+)?(?:shots\s+on\s+goal|shots|sog)\b"),
        ("REB", r"\b(?:total\s+)?(?:rebounds?|rebs?)\b"),
        ("AST", r"\b(?:total\s+)?(?:assists?|asts?)\b"),
        ("G", r"\b(?:total\s+)?(?:goals?)\b"),
        ("PTS", r"\b(?:total\s+)?(?:points?|pts)\b"),
    ]
    stat_key = None
    stat_pos = len(pick)
    for sk, pat in stat_patterns:
        ms = re.search(pat, pick, flags=re.I)
        if ms:
            stat_key = sk
            stat_pos = min(stat_pos, ms.start())
            break
    if not stat_key:
        return None

    sp = _canonical_sport(bet.get("sport", ""))
    full_sel_norm = _normalize_ascii_text(sel)

    # Candidate player name is text before whichever comes first: stat keyword or Over/Under
    cut_pos = min(stat_pos, m_ou.start())
    cand_raw = re.sub(r"\btotal\b\s*$", "", pick[:cut_pos], flags=re.I).strip(" -:|")
    cand_norm = _normalize_ascii_text(cand_raw)
    cand_tokens = [t for t in cand_norm.split() if len(t) >= 2]

    # Nameless QB Passing Yards fallback (e.g. "Under 201.5 Passing Yards (Eagles @ Cowboys)"):
    # If the matchup is unambiguous and ALL primary QBs (>= 10 pass attempts) in that game land on the SAME side of the line, grade deterministically!
    if (not cand_norm or len(cand_norm) < 3 or cand_norm[0].isdigit()) and stat_key == "PASS_YDS":
        for g in final_games:
            if not g.get("completed"):
                continue
            pbox = g.get("player_boxscore") or {}
            if not pbox:
                continue
            g_league = _canonical_sport(g.get("league", ""))
            if sp and sp != "MULTI-SPORT" and g_league and g_league != sp:
                continue
            h_toks = [_normalize_ascii_text(g.get("home_team", "")), _normalize_ascii_text(g.get("home_short", ""))]
            a_toks = [_normalize_ascii_text(g.get("away_team", "")), _normalize_ascii_text(g.get("away_short", ""))]
            h_hit = any(t and len(t) >= 3 and t in full_sel_norm for t in h_toks)
            a_hit = any(t and len(t) >= 3 and t in full_sel_norm for t in a_toks)
            if not (h_hit and a_hit):
                continue
            qbs = [(nm, st) for nm, st in pbox.items() if "PASS_YDS" in st and int(st.get("ATT", 0) or 0) >= 10]
            if not qbs:
                continue
            outcomes = set()
            qb_notes = []
            for nm, st in qbs:
                val = float(st["PASS_YDS"])
                if abs(val - line) < 1e-9:
                    outcomes.add("Push")
                elif (side == "over" and val > line) or (side == "under" and val < line):
                    outcomes.add("Won")
                else:
                    outcomes.add("Lost")
                qb_notes.append(f"{nm}: {int(val)} PASS_YDS")
            if len(outcomes) == 1:
                res = next(iter(outcomes))
                tag = "WIN: Disciplined Straight / Floor Play" if res == "Won" else ("FAIL: Hook / Half-Point Variance" if res == "Lost" else "")
                score_str = f"{g['away_team']} {int(g['away_score'])} - {g['home_team']} {int(g['home_score'])}"
                return {
                    "row_index": bet.get("row_index"),
                    "bet_id": bet.get("bet_id", ""),
                    "result": res,
                    "edge_tag": tag,
                    "verification_note": f"{score_str} ({', '.join(qb_notes)} vs {side.title()} {line:g}).",
                }
        return None

    if not cand_norm or len(cand_norm) < 3 or cand_norm[0].isdigit():
        return None

    best_match = None
    best_score = 0
    for g in final_games:
        if not g.get("completed"):
            continue
        pbox = g.get("player_boxscore") or {}
        if not pbox:
            continue
        g_league = _canonical_sport(g.get("league", ""))
        if sp and sp != "MULTI-SPORT" and g_league and g_league != sp:
            continue

        h_norm = _normalize_ascii_text(g.get("home_team", ""))
        a_norm = _normalize_ascii_text(g.get("away_team", ""))
        hs_norm = _normalize_ascii_text(g.get("home_short", ""))
        as_norm = _normalize_ascii_text(g.get("away_short", ""))
        team_bonus = 15 if any(t and len(t) >= 3 and t in full_sel_norm for t in (h_norm, a_norm, hs_norm, as_norm)) else 0

        for ath_name, stats in pbox.items():
            if stat_key not in stats:
                continue
            ath_norm = _normalize_ascii_text(ath_name)
            ath_tokens = [t for t in ath_norm.split() if len(t) >= 2]
            sc = 0
            if cand_norm == ath_norm:
                sc = 100
            elif len(cand_tokens) >= 2 and all(ct in ath_tokens for ct in cand_tokens):
                sc = 90
            elif len(cand_tokens) >= 2 and len(ath_tokens) >= 2 and cand_tokens[-1] == ath_tokens[-1] and cand_tokens[0][0] == ath_tokens[0][0]:
                sc = 80
            elif len(cand_tokens) == 1 and len(cand_tokens[0]) >= 4 and ath_tokens and cand_tokens[0] == ath_tokens[-1] and team_bonus > 0:
                sc = 60

            if sc > 0 and (sc + team_bonus) > best_score:
                best_score = sc + team_bonus
                best_match = (g, ath_name, stats)

    if not best_match:
        return None

    g, ath_name, stats = best_match
    actual = float(stats[stat_key])
    if abs(actual - line) < 1e-9:
        res = "Push"
    elif (side == "over" and actual > line) or (side == "under" and actual < line):
        res = "Won"
    else:
        res = "Lost"

    h_team = g["home_team"]
    a_team = g["away_team"]
    h_sc = int(g["home_score"])
    a_sc = int(g["away_score"])
    actual_disp = int(actual) if actual.is_integer() else actual
    tag = "WIN: Disciplined Straight / Floor Play" if res == "Won" else ("FAIL: Hook / Half-Point Variance" if res == "Lost" else "")
    return {
        "row_index": bet.get("row_index"),
        "bet_id": bet.get("bet_id", ""),
        "result": res,
        "edge_tag": tag,
        "verification_note": f"{a_team} {a_sc} - {h_team} {h_sc} ({ath_name}: {actual_disp} {stat_key} vs {side.title()} {line:g}).",
    }


# --- 3. GRADE & AUTO-TAG PENDING TICKETS (DETERMINISTIC FIRST, GEMINI FOR PARLAYS/EXOTIC PROPS) ---
def grade_pending_bets(pending_bets, final_games):
    graded_map = {}
    remaining_for_gemini = []

    # 1. Deterministic exact arithmetic first for Straight Spread / ML / Totals / Player Props
    for bet in pending_bets:
        bid = str(bet.get("bet_id", "")).strip()
        det = _grade_straight_bet_deterministic(bet, final_games) or _grade_player_prop_deterministic(bet, final_games)
        if det and bid:
            graded_map[bid] = det
        else:
            remaining_for_gemini.append(bet)

    if not remaining_for_gemini or not GEMINI_API_KEY:
        return list(graded_map.values())

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
{json.dumps(remaining_for_gemini, indent=2)}

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

    if response is not None:
        try:
            for s in json.loads(response.text).get("settlements", []):
                bid = str(s.get("bet_id", "")).strip()
                res_v = str(s.get("result", "")).strip()
                if bid and bid not in graded_map and res_v in ("Won", "Lost", "Push"):
                    graded_map[bid] = s
        except Exception as e:
            print(f"Error parsing Gemini settlement response: {e}", flush=True)
            print("Raw model response:", response.text, flush=True)

    return list(graded_map.values())

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
    # Handle "7:00 PM EST • Away @ Home (CFB) — Western Michigan -4.5 (-105) — $3.50"
    if "—" in s:
        parts = [p.strip() for p in s.split("—") if p.strip()]
        if len(parts) >= 2 and ("@" in parts[0] or "•" in parts[0] or re.search(r"\b(?:AM|PM)\s+EST\b", parts[0], flags=re.I)):
            s = parts[1]
    core = re.split(
        r"(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)",
        s,
        flags=re.I,
    )[0]
    core = re.sub(r"\b(?:total\s+)?(?:points?|rebounds?|assists?|shots(?:\s+on\s+goal)?|sog|goals?|pra|pts|reb|ast)\s*$", "", core, flags=re.I).strip()
    return _normalize_ascii_text(core)


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
        if not core or len(core) < 2:
            continue

        m_sp = re.search(r"[\(\[•]\s*(CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA|SOCCER)\s*[\)\]]", pr["clean_body"], flags=re.I)
        pr_sport = _canonical_sport(m_sp.group(1)) if m_sp else ""
        core_re = re.compile(rf"\b{re.escape(core)}\b")

        def _ticket_matches(t):
            t_sp = _canonical_sport(t.get("sport", ""))
            if pr_sport and t_sp and pr_sport not in ("MULTI-SPORT", "OTHER") and t_sp not in ("MULTI-SPORT", "OTHER") and pr_sport != t_sp:
                return False
            return bool(core_re.search(_normalize_ascii_text(t.get("selections", ""))))

        is_pending = any(_ticket_matches(t) for t in pending_tickets)
        is_settled_today = any(_ticket_matches(t) for t in today_tickets)
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
            feed_ws.update_acell(f"L{target_row_idx}", updated_text[:25000])
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


# --- 5. SHADOW GRADING ENGINE (ALL CURATED PLAYS: PLACED + SKIPPED) ---
def _classify_shadow_market(sel_text):
    s = str(sel_text or "").strip().lower()
    if "—" in s:
        s = s.split("—")[-1].strip()
    if re.search(r"^(?:over|under)\s+\d{2,3}(?:\.\d+)?\b", s) or re.search(
        r"\b(?:over|under)\s+\d{2,3}(?:\.\d+)?\s+total\b", s
    ):
        return "Game Total"
    if re.search(
        r"\b(?:assists?|points?|rebounds?|threes?|3-?pointers?|shots(?:\s+on\s+goal)?|sog|goals?|yards?|touchdowns?|hits|strikeouts?|saves|pra|pts|reb|ast)\b",
        s,
    ):
        return "Player Prop"
    if re.search(r"\b(?:ml|moneyline|to\s+win)\b", s):
        return "Moneyline"
    if re.search(r"(?:^|\s)[+-]\d+(?:\.\d+)?(?:\s|$)|\bpk\b", s):
        return "Spread"
    return "Other"


def _calc_flat_unit_pl(result, odds_str):
    rl = str(result or "").strip().lower()
    if rl == "push":
        return 0.0
    if rl == "lost":
        return -1.0
    if rl != "won":
        return 0.0
    raw = str(odds_str or "-110").strip().replace("+", "")
    try:
        o = int(float(raw))
    except Exception:
        o = -110
    if o == 0:
        o = -110
    return round(o / 100.0, 2) if o > 0 else round(100.0 / abs(o), 2)


def _shadow_dedup_key(date_str, selection_str, market_str=None):
    core = _extract_selection_core(selection_str)
    mkt = market_str or _classify_shadow_market(selection_str)
    return f"{str(date_str or '').strip()}|{core}|{mkt}"


def _parse_curated_play_line(raw_title, sub_bullets, row_date):
    clean = str(raw_title or "").replace("*", "").strip()
    if (
        not clean
        or "⛔" in clean
        or "NO GOOD PLAY" in clean.upper()
        or bool(re.search(r"\bPASS\b", clean.upper()))
    ):
        return None

    # Extract trailing confidence % if present (e.g. "— 77% ✅" or "— [87% 💎]")
    conf_pct = None
    m_tail_conf = re.search(r"[—-]\s*\[?(\d{2,3})%\s*([^\]]*)\]?$", clean)
    if m_tail_conf:
        ctx = (m_tail_conf.group(2) or "").upper()
        if "BR" not in ctx and "BANKROLL" not in ctx and "RESULTING" not in ctx:
            conf_pct = int(m_tail_conf.group(1))
        clean = clean[: m_tail_conf.start()].strip()

    # Strip trailing stake / units chunk ("— $3.50 (1.91U)" or "— $6.50")
    clean = re.sub(r"\s*[—-]\s*\$\d+(?:\.\d{1,2})?(?:\s*\([^)]*\))?\s*$", "", clean).strip()

    sport = ""
    matchup = ""
    selection_chunk = clean

    # Format A (new): "7:00 PM EST • Away @ Home (CFB) — Western Michigan -4.5 (-105)"
    if "—" in clean:
        parts = [p.strip() for p in clean.split("—") if p.strip()]
        if len(parts) >= 2:
            head_part = parts[0]
            selection_chunk = parts[1]
            # Extract sport from "(CFB)" or "[... • CFB]" in header
            m_sp = re.search(
                r"[\(\[•]\s*(CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA|SOCCER)\s*[\)\]]",
                head_part,
                flags=re.I,
            )
            if m_sp:
                sport = _canonical_sport(m_sp.group(1))
            # Extract matchup after bullet/time
            m_clean = re.sub(r"^\[[^\]]+\]\s*", "", head_part).strip()
            m_clean = re.sub(r"^\d{1,2}:\d{2}\s*[AP]M\s*EST\s*[•\-|]?\s*", "", m_clean, flags=re.I).strip()
            m_clean = re.sub(r"\s*\((?:CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA|SOCCER)\)\s*$", "", m_clean, flags=re.I).strip()
            if "@" in m_clean or " vs " in m_clean.lower():
                matchup = m_clean
    else:
        # Format B (legacy): "[7:00 PM EST • NBA] Detroit Pistons ML (-135)"
        m_br = re.match(r"^\[([^\]]+)\]\s*(.+)$", clean)
        if m_br:
            hdr = m_br.group(1)
            selection_chunk = m_br.group(2).strip()
            m_sp = re.search(
                r"\b(CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA|SOCCER)\b",
                hdr,
                flags=re.I,
            )
            if m_sp:
                sport = _canonical_sport(m_sp.group(1))

    if not sport:
        m_sp_any = re.search(
            r"\((CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA)\)",
            clean,
            flags=re.I,
        )
        if m_sp_any:
            sport = _canonical_sport(m_sp_any.group(1))

    # Extract American odds from selection chunk (e.g. "(-105)" or "(+125)")
    odds = "-110"
    m_odds = re.search(r"\(([+-]\d{3,4})\)", selection_chunk)
    if m_odds:
        odds = m_odds.group(1)
        selection_chunk = (selection_chunk[: m_odds.start()] + selection_chunk[m_odds.end() :]).strip()

    # Clean stray parentheticals like "(7:00 PM EST)" or "(Straight)"
    selection_clean = re.sub(r"\s*\(\d{1,2}:\d{2}\s*[AP]M\s*EST\)", "", selection_chunk, flags=re.I).strip()
    selection_clean = re.sub(r"\s{2,}", " ", selection_clean).strip(" —-")
    if not selection_clean or len(selection_clean) < 3:
        return None

    # Parse bullet lines for Confidence Math / Confidence % / Pillars
    pillars = ""
    for b in (sub_bullets or []):
        bl = b.lower()
        if bl.startswith("confidence math:"):
            cm = re.sub(r"^confidence math:\s*", "", b, flags=re.I).strip()
            pm = re.search(r"=\s*(\d{2,3})\s*%", cm) or re.search(r"(\d{2,3})\s*%", cm)
            if pm:
                conf_pct = int(pm.group(1))
            # Strip "Base 50% + " prefix and " = 77% (Tier ...)" suffix for clean pillar summary
            pil = re.sub(r"^Base\s*\d+%\s*\+\s*", "", cm, flags=re.I)
            pil = re.sub(r"\s*=\s*\d{2,3}%.*$", "", pil).strip()
            pillars = pil[:140]
        elif bl.startswith("confidence:"):
            cb = re.sub(r"^confidence:\s*", "", b, flags=re.I).strip(" []")
            pm = re.search(r"(\d{2,3})\s*%", cb)
            if pm and conf_pct is None:
                conf_pct = int(pm.group(1))
        elif bl.startswith("edge:") and not pillars:
            pillars = re.sub(r"^edge:\s*", "", b, flags=re.I).strip()[:140]

    market = _classify_shadow_market(selection_clean)
    full_sel = f"{matchup} — {selection_clean}" if matchup else selection_clean
    return {
        "date": row_date,
        "sport": sport or "OTHER",
        "market": market,
        "selection": selection_clean,
        "full_selection": full_sel,
        "odds": odds,
        "confidence_pct": conf_pct if conf_pct is not None else "",
        "pillars": pillars,
        "dedup_key": _shadow_dedup_key(row_date, selection_clean, market),
    }


def _extract_curated_plays_from_feed(feed_vals, lookback_days=4, today_str=None):
    """
    Extracts deduplicated curated plays from 'Curator Feed' rows over the last `lookback_days`
    (inclusive of today EST, excluding audit-only and PASS rows).
    """
    if not today_str:
        today_str = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")
    try:
        today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    except Exception:
        today_dt = datetime.now(timezone(timedelta(hours=-5))).date()
    cutoff_dt = today_dt - timedelta(days=max(1, int(lookback_days)))

    plays_by_key = {}
    for r in (feed_vals or []):
        if len(r) < 12 or not str(r[11]).strip():
            continue
        row_date = str(r[0]).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", row_date):
            continue
        try:
            rd = datetime.strptime(row_date, "%Y-%m-%d").date()
        except Exception:
            continue
        if rd < cutoff_dt or rd > today_dt:
            continue

        raw_txt = str(r[11]).strip()
        # Skip pre-tipoff audit-only rows or rows without numbered plays
        if "primetime pre-tipoff audit" in raw_txt.lower() and not re.search(r"^\s*1\.\s+", raw_txt, flags=re.M):
            continue
        if not re.search(r"^\s*1\.\s+", raw_txt, flags=re.M):
            continue

        lines = raw_txt.splitlines()
        cur_title = None
        cur_bullets = []
        for ln in lines:
            s = ln.strip()
            m = re.match(r"^\d+\.\s+(.+)$", s)
            if m:
                if cur_title:
                    parsed = _parse_curated_play_line(cur_title, cur_bullets, row_date)
                    if parsed:
                        plays_by_key[parsed["dedup_key"]] = parsed
                cur_title = m.group(1)
                cur_bullets = []
            elif cur_title and (s.startswith("- ") or s.startswith("• ")):
                cur_bullets.append(re.sub(r"^[-•]\s*", "", s).replace("*", "").replace("`", "").strip())
        if cur_title:
            parsed = _parse_curated_play_line(cur_title, cur_bullets, row_date)
            if parsed:
                plays_by_key[parsed["dedup_key"]] = parsed

    return list(plays_by_key.values())


def _get_or_create_shadow_sheet(workbook):
    try:
        ws = workbook.worksheet(SHADOW_TAB_NAME)
    except Exception:
        ws = workbook.add_worksheet(title=SHADOW_TAB_NAME, rows=500, cols=len(SHADOW_HEADERS))
        ws.append_row(SHADOW_HEADERS, value_input_option="USER_ENTERED")
        return ws

    existing = ws.get_all_values()
    if not existing:
        ws.append_row(SHADOW_HEADERS, value_input_option="USER_ENTERED")
    return ws


def _was_curated_play_placed(play, tracker_entries):
    """Cross-references a curated play against Challenge Tracker rows within +/- 1 day (sport + word-boundary safe)."""
    p_core = _extract_selection_core(play.get("selection", ""))
    if not p_core or len(p_core) < 2:
        return False
    p_sport = _canonical_sport(play.get("sport", ""))
    p_re = re.compile(rf"\b{re.escape(p_core)}\b")
    try:
        p_dt = datetime.strptime(play["date"], "%Y-%m-%d").date()
    except Exception:
        p_dt = None

    for te in tracker_entries:
        if p_dt and te.get("date_obj"):
            if abs((te["date_obj"] - p_dt).days) > 1:
                continue
        t_sport = te.get("sport", "")
        if p_sport and t_sport and p_sport not in ("MULTI-SPORT", "OTHER") and t_sport not in ("MULTI-SPORT", "OTHER") and p_sport != t_sport:
            continue
        if p_re.search(te["sel_norm"]):
            return True
    return False


def run_shadow_grading(workbook, tracker_all_values=None, existing_final_games=None, lookback_days=4):
    """
    Deterministically shadow-grades all curated plays (both Placed and Skipped) from the last
    `lookback_days` in Curator Feed against completed ESPN games + boxscores, persisting unique
    graded rows to the 'Shadow Grading Log' worksheet tab.
    """
    est_now = datetime.now(timezone(timedelta(hours=-5)))
    today_str = est_now.strftime("%Y-%m-%d")
    graded_at_str = est_now.strftime("%Y-%m-%d %I:%M %p EST")

    try:
        feed_ws = workbook.worksheet(FEED_TAB_NAME)
        feed_vals = feed_ws.get_all_values()
    except Exception as exc:
        print(f"Shadow Grading: Could not open {FEED_TAB_NAME}: {exc}", flush=True)
        return {"graded_count": 0, "placed_count": 0, "skipped_count": 0, "wins": 0, "losses": 0, "pushes": 0}

    curated_plays = _extract_curated_plays_from_feed(feed_vals, lookback_days=lookback_days, today_str=today_str)
    if not curated_plays:
        print("Shadow Grading: No curated plays found in lookback window.", flush=True)
        return {"graded_count": 0, "placed_count": 0, "skipped_count": 0, "wins": 0, "losses": 0, "pushes": 0}

    shadow_ws = _get_or_create_shadow_sheet(workbook)
    shadow_rows = shadow_ws.get_all_values()
    existing_keys = set()
    for r in shadow_rows[1:]:
        if len(r) >= 4 and str(r[0]).strip() and str(r[3]).strip():
            mkt = str(r[2]).strip() if len(r) > 2 else ""
            existing_keys.add(_shadow_dedup_key(r[0], r[3], mkt or None))

    ungraded = [p for p in curated_plays if p["dedup_key"] not in existing_keys]
    if not ungraded:
        print("Shadow Grading: All recent curated plays are already shadow-graded.", flush=True)
        return {"graded_count": 0, "placed_count": 0, "skipped_count": 0, "wins": 0, "losses": 0, "pushes": 0}

    # Build normalized tracker index for Placed vs Skipped detection
    if tracker_all_values is None:
        tracker_all_values = get_tracker_sheet(workbook).get_all_values()
    header_idx = next((i for i, r in enumerate(tracker_all_values) if "Bet ID" in r and "Result" in r), 15)
    tracker_entries = []
    for r in tracker_all_values[header_idx + 1 :]:
        if not r or not str(r[0]).strip().startswith("BET-"):
            continue
        b_date = str(r[1]).strip() if len(r) > 1 else ""
        b_sport = _canonical_sport(r[2]) if len(r) > 2 else ""
        sel_txt = str(r[5]).strip() if len(r) > 5 else ""
        try:
            d_obj = datetime.strptime(b_date, "%Y-%m-%d").date()
        except Exception:
            d_obj = None
        tracker_entries.append({
            "date_obj": d_obj,
            "sport": b_sport,
            "sel_norm": _normalize_ascii_text(sel_txt),
        })

    # Collect scoreboard dates needed (play date + next UTC day)
    shadow_dates = set()
    shadow_bet_objs = []
    for idx, p in enumerate(ungraded):
        d_clean = p["date"].replace("-", "")
        if len(d_clean) == 8 and d_clean.isdigit():
            shadow_dates.add(d_clean)
            dt_obj = datetime.strptime(d_clean, "%Y%m%d")
            shadow_dates.add((dt_obj + timedelta(days=1)).strftime("%Y%m%d"))
        shadow_bet_objs.append({
            "bet_id": f"SHADOW-{idx + 1}",
            "sport": p["sport"],
            "bet_type": "Straight",
            "selections": p["full_selection"],
            "notes": p["pillars"],
        })

    # Fetch ESPN scores for ungraded shadow plays and merge with any already-fetched games
    fetched_games = fetch_final_scores(sorted(shadow_dates), pending_bets=shadow_bet_objs) if shadow_dates else []
    combined_games = list(existing_final_games or []) + fetched_games

    new_rows = []
    placed_cnt = 0
    skipped_cnt = 0
    wins_cnt = 0
    losses_cnt = 0
    pushes_cnt = 0

    for p, bet_obj in zip(ungraded, shadow_bet_objs):
        # Strictly scope candidate games to this play's date (plus next UTC day) so unplayed games never match earlier days
        d_clean = p["date"].replace("-", "")
        valid_dates = set()
        if len(d_clean) == 8 and d_clean.isdigit():
            valid_dates.add(d_clean)
            dt_obj = datetime.strptime(d_clean, "%Y%m%d")
            valid_dates.add((dt_obj + timedelta(days=1)).strftime("%Y%m%d"))
        play_games = [
            g for g in combined_games
            if not valid_dates or not g.get("date") or str(g.get("date")).replace("-", "") in valid_dates
        ]
        graded = _grade_straight_bet_deterministic(bet_obj, play_games) or _grade_player_prop_deterministic(bet_obj, play_games)
        if not graded:
            continue
        res = graded.get("result", "").strip()
        if res not in ("Won", "Lost", "Push"):
            continue
        execution = "Placed" if _was_curated_play_placed(p, tracker_entries) else "Skipped"
        flat_pl = _calc_flat_unit_pl(res, p["odds"])

        if execution == "Placed":
            placed_cnt += 1
        else:
            skipped_cnt += 1
        if res == "Won":
            wins_cnt += 1
        elif res == "Lost":
            losses_cnt += 1
        else:
            pushes_cnt += 1

        new_rows.append([
            p["date"],
            p["sport"],
            p["market"],
            p["selection"],
            p["odds"],
            p["confidence_pct"],
            p["pillars"],
            execution,
            res,
            flat_pl,
            graded.get("verification_note", ""),
            graded_at_str,
        ])

    if new_rows:
        shadow_ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(
            f"Shadow Grading: Logged {len(new_rows)} curated play(s) "
            f"({wins_cnt}W-{losses_cnt}L-{pushes_cnt}P | {placed_cnt} Placed, {skipped_cnt} Skipped).",
            flush=True,
        )
    else:
        print(f"Shadow Grading: Checked {len(ungraded)} ungraded play(s); none completed/gradable yet.", flush=True)

    return {
        "graded_count": len(new_rows),
        "placed_count": placed_cnt,
        "skipped_count": skipped_cnt,
        "wins": wins_cnt,
        "losses": losses_cnt,
        "pushes": pushes_cnt,
    }


# --- 6. EXECUTION ENTRYPOINT ---
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

    # Normalize any legacy CBB / NCAAB sport cells in Col C to canonical NCAAM and clean up dropdown validation
    if col_sport is not None:
        sport_col_letter = chr(ord("A") + col_sport)
        normalized_sport_rows = 0
        for r_idx in range(header_row_idx + 1, len(all_values)):
            row = all_values[r_idx]
            if len(row) > col_sport:
                raw_sp = row[col_sport].strip()
                raw_u = raw_sp.upper()
                if raw_u in ("CBB", "NCAAB", "MEN'S COLLEGE BASKETBALL", "COLLEGE BASKETBALL"):
                    sheet.update_acell(f"{sport_col_letter}{r_idx + 1}", "NCAAM")
                    row[col_sport] = "NCAAM"
                    normalized_sport_rows += 1
                elif raw_u in ("LA LIGA", "LA_LIGA", "SPANISH LA LIGA"):
                    sheet.update_acell(f"{sport_col_letter}{r_idx + 1}", "LALIGA")
                    row[col_sport] = "LALIGA"
                    normalized_sport_rows += 1
                elif raw_u in ("CHAMPIONS LEAGUE", "UEFA CHAMPIONS LEAGUE"):
                    sheet.update_acell(f"{sport_col_letter}{r_idx + 1}", "UCL")
                    row[col_sport] = "UCL"
                    normalized_sport_rows += 1
        if normalized_sport_rows:
            print(f"Normalized {normalized_sport_rows} spreadsheet row(s) to canonical sport labels.", flush=True)
        try:
            canonical_sports = ["CFB", "NFL", "NBA", "NCAAM", "MLB", "NHL", "Soccer", "EPL", "UCL", "LALIGA", "ATP", "WTA", "Multi-Sport", "Other"]
            sheet.spreadsheet.batch_update({
                "requests": [{
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet.id,
                            "startRowIndex": header_row_idx + 1,
                            "endRowIndex": max(len(all_values) + 50, 200),
                            "startColumnIndex": col_sport,
                            "endColumnIndex": col_sport + 1,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [{"userEnteredValue": v} for v in canonical_sports],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                }]
            })
        except Exception as dv_exc:
            print(f"Warning: Could not refresh Sport/League dropdown validation: {dv_exc}", flush=True)

    regrade_ids = {
        x.strip().upper()
        for x in os.environ.get("REGRADE_BET_ID", "").split(",")
        if x.strip()
    }
    pending_bets = []
    dates_needed = set()

    for r_idx in range(header_row_idx + 1, len(all_values)):
        row = all_values[r_idx]
        if len(row) <= col_result:
            continue

        bet_id = row[col_bet_id].strip()
        result_val = row[col_result].strip().lower()

        if bet_id and (result_val == "pending" or bet_id.upper() in regrade_ids):
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
    final_games = []

    if not pending_bets:
        print("No pending bets found to settle. Checking remaining bet wager sync...", flush=True)
    else:
        now_utc = datetime.now(timezone.utc)
        dates_needed.add(now_utc.strftime("%Y%m%d"))
        dates_needed.add((now_utc - timedelta(days=1)).strftime("%Y%m%d"))

        print(f"Found {len(pending_bets)} pending/regrade bet(s). Fetching ESPN scores for {sorted(dates_needed)}...", flush=True)
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
                    existing_note = re.sub(r"\s*\|\s*\[Settled:[^\]]*\]", "", existing_note).strip()
                    existing_note = re.sub(r"^\[Settled:[^\]]*\]\s*\|?\s*", "", existing_note).strip()
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

    # Shadow-grade all completed curated plays (Placed + Skipped) safely without ever blocking live settlement
    shadow_summary = {"graded_count": 0, "placed_count": 0, "skipped_count": 0, "wins": 0, "losses": 0, "pushes": 0}
    try:
        shadow_summary = run_shadow_grading(
            workbook,
            tracker_all_values=refreshed,
            existing_final_games=final_games,
        )
    except Exception as sh_exc:
        print(f"Warning: Shadow grading failed safely without impacting settlement: {sh_exc}", flush=True)

    if (
        updated_count > 0
        or recalc["feed_updated"]
        or shadow_summary.get("graded_count", 0) > 0
        or _check_static_data_drift(new_bankroll_val, still_pending)
    ):
        try:
            with open(".tg_changed", "w", encoding="utf-8") as f:
                f.write(str(max(1, updated_count + shadow_summary.get("graded_count", 0))))
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
        "shadow_summary": shadow_summary,
    }


if __name__ == "__main__":
    run_settlement()

