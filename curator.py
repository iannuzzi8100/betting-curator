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
CORE_STAKE = round(BASE_UNIT * 1.75, 2)       # $7.00 (1.75U)
SECONDARY_STAKE = round(BASE_UNIT * 0.75, 2)  # $3.00 (0.75U)

# ESPN Public Leagues
ESPN_LEAGUES = {
    "CFB": {"path": "football/college-football", "groups": "80"},
    "NFL": {"path": "football/nfl", "groups": None},
    "NBA": {"path": "basketball/nba", "groups": None},
    "NCAAM": {"path": "basketball/mens-college-basketball", "groups": "50"},
    "NHL": {"path": "hockey/nhl", "groups": None}
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
            "Kickoff (UTC)",
            "Matchup",
            "Play Type",
            "Consensus Total",
            "Target Under",
            "Favorite",
            "Favorite ML",
            "Target Combined Odds",
            "Rec Stake ($)",
            "Status"
        ])
    return sheet

# --- 2. ESPN PUBLIC SCRAPER (WITH DATES PARAMETER) ---
def fetch_espn_events_for_date(league_info, yyyymmdd):
    path = league_info["path"]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={yyyymmdd}"
    if league_info.get("groups"):
        url += f"&groups={league_info['groups']}"

    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return res.json().get("events", [])
    except Exception as e:
        print(f"Error fetching {path} for {yyyymmdd}: {e}")
    return []

def parse_game_odds(competition):
    odds_list = competition.get("odds", [])
    if not odds_list:
        return None

    odds_data = odds_list[0]
    total = odds_data.get("overUnder")
    details = odds_data.get("details", "")

    # Parse moneyline (handling int or string formats)
    def clean_ml(val):
        if val is None:
            return None
        try:
            return int(str(val).replace("+", ""))
        except ValueError:
            return None

    home_ml = clean_ml(odds_data.get("homeTeamOdds", {}).get("moneyLine"))
    away_ml = clean_ml(odds_data.get("awayTeamOdds", {}).get("moneyLine"))

    return {
        "total": float(total) if total is not None else None,
        "details": details,
        "home_ml": home_ml,
        "away_ml": away_ml
    }

# --- 3. CURATION LOGIC ---
def curate_card():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=36)

    today_str = now.strftime("%Y%m%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y%m%d")
    dates_to_check = [today_str, tomorrow_str]

    candidate_plays = []

    for league_name, league_info in ESPN_LEAGUES.items():
        all_events = []
        for d in dates_to_check:
            all_events.extend(fetch_espn_events_for_date(league_info, d))

        # Deduplicate events by event ID
        seen_ids = set()
        unique_events = []
        for e in all_events:
            if e.get("id") not in seen_ids:
                seen_ids.add(e.get("id"))
                unique_events.append(e)

        for event in unique_events:
            date_str = event.get("date")
            if not date_str:
                continue

            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # Only consider games that have not started and start within the next 36 hours
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
            home_abbr = home_comp.get("team", {}).get("abbreviation", "")
            away_abbr = away_comp.get("team", {}).get("abbreviation", "")

            matchup_str = f"{away_team} @ {home_team}".lower()
            if any(kw in matchup_str for kw in EXCLUDED_KEYWORDS):
                continue

            odds = parse_game_odds(comp)
            if not odds:
                continue

            total = odds["total"]
            home_ml = odds["home_ml"]
            away_ml = odds["away_ml"]
            details = odds["details"].strip() if odds["details"] else ""

            fav_team, fav_price = None, None

            # 1. Direct Moneyline check (-250 to -600)
            if home_ml and -600 <= home_ml <= -250:
                fav_team, fav_price = home_team, home_ml
            elif away_ml and -600 <= away_ml <= -250:
                fav_team, fav_price = away_team, away_ml

            # 2. Spread fallback if moneyline not explicitly populated
            if not fav_team and details and " -" in details:
                parts = details.split(" -")
                team_abbrev = parts[0].strip()
                try:
                    spread_val = float(parts[1])
                    # Spreads from 6.0 to 17.5 roughly correspond to -250 to -600 ML
                    if 6.0 <= spread_val <= 17.5:
                        if team_abbrev == home_abbr:
                            fav_team, fav_price = home_team, -300
                        elif team_abbrev == away_abbr:
                            fav_team, fav_price = away_team, -300
                except (ValueError, IndexError):
                    pass

            # Core Tier 1 (Football or Basketball with consensus total)
            if fav_team and total and league_name in ["CFB", "NFL", "NBA", "NCAAM"]:
                padding = 10.0 if "NBA" in league_name or "NCAAM" in league_name else 7.0
                candidate_plays.append({
                    "scraped_date": now.strftime("%Y-%m-%d"),
                    "kickoff": event_time.strftime("%Y-%m-%d %H:%M UTC"),
                    "matchup": f"{away_team} @ {home_team}",
                    "play_type": f"Core Tier 1 ({league_name} SGP)",
                    "consensus_total": total,
                    "target_under": f"Under {total + padding}",
                    "favorite": fav_team,
                    "fav_price": fav_price,
                    "target_odds": "-135 to -110",
                    "stake": CORE_STAKE,
                    "status": "UNEXECUTED"
                })

    return candidate_plays[:2]  # Limit to top 2 qualifying plays

# --- 4. EXECUTION ---
if __name__ == "__main__":
    sheet = get_curator_sheet()
    plays = curate_card()

    if not plays:
        print("No qualifying plays found across ESPN slates in the next 36 hours.")
    else:
        for p in plays:
            print(f"Qualifying Play: {p['matchup']} | {p['favorite']} ({p['fav_price']}) + {p['target_under']}")
            sheet.append_row([
                p["scraped_date"],
                p["kickoff"],
                p["matchup"],
                p["play_type"],
                p["consensus_total"],
                p["target_under"],
                p["favorite"],
                p["fav_price"],
                p["target_odds"],
                p["stake"],
                p["status"]
            ])
        print("Successfully updated Curator Feed tab.")
