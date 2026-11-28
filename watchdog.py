import datetime
import json
import os
import re
import requests

import quant_signals


ESPN_SPORT_ROUTES = {
    "NFL": ("football", "nfl", None),
    "CFB": ("football", "college-football", ("80", "81")),
    "NBA": ("basketball", "nba", None),
    "NHL": ("hockey", "nhl", None),
    "NCAAM": ("basketball", "mens-college-basketball", "50"),
    "EPL": ("soccer", "eng.1", None),
    "UCL": ("soccer", "uefa.champions", None),
    "LALIGA": ("soccer", "esp.1", None),
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _american_to_implied(odds):
    if odds is None:
        return None
    try:
        o = int(float(odds))
    except Exception:
        return None
    if o < 0:
        return abs(o) / (abs(o) + 100.0)
    return 100.0 / (o + 100.0)


def _safe_int_odds(val):
    if val is None:
        return None
    s = str(val).strip().upper()
    if s == "EVEN":
        return 100
    try:
        return int(float(s.replace("+", "")))
    except Exception:
        return None


def _safe_float_line(val):
    if val is None:
        return None
    s = re.sub(r"^[ouOU]", "", str(val).strip())
    try:
        return float(s)
    except Exception:
        return None


def _norm_tokens(text):
    stop = {"the", "ml", "over", "under", "spread", "total", "1h", "vs", "at", "fc", "cf", "sc", "afc"}
    toks = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {t for t in toks if len(t) >= 2 and t not in stop}


def _match_play_to_event(sport_label, selection_text, game_line, events):
    """
    Matches a pending ticket or curated play to a live ESPN scoreboard event + identifies
    which side (`away` or `home`) is backed.
    Returns (event, comp, backed_comp, opp_comp, is_home) or None.
    """
    sel_l = str(selection_text or "").lower()
    gl_l = str(game_line or "").lower()
    sel_toks = _norm_tokens(selection_text)
    gl_toks = _norm_tokens(game_line)

    best_match = None
    best_score = 0

    for ev in (events or []):
        state = (ev.get("status") or {}).get("type", {}).get("state", "")
        if state != "pre":
            continue
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        h_team = home.get("team") or {}
        a_team = away.get("team") or {}
        h_names = [
            str(h_team.get("displayName", "")).lower(),
            str(h_team.get("shortDisplayName", "")).lower(),
            str(h_team.get("name", "")).lower(),
            str(h_team.get("abbreviation", "")).lower(),
        ]
        a_names = [
            str(a_team.get("displayName", "")).lower(),
            str(a_team.get("shortDisplayName", "")).lower(),
            str(a_team.get("name", "")).lower(),
            str(a_team.get("abbreviation", "")).lower(),
        ]

        def _side_score(names, target_str, target_toks):
            sc = 0
            for nm in names:
                if not nm:
                    continue
                if len(nm) >= 3 and re.search(rf"\b{re.escape(nm)}\b", target_str):
                    sc += 5 if len(nm) >= 5 else 3
                elif len(nm) == 2 and nm in target_toks:
                    sc += 2
            return sc

        h_sel = _side_score(h_names, sel_l, sel_toks)
        a_sel = _side_score(a_names, sel_l, sel_toks)
        h_gl = _side_score(h_names, gl_l, gl_toks)
        a_gl = _side_score(a_names, gl_l, gl_toks)

        if max(h_sel, a_sel) < 2:
            continue
        total_sc = max(h_sel, a_sel) * 2 + (h_gl + a_gl)
        if total_sc > best_score:
            best_score = total_sc
            if h_sel >= a_sel:
                best_match = (ev, comp, home, away, True)
            else:
                best_match = (ev, comp, away, home, False)

    return best_match


def evaluate_item_alerts(item, ev, comp, backed_comp, opp_comp, is_home, sum_data, now_utc=None):
    """
    Evaluates a single upcoming Pending Bet or Unplaced Curated Play for critical pre-tipoff red flags:
    1. Key Injury Auto-Alert (backed team QB Out/Doubtful or rotation cluster Out)
    2. NHL Goalie Switch Alert (backed starting goalie no longer listed as probable)
    3. Severe Line Drift / Reverse Steam Alert (for unplaced curated plays only)
    Returns list of dicts: [{"key": str, "severity": str, "message": str}].
    """
    alerts = []
    sport = str(item.get("sport") or "").upper()
    kind = item.get("kind", "curated")  # "pending" | "curated"
    title = item.get("label") or item.get("selection") or "Play"

    b_team = backed_comp.get("team") or {}
    o_team = opp_comp.get("team") or {}
    b_abbr = str(b_team.get("abbreviation") or b_team.get("displayName") or "")
    o_abbr = str(o_team.get("abbreviation") or o_team.get("displayName") or "")
    b_name = str(b_team.get("displayName") or b_abbr)
    o_name = str(o_team.get("displayName") or o_abbr)

    a_abbr, h_abbr = (o_abbr, b_abbr) if is_home else (b_abbr, o_abbr)
    a_name, h_name = (o_name, b_name) if is_home else (b_name, o_name)

    # 1. Per-Game Key Injury Alert on the Backed Side
    inj = quant_signals.extract_game_injury_impact(sum_data or {}, sport, a_abbr, h_abbr, a_name, h_name)
    backed_pen = int((inj.get("penalties") or {}).get(b_abbr, 0))
    if backed_pen <= -6:
        outs_list = (inj.get("outs") or {}).get(b_abbr) or []
        outs_str = ", ".join(outs_list[:3]) if outs_list else "key starters"
        alerts.append({
            "key": f"INJ:{sport}:{b_abbr}:{outs_str}",
            "severity": "HIGH",
            "message": f"🏥 INJURY ALERT ({kind.upper()}): {title} — {b_abbr} missing {outs_str} (Impact {backed_pen} pts)",
        })

    # 2. NHL Starting Goalie Switch Alert
    if sport == "NHL":
        gl = str(item.get("game_line") or "")
        # Look for "Goalie Mismatch: <Name> (" in curated play intel
        m_gm = re.search(r"Goalie Mismatch:\s*([A-Za-zÀ-ÿ' .-]+?)\s*\(", gl)
        if m_gm:
            expected_g = m_gm.group(1).strip()
            probs = backed_comp.get("probables") or []
            live_g = (probs[0].get("athlete") or {}).get("displayName", "") if probs else ""
            if live_g and quant_signals._norm_exact(live_g) != quant_signals._norm_exact(expected_g):
                alerts.append({
                    "key": f"GOALIE:{b_abbr}:{live_g}",
                    "severity": "HIGH",
                    "message": f"🥅 GOALIE SWITCH ({kind.upper()}): {title} — Expected {expected_g}, ESPN now lists {live_g} for {b_abbr}",
                })

    # 3. Severe Line Drift / Reverse Steam Alert (Unplaced Curated Plays only)
    if kind == "curated":
        odds_list = comp.get("odds") or []
        if odds_list:
            od = odds_list[0]
            sel_s = str(item.get("selection") or "")
            cur_odds_orig = _safe_int_odds(item.get("odds"))
            side_key = "home" if is_home else "away"

            # Moneyline drift check
            if re.search(r"\bML\b", sel_s, flags=re.I) and cur_odds_orig is not None:
                live_ml = _safe_int_odds(((od.get("moneyline") or {}).get(side_key) or {}).get("close", {}).get("odds"))
                if live_ml is not None:
                    imp_orig = _american_to_implied(cur_odds_orig)
                    imp_live = _american_to_implied(live_ml)
                    if imp_orig is not None and imp_live is not None:
                        imp_delta = (imp_live - imp_orig) * 100.0
                        # Worsened past -155 cap (steam chased past sweet spot)
                        if live_ml < -155 and cur_odds_orig >= -155 and imp_delta >= 4.0:
                            alerts.append({
                                "key": f"MLCAP:{sport}:{b_abbr}:{live_ml}",
                                "severity": "WARN",
                                "message": f"📈 LINE PAST CAP (UNPLACED): {title} — Curated {cur_odds_orig:+d} now {live_ml:+d} (past -155 cap; do not chase)",
                            })
                        # Sharp reverse steam against our backed side (market dropped >= 6.5% implied prob)
                        elif imp_delta <= -6.5:
                            alerts.append({
                                "key": f"REVML:{sport}:{b_abbr}:{live_ml}",
                                "severity": "HIGH",
                                "message": f"⚠️ REVERSE STEAM (UNPLACED): {title} — Curated {cur_odds_orig:+d} drifted to {live_ml:+d} ({imp_delta:+.1f}% implied prob drop)",
                            })

            # Point Spread drift check
            m_sp = re.search(r"([+-]\d+(?:\.\d+)?)", sel_s)
            if m_sp and not re.search(r"\b(?:ML|Over|Under)\b", sel_s, flags=re.I):
                orig_sp = _safe_float_line(m_sp.group(1))
                live_sp = _safe_float_line(((od.get("pointSpread") or {}).get(side_key) or {}).get("close", {}).get("line"))
                if orig_sp is not None and live_sp is not None:
                    # Worse line means live_sp is lower (more negative for fav, smaller + for dog)
                    worse_by = orig_sp - live_sp
                    reverse_by = live_sp - orig_sp
                    if worse_by >= 2.0:
                        alerts.append({
                            "key": f"SPCHASE:{sport}:{b_abbr}:{live_sp}",
                            "severity": "WARN",
                            "message": f"📈 SPREAD MOVED PAST NUMBER (UNPLACED): {title} — Curated {orig_sp:+.1f} now {live_sp:+.1f} ({worse_by:.1f} pts worse)",
                        })
                    elif reverse_by >= 2.0:
                        alerts.append({
                            "key": f"SPREV:{sport}:{b_abbr}:{live_sp}",
                            "severity": "HIGH",
                            "message": f"⚠️ REVERSE SPREAD STEAM (UNPLACED): {title} — Curated {orig_sp:+.1f} moved against you to {live_sp:+.1f} (+{reverse_by:.1f} pts)",
                        })

    return alerts


def _load_seen_keys(today_str, state_path=".watchdog_seen.json"):
    seen = set()
    # Try Google Sheet 'Watchdog Alerts' tab first for persistence across ephemeral GitHub Actions runners
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
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if gcp_sa:
            creds = Credentials.from_service_account_info(json.loads(gcp_sa), scopes=scopes)
        elif os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        else:
            creds = None
        if creds:
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(sheet_id)
            try:
                ws = sh.worksheet("Watchdog Alerts")
            except Exception:
                ws = sh.add_worksheet(title="Watchdog Alerts", rows=200, cols=4)
                ws.append_row(["Date", "AlertKey", "Severity", "Message"])
            for r in ws.get_all_values()[1:]:
                if len(r) >= 2 and str(r[0]).strip() == today_str:
                    seen.add(str(r[1]).strip())
            return seen, ws
    except Exception:
        pass

    try:
        if os.path.exists(state_path):
            d = json.load(open(state_path, "r", encoding="utf-8"))
            if d.get("date") == today_str:
                seen.update(d.get("keys") or [])
    except Exception:
        pass
    return seen, None


def _record_seen_alerts(today_str, new_alerts, ws=None, state_path=".watchdog_seen.json"):
    if not new_alerts:
        return
    if ws is not None:
        try:
            rows = [[today_str, a["key"], a["severity"], a["message"]] for a in new_alerts]
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            return
        except Exception:
            pass
    try:
        existing = []
        if os.path.exists(state_path):
            d = json.load(open(state_path, "r", encoding="utf-8"))
            if d.get("date") == today_str:
                existing = d.get("keys") or []
        merged = list(dict.fromkeys(existing + [a["key"] for a in new_alerts]))
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"date": today_str, "keys": merged}, f)
    except Exception:
        pass


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Watchdog Telegram dry-run (missing credentials):\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=12,
        )
        return r.status_code == 200
    except Exception as exc:
        print(f"Watchdog Telegram send failed: {exc}")
        return False


def run_watchdog(data_path="docs/data.json", hours_ahead=6.0):
    if not os.path.exists(data_path):
        print("Watchdog: docs/data.json not found; skipping.")
        return []

    data = json.load(open(data_path, "r", encoding="utf-8"))
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.strftime("%Y-%m-%d")
    today_ymd = now_est.strftime("%Y%m%d")

    watch_items = []
    for t in (data.get("pending_tickets") or []):
        sp = str(t.get("sport") or "").upper()
        if sp in ESPN_SPORT_ROUTES:
            watch_items.append({
                "kind": "pending",
                "sport": sp,
                "label": f"{t.get('bet_id', '')} {t.get('selections', '')}".strip(),
                "selection": t.get("selections") or "",
                "odds": t.get("odds"),
                "game_line": "",
            })

    if str(data.get("curated_date") or "").strip() == today_str:
        for p in (data.get("curated_plays") or []):
            if p.get("is_pass") or p.get("status") == "pending":
                continue
            sp = str(p.get("sport") or "").upper()
            if sp in ESPN_SPORT_ROUTES:
                watch_items.append({
                    "kind": "curated",
                    "sport": sp,
                    "label": p.get("selection") or p.get("title") or "",
                    "selection": p.get("selection") or p.get("title") or "",
                    "odds": p.get("odds"),
                    "game_line": p.get("game_line") or "",
                })

    if not watch_items:
        print("Watchdog: No active pending tickets or unplaced curated plays to monitor.")
        return []

    sports_needed = sorted({it["sport"] for it in watch_items})
    sb_by_sport = {}
    for sp in sports_needed:
        sport_path, league_path, groups = ESPN_SPORT_ROUTES[sp]
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/{league_path}/scoreboard"
        grp_list = groups if isinstance(groups, (list, tuple)) else [groups]
        ev_list = []
        seen_e = set()
        for grp in grp_list:
            params = {"dates": today_ymd, "limit": 200}
            if grp:
                params["groups"] = grp
            try:
                for ev_i in requests.get(url, params=params, timeout=12).json().get("events", []):
                    eid = str(ev_i.get("id", ""))
                    if eid and eid in seen_e:
                        continue
                    if eid:
                        seen_e.add(eid)
                    ev_list.append(ev_i)
            except Exception:
                continue
        sb_by_sport[sp] = ev_list

    matched = []
    ev_ids_by_sport = {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for it in watch_items:
        m = _match_play_to_event(it["sport"], it["selection"], it["game_line"], sb_by_sport.get(it["sport"], []))
        if not m:
            continue
        ev, comp, b_comp, o_comp, is_home = m
        raw_dt = ev.get("date", "")
        if raw_dt:
            try:
                dt_utc = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                hrs_until = (dt_utc - now_utc).total_seconds() / 3600.0
                if hrs_until < -0.1 or hrs_until > hours_ahead:
                    continue
            except Exception:
                pass
        eid = str(ev.get("id", ""))
        matched.append((it, ev, comp, b_comp, o_comp, is_home))
        ev_ids_by_sport.setdefault(it["sport"], []).append(eid)

    summaries_by_sport = {}
    for sp, eids in ev_ids_by_sport.items():
        sport_path, league_path, _ = ESPN_SPORT_ROUTES[sp]
        summaries_by_sport[sp] = quant_signals.prefetch_espn_summaries(sport_path, league_path, eids, max_workers=6)

    seen_keys, ws = _load_seen_keys(today_str)
    new_alerts = []
    for it, ev, comp, b_comp, o_comp, is_home in matched:
        eid = str(ev.get("id", ""))
        sum_data = (summaries_by_sport.get(it["sport"]) or {}).get(eid) or {}
        for al in evaluate_item_alerts(it, ev, comp, b_comp, o_comp, is_home, sum_data, now_utc=now_utc):
            if al["key"] not in seen_keys:
                seen_keys.add(al["key"])
                new_alerts.append(al)

    if new_alerts:
        header = f"🚨 Pre-Tipoff Watchdog Alert ({now_est.strftime('%I:%M %p EST').lstrip('0')})\n\n"
        body = "\n\n".join(a["message"] for a in new_alerts)
        send_telegram_message(header + body)
        _record_seen_alerts(today_str, new_alerts, ws=ws)
        print(f"Watchdog: Dispatched {len(new_alerts)} new pre-tipoff alert(s).")
    else:
        print(f"Watchdog: Checked {len(matched)} upcoming play(s) — all clear (0 new red flags).")
    return new_alerts


if __name__ == "__main__":
    run_watchdog()
