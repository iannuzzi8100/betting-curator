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


def _read_intel_quant_lines(creds):
    try:
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().get(documentId=INTEL_DOC_ID).execute()
        chunks = []
        for el in doc.get("body", {}).get("content", []):
            for pe in el.get("paragraph", {}).get("elements", []):
                chunks.append(pe.get("textRun", {}).get("content", ""))
        full_txt = "".join(chunks)
        return [ln.strip() for ln in full_txt.splitlines() if "QUANT]" in ln or "PROP STEAM]" in ln]
    except Exception:
        return []


def fetch_dashboard_data():
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    now_est = datetime.datetime.now(est_tz)
    today_str = now_est.strftime("%Y-%m-%d")
    yesterday_str = (now_est - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

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

    yest_wins = 0
    yest_losses = 0
    yest_pl = 0.0
    yest_tickets = []

    pending_tickets = []
    pending_total = 0.0
    equity_curve = [{"label": "START", "bankroll": 100.0}]
    clv_values = []

    for r in all_rows[header_idx + 1 :]:
        if not r or not r[0].strip().startswith("BET-"):
            continue
        bet_id = r[0].strip()
        b_date = r[1].strip() if len(r) > 1 else ""
        sport = r[2].strip() if len(r) > 2 else ""
        b_type = r[3].strip() if len(r) > 3 else ""
        sel = r[5].strip() if len(r) > 5 else ""
        odds = r[6].strip() if len(r) > 6 else ""
        stake = _parse_num(r[8]) if len(r) > 8 else 0.0
        payout = _parse_num(r[9]) if len(r) > 9 else 0.0
        res = r[10].strip() if len(r) > 10 else ""
        net_pl = _parse_num(r[12]) if len(r) > 12 else 0.0
        run_br = _parse_num(r[13]) if len(r) > 13 else settled_bankroll
        notes = r[14].strip() if len(r) > 14 else ""

        clv_match = re.search(r"\[CLV:\s*([+-]?\d+(?:\.\d+)?)%", notes)
        clv_val = float(clv_match.group(1)) if clv_match else None
        if clv_val is not None:
            clv_values.append(clv_val)

        res_l = res.lower()
        if res_l == "won":
            overall_wins += 1
            total_staked_settled += stake
            total_net_pl += net_pl
            settled_bankroll = run_br
            equity_curve.append({"label": bet_id, "bankroll": round(run_br, 2)})
        elif res_l == "lost":
            overall_losses += 1
            total_staked_settled += stake
            total_net_pl += net_pl
            settled_bankroll = run_br
            equity_curve.append({"label": bet_id, "bankroll": round(run_br, 2)})
        elif res_l == "push":
            overall_pushes += 1
            equity_curve.append({"label": bet_id, "bankroll": round(run_br, 2)})
        elif res_l == "pending":
            pending_total += stake
            pending_tickets.append({
                "bet_id": bet_id,
                "date": b_date,
                "sport": sport,
                "bet_type": b_type,
                "selections": sel,
                "odds": odds,
                "stake": round(stake, 2),
                "payout": round(payout, 2),
                "clv": clv_val,
            })

        if b_date == yesterday_str and res_l in ("won", "lost", "push"):
            if res_l == "won":
                yest_wins += 1
            elif res_l == "lost":
                yest_losses += 1
            yest_pl += net_pl
            yest_tickets.append({
                "bet_id": bet_id,
                "sport": sport,
                "selections": sel,
                "odds": odds,
                "stake": round(stake, 2),
                "result": res,
                "net_pl": round(net_pl, 2),
            })

    overall_total = overall_wins + overall_losses
    overall_win_pct = round((overall_wins / overall_total) * 100.0, 1) if overall_total > 0 else 0.0
    overall_roi = round((total_net_pl / total_staked_settled) * 100.0, 1) if total_staked_settled > 0 else 0.0

    yest_total = yest_wins + yest_losses
    yest_win_pct = round((yest_wins / yest_total) * 100.0, 1) if yest_total > 0 else 0.0

    # Parse latest Curated Bets from Curator Feed
    feed_vals = feed_ws.get_all_values()
    latest_curated_text = ""
    latest_curated_date = today_str
    for r in reversed(feed_vals):
        if len(r) >= 12 and r[11].strip():
            latest_curated_date = r[0].strip() or today_str
            latest_curated_text = r[11].strip()
            break

    intel_quant_lines = _read_intel_quant_lines(creds)

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
                # Check if confidence % is already on the headline
                hm = re.search(r"(\d{2,3})\s*%", clean_title)
                init_cp = int(hm.group(1)) if hm and "BR" not in clean_title[hm.start() : hm.end() + 5] else None
                # Strip trailing "— 87% 💎" from headline since badge shows it on the right
                clean_title = re.sub(r"\s*[—-]\s*\[?\d{2,3}%\s*[^\]]*\]?$", "", clean_title).strip()
                cur_play = {
                    "title": clean_title,
                    "edge": "",
                    "game_line": "",
                    "confidence_math": "",
                    "confidence_pct": init_cp,
                    "confidence_tier": "",
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
            # Extract team keywords from title to match the scraped QUANT line
            t_clean = re.sub(r"\[[^\]]*\]", "", cp_item["title"])
            words = [w for w in re.findall(r"[A-Za-z]{3,}", t_clean) if w.upper() not in ("EST", "CFB", "NFL", "NBA", "NHL", "EPL", "CBB", "OVER", "UNDER", "RESULTING", "STRAIGHT")]
            for q_ln in intel_quant_lines:
                if any(w.lower() in q_ln.lower() for w in words[:2]):
                    # Extract the matchup + odds summary portion before tags
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

    return {
        "updated_at": now_est.strftime("%Y-%m-%d %I:%M %p EST"),
        "today": today_str,
        "yesterday": yesterday_str,
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
        "yesterday_stats": {
            "date": yesterday_str,
            "wins": yest_wins,
            "losses": yest_losses,
            "win_pct": yest_win_pct,
            "net_pl": round(yest_pl, 2),
            "tickets": yest_tickets,
        },
        "pending_tickets": pending_tickets,
        "curated_date": latest_curated_date,
        "curated_plays": curated_plays,
        "curated_raw": latest_curated_text,
        "equity_curve": equity_curve,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
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
      padding: 18px 14px 48px;
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
    .title-group h1 { font-size: 1.45rem; font-weight: 800; letter-spacing: -0.02em; }
    .title-group p { color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
    .refresh-btn {
      background: linear-gradient(135deg, var(--blue), var(--accent));
      color: #fff;
      border: none;
      border-radius: 10px;
      padding: 9px 16px;
      font-weight: 600;
      font-size: 0.85rem;
      cursor: pointer;
      transition: opacity 0.2s;
    }
    .refresh-btn:hover { opacity: 0.9; }
    .grid-3 {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
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
    }
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
    .chart-wrap { height: 210px; position: relative; margin-top: 6px; }
    .stack { display: flex; flex-direction: column; gap: 16px; }
    .empty-msg { color: var(--muted); font-size: 0.9rem; padding: 8px 0; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1>🎯 Betting Curator Terminal</h1>
        <p id="updated-at">Loading live ledger...</p>
      </div>
      <button class="refresh-btn" id="refresh-btn" onclick="loadData()">↻ Refresh Live</button>
    </header>

    <div class="grid-3">
      <div class="card">
        <div class="card-label">Overall Win / Loss Ratio</div>
        <div class="big-num" id="overall-ratio">--</div>
        <div class="sub-row" id="overall-sub"></div>
      </div>

      <div class="card">
        <div class="card-label" id="yest-label">Yesterday's Win / Loss Ratio</div>
        <div class="big-num" id="yest-ratio">--</div>
        <div class="sub-row" id="yest-sub"></div>
      </div>

      <div class="card">
        <div class="card-label">Live Bankroll &amp; Exposure</div>
        <div class="big-num" id="bankroll-val">--</div>
        <div class="sub-row" id="bankroll-sub"></div>
      </div>
    </div>

    <div class="stack">
      <div class="card">
        <div class="section-title">
          <span>🔥 Today's Curated Bets (<span id="curated-date">--</span>)</span>
          <span class="pill pill-blue" id="curated-count">0 Plays</span>
        </div>
        <div class="bet-list" id="curated-list"></div>
      </div>

      <div class="card">
        <div class="section-title">
          <span>⏳ Active Pending Tickets</span>
          <span class="pill pill-amber" id="pending-count">0 Active</span>
        </div>
        <div class="bet-list" id="pending-list"></div>
      </div>

      <div class="card">
        <div class="section-title">
          <span>📅 Yesterday's Graded Tickets</span>
          <span class="pill pill-blue" id="yest-count">0 Settled</span>
        </div>
        <div class="bet-list" id="yest-list"></div>
      </div>

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

  <script>
    const STATIC_DATA = __INITIAL_DATA_JSON__;
    let chartInstance = null;
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

    function fmtMoney(n) {
      const sign = n < 0 ? "-" : "";
      return sign + "$" + Math.abs(n).toFixed(2);
    }

    function render(data) {
      document.getElementById("updated-at").textContent = "Last synced: " + data.updated_at;

      // 1. Overall W/L
      const ov = data.overall;
      document.getElementById("overall-ratio").innerHTML =
        `${ov.wins}W - ${ov.losses}L <span style="font-size:1.1rem;color:var(--muted)">(${ov.win_pct}%)</span>`;
      const plCls = ov.net_pl >= 0 ? "pos" : "neg";
      const clvHtml = (ov.avg_clv !== null && ov.avg_clv !== undefined)
        ? `<span>Avg CLV: <span class="${ov.avg_clv >= 0 ? "pos" : "neg"}">${ov.avg_clv >= 0 ? "+" : ""}${ov.avg_clv}%</span></span>`
        : "";
      document.getElementById("overall-sub").innerHTML =
        `<span>Net P/L: <span class="${plCls}">${ov.net_pl >= 0 ? "+" : ""}${fmtMoney(ov.net_pl)}</span></span>` +
        `<span>ROI: <span class="${plCls}">${ov.roi >= 0 ? "+" : ""}${ov.roi}%</span></span>` +
        clvHtml;

      // 2. Yesterday W/L
      const ys = data.yesterday_stats;
      document.getElementById("yest-label").textContent = `Yesterday (${ys.date}) Win / Loss`;
      document.getElementById("yest-ratio").innerHTML =
        `${ys.wins}W - ${ys.losses}L <span style="font-size:1.1rem;color:var(--muted)">(${ys.win_pct}%)</span>`;
      const yCls = ys.net_pl >= 0 ? "pos" : "neg";
      document.getElementById("yest-sub").innerHTML =
        `<span>Daily P/L: <span class="${yCls}">${ys.net_pl >= 0 ? "+" : ""}${fmtMoney(ys.net_pl)}</span></span>`;

      // 3. Bankroll
      document.getElementById("bankroll-val").textContent = fmtMoney(data.bankroll);
      document.getElementById("bankroll-sub").innerHTML =
        `<span>Active Pending: <span class="warn">${fmtMoney(data.pending_total)}</span></span>` +
        `<span>Avail Cash: <span class="pos">${fmtMoney(data.available_cash)}</span></span>`;

      // 4. Today's Curated Bets (Streamlined Headline + Click-to-Expand Drawer)
      document.getElementById("curated-date").textContent = data.curated_date;
      const cList = document.getElementById("curated-list");
      const plays = data.curated_plays || [];
      document.getElementById("curated-count").textContent = `${plays.length} Play${plays.length === 1 ? "" : "s"}`;
      if (plays.length > 0) {
        cList.innerHTML = plays.map((p, idx) => {
          const cp = p.confidence_pct;
          let meterHtml = "";
          let pillCls = "pill-blue";
          let pillTxt = "CURATED";
          if (cp !== null && cp !== undefined) {
            const barColor = cp >= 88 ? "#10b981" : (cp >= 78 ? "#3b82f6" : (cp >= 68 ? "#f59e0b" : "#ef4444"));
            pillCls = cp >= 88 ? "pill-green" : (cp >= 78 ? "pill-blue" : (cp >= 68 ? "pill-amber" : "pill-red"));
            pillTxt = `${cp}%`;
            meterHtml = `
              <div style="margin-top:8px;">
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.75rem;font-weight:700;color:${barColor};margin-bottom:4px;">
                  <span>${p.confidence_tier || "Confidence Meter"}</span>
                  <span style="color:var(--muted);font-weight:500;" id="curated-caret-${idx}">${openDrawers.has(idx) ? "▴ Hide details" : "▾ Tap for details"}</span>
                </div>
                <div style="width:100%;height:6px;background:rgba(255,255,255,0.08);border-radius:999px;overflow:hidden;">
                  <div style="width:${Math.min(100, Math.max(0, cp))}%;height:100%;background:${barColor};border-radius:999px;"></div>
                </div>
              </div>
            `;
          }

          // Build color-coded Confidence Pillar Chips for the expandable drawer
          const rawMath = p.confidence_math || p.edge || "";
          const pillarItems = rawMath.split(",").map(s => s.trim()).filter(Boolean);
          const pillarChipsHtml = pillarItems.length > 0
            ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">
                <span class="pill pill-blue" style="font-size:0.74rem;">Base Score: 50%</span>
                ${pillarItems.map(item => {
                  const isNeg = item.includes("(-");
                  const isPos = item.includes("(+");
                  const cls = isNeg ? "pill-red" : (isPos ? "pill-green" : "pill-blue");
                  return `<span class="pill ${cls}" style="font-size:0.74rem;">${item}</span>`;
                }).join("")}
               </div>`
            : `<div class="bet-meta">Standard quantitative model evaluation.</div>`;

          const gameLineHtml = p.game_line
            ? p.game_line.split("|").map(seg => `<div style="margin-bottom:3px;">• ${seg.trim()}</div>`).join("")
            : `<div style="margin-bottom:3px;">• ${p.title}</div>`;

          return `
          <div class="bet-item" style="cursor:pointer;transition:background 0.15s;" onclick="toggleCuratedDetail(${idx})">
            <div class="bet-top">
              <div class="bet-title">${idx + 1}. ${p.title}</div>
              <span class="pill ${pillCls}">${pillTxt}</span>
            </div>
            ${meterHtml}
            <div id="curated-detail-${idx}" style="display:${openDrawers.has(idx) ? "block" : "none"};margin-top:12px;padding-top:12px;border-top:1px solid var(--card-border);font-size:0.83rem;">
              <div style="font-weight:700;color:#e2e8f0;margin-bottom:5px;">🏟️ Game &amp; Line Info</div>
              <div style="color:var(--muted);line-height:1.45;margin-bottom:10px;">
                ${gameLineHtml}
              </div>
              <div style="font-weight:700;color:#e2e8f0;margin-bottom:4px;">🧮 How Confidence (${cp !== null && cp !== undefined ? cp + "%" : "--"}) Was Chosen</div>
              ${pillarChipsHtml}
            </div>
          </div>
        `;
        }).join("");
      } else {
        cList.innerHTML = `<div class="empty-msg">${(data.curated_raw || "No curated plays found yet.").replace(/\\n/g, "<br>")}</div>`;
      }

      // 5. Active Pending Tickets (with live in-game ESPN score badges ONLY for games in play)
      const pList = document.getElementById("pending-list");
      const pend = data.pending_tickets || [];
      document.getElementById("pending-count").textContent = `${pend.length} Active (${fmtMoney(data.pending_total)})`;
      if (pend.length > 0) {
        pList.innerHTML = pend.map((t, idx) => {
          const clvBadge = (t.clv !== null && t.clv !== undefined)
            ? ` <span class="pill ${t.clv >= 0 ? "pill-green" : "pill-red"}">CLV ${t.clv >= 0 ? "+" : ""}${t.clv}%</span>`
            : "";
          return `
          <div class="bet-item">
            <div class="bet-top">
              <div class="bet-title">${t.bet_id} • ${t.selections}</div>
              <div><span class="pill pill-amber">${t.odds} | ${fmtMoney(t.stake)} &rarr; ${fmtMoney(t.payout)}</span>${clvBadge}</div>
            </div>
            <div class="bet-meta" style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
              <span>${t.sport} &bull; ${t.bet_type} &bull; Placed ${t.date}</span>
              <span id="live-score-${idx}" style="font-weight:700;color:#60a5fa;"></span>
            </div>
          </div>
        `;
        }).join("");
        fetchLiveScoresForPending(pend);
      } else {
        pList.innerHTML = `<div class="empty-msg">No active pending bets right now.</div>`;
      }

      // 6. Yesterday's Tickets
      const yList = document.getElementById("yest-list");
      const yTix = ys.tickets || [];
      document.getElementById("yest-count").textContent = `${yTix.length} Settled`;
      if (yTix.length > 0) {
        yList.innerHTML = yTix.map(t => {
          const isWin = t.result.toLowerCase() === "won";
          const pClass = isWin ? "pill-green" : "pill-red";
          return `
            <div class="bet-item">
              <div class="bet-top">
                <div class="bet-title">${t.bet_id} • ${t.selections} (${t.odds})</div>
                <span class="pill ${pClass}">${t.result.toUpperCase()} (${t.net_pl >= 0 ? "+" : ""}${fmtMoney(t.net_pl)})</span>
              </div>
              <div class="bet-meta">${t.sport} &bull; Stake ${fmtMoney(t.stake)}</div>
            </div>
          `;
        }).join("");
      } else {
        yList.innerHTML = `<div class="empty-msg">No settled bets recorded for ${ys.date}.</div>`;
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
      const ymd = ((STATIC_DATA && STATIC_DATA.today) || new Date().toISOString().slice(0,10)).replace(/-/g, "");
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
            (tm.display && selLower.includes(tm.display))
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
      if (window.location.protocol.startsWith("http") && !STATIC_DATA) {
        btn.textContent = "Refreshing...";
        try {
          const r = await fetch("/api/dashboard");
          const d = await r.json();
          render(d);
        } catch (e) {
          console.error(e);
        }
        btn.textContent = "↻ Refresh Live";
      } else if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
        btn.textContent = "Refreshing...";
        try {
          const r = await fetch("/api/dashboard");
          const d = await r.json();
          render(d);
        } catch (e) {
          if (STATIC_DATA) render(STATIC_DATA);
        }
        btn.textContent = "↻ Refresh Live";
      } else {
        btn.textContent = "Refreshing...";
        try {
          const r = await fetch(`./data.json?t=${Date.now()}`, { cache: "no-store" });
          if (r.ok) {
            const freshData = await r.json();
            render(freshData);
          } else if (STATIC_DATA) {
            render(STATIC_DATA);
          }
        } catch (e) {
          if (STATIC_DATA) render(STATIC_DATA);
        }
        const nowStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        const upEl = document.getElementById("updated-at");
        if (upEl && !upEl.textContent.includes("checked")) {
          upEl.textContent += ` (Live checked ${nowStr})`;
        }
        btn.textContent = "↻ Refresh Live";
      }
    }

    if (STATIC_DATA) {
      render(STATIC_DATA);
      loadData();
    } else {
      loadData();
    }
    // Auto-refresh live scores & latest data.json every 60 seconds on its own!
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
        else:
            html = HTML_TEMPLATE.replace("__INITIAL_DATA_JSON__", "null").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

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
