import os
import json
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai
from google.genai import types

# Environment / GitHub Secrets
GCP_SA_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
SHEET_ID = os.environ.get("MASTER_SHEET_ID", "")
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_google_clients():
    creds_dict = json.loads(GCP_SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/documents",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    docs_service = build("docs", "v1", credentials=creds)
    return gc, docs_service


def read_google_doc_text(docs_service, doc_id):
    doc = docs_service.documents().get(documentId=doc_id).execute()
    chunks = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if para:
            text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
            chunks.append(text)
    return "".join(chunks)


def read_tracker_context(gc):
    sh = gc.open_by_key(SHEET_ID)
    tracker_ws = sh.worksheet("Challenge Tracker ($100-$1k)")
    rules_ws = sh.worksheet("Unit & Tier Rules")
    audit_ws = sh.worksheet("Strategy Edge & Mistake Audit")

    tracker_rows = tracker_ws.get_all_values()
    rules_rows = rules_ws.get_all_values()
    audit_rows = audit_ws.get_all_values()

    summary_block = "\n".join([" | ".join(r[:20]) for r in tracker_rows[:12]])
    recent_bets = "\n".join([" | ".join(r[:20]) for r in tracker_rows[-15:]])
    rules_text = "\n".join([" | ".join(r[:8]) for r in rules_rows])
    audit_text = "\n".join([" | ".join(r[:8]) for r in audit_rows])

    return summary_block, recent_bets, rules_text, audit_text


def run_argon_curation(today_str, summary_block, recent_bets, rules_text, audit_text, intel_text):
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    prompt = f"""
You are the Argon Quantitative Sports Betting Curation Engine for the $100 to $1,000 Hard Rock Bet Challenge.
Today's Date: {today_str}

### 1. LIVE BANKROLL & RECENT LEDGER
{summary_block}

Recent Bets:
{recent_bets}

### 2. UNIT & TIER RULES (STRICT ENFORCEMENT)
{rules_text}

### 3. STRATEGY EDGE & MISTAKE AUDIT
{audit_text}

### 4. ROLLING 7-DAY SCRAPED SLATE INTEL
{intel_text[:28000]}

### TASK & HARD CONSTRAINTS:
1. Determine exact Settled Bankroll and Active Tier from the ledger:
   - Settled Bankroll < $50.00 -> SURVIVAL TIER ($2.00 unit, straight bets only, ZERO parlays).
   - Settled Bankroll $50.00-$249.99 -> TIER 1 ($4.00 unit, Core straights $6.00-$12.00).
2. Enforce Rule 1: Total active exposure MUST NOT exceed 50% of settled bankroll minus pending bets.
3. Enforce Rule 8: Max juice -140 on straights; avoid .5 hooks on key numbers 3, 7, 10.
4. Enforce Rule 5: Max 2 correlated legs on any SGP/parlay.

### STRICT TELEGRAM FORMATTING RULES (CLEAN & MINIMAL):
- Output ONLY the compact template below. ZERO extra commentary, NO paragraphs, NO UTC timestamps, NO "Leg 1 / Leg 2" prefixes.
- Use short team names (e.g., "Spurs", "Sharks", "Knicks").
- For every parlay/SGP (both Pending and New), list each pick on its own clean sub-line (`  - Pick (Odds)`).

🎯 *{today_str} Hard Rock Bet Slate*
Bankroll: $XX.XX | Tier: [Tier] | Available: $X.XX

*Pending ($XX.XX / $XX.XX Cap):*
• *BET-XXX* ($X.XX) — [Straight Pick (Odds)]
• *BET-XXX* ($X.XX) — [Parlay Title (Total Odds)]
  - [Pick 1 (Odds)]
  - [Pick 2 (Odds)]

*Today's Plays:*
• *[League] Selection (Odds)* — *$X.XX* (X.XU)
  - Edge: [1 short sentence max]

(If no new plays qualify, write ONLY: `• None — exposure near 50% cap.` with zero extra sentences.)
"""
    candidate_models = [
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    ]
    try:
        live_models = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
            and "gemini" in m.name.lower()
            and "embedding" not in m.name.lower()
            and "image" not in m.name.lower()
            and "audio" not in m.name.lower()
            and "tts" not in m.name.lower()
        ]
        for m in live_models:
            if m not in candidate_models:
                candidate_models.append(m)
    except Exception as list_exc:
        print(f"Model listing warning: {list_exc}")

    for model_id in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
        except Exception as exc:
            print(f"Model {model_id} skipped: {exc}")
    raise RuntimeError("All discovered Gemini models failed.")


def append_to_curator_feed(gc, today_str, curated_message):
    sh = gc.open_by_key(SHEET_ID)
    feed_ws = sh.worksheet("Curator Feed")
    feed_ws.append_row([
        today_str,
        f"{today_str} 09:12 EST",
        "Argon Daily Curation Summary",
        "Daily Brief",
        "-",
        "-",
        "-",
        "-",
        "-",
        "-",
        "SENT_912AM",
        curated_message[:4500],
    ])


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Alert text:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    print("9:12 AM Telegram bet slip delivered successfully.")


def main():
    today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")
    gc, docs_service = get_google_clients()
    summary_block, recent_bets, rules_text, audit_text = read_tracker_context(gc)
    intel_text = read_google_doc_text(docs_service, INTEL_DOC_ID) if INTEL_DOC_ID else "No Intel Doc ID set."
    curated_message = run_argon_curation(today_str, summary_block, recent_bets, rules_text, audit_text, intel_text)
    append_to_curator_feed(gc, today_str, curated_message)
    send_telegram_alert(curated_message)


if __name__ == "__main__":
    main()
