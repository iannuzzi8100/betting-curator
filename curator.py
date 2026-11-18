import os
import json
import datetime
import requests
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai
from google.genai import types

load_dotenv()

# Environment / GitHub Secrets
GCP_SA_KEY = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or ""
SHEET_ID = (
    os.environ.get("SHEET_ID")
    or os.environ.get("MASTER_SHEET_ID")
    or os.environ.get("SPREADSHEET_ID")
    or os.environ.get("TRACKER_SHEET_ID")
    or os.environ.get("GOOGLE_SHEET_ID")
    or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
)
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID") or "1vTMvUyr_DABrrzVBU7ah1A8RYn9-sfO1Uu-0Syk2jnU"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_google_clients():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
    ]
    if GCP_SA_KEY:
        creds_dict = json.loads(GCP_SA_KEY)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    gc = gspread.authorize(creds)
    docs_service = build("docs", "v1", credentials=creds)
    return gc, docs_service


def open_tracker_workbook(gc):
    try:
        return gc.open(SHEET_NAME)
    except Exception:
        return gc.open_by_key(SHEET_ID)


def read_google_doc_text(docs_service, doc_id):
    doc = docs_service.documents().get(documentId=doc_id).execute()
    chunks = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if para:
            text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
            chunks.append(text)
    return "".join(chunks)


def _parse_dollar(val):
    if not val:
        return 0.0
    cleaned = str(val).replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def read_tracker_context(gc):
    sh = open_tracker_workbook(gc)
    tracker_ws = sh.worksheet("Challenge Tracker ($100-$1k)")
    rules_ws = sh.worksheet("Unit & Tier Rules")
    audit_ws = sh.worksheet("Strategy Edge & Mistake Audit")

    tracker_rows = tracker_ws.get_all_values()
    rules_rows = rules_ws.get_all_values()
    audit_rows = audit_ws.get_all_values()

    # Deterministic calculation of Settled Bankroll, Pending Wagers, and Available Bankroll
    settled_bankroll = _parse_dollar(tracker_rows[4][0]) if len(tracker_rows) > 4 and tracker_rows[4] else 0.0
    pending_lines = []
    pending_total = 0.0

    header_idx = next((i for i, r in enumerate(tracker_rows) if "Bet ID" in r and "Result" in r), 12)
    for r in tracker_rows[header_idx + 1 :]:
        if len(r) > 10 and r[0].strip() and r[10].strip().lower() == "pending":
            bet_id = r[0].strip()
            bet_type = r[3].strip() if len(r) > 3 else ""
            selection = r[5].strip() if len(r) > 5 else ""
            odds = r[6].strip() if len(r) > 6 else ""
            stake_val = _parse_dollar(r[8]) if len(r) > 8 else 0.0
            pending_total += stake_val
            pending_lines.append(f"- {bet_id} (${stake_val:.2f}) [{bet_type}]: {selection} ({odds})")
        elif len(r) > 13 and r[0].strip() and r[10].strip().lower() in ("won", "lost", "push"):
            rb = _parse_dollar(r[13])
            if rb > 0:
                settled_bankroll = rb

    available_bankroll = max(0.0, round(settled_bankroll - pending_total, 2))
    verified_bankroll_banner = (
        f"VERIFIED LEDGER NUMBERS (USE EXACTLY THESE IN OUTPUT HEADER):\n"
        f"- Settled Bankroll: ${settled_bankroll:.2f}\n"
        f"- Pending Wagers Total: ${pending_total:.2f} ({len(pending_lines)} active ticket(s))\n"
        f"- Available Bankroll: ${available_bankroll:.2f}\n"
        f"- Active Pending Tickets:\n"
        + ("\n".join(pending_lines) if pending_lines else "  * None")
    )

    summary_block = verified_bankroll_banner + "\n\n" + "\n".join([" | ".join(r[:20]) for r in tracker_rows[:12]])
    recent_bets = "\n".join([" | ".join(r[:20]) for r in tracker_rows[-18:]])
    rules_text = "\n".join([" | ".join(r[:8]) for r in rules_rows])
    audit_text = "\n".join([" | ".join(r[:8]) for r in audit_rows])

    return summary_block, recent_bets, rules_text, audit_text


def run_argon_curation(today_str, summary_block, recent_bets, rules_text, audit_text, intel_text):
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    prompt = f"""
You are the Argon Quantitative Sports Betting Engine driving a rapid-compounding $100 to $1,000 Hard Rock Bet Challenge using Fractional Kelly staking.
Today's Date: {today_str}

### 1. LIVE BANKROLL & RECENT LEDGER
{summary_block}

Recent Bets:
{recent_bets}

### 2. UNIT & TIER RULES + HISTORICAL MISTAKE AUDIT
{rules_text}

{audit_text}

### 3. SCRAPED SLATE INTEL (GAME ODDS + 1H LINES + NO-VIG +EV% + PLAYER PROPS + INJURIES + YOUTUBE CONSENSUS)
{intel_text[:32000]}

### OBJECTIVE & ALLOCATION ENGINE (MAXIMIZE BANKROLL GROWTH VELOCITY):
1. Calculate exact **Available Bankroll** (Settled Balance minus Pending Wagers).
2. Scan ALL markets across NBA, NHL, CBB, CFB, NFL, EPL/UCL Soccer, and ATP/WTA Tennis:
   - **Pre-Computed `[+EV: ...]` Straights & 1st-Half Lines** (prioritize lines with verified positive mathematical EV vs Pinnacle/DraftKings).
   - **+EV Player Props** (line discrepancies or +2%+ EV vs sharp books).
   - **Correlated Same-Game Parlays (2 legs):**
     * NBA/CBB Favorite -7.5 or higher + Game Over (blowout pace correlation) OR Underdog +8.5 + Game Under.
     * NHL Underdog Moneyline + Game Under 5.5 (low-scoring goalie upset correlation).
   - **Cross-Sport Multi-Leg Parlays (2 to 4 legs):** Combine highest-conviction +EV picks or dominant mismatches across different leagues/teams.
3. Rank the **Top 1 to 4 Best Bets of the Day** and size stakes via **Fractional Kelly** based on edge quality:
   - **A+ Single Play (>6% mathematical EV or massive injury mismatch):** Stake **15%–25%** of Available Bankroll.
   - **A Core Play / Player Prop (2.5%–6% EV):** Stake **8%–15%** of Available Bankroll.
   - **Correlated SGP / Cross-Sport Parlay (2–4 legs):** Stake **5%–10%** of Available Bankroll for asymmetric growth.
   - Total deployed across today's slate can reach up to **75% of Available Bankroll** when multiple strong edges exist simultaneously (or **0%** if Available Bankroll < $4.00).
4. **LINEUP GUARDRAIL:** If any recommended play involves a key player listed as `Questionable` or `Day-To-Day` on the ESPN injury feed, append `[WAIT FOR LINEUP]` so the user confirms active status before locking it in on Hard Rock Bet.

### STRICT TELEGRAM FORMATTING RULES (CLEAN, COMPACT & SCANNABLE):
- Output ONLY the compact template below. NO extra paragraphs, NO UTC timestamps, NO "Leg 1 / Leg 2" prefixes.
- Use short team/player names.
- Every parlay/SGP (both Pending and New) MUST list each pick on its own indented sub-line (`  - Pick (Odds)`).

🎯 *{today_str} Hard Rock Bet Slate*
Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX

*Pending ($XX.XX):*
• *BET-XXX* ($X.XX) — [Straight Pick (Odds)]
• *BET-XXX* ($X.XX) — [Parlay Title (Total Odds)]
  - [Pick 1 (Odds)]
  - [Pick 2 (Odds)]

*Today's Best EV Plays:*
1. *[Play Type] Selection (Odds)* — *$X.XX*
   - [If Parlay/SGP: list each leg on its own sub-line]
   - Edge: [1 short line: No-Vig EV%, sharp delta, or injury edge]

(If Available Bankroll < $4.00, output ONLY: `• Holding — waiting for pending slate to settle before deploying capital.`)
"""
    candidate_models = [
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
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
    sh = open_tracker_workbook(gc)
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
