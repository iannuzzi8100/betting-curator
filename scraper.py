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
import quant_signals

load_dotenv()

# Configuration from Environment / GitHub Secrets
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID") or "1vTMvUyr_DABrrzVBU7ah1A8RYn9-sfO1Uu-0Syk2jnU"
GCP_SA_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or os.environ.get("GCP_SA_KEY") or ""

# Expanded to include high-edge European soccer (EPL, UCL, La Liga) & tennis alongside core US sports
SPORTS_ODDS_KEYS = [
    "basketball_nba",
    "icehockey_nhl",
    "basketball_ncaab",
    "americanfootball_ncaaf",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "soccer_spain_la_liga",
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
    "soccer_spain_la_liga": "h2h,totals",
    "tennis_atp_finals": "h2h",
    "tennis_wta_finals": "h2h",
}

# Per-event endpoint (/v4/sports/{sport}/events/{eventId}/odds) supports 1st-Half lines & Player Props
PROP_MARKETS_BY_SPORT = {
    "basketball_nba": "spreads_h1,totals_h1,player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists",
    "basketball_ncaab": "spreads_h1,totals_h1",
    "americanfootball_ncaaf": "spreads_h1,totals_h1",
    "americanfootball_nfl": "spreads_h1,totals_h1,player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_rush_attempts,player_pass_tds",
    "icehockey_nhl": "player_points,player_assists,player_shots_on_goal",
}

SPORT_KEY_TO_LABEL = {
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAM",
    "americanfootball_ncaaf": "CFB",
    "americanfootball_nfl": "NFL",
    "soccer_epl": "EPL",
    "soccer_uefa_champs_league": "UCL",
    "soccer_spain_la_liga": "LALIGA",
}

PROP_MARKET_LABELS = {
    "spreads_h1": "1H Spread",
    "totals_h1": "1H Total",
    "player_pass_yds": "Passing Yds",
    "player_rush_yds": "Rush Yds",
    "player_reception_yds": "Rec Yds",
    "player_receptions": "Receptions",
    "player_rush_attempts": "Rush Att",
    "player_pass_tds": "Passing TDs",
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "3PM",
    "player_points_rebounds_assists": "PRA",
    "player_shots_on_goal": "Shots on Goal",
}

MAJOR_TV_NETWORKS = {
    "NBC", "CBS", "FOX", "ABC", "ESPN", "TNT", "TBS", "TRUTV",
    "PRIME VIDEO", "PRIME", "AMAZON PRIME", "NETFLIX", "PEACOCK",
    "USA", "USA NETWORK",
}

SOCCER_BIG_CLUBS = {
    "arsenal", "liverpool", "manchester city", "manchester united",
    "chelsea", "tottenham", "tottenham hotspur", "newcastle", "newcastle united",
    "aston villa", "real madrid", "barcelona", "atletico madrid", "atlético madrid",
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
    "NCAAM": {
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
    "EPL": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news",
    },
    "UCL": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/news",
    },
    "LALIGA": {
        "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
        "news": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/news",
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


def power_devig_prob(imp_target, imp_opp):
    """
    Power (Shin-style) devigging: solves for exponent k >= 1.0 such that imp_target^k + imp_opp^k = 1.0.
    Corrects for the Favorite-Longshot Bias where bookmakers pack disproportionate vig into underdogs.
    """
    if imp_target <= 0 or imp_opp <= 0 or (imp_target + imp_opp) <= 1.0:
        tot = imp_target + imp_opp
        return (imp_target / tot) if tot > 0 else 0.0
    lo, hi = 1.0, 5.0
    for _ in range(25):
        mid = (lo + hi) / 2.0
        val = (imp_target ** mid) + (imp_opp ** mid)
        if val > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    return imp_target ** k


def compute_novig_ev(hr_price, sharp_price_target, sharp_price_opp):
    """Strips vig from sharp 2-way line via Power Devigging and calculates exact EV% and Quarter-Kelly%."""
    imp_target = american_to_implied(sharp_price_target)
    imp_opp = american_to_implied(sharp_price_opp)
    if (imp_target + imp_opp) <= 0:
        return 0.0, 0.0, 0.0
    fair_prob = power_devig_prob(imp_target, imp_opp)
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


def _build_marquee_best_bet_lines(marquee_meta):
    """
    For every detected marquee/primetime game, pick the single highest-confidence bet across
    ALL scanned markets (Full-Game ML/Spread/Total, 1H Spread/Total, Player Props, and Prop Steam).
    - Emits [MARQUEE BEST BET] ONLY if the #1 candidate clears Tier 1 (>=88%) or Tier 2 (>=78%).
    - Otherwise emits [MARQUEE SKIP] so the curator omits the game cleanly instead of forcing a weak bet.
    """
    best_lines = []
    for entry in (marquee_meta or {}).values():
        cands = entry.get("candidates") or []
        label = entry.get("label", "")
        slot_tag = entry.get("slot_tag", "[WINDOW 5: 6:30-9:00 PM PRIMETIME]")
        time_est = entry.get("time_est", "TBD EST")
        matchup = entry.get("matchup", "")
        reason = entry.get("reason", "Marquee")
        rec_str = entry.get("rec_str", "")
        odds_summary = entry.get("odds_summary", "")

        if not cands:
            best_lines.append(
                f"- [MARQUEE SKIP] [{label}] ({time_est}) {matchup} ({reason}) -> No playable market cleared Tier 2 (78%+) — SKIP"
            )
            continue

        best = max(cands, key=lambda c: (c.get("score", 0), 1 if c.get("market") == "Full Game" else 0))
        b_score = int(best.get("score", 0))
        if b_score >= 78:
            sc_val, sc_bar, sc_tier = _format_confidence_meter(b_score)
            gl_parts = [p for p in (rec_str, odds_summary) if p]
            gl_str = " | ".join(gl_parts) if gl_parts else matchup
            best_lines.append(
                f"- [MARQUEE BEST BET] [{label}] {slot_tag} ({time_est}) {matchup} ({reason}) -> "
                f"{best['pick']} [{best.get('market', 'Best Market')}] "
                f"[CONFIDENCE METER: {sc_val}% | {sc_bar} | {sc_tier} -> Top Play: {best['pick']} ({best.get('reasons', 'Top Marquee Market')})] "
                f"| Game & Line: {gl_str}"
            )
        else:
            best_lines.append(
                f"- [MARQUEE SKIP] [{label}] ({time_est}) {matchup} ({reason}) -> "
                f"Highest market ({best.get('pick', 'N/A')}) scored {b_score}% (<78% Tier 2 floor) — SKIP"
            )
    return best_lines


def fetch_hardrock_odds_and_props():
    game_lines = []
    prop_lines = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc + datetime.timedelta(hours=36)

    # 1. Run ESPN Quantitative & Marquee/Primetime Detector FIRST (100% Free & Unlimited)
    #    so The Odds API always prioritizes every marquee/primetime game for full-market & player-prop deep scans.
    espn_quant_games, espn_quant_props, marquee_meta = fetch_espn_quant_matchups_and_props(return_marquee_meta=True)

    if ODDS_API_KEY:
        for sport_key in SPORTS_ODDS_KEYS:
            # 0-credit pre-check: /v4/sports/{sport}/events is FREE in The Odds API and prevents spending credits on off-days
            try:
                ev_check_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
                ev_check_resp = requests.get(ev_check_url, params={"apiKey": ODDS_API_KEY}, timeout=10)
                if ev_check_resp.status_code == 200:
                    upcoming = [
                        e
                        for e in ev_check_resp.json()
                        if e.get("commence_time")
                        and now_utc
                        <= datetime.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                        <= cutoff_utc
                    ]
                    if not upcoming:
                        continue
            except Exception:
                pass

            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            params = {
                "apiKey": ODDS_API_KEY,
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

                    mq_entry = _find_marquee_entry(marquee_meta, sport_key, away, home)
                    is_today_slate = False
                    if commence and event_id:
                        try:
                            c_dt = datetime.datetime.fromisoformat(commence.replace("Z", "+00:00"))
                            if now_utc <= c_dt <= cutoff_utc:
                                is_today_slate = True
                                todays_event_ids.append((event_id, f"{away} @ {home}", mq_entry))
                        except Exception:
                            pass

                    # Multi-book sharp lookup for consensus No-Vig devigging & stale line outlier detection
                    hr_summary = []
                    ev_tags = []
                    has_pinnacle_bulk = any(b["key"] == "pinnacle" for b in sharp_books)
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

                            ev_pct_val = 0.0
                            if fair_probs:
                                avg_fp = sum(fair_probs) / len(fair_probs)
                                hr_dec = american_to_decimal(o_price)
                                ev_pct_val = round(((avg_fp * hr_dec) - 1.0) * 100.0, 2)
                                b_dec = hr_dec - 1.0
                                full_k = ((b_dec * avg_fp) - (1.0 - avg_fp)) / b_dec if b_dec > 0 else 0.0
                                q_kelly = round(max(0.0, full_k * 0.25 * 100.0), 1)
                                if ev_pct_val >= 1.5:
                                    ev_tags.append(
                                        f"[+EV ({len(fair_probs)}B Consensus): {m_key.upper()} {o_name} {o_pt if o_pt is not None else ''} @ {o_price:+d} | "
                                        f"FairWin: {round(avg_fp*100,1)}% | EV: +{ev_pct_val}% | 1/4-Kelly: {q_kelly}%]"
                                    )
                            if len(stale_diffs) >= 2:
                                ev_tags.append(
                                    f"[🔥 STALE LINE: {m_key.upper()} {o_name} {o_pt} ({o_price:+d}) on HRB vs Sharp ({', '.join(stale_diffs[:3])})]"
                                )

                            # Score every full-game market candidate for marquee games
                            if mq_entry is not None and (ev_pct_val >= 1.5 or len(stale_diffs) >= 2):
                                max_lay = -7.5 if sport_key == "americanfootball_ncaaf" else -6.5
                                if not (m_key == "spreads" and o_pt is not None and o_pt < max_lay):
                                    c_score, c_reasons = _score_market_candidate_confidence(
                                        ev_pct=ev_pct_val,
                                        has_line_edge=len(stale_diffs) >= 1,
                                        stale_book_count=len(stale_diffs),
                                        is_pinnacle=has_pinnacle_bulk,
                                        n_sharp_books=max(len(fair_probs), len(stale_diffs)),
                                        hr_price=o_price,
                                    )
                                    if c_score > 0:
                                        if m_key == "h2h":
                                            pick_lbl = f"{o_name} ML ({o_price:+d})"
                                            mkt_lbl = "Sharp ML"
                                        elif m_key == "spreads":
                                            pick_lbl = f"{o_name} {o_pt:+.1f} ({o_price:+d})"
                                            mkt_lbl = "Sharp Spread"
                                        else:
                                            pick_lbl = f"{o_name} {o_pt} ({o_price:+d})"
                                            mkt_lbl = "Sharp Total"
                                        mq_entry["candidates"].append({
                                            "score": c_score,
                                            "pick": pick_lbl,
                                            "market": mkt_lbl,
                                            "reasons": ", ".join(c_reasons),
                                        })

                        hr_summary.append(f"{m_key.upper()}: [{', '.join(outcomes_str)}]")

                    if not is_today_slate and not ev_tags:
                        continue

                    ev_banner = (" -> " + " ".join(ev_tags)) if ev_tags else ""
                    sharp_label = sharp_book["key"] if sharp_book else "none"
                    mq_prefix = f"[MARQUEE: {mq_entry['reason']}] " if mq_entry else ""
                    game_lines.append(
                        f"- [{sport_key}] {mq_prefix}{away} @ {home} ({commence}) [SharpRef: {sharp_label}] -> {' ; '.join(hr_summary)}{ev_banner}"
                    )

                # Prioritize MARQUEE / PRIMETIME games FIRST in per-event 1H & Player Prop deep scan
                if sport_key in PROP_MARKETS_BY_SPORT and todays_event_ids and rem_quota > 40:
                    marquee_evs = [x for x in todays_event_ids if x[2] is not None]
                    other_evs = [x for x in todays_event_ids if x[2] is None]
                    max_scan = 6 if marquee_evs else 4
                    scan_queue = marquee_evs[:6]
                    if rem_quota > 60 and len(scan_queue) < max_scan:
                        scan_queue.extend(other_evs[: max_scan - len(scan_queue)])

                    for ev_id, matchup, mq_entry in scan_queue:
                        ev_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{ev_id}/odds"
                        ev_params = {
                            "apiKey": ODDS_API_KEY,
                            "markets": PROP_MARKETS_BY_SPORT[sport_key],
                            "oddsFormat": "american",
                            "bookmakers": "hardrockbet,pinnacle,fanduel,draftkings",
                        }
                        try:
                            p_resp = requests.get(ev_url, params=ev_params, timeout=15)
                            if p_resp.status_code != 200:
                                continue
                            p_data = p_resp.json()
                            ev_books = p_data.get("bookmakers", [])
                            hr_p = next((b for b in ev_books if b["key"] == "hardrockbet"), None)
                            ref_books = [b for b in ev_books if b["key"] in ("pinnacle", "fanduel", "draftkings")]
                            ref_p = (
                                next((b for b in ref_books if b["key"] == "pinnacle"), None)
                                or next((b for b in ref_books if b["key"] == "fanduel"), None)
                                or next((b for b in ref_books if b["key"] == "draftkings"), None)
                            )
                            if not hr_p or not ref_p:
                                continue

                            # Build per-book lookup so we can compute multi-book sharp consensus + Pinnacle priority
                            ref_lookups = {}
                            for rb in ref_books:
                                lk = {}
                                for rm in rb.get("markets", []):
                                    for ro in rm.get("outcomes", []):
                                        lk[(rm["key"], ro.get("description", ""), ro.get("name", ""))] = (
                                            ro.get("point"),
                                            ro.get("price"),
                                        )
                                ref_lookups[rb["key"]] = lk

                            for m in hr_p.get("markets", []):
                                m_key = m["key"]
                                m_disp = PROP_MARKET_LABELS.get(m_key, m_key)
                                if m_key in ("spreads_h1", "totals_h1"):
                                    for o in m.get("outcomes", []):
                                        o_name = o.get("name", "")
                                        o_pt = o.get("point")
                                        o_price = o.get("price")
                                        if o_pt is None or o_price is None or o_price < -155 or o_price > 130:
                                            continue
                                        h1_fps = []
                                        h1_stale = 0
                                        h1_has_pin = False
                                        for rb in ref_books:
                                            s_outs = [
                                                ro for rm in rb.get("markets", []) if rm["key"] == m_key for ro in rm.get("outcomes", [])
                                            ]
                                            if len(s_outs) != 2:
                                                continue
                                            s_same = next((x for x in s_outs if x.get("name") == o_name), None)
                                            s_other = next((x for x in s_outs if x.get("name") != o_name), None)
                                            if not s_same or not s_other:
                                                continue
                                            if s_same.get("point") == o_pt:
                                                fp, _, _ = compute_novig_ev(o_price, s_same["price"], s_other["price"])
                                                if fp > 0:
                                                    h1_fps.append(fp / 100.0)
                                                    if rb["key"] == "pinnacle":
                                                        h1_has_pin = True
                                            elif s_same.get("point") is not None and o_price >= -125:
                                                sp_ref = s_same.get("point")
                                                if (m_key == "spreads_h1" and o_pt > sp_ref) or (
                                                    m_key == "totals_h1"
                                                    and ((o_name == "Over" and o_pt < sp_ref) or (o_name == "Under" and o_pt > sp_ref))
                                                ):
                                                    h1_stale += 1
                                                    if rb["key"] == "pinnacle":
                                                        h1_has_pin = True
                                        h1_ev = 0.0
                                        fair_p = 0.0
                                        q_kelly = 0.0
                                        if h1_fps:
                                            avg_fp = sum(h1_fps) / len(h1_fps)
                                            hr_dec = american_to_decimal(o_price)
                                            fair_p = round(avg_fp * 100.0, 1)
                                            h1_ev = round(((avg_fp * hr_dec) - 1.0) * 100.0, 2)
                                            b_dec = hr_dec - 1.0
                                            full_k = ((b_dec * avg_fp) - (1.0 - avg_fp)) / b_dec if b_dec > 0 else 0.0
                                            q_kelly = round(max(0.0, full_k * 0.25 * 100.0), 1)
                                        if h1_ev >= 1.5 or h1_stale >= 1:
                                            c_score, c_reasons = _score_market_candidate_confidence(
                                                ev_pct=h1_ev,
                                                has_line_edge=h1_stale >= 1,
                                                stale_book_count=h1_stale,
                                                is_pinnacle=h1_has_pin,
                                                n_sharp_books=max(len(h1_fps), h1_stale),
                                                hr_price=o_price,
                                            )
                                            cm_str = ""
                                            if c_score >= 78:
                                                sc_v, sc_b, sc_t = _format_confidence_meter(c_score)
                                                cm_str = f" [CONFIDENCE METER: {sc_v}% | {sc_b} | {sc_t}]"
                                            pt_fmt = f"{o_pt:+.1f}" if m_key == "spreads_h1" else f"{o_pt}"
                                            game_lines.append(
                                                f"- [{sport_key} 1H] {matchup} -> {m_disp} {o_name} {pt_fmt} ({o_price:+d}) "
                                                f"| FairWin: {fair_p}% | EV: +{h1_ev}% | 1/4-Kelly: {q_kelly}%{cm_str}"
                                            )
                                            if mq_entry is not None and c_score > 0:
                                                mq_entry["candidates"].append({
                                                    "score": c_score,
                                                    "pick": f"{o_name} {pt_fmt} 1H ({o_price:+d})" if m_key == "spreads_h1" else f"1H {o_name} {pt_fmt} ({o_price:+d})",
                                                    "market": m_disp,
                                                    "reasons": ", ".join(c_reasons),
                                                })
                                    continue

                                for o in m.get("outcomes", []):
                                    player = o.get("description", "")
                                    side = o.get("name", "")
                                    opp_side = "Under" if side == "Over" else "Over"
                                    hr_pt = o.get("point")
                                    hr_price = o.get("price")
                                    if hr_pt is None or hr_price is None or hr_price < -155 or hr_price > 130:
                                        continue

                                    p_fps = []
                                    stale_books = []
                                    has_pin_prop = False
                                    best_ref_label = ref_p["key"]
                                    best_ref_pt = None
                                    best_ref_price = None

                                    for rb_key, lk in ref_lookups.items():
                                        rv = lk.get((m_key, player, side))
                                        ro_opp = lk.get((m_key, player, opp_side))
                                        if not rv or rv[0] is None or rv[1] is None:
                                            continue
                                        r_pt, r_pr = rv
                                        # Market Width (Hold) Filter: reject wide/illiquid sharp markets (> 9.5% total hold)
                                        if ro_opp and ro_opp[1] is not None:
                                            if (american_to_implied(r_pr) + american_to_implied(ro_opp[1])) > 1.095:
                                                continue
                                        if best_ref_pt is None or rb_key == "pinnacle":
                                            best_ref_label = rb_key
                                            best_ref_pt = r_pt
                                            best_ref_price = r_pr

                                        if hr_pt == r_pt and ro_opp and ro_opp[0] == hr_pt and ro_opp[1] is not None:
                                            fp, _, _ = compute_novig_ev(hr_price, r_pr, ro_opp[1])
                                            if fp > 0:
                                                p_fps.append(fp / 100.0)
                                                if rb_key == "pinnacle":
                                                    has_pin_prop = True
                                        elif (
                                            ((side == "Over" and hr_pt < r_pt) or (side == "Under" and hr_pt > r_pt))
                                            and hr_price >= (r_pr - 25)
                                        ):
                                            stale_books.append(f"{rb_key}:{r_pt}")
                                            if rb_key == "pinnacle":
                                                has_pin_prop = True

                                    if best_ref_pt is None:
                                        continue

                                    prop_ev = 0.0
                                    ev_str = ""
                                    if p_fps:
                                        avg_fp = sum(p_fps) / len(p_fps)
                                        hr_dec = american_to_decimal(hr_price)
                                        prop_ev = round(((avg_fp * hr_dec) - 1.0) * 100.0, 2)
                                        b_dec = hr_dec - 1.0
                                        full_k = ((b_dec * avg_fp) - (1.0 - avg_fp)) / b_dec if b_dec > 0 else 0.0
                                        q_kelly = round(max(0.0, full_k * 0.25 * 100.0), 1)
                                        if prop_ev >= 2.0:
                                            ev_str = f" | FairWin: {round(avg_fp*100,1)}% | EV: +{prop_ev}% | 1/4-Kelly: {q_kelly}%"

                                    has_line_edge = len(stale_books) >= 1
                                    if has_line_edge or prop_ev >= 2.0:
                                        c_score, c_reasons = _score_market_candidate_confidence(
                                            ev_pct=prop_ev,
                                            has_line_edge=has_line_edge,
                                            stale_book_count=len(stale_books),
                                            is_pinnacle=has_pin_prop,
                                            n_sharp_books=max(len(p_fps), len(stale_books)),
                                            hr_price=hr_price,
                                        )
                                        # Severe outdoor NFL weather adjustment for Passing Props
                                        if (
                                            c_score > 0
                                            and sport_key == "americanfootball_nfl"
                                            and m_key in ("player_pass_yds", "player_pass_tds")
                                            and mq_entry is not None
                                            and (mq_entry.get("weather") or {}).get("severe")
                                        ):
                                            if side == "Over":
                                                c_score = max(25, c_score - 8)
                                                c_reasons.append("Severe Weather Wind/Precip (-8)")
                                            elif side == "Under":
                                                c_score = min(96, c_score + 6)
                                                c_reasons.append("Severe Weather Wind/Precip (+6)")
                                        cm_str = ""
                                        if c_score >= 78:
                                            sc_v, sc_b, sc_t = _format_confidence_meter(c_score)
                                            cm_str = f" [CONFIDENCE METER: {sc_v}% | {sc_b} | {sc_t}]"
                                        prop_lines.append(
                                            f"- [{sport_key} PROP] {player} ({matchup}) {m_disp} {side} {hr_pt} ({hr_price:+d}) "
                                            f"vs {best_ref_label} {best_ref_pt} ({best_ref_price:+d}){ev_str}{cm_str}"
                                        )
                                        if mq_entry is not None and c_score > 0:
                                            mq_entry["candidates"].append({
                                                "score": c_score,
                                                "pick": f"{player} {side} {hr_pt} {m_disp} ({hr_price:+d})",
                                                "market": f"Player Prop ({m_disp})",
                                                "reasons": ", ".join(c_reasons),
                                            })
                        except Exception:
                            continue
            except Exception as exc:
                game_lines.append(f"- [{sport_key}] Odds fetch error: {exc}")

    # Emit Marquee / Primetime Full-Market Best Bets (strictly >= 78% Tier 1 & Tier 2; <78% skipped) at the top
    marquee_summary_lines = _build_marquee_best_bet_lines(marquee_meta)
    final_game_lines = marquee_summary_lines + game_lines + espn_quant_games
    final_prop_lines = prop_lines + espn_quant_props

    return (
        final_game_lines or ["- No active lines found for today's slate."],
        final_prop_lines or ["- No positive-EV player prop discrepancies detected on today's slate."],
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


def _format_confidence_meter(score):
    score = max(25, min(99, int(round(score))))
    filled = max(1, min(10, int(round(score / 10.0))))
    bar = "█" * filled + "░" * (10 - filled)
    if score >= 88:
        tier = "🔥 TIER 1: MAX HAMMER"
    elif score >= 78:
        tier = "💎 TIER 2: STRONG CONVICTION"
    else:
        tier = "⛔ PASS (<78% — Tier 1 & 2 Only)"
    return score, bar, tier


def _parse_rec_wins_losses(rec_str):
    if not rec_str:
        return 0, 0
    m = re.match(r"(\d+)-(\d+)", str(rec_str).strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def _extract_major_tv_networks(comp):
    """Extract national TV broadcast labels (NBC, CBS, FOX, ABC, ESPN, TNT, Prime Video, Netflix, etc.)."""
    found = []
    seen = set()
    raw_names = []
    for b in comp.get("broadcasts", []) or []:
        for n in b.get("names", []) or []:
            if n:
                raw_names.append(str(n).strip())
    for gb in comp.get("geoBroadcasts", []) or []:
        short_n = (gb.get("media") or {}).get("shortName")
        if short_n:
            raw_names.append(str(short_n).strip())
    for raw in raw_names:
        up = raw.upper().strip()
        # Exclude streaming-only minor tiers like ESPN+ / ESPNU / ESPNEWS
        if up in ("ESPN+", "ESPNU", "ESPNEWS", "ACCX", "SECN+", "BTN+"):
            continue
        matched = None
        if up in MAJOR_TV_NETWORKS:
            matched = raw
        elif up.startswith("PRIME") or "AMAZON PRIME" in up:
            matched = "Prime Video"
        elif up.startswith("TNT"):
            matched = "TNT"
        elif up.startswith("NBC") and up != "NBCSN":
            matched = "NBC"
        elif up in ("ESPN", "ESPN2"):
            matched = up
        if matched and matched.upper() not in seen:
            seen.add(matched.upper())
            found.append(matched)
    return found


def _detect_marquee_game(label, event, comp, home, away, dt_est=None):
    """
    Auto-detect major / primetime games across leagues:
    - NFL: Standalone non-Sunday (TNF/Black Friday/Sat/MNF), Sunday 4:05/4:25 PM national window or 8:20 PM SNF,
      morning international games, or Sunday matchups where both teams are >= .500 with 4+ wins.
    - CFB / NCAAM: AP Top-25 vs Top-25 ranked matchup, Top-15 team on major national TV, or non-Saturday CFB on national TV.
    - NBA / NHL: Major national TV broadcast (ABC/NBC/ESPN/TNT/Prime/CBS/FOX/TBS/truTV/Netflix) or elite matchup (both >= .600 with 10+ GP).
    - UCL: All UEFA Champions League matches.
    - EPL / LALIGA: Big-club headliner (two big clubs) or national TV (NBC/USA/ABC/ESPN).
    Returns (is_marquee: bool, reason: str).
    """
    lbl = str(label or "").upper()
    tv_nets = _extract_major_tv_networks(comp or {})
    primary_tv = tv_nets[0] if tv_nets else ""
    tier1_tv = [n for n in tv_nets if n.upper() != "ESPN2"]
    h_name = (home.get("team") or {}).get("displayName", "")
    a_name = (away.get("team") or {}).get("displayName", "")
    h_rec = next((r.get("summary", "") for r in (home.get("records") or []) if r.get("type") == "total"), "")
    a_rec = next((r.get("summary", "") for r in (away.get("records") or []) if r.get("type") == "total"), "")
    hw, hl = _parse_rec_wins_losses(h_rec)
    aw, al = _parse_rec_wins_losses(a_rec)

    h_rank = int((home.get("curatedRank") or {}).get("current", 99) or 99)
    a_rank = int((away.get("curatedRank") or {}).get("current", 99) or 99)

    if lbl == "NFL":
        if dt_est is not None:
            wd = dt_est.weekday()  # 6 = Sunday
            hr = dt_est.hour + dt_est.minute / 60.0
            if wd != 6:
                net_tag = f" ({primary_tv})" if primary_tv else ""
                return True, f"NFL Standalone Primetime{net_tag}"
            if hr >= 19.5:
                return True, f"NFL Sunday Night ({primary_tv or 'NBC'})"
            if hr >= 15.75:
                return True, f"NFL Late National Window ({primary_tv or 'CBS/FOX'})"
            if hr < 11.5:
                return True, f"NFL International Window ({primary_tv or 'NFLN'})"
        if hw >= 4 and aw >= 4 and hw >= hl and aw >= al:
            return True, f"NFL Winning Matchup ({a_rec} vs {h_rec})"
        return False, ""

    if lbl in ("CFB", "NCAAM"):
        if 1 <= a_rank <= 25 and 1 <= h_rank <= 25:
            return True, f"AP Top-25 Matchup (#{a_rank} vs #{h_rank})"
        best_rank = min(a_rank, h_rank)
        if 1 <= best_rank <= 15 and tv_nets:
            return True, f"Top-15 National TV (#{best_rank} on {primary_tv})"
        if lbl == "CFB" and dt_est is not None and dt_est.weekday() != 5 and tier1_tv:
            return True, f"CFB Standalone National TV ({tier1_tv[0]})"
        return False, ""

    if lbl in ("NBA", "NHL"):
        if tier1_tv:
            return True, f"National TV ({tier1_tv[0]})"
        if (hw + hl) >= 10 and (aw + al) >= 10 and (hw / (hw + hl)) >= 0.60 and (aw / (aw + al)) >= 0.60:
            return True, f"Contender Matchup ({a_rec} vs {h_rec})"
        return False, ""

    if lbl == "UCL":
        return True, "UEFA Champions League"

    if lbl in ("EPL", "LALIGA"):
        h_low = h_name.lower()
        a_low = a_name.lower()
        h_big = any(c in h_low for c in SOCCER_BIG_CLUBS)
        a_big = any(c in a_low for c in SOCCER_BIG_CLUBS)
        if h_big and a_big:
            return True, f"{lbl} Big-Club Headliner"
        if tier1_tv:
            return True, f"{lbl} National TV ({tier1_tv[0]})"
        return False, ""

    return False, ""


def _score_market_candidate_confidence(
    ev_pct=0.0,
    has_line_edge=False,
    stale_book_count=0,
    is_pinnacle=False,
    n_sharp_books=1,
    hr_price=-110,
    steam_shift=0.0,
):
    """
    Deterministic 0-99 Confidence Meter for Odds API 1H, Totals, Spreads, ML, and Player Props.
    Calibrated strictly so ONLY genuine high-conviction sharp edges reach Tier 2 (>=78%) or Tier 1 (>=88%).
    Returns (score: int, reasons: list[str]).
    """
    if hr_price is None or hr_price < -155 or hr_price > 130:
        return 0, []

    score = 50
    reasons = []
    has_edge_driver = False

    if ev_pct >= 6.0:
        score += 24
        reasons.append(f"Sharp No-Vig +{ev_pct:.1f}% EV (+24)")
        has_edge_driver = True
    elif ev_pct >= 4.0:
        score += 20
        reasons.append(f"Sharp No-Vig +{ev_pct:.1f}% EV (+20)")
        has_edge_driver = True
    elif ev_pct >= 2.5:
        score += 15
        reasons.append(f"Sharp No-Vig +{ev_pct:.1f}% EV (+15)")
        has_edge_driver = True
    elif ev_pct >= 1.5:
        score += 10
        reasons.append(f"Sharp No-Vig +{ev_pct:.1f}% EV (+10)")
        has_edge_driver = True

    if stale_book_count >= 2:
        score += 16
        reasons.append(f"HRB Stale Line vs {stale_book_count} Sharp Books (+16)")
        has_edge_driver = True
    elif has_line_edge or stale_book_count == 1:
        score += 12
        reasons.append("HRB Stale Line Edge (+12)")
        has_edge_driver = True

    if steam_shift >= 7.0:
        score += 14
        reasons.append(f"Sharp Juice Steam +{steam_shift:.1f}% (+14)")
        has_edge_driver = True
    elif steam_shift >= 4.5:
        score += 10
        reasons.append(f"Sharp Juice Steam +{steam_shift:.1f}% (+10)")
        has_edge_driver = True

    if not has_edge_driver:
        return 0, []

    if is_pinnacle or n_sharp_books >= 2:
        score += 6
        reasons.append("Pinnacle/Multi-Book Sharp Consensus (+6)")
    elif n_sharp_books == 1:
        score += 3
        reasons.append("Sharp Book Benchmark (+3)")

    if -145 <= hr_price <= 110:
        score += 6
        reasons.append(f"Odds Sweet-Spot {hr_price:+d} (+6)")
    else:
        score += 3
        reasons.append(f"Playable Odds {hr_price:+d} (+3)")

    return max(25, min(96, int(round(score)))), reasons


def _norm_team_key(name):
    toks = [t for t in re.findall(r"[a-z0-9]+", str(name or "").lower()) if len(t) >= 3 and t not in ("the", "fc", "cf", "sc", "afc", "club")]
    return set(toks)


def _find_marquee_entry(marquee_meta, sport_key, away, home):
    if not marquee_meta:
        return None
    target_lbl = SPORT_KEY_TO_LABEL.get(sport_key, "").upper()
    a_toks = _norm_team_key(away)
    h_toks = _norm_team_key(home)
    if not a_toks or not h_toks:
        return None
    for entry in marquee_meta.values():
        if target_lbl and entry.get("label") != target_lbl:
            continue
        ea = _norm_team_key(entry.get("a_name"))
        eh = _norm_team_key(entry.get("h_name"))
        if (a_toks & ea) and (h_toks & eh):
            return entry
    return None


def _compute_matchup_confidence(
    label,
    a_abbr,
    h_abbr,
    a_rec,
    h_rec,
    a_ml,
    h_ml,
    h_sp,
    a_sp_odds,
    h_sp_odds,
    a_bpi,
    h_bpi,
    l5_parsed,
    tags,
    extra_signals=None,
    pillar_weights=None,
):
    """
    Deterministic 0-99 Confidence Meter across Core + Multi-Model Quantitative Pillars:
    1. TrueWin Probability & Odds Sweet-Spot (+14 Moderate Fav, +8 Light Fav/Even, -10 Dog without 2+ signals, -25 Cap)
    2. Model & Market +EV% Edge (ESPN BPI/FPI for NBA/NFL/CFB; Bart Torvik AdjEff Log5+HCA for NCAAM)
    3. Last-5-Games Form & Scoring Margin + Season Net Differential & Regression Guard
    4. Sharp Line Movement & Timing (+10 Early Sharp Steam, -30 Late Chased Steam Auto-Kill, -15 Reverse Steam)
    5. Situational & Matchup Mismatch (0d-Rest B2B, 3-in-4 Fatigue, Home/Road Splits, Key Injuries, NHL Goalie SV%/GSAx & MoneyPuck 5v5 xGF%)
    6. Multi-Signal Confluence Bonus (+8 for 3+ aligned pillars, +4 for 2 aligned pillars)
    Optional `pillar_weights` applies bounded [0.65, 1.25] self-calibrated multipliers from `Shadow Grading Log`.
    """
    tags_joined = " ".join(tags)
    max_fav_cap = -7.5 if label == "CFB" else -6.5
    a_sp = (-h_sp) if h_sp is not None else None
    ex = extra_signals or {}
    pw = pillar_weights or {}

    def _w(pts, key):
        return int(round(pts * float(pw.get(key, 1.0))))

    split_info = ex.get("split_info") or {}
    injury_info = ex.get("injury_info") or {}
    net_diff_info = ex.get("net_diff_info") or {}
    torvik_probs = ex.get("torvik_probs") or (None, None)
    moneypuck_info = ex.get("moneypuck_info") or {}
    density_info = ex.get("density_info") or {}

    mkt_a = mkt_h = None
    if a_ml is not None and h_ml is not None:
        imp_a = american_to_implied(a_ml)
        imp_h = american_to_implied(h_ml)
        if (imp_a + imp_h) > 0:
            mkt_a = power_devig_prob(imp_a, imp_h) * 100.0
            mkt_h = power_devig_prob(imp_h, imp_a) * 100.0

    best_score = -999
    best_pick_label = "No Playable Side"
    best_reasons = []

    sides = [
        (False, a_abbr, h_abbr, a_rec, h_rec, a_ml, a_sp, a_sp_odds, a_bpi, mkt_a, torvik_probs[0]),
        (True, h_abbr, a_abbr, h_rec, a_rec, h_ml, h_sp, h_sp_odds, h_bpi, mkt_h, torvik_probs[1]),
    ]

    for is_home, s_abbr, o_abbr, s_rec, o_rec, s_ml, s_sp, s_sp_odds, s_bpi, s_mkt, s_torv in sides:
        score = 50
        pillars = 0
        indep_edges = 0
        is_chased_steam = False
        reasons = []

        # Determine best playable straight market for this side (strongly prefer Moneyline sweet-spot over steeper spreads)
        pick_str = None
        mkt_tier = None  # "mod_ml" | "even_ml" | "small_sp" | "mid_sp" | "short_dog_sp" | "plus_dog" | "blocked"

        if s_ml is not None and -155 <= s_ml <= -110:
            pick_str = f"{s_abbr} ML ({s_ml:+d})"
            mkt_tier = "mod_ml"
        elif s_ml is not None and -109 <= s_ml <= 105:
            pick_str = f"{s_abbr} ML ({s_ml:+d})"
            mkt_tier = "even_ml"
        elif s_sp is not None and s_sp_odds is not None and -3.5 <= s_sp <= -0.5 and -130 <= s_sp_odds <= 110:
            pick_str = f"{s_abbr} {s_sp:+.1f} ({s_sp_odds:+d})"
            mkt_tier = "small_sp"
        elif s_sp is not None and s_sp_odds is not None and max_fav_cap <= s_sp < -3.5 and -130 <= s_sp_odds <= 110:
            pick_str = f"{s_abbr} {s_sp:+.1f} ({s_sp_odds:+d})"
            mkt_tier = "mid_sp"
        elif s_sp is not None and s_sp_odds is not None and 0.5 <= s_sp <= 3.5 and -125 <= s_sp_odds <= 110:
            pick_str = f"{s_abbr} {s_sp:+.1f} ({s_sp_odds:+d})"
            mkt_tier = "short_dog_sp"
        elif s_ml is not None and 106 <= s_ml <= 130:
            pick_str = f"{s_abbr} ML ({s_ml:+d})"
            mkt_tier = "plus_dog"
        elif s_sp is not None and s_sp_odds is not None and 3.5 < s_sp <= 7.5 and -125 <= s_sp_odds <= 110:
            pick_str = f"{s_abbr} {s_sp:+.1f} ({s_sp_odds:+d})"
            mkt_tier = "plus_dog"
        elif s_sp is not None and s_sp < max_fav_cap:
            pick_str = f"{s_abbr} {s_sp:+.1f} (EXCEEDS {max_fav_cap} CAP — PIVOT 1H/TOTAL OR PASS)"
            mkt_tier = "blocked"
        elif s_ml is not None:
            pick_str = f"{s_abbr} ML ({s_ml:+d})"
            if s_ml < -155 or s_ml > 130:
                mkt_tier = "blocked"
            else:
                mkt_tier = "even_ml"
        else:
            continue

        # Pillar 1: Market & Odds Sweet-Spot (ML prioritized over -4.0..-6.5 spreads)
        if mkt_tier == "mod_ml":
            pts = _w(14, "mod_ml")
            score += pts
            pillars += 1
            reasons.append(f"ModFav ML Sweet-Spot (+{pts})")
        elif mkt_tier == "even_ml":
            score += 8
            pillars += 1
            reasons.append("Even/LightFav ML (+8)")
        elif mkt_tier == "small_sp":
            score += 6
            pillars += 1
            reasons.append("Small Fav Spread <=3.5 (+6)")
        elif mkt_tier == "short_dog_sp":
            score += 4
            reasons.append("Short Dog Spread (+4)")
        elif mkt_tier == "blocked":
            score -= 25
            reasons.append("Outside Odds/Spread Cap (-25)")

        # Pillar 2A: ESPN BPI/FPI Model & Market +EV% Edge (NBA/NFL/CFB)
        if s_bpi is not None and s_mkt is not None and s_ml is not None and label in ("NBA", "NFL", "CFB"):
            dec_o = american_to_decimal(s_ml)
            ev_pct = ((s_bpi / 100.0) * dec_o - 1.0) * 100.0
            if ev_pct >= 5.0 and (s_bpi - s_mkt) >= 3.5:
                pts = _w(15, "bpi_ev")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"BPI/FPI +{ev_pct:.1f}% EV (+{pts})")
            elif ev_pct >= 2.5 and (s_bpi - s_mkt) >= 2.0:
                pts = _w(9, "bpi_ev")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"BPI/FPI +{ev_pct:.1f}% EV (+{pts})")
            elif (s_bpi - s_mkt) <= -4.0:
                score -= 12
                reasons.append("Model Disagrees (-12)")

        # Pillar 2B: Bart Torvik NCAAM Adjusted Efficiency (Barthag Log5 + HCA) vs Market
        if label == "NCAAM" and s_torv is not None and s_mkt is not None and s_ml is not None:
            dec_o = american_to_decimal(s_ml)
            t_ev = ((s_torv / 100.0) * dec_o - 1.0) * 100.0
            if t_ev >= 5.0 and (s_torv - s_mkt) >= 4.0:
                pts = _w(13, "torvik_ev")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"Torvik AdjEff +{t_ev:.1f}% EV (+{pts})")
            elif t_ev >= 2.5 and (s_torv - s_mkt) >= 2.5:
                pts = _w(8, "torvik_ev")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"Torvik AdjEff +{t_ev:.1f}% EV (+{pts})")
            elif (s_torv - s_mkt) <= -5.0:
                score -= 10
                reasons.append("Torvik Disagrees (-10)")

        # Pillar 3: Last-5-Games Form & Scoring Margin (historically highest-ROI independent pillar)
        if f"FORM EDGE: {s_abbr}" in tags_joined:
            pts = _w(12, "l5_form")
            score += pts
            pillars += 1
            indep_edges += 1
            reasons.append(f"Major L5 Form Edge (+{pts})")
        elif s_abbr in l5_parsed and o_abbr in l5_parsed:
            sw, sl, sm = l5_parsed[s_abbr]
            ow, ol, om = l5_parsed[o_abbr]
            if sw > ow and sm > om:
                pts = _w(6, "l5_form")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"L5 Edge {sw}-{sl} ({sm:+.1f}) (+{pts})")
            elif sw <= 1 and sl >= 4:
                score -= 10
                reasons.append(f"Cold L5 {sw}-{sl} (-10)")
            elif sw >= 4 and sm >= 3.0:
                pts = _w(5, "l5_form")
                score += pts
                indep_edges += 1
                reasons.append(f"Hot L5 {sw}-{sl} (+{pts})")

        # Pillar 3B: Season Scoring / Net Differential & Lucky-Record Regression Guard
        if net_diff_info.get("edge_side") == s_abbr and net_diff_info.get("edge_pts", 0) > 0:
            pts = _w(net_diff_info["edge_pts"], "net_diff")
            score += pts
            pillars += 1
            indep_edges += 1
            reasons.append(f"Season Net-Diff Edge (+{pts})")
        if s_abbr in (net_diff_info.get("regression_sides") or set()):
            score -= 6
            reasons.append("Regression Risk Neg NetDiff (-6)")

        # Pillar 4: Sharp Line Movement & Timing (hard auto-kill on Late Chased Steam)
        if "ALREADY STEAMED" in tags_joined and f"toward {s_abbr}" in tags_joined:
            score -= 30
            is_chased_steam = True
            reasons.append("Late Chased Steam Auto-Kill (-30)")
        elif ("SPREAD STEAM: Moved" in tags_joined and f"toward {s_abbr}" in tags_joined) or (
            f"ML STEAM: Sharp action steamed {s_abbr}" in tags_joined
        ):
            pts = _w(10, "sharp_steam")
            score += pts
            pillars += 1
            indep_edges += 1
            reasons.append(f"Sharp Steam (+{pts})")
        elif ("SPREAD STEAM: Moved" in tags_joined and f"toward {o_abbr}" in tags_joined) or (
            f"ML STEAM: Sharp action steamed {o_abbr}" in tags_joined
        ):
            score -= 15
            reasons.append("Fighting Reverse Steam (-15)")

        # Pillar 5A: Schedule Fatigue (0d-Rest B2B + 3-in-4 Nights Density)
        has_b2b_signal = False
        if f"FATIGUE EDGE: {o_abbr} on" in tags_joined:
            pts = _w(12, "b2b_fatigue")
            score += pts
            pillars += 1
            indep_edges += 1
            has_b2b_signal = True
            reasons.append(f"Vs {o_abbr} 0d-Rest B2B (+{pts})")
        elif f"FATIGUE EDGE: {s_abbr} on" in tags_joined:
            score -= 15
            has_b2b_signal = True
            reasons.append("On 0d-Rest B2B (-15)")

        if not has_b2b_signal and density_info:
            s_3in4 = bool(density_info.get(s_abbr))
            o_3in4 = bool(density_info.get(o_abbr))
            if o_3in4 and not s_3in4:
                pts = _w(7, "b2b_fatigue")
                score += pts
                pillars += 1
                indep_edges += 1
                reasons.append(f"Vs {o_abbr} 3-in-4 Fatigue (+{pts})")
            elif s_3in4 and not o_3in4:
                score -= 8
                reasons.append("On 3-in-4 Fatigue (-8)")

        # Pillar 5B: NHL Goalie SV% Mismatch + MoneyPuck 5v5 xGF% & Goalie GSAx
        goalie_pts_added = 0
        if f"GOALIE MISMATCH EDGE: {s_abbr}" in tags_joined:
            pts = _w(12, "goalie_edge")
            score += pts
            goalie_pts_added += pts
            pillars += 1
            indep_edges += 1
            reasons.append(f"NHL Goalie Mismatch (+{pts})")

        if label == "NHL" and moneypuck_info:
            xgf_map = moneypuck_info.get("xgf") or {}
            s_xgf = xgf_map.get(s_abbr)
            o_xgf = xgf_map.get(o_abbr)
            if s_xgf is not None and o_xgf is not None:
                if (s_xgf - o_xgf) >= 4.5:
                    pts = _w(9, "moneypuck_xgf")
                    score += pts
                    pillars += 1
                    indep_edges += 1
                    reasons.append(f"MoneyPuck xGF% Edge ({s_xgf:.1f}% vs {o_xgf:.1f}%) (+{pts})")
                elif (s_xgf - o_xgf) <= -4.5:
                    score -= 7
                    reasons.append(f"MoneyPuck xGF% Deficit ({s_xgf:.1f}% vs {o_xgf:.1f}%) (-7)")

            gsax_map = moneypuck_info.get("gsax") or {}
            s_gsax = gsax_map.get(s_abbr)
            o_gsax = gsax_map.get(o_abbr)
            if s_gsax is not None and o_gsax is not None:
                if (s_gsax - o_gsax) >= 5.0:
                    raw_g = _w(8, "goalie_edge")
                    g_pts = max(0, min(raw_g, 16 - goalie_pts_added))
                    if g_pts > 0:
                        score += g_pts
                        if goalie_pts_added == 0:
                            pillars += 1
                            indep_edges += 1
                        goalie_pts_added += g_pts
                        reasons.append(f"Goalie GSAx Edge ({s_gsax:+.1f} vs {o_gsax:+.1f}) (+{g_pts})")
                elif (s_gsax - o_gsax) <= -5.0:
                    score -= 7
                    reasons.append(f"Goalie GSAx Deficit ({s_gsax:+.1f} vs {o_gsax:+.1f}) (-7)")

        # Pillar 5C: Home vs. Away Split Profile Edge
        if split_info.get("edge_side") == s_abbr and split_info.get("edge_pts", 0) > 0:
            pts = _w(split_info["edge_pts"], "home_road_split")
            split_lbl = "Home/Road Split" if is_home else "Road Warrior Split"
            score += pts
            pillars += 1
            indep_edges += 1
            reasons.append(f"{split_lbl} (+{pts})")
        elif split_info.get("edge_side") == o_abbr and split_info.get("opp_penalty", 0) < 0:
            pen = int(split_info["opp_penalty"])
            score += pen
            reasons.append(f"Poor Split Profile ({pen})")

        # Pillar 5D: Per-Game Key Injury Auto-Adjustment
        inj_pen = int((injury_info.get("penalties") or {}).get(s_abbr, 0))
        inj_bst = int((injury_info.get("boosts") or {}).get(s_abbr, 0))
        if inj_pen < 0:
            score += inj_pen
            reasons.append(f"Key Injury Penalty ({inj_pen})")
        if inj_bst > 0:
            score += inj_bst
            if inj_bst >= 4:
                pillars += 1
                indep_edges += 1
            reasons.append(f"Opp Key Injury Edge (+{inj_bst})")

        sw_rec, sl_rec = _parse_rec_wins_losses(s_rec)
        ow_rec, ol_rec = _parse_rec_wins_losses(o_rec)
        if is_home and sw_rec > sl_rec:
            score += 4
            reasons.append("Home Winning Rec (+4)")
        # Record superiority is a modest supporting bonus (no separate pillar increment to prevent favorite double-counting)
        if (sw_rec - sl_rec) - (ow_rec - ol_rec) >= 4:
            score += 3
            reasons.append("Record Superiority (+3)")

        # Plus-Money Underdog Penalty (unless 2+ independent signals align)
        if mkt_tier == "plus_dog" and indep_edges < 2:
            score -= 10
            reasons.append("Unconfirmed Underdog (-10)")

        # Pillar 6: Multi-Signal Confluence Bonus (requires genuinely independent signals)
        if pillars >= 3 and indep_edges >= 2:
            score += 8
            reasons.append(f"{pillars}-Pillar Confluence (+8)")
        elif pillars >= 2 and indep_edges >= 1:
            score += 4
            reasons.append("2-Pillar Confluence (+4)")

        # Guardrails: no high tier without an independent edge, and chased steam is strictly PASS
        if indep_edges == 0 and score > 75:
            score = 75
        if is_chased_steam and score > 62:
            score = 62

        if score > best_score:
            best_score = score
            best_pick_label = pick_str
            best_reasons = reasons

    final_score, bar, tier = _format_confidence_meter(best_score)
    return f"[CONFIDENCE METER: {final_score}% | {bar} | {tier} -> Top Play: {best_pick_label} ({', '.join(best_reasons)})]"


def fetch_espn_quant_matchups_and_props(return_marquee_meta=False):
    """
    100% Free Unlimited Quantitative Engine via ESPN API:
    - Live Spreads, Moneylines, Totals + Opening-to-Current Steam Moves
    - ESPN BPI / FPI Pre-Game Win Probability vs. No-Vig Market Implied Win% (+EV% & 1/4-Kelly)
    - Automated Back-to-Back (0-Days Rest) & Road Fatigue Detection
    - NHL Confirmed/Expected Starting Goalies + Season SV% & GAA
    - ESPN Core API Player Prop Steam Move Detection (Open vs. Current Prop Movement)
    - Marquee / Primetime Game Detection (National TV, AP Top-25, NFL Standalone, UCL, Soccer Big-6)
    """
    quant_games = []
    quant_props = []
    marquee_meta = {}
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    now_est = datetime.datetime.now(est_tz)
    today_ymd = now_est.strftime("%Y%m%d")
    yesterday_ymd = (now_est - datetime.timedelta(days=1)).strftime("%Y%m%d")

    all_league_configs = [
        ("EPL", "soccer", "eng.1", None),
        ("UCL", "soccer", "uefa.champions", None),
        ("LALIGA", "soccer", "esp.1", None),
        ("CFB", "football", "college-football", "80"),
        ("NFL", "football", "nfl", None),
        ("NBA", "basketball", "nba", None),
        ("NHL", "hockey", "nhl", None),
        ("NCAAM", "basketball", "mens-college-basketball", "50"),
    ]
    soccer_labels = ("EPL", "UCL", "LALIGA")

    # Auto-detect Weekend Football + European Soccer Mode (Saturday = CFB + EPL/UCL/LALIGA; Sunday = NFL + EPL/UCL/LALIGA).
    # Automatically falls back to ALL leagues as soon as CFB/NFL season ends (0 games scheduled).
    weekday = now_est.weekday()  # 5 = Saturday, 6 = Sunday
    league_configs = all_league_configs
    if weekday == 5:
        try:
            cfb_check = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
                params={"dates": today_ymd, "groups": "80"},
                timeout=10,
            ).json().get("events", [])
            if len(cfb_check) > 0:
                league_configs = [c for c in all_league_configs if c[0] in ("EPL", "UCL", "LALIGA", "CFB")]
                quant_games.append("- [SLATE MODE] SATURDAY FOOTBALL + SOCCER MODE ACTIVE (Strictly CFB & EPL/UCL/La Liga Soccer)")
        except Exception:
            pass
    elif weekday == 6:
        try:
            nfl_check = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                params={"dates": today_ymd},
                timeout=10,
            ).json().get("events", [])
            if len(nfl_check) > 0:
                league_configs = [c for c in all_league_configs if c[0] in ("EPL", "UCL", "LALIGA", "NFL")]
                quant_games.append("- [SLATE MODE] SUNDAY FOOTBALL + SOCCER MODE ACTIVE (Strictly NFL & EPL/UCL/La Liga Soccer)")
        except Exception:
            pass

    athlete_cache = {}

    # Load self-calibrated pillar weights from Shadow Grading Log (gracefully neutral 1.00x if <25 plays)
    pillar_weights, cal_note = quant_signals.load_shadow_pillar_weights()
    quant_games.append(f"- [CALIBRATION] {cal_note}")

    # Pre-fetch free external efficiency models once per scrape (fail safely to empty dicts if unreachable)
    active_labels = {c[0] for c in league_configs}
    torvik_ratings = quant_signals.fetch_barttorvik_ratings(now_est) if "NCAAM" in active_labels else {}
    moneypuck_data = quant_signals.fetch_moneypuck_nhl(now_est) if "NHL" in active_labels else {"teams": {}, "goalies": {}}

    for label, sport, league, groups in league_configs:
        base_sb = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        # 1. Identify Back-to-Back (0d rest) & 3-in-4 Nights schedule density for NBA/NHL/NCAAM
        b2b_teams = set()
        games_in_3d = {}
        if label in ("NBA", "NHL", "NCAAM"):
            try:
                b2b_teams, games_in_3d = quant_signals.fetch_recent_schedule_density(
                    base_sb, now_est, groups=groups, lookback_days=3
                )
            except Exception:
                pass

        # 2. Fetch Today's Games & Odds
        try:
            t_params = {"dates": today_ymd}
            if groups:
                t_params["groups"] = groups
            events = requests.get(base_sb, params=t_params, timeout=12).json().get("events", [])
        except Exception:
            continue

        max_events = 65 if label == "CFB" else (25 if label in ("NFL", *soccer_labels) else 20)
        sliced_events = events[:max_events]
        pre_ids = [
            str(e.get("id", ""))
            for e in sliced_events
            if e.get("id") and e.get("status", {}).get("type", {}).get("state", "") not in ("in", "post")
        ]
        summary_cache = quant_signals.prefetch_espn_summaries(sport, league, pre_ids, max_workers=8, timeout=10)

        for ev in sliced_events:
            ev_id = str(ev.get("id", ""))
            status_state = ev.get("status", {}).get("type", {}).get("state", "")
            # Skip already-finished OR already-live games so curator only picks upcoming playable slots
            if status_state in ("in", "post"):
                continue

            # Parse exact EST kickoff/tipoff time & assign Sequential Time-Slot Window
            time_est_str = "TBD EST"
            slot_tag = "[WINDOW 5: 6:30-9:00 PM PRIMETIME]"
            dt_est = None
            raw_date = ev.get("date", "")
            if raw_date:
                try:
                    dt_utc = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    dt_est = dt_utc.astimezone(est_tz)
                    time_est_str = dt_est.strftime("%I:%M %p EST").lstrip("0")
                    hr_dec = dt_est.hour + dt_est.minute / 60.0
                    if hr_dec < 9.5:
                        slot_tag = "[WINDOW 1: 7:30 AM EARLY MORNING]"
                    elif hr_dec < 11.75:
                        slot_tag = "[WINDOW 2: 10:00 AM LATE MORNING]"
                    elif hr_dec < 14.75:
                        slot_tag = "[WINDOW 3: 12:00-2:30 PM EARLY AFTERNOON]"
                    elif hr_dec < 18.25:
                        slot_tag = "[WINDOW 4: 3:00-6:00 PM LATE AFTERNOON]"
                    elif hr_dec < 21.25:
                        slot_tag = "[WINDOW 5: 6:30-9:00 PM PRIMETIME]"
                    else:
                        slot_tag = "[WINDOW 6: 9:30 PM+ LATE NIGHT]"
                except Exception:
                    pass

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
            is_marquee, mq_reason = _detect_marquee_game(label, ev, comp, home, away, dt_est)

            # Extract Overall / Home / Road records (+ Soccer 5-match form string if present)
            h_rec = next((r.get("summary", "") for r in home.get("records", []) if r.get("type") == "total"), "")
            a_rec = next((r.get("summary", "") for r in away.get("records", []) if r.get("type") == "total"), "")
            rec_str = f"({a_abbr} {a_rec} @ {h_abbr} {h_rec})" if (h_rec or a_rec) else ""

            tags = []
            odds_summary = []

            # Home vs. Away Split Profile (Zero extra requests — directly inside scoreboard records)
            split_info = quant_signals.extract_home_away_splits(home, away, h_abbr, a_abbr)
            if split_info.get("summary_str"):
                odds_summary.append(split_info["summary_str"])
            if split_info.get("tag"):
                tags.append(split_info["tag"])

            # European Soccer (EPL / UCL / La Liga) 5-match form string directly from scoreboard (e.g. "WWDLW")
            if label in soccer_labels:
                a_form = str(away.get("form", "") or "").upper()
                h_form = str(home.get("form", "") or "").upper()
                if a_form and h_form:
                    a_w = a_form.count("W")
                    h_w = h_form.count("W")
                    a_pts = a_w * 3 + a_form.count("D")
                    h_pts = h_w * 3 + h_form.count("D")
                    tags.append(f"[{label} L5 Form: {a_abbr} {a_form} ({a_pts}pts) vs {h_abbr} {h_form} ({h_pts}pts)]")
                    if (h_pts - a_pts) >= 5:
                        tags.append(f"[📈 FORM EDGE: {h_abbr} L5 {h_form} ({h_pts}pts) vs {a_abbr} L5 {a_form} ({a_pts}pts)]")
                    elif (a_pts - h_pts) >= 5:
                        tags.append(f"[📈 FORM EDGE: {a_abbr} L5 {a_form} ({a_pts}pts) vs {h_abbr} L5 {h_form} ({h_pts}pts)]")

            # Back-to-Back (0d rest) & 3-in-4 Nights Schedule Density Fatigue Flags
            a_b2b = a_name in b2b_teams
            h_b2b = h_name in b2b_teams
            density_info = {}
            if a_b2b and not h_b2b:
                tags.append(f"[⚠️ FATIGUE EDGE: {a_abbr} on Road Back-to-Back (0d rest) vs Rested {h_abbr}]")
            elif h_b2b and not a_b2b:
                tags.append(f"[⚠️ FATIGUE EDGE: {h_abbr} on Back-to-Back (0d rest) vs Rested {a_abbr}]")
            elif label in ("NBA", "NHL", "NCAAM") and games_in_3d:
                a_3in4 = games_in_3d.get(a_name, 0) >= 2
                h_3in4 = games_in_3d.get(h_name, 0) >= 2
                density_info = {a_abbr: a_3in4, h_abbr: h_3in4}
                if a_3in4 and not h_3in4:
                    tags.append(f"[⚠️ 3-IN-4 FATIGUE: {a_abbr} playing 3rd game in 4 nights vs Rested {h_abbr}]")
                elif h_3in4 and not a_3in4:
                    tags.append(f"[⚠️ 3-IN-4 FATIGUE: {h_abbr} playing 3rd game in 4 nights vs Rested {a_abbr}]")

            # Extract Odds & Opening-to-Current Steam Movement from Scoreboard
            odds_list = comp.get("odds", [])
            h_ml_close = a_ml_close = None
            h_sp_close = None
            h_sp_odds = a_sp_odds = None
            t_close = t_under_odds = None
            h_bpi = a_bpi = None
            l5_parsed = {}
            injury_info = {}
            weather_info = {}
            net_diff_info = {}
            torvik_probs = (None, None)
            moneypuck_info = {}
            if odds_list:
                od = odds_list[0]
                details = od.get("details", "")
                ou = od.get("overUnder", "")
                if details or ou:
                    odds_summary.append(f"Spread: {details} | O/U: {ou}")

                # Moneyline close vs open (plus 3-way Draw odds for Soccer)
                ml = od.get("moneyline", {})
                h_ml_close = _safe_int_odds(ml.get("home", {}).get("close", {}).get("odds"))
                a_ml_close = _safe_int_odds(ml.get("away", {}).get("close", {}).get("odds"))
                d_ml_close = _safe_int_odds(ml.get("draw", {}).get("close", {}).get("odds"))
                h_ml_open = _safe_int_odds(ml.get("home", {}).get("open", {}).get("odds"))
                if h_ml_close is not None and a_ml_close is not None:
                    draw_str = f" / Draw ({d_ml_close:+d})" if d_ml_close is not None else ""
                    odds_summary.append(f"ML: {a_abbr} ({a_ml_close:+d}) / {h_abbr} ({h_ml_close:+d}){draw_str}")
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
                    delta_sp = abs(h_sp_close - h_sp_open)
                    if delta_sp >= 2.5:
                        tags.append(
                            f"[⚠️ SPREAD ALREADY STEAMED ({delta_sp:.1f} pts toward {moved_toward}: {h_sp_open:+.1f} -> {h_sp_close:+.1f}) — DO NOT CHASE LATE]"
                        )
                    else:
                        tags.append(
                            f"[🔥 SPREAD STEAM: Moved {delta_sp:.1f} pts toward {moved_toward} (Open {h_abbr} {h_sp_open:+.1f} -> Now {h_sp_close:+.1f})]"
                        )

                # Total close vs open (include Over/Under juice for European soccer totals like o2.5 / u2.5)
                tot = od.get("total", {})
                t_close = _safe_float_line(tot.get("over", {}).get("close", {}).get("line"))
                t_open = _safe_float_line(tot.get("over", {}).get("open", {}).get("line"))
                t_over_odds = _safe_int_odds(tot.get("over", {}).get("close", {}).get("odds"))
                t_under_odds = _safe_int_odds(tot.get("under", {}).get("close", {}).get("odds"))
                if t_close is not None and t_over_odds is not None and t_under_odds is not None and label in soccer_labels:
                    odds_summary.append(f"Total Prices: O{t_close} ({t_over_odds:+d}) / U{t_close} ({t_under_odds:+d})")
                if t_open is not None and t_close is not None and abs(t_close - t_open) >= 1.0:
                    dir_lbl = "OVER" if t_close > t_open else "UNDER"
                    delta_t = abs(t_close - t_open)
                    if delta_t >= 3.5:
                        tags.append(
                            f"[⚠️ TOTAL ALREADY STEAMED ({delta_t:.1f} pts toward {dir_lbl}: {t_open} -> {t_close}) — DO NOT CHASE LATE]"
                        )
                    else:
                        tags.append(f"[🔥 TOTAL STEAM: Moved {delta_t:.1f} pts toward {dir_lbl} (Open {t_open} -> Now {t_close})]")

            # Bart Torvik NCAAM Adjusted Efficiency Win Probabilities (Zero per-game network calls!)
            if label == "NCAAM" and torvik_ratings:
                a_loc = away.get("team", {}).get("location", "")
                h_loc = home.get("team", {}).get("location", "")
                a_tr = quant_signals.lookup_torvik_team(torvik_ratings, a_name, a_loc)
                h_tr = quant_signals.lookup_torvik_team(torvik_ratings, h_name, h_loc)
                a_tp, h_tp = quant_signals.compute_torvik_matchup_probs(
                    a_tr, h_tr, is_neutral=bool(comp.get("neutralSite", False))
                )
                if a_tp is not None and h_tp is not None:
                    torvik_probs = (a_tp, h_tp)
                    odds_summary.append(
                        f"Torvik AdjEff: {a_abbr} {a_tp:.1f}% (OE {a_tr['adjoe']:.1f}/DE {a_tr['adjde']:.1f}) vs {h_abbr} {h_tp:.1f}% (OE {h_tr['adjoe']:.1f}/DE {h_tr['adjde']:.1f})"
                    )
                    if h_ml_close is not None and a_ml_close is not None:
                        imp_h = american_to_implied(h_ml_close)
                        imp_a = american_to_implied(a_ml_close)
                        if (imp_h + imp_a) > 0:
                            mkt_h = power_devig_prob(imp_h, imp_a) * 100.0
                            mkt_a = power_devig_prob(imp_a, imp_h) * 100.0
                            if (h_tp - mkt_h) >= 3.5 and -155 <= h_ml_close <= 120:
                                ev_h = round(((h_tp / 100.0) * american_to_decimal(h_ml_close) - 1.0) * 100.0, 2)
                                if ev_h >= 2.5:
                                    tags.append(f"[📊 TORVIK +EV: {h_abbr} ML ({h_ml_close:+d}) | Torvik {h_tp:.1f}% vs Mkt {mkt_h:.1f}% | EV +{ev_h}%]")
                            elif (a_tp - mkt_a) >= 3.5 and -155 <= a_ml_close <= 120:
                                ev_a = round(((a_tp / 100.0) * american_to_decimal(a_ml_close) - 1.0) * 100.0, 2)
                                if ev_a >= 2.5:
                                    tags.append(f"[📊 TORVIK +EV: {a_abbr} ML ({a_ml_close:+d}) | Torvik {a_tp:.1f}% vs Mkt {mkt_a:.1f}% | EV +{ev_a}%]")

            # Fetch Event Summary for ESPN BPI/FPI Predictor, Injuries, Weather, Season NetDiff, L5 Form & NHL Goalies
            try:
                sum_data = summary_cache.get(ev_id)
                if sum_data is None:
                    sum_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={ev_id}"
                    sum_data = requests.get(sum_url, timeout=10).json()

                # Per-Game Key Injury Auto-Adjustment (Zero extra calls)
                injury_info = quant_signals.extract_game_injury_impact(sum_data, label, a_abbr, h_abbr, a_name, h_name)
                for itag in injury_info.get("tags") or []:
                    tags.append(itag)

                # Outdoor Weather Impact for NFL & CFB (Zero extra calls)
                weather_info = quant_signals.extract_game_weather(sum_data, label)
                if weather_info.get("tag"):
                    tags.append(weather_info["tag"])

                # Season Scoring / Net Differential & Lucky-Record Regression Guard (Zero extra calls)
                net_diff_info = quant_signals.extract_season_net_differential(
                    sum_data, home, away, label, a_abbr, h_abbr, a_rec, h_rec
                )
                if net_diff_info.get("summary_str"):
                    odds_summary.append(net_diff_info["summary_str"])
                for ntag in net_diff_info.get("tags") or []:
                    tags.append(ntag)

                # ESPN BPI / FPI Model Win Probability vs Power-Devigged Market Implied Probability
                # Restrict BPI/FPI +EV tags to high-information leagues (NBA, NFL, CFB — exclude noisy early-season mid-major CBB)
                # and High-Probability Moderate Favorite / Coin-Flip Sweet Spot (-150 to +110) without fighting reverse steam
                pred = sum_data.get("predictor", {})
                h_proj = pred.get("homeTeam", {}).get("gameProjection")
                a_proj = pred.get("awayTeam", {}).get("gameProjection")
                if h_proj is not None and a_proj is not None:
                    h_bpi = float(h_proj)
                    a_bpi = float(a_proj)
                    odds_summary.append(f"ESPN BPI/FPI WinProb: {a_abbr} {a_bpi:.1f}% / {h_abbr} {h_bpi:.1f}%")
                    if label in ("NBA", "NFL", "CFB") and h_ml_close is not None and a_ml_close is not None:
                        imp_h = american_to_implied(h_ml_close)
                        imp_a = american_to_implied(a_ml_close)
                        if (imp_h + imp_a) > 0:
                            mkt_h = power_devig_prob(imp_h, imp_a) * 100.0
                            mkt_a = power_devig_prob(imp_a, imp_h) * 100.0
                            steam_str = " ".join(tags)
                            if (
                                3.5 <= (h_bpi - mkt_h) <= 18.0
                                and -150 <= h_ml_close <= 110
                                and f"toward {a_abbr}" not in steam_str
                            ):
                                dec_h = american_to_decimal(h_ml_close)
                                ev_h = round(((h_bpi / 100.0) * dec_h - 1.0) * 100.0, 2)
                                if ev_h >= 2.5:
                                    tags.append(
                                        f"[📊 BPI/FPI +EV (Sweet-Spot): {h_abbr} ML ({h_ml_close:+d}) | BPI Win: {h_bpi:.1f}% vs PowerNoVig: {mkt_h:.1f}% | EV: +{ev_h}%]"
                                    )
                            elif (
                                3.5 <= (a_bpi - mkt_a) <= 18.0
                                and -150 <= a_ml_close <= 110
                                and f"toward {h_abbr}" not in steam_str
                            ):
                                dec_a = american_to_decimal(a_ml_close)
                                ev_a = round(((a_bpi / 100.0) * dec_a - 1.0) * 100.0, 2)
                                if ev_a >= 2.5:
                                    tags.append(
                                        f"[📊 BPI/FPI +EV (Sweet-Spot): {a_abbr} ML ({a_ml_close:+d}) | BPI Win: {a_bpi:.1f}% vs PowerNoVig: {mkt_a:.1f}% | EV: +{ev_a}%]"
                                    )

                # Last-5-Games Form & Scoring Margin Extractor (Free inside ESPN summary!)
                l5_blocks = sum_data.get("lastFiveGames", [])
                l5_parsed = {}
                for tb in l5_blocks:
                    t_info = tb.get("team", {})
                    t_abbr = t_info.get("abbreviation", "")
                    t_id = str(t_info.get("id", ""))
                    evts_5 = tb.get("events", [])[:5]
                    if not t_abbr or not evts_5:
                        continue
                    w5 = 0
                    l5 = 0
                    margins = []
                    for g5 in evts_5:
                        gr = str(g5.get("gameResult", "")).upper()
                        if gr == "W":
                            w5 += 1
                        elif gr == "L":
                            l5 += 1
                        try:
                            hs = int(g5.get("homeTeamScore", 0) or 0)
                            as_ = int(g5.get("awayTeamScore", 0) or 0)
                            if str(g5.get("homeTeamId", "")) == t_id:
                                margins.append(hs - as_)
                            else:
                                margins.append(as_ - hs)
                        except Exception:
                            pass
                    avg_m = round(sum(margins) / len(margins), 1) if margins else 0.0
                    l5_parsed[t_abbr] = (w5, l5, avg_m)
                if len(l5_parsed) == 2 and a_abbr in l5_parsed and h_abbr in l5_parsed:
                    aw, al, am = l5_parsed[a_abbr]
                    hw, hl, hm = l5_parsed[h_abbr]
                    odds_summary.append(f"L5 Form: {a_abbr} {aw}-{al} ({am:+.1f}) vs {h_abbr} {hw}-{hl} ({hm:+.1f})")
                    margin_thresh = 0.8 if label in soccer_labels else (1.2 if label == "NHL" else 4.5)
                    if (hw - aw) >= 2 and (hm - am) >= margin_thresh:
                        tags.append(f"[📈 FORM EDGE: {h_abbr} L5 {hw}-{hl} ({hm:+.1f} avg margin) vs {a_abbr} L5 {aw}-{al} ({am:+.1f})]")
                    elif (aw - hw) >= 2 and (am - hm) >= margin_thresh:
                        tags.append(f"[📈 FORM EDGE: {a_abbr} L5 {aw}-{al} ({am:+.1f} avg margin) vs {h_abbr} L5 {hw}-{hl} ({hm:+.1f})]")

                # NHL Probable / Confirmed Starting Goalies + Season SV% & GAA + MoneyPuck 5v5 xGF% & Goalie GSAx
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
                    parsed_goalies = {}
                    probable_goalie_names = {}
                    for side_comp, side_abbr, side_ml in ((away, a_abbr, a_ml_close), (home, h_abbr, h_ml_close)):
                        probs = side_comp.get("probables", [])
                        if probs:
                            p0 = probs[0]
                            pid = str(p0.get("playerId", ""))
                            p_name = p0.get("athlete", {}).get("displayName", "Unknown")
                            p_status = p0.get("status", {}).get("name", "Probable")
                            probable_goalie_names[side_abbr] = p_name
                            _, sv_pct, gaa = goalie_stats_lookup.get(pid, (p_name, "N/A", "N/A"))
                            goalie_notes.append(f"{side_abbr}: {p_name} ({p_status}, {sv_pct} SV%, {gaa} GAA)")
                            try:
                                sv_f = float(sv_pct) if sv_pct != "N/A" else None
                                gaa_f = float(gaa) if gaa != "N/A" else None
                                if sv_f and gaa_f:
                                    parsed_goalies[side_abbr] = (p_name, sv_f, gaa_f, side_ml)
                            except Exception:
                                pass
                    if goalie_notes:
                        tags.append(f"[🥅 NHL GOALIES: {' vs '.join(goalie_notes)}]")
                    if len(parsed_goalies) == 2:
                        a_g, h_g = parsed_goalies.get(a_abbr), parsed_goalies.get(h_abbr)
                        if a_g and h_g:
                            if (a_g[1] - h_g[1]) >= 0.025 and a_g[3] is not None and -160 <= a_g[3] <= 135:
                                tags.append(
                                    f"[🎯 GOALIE MISMATCH EDGE: {a_abbr} ML ({a_g[3]:+d}) — {a_g[0]} ({a_g[1]:.3f} SV%, {a_g[2]:.2f} GAA) vs {h_g[0]} ({h_g[1]:.3f} SV%, {h_g[2]:.2f} GAA)]"
                                )
                            elif (h_g[1] - a_g[1]) >= 0.025 and h_g[3] is not None and -160 <= h_g[3] <= 135:
                                tags.append(
                                    f"[🎯 GOALIE MISMATCH EDGE: {h_abbr} ML ({h_g[3]:+d}) — {h_g[0]} ({h_g[1]:.3f} SV%, {h_g[2]:.2f} GAA) vs {a_g[0]} ({a_g[1]:.3f} SV%, {a_g[2]:.2f} GAA)]"
                                )

                    # MoneyPuck 5v5 xGF% + Goalie GSAx overlay
                    mp_teams = moneypuck_data.get("teams") or {}
                    mp_goalies = moneypuck_data.get("goalies") or {}
                    xgf_pair = {}
                    gsax_pair = {}
                    a_mp_t = mp_teams.get(str(a_abbr).upper())
                    h_mp_t = mp_teams.get(str(h_abbr).upper())
                    if a_mp_t and h_mp_t:
                        xgf_pair[a_abbr] = a_mp_t["xgf_pct"]
                        xgf_pair[h_abbr] = h_mp_t["xgf_pct"]
                        odds_summary.append(f"MoneyPuck 5v5 xGF%: {a_abbr} {a_mp_t['xgf_pct']:.1f}% vs {h_abbr} {h_mp_t['xgf_pct']:.1f}%")
                        if (h_mp_t["xgf_pct"] - a_mp_t["xgf_pct"]) >= 4.5:
                            tags.append(f"[🏒 MONEYPUCK xGF% EDGE: {h_abbr} {h_mp_t['xgf_pct']:.1f}% vs {a_abbr} {a_mp_t['xgf_pct']:.1f}%]")
                        elif (a_mp_t["xgf_pct"] - h_mp_t["xgf_pct"]) >= 4.5:
                            tags.append(f"[🏒 MONEYPUCK xGF% EDGE: {a_abbr} {a_mp_t['xgf_pct']:.1f}% vs {h_abbr} {h_mp_t['xgf_pct']:.1f}%]")
                    if a_abbr in probable_goalie_names and h_abbr in probable_goalie_names:
                        a_mg = mp_goalies.get(quant_signals._norm_exact(probable_goalie_names[a_abbr]))
                        h_mg = mp_goalies.get(quant_signals._norm_exact(probable_goalie_names[h_abbr]))
                        if a_mg and h_mg:
                            gsax_pair[a_abbr] = a_mg["gsax"]
                            gsax_pair[h_abbr] = h_mg["gsax"]
                            odds_summary.append(f"Goalie GSAx: {a_abbr} {a_mg['gsax']:+.1f} vs {h_abbr} {h_mg['gsax']:+.1f}")
                            if (h_mg["gsax"] - a_mg["gsax"]) >= 5.0:
                                tags.append(f"[🥅 GOALIE GSAx EDGE: {h_abbr} {h_mg['name']} ({h_mg['gsax']:+.1f}) vs {a_abbr} {a_mg['name']} ({a_mg['gsax']:+.1f})]")
                            elif (a_mg["gsax"] - h_mg["gsax"]) >= 5.0:
                                tags.append(f"[🥅 GOALIE GSAx EDGE: {a_abbr} {a_mg['name']} ({a_mg['gsax']:+.1f}) vs {h_abbr} {h_mg['name']} ({h_mg['gsax']:+.1f})]")
                    if xgf_pair or gsax_pair:
                        moneypuck_info = {"xgf": xgf_pair, "gsax": gsax_pair}

                # Multi-Signal Confluence Detector (2+ independent signals on the same game, excluding DO NOT CHASE warnings)
                edge_signals = [
                    t
                    for t in tags
                    if any(
                        k in t
                        for k in (
                            "FATIGUE EDGE",
                            "3-IN-4 FATIGUE",
                            "FORM EDGE",
                            "🔥 SPREAD STEAM",
                            "🔥 ML STEAM",
                            "BPI/FPI +EV",
                            "TORVIK +EV",
                            "GOALIE MISMATCH",
                            "GOALIE GSAx",
                            "MONEYPUCK xGF%",
                            "HOME/ROAD SPLIT",
                            "ROAD WARRIOR",
                            "INJURY IMPACT",
                            "NET-DIFF EDGE",
                        )
                    )
                ]
                if len(edge_signals) >= 2:
                    tags.append(f"[🏆 A+ CONFLUENCE ({len(edge_signals)} Signals Aligned)]")
            except Exception:
                pass

            # Compute Deterministic 0-99 Confidence Meter across Core + Multi-Model Quantitative Pillars
            extra_signals = {
                "split_info": split_info,
                "injury_info": injury_info,
                "weather_info": weather_info,
                "net_diff_info": net_diff_info,
                "torvik_probs": torvik_probs,
                "moneypuck_info": moneypuck_info,
                "density_info": density_info,
            }
            conf_tag = ""
            try:
                conf_tag = _compute_matchup_confidence(
                    label=label,
                    a_abbr=a_abbr,
                    h_abbr=h_abbr,
                    a_rec=a_rec,
                    h_rec=h_rec,
                    a_ml=a_ml_close,
                    h_ml=h_ml_close,
                    h_sp=h_sp_close,
                    a_sp_odds=a_sp_odds,
                    h_sp_odds=h_sp_odds,
                    a_bpi=a_bpi,
                    h_bpi=h_bpi,
                    l5_parsed=l5_parsed,
                    tags=tags,
                    extra_signals=extra_signals,
                    pillar_weights=pillar_weights,
                )
                tags.append(conf_tag)
            except Exception:
                pass

            if is_marquee:
                tags.insert(0, f"[MARQUEE: {mq_reason}]")
                mq_key = f"{label}:{matchup}"
                mq_entry = {
                    "label": label,
                    "matchup": matchup,
                    "a_name": a_name,
                    "h_name": h_name,
                    "a_abbr": a_abbr,
                    "h_abbr": h_abbr,
                    "slot_tag": slot_tag,
                    "time_est": time_est_str,
                    "reason": mq_reason,
                    "rec_str": rec_str,
                    "odds_summary": " | ".join(odds_summary),
                    "weather": weather_info,
                    "candidates": [],
                }
                tags_blob = " ".join(tags)
                if conf_tag and "DO NOT CHASE LATE" not in tags_blob:
                    m_fg = re.search(
                        r"\[CONFIDENCE METER:\s*(\d{2,3})%\s*\|[^|]+\|\s*([^->]+?)\s*->\s*Top Play:\s*([^\]]+)\]",
                        conf_tag,
                    )
                    if m_fg:
                        fg_score = int(m_fg.group(1))
                        fg_top = m_fg.group(3).strip()
                        fg_pick = fg_top
                        fg_reasons = ""
                        m_pr = re.match(r"^(.+?\([+-]\d{3,4}\))\s*\((.+)\)$", fg_top)
                        if m_pr:
                            fg_pick = m_pr.group(1).strip()
                            fg_reasons = m_pr.group(2).strip()
                        # Expand team abbreviation prefix to full team name for crisp marquee pick display
                        if a_abbr and re.match(rf"^{re.escape(a_abbr)}\b", fg_pick):
                            fg_pick = re.sub(rf"^{re.escape(a_abbr)}\b", a_name, fg_pick, count=1)
                        elif h_abbr and re.match(rf"^{re.escape(h_abbr)}\b", fg_pick):
                            fg_pick = re.sub(rf"^{re.escape(h_abbr)}\b", h_name, fg_pick, count=1)
                        m_fo = re.search(r"\(([+-]\d{3,4})\)", fg_pick)
                        fg_odds = int(m_fo.group(1)) if m_fo else None
                        if fg_odds is not None and -155 <= fg_odds <= 130 and "EXCEEDS" not in fg_pick:
                            mq_entry["candidates"].append({
                                "score": fg_score,
                                "pick": fg_pick,
                                "market": "Full Game",
                                "reasons": fg_reasons,
                            })

                # Severe Outdoor Weather + Non-Chased UNDER Candidate for Marquee NFL/CFB
                if (
                    label in ("NFL", "CFB")
                    and weather_info.get("severe")
                    and t_close is not None
                    and t_under_odds is not None
                    and -155 <= t_under_odds <= 130
                    and "TOTAL ALREADY STEAMED" not in tags_blob
                ):
                    w_score = 54
                    w_reasons = []
                    w_mph = float(weather_info.get("wind_mph") or 0.0)
                    if w_mph >= 18.0:
                        w_score += 14
                        w_reasons.append(f"Severe Wind {int(round(w_mph))}mph (+14)")
                    elif w_mph >= 15.0:
                        w_score += 10
                        w_reasons.append(f"High Wind {int(round(w_mph))}mph (+10)")
                    else:
                        w_score += 8
                        w_reasons.append("Severe Outdoor Weather (+8)")
                    if weather_info.get("is_precip"):
                        w_score += 6
                        w_reasons.append("Rain/Snow Precip (+6)")
                    if "TOTAL STEAM: Moved" in tags_blob and "toward UNDER" in tags_blob:
                        w_score += 10
                        w_reasons.append("Sharp Under Steam (+10)")
                    if -145 <= t_under_odds <= 110:
                        w_score += 6
                        w_reasons.append(f"Odds Sweet-Spot {t_under_odds:+d} (+6)")
                    else:
                        w_score += 3
                        w_reasons.append(f"Playable Odds {t_under_odds:+d} (+3)")
                    mq_entry["candidates"].append({
                        "score": min(96, w_score),
                        "pick": f"Under {t_close} ({t_under_odds:+d})",
                        "market": "Weather Total",
                        "reasons": ", ".join(w_reasons),
                    })

                marquee_meta[mq_key] = mq_entry

            if odds_summary or tags:
                quant_games.append(
                    f"- [{label} QUANT] {slot_tag} ({time_est_str}) {matchup} {rec_str} -> {' | '.join(odds_summary)}"
                    + ((" -> " + " ".join(tags)) if tags else "")
                )

            # 3. Scan ESPN Core API Player Props for Significant Line/Juice Steam Moves (NBA, NHL, and Marquee NFL)
            if (label in ("NBA", "NHL") or (label == "NFL" and is_marquee)) and ev_id:
                try:
                    pb_url = f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{ev_id}/competitions/{ev_id}/odds/100/propBets?lang=en&region=us&limit=120"
                    pb_items = requests.get(pb_url, timeout=10).json().get("items", [])
                    steamed_candidates = []
                    allowed_espn_props = {
                        "Total Points",
                        "Total Rebounds",
                        "Total Assists",
                        "Total 3-Point Field Goals",
                        "Total Pts, Reb, & Ast",
                        "Total Shots on Goal",
                        "Total Passing Yards",
                        "Total Rushing Yards",
                        "Total Receiving Yards",
                        "Total Receptions",
                        "Total Passing Touchdowns",
                    }
                    for item in pb_items:
                        prop_name = item.get("type", {}).get("name", "")
                        if prop_name not in allowed_espn_props:
                            continue
                        cur_line = _safe_float_line(item.get("current", {}).get("target", {}).get("value"))
                        open_line = _safe_float_line(item.get("open", {}).get("target", {}).get("value"))
                        cur_odds = _safe_int_odds(item.get("odds", {}).get("american", {}).get("value"))
                        open_odds = _safe_int_odds(item.get("odds", {}).get("american", {}).get("open"))
                        ath_ref = item.get("athlete", {}).get("$ref", "")
                        if cur_line is None or cur_odds is None or cur_odds < -155 or not ath_ref:
                            continue

                        # Only flag JUICE-ONLY steam where the target number did NOT jump a full point (prevents chasing inflated numbers like 4.5 -> 5.5!)
                        line_delta = (cur_line - open_line) if open_line is not None else 0.0
                        imp_shift = (
                            (american_to_implied(cur_odds) - american_to_implied(open_odds)) * 100.0
                            if open_odds is not None
                            else 0.0
                        )
                        if line_delta == 0.0 and imp_shift >= 4.0 and -150 <= cur_odds <= 115:
                            steamed_candidates.append(
                                (imp_shift, ath_ref, prop_name, open_line, cur_line, open_odds, cur_odds)
                            )

                    steamed_candidates.sort(key=lambda x: x[0], reverse=True)
                    for imp_s, ath_ref, prop_name, open_line, cur_line, open_odds, cur_odds in steamed_candidates[:4]:
                        if ath_ref not in athlete_cache:
                            try:
                                ath_info = requests.get(ath_ref.replace("http://", "https://"), timeout=6).json()
                                athlete_cache[ath_ref] = ath_info.get("displayName", "Player")
                            except Exception:
                                athlete_cache[ath_ref] = "Player"
                        p_name = athlete_cache[ath_ref]
                        if p_name == "Player":
                            continue
                        if open_line is not None and cur_line != open_line:
                            dir_side = "Over" if cur_line > open_line else "Under"
                        else:
                            dir_side = "Over" if cur_odds < (open_odds or cur_odds) else "Under"
                        p_score, p_reasons = _score_market_candidate_confidence(
                            ev_pct=0.0,
                            has_line_edge=False,
                            stale_book_count=0,
                            is_pinnacle=False,
                            n_sharp_books=1,
                            hr_price=cur_odds,
                            steam_shift=imp_s,
                        )
                        if p_score <= 0:
                            continue
                        if (
                            label == "NFL"
                            and weather_info.get("severe")
                            and prop_name in ("Total Passing Yards", "Total Passing Touchdowns")
                        ):
                            if dir_side == "Over":
                                p_score = max(25, p_score - 8)
                                p_reasons.append("Severe Weather Wind/Precip (-8)")
                            elif dir_side == "Under":
                                p_score = min(96, p_score + 6)
                                p_reasons.append("Severe Weather Wind/Precip (+6)")
                        ps_val, ps_bar, ps_tier = _format_confidence_meter(p_score)
                        quant_props.append(
                            f"- [{label} PROP STEAM] {slot_tag} ({time_est_str}) {p_name} ({a_abbr} @ {h_abbr}) {prop_name} {dir_side} {cur_line} ({cur_odds:+d}) "
                            f"[Steamed from Open {open_line} ({open_odds:+d}) -> Now {cur_line} ({cur_odds:+d})] "
                            f"[CONFIDENCE METER: {ps_val}% | {ps_bar} | {ps_tier}]"
                        )
                        if is_marquee:
                            mq_key = f"{label}:{matchup}"
                            if mq_key in marquee_meta:
                                clean_pn = prop_name.replace("Total ", "")
                                marquee_meta[mq_key]["candidates"].append({
                                    "score": ps_val,
                                    "pick": f"{p_name} {dir_side} {cur_line} {clean_pn} ({cur_odds:+d})",
                                    "market": "Player Prop Steam",
                                    "reasons": ", ".join(p_reasons) + f", Open {open_line} ({open_odds:+d}) -> {cur_line} ({cur_odds:+d})",
                                })
                except Exception:
                    pass

    if return_marquee_meta:
        return quant_games, quant_props, marquee_meta
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
