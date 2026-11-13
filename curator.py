import os
import re
import json
import time
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types, errors

# --- CONFIGURATION ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"

# Priority ordered by user preference (#1 CFB, #2 NFL, then NHL/NBA/NCAAM/Soccer/MLB)
ESPN_ENDPOINTS = {
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50",
    "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
    "BUNDESLIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",
    "UCL": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
    "LALIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    "SERIEA": "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
}

SUMMARY_SPORT_PATHS = {
    "CFB": "football/college-football",
    "NFL": "football/nfl",
    "NHL": "hockey/nhl",
    "NBA": "basketball/nba",
    "NCAAM": "basketball/mens-college-basketball",
    "EPL": "soccer/eng.1",
    "BUNDESLIGA": "soccer/ger.1",
    "UCL": "soccer/uefa.champions",
    "LALIGA": "soccer/esp.1",
    "SERIEA": "soccer/ita.1",
    "MLB": "baseball/mlb"
}

# Mapping to The Odds API v4 sport keys (ordered by priority so top 5 active leagues get queried)
ODDS_API_SPORT_MAP = {
    "CFB": "americanfootball_ncaaf",
    "NFL": "americanfootball_nfl",
    "NHL": "icehockey_nhl",
    "NBA": "basketball_nba",
    "NCAAM": "basketball_ncaab",
    "EPL": "soccer_epl",
    "BUNDESLIGA": "soccer_germany_bundesliga",
    "UCL": "soccer_uefa_champs_league",
    "MLB": "baseball_mlb"
}

# Multi-site RSS & News endpoints (CBS Sports, Yahoo Sports, ESPN News)
MULTI_SITE_NEWS_FEEDS = [
    ("CBS_CFB", "https://www.cbssports.com/rss/headlines/college-football/"),
    ("CBS_NFL", "https://www.cbssports.com/rss/headlines/nfl/"),
    ("CBS_NBA", "https://www.cbssports.com/rss/headlines/nba/"),
    ("CBS_NHL", "https://www.cbssports.com/rss/headlines/nhl/"),
    ("YAHOO_SPORTS", "https://sports.yahoo.com/rss/")
]

ESPN_NEWS_ENDPOINTS = {
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news?limit=15",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit=15",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=15",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news?limit=15",
    "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/news?limit=15"
}

# User Watchability Hierarchy (Soft Tiebreaker Only)
FAVORITE_TEAMS_KEYWORDS = [
    "florida gators", "florida", "gators",
    "georgia", "alabama", "lsu", "tennessee", "texas", "ole miss", "texas a&m", "auburn", "south carolina", "kentucky", "missouri", "vanderbilt", "arkansas", "mississippi state", "oklahoma",
    "philadelphia eagles", "eagles",
    "pittsburgh steelers", "steelers",
    "new jersey devils", "devils",
    "philadelphia 76ers", "76ers", "sixers",
    "philadelphia phillies", "phillies",
    "new york mets", "mets",
    "liverpool",
    "borussia dortmund", "dortmund"
]

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

def get_tier_parameters(bankroll):
    if bankroll < 50.0:
        return {
            "tier_name": "Survival Tier (<$50)",
            "base_unit": 2.00,
            "core_min": 3.00,
            "core_max": 6.00,
            "parlay_min": 1.50,
            "parlay_max": 3.00,
            "max_active_exposure": round(bankroll * 0.50, 2)
        }
    elif bankroll < 250.0:
        return {
            "tier_name": "Tier 1 ($50-$249.99)",
            "base_unit": 4.00,
            "core_min": 6.00,
            "core_max": 12.00,
            "parlay_min": 3.00,
            "parlay_max": 6.00,
            "max_active_exposure": round(bankroll * 0.50, 2)
        }
    elif bankroll < 500.0:
        return {
            "tier_name": "Tier 2 ($250-$499.99)",
            "base_unit": 10.00,
            "core_min": 15.00,
            "core_max": 30.00,
            "parlay_min": 7.50,
            "parlay_max": 15.00,
            "max_active_exposure": round(bankroll * 0.50, 2)
        }
    elif bankroll < 750.0:
        return {
            "tier_name": "Tier 3 ($500-$749.99)",
            "base_unit": 20.00,
            "core_min": 30.00,
            "core_max": 60.00,
            "parlay_min": 15.00,
            "parlay_max": 30.00,
            "max_active_exposure": round(bankroll * 0.50, 2)
        }
    else:
        return {
            "tier_name": "Tier 4 ($750-$1,000+)",
            "base_unit": 30.00,
            "core_min": 45.00,
            "core_max": 90.00,
            "parlay_min": 22.50,
            "parlay_max": 45.00,
            "max_active_exposure": round(bankroll * 0.50, 2)
        }

def load_post_settlement_context(workbook):
    tracker_ws = workbook.worksheet("Challenge Tracker ($100-$1k)")
    tracker_rows = tracker_ws.get_all_values()

    current_bankroll = 100.0
    next_bet_num = 1
    pending_count = 0
    settled_bets = 0
    tag_performance = {}

    for row in tracker_rows:
        if len(row) >= 14:
            bet_id = row[0].strip()
            has_date = bool(row[1].strip())
            result_status = row[10].strip() if len(row) > 10 else ""
            edge_tag = row[15].strip() if len(row) > 15 else ""

            if (bet_id == "START" or bet_id.startswith("BET-")) and has_date:
                if row[13].strip():
                    try:
                        current_bankroll = float(row[13].replace("$", "").replace(",", ""))
                    except ValueError:
                        pass
                if result_status.lower() == "pending":
                    pending_count += 1
                elif result_status.lower() in ("won", "lost", "push"):
                    settled_bets += 1
                    if edge_tag:
                        tag_performance.setdefault(edge_tag, {"won": 0, "lost": 0})
                        if result_status.lower() == "won":
                            tag_performance[edge_tag]["won"] += 1
                        elif result_status.lower() == "lost":
                            tag_performance[edge_tag]["lost"] += 1
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

    tier_params = get_tier_parameters(current_bankroll)
    learning_mode = "CALIBRATED_LEARNING (N >= 30)" if settled_bets >= 30 else f"OBJECTIVE_BASELINE_COLLECTION (N = {settled_bets}/30)"

    return {
        "current_bankroll": current_bankroll,
        "tier_parameters": tier_params,
        "settled_bets_count": settled_bets,
        "learning_mode": learning_mode,
        "historical_edge_tags": tag_performance if settled_bets >= 30 else "Disabled until N >= 30 settled bets",
        "pending_bets_remaining": pending_count,
        "next_bet_num": next_bet_num,
        "unit_tier_rules": rules_rows
    }

# --- 2. HARD ROCK BET & SHARP MARKET ODDS FETCHER (THE ODDS API v4) ---
def fetch_hardrock_and_sharp_odds(active_leagues):
    """
    Queries at most 5 active leagues that actually have games today.
    Uses bookmakers=hardrockbet,pinnacle,fanduel,draftkings (1 region equivalent = 3 credits/sport).
    """
    if not ODDS_API_KEY:
        print("ODDS_API_KEY not set; using ESPN + Google Search for Hard Rock Bet lines.", flush=True)
        return {}

    odds_lookup = {}
    queried_count = 0

    for league in active_leagues:
        sport_key = ODDS_API_SPORT_MAP.get(league)
        if not sport_key or queried_count >= 5:
            continue

        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "bookmakers": "hardrockbet,pinnacle,fanduel,draftkings",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american"
        }
        try:
            res = requests.get(url, params=params, timeout=8)
            remaining = res.headers.get("x-requests-remaining", "unknown")
            print(f"Odds API [{league}] status={res.status_code} | Credits remaining={remaining}", flush=True)
            if res.status_code != 200:
                continue

            queried_count += 1
            for game in res.json():
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                key = f"{away.lower()} @ {home.lower()}"

                books_summary = {}
                for bm in game.get("bookmakers", []):
                    bm_key = bm.get("key", "")
                    markets_dict = {}
                    for m in bm.get("markets", []):
                        m_key = m.get("key", "")
                        outcomes = []
                        for o in m.get("outcomes", []):
                            pt = f" ({o['point']:+g})" if "point" in o and m_key == "spreads" else (f" {o['point']}" if "point" in o else "")
                            outcomes.append(f"{o.get('name')}{pt}: {o.get('price')}")
                        markets_dict[m_key] = " | ".join(outcomes)
                    books_summary[bm_key] = markets_dict

                odds_lookup[key] = {
                    "hardrockbet": books_summary.get("hardrockbet", "Not posted yet on Hard Rock"),
                    "pinnacle_sharp": books_summary.get("pinnacle", "N/A"),
                    "fanduel": books_summary.get("fanduel", "N/A"),
                    "draftkings": books_summary.get("draftkings", "N/A")
                }

            try:
                if remaining != "unknown" and int(float(remaining)) < 30:
                    print("Odds API credits < 30; stopping further queries to preserve monthly cap.", flush=True)
                    break
            except ValueError:
                pass
        except Exception as e:
            print(f"Warning: Odds API fetch error for {league}: {e}", flush=True)

    return odds_lookup

# --- 3. PARALLEL ESPN DEEP ENRICHMENT (INJURIES, LINE MOVEMENT, ATS, BPI/FPI) ---
def fetch_single_event_summary(league, event_id):
    path = SUMMARY_SPORT_PATHS.get(league)
    if not path or not event_id:
        return event_id, {}

    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code != 200:
            return event_id, {}
        data = res.json()

        # 1. BPI / FPI Predictor
        predictor = data.get("predictor", {})
        home_proj = predictor.get("homeTeam", {}).get("gameProjection", "N/A")
        away_proj = predictor.get("awayTeam", {}).get("gameProjection", "N/A")
        win_prob = f"Home {home_proj}% / Away {away_proj}%" if home_proj != "N/A" else "N/A"

        # 2. Pickcenter (Opening vs Current Line Movement & ATS Records)
        pickcenter = data.get("pickcenter", [])
        line_movement = "N/A"
        ats_records = "N/A"
        if pickcenter:
            pc = pickcenter[0]
            open_spread = pc.get("pointSpread", {}).get("open", {}).get("line", "")
            curr_spread = pc.get("details", "")
            open_total = pc.get("total", {}).get("open", {}).get("line", "")
            curr_total = pc.get("overUnder", "")
            line_movement = f"Spread Open: {open_spread or 'N/A'} -> Curr: {curr_spread} | Total Open: {open_total or 'N/A'} -> Curr: {curr_total}"
            home_ats = pc.get("homeTeamOdds", {}).get("spreadRecord", {}).get("summary", "N/A")
            away_ats = pc.get("awayTeamOdds", {}).get("spreadRecord", {}).get("summary", "N/A")
            ats_records = f"Away ATS: {away_ats} | Home ATS: {home_ats}"

        # 3. Key Injuries (Out / Doubtful / Questionable)
        injuries_summary = []
        for team_inj in data.get("injuries", []):
            t_name = team_inj.get("team", {}).get("abbreviation", "")
            notable = []
            for item in team_inj.get("injuries", [])[:6]:
                athlete = item.get("athlete", {}).get("displayName", "")
                pos = item.get("athlete", {}).get("position", {}).get("abbreviation", "")
                status = item.get("status", "")
                if athlete and status in ("Out", "Doubtful", "Questionable", "Day-To-Day"):
                    notable.append(f"{athlete} ({pos}-{status})")
            if notable:
                injuries_summary.append(f"{t_name}: {', '.join(notable)}")

        return event_id, {
            "espn_bpi_fpi_prob": win_prob,
            "line_movement": line_movement,
            "ats_records": ats_records,
            "key_injuries": " | ".join(injuries_summary) if injuries_summary else "No major injuries listed"
        }
    except Exception:
        return event_id, {}

# --- 4. MULTI-SITE HEADLINES & BREAKING STORYLINES SCRAPER ---
def fetch_multisite_storylines(active_leagues):
    headlines = []

    # 1. ESPN League News JSON
    for league in active_leagues:
        url = ESPN_NEWS_ENDPOINTS.get(league)
        if not url:
            continue
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                for art in res.json().get("articles", [])[:6]:
                    hl = art.get("headline", "")
                    desc = art.get("description", "")
                    if hl:
                        headlines.append(f"[{league} - ESPN] {hl}: {desc[:140]}")
        except Exception:
            pass

    # 2. CBS Sports & Yahoo Sports RSS Feeds
    for source_name, rss_url in MULTI_SITE_NEWS_FEEDS:
        try:
            res = requests.get(rss_url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            if res.status_code == 200:
                root = ET.fromstring(res.content)
                for item in root.findall(".//item")[:6]:
                    title = item.findtext("title", "").strip()
                    if title:
                        headlines.append(f"[{source_name}] {title}")
        except Exception:
            pass

    return headlines[:35]

# --- 5. PRIMARY SLATE BUILDER ---
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

def compute_watchability_tag(league, home_team, away_team, home_rank, away_rank, tv_broadcast):
    combined = f"{away_team} {home_team}".lower()
    is_fav_team = any(kw in combined for kw in FAVORITE_TEAMS_KEYWORDS)
    is_ranked = (home_rank <= 25) or (away_rank <= 25)
    is_national_tv = any(net in tv_broadcast.upper() for net in ["ABC", "ESPN", "FOX", "CBS", "NBC", "TNT", "PRIME"])

    tags = []
    if league == "CFB":
        tags.append("PRIORITY #1 SPORT (CFB)")
    elif league == "NFL":
        tags.append("PRIORITY #2 SPORT (NFL)")
    if is_fav_team:
        tags.append("USER FAVORITE TEAM/CONF")
    if is_ranked:
        tags.append("TOP-25 MATCHUP")
    if is_national_tv:
        tags.append(f"NATIONAL TV ({tv_broadcast})")

    return " | ".join(tags) if tags else "Standard Slate"

def fetch_daily_slate():
    raw_slate = []
    active_leagues = []
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

        league_has_pre = False
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
            tv_str = ", ".join(broadcasts) if broadcasts else "Streaming"

            est_time_str, time_window = classify_est_window(event.get("date", ""))
            home_name = home.get("team", {}).get("displayName", "")
            away_name = away.get("team", {}).get("displayName", "")
            home_rank = home.get("curatedRank", {}).get("current", 99) or 99
            away_rank = away.get("curatedRank", {}).get("current", 99) or 99

            league_has_pre = True
            raw_slate.append({
                "event_id": event.get("id", ""),
                "league": league,
                "kickoff_utc": event.get("date", ""),
                "kickoff_est": est_time_str,
                "time_window": time_window,
                "tv_broadcast": tv_str,
                "watchability_tier": compute_watchability_tag(league, home_name, away_name, home_rank, away_rank, tv_str),
                "matchup": f"{away_name} @ {home_name}",
                "home_team": home_name,
                "home_rank": home_rank,
                "home_records": extract_records(home),
                "home_stats": extract_stats(home),
                "away_team": away_name,
                "away_rank": away_rank,
                "away_records": extract_records(away),
                "away_stats": extract_stats(away),
                "espn_spread": odds_info.get("details", "N/A"),
                "espn_total": odds_info.get("overUnder", "N/A"),
                "espn_home_ml": odds_info.get("homeTeamOdds", {}).get("moneyLine", "N/A"),
                "espn_away_ml": odds_info.get("awayTeamOdds", {}).get("moneyLine", "N/A"),
                "espn_draw_ml": odds_info.get("drawOdds", {}).get("moneyLine", "N/A")
            })

        if league_has_pre:
            active_leagues.append(league)

    # Filter to lined games or priority games for fast parallel deep enrichment (max 35 games)
    candidates = [
        g for g in raw_slate
        if g["espn_spread"] != "N/A" or g["espn_home_ml"] != "N/A" or "USER FAVORITE" in g["watchability_tier"] or g["league"] in ("CFB", "NFL")
    ][:35]

    print(f"Running parallel deep enrichment on {len(candidates)} lined/priority matchups across {active_leagues}...", flush=True)

    summary_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single_event_summary, g["league"], g["event_id"]) for g in candidates]
        for fut in as_completed(futures):
            eid, info = fut.result()
            if eid:
                summary_map[eid] = info

    # Fetch Hard Rock Bet & Sharp comparison lines for active leagues
    odds_api_map = fetch_hardrock_and_sharp_odds(active_leagues)

    for g in candidates:
        enrich = summary_map.get(g["event_id"], {})
        g["espn_bpi_fpi_prob"] = enrich.get("espn_bpi_fpi_prob", "N/A")
        g["line_movement"] = enrich.get("line_movement", "N/A")
        g["ats_records"] = enrich.get("ats_records", "N/A")
        g["key_injuries"] = enrich.get("key_injuries", "No major injuries listed")

        m_key = g["matchup"].lower()
        if m_key in odds_api_map:
            g["hardrock_and_sharp_books"] = odds_api_map[m_key]
        else:
            g["hardrock_and_sharp_books"] = "Use ESPN odds / Google Search Hard Rock Bet lines"

    storylines = fetch_multisite_storylines(active_leagues)
    return candidates, storylines

# --- 6. ROBUST JSON EXTRACTOR ---
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

# --- 7. MULTI-WINDOW AI CURATION ENGINE (FREE-TIER MODELS ONLY) ---
def run_ai_curation(slate, storylines, context):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    start_num = context["next_bet_num"]
    tp = context["tier_parameters"]

    prompt = f"""
You are the quantitative sports betting curator for a $100-to-$1,000 challenge exclusively on **Hard Rock Bet**.
Yesterday's bets have been settled by settler.py, and today's multi-source enriched slate (with Hard Rock Bet vs. Pinnacle/FanDuel/DraftKings lines, ESPN BPI/FPI win probabilities, opening-to-current line movement, ATS records, injury reports, and CBS/Yahoo/ESPN storylines) is loaded below.

OPERATIONAL MANDATES:
1. MULTI-WINDOW TIME-SLOT ACTION (NO 2-BET CAP & ZERO LOSS FREEZES):
   - Spread action across today's available broadcast time windows ('Window 1: Early / Noon', 'Window 2: Afternoon', 'Window 3: Primetime', 'Window 4: Late Night').
   - On full slates (like CFB Saturdays or NFL Sundays), recommend 3 to 5 plays covering every active broadcast window (Noon, 3:30/4:30 PM, 7:30 PM, 10:00 PM EST). On weekday slates, recommend 2 to 4 plays across active windows.
2. BANKROLL, TIER & 50% MAX ACTIVE EXPOSURE RULE:
   - Active Tier: {tp['tier_name']} | 3:12 AM Locked Bankroll: ${context['current_bankroll']:.2f} | 1U = ${tp['base_unit']:.2f}
   - Maximum Active Exposure (50% of Bankroll): ${tp['max_active_exposure']:.2f}
   - Total combined 'rec_stake' across all recommended plays today MUST NOT exceed ${tp['max_active_exposure']:.2f}.
   - Core Plays (Straight Spreads, Straight Totals, or 2-Leg Correlated SGPs): ${tp['core_min']:.2f} to ${tp['core_max']:.2f} (1.5U to 3.0U).
   - Parlays / Window Action Plays: ${tp['parlay_min']:.2f} to ${tp['parlay_max']:.2f} (0.75U to 1.5U).
3. HARD ROCK BET LINE ACCURACY & RULE 8 (MAX JUICE & LEG CAP):
   - Always prioritize 'hardrockbet' lines when present in 'hardrock_and_sharp_books' and compare them against 'pinnacle_sharp' / 'fanduel' / 'draftkings' to spot +EV discrepancies where Hard Rock Bet is hanging a softer number.
   - Straight bets MUST have odds of -140 or better (NEVER recommend a -160 or -250 straight ML).
   - Individual legs inside a Parlay or SGP MUST have odds of -450 or better (NEVER include -500 or -700 heavy favorites), and parlays are capped at a maximum of 3 legs.
4. DYNAMIC BET STRUCTURES (NEVER FORCE FIXED SLOTS):
   - Choose the highest-edge structure for each time window: Straight Spread, Straight Over/Under, 2-Leg Correlated SGP (Favorite ML/Spread + Alternate Over or Alternate Under with +6.0 to +7.5 pts cushion in CFB/NFL or +8.0 to +10.0 pts in NBA/NCAAM), or 2-to-3 Leg Parlay.
5. WATCHABILITY HIERARCHY & OVERS AS SOFT TIEBREAKER (#1 GOAL IS $1,000 BANKROLL):
   - Mathematical edge (Hard Rock vs. sharp line value, BPI/FPI edge, injury mismatches, line movement, rest) is ALWAYS #1.
   - Soft Tiebreaker: When two plays have similar +EV, prefer the user's watchability hierarchy:
     * #1 Sport: College Football (especially Florida Gators & SEC), followed by NFL (Eagles, Steelers), NHL (Devils), NBA (76ers), MLB (Phillies, Mets), and European Soccer (Liverpool, Dortmund), plus national TV games.
     * Prefer Overs (or Alternate Over cushions in SGPs) when the math supports points, but never hesitate to bet an Under if the defensive/weather/line-movement edge is stronger.
6. LEARNING CALIBRATION MODE ({context['learning_mode']}) & MARKET HYGIENE:
   - While N < 30 settled bets, remain in pure objective data-collection mode (zero arbitrary venue/trend bans). Once N >= 30, weight historical 'WIN:' and 'FAIL:' edge tags.
   - Rule 3 (No Team Stacking): Never include the same team across multiple tickets.
   - For Soccer, always specify 'Draw No Bet (DNB)', 'Double Chance', or 'Over/Under Goals'. For NHL, specify '2-Way ML (Inc. OT/SO)'.

Number the 'bet_id' field sequentially starting at BET-{start_num:03d} (e.g., BET-{start_num:03d}, BET-{start_num+1:03d}, BET-{start_num+2:03d}...).

POST-SETTLEMENT CONTEXT:
{json.dumps(context, indent=2)}

MULTI-SITE BREAKING HEADLINES & STORYLINES (CBS / YAHOO / ESPN):
{json.dumps(storylines, indent=2)}

TODAY'S ENRICHED SLATE (HARD ROCK LINES, INJURIES, LINE MOVEMENT, BPI/FPI, TIME WINDOWS):
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
      "favorite": "Exact Hard Rock Bet Selection & Line",
      "favorite_ml": "-110",
      "target_combined_odds": "-110",
      "rec_stake": 8,
      "rationale": "Cite Hard Rock line vs sharp books, line movement, injuries/storylines, BPI/FPI edge, and watchability/window fit."
    }}
  ]
}}
"""

    # Strictly Free-Tier eligible models in Google AI Studio
    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash"
    ]

    for attempt, model_name in enumerate(models_to_try, start=1):
        # Pass 1: Try with live Google Search grounding for weather & late scratches
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
            if "recommendations" in parsed and parsed["recommendations"]:
                return parsed["recommendations"]
        except Exception as e:
            print(f"Attempt {attempt} ({model_name} + Search) fallback to strict JSON: {e}", flush=True)

        # Pass 2: Guaranteed Strict JSON mode using scraped multi-source data
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
            if "recommendations" in parsed and parsed["recommendations"]:
                return parsed["recommendations"]
        except errors.ServerError as e:
            print(f"Attempt {attempt} ({model_name}) hit 503 overload: {e}. Retrying in 15s...", flush=True)
            time.sleep(15)
        except Exception as e:
            print(f"Attempt {attempt} ({model_name} strict JSON) failed: {e}. Retrying in 10s...", flush=True)
            time.sleep(10)

    raise RuntimeError("All Gemini model attempts failed.")

# --- 8. EXECUTION ENTRYPOINT ---
if __name__ == "__main__":
    print("Step 2: Starting Multi-Source Hard Rock Bet Curation Engine...", flush=True)
    workbook = get_workbook()
    context = load_post_settlement_context(workbook)
    tp = context["tier_parameters"]
    print(
        f"Tier: {tp['tier_name']} | Bankroll: ${context['current_bankroll']:.2f} | "
        f"50% Max Exposure Budget: ${tp['max_active_exposure']:.2f} | "
        f"Mode: {context['learning_mode']} | Next ID: BET-{context['next_bet_num']:03d}",
        flush=True
    )

    slate, storylines = fetch_daily_slate()
    print(f"Enriched {len(slate)} matchups and {len(storylines)} multi-site storylines for today (EST).", flush=True)

    if not slate:
        print("No pre-game matchups found on ESPN for today's date. Exiting.", flush=True)
        exit(0)

    recommendations = run_ai_curation(slate, storylines, context)
    print(f"Generated {len(recommendations)} multi-window play(s) for today.", flush=True)

    feed_ws = workbook.worksheet("Curator Feed")
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    # Remove any prior UNEXECUTED rows from today before writing fresh multi-window picks
    existing_rows = feed_ws.get_all_values()
    for idx in range(len(existing_rows), 1, -1):
        row = existing_rows[idx - 1]
        if len(row) >= 11 and row[0].strip() == today_str and row[10].strip() == "UNEXECUTED":
            feed_ws.delete_rows(idx)

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
