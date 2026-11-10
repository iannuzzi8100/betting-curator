import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# --- CONFIGURATION (TIER 1 PROFILE) ---
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
FEED_TAB_NAME = "Curator Feed"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00
CORE_STAKE = round(BASE_UNIT * 1.75, 2)     # $7.00 (1.75U)
SECONDARY_STAKE = round(BASE_UNIT * 0.75, 2) # $3.00 (0.75U)

# ESPN Public Scoreboard Endpoints (No API key required)
ESPN_LEAGUES = {
    "CFB": "football/college-football",
    "NFL": "football/nfl",
    "NBA": "basketball/nba",
    "NCAAM": "basketball/mens-college-basketball",
    "NHL": "hockey/nhl",
    "MLB": "baseball/mlb"
}

# --- 1. GOOGLE SHEETS CONNECTION ---
def get_curator_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    if GCP_SA_KEY:
        service_account_info = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    
    client = gspread.authorize(creds)
    workbook = client.open(SHEET_NAME)

    try:
        sheet = workbook.worksheet(FEED_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = workbook.add_worksheet(title=FEED_TAB_NAME, rows=500, cols=12)
        sheet.append_row([
            "Scraped Date",
            "Sport / League",
            "Bet Type & Tier",
            "Curated Selections / Legs",
            "Target Odds",
            "Rec Stake ($)",
            "Allocation",
            "Status",
            "Strategy Rationale"
        ])
    return sheet

# --- 2. ESPN PUBLIC SCRAPER ---
def fetch_espn_events(league_path):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league_path}/scoreboard"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("events", [])
    except Exception as e:
        print(f"Error fetching {league_path}: {e}")
    return []

def parse_game_odds(competition):
    """Extracts spread, over/under, and moneylines from ESPN odds block."""
    odds_list = competition.get("odds", [])
    if not odds_list:
        return None

    odds_data = odds_list[0]
    total = odds_data.get("overUnder")
    details = odds_data.get("details", "")

    home_ml = odds_data.get("homeTeamOdds", {}).get("moneyLine")
    away_ml = odds_data.get("awayTeamOdds", {}).get("moneyLine")

    return {
        "total": float(total) if total is not None else None,
        "details": details,
        "home_ml": home_ml,
        "away_ml": away_ml
    }

# --- 3. CURATION LOGIC ---
def curate_card():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    used_teams = set()
    core_candidates = []
    parlay_candidates = []

    for league_name, league_path in ESPN_LEAGUES.items():
        events = fetch_espn_events(league_path)

        for event in events:
            date_str = event.get("date")
            if not date_str:
                continue

            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if not (now <= event_time <= cutoff):
                continue

            comps = event.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home_comp = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_comp = next((c for c in competitors if c.get("homeAway") == "away"), None)

            if not home_comp or not away_comp:
                continue

            home_team = home_comp.get("team", {}).get("displayName", "")
            away_team = away_comp.get("team", {}).get("displayName", "")
            matchup_str = f"{away_team} @ {home_team}".lower()

            # Kentucky / Wildcats exclusion
            if any(kw in matchup_str for kw in EXCLUDED_KEYWORDS):
                continue

            odds = parse_game_odds(comp)
            if not odds:
                continue

            total = odds["total"]
            home_ml = odds["home_ml"]
            away_ml = odds["away_ml"]

            # Evaluate favorites
            fav_team, fav_price = None, None
            if home_ml and -600 <= home_ml <= -250:
                fav_team, fav_price = home_team, home_ml
            elif away_ml and -600 <= away_ml <= -250:
                fav_team, fav_price = away_team, away_ml

            # Fallback for spreads if ESPN doesn't list raw moneyline
            if not fav_team and odds["details"]:
                spread_text = odds["details"].strip()
                if " -" in spread_text:
                    parts = spread_text.split(" -")
                    team_abbrev = parts[0]
                    try:
                        spread_val = float(parts[1])
                        if 7.5 <= spread_val <= 17.5:
                            if team_abbrev in home_comp.get("team", {}).get("abbreviation", ""):
                                fav_team, fav_price = home_team, -350
                            elif team_abbrev in away_comp.get("team", {}).get("abbreviation", ""):
                                fav_team, fav_price = away_team, -350
                    except ValueError:
                        pass

            # A. Check for Core Tier 1 SGP (Football or Basketball with total)
            if fav_team and total and league_name in ["CFB", "NFL", "NBA", "NCAAM"]:
                padding = 10.0 if "NBA" in league_name or "NCAAM" in league_name else 7.0
                core_candidates.append({
                    "league": league_name,
                    "matchup": f"{away_team} @ {home_team}",
                    "fav_team": fav_team,
                    "fav_price": fav_price,
                    "padded_under": total + padding,
                    "home": home_team,
                    "away": away_team
                })

            # B. Check for Moderate Favorites for Tier 2 Cross-Slate Parlay (-130 to -220)
            for team, ml in [(home_team, home_ml), (away_team, away_ml)]:
                if ml and -220 <= ml <= -130:
                    parlay_candidates.append({
                        "league": league_name,
                        "team": team,
                        "price": ml,
                        "matchup": f"{away_team} @ {home_team}"
                    })

    curated_card = []

    # 1. Select Primary Core Tier 1 SGP Play
    if core_candidates:
        best_core = sorted(core_candidates, key=lambda x: x["fav_price"] if x["fav_price"] else -300)[0]
        curated_card.append({
            "league": best_core["league"],
            "tier": "Core Tier 1 (SGP)",
            "selection": f"{best_core['fav_team']} ML ({best_core['fav_price']}) + Under {best_core['padded_under']} Total Pts ({best_core['matchup']})",
            "odds": "-135 to -110",
            "stake": CORE_STAKE,
            "allocation": "1.75U",
            "rationale": f"Correlated SGP: Heavy ML favorite paired with padded alternate under cushion."
        })
        used_teams.update([best_core["home"], best_core["away"]])

    # 2. Select Secondary Multi-Window Tier 2 Parlay
    available_parlay = [c for c in parlay_candidates if c["team"] not in used_teams]
    if len(available_parlay) >= 2:
        leg1 = available_parlay[0]
        leg2 = available_parlay[1]
        curated_card.append({
            "league": f"{leg1['league']} / {leg2['league']}",
            "tier": "Multi-Window Tier 2",
            "selection": f"{leg1['team']} ML ({leg1['price']}) + {leg2['team']} ML ({leg2['price']})",
            "odds": "+165 to +215",
            "stake": SECONDARY_STAKE,
            "allocation": "0.75U",
            "rationale": f"Cross-slate ML floor parlay ({leg1['matchup']} & {leg2['matchup']})."
        })

    return curated_card

# --- 4. EXECUTION ---
if __name__ == "__main__":
    sheet = get_curator_sheet()
    plays = curate_card()

    if not plays:
        print("No qualifying plays found across ESPN slates in the next 24 hours.")
    else:
        for p in plays:
            print(f"[{p['tier']}]
