import os
import sys
import time
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

# Non-challenge personal tickets to always ignore if they appear in screenshots
IGNORED_TICKET_IDS = {
    "2313737142865625362",  # Florida Gators 2026/2027 NCAAM National Championship (+600, $100) — separate from $100-$1k challenge
}


def _is_ignored_non_challenge_ticket(t_id="", sel="", stake=0.0):
    if t_id and str(t_id).strip() in IGNORED_TICKET_IDS:
        return True
    s = str(sel or "").lower()
    if "florida gators" in s and ("championship" in s or "winner" in s or "national" in s or abs(float(stake or 0) - 100.0) < 0.01):
        return True
    return False



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
        if feed_ws.col_count < 13:
            feed_ws.add_cols(13 - feed_ws.col_count)
        feed_ws.update_acell("M1", f"TG_OFFSET:{update_id}")
    except Exception:
        pass


def _bet_num(bet_id):
    try:
        return int(str(bet_id).strip().split("-")[1])
    except Exception:
        return 0


def get_current_sheet_state(tracker_ws):
    all_vals = tracker_ws.get_all_values()
    header_idx = next((i for i, r in enumerate(all_vals) if "Bet ID" in r and "Result" in r), 15)
    pending_rows = []
    recent_rows = []
    for idx in range(header_idx + 1, len(all_vals)):
        r = all_vals[idx]
        if not r or not r[0].strip().startswith("BET-"):
            continue
        item = {
            "row_index": idx + 1,
            "bet_id": r[0].strip(),
            "date": r[1].strip() if len(r) > 1 else "",
            "sport": r[2].strip() if len(r) > 2 else "",
            "bet_type": r[3].strip() if len(r) > 3 else "",
            "selections": r[5].strip() if len(r) > 5 else "",
            "odds": r[6].strip() if len(r) > 6 else "",
            "stake": r[8].strip() if len(r) > 8 else "",
            "result": r[10].strip() if len(r) > 10 else "",
        }
        recent_rows.append(item)
        if item["result"].lower() == "pending":
            pending_rows.append(item)
    recent_rows.sort(key=lambda x: _bet_num(x["bet_id"]))
    return all_vals, header_idx, pending_rows, recent_rows[-10:]


def parse_telegram_reply(text_msg=None, image_bytes=None, pending_rows=None, recent_rows=None, image_mime_type="image/jpeg"):
    client = genai.Client(api_key=GEMINI_API_KEY)
    est_today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5))).strftime("%Y-%m-%d")

    prompt = f"""
You are a deterministic sportsbook ledger controller for a Hard Rock Bet tracker.
Interpret the user's Telegram reply (text, caption, and/or screenshot) and determine all spreadsheet actions needed:
1. New tickets placed (from screenshots or text like "put $4 on Ducks ML +110").
2. Full-refund cashouts or removals of existing pending bets (e.g., "Cashed out the Portland bet for a full refund", "remove BET-042", "void Temple bet").
3. Partial cashouts or edits to existing bets (e.g., "cashed out BET-038 for $5.14", "changed Blues stake to $5.00", "Temple is CFB not NCAAM", "BET-043 won").

Today's Date: {est_today}

CURRENT ACTIVE PENDING BETS ON SPREADSHEET:
{json.dumps(pending_rows or [], indent=2)}

RECENT BETS ON SPREADSHEET:
{json.dumps(recent_rows or [], indent=2)}

RULES:
1. For "sport": use one of ["NBA", "NHL", "NCAAM", "CFB", "NFL", "EPL", "UCL", "ATP", "WTA", "MLB", "Multi-Sport"].
   - Note: Rice @ Temple, MACtion, FBS games = "CFB". College basketball = "NCAAM".
2. For "bet_type": use one of ["Straight", "SGP", "Parlay"].
3. For "selections": format cleanly as "<Pick> (<Matchup> • <Game Time EST>)" if the game time is visible on the ticket (e.g. "Josh Giddey Over 7.5 Rebounds (Wizards @ Bulls • 8:00 PM EST)" or "Canisius ML (Mercyhurst @ Canisius • 5:30 PM EST)"), otherwise "<Pick> (<Matchup>)".
4. If the user says they cashed out a pending bet for a full refund (or wants to remove/replace a pending bet), list that pending bet's exact "bet_id" and "row_index" in "removals_or_cashouts" with "is_full_refund": true.
5. If the user says they cashed out a pending bet for a partial amount (less than stake), set "is_full_refund": false and "cashout_return": <dollar amount>.
6. If the user asks to edit an existing row's sport, stake, odds, selection, or result, include an entry in "updates".
7. ALWAYS IGNORE non-challenge personal futures bets — specifically Hard Rock ID 2313737142865625362 (Florida Gators NCAAM National Championship 2026/2027 +600 for $100). Never include it in "tickets".

Respond ONLY with valid JSON matching this exact schema:
{{
  "removals_or_cashouts": [
    {{
      "row_index": 59,
      "bet_id": "BET-042",
      "is_full_refund": true,
      "cashout_return": 4.00,
      "reason": "Cashed out Portland Trail Blazers -9.5 for full refund"
    }}
  ],
  "updates": [
    {{
      "row_index": 62,
      "bet_id": "BET-045",
      "sport": "CFB",
      "selections": null,
      "odds": null,
      "stake": null,
      "result": null
    }}
  ],
  "tickets": [
    {{
      "date": "{est_today}",
      "sport": "NHL",
      "bet_type": "Straight",
      "selections": "Anaheim Ducks ML (Ducks @ Avalanche)",
      "odds": 110,
      "stake": 4.00,
      "ticket_id": "1825834922064085270"
    }}
  ]
}}
"""
    contents = [prompt]
    if text_msg:
        contents.append(f"User Telegram reply text/caption: {text_msg}")
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type or "image/jpeg"))

    for model_id in [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]:
        try:
            resp = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
            )
            if resp and resp.text:
                return json.loads(resp.text)
        except Exception as exc:
            print(f"Model {model_id} skipped: {exc}")
    return None


def download_telegram_photo(file_id):
    file_path = None
    for attempt in range(3):
        try:
            r1 = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
                timeout=30,
            ).json()
            file_path = r1.get("result", {}).get("file_path")
            if file_path:
                break
        except Exception as exc:
            print(f"Telegram getFile attempt {attempt + 1} failed: {exc}", flush=True)
            time.sleep(2)
    if not file_path:
        return None
    for attempt in range(3):
        try:
            r2 = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}", timeout=30)
            if r2.status_code == 200:
                return r2.content
        except Exception as exc:
            print(f"Telegram photo download attempt {attempt + 1} failed: {exc}", flush=True)
            time.sleep(2)
    return None


def _normalize_sport(sport):
    s = str(sport or "").strip()
    if s.upper() in ("CBB", "NCAAB", "NCAAM", "MEN'S COLLEGE BASKETBALL", "COLLEGE BASKETBALL"):
        return "NCAAM"
    return s


def build_row_payload(bet_id, cur_row, date_str, sport, bet_type, sel, odds, stake, t_id="", extra_note=""):
    prev_row = cur_row - 1
    sport = _normalize_sport(sport)
    units_str = f"{stake / 4.0:.2f}U"
    tier_label = "Core Tier 1" if bet_type == "Straight" else "Flyer Tier 3"
    note_str = f"Hard Rock Bet Ticket: {sel} ({odds:+d}) | Stake: ${stake:.2f}"
    if t_id:
        note_str += f" (Hard Rock ID: {t_id})"
    if extra_note:
        note_str += f" [{extra_note}]"

    return [
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
        f'=IF(K{cur_row}="Won", J{cur_row}, IF(K{cur_row}="Lost", 0, IF(K{cur_row}="Push", I{cur_row}, "")))',
        f'=IF(OR(K{cur_row}="", K{cur_row}="Pending"), "", IF(K{cur_row}="Lost", -I{cur_row}, IF(ISNUMBER(L{cur_row}), L{cur_row}-I{cur_row}, "")))',
        f'=IF(OR(ISBLANK(I{cur_row}), I{cur_row}=""), "", ROUND(100 + SUM(M{cur_row}:M), 2))',
        note_str,
        "",
    ]


def _clean_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(str(val).replace("$", "").replace(",", "").strip())
    except Exception:
        return default


def _clean_int(val, default=-110):
    if val is None:
        return default
    try:
        return int(float(str(val).replace("+", "").replace(",", "").strip()))
    except Exception:
        return default


def apply_actions_to_sheet(tracker_ws, actions):
    all_vals, header_idx, _, _ = get_current_sheet_state(tracker_ws)
    existing_text = "\n".join(" | ".join(r) for r in all_vals[header_idx + 1 :])

    removals = actions.get("removals_or_cashouts", []) or []
    updates = actions.get("updates", []) or []
    new_tickets = actions.get("tickets", []) or []

    action_notes = []
    freed_slots = []  # (row_index, bet_id, reason)

    # 1. Process removals / cashouts
    for rem in removals:
        r_idx = rem.get("row_index")
        b_id = str(rem.get("bet_id", "")).strip()
        is_full = bool(rem.get("is_full_refund", True))
        c_ret = rem.get("cashout_return")
        reason = str(rem.get("reason", "Cashed out")).strip()

        if not r_idx and b_id:
            for i in range(header_idx + 1, len(all_vals)):
                if all_vals[i] and all_vals[i][0].strip() == b_id:
                    r_idx = i + 1
                    break
        if not r_idx:
            continue

        if is_full:
            freed_slots.append((r_idx, b_id, reason))
        else:
            # Partial cashout: record actual return & mark Won if profitable, Lost otherwise
            c_ret_f = _clean_float(c_ret, 0.0)
            stake_f = _clean_float(all_vals[r_idx - 1][8] if len(all_vals[r_idx - 1]) > 8 else "0", 0.0)
            net_pl = round(c_ret_f - stake_f, 2)
            tracker_ws.update_acell(f"K{r_idx}", "Won" if net_pl > 0 else "Lost")
            if c_ret is not None:
                tracker_ws.update_acell(f"L{r_idx}", c_ret_f)
                tracker_ws.update_acell(f"M{r_idx}", net_pl)
            action_notes.append(f"🔄 *Partial Cashout on {b_id}* (Row {r_idx}): Returned ${c_ret_f:.2f} (Net {net_pl:+.2f})")

    # 2. Process in-place field updates
    for upd in updates:
        r_idx = upd.get("row_index")
        b_id = str(upd.get("bet_id", "")).strip()
        if not r_idx and b_id:
            for i in range(header_idx + 1, len(all_vals)):
                if all_vals[i] and all_vals[i][0].strip() == b_id:
                    r_idx = i + 1
                    break
        if not r_idx:
            continue

        changed_fields = []
        if upd.get("sport"):
            norm_sp = _normalize_sport(upd["sport"])
            tracker_ws.update_acell(f"C{r_idx}", norm_sp)
            changed_fields.append(f"Sport={norm_sp}")
        if upd.get("selections"):
            tracker_ws.update_acell(f"F{r_idx}", str(upd["selections"]).strip())
            changed_fields.append(f"Selection={upd['selections']}")
        if upd.get("odds") is not None:
            o_val = _clean_int(upd["odds"])
            tracker_ws.update_acell(f"G{r_idx}", o_val)
            changed_fields.append(f"Odds={o_val:+d}")
        if upd.get("stake") is not None:
            st = _clean_float(upd["stake"])
            tracker_ws.update_acell(f"H{r_idx}", f"{st/4.0:.2f}U")
            tracker_ws.update_acell(f"I{r_idx}", st)
            changed_fields.append(f"Stake=${st:.2f}")
        if upd.get("result"):
            tracker_ws.update_acell(f"K{r_idx}", str(upd["result"]).strip())
            changed_fields.append(f"Result={upd['result']}")
        if changed_fields:
            action_notes.append(f"✏️ *Updated {b_id}* (Row {r_idx}): {', '.join(changed_fields)}")

    # 3. Filter new tickets for duplicates
    valid_new_tickets = []
    for t in new_tickets:
        t_id = str(t.get("ticket_id", "")).strip()
        sel = str(t.get("selections", "")).strip()
        stake = _clean_float(t.get("stake", 0.0))
        if not sel or stake <= 0:
            continue
        if _is_ignored_non_challenge_ticket(t_id, sel, stake):
            print(f"Skipping ignored non-challenge ticket {t_id} ({sel})")
            continue
        if t_id and t_id in existing_text:
            print(f"Skipping duplicate ticket ID {t_id} ({sel})")
            continue
        valid_new_tickets.append(t)

    # 4. If we have freed_slots from full-refund cashouts, fill them with replacement tickets first!
    while freed_slots and valid_new_tickets:
        r_idx, b_id, reason = freed_slots.pop(0)
        t = valid_new_tickets.pop(0)
        t_id = str(t.get("ticket_id", "")).strip()
        sel = str(t.get("selections", "")).strip()
        stake = _clean_float(t.get("stake", 0.0))
        odds = _clean_int(t.get("odds", -110))
        sport = str(t.get("sport", "NBA")).strip()
        bet_type = str(t.get("bet_type", "Straight")).strip()
        date_str = str(t.get("date", "")).strip()

        row_payload = build_row_payload(
            b_id, r_idx, date_str, sport, bet_type, sel, odds, stake, t_id=t_id, extra_note=f"Replaced cashed-out ticket: {reason}"
        )
        tracker_ws.update(range_name=f"A{r_idx}:P{r_idx}", values=[row_payload], value_input_option="USER_ENTERED")
        action_notes.append(f"🔄 *Replaced {b_id}* (Row {r_idx} full refund) -> *{sel} ({odds:+d})* — *${stake:.2f}*")

    # 5. Delete remaining freed_slots bottom-to-top (newest-first SUM(M{r}:M) stays exact with zero blank rows)
    for r_idx, b_id, reason in sorted(freed_slots, key=lambda x: x[0], reverse=True):
        tracker_ws.delete_rows(r_idx)
        action_notes.append(f"🗑️ *Removed Cashed-Out {b_id}* (Row {r_idx} — {reason})")

    # 6. Insert remaining new tickets at the TOP of the tracker (newest-first, right under header)
    if valid_new_tickets:
        all_vals, header_idx, _, _ = get_current_sheet_state(tracker_ws)
        max_bet_num = 0
        for idx in range(header_idx + 1, len(all_vals)):
            r = all_vals[idx]
            if r and r[0].strip().startswith("BET-"):
                num = _bet_num(r[0].strip())
                if num > max_bet_num:
                    max_bet_num = num

        insert_row = header_idx + 2
        for t in valid_new_tickets:
            t_id = str(t.get("ticket_id", "")).strip()
            sel = str(t.get("selections", "")).strip()
            stake = _clean_float(t.get("stake", 0.0))
            odds = _clean_int(t.get("odds", -110))
            sport = str(t.get("sport", "NBA")).strip()
            bet_type = str(t.get("bet_type", "Straight")).strip()
            date_str = str(t.get("date", "")).strip()

            max_bet_num += 1
            bet_id = f"BET-{max_bet_num:03d}"
            row_payload = build_row_payload(bet_id, insert_row, date_str, sport, bet_type, sel, odds, stake, t_id=t_id)
            tracker_ws.insert_row(row_payload, index=insert_row, value_input_option="USER_ENTERED")
            action_notes.append(f"✅ *Logged {bet_id}* (${stake:.2f}) — {sel} ({odds:+d})")

    return action_notes


def process_once(poll_timeout=5):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not configured.")
        return

    sh = get_workbook()
    tracker_ws = sh.worksheets()[0]
    feed_ws = sh.worksheet("Curator Feed")
    last_offset = get_last_update_id(feed_ws)

    resp = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": last_offset + 1, "timeout": poll_timeout},
                timeout=max(25, poll_timeout + 15),
            )
            if r.status_code == 409:
                print("Telegram webhook is active — instant webhook handles bet sync; skipping polling cleanly.", flush=True)
                return
            r.raise_for_status()
            resp = r.json()
            break
        except Exception as exc:
            print(f"Telegram getUpdates attempt {attempt + 1} failed: {exc}", flush=True)
            time.sleep(2)

    if not resp:
        print("Skipping Telegram sync cycle due to transient Telegram timeout.", flush=True)
        return

    updates = resp.get("result", [])
    if not updates:
        return

    max_update_id = last_offset
    all_action_notes = []

    for u in updates:
        u_id = u.get("update_id", 0)
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            if u_id > max_update_id:
                max_update_id = u_id
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            if u_id > max_update_id:
                max_update_id = u_id
            continue

        text = (msg.get("text") or msg.get("caption") or "").strip()
        photos = msg.get("photo", [])
        doc = msg.get("document") or {}
        doc_mime = str(doc.get("mime_type", ""))
        has_image = bool(photos) or (doc_mime.startswith("image/") and bool(doc.get("file_id")))
        img_mime = "image/jpeg"
        img_bytes = None
        if photos:
            img_bytes = download_telegram_photo(photos[-1]["file_id"])
        elif doc_mime.startswith("image/") and doc.get("file_id"):
            img_mime = doc_mime
            img_bytes = download_telegram_photo(doc["file_id"])

        if has_image and not img_bytes:
            print(f"Failed to download image for update {u_id}; halting offset advance for retry.", flush=True)
            break

        if not img_bytes and text.lower() in ("/start", "hi", "hello"):
            if u_id > max_update_id:
                max_update_id = u_id
            continue

        if not img_bytes and text.lower() in ("status", "/status", "bankroll", "/bankroll"):
            all_vals, _, pending_rows, _ = get_current_sheet_state(tracker_ws)
            bankroll_str = all_vals[4][0] if len(all_vals) > 4 and all_vals[4] else "$0.00"
            avail_str = all_vals[5][0] if len(all_vals) > 5 and all_vals[5] else ""
            p_lines = [f"• *{p['bet_id']}* ({p['stake']}) — {p['selections']} ({p['odds']})" for p in pending_rows]
            status_reply = (
                f"📊 *Live Challenge Tracker Status*\n"
                f"Settled Bankroll: *{bankroll_str}* ({avail_str})\n\n"
                f"*Active Pending ({len(pending_rows)}):*\n"
                + ("\n".join(p_lines) if p_lines else "• None")
            )
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": status_reply, "parse_mode": "Markdown"},
                timeout=10,
            )
            if u_id > max_update_id:
                max_update_id = u_id
            continue

        if img_bytes or text:
            _, _, pending_rows, recent_rows = get_current_sheet_state(tracker_ws)
            parsed = parse_telegram_reply(
                text_msg=text,
                image_bytes=img_bytes,
                pending_rows=pending_rows,
                recent_rows=recent_rows,
                image_mime_type=img_mime,
            )
            if parsed is None:
                print(f"Gemini parse failed for update {u_id}; halting offset advance for retry.", flush=True)
                break
            notes = apply_actions_to_sheet(tracker_ws, parsed)
            all_action_notes.extend(notes)

        if u_id > max_update_id:
            max_update_id = u_id

    if max_update_id > last_offset:
        set_last_update_id(feed_ws, max_update_id)
        try:
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": max_update_id + 1, "timeout": 1},
                timeout=6,
            )
        except Exception:
            pass

    if all_action_notes:
        try:
            with open(".tg_changed", "w", encoding="utf-8") as f:
                f.write(str(len(all_action_notes)))
        except Exception:
            pass
        all_vals, _, pending_rows, _ = get_current_sheet_state(tracker_ws)
        bankroll_str = all_vals[4][0] if len(all_vals) > 4 and all_vals[4] else "$0.00"
        avail_str = all_vals[5][0] if len(all_vals) > 5 and all_vals[5] else ""
        reply_text = (
            "📋 *Spreadsheet Synced from Telegram Reply:*\n"
            + "\n".join(all_action_notes)
            + f"\n\n💰 *Bankroll:* {bankroll_str} | {avail_str} | *Active Tickets:* {len(pending_rows)}"
        )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": reply_text, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"Synced {len(all_action_notes)} action(s) from Telegram to Google Sheet.", flush=True)


def main():
    if "--watch" in sys.argv:
        print("Starting real-time Telegram-to-Spreadsheet Watcher daemon...", flush=True)
        while True:
            try:
                process_once(poll_timeout=15)
            except Exception as exc:
                print(f"Watcher cycle warning: {exc}", flush=True)
            time.sleep(3)
    else:
        process_once(poll_timeout=0)


if __name__ == "__main__":
    main()
