import os
import re
import json
import datetime
import requests
import feedparser
from dateutil import parser as date_parser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

# Configuration from Environment / GitHub Secrets
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID", "")
GCP_SA_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")

SPORTS_ODDS_KEYS = [
    "basketball_nba",
    "icehockey_nhl",
    "basketball_ncaab",
    "americanfootball_ncaaf",
    "americanfootball_nfl",
]

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

# 14 Target YouTube Channels (Name -> Search/Handle URL for yt-dlp recent upload lookup)
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
    {"name": "Late Kick with Josh Pate", "url": "https://www.youtube.com/@LateKickJosh/videos"},
    {"name": "Land Your Bets", "url": "https://www.youtube.com/@LandYourBets/videos"},
]


def get_docs_service():
    creds_dict = json.loads(GCP_SA_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/documents"]
    )
    return build("docs", "v1", credentials=creds)


def fetch_hardrock_odds():
    if not ODDS_API_KEY:
        return ["[WARNING] ODDS_API_KEY not configured."]
    lines = []
    for sport_key in SPORTS_ODDS_KEYS:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
            "bookmakers": "hardrockbet,pinnacle,draftkings",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            games = resp.json()
            for g in games:
                home = g.get("home_team")
                away = g.get("away_team")
                commence = g.get("commence_time", "")
                hr_book = next((b for b in g.get("bookmakers", []) if b["key"] == "hardrockbet"), None)
                pin_book = next((b for b in g.get("bookmakers", []) if b["key"] in ("pinnacle", "draftkings")), None)
                if not hr_book:
                    continue
                hr_summary = []
                for m in hr_book.get("markets", []):
                    outcomes = ", ".join(
                        f"{o['name']} {o.get('point', '')} ({o['price']:+d})".strip()
                        for o in m.get("outcomes", [])
                    )
                    hr_summary.append(f"{m['key'].upper()}: [{outcomes}]")
                pin_note = ""
                if pin_book:
                    pin_spread = next((m for m in pin_book.get("markets", []) if m["key"] == "spreads"), None)
                    if pin_spread:
                        p_out = ", ".join(f"{o['name']} {o.get('point')} ({o['price']:+d})" for o in pin_spread["outcomes"])
                        pin_note = f" | Sharp Ref ({pin_book['key']}): {p_out}"
                lines.append(f"- [{sport_key}] {away} @ {home} ({commence}) -> Hard Rock: {' ; '.join(hr_summary)}{pin_note}")
        except Exception as exc:
            lines.append(f"- [{sport_key}] Odds fetch error: {exc}")
    return lines or ["- No Hard Rock Bet lines active for target sports."]


def fetch_espn_intel():
    intel_lines = []
    for league, urls in ESPN_ENDPOINTS.items():
        # Breaking News
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

        # Injuries
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
                    upload_date = vid.get("upload_date")  # YYYYMMDD if available
                    if upload_date:
                        dt = datetime.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
                        if dt < cutoff - datetime.timedelta(days=1):
                            continue
                    try:
                        transcript_list = YouTubeTranscriptApi.get_transcript(vid_id)
                        full_text = " ".join(chunk["text"] for chunk in transcript_list)
                        condensed = re.sub(r"\s+", " ", full_text)[:3500]
                        yt_lines.append(f"- [{ch['name']}] {title} (https://youtu.be/{vid_id})
  Transcript Extract: {condensed}")
                    except Exception:
                        yt_lines.append(f"- [{ch['name']}] {title} (https://youtu.be/{vid_id}) [No captions available]")
        except Exception as exc:
            yt_lines.append(f"- [{ch['name']}] Channel check skipped: {exc}")
    return yt_lines or ["- No new YouTube uploads found in the last 24 hours."]


def update_rolling_google_doc(today_str, section_body):
    if not INTEL_DOC_ID or not GCP_SA_JSON:
        print("Missing INTEL_DOC_ID or GCP_SERVICE_ACCOUNT_JSON; printing output locally.")
        print(section_body)
        return

    service = get_docs_service()
    doc = service.documents().get(documentId=INTEL_DOC_ID).execute()
    content = doc.get("body", {}).get("content", [])

    # Locate Rolling Daily Slate Entries anchor and prune sections older than 7 days
    cutoff_date = (datetime.date.fromisoformat(today_str) - datetime.timedelta(days=7)).isoformat()
    full_text = ""
    anchor_index = 1

    for elem in content:
        para = elem.get("paragraph")
        if not para:
            continue
        start_idx = elem.get("startIndex", 1)
        text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
        full_text += text
        if "Rolling Daily Slate Entries" in text:
            anchor_index = elem.get("endIndex", start_idx)

    # Find any dated headers older than 7 days to delete
    requests_batch = []
    date_headers = list(re.finditer(r"(\d{4}-\d{2}-\d{2}) Slate Intel", full_text))
    for i, match in enumerate(date_headers):
        sec_date = match.group(1)
        if sec_date < cutoff_date:
            # Locate exact document indices for pruning
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
        # Execute deletions in reverse order so indices stay valid
        requests_batch.reverse()
        service.documents().batchUpdate(
            documentId=INTEL_DOC_ID, body={"requests": requests_batch}
        ).execute()

    # Re-fetch anchor after pruning and insert today's fresh slate intel at the top
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
                        "text": f"
{section_body}
",
                    }
                }
            ]
        },
    ).execute()
    print(f"Successfully updated Rolling 7-Day Intel Doc for {today_str}.")


def main():
    today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")
    odds_lines = fetch_hardrock_odds()
    espn_lines = fetch_espn_intel()
    yt_lines = fetch_youtube_transcripts()

    section_body = (
        f"## {today_str} Slate Intel

"
        f"### 1. Hard Rock Bet vs. Sharp Market Odds
"
        + "
".join(odds_lines)
        + "

### 2. ESPN Breaking News & Injury Alerts
"
        + "
".join(espn_lines)
        + "

### 3. Curated YouTube Channel Transcripts & Angles
"
        + "
".join(yt_lines)
        + "
"
    )
    update_rolling_google_doc(today_str, section_body)


if __name__ == "__main__":
    main()
