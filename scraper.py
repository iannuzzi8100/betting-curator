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

# Markets with 1st-Half lines enabled for NBA & CBB (softer than full-game lines)
MARKETS_BY_SPORT = {
    "basketball_nba": "h2h,spreads,totals,spreads_h1,totals_h1",
    "basketball_ncaab": "h2h,spreads,totals,spreads_h1,totals_h1",
    "icehockey_nhl": "h2h,spreads,totals",
    "americanfootball_ncaaf": "h2h,spreads,totals,spreads_h1",
    "americanfootball_nfl": "h2h,spreads,totals,spreads_h1",
    "soccer_epl": "h2h,totals",
    "soccer_uefa_champs_league": "h2h,totals",
    "tennis_atp_finals": "h2h",
    "tennis_wta_finals": "h2h",
}

PROP_MARKETS_BY_SPORT = {
    "basketball_nba": "player_points,player_rebounds,player_assists,player_points_rebounds_assists",
    "icehockey_nhl": "player_points,player_shots_on_goal",
}

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
    {"name": "Circles Off", "url": "https://www.youtube.com/@CirclesOff/videos"},
    {"name": "Bet the Process", "url": "https://www.youtube.com/@BetTheProcess/videos"},
    {"name": "Deep Dive Podcast", "url": "https://www.youtube.com/@TheDeepDivePodcast/videos"},
    {"name": "Shane Huang", "url": "https://www.youtube.com/@ShaneHuang/videos"},
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
    if not ODDS_API_KEY:
        return ["[WARNING] ODDS_API_KEY not configured."], ["- No prop data retrieved."]

    game_lines = []
    prop_lines = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc + datetime.timedelta(hours=30)

    for sport_key in SPORTS_ODDS_KEYS:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us,eu",
            "markets": MARKETS_BY_SPORT.get(sport_key, "h2h,spreads,totals"),
            "oddsFormat": "american",
            "bookmakers": "hardrockbet,pinnacle,draftkings",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            games = resp.json()
            todays_event_ids = []

            for g in games:
                home = g.get("home_team")
                away = g.get("away_team")
                commence = g.get("commence_time", "")
                event_id = g.get("id")

                hr_book = next((b for b in g.get("bookmakers", []) if b["key"] == "hardrockbet"), None)
                # Prefer Pinnacle as sharpest global book, fallback to DraftKings
                sharp_book = next((b for b in g.get("bookmakers", []) if b["key"] == "pinnacle"), None) or next(
                    (b for b in g.get("bookmakers", []) if b["key"] == "draftkings"), None
                )
                if not hr_book:
                    continue

                if commence and event_id:
                    try:
                        c_dt = datetime.datetime.fromisoformat(commence.replace("Z", "+00:00"))
                        if now_utc <= c_dt <= cutoff_utc:
                            todays_event_ids.append((event_id, f"{away} @ {home}"))
                    except Exception:
                        pass

                # Build sharp lookup for exact No-Vig devigging
                sharp_markets = {}
                if sharp_book:
                    for m in sharp_book.get("markets", []):
                        sharp_markets[m["key"]] = m.get("outcomes", [])

                hr_summary = []
                ev_tags = []
                for m in hr_book.get("markets", []):
                    m_key = m["key"]
                    outcomes_str = []
                    s_outs = sharp_markets.get(m_key, [])

                    for o in m.get("outcomes", []):
                        o_name = o["name"]
                        o_pt = o.get("point")
                        o_price = o["price"]
                        outcomes_str.append(f"{o_name} {o_pt if o_pt is not None else ''} ({o_price:+d})".strip())

                        # Compute exact No-Vig EV if sharp book has matching 2-way line
                        if len(s_outs) == 2:
                            s_match = next((x for x in s_outs if x["name"] == o_name and x.get("point") == o_pt), None)
                            s_other = next((x for x in s_outs if x["name"] != o_name), None)
                            if s_match and s_other:
                                fair_p, ev_pct, q_kelly = compute_novig_ev(o_price, s_match["price"], s_other["price"])
                                if ev_pct >= 1.5:
                                    ev_tags.append(
                                        f"[+EV: {m_key.upper()} {o_name} {o_pt if o_pt is not None else ''} @ {o_price:+d} | "
                                        f"FairWin: {fair_p}% | EV: +{ev_pct}% | 1/4-Kelly: {q_kelly}%]"
                                    )

                    hr_summary.append(f"{m_key.upper()}: [{', '.join(outcomes_str)}]")

                ev_banner = (" -> " + " ".join(ev_tags)) if ev_tags else ""
                sharp_label = sharp_book["key"] if sharp_book else "none"
                game_lines.append(
                    f"- [{sport_key}] {away} @ {home} ({commence}) [SharpRef: {sharp_label}] -> {' ; '.join(hr_summary)}{ev_banner}"
                )

            # Fetch Player Props for up to 5 today-slate games in NBA & NHL
            if sport_key in PROP_MARKETS_BY_SPORT and todays_event_ids:
                for ev_id, matchup in todays_event_ids[:5]:
                    ev_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{ev_id}/odds"
                    ev_params = {
                        "apiKey": ODDS_API_KEY,
                        "regions": "us,eu",
                        "markets": PROP_MARKETS_BY_SPORT[sport_key],
                        "oddsFormat": "american",
                        "bookmakers": "hardrockbet,pinnacle,draftkings",
                    }
                    try:
                        p_resp = requests.get(ev_url, params=ev_params, timeout=15)
                        if p_resp.status_code != 200:
                            continue
                        p_data = p_resp.json()
                        hr_p = next((b for b in p_data.get("bookmakers", []) if b["key"] == "hardrockbet"), None)
                        ref_p = next((b for b in p_data.get("bookmakers", []) if b["key"] == "pinnacle"), None) or next(
                            (b for b in p_data.get("bookmakers", []) if b["key"] == "draftkings"), None
                        )
                        if not hr_p or not ref_p:
                            continue

                        ref_lookup = {}
                        for m in ref_p.get("markets", []):
                            for o in m.get("outcomes", []):
                                key = (m["key"], o.get("description", ""), o.get("name", ""))
                                ref_lookup[key] = (o.get("point"), o.get("price"))

                        for m in hr_p.get("markets", []):
                            for o in m.get("outcomes", []):
                                player = o.get("description", "")
                                side = o.get("name", "")
                                opp_side = "Under" if side == "Over" else "Over"
                                hr_pt = o.get("point")
                                hr_price = o.get("price")
                                ref_val = ref_lookup.get((m["key"], player, side))
                                ref_opp = ref_lookup.get((m["key"], player, opp_side))
                                if not ref_val or hr_pt is None:
                                    continue
                                ref_pt, ref_price = ref_val

                                has_line_edge = (side == "Over" and hr_pt < ref_pt) or (side == "Under" and hr_pt > ref_pt)
                                ev_str = ""
                                if hr_pt == ref_pt and ref_opp and ref_opp[0] == hr_pt:
                                    fair_p, ev_pct, q_kelly = compute_novig_ev(hr_price, ref_price, ref_opp[1])
                                    if ev_pct >= 2.0:
                                        ev_str = f" | FairWin: {fair_p}% | EV: +{ev_pct}% | 1/4-Kelly: {q_kelly}%"

                                if has_line_edge or ev_str:
                                    prop_lines.append(
                                        f"- [{sport_key} PROP] {player} ({matchup}) {m['key']} {side} {hr_pt} ({hr_price:+d}) "
                                        f"vs {ref_p['key']} {ref_pt} ({ref_price:+d}){ev_str}"
                                    )
                    except Exception:
                        continue
        except Exception as exc:
            game_lines.append(f"- [{sport_key}] Odds fetch error: {exc}")

    return (
        game_lines or ["- No Hard Rock Bet lines active for target sports."],
        prop_lines or ["- No positive-EV player prop discrepancies detected on today's slate."],
    )


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
                        condensed = re.sub(r"\s+", " ", full_text)[:3500]
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
    full_text = ""

    for elem in content:
        para = elem.get("paragraph")
        if not para:
            continue
        text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
        full_text += text

    requests_batch = []
    date_headers = list(re.finditer(r"(\d{4}-\d{2}-\d{2}) Slate Intel", full_text))
    for match in date_headers:
        sec_date = match.group(1)
        if sec_date < cutoff_date:
            sec_start = None
            sec_end = content[-1].get("endIndex", 1) - 1
            for elem in content:
                para = elem.get("paragraph")
                if not para:
                    continue
                t = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
                if f"{sec_date} Slate Intel" in t:
                    sec_start = elem.get("startIndex")
                elif sec_start and re.search(r"\d{4}-\d{2}-\d{2} Slate Intel", t):
                    sec_end = elem.get("startIndex")
                    break
            if sec_start and sec_end > sec_start:
                requests_batch.append({
                    "deleteContentRange": {
                        "range": {"startIndex": sec_start, "endIndex": sec_end}
                    }
                })

    if requests_batch:
        requests_batch.reverse()
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
