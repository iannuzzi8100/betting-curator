import os
import json
import datetime
import requests
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

load_dotenv()

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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_workbook():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if GCP_SA_KEY:
        creds = Credentials.from_service_account_info(json.loads(GCP_SA_KEY), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    gc = gspread.authorize(creds)
    try:
        return gc.open(SHEET_NAME)
    except Exception:
        return gc.open_by_key(SHEET_ID)


def get_last_update_id(feed_ws):
    try:
        val = feed_ws.acell("M1").value
        if val and str(val).startswith("TG_OFFSET:"):
            return int(str(val).split(":")[1])
    except Exception:
        pass
    return 0


def set_last_update_id(feed_ws, update_id):
    try:
        feed_ws.update_acell("M1", f"TG_OFFSET:{update_id}")
    except Exception:
        pass


def parse_bet_from_telegram(text_msg=None, image_bytes=None):
    client = genai.Client(api_key=GEMINI_API_KEY)
    est_today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")

    prompt = f"""
You are a deterministic sportsbook ticket parser for a Hard Rock Bet tracker.
Extract every sports bet shown in the user's message or Hard Rock Bet screenshot.
Today's Date: {est_today}

RULES:
1. For "sport": use one of ["NBA", "NHL", "NCAAM", "CFB", "NFL", "EPL", "UCL", "ATP", "WTA", "MLB", "Multi-Sport"]. (College basketball = "NCAAM", College football = "CFB").
2. For "bet_type": use one of ["Straight", "SGP", "Parlay"].
3. For "selections": format cleanly as "<Pick> (<Matchup>)" e.g. "Howard Bison ML (Howard @ Duquesne)" or "Devon Toews Over 1.5 Shots (Avalanche @ Mammoth)".
4. For "odds": integer American odds (e.g. -115, 190, 100).
5. For "stake": numeric dollar wager amount (e.g. 2.50, 1.50).
6. For "ticket_id": string Hard Rock Bet ID if visible on screenshot (e.g. "4311946676851048722"), else "".

Respond ONLY with valid JSON matching this schema:
{{
  "tickets": [
    {{
      "date": "{est_today}",
      "sport": "NCAAM",
      "bet_type": "Straight",
      "selections": "Howard Bison ML (Howard @ Duquesne)",
      "odds": 190,
      "stake": 1.50,
      "ticket_id": "4311946676851048722"
    }}
  ]
}}
"""
    contents = [prompt]
    if text_msg:
        contents.append(f"User message: {text_msg}")
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    for model_id in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash"]:
        try:
            resp = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
            )
            if resp and resp.text:
                return json.loads(resp.text).get("tickets", [])
        except Exception as exc:
            print(f"Model {model_id} skipped: {exc}")
    return []


def download_telegram_photo(file_id):
    r1 = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=10,
    ).json()
    file_path = r1.get("result", {}).get("file_path")
    if not file_path:
        return None
    r2 = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}", timeout=15)
    return r2.content if r2.status_code == 200 else None


def log_tickets_to_sheet(tracker_ws, tickets):
    all_vals = tracker_ws.get_all_values()
    header_idx = next((i for i, r in enumerate(all_vals) if "Bet ID" in r and "Result" in r), 12)

    existing_text = "\n".join(" | ".join(r) for r in all_vals[header_idx + 1 :])
    last_bet_num = 0
    last_bet_row = header_idx + 2

    for idx in range(header_idx + 1, len(all_vals)):
        r = all_vals[idx]
        if r and r[0].strip().startswith("BET-"):
            try:
                num = int(r[0].strip().split("-")[1])
                if num > last_bet_num:
                    last_bet_num = num
                    last_bet_row = idx + 1
            except Exception:
                pass

    logged = []
    for t in tickets:
        t_id = str(t.get("ticket_id", "")).strip()
        sel = str(t.get("selections", "")).strip()
        stake = float(t.get("stake", 0.0) or 0.0)
        odds = int(t.get("odds", -110) or -110)
        sport = str(t.get("sport", "NBA")).strip()
        bet_type = str(t.get("bet_type", "Straight")).strip()
        date_str = str(t.get("date", "")).strip()

        if not sel or stake <= 0:
            continue
        if t_id and t_id in existing_text:
            print(f"Skipping duplicate ticket ID {t_id} ({sel})")
            continue

        last_bet_num += 1
        last_bet_row += 1
        bet_id = f"BET-{last_bet_num:03d}"
        prev_row = last_bet_row - 1
        cur_row = last_bet_row
        units_str = f"{stake / 4.0:.2f}U"
        tier_label = "Core Tier 1" if bet_type == "Straight" else "Flyer Tier 3"
        note_str = f"Hard Rock Bet Ticket: {sel} ({odds:+d}) | Stake: ${stake:.2f}" + (
            f" (Hard Rock ID: {t_id})" if t_id else ""
        )

        row_payload = [
            bet_id,
            date_str,
            sport,
            bet_type,
            tier_label,
            sel,
            odds,
            units_str,
            stake,
            f'=IF(OR(ISBLANK(I{cur_row}), ISBLANK(G{cur_row}), I{cur_row}="", G{cur_row}=""), "", IF(G{cur_row}>0, ROUND(I{cur_row}*(1+G{cur_row}/100), 2), ROUND(I{cur_row}*(1+100/ABS(G{cur_row})), 2)))',
            "Pending",
            f'=IF(K{cur_row}="Won", J{cur_row}, IF(K{cur_row}="Lost", 0, ""))',
            f'=IF(OR(K{cur_row}="", K{cur_row}="Pending"), "", IF(K{cur_row}="Lost", -I{cur_row}, IF(ISNUMBER(L{cur_row}), L{cur_row}-I{cur_row}, "")))',
            f'=IF(OR(ISBLANK(I{cur_row}), I{cur_row}=""), "", IF(ISNUMBER(M{cur_row}), N{prev_row}+M{cur_row}, N{prev_row}))',
            note_str,
            "",
        ]
        tracker_ws.update(
            range_name=f"A{cur_row}:P{cur_row}",
            values=[row_payload],
            value_input_option="USER_ENTERED",
        )
        logged.append((bet_id, sel, odds, stake))

    return logged


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not configured.")
        return

    sh = get_workbook()
    tracker_ws = sh.worksheets()[0]
    feed_ws = sh.worksheet("Curator Feed")
    last_offset = get_last_update_id(feed_ws)

    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        params={"offset": last_offset + 1, "timeout": 5},
        timeout=12,
    ).json()

    updates = resp.get("result", [])
    if not updates:
        print("No new Telegram bet messages or screenshots to log.")
        return

    max_update_id = last_offset
    all_extracted = []

    for u in updates:
        u_id = u.get("update_id", 0)
        if u_id > max_update_id:
            max_update_id = u_id

        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            continue

        text = msg.get("text") or msg.get("caption") or ""
        photos = msg.get("photo", [])
        img_bytes = None
        if photos:
            best_photo = photos[-1]
            img_bytes = download_telegram_photo(best_photo["file_id"])

        if text.strip().lower() in ("/start", "hi", "hello", "status"):
            continue

        if img_bytes or text:
            extracted = parse_bet_from_telegram(text_msg=text, image_bytes=img_bytes)
            all_extracted.extend(extracted)

    set_last_update_id(feed_ws, max_update_id)

    if not all_extracted:
        print("Processed updates; no new sportsbook tickets detected.")
        return

    logged = log_tickets_to_sheet(tracker_ws, all_extracted)
    if logged:
        lines = [f"✅ *Logged {len(logged)} Ticket(s) to Challenge Tracker:*"]
        for bid, sel, odds, stake in logged:
            lines.append(f"• *{bid}* (${stake:.2f}) — {sel} ({odds:+d})")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "\n".join(lines),
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        print(f"Successfully logged {len(logged)} ticket(s) from Telegram.")


if __name__ == "__main__":
    main()
