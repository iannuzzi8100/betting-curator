import os
import re
import html
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


def _calc_half_kelly_target(pick_txt, sport_txt, conf_pct, odds_i, eff_br, is_drawdown):
    """
    Phase 1 Calibrated Half-Kelly Wager Sizing:
    - Maps Confidence Meter 68-99% -> calibrated win prob 54.5%-65.5%
    - Applies market-type Kelly multiplier:
        * Straight Moneyline (core Era 2 edge): 0.55x Kelly
        * New leagues (UCL / LALIGA): 0.30x Kelly + max 10% single cap
        * Spreads / Totals / Player Props: 0.40x Kelly
    - Hard caps: 18% single / 58% concurrent (Growth) | 12% single / 40% concurrent (Drawdown Brake)
    - $1,000 Goal-Lock Brake: never risk more than needed to cross $1,000.00
    """
    sp_u = str(sport_txt or "").upper()
    pk_u = str(pick_txt or "")
    is_ml = bool(re.search(r"\b(ml|moneyline|to\s+win)\b", pk_u, flags=re.I)) and not bool(
        re.search(r"\b(over|under)\s+\d", pk_u, flags=re.I)
    )
    is_new_league = sp_u in ("UCL", "LALIGA")
    if is_new_league:
        k_mult = 0.30
    elif is_ml:
        k_mult = 0.55
    else:
        k_mult = 0.40

    max_single_pct = 0.12 if is_drawdown else 0.18
    if is_new_league:
        max_single_pct = min(max_single_pct, 0.10)

    b_mult = (odds_i / 100.0) if odds_i > 0 else (100.0 / max(1, abs(odds_i)))
    cp = conf_pct if conf_pct is not None else 74
    p_win = 0.545 + (max(0, min(99, cp) - 68) / 31.0) * 0.11
    full_kelly = max(0.0, (p_win * b_mult - (1.0 - p_win)) / b_mult) if b_mult > 0 else 0.0
    adj_kelly = full_kelly * k_mult
    raw_st = min(eff_br * max_single_pct, eff_br * adj_kelly)
    if eff_br < 1000.0 and b_mult > 0:
        raw_st = min(raw_st, (1000.0 - eff_br) / b_mult)
    return {
        "p_win": p_win,
        "full_kelly": full_kelly,
        "adj_kelly": adj_kelly,
        "k_mult": k_mult,
        "max_single_amt": eff_br * max_single_pct,
        "raw_st": raw_st,
    }


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
    peak_bankroll = max(100.0, settled_bankroll)
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
                if rb > peak_bankroll:
                    peak_bankroll = rb

    available_bankroll = max(0.0, round(settled_bankroll - pending_total, 2))
    is_drawdown = (settled_bankroll < 75.0) or (peak_bankroll > 0 and ((peak_bankroll - settled_bankroll) / peak_bankroll) > 0.20)
    max_conc_ratio = 0.40 if is_drawdown else 0.58
    max_exposure_cap = round(settled_bankroll * max_conc_ratio, 2)
    remaining_cap_amt = max(0.0, round(max_exposure_cap - pending_total, 2))
    unit_val_banner = round(settled_bankroll * 0.05, 2)

    if is_drawdown:
        active_tier = f"Drawdown Brake Active (< $75 or >20% off ${peak_bankroll:.2f} peak | 1U = ${unit_val_banner:.2f} | Half-Kelly 12% Single / 40% Concurrent)"
    else:
        active_tier = f"Growth Mode (1U = ${unit_val_banner:.2f} = 5% BR | Half-Kelly 18% Single / 58% Concurrent)"

    verified_bankroll_banner = (
        f"VERIFIED LEDGER NUMBERS (USE EXACTLY THESE IN OUTPUT HEADER):\n"
        f"- Settled Bankroll: ${settled_bankroll:.2f} (Challenge Peak: ${peak_bankroll:.2f})\n"
        f"- Active Mode: {active_tier}\n"
        f"- Pending Wagers Total: ${pending_total:.2f} ({len(pending_lines)} active ticket(s))\n"
        f"- Available Unstaked Bankroll: ${available_bankroll:.2f}\n"
        f"- {int(max_conc_ratio * 100)}% Max Concurrent Exposure Cap: ${max_exposure_cap:.2f} (Remaining Headroom: ${remaining_cap_amt:.2f})\n"
        f"- Active Pending Tickets:\n"
        + ("\n".join(pending_lines) if pending_lines else "  * None")
    )

    summary_block = verified_bankroll_banner + "\n\n" + "\n".join([" | ".join(r[:20]) for r in tracker_rows[:12]])
    recent_bets = "\n".join([" | ".join(r[:20]) for r in bet_rows_chrono[-15:]])
    rules_text = "\n".join([" | ".join(r[:8]) for r in rules_rows])
    audit_text = "\n".join([" | ".join(r[:8]) for r in audit_rows])

    ledger_meta = {
        "settled": settled_bankroll,
        "peak": peak_bankroll,
        "is_drawdown": is_drawdown,
        "pending_total": pending_total,
        "available": available_bankroll,
        "max_cap": max_exposure_cap,
        "remaining_cap": remaining_cap_amt,
        "pending_lines": pending_lines,
    }
    return summary_block, recent_bets, rules_text, audit_text, ledger_meta


def run_argon_curation(today_str, summary_block, recent_bets, rules_text, audit_text, intel_text, ledger_meta=None):
    ledger_meta = ledger_meta or {"settled": 20.0, "peak": 100.0, "is_drawdown": True, "pending_total": 0.0, "available": 20.0, "max_cap": 8.0, "remaining_cap": 8.0, "pending_lines": []}
    remaining_cap = ledger_meta["remaining_cap"]
    available_bankroll = ledger_meta["available"]

    # Deterministic Concurrent Exposure Cap Guardrail:
    # If active slate is already at/near the max concurrent cap,
    # switch into PRE-TIPOFF ACTIVE-TICKET RISK & INJURY AUDIT MODE instead of recommending extra bets!
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
    # Phase 1 Half-Kelly Smart Wager Engine (1U = 5% BR, 18% single / 58% concurrent; Drawdown Brake -> 12% / 40%)
    settled_bankroll = ledger_meta["settled"]
    peak_bankroll = float(ledger_meta.get("peak") or max(100.0, settled_bankroll))
    unit_val = round(settled_bankroll * 0.05, 2)
    is_drawdown = bool(ledger_meta.get("is_drawdown")) or (settled_bankroll < 75.0) or (peak_bankroll > 0 and ((peak_bankroll - settled_bankroll) / peak_bankroll) > 0.20)
    max_single_pct = 12 if is_drawdown else 18
    max_conc_pct = 40 if is_drawdown else 58
    max_single_amt = round(min(available_bankroll, max(2.00, settled_bankroll * (max_single_pct / 100.0))), 2)
    max_conc_amt = round(settled_bankroll * (max_conc_pct / 100.0), 2)
    stake_t1_solo = round(max_single_amt * 2) / 2.0
    stake_t2_solo = round(min(max_single_amt, max(2.00, settled_bankroll * (0.08 if is_drawdown else 0.11))) * 2) / 2.0
    stake_t3_solo = round(min(max_single_amt, max(2.00, settled_bankroll * (0.05 if is_drawdown else 0.07))) * 2) / 2.0

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
2. **CALIBRATED HALF-KELLY & OVERLAP-SAFE WAGER SIZING (CURRENT BR = ${settled_bankroll:.2f} | 1U = ${unit_val:.2f} = 5% BR):**
   - **STRICT MAX 2 PLAYS PER TIME-SLOT WINDOW:** Pick the single best play (or at most the top 2 highest-confidence plays) per time-slot window. NEVER stack 3–5 plays in one window and NEVER write `[Included in Hammer Slot]` — every play MUST have an explicit `$XX.XX (X.XU)` wager rounded to the nearest `$0.50`!
   - **HARD CONCURRENT OVERLAP CAP ({max_conc_pct}% BR = ${max_conc_amt:.2f}) & SINGLE-BET CAP ({max_single_pct}% BR = ${max_single_amt:.2f}):** Never let overlapping games (across adjacent windows!) exceed {max_conc_pct}% of bankroll at risk simultaneously.
   - **ODDS-ADJUSTED HALF-KELLY GUIDELINES (0.55x ML | 0.40x Spread/Prop | 0.30x UCL/La Liga):**
     * **🔥 TIER 1 (`88%–99%`):** Up to ~{max_single_pct}% BR (~${stake_t1_solo:.2f}, trimmed slightly on heavier `-130` to `-150` juice or spreads).
     * **💎 TIER 2 (`78%–87%`):** ~7%–12% BR (~${stake_t2_solo:.2f}), scaled down on heavier juice (`-130+`) or overlapping windows.
     * **✅ TIER 3 (`68%–77%`):** ~4%–8% BR (~${stake_t3_solo:.2f}), min `$2.00`.
     * **🎯 $1,000 GOAL-LOCK BRAKE:** Never wager more than needed to reach `$1,000.00` total bankroll.
     * **⛔ TIER 4 (`Below 68%`):** PASS (`$0.00`).
3. **100% STRAIGHT-BET COMPOUNDING ENGINE (ZERO SGPs / ZERO PARLAYS):**
   - Recommend **100% Straight Bets** (Moneyline, Spread, Total, 1st-Half, or Straight Player Prop). Never recommend SGPs or Parlays.
4. **MONEYLINE SWEET-SPOT PRIORITY & "-155 TO +130" BAND:**
   - **STRONGLY PRIORITIZE STRAIGHT MONEYLINES (`-110` to `-155`)** — our Era 2 ledger audit shows Moneylines at `8W-1L (88.9%, +62.1% ROI)` vs Spreads at `6W-9L (40.0%, -13.7% ROI)`. Whenever a strong favorite is available on the ML at `-155` or better, ALWAYS prefer the ML over laying `-4.5` to `-6.5`!
   - Restrict all plays to **`-155` to `+130`**. Never lay `-175+` heavy juice and never chase `+150+` longshots.
5. **STRICT SPREAD DISCIPLINE (`-3.5` PREFERRED, `-6.5` HARD CAP / `-7.5` CFB):**
   - Prefer small spreads (`-0.5` to `-3.5`). Only lay `-4.0` to `-6.5` when 2+ independent quantitative edges (BPI/FPI `+EV`, Major L5 Form, Sharp Steam, or B2B Fatigue) clearly align. Never lay steeper than `-6.5` (`-7.5` in CFB).
6. **NEVER CHASE LATE STEAM:**
   - Strictly ignore any side or total tagged `[⚠️ ... ALREADY STEAMED — DO NOT CHASE LATE]`.
7. **STRICT 1-BET-PER-GAME DIVERSIFICATION:**
   - Even when placing 2 dual bets in the same time window, they MUST come from **2 different games**.
8. **STRICT FACTUAL GROUNDING (ZERO HALLUCINATIONS):**
   - ONLY cite odds, records, L5 form, FPI/BPI numbers, and Confidence Meter scores that appear **verbatim** in Section 3 above.
9. **LINEUP GUARDRAIL:** If any recommended play involves a key player listed as `Questionable` or `Day-To-Day`, append `[WAIT FOR LINEUP]`.

### STRICT FORMATTING RULES (STREAMLINED HEADLINE + EXPANDABLE DRAWER DATA):
- Output ONLY the template below. NO extra paragraphs, NO UTC timestamps, NO square brackets around time/wager/confidence.
- **EXACT GAME START TIME ONLY:** On every numbered play, use the game's **EXACT start time in EST** from the `(H:MM PM EST)` tag in Section 3 (e.g. `2:00 PM EST`, `7:00 PM EST`). **NEVER** use a generic window bucket label like `Window 3: 12:00 PM`.
- On each numbered headline (`1.`, `2.`, etc.), show ONLY: `Exact Time EST • Away @ Home (League) — Bet (Odds) — $XX.XX (X.XU) — XX% Icon`. Do NOT write `Included in Hammer Slot` or `of Resulting BR`.
- Underneath each numbered headline, include the two structured drawer lines (`- Game & Line:` and `- Confidence Math:`) so the web app can display them ONLY when the user clicks on the bet!

🎯 *{today_str} {slate_header_tag}*
Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX

*Pending ($XX.XX):*
• *None* (or list active pending BET-XXX rows if any)

*Today's Curated Plays:*

1. 7:00 PM EST • Away Team @ Home Team (NBA) — Pick Name -3.5 (-110) — $XX.XX (X.XU) — 87% 💎
   - Game & Line: [Records, Full Spread / ML / Total O/U, L5 Form & avg margin, ESPN BPI/FPI Win Prob, and any line steam from Section 3]
   - Confidence Math: [Base 50% + exact comma-separated Pillar points from Section 3 = XX% (Tier Name)]

2. 8:00 PM EST • Away Team @ Home Team (NHL) — Pick Name (-120) — $XX.XX (X.XU) — 83% 💎
   - Game & Line: [Records, Full Spread / ML / Total O/U, L5 Form & avg margin, Goalies/BPI, and any line steam from Section 3]
   - Confidence Math: [Base 50% + exact comma-separated Pillar points from Section 3 = XX% (Tier Name)]
(Continue in chronological order for every remaining time-slot window today; if a window has no 68%+ play, omit it or list `X. All Windows — ⛔ PASS (<68% Confidence)` only if zero plays qualify all day.)
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
                    return _filter_and_size_gemini_slate(txt, ledger_meta)
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


def _filter_and_size_gemini_slate(txt, ledger_meta):
    """
    Post-processes Gemini's curated slate with our exact Half-Kelly engine so that:
    - Any explicit PASS or low-edge play whose Half-Kelly stake rounds below $2.00 is dropped
      before reaching Telegram or Curator Feed.
    - Surviving plays have exact $0.50-rounded Half-Kelly $XX.XX (X.XU) wagers and clean 1..N numbering.
    """
    settled_br = float((ledger_meta or {}).get("settled", 0.0) or 0.0)
    if settled_br <= 0 or not txt or not re.search(r"^\s*1\.\s+", txt, flags=re.M):
        return txt

    peak_br = float((ledger_meta or {}).get("peak") or max(100.0, settled_br))
    is_dd = bool((ledger_meta or {}).get("is_drawdown")) or (settled_br < 75.0) or (peak_br > 0 and ((peak_br - settled_br) / peak_br) > 0.20)
    unit_val = max(0.01, settled_br * 0.05)

    lines = txt.splitlines()
    header_lines = []
    blocks = []
    cur_block = None
    in_plays = False

    for ln in lines:
        m_num = re.match(r"^\s*\d+\.\s+(.+)$", ln)
        if m_num:
            in_plays = True
            if cur_block:
                blocks.append(cur_block)
            cur_block = {"headline": m_num.group(1).strip(), "sublines": []}
        elif not in_plays:
            header_lines.append(ln)
        elif cur_block is not None:
            if ln.strip() or (cur_block["sublines"] and ln.strip()):
                cur_block["sublines"].append(ln)
    if cur_block:
        blocks.append(cur_block)

    if not blocks:
        return txt

    kept_blocks = []
    dropped_cnt = 0
    for blk in blocks:
        hl = blk["headline"]
        clean_hl = hl.replace("*", "").strip()
        if "⛔" in clean_hl or "NO GOOD PLAY" in clean_hl.upper() or re.search(r"\bPASS\b", clean_hl.upper()):
            dropped_cnt += 1
            continue

        # Extract sport
        sport = ""
        m_sp = re.search(r"[\(\[•]\s*(CFB|NCAAF|NFL|NBA|NCAAM|NCAAB|CBB|NHL|MLB|EPL|UCL|LALIGA|LA LIGA|SOCCER)\s*[\)\]]", clean_hl, flags=re.I)
        if m_sp:
            sport = m_sp.group(1).strip().upper()
            if sport in ("CBB", "NCAAB"):
                sport = "NCAAM"

        # Extract pick segment
        pick_seg = clean_hl
        if "—" in clean_hl:
            parts = [p.strip() for p in clean_hl.split("—") if p.strip()]
            if len(parts) >= 2:
                pick_seg = parts[1]

        # Extract American odds
        m_odds = re.search(r"\(([+-]\d{3,4})\)", pick_seg) or re.search(r"\(([+-]\d{3,4})\)", clean_hl)
        odds_i = int(m_odds.group(1)) if m_odds else -110

        # Extract confidence %
        conf_pct = None
        m_tail_c = re.search(r"[—-]\s*\[?(\d{2,3})%\s*([^\]]*)\]?$", clean_hl)
        if m_tail_c:
            ctx = (m_tail_c.group(2) or "").upper()
            if "BR" not in ctx and "BANKROLL" not in ctx:
                conf_pct = int(m_tail_c.group(1))
        for sl in blk["sublines"]:
            low_sl = sl.lower()
            if "confidence math:" in low_sl or "confidence:" in low_sl:
                pm = re.search(r"=\s*(\d{2,3})\s*%", sl) or re.search(r"(\d{2,3})\s*%", sl)
                if pm:
                    conf_pct = int(pm.group(1))
                    break

        if conf_pct is not None and conf_pct < 68:
            dropped_cnt += 1
            continue

        kp = _calc_half_kelly_target(pick_seg, sport, conf_pct, odds_i, settled_br, is_dd)
        if kp["raw_st"] < 2.0:
            dropped_cnt += 1
            continue

        stake = round(kp["raw_st"] * 2.0) / 2.0
        units = round(stake / unit_val, 1)
        wager_str = f"${stake:.2f} ({units:.1f}U)"

        # Replace existing "$XX.XX (X.XU)" or "$XX.XX" in headline with exact Half-Kelly wager
        new_hl = re.sub(
            r"\$\d+(?:\.\d{1,2})?(?:\s*\(\d+(?:\.\d+)?U\))?",
            lambda _m: wager_str,
            hl,
            count=1,
        )
        kept_blocks.append({"headline": new_hl, "sublines": blk["sublines"]})

    if dropped_cnt:
        print(
            f"Half-Kelly Slate Filter: Kept {len(kept_blocks)} actionable play(s), dropped {dropped_cnt} PASS / low-edge (<$2 min) play(s).",
            flush=True,
        )

    out_lines = list(header_lines)
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()

    if not kept_blocks:
        out_lines.append("")
        out_lines.append("• Holding — no remaining plays cleared the Half-Kelly $2.00 minimum edge floor after vig.")
        return "\n".join(out_lines)

    for idx, blk in enumerate(kept_blocks, start=1):
        out_lines.append("")
        out_lines.append(f"{idx}. {blk['headline']}")
        for sl in blk["sublines"]:
            if sl.strip():
                out_lines.append(sl)

    return "\n".join(out_lines)


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

    # Group by window and keep up to top 2 highest-confidence plays per window
    by_win = {}
    for c in candidates:
        by_win.setdefault(c["win_num"], []).append(c)

    selected = []
    for w_num in sorted(by_win.keys()):
        w_plays = sorted(by_win[w_num], key=lambda x: (-x["conf_pct"], x["t_min"]))
        if w_plays and w_plays[0]["conf_pct"] >= 88:
            chosen = w_plays[:2] if (len(w_plays) >= 2 and w_plays[1]["conf_pct"] >= 82) else w_plays[:1]
        else:
            chosen = w_plays[:2]
        selected.extend(chosen)

    # Smart Wager Engine: Calibrated Fractional Kelly + 1U=5% BR + 35% single / 80% concurrent overlap cap + Drawdown & $1,000 Goal-Lock Brakes
    def _sport_dur_min(sp_lbl):
        u = (sp_lbl or "").upper()
        if u in ("EPL", "UCL", "LALIGA", "SOCCER"):
            return 120
        if u == "NCAAM":
            return 130
        if u == "NBA":
            return 150
        if u == "NHL":
            return 160
        if u == "MLB":
            return 175
        if u == "NFL":
            return 200
        if u in ("CFB", "NCAAF"):
            return 215
        return 165

    def _peak_overlap_risk(intervals, s_m, e_m):
        pts = [s_m]
        for iv in intervals:
            if iv["start"] < e_m and iv["end"] > s_m and s_m <= iv["start"] < e_m:
                pts.append(iv["start"])
        best = 0.0
        for t in pts:
            tot = sum(iv["stake"] for iv in intervals if iv["start"] <= t < iv["end"])
            if tot > best:
                best = tot
        return best

    peak_br = float(ledger_meta.get("peak") or max(100.0, settled_br))
    is_dd = bool(ledger_meta.get("is_drawdown")) or (settled_br < 75.0) or (peak_br > 0 and ((peak_br - settled_br) / peak_br) > 0.20)
    max_conc_pct = 0.40 if is_dd else 0.58
    unit_val = max(0.01, settled_br * 0.05)
    max_conc_amt = settled_br * max_conc_pct

    # Pre-load any active pending tickets into overlap intervals if times are present
    allocated_intervals = []
    for pln in pending_lines:
        pm_st = re.search(r"\(\$(\d+(?:\.\d{1,2})?)\)", pln)
        pm_tm = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", pln, flags=re.I)
        st_v = float(pm_st.group(1)) if pm_st else 0.0
        if st_v > 0:
            ps_m = _time_to_min(pm_tm.group(1)) if pm_tm else now_min
            allocated_intervals.append({"start": ps_m, "end": ps_m + 150, "stake": st_v})

    for item in selected:
        m_o = re.search(r"\(([+-]\d{3,4})\)", item["pick"])
        odds_i = int(m_o.group(1)) if m_o else -110
        kp = _calc_half_kelly_target(item["pick"], item["league"], item["conf_pct"], odds_i, settled_br, is_dd)
        item["odds_i"] = odds_i
        item["kelly_f"] = kp["adj_kelly"]
        item["max_single_amt"] = kp["max_single_amt"]
        item["raw_st"] = kp["raw_st"]
        item["e_min"] = item["t_min"] + _sport_dur_min(item["league"])

    # Allocate in priority order (highest Half-Kelly edge first) so stronger plays reserve concurrent room first
    for item in sorted(selected, key=lambda x: (-x["kelly_f"], x["t_min"])):
        pk = _peak_overlap_risk(allocated_intervals, item["t_min"], item["e_min"])
        room = max(0.0, max_conc_amt - pk)
        st = min(item["raw_st"], item["max_single_amt"], room)
        st = round(st * 2.0) / 2.0 if st >= 2.0 else 0.0
        item["stake"] = st
        item["units"] = round(st / unit_val, 1) if st > 0 else 0.0
        if st > 0:
            allocated_intervals.append({"start": item["t_min"], "end": item["e_min"], "stake": st})

    selected = [x for x in selected if x.get("stake", 0.0) >= 2.0]
    selected.sort(key=lambda x: (x["t_min"], -x["conf_pct"]))

    pend_block = "\n".join(ln.replace("- ", "• ", 1) for ln in pending_lines) if pending_lines else "• *None*"
    out = [
        f"🎯 *{today_str} {slate_header_tag}*",
        f"Bankroll: ${settled_br:.2f} | Pending: ${pending_tot:.2f} | Available: ${avail_br:.2f} | 1U: ${unit_val:.2f}",
        "",
        f"*Pending (${pending_tot:.2f}):*",
        pend_block,
        "",
        "*Today's Curated Plays:*",
    ]
    if not selected:
        out.append("")
        out.append("1. All Windows — ⛔ PASS (<68% Confidence — Hold bankroll for next window)")
    else:
        for idx, p in enumerate(selected, start=1):
            icon = "🔥" if p["conf_pct"] >= 88 else ("💎" if p["conf_pct"] >= 78 else "✅")
            out.append("")
            out.append(
                f"{idx}. {p['time_est']} • {p['matchup']} ({p['league']}) — {p['pick']} — ${p['stake']:.2f} ({p['units']:.1f}U) — {p['conf_pct']}% {icon}"
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
        plain_ln = html.unescape(re.sub(r"<[^>]+>", "", ln))
        m = re.match(r"^\s*\d+\.\s*(.+)$", plain_ln)
        if not m:
            continue
        clean = m.group(1).replace("*", "").strip()
        # Keep play_idx aligned with Curator Feed parser, but don't add a Log button for PASS windows
        if "⛔" in clean or re.search(r"\bPASS\b", clean):
            play_idx += 1
            continue
        # Strip leading [Time — Sport] bracket or "7:00 PM EST • " prefix for a crisp iPhone button label
        short_label = re.sub(r"^\[[^\]]+\]\s*", "", clean).strip()
        parts = [p.strip() for p in re.split(r"\s+[—–]\s+", short_label) if p.strip()]
        if len(parts) >= 2:
            short_label = parts[1]
            if len(parts) >= 3 and "$" in parts[2]:
                st_m = re.search(r"\$\d+(?:\.\d{1,2})?", parts[2])
                if st_m:
                    short_label = f"{short_label} ({st_m.group(0)})"
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


def format_telegram_slate(curated_message, ledger_meta=None, today_str=None):
    """
    Deterministically formats the Telegram alert in HTML mode to match the clean scannable card layout:
      🎯 <b>YYYY-MM-DD Confidence Meter Time-Slot Slate</b>
      Bankroll: $XX.XX | Pending: $XX.XX | Available: $XX.XX

      <b>Pending ($XX.XX):</b>
      • <b>None</b>

      <b>Today's Curated Plays:</b>

      1. 7:00 PM EST • Away @ Home (NBA) — Pick (-110) — $XX.XX (X.XU) — 87% 💎

      2. ...
    Also enforces exact Smart Wager Engine stakes ($XX.XX (X.XU)) on every play so Telegram
    matches the web dashboard and never displays broken Markdown, brackets, or stale sizing text.
    """
    raw = (curated_message or "").strip()
    if not raw:
        return ""

    # Preserve Primetime Pre-Tipoff Audit alerts cleanly in HTML mode
    if raw.startswith("🛡️") or "Primetime Pre-Tipoff Audit" in raw:
        out_lines = []
        for ln in raw.splitlines():
            esc_ln = html.escape(ln)
            esc_ln = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", esc_ln)
            out_lines.append(esc_ln)
        return "\n".join(out_lines)

    # Extract header title
    title_text = ""
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("🎯"):
            title_text = s.lstrip("🎯").replace("*", "").strip()
            break
    if not title_text:
        title_text = f"{today_str or ''} Confidence Meter Time-Slot Slate".strip()

    # Verified ledger numbers
    meta = ledger_meta or {}
    settled_br = float(meta.get("settled") or 0.0)
    pending_tot = float(meta.get("pending_total") or 0.0)
    avail_br = float(meta.get("available") or 0.0)
    pending_lines = list(meta.get("pending_lines") or [])
    if settled_br <= 0:
        bm = re.search(r"Bankroll:\s*\$(\d+(?:\.\d{1,2})?)", raw)
        pm = re.search(r"Pending:\s*\$(\d+(?:\.\d{1,2})?)", raw)
        am = re.search(r"Available:\s*\$(\d+(?:\.\d{1,2})?)", raw)
        if bm:
            settled_br = float(bm.group(1))
        if pm:
            pending_tot = float(pm.group(1))
        if am:
            avail_br = float(am.group(1))

    peak_br = float(meta.get("peak") or max(100.0, settled_br))
    is_dd = bool(meta.get("is_drawdown")) or (settled_br < 75.0) or (peak_br > 0 and ((peak_br - settled_br) / peak_br) > 0.20)
    max_conc_pct = 0.40 if is_dd else 0.58
    unit_val = max(0.01, settled_br * 0.05)

    def _time_to_min(txt):
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", str(txt or ""), flags=re.I)
        if not m:
            return 24 * 60
        hr, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ap == "PM" and hr != 12:
            hr += 12
        if ap == "AM" and hr == 12:
            hr = 0
        return hr * 60 + mn

    def _dur_min(sp, sel_txt=""):
        u = str(sp or "").upper()
        mins = 165
        if u in ("EPL", "UCL", "LALIGA", "SOCCER"):
            mins = 120
        elif u in ("NCAAM", "CBB", "NCAAB"):
            mins = 135
        elif u in ("NBA", "NHL"):
            mins = 160
        elif u in ("NFL", "CFB", "NCAAF"):
            mins = 205
        elif u == "MLB":
            mins = 180
        if re.search(r"\b(?:1h|1st\s*half|first\s*half)\b", str(sel_txt or ""), flags=re.I):
            mins = max(60, int(mins * 0.55))
        return mins

    def _peak_overlap(intervals, s_m, e_m):
        pts = [s_m]
        for iv in intervals:
            if iv["start"] < e_m and iv["end"] > s_m and s_m <= iv["start"] < e_m:
                pts.append(iv["start"])
        best = 0.0
        for t in pts:
            tot = sum(iv["stake"] for iv in intervals if iv["start"] <= t < iv["end"])
            if tot > best:
                best = tot
        return best

    # Parse numbered plays (ignoring expandable drawer sub-bullets)
    parsed_plays = []
    for ln in raw.splitlines():
        s = ln.strip()
        if re.match(r"^[-•]\s*(Game & Line|Confidence Math|Confidence|Edge):", s, flags=re.I):
            continue
        m = re.match(r"^\d+\.\s*(.+)$", s)
        if not m:
            continue
        body = m.group(1).replace("*", "").strip()
        is_pass = ("⛔" in body) or bool(re.search(r"\bPASS\b", body))
        if is_pass:
            body_clean = re.sub(r"^\[([^\]]+)\]", r"\1", body)
            parsed_plays.append({"is_pass": True, "raw_clean": body_clean})
            continue

        wait_lineup = bool(re.search(r"\[WAIT FOR LINEUP\]", body, flags=re.I))
        body_no_flag = re.sub(r"\s*\[WAIT FOR LINEUP\]", "", body, flags=re.I).strip()

        # Split on em-dash / en-dash separators
        segs = [p.strip() for p in re.split(r"\s+[—–]\s+", body_no_flag) if p.strip()]
        header_seg = segs[0] if len(segs) >= 1 else body_no_flag
        # Unwrap leading [7:00 PM EST] bracket if present
        header_seg = re.sub(r"^\[(\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?)\]", r"\1", header_seg, flags=re.I).strip()
        pick_seg = segs[1] if len(segs) >= 2 else ""

        # Extract confidence % & icon from trailing segment (or anywhere after pick)
        conf_pct = None
        conf_icon = ""
        for sg in reversed(segs[1:]):
            sg_unbr = sg.strip("[] ")
            cm = re.search(r"\b(\d{2,3})%\s*(🔥|💎|✅)?", sg_unbr)
            if cm and not re.search(r"(BR|BANKROLL|RESULTING)", sg_unbr, flags=re.I):
                conf_pct = int(cm.group(1))
                conf_icon = cm.group(2) or ("🔥" if conf_pct >= 88 else ("💎" if conf_pct >= 78 else "✅"))
                break

        # Extract league, odds, and start time for exact Smart Wager Engine sizing
        sp_m = re.search(r"\((NCAAM|CBB|NCAAB|NBA|NHL|NFL|CFB|NCAAF|EPL|UCL|LALIGA|MLB|SOCCER)\)", header_seg, flags=re.I)
        sport = sp_m.group(1).upper() if sp_m else ""
        if sport in ("CBB", "NCAAB"):
            sport = "NCAAM"
            header_seg = re.sub(r"\((CBB|NCAAB)\)", "(NCAAM)", header_seg, flags=re.I)

        od_m = re.search(r"\(([+-]\d{3,4})\)", pick_seg) or re.search(r"([+-]\d{3,4})", pick_seg)
        odds_i = int(od_m.group(1)) if od_m else -110
        t_min = _time_to_min(header_seg)

        parsed_plays.append({
            "is_pass": False,
            "header_seg": header_seg,
            "pick_seg": pick_seg,
            "conf_pct": conf_pct,
            "conf_icon": conf_icon,
            "sport": sport,
            "odds_i": odds_i,
            "t_min": t_min,
            "e_min": t_min + _dur_min(sport, pick_seg),
            "wait_lineup": wait_lineup,
            "orig_segs": segs,
            "body_clean": body_no_flag,
        })

    # Recompute exact Kelly + overlap-safe dollar & unit stakes when bankroll > 0
    if settled_br > 0:
        allocated = []
        for pln in pending_lines:
            pm_st = re.search(r"\(\$(\d+(?:\.\d{1,2})?)\)", pln)
            pm_tm = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", pln, flags=re.I)
            st_v = float(pm_st.group(1)) if pm_st else 0.0
            if st_v > 0:
                ps_m = _time_to_min(pm_tm.group(1)) if pm_tm else 0
                allocated.append({"start": ps_m, "end": ps_m + 150, "stake": st_v})

        active_plays = [p for p in parsed_plays if not p["is_pass"] and p.get("pick_seg")]
        max_conc_amt = settled_br * max_conc_pct
        for p in active_plays:
            kp = _calc_half_kelly_target(p["pick_seg"], p["sport"], p["conf_pct"], p["odds_i"], settled_br, is_dd)
            p["kelly_f"] = kp["adj_kelly"]
            p["max_single_amt"] = kp["max_single_amt"]
            p["raw_st"] = kp["raw_st"]

        for p in sorted(active_plays, key=lambda x: (-x["kelly_f"], x["t_min"])):
            pk = _peak_overlap(allocated, p["t_min"], p["e_min"])
            room = max(0.0, max_conc_amt - pk)
            st = min(p["raw_st"], p["max_single_amt"], room)
            st = round(st * 2.0) / 2.0 if st >= 2.0 else 0.0
            p["stake"] = st
            p["units"] = round(st / unit_val, 1) if st > 0 else 0.0
            p["capped_by_overlap"] = (st == 0.0 and p["raw_st"] >= 2.0)
            if st > 0:
                allocated.append({"start": p["t_min"], "end": p["e_min"], "stake": st})

    # Assemble exact clean HTML message matching screenshot
    out = [
        f"🎯 <b>{html.escape(title_text)}</b>",
        f"Bankroll: ${settled_br:.2f} | Pending: ${pending_tot:.2f} | Available: ${avail_br:.2f}",
        "",
        f"<b>Pending (${pending_tot:.2f}):</b>",
    ]
    if pending_lines:
        for pln in pending_lines:
            clean_p = pln.lstrip("-• ").replace("*", "").strip()
            out.append(f"• {html.escape(clean_p)}")
    else:
        out.append("• <b>None</b>")

    out.append("")
    out.append("<b>Today's Curated Plays:</b>")

    if not parsed_plays:
        # Fallback if message had no numbered lines
        out.append("")
        out.append("1. All Windows — ⛔ PASS (&lt;68% Confidence)")
    else:
        for idx, p in enumerate(parsed_plays, start=1):
            out.append("")
            if p["is_pass"]:
                out.append(f"{idx}. {html.escape(p['raw_clean'])}")
                continue
            if p.get("pick_seg"):
                if p.get("stake", 0.0) >= 2.0:
                    wager_str = f"${p['stake']:.2f} ({p['units']:.1f}U)"
                elif settled_br > 0:
                    wager_str = f"Cap Full ({int(max_conc_pct * 100)}%)" if p.get("capped_by_overlap") else "Pass (Low Edge)"
                else:
                    # Keep original wager segment cleaned of brackets if no bankroll
                    wager_str = p["orig_segs"][2].strip("[] ") if len(p["orig_segs"]) >= 3 else ""
                pieces = [p["header_seg"], p["pick_seg"]]
                if wager_str:
                    pieces.append(wager_str)
                if p["conf_pct"] is not None:
                    pieces.append(f"{p['conf_pct']}% {p['conf_icon']}".strip())
                line_txt = " — ".join(pieces)
                if p["wait_lineup"]:
                    line_txt += " [WAIT FOR LINEUP]"
                out.append(f"{idx}. {html.escape(line_txt)}")
            else:
                out.append(f"{idx}. {html.escape(p['body_clean'])}")

    return "\n".join(out)


def send_telegram_alert(message, raw_message=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Alert text:\n", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    html_text = (message or "")[:4000]
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", html_text)).replace("*", "")
    keyboard = _build_curated_inline_keyboard(raw_message or html_text)
    attempts = [
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": plain_text,
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": plain_text,
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
    tg_html = format_telegram_slate(curated_message, ledger_meta=ledger_meta, today_str=today_str)
    send_telegram_alert(tg_html, raw_message=tg_html)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        err_line = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:900]
        print(f"::error::curator.py failed: {err_line}", flush=True)
        raise
