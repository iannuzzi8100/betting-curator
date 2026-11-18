import os
import re
import json
import datetime
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

load_dotenv()

# Configuration from Environment / GitHub Secrets
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID") or "1vTMvUyr_DABrrzVBU7ah1A8RYn9-sfO1Uu-0Syk2jnU"
GCP_SA_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or os.environ.get("GCP_SA_KEY") or ""

# Expanded to include high-edge niche leagues (EPL, UCL, ATP/WTA) alongside core US sports
SPORTS_ODDS_KEYS = [
    "basketball_nba",
    "icehockey_nhl",
    "basketball_ncaab",
    "americanfootball_ncaaf",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "tennis_atp_finals",
    "tennis_wta_finals",
]

# Bulk endpoint (/v4/sports/{sport}/odds) supports core full-game markets (h2h, spreads, totals)
MARKETS_BY_SPORT = {
    "basketball_nba": "h2h,spreads,totals",
    "basketball_ncaab": "h2h,spreads,totals",
    "icehockey_nhl": "h2h,spreads,totals",
    "americanfootball_ncaaf": "h2h,spreads,totals",
    "americanfootball_nfl": "h2h,spreads,totals",
    "soccer_epl": "h2h,totals",
    "soccer_uefa_champs_league": "h2h,totals",
    "tennis_atp_finals": "h2h",
    "tennis_wta_finals": "h2h",
}

# Per-event endpoint (/v4/sports/{sport}/events/{eventId}/odds) supports 1st-Half lines & Player Props
PROP_MARKETS_BY_SPORT = {
    "basketball_nba": "spreads_h1,totals_h1,player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists",
    "basketball_ncaab": "spreads_h1,totals_h1",
    "americanfootball_ncaaf": "spreads_h1,totals_h1",
    "americanfootball_nfl": "spreads_h1,totals_h1",
    "icehockey_nhl": "player_points,player_assists,player_shots_on_goal",
}

SHARP_BOOK_KEYS = ["pinnacle", "lowvig", "bookmaker", "fanduel", "draftkings", "betmgm", "caesars"]

ESPN_ENDPOINTS = {
    "NBA": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news",
        "injuries": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    },
    "NHL": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news",
        "injuries": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries",
    },
    "CBB": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/news",
    },
    "CFB": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news",
    },
    "NFL": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
        "injuries": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
    },
}

YOUTUBE_CHANNELS = [
    {"name": "SportsLine", "url": "https://www.youtube.com/@SportsLine/videos"},
    {"name": "BettingPros", "url": "https://www.youtube.com/@BettingPros/videos"},
    {"name": "The Action Network", "url": "https://www.youtube.com/@ActionNetworkHQ/videos"},
    {"name": "WagerTalk TV", "url": "https://www.youtube.com/@WagerTalk/videos"},
    {"name": "OddsShopper Sports Betting", "url": "https://www.youtube.com/@OddsShopper/videos"},
    {"name": "VSiN", "url": "https://www.youtube.com/@VSiNLive/videos"},
    {"name": "CashKeg", "url": "https://www.youtube.com/@CashKeg/videos"},
    {"name": "Circles Off", "url": "https://www.youtube.com/@CirclesOffHQ/videos"},
    {"name": "Bet the Process", "url": "https://www.youtube.com/@BetTheProcess/videos"},
    {"name": "Deep Dive Podcast", "url": "https://www.youtube.com/@DeepDivePodcast/videos"},
    {"name": "Shane Huang", "url": "https://www.youtube.com/@ShaneHuang88/videos"},
    {"name": "OddsJam", "url": "https://www.youtube.com/@OddsJam/videos"},
    {"name": "Josh Pate CFB", "url": "https://www.youtube.com/@JoshPateCFB/videos"},
    {"name": "Land Your Bets", "url": "https://www.youtube.com/@LandYourBets/videos"},
]


def american_to_implied(odds):
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def american_to_decimal(odds):
    if odds < 0:
        return 1.0 + (100.0 / abs(odds))
    return 1.0 + (odds / 100.0)


def compute_novig_ev(hr_price, sharp_price_target, sharp_price_opp):
    """Strips vig from sharp 2-way line and calculates exact EV% and Quarter-Kelly% for Hard Rock price."""
    imp_target = american_to_implied(sharp_price_target)
    imp_opp = american_to_implied(sharp_price_opp)
    total_imp = imp_target + imp_opp
    if total_imp <= 0:
        return 0.0, 0.0, 0.0
    fair_prob = imp_target / total_imp
    hr_dec = american_to_decimal(hr_price)
    ev_pct = ((fair_prob * hr_dec) - 1.0) * 100.0
    # Quarter Kelly fraction: 0.25 * ((b * p - q) / b)
    b = hr_dec - 1.0
    q = 1.0 - fair_prob
    full_kelly = ((b * fair_prob) - q) / b if b > 0 else 0.0
    q_kelly_pct = max(0.0, full_kelly * 0.25 * 100.0)
    return round(fair_prob * 100.0, 1), round(ev_pct, 2), round(q_kelly_pct, 1)


def get_docs_service():
    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]
    if GCP_SA_JSON:
        creds_dict = json.loads(GCP_SA_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    return build("docs", "v1", credentials=creds)


def fetch_hardrock_odds_and_props():
    game_lines = []
    prop_lines = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc + datetime.timedelta(hours=36)

    if ODDS_API_KEY:
        for sport_key in SPORTS_ODDS_KEYS:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "us,eu",
                "markets": MARKETS_BY_SPORT.get(sport_key, "h2h,spreads,totals"),
                "oddsFormat": "american",
                "bookmakers": "hardrockbet," + ",".join(SHARP_BOOK_KEYS),
            }
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code != 200:
                    continue
                rem_quota = int(resp.headers.get("x-requests-remaining", "0") or 0)
                games = resp.json()
                todays_event_ids = []

                for g in games:
                    home = g.get("home_team")
                    away = g.get("away_team")
                    commence = g.get("commence_time", "")
                    event_id = g.get("id")
                    books = g.get("bookmakers", [])

                    hr_book = next((b for b in books if b["key"] == "hardrockbet"), None)
                    sharp_books = [b for b in books if b["key"] in SHARP_BOOK_KEYS]
                    sharp_book = next((b for b in books if b["key"] == "pinnacle"), None) or (
                        sharp_books[0] if sharp_books else None
                    )
                    if not hr_book:
                        continue

                    is_today_slate = False
                    if commence and event_id:
                        try:
                            c_dt = datetime.datetime.fromisoformat(commence.replace("Z", "+00:00"))
                            if now_utc <= c_dt <= cutoff_utc:
                                is_today_slate = True
                                todays_event_ids.append((event_id, f"{away} @ {home}"))
                        except Exception:
                            pass

                    # Multi-book sharp lookup for consensus No-Vig devigging & stale line outlier detection
                    hr_summary = []
                    ev_tags = []
                    for m in hr_book.get("markets", []):
                        m_key = m["key"]
                        outcomes_str = []

                        for o in m.get("outcomes", []):
                            o_name = o["name"]
                            o_pt = o.get("point")
                            o_price = o["price"]
                            outcomes_str.append(f"{o_name} {o_pt if o_pt is not None else ''} ({o_price:+d})".strip())

                            if o_price < -160:
                                continue

                            # Collect matching 2-way fair probabilities across all available sharp books
                            fair_probs = []
                            stale_diffs = []
                            for sb in sharp_books:
                                sb_m = next((sm for sm in sb.get("markets", []) if sm["key"] == m_key), None)
                                if not sb_m or len(sb_m.get("outcomes", [])) != 2:
                                    continue
                                s_outs = sb_m["outcomes"]
                                s_same_side = next((x for x in s_outs if x["name"] == o_name), None)
                                s_other = next((x for x in s_outs if x["name"] != o_name), None)
                                if not s_same_side or not s_other:
                                    continue

                                s_pt = s_same_side.get("point")
                                if s_pt == o_pt:
                                    fp, _, _ = compute_novig_ev(o_price, s_same_side["price"], s_other["price"])
                                    if fp > 0:
                                        fair_probs.append(fp / 100.0)
                                elif o_pt is not None and s_pt is not None and o_price >= -125:
                                    # Detect Stale Hard Rock Line Outlier vs Sharp Consensus
                                    if m_key == "spreads" and o_pt > s_pt:
                                        stale_diffs.append(f"{sb['key']}:{s_pt}")
                                    elif m_key == "totals" and (
                                        (o_name == "Over" and o_pt < s_pt) or (o_name == "Under" and o_pt > s_pt)
                                    ):
                                        stale_diffs.append(f"{sb['key']}:{s_pt}")

                            if fair_probs:
                                avg_fp = sum(fair_probs) / len(fair_probs)
                                hr_dec = american_to_decimal(o_price)
                                ev_pct = round(((avg_fp * hr_dec) - 1.0) * 100.0, 2)
                                b_dec = hr_dec - 1.0
                                full_k = ((b_dec * avg_fp) - (1.0 - avg_fp)) / b_dec if b_dec > 0 else 0.0
                                q_kelly = round(max(0.0, full_k * 0.25 * 100.0), 1)
                                if ev_pct >= 1.5:
                                    ev_tags.append(
                                        f"[+EV ({len(fair_probs)}B Consensus): {m_key.upper()} {o_name} {o_pt if o_pt is not None else ''} @ {o_price:+d} | "
                                        f"FairWin: {round(avg_fp*100,1)}% | EV: +{ev_pct}% | 1/4-Kelly: {q_kelly}%]"
                                    )
                            if len(stale_diffs) >= 2:
                                ev_tags.append(
                                    f"[🔥 STALE LINE: {m_key.upper()} {o_name} {o_pt} ({o_price:+d}) on HRB vs Sharp ({', '.join(stale_diffs[:3])})]"
                                )

                        hr_summary.append(f"{m_key.upper()}: [{', '.join(outcomes_str)}]")

                    if not is_today_slate and not ev_tags:
                        continue

                    ev_banner = (" -> " + " ".join(ev_tags)) if ev_tags else ""
                    sharp_label = sharp_book["key"] if sharp_book else "none"
                    game_lines.append(
                        f"- [{sport_key}] {away} @ {home} ({commence}) [SharpRef: {sharp_label}] -> {' ; '.join(hr_summary)}{ev_banner}"
                    )

                # Fetch Player Props & 1H lines for up to 8 today-slate games (if quota permits)
                if sport_key in PROP_MARKETS_BY_SPORT and todays_event_ids and rem_quota > 15:
                    for ev_id, matchup in todays_event_ids[:8]:
                        ev_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{ev_id}/odds"
                        ev_params = {
                            "apiKey": ODDS_API_KEY,
                            "regions": "us,eu",
                            "markets": PROP_MARKETS_BY_SPORT[sport_key],
                            "oddsFormat": "american",
                            "bookmakers": "hardrockbet,pinnacle,fanduel,draftkings",
                        }
                        try:
                            p_resp = requests.get(ev_url, params=ev_params, timeout=15)
                            if p_resp.status_code != 200:
                                continue
                            p_data = p_resp.json()
                            hr_p = next((b for b in p_data.get("bookmakers", []) if b["key"] == "hardrockbet"), None)
                            ref_p = (
                                next((b for b in p_data.get("bookmakers", []) if b["key"] == "pinnacle"), None)
                                or next((b for b in p_data.get("bookmakers", []) if b["key"] == "fanduel"), None)
                                or next((b for b in p_data.get("bookmakers", []) if b["key"] == "draftkings"), None)
                            )
                            if not hr_p or not ref_p:
                                continue

                            ref_lookup = {}
                            for m in ref_p.get("markets", []):
                                for o in m.get("outcomes", []):
                                    key = (m["key"], o.get("description", ""), o.get("name", ""))
                                    ref_lookup[key] = (o.get("point"), o.get("price"))

                            for m in hr_p.get("markets", []):
                                m_key = m["key"]
                                if m_key in ("spreads_h1", "totals_h1"):
                                    s_outs = [
                                        o for rm in ref_p.get("markets", []) if rm["key"] == m_key for o in rm.get("outcomes", [])
                                    ]
                                    for o in m.get("outcomes", []):
                                        o_name = o.get("name", "")
                                        o_pt = o.get("point")
                                        o_price = o.get("price")
                                        if len(s_outs) == 2 and o_pt is not None and o_price is not None and o_price >= -155:
                                            s_match = next(
                                                (x for x in s_outs if x.get("name") == o_name and x.get("point") == o_pt),
                                                None,
                                            )
                                            s_other = next((x for x in s_outs if x.get("name") != o_name), None)
                                            if s_match and s_other:
                                                fair_p, ev_pct, q_kelly = compute_novig_ev(
                                                    o_price, s_match["price"], s_other["price"]
                                                )
                                                if ev_pct >= 1.5:
                                                    game_lines.append(
                                                        f"- [{sport_key} 1H] {matchup} -> {m_key.upper()} {o_name} {o_pt} ({o_price:+d}) "
                                                        f"vs {ref_p['key']} ({s_match['price']:+d}) | FairWin: {fair_p}% | EV: +{ev_pct}% | 1/4-Kelly: {q_kelly}%"
                                                    )
                                    continue

                                for o in m.get("outcomes", []):
                                    player = o.get("description", "")
                                    side = o.get("name", "")
                                    opp_side = "Under" if side == "Over" else "Over"
                                    hr_pt = o.get("point")
                                    hr_price = o.get("price")
                                    ref_val = ref_lookup.get((m_key, player, side))
                                    ref_opp = ref_lookup.get((m_key, player, opp_side))
                                    if not ref_val or hr_pt is None or hr_price is None or hr_price < -165:
                                        continue
                                    ref_pt, ref_price = ref_val

                                    # Market Width (Hold) Filter: reject wide/illiquid sharp markets (> 36 cents width)
                                    if ref_opp and ref_opp[1] is not None:
                                        imp_sum = american_to_implied(ref_price) + american_to_implied(ref_opp[1])
                                        if imp_sum > 1.095:
                                            continue

                                    has_line_edge = (
                                        ((side == "Over" and hr_pt < ref_pt) or (side == "Under" and hr_pt > ref_pt))
                                        and hr_price >= -155
                                        and hr_price >= (ref_price - 25)
                                    )
                                    ev_str = ""
                                    if hr_pt == ref_pt and ref_opp and ref_opp[0] == hr_pt:
                                        fair_p, ev_pct, q_kelly = compute_novig_ev(hr_price, ref_price, ref_opp[1])
                                        if ev_pct >= 2.0:
                                            ev_str = f" | FairWin: {fair_p}% | EV: +{ev_pct}% | 1/4-Kelly: {q_kelly}%"

                                    if has_line_edge or ev_str:
                                        prop_lines.append(
                                            f"- [{sport_key} PROP] {player} ({matchup}) {m_key} {side} {hr_pt} ({hr_price:+d}) "
                                            f"vs {ref_p['key']} {ref_pt} ({ref_price:+d}){ev_str}"
                                        )
                        except Exception:
                            continue
            except Exception as exc:
                game_lines.append(f"- [{sport_key}] Odds fetch error: {exc}")

    # Always augment with ESPN Quantitative Matchup, BPI/FPI, Steam Move, B2B Rest & Prop Steam Engine (100% Free & Unlimited)
    espn_quant_games, espn_quant_props = fetch_espn_quant_matchups_and_props()
    game_lines.extend(espn_quant_games)
    prop_lines.extend(espn_quant_props)

    return (
        game_lines or ["- No active lines found for today's slate."],
        prop_lines or ["- No positive-EV player prop discrepancies detected on today's slate."],
    )


def _safe_int_odds(val):
    if val is None:
        return None
    s = str(val).strip().upper()
    if s == "EVEN":
        return 100
    try:
        return int(float(s.replace("+", "")))
    except Exception:
        return None


def _safe_float_line(val):
    if val is None:
        return None
    s = re.sub(r"^[ouOU]", "", str(val).strip())
    try:
        return float(s)
    except Exception:
        return None


def fetch_espn_quant_matchups_and_props():
    """
    100% Free Unlimited Quantitative Engine via ESPN API:
    - Live Spreads, Moneylines, Totals + Opening-to-Current Steam Moves
    - ESPN BPI / FPI Pre-Game Win Probability vs. No-Vig Market Implied Win% (+EV% & 1/4-Kelly)
    - Automated Back-to-Back (0-Days Rest) & Road Fatigue Detection
    - NHL Confirmed/Expected Starting Goalies + Season SV% & GAA
    - ESPN Core API Player Prop Steam Move Detection (Open vs. Current Prop Movement)
    """
    quant_games = []
    quant_props = []
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    now_est = datetime.datetime.now(est_tz)
    today_ymd = now_est.strftime("%Y%m%d")
    yesterday_ymd = (now_est - datetime.timedelta(days=1)).strftime("%Y%m%d")

    league_configs = [
        ("NBA", "basketball", "nba", None),
        ("NHL", "hockey", "nhl", None),
        ("CBB", "basketball", "mens-college-basketball", "50"),
        ("CFB", "football", "college-football", "80"),
        ("NFL", "football", "nfl", None),
    ]

    athlete_cache = {}

    for label, sport, league, groups in league_configs:
        base_sb = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        # 1. Identify teams that played yesterday (Back-to-Back 0-days rest detector for NBA/NHL/CBB)
        b2b_teams = set()
        if label in ("NBA", "NHL", "CBB"):
            try:
                y_params = {"dates": yesterday_ymd}
                if groups:
                    y_params["groups"] = groups
                y_evts = requests.get(base_sb, params=y_params, timeout=10).json().get("events", [])
                for ye in y_evts:
                    for comp in ye.get("competitions", []):
                        for c in comp.get("competitors", []):
                            t_name = c.get("team", {}).get("displayName", "")
                            if t_name:
                                b2b_teams.add(t_name)
            except Exception:
                pass

        # 2. Fetch today's scoreboard
        try:
            t_params = {"dates": today_ymd}
            if groups:
                t_params["groups"] = groups
            events = requests.get(base_sb, params=t_params, timeout=12).json().get("events", [])
        except Exception:
            continue

        for ev in events[:15]:
            ev_id = ev.get("id", "")
            status_state = ev.get("status", {}).get("type", {}).get("state", "")
            if status_state == "post":
                continue

            comps = ev.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            h_name = home.get("team", {}).get("displayName", "")
            a_name = away.get("team", {}).get("displayName", "")
            h_abbr = home.get("team", {}).get("abbreviation", h_name)
            a_abbr = away.get("team", {}).get("abbreviation", a_name)
            matchup = f"{a_name} @ {h_name}"

            # Extract Overall / Home / Road records
            h_rec = next((r.get("summary", "") for r in home.get("records", []) if r.get("type") == "total"), "")
            a_rec = next((r.get("summary", "") for r in away.get("records", []) if r.get("type") == "total"), "")
            rec_str = f"({a_abbr} {a_rec} @ {h_abbr} {h_rec})" if (h_rec or a_rec) else ""

            tags = []
            # Back-to-Back Fatigue Flags
            if a_name in b2b_teams and h_name not in b2b_teams:
                tags.append(f"[⚠️ FATIGUE EDGE: {a_abbr} on Road Back-to-Back (0d rest) vs Rested {h_abbr}]")
            elif h_name in b2b_teams and a_name not in b2b_teams:
                tags.append(f"[⚠️ FATIGUE EDGE: {h_abbr} on Back-to-Back (0d rest) vs Rested {a_abbr}]")

            # Extract Odds & Opening-to-Current Steam Movement from Scoreboard
            odds_list = comp.get("odds", [])
            odds_summary = []
            h_ml_close = a_ml_close = None
            if odds_list:
                od = odds_list[0]
                details = od.get("details", "")
                ou = od.get("overUnder", "")
                if details or ou:
                    odds_summary.append(f"Spread: {details} | O/U: {ou}")

                # Moneyline close vs open
                ml = od.get("moneyline", {})
                h_ml_close = _safe_int_odds(ml.get("home", {}).get("close", {}).get("odds"))
                a_ml_close = _safe_int_odds(ml.get("away", {}).get("close", {}).get("odds"))
                h_ml_open = _safe_int_odds(ml.get("home", {}).get("open", {}).get("odds"))
                if h_ml_close is not None and a_ml_close is not None:
                    odds_summary.append(f"ML: {a_abbr} ({a_ml_close:+d}) / {h_abbr} ({h_ml_close:+d})")
                if h_ml_open is not None and h_ml_close is not None:
                    imp_diff = (american_to_implied(h_ml_close) - american_to_implied(h_ml_open)) * 100.0
                    if abs(imp_diff) >= 3.5:
                        steamed_team = h_abbr if imp_diff > 0 else a_abbr
                        tags.append(f"[🔥 ML STEAM: Sharp action steamed {steamed_team} ML ({imp_diff:+.1f}% implied prob shift)]")

                # Point Spread close vs open
                ps = od.get("pointSpread", {})
                h_sp_close = _safe_float_line(ps.get("home", {}).get("close", {}).get("line"))
                h_sp_open = _safe_float_line(ps.get("home", {}).get("open", {}).get("line"))
                h_sp_odds = _safe_int_odds(ps.get("home", {}).get("close", {}).get("odds"))
                a_sp_odds = _safe_int_odds(ps.get("away", {}).get("close", {}).get("odds"))
                if h_sp_close is not None and h_sp_odds is not None and a_sp_odds is not None:
                    odds_summary.append(
                        f"Spread Prices: {a_abbr} {-h_sp_close:+.1f} ({a_sp_odds:+d}) / {h_abbr} {h_sp_close:+.1f} ({h_sp_odds:+d})"
                    )
                if h_sp_open is not None and h_sp_close is not None and abs(h_sp_close - h_sp_open) >= 1.0:
                    moved_toward = h_abbr if h_sp_close < h_sp_open else a_abbr
                    tags.append(
                        f"[🔥 SPREAD STEAM: Moved {abs(h_sp_close - h_sp_open):.1f} pts toward {moved_toward} (Open {h_abbr} {h_sp_open:+.1f} -> Now {h_sp_close:+.1f})]"
                    )

                # Total close vs open
                tot = od.get("total", {})
                t_close = _safe_float_line(tot.get("over", {}).get("close", {}).get("line"))
                t_open = _safe_float_line(tot.get("over", {}).get("open", {}).get("line"))
                if t_open is not None and t_close is not None and abs(t_close - t_open) >= 1.0:
                    dir_lbl = "OVER" if t_close > t_open else "UNDER"
                    tags.append(f"[🔥 TOTAL STEAM: Moved {abs(t_close - t_open):.1f} pts toward {dir_lbl} (Open {t_open} -> Now {t_close})]")

            # Fetch Event Summary for ESPN BPI/FPI Predictor & NHL Starting Goalie Stats
            try:
                sum_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={ev_id}"
                sum_data = requests.get(sum_url, timeout=10).json()

                # ESPN BPI / FPI Model Win Probability vs Market Implied Probability
                pred = sum_data.get("predictor", {})
                h_proj = pred.get("homeTeam", {}).get("gameProjection")
                a_proj = pred.get("awayTeam", {}).get("gameProjection")
                if h_proj is not None and a_proj is not None:
                    h_bpi = float(h_proj)
                    a_bpi = float(a_proj)
                    odds_summary.append(f"ESPN BPI/FPI WinProb: {a_abbr} {a_bpi:.1f}% / {h_abbr} {h_bpi:.1f}%")
                    if h_ml_close is not None and a_ml_close is not None:
                        imp_h = american_to_implied(h_ml_close)
                        imp_a = american_to_implied(a_ml_close)
                        tot_imp = imp_h + imp_a
                        if tot_imp > 0:
                            mkt_h = (imp_h / tot_imp) * 100.0
                            mkt_a = (imp_a / tot_imp) * 100.0
                            if (h_bpi - mkt_h) >= 4.0 and h_ml_close >= -160:
                                dec_h = american_to_decimal(h_ml_close)
                                ev_h = round(((h_bpi / 100.0) * dec_h - 1.0) * 100.0, 2)
                                if ev_h >= 2.0:
                                    tags.append(
                                        f"[📊 BPI/FPI +EV: {h_abbr} ML ({h_ml_close:+d}) | BPI Win: {h_bpi:.1f}% vs Mkt NoVig: {mkt_h:.1f}% | EV: +{ev_h}%]"
                                    )
                            elif (a_bpi - mkt_a) >= 4.0 and a_ml_close >= -160:
                                dec_a = american_to_decimal(a_ml_close)
                                ev_a = round(((a_bpi / 100.0) * dec_a - 1.0) * 100.0, 2)
                                if ev_a >= 2.0:
                                    tags.append(
                                        f"[📊 BPI/FPI +EV: {a_abbr} ML ({a_ml_close:+d}) | BPI Win: {a_bpi:.1f}% vs Mkt NoVig: {mkt_a:.1f}% | EV: +{ev_a}%]"
                                    )

                # NHL Probable / Confirmed Starting Goalies + Season SV% & GAA
                if label == "NHL":
                    goalie_stats_lookup = {}
                    g_block = sum_data.get("goalies", {})
                    for side_key in ("homeTeam", "awayTeam"):
                        for g_ath in g_block.get(side_key, {}).get("athletes", []):
                            gid = str(g_ath.get("id", ""))
                            st_map = {s.get("abbreviation"): s.get("displayValue") for s in g_ath.get("statistics", [])}
                            goalie_stats_lookup[gid] = (
                                g_ath.get("displayName", ""),
                                st_map.get("SV%", "N/A"),
                                st_map.get("GAA", "N/A"),
                            )

                    goalie_notes = []
                    for side_comp, side_abbr in ((away, a_abbr), (home, h_abbr)):
                        probs = side_comp.get("probables", [])
                        if probs:
                            p0 = probs[0]
                            pid = str(p0.get("playerId", ""))
                            p_name = p0.get("athlete", {}).get("displayName", "Unknown")
                            p_status = p0.get("status", {}).get("name", "Probable")
                            _, sv_pct, gaa = goalie_stats_lookup.get(pid, (p_name, "N/A", "N/A"))
                            goalie_notes.append(f"{side_abbr}: {p_name} ({p_status}, {sv_pct} SV%, {gaa} GAA)")
                    if goalie_notes:
                        tags.append(f"[🥅 NHL GOALIES: {' vs '.join(goalie_notes)}]")
            except Exception:
                pass

            if odds_summary or tags:
                quant_games.append(
                    f"- [{label} QUANT] {matchup} {rec_str} -> {' | '.join(odds_summary)}"
                    + ((" -> " + " ".join(tags)) if tags else "")
                )

            # 3. Scan ESPN Core API Player Props for Significant Line/Juice Steam Moves (NBA & NHL)
            if label in ("NBA", "NHL") and ev_id:
                try:
                    pb_url = f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{ev_id}/competitions/{ev_id}/odds/100/propBets?lang=en&region=us&limit=120"
                    pb_items = requests.get(pb_url, timeout=10).json().get("items", [])
                    steamed_candidates = []
                    for item in pb_items:
                        prop_name = item.get("type", {}).get("name", "")
                        if prop_name not in (
                            "Total Points",
                            "Total Rebounds",
                            "Total Assists",
                            "Total 3-Point Field Goals",
                            "Total Pts, Reb, & Ast",
                            "Total Shots on Goal",
                        ):
                            continue
                        cur_line = _safe_float_line(item.get("current", {}).get("target", {}).get("value"))
                        open_line = _safe_float_line(item.get("open", {}).get("target", {}).get("value"))
                        cur_odds = _safe_int_odds(item.get("odds", {}).get("american", {}).get("value"))
                        open_odds = _safe_int_odds(item.get("odds", {}).get("american", {}).get("open"))
                        ath_ref = item.get("athlete", {}).get("$ref", "")
                        if cur_line is None or cur_odds is None or cur_odds < -155 or not ath_ref:
                            continue

                        line_delta = (cur_line - open_line) if open_line is not None else 0.0
                        imp_shift = (
                            (american_to_implied(cur_odds) - american_to_implied(open_odds)) * 100.0
                            if open_odds is not None
                            else 0.0
                        )
                        if abs(line_delta) >= 1.0 or imp_shift >= 4.0:
                            steamed_candidates.append(
                                (abs(line_delta) * 5.0 + imp_shift, ath_ref, prop_name, open_line, cur_line, open_odds, cur_odds)
                            )

                    steamed_candidates.sort(key=lambda x: x[0], reverse=True)
                    for _, ath_ref, prop_name, open_line, cur_line, open_odds, cur_odds in steamed_candidates[:4]:
                        if ath_ref not in athlete_cache:
                            try:
                                ath_info = requests.get(ath_ref.replace("http://", "https://"), timeout=6).json()
                                athlete_cache[ath_ref] = ath_info.get("displayName", "Player")
                            except Exception:
                                athlete_cache[ath_ref] = "Player"
                        p_name = athlete_cache[ath_ref]
                        if p_name == "Player":
                            continue
                        dir_side = "Over" if (cur_line > (open_line or cur_line) or cur_odds < (open_odds or cur_odds)) else "Under"
                        quant_props.append(
                            f"- [{label} PROP STEAM] {p_name} ({a_abbr} @ {h_abbr}) {prop_name} {dir_side} {cur_line} ({cur_odds:+d}) "
                            f"[Steamed from Open {open_line} ({open_odds:+d}) -> Now {cur_line} ({cur_odds:+d})]"
                        )
                except Exception:
                    pass

    return quant_games, quant_props


def fetch_espn_intel():
    intel_lines = []
    for league, urls in ESPN_ENDPOINTS.items():
        try:
            news_resp = requests.get(urls["news"], timeout=12).json()
            articles = news_resp.get("articles", [])[:6]
            for art in articles:
                headline = art.get("headline", "").strip()
                desc = art.get("description", "").strip()
                if headline:
                    intel_lines.append(f"- [{league} NEWS] {headline}: {desc}")
        except Exception:
            pass

        if "injuries" in urls:
            try:
                inj_resp = requests.get(urls["injuries"], timeout=12).json()
                for team_entry in inj_resp.get("injuries", []):
                    team_name = team_entry.get("team", {}).get("displayName", "Unknown")
                    flagged = []
                    for item in team_entry.get("injuries", []):
                        status = item.get("status", "")
                        if status in ("Out", "Questionable", "Doubtful", "Day-To-Day"):
                            athlete = item.get("athlete", {}).get("displayName", "")
                            pos = item.get("athlete", {}).get("position", {}).get("abbreviation", "")
                            detail = item.get("shortComment", "")
                            flagged.append(f"{athlete} ({pos}) - {status}: {detail}")
                    if flagged:
                        intel_lines.append(f"- [{league} INJURY - {team_name}] " + " | ".join(flagged[:5]))
            except Exception:
                pass
    return intel_lines or ["- No ESPN injury or breaking news items retrieved."]


def fetch_youtube_transcripts():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=26)
    yt_lines = []
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "playlistend": 3,
        "ignoreerrors": True,
    }
    for ch in YOUTUBE_CHANNELS:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(ch["url"], download=False)
                entries = (info or {}).get("entries", [])
                for vid in entries:
                    if not vid:
                        continue
                    vid_id = vid.get("id")
                    title = vid.get("title", "")
                    upload_date = vid.get("upload_date")
                    if upload_date:
                        dt = datetime.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
                        if dt < cutoff - datetime.timedelta(days=1):
                            continue
                    try:
                        if hasattr(YouTubeTranscriptApi, "get_transcript"):
                            transcript_list = YouTubeTranscriptApi.get_transcript(vid_id)
                            full_text = " ".join(chunk["text"] for chunk in transcript_list)
                        else:
                            fetched = YouTubeTranscriptApi().fetch(vid_id)
                            full_text = " ".join(
                                (chunk.text if hasattr(chunk, "text") else chunk["text"])
                                for chunk in fetched
                            )
                        condensed = re.sub(r"\s+", " ", full_text)[:1500]
                        yt_lines.append(f"- [{ch['name']}] {title} (https://youtu.be/{vid_id})\n  Transcript Extract: {condensed}")
                    except Exception:
                        yt_lines.append(f"- [{ch['name']}] {title} (https://youtu.be/{vid_id}) [No captions available]")
        except Exception as exc:
            yt_lines.append(f"- [{ch['name']}] Channel check skipped: {exc}")
    return yt_lines or ["- No new YouTube uploads found in the last 24 hours."]


def update_rolling_google_doc(today_str, section_body):
    if not INTEL_DOC_ID or (not GCP_SA_JSON and not os.path.exists("credentials.json")):
        print("Missing INTEL_DOC_ID or Google Service Account credentials; printing output locally.")
        print(section_body)
        return

    service = get_docs_service()
    doc = service.documents().get(documentId=INTEL_DOC_ID).execute()
    content = doc.get("body", {}).get("content", [])

    cutoff_date = (datetime.date.fromisoformat(today_str) - datetime.timedelta(days=7)).isoformat()
    doc_end_idx = content[-1].get("endIndex", 1) - 1

    # Locate each 'YYYY-MM-DD Slate Intel' section header paragraph and its exact start/end indices
    sec_starts = []
    for elem in content:
        para = elem.get("paragraph")
        if not para:
            continue
        t = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
        m = re.search(r"(\d{4}-\d{2}-\d{2}) Slate Intel", t)
        if m:
            sec_starts.append((m.group(1), elem.get("startIndex")))

    requests_batch = []
    for i, (sec_date, s_idx) in enumerate(sec_starts):
        e_idx = sec_starts[i + 1][1] if (i + 1) < len(sec_starts) else doc_end_idx
        if (sec_date < cutoff_date or sec_date == today_str) and s_idx and e_idx > s_idx:
            requests_batch.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": s_idx, "endIndex": e_idx}
                    }
                }
            )

    if requests_batch:
        # Delete from bottom of document to top so earlier character indices never shift
        requests_batch.sort(key=lambda r: r["deleteContentRange"]["range"]["startIndex"], reverse=True)
        service.documents().batchUpdate(
            documentId=INTEL_DOC_ID, body={"requests": requests_batch}
        ).execute()

    doc = service.documents().get(documentId=INTEL_DOC_ID).execute()
    insert_idx = 1
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if para:
            t = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
            if "Rolling Daily Slate Entries" in t:
                insert_idx = elem.get("endIndex", 1)
                break

    service.documents().batchUpdate(
        documentId=INTEL_DOC_ID,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": insert_idx},
                        "text": f"\n{section_body}\n",
                    }
                }
            ]
        },
    ).execute()
    print(f"Successfully updated Rolling 7-Day Intel Doc for {today_str}.")


def main():
    today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")
    odds_lines, prop_lines = fetch_hardrock_odds_and_props()
    espn_lines = fetch_espn_intel()
    yt_lines = fetch_youtube_transcripts()

    section_body = (
        f"## {today_str} Slate Intel\n\n"
        f"### 1. Hard Rock Bet vs. Sharp Market Odds (Includes 1H & No-Vig +EV%)\n"
        + "\n".join(odds_lines)
        + "\n\n### 2. +EV Player Prop Discrepancies (Hard Rock vs. Sharp Books)\n"
        + "\n".join(prop_lines)
        + "\n\n### 3. ESPN Breaking News & Injury Alerts\n"
        + "\n".join(espn_lines)
        + "\n\n### 4. Curated YouTube Channel Transcripts & Angles\n"
        + "\n".join(yt_lines)
        + "\n"
    )
    update_rolling_google_doc(today_str, section_body)


if __name__ == "__main__":
    main()
