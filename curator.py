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


def read_google_doc_text(docs_service, doc_id, today_str=None):
    doc = docs_service.documents().get(documentId=doc_id).execute()
    chunks = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if para:
            text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
            chunks.append(text)
    full_text = "".join(chunks)
    if today_str and f"## {today_str} Slate Intel" in full_text:
        start_pos = full_text.index(f"## {today_str} Slate Intel")
        return full_text[start_pos:]
    return full_text


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
    max_exposure_cap = round(settled_bankroll * 0.50, 2)
    remaining_50pct_cap = max(0.0, round(max_exposure_cap - pending_total, 2))

    if settled_bankroll < 50.0:
        active_tier = "Survival Tier (< $50.00 | 1U = $2.00 | 100% Straight Bets Only, Zero Parlays)"
    elif settled_bankroll < 250.0:
        active_tier = "Tier 1: Foundation ($50.00–$249.99 | 1U = $4.00 | Max 2-Leg Correlated SGP/Parlay)"
    elif settled_bankroll < 500.0:
        active_tier = "Tier 2: Acceleration ($250.00–$499.99 | 1U = $10.00 | Max 2-Leg Correlated SGP/Parlay)"
    elif settled_bankroll < 750.0:
        active_tier = "Tier 3: Expansion ($500.00–$749.99 | 1U = $20.00 | Max 2-Leg Correlated SGP/Parlay)"
    else:
        active_tier = "Tier 4: Final Sprint ($750.00+ | 1U = $30.00 | Max 2-Leg Correlated SGP/Parlay)"

    verified_bankroll_banner = (
        f"VERIFIED LEDGER NUMBERS (USE EXACTLY THESE IN OUTPUT HEADER):\n"
        f"- Settled Bankroll: ${settled_bankroll:.2f}\n"
        f"- Active Tier: {active_tier}\n"
        f"- Pending Wagers Total: ${pending_total:.2f} ({len(pending_lines)} active ticket(s))\n"
        f"- Available Unstaked Bankroll: ${available_bankroll:.2f}\n"
        f"- 50% Max Active Exposure Cap: ${max_exposure_cap:.2f} (Remaining 50% Exposure Headroom: ${remaining_50pct_cap:.2f})\n"
        f"- Active Pending Tickets:\n"
        + ("\n".join(pending_lines) if pending_lines else "  * None")
    )

    summary_block = verified_bankroll_banner + "\n\n" + "\n".join([" | ".join(r[:20]) for r in tracker_rows[:12]])
    non_empty_rows = [r for r in tracker_rows[header_idx + 1 :] if r and r[0].strip()]
    recent_bets = "\n".join([" | ".join(r[:20]) for r in non_empty_rows[-15:]])
    rules_text = "\n".join([" | ".join(r[:8]) for r in rules_rows])
    audit_text = "\n".join([" | ".join(r[:8]) for r in audit_rows])

    ledger_meta = {
        "settled": settled_bankroll,
        "pending_total": pending_total,
        "available": available_bankroll,
        "max_cap": max_exposure_cap,
        "remaining_cap": remaining_50pct_cap,
        "pending_lines": pending_lines,
    }
    return summary_block, recent_bets, rules_text, audit_text, ledger_meta


def run_argon_curation(today_str, summary_block, recent_bets, rules_text, audit_text, intel_text, ledger_meta=None):
    ledger_meta = ledger_meta or {"settled": 20.0, "pending_total": 0.0, "available": 20.0, "max_cap": 10.0, "remaining_cap": 10.0, "pending_lines": []}
    remaining_cap = ledger_meta["remaining_cap"]
    available_bankroll = ledger_meta["available"]

    # Deterministic 50% Exposure Cap Guardrail:
    # If active slate is already at/near the 50% cap (e.g., 5:30 PM Primetime Refresh after morning bets are placed),
    # switch into PRE-TIPOFF ACTIVE-TICKET RISK & INJURY AUDIT MODE instead of recommending 4 extra bets!
    if remaining_cap < 2.00:
        if not ledger_meta["pending_lines"]:
            return (
                f"🎯 *{today_str} Hard Rock Bet Slate*\n"
                f"Bankroll: ${ledger_meta['settled']:.2f} | Pending: $0.00 | Available: ${available_bankroll:.2f}\n\n"
                f"• Holding — remaining exposure cap (${remaining_cap:.2f}) is below minimum $2.00 stake."
            )
        return run_active_ticket_audit(today_str, summary_block, intel_text, ledger_meta)

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    # Simultaneous Multi-Bet Fractional Kelly Stakes sized strictly against remaining_cap
    max_new_budget = round(min(available_bankroll, remaining_cap), 2)
    stake_a_plus = max(1.50, round(max_new_budget * 0.32, 2))
    stake_a_core = max(1.50, round(max_new_budget * 0.25, 2))
    stake_b_prop = max(1.50, round(max_new_budget * 0.21, 2))

    prompt = f"""
You are the Argon Quantitative Sports Betting Engine driving a rapid-compounding $100 to $1,000 Hard Rock Bet Challenge using Simultaneous Multi-Bet Fractional Kelly staking and Top-Down Sharp Devigging.
Today's Date: {today_str}

### 1. LIVE BANKROLL & RECENT LEDGER
{summary_block}

Recent Bets:
{recent_bets}

### 2. UNIT & TIER RULES + HISTORICAL MISTAKE AUDIT
{rules_text}

{audit_text}

### 3. SCRAPED SLATE INTEL (POWER-DEVIGGED +EV% + GOALIE MISMATCHES + B2B FATIGUE + BPI/FPI + PROP STEAM + INJURIES)
{intel_text[:32000]}

### 4. INSTITUTIONAL QUANTITATIVE STRATEGY ENGINE (7 HARD MANDATES FROM EMPIRICAL LEDGER AUDIT):
1. **MANDATORY DAILY DEPLOYMENT:** Because Available Unstaked Bankroll is **${available_bankroll:.2f}** (>= $2.00), you **MUST recommend 3 to 4 concrete Best EV Plays** from today's scraped slate. NEVER output "Holding" or "None".
2. **100% STRAIGHT-BET COMPOUNDING ENGINE (ZERO SGPs / ZERO PARLAYS):**
   - Historical ledger audit proved SGPs (14.3% win rate, -74.6% ROI) and Parlays (16.7% win rate, -44.7% ROI) lose to synthetic vig compounding (~22% house hold on SGPs), while Sweet-Spot Straights & Props win at **71.4% (+37.69 net)**.
   - Recommend **100% Straight Bets** (Moneyline, Spread, Total, 1st-Half, or Straight Player Prop).
3. **THE "-6.5 FAVORITE SPREAD CAP" (ANTI-GARBAGE-TIME BACKDOOR RULE):**
   - Ledger audit showed 0W-4L (-$40.00) on big favorite spreads (-7.5 to -16.5) due to 4th-quarter garbage-time benching.
   - **NEVER recommend laying more than -6.5 points on a full-game favorite spread** (-7.5 max in college). If a matchup has a heavy favorite (-8.5 to -16.5) with a strong fatigue or talent edge (e.g., resting home team vs B2B opponent), pivot to a **Player Prop**, **1st-Half Spread**, **Game Total**, or skip the full-game spread!
4. **THE "-155 TO +135 ODDS SWEET-SPOT BAND":**
   - Restrict all recommended straight plays to American odds between **`-155` and `+135`** (`42.5%` to `60.8%` implied win probability).
   - Never recommend low-probability `+175+` longshot moneylines (eliminates Favorite-Longshot bias traps) and never lay `-175+` heavy juice.
5. **MULTI-SIGNAL CONFLUENCE PRIORITY (RANKING HIERARCHY):**
   - **Priority 1 (`[🏆 A+ CONFLUENCE]` & `[🔥 STALE LINE]` & `[+EV Consensus]`):** Games where 2+ independent quantitative signals align (e.g., BPI/FPI +EV + Sharp Steam, or B2B Fatigue + Steam) or Hard Rock lags behind Pinnacle/FanDuel/DraftKings.
   - **Priority 2 (`[🎯 GOALIE MISMATCH EDGE]` & `[⚠️ FATIGUE EDGE]`):** NHL starting goalie SV% differential >= .025 (e.g., .920+ SV% starter vs sub-.885 SV% starter at playable ML odds) or fading a team on a 0-day-rest Back-to-Back.
   - **Priority 3 (`[📊 BPI/FPI +EV (Sweet-Spot)]` & `[PROP STEAM]`):** Sweet-spot moneylines/spreads (-155 to +135) where BPI/FPI beats Power-Devigged market probability by 4%+ without fighting reverse steam, plus player props that steamed >= 1.0 full point from open to current.
6. **STRICT 1-BET-PER-GAME DIVERSIFICATION:**
   - Every recommended play on today's slip MUST come from a **different game/matchup**. Never place 2 tickets in the same game (prevents correlated downside).
7. **SIMULTANEOUS MULTI-BET KELLY SIZING (EXACT PRE-COMPUTED STAKES):**
   - Size your 3 to 4 plays using these exact pre-computed Simultaneous Fractional Kelly tiers (keeping total new deployment <= **${max_new_budget:.2f}**):
     * **Play #1 (A+ Confluence / Top Edge):** **${stake_a_plus:.2f}**
     * **Play #2 (A Core Edge / Goalie or BPI Mismatch):** **${stake_a_core:.2f}**
     * **Play #3 (B+ Core / Steamed Player Prop):** **${stake_b_prop:.2f}**
     * **Play #4 (Optional B+ Steamed Player Prop / Straight):** **${stake_b_prop:.2f}**
8. **LINEUP GUARDRAIL:** If any recommended play involves a key player listed as `Questionable` or `Day-To-Day` on the ESPN injury feed, append `[WAIT FOR LINEUP]`.

### STRICT TELEGRAM FORMATTING RULES (CLEAN, COMPACT & SCANNABLE):
- Output ONLY the compact template below. NO extra paragraphs, NO UTC timestamps, NO "Leg 1 / Leg 2" prefixes.
- Use short team/player names.

🎯 *{today_str} Hard Rock Bet Slate*
Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX

*Pending ($XX.XX):*
• *None* (or list active pending BET-XXX rows if any)

*Today's Best EV Plays:*
1. *[Straight] Selection (Odds)* — *$X.XX*
   - Edge: [1 short line: Confluence signals, goalie SV%/GAA delta, BPI/FPI Power-NoVig EV%, B2B fatigue, or prop steam]
2. *[Straight] Selection (Odds)* — *$X.XX*
   - Edge: [1 short line: Confluence signals, goalie SV%/GAA delta, BPI/FPI Power-NoVig EV%, B2B fatigue, or prop steam]
3. *[Straight] Selection (Odds)* — *$X.XX*
   - Edge: [1 short line: Confluence signals, goalie SV%/GAA delta, BPI/FPI Power-NoVig EV%, B2B fatigue, or prop steam]
4. *[Straight] Selection (Odds)* — *$X.XX*
   - Edge: [1 short line: Confluence signals, goalie SV%/GAA delta, BPI/FPI Power-NoVig EV%, B2B fatigue, or prop steam]
"""
    candidate_models = [
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
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
                txt = response.text.strip()
                if available_bankroll >= 2.0 and ("Holding —" in txt or "1." not in txt):
                    print(f"Model {model_id} returned a Holding stub despite ${available_bankroll:.2f} available; retrying next model...")
                    continue
                return txt
        except Exception as exc:
            print(f"Model {model_id} skipped: {exc}")
    raise RuntimeError("All discovered Gemini models failed.")


def run_active_ticket_audit(today_str, summary_block, intel_text, ledger_meta):
    """
    Primetime Pre-Tipoff Risk & Injury Audit Mode:
    Triggered automatically when the 50% exposure cap is already deployed.
    Scrubs fresh 5:00 PM ESPN injuries, breaking news, NHL goalies, and afternoon sharp line movement
    specifically for the user's locked-in pending tickets to flag HOLD / WATCH / CASH OUT + 1 Pivot Play.
    """
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    prompt = f"""
You are the Argon Pre-Tipoff Risk & Lineup Auditor.
The user has ALREADY locked in their bets for today ({today_str}) and deployed their 50% exposure cap.
Do NOT recommend adding 4 extra bets on top of their existing slate!

Instead, scrub the freshly scraped 5:00 PM Slate Intel (ESPN injury reports, breaking news, NHL starting goalies, and current line/prop movement) specifically for EACH of the user's Active Pending Tickets below.

### 1. USER'S ACTIVE PENDING TICKETS
{summary_block}

### 2. FRESH 5:00 PM SLATE INTEL (ODDS MOVEMENT + PROPS + ESPN NEWS & INJURIES)
{intel_text[:32000]}

### AUDIT RULES FOR EACH PENDING TICKET (WITH CASHOUT PRICING & LINE-MOVEMENT MATH):
- Remember how sportsbook cashouts work:
  * If the line moved **AGAINST** our ticket (negative CLV, e.g. `+130 -> +150`), Hard Rock Bet drops the cashout offer **below our original stake** (charging both the line move AND an extra ~8%–12% cashout vig haircut). NEVER recommend cashing out solely for a minor adverse line move!
  * If the line moved **IN OUR FAVOR** (positive CLV, e.g. `+120 -> -132`), Hard Rock may offer a small profit cashout, but it still taxes our true EV — **HOLD** to capture the full edge!
  * ONLY recommend 🔴 **CASH OUT ALERT** when:
    1. A major late **injury scratch / starting goalie change** craters the ticket's true win probability by more than the cashout penalty (damage-control exit), OR
    2. The line has **not moved against us** (so Hard Rock still offers a **100% full-stake refund** with $0 penalty) AND a clearly superior A+ play opened up.
- Assign one of three exact statuses to EVERY pending ticket:
  * 🟢 **HOLD** — No negative injury news, lineup confirmed/healthy, or line moved in our favor (positive CLV).
  * 🟡 **WATCH** — Key player/teammate is Questionable/Day-To-Day, or minor adverse line move (note that cashout will be discounted below stake, so hold unless injury news worsens).
  * 🔴 **CASH OUT ALERT** — Player ruled OUT/Doubtful, goalie scratched, or major injury damage-control exit needed (or $0-penalty full-refund swap).
- If ANY ticket is marked 🔴 **CASH OUT ALERT**, include a single **1-Play Backup Pivot** at the bottom. Otherwise, confirm **"All Clear — Let Your Active Slate Ride."**

### STRICT TELEGRAM FORMATTING (COMPACT & SCANNABLE):
🛡️ *{today_str} Primetime Pre-Tipoff Audit*
Bankroll: ${ledger_meta['settled']:.2f} | Pending: ${ledger_meta['pending_total']:.2f} | Available: ${ledger_meta['available']:.2f}

*Active Ticket Check:*
• 🟢/🟡/🔴 *BET-XXX* — [Short Pick] ([Odds])
  - Status: [1 crisp line: latest injury status, goalie update, or current line vs our locked odds]

*Action Plan:*
• [Either "All Clear — Hold all active tickets." OR "If Hard Rock offers full refund on BET-XXX, pivot to: [1 single best replacement play + edge]"]
"""
    for model_id in [
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
    ]:
        try:
            resp = client.models.generate_content(model=model_id, contents=prompt)
            if resp and resp.text:
                return resp.text.strip()
        except Exception as exc:
            print(f"Audit model {model_id} skipped: {exc}")

    p_formatted = [line.replace("- ", "• ") for line in ledger_meta["pending_lines"]]
    return (
        f"🛡️ *{today_str} Primetime Pre-Tipoff Audit*\n"
        f"Bankroll: ${ledger_meta['settled']:.2f} | Pending: ${ledger_meta['pending_total']:.2f} | Available: ${ledger_meta['available']:.2f}\n\n"
        + "\n".join(p_formatted)
        + "\n\n• 50% exposure cap already deployed — hold active tickets."
    )


def append_to_curator_feed(gc, today_str, curated_message):
    sh = open_tracker_workbook(gc)
    feed_ws = sh.worksheet("Curator Feed")
    est_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5)))
    ts_label = est_now.strftime("%Y-%m-%d %H:%M EST")
    status_tag = "SENT_PRIMETIME_AUDIT" if est_now.hour >= 16 else "SENT_MORNING"
    feed_ws.append_row([
        today_str,
        ts_label,
        "Argon Daily Curation Summary",
        "Daily Brief",
        "-",
        "-",
        "-",
        "-",
        "-",
        "-",
        status_tag,
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


def record_pretipoff_clv(gc, intel_text):
    """
    Compares each active Pending bet's locked-in odds against fresh pre-tipoff lines in intel_text
    and appends '| [CLV: +X.X% | Close: ...]' into Column O (Notes) once per ticket.
    """
    try:
        sh = open_tracker_workbook(gc)
        ws = sh.worksheets()[0]
        all_vals = ws.get_all_values()
        header_idx = next((i for i, r in enumerate(all_vals) if "Bet ID" in r and "Result" in r), 15)

        targets = []
        for idx in range(header_idx + 1, len(all_vals)):
            r = all_vals[idx]
            if len(r) > 10 and r[0].strip().startswith("BET-") and r[10].strip().lower() == "pending":
                existing_note = r[14] if len(r) > 14 else ""
                if "[CLV:" in existing_note:
                    continue
                targets.append({
                    "row_index": idx + 1,
                    "bet_id": r[0].strip(),
                    "selections": r[5].strip() if len(r) > 5 else "",
                    "locked_odds": r[6].strip() if len(r) > 6 else "",
                    "existing_note": existing_note,
                })

        if not targets:
            return

        client = genai.Client(api_key=GEMINI_API_KEY)
        clv_prompt = f"""
For each pending bet below, find the current pre-tipoff market odds in the Fresh Slate Intel.
Compute CLV% = ((implied_prob(current_odds) / implied_prob(locked_odds)) - 1) * 100 (approximate implied probability edge gained or lost from line movement).
If a bet's current line cannot be found in the intel, omit it.

PENDING BETS:
{json.dumps(targets, indent=2)}

FRESH SLATE INTEL:
{intel_text[:28000]}

Respond ONLY with valid JSON:
{{
  "clv_records": [
    {{
      "row_index": 63,
      "bet_id": "BET-046",
      "close_odds": "-132",
      "clv_pct": 11.2
    }}
  ]
}}
"""
        for model_id in ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]:
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=clv_prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
                )
                if resp and resp.text:
                    records = json.loads(resp.text).get("clv_records", [])
                    row_map = {t["row_index"]: t for t in targets}
                    for rec in records:
                        r_i = rec.get("row_index")
                        clv_pct = rec.get("clv_pct")
                        close_odds = rec.get("close_odds", "")
                        if r_i in row_map and clv_pct is not None and close_odds:
                            base_note = row_map[r_i]["existing_note"]
                            clv_tag = f"[CLV: {float(clv_pct):+.1f}% | Close: {close_odds}]"
                            new_note = f"{base_note} | {clv_tag}".strip(" |")
                            ws.update_acell(f"O{r_i}", new_note)
                            print(f"Recorded CLV for {row_map[r_i]['bet_id']}: {clv_tag}")
                    return
            except Exception:
                continue
    except Exception as exc:
        print(f"CLV logging warning: {exc}")


def main():
    est_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5)))
    today_str = est_now.strftime("%Y-%m-%d")
    gc, docs_service = get_google_clients()
    summary_block, recent_bets, rules_text, audit_text, ledger_meta = read_tracker_context(gc)
    intel_text = read_google_doc_text(docs_service, INTEL_DOC_ID, today_str=today_str) if INTEL_DOC_ID else "No Intel Doc ID set."
    if ledger_meta.get("pending_lines"):
        record_pretipoff_clv(gc, intel_text)
    curated_message = run_argon_curation(
        today_str, summary_block, recent_bets, rules_text, audit_text, intel_text, ledger_meta=ledger_meta
    )
    append_to_curator_feed(gc, today_str, curated_message)
    send_telegram_alert(curated_message)


if __name__ == "__main__":
    main()
