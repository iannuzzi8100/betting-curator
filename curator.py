import os
import json
import requests
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# --- CONFIGURATION ---
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
FEED_TAB_NAME = "Curator Feed"

EXCLUDED_KEYWORDS = ["kentucky", "wildcats"]
BASE_UNIT = 4.00
CORE_STAKE = round(BASE_UNIT * 1.75, 2)     # $7.00
SECONDARY_STAKE = round(BASE_UNIT * 0.75, 2) # $3.00

# Full whitelist of your approved sports groups
APPROVED_SPORT_KEYS = [
    # Football
    "americanfootball_ncaaf",
    "americanfootball_nfl",
    # Basketball
    "basketball_nba",
    "basketball_ncaab",
    # Hockey & Baseball
    "icehockey_nhl",
    "baseball_mlb",
    "baseball_ncaa",
    # Soccer
    "soccer_usa_mls",
    "soccer_epl",
    "soccer_uefa_champs_league",
    # Tennis
    "tennis_atp",
    "tennis_wta",
    # Golf
    "golf_pga"
]

# --- 1. SPREADSHEET CONNECTION ---
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

# --- 2. QUOTA-CONSCIOUS DISCOVERY ---
def get_active_in_season_sports():
    """Fetches currently active sports (0 quota cost)."""
    url = "https://api.the-odds-api.com/v4/sports/"
    params = {"apiKey": ODDS_API_KEY}
    response = requests.get(url, params=params)
    if response.status_code != 200:
        return ["americanfootball_ncaaf", "basketball_nba", "icehockey_nhl"]
    
    active_sports = [s["key"] for s in response.json() if s.get("active", False)]
    # Filter only against your approved inventory
    return [key for key in active_sports if any(app in key for app in APPROVED_SPORT_KEYS)]

def fetch_odds_for_sport(sport_key, markets="h2h,totals"):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american"
    }
    response = requests.get(url, params=params)
    if response.status_code != 200:
        return []
    return response.json()

# --- 3. CURATION ENGINE ---
def is_valid_matchup(game, now, cutoff):
    commence_time = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
    if not (now <= commence_time <= cutoff):
        return False
    matchup_str = f"{game.get('away_team', '')} {game.get('home_team', '')}".lower()
    if any(kw in matchup_str for kw in EXCLUDED_KEYWORDS):
        return False
    return True

def curate_multi_sport_card():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    active_sports = get_active_in_season_sports()
    
    curated_plays = []
    used_teams = set()

    # Prioritize high-scoring sports for Core Tier 1 SGP (Football / Basketball)
    high_scoring = [s for s in active_sports if "football" in s or "basketball" in s]
    other_sports = [s for s in active_sports if s not in high_scoring]

    # --- A. BUILD CORE TIER 1 SGP ---
    core_candidates = []
    for sport in high_scoring[:3]:  # Check top active leagues
        games = fetch_odds_for_sport(sport, markets="h2h,totals")
        for game in games:
            if not is_valid_matchup(game, now, cutoff):
                continue

            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                continue

            markets = bookmakers[0].get("markets", [])
            h2h = next((m for m in markets if m["key"] == "h2h"), None)
            totals = next((m for m in markets if m["key"] == "totals"), None)

            if not h2h or not totals:
                continue

            favorite, fav_price = None, None
            for outcome in h2h["outcomes"]:
                if -600 <= outcome["price"] <= -250:
                    favorite = outcome["name"]
                    fav_price = outcome["price"]
                    break

            if favorite:
                c_total = totals["outcomes"][0]["point"]
                # SGP buffer: +7.0 for football, +10.0 for basketball
                cushion = 10.0 if "basketball" in sport else 7.0
                padded_under = c_total + cushion

                core_candidates.append({
                    "sport": game.get("sport_title", sport),
                    "matchup": f"{game['away_team']} @ {game['home_team']}",
                    "favorite": favorite,
                    "fav_price": fav_price,
                    "padded_under": padded_under,
                    "home": game["home_team"],
                    "away": game["away_team"]
                })

    if core_candidates:
        best_core = sorted(core_candidates, key=lambda x: x["fav_price"])[0]
        curated_plays.append({
            "sport": best_core["sport"],
            "tier": "Core Tier 1 (SGP)",
            "selection": f"{best_core['favorite']} ML ({best_core['fav_price']}) + Under {best_core['padded_under']} Total Pts ({best_core['matchup']})",
            "odds": "-130 to -115",
            "stake": CORE_STAKE,
            "allocation": "1.75U",
            "rationale": "High-floor favorite with alternate under padding cushion."
        })
        used_teams.update([best_core["home"], best_core["away"]])

    # --- B. BUILD TIER 2 MULTI-SPORT PARLAY ---
    # Draw from all available active sports (NHL, Tennis, NBA, Soccer)
    candidate_favorites = []
    for sport in (active_sports[:5]):
        games = fetch_odds_for_sport(sport, markets="h2h")
        for game in games:
            if not is_valid_matchup(game, now, cutoff):
                continue
            if game.get("home_team") in used_teams or game.get("away_team") in used_teams:
                continue

            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                continue

            h2h = next((m for m in bookmakers[0].get("markets", []) if m["key"] == "h2h"), None)
            if not h2h:
                continue

            for outcome in h2h["outcomes"]:
                if -220 <= outcome["price"] <= -135:
                    candidate_favorites.append({
                        "sport": game.get("sport_title", sport),
                        "team": outcome["name"],
                        "price": outcome["price"],
                        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}"
                    })

    if len(candidate_favorites) >= 2:
        leg1 = candidate_favorites[0]
        leg2 = candidate_favorites[1]
        curated_plays.append({
            "sport": f"{leg1['sport']} / {leg2['sport']}",
            "tier": "Multi-Window Tier 2",
            "selection": f"{leg1['team']} ML ({leg1['price']}) + {leg2['team']} ML ({leg2['price']})",
            "odds": "+160 to +220",
            "stake": SECONDARY_STAKE,
            "allocation": "0.75U",
            "rationale": f"Cross-sport ML floor parlay ({leg1['matchup']} & {leg2['matchup']})."
        })

    return curated_plays

# --- 4. EXECUTION ---
if __name__ == "__main__":
    sheet = get_curator_sheet()
    plays = curate_multi_sport_card()

    if not plays:
        print("No qualifying plays across active sports today.")
    else:
        for p in plays:
            print(f"[{p['tier']}] {p['selection']} | Rec Stake: ${p['stake']}")
            sheet.append_row([
                datetime.now().strftime("%Y-%m-%d"),
                p["sport"],
                p["tier"],
                p["selection"],
                p["odds"],
                p["stake"],
                p["allocation"],
                "UNEXECUTED",
                p["rationale"]
            ])
        print("Updated multi-sport card pushed to Curator Feed.")
