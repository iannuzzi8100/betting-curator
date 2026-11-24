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


def _normalize_sport(sport):
    s = str(sport or "").strip()
    u = s.upper()
    if u in ("CBB", "NCAAB", "NCAAM", "MEN'S COLLEGE BASKETBALL", "COLLEGE BASKETBALL"):
        return "NCAAM"
    if u in ("LALIGA", "LA LIGA", "LA_LIGA", "SPANISH LA LIGA", "ESP.1"):
        return "LALIGA"
    if u in ("UCL", "CHAMPIONS LEAGUE", "UEFA CHAMPIONS LEAGUE"):
        return "UCL"
    return s


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

    # Handle streamlined format: "[1:30 PM EST] • Away @ Home (League) — Bet" OR "1:30 PM EST • Away @ Home (League) — Bet"
    bet_segment = rest
    m_stream = re.match(
        r"^(?:(\d{1,2}:\d{2}\s*[AP]M(?:\s*[A-Z]{2,4})?)\s*)?[•·]\s*[^—]+?\(([A-Za-z0-9/ ]{2,10})\)\s*[—–]\s*(.+)$",
        rest,
        flags=re.I,
    )
    if m_stream:
        if m_stream.group(1) and not game_time:
            game_time = m_stream.group(1).strip()
        sport = m_stream.group(2).strip().upper()
        bet_segment = m_stream.group(3).strip()

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
        "sport": _normalize_sport(sport),
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
            "sport": _normalize_sport(r[2]) if len(r) > 2 else "",
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
                # Check if confidence % is already on the trailing badge (e.g. "— [87% 💎]"), never matching "(35% Resulting BR)"
                hm = re.search(r"[—-]\s*\[?(\d{2,3})%\s*([^\]]*)\]?$", clean_title)
                init_cp = None
                if hm and not is_pass_row:
                    trailing_ctx = (hm.group(2) or "").upper()
                    if "BR" not in trailing_ctx and "BANKROLL" not in trailing_ctx and "RESULTING" not in trailing_ctx:
                        init_cp = int(hm.group(1))
                # Strip trailing "— 87% 💎" or "— [87% 💎]" from headline since badge shows it on the right
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
                    if pm and not cur_play.get("is_pass"):
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

        has_generic_window = bool(re.match(r"^\[Window\s*\d+:", cp_item["title"], flags=re.I))
        if (not cp_item["game_line"] or has_generic_window) and intel_quant_lines:
            t_clean = re.sub(r"\[[^\]]*\]", "", cp_item["title"])
            words = [
                w
                for w in re.findall(r"[A-Za-z]{3,}", t_clean)
                if w.upper() not in ("EST", "CFB", "NFL", "NBA", "NHL", "EPL", "UCL", "LALIGA", "CBB", "NCAAM", "NCAAB", "OVER", "UNDER", "RESULTING", "STRAIGHT", "WINDOW")
            ]
            for q_ln in intel_quant_lines:
                if any(w.lower() in q_ln.lower() for w in words[:2]):
                    if has_generic_window:
                        tm_exact = re.search(r"\((\d{1,2}:\d{2}\s*[AP]M\s*EST)\)", q_ln, flags=re.I)
                        if tm_exact:
                            exact_t = tm_exact.group(1).strip()
                            cp_item["title"] = re.sub(
                                r"^\[Window\s*\d+:[^\]]*\]", f"[{exact_t}]", cp_item["title"], flags=re.I
                            )
                            cp_item["game_time"] = exact_t
                            if cp_item.get("selection"):
                                cp_item["selection"] = re.sub(
                                    r"\(Window\s*\d+:[^)]*\)", f"({exact_t})", cp_item["selection"], flags=re.I
                                )
                    if not cp_item["game_line"]:
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
        equity_curve=equity_curve,
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
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", core.lower())).strip()


def _adjust_curated_plays_for_bankroll(
    curated_plays, curated_raw, settled_bankroll, pending_total, pending_tickets, today_tickets, equity_curve=None
):
    """
    Deterministic Wager Engine (Server-Side):
    - 1U = 5% of Total Bankroll, rounded cleanly to nearest $0.50 (min $2.00)
    - Calibrated Fractional Kelly sizing (Confidence 68-99 -> 55.5%-68.5% win prob, odds-adjusted)
    - 35% single-bet cap & 80% concurrent overlap exposure cap across exact start->finish intervals
    - Drawdown Brake (< $60 or > 25% off peak -> tightens to 25% single / 60% concurrent)
    - $1,000 Endgame Goal-Lock Brake (never risk more than needed to cross $1,000)
    """
    if not curated_plays or settled_bankroll <= 0:
        return curated_raw

    avail_cash = round(max(0.0, settled_bankroll - pending_total), 2)
    peak_br = max([100.0, settled_bankroll] + [float(pt.get("bankroll", 0) or 0) for pt in (equity_curve or [])])
    is_dd = (settled_bankroll < 60.0) or (peak_br > 0 and ((peak_br - settled_bankroll) / peak_br) > 0.25)
    max_single_pct = 0.25 if is_dd else 0.35
    max_conc_pct = 0.60 if is_dd else 0.80
    unit_val = max(0.01, settled_bankroll * 0.05)
    max_single_amt = settled_bankroll * max_single_pct
    max_conc_amt = settled_bankroll * max_conc_pct

    def _dur_min(sp, sel_txt=""):
        u = _normalize_sport(sp).upper()
        mins = 165
        if u in ("EPL", "UCL", "LALIGA", "SOCCER", "PREMIER LEAGUE"):
            mins = 120
        elif u == "NCAAM":
            mins = 130
        elif u == "NBA":
            mins = 150
        elif u == "NHL":
            mins = 160
        elif u == "MLB":
            mins = 175
        elif u == "NFL":
            mins = 200
        elif u in ("CFB", "NCAAF"):
            mins = 215
        if re.search(r"\b(?:1h|1st\s*half|first\s*half)\b", str(sel_txt or ""), flags=re.I):
            mins = round(mins * 0.48)
        return mins

    def _time_min(txt):
        m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", str(txt or ""), flags=re.I)
        if not m:
            return 24 * 60
        hr, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ap == "PM" and hr != 12:
            hr += 12
        if ap == "AM" and hr == 12:
            hr = 0
        return hr * 60 + mn

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

    allocated_intervals = []
    for t in (pending_tickets or []):
        st_v = float(t.get("stake") or 0.0)
        if st_v <= 0:
            continue
        ps_m = _time_min(t.get("selections", ""))
        pe_m = ps_m + _dur_min(t.get("sport", ""), t.get("selections", ""))
        allocated_intervals.append({"start": ps_m, "end": pe_m, "stake": st_v})

    remaining_items = []
    for p in curated_plays:
        title = str(p.get("title", ""))
        is_pass = ("⛔" in title) or bool(re.search(r"\bPASS\b", title))
        if is_pass:
            p["status"] = "pass"
            continue

        def _norm_t(txt):
            return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(txt or "").lower())).strip()

        core = _extract_selection_core(p.get("selection") or title)
        match_pend = (
            next((t for t in (pending_tickets or []) if core and len(core) >= 3 and core in _norm_t(t.get("selections", ""))), None)
            if core
            else None
        )
        match_settled = (
            next((t for t in (today_tickets or []) if core and len(core) >= 3 and core in _norm_t(t.get("selections", ""))), None)
            if (core and not match_pend)
            else None
        )

        if match_pend:
            p["status"] = "pending"
            p["pending_bet_id"] = match_pend.get("bet_id")
            p["stake"] = round(float(match_pend.get("stake") or p.get("stake") or 0.0), 2)
            p["units"] = round(p["stake"] / unit_val, 1)
            continue

        if match_settled:
            p["status"] = "settled"
            p["settled_bet_id"] = match_settled.get("bet_id")
            p["settled_result"] = match_settled.get("result")
            p["settled_pl"] = round(float(match_settled.get("net_pl") or 0.0), 2)
            continue

        p["status"] = "remaining"
        odds_i = int(p.get("odds") or -110)
        b_mult = (odds_i / 100.0) if odds_i > 0 else (100.0 / abs(odds_i))
        cp = float(p.get("confidence_pct") if p.get("confidence_pct") is not None else 74)
        p_win = 0.555 + (max(0.0, min(99.0, cp) - 68.0) / 31.0) * 0.13
        kelly_f = max(0.0, (p_win * b_mult - (1.0 - p_win)) / b_mult) if b_mult > 0 else 0.0
        raw_st = min(max_single_amt, settled_bankroll * kelly_f)
        if settled_bankroll < 1000.0 and b_mult > 0:
            raw_st = min(raw_st, (1000.0 - settled_bankroll) / b_mult)
        s_m = _time_min(p.get("game_time") or title)
        e_m = s_m + _dur_min(p.get("sport", ""), p.get("selection") or title)
        remaining_items.append({"p": p, "kelly_f": kelly_f, "raw_st": raw_st, "s": s_m, "e": e_m})

    # Priority-ordered concurrent overlap allocation (highest Kelly edge reserves room first)
    for item in sorted(remaining_items, key=lambda x: (-x["kelly_f"], x["s"])):
        p = item["p"]
        old_stake = round(float(p.get("stake") or 0.0), 2)
        pk = _peak_overlap(allocated_intervals, item["s"], item["e"])
        room = max(0.0, max_conc_amt - pk)
        st = min(item["raw_st"], max_single_amt, room)
        new_stake = round(st * 2.0) / 2.0 if st >= 2.0 else 0.0
        p["stake"] = new_stake
        p["units"] = round(new_stake / unit_val, 1) if new_stake > 0 else 0.0
        p["wager_pct"] = round((new_stake / settled_bankroll) * 100.0, 1) if settled_bankroll > 0 else 0.0
        if old_stake > 0 and abs(new_stake - old_stake) >= 0.01:
            p["original_stake"] = old_stake
            p["stake_adjusted"] = True
        else:
            p["stake_adjusted"] = False
        if new_stake > 0:
            allocated_intervals.append({"start": item["s"], "end": item["e"], "stake": new_stake})
            title = str(p.get("title", ""))
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
      transition: background 0.25s ease, border-color 0.25s ease;
    }
    .bet-item.trend-win {
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.18), rgba(16, 185, 129, 0.06));
      border-color: rgba(16, 185, 129, 0.50);
      box-shadow: inset 3px 0 0 #10b981;
    }
    .bet-item.trend-loss {
      background: linear-gradient(135deg, rgba(239, 68, 68, 0.18), rgba(239, 68, 68, 0.06));
      border-color: rgba(239, 68, 68, 0.50);
      box-shadow: inset 3px 0 0 #ef4444;
    }
    .bet-item.trend-push {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.14), rgba(245, 158, 11, 0.05));
      border-color: rgba(245, 158, 11, 0.42);
      box-shadow: inset 3px 0 0 #f59e0b;
    }
    .bet-item.curated-placed {
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.16), rgba(15, 23, 42, 0.62));
      border-color: rgba(16, 185, 129, 0.45);
      box-shadow: inset 3px 0 0 #10b981;
      opacity: 0.72;
    }
    .bet-item.curated-placed:hover {
      opacity: 0.95;
    }
    .bet-item.curated-settled {
      background: rgba(15, 23, 42, 0.55);
      border-color: rgba(148, 163, 184, 0.28);
      box-shadow: inset 3px 0 0 #64748b;
      opacity: 0.60;
    }
    .bet-item.curated-settled:hover {
      opacity: 0.88;
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
    .line-adjust-row {
      display: grid;
      grid-template-columns: auto 1fr auto auto;
      gap: 6px;
      align-items: center;
    }
    .step-btn {
      background: rgba(59, 130, 246, 0.14);
      border: 1px solid rgba(59, 130, 246, 0.4);
      color: #93c5fd;
      border-radius: 10px;
      padding: 9px 11px;
      font-weight: 800;
      font-size: 0.85rem;
      cursor: pointer;
      white-space: nowrap;
      touch-action: manipulation;
    }
    .step-btn:active { transform: scale(0.96); }
    .step-btn.active {
      background: rgba(16, 185, 129, 0.22);
      border-color: rgba(16, 185, 129, 0.55);
      color: #34d399;
    }
    .curated-meta {
      display: flex;
      align-items: center;
      gap: 7px;
      flex-wrap: wrap;
    }
    .curated-time {
      font-size: 0.78rem;
      font-weight: 700;
      color: #93c5fd;
      letter-spacing: 0.01em;
    }
    .curated-sport {
      display: inline-block;
      background: rgba(148, 163, 184, 0.16);
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 6px;
      padding: 1px 7px;
      font-size: 0.69rem;
      font-weight: 800;
      letter-spacing: 0.05em;
    }
    .curated-matchup {
      font-size: 0.84rem;
      color: var(--muted);
      margin-top: 4px;
      font-weight: 500;
    }
    .curated-bet-row {
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }
    .curated-bet {
      font-size: 1.0rem;
      font-weight: 800;
      color: var(--text);
      letter-spacing: -0.01em;
    }
    .curated-wager {
      font-size: 0.86rem;
      font-weight: 700;
      color: var(--green);
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
      <!-- 1. AVAILABLE CASH & BANKROLL AT THE TOP -->
      <div class="card">
        <div class="card-label">💵 Available Cash &amp; Exposure</div>
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

      <!-- 3. TODAY'S CURATED PICKS (Upcoming Only; Click Title for Started & Full Slate) -->
      <div class="card">
        <div class="section-title" style="cursor:pointer;" onclick="openCuratedArchiveModal('started')" title="Tap to view started &amp; all curated bets">
          <span>🔥 Today's Curated Bets (<span id="curated-date">--</span>)</span>
          <div class="section-right">
            <span class="pill pill-blue" id="curated-count">0 Upcoming</span>
            <span class="card-tap-hint" id="curated-started-hint" style="margin-left:0;"></span>
          </div>
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
              <option value="LALIGA">LALIGA</option>
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
          <input id="f-sel" type="text" placeholder="e.g. Middle Tennessee -6.5 (2:00 PM EST)" oninput="syncLineControlsFromSel()" />
        </div>
        <div class="field">
          <label>Quick Spread / Moneyline</label>
          <div class="line-adjust-row">
            <button type="button" class="step-btn" onclick="stepSelectionLine(-0.5)">−0.5</button>
            <select id="f-line-select" onchange="applyLineFromSelect()"></select>
            <button type="button" class="step-btn" onclick="stepSelectionLine(0.5)">+0.5</button>
            <button type="button" class="step-btn" id="f-ml-btn" onclick="setSelectionLine('ML')">ML</button>
          </div>
        </div>
        <div class="form-row-2">
          <div class="field">
            <label>American Odds</label>
            <select id="f-odds" onchange="onOddsSelectChange('f')"></select>
            <input id="f-odds-custom" type="text" inputmode="text" placeholder="Custom e.g. +650" style="display:none;margin-top:6px;" oninput="updatePayoutPreview()" />
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
            <select id="e-odds" onchange="onOddsSelectChange('e')"></select>
            <input id="e-odds-custom" type="text" inputmode="text" placeholder="Custom e.g. +650" style="display:none;margin-top:6px;" />
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

  <!-- MODAL 5: STARTED & FULL CURATED BETS PAGE -->
  <div class="modal-backdrop" id="curated-archive-modal" onclick="if(event.target===this) closeModal('curated-archive-modal')">
    <div class="modal" style="max-width:580px;">
      <div class="modal-header">
        <h3 id="curated-archive-title">🔥 Started Curated Bets</h3>
        <button class="close-btn" onclick="closeModal('curated-archive-modal')">&times;</button>
      </div>
      <div class="tabs">
        <button class="tab-btn active" id="curated-tab-started" onclick="openCuratedArchiveModal('started')">🔴 Started (<span id="curated-tab-started-cnt">0</span>)</button>
        <button class="tab-btn" id="curated-tab-all" onclick="openCuratedArchiveModal('all')">📋 All Today (<span id="curated-tab-all-cnt">0</span>)</button>
      </div>
      <div class="bet-list" id="curated-archive-list"></div>
      <div class="modal-footer">
        <button class="btn-secondary" onclick="closeModal('curated-archive-modal')">Close</button>
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

    function toggleCuratedDetail(domId, drawerKey) {
      const el = document.getElementById(`curated-detail-${domId}`);
      const caret = document.getElementById(`curated-caret-${domId}`);
      const key = drawerKey || String(domId);
      if (!el) return;
      if (el.style.display === "none") {
        el.style.display = "block";
        openDrawers.add(key);
        if (caret) caret.textContent = "▴ Hide details";
      } else {
        el.style.display = "none";
        openDrawers.delete(key);
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

        // 1. Live Google Apps Script settlement + instant client-side ESPN fallback + wager recalculation
        if (cfg.url && cfg.pin) {
          const prevBr = Number((currentData && currentData.bankroll) || 0);
          let res = { ok: true, settled_count: 0, prev_bankroll: prevBr, new_bankroll: prevBr, adjusted_plays: [], dashboard: currentData };
          try {
            const apiRes = await postLiveApi({ action: "settle", stage: "settler" });
            if (apiRes) res = apiRes;
          } catch (apiErr) {
            console.warn("Apps Script settle call error, trying client-side ESPN fallback:", apiErr);
          }

          let latestDash = res.dashboard || currentData;
          let totalSettled = Number(res.settled_count || 0);
          const clientSettledNotes = [];

          // Client-side ESPN final verification fallback (uses &limit=500 + deterministic grading so final games settle even if Apps Script hasn't been redeployed)
          if (latestDash && Array.isArray(latestDash.pending_tickets) && latestDash.pending_tickets.length > 0) {
            const clientRes = await settleFinalPendingFromBrowser(latestDash);
            if (clientRes.settledCount > 0) {
              totalSettled += clientRes.settledCount;
              clientRes.notes.forEach(n => clientSettledNotes.push(n));
              if (clientRes.dashboard) latestDash = clientRes.dashboard;
            }
          }

          if (latestDash) render(latestDash);
          const startBr = Number(res.prev_bankroll != null ? res.prev_bankroll : prevBr);
          const endBr = Number((latestDash && latestDash.bankroll != null) ? latestDash.bankroll : (res.new_bankroll || startBr));

          // If user also saved a GitHub PAT in browser and Apps Script didn't trigger GitHub, dispatch GitHub workflow too
          let ghDispatched = Boolean(res.github_triggered);
          if (!ghDispatched && cfg.ghPat) {
            try {
              await dispatchGithubSettleWorkflow(cfg.ghPat);
              ghDispatched = true;
            } catch (_) {}
          }

          const adjList = (res.adjusted_plays || []).filter(a => a && a.new_stake > 0);
          if (totalSettled > 0) {
            const detailTxt = clientSettledNotes.length > 0 ? ` (${clientSettledNotes.join(", ")})` : "";
            const adjSummary = adjList.length > 0
              ? ` • Adjusted ${adjList.length} remaining bet${adjList.length === 1 ? "" : "s"} (${adjList.map(a => fmtMoney(a.new_stake)).join(", ")})`
              : "";
            showToast(`✅ Settled ${totalSettled} bet${totalSettled === 1 ? "" : "s"}${detailTxt}! BR: ${fmtMoney(startBr)} → ${fmtMoney(endBr)}${adjSummary}`, 6000);
          } else if (adjList.length > 0 && adjList.some(a => a.changed)) {
            showToast(`🔄 Recalculated remaining wagers for ${fmtMoney(endBr)} BR (${adjList.map(a => fmtMoney(a.new_stake)).join(", ")})`, 4800);
          } else {
            showToast(`🏁 Checked ESPN scores (0 newly final) • Remaining wagers synced to ${fmtMoney(endBr)} BR${ghDispatched ? " • GitHub workflow triggered" : ""}`, 4200);
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

    function buildStandardOddsList() {
      const vals = [];
      for (let v = -500; v <= -310; v += 10) vals.push(v);
      for (let v = -300; v <= -205; v += 5) vals.push(v);
      for (let v = -200; v <= -101; v += 1) vals.push(v);
      for (let v = 100; v <= 200; v += 1) vals.push(v);
      for (let v = 205; v <= 300; v += 5) vals.push(v);
      for (let v = 310; v <= 500; v += 10) vals.push(v);
      return vals;
    }

    function populateOddsSelect(prefix) {
      const sel = document.getElementById(`${prefix}-odds`);
      if (!sel || sel.options.length > 0) return;
      const vals = buildStandardOddsList();
      sel.innerHTML = vals.map(v => {
        const lbl = v > 0 ? `+${v}` : String(v);
        return `<option value="${lbl}">${lbl}</option>`;
      }).join("") + `<option value="custom">Custom...</option>`;
      sel.value = "-110";
    }

    function populateOddsDropdowns() {
      populateOddsSelect("f");
      populateOddsSelect("e");
    }

    function setOddsValue(prefix, rawOdds) {
      populateOddsSelect(prefix);
      const sel = document.getElementById(`${prefix}-odds`);
      const customEl = document.getElementById(`${prefix}-odds-custom`);
      if (!sel) return;
      const num = parseInt(String(rawOdds == null ? "" : rawOdds).replace(/[^0-9+-]/g, ""), 10);
      if (isNaN(num) || num === 0) {
        sel.value = "-110";
        if (customEl) { customEl.style.display = "none"; customEl.value = ""; }
        return;
      }
      const norm = num > 0 ? `+${num}` : String(num);
      let opt = [...sel.options].find(o => o.value === norm);
      if (!opt) {
        // Insert exact odds value in sorted numeric order before Custom...
        const customOpt = [...sel.options].find(o => o.value === "custom");
        opt = document.createElement("option");
        opt.value = norm;
        opt.textContent = norm;
        const insertBefore = [...sel.options].find(o => o.value !== "custom" && parseInt(o.value, 10) > num) || customOpt;
        sel.insertBefore(opt, insertBefore || null);
      }
      sel.value = norm;
      if (customEl) { customEl.style.display = "none"; customEl.value = ""; }
    }

    function getOddsValue(prefix) {
      const sel = document.getElementById(`${prefix}-odds`);
      const customEl = document.getElementById(`${prefix}-odds-custom`);
      if (!sel) return "-110";
      if (sel.value === "custom") {
        return customEl ? customEl.value.trim() : "";
      }
      return sel.value.trim();
    }

    function onOddsSelectChange(prefix) {
      const sel = document.getElementById(`${prefix}-odds`);
      const customEl = document.getElementById(`${prefix}-odds-custom`);
      if (customEl) {
        const isCustom = sel && sel.value === "custom";
        customEl.style.display = isCustom ? "block" : "none";
        if (isCustom) customEl.focus();
      }
      if (prefix === "f") updatePayoutPreview();
    }

    function formatHalfNum(n) {
      const rounded = Math.round(n * 2) / 2;
      return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
    }

    function formatSpreadToken(n) {
      const rounded = Math.round(n * 2) / 2;
      if (rounded === 0) return "PK";
      const s = Number.isInteger(rounded) ? String(Math.abs(rounded)) : Math.abs(rounded).toFixed(1);
      return (rounded > 0 ? "+" : "-") + s;
    }

    function populateLineDropdown() {
      const sel = document.getElementById("f-line-select");
      if (!sel || sel.options.length > 0) return;
      const opts = [
        `<option value="">— Line —</option>`,
        `<option value="ML">ML (Moneyline)</option>`,
        `<option value="PK">PK (Pick 'em)</option>`,
      ];
      for (let v = -20.5; v <= -0.5; v += 0.5) {
        const tok = formatSpreadToken(v);
        opts.push(`<option value="${tok}">${tok}</option>`);
      }
      for (let v = 0.5; v <= 20.5; v += 0.5) {
        const tok = formatSpreadToken(v);
        opts.push(`<option value="${tok}">${tok}</option>`);
      }
      sel.innerHTML = opts.join("");
    }

    function parseSelectionParts(rawSel) {
      const s = String(rawSel || "").trim();
      let suffix = "";
      let core = s;
      // Preserve trailing parenthetical such as "(Window 2: 10:00 AM)" or "(7:00 PM EST)"
      const parenMatch = core.match(/\s*(\([^)]*\))\s*$/);
      if (parenMatch) {
        suffix = " " + parenMatch[1];
        core = core.slice(0, parenMatch.index).trim();
      }
      // Match Over/Under total at end of core
      const ouMatch = core.match(/\s+(Over|Under|O|U)\s*(\d+(?:\.\d+)?)\s*$/i);
      if (ouMatch) {
        const dir = /^u/i.test(ouMatch[1]) ? "Under" : "Over";
        const val = parseFloat(ouMatch[2]);
        return {
          base: core.slice(0, ouMatch.index).trim(),
          kind: "total",
          dir,
          num: val,
          token: `${dir} ${formatHalfNum(val)}`,
          suffix,
        };
      }
      // Match Moneyline / TO WIN at end of core
      const mlMatch = core.match(/\s+(ML|Moneyline|TO\s+WIN)\s*$/i);
      if (mlMatch) {
        return {
          base: core.slice(0, mlMatch.index).trim(),
          kind: "ml",
          num: null,
          token: "ML",
          suffix,
        };
      }
      // Match PK / Pick'em at end of core
      const pkMatch = core.match(/\s+(PK|Pick(?:'em)?)\s*$/i);
      if (pkMatch) {
        return {
          base: core.slice(0, pkMatch.index).trim(),
          kind: "spread",
          num: 0,
          token: "PK",
          suffix,
        };
      }
      // Match signed spread at end of core (e.g. -5.5, +3, -6.5)
      const spMatch = core.match(/\s+([+-]\d+(?:\.\d+)?)\s*$/);
      if (spMatch) {
        const val = parseFloat(spMatch[1]);
        return {
          base: core.slice(0, spMatch.index).trim(),
          kind: "spread",
          num: val,
          token: formatSpreadToken(val),
          suffix,
        };
      }
      return { base: core, kind: "none", num: null, token: "", suffix };
    }

    function syncLineControlsFromSel() {
      populateLineDropdown();
      const selInput = document.getElementById("f-sel");
      const lineSelect = document.getElementById("f-line-select");
      const mlBtn = document.getElementById("f-ml-btn");
      if (!selInput || !lineSelect) return;
      const parts = parseSelectionParts(selInput.value);
      if (mlBtn) mlBtn.classList.toggle("active", parts.kind === "ml");
      if (!parts.token) {
        lineSelect.value = "";
        return;
      }
      let opt = [...lineSelect.options].find(o => o.value === parts.token);
      if (!opt) {
        opt = document.createElement("option");
        opt.value = parts.token;
        opt.textContent = parts.token;
        lineSelect.insertBefore(opt, lineSelect.options[1] || null);
      }
      lineSelect.value = parts.token;
    }

    function setSelectionLine(newToken) {
      const selInput = document.getElementById("f-sel");
      if (!selInput) return;
      const parts = parseSelectionParts(selInput.value);
      const base = parts.base;
      if (!base) {
        selInput.value = newToken;
      } else if (!newToken) {
        selInput.value = `${base}${parts.suffix}`.trim();
      } else {
        selInput.value = `${base} ${newToken}${parts.suffix}`.trim();
      }
      syncLineControlsFromSel();
    }

    function applyLineFromSelect() {
      const lineSelect = document.getElementById("f-line-select");
      if (!lineSelect || !lineSelect.value) return;
      setSelectionLine(lineSelect.value);
    }

    function stepSelectionLine(delta) {
      const selInput = document.getElementById("f-sel");
      if (!selInput) return;
      const parts = parseSelectionParts(selInput.value);
      if (parts.kind === "total" && parts.num != null) {
        const nextVal = Math.max(0.5, Math.round((parts.num + delta) * 2) / 2);
        setSelectionLine(`${parts.dir} ${formatHalfNum(nextVal)}`);
        return;
      }
      let cur = 0;
      if (parts.kind === "spread" && parts.num != null) {
        cur = parts.num;
      }
      const nextSpread = Math.round((cur + delta) * 2) / 2;
      setSelectionLine(formatSpreadToken(nextSpread));
    }

    function updatePayoutPreview() {
      const odds = parseInt(String(getOddsValue("f") || "").replace(/\+/g, ""), 10);
      const stake = parseFloat(document.getElementById("f-stake").value || "0");
      let payout = 0;
      if (!isNaN(odds) && odds !== 0 && !isNaN(stake) && stake > 0) {
        payout = odds > 0 ? stake * (1 + odds / 100) : stake * (1 + 100 / Math.abs(odds));
      }
      document.getElementById("f-payout-preview").textContent = fmtMoney(payout);
    }

    function normalizeSport(sport) {
      const s = String(sport || "").trim();
      const u = s.toUpperCase();
      if (u === "CBB" || u === "NCAAB" || u === "NCAAM" || u === "MEN'S COLLEGE BASKETBALL" || u === "COLLEGE BASKETBALL") {
        return "NCAAM";
      }
      if (u === "LALIGA" || u === "LA LIGA" || u === "LA_LIGA" || u === "SPANISH LA LIGA" || u === "ESP.1") {
        return "LALIGA";
      }
      if (u === "UCL" || u === "CHAMPIONS LEAGUE" || u === "UEFA CHAMPIONS LEAGUE") {
        return "UCL";
      }
      return s;
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
      const smStream = rest.match(/^(?:(\d{1,2}:\d{2}\s*[AP]M(?:\s*[A-Z]{2,4})?)\s*)?[•·]\s*[^—]+?\(([A-Za-z0-9/ ]{2,10})\)\s*[—–]\s*(.+)$/i);
      if (smStream) {
        if (smStream[1] && !gameTime) gameTime = smStream[1].trim();
        sport = smStream[2].trim().toUpperCase();
        betSeg = smStream[3].trim();
      }
      const om = betSeg.match(/\(([+-]\d{3,4})\)/) || betSeg.match(/\s([+-]\d{3,4})(?:\s|$|—|–)/);
      const sm = betSeg.match(/\$(\d+(?:\.\d{1,2})?)/) || rest.match(/\$(\d+(?:\.\d{1,2})?)/);
      let pick = om ? betSeg.slice(0, om.index).trim() : betSeg.split("—")[0].trim();
      if (gameTime && pick && !pick.includes(gameTime)) pick += ` (${gameTime})`;
      return {
        sport: normalizeSport(sport),
        selection: pick,
        odds: om ? parseInt(om[1], 10) : -110,
        stake: sm ? parseFloat(sm[1]) : 0,
      };
    }

    function normalizeSelForMatch(text) {
      return String(text || "")
        .replace(/[øØ]/g, "o")
        .replace(/[æÆ]/g, "ae")
        .replace(/ß/g, "ss")
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function extractSelectionCore(text) {
      let s = String(text || "").trim();
      s = s.replace(/^\d+\.\s*/, "").replace(/\*/g, "").trim();
      s = s.replace(/^\[[^\]]+\]\s*/, "").trim();
      const smStream = s.match(/^(?:(?:\d{1,2}:\d{2}\s*[AP]M(?:\s*[A-Z]{2,4})?)\s*)?[•·]\s*[^—]+?\([A-Za-z0-9/ ]{2,10}\)\s*[—–]\s*(.+)$/i);
      if (smStream) s = smStream[1].trim();
      const core = s.split(/(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)/i)[0];
      return normalizeSelForMatch(core);
    }

    function findMatchingTicketForPlay(ticketList, play) {
      if (!Array.isArray(ticketList) || ticketList.length === 0 || !play) return null;
      const fb = parseTitleFallback(play.title);
      const core = extractSelectionCore(play.selection || fb.selection || play.title || "");
      if (!core || core.length < 3) return null;
      return ticketList.find(t => {
        const normSel = normalizeSelForMatch(t && t.selections);
        if (!normSel) return false;
        if (normSel.includes(core)) return true;
        const tCore = extractSelectionCore(t && t.selections);
        return Boolean(tCore && tCore.length >= 3 && tCore === core);
      }) || null;
    }

    function peakOverlapRiskMs(intervals, s, e) {
      const pts = [s];
      for (const iv of intervals) {
        if (iv.start < e && iv.end > s && iv.start >= s && iv.start < e) {
          pts.push(iv.start);
        }
      }
      let best = 0;
      for (const t of pts) {
        let sum = 0;
        for (const iv of intervals) {
          if (iv.start <= t && t < iv.end) sum += iv.stake;
        }
        if (sum > best) best = sum;
      }
      return best;
    }

    function computeSmartCuratedWagers(curatedPlays, pendingTickets, data) {
      if (!Array.isArray(curatedPlays) || curatedPlays.length === 0) return;
      const settledBr = Number((data && data.bankroll) || 0);
      if (settledBr <= 0) return;

      const pendList = pendingTickets || (data && data.pending_tickets) || [];
      const todayList = (data && data.today_stats && data.today_stats.tickets) || [];
      const pendingTot = pendList.reduce((acc, t) => acc + Number(t.stake || 0), 0);
      const availCash = Math.max(0, Math.round((settledBr - pendingTot) * 100) / 100);
      const eqPts = (data && data.equity_curve) || [];
      const peakBr = Math.max(100, settledBr, ...eqPts.map(pt => Number(pt.bankroll || 0)));
      const isDrawdown = (settledBr < 60) || (peakBr > 0 && ((peakBr - settledBr) / peakBr) > 0.25);
      const maxSinglePct = isDrawdown ? 0.25 : 0.35;
      const maxConcPct = isDrawdown ? 0.60 : 0.80;
      const baseUnit = Math.max(0.01, settledBr * 0.05);
      const dateStr = (data && (data.curated_date || data.today)) || "";

      // Build active pending intervals & anticipated blowout win pool (used for suggested wagers ONLY — never alters displayed bankroll)
      const baseIntervals = [];
      const anticipatedWins = [];
      pendList.forEach(t => {
        const st = Number(t.stake || 0);
        if (st <= 0) return;
        const durMs = getSportDurationMinutes(t.sport, t.selections) * 60000;
        const pEnd = t.__estFinishMs || estimateTicketFinishMs(t);
        const pStart = t.__startMs || (pEnd - durMs);
        const isFinal = t.__espnState === "post";
        if (!isFinal) {
          baseIntervals.push({ start: pStart, end: pEnd, stake: st, label: t.bet_id || "Pending" });
        }
        if (t.__anticipatedWin) {
          const payout = Number(t.payout || 0);
          const profit = Math.max(0, payout - st);
          anticipatedWins.push({
            bet_id: t.bet_id,
            endMs: isFinal ? 0 : pEnd,
            payout: payout,
            profit: profit,
            reason: t.__anticipatedReason || "Lock",
          });
        }
      });

      const remaining = [];
      curatedPlays.forEach(p => {
        const title = String(p.title || "");
        const isPass = Boolean(p.is_pass) || title.includes("⛔") || /\bPASS\b/.test(title) || /NO GOOD PLAY/i.test(title);
        if (isPass) {
          p.status = "pass";
          return;
        }
        const fb = parseTitleFallback(title);
        const matchPend = findMatchingTicketForPlay(pendList, p);
        const matchSettled = !matchPend ? findMatchingTicketForPlay(todayList, p) : null;

        if (matchPend) {
          p.status = "pending";
          p.pending_bet_id = matchPend.bet_id;
          p.stake = Number(matchPend.stake || p.stake || 0);
          p.units = Math.round((p.stake / baseUnit) * 10) / 10;
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
        if (isCuratedPlayStarted(p, dateStr)) return;

        const oddsVal = Number(p.odds != null ? p.odds : (fb.odds != null ? fb.odds : -110));
        const b = oddsVal > 0 ? oddsVal / 100 : 100 / Math.max(1, Math.abs(oddsVal));
        const cp = Number(p.confidence_pct != null ? p.confidence_pct : 74);
        const pWin = 0.555 + (Math.max(0, Math.min(99, cp) - 68) / 31) * 0.13;
        const kellyF = b > 0 ? Math.max(0, (pWin * b - (1 - pWin)) / b) : 0;
        const sMs = estimateCuratedStartMs(p, dateStr);
        const eMs = sMs + getSportDurationMinutes(p.sport || fb.sport, p.selection || title) * 60000;

        // Anticipate locked-in blowout payouts ONLY for games finishing before this game's tipoff (+15m buffer)
        const relAnt = anticipatedWins.filter(aw => aw.endMs <= sMs + 15 * 60000);
        const antProfit = relAnt.reduce((acc, a) => acc + a.profit, 0);
        const antPayout = relAnt.reduce((acc, a) => acc + a.payout, 0);
        const effBr = settledBr + antProfit;
        const effMaxSingle = effBr * maxSinglePct;
        const effMaxConc = effBr * maxConcPct;

        let rawTarget = Math.min(effMaxSingle, effBr * kellyF);
        let goalBrake = false;
        if (effBr < 1000 && b > 0) {
          const toGoal = (1000 - effBr) / b;
          if (rawTarget > toGoal) {
            rawTarget = toGoal;
            goalBrake = true;
          }
        }

        remaining.push({
          p, title, fb, oddsVal, b, cp, pWin, kellyF, sMs, eMs,
          effBr, effMaxSingle, effMaxConc, rawTarget, goalBrake,
          antIds: relAnt.map(a => a.bet_id),
          antProfit: Math.round(antProfit * 100) / 100,
          antPayout: Math.round(antPayout * 100) / 100,
        });
      });

      // Priority-ordered allocation: highest Kelly edge reserves concurrent room first (Lookahead Cash Reservation)
      const allocated = baseIntervals.slice();
      const sortedPriority = remaining.slice().sort((a, b) => (b.kellyF - a.kellyF) || (a.sMs - b.sMs));
      for (const it of sortedPriority) {
        const pk = peakOverlapRiskMs(allocated, it.sMs, it.eMs);
        const room = Math.max(0, it.effMaxConc - pk);
        const rawAllowed = Math.min(it.rawTarget, it.effMaxSingle, room);
        const finalStake = rawAllowed >= 2.0 ? Math.round(rawAllowed * 2) / 2 : 0;
        it.finalStake = finalStake;
        if (finalStake > 0) {
          allocated.push({ start: it.sMs, end: it.eMs, stake: finalStake, label: it.p.__key || "play" });
        }
      }

      // Post-pass to compute exact final peak concurrent exposure & breakdown for every card
      for (const it of remaining) {
        const p = it.p;
        const finalStake = it.finalStake || 0;
        const peakAfter = peakOverlapRiskMs(allocated, it.sMs, it.eMs);
        const overlapCount = allocated.filter(iv => iv.start < it.eMs && iv.end > it.sMs && iv.label !== (p.__key || "play")).length;
        const units = finalStake > 0 ? Math.round((finalStake / baseUnit) * 10) / 10 : 0;
        const expCash = availCash + it.antPayout;
        let cashNowNote = "";
        if (finalStake > availCash + 0.01) {
          if (it.antPayout > 0 && finalStake <= expCash + 0.01) {
            cashNowNote = `Ready after ${it.antIds.join(", ")} settles (${fmtMoney(availCash)} free now)`;
          } else {
            cashNowNote = `${fmtMoney(availCash)} cash free now (rest frees as earlier games settle)`;
          }
        }
        p.stake = finalStake;
        p.units = units;
        p.wager_pct = Math.round((finalStake / it.effBr) * 1000) / 10;
        p.stake_adjusted = false;
        p.__wagerBreakdown = {
          unitSize: Math.round(baseUnit * 100) / 100,
          units,
          effBr: Math.round(it.effBr * 100) / 100,
          probPct: Math.round(it.pWin * 1000) / 10,
          kellyPct: Math.round(it.kellyF * 1000) / 10,
          oddsVal: it.oddsVal,
          peakConcPct: Math.round((peakAfter / it.effBr) * 1000) / 10,
          maxConcPct: Math.round(maxConcPct * 100),
          maxSinglePct: Math.round(maxSinglePct * 100),
          overlapCount,
          isDrawdown,
          goalBrake: it.goalBrake,
          antIds: it.antIds,
          antProfit: it.antProfit,
          cashNowNote,
          cappedByOverlap: finalStake === 0 && it.rawTarget >= 2.0,
        };
      }
    }

    function adjustCuratedPlaysClientSide(data) {
      if (!data || !Array.isArray(data.curated_plays)) return;
      computeSmartCuratedWagers(data.curated_plays, data.pending_tickets, data);
    }

    function openLogModal(prefillPlay) {
      if (!isLiveConfigured()) {
        openSettingsModal();
        showToast("Set up Live Sync once to log bets directly in 1 tap.");
        return;
      }
      switchLogTab("form");
      populateOddsDropdowns();
      populateLineDropdown();
      if (prefillPlay) {
        const fb = parseTitleFallback(prefillPlay.title);
        const rawSport = (prefillPlay.sport && prefillPlay.sport !== "CFB") ? prefillPlay.sport : (fb.sport || prefillPlay.sport);
        const sport = normalizeSport(rawSport);
        const rawSel = prefillPlay.selection || "";
        const sel = (rawSel && !/^[•·].*[—–]/.test(rawSel)) ? rawSel : (fb.selection || rawSel);
        const odds = prefillPlay.odds != null ? prefillPlay.odds : fb.odds;
        const stake = (prefillPlay.stake && prefillPlay.stake > 0) ? prefillPlay.stake : (fb.stake || (currentData && currentData.available_cash ? Math.min(currentData.available_cash, 10) : 5));
        document.getElementById("log-modal-title").textContent = "⚡ Confirm Curated Bet";
        const spEl = document.getElementById("f-sport");
        if ([...spEl.options].some(o => o.value === sport)) spEl.value = sport;
        document.getElementById("f-type").value = "Straight";
        document.getElementById("f-sel").value = sel;
        setOddsValue("f", odds);
        document.getElementById("f-stake").value = Number(stake).toFixed(2);
      } else {
        document.getElementById("log-modal-title").textContent = "➕ Log Placed Bet";
        document.getElementById("f-sel").value = "";
        setOddsValue("f", "-110");
        document.getElementById("f-stake").value = "";
      }
      syncLineControlsFromSel();
      updatePayoutPreview();
      openModal("log-modal");
    }

    async function submitLogBet() {
      const btn = document.getElementById("f-submit-btn");
      const sport = normalizeSport(document.getElementById("f-sport").value);
      const bet_type = document.getElementById("f-type").value;
      const selections = document.getElementById("f-sel").value.trim();
      const odds = getOddsValue("f");
      const stake = parseFloat(document.getElementById("f-stake").value);
      if (!selections || !stake || stake <= 0) {
        showToast("Enter a selection and valid stake.");
        return;
      }
      if (!odds || isNaN(parseInt(odds.replace(/\+/g, ""), 10))) {
        showToast("Select or enter valid American odds (e.g. -110).");
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
      document.getElementById("e-sport").value = normalizeSport(ticket.sport || "");
      document.getElementById("e-result").value = "Pending";
      document.getElementById("e-sel").value = ticket.selections || "";
      setOddsValue("e", ticket.odds || "-110");
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
          sport: normalizeSport(document.getElementById("e-sport").value),
          result: document.getElementById("e-result").value,
          selections: document.getElementById("e-sel").value.trim(),
          odds: getOddsValue("e"),
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
          if (t.sport) metaParts.push(esc(normalizeSport(t.sport)));
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

      // 1. Available Cash & Bankroll (Top — Available Cash primary, Live Bankroll + 1U in sub-row)
      const unitSz = Math.round((Number(data.bankroll || 0) * 0.05) * 100) / 100;
      document.getElementById("bankroll-val").textContent = fmtMoney(data.available_cash);
      document.getElementById("bankroll-sub").innerHTML =
        `<span>Active Pending: <span class="warn">${fmtMoney(data.pending_total)}</span></span>` +
        `<span>Live Bankroll: <span class="pos">${fmtMoney(data.bankroll)}</span></span>` +
        `<span>1U (5%): <span style="color:#93c5fd;">${fmtMoney(unitSz)}</span></span>`;

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

      // 2. Prepare Active Pending Tickets (sorted by earliest finish time + live ESPN clock refinement)
      const pend = (data.pending_tickets || []).map((t, origIdx) => {
        const copy = { ...t, __origIdx: origIdx };
        copy.__finishRank = 2;
        copy.__estFinishMs = estimateTicketFinishMs(copy);
        return copy;
      });
      sortPendingByFinish(pend);
      document.getElementById("pending-count").textContent = `${pend.length} Active (${fmtMoney(data.pending_total)})`;
      renderPendingList(pend);

      // 3. Today's Curated Bets (sorted earliest-to-latest start time, live Kelly + 80% overlap wager sizing)
      document.getElementById("curated-date").textContent = data.curated_date;
      const plays = (data.curated_plays || []).map((p, origIdx) => {
        const copy = { ...p, __origIdx: origIdx };
        copy.__key = extractSelectionCore(copy.selection || copy.title || "") || `play-${origIdx}`;
        return copy;
      });
      sortCuratedByStartTime(plays, data.curated_date || data.today);
      computeSmartCuratedWagers(plays, pend, data);
      renderCuratedList(plays, data);

      if (pend.length > 0 || plays.length > 0) {
        fetchLiveScoresForPending(pend, plays);
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

    function getSportDurationMinutes(sport, selections) {
      const sp = normalizeSport(sport).toUpperCase();
      let mins = 165;
      if (sp === "EPL" || sp === "UCL" || sp === "LALIGA" || sp === "SOCCER" || sp === "PREMIER LEAGUE") mins = 120;
      else if (sp === "NCAAM") mins = 130;
      else if (sp === "NBA") mins = 150;
      else if (sp === "NHL") mins = 160;
      else if (sp === "MLB") mins = 175;
      else if (sp === "NFL") mins = 200;
      else if (sp === "CFB" || sp === "NCAAF") mins = 215;
      if (/\b(?:1h|1st\s*half|first\s*half)\b/i.test(String(selections || ""))) {
        mins = Math.round(mins * 0.48);
      }
      return mins;
    }

    function estimateTicketFinishMs(ticket) {
      const dateStr = String(ticket.date || (currentData && currentData.today) || "").trim();
      const sel = String(ticket.selections || "");
      const durMs = getSportDurationMinutes(ticket.sport, sel) * 60000;
      let baseDayMs = Date.now();
      if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
        const parsedDay = Date.parse(`${dateStr}T00:00:00-05:00`);
        if (!isNaN(parsedDay)) baseDayMs = parsedDay;
      }
      const tm = sel.match(/(\d{1,2}):(\d{2})\s*(AM|PM)/i);
      if (tm) {
        let hr = parseInt(tm[1], 10);
        const min = parseInt(tm[2], 10);
        const ampm = tm[3].toUpperCase();
        if (ampm === "PM" && hr !== 12) hr += 12;
        if (ampm === "AM" && hr === 12) hr = 0;
        const startMs = baseDayMs + (hr * 60 + min) * 60000;
        return startMs + durMs;
      }
      // Fallback if no time in selection: place after dated timed tickets for that day
      return baseDayMs + 24 * 3600000 + durMs;
    }

    function estimateLiveEspnFinish(match, ticket) {
      const state = match.status?.type?.state || "";
      const nowMs = Date.now();
      const durMins = getSportDurationMinutes(ticket.sport, ticket.selections);
      const is1H = /\b(?:1h|1st\s*half|first\s*half)\b/i.test(String(ticket.selections || ""));

      if (state === "post") {
        // Already finished (awaiting settlement) -> very top
        const endTs = match.date ? Date.parse(match.date) : nowMs;
        return { rank: 0, estFinishMs: isNaN(endTs) ? nowMs : endTs };
      }

      if (state === "in") {
        const sp = normalizeSport(ticket.sport).toUpperCase();
        const period = Number(match.status?.period || 1);
        const clockSec = Number(match.status?.clock || 0);
        const detail = String(match.status?.type?.shortDetail || match.status?.type?.detail || "").toLowerCase();

        // Soccer counts UP to 90'+
        if (sp === "EPL" || sp === "UCL" || sp === "LALIGA" || sp === "SOCCER" || sp === "PREMIER LEAGUE") {
          if (detail.includes("ht") || detail.includes("half")) {
            return { rank: 1, estFinishMs: nowMs + (is1H ? 0 : 60) * 60000 };
          }
          const minMatch = detail.match(/(\d{1,3})/);
          const elapsed = minMatch ? parseInt(minMatch[1], 10) : Math.floor(clockSec / 60);
          const target = is1H ? 47 : 96;
          const remReal = Math.max(1, target - elapsed);
          return { rank: 1, estFinishMs: nowMs + remReal * 60000 };
        }

        // Timed period sports (NCAAM/NBA/NHL/NFL/CFB)
        let regPeriods = 4, periodLenSec = 900, realPerGameMin = 3.2;
        if (sp === "NCAAM") { regPeriods = 2; periodLenSec = 1200; realPerGameMin = 3.2; }
        else if (sp === "NBA") { regPeriods = 4; periodLenSec = 720; realPerGameMin = 3.1; }
        else if (sp === "NHL") { regPeriods = 3; periodLenSec = 1200; realPerGameMin = 2.6; }
        else if (sp === "NFL" || sp === "CFB" || sp === "NCAAF") { regPeriods = 4; periodLenSec = 900; realPerGameMin = 3.4; }

        if (is1H) {
          const halfEndPeriod = Math.ceil(regPeriods / 2);
          if (period > halfEndPeriod || detail.includes("half")) {
            return { rank: 0, estFinishMs: nowMs };
          }
          const remGameSec = Math.max(0, halfEndPeriod - period) * periodLenSec + clockSec;
          return { rank: 1, estFinishMs: nowMs + Math.max(1, (remGameSec / 60) * realPerGameMin) * 60000 };
        }

        if (period > regPeriods) {
          // Overtime -> finishes very soon
          const otRemMin = Math.max(1, (clockSec / 60) * realPerGameMin);
          return { rank: 1, estFinishMs: nowMs + otRemMin * 60000 };
        }
        const remPeriodsAfter = Math.max(0, regPeriods - period);
        const remGameMin = (remPeriodsAfter * periodLenSec + clockSec) / 60;
        const intermissionMin = detail.includes("half") ? 12 : (remPeriodsAfter > 0 ? remPeriodsAfter * 3 : 0);
        const remRealMin = Math.max(1, remGameMin * realPerGameMin + intermissionMin);
        return { rank: 1, estFinishMs: nowMs + remRealMin * 60000 };
      }

      // Upcoming game ("pre"): use ESPN exact UTC start time + typical game duration
      const startMs = match.date ? Date.parse(match.date) : NaN;
      if (!isNaN(startMs)) {
        return { rank: 2, estFinishMs: startMs + durMins * 60000 };
      }
      return { rank: 2, estFinishMs: estimateTicketFinishMs(ticket) };
    }

    function sortPendingByFinish(list) {
      list.sort((a, b) => {
        const rA = a.__finishRank != null ? a.__finishRank : 2;
        const rB = b.__finishRank != null ? b.__finishRank : 2;
        if (rA !== rB) return rA - rB;
        const fA = a.__estFinishMs != null ? a.__estFinishMs : Number.MAX_SAFE_INTEGER;
        const fB = b.__estFinishMs != null ? b.__estFinishMs : Number.MAX_SAFE_INTEGER;
        if (fA !== fB) return fA - fB;
        return (a.__origIdx || 0) - (b.__origIdx || 0);
      });
    }

    function normTeamStr(s) {
      return String(s || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
    }

    function matchCompetitorForPick(away, home, teamText) {
      const normPick = normTeamStr(teamText);
      if (!normPick) return null;
      const pickTokens = normPick.split(" ").filter(w => w.length >= 3);

      function scoreComp(c) {
        const tm = c.team || {};
        const aliases = [tm.displayName, tm.shortDisplayName, tm.location, tm.name, tm.abbreviation]
          .map(normTeamStr)
          .filter(Boolean);
        let best = 0;
        for (const a of aliases) {
          if (a.length <= 3) {
            if (a.length >= 2 && new RegExp(`\\b${a}\\b`).test(normPick)) {
              best = Math.max(best, a.length + 1);
            }
          } else if (normPick.includes(a)) {
            best = Math.max(best, a.length + 10);
          } else if (normPick.length >= 4 && a.includes(normPick)) {
            best = Math.max(best, normPick.length + 6);
          }
        }
        if (best === 0 && pickTokens.length > 0) {
          const dispTokens = new Set(normTeamStr(tm.displayName).split(" ").filter(w => w.length >= 3));
          let hits = 0;
          pickTokens.forEach(w => { if (dispTokens.has(w)) hits++; });
          if (hits > 0) best = hits * 3;
        }
        return best;
      }

      const aScore = scoreComp(away);
      const hScore = scoreComp(home);
      if (aScore > hScore && aScore > 0) return { picked: away, opp: home };
      if (hScore > aScore && hScore > 0) return { picked: home, opp: away };
      return null;
    }

    function evaluateLiveTicketTrend(match, ticket) {
      const state = match.status?.type?.state || "";
      if (state !== "in" && state !== "post") return null;
      const comp = (match.competitions || [])[0];
      if (!comp) return null;
      const away = (comp.competitors || []).find(c => c.homeAway === "away");
      const home = (comp.competitors || []).find(c => c.homeAway === "home");
      if (!away || !home) return null;

      // Skip multi-leg parlays / SGPs
      const bType = String(ticket.bet_type || "").toLowerCase();
      if (bType.includes("parlay") || bType.includes("sgp") || bType.includes("teaser")) return null;

      let rawSel = String(ticket.selections || "").trim();
      if (!rawSel) return null;

      // Strip leading matchup prefix before em-dash / en-dash (e.g. "• Away @ Home (CBB) — Pick -5.5")
      const dashParts = rawSel.split(/\s*[—–]\s*/);
      let pickPart = rawSel;
      if (dashParts.length >= 2 && (/@|\bvs\.?\b/i.test(dashParts[0]))) {
        pickPart = dashParts.slice(1).join(" — ").trim();
      }
      // Reject obvious multi-leg separators
      if (/\s\+\s|[;|]/.test(pickPart)) return null;

      // Strip parenthetical time / odds / league tags like "(4:00 PM EST)", "(-110)", "(NCAAM)"
      const cleanPick = pickPart
        .replace(/\(\s*\d{1,2}:\d{2}\s*[AP]M[^)]*\)/gi, " ")
        .replace(/\(\s*[+-]\d{3,4}\s*\)/g, " ")
        .replace(/\(\s*(?:CBB|NCAAB|NCAAM|NBA|NHL|NFL|CFB|NCAAF|EPL|UCL|LALIGA|LA\s*LIGA|MLB|SOCCER|ATP|WTA)\s*\)/gi, " ")
        .replace(/\s+/g, " ")
        .trim();

      // Skip player props (team scoreboard cannot grade player stats)
      if (/\b(points|pts|rebounds|reb|assists|ast|shots|sog|goals|yards|yds|touchdowns|tds?|passing|rushing|receiving|pra|3pm|threes|strikeouts|saves|hits|bases|blocks|steals)\b/i.test(cleanPick)) {
        return null;
      }

      const sp = normalizeSport(ticket.sport).toUpperCase();
      const is1H = /\b(?:1h|1st\s*half|first\s*half)\b/i.test(cleanPick);
      const period = Number(match.status?.period || 1);
      const clockSec = Number(match.status?.clock || 0);
      const detailLow = String(match.status?.type?.shortDetail || match.status?.type?.detail || "").toLowerCase();

      function getScore(c) {
        if (is1H && Array.isArray(c.linescores) && c.linescores.length > 0) {
          const halfPeriods = (sp === "NCAAM") ? 1 : ((sp === "NBA" || sp === "NFL" || sp === "CFB" || sp === "NCAAF") ? 2 : 1);
          if (state === "post" || period > halfPeriods || detailLow.includes("half")) {
            let sum = 0;
            for (let i = 0; i < Math.min(halfPeriods, c.linescores.length); i++) {
              sum += Number(c.linescores[i]?.value || 0);
            }
            return sum;
          }
        }
        return Number(c.score || 0);
      }

      const aPts = getScore(away);
      const hPts = getScore(home);
      if (isNaN(aPts) || isNaN(hPts)) return null;

      const fmtSigned = (n) => {
        const rounded = Math.round(n * 10) / 10;
        return (rounded > 0 ? "+" : "") + rounded;
      };

      // 1. Game Totals (Over / Under)
      const totMatch = cleanPick.match(/\b(over|under)\s+(\d+(?:\.\d+)?)\b/i)
        || cleanPick.match(/(?:^|\s)([ou])\s*(\d+(?:\.\d+)?)\b/i);
      if (totMatch && !/\b(?:team\s*total|tt)\b/i.test(cleanPick)) {
        const isOver = totMatch[1].toLowerCase().startsWith("o");
        const line = parseFloat(totMatch[2]);
        if (!isNaN(line) && line >= 1.5) {
          const total = aPts + hPts;
          const halfOver = is1H && (state === "post" || detailLow.includes("half") || period > (sp === "NCAAM" ? 1 : 2));
          if (state === "post" || halfOver) {
            if (total === line) return { status: "push", note: `Push (${total})`, coverMargin: 0, isTotal: true, isOver, isFinal: true };
            const won = isOver ? total > line : total < line;
            return { status: won ? "win" : "loss", note: `${total} vs ${isOver ? "O" : "U"}${line}`, coverMargin: isOver ? (total - line) : (line - total), isTotal: true, isOver, isFinal: true };
          }
          if (isOver && total > line) return { status: "win", note: `Cashed (${total})`, coverMargin: total - line, isCashedOver: true, isTotal: true, isOver: true };
          if (!isOver && total > line) return { status: "loss", note: `Over (${total})`, coverMargin: line - total, isTotal: true, isOver: false };

          // Live pace projection
          let elapsedFrac = 0;
          if (sp === "EPL" || sp === "UCL" || sp === "LALIGA" || sp === "SOCCER" || sp === "PREMIER LEAGUE") {
            const minM = detailLow.match(/(\d{1,3})/);
            const elapsedMin = minM ? parseInt(minM[1], 10) : Math.floor(clockSec / 60);
            elapsedFrac = Math.min(1, elapsedMin / (is1H ? 45 : 90));
          } else {
            let regP = 4, pLen = 900;
            if (sp === "NCAAM") { regP = 2; pLen = 1200; }
            else if (sp === "NBA") { regP = 4; pLen = 720; }
            else if (sp === "NHL") { regP = 3; pLen = 1200; }
            const targetP = is1H ? Math.ceil(regP / 2) : regP;
            const totalSec = targetP * pLen;
            const completedSec = Math.max(0, Math.min(period - 1, targetP)) * pLen;
            const curElapsed = period <= targetP ? Math.max(0, pLen - clockSec) : 0;
            elapsedFrac = Math.min(1, (completedSec + curElapsed) / totalSec);
          }
          if (elapsedFrac >= 0.05) {
            const proj = total / elapsedFrac;
            const rawMargin = isOver ? (total - line) : (line - total);
            if (Math.abs(proj - line) < 0.05) return { status: "push", note: `Pace ${proj.toFixed(1)}`, coverMargin: rawMargin, isTotal: true, isOver };
            const trendingWin = isOver ? proj > line : proj < line;
            return { status: trendingWin ? "win" : "loss", note: `Pace ${proj.toFixed(1)}`, coverMargin: rawMargin, isTotal: true, isOver };
          }
          return null;
        }
      }

      // 2. Spread (e.g. "Ole Miss Rebels -5.5", "Oregon State +6.5", "Team PK")
      const spreadMatch = cleanPick.match(/(?:^|\s)([+-]\d+(?:\.\d+)?)(?:\s|$)/)
        || cleanPick.match(/\b(pk|pick(?:\s*'?\s*em)?)\b/i);
      if (spreadMatch) {
        const rawVal = spreadMatch[1].toLowerCase();
        const spreadVal = (rawVal === "pk" || rawVal.startsWith("pick")) ? 0 : parseFloat(rawVal);
        if (!isNaN(spreadVal) && Math.abs(spreadVal) < 60) {
          const teamText = cleanPick.slice(0, spreadMatch.index)
            .replace(/\b(?:1h|1st\s*half|first\s*half|spread|puck\s*line|run\s*line|alt(?:ernate)?)\b/gi, " ")
            .trim();
          const matched = matchCompetitorForPick(away, home, teamText);
          if (matched) {
            const pickedScore = matched.picked === away ? aPts : hPts;
            const oppScore = matched.opp === away ? aPts : hPts;
            const coverMargin = pickedScore - oppScore + spreadVal;
            if (coverMargin > 0) return { status: "win", note: `Covering (${fmtSigned(coverMargin)})`, coverMargin };
            if (coverMargin < 0) return { status: "loss", note: `Trailing (${fmtSigned(coverMargin)})`, coverMargin };
            return { status: "push", note: "On number", coverMargin: 0 };
          }
        }
      }

      // 3. Moneyline (e.g. "Belmont Bruins ML", "Chiefs Moneyline", "Arsenal to win")
      const mlMatch = cleanPick.match(/\b(ml|moneyline|to\s+win)\b/i);
      const teamCandidate = (mlMatch ? cleanPick.slice(0, mlMatch.index) : cleanPick)
        .replace(/\b(?:1h|1st\s*half|first\s*half|3-way|3\s*way)\b/gi, " ")
        .trim();
      const matchedMl = matchCompetitorForPick(away, home, teamCandidate);
      if (matchedMl) {
        const pickedScore = matchedMl.picked === away ? aPts : hPts;
        const oppScore = matchedMl.opp === away ? aPts : hPts;
        const margin = pickedScore - oppScore;
        if (margin > 0) return { status: "win", note: `Ahead (+${margin})`, coverMargin: margin };
        if (margin < 0) return { status: "loss", note: `Behind (${margin})`, coverMargin: margin };
        if (state === "post") {
          const isSoccer = (sp === "EPL" || sp === "UCL" || sp === "LALIGA" || sp === "SOCCER" || sp === "PREMIER LEAGUE");
          return isSoccer
            ? { status: "loss", note: "Tied (Final)", coverMargin: 0 }
            : { status: "push", note: "Tied (Push)", coverMargin: 0 };
        }
        return { status: "push", note: "Tied", coverMargin: 0 };
      }

      return null;
    }

    function isTicketFinalForSettlement(match, ticket) {
      if (!match) return false;
      const st = match.status?.type || {};
      const state = st.state || "";
      if (state === "post" || st.completed === true) return true;
      // Allow 1H straight bets to settle once the 1st half is officially over and linescores exist
      const sel = String((ticket && ticket.selections) || "");
      if (/\b(?:1h|1st\s*half|first\s*half)\b/i.test(sel) && state === "in") {
        const sp = normalizeSport(ticket && ticket.sport).toUpperCase();
        const period = Number(match.status?.period || 1);
        const detailLow = String(st.shortDetail || st.detail || "").toLowerCase();
        const halfPeriods = (sp === "NCAAM") ? 1 : ((sp === "NBA" || sp === "NFL" || sp === "CFB" || sp === "NCAAF") ? 2 : 1);
        const comp = (match.competitions || [])[0];
        const comps = (comp && comp.competitors) || [];
        const hasLines = comps.length === 2 && comps.every(c => Array.isArray(c.linescores) && c.linescores.length >= halfPeriods);
        if (hasLines && (period > halfPeriods || detailLow.includes("half"))) {
          return true;
        }
      }
      return false;
    }

    function evaluateBlowoutAnticipation(match, ticket, trend) {
      if (!match || !trend || trend.status !== "win") return { anticipated: false, reason: "" };
      if (isTicketFinalForSettlement(match, ticket)) {
        return { anticipated: true, reason: "Final (awaiting settle)" };
      }
      if (trend.isCashedOver) {
        return { anticipated: true, reason: "Over already cashed" };
      }
      const st = match.status?.type || {};
      if ((st.state || "") !== "in") return { anticipated: false, reason: "" };

      const coverMargin = Number(trend.coverMargin);
      if (isNaN(coverMargin) || coverMargin <= 0) return { anticipated: false, reason: "" };

      const sp = normalizeSport(ticket && ticket.sport).toUpperCase();
      const sel = String((ticket && ticket.selections) || "");
      const is1H = /\b(?:1h|1st\s*half|first\s*half)\b/i.test(sel);
      const period = Number(match.status?.period || 1);
      const clockSec = Number(match.status?.clock || 0);
      const detailLow = String(st.shortDetail || st.detail || "").toLowerCase();
      const isUnder = Boolean(trend.isTotal && !trend.isOver);
      const marginTxt = `+${Math.round(coverMargin * 10) / 10}`;

      if (sp === "EPL" || sp === "UCL" || sp === "LALIGA" || sp === "SOCCER" || sp === "PREMIER LEAGUE") {
        const minM = detailLow.match(/(\d{1,3})/);
        const elapsedMin = minM ? parseInt(minM[1], 10) : Math.floor(clockSec / 60);
        const targetMin = is1H ? 45 : 90;
        const remMin = Math.max(0, targetMin - elapsedMin);
        const hit = (remMin <= 12 && coverMargin >= 2.5) || (remMin <= 6 && coverMargin >= 1.5);
        return hit ? { anticipated: true, reason: `Blowout (${marginTxt}, ${remMin}m left)` } : { anticipated: false, reason: "" };
      }

      if (sp === "MLB") {
        if (is1H) return { anticipated: false, reason: "" };
        const hit = (period >= 9 && coverMargin >= 4) || (period >= 8 && coverMargin >= 6);
        return hit ? { anticipated: true, reason: `Blowout (${marginTxt}, Inn ${period})` } : { anticipated: false, reason: "" };
      }

      let regP = 4, pLen = 900;
      if (sp === "NCAAM") { regP = 2; pLen = 1200; }
      else if (sp === "NBA") { regP = 4; pLen = 720; }
      else if (sp === "NHL") { regP = 3; pLen = 1200; }
      const targetP = is1H ? Math.ceil(regP / 2) : regP;
      const remSec = period <= targetP
        ? Math.max(0, targetP - period) * pLen + Math.max(0, clockSec)
        : Math.max(0, clockSec);
      const remMin = remSec / 60;

      let hit = false;
      if (sp === "NBA") {
        hit = isUnder
          ? ((remMin <= 5 && coverMargin >= 24) || (remMin <= 3 && coverMargin >= 18) || (remMin <= 1.5 && coverMargin >= 12))
          : ((remMin <= 5 && coverMargin >= 18) || (remMin <= 3 && coverMargin >= 14) || (remMin <= 1.5 && coverMargin >= 10));
      } else if (sp === "NCAAM") {
        hit = isUnder
          ? ((remMin <= 5 && coverMargin >= 22) || (remMin <= 3 && coverMargin >= 16) || (remMin <= 1.5 && coverMargin >= 11))
          : ((remMin <= 5 && coverMargin >= 16) || (remMin <= 3 && coverMargin >= 12) || (remMin <= 1.5 && coverMargin >= 9));
      } else if (sp === "NFL" || sp === "CFB" || sp === "NCAAF") {
        hit = isUnder
          ? ((remMin <= 4 && coverMargin >= 17) || (remMin <= 2 && coverMargin >= 11))
          : ((remMin <= 6 && coverMargin >= 17) || (remMin <= 3 && coverMargin >= 14) || (remMin <= 1.5 && coverMargin >= 10));
      } else if (sp === "NHL") {
        hit = isUnder
          ? ((remMin <= 4 && coverMargin >= 3.5) || (remMin <= 2 && coverMargin >= 2.5))
          : ((remMin <= 6 && coverMargin >= 3.5) || (remMin <= 3 && coverMargin >= 2.5) || (remMin <= 1.5 && coverMargin >= 1.5));
      }

      return hit
        ? { anticipated: true, reason: `Blowout (${marginTxt}, ${remMin.toFixed(1)}m left)` }
        : { anticipated: false, reason: "" };
    }

    function renderPendingList(pend) {
      const pList = document.getElementById("pending-list");
      window.__pendingTickets = pend;
      if (!pend || pend.length === 0) {
        pList.innerHTML = `<div class="empty-msg">No active pending bets right now.</div>`;
        return;
      }
      pList.innerHTML = pend.map((t, idx) => {
        const clvBadge = (t.clv !== null && t.clv !== undefined)
          ? ` <span class="pill ${t.clv >= 0 ? "pill-green" : "pill-red"}">CLV ${t.clv >= 0 ? "+" : ""}${t.clv}%</span>`
          : "";
        const trendCls = t.__trend === "win"
          ? " trend-win"
          : (t.__trend === "loss" ? " trend-loss" : (t.__trend === "push" ? " trend-push" : ""));
        const liveStyle = `font-weight:700;color:${t.__liveColor || "#60a5fa"};`;
        return `
        <div class="bet-item${trendCls}">
          <div class="bet-top">
            <div class="bet-title">${esc(t.bet_id)} • ${esc(t.selections)}</div>
            <div><span class="pill pill-amber">${esc(t.odds)} | ${fmtMoney(t.stake)} &rarr; ${fmtMoney(t.payout)}</span>${clvBadge}</div>
          </div>
          <div class="bet-meta" style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
            <span>${esc(normalizeSport(t.sport))} &bull; ${esc(t.bet_type)} &bull; Placed ${esc(t.date)}</span>
            <span id="live-score-${idx}" style="${liveStyle}">${esc(t.__liveText || "")}</span>
          </div>
          <div class="bet-actions">
            <button class="btn-mini" onclick="openEditModal(window.__pendingTickets[${idx}])">✏️ Edit / Cash Out</button>
          </div>
        </div>
      `;
      }).join("");
    }

    function formatEspnTimeEst(isoDateStr) {
      if (!isoDateStr) return "";
      const dt = new Date(isoDateStr);
      if (isNaN(dt.getTime())) return "";
      try {
        const parts = new Intl.DateTimeFormat("en-US", {
          timeZone: "America/New_York",
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        }).format(dt);
        return `${parts} EST`;
      } catch (e) {
        return "";
      }
    }

    function findEspnMatchForText(evts, textLower) {
      const hay = String(textLower || "").toLowerCase();
      if (!hay) return null;
      let bestEv = null;
      let bestScore = 0;

      for (const ev of (evts || [])) {
        const comp = (ev.competitions || [])[0];
        if (!comp) continue;
        let evScore = 0;
        for (const c of (comp.competitors || [])) {
          const display = (c.team?.displayName || "").toLowerCase();
          const shortN = (c.team?.shortDisplayName || "").toLowerCase();
          const loc = (c.team?.location || "").toLowerCase();
          const name = (c.team?.name || "").toLowerCase();
          const abbr = (c.team?.abbreviation || "").toLowerCase();
          let tmBest = 0;
          if (display && hay.includes(display)) {
            tmBest = Math.max(tmBest, 100 + display.length);
          }
          for (const cand of [shortN, loc, name]) {
            if (cand && cand.length > 2) {
              const escC = cand.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
              if (new RegExp(`\\b${escC}\\b`).test(hay)) {
                tmBest = Math.max(tmBest, 50 + cand.length);
              } else if (hay.includes(cand)) {
                tmBest = Math.max(tmBest, 20 + cand.length);
              }
            }
          }
          if (abbr && abbr.length >= 3) {
            const escA = abbr.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            if (new RegExp(`\\b${escA}\\b`).test(hay)) {
              tmBest = Math.max(tmBest, 35 + abbr.length);
            }
          }
          evScore += tmBest;
        }
        if (evScore > bestScore) {
          bestScore = evScore;
          bestEv = ev;
        }
      }
      return bestEv;
    }

    const ESPN_SPORT_ENDPOINTS = {
      "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
      "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard",
      "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
      "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=500",
      "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=500",
      "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50&limit=500",
      "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
      "SOCCER": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
      "PREMIER LEAGUE": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
      "UCL": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard",
      "LALIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
      "LA LIGA": "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",
    };

    async function fetchEspnEventsMap(pendingTickets, curatedPlays) {
      const todayYmd = ((currentData && currentData.today) || new Date().toISOString().slice(0, 10)).replace(/-/g, "");
      const pairs = new Map(); // key: `${sp}:${ymd}` -> { sp, ymd }
      (pendingTickets || []).forEach(t => {
        const sp = normalizeSport(t.sport).toUpperCase();
        if (!ESPN_SPORT_ENDPOINTS[sp]) return;
        const tYmd = String(t.date || "").replace(/-/g, "");
        if (/^\d{8}$/.test(tYmd)) pairs.set(`${sp}:${tYmd}`, { sp, ymd: tYmd });
        pairs.set(`${sp}:${todayYmd}`, { sp, ymd: todayYmd });
      });
      (curatedPlays || []).forEach(p => {
        const sp = normalizeSport(p.sport || parseTitleFallback(p.title).sport || "").toUpperCase();
        if (!ESPN_SPORT_ENDPOINTS[sp]) return;
        pairs.set(`${sp}:${todayYmd}`, { sp, ymd: todayYmd });
      });

      const eventsBySport = {};
      const seenBySport = {};
      await Promise.all([...pairs.values()].map(async ({ sp, ymd }) => {
        try {
          const base = ESPN_SPORT_ENDPOINTS[sp];
          const sep = base.includes("?") ? "&" : "?";
          const r = await fetch(`${base}${sep}dates=${ymd}`, { cache: "no-store" });
          const j = await r.json();
          if (!eventsBySport[sp]) {
            eventsBySport[sp] = [];
            seenBySport[sp] = new Set();
          }
          for (const ev of (j.events || [])) {
            const id = String(ev.id || "");
            if (id && seenBySport[sp].has(id)) continue;
            if (id) seenBySport[sp].add(id);
            eventsBySport[sp].push(ev);
          }
        } catch (e) {
          if (!eventsBySport[sp]) eventsBySport[sp] = [];
        }
      }));
      return eventsBySport;
    }

    async function settleFinalPendingFromBrowser(dash) {
      const pending = (dash && dash.pending_tickets) || [];
      if (pending.length === 0) return { settledCount: 0, notes: [], dashboard: dash };
      const eventsBySport = await fetchEspnEventsMap(pending, []);
      let latestDash = dash;
      let settledCount = 0;
      const notes = [];

      for (const t of pending) {
        const sp = normalizeSport(t.sport).toUpperCase();
        const match = findEspnMatchForText(eventsBySport[sp], (t.selections || "").toLowerCase());
        if (!match || !isTicketFinalForSettlement(match, t)) continue;
        const trend = evaluateLiveTicketTrend(match, t);
        if (!trend || !trend.status) continue;
        const finalRes = trend.status === "win" ? "Won" : (trend.status === "loss" ? "Lost" : (trend.status === "push" ? "Push" : ""));
        if (!finalRes) continue;
        try {
          const editRes = await postLiveApi({
            action: "edit_bet",
            row_index: t.row_index || null,
            bet_id: t.bet_id,
            result: finalRes,
          });
          if (editRes && editRes.ok) {
            settledCount++;
            notes.push(`${t.bet_id} ${finalRes}`);
            if (editRes.dashboard) latestDash = editRes.dashboard;
          }
        } catch (e) {
          console.warn(`Client settle failed for ${t.bet_id}:`, e);
        }
      }
      return { settledCount, notes, dashboard: latestDash };
    }

    let pendingScoreSeq = 0;
    async function fetchLiveScoresForPending(pendingTickets, curatedPlays) {
      const mySeq = ++pendingScoreSeq;
      const eventsBySport = await fetchEspnEventsMap(pendingTickets, curatedPlays);
      if (mySeq !== pendingScoreSeq) return;

      (pendingTickets || []).forEach(t => {
        t.__anticipatedWin = false;
        t.__anticipatedReason = "";
        const sp = normalizeSport(t.sport).toUpperCase();
        const match = findEspnMatchForText(eventsBySport[sp], (t.selections || "").toLowerCase());
        if (!match) return;
        const comp = match.competitions[0];
        const away = comp.competitors.find(c => c.homeAway === "away");
        const home = comp.competitors.find(c => c.homeAway === "home");
        const state = match.status?.type?.state || "";
        const detail = match.status?.type?.shortDetail || "";
        if (!away || !home) return;
        t.__espnState = state;
        if (match.date) {
          const sMs = Date.parse(match.date);
          if (!isNaN(sMs)) t.__startMs = sMs;
        }
        const aAbbr = away.team?.abbreviation || away.team?.shortDisplayName || "";
        const hAbbr = home.team?.abbreviation || home.team?.shortDisplayName || "";
        const trend = evaluateLiveTicketTrend(match, t);
        t.__trend = trend ? trend.status : "";
        const ant = evaluateBlowoutAnticipation(match, t, trend);
        t.__anticipatedWin = ant.anticipated;
        t.__anticipatedReason = ant.reason;
        const trendSuffix = trend && trend.note ? ` • ${trend.note}` : "";
        const antSuffix = (ant.anticipated && state === "in") ? " • ⚡ Projected Win" : "";
        if (state === "in") {
          t.__liveColor = trend
            ? (trend.status === "win" ? "#34d399" : (trend.status === "loss" ? "#f87171" : "#fbbf24"))
            : "#10b981";
          t.__liveText = `🔴 LIVE (${detail}) • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}${trendSuffix}${antSuffix}`;
        } else if (state === "post") {
          t.__liveColor = trend
            ? (trend.status === "win" ? "#34d399" : (trend.status === "loss" ? "#f87171" : "#fbbf24"))
            : "#94a3b8";
          t.__liveText = `FINAL • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}${trendSuffix}`;
        } else {
          t.__liveColor = "#60a5fa";
          t.__liveText = `🕒 ${detail}`;
        }
        const est = estimateLiveEspnFinish(match, t);
        t.__finishRank = est.rank;
        t.__estFinishMs = est.estFinishMs;
      });

      if (pendingTickets && pendingTickets.length > 0) {
        sortPendingByFinish(pendingTickets);
        renderPendingList(pendingTickets);
      }

      // Sync exact ESPN tipoff/kickoff times & live game state onto Curated Plays and re-sort earliest-to-latest
      if (curatedPlays && curatedPlays.length > 0) {
        curatedPlays.forEach(p => {
          const sp = normalizeSport(p.sport || parseTitleFallback(p.title).sport || "").toUpperCase();
          const match = findEspnMatchForText(eventsBySport[sp], `${p.selection || ""} ${p.title || ""}`.toLowerCase());
          if (!match) return;
          const state = match.status?.type?.state || "";
          const detail = match.status?.type?.shortDetail || "";
          if (state) p.__espnState = state;
          const comp = (match.competitions || [])[0];
          const away = comp && (comp.competitors || []).find(c => c.homeAway === "away");
          const home = comp && (comp.competitors || []).find(c => c.homeAway === "home");
          if (away && home) {
            const aAbbr = away.team?.abbreviation || away.team?.shortDisplayName || "";
            const hAbbr = home.team?.abbreviation || home.team?.shortDisplayName || "";
            if (state === "in") {
              p.__liveColor = "#10b981";
              p.__liveText = `🔴 LIVE (${detail}) • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}`;
            } else if (state === "post") {
              p.__liveColor = "#94a3b8";
              p.__liveText = `FINAL • ${aAbbr} ${away.score} - ${hAbbr} ${home.score}`;
            } else {
              p.__liveColor = "";
              p.__liveText = "";
            }
          }
          if (!match.date) return;
          const exactTime = formatEspnTimeEst(match.date);
          const startMs = Date.parse(match.date);
          if (!isNaN(startMs)) p.__startMs = startMs;
          if (!exactTime) return;
          p.__exactTime = exactTime;
          p.game_time = exactTime;
          let updatedTitle = String(p.title || "");
          if (/^\[Window\s*\d+:[^\]]*\]/i.test(updatedTitle)) {
            updatedTitle = updatedTitle.replace(/^\[Window\s*\d+:[^\]]*\]/i, `[${exactTime}]`);
          } else if (/^\[?\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?\]?/i.test(updatedTitle)) {
            updatedTitle = updatedTitle.replace(/^\[?\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?\]?/i, `[${exactTime}]`);
          }
          p.title = updatedTitle;
          if (p.selection) {
            if (/\(Window\s*\d+:[^)]*\)/i.test(p.selection)) {
              p.selection = p.selection.replace(/\(Window\s*\d+:[^)]*\)/i, `(${exactTime})`);
            } else if (/\(\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?\)/i.test(p.selection)) {
              p.selection = p.selection.replace(/\(\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?\)/i, `(${exactTime})`);
            }
          }
        });
        sortCuratedByStartTime(curatedPlays, currentData && (currentData.curated_date || currentData.today));
        computeSmartCuratedWagers(curatedPlays, pendingTickets, currentData || {});
        renderCuratedList(curatedPlays, currentData || {});
      }
    }

    function parseCuratedDisplayParts(play) {
      const rawTitle = String(play.title || "").trim();
      const fb = parseTitleFallback(rawTitle);

      // 1. Clean Start Time (never show "Window X:")
      let startTime = play.__exactTime || "";
      if (!startTime) {
        const tmSrc = `${play.game_time || ""} ${rawTitle}`;
        const tm = tmSrc.match(/(\d{1,2}:\d{2}\s*[AP]M)(?:\s*EST)?/i);
        if (tm) startTime = `${tm[1].toUpperCase()} EST`;
      }

      // 2. Clean Sport
      let sport = String(play.sport || fb.sport || "").trim().toUpperCase();
      const spParen = rawTitle.match(/\((CBB|NCAAB|NCAAM|NBA|NHL|NFL|CFB|NCAAF|EPL|UCL|LALIGA|LA\s*LIGA|MLB|UFC|SOCCER|ATP|WTA)\)/i);
      if (spParen) sport = spParen[1].toUpperCase();
      sport = normalizeSport(sport.replace(/\s*\([^)]*\)/g, "").trim());

      // 3. Clean Team Names (Matchup) + Clean Bet
      let stripped = rawTitle
        .replace(/^\[[^\]]*\]\s*/, "")
        .replace(/^\d{1,2}:\d{2}\s*[AP]M(?:\s*EST)?\s*/i, "")
        .replace(/^[•·]\s*/, "")
        .trim();

      // Remove trailing wager / confidence segments like "— [$21.08 (35% BR)]" or "— $21.08 (...)"
      stripped = stripped
        .replace(/\s*[—–]\s*\[?\d{2,3}%\s*[^\]]*\]?$/g, "")
        .replace(/\s*[—–]\s*\[?\$[\d.,]+[^\]]*\]?$/g, "")
        .replace(/\s*[—–]\s*\[?\d{1,2}(?:\.\d+)?%\s*of\s+Resulting\s+BR[^\]]*\]?$/gi, "")
        .trim();

      let matchup = "";
      let betText = stripped;

      const mStream = stripped.match(/^([^—–]+?)\s*\(([A-Za-z0-9/ ]{2,10})\)\s*[—–]\s*(.+)$/);
      if (mStream) {
        matchup = mStream[1].trim();
        betText = mStream[3].trim();
      } else if (stripped.includes("—")) {
        const parts = stripped.split("—").map(s => s.trim()).filter(Boolean);
        if (parts.length >= 2 && (parts[0].includes("@") || /\bvs\.?\b/i.test(parts[0]))) {
          matchup = parts[0].replace(/\s*\([A-Za-z0-9/ ]{2,10}\)\s*$/, "").trim();
          betText = parts[1];
        } else {
          betText = parts[0];
        }
      }

      // Fallback matchup from game_line if not in title
      if (!matchup && play.game_line) {
        const firstSeg = String(play.game_line).split(/[•|]/)[0].trim();
        if (firstSeg.includes("@") || /\bvs\.?\b/i.test(firstSeg)) {
          matchup = firstSeg;
        }
      }

      // Final cleanup on betText (remove any stray leading/trailing brackets or Window tags)
      betText = betText
        .replace(/\s*[—–]\s*\[?\$[\d.,]+.*$/g, "")
        .replace(/\s*\(Window\s*\d+:[^)]*\)/gi, "")
        .trim();

      const wagerVal = Number(play.stake || fb.stake || 0);
      const brRef = Number((play.__wagerBreakdown && play.__wagerBreakdown.unitSize) ? play.__wagerBreakdown.unitSize * 20 : ((currentData && currentData.bankroll) || 0));
      const unitsVal = (play.units != null && Number(play.units) > 0)
        ? Number(play.units)
        : (wagerVal > 0 && brRef > 0 ? Math.round((wagerVal / (brRef * 0.05)) * 10) / 10 : 0);
      const wagerText = wagerVal > 0
        ? (unitsVal > 0 ? `${fmtMoney(wagerVal)} (${unitsVal.toFixed(1)}U)` : fmtMoney(wagerVal))
        : "";

      return { startTime, sport, matchup, betText, wagerText, unitsVal };
    }

    function estimateCuratedStartMs(play, dateStr) {
      if (play.__startMs && !isNaN(play.__startMs)) return play.__startMs;
      const parts = parseCuratedDisplayParts(play);
      const dStr = String(dateStr || (currentData && currentData.today) || "").trim();
      let baseDayMs = Date.now();
      if (/^\d{4}-\d{2}-\d{2}$/.test(dStr)) {
        const parsedDay = Date.parse(`${dStr}T00:00:00-05:00`);
        if (!isNaN(parsedDay)) baseDayMs = parsedDay;
      }
      const tm = String(parts.startTime || play.title || "").match(/(\d{1,2}):(\d{2})\s*(AM|PM)/i);
      if (tm) {
        let hr = parseInt(tm[1], 10);
        const min = parseInt(tm[2], 10);
        const ampm = tm[3].toUpperCase();
        if (ampm === "PM" && hr !== 12) hr += 12;
        if (ampm === "AM" && hr === 12) hr = 0;
        return baseDayMs + (hr * 60 + min) * 60000;
      }
      return baseDayMs + 24 * 3600000;
    }

    function isCuratedPlayStarted(play, dateStr, nowMs) {
      if (play.__espnState === "in" || play.__espnState === "post") return true;
      if (play.__espnState === "pre") return false;
      const now = nowMs != null ? nowMs : Date.now();
      return now >= estimateCuratedStartMs(play, dateStr);
    }

    function sortCuratedByStartTime(plays, dateStr) {
      (plays || []).sort((a, b) => {
        const sA = estimateCuratedStartMs(a, dateStr);
        const sB = estimateCuratedStartMs(b, dateStr);
        if (sA !== sB) return sA - sB;
        return (a.__origIdx || 0) - (b.__origIdx || 0);
      });
    }

    function buildCuratedCardsHtml(subsetPlays, data, domPrefix) {
      const pendingList = (data && data.pending_tickets) || [];
      const todaySettledList = (data && data.today_stats && data.today_stats.tickets) || [];

      function findMatchingPending(play) {
        return findMatchingTicketForPlay(pendingList, play);
      }

      function findMatchingSettledToday(play) {
        return findMatchingTicketForPlay(todaySettledList, play);
      }

      return subsetPlays.map((p, i) => {
        const globalIdx = p.__allIdx != null ? p.__allIdx : i;
        const domId = `${domPrefix}-${i}`;
        const isPass = Boolean(p.is_pass) || (p.title || "").includes("⛔") || /\bPASS\b/.test(p.title || "") || /NO GOOD PLAY/i.test(p.title || "");
        const existingPend = isPass ? null : findMatchingPending(p);
        const existingSettled = (isPass || existingPend) ? null : findMatchingSettledToday(p);
        const cp = isPass ? null : p.confidence_pct;
        const dParts = parseCuratedDisplayParts(p);
        const wb = p.__wagerBreakdown || null;
        const drawerKey = p.__key || String(globalIdx);
        const isOpen = openDrawers.has(drawerKey);

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
                <span style="color:var(--muted);font-weight:500;" id="curated-caret-${domId}">${isOpen ? "▴ Hide details" : "▾ Tap for details"}</span>
              </div>
              <div style="width:100%;height:6px;background:rgba(255,255,255,0.08);border-radius:999px;overflow:hidden;">
                <div style="width:${Math.min(100, Math.max(0, cp))}%;height:100%;background:${barColor};border-radius:999px;"></div>
              </div>
            </div>
          `;
        } else if (!isPass && (p.game_line || p.confidence_math || p.edge)) {
          meterHtml = `
            <div style="margin-top:6px;display:flex;justify-content:flex-end;font-size:0.75rem;color:var(--muted);">
              <span id="curated-caret-${domId}">${isOpen ? "▴ Hide details" : "▾ Tap for details"}</span>
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
          : `<div style="margin-bottom:3px;">• ${esc(dParts.matchup || dParts.betText)}</div>`;

        let wagerBreakdownHtml = "";
        if (wb && !existingSettled) {
          const oddsTxt = wb.oddsVal > 0 ? `+${wb.oddsVal}` : `${wb.oddsVal}`;
          const overlapLabel = wb.overlapCount > 0 ? `${wb.overlapCount} overlapping game${wb.overlapCount === 1 ? "" : "s"}` : "solo window";
          const extraLines = [];
          if (wb.antIds && wb.antIds.length > 0) {
            extraLines.push(`<div style="color:#34d399;margin-top:4px;">⚡ Anticipates ${esc(wb.antIds.join(", "))} win (+${fmtMoney(wb.antProfit)} projected profit → ${fmtMoney(wb.effBr)} sizing base; displayed bankroll stays untouched until final).</div>`);
          }
          if (wb.cashNowNote) {
            extraLines.push(`<div style="color:#fbbf24;margin-top:4px;">💵 ${esc(wb.cashNowNote)}</div>`);
          }
          if (wb.isDrawdown) {
            extraLines.push(`<div style="color:#fbbf24;margin-top:4px;">🛡️ Drawdown Brake active: caps tightened to ${wb.maxSinglePct}% single / ${wb.maxConcPct}% concurrent.</div>`);
          }
          if (wb.goalBrake) {
            extraLines.push(`<div style="color:#34d399;margin-top:4px;">🎯 $1,000 Goal-Lock Brake: stake trimmed so a win crosses $1,000 cleanly without extra risk.</div>`);
          }
          wagerBreakdownHtml = `
            <div style="margin-top:12px;padding-top:10px;border-top:1px dashed rgba(255,255,255,0.1);">
              <div style="font-weight:700;color:#e2e8f0;margin-bottom:6px;">💰 Wager Sizing Breakdown</div>
              <div style="display:flex;flex-wrap:wrap;gap:6px;">
                <span class="pill pill-blue" style="font-size:0.74rem;">1U = ${fmtMoney(wb.unitSize)} (5% BR)</span>
                <span class="pill pill-blue" style="font-size:0.74rem;">Win Prob: ${wb.probPct}% @ ${esc(oddsTxt)}</span>
                <span class="pill pill-blue" style="font-size:0.74rem;">Kelly: ${wb.kellyPct}% (cap ${wb.maxSinglePct}%)</span>
                <span class="pill ${wb.peakConcPct >= wb.maxConcPct - 2 ? "pill-amber" : "pill-green"}" style="font-size:0.74rem;">Concurrent: ${wb.peakConcPct}% / ${wb.maxConcPct}% (${esc(overlapLabel)})</span>
              </div>
              ${extraLines.join("")}
            </div>
          `;
        }

        const drawerHtml = isPass ? "" : `
          <div id="curated-detail-${domId}" style="display:${isOpen ? "block" : "none"};margin-top:12px;padding-top:12px;border-top:1px solid var(--card-border);font-size:0.83rem;">
            <div style="font-weight:700;color:#e2e8f0;margin-bottom:5px;">🏟️ Game &amp; Line Info</div>
            <div style="color:var(--muted);line-height:1.45;margin-bottom:10px;">
              ${gameLineHtml}
            </div>
            <div style="font-weight:700;color:#e2e8f0;margin-bottom:4px;">🧮 How Confidence (${cp !== null && cp !== undefined ? cp + "%" : "--"}) Was Chosen</div>
            ${pillarChipsHtml}
            ${wagerBreakdownHtml}
          </div>
        `;

        let actionHtml = "";
        if (!isPass) {
          if (existingPend) {
            const pendUnit = (data && data.bankroll > 0) ? ` • ${(Number(existingPend.stake || 0) / (data.bankroll * 0.05)).toFixed(1)}U` : "";
            const recalcNote = (p.recalculated_stake && Math.abs(p.recalculated_stake - existingPend.stake) >= 0.01)
              ? `<span class="pill pill-blue">${esc(p.wager_pct || "")}% of ${fmtMoney(data.bankroll)} BR = ${fmtMoney(p.recalculated_stake)}</span>`
              : "";
            actionHtml = `
              <div class="bet-actions" onclick="event.stopPropagation()">
                <span class="pill pill-green">✓ Placed • Pending (${esc(existingPend.bet_id)} • ${fmtMoney(existingPend.stake)}${pendUnit})</span>
                ${recalcNote}
                <button class="btn-mini" onclick="event.stopPropagation(); if(confirm('You already have ${esc(existingPend.bet_id)} pending on this team. Log another bet anyway?')) openLogModal(window.__curatedPlays[${globalIdx}])">+ Log Again</button>
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
            const statusPills = [];
            if (wb && wb.antIds && wb.antIds.length > 0) {
              statusPills.push(`<span class="pill pill-green">⚡ Anticipates ${esc(wb.antIds.join(", "))} (+${fmtMoney(wb.antProfit)})</span>`);
            }
            if (wb && wb.cashNowNote) {
              statusPills.push(`<span class="pill pill-amber">💵 ${esc(wb.cashNowNote)}</span>`);
            }
            if (wb && wb.isDrawdown) {
              statusPills.push(`<span class="pill pill-amber">🛡️ Drawdown Brake (${wb.maxSinglePct}%/${wb.maxConcPct}%)</span>`);
            }
            if (wb && wb.goalBrake) {
              statusPills.push(`<span class="pill pill-green">🎯 $1k Goal-Lock</span>`);
            }
            if (wb && wb.cappedByOverlap) {
              actionHtml = `
                <div class="bet-actions" onclick="event.stopPropagation()">
                  <span class="pill pill-amber">⏸️ Concurrent ${wb.maxConcPct}% Cap Reached</span>
                  ${statusPills.join("")}
                  <button class="btn-mini" onclick="event.stopPropagation(); openLogModal(window.__curatedPlays[${globalIdx}])">+ Log Manually</button>
                </div>`;
            } else {
              const btnLabel = dParts.unitsVal > 0
                ? `+ Log Bet (${fmtMoney(p.stake)} • ${dParts.unitsVal.toFixed(1)}U)`
                : `+ Log Bet (${fmtMoney(p.stake)})`;
              actionHtml = `
                <div class="bet-actions" onclick="event.stopPropagation()">
                  <button class="btn-mini" style="border-color:rgba(59,130,246,0.55);color:#93c5fd;background:rgba(59,130,246,0.16);font-weight:700;padding:6px 12px;" onclick="event.stopPropagation(); openLogModal(window.__curatedPlays[${globalIdx}])">${btnLabel}</button>
                  ${statusPills.join("")}
                </div>`;
            }
          }
        }

        const safeKeyAttr = esc(drawerKey).replace(/'/g, "\\'");
        const clickAttr = isPass ? "" : ` style="cursor:pointer;transition:background 0.15s;" onclick="toggleCuratedDetail('${domId}', '${safeKeyAttr}')"`;
        const liveScoreBadge = p.__liveText
          ? `<span style="font-size:0.74rem;font-weight:700;color:${p.__liveColor || "#94a3b8"};">${esc(p.__liveText)}</span>`
          : "";
        const metaRow = `
          <div class="curated-meta">
            ${dParts.startTime ? `<span class="curated-time">🕒 ${esc(dParts.startTime)}</span>` : ""}
            ${dParts.sport ? `<span class="curated-sport">${esc(dParts.sport)}</span>` : ""}
            ${liveScoreBadge}
          </div>
        `;
        const matchupRow = dParts.matchup ? `<div class="curated-matchup">${esc(dParts.matchup)}</div>` : "";
        const betRow = `
          <div class="curated-bet-row">
            <span class="curated-bet">${esc(dParts.betText)}</span>
            ${(!isPass && dParts.wagerText) ? `<span class="curated-wager">&bull; ${esc(dParts.wagerText)}</span>` : ""}
          </div>
        `;

        const statusCardCls = existingPend
          ? " curated-placed"
          : (existingSettled ? " curated-settled" : "");
        const placedTopPill = existingPend
          ? `<span class="pill pill-green" style="flex-shrink:0;">✓ PLACED</span>`
          : "";

        return `
        <div class="bet-item${statusCardCls}"${clickAttr}>
          <div class="bet-top" style="align-items:flex-start;">
            <div style="flex:1;min-width:0;">
              ${metaRow}
              ${matchupRow}
              ${betRow}
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
              ${placedTopPill}
              <span class="pill ${pillCls}">${esc(pillTxt)}</span>
            </div>
          </div>
          ${meterHtml}
          ${drawerHtml}
          ${actionHtml}
        </div>
      `;
      }).join("");
    }

    function renderCuratedList(plays, data) {
      const cList = document.getElementById("curated-list");
      const allPlays = plays || [];
      allPlays.forEach((p, idx) => { p.__allIdx = idx; });
      window.__curatedPlays = allPlays;
      const dateStr = (data && (data.curated_date || data.today)) || "";

      const upcomingPlays = [];
      const startedPlays = [];
      allPlays.forEach(p => {
        if (isCuratedPlayStarted(p, dateStr)) {
          startedPlays.push(p);
        } else {
          upcomingPlays.push(p);
        }
      });
      window.__startedCuratedPlays = startedPlays;
      window.__upcomingCuratedPlays = upcomingPlays;

      const pendList = (data && data.pending_tickets) || [];
      const placedUpcomingCount = upcomingPlays.filter(p => Boolean(findMatchingTicketForPlay(pendList, p))).length;
      const openUpcomingCount = Math.max(0, upcomingPlays.length - placedUpcomingCount);
      const cntEl = document.getElementById("curated-count");
      if (cntEl) {
        cntEl.textContent = placedUpcomingCount > 0
          ? `${openUpcomingCount} Open • ${placedUpcomingCount} Placed`
          : `${upcomingPlays.length} Upcoming`;
      }
      const hintEl = document.getElementById("curated-started-hint");
      if (hintEl) {
        hintEl.textContent = startedPlays.length > 0
          ? `${startedPlays.length} Started →`
          : (allPlays.length > 0 ? "Full Slate →" : "");
      }

      if (allPlays.length === 0) {
        cList.innerHTML = `<div class="empty-msg">${esc((data && data.curated_raw) || "No curated plays found yet.").replace(/\n/g, "<br>")}</div>`;
      } else if (upcomingPlays.length === 0) {
        cList.innerHTML = `<div class="empty-msg" style="cursor:pointer;" onclick="openCuratedArchiveModal('started')">All ${startedPlays.length} curated game${startedPlays.length === 1 ? "" : "s"} for today have already started. Tap here or the title above to view them &rarr;</div>`;
      } else {
        cList.innerHTML = buildCuratedCardsHtml(upcomingPlays, data, "main");
      }

      // If the Started / All Today modal is open, refresh it live too
      const archModal = document.getElementById("curated-archive-modal");
      if (archModal && archModal.classList.contains("open")) {
        renderCuratedArchiveModal(window.__activeCuratedTab || "started");
      }
    }

    function renderCuratedArchiveModal(tab) {
      tab = tab || "started";
      window.__activeCuratedTab = tab;
      const startedPlays = window.__startedCuratedPlays || [];
      const allPlays = window.__curatedPlays || [];
      const sCntEl = document.getElementById("curated-tab-started-cnt");
      const aCntEl = document.getElementById("curated-tab-all-cnt");
      if (sCntEl) sCntEl.textContent = String(startedPlays.length);
      if (aCntEl) aCntEl.textContent = String(allPlays.length);

      const tStarted = document.getElementById("curated-tab-started");
      const tAll = document.getElementById("curated-tab-all");
      if (tStarted) tStarted.classList.toggle("active", tab === "started");
      if (tAll) tAll.classList.toggle("active", tab === "all");

      const titleEl = document.getElementById("curated-archive-title");
      const dateLbl = (currentData && currentData.curated_date) ? ` (${currentData.curated_date})` : "";
      if (titleEl) {
        titleEl.textContent = tab === "started"
          ? `🔴 Started Curated Bets${dateLbl}`
          : `📋 All Today's Curated Bets${dateLbl}`;
      }

      const listEl = document.getElementById("curated-archive-list");
      const subset = tab === "started" ? startedPlays : allPlays;
      if (!subset || subset.length === 0) {
        listEl.innerHTML = tab === "started"
          ? `<div class="empty-msg">No curated games have started yet today.</div>`
          : `<div class="empty-msg">No curated plays found yet.</div>`;
      } else {
        listEl.innerHTML = buildCuratedCardsHtml(subset, currentData || {}, "modal");
      }
    }

    function openCuratedArchiveModal(tab) {
      renderCuratedArchiveModal(tab || "started");
      openModal("curated-archive-modal");
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

    populateOddsDropdowns();
    populateLineDropdown();
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

