import os
import sys
import json
import re
import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

GCP_SA_KEY = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or ""
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID") or "1vTMvUyr_DABrrzVBU7ah1A8RYn9-sfO1Uu-0Syk2jnU"
SHEET_ID = (
    os.environ.get("SHEET_ID")
    or os.environ.get("MASTER_SHEET_ID")
    or os.environ.get("SPREADSHEET_ID")
    or os.environ.get("TRACKER_SHEET_ID")
    or os.environ.get("GOOGLE_SHEET_ID")
    or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
)
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"


def _parse_num(val):
    if val is None:
        return 0.0
    s = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except Exception:
        return 0.0


def _parse_curated_title_fields(title):
    sport = "CFB"
    game_time = ""
    rest = title.strip()
    m_br = re.match(r"^\[([^\]]+)\]\s*(.*)$", rest)
    if m_br:
        inside = m_br.group(1)
        rest = m_br.group(2).strip()
        parts = [p.strip() for p in re.split(r"[—–•-]", inside) if p.strip()]
        if len(parts) >= 2:
            game_time = parts[0]
            sport = parts[-1].upper()
        elif len(parts) == 1:
            if re.search(r"\b(?:AM|PM|EST|EDT|CST|PST)\b", parts[0], flags=re.I) or re.search(r"\d:\d{2}", parts[0]):
                game_time = parts[0]
            else:
                sport = parts[0].upper()

    # Handle streamlined format: "• Away @ Home (League) — Bet & Odds — $XX.XX (XX% BR)"
    bet_segment = rest
    m_stream = re.match(r"^[•·]\s*[^—]+?\(([A-Za-z0-9/ ]{2,10})\)\s*[—–]\s*(.+)$", rest)
    if m_stream:
        sport = m_stream.group(1).strip().upper()
        bet_segment = m_stream.group(2).strip()

    odds = -110
    m_odds = re.search(r"\(([+-]\d{3,4})\)", bet_segment) or re.search(r"\s([+-]\d{3,4})(?:\s|$|—|–)", bet_segment)
    if m_odds:
        try:
            odds = int(m_odds.group(1))
        except Exception:
            odds = -110

    stake = 0.0
    m_stake = re.search(r"\$(\d+(?:\.\d{1,2})?)", bet_segment) or re.search(r"\$(\d+(?:\.\d{1,2})?)", rest)
    if m_stake:
        try:
            stake = float(m_stake.group(1))
        except Exception:
            stake = 0.0

    pick = bet_segment
    if m_odds:
        pick = bet_segment[: m_odds.start()].strip()
    elif "—" in bet_segment:
        pick = bet_segment.split("—", 1)[0].strip()
    if game_time and pick and game_time not in pick:
        pick = f"{pick} ({game_time})"

    return {
        "sport": sport,
        "selection": pick,
        "odds": odds,
        "stake": stake,
        "game_time": game_time,
    }


def get_workbook():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents.readonly",
    ]
    if GCP_SA_KEY:
        creds = Credentials.from_service_account_info(json.loads(GCP_SA_KEY), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    gc = gspread.authorize(creds)
    try:
        sh = gc.open(SHEET_NAME)
    except Exception:
        sh = gc.open_by_key(SHEET_ID)
    return sh, creds


def _read_intel_quant_lines(creds, today_str=None):
    try:
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().get(documentId=INTEL_DOC_ID).execute()
        chunks = []
        for el in doc.get("body", {}).get("content", []):
            for pe in el.get("paragraph", {}).get("elements", []):
                chunks.append(pe.get("textRun", {}).get("content", ""))
        full_txt = "".join(chunks)
        if today_str and f"## {today_str} Slate Intel" in full_txt:
            start_pos = full_txt.index(f"## {today_str} Slate Intel")
            rest = full_txt[start_pos:]
            next_sec = re.search(r"\n## \d{4}-\d{2}-\d{2} Slate Intel", rest[1:])
            full_txt = rest[: next_sec.start() + 1] if next_sec else rest
        return [ln.strip() for ln in full_txt.splitlines() if "QUANT]" in ln or "PROP STEAM]" in ln]
    except Exception:
        return []


def fetch_dashboard_data():
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.strftime("%Y-%m-%d")
    yesterday_str = (now_est - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    week_start_str = (now_est - datetime.timedelta(days=6)).strftime("%Y-%m-%d")

    sh, creds = get_workbook()
    tracker_ws = sh.worksheets()[0]
    feed_ws = sh.worksheet("Curator Feed")

    all_rows = tracker_ws.get_all_values()
    header_idx = next((i for i, r in enumerate(all_rows) if "Bet ID" in r and "Result" in r), 15)

    settled_bankroll = _parse_num(all_rows[4][0]) if len(all_rows) > 4 and all_rows[4] else 100.0
    overall_wins = 0
    overall_losses = 0
    overall_pushes = 0
    total_staked_settled = 0.0
    total_net_pl = 0.0

    today_wins = 0
    today_losses = 0
    today_pushes = 0
    today_pl = 0.0
    today_tickets = []

    yest_wins = 0
    yest_losses = 0
    yest_pushes = 0
    yest_pl = 0.0
    yest_tickets = []

    week_wins = 0
    week_losses = 0
    week_pushes = 0
    week_pl = 0.0
    week_tickets = []

    settled_tickets = []
    pending_tickets = []
    pending_total = 0.0
    equity_curve = [{"label": "START", "bankroll": 100.0}]
    clv_values = []

    def _bet_num(bid):
        try:
            return int(str(bid).strip().split("-")[1])
        except Exception:
            return 0

    bet_entries = []
    for idx_row, r in enumerate(all_rows[header_idx + 1 :], start=header_idx + 2):
        if not r or not r[0].strip().startswith("BET-"):
            continue
        bet_id = r[0].strip()
        bet_entries.append({
            "row_index": idx_row,
            "bet_num": _bet_num(bet_id),
            "bet_id": bet_id,
            "b_date": r[1].strip() if len(r) > 1 else "",
            "sport": r[2].strip() if len(r) > 2 else "",
            "b_type": r[3].strip() if len(r) > 3 else "",
            "sel": r[5].strip() if len(r) > 5 else "",
            "odds": r[6].strip() if len(r) > 6 else "",
            "stake": _parse_num(r[8]) if len(r) > 8 else 0.0,
            "payout": _parse_num(r[9]) if len(r) > 9 else 0.0,
            "res": r[10].strip() if len(r) > 10 else "",
            "net_pl": _parse_num(r[12]) if len(r) > 12 else 0.0,
            "run_br": _parse_num(r[13]) if (len(r) > 13 and r[13].strip()) else 0.0,
            "notes": r[14].strip() if len(r) > 14 else "",
        })

    # Chronological order (BET-001 -> latest) for Equity Curve & Settled Bankroll regardless of sheet order
    for e in sorted(bet_entries, key=lambda x: x["bet_num"]):
        clv_match = re.search(r"\[CLV:\s*([+-]?\d+(?:\.\d+)?)%", e["notes"])
        clv_val = float(clv_match.group(1)) if clv_match else None
        if clv_val is not None:
            clv_values.append(clv_val)

        res_l = e["res"].lower()
        if res_l == "won":
            overall_wins += 1
            total_staked_settled += e["stake"]
            total_net_pl += e["net_pl"]
            settled_bankroll = e["run_br"] if e["run_br"] > 0 else round(100.0 + total_net_pl, 2)
            equity_curve.append({"label": e["bet_id"], "bankroll": round(settled_bankroll, 2)})
        elif res_l == "lost":
            overall_losses += 1
            total_staked_settled += e["stake"]
            total_net_pl += e["net_pl"]
            settled_bankroll = e["run_br"] if e["run_br"] > 0 else round(100.0 + total_net_pl, 2)
            equity_curve.append({"label": e["bet_id"], "bankroll": round(settled_bankroll, 2)})
        elif res_l == "push":
            overall_pushes += 1
            equity_curve.append({"label": e["bet_id"], "bankroll": round(settled_bankroll, 2)})

    # Newest-first order for Pending, Today, Yesterday, This Week, and full Settled history lists
    for e in sorted(bet_entries, key=lambda x: x["bet_num"], reverse=True):
        clv_match = re.search(r"\[CLV:\s*([+-]?\d+(?:\.\d+)?)%", e["notes"])
        clv_val = float(clv_match.group(1)) if clv_match else None
        res_l = e["res"].lower()

        if res_l == "pending":
            pending_total += e["stake"]
            pending_tickets.append({
                "row_index": e["row_index"],
                "bet_id": e["bet_id"],
                "date": e["b_date"],
                "sport": e["sport"],
                "bet_type": e["b_type"],
                "selections": e["sel"],
                "odds": e["odds"],
                "stake": round(e["stake"], 2),
                "payout": round(e["payout"], 2),
                "clv": clv_val,
            })
        elif res_l in ("won", "lost", "push"):
            t_obj = {
                "bet_id": e["bet_id"],
                "date": e["b_date"],
                "sport": e["sport"],
                "bet_type": e["b_type"],
                "selections": e["sel"],
                "odds": e["odds"],
                "stake": round(e["stake"], 2),
                "result": e["res"],
                "net_pl": round(e["net_pl"], 2),
            }
            settled_tickets.append(t_obj)

            if week_start_str <= e["b_date"] <= today_str:
                if res_l == "won":
                    week_wins += 1
                elif res_l == "lost":
                    week_losses += 1
                elif res_l == "push":
                    week_pushes += 1
                week_pl += e["net_pl"]
                week_tickets.append(t_obj)

            if e["b_date"] == today_str:
                if res_l == "won":
                    today_wins += 1
                elif res_l == "lost":
                    today_losses += 1
                elif res_l == "push":
                    today_pushes += 1
                today_pl += e["net_pl"]
                today_tickets.append(t_obj)

            if e["b_date"] == yesterday_str:
                if res_l == "won":
                    yest_wins += 1
                elif res_l == "lost":
                    yest_losses += 1
                elif res_l == "push":
                    yest_pushes += 1
                yest_pl += e["net_pl"]
                yest_tickets.append(t_obj)

    overall_total = overall_wins + overall_losses
    overall_win_pct = round((overall_wins / overall_total) * 100.0, 1) if overall_total > 0 else 0.0
    overall_roi = round((total_net_pl / total_staked_settled) * 100.0, 1) if total_staked_settled > 0 else 0.0

    week_total = week_wins + week_losses
    week_win_pct = round((week_wins / week_total) * 100.0, 1) if week_total > 0 else 0.0

    today_total = today_wins + today_losses
    today_win_pct = round((today_wins / today_total) * 100.0, 1) if today_total > 0 else 0.0

    yest_total = yest_wins + yest_losses
    yest_win_pct = round((yest_wins / yest_total) * 100.0, 1) if yest_total > 0 else 0.0

    # Parse latest Curated Bets from Curator Feed (prefer latest row with numbered plays over audit-only rows)
    feed_vals = feed_ws.get_all_values()
    latest_curated_text = ""
    latest_curated_date = today_str
    fallback_text = ""
    fallback_date = today_str
    for r in reversed(feed_vals):
        if len(r) >= 12 and r[11].strip():
            txt = r[11].strip()
            dt = r[0].strip() or today_str
            if not fallback_text:
                fallback_text = txt
                fallback_date = dt
            if re.search(r"^\s*1\.\s+", txt, flags=re.M):
                latest_curated_date = dt
                latest_curated_text = txt
                break
    if not latest_curated_text:
        latest_curated_text = fallback_text
        latest_curated_date = fallback_date

    intel_quant_lines = _read_intel_quant_lines(creds, today_str=latest_curated_date or today_str)

    curated_plays = []
    if latest_curated_text:
        lines = latest_curated_text.splitlines()
        cur_play = None
        for ln in lines:
            s = ln.strip()
            m = re.match(r"^\d+\.\s*(.+)$", s)
            if m:
                if cur_play:
                    curated_plays.append(cur_play)
                clean_title = m.group(1).replace("*", "").strip()
                is_pass_row = (
                    "⛔" in clean_title
                    or "NO GOOD PLAY" in clean_title.upper()
                    or bool(re.search(r"\bPASS\b", clean_title.upper()))
                )
                # Check if confidence % is already on the headline (ignore "<68%" on PASS rows)
                hm = re.search(r"(?<![<])(\d{2,3})\s*%", clean_title)
                init_cp = (
                    int(hm.group(1))
                    if (hm and not is_pass_row and "BR" not in clean_title[hm.start() : hm.end() + 5])
                    else None
                )
                # Strip trailing "— 87% 💎" from headline since badge shows it on the right
                clean_title = re.sub(r"\s*[—-]\s*\[?\d{2,3}%\s*[^\]]*\]?$", "", clean_title).strip()
                # Streamline verbose "[Window X: ... (8:20 PM EST)]" prefix if present
                clean_title = re.sub(
                    r"^\[Window\s*\d+:[^\]]*?([A-Z]{2,5})\s*\((\d{1,2}:\d{2}\s*[AP]M\s*EST)\)\]\s*",
                    r"[\2 • \1] ",
                    clean_title,
                )
                clean_title = re.sub(r"\s*[—-]\s*Capped at Exposure Limit", "", clean_title, flags=re.I)
                parsed_fields = _parse_curated_title_fields(clean_title)
                cur_play = {
                    "title": clean_title,
                    "is_pass": is_pass_row,
                    "edge": "",
                    "game_line": "",
                    "confidence_math": "",
                    "confidence_pct": init_cp,
                    "confidence_tier": "",
                    **parsed_fields,
                }
            elif cur_play and (s.startswith("- ") or s.startswith("• ")):
                raw_bullet = re.sub(r"^[-•]\s*", "", s).replace("*", "").replace("`", "").strip()
                low_b = raw_bullet.lower()
                if low_b.startswith("confidence math:"):
                    cm_body = re.sub(r"^confidence math:\s*", "", raw_bullet, flags=re.I).strip()
                    cur_play["confidence_math"] = cm_body
                    pm = re.search(r"=\s*(\d{2,3})\s*%", cm_body) or re.search(r"(\d{2,3})\s*%", cm_body)
                    if pm and cur_play["confidence_pct"] is None:
                        cur_play["confidence_pct"] = int(pm.group(1))
                elif low_b.startswith("game & line:") or low_b.startswith("game and line:"):
                    gl_body = re.sub(r"^game\s*(?:&|and)\s*line:\s*", "", raw_bullet, flags=re.I).strip()
                    cur_play["game_line"] = gl_body
                elif low_b.startswith("confidence:"):
                    conf_body = re.sub(r"^confidence:\s*", "", raw_bullet, flags=re.I).strip(" []")
                    pm = re.search(r"(\d{2,3})\s*%", conf_body)
                    if pm:
                        cur_play["confidence_pct"] = int(pm.group(1))
                    parts = [p.strip() for p in conf_body.split("|") if p.strip()]
                    if len(parts) >= 3:
                        cur_play["confidence_tier"] = parts[-1]
                    else:
                        cur_play["confidence_tier"] = conf_body
                else:
                    edge_txt = re.sub(r"^Edge:\s*", "", raw_bullet, flags=re.I).strip()
                    cur_play["edge"] = (cur_play["edge"] + " " + edge_txt).strip()
                    if not cur_play["confidence_math"]:
                        cur_play["confidence_math"] = cur_play["edge"]
        if cur_play:
            curated_plays.append(cur_play)

    # Enrich each curated play from today's scraped Intel Doc if game_line or confidence_math isn't populated yet
    for cp_item in curated_plays:
        if cp_item.get("is_pass"):
            cp_item["confidence_pct"] = None
            cp_item["confidence_tier"] = "⛔ PASS WINDOW"
            continue
        if cp_item["confidence_pct"] is not None and not cp_item["confidence_tier"]:
            sc = cp_item["confidence_pct"]
            if sc >= 88:
                cp_item["confidence_tier"] = "🔥 TIER 1: MAX HAMMER"
            elif sc >= 78:
                cp_item["confidence_tier"] = "💎 TIER 2: STRONG CONVICTION"
            elif sc >= 68:
                cp_item["confidence_tier"] = "✅ TIER 3: SOLID EDGE"
            else:
                cp_item["confidence_tier"] = "⛔ TIER 4: PASS"

        if not cp_item["game_line"] and intel_quant_lines:
            t_clean = re.sub(r"\[[^\]]*\]", "", cp_item["title"])
            words = [
                w
                for w in re.findall(r"[A-Za-z]{3,}", t_clean)
                if w.upper() not in ("EST", "CFB", "NFL", "NBA", "NHL", "EPL", "CBB", "OVER", "UNDER", "RESULTING", "STRAIGHT")
            ]
            for q_ln in intel_quant_lines:
                if any(w.lower() in q_ln.lower() for w in words[:2]):
                    parts_q = q_ln.split("->")
                    if len(parts_q) >= 2:
                        match_hdr = re.sub(r"^-\s*\[[^\]]+\]\s*\[[^\]]+\]\s*", "", parts_q[0]).strip()
                        lines_body = parts_q[1].strip()
                        cp_item["game_line"] = f"{match_hdr} | {lines_body}"
                    if not cp_item["confidence_math"]:
                        cm_m = re.search(r"Top Play:\s*([^]]+)\]", q_ln)
                        if cm_m:
                            cp_item["confidence_math"] = cm_m.group(1).strip()
                    break

    latest_curated_text = _adjust_curated_plays_for_bankroll(
        curated_plays,
        latest_curated_text,
        round(settled_bankroll, 2),
        round(pending_total, 2),
        pending_tickets,
        today_tickets,
    )

    return {
        "updated_at": now_est.strftime("%Y-%m-%d %I:%M %p EST"),
        "today": today_str,
        "yesterday": yesterday_str,
        "week_start": week_start_str,
        "bankroll": round(settled_bankroll, 2),
        "pending_total": round(pending_total, 2),
        "available_cash": round(max(0.0, settled_bankroll - pending_total), 2),
        "overall": {
            "wins": overall_wins,
            "losses": overall_losses,
            "pushes": overall_pushes,
            "win_pct": overall_win_pct,
            "net_pl": round(total_net_pl, 2),
            "roi": overall_roi,
            "avg_clv": round(sum(clv_values) / len(clv_values), 2) if clv_values else None,
            "clv_count": len(clv_values),
        },
        "week_stats": {
            "start_date": week_start_str,
            "end_date": today_str,
            "wins": week_wins,
            "losses": week_losses,
            "pushes": week_pushes,
            "win_pct": week_win_pct,
            "net_pl": round(week_pl, 2),
            "tickets": week_tickets,
        },
        "today_stats": {
            "date": today_str,
            "wins": today_wins,
            "losses": today_losses,
            "pushes": today_pushes,
            "win_pct": today_win_pct,
            "net_pl": round(today_pl, 2),
            "tickets": today_tickets,
        },
        "yesterday_stats": {
            "date": yesterday_str,
            "wins": yest_wins,
            "losses": yest_losses,
            "pushes": yest_pushes,
            "win_pct": yest_win_pct,
            "net_pl": round(yest_pl, 2),
            "tickets": yest_tickets,
        },
        "settled_tickets": settled_tickets,
        "pending_tickets": pending_tickets,
        "curated_date": latest_curated_date,
        "curated_plays": curated_plays,
        "curated_raw": latest_curated_text,
        "equity_curve": equity_curve,
    }


def _extract_selection_core(text):
    s = str(text or "").strip()
    s = re.sub(r"^\d+\.\s*", "", s).replace("*", "").strip()
    s = re.sub(r"^\[[^\]]+\]\s*", "", s).strip()
    core = re.split(
        r"(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)",
        s,
        flags=re.I,
    )[0]
    return re.sub(r"[^a-z0-9\s]", "", core.lower()).strip()


def _adjust_curated_plays_for_bankroll(
    curated_plays, curated_raw, settled_bankroll, pending_total, pending_tickets, today_tickets
):
    """
    Deterministically recalculates the wager amounts for remaining unplaced curated bets
    for the day whenever a bet settles and the settled bankroll changes.
    """
    if not curated_plays:
        return curated_raw

    m_br = re.search(r"Bankroll:\s*\$(\d+(?:\.\d{1,2})?)", str(curated_raw or ""))
    feed_br = float(m_br.group(1)) if m_br else settled_bankroll
    avail_cash = round(max(0.0, settled_bankroll - pending_total), 2)

    win_counts = {}
    for p in curated_plays:
        title = str(p.get("title", ""))
        is_pass = ("⛔" in title) or bool(re.search(r"\bPASS\b", title))
        gt = p.get("game_time", "")
        if not is_pass and gt:
            win_counts[gt] = win_counts.get(gt, 0) + 1

    for p in curated_plays:
        title = str(p.get("title", ""))
        is_pass = ("⛔" in title) or bool(re.search(r"\bPASS\b", title))
        if is_pass:
            p["status"] = "pass"
            continue

        core = _extract_selection_core(p.get("selection") or title)
        match_pend = (
            next((t for t in (pending_tickets or []) if core and len(core) >= 3 and core in str(t.get("selections", "")).lower()), None)
            if core
            else None
        )
        match_settled = (
            next((t for t in (today_tickets or []) if core and len(core) >= 3 and core in str(t.get("selections", "")).lower()), None)
            if (core and not match_pend)
            else None
        )

        wager_pct = None
        m_pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s+(?:Resulting\s+)?)?(?:BR|Bankroll)", title, flags=re.I)
        if m_pct:
            wager_pct = float(m_pct.group(1))
        elif p.get("confidence_pct") is not None:
            cp = int(p["confidence_pct"])
            dual = win_counts.get(p.get("game_time", ""), 1) >= 2
            if cp >= 88:
                wager_pct = 35.0 if dual else 50.0
            elif cp >= 78:
                wager_pct = 25.0 if dual else 35.0
            elif cp >= 68:
                wager_pct = 18.0 if dual else 20.0
        elif feed_br > 0 and float(p.get("stake") or 0) > 0:
            wager_pct = round((float(p["stake"]) / feed_br) * 100.0, 1)

        if wager_pct is not None:
            p["wager_pct"] = int(wager_pct) if float(wager_pct).is_integer() else wager_pct

        if match_pend:
            p["status"] = "pending"
            p["pending_bet_id"] = match_pend.get("bet_id")
            p["stake"] = round(float(match_pend.get("stake") or p.get("stake") or 0.0), 2)
            if wager_pct and settled_bankroll > 0:
                p["recalculated_stake"] = round(
                    max(2.00, settled_bankroll * (wager_pct / 100.0)) if settled_bankroll >= 2.0 else settled_bankroll,
                    2,
                )
            continue

        if match_settled:
            p["status"] = "settled"
            p["settled_bet_id"] = match_settled.get("bet_id")
            p["settled_result"] = match_settled.get("result")
            p["settled_pl"] = round(float(match_settled.get("net_pl") or 0.0), 2)
            continue

        p["status"] = "remaining"
        if not wager_pct or wager_pct <= 0 or settled_bankroll <= 0:
            continue

        old_stake = round(float(p.get("stake") or 0.0), 2)
        new_stake = round(max(2.00, settled_bankroll * (wager_pct / 100.0)) if settled_bankroll >= 2.0 else settled_bankroll, 2)
        p["stake"] = new_stake
        if old_stake > 0 and abs(new_stake - old_stake) >= 0.01:
            p["original_stake"] = old_stake
            p["stake_adjusted"] = True
        else:
            p["stake_adjusted"] = False

        m_stakes = list(re.finditer(r"\$(\d+(?:\.\d{1,2})?)", title))
        if m_stakes:
            last_m = m_stakes[-1]
            p["title"] = title[: last_m.start()] + f"${new_stake:.2f}" + title[last_m.end() :]

    if curated_raw:
        updated_raw = re.sub(
            r"Bankroll:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Pending:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Available:\s*\$\d+(?:\.\d{1,2})?",
            f"Bankroll: ${settled_bankroll:.2f} | Pending: ${pending_total:.2f} | Available: ${avail_cash:.2f}",
            curated_raw,
        )
        raw_lines = updated_raw.splitlines()
        play_idx = 0
        for idx, ln in enumerate(raw_lines):
            if re.match(r"^\s*\d+\.\s+", ln) and play_idx < len(curated_plays):
                pl = curated_plays[play_idx]
                play_idx += 1
                if pl.get("status") == "remaining" and float(pl.get("stake") or 0) > 0:
                    m_st = list(re.finditer(r"\$(\d+(?:\.\d{1,2})?)", ln))
                    if m_st:
                        lm = m_st[-1]
                        raw_lines[idx] = ln[: lm.start()] + f"${float(pl['stake']):.2f}" + ln[lm.end() :]
        curated_raw = "\n".join(raw_lines)

    return curated_raw


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <title>Betting Curator • $100 to $1,000</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0b1120;
      --card: #111827;
      --card-border: #1f2937;
      --text: #f8fafc;
      --muted: #94a3b8;
      --green: #10b981;
      --red: #ef4444;
      --amber: #f59e0b;
      --blue: #3b82f6;
      --accent: #6366f1;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 18px 14px 60px;
      line-height: 1.45;
    }
    .container { max-width: 960px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .title-group h1 { font-size: 1.45rem; font-weight: 800; letter-spacing: -0.02em; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .title-group p { color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
    .header-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .refresh-btn {
      background: linear-gradient(135deg, var(--blue), var(--accent));
      color: #fff;
      border: none;
      border-radius: 10px;
      padding: 9px 14px;
      font-weight: 600;
      font-size: 0.84rem;
      cursor: pointer;
      transition: opacity 0.2s;
    }
    .refresh-btn:hover { opacity: 0.9; }
    .settle-btn {
      background: linear-gradient(135deg, #10b981, #059669);
      color: #fff;
      border: none;
      border-radius: 10px;
      padding: 9px 14px;
      font-weight: 700;
      font-size: 0.84rem;
      cursor: pointer;
      transition: opacity 0.2s, transform 0.12s;
      box-shadow: 0 4px 14px rgba(16, 185, 129, 0.25);
    }
    .settle-btn:hover { opacity: 0.92; }
    .settle-btn:active { transform: scale(0.98); }
    .settle-btn:disabled { opacity: 0.6; cursor: not-allowed; }
    .btn-secondary {
      background: rgba(255,255,255,0.06);
      color: var(--text);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 8px 13px;
      font-weight: 600;
      font-size: 0.83rem;
      cursor: pointer;
    }
    .btn-secondary:hover { background: rgba(255,255,255,0.11); }
    .btn-green {
      background: rgba(16,185,129,0.18);
      color: var(--green);
      border: 1px solid rgba(16,185,129,0.35);
      border-radius: 8px;
      padding: 6px 11px;
      font-weight: 700;
      font-size: 0.78rem;
      cursor: pointer;
    }
    .btn-green:hover { background: rgba(16,185,129,0.28); }
    .btn-mini {
      background: rgba(255,255,255,0.05);
      color: var(--muted);
      border: 1px solid var(--card-border);
      border-radius: 7px;
      padding: 4px 9px;
      font-weight: 600;
      font-size: 0.74rem;
      cursor: pointer;
    }
    .btn-mini:hover { color: var(--text); background: rgba(255,255,255,0.1); }
    .btn-mini-danger { color: #fca5a5; border-color: rgba(239,68,68,0.35); }
    .btn-mini-danger:hover { background: rgba(239,68,68,0.18); color: #fecaca; }
    .grid-3 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.28);
    }
    .card-label {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 8px;
    }
    .big-num { font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; }
    .sub-row { font-size: 0.85rem; color: var(--muted); margin-top: 6px; display: flex; gap: 12px; flex-wrap: wrap; }
    .pos { color: var(--green); font-weight: 700; }
    .neg { color: var(--red); font-weight: 700; }
    .warn { color: var(--amber); font-weight: 700; }
    .section-title {
      font-size: 1rem;
      font-weight: 700;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .section-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .pill {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
    }
    .pill-green { background: rgba(16,185,129,0.16); color: var(--green); }
    .pill-red { background: rgba(239,68,68,0.16); color: var(--red); }
    .pill-amber { background: rgba(245,158,11,0.18); color: var(--amber); }
    .pill-blue { background: rgba(59,130,246,0.18); color: #60a5fa; }
    .bet-list { display: flex; flex-direction: column; gap: 10px; }
    .bet-item {
      background: rgba(255,255,255,0.025);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 13px 14px;
    }
    .bet-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      flex-wrap: wrap;
    }
    .bet-title { font-weight: 700; font-size: 0.96rem; }
    .bet-meta { font-size: 0.82rem; color: var(--muted); margin-top: 4px; }
    .bet-actions { display: flex; gap: 7px; margin-top: 10px; flex-wrap: wrap; align-items: center; }
    .chart-wrap { height: 210px; position: relative; margin-top: 6px; }
    .stack { display: flex; flex-direction: column; gap: 16px; }
    .empty-msg { color: var(--muted); font-size: 0.9rem; padding: 8px 0; }

    /* Modal & Form styles */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(2, 6, 23, 0.78);
      backdrop-filter: blur(4px);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 16px;
      z-index: 1000;
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      width: 100%;
      max-width: 460px;
      background: #111827;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.55);
      max-height: 92vh;
      overflow-y: auto;
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    .modal-header h3 { font-size: 1.05rem; font-weight: 800; }
    .close-btn {
      background: transparent;
      border: none;
      color: var(--muted);
      font-size: 1.3rem;
      cursor: pointer;
      padding: 2px 6px;
    }
    .tabs { display: flex; gap: 6px; margin-bottom: 14px; }
    .tab-btn {
      flex: 1;
      padding: 8px 10px;
      border-radius: 9px;
      border: 1px solid var(--card-border);
      background: rgba(255,255,255,0.03);
      color: var(--muted);
      font-weight: 700;
      font-size: 0.82rem;
      cursor: pointer;
    }
    .tab-btn.active {
      background: rgba(59,130,246,0.2);
      border-color: rgba(59,130,246,0.5);
      color: #93c5fd;
    }
    .form-grid { display: flex; flex-direction: column; gap: 11px; }
    .form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .field label { display: block; font-size: 0.75rem; font-weight: 700; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
    .field input, .field select, .field textarea {
      width: 100%;
      background: #0b1120;
      color: var(--text);
      border: 1px solid #334155;
      border-radius: 9px;
      padding: 9px 11px;
      font-size: 0.92rem;
      font-family: inherit;
    }
    .field input:focus, .field select:focus { outline: none; border-color: var(--blue); }
    .payout-preview {
      background: rgba(16,185,129,0.08);
      border: 1px solid rgba(16,185,129,0.25);
      border-radius: 10px;
      padding: 9px 12px;
      font-size: 0.84rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-footer { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
    .btn-primary {
      background: linear-gradient(135deg, var(--green), #059669);
      color: #fff;
      border: none;
      border-radius: 10px;
      padding: 10px 16px;
      font-weight: 700;
      font-size: 0.88rem;
      cursor: pointer;
    }
    .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-danger {
      background: rgba(239,68,68,0.2);
      color: #fca5a5;
      border: 1px solid rgba(239,68,68,0.45);
      border-radius: 10px;
      padding: 10px 14px;
      font-weight: 700;
      font-size: 0.86rem;
      cursor: pointer;
    }
    .help-text { font-size: 0.78rem; color: var(--muted); line-height: 1.4; }
    .toast {
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      background: #1e293b;
      border: 1px solid #475569;
      color: #f8fafc;
      padding: 10px 16px;
      border-radius: 12px;
      font-size: 0.85rem;
      font-weight: 600;
      box-shadow: 0 12px 30px rgba(0,0,0,0.45);
      z-index: 2000;
      display: none;
      max-width: 92vw;
      text-align: center;
    }
    .card-btn {
      cursor: pointer;
      text-align: left;
      width: 100%;
      font: inherit;
      color: inherit;
      transition: border-color 0.18s, transform 0.12s, background 0.18s;
    }
    .card-btn:hover {
      border-color: rgba(59, 130, 246, 0.55);
      background: #151f33;
    }
    .card-btn:active { transform: scale(0.99); }
    .card-tap-hint {
      font-size: 0.74rem;
      color: #60a5fa;
      font-weight: 700;
      margin-left: auto;
    }
    .wl-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 10px;
      margin-top: 8px;
    }
    .wl-cell {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 11px 13px;
    }
    .wl-cell-label {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 4px;
    }
    .wl-cell-val {
      font-size: 1.25rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }
    .wl-cell-sub {
      font-size: 0.78rem;
      color: var(--muted);
      margin-top: 4px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .day-group {
      border: 1px solid var(--card-border);
      border-radius: 13px;
      background: rgba(255, 255, 255, 0.015);
      padding: 11px 12px;
      margin-bottom: 12px;
    }
    .day-group-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      padding-bottom: 8px;
      margin-bottom: 9px;
      border-bottom: 1px solid var(--card-border);
      font-size: 0.85rem;
      font-weight: 700;
    }
    .toast.show { display: block; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1>
          🎯 Betting Curator Terminal
          <span class="pill pill-amber" id="sync-mode-pill">STATIC SNAPSHOT</span>
        </h1>
        <p id="updated-at">Loading live ledger...</p>
      </div>
      <div class="header-actions">
        <button class="btn-secondary" onclick="openSettingsModal()">⚙️ Live Sync</button>
        <button class="settle-btn" id="settle-btn" onclick="triggerSettleWorkflow()">🏁 Settle Refresh</button>
        <button class="refresh-btn" id="refresh-btn" onclick="loadData()">↻ Refresh Live</button>
      </div>
    </header>

    <div class="stack">
      <!-- 1. BANKROLL AT THE TOP -->
      <div class="card">
        <div class="card-label">💰 Live Bankroll &amp; Exposure</div>
        <div class="big-num" id="bankroll-val">--</div>
        <div class="sub-row" id="bankroll-sub"></div>
      </div>

      <!-- 2. ACTIVE PENDING TICKETS -->
      <div class="card">
        <div class="section-title">
          <span>⏳ Active Pending Tickets</span>
          <div class="section-right">
            <span class="pill pill-amber" id="pending-count">0 Active</span>
            <button class="btn-mini" id="settle-mini-btn" onclick="triggerSettleWorkflow()" style="border-color:rgba(16,185,129,0.45);color:#34d399;">🏁 Settle Refresh</button>
            <button class="btn-green" onclick="openLogModal()">+ Log Bet</button>
          </div>
        </div>
        <div class="bet-list" id="pending-list"></div>
      </div>

      <!-- 3. TODAY'S CURATED PICKS -->
      <div class="card">
        <div class="section-title">
          <span>🔥 Today's Curated Bets (<span id="curated-date">--</span>)</span>
          <span class="pill pill-blue" id="curated-count">0 Plays</span>
        </div>
        <div class="bet-list" id="curated-list"></div>
      </div>

      <!-- 4. COMBINED WIN-LOSS RECORDS BUTTON (OVERALL, THIS WEEK, YESTERDAY, TODAY) -->
      <button type="button" class="card card-btn" onclick="openGradedModal('all')">
        <div class="section-title" style="margin-bottom:4px;">
          <span>📊 Win / Loss Records &amp; Bet History</span>
          <span class="card-tap-hint" id="wl-tap-hint">Tap for Bet History by Day &rarr;</span>
        </div>
        <div class="wl-grid">
          <div class="wl-cell">
            <div class="wl-cell-label">Overall</div>
            <div class="wl-cell-val" id="overall-ratio">--</div>
            <div class="wl-cell-sub" id="overall-sub"></div>
          </div>
          <div class="wl-cell">
            <div class="wl-cell-label" id="week-label">This Week</div>
            <div class="wl-cell-val" id="week-ratio">--</div>
            <div class="wl-cell-sub" id="week-sub"></div>
          </div>
          <div class="wl-cell">
            <div class="wl-cell-label" id="yest-label">Yesterday</div>
            <div class="wl-cell-val" id="yest-ratio">--</div>
            <div class="wl-cell-sub" id="yest-sub"></div>
          </div>
          <div class="wl-cell">
            <div class="wl-cell-label" id="today-label">Today</div>
            <div class="wl-cell-val" id="today-ratio">--</div>
            <div class="wl-cell-sub" id="today-sub"></div>
          </div>
        </div>
      </button>

      <!-- 5. BANKROLL EQUITY CURVE GRAPH -->
      <div class="card">
        <div class="section-title">
          <span>📈 Bankroll Equity Curve ($100 Start)</span>
        </div>
        <div class="chart-wrap">
          <canvas id="equityChart"></canvas>
        </div>
      </div>
    </div>
  </div>

  <!-- MODAL 1: LOG BET (1-Tap Form + Screenshot OCR Upload) -->
  <div class="modal-backdrop" id="log-modal">
    <div class="modal">
      <div class="modal-header">
        <h3 id="log-modal-title">➕ Log Placed Bet</h3>
        <button class="close-btn" onclick="closeModal('log-modal')">&times;</button>
      </div>
      <div class="tabs">
        <button class="tab-btn active" id="tab-form-btn" onclick="switchLogTab('form')">⚡ Quick Bet Form</button>
        <button class="tab-btn" id="tab-shot-btn" onclick="switchLogTab('shot')">📸 Upload Screenshot</button>
      </div>

      <div id="log-tab-form" class="form-grid">
        <div class="form-row-2">
          <div class="field">
            <label>Sport</label>
            <select id="f-sport">
              <option value="CFB">CFB</option>
              <option value="NFL">NFL</option>
              <option value="NBA">NBA</option>
              <option value="NHL">NHL</option>
              <option value="NCAAM">NCAAM</option>
              <option value="EPL">EPL</option>
              <option value="UCL">UCL</option>
              <option value="MLB">MLB</option>
              <option value="ATP">ATP</option>
              <option value="WTA">WTA</option>
              <option value="Multi-Sport">Multi-Sport</option>
            </select>
          </div>
          <div class="field">
            <label>Bet Type</label>
            <select id="f-type">
              <option value="Straight">Straight</option>
              <option value="SGP">SGP</option>
              <option value="Parlay">Parlay</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>Selection &amp; Matchup</label>
          <input id="f-sel" type="text" placeholder="e.g. Middle Tennessee -6.5 (2:00 PM EST)" />
        </div>
        <div class="form-row-2">
          <div class="field">
            <label>American Odds</label>
            <input id="f-odds" type="text" inputmode="text" placeholder="-110" oninput="updatePayoutPreview()" />
          </div>
          <div class="field">
            <label>Stake ($)</label>
            <input id="f-stake" type="number" step="0.01" min="0.01" placeholder="10.00" oninput="updatePayoutPreview()" />
          </div>
        </div>
        <div class="payout-preview">
          <span style="color:var(--muted);">Estimated Payout</span>
          <strong id="f-payout-preview" style="color:var(--green);">$0.00</strong>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" onclick="closeModal('log-modal')">Cancel</button>
          <button class="btn-primary" id="f-submit-btn" onclick="submitLogBet()">✓ Confirm &amp; Log Bet</button>
        </div>
      </div>

      <div id="log-tab-shot" class="form-grid" style="display:none;">
        <div class="field">
          <label>Hard Rock Bet Screenshot</label>
          <input id="s-file" type="file" accept="image/*" />
        </div>
        <div class="field">
          <label>Optional Note / Caption</label>
          <input id="s-caption" type="text" placeholder="Optional: e.g. cashed out BET-051 and placed this" />
        </div>
        <p class="help-text">Gemini Vision parses the ticket and writes straight to your Google Sheet in ~3 seconds.</p>
        <div class="modal-footer">
          <button class="btn-secondary" onclick="closeModal('log-modal')">Cancel</button>
          <button class="btn-primary" id="s-submit-btn" onclick="submitScreenshot()">📸 Parse &amp; Log</button>
        </div>
      </div>
    </div>
  </div>

  <!-- MODAL 2: EDIT / CASHOUT EXISTING PENDING BET -->
  <div class="modal-backdrop" id="edit-modal">
    <div class="modal">
      <div class="modal-header">
        <h3 id="edit-modal-title">✏️ Manage Ticket</h3>
        <button class="close-btn" onclick="closeModal('edit-modal')">&times;</button>
      </div>
      <input type="hidden" id="e-row" />
      <input type="hidden" id="e-betid" />
      <div class="form-grid">
        <div class="form-row-2">
          <div class="field">
            <label>Sport</label>
            <input id="e-sport" type="text" />
          </div>
          <div class="field">
            <label>Result</label>
            <select id="e-result">
              <option value="Pending">Pending</option>
              <option value="Won">Won</option>
              <option value="Lost">Lost</option>
              <option value="Push">Push</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>Selection</label>
          <input id="e-sel" type="text" />
        </div>
        <div class="form-row-2">
          <div class="field">
            <label>Odds</label>
            <input id="e-odds" type="text" />
          </div>
          <div class="field">
            <label>Stake ($)</label>
            <input id="e-stake" type="number" step="0.01" />
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" onclick="closeModal('edit-modal')">Cancel</button>
          <button class="btn-primary" id="e-save-btn" onclick="submitEditBet()">Save Changes</button>
        </div>

        <hr style="border:none;border-top:1px solid var(--card-border);margin:6px 0;" />
        <div class="field">
          <label>Quick Cash Out / Void</label>
          <div class="form-row-2">
            <button class="btn-danger" id="e-void-btn" onclick="submitCashout(true)">🗑️ Full Refund / Void</button>
            <div style="display:flex;gap:6px;">
              <input id="e-cashout-ret" type="number" step="0.01" placeholder="Return $" />
              <button class="btn-secondary" id="e-partial-btn" onclick="submitCashout(false)" style="white-space:nowrap;">Cash Out</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- MODAL 3: LIVE SYNC SETTINGS -->
  <div class="modal-backdrop" id="settings-modal">
    <div class="modal">
      <div class="modal-header">
        <h3>⚙️ Instant Live Sheet &amp; Settle Sync</h3>
        <button class="close-btn" onclick="closeModal('settings-modal')">&times;</button>
      </div>
      <div class="form-grid">
        <p class="help-text">
          Connect your deployed Google Apps Script Web App (see <code>apps_script/SETUP.md</code>) for instant 1-tap bet logging, direct sheet settlement, and screenshot OCR — or add a GitHub PAT to dispatch the GitHub Actions Settle Workflow directly. Stored only on this device.
        </p>
        <div class="field">
          <label>Apps Script Web App URL (/exec)</label>
          <input id="cfg-url" type="url" placeholder="https://script.google.com/macros/s/.../exec" />
        </div>
        <div class="field">
          <label>Dashboard PIN</label>
          <input id="cfg-pin" type="password" placeholder="Your DASHBOARD_PIN secret" />
        </div>
        <div class="field">
          <label>GitHub Token (Optional — Triggers Settle Workflow)</label>
          <input id="cfg-gh-pat" type="password" placeholder="github_pat_... or ghp_... (Actions write scope)" />
        </div>
        <div class="modal-footer">
          <button class="btn-danger" onclick="clearLiveConfig()">Disconnect</button>
          <button class="btn-secondary" onclick="closeModal('settings-modal')">Cancel</button>
          <button class="btn-primary" id="cfg-save-btn" onclick="saveLiveConfig()">Save &amp; Connect</button>
        </div>
      </div>
    </div>
  </div>

  <!-- MODAL 4: BET HISTORY & GRADED TICKETS BY DAY -->
  <div class="modal-backdrop" id="graded-modal" onclick="if(event.target===this) closeModal('graded-modal')">
    <div class="modal" style="max-width:560px;">
      <div class="modal-header">
        <h3 id="graded-modal-title">📊 Bet History by Day</h3>
        <button class="close-btn" onclick="closeModal('graded-modal')">&times;</button>
      </div>
      <div class="tabs">
        <button class="tab-btn active" id="graded-tab-all" onclick="openGradedModal('all')">📊 Overall</button>
        <button class="tab-btn" id="graded-tab-week" onclick="openGradedModal('week')">📆 This Week</button>
        <button class="tab-btn" id="graded-tab-yesterday" onclick="openGradedModal('yesterday')">📅 Yesterday</button>
        <button class="tab-btn" id="graded-tab-today" onclick="openGradedModal('today')">🏁 Today</button>
      </div>
      <div class="sub-row" id="graded-modal-summary" style="margin-top:0;margin-bottom:12px;"></div>
      <div id="graded-modal-list"></div>
      <div class="modal-footer">
        <button class="btn-secondary" onclick="closeModal('graded-modal')">Close</button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const STATIC_DATA = __INITIAL_DATA_JSON__;
    const LS_URL_KEY = "bc_live_api_url";
    const LS_PIN_KEY = "bc_live_api_pin";
    const LS_GH_PAT_KEY = "bc_gh_pat";
    const GH_REPO = "iannuzzi8100/betting-curator";
    let chartInstance = null;
    let currentData = STATIC_DATA || null;
    const openDrawers = new Set();

    function toggleCuratedDetail(idx) {
      const el = document.getElementById(`curated-detail-${idx}`);
      const caret = document.getElementById(`curated-caret-${idx}`);
      if (!el) return;
      if (el.style.display === "none") {
        el.style.display = "block";
        openDrawers.add(idx);
        if (caret) caret.textContent = "▴ Hide details";
      } else {
        el.style.display = "none";
        openDrawers.delete(idx);
        if (caret) caret.textContent = "▾ Tap for details";
      }
    }

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function fmtMoney(n) {
      const num = Number(n) || 0;
      const sign = num < 0 ? "-" : "";
      return sign + "$" + Math.abs(num).toFixed(2);
    }

    function showToast(msg, ms) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => el.classList.remove("show"), ms || 3600);
    }

    function getLiveConfig() {
      return {
        url: (localStorage.getItem(LS_URL_KEY) || "").trim(),
        pin: (localStorage.getItem(LS_PIN_KEY) || "").trim(),
        ghPat: (localStorage.getItem(LS_GH_PAT_KEY) || "").trim(),
      };
    }

    function isLiveConfigured() {
      const cfg = getLiveConfig();
      return Boolean(cfg.url && cfg.pin);
    }

    function updateSyncPill(isLive) {
      const pill = document.getElementById("sync-mode-pill");
      if (!pill) return;
      if (isLive) {
        pill.className = "pill pill-green";
        pill.textContent = "● LIVE SHEET";
      } else {
        pill.className = "pill pill-amber";
        pill.textContent = "STATIC SNAPSHOT";
      }
    }

    function openModal(id) { document.getElementById(id).classList.add("open"); }
    function closeModal(id) { document.getElementById(id).classList.remove("open"); }

    function openSettingsModal() {
      const cfg = getLiveConfig();
      document.getElementById("cfg-url").value = cfg.url;
      document.getElementById("cfg-pin").value = cfg.pin;
      const ghEl = document.getElementById("cfg-gh-pat");
      if (ghEl) ghEl.value = cfg.ghPat;
      openModal("settings-modal");
    }

    async function saveLiveConfig() {
      const url = document.getElementById("cfg-url").value.trim();
      const pin = document.getElementById("cfg-pin").value.trim();
      const ghEl = document.getElementById("cfg-gh-pat");
      const ghPat = ghEl ? ghEl.value.trim() : "";

      if ((!url || !pin) && !ghPat) {
        showToast("Enter your Apps Script /exec URL + PIN, or a GitHub Token.");
        return;
      }

      if (ghPat) {
        localStorage.setItem(LS_GH_PAT_KEY, ghPat);
      } else {
        localStorage.removeItem(LS_GH_PAT_KEY);
      }

      if (!url || !pin) {
        closeModal("settings-modal");
        showToast("✅ Saved GitHub Token for Settle Workflow dispatch!");
        return;
      }

      const btn = document.getElementById("cfg-save-btn");
      btn.disabled = true;
      btn.textContent = "Testing...";
      try {
        const sep = url.includes("?") ? "&" : "?";
        const r = await fetch(`${url}${sep}action=dashboard&pin=${encodeURIComponent(pin)}&t=${Date.now()}`, {
          redirect: "follow",
          cache: "no-store",
        });
        const j = await r.json();
        if (!j.ok || !j.dashboard) {
          throw new Error(j.error || "Connection rejected");
        }
        localStorage.setItem(LS_URL_KEY, url);
        localStorage.setItem(LS_PIN_KEY, pin);
        render(j.dashboard);
        updateSyncPill(true);
        closeModal("settings-modal");
        showToast("✅ Connected to Live Google Sheet!");
      } catch (e) {
        showToast("❌ " + (e.message || "Could not connect to Apps Script"));
      } finally {
        btn.disabled = false;
        btn.textContent = "Save & Connect";
      }
    }

    function clearLiveConfig() {
      localStorage.removeItem(LS_URL_KEY);
      localStorage.removeItem(LS_PIN_KEY);
      localStorage.removeItem(LS_GH_PAT_KEY);
      updateSyncPill(false);
      closeModal("settings-modal");
      showToast("Disconnected Live Sync (using static snapshot).");
    }

    async function postLiveApi(payload) {
      const cfg = getLiveConfig();
      if (!cfg.url || !cfg.pin) {
        openSettingsModal();
        throw new Error("Connect Live Sync first (paste your Apps Script URL + PIN).");
      }
      const r = await fetch(cfg.url, {
        method: "POST",
        redirect: "follow",
        headers: { "Content-Type": "text/plain;charset=utf-8" },
        body: JSON.stringify({ ...payload, pin: cfg.pin }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || "API error");
      return j;
    }

    async function dispatchGithubSettleWorkflow(ghPat) {
      const r = await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/daily_curator.yml/dispatches`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${ghPat}`,
          "Accept": "application/vnd.github+json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { stage: "settler" } }),
      });
      if (!r.ok) {
        let errTxt = `GitHub HTTP ${r.status}`;
        try {
          const ej = await r.json();
          if (ej && ej.message) errTxt = ej.message;
        } catch (_) {}
        throw new Error(errTxt);
      }
      return true;
    }

    function pollAfterSettleDispatch() {
      clearInterval(window.__settlePollTimer);
      const startBr = currentData ? currentData.bankroll : null;
      const startPendCnt = currentData && currentData.pending_tickets ? currentData.pending_tickets.length : null;
      const startUpd = currentData ? currentData.updated_at : null;
      let attempts = 0;
      window.__settlePollTimer = setInterval(async () => {
        attempts++;
        await loadData();
        if (currentData && (currentData.bankroll !== startBr || (currentData.pending_tickets || []).length !== startPendCnt || currentData.updated_at !== startUpd)) {
          clearInterval(window.__settlePollTimer);
          const brChanged = startBr !== null && Math.abs(Number(currentData.bankroll) - Number(startBr)) >= 0.01;
          showToast(
            brChanged
              ? `✅ Settle workflow complete! Bankroll: ${fmtMoney(startBr)} → ${fmtMoney(currentData.bankroll)} (remaining wagers adjusted)`
              : `✅ Settle workflow complete! Ledger synced (${currentData.updated_at})`,
            5200
          );
        } else if (attempts >= 9) {
          clearInterval(window.__settlePollTimer);
        }
      }, 10000);
    }

    async function triggerSettleWorkflow() {
      const btn = document.getElementById("settle-btn");
      const miniBtn = document.getElementById("settle-mini-btn");
      const setBusy = (busy) => {
        if (btn) {
          btn.disabled = busy;
          btn.textContent = busy ? "⏳ Settling..." : "🏁 Settle Refresh";
        }
        if (miniBtn) {
          miniBtn.disabled = busy;
          miniBtn.textContent = busy ? "⏳ Settling..." : "🏁 Settle Refresh";
        }
      };

      setBusy(true);
      try {
        const cfg = getLiveConfig();
        const isLocalServer = (window.location.protocol.startsWith("http") && !STATIC_DATA) || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";

        // 1. Live Google Apps Script settlement + wager recalculation
        if (cfg.url && cfg.pin) {
          const res = await postLiveApi({ action: "settle", stage: "settler" });
          if (res.dashboard) render(res.dashboard);

          // If user also saved a GitHub PAT in browser and Apps Script didn't trigger GitHub, dispatch GitHub workflow too
          let ghDispatched = Boolean(res.github_triggered);
          if (!ghDispatched && cfg.ghPat) {
            try {
              await dispatchGithubSettleWorkflow(cfg.ghPat);
              ghDispatched = true;
            } catch (_) {}
          }

          const settledCnt = Number(res.settled_count || 0);
          const adjList = (res.adjusted_plays || []).filter(a => a && a.new_stake > 0);
          if (settledCnt > 0) {
            const adjSummary = adjList.length > 0
              ? ` • Adjusted ${adjList.length} remaining bet${adjList.length === 1 ? "" : "s"} (${adjList.map(a => fmtMoney(a.new_stake)).join(", ")})`
              : "";
            showToast(`✅ Settled ${settledCnt} bet${settledCnt === 1 ? "" : "s"}! BR: ${fmtMoney(res.prev_bankroll)} → ${fmtMoney(res.new_bankroll)}${adjSummary}`, 5500);
          } else if (adjList.length > 0 && adjList.some(a => a.changed)) {
            showToast(`🔄 Recalculated remaining wagers for ${fmtMoney(res.new_bankroll)} BR (${adjList.map(a => fmtMoney(a.new_stake)).join(", ")})`, 4800);
          } else {
            showToast(`🏁 Checked ESPN scores (0 newly final) • Remaining wagers synced to ${fmtMoney(res.new_bankroll)} BR${ghDispatched ? " • GitHub workflow triggered" : ""}`, 4200);
          }
          return;
        }

        // 2. Local Python server mode (/api/settle)
        if (isLocalServer) {
          const r = await fetch("/api/settle", { method: "POST" });
          const j = await r.json();
          if (!j.ok) throw new Error(j.error || "Settlement failed");
          if (j.dashboard) render(j.dashboard);
          const settledCnt = Number(j.settled_count || 0);
          if (settledCnt > 0) {
            showToast(`✅ Settled ${settledCnt} bet${settledCnt === 1 ? "" : "s"}! BR: ${fmtMoney(j.prev_bankroll)} → ${fmtMoney(j.new_bankroll)} (remaining wagers adjusted)`, 5200);
          } else {
            showToast(`🏁 Settlement checked • Remaining wagers synced to ${fmtMoney(j.new_bankroll)} BR`, 4000);
          }
          return;
        }

        // 3. Direct GitHub Actions Workflow Dispatch from browser
        if (cfg.ghPat) {
          await dispatchGithubSettleWorkflow(cfg.ghPat);
          await loadData();
          pollAfterSettleDispatch();
          showToast("🚀 Settle Workflow triggered on GitHub! Auto-refreshing ledger & wager amounts...", 5200);
          return;
        }

        // 4. Fallback: refresh latest data.json, recalculate remaining wagers client-side, and prompt 1-time setup
        await loadData();
        openSettingsModal();
        showToast("Connect Live Sync or paste a GitHub Token once to trigger the Settle Workflow in 1 tap.", 5000);
      } catch (e) {
        showToast("❌ " + (e.message || "Could not run Settle Refresh"), 4800);
      } finally {
        setBusy(false);
      }
    }

    function switchLogTab(tab) {
      document.getElementById("log-tab-form").style.display = tab === "form" ? "flex" : "none";
      document.getElementById("log-tab-shot").style.display = tab === "shot" ? "flex" : "none";
      document.getElementById("tab-form-btn").classList.toggle("active", tab === "form");
      document.getElementById("tab-shot-btn").classList.toggle("active", tab === "shot");
    }

    function updatePayoutPreview() {
      const odds = parseInt(String(document.getElementById("f-odds").value || "").replace(/\+/g, ""), 10);
      const stake = parseFloat(document.getElementById("f-stake").value || "0");
      let payout = 0;
      if (!isNaN(odds) && odds !== 0 && !isNaN(stake) && stake > 0) {
        payout = odds > 0 ? stake * (1 + odds / 100) : stake * (1 + 100 / Math.abs(odds));
      }
      document.getElementById("f-payout-preview").textContent = fmtMoney(payout);
    }

    function parseTitleFallback(title) {
      let sport = "CFB", gameTime = "", rest = String(title || "").trim();
      const bm = rest.match(/^\[([^\]]+)\]\s*(.*)$/);
      if (bm) {
        rest = bm[2].trim();
        const parts = bm[1].split(/[—–•-]/).map(s => s.trim()).filter(Boolean);
        if (parts.length >= 2) { gameTime = parts[0]; sport = parts[parts.length - 1].toUpperCase(); }
        else if (parts.length === 1) {
          if (/\b(?:AM|PM|EST|EDT|CST|PST)\b/i.test(parts[0]) || /\d:\d{2}/.test(parts[0])) {
            gameTime = parts[0];
          } else {
            sport = parts[0].toUpperCase();
          }
        }
      }
      let betSeg = rest;
      const smStream = rest.match(/^[•·]\s*[^—]+?\(([A-Za-z0-9/ ]{2,10})\)\s*[—–]\s*(.+)$/);
      if (smStream) {
        sport = smStream[1].trim().toUpperCase();
        betSeg = smStream[2].trim();
      }
      const om = betSeg.match(/\(([+-]\d{3,4})\)/) || betSeg.match(/\s([+-]\d{3,4})(?:\s|$|—|–)/);
      const sm = betSeg.match(/\$(\d+(?:\.\d{1,2})?)/) || rest.match(/\$(\d+(?:\.\d{1,2})?)/);
      let pick = om ? betSeg.slice(0, om.index).trim() : betSeg.split("—")[0].trim();
      if (gameTime && pick && !pick.includes(gameTime)) pick += ` (${gameTime})`;
      return {
        sport,
        selection: pick,
        odds: om ? parseInt(om[1], 10) : -110,
        stake: sm ? parseFloat(sm[1]) : 0,
      };
    }

    function extractSelectionCore(text) {
      let s = String(text || "").trim();
      s = s.replace(/^\d+\.\s*/, "").replace(/\*/g, "").trim();
      s = s.replace(/^\[[^\]]+\]\s*/, "").trim();
      const core = s.split(/(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)/i)[0];
      return core.toLowerCase().replace(/[^a-z0-9\s]/g, "").trim();
    }

    function adjustCuratedPlaysClientSide(data) {
      if (!data || !Array.isArray(data.curated_plays) || data.curated_plays.length === 0) return;
      const settledBr = Number(data.bankroll || 0);
      if (settledBr <= 0) return;
      const pendingList = data.pending_tickets || [];
      const todayList = (data.today_stats && data.today_stats.tickets) || [];
      const raw = String(data.curated_raw || "");
      const brMatch = raw.match(/Bankroll:\s*\$(\d+(?:\.\d{1,2})?)/i);
      const feedBr = brMatch ? parseFloat(brMatch[1]) : settledBr;

      const winCounts = {};
      data.curated_plays.forEach(p => {
        const t = String(p.title || "");
        const isPass = t.includes("⛔") || /\bPASS\b/.test(t);
        if (!isPass && p.game_time) {
          winCounts[p.game_time] = (winCounts[p.game_time] || 0) + 1;
        }
      });

      data.curated_plays.forEach(p => {
        const title = String(p.title || "");
        const isPass = title.includes("⛔") || /\bPASS\b/.test(title);
        if (isPass) {
          p.status = "pass";
          return;
        }
        const fb = parseTitleFallback(title);
        const core = extractSelectionCore(p.selection || fb.selection || title);
        const matchPend = (core && core.length >= 3)
          ? pendingList.find(t => String(t.selections || "").toLowerCase().includes(core))
          : null;
        const matchSettled = (!matchPend && core && core.length >= 3)
          ? todayList.find(t => String(t.selections || "").toLowerCase().includes(core))
          : null;

        let wagerPct = p.wager_pct != null ? Number(p.wager_pct) : null;
        if (!wagerPct) {
          const pm = title.match(/(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s+(?:Resulting\s+)?)?(?:BR|Bankroll)/i);
          if (pm) {
            wagerPct = parseFloat(pm[1]);
          } else if (p.confidence_pct != null) {
            const cp = Number(p.confidence_pct);
            const dual = (winCounts[p.game_time] || 1) >= 2;
            if (cp >= 88) wagerPct = dual ? 35 : 50;
            else if (cp >= 78) wagerPct = dual ? 25 : 35;
            else if (cp >= 68) wagerPct = dual ? 18 : 20;
          } else if (feedBr > 0 && p.stake > 0) {
            wagerPct = Math.round((Number(p.stake) / feedBr) * 1000) / 10;
          }
        }
        if (wagerPct) p.wager_pct = wagerPct;

        if (matchPend) {
          p.status = "pending";
          p.pending_bet_id = matchPend.bet_id;
          p.stake = Number(matchPend.stake || p.stake || 0);
          if (wagerPct) {
            p.recalculated_stake = Math.round((settledBr >= 2 ? Math.max(2, settledBr * (wagerPct / 100)) : settledBr) * 100) / 100;
          }
          return;
        }
        if (matchSettled) {
          p.status = "settled";
          p.settled_bet_id = matchSettled.bet_id;
          p.settled_result = matchSettled.result;
          p.settled_pl = Number(matchSettled.net_pl || 0);
          return;
        }

        p.status = "remaining";
        if (!wagerPct || wagerPct <= 0) return;

        const baseOldStake = p.original_stake != null ? Number(p.original_stake) : Number(p.stake || fb.stake || 0);
        const newStake = Math.round((settledBr >= 2 ? Math.max(2, settledBr * (wagerPct / 100)) : settledBr) * 100) / 100;
        if (baseOldStake > 0 && Math.abs(newStake - baseOldStake) >= 0.01) {
          p.original_stake = baseOldStake;
          p.stake_adjusted = true;
        }
        p.stake = newStake;
        p.title = title.replace(/\$(\d+(?:\.\d{1,2})?)(?!.*?\$\d)/, "$" + newStake.toFixed(2));
      });
    }

    function openLogModal(prefillPlay) {
      if (!isLiveConfigured()) {
        openSettingsModal();
        showToast("Set up Live Sync once to log bets directly in 1 tap.");
        return;
      }
      switchLogTab("form");
      if (prefillPlay) {
        const fb = parseTitleFallback(prefillPlay.title);
        const sport = prefillPlay.sport || fb.sport;
        const sel = prefillPlay.selection || fb.selection;
        const odds = prefillPlay.odds != null ? prefillPlay.odds : fb.odds;
        const stake = (prefillPlay.stake && prefillPlay.stake > 0) ? prefillPlay.stake : (fb.stake || (currentData && currentData.available_cash ? Math.min(currentData.available_cash, 10) : 5));
        document.getElementById("log-modal-title").textContent = "⚡ Confirm Curated Bet";
        const spEl = document.getElementById("f-sport");
        if ([...spEl.options].some(o => o.value === sport)) spEl.value = sport;
        document.getElementById("f-type").value = "Straight";
        document.getElementById("f-sel").value = sel;
        document.getElementById("f-odds").value = odds >= 0 ? `+${odds}` : String(odds);
        document.getElementById("f-stake").value = Number(stake).toFixed(2);
      } else {
        document.getElementById("log-modal-title").textContent = "➕ Log Placed Bet";
        document.getElementById("f-sel").value = "";
        document.getElementById("f-odds").value = "-110";
        document.getElementById("f-stake").value = "";
      }
      updatePayoutPreview();
      openModal("log-modal");
    }

    async function submitLogBet() {
      const btn = document.getElementById("f-submit-btn");
      const sport = document.getElementById("f-sport").value;
      const bet_type = document.getElementById("f-type").value;
      const selections = document.getElementById("f-sel").value.trim();
      const odds = document.getElementById("f-odds").value.trim();
      const stake = parseFloat(document.getElementById("f-stake").value);
      if (!selections || !stake || stake <= 0) {
        showToast("Enter a selection and valid stake.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Logging...";
      try {
        const res = await postLiveApi({ action: "log_bet", sport, bet_type, selections, odds, stake });
        if (res.dashboard) render(res.dashboard);
        closeModal("log-modal");
        showToast(`✅ Logged ${res.bet_id || "bet"} straight to Sheet!`);
      } catch (e) {
        showToast("❌ " + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "✓ Confirm & Log Bet";
      }
    }

    function fileToBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }

    async function submitScreenshot() {
      const fileInput = document.getElementById("s-file");
      const caption = document.getElementById("s-caption").value.trim();
      const file = fileInput.files && fileInput.files[0];
      if (!file && !caption) {
        showToast("Pick a screenshot or enter a text note.");
        return;
      }
      const btn = document.getElementById("s-submit-btn");
      btn.disabled = true;
      btn.textContent = "Parsing...";
      try {
        let image_base64 = "";
        let mime_type = "image/jpeg";
        if (file) {
          image_base64 = await fileToBase64(file);
          mime_type = file.type || "image/jpeg";
        }
        const res = await postLiveApi({ action: "log_screenshot", image_base64, mime_type, caption });
        if (res.dashboard) render(res.dashboard);
        fileInput.value = "";
        document.getElementById("s-caption").value = "";
        closeModal("log-modal");
        showToast("✅ Screenshot parsed & logged!");
      } catch (e) {
        showToast("❌ " + e.message, 4500);
      } finally {
        btn.disabled = false;
        btn.textContent = "📸 Parse & Log";
      }
    }

    function openEditModal(ticket) {
      if (!isLiveConfigured()) {
        openSettingsModal();
        showToast("Connect Live Sync once to edit or cash out tickets.");
        return;
      }
      document.getElementById("edit-modal-title").textContent = `✏️ Manage ${ticket.bet_id}`;
      document.getElementById("e-row").value = ticket.row_index || "";
      document.getElementById("e-betid").value = ticket.bet_id || "";
      document.getElementById("e-sport").value = ticket.sport || "";
      document.getElementById("e-result").value = "Pending";
      document.getElementById("e-sel").value = ticket.selections || "";
      document.getElementById("e-odds").value = ticket.odds || "";
      document.getElementById("e-stake").value = ticket.stake != null ? Number(ticket.stake).toFixed(2) : "";
      document.getElementById("e-cashout-ret").value = "";
      openModal("edit-modal");
    }

    async function submitEditBet() {
      const btn = document.getElementById("e-save-btn");
      btn.disabled = true;
      btn.textContent = "Saving...";
      try {
        const res = await postLiveApi({
          action: "edit_bet",
          row_index: document.getElementById("e-row").value || null,
          bet_id: document.getElementById("e-betid").value,
          sport: document.getElementById("e-sport").value.trim(),
          result: document.getElementById("e-result").value,
          selections: document.getElementById("e-sel").value.trim(),
          odds: document.getElementById("e-odds").value.trim(),
          stake: document.getElementById("e-stake").value,
        });
        if (res.dashboard) render(res.dashboard);
        closeModal("edit-modal");
        showToast("✅ Ticket updated in Sheet!");
      } catch (e) {
        showToast("❌ " + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "Save Changes";
      }
    }

    async function submitCashout(isFullRefund) {
      const betId = document.getElementById("e-betid").value;
      const rowIndex = document.getElementById("e-row").value || null;
      const retVal = document.getElementById("e-cashout-ret").value;
      if (!isFullRefund && (retVal === "" || isNaN(parseFloat(retVal)))) {
        showToast("Enter the cashout return dollar amount.");
        return;
      }
      const btn = document.getElementById(isFullRefund ? "e-void-btn" : "e-partial-btn");
      btn.disabled = true;
      try {
        const res = await postLiveApi({
          action: "cashout_bet",
          bet_id: betId,
          row_index: rowIndex,
          is_full_refund: isFullRefund,
          cashout_return: isFullRefund ? null : parseFloat(retVal),
        });
        if (res.dashboard) render(res.dashboard);
        closeModal("edit-modal");
        showToast(isFullRefund ? `🗑️ Removed ${betId} (full refund)` : `🔄 Cashed out ${betId}`);
      } catch (e) {
        showToast("❌ " + e.message);
      } finally {
        btn.disabled = false;
      }
    }

    function renderGradedModal(which) {
      which = which || "all";
      window.__activeGradedTab = which;
      const statsMap = window.__gradedStats || {};
      const st = statsMap[which] || { label: "", wins: 0, losses: 0, pushes: 0, win_pct: 0, net_pl: 0, tickets: [] };
      const todayDate = (currentData && currentData.today) || "";
      const yestDate = (currentData && currentData.yesterday) || "";

      ["all", "week", "yesterday", "today"].forEach(tabKey => {
        const el = document.getElementById(`graded-tab-${tabKey}`);
        if (el) el.classList.toggle("active", tabKey === which);
      });

      const titles = {
        all: "📊 Overall Bet History (Separated by Day)",
        week: `📆 This Week's Bet History (${st.date || "Last 7 Days"})`,
        yesterday: `📅 Yesterday's Bet History${st.date ? ` (${st.date})` : ""}`,
        today: `🏁 Today's Bet History${st.date ? ` (${st.date})` : ""}`,
      };
      document.getElementById("graded-modal-title").textContent = titles[which] || "📊 Bet History by Day";

      const tix = st.tickets || [];
      const plCls = st.net_pl >= 0 ? "pos" : "neg";
      const extraMeta = (which === "all" && st.roi !== undefined)
        ? `<span>ROI: <span class="${st.roi >= 0 ? "pos" : "neg"}">${st.roi >= 0 ? "+" : ""}${st.roi}%</span></span>`
        : "";
      document.getElementById("graded-modal-summary").innerHTML =
        `<span class="pill pill-blue">${st.total_count || tix.length} Settled</span>` +
        `<span>Record: <strong>${st.wins}W - ${st.losses}L${st.pushes ? ` - ${st.pushes}P` : ""} (${st.win_pct}%)</strong></span>` +
        `<span>Net P/L: <span class="${plCls}">${st.net_pl >= 0 ? "+" : ""}${fmtMoney(st.net_pl)}</span></span>` +
        extraMeta;

      const listEl = document.getElementById("graded-modal-list");
      if (tix.length === 0) {
        listEl.innerHTML = `<div class="empty-msg">No settled bets recorded for this period yet.</div>`;
        return;
      }

      // Group tickets by day (newest date first)
      const dayGroups = [];
      const dayMap = {};
      tix.forEach(t => {
        const dKey = String(t.date || "Earlier Challenge History").trim();
        if (!dayMap[dKey]) {
          dayMap[dKey] = { date: dKey, wins: 0, losses: 0, pushes: 0, net_pl: 0, tickets: [] };
          dayGroups.push(dayMap[dKey]);
        }
        const g = dayMap[dKey];
        const rl = String(t.result || "").toLowerCase();
        if (rl === "won") g.wins++;
        else if (rl === "lost") g.losses++;
        else if (rl === "push") g.pushes++;
        g.net_pl = Math.round((g.net_pl + Number(t.net_pl || 0)) * 100) / 100;
        g.tickets.push(t);
      });

      dayGroups.sort((a, b) => {
        if (a.date === "Earlier Challenge History") return 1;
        if (b.date === "Earlier Challenge History") return -1;
        return b.date.localeCompare(a.date);
      });

      listEl.innerHTML = dayGroups.map(g => {
        const dTot = g.wins + g.losses;
        const dPct = dTot > 0 ? Math.round((g.wins / dTot) * 1000) / 10 : 0;
        const dCls = g.net_pl >= 0 ? "pos" : "neg";
        let dayTitle = `📅 ${esc(g.date)}`;
        if (g.date === todayDate) dayTitle += " (Today)";
        else if (g.date === yestDate) dayTitle += " (Yesterday)";

        const dayRows = g.tickets.map(t => {
          const rl = String(t.result || "").toLowerCase();
          const pClass = rl === "won" ? "pill-green" : (rl === "push" ? "pill-amber" : "pill-red");
          const oddsTxt = t.odds ? ` (${esc(t.odds)})` : "";
          const metaParts = [];
          if (t.sport) metaParts.push(esc(t.sport));
          if (t.stake != null && t.stake > 0) metaParts.push(`Stake ${fmtMoney(t.stake)}`);
          return `
            <div class="bet-item">
              <div class="bet-top">
                <div class="bet-title">${esc(t.bet_id)}${t.selections ? ` • ${esc(t.selections)}` : ""}${oddsTxt}</div>
                <span class="pill ${pClass}">${esc((t.result || "").toUpperCase())} (${t.net_pl >= 0 ? "+" : ""}${fmtMoney(t.net_pl)})</span>
              </div>
              ${metaParts.length > 0 ? `<div class="bet-meta">${metaParts.join(" &bull; ")}</div>` : ""}
            </div>
          `;
        }).join("");

        return `
          <div class="day-group">
            <div class="day-group-header">
              <span>${dayTitle}</span>
              <span style="display:flex;gap:10px;align-items:center;">
                <span style="color:var(--muted);">${g.wins}W - ${g.losses}L${g.pushes ? ` - ${g.pushes}P` : ""} (${dPct}%)</span>
                <span class="${dCls}">${g.net_pl >= 0 ? "+" : ""}${fmtMoney(g.net_pl)}</span>
              </span>
            </div>
            <div class="bet-list">${dayRows}</div>
          </div>
        `;
      }).join("");
    }

    function openGradedModal(which) {
      renderGradedModal(which || "all");
      openModal("graded-modal");
    }

    function render(data) {
      adjustCuratedPlaysClientSide(data);
      currentData = data;
      updateSyncPill(Boolean(data.live_mode));
      document.getElementById("updated-at").textContent = "Last synced: " + data.updated_at;

      // 1. Bankroll (Top)
      document.getElementById("bankroll-val").textContent = fmtMoney(data.bankroll);
      document.getElementById("bankroll-sub").innerHTML =
        `<span>Active Pending: <span class="warn">${fmtMoney(data.pending_total)}</span></span>` +
        `<span>Avail Cash: <span class="pos">${fmtMoney(data.available_cash)}</span></span>`;

      // Normalize Today & Yesterday tickets with explicit date field
      const ts = data.today_stats || { date: data.today || "", wins: 0, losses: 0, pushes: 0, win_pct: 0, net_pl: 0, tickets: [] };
      (ts.tickets || []).forEach(t => { if (!t.date) t.date = ts.date || data.today || ""; });

      const ys = data.yesterday_stats || { date: data.yesterday || "", wins: 0, losses: 0, pushes: 0, win_pct: 0, net_pl: 0, tickets: [] };
      (ys.tickets || []).forEach(t => { if (!t.date) t.date = ys.date || data.yesterday || ""; });

      // Build complete settled tickets list (uses data.settled_tickets when present, with fallback to today + yesterday + equity curve)
      let allSettled = [];
      const seenIds = {};
      const pushUnique = (t, fallbackDate) => {
        if (!t || !t.bet_id || seenIds[t.bet_id]) return;
        seenIds[t.bet_id] = true;
        allSettled.push({ ...t, date: t.date || fallbackDate || "" });
      };
      if (Array.isArray(data.settled_tickets) && data.settled_tickets.length > 0) {
        data.settled_tickets.forEach(t => pushUnique(t, ""));
      }
      (ts.tickets || []).forEach(t => pushUnique(t, ts.date));
      (ys.tickets || []).forEach(t => pushUnique(t, ys.date));
      if (data.week_stats && Array.isArray(data.week_stats.tickets)) {
        data.week_stats.tickets.forEach(t => pushUnique(t, ""));
      }
      // If static snapshot didn't have older rows in settled_tickets yet, reconstruct delta from equity_curve
      const eq = data.equity_curve || [];
      const derivedOlder = [];
      for (let i = 1; i < eq.length; i++) {
        const bid = eq[i].label;
        const delta = Math.round((Number(eq[i].bankroll) - Number(eq[i - 1].bankroll)) * 100) / 100;
        if (bid && bid.startsWith("BET-") && !seenIds[bid]) {
          derivedOlder.push({
            bet_id: bid,
            date: "Earlier Challenge History",
            sport: "Challenge Ledger",
            selections: `Settled Challenge Ticket (Running BR: ${fmtMoney(eq[i].bankroll)})`,
            odds: "",
            stake: delta < 0 ? Math.abs(delta) : 0,
            result: delta > 0 ? "Won" : (delta < 0 ? "Lost" : "Push"),
            net_pl: delta,
          });
        }
      }
      derivedOlder.reverse().forEach(t => pushUnique(t, "Earlier Challenge History"));

      // Compute This Week stats (use data.week_stats if provided, otherwise derive from recent dated tickets)
      let ws = data.week_stats;
      if (!ws) {
        const datedWeekTix = allSettled.filter(t => t.date && /^\d{4}-\d{2}-\d{2}$/.test(t.date));
        let wW = 0, wL = 0, wP = 0, wPl = 0;
        datedWeekTix.forEach(t => {
          const rl = String(t.result || "").toLowerCase();
          if (rl === "won") wW++;
          else if (rl === "lost") wL++;
          else if (rl === "push") wP++;
          wPl += Number(t.net_pl || 0);
        });
        const wTot = wW + wL;
        ws = {
          date: "Last 7 Days",
          wins: wW,
          losses: wL,
          pushes: wP,
          win_pct: wTot > 0 ? Math.round((wW / wTot) * 1000) / 10 : 0,
          net_pl: Math.round(wPl * 100) / 100,
          tickets: datedWeekTix,
        };
      } else {
        ws.date = ws.start_date && ws.end_date ? `${ws.start_date} to ${ws.end_date}` : "Last 7 Days";
      }

      // 4. Populate Combined Win / Loss Button (Overall, This Week, Yesterday, Today)
      const ov = data.overall;
      const totalSettledCount = (ov.wins || 0) + (ov.losses || 0) + (ov.pushes || 0);
      const tapHintEl = document.getElementById("wl-tap-hint");
      if (tapHintEl) tapHintEl.innerHTML = `${totalSettledCount} settled bets &bull; View by Day &rarr;`;

      document.getElementById("overall-ratio").innerHTML =
        `${ov.wins}W - ${ov.losses}L <span style="font-size:0.9rem;color:var(--muted)">(${ov.win_pct}%)</span>`;
      const plCls = ov.net_pl >= 0 ? "pos" : "neg";
      const clvHtml = (ov.avg_clv !== null && ov.avg_clv !== undefined)
        ? `<span>CLV: <span class="${ov.avg_clv >= 0 ? "pos" : "neg"}">${ov.avg_clv >= 0 ? "+" : ""}${ov.avg_clv}%</span></span>`
        : "";
      document.getElementById("overall-sub").innerHTML =
        `<span>P/L: <span class="${plCls}">${ov.net_pl >= 0 ? "+" : ""}${fmtMoney(ov.net_pl)}</span></span>` +
        `<span>ROI: <span class="${plCls}">${ov.roi >= 0 ? "+" : ""}${ov.roi}%</span></span>` +
        clvHtml;

      document.getElementById("week-label").textContent = "This Week";
      document.getElementById("week-ratio").innerHTML =
        `${ws.wins}W - ${ws.losses}L` + (ws.pushes ? ` - ${ws.pushes}P` : "") +
        ` <span style="font-size:0.9rem;color:var(--muted)">(${ws.win_pct}%)</span>`;
      const wCls = ws.net_pl >= 0 ? "pos" : "neg";
      document.getElementById("week-sub").innerHTML =
        `<span>P/L: <span class="${wCls}">${ws.net_pl >= 0 ? "+" : ""}${fmtMoney(ws.net_pl)}</span></span>`;

      document.getElementById("yest-label").textContent = `Yesterday (${ys.date})`;
      document.getElementById("yest-ratio").innerHTML =
        `${ys.wins}W - ${ys.losses}L` + (ys.pushes ? ` - ${ys.pushes}P` : "") +
        ` <span style="font-size:0.9rem;color:var(--muted)">(${ys.win_pct}%)</span>`;
      const yCls = ys.net_pl >= 0 ? "pos" : "neg";
      document.getElementById("yest-sub").innerHTML =
        `<span>P/L: <span class="${yCls}">${ys.net_pl >= 0 ? "+" : ""}${fmtMoney(ys.net_pl)}</span></span>`;

      document.getElementById("today-label").textContent = `Today (${ts.date})`;
      document.getElementById("today-ratio").innerHTML =
        `${ts.wins}W - ${ts.losses}L` + (ts.pushes ? ` - ${ts.pushes}P` : "") +
        ` <span style="font-size:0.9rem;color:var(--muted)">(${ts.win_pct}%)</span>`;
      const tCls = ts.net_pl >= 0 ? "pos" : "neg";
      document.getElementById("today-sub").innerHTML =
        `<span>P/L: <span class="${tCls}">${ts.net_pl >= 0 ? "+" : ""}${fmtMoney(ts.net_pl)}</span></span>`;

      // 3. Today's Curated Bets (with 1-Tap Log Bet button + bankroll-adjusted wager badges)
      document.getElementById("curated-date").textContent = data.curated_date;
      const cList = document.getElementById("curated-list");
      const plays = data.curated_plays || [];
      const pendingList = data.pending_tickets || [];
      const todaySettledList = (ts && ts.tickets) || [];
      window.__curatedPlays = plays;
      document.getElementById("curated-count").textContent = `${plays.length} Play${plays.length === 1 ? "" : "s"}`;

      function findMatchingPending(play) {
        const fb = parseTitleFallback(play.title);
        const core = extractSelectionCore(play.selection || fb.selection || play.title || "");
        if (!core || core.length < 3) return null;
        return pendingList.find(t => (t.selections || "").toLowerCase().includes(core)) || null;
      }

      function findMatchingSettledToday(play) {
        const fb = parseTitleFallback(play.title);
        const core = extractSelectionCore(play.selection || fb.selection || play.title || "");
        if (!core || core.length < 3) return null;
        return todaySettledList.find(t => (t.selections || "").toLowerCase().includes(core)) || null;
      }

      if (plays.length > 0) {
        cList.innerHTML = plays.map((p, idx) => {
          const isPass = Boolean(p.is_pass) || (p.title || "").includes("⛔") || /\bPASS\b/.test(p.title || "") || /NO GOOD PLAY/i.test(p.title || "");
          const existingPend = isPass ? null : findMatchingPending(p);
          const existingSettled = (isPass || existingPend) ? null : findMatchingSettledToday(p);
          const cp = isPass ? null : p.confidence_pct;
          let meterHtml = "";
          let pillCls = isPass ? "pill-red" : "pill-blue";
          let pillTxt = isPass ? "PASS" : "CURATED";
          if (cp !== null && cp !== undefined) {
            const barColor = cp >= 88 ? "#10b981" : (cp >= 78 ? "#3b82f6" : (cp >= 68 ? "#f59e0b" : "#ef4444"));
            pillCls = cp >= 88 ? "pill-green" : (cp >= 78 ? "pill-blue" : (cp >= 68 ? "pill-amber" : "pill-red"));
            pillTxt = `${cp}%`;
            meterHtml = `
              <div style="margin-top:8px;">
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.75rem;font-weight:700;color:${barColor};margin-bottom:4px;">
                  <span>${esc(p.confidence_tier || "Confidence Meter")}</span>
                  <span style="color:var(--muted);font-weight:500;" id="curated-caret-${idx}">${openDrawers.has(idx) ? "▴ Hide details" : "▾ Tap for details"}</span>
                </div>
                <div style="width:100%;height:6px;background:rgba(255,255,255,0.08);border-radius:999px;overflow:hidden;">
                  <div style="width:${Math.min(100, Math.max(0, cp))}%;height:100%;background:${barColor};border-radius:999px;"></div>
                </div>
              </div>
            `;
          } else if (!isPass && (p.game_line || p.confidence_math || p.edge)) {
            meterHtml = `
              <div style="margin-top:6px;display:flex;justify-content:flex-end;font-size:0.75rem;color:var(--muted);">
                <span id="curated-caret-${idx}">${openDrawers.has(idx) ? "▴ Hide details" : "▾ Tap for details"}</span>
              </div>
            `;
          }

          const rawMath = p.confidence_math || p.edge || "";
          const pillarItems = rawMath.split(",").map(s => s.trim()).filter(Boolean);
          const pillarChipsHtml = pillarItems.length > 0
            ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">
                <span class="pill pill-blue" style="font-size:0.74rem;">Base Score: 50%</span>
                ${pillarItems.map(item => {
                  const isNeg = item.includes("(-");
                  const isPos = item.includes("(+");
                  const cls = isNeg ? "pill-red" : (isPos ? "pill-green" : "pill-blue");
                  return `<span class="pill ${cls}" style="font-size:0.74rem;">${esc(item)}</span>`;
                }).join("")}
               </div>`
            : `<div class="bet-meta">Standard quantitative model evaluation.</div>`;

          const gameLineHtml = p.game_line
            ? p.game_line.split("|").map(seg => `<div style="margin-bottom:3px;">• ${esc(seg.trim())}</div>`).join("")
            : `<div style="margin-bottom:3px;">• ${esc(p.title)}</div>`;

          const drawerHtml = isPass ? "" : `
            <div id="curated-detail-${idx}" style="display:${openDrawers.has(idx) ? "block" : "none"};margin-top:12px;padding-top:12px;border-top:1px solid var(--card-border);font-size:0.83rem;">
              <div style="font-weight:700;color:#e2e8f0;margin-bottom:5px;">🏟️ Game &amp; Line Info</div>
              <div style="color:var(--muted);line-height:1.45;margin-bottom:10px;">
                ${gameLineHtml}
              </div>
              <div style="font-weight:700;color:#e2e8f0;margin-bottom:4px;">🧮 How Confidence (${cp !== null && cp !== undefined ? cp + "%" : "--"}) Was Chosen</div>
              ${pillarChipsHtml}
            </div>
          `;

          let actionHtml = "";
          if (!isPass) {
            if (existingPend) {
              const recalcNote = (p.recalculated_stake && Math.abs(p.recalculated_stake - existingPend.stake) >= 0.01)
                ? `<span class="pill pill-blue">${esc(p.wager_pct || "")}% of ${fmtMoney(data.bankroll)} BR = ${fmtMoney(p.recalculated_stake)}</span>`
                : "";
              actionHtml = `
                <div class="bet-actions" onclick="event.stopPropagation()">
                  <span class="pill pill-green">✓ Already Pending (${esc(existingPend.bet_id)} • ${fmtMoney(existingPend.stake)})</span>
                  ${recalcNote}
                  <button class="btn-mini" onclick="event.stopPropagation(); if(confirm('You already have ${esc(existingPend.bet_id)} pending on this team. Log another bet anyway?')) openLogModal(window.__curatedPlays[${idx}])">+ Log Again</button>
                </div>`;
            } else if (existingSettled) {
              const resU = String(existingSettled.result || "").toUpperCase();
              const resCls = resU === "WON" ? "pill-green" : (resU === "PUSH" ? "pill-amber" : "pill-red");
              const plSign = Number(existingSettled.net_pl || 0) >= 0 ? "+" : "";
              actionHtml = `
                <div class="bet-actions" onclick="event.stopPropagation()">
                  <span class="pill ${resCls}">🏁 Settled: ${esc(resU)} (${esc(existingSettled.bet_id)} • ${plSign}${fmtMoney(existingSettled.net_pl)})</span>
                </div>`;
            } else {
              let adjBadge = "";
              if (p.stake_adjusted && p.original_stake) {
                adjBadge = `<span class="pill pill-amber">🔄 Wager Adjusted for ${fmtMoney(data.bankroll)} BR: ${fmtMoney(p.stake)} (${esc(p.wager_pct || "")}% BR • was ${fmtMoney(p.original_stake)})</span>`;
              } else if (p.stake > 0 && p.wager_pct) {
                adjBadge = `<span class="pill pill-blue">💰 Wager: ${fmtMoney(p.stake)} (${esc(p.wager_pct)}% of ${fmtMoney(data.bankroll)} BR)</span>`;
              }
              actionHtml = `
                <div class="bet-actions" onclick="event.stopPropagation()">
                  <button class="btn-green" onclick="event.stopPropagation(); openLogModal(window.__curatedPlays[${idx}])">✓ I Placed This (${fmtMoney(p.stake)})</button>
                  ${adjBadge}
                </div>`;
            }
          }
          const clickAttr = isPass ? "" : ` style="cursor:pointer;transition:background 0.15s;" onclick="toggleCuratedDetail(${idx})"`;
          return `
          <div class="bet-item"${clickAttr}>
            <div class="bet-top">
              <div class="bet-title">${idx + 1}. ${esc(p.title)}</div>
              <span class="pill ${pillCls}">${esc(pillTxt)}</span>
            </div>
            ${meterHtml}
            ${drawerHtml}
            ${actionHtml}
          </div>
        `;
        }).join("");
      } else {
        cList.innerHTML = `<div class="empty-msg">${esc(data.curated_raw || "No curated plays found yet.").replace(/\n/g, "<br>")}</div>`;
      }

      // 2. Active Pending Tickets (with live in-game ESPN score badges + Edit/Cashout)
      const pList = document.getElementById("pending-list");
      const pend = data.pending_tickets || [];
      window.__pendingTickets = pend;
      document.getElementById("pending-count").textContent = `${pend.length} Active (${fmtMoney(data.pending_total)})`;
      if (pend.length > 0) {
        pList.innerHTML = pend.map((t, idx) => {
          const clvBadge = (t.clv !== null && t.clv !== undefined)
            ? ` <span class="pill ${t.clv >= 0 ? "pill-green" : "pill-red"}">CLV ${t.clv >= 0 ? "+" : ""}${t.clv}%</span>`
            : "";
          return `
          <div class="bet-item">
            <div class="bet-top">
              <div class="bet-title">${esc(t.bet_id)} • ${esc(t.selections)}</div>
              <div><span class="pill pill-amber">${esc(t.odds)} | ${fmtMoney(t.stake)} &rarr; ${fmtMoney(t.payout)}</span>${clvBadge}</div>
            </div>
            <div class="bet-meta" style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
              <span>${esc(t.sport)} &bull; ${esc(t.bet_type)} &bull; Placed ${esc(t.date)}</span>
              <span id="live-score-${idx}" style="font-weight:700;color:#60a5fa;"></span>
            </div>
            <div class="bet-actions">
              <button class="btn-mini" onclick="openEditModal(window.__pendingTickets[${idx}])">✏️ Edit / Cash Out</button>
            </div>
          </div>
        `;
        }).join("");
        fetchLiveScoresForPending(pend);
      } else {
        pList.innerHTML = `<div class="empty-msg">No active pending bets right now.</div>`;
      }

      // 6. Store latest graded stats for Overall / This Week / Yesterday / Today modal
      window.__gradedStats = {
        all: {
          date: "All Days",
          wins: ov.wins,
          losses: ov.losses,
          pushes: ov.pushes || 0,
          win_pct: ov.win_pct,
          net_pl: ov.net_pl,
          roi: ov.roi,
          total_count: totalSettledCount,
          tickets: allSettled,
        },
        week: ws,
        yesterday: ys,
        today: ts,
      };
      if (document.getElementById("graded-modal").classList.contains("open") && window.__activeGradedTab) {
        renderGradedModal(window.__activeGradedTab);
      }

      // 7. Equity Curve Chart
      const ctx = document.getElementById("equityChart").getContext("2d");
      const labels = (data.equity_curve || []).map(p => p.label);
      const values = (data.equity_curve || []).map(p => p.bankroll);
      if (chartInstance) chartInstance.destroy();
      chartInstance = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [{
            label: "Bankroll ($)",
            data: values,
            borderColor: "#10b981",
            backgroundColor: "rgba(16, 185, 129, 0.14)",
            borderWidth: 2.5,
            pointRadius: 2,
            fill: true,
            tension: 0.25
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#94a3b8", maxTicksLimit: 10 }, grid: { color: "rgba(148,163,184,0.08)" } },
            y: { ticks: { color: "#94a3b8", callback: v => "$" + v }, grid: { color: "rgba(148,163,184,0.08)" } }
          }
        }
      });
    }

    async function fetchLiveScoresForPending(pendingTickets) {
      const sportMap = {
        "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
        "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80",
        "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80",
        "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50",
        "CBB": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50",
        "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "SOCCER": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "PREMIER LEAGUE": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "UCL": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
      };
      const neededSports = [...new Set(pendingTickets.map(t => (t.sport || "").toUpperCase()).filter(s => sportMap[s]))];
      const ymd = ((currentData && currentData.today) || new Date().toISOString().slice(0,10)).replace(/-/g, "");
      const eventsBySport = {};
      await Promise.all(neededSports.map(async sp => {
        try {
          const sep = sportMap[sp].includes("?") ? "&" : "?";
          const r = await fetch(`${sportMap[sp]}${sep}dates=${ymd}`, { cache: "no-store" });
          const j = await r.json();
          eventsBySport[sp] = j.events || [];
        } catch (e) {
          eventsBySport[sp] = [];
        }
      }));

      pendingTickets.forEach((t, idx) => {
        const el = document.getElementById(`live-score-${idx}`);
        if (!el) return;
        const sp = (t.sport || "").toUpperCase();
        const evts = eventsBySport[sp] || [];
        const selLower = (t.selections || "").toLowerCase();
        const match = evts.find(ev => {
          const comp = (ev.competitions || [])[0];
          if (!comp) return false;
          const teams = (comp.competitors || []).map(c => ({
            short: (c.team?.shortDisplayName || "").toLowerCase(),
            name: (c.team?.name || "").toLowerCase(),
            abbr: (c.team?.abbreviation || "").toLowerCase(),
            display: (c.team?.displayName || "").toLowerCase(),
          }));
          return teams.some(tm =>
            (tm.short && tm.short.length > 2 && selLower.includes(tm.short)) ||
            (tm.name && tm.name.length > 2 && selLower.includes(tm.name)) ||
            (tm.display && selLower.includes(tm.display)) ||
            (tm.abbr && tm.abbr.length >= 3 && new RegExp(`\\b${tm.abbr.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`).test(selLower))
          );
        });
        if (!match) return;
        const comp = match.competitions[0];
        const away = comp.competitors.find(c => c.homeAway === "away");
        const home = comp.competitors.find(c => c.homeAway === "home");
        const state = match.status?.type?.state || "";
        const detail = match.status?.type?.shortDetail || "";
        if (!away || !home) return;
        const aAbbr = away.team?.abbreviation || away.team?.shortDisplayName || "";
        const hAbbr = home.team?.abbreviation || home.team?.shortDisplayName || "";
        if (state === "in") {
          el.style.color = "#10b981";
          el.textContent = `🔴 LIVE (${detail}) • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}`;
        } else if (state === "post") {
          el.style.color = "#94a3b8";
          el.textContent = `FINAL • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}`;
        } else {
          el.style.color = "#60a5fa";
          el.textContent = `🕒 ${detail}`;
        }
      });
    }

    async function loadData() {
      const btn = document.getElementById("refresh-btn");
      btn.textContent = "Refreshing...";
      try {
        // 1. Prefer Live Apps Script Web App if configured
        const cfg = getLiveConfig();
        if (cfg.url && cfg.pin) {
          const sep = cfg.url.includes("?") ? "&" : "?";
          const r = await fetch(`${cfg.url}${sep}action=dashboard&pin=${encodeURIComponent(cfg.pin)}&t=${Date.now()}`, {
            redirect: "follow",
            cache: "no-store",
          });
          if (r.ok) {
            const j = await r.json();
            if (j.ok && j.dashboard) {
              const liveDash = j.dashboard;
              if ((!liveDash.curated_plays || liveDash.curated_plays.length === 0)) {
                try {
                  const sr = await fetch(`./data.json?t=${Date.now()}`, { cache: "no-store" });
                  if (sr.ok) {
                    const sd = await sr.json();
                    if (sd && sd.curated_plays && sd.curated_plays.length > 0 && sd.curated_date === liveDash.today) {
                      liveDash.curated_plays = sd.curated_plays;
                      liveDash.curated_date = sd.curated_date;
                    }
                  }
                } catch (_) {}
              }
              render(liveDash);
              btn.textContent = "↻ Refresh Live";
              return;
            }
          }
        }

        // 2. Local Python server mode
        if ((window.location.protocol.startsWith("http") && !STATIC_DATA) || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
          const r = await fetch("/api/dashboard");
          const d = await r.json();
          render(d);
          btn.textContent = "↻ Refresh Live";
          return;
        }

        // 3. Fallback: GitHub Pages static data.json
        const r = await fetch(`./data.json?t=${Date.now()}`, { cache: "no-store" });
        if (r.ok) {
          const freshData = await r.json();
          render(freshData);
        } else if (STATIC_DATA) {
          render(STATIC_DATA);
        }
      } catch (e) {
        if (STATIC_DATA && !currentData) render(STATIC_DATA);
        console.error(e);
      }
      const nowStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const upEl = document.getElementById("updated-at");
      if (upEl && !upEl.textContent.includes("checked")) {
        upEl.textContent += ` (checked ${nowStr})`;
      }
      btn.textContent = "↻ Refresh Live";
    }

    if (STATIC_DATA) {
      render(STATIC_DATA);
      loadData();
    } else {
      loadData();
    }
    // Auto-refresh live sheet & scores every 60 seconds
    setInterval(loadData, 60000);
  </script>
</body>
</html>
"""


def build_static_html(out_path="docs/index.html"):
    data = fetch_dashboard_data()
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    html = HTML_TEMPLATE.replace("__INITIAL_DATA_JSON__", json.dumps(data))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    json_path = os.path.join(out_dir, "data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Built static dashboard at {out_path} and {json_path} ({data['updated_at']}).")


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/dashboard":
            try:
                data = fetch_dashboard_data()
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
        elif self.path == "/api/settle":
            self._handle_settle()
        else:
            html = HTML_TEMPLATE.replace("__INITIAL_DATA_JSON__", "null").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

    def do_POST(self):
        if self.path == "/api/settle":
            self._handle_settle()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "Not found"}).encode("utf-8"))

    def _handle_settle(self):
        try:
            import settler

            res = settler.run_settlement()
            res["dashboard"] = fetch_dashboard_data()
            body = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"))

    def log_message(self, format, *args):
        return


def main():
    if "--build-static" in sys.argv:
        build_static_html("docs/index.html")
        return

    port = int(os.environ.get("PORT", "5050"))
    print(f"Starting Betting Curator Web App on http://localhost:{port} ...", flush=True)
    HTTPServer(("0.0.0.0", port), DashboardHandler).serve_forever()


if __name__ == "__main__":
    main()

