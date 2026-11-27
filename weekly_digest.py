import datetime
import json
import os
import requests

import quant_signals

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _load_shadow_rows():
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        gcp_sa = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or ""
        sheet_id = (
            os.environ.get("SHEET_ID")
            or os.environ.get("MASTER_SHEET_ID")
            or os.environ.get("SPREADSHEET_ID")
            or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        if gcp_sa:
            creds = Credentials.from_service_account_info(json.loads(gcp_sa), scopes=scopes)
        elif os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        else:
            return []
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet("Shadow Grading Log")
        return ws.get_all_values()[1:]
    except Exception:
        return []


def build_weekly_digest_message(data, shadow_rows=None, as_of_date=None):
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    end_date = as_of_date or datetime.datetime.now(est_tz).date()
    start_date = end_date - datetime.timedelta(days=6)
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    settled = data.get("settled_tickets") or []
    week_tickets = [
        t for t in settled
        if start_str <= str(t.get("date") or "").strip() <= end_str
    ]

    wins = losses = pushes = 0
    staked = 0.0
    net_pl = 0.0
    clv_vals = []
    by_sport = {}
    by_market = {}

    def _acc(mp, key, res_l, st, pl):
        b = mp.setdefault(key, {"w": 0, "l": 0, "p": 0, "staked": 0.0, "pl": 0.0})
        if res_l == "won":
            b["w"] += 1
            b["staked"] += st
            b["pl"] += pl
        elif res_l == "lost":
            b["l"] += 1
            b["staked"] += st
            b["pl"] += pl
        elif res_l == "push":
            b["p"] += 1

    for t in week_tickets:
        res_l = str(t.get("result") or "").strip().lower()
        st = float(t.get("stake") or 0.0)
        pl = float(t.get("net_pl") or 0.0)
        sp = str(t.get("sport") or "Other").strip().upper() or "OTHER"
        mkt = str(t.get("market_type") or "Other").strip() or "Other"
        if t.get("clv") is not None:
            try:
                clv_vals.append(float(t["clv"]))
            except Exception:
                pass
        if res_l == "won":
            wins += 1
            staked += st
            net_pl += pl
        elif res_l == "lost":
            losses += 1
            staked += st
            net_pl += pl
        elif res_l == "push":
            pushes += 1
        _acc(by_sport, sp, res_l, st, pl)
        _acc(by_market, mkt, res_l, st, pl)

    dec = wins + losses
    win_pct = (wins / dec * 100.0) if dec > 0 else 0.0
    roi = (net_pl / staked * 100.0) if staked > 0 else 0.0
    bankroll = float(data.get("bankroll") or 0.0)
    unit_sz = max(1.0, bankroll * 0.05) if bankroll > 0 else 5.0
    units_pl = net_pl / unit_sz

    def _top_entry(mp):
        items = [(k, v) for k, v in mp.items() if (v["w"] + v["l"]) > 0]
        if not items:
            return None
        items.sort(key=lambda kv: (kv[1]["pl"], kv[1]["w"]), reverse=True)
        k, v = items[0]
        r = (v["pl"] / v["staked"] * 100.0) if v["staked"] > 0 else 0.0
        sgn = "+" if v["pl"] >= 0 else "-"
        return f"{k} ({v['w']}W-{v['l']}L, {sgn}${abs(v['pl']):.2f}, {r:+.1f}% ROI)"

    best_sport = _top_entry(by_sport)
    best_market = _top_entry(by_market)
    pl_sign = "+" if net_pl >= 0 else "-"
    u_sign = "+" if units_pl >= 0 else "-"

    lines = [
        f"📊 Lefty's Ledger — Weekly Recap ({start_str} to {end_str})",
        "",
        f"Record: {wins}W-{losses}L" + (f"-{pushes}P" if pushes else "") + f" ({win_pct:.1f}%)",
        f"Net P&L: {pl_sign}${abs(net_pl):.2f} ({u_sign}{abs(units_pl):.2f}U) | ROI: {roi:+.1f}% on ${staked:.2f}",
        f"Current Bankroll: ${bankroll:.2f}",
    ]
    if clv_vals:
        avg_clv = sum(clv_vals) / len(clv_vals)
        lines.append(f"Average CLV: {avg_clv:+.1f}% ({len(clv_vals)} tracked)")
    if best_sport:
        lines.append(f"Best Sport: {best_sport}")
    if best_market:
        lines.append(f"Best Market: {best_market}")

    # Shadow Model vs. Placed comparison
    sh = data.get("shadow_stats") or {}
    if int(sh.get("total") or 0) > 0:
        pl_b = sh.get("placed") or {}
        sk_b = sh.get("skipped") or {}
        lines.append("")
        lines.append(
            f"🎯 Model Audit (All-Time Shadow): {sh.get('wins', 0)}W-{sh.get('losses', 0)}L "
            f"({sh.get('win_pct', 0):.1f}%, {float(sh.get('flat_units', 0)):+.2f}U)"
        )
        lines.append(
            f"• Placed: {pl_b.get('wins', 0)}W-{pl_b.get('losses', 0)}L ({float(pl_b.get('flat_units', 0)):+.2f}U) | "
            f"Skipped: {sk_b.get('wins', 0)}W-{sk_b.get('losses', 0)}L ({float(sk_b.get('flat_units', 0)):+.2f}U)"
        )

    # Pillar Scorecard & Calibration Status
    if shadow_rows is None:
        shadow_rows = _load_shadow_rows()
    if shadow_rows:
        sc = quant_signals.compute_pillar_scorecard_from_rows(shadow_rows)
        active_p = [p for p in sc["pillars"] if p["total"] > 0]
        lines.append("")
        lines.append(f"⚙️ Pillar Calibration: {sc['note']}")
        if active_p:
            top_p = active_p[:3]
            lines.append("Top Pillars:")
            for p in top_p:
                lines.append(
                    f"• {p['label']}: {p['wins']}W-{p['losses']}L ({p['win_pct']:.1f}%, {p['flat_units']:+.2f}U, wt {p['weight']:.2f}x)"
                )

    return "\n".join(lines)


def send_weekly_digest(data_path="docs/data.json"):
    if not os.path.exists(data_path):
        print("Weekly Digest: docs/data.json not found.")
        return ""
    data = json.load(open(data_path, "r", encoding="utf-8"))
    msg = build_weekly_digest_message(data)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Weekly Digest dry-run (missing Telegram credentials):\n" + msg)
        return msg
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=12,
        )
        print(f"Weekly Digest sent to Telegram (HTTP {r.status_code}).")
    except Exception as exc:
        print(f"Weekly Digest Telegram failed: {exc}")
    return msg


if __name__ == "__main__":
    send_weekly_digest()
