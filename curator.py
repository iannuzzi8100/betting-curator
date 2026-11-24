import os
import re
import json
import time
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
        rest = full_text[start_pos:]
        next_sec = re.search(r"\n## \d{4}-\d{2}-\d{2} Slate Intel", rest[1:])
        if next_sec:
            return rest[: next_sec.start() + 1]
        return rest
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

    def _bet_num(row):
        try:
            return int(str(row[0]).strip().split("-")[1])
        except Exception:
            return 0

    bet_rows_chrono = sorted(
        [r for r in tracker_rows[header_idx + 1 :] if r and r[0].strip().startswith("BET-")],
        key=_bet_num,
    )
    for r in bet_rows_chrono:
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
    recent_bets = "\n".join([" | ".join(r[:20]) for r in bet_rows_chrono[-15:]])
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
        http_options=types.HttpOptions(api_version="v1beta", timeout=45_000),
    )
    # Confidence Meter Bankroll Allocation Tiers (Single-Bet Window vs. Same-Window Multi-Bet Split):
    settled_bankroll = ledger_meta["settled"]
    stake_t1_solo = round(min(available_bankroll, max(2.00, settled_bankroll * 0.50)), 2)   # 88-99% Tier 1 Solo: 50% BR
    stake_t2_solo = round(min(available_bankroll, max(2.00, settled_bankroll * 0.35)), 2)   # 78-87% Tier 2 Solo: 35% BR
    stake_t2_dual = round(min(available_bankroll / 2.0, max(2.00, settled_bankroll * 0.25)), 2)  # Two 78-87% in same window: 25% BR each (50% total)
    stake_t3_solo = round(min(available_bankroll, max(2.00, settled_bankroll * 0.20)), 2)   # 68-77% Tier 3 Solo: 20% BR
    stake_t3_dual = round(min(available_bankroll / 2.0, max(2.00, settled_bankroll * 0.18)), 2)  # Two 68-77% in same window: 18% BR each (36% total)

    # Detect Weekend Football + European Soccer Mode automatically from scraped intel / day of week
    est_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5)))
    dow = est_now.weekday()  # 5 = Saturday, 6 = Sunday
    if dow == 5 and ("SATURDAY FOOTBALL + EPL MODE ACTIVE" in intel_text or "SATURDAY FOOTBALL + SOCCER MODE ACTIVE" in intel_text or "[CFB QUANT]" in intel_text):
        league_rule = (
            "**SATURDAY FOOTBALL + EUROPEAN SOCCER ONLY (STRICT):** Today is Saturday during College Football season. "
            "Pick **SOLELY from College Football (CFB) and European Soccer (EPL, UCL, LALIGA)**. "
            "Do NOT recommend NBA, NHL, NCAAM, or any other sport today. "
            "Cover every remaining upcoming Time-Slot Window in chronological order:\n"
            "     • **Window 1 — Early Morning (7:30–9:30 AM EPL/LALIGA):** 7:30 AM – 9:30 AM EST\n"
            "     • **Window 2 — Late Morning (10:00–11:30 AM EPL/LALIGA):** 9:45 AM – 11:45 AM EST\n"
            "     • **Window 3 — Noon / Early Afternoon (12:00–2:30 PM CFB/EPL/LALIGA/UCL):** 11:45 AM – 2:45 PM EST\n"
            "     • **Window 4 — Late Afternoon (3:00–5:30 PM CFB/LALIGA/UCL):** 2:45 PM – 6:15 PM EST\n"
            "     • **Window 5 — Primetime (6:30–9:00 PM CFB):** 6:15 PM – 9:15 PM EST\n"
            "     • **Window 6 — Late Night (9:30 PM+ CFB):** 9:15 PM – 11:30 PM EST"
        )
        slate_header_tag = "Saturday CFB + European Soccer Confidence Meter Slate"
    elif dow == 6 and ("SUNDAY FOOTBALL + EPL MODE ACTIVE" in intel_text or "SUNDAY FOOTBALL + SOCCER MODE ACTIVE" in intel_text or "[NFL QUANT]" in intel_text):
        league_rule = (
            "**SUNDAY FOOTBALL + EUROPEAN SOCCER ONLY (STRICT):** Today is Sunday during NFL season. "
            "Pick **SOLELY from NFL and European Soccer (EPL, UCL, LALIGA)**. "
            "Do NOT recommend NBA, NHL, NCAAM, or any other sport today. "
            "Cover every remaining upcoming Time-Slot Window in chronological order:\n"
            "     • **Window 1 — Morning (8:00–11:30 AM EPL/LALIGA):** 8:00 AM – 11:45 AM EST\n"
            "     • **Window 2 — Early Afternoon (1:00 PM NFL / 12:30–3:00 PM LALIGA/EPL):** 12:00 PM – 3:45 PM EST\n"
            "     • **Window 3 — Late Afternoon (4:05 / 4:25 PM NFL):** 4:00 PM – 7:30 PM EST\n"
            "     • **Window 4 — Primetime (8:20 PM NFL SNF):** 8:00 PM – 11:30 PM EST"
        )
        slate_header_tag = "Sunday NFL + European Soccer Confidence Meter Slate"
    else:
        league_rule = (
            "**ALL-LEAGUE BEST-EV TIME-SLOT MODE:** Pick the highest-conviction plays across any active league "
            "(NBA, NHL, CFB, NFL, NCAAM, EPL, UCL, LALIGA, MLB) — staggered across every remaining ~3-hour betting window "
            "so each window settles and compounds the resulting bankroll into the next window. "
            "Use exact league tags `UCL` for UEFA Champions League and `LALIGA` for Spanish La Liga."
        )
        slate_header_tag = "Confidence Meter Time-Slot Slate"

    prompt = f"""
You are the Argon Quantitative Sports Betting Engine driving a rapid-compounding $100 to $1,000 Hard Rock Bet Challenge using our **Deterministic 0–100 Confidence Meter** and **Sequential Time-Slot Compounding**.
Today's Date: {today_str} | Current Time: {est_now.strftime('%I:%M %p EST').lstrip('0')}

### 1. LIVE BANKROLL & RECENT LEDGER
{summary_block}

Recent Bets:
{recent_bets}

### 2. UNIT & TIER RULES + HISTORICAL MISTAKE AUDIT
{rules_text}

{audit_text}

### 3. SCRAPED SLATE INTEL (INCLUDES DETERMINISTIC `[CONFIDENCE METER: XX% | ████████░░ | TIER ...]` TAGS)
{intel_text[:95000]}

### 4. CONFIDENCE METER & SEQUENTIAL TIME-SLOT COMPOUNDING RULES (9 HARD MANDATES):
1. **USE THE PRE-COMPUTED `[CONFIDENCE METER: XX% ...]` SCORES FROM SECTION 3:**
   - Every matchup in Section 3 has a deterministic Python-computed `[CONFIDENCE METER: XX% | ... | TIER ... -> Top Play: ...]` tag.
   - Use those exact Confidence Meter scores and factor breakdowns to rank and select plays for **EVERY** remaining upcoming Time-Slot Window (strictly after `{est_now.strftime('%I:%M %p EST').lstrip('0')}` — including Late Morning 10:00–11:45 AM, Early Afternoon 12:00–2:45 PM, Late Afternoon 3:00–6:00 PM, Primetime 6:30–9:00 PM, and Late Night 9:30 PM+).
   - {league_rule}
2. **CONFIDENCE METER BANKROLL SIZING & SAME-WINDOW MULTI-BET RULES (CURRENT BR = ${settled_bankroll:.2f}):**
   - **STRICT MAX 2 PLAYS PER TIME-SLOT WINDOW:** Pick the single best play (or at most the top 2 highest-confidence plays) per time-slot window. NEVER stack 3–5 plays in one window and NEVER write `[Included in Hammer Slot]` — every play MUST have an explicit `$XX.XX (XX% BR)` wager!
   - **🔥 TIER 1: MAX HAMMER (`88% – 99%` Confidence):**
     * **Standout Solo Play:** Wager **50% of Bankroll = ${stake_t1_solo:.2f}** (or `50% of Resulting BR (Est. ${stake_t1_solo:.2f})` for later windows).
     * *Multi-Bet Rule:* Put the full 50% on this standout lock unless a 2nd game in the exact same time window is `82%+`, in which case split **35% (${stake_t2_solo:.2f}) / 20% (${stake_t3_solo:.2f})**.
   - **💎 TIER 2: STRONG CONVICTION (`78% – 87%` Confidence):**
     * **Solo Play in Window:** Wager **35% of Bankroll = ${stake_t2_solo:.2f}** (or `35% of Resulting BR (Est. ${stake_t2_solo:.2f})` for later windows).
     * **Dual-Bet Same Window:** If **2 separate games in the same window** both score **`78% – 87%`**, recommend **BOTH at 25% of Bankroll each = ${stake_t2_dual:.2f} each** (`50%` total window allocation).
   - **✅ TIER 3: SOLID EDGE (`68% – 77%` Confidence):**
     * **Solo Play in Window:** Wager **20% of Bankroll = ${stake_t3_solo:.2f}** (or `20% of Resulting BR (Est. ${stake_t3_solo:.2f})` for later windows).
     * **Dual-Bet Same Window:** If **2 separate games in the same window** both score **`68% – 77%`**, recommend **BOTH at 18% of Bankroll each = ${stake_t3_dual:.2f} each** (`36%` total window allocation).
   - **⛔ TIER 4: PASS / NO EDGE (`Below 68%` Confidence):**
     * **0% Wager ($0.00):** If a time-slot window has NO game scoring `>= 68%`, explicitly output:
       `*[Window Time EST] — ⛔ PASS (<68% Confidence — Hold bankroll for next window)*`.
3. **100% STRAIGHT-BET COMPOUNDING ENGINE (ZERO SGPs / ZERO PARLAYS):**
   - Recommend **100% Straight Bets** (Moneyline, Spread, Total, 1st-Half, or Straight Player Prop). Never recommend SGPs or Parlays.
4. **MODERATE FAVORITE PRIORITY & "-155 TO +130" SWEET-SPOT BAND:**
   - Strongly prioritize Moderate Favorites (`-110` to `-150` ML or `-1.5` to `-6.5` spreads; `-7.5` max in CFB) which are `11W-3L (78.6%)` in our ledger audit.
   - Restrict all plays to **`-155` to `+130`**. Never lay `-175+` heavy juice and never chase `+150+` longshots.
5. **THE "-6.5 FAVORITE SPREAD CAP" (`-7.5` MAX IN CFB):**
   - Never lay more than `-6.5` points (`-7.5` in CFB) on a full-game spread.
6. **NEVER CHASE LATE STEAM:**
   - Strictly ignore any side or total tagged `[⚠️ ... ALREADY STEAMED — DO NOT CHASE LATE]`.
7. **STRICT 1-BET-PER-GAME DIVERSIFICATION:**
   - Even when placing 2 dual bets in the same time window, they MUST come from **2 different games**.
8. **STRICT FACTUAL GROUNDING (ZERO HALLUCINATIONS):**
   - ONLY cite odds, records, L5 form, FPI/BPI numbers, and Confidence Meter scores that appear **verbatim** in Section 3 above.
9. **LINEUP GUARDRAIL:** If any recommended play involves a key player listed as `Questionable` or `Day-To-Day`, append `[WAIT FOR LINEUP]`.

### STRICT FORMATTING RULES (STREAMLINED HEADLINE + EXPANDABLE DRAWER DATA):
- Output ONLY the template below. NO extra paragraphs, NO UTC timestamps.
- **EXACT GAME START TIME ONLY:** On every numbered play, use the game's **EXACT start time in EST** from the `(H:MM PM EST)` tag in Section 3 (e.g. `[2:00 PM EST]`, `[2:30 PM EST]`, `[7:00 PM EST]`). **NEVER** use a generic window bucket label like `[Window 3: 12:00 PM]` or `[Window 4: 3:00 PM]` in place of the game's actual start time.
- On each numbered headline (`1.`, `2.`, etc.), show ONLY: **Exact Time EST • Game (League) — Bet (Odds) — Wager — Confidence %**.
- Underneath each numbered headline, include the two structured drawer lines (`- Game & Line:` and `- Confidence Math:`) so the web app can display them ONLY when the user clicks on the bet!

🎯 *{today_str} {slate_header_tag}*
Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX

*Pending ($XX.XX):*
• *None* (or list active pending BET-XXX rows if any)

*Today's Curated Plays:*
1. *[Exact Tipoff/Kickoff Time EST] • [Away @ Home] ([League])* — *[Bet & Odds]* — *[$XX.XX (XX% BR)]* — *[XX% 🔥/💎/✅]*
   - Game & Line: [Records, Full Spread / ML / Total O/U, L5 Form & avg margin, ESPN BPI/FPI Win Prob, and any line steam from Section 3]
   - Confidence Math: [Base 50% + exact comma-separated Pillar points from Section 3 = XX% (Tier Name)]
2. *[Exact Tipoff/Kickoff Time EST] • [Away @ Home] ([League])* — *[Bet & Odds]* — *[XX% of Resulting BR (Est. $XX.XX)]* — *[XX% 🔥/💎/✅]*
   - Game & Line: [Records, Full Spread / ML / Total O/U, L5 Form & avg margin, ESPN BPI/FPI Win Prob, and any line steam from Section 3]
   - Confidence Math: [Base 50% + exact comma-separated Pillar points from Section 3 = XX% (Tier Name)]
(Continue in chronological order for every remaining time-slot window today; if a window has no 68%+ play, list `X. *[Window Time EST] — ⛔ PASS (<68% Confidence)*`)
"""
    candidate_models = [
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    skip_kw = ("embedding", "image", "audio", "tts", "pro", "thinking", "deep-research", "robotics", "computer-use", "veo", "imagen", "learnlm", "2.5", "2.0", "1.5")
    try:
        live_models = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
            and "gemini" in m.name.lower()
            and "flash" in m.name.lower()
            and not any(k in m.name.lower() for k in skip_kw)
        ]
        ordered = []
        for m in candidate_models:
            if m in live_models and m not in ordered:
                ordered.append(m)
        for m in live_models:
            if m not in ordered:
                ordered.append(m)
        if ordered:
            candidate_models = ordered[:8]
    except Exception as list_exc:
        print(f"Model listing warning: {list_exc}")

    model_errors = []
    for round_idx in range(2):
        if round_idx == 1:
            print("Retrying Gemini models after brief 4s backoff for transient 503/504 spikes...", flush=True)
            time.sleep(4)
        for model_id in candidate_models:
            try:
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                )
                if response and response.text:
                    txt = response.text.strip()
                    if available_bankroll >= 2.0 and ("Holding —" in txt or "1." not in txt):
                        msg = f"{model_id}: returned stub without numbered plays ({txt[:80]})"
                        print(msg)
                        model_errors.append(msg)
                        continue
                    return txt
            except Exception as exc:
                err_s = f"{model_id}: {type(exc).__name__}: {str(exc)[:140]}"
                print(f"Model {err_s}")
                model_errors.append(err_s)

    print(
        f"::warning::All Gemini models unavailable ({' | '.join(model_errors[:4])}); falling back to deterministic Confidence Meter slate.",
        flush=True,
    )
    return _build_deterministic_curated_slate(
        today_str=today_str,
        slate_header_tag=slate_header_tag,
        intel_text=intel_text,
        ledger_meta=ledger_meta,
        est_now=est_now,
        dow=dow,
    )


def _build_deterministic_curated_slate(today_str, slate_header_tag, intel_text, ledger_meta, est_now, dow):
    """
    Deterministic fallback curator when Gemini API experiences 503/504 high-demand outages.
    Parses the Python-computed [CONFIDENCE METER: XX% ...] quant lines from today's scraped Intel Doc
    and formats them identically to Gemini's output so the dashboard and Telegram never miss a slate.
    """
    settled_br = float(ledger_meta.get("settled", 0.0) or 0.0)
    pending_tot = float(ledger_meta.get("pending_total", 0.0) or 0.0)
    avail_br = float(ledger_meta.get("available", 0.0) or 0.0)
    pending_lines = ledger_meta.get("pending_lines") or []

    allowed_leagues = None
    if dow == 5 and ("SATURDAY FOOTBALL + EPL MODE ACTIVE" in intel_text or "SATURDAY FOOTBALL + SOCCER MODE ACTIVE" in intel_text or "[CFB QUANT]" in intel_text):
        allowed_leagues = {"CFB", "EPL", "UCL", "LALIGA"}
    elif dow == 6 and ("SUNDAY FOOTBALL + EPL MODE ACTIVE" in intel_text or "SUNDAY FOOTBALL + SOCCER MODE ACTIVE" in intel_text or "[NFL QUANT]" in intel_text):
        allowed_leagues = {"NFL", "EPL", "UCL", "LALIGA"}

    def _time_to_min(t_str):
        m_t = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", t_str or "", flags=re.I)
        if not m_t:
            return 24 * 60
        hr = int(m_t.group(1))
        mn = int(m_t.group(2))
        ap = m_t.group(3).upper()
        if ap == "PM" and hr != 12:
            hr += 12
        if ap == "AM" and hr == 12:
            hr = 0
        return hr * 60 + mn

    now_min = est_now.hour * 60 + est_now.minute
    candidates = []
    seen_matchups = set()

    for ln in (intel_text or "").splitlines():
        if "[CONFIDENCE METER:" not in ln or "QUANT]" not in ln:
            continue
        if "DO NOT CHASE LATE" in ln:
            continue
        m_hdr = re.match(
            r"^\s*-\s*\[([A-Z0-9]+)\s+QUANT\]\s*\[WINDOW\s*(\d+):[^\]]*\]\s*\(([^)]+)\)\s*(.+?)\s*->\s*(.+)$",
            ln.strip(),
        )
        if not m_hdr:
            continue
        league = m_hdr.group(1).strip().upper()
        if league in ("CBB", "NCAAB"):
            league = "NCAAM"
        if allowed_leagues and league not in allowed_leagues:
            continue
        win_num = int(m_hdr.group(2))
        time_est = m_hdr.group(3).strip()
        matchup_and_rec = m_hdr.group(4).strip()
        rest_body = m_hdr.group(5).strip()

        m_cm = re.search(
            r"\[CONFIDENCE METER:\s*(\d{2,3})%\s*\|[^|]+\|\s*([^->]+?)\s*->\s*Top Play:\s*([^\]]+)\]",
            rest_body,
        )
        if not m_cm:
            continue
        conf_pct = int(m_cm.group(1))
        if conf_pct < 68:
            continue
        tier_txt = m_cm.group(2).strip()
        top_play_full = m_cm.group(3).strip()

        # Split Top Play into pick + reasons: "ARS ML (-135) (Base 50%, ...)"
        pick_str = top_play_full
        reasons_str = ""
        m_pr = re.match(r"^(.+?\([+-]\d{3,4}\))\s*\((.+)\)$", top_play_full)
        if m_pr:
            pick_str = m_pr.group(1).strip()
            reasons_str = m_pr.group(2).strip()
        else:
            m_pr2 = re.match(r"^([^(]+?)\s*\((.+)\)$", top_play_full)
            if m_pr2:
                pick_str = m_pr2.group(1).strip()
                reasons_str = m_pr2.group(2).strip()

        # Enforce -155 to +130 odds sweet spot & -6.5 spread cap (-7.5 CFB)
        m_odds = re.search(r"\(([+-]\d{3,4})\)", pick_str)
        if m_odds:
            o_int = int(m_odds.group(1))
            if o_int < -155 or o_int > 130:
                continue
        m_sp = re.search(r"\s(-\d+(?:\.\d+)?)\b", pick_str)
        if m_sp:
            sp_val = float(m_sp.group(1))
            max_lay = -7.5 if league == "CFB" else -6.5
            if sp_val < max_lay:
                continue

        # Clean matchup & expand abbr in pick_str (e.g. "ARS ML" -> "Arsenal ML")
        rec_m = re.search(r"\(([^()]*@[^()]*)\)\s*$", matchup_and_rec)
        matchup = matchup_and_rec[: rec_m.start()].strip() if rec_m else matchup_and_rec
        rec_raw = rec_m.group(1).strip() if rec_m else ""
        if matchup in seen_matchups:
            continue
        seen_matchups.add(matchup)

        if "@" in matchup:
            a_full, h_full = [x.strip() for x in matchup.split("@", 1)]
            if "@" in rec_raw:
                a_rec_part, h_rec_part = [x.strip() for x in rec_raw.split("@", 1)]
                a_ab = a_rec_part.split()[0] if a_rec_part else ""
                h_ab = h_rec_part.split()[0] if h_rec_part else ""
                if a_ab and re.match(rf"^{re.escape(a_ab)}\b", pick_str):
                    pick_str = re.sub(rf"^{re.escape(a_ab)}\b", a_full, pick_str, count=1)
                elif h_ab and re.match(rf"^{re.escape(h_ab)}\b", pick_str):
                    pick_str = re.sub(rf"^{re.escape(h_ab)}\b", h_full, pick_str, count=1)

        t_min = _time_to_min(time_est)
        if t_min < now_min - 5:
            continue

        odds_part = rest_body.split("->")[0].strip()
        game_line = f"{matchup_and_rec} | {odds_part}"
        candidates.append({
            "win_num": win_num,
            "t_min": t_min,
            "time_est": time_est,
            "league": league,
            "matchup": matchup,
            "pick": pick_str,
            "conf_pct": conf_pct,
            "tier": tier_txt,
            "reasons": reasons_str,
            "game_line": game_line,
        })

    # Group by window and keep up to top 2 plays per window (highest confidence)
    by_win = {}
    for c in candidates:
        by_win.setdefault(c["win_num"], []).append(c)

    selected = []
    for w_num in sorted(by_win.keys()):
        w_plays = sorted(by_win[w_num], key=lambda x: (-x["conf_pct"], x["t_min"]))
        if w_plays and w_plays[0]["conf_pct"] >= 88:
            if len(w_plays) >= 2 and w_plays[1]["conf_pct"] >= 82:
                chosen = w_plays[:2]
                pcts = [35, 20]
            else:
                chosen = w_plays[:1]
                pcts = [50]
        elif len(w_plays) >= 2 and w_plays[0]["conf_pct"] >= 78 and w_plays[1]["conf_pct"] >= 78:
            chosen = w_plays[:2]
            pcts = [25, 25]
        elif w_plays and w_plays[0]["conf_pct"] >= 78:
            chosen = w_plays[:1]
            pcts = [35]
        elif len(w_plays) >= 2:
            chosen = w_plays[:2]
            pcts = [18, 18]
        else:
            chosen = w_plays[:1]
            pcts = [20]
        for item, pct in zip(chosen, pcts):
            item["wager_pct"] = pct
            selected.append(item)

    selected.sort(key=lambda x: (x["t_min"], -x["conf_pct"]))

    pend_block = "\n".join(ln.replace("- ", "• ", 1) for ln in pending_lines) if pending_lines else "• *None*"
    out = [
        f"🎯 *{today_str} {slate_header_tag}*",
        f"Bankroll: ${settled_br:.2f} | Pending: ${pending_tot:.2f} | Available: ${avail_br:.2f}",
        "",
        f"*Pending (${pending_tot:.2f}):*",
        pend_block,
        "",
        "*Today's Curated Plays:*",
    ]
    if not selected:
        out.append("1. *[All Windows] — ⛔ PASS (<68% Confidence — Hold bankroll for next window)*")
    else:
        for idx, p in enumerate(selected, start=1):
            stake = round(min(avail_br, max(2.00, settled_br * (p["wager_pct"] / 100.0))), 2)
            icon = "🔥" if p["conf_pct"] >= 88 else ("💎" if p["conf_pct"] >= 78 else "✅")
            out.append(
                f"{idx}. *[{p['time_est']}] • {p['matchup']} ({p['league']})* — *{p['pick']}* — *[${stake:.2f} ({p['wager_pct']}% BR)]* — *[{p['conf_pct']}% {icon}]*"
            )
            out.append(f"   - Game & Line: {p['game_line']}")
            out.append(f"   - Confidence Math: {p['reasons'] or 'Deterministic 6-Pillar Score'} = {p['conf_pct']}% ({p['tier']})")
    return "\n".join(out)


def run_active_ticket_audit(today_str, summary_block, intel_text, ledger_meta):
    """
    Primetime Pre-Tipoff Risk & Injury Audit Mode:
    Triggered automatically when the 50% exposure cap is already deployed.
    Scrubs fresh 5:00 PM ESPN injuries, breaking news, NHL goalies, and afternoon sharp line movement
    specifically for the user's locked-in pending tickets to flag HOLD / WATCH / CASH OUT + 1 Pivot Play.
    """
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta", timeout=45_000),
    )
    prompt = f"""
You are the Argon Pre-Tipoff Risk & Lineup Auditor.
The user has ALREADY locked in their bets for today ({today_str}) and deployed their 50% exposure cap.
Do NOT recommend adding 4 extra bets on top of their existing slate!

Instead, scrub the freshly scraped 5:00 PM Slate Intel (ESPN injury reports, breaking news, NHL starting goalies, and current line/prop movement) specifically for EACH of the user's Active Pending Tickets below.

### 1. USER'S ACTIVE PENDING TICKETS
{summary_block}

### 2. FRESH 5:00 PM SLATE INTEL (ODDS MOVEMENT + PROPS + ESPN NEWS & INJURIES)
{intel_text[:65000]}

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
        "gemini-2.5-flash",
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
        curated_message[:25000],
    ])


def _build_curated_inline_keyboard(message):
    rows = []
    play_idx = 0
    for ln in (message or "").splitlines():
        m = re.match(r"^\s*\d+\.\s*(.+)$", ln)
        if not m:
            continue
        clean = m.group(1).replace("*", "").strip()
        # Keep play_idx aligned with Curator Feed parser, but don't add a Log button for PASS windows
        if "⛔" in clean or re.search(r"\bPASS\b", clean):
            play_idx += 1
            continue
        # Strip leading [Time — Sport] bracket for a crisp iPhone button label
        short_label = re.sub(r"^\[[^\]]+\]\s*", "", clean).strip()
        if len(short_label) > 40:
            short_label = short_label[:37].rstrip() + "..."
        rows.append([{
            "text": f"✅ Log #{play_idx + 1}: {short_label}",
            "callback_data": f"log_curated:{play_idx}",
        }])
        play_idx += 1
        if len(rows) >= 10:
            break
    rows.append([
        {"text": "📊 Live Status", "callback_data": "status"},
        {"text": "📱 Open Dashboard", "url": "https://iannuzzi8100.github.io/betting-curator/"},
    ])
    return {"inline_keyboard": rows}


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Alert text:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    clean_text = (message or "")[:4000]
    keyboard = _build_curated_inline_keyboard(message)
    attempts = [
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": clean_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": clean_text.replace("*", ""),
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": clean_text.replace("*", ""),
            "disable_web_page_preview": True,
        },
    ]
    for idx, payload in enumerate(attempts, start=1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                print(f"Telegram bet slip delivered successfully (attempt {idx}).")
                return
            print(f"Telegram attempt {idx} failed ({resp.status_code}): {resp.text[:300]}")
        except Exception as exc:
            print(f"Telegram attempt {idx} exception: {exc}")
    print("Warning: All Telegram delivery attempts failed; continuing so dashboard still publishes.")


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
{intel_text[:65000]}

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
        for model_id in ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]:
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
    # Strip expandable drawer sub-bullets from Telegram alert so Telegram shows ONLY Time, Game, Bet, Wager, Confidence
    tg_streamlined_lines = [
        ln
        for ln in curated_message.splitlines()
        if not re.match(r"^\s*[-•]\s*(Game & Line|Confidence Math|Confidence|Edge):", ln, flags=re.I)
    ]
    send_telegram_alert("\n".join(tg_streamlined_lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        err_line = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:900]
        print(f"::error::curator.py failed: {err_line}", flush=True)
        raise
