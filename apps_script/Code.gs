/**
 * Betting Curator — Instant Live Sheet API & Real-Time Telegram Webhook
 *
 * Deploy as a Google Apps Script Web App bound to your tracker spreadsheet:
 *   Extensions -> Apps Script -> paste this file -> Deploy -> New deployment -> Web app
 *   - Execute as: Me
 *   - Who has access: Anyone
 *
 * Configure Script Properties (Project Settings -> Script Properties):
 *   DASHBOARD_PIN      (required) Secret PIN you choose for your mobile dashboard & iPhone Shortcut
 *   TELEGRAM_BOT_TOKEN (optional) Existing Telegram bot token (for receipts + instant webhook)
 *   TELEGRAM_CHAT_ID   (optional) Your personal numeric Telegram chat ID
 *   GEMINI_API_KEY     (optional) Gemini API key (for instant screenshot & natural-language parsing)
 *   TG_WEBHOOK_SECRET  (optional) Random string appended to webhook URL (?tg_secret=...)
 *   WEB_APP_URL        (optional) Deployed /exec URL (used by setupTelegramWebhook())
 *   GITHUB_PAT         (optional) GitHub Personal Access Token (workflow scope) to auto-trigger daily_curator.yml after settlement
 *   GITHUB_REPO        (optional) Defaults to "iannuzzi8100/betting-curator"
 */

const TRACKER_SHEET_NAME = "Challenge Tracker ($100-$1k)";
const FEED_SHEET_NAME = "Curator Feed";
const TIMEZONE = "America/New_York";
const GEMINI_MODELS = [
  "gemini-3.5-flash-lite",
  "gemini-3.1-flash-lite",
  "gemini-3.6-flash",
  "gemini-3.7-flash",
  "gemini-3.8-flash",
  "gemini-3.5-flash",
  "gemini-2.5-flash",
];

// Non-challenge personal tickets to always ignore if they appear in screenshots
const IGNORED_TICKET_IDS = [
  "2313737142865625362", // Florida Gators 2026/2027 NCAAM National Championship (+600, $100) — separate from $100-$1k challenge
];

function isIgnoredNonChallengeTicket_(tId, sel, stake) {
  const idStr = String(tId || "").trim();
  if (idStr && IGNORED_TICKET_IDS.indexOf(idStr) !== -1) return true;
  const s = String(sel || "").toLowerCase();
  if (s.indexOf("florida gators") !== -1 && (s.indexOf("championship") !== -1 || s.indexOf("winner") !== -1 || s.indexOf("national") !== -1 || Math.abs(Number(stake || 0) - 100.0) < 0.01)) {
    return true;
  }
  return false;
}


// ============================================================================
// HTTP ENTRYPOINTS (CORS-safe: GET + POST with text/plain or application/json)
// ============================================================================

function doGet(e) {
  try {
    const params = (e && e.parameter) || {};
    if (!verifyDashboardPin_(params.pin)) {
      return jsonOut_({ ok: false, error: "Invalid or missing PIN." });
    }
    const action = (params.action || "dashboard").toLowerCase();
    if (action === "ping") {
      return jsonOut_({ ok: true, status: "connected", ts: new Date().toISOString() });
    }
    if (action === "settle" || action === "settle_refresh" || action === "settle_bets") {
      return jsonOut_(handleSettleBetsApi_(params));
    }
    const data = buildDashboardData_();
    return jsonOut_({ ok: true, dashboard: data });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

function doPost(e) {
  try {
    const raw = (e && e.postData && e.postData.contents) ? e.postData.contents : "{}";
    const body = JSON.parse(raw);
    const params = (e && e.parameter) || {};

    // 1. Instant Telegram Webhook update
    if (body && typeof body.update_id !== "undefined") {
      const expectedSecret = getProp_("TG_WEBHOOK_SECRET");
      if (expectedSecret && params.tg_secret !== expectedSecret) {
        return jsonOut_({ ok: false, error: "Unauthorized webhook secret." });
      }
      handleTelegramWebhook_(body);
      return jsonOut_({ ok: true });
    }

    // 2. Dashboard Web App / iPhone Shortcut API call
    const pin = body.pin || params.pin || "";
    if (!verifyDashboardPin_(pin)) {
      return jsonOut_({ ok: false, error: "Invalid or missing PIN." });
    }

    const action = String(body.action || "").toLowerCase();
    if (action === "dashboard") {
      return jsonOut_({ ok: true, dashboard: buildDashboardData_() });
    }
    if (action === "settle" || action === "settle_refresh" || action === "settle_bets") {
      return jsonOut_(handleSettleBetsApi_(body));
    }
    if (action === "log_bet") {
      return jsonOut_(handleLogBetApi_(body));
    }
    if (action === "cashout_bet") {
      return jsonOut_(handleCashoutBetApi_(body));
    }
    if (action === "edit_bet") {
      return jsonOut_(handleEditBetApi_(body));
    }
    if (action === "log_screenshot") {
      return jsonOut_(handleScreenshotApi_(body));
    }
    return jsonOut_({ ok: false, error: "Unknown action: " + action });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

// ============================================================================
// CORE DASHBOARD READ MODEL (Matches dashboard_app.py schema 1-for-1)
// ============================================================================

function buildDashboardData_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
  const feedWs = ss.getSheetByName(FEED_SHEET_NAME);

  const now = new Date();
  const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  const weekStart = new Date(now.getTime() - 6 * 24 * 60 * 60 * 1000);
  const todayStr = Utilities.formatDate(now, TIMEZONE, "yyyy-MM-dd");
  const yesterdayStr = Utilities.formatDate(yesterday, TIMEZONE, "yyyy-MM-dd");
  const weekStartStr = Utilities.formatDate(weekStart, TIMEZONE, "yyyy-MM-dd");
  const updatedAtStr = Utilities.formatDate(now, TIMEZONE, "yyyy-MM-dd hh:mm a") + " ET";

  const allRows = trackerWs.getDataRange().getDisplayValues();
  const headerIdx = findHeaderIdx_(allRows);

  let settledBankroll = (allRows.length > 4 && allRows[4].length > 0) ? parseNum_(allRows[4][0], 100.0) : 100.0;
  let overallWins = 0;
  let overallLosses = 0;
  let overallPushes = 0;
  let totalStakedSettled = 0.0;
  let totalNetPl = 0.0;

  let todayWins = 0;
  let todayLosses = 0;
  let todayPushes = 0;
  let todayPl = 0.0;
  const todayTickets = [];

  let yestWins = 0;
  let yestLosses = 0;
  let yestPushes = 0;
  let yestPl = 0.0;
  const yestTickets = [];

  let weekWins = 0;
  let weekLosses = 0;
  let weekPushes = 0;
  let weekPl = 0.0;
  const weekTickets = [];

  const settledTickets = [];
  const pendingTickets = [];
  let pendingTotal = 0.0;
  const equityCurve = [{ label: "START", bankroll: 100.0 }];
  const clvValues = [];

  const betEntries = [];
  for (let idx = headerIdx + 1; idx < allRows.length; idx++) {
    const r = allRows[idx];
    if (!r || !String(r[0] || "").trim().startsWith("BET-")) continue;
    const betId = String(r[0]).trim();
    betEntries.push({
      row_index: idx + 1,
      bet_num: parseBetNum_(betId),
      bet_id: betId,
      bDate: String(r[1] || "").trim(),
      sport: String(r[2] || "").trim(),
      bType: String(r[3] || "").trim(),
      sel: String(r[5] || "").trim(),
      odds: String(r[6] || "").trim(),
      stake: parseNum_(r[8], 0.0),
      payout: parseNum_(r[9], 0.0),
      res: String(r[10] || "").trim(),
      netPl: parseNum_(r[12], 0.0),
      runBrRaw: (r.length > 13 && String(r[13] || "").trim() !== "") ? parseNum_(r[13], 0.0) : 0.0,
      notes: String(r[14] || "").trim(),
    });
  }

  // Chronological order (BET-001 -> latest) for Equity Curve & Settled Bankroll regardless of sheet sort order
  const chronoEntries = betEntries.slice().sort((a, b) => a.bet_num - b.bet_num);
  for (let i = 0; i < chronoEntries.length; i++) {
    const e = chronoEntries[i];
    const clvMatch = e.notes.match(/\[CLV:\s*([+-]?\d+(?:\.\d+)?)%/);
    const clvVal = clvMatch ? parseFloat(clvMatch[1]) : null;
    if (clvVal !== null && !isNaN(clvVal)) {
      clvValues.push(clvVal);
    }

    const resL = e.res.toLowerCase();
    if (resL === "won") {
      overallWins++;
      totalStakedSettled += e.stake;
      totalNetPl += e.netPl;
      if (e.runBrRaw > 0) settledBankroll = e.runBrRaw;
      else settledBankroll = round2_(100.0 + totalNetPl);
      equityCurve.push({ label: e.bet_id, bankroll: round2_(settledBankroll) });
    } else if (resL === "lost") {
      overallLosses++;
      totalStakedSettled += e.stake;
      totalNetPl += e.netPl;
      if (e.runBrRaw > 0) settledBankroll = e.runBrRaw;
      else settledBankroll = round2_(100.0 + totalNetPl);
      equityCurve.push({ label: e.bet_id, bankroll: round2_(settledBankroll) });
    } else if (resL === "push") {
      overallPushes++;
      equityCurve.push({ label: e.bet_id, bankroll: round2_(settledBankroll) });
    }
  }

  // Newest-first order for Pending, Today, Yesterday, This Week, and full Settled history lists
  const newestEntries = betEntries.slice().sort((a, b) => b.bet_num - a.bet_num);
  for (let i = 0; i < newestEntries.length; i++) {
    const e = newestEntries[i];
    const clvMatch = e.notes.match(/\[CLV:\s*([+-]?\d+(?:\.\d+)?)%/);
    const clvVal = clvMatch ? parseFloat(clvMatch[1]) : null;
    const resL = e.res.toLowerCase();

    if (resL === "pending") {
      pendingTotal += e.stake;
      pendingTickets.push({
        row_index: e.row_index,
        bet_id: e.bet_id,
        date: e.bDate,
        sport: e.sport,
        bet_type: e.bType,
        selections: e.sel,
        odds: e.odds,
        stake: round2_(e.stake),
        payout: round2_(e.payout),
        clv: clvVal,
      });
    } else if (resL === "won" || resL === "lost" || resL === "push") {
      const tObj = {
        bet_id: e.bet_id,
        date: e.bDate,
        sport: e.sport,
        bet_type: e.bType,
        selections: e.sel,
        odds: e.odds,
        stake: round2_(e.stake),
        result: e.res,
        net_pl: round2_(e.netPl),
      };
      settledTickets.push(tObj);

      if (e.bDate >= weekStartStr && e.bDate <= todayStr) {
        if (resL === "won") weekWins++;
        else if (resL === "lost") weekLosses++;
        else if (resL === "push") weekPushes++;
        weekPl += e.netPl;
        weekTickets.push(tObj);
      }

      if (e.bDate === todayStr) {
        if (resL === "won") todayWins++;
        else if (resL === "lost") todayLosses++;
        else if (resL === "push") todayPushes++;
        todayPl += e.netPl;
        todayTickets.push(tObj);
      }

      if (e.bDate === yesterdayStr) {
        if (resL === "won") yestWins++;
        else if (resL === "lost") yestLosses++;
        else if (resL === "push") yestPushes++;
        yestPl += e.netPl;
        yestTickets.push(tObj);
      }
    }
  }

  const overallTotal = overallWins + overallLosses;
  const overallWinPct = overallTotal > 0 ? round1_((overallWins / overallTotal) * 100.0) : 0.0;
  const overallRoi = totalStakedSettled > 0 ? round1_((totalNetPl / totalStakedSettled) * 100.0) : 0.0;

  const weekTotal = weekWins + weekLosses;
  const weekWinPct = weekTotal > 0 ? round1_((weekWins / weekTotal) * 100.0) : 0.0;

  const todayTotal = todayWins + todayLosses;
  const todayWinPct = todayTotal > 0 ? round1_((todayWins / todayTotal) * 100.0) : 0.0;

  const yestTotal = yestWins + yestLosses;
  const yestWinPct = yestTotal > 0 ? round1_((yestWins / yestTotal) * 100.0) : 0.0;

  const curated = getLatestCuratedPlays_(feedWs, todayStr);
  adjustCuratedPlaysForBankroll_(
    curated,
    round2_(settledBankroll),
    round2_(pendingTotal),
    pendingTickets,
    todayTickets
  );

  return {
    live_mode: true,
    updated_at: updatedAtStr,
    today: todayStr,
    yesterday: yesterdayStr,
    week_start: weekStartStr,
    bankroll: round2_(settledBankroll),
    pending_total: round2_(pendingTotal),
    available_cash: round2_(Math.max(0.0, settledBankroll - pendingTotal)),
    overall: {
      wins: overallWins,
      losses: overallLosses,
      pushes: overallPushes,
      win_pct: overallWinPct,
      net_pl: round2_(totalNetPl),
      roi: overallRoi,
      avg_clv: clvValues.length > 0 ? round2_(clvValues.reduce((a, b) => a + b, 0) / clvValues.length) : null,
      clv_count: clvValues.length,
    },
    week_stats: {
      start_date: weekStartStr,
      end_date: todayStr,
      wins: weekWins,
      losses: weekLosses,
      pushes: weekPushes,
      win_pct: weekWinPct,
      net_pl: round2_(weekPl),
      tickets: weekTickets,
    },
    today_stats: {
      date: todayStr,
      wins: todayWins,
      losses: todayLosses,
      pushes: todayPushes,
      win_pct: todayWinPct,
      net_pl: round2_(todayPl),
      tickets: todayTickets,
    },
    yesterday_stats: {
      date: yesterdayStr,
      wins: yestWins,
      losses: yestLosses,
      pushes: yestPushes,
      win_pct: yestWinPct,
      net_pl: round2_(yestPl),
      tickets: yestTickets,
    },
    settled_tickets: settledTickets,
    pending_tickets: pendingTickets,
    curated_date: curated.date,
    curated_plays: curated.plays,
    curated_raw: curated.raw,
    equity_curve: equityCurve,
  };
}

function getLatestCuratedPlays_(feedWs, fallbackDate) {
  if (!feedWs) return { date: fallbackDate, plays: [], raw: "", rowIndex: null };
  const vals = feedWs.getDataRange().getDisplayValues();
  let latestText = "";
  let latestDate = fallbackDate;
  let latestRow = null;
  let fbText = "";
  let fbDate = fallbackDate;
  let fbRow = null;
  for (let i = vals.length - 1; i >= 0; i--) {
    const r = vals[i];
    if (r.length >= 12 && String(r[11] || "").trim()) {
      const txt = String(r[11]).trim();
      const dt = String(r[0] || "").trim() || fallbackDate;
      if (!fbText) {
        fbText = txt;
        fbDate = dt;
        fbRow = i + 1;
      }
      if (/^\s*1\.\s+/m.test(txt)) {
        latestDate = dt;
        latestText = txt;
        latestRow = i + 1;
        break;
      }
    }
  }
  if (!latestText) {
    latestText = fbText;
    latestDate = fbDate;
    latestRow = fbRow;
  }
  const plays = parseCuratedPlaysText_(latestText);
  return { date: latestDate, plays: plays, raw: latestText, rowIndex: latestRow };
}

function extractSelectionCore_(text) {
  let s = String(text || "").trim();
  s = s.replace(/^\d+\.\s*/, "").replace(/\*/g, "").trim();
  s = s.replace(/^\[[^\]]+\]\s*/, "").trim();
  const core = s.split(/(\s+[+-]\d|\s+ml\b|\s+over\b|\s+under\b|\s+to\s+win\b|\s+1h\b|\s+first\s+half\b|\(|—)/i)[0];
  return core.toLowerCase().replace(/[^a-z0-9\s]/g, "").trim();
}

function adjustCuratedPlaysForBankroll_(curated, settledBankroll, pendingTotal, pendingTickets, todayTickets) {
  if (!curated || !curated.plays || curated.plays.length === 0) return [];
  const plays = curated.plays;
  const raw = String(curated.raw || "");
  const brMatch = raw.match(/Bankroll:\s*\$(\d+(?:\.\d{1,2})?)/i);
  const feedBr = brMatch ? parseFloat(brMatch[1]) : settledBankroll;
  const availCash = round2_(Math.max(0.0, settledBankroll - pendingTotal));

  const winCounts = {};
  for (let i = 0; i < plays.length; i++) {
    const p = plays[i];
    const isPass = (p.title || "").indexOf("⛔") !== -1 || /\bPASS\b/.test(p.title || "");
    if (!isPass && p.game_time) {
      winCounts[p.game_time] = (winCounts[p.game_time] || 0) + 1;
    }
  }

  const adjustments = [];
  for (let i = 0; i < plays.length; i++) {
    const p = plays[i];
    const title = String(p.title || "");
    const isPass = title.indexOf("⛔") !== -1 || /\bPASS\b/.test(title);
    if (isPass) {
      p.status = "pass";
      continue;
    }

    const core = extractSelectionCore_(p.selection || title);
    const matchPend = (core && core.length >= 3)
      ? (pendingTickets || []).find(t => String(t.selections || "").toLowerCase().indexOf(core) !== -1)
      : null;
    const matchSettled = (!matchPend && core && core.length >= 3)
      ? (todayTickets || []).find(t => String(t.selections || "").toLowerCase().indexOf(core) !== -1)
      : null;

    let wagerPct = null;
    const pctMatch = title.match(/(\d{1,2}(?:\.\d+)?)\s*%\s*(?:of\s+(?:Resulting\s+)?)?(?:BR|Bankroll)/i);
    if (pctMatch) {
      wagerPct = parseFloat(pctMatch[1]);
    } else if (p.confidence_pct !== null && p.confidence_pct !== undefined) {
      const cp = Number(p.confidence_pct);
      const dual = (winCounts[p.game_time] || 1) >= 2;
      if (cp >= 88) wagerPct = dual ? 35 : 50;
      else if (cp >= 78) wagerPct = dual ? 25 : 35;
      else if (cp >= 68) wagerPct = dual ? 18 : 20;
    } else if (feedBr > 0 && p.stake > 0) {
      wagerPct = round1_((p.stake / feedBr) * 100.0);
    }
    if (wagerPct) p.wager_pct = wagerPct;

    if (matchPend) {
      p.status = "pending";
      p.pending_bet_id = matchPend.bet_id;
      p.stake = round2_(matchPend.stake);
      if (wagerPct && settledBankroll > 0) {
        p.recalculated_stake = round2_(settledBankroll >= 2.0 ? Math.max(2.0, settledBankroll * (wagerPct / 100.0)) : settledBankroll);
      }
      continue;
    }
    if (matchSettled) {
      p.status = "settled";
      p.settled_bet_id = matchSettled.bet_id;
      p.settled_result = matchSettled.result;
      p.settled_pl = round2_(matchSettled.net_pl);
      continue;
    }

    p.status = "remaining";
    if (!wagerPct || wagerPct <= 0 || settledBankroll <= 0) continue;

    const oldStake = round2_(p.stake || 0);
    const newStake = round2_(settledBankroll >= 2.0 ? Math.max(2.0, settledBankroll * (wagerPct / 100.0)) : settledBankroll);
    p.stake = newStake;
    if (oldStake > 0 && Math.abs(newStake - oldStake) >= 0.01) {
      p.original_stake = oldStake;
      p.stake_adjusted = true;
    } else {
      p.stake_adjusted = false;
    }

    // Replace the last $XX.XX in the play title with the recalculated wager amount
    p.title = title.replace(/\$(\d+(?:\.\d{1,2})?)(?!.*?\$\d)/, "$" + newStake.toFixed(2));
    adjustments.push({
      selection: p.selection || title.split("—")[0].trim(),
      wager_pct: wagerPct,
      old_stake: oldStake,
      new_stake: newStake,
      changed: Math.abs(newStake - oldStake) >= 0.01,
    });
  }

  if (raw) {
    let updatedRaw = raw.replace(
      /Bankroll:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Pending:\s*\$\d+(?:\.\d{1,2})?\s*\|\s*Available:\s*\$\d+(?:\.\d{1,2})?/i,
      "Bankroll: $" + settledBankroll.toFixed(2) + " | Pending: $" + pendingTotal.toFixed(2) + " | Available: $" + availCash.toFixed(2)
    );
    const rawLines = updatedRaw.split(/\r?\n/);
    let playIdx = 0;
    for (let l = 0; l < rawLines.length; l++) {
      if (/^\s*\d+\.\s+/.test(rawLines[l]) && playIdx < plays.length) {
        const pl = plays[playIdx++];
        if (pl.status === "remaining" && pl.stake > 0) {
          rawLines[l] = rawLines[l].replace(/\$(\d+(?:\.\d{1,2})?)(?!.*?\$\d)/, "$" + pl.stake.toFixed(2));
        }
      }
    }
    curated.raw = rawLines.join("\n");
  }
  curated.adjustments = adjustments;
  return adjustments;
}

function recalculateCuratorFeedInSheet_(ss, dash) {
  try {
    const feedWs = ss.getSheetByName(FEED_SHEET_NAME);
    if (!feedWs || !dash) return [];
    const curated = getLatestCuratedPlays_(feedWs, dash.today);
    if (!curated.rowIndex || !curated.raw) return [];
    const beforeRaw = curated.raw;
    const adjustments = adjustCuratedPlaysForBankroll_(
      curated,
      dash.bankroll,
      dash.pending_total,
      dash.pending_tickets,
      dash.today_stats ? dash.today_stats.tickets : []
    );
    if (curated.raw && curated.raw !== beforeRaw) {
      feedWs.getRange("L" + curated.rowIndex).setValue(curated.raw.slice(0, 25000));
      SpreadsheetApp.flush();
    }
    return adjustments;
  } catch (e) {
    return [];
  }
}

function parseCuratedPlaysText_(text) {
  if (!text) return [];
  const lines = text.split(/\r?\n/);
  const plays = [];
  let curPlay = null;

  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    const m = s.match(/^\d+\.\s*(.+)$/);
    if (m) {
      if (curPlay) plays.push(curPlay);
      let cleanTitle = m[1].replace(/\*/g, "").trim();
      const isPassRow = cleanTitle.includes("⛔") || /NO GOOD PLAY/i.test(cleanTitle) || /\bPASS\b/i.test(cleanTitle);
      const hm = cleanTitle.match(/[—-]\s*\[?(\d{2,3})%\s*([^\]]*)\]?$/);
      let initCp = null;
      if (hm && !isPassRow) {
        const trailingCtx = (hm[2] || "").toUpperCase();
        if (!trailingCtx.includes("BR") && !trailingCtx.includes("BANKROLL") && !trailingCtx.includes("RESULTING")) {
          initCp = parseInt(hm[1], 10);
        }
      }
      cleanTitle = cleanTitle.replace(/\s*[—-]\s*\[?\d{2,3}%\s*[^\]]*\]?$/, "").trim();
      cleanTitle = cleanTitle.replace(
        /^\[Window\s*\d+:[^\]]*?([A-Z]{2,5})\s*\((\d{1,2}:\d{2}\s*[AP]M\s*EST)\)\]\s*/,
        "[$2 • $1] "
      );
      cleanTitle = cleanTitle.replace(/\s*[—-]\s*Capped at Exposure Limit/i, "").trim();

      const parsed = parseCuratedTitleFields_(cleanTitle);
      curPlay = {
        title: cleanTitle,
        is_pass: isPassRow,
        edge: "",
        game_line: "",
        confidence_math: "",
        confidence_pct: isPassRow ? null : initCp,
        confidence_tier: isPassRow ? "⛔ PASS WINDOW" : "",
        sport: parsed.sport,
        selection: parsed.selection,
        odds: parsed.odds,
        stake: parsed.stake,
        game_time: parsed.game_time,
      };
    } else if (curPlay && (s.startsWith("- ") || s.startsWith("• "))) {
      const rawBullet = s.replace(/^[-•]\s*/, "").replace(/[*`]/g, "").trim();
      if (/^confidence math:/i.test(rawBullet)) {
        const cmBody = rawBullet.replace(/^confidence math:\s*/i, "").trim();
        curPlay.confidence_math = cmBody;
        const pm = cmBody.match(/=\s*(\d{2,3})\s*%/) || cmBody.match(/(\d{2,3})\s*%/);
        if (pm && !curPlay.is_pass) {
          curPlay.confidence_pct = parseInt(pm[1], 10);
        }
      } else if (/^game\s*(?:&|and)\s*line:/i.test(rawBullet)) {
        curPlay.game_line = rawBullet.replace(/^game\s*(?:&|and)\s*line:\s*/i, "").trim();
      } else if (/^confidence:/i.test(rawBullet)) {
        const confBody = rawBullet.replace(/^confidence:\s*/i, "").replace(/^[\s\[\]]+|[\s\[\]]+$/g, "");
        const pm = confBody.match(/(\d{2,3})\s*%/);
        if (pm && !curPlay.is_pass) curPlay.confidence_pct = parseInt(pm[1], 10);
        const parts = confBody.split("|").map(p => p.trim()).filter(Boolean);
        curPlay.confidence_tier = parts.length >= 3 ? parts[parts.length - 1] : confBody;
      } else {
        const edgeTxt = rawBullet.replace(/^edge:\s*/i, "").trim();
        curPlay.edge = (curPlay.edge + " " + edgeTxt).trim();
        if (!curPlay.confidence_math) curPlay.confidence_math = curPlay.edge;
      }
    }
  }
  if (curPlay) plays.push(curPlay);

  plays.forEach(p => {
    if (p.is_pass) {
      p.confidence_pct = null;
      p.confidence_tier = "⛔ PASS WINDOW";
      return;
    }
    if (p.confidence_pct !== null && p.confidence_pct !== undefined && !p.confidence_tier) {
      const sc = Number(p.confidence_pct);
      if (sc >= 88) p.confidence_tier = "🔥 TIER 1: MAX HAMMER";
      else if (sc >= 78) p.confidence_tier = "💎 TIER 2: STRONG CONVICTION";
      else if (sc >= 68) p.confidence_tier = "✅ TIER 3: SOLID EDGE";
      else p.confidence_tier = "⛔ TIER 4: PASS";
    }
  });

  return plays;
}

function parseCuratedTitleFields_(title) {
  let sport = "CFB";
  let gameTime = "";
  let rest = title;

  const bracket = title.match(/^\[([^\]]+)\]\s*(.*)$/);
  if (bracket) {
    const inside = bracket[1];
    rest = bracket[2].trim();
    const parts = inside.split(/[—–•-]/).map(s => s.trim()).filter(Boolean);
    if (parts.length >= 2) {
      gameTime = parts[0];
      sport = parts[parts.length - 1].toUpperCase();
    } else if (parts.length === 1) {
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

  let odds = -110;
  const oddsMatch = betSeg.match(/\(([+-]\d{3,4})\)/) || betSeg.match(/\s([+-]\d{3,4})(?:\s|$|—|–)/);
  if (oddsMatch) {
    odds = parseInt(oddsMatch[1], 10);
  }

  let stake = 0.0;
  const stakeMatch = betSeg.match(/\$(\d+(?:\.\d{1,2})?)/) || rest.match(/\$(\d+(?:\.\d{1,2})?)/);
  if (stakeMatch) {
    stake = parseFloat(stakeMatch[1]);
  }

  let pick = betSeg;
  if (oddsMatch) {
    pick = betSeg.slice(0, oddsMatch.index).trim();
  } else if (betSeg.indexOf("—") !== -1) {
    pick = betSeg.slice(0, betSeg.indexOf("—")).trim();
  }
  if (gameTime && pick && !pick.includes(gameTime)) {
    pick = pick + " (" + gameTime + ")";
  }

  return { sport: sport, selection: pick, odds: odds, stake: stake, game_time: gameTime };
}

// ============================================================================
// SHEET WRITE ACTIONS (With ScriptLock for Atomic BET-XXX Assignment)
// ============================================================================

function handleLogBetApi_(body) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
    const todayStr = Utilities.formatDate(new Date(), TIMEZONE, "yyyy-MM-dd");

    const ticket = {
      date: String(body.date || todayStr).trim(),
      sport: String(body.sport || "CFB").trim(),
      bet_type: String(body.bet_type || "Straight").trim(),
      selections: String(body.selections || "").trim(),
      odds: cleanInt_(body.odds, -110),
      stake: cleanFloat_(body.stake, 0.0),
      ticket_id: String(body.ticket_id || "").trim(),
      extra_note: String(body.notes || "Logged via Live Dashboard").trim(),
    };

    if (!ticket.selections || ticket.stake <= 0) {
      return { ok: false, error: "Selection and positive stake are required." };
    }

    const res = applyActionsToSheet_(trackerWs, {
      removals_or_cashouts: [],
      updates: [],
      tickets: [ticket],
    }, { skipSameDayDupCheck: Boolean(body.allow_duplicate) });

    SpreadsheetApp.flush();

    if (res.logged_ids.length === 0) {
      return {
        ok: false,
        error: "Identical pending bet is already logged today (duplicate skipped).",
        dashboard: buildDashboardData_(),
      };
    }

    if (res.notes.length > 0 && body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "📱 *Bet Logged via Live Dashboard:*");
    }

    return {
      ok: true,
      notes: res.notes,
      bet_id: res.logged_ids[0],
      dashboard: buildDashboardData_(),
    };
  } finally {
    lock.releaseLock();
  }
}

function handleCashoutBetApi_(body) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];

    const isFull = body.is_full_refund !== false;
    const cashoutRet = isFull ? null : cleanFloat_(body.cashout_return, 0.0);
    const res = applyActionsToSheet_(trackerWs, {
      removals_or_cashouts: [{
        row_index: body.row_index ? parseInt(body.row_index, 10) : null,
        bet_id: String(body.bet_id || "").trim(),
        is_full_refund: isFull,
        cashout_return: cashoutRet,
        reason: String(body.reason || (isFull ? "Full refund cashout / void" : "Partial cashout")).trim(),
      }],
      updates: [],
      tickets: [],
    });

    SpreadsheetApp.flush();
    let dash = buildDashboardData_();
    recalculateCuratorFeedInSheet_(ss, dash);
    dash = buildDashboardData_();

    if (res.notes.length > 0 && body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "🔄 *Bet Updated via Live Dashboard:*");
    }

    return { ok: true, notes: res.notes, dashboard: dash };
  } finally {
    lock.releaseLock();
  }
}

function handleEditBetApi_(body) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];

    const res = applyActionsToSheet_(trackerWs, {
      removals_or_cashouts: [],
      updates: [{
        row_index: body.row_index ? parseInt(body.row_index, 10) : null,
        bet_id: String(body.bet_id || "").trim(),
        sport: body.sport || null,
        bet_type: body.bet_type || null,
        selections: body.selections || null,
        odds: (body.odds !== undefined && body.odds !== null && body.odds !== "") ? body.odds : null,
        stake: (body.stake !== undefined && body.stake !== null && body.stake !== "") ? body.stake : null,
        result: body.result || null,
      }],
      tickets: [],
    });

    SpreadsheetApp.flush();
    let dash = buildDashboardData_();
    recalculateCuratorFeedInSheet_(ss, dash);
    dash = buildDashboardData_();

    if (res.notes.length > 0 && body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "✏️ *Bet Edited via Live Dashboard:*");
    }

    return { ok: true, notes: res.notes, dashboard: dash };
  } finally {
    lock.releaseLock();
  }
}

function handleScreenshotApi_(body) {
  const imageBase64 = String(body.image_base64 || "").replace(/^data:[^;]+;base64,/, "").trim();
  const mimeType = String(body.mime_type || "image/jpeg").trim();
  const caption = String(body.caption || "").trim();
  if (!imageBase64 && !caption) {
    return { ok: false, error: "Provide an image_base64 screenshot or caption text." };
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
    const state = getSheetState_(trackerWs);
    const parsed = parseWithGemini_(caption, imageBase64, mimeType, state.pending, state.recent);
    if (!parsed) {
      return { ok: false, error: "Gemini could not parse that screenshot. Check GEMINI_API_KEY or retry." };
    }
    const res = applyActionsToSheet_(trackerWs, parsed);
    SpreadsheetApp.flush();

    if (res.notes.length === 0) {
      return { ok: false, error: "No new bet tickets or edits detected (it may already be logged).", dashboard: buildDashboardData_() };
    }

    if (body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "📸 *Screenshot Synced Instantly:*");
    }
    return { ok: true, notes: res.notes, dashboard: buildDashboardData_() };
  } finally {
    lock.releaseLock();
  }
}

function buildRowPayload_(betId, curRow, dateStr, sport, betType, sel, odds, stake, tId, extraNote) {
  const prevRow = curRow - 1;
  const unitsStr = (stake / 4.0).toFixed(2) + "U";
  const tierLabel = betType === "Straight" ? "Core Tier 1" : "Flyer Tier 3";
  const oddsStr = odds >= 0 ? "+" + odds : String(odds);
  let noteStr = "Hard Rock Bet Ticket: " + sel + " (" + oddsStr + ") | Stake: $" + stake.toFixed(2);
  if (tId) noteStr += " (Hard Rock ID: " + tId + ")";
  if (extraNote) noteStr += " [" + extraNote + "]";

  return [
    betId,
    dateStr,
    sport,
    betType,
    tierLabel,
    sel,
    odds,
    unitsStr,
    stake,
    '=IF(OR(ISBLANK(I' + curRow + '), ISBLANK(G' + curRow + '), I' + curRow + '="", G' + curRow + '=""), "", IF(G' + curRow + '>0, ROUND(I' + curRow + '*(1+G' + curRow + '/100), 2), ROUND(I' + curRow + '*(1+100/ABS(G' + curRow + ')), 2)))',
    "Pending",
    '=IF(K' + curRow + '="Won", J' + curRow + ', IF(K' + curRow + '="Lost", 0, IF(K' + curRow + '="Push", I' + curRow + ', "")))',
    '=IF(OR(K' + curRow + '="", K' + curRow + '="Pending"), "", IF(K' + curRow + '="Lost", -I' + curRow + ', IF(ISNUMBER(L' + curRow + '), L' + curRow + '-I' + curRow + ', "")))',
    '=IF(OR(ISBLANK(I' + curRow + '), I' + curRow + '=""), "", ROUND(100 + SUM(M' + curRow + ':M), 2))',
    noteStr,
    "",
  ];
}

function applyActionsToSheet_(trackerWs, actions, opts) {
  opts = opts || {};
  const state = getSheetState_(trackerWs);
  const allVals = state.allVals;
  const headerIdx = state.headerIdx;
  const existingText = allVals.slice(headerIdx + 1).map(r => r.join(" | ")).join("\n");

  const removals = (actions && actions.removals_or_cashouts) || [];
  const updates = (actions && actions.updates) || [];
  const newTickets = (actions && actions.tickets) || [];

  const actionNotes = [];
  const loggedIds = [];
  const loggedTickets = [];
  const freedSlots = [];

  // 1. Removals / cashouts (resolve by bet_id first so shifted rows never miss)
  for (let i = 0; i < removals.length; i++) {
    const rem = removals[i];
    const bId = String(rem.bet_id || "").trim();
    let rIdx = bId ? findRowByBetId_(allVals, headerIdx, bId) : null;
    if (!rIdx && rem.row_index) {
      rIdx = parseInt(rem.row_index, 10);
    }
    const isFull = rem.is_full_refund !== false;
    const cRet = rem.cashout_return;
    const reason = String(rem.reason || "Cashed out").trim();

    if (!rIdx || rIdx <= headerIdx + 1) continue;

    if (isFull) {
      freedSlots.push({ rIdx: rIdx, bId: bId || String(allVals[rIdx - 1][0] || "").trim(), reason: reason });
    } else {
      const retNum = cleanFloat_(cRet, 0.0);
      const stakeCell = parseNum_(allVals[rIdx - 1][8], 0.0);
      const netPl = round2_(retNum - stakeCell);
      const statusLabel = netPl > 0 ? "Won" : "Lost";
      trackerWs.getRange("K" + rIdx).setValue(statusLabel);
      trackerWs.getRange("L" + rIdx).setValue(retNum);
      trackerWs.getRange("M" + rIdx).setValue(netPl);
      const oldNote = String(allVals[rIdx - 1][14] || "").trim();
      trackerWs.getRange("O" + rIdx).setValue((oldNote ? oldNote + " | " : "") + "[Cashout: $" + retNum.toFixed(2) + "]");
      actionNotes.push("🔄 *Partial Cashout on " + (bId || ("Row " + rIdx)) + "*: Returned $" + retNum.toFixed(2) + " (Net " + (netPl >= 0 ? "+" : "") + "$" + netPl.toFixed(2) + ")");
    }
  }

  // 2. In-place edits (resolve by bet_id first so shifted rows never miss)
  for (let i = 0; i < updates.length; i++) {
    const upd = updates[i];
    const bId = String(upd.bet_id || "").trim();
    let rIdx = bId ? findRowByBetId_(allVals, headerIdx, bId) : null;
    if (!rIdx && upd.row_index) {
      rIdx = parseInt(upd.row_index, 10);
    }
    if (!rIdx || rIdx <= headerIdx + 1) continue;

    const changed = [];
    if (upd.sport) {
      trackerWs.getRange("C" + rIdx).setValue(String(upd.sport).trim());
      changed.push("Sport=" + upd.sport);
    }
    if (upd.bet_type) {
      trackerWs.getRange("D" + rIdx).setValue(String(upd.bet_type).trim());
      changed.push("Type=" + upd.bet_type);
    }
    if (upd.selections) {
      trackerWs.getRange("F" + rIdx).setValue(String(upd.selections).trim());
      changed.push("Selection=" + upd.selections);
    }
    if (upd.odds !== null && upd.odds !== undefined) {
      const oVal = cleanInt_(upd.odds, -110);
      trackerWs.getRange("G" + rIdx).setValue(oVal);
      changed.push("Odds=" + (oVal >= 0 ? "+" + oVal : String(oVal)));
    }
    if (upd.stake !== null && upd.stake !== undefined) {
      const st = cleanFloat_(upd.stake, 0.0);
      trackerWs.getRange("H" + rIdx).setValue((st / 4.0).toFixed(2) + "U");
      trackerWs.getRange("I" + rIdx).setValue(st);
      changed.push("Stake=$" + st.toFixed(2));
    }
    if (upd.result) {
      trackerWs.getRange("K" + rIdx).setValue(String(upd.result).trim());
      changed.push("Result=" + upd.result);
    }
    if (changed.length > 0) {
      actionNotes.push("✏️ *Updated " + (bId || ("Row " + rIdx)) + "*: " + changed.join(", "));
    }
  }

  // 3. Deduplicate new tickets
  const todayStr = Utilities.formatDate(new Date(), TIMEZONE, "yyyy-MM-dd");
  const validNew = [];
  for (let i = 0; i < newTickets.length; i++) {
    const t = newTickets[i];
    const tId = String(t.ticket_id || "").trim();
    const sel = String(t.selections || "").trim();
    const stake = cleanFloat_(t.stake, 0.0);
    if (!sel || stake <= 0) continue;
    if (isIgnoredNonChallengeTicket_(tId, sel, stake)) continue;
    if (tId && existingText.indexOf(tId) !== -1) continue;

    if (!opts.skipSameDayDupCheck) {
      const normSel = sel.toLowerCase().replace(/\s+/g, " ");
      const isDupPending = state.pending.some(p =>
        p.date === (t.date || todayStr) &&
        p.selections.toLowerCase().replace(/\s+/g, " ") === normSel &&
        Math.abs(parseNum_(p.stake, 0) - stake) < 0.02
      );
      if (isDupPending) continue;
    }
    validNew.push(t);
  }

  // 4. Fill freed slots first
  while (freedSlots.length > 0 && validNew.length > 0) {
    const slot = freedSlots.shift();
    const t = validNew.shift();
    const stake = cleanFloat_(t.stake, 0.0);
    const odds = cleanInt_(t.odds, -110);
    const sport = String(t.sport || "CFB").trim();
    const betType = String(t.bet_type || "Straight").trim();
    const dateStr = String(t.date || todayStr).trim();
    const sel = String(t.selections || "").trim();
    const tId = String(t.ticket_id || "").trim();

    const payload = buildRowPayload_(slot.bId, slot.rIdx, dateStr, sport, betType, sel, odds, stake, tId, "Replaced cashed-out ticket: " + slot.reason);
    trackerWs.getRange("A" + slot.rIdx + ":P" + slot.rIdx).setValues([payload]);
    loggedIds.push(slot.bId);
    loggedTickets.push({ bet_id: slot.bId, date: dateStr, sport: sport, selections: sel });
    actionNotes.push("🔄 *Replaced " + slot.bId + "* -> *" + sel + " (" + (odds >= 0 ? "+" + odds : odds) + ")* — *$" + stake.toFixed(2) + "*");
  }

  // 5. Delete remaining freed slots bottom-to-top (SUM(M{r}:M) stays exact with zero blank rows)
  freedSlots.sort((a, b) => b.rIdx - a.rIdx);
  for (let i = 0; i < freedSlots.length; i++) {
    const slot = freedSlots[i];
    trackerWs.deleteRow(slot.rIdx);
    actionNotes.push("🗑️ *Removed " + slot.bId + "* (Row " + slot.rIdx + " — " + slot.reason + ")");
  }

  // 6. Insert remaining new tickets at the TOP of the tracker (newest-first, right under header)
  if (validNew.length > 0) {
    const fresh = getSheetState_(trackerWs);
    let maxBetNum = 0;
    for (let idx = fresh.headerIdx + 1; idx < fresh.allVals.length; idx++) {
      const r = fresh.allVals[idx];
      if (r && String(r[0] || "").trim().startsWith("BET-")) {
        const num = parseBetNum_(String(r[0]).trim());
        if (num > maxBetNum) maxBetNum = num;
      }
    }

    const insertRow = fresh.headerIdx + 2;
    for (let i = 0; i < validNew.length; i++) {
      const t = validNew[i];
      maxBetNum++;
      const betId = "BET-" + String(maxBetNum).padStart(3, "0");
      const stake = cleanFloat_(t.stake, 0.0);
      const odds = cleanInt_(t.odds, -110);
      const sport = String(t.sport || "CFB").trim();
      const betType = String(t.bet_type || "Straight").trim();
      const dateStr = String(t.date || todayStr).trim();
      const sel = String(t.selections || "").trim();
      const tId = String(t.ticket_id || "").trim();
      const extraNote = String(t.extra_note || "").trim();

      trackerWs.insertRowBefore(insertRow);
      if (trackerWs.getLastRow() >= insertRow + 1) {
        trackerWs
          .getRange(insertRow + 1, 1, 1, 16)
          .copyTo(trackerWs.getRange(insertRow, 1, 1, 16), SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
      }
      const payload = buildRowPayload_(betId, insertRow, dateStr, sport, betType, sel, odds, stake, tId, extraNote);
      trackerWs.getRange("A" + insertRow + ":P" + insertRow).setValues([payload]);
      loggedIds.push(betId);
      loggedTickets.push({ bet_id: betId, date: dateStr, sport: sport, selections: sel });
      actionNotes.push("✅ *Logged " + betId + "* ($" + stake.toFixed(2) + ") — " + sel + " (" + (odds >= 0 ? "+" + odds : odds) + ")");
    }
  }

  // 7. Auto-schedule finish-time settlement triggers for newly logged bets
  if (loggedTickets.length > 0) {
    try {
      scheduleFinishTriggers_(loggedTickets);
    } catch (e) {
      // Non-fatal: 15-min watchdog still catches it
    }
  }

  return { notes: actionNotes, logged_ids: loggedIds };
}

// ============================================================================
// REAL-TIME TELEGRAM WEBHOOK HANDLER (Inline Buttons + Screenshots + Text)
// ============================================================================

function handleTelegramWebhook_(update) {
  const botToken = getProp_("TELEGRAM_BOT_TOKEN");
  const allowedChatId = String(getProp_("TELEGRAM_CHAT_ID") || "").trim();
  if (!botToken) return;

  // Atomic deduplication of Telegram webhook retries (Apps Script redirects/latency can cause concurrent retries)
  const updId = Number(update.update_id || 0);
  if (updId > 0) {
    const dedupeLock = LockService.getScriptLock();
    dedupeLock.waitLock(8000);
    try {
      const cache = CacheService.getScriptCache();
      const cacheKey = "tg_upd_" + updId;
      if (cache.get(cacheKey)) return;
      const props = PropertiesService.getScriptProperties();
      const lastId = Number(props.getProperty("LAST_TG_UPDATE_ID") || 0);
      if (updId <= lastId) return;
      cache.put(cacheKey, "1", 21600);
      props.setProperty("LAST_TG_UPDATE_ID", String(updId));
    } finally {
      dedupeLock.releaseLock();
    }
  }

  // A. Inline keyboard button taps (1-tap "Log #1", "Status", etc.)
  if (update.callback_query) {
    const cq = update.callback_query;
    const chatId = String((cq.message && cq.message.chat && cq.message.chat.id) || "");
    if (allowedChatId && chatId !== allowedChatId) return;

    const data = String(cq.data || "");
    if (data === "status") {
      answerCallback_(botToken, cq.id, "Fetching live bankroll...");
      sendLiveStatusTelegram_(botToken, chatId);
      return;
    }

    if (data.startsWith("log_curated:")) {
      const idx = parseInt(data.split(":")[1], 10);
      const lock = LockService.getScriptLock();
      lock.waitLock(15000);
      try {
        const ss = SpreadsheetApp.getActiveSpreadsheet();
        const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
        const feedWs = ss.getSheetByName(FEED_SHEET_NAME);
        const todayStr = Utilities.formatDate(new Date(), TIMEZONE, "yyyy-MM-dd");
        const curated = getLatestCuratedPlays_(feedWs, todayStr);
        const play = curated.plays[idx];
        if (!play) {
          answerCallback_(botToken, cq.id, "Play #" + (idx + 1) + " not found in latest slate.");
          return;
        }
        const stake = play.stake > 0 ? play.stake : 5.0;
        const res = applyActionsToSheet_(trackerWs, {
          removals_or_cashouts: [],
          updates: [],
          tickets: [{
            date: todayStr,
            sport: play.sport || "CFB",
            bet_type: "Straight",
            selections: play.selection || play.title,
            odds: play.odds || -110,
            stake: stake,
            extra_note: "1-Tap Telegram Curated Play #" + (idx + 1),
          }],
        });
        SpreadsheetApp.flush();

        if (res.notes.length > 0) {
          answerCallback_(botToken, cq.id, "✅ Logged " + (res.logged_ids[0] || "bet") + "!");
          sendTelegramReceipt_(trackerWs, res.notes, "⚡ *1-Tap Curated Bet Logged:*");
        } else {
          answerCallback_(botToken, cq.id, "Already logged today!");
        }
      } finally {
        lock.releaseLock();
      }
      return;
    }
    answerCallback_(botToken, cq.id, "Unknown action.");
    return;
  }

  // B. Standard messages (photos, uncompressed image documents, text)
  const msg = update.message || update.edited_message;
  if (!msg) return;
  const chatId = String((msg.chat && msg.chat.id) || "");
  if (allowedChatId && chatId !== allowedChatId) return;

  const text = String(msg.text || msg.caption || "").trim();
  let fileId = null;
  let mimeType = "image/jpeg";

  if (msg.photo && msg.photo.length > 0) {
    fileId = msg.photo[msg.photo.length - 1].file_id;
  } else if (msg.document && msg.document.mime_type && String(msg.document.mime_type).startsWith("image/")) {
    fileId = msg.document.file_id;
    mimeType = msg.document.mime_type;
  }

  if (!fileId && /^(?:\/start|hi|hello)$/i.test(text)) {
    sendTelegramMessage_(botToken, chatId, "👋 Instant Betting Tracker is live! Send a screenshot, type a bet/cashout, tap a curated play button, or type /status.");
    return;
  }

  if (!fileId && /^(?:\/?status|\/?bankroll)$/i.test(text)) {
    sendLiveStatusTelegram_(botToken, chatId);
    return;
  }

  if (!fileId && !text) return;

  let imgBase64 = null;
  if (fileId) {
    imgBase64 = downloadTelegramFileBase64_(botToken, fileId);
    if (!imgBase64) {
      sendTelegramMessage_(botToken, chatId, "⚠️ Couldn't download that image from Telegram. Please resend or log it in the Live Dashboard.");
      return;
    }
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const trackerWs = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
    const state = getSheetState_(trackerWs);
    const parsed = parseWithGemini_(text, imgBase64, mimeType, state.pending, state.recent);
    if (!parsed) {
      sendTelegramMessage_(botToken, chatId, "⚠️ Couldn't parse that message right now (Gemini API error). Try again or use 1-tap log in the dashboard.");
      return;
    }
    const res = applyActionsToSheet_(trackerWs, parsed);
    SpreadsheetApp.flush();

    if (res.notes.length > 0) {
      sendTelegramReceipt_(trackerWs, res.notes, "⚡ *Instant Sheet Sync (Telegram):*");
    } else if (fileId) {
      sendTelegramMessage_(botToken, chatId, "ℹ️ No new ticket found in that screenshot (it may already be logged). Send /status to verify.");
    }
  } finally {
    lock.releaseLock();
  }
}

// ============================================================================
// GEMINI VISION / NLP PARSER
// ============================================================================

function parseWithGemini_(textMsg, imageBase64, mimeType, pendingRows, recentRows) {
  const apiKey = getProp_("GEMINI_API_KEY");
  if (!apiKey) return null;

  const todayStr = Utilities.formatDate(new Date(), TIMEZONE, "yyyy-MM-dd");
  const prompt = [
    "You are a deterministic sportsbook ledger controller for a Hard Rock Bet tracker.",
    "Interpret the user's input (text, caption, and/or screenshot) and determine all spreadsheet actions needed:",
    "1. New tickets placed (from screenshots or text like \"put $10 on Ducks ML +110\").",
    "2. Full-refund cashouts or removals of existing pending bets.",
    "3. Partial cashouts or edits to existing bets.",
    "",
    "Today's Date: " + todayStr,
    "CURRENT ACTIVE PENDING BETS ON SPREADSHEET:",
    JSON.stringify(pendingRows || [], null, 2),
    "RECENT BETS ON SPREADSHEET:",
    JSON.stringify(recentRows || [], null, 2),
    "",
    "RULES:",
    "1. For \"sport\": use one of [\"NBA\", \"NHL\", \"NCAAM\", \"CFB\", \"NFL\", \"EPL\", \"UCL\", \"ATP\", \"WTA\", \"MLB\", \"Multi-Sport\"].",
    "2. For \"bet_type\": use one of [\"Straight\", \"SGP\", \"Parlay\"].",
    "3. For \"selections\": format cleanly as \"<Pick> (<Matchup> • <Game Time EST>)\" if game time is visible, otherwise \"<Pick> (<Matchup>)\".",
    "4. Full-refund cashout / void -> entry in \"removals_or_cashouts\" with \"is_full_refund\": true.",
    "5. Partial cashout -> \"is_full_refund\": false and \"cashout_return\": <dollar amount>.",
    "6. Edit existing row -> entry in \"updates\".",
    "7. ALWAYS IGNORE non-challenge personal futures bets — specifically Hard Rock ID 2313737142865625362 (Florida Gators NCAAM National Championship 2026/2027 +600 for $100). Never include it in \"tickets\".",
    "",
    "Respond ONLY with valid JSON: {\"removals_or_cashouts\": [...], \"updates\": [...], \"tickets\": [{\"date\": \"" + todayStr + "\", \"sport\": \"CFB\", \"bet_type\": \"Straight\", \"selections\": \"...\", \"odds\": -110, \"stake\": 10.00, \"ticket_id\": \"\"}]}",
  ].join("\n");

  const parts = [{ text: prompt }];
  if (textMsg) {
    parts.push({ text: "User text/caption: " + textMsg });
  }
  if (imageBase64) {
    parts.push({ inline_data: { mime_type: mimeType || "image/jpeg", data: imageBase64 } });
  }

  for (let i = 0; i < GEMINI_MODELS.length; i++) {
    const model = GEMINI_MODELS[i];
    try {
      const url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent?key=" + encodeURIComponent(apiKey);
      const resp = UrlFetchApp.fetch(url, {
        method: "post",
        contentType: "application/json",
        muteHttpExceptions: true,
        payload: JSON.stringify({
          contents: [{ parts: parts }],
          generationConfig: { responseMimeType: "application/json", temperature: 0.0 },
        }),
      });
      if (resp.getResponseCode() !== 200) continue;
      const json = JSON.parse(resp.getContentText());
      const outText = json && json.candidates && json.candidates[0] && json.candidates[0].content && json.candidates[0].content.parts && json.candidates[0].content.parts[0] && json.candidates[0].content.parts[0].text;
      if (outText) {
        return JSON.parse(outText);
      }
    } catch (e) {
      // Try next fallback model
    }
  }
  return null;
}

// ============================================================================
// TELEGRAM HELPERS & 1-CLICK WEBHOOK REGISTRATION
// ============================================================================

function setupTelegramWebhook() {
  const botToken = getProp_("TELEGRAM_BOT_TOKEN");
  const webAppUrl = getProp_("WEB_APP_URL");
  if (!botToken || !webAppUrl) {
    throw new Error("Set TELEGRAM_BOT_TOKEN and WEB_APP_URL in Script Properties first.");
  }
  let secret = getProp_("TG_WEBHOOK_SECRET");
  if (!secret) {
    secret = Utilities.getUuid().replace(/-/g, "").slice(0, 20);
    PropertiesService.getScriptProperties().setProperty("TG_WEBHOOK_SECRET", secret);
  }
  const hookUrl = webAppUrl + (webAppUrl.indexOf("?") === -1 ? "?" : "&") + "tg_secret=" + encodeURIComponent(secret);
  PropertiesService.getScriptProperties().deleteProperty("LAST_TG_UPDATE_ID");
  const resp = UrlFetchApp.fetch("https://api.telegram.org/bot" + botToken + "/setWebhook", {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({
      url: hookUrl,
      allowed_updates: ["message", "edited_message", "callback_query"],
      drop_pending_updates: true,
    }),
  });
  Logger.log("setWebhook response: " + resp.getContentText());
}

function removeTelegramWebhook() {
  const botToken = getProp_("TELEGRAM_BOT_TOKEN");
  if (!botToken) throw new Error("Set TELEGRAM_BOT_TOKEN in Script Properties first.");
  PropertiesService.getScriptProperties().deleteProperty("LAST_TG_UPDATE_ID");
  const resp = UrlFetchApp.fetch("https://api.telegram.org/bot" + botToken + "/deleteWebhook?drop_pending_updates=true");
  Logger.log("deleteWebhook response: " + resp.getContentText());
}

function testDashboardData() {
  const data = buildDashboardData_();
  Logger.log(JSON.stringify({
    updated_at: data.updated_at,
    bankroll: data.bankroll,
    today_wl: data.today_stats.wins + "W-" + data.today_stats.losses + "L",
    pending_count: data.pending_tickets.length,
    curated_count: data.curated_plays.length,
  }, null, 2));
}

/**
 * 1-Click Safe Migration: Reorders all BET-XXX rows to NEWEST-FIRST (highest BET-XXX at top, BET-001 at bottom),
 * updates J/L/M/N formulas for newest-first order (preserving any manual partial-cashout numbers),
 * anchors Rows 1-15 summary formulas to the header row so inserting new rows at top never shifts ranges,
 * and verifies Settled Bankroll matches before & after.
 */
function reverseSheetToNewestFirst() {
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const ws = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
    const beforeData = buildDashboardData_();
    const beforeBankroll = beforeData.bankroll;

    const range = ws.getDataRange();
    const displayVals = range.getDisplayValues();
    const rawVals = range.getValues();
    const formulas = range.getFormulas();
    const headerIdx = findHeaderIdx_(displayVals);
    const headerRow = headerIdx + 1;
    const firstDataRow = headerIdx + 2;

    const betRows = [];
    for (let i = headerIdx + 1; i < displayVals.length; i++) {
      const betId = String(displayVals[i][0] || "").trim();
      if (!betId.startsWith("BET-")) continue;
      betRows.push({
        betNum: parseBetNum_(betId),
        raw: rawVals[i].slice(0, 16),
        formula: formulas[i].slice(0, 16),
      });
    }

    if (betRows.length === 0) {
      Logger.log("No BET-XXX rows found.");
      return;
    }

    // Sort DESCENDING (newest BET-XXX at row firstDataRow, BET-001 at bottom)
    betRows.sort((a, b) => b.betNum - a.betNum);

    const outRows = [];
    for (let idx = 0; idx < betRows.length; idx++) {
      const r = firstDataRow + idx;
      const item = betRows[idx];
      const rowOut = [];
      for (let c = 0; c < 16; c++) {
        rowOut.push(item.raw[c] !== undefined ? item.raw[c] : "");
      }
      // Col J (idx 9): Potential Payout formula
      rowOut[9] = '=IF(OR(ISBLANK(I' + r + '), ISBLANK(G' + r + '), I' + r + '="", G' + r + '=""), "", IF(G' + r + '>0, ROUND(I' + r + '*(1+G' + r + '/100), 2), ROUND(I' + r + '*(1+100/ABS(G' + r + ')), 2)))';
      // Col L (idx 11): Preserve hardcoded partial cashout return if non-formula number; otherwise standard formula
      const hadFormulaL = Boolean(item.formula[11]);
      if (hadFormulaL || item.raw[11] === "" || item.raw[11] === null) {
        rowOut[11] = '=IF(K' + r + '="Won", J' + r + ', IF(K' + r + '="Lost", 0, IF(K' + r + '="Push", I' + r + ', "")))';
      }
      // Col M (idx 12): Preserve hardcoded partial cashout net P/L if non-formula number; otherwise standard formula
      const hadFormulaM = Boolean(item.formula[12]);
      if (hadFormulaM || item.raw[12] === "" || item.raw[12] === null) {
        rowOut[12] = '=IF(OR(K' + r + '="", K' + r + '="Pending"), "", IF(K' + r + '="Lost", -I' + r + ', IF(ISNUMBER(L' + r + '), L' + r + '-I' + r + ', "")))';
      }
      // Col N (idx 13): Order-proof newest-first Running Bankroll formula (sums from this row down to BET-001)
      rowOut[13] = '=IF(OR(ISBLANK(I' + r + '), I' + r + '=""), "", ROUND(100 + SUM(M' + r + ':M), 2))';
      outRows.push(rowOut);
    }

    // Relax any strict Data Validation rules on data rows (and expand Col C Sport dropdown to include NCAAM/EPL/UCL/ATP/WTA)
    const maxDataRows = Math.max(outRows.length, ws.getMaxRows() - firstDataRow + 1);
    ws.getRange(firstDataRow, 1, maxDataRows, 16).clearDataValidations();
    const sportRule = SpreadsheetApp.newDataValidation()
      .requireValueInList(["CFB", "NFL", "NBA", "CBB", "NCAAM", "MLB", "NHL", "Soccer", "EPL", "UCL", "ATP", "WTA", "Multi-Sport", "Other"], true)
      .setAllowInvalid(true)
      .build();
    ws.getRange(firstDataRow, 3, maxDataRows, 1).setDataValidation(sportRule);
    const resultRule = SpreadsheetApp.newDataValidation()
      .requireValueInList(["Pending", "Won", "Lost", "Push"], true)
      .setAllowInvalid(true)
      .build();
    ws.getRange(firstDataRow, 11, maxDataRows, 1).setDataValidation(resultRule);

    // Write sorted newest-first rows starting right under the header
    ws.getRange(firstDataRow, 1, outRows.length, 16).setValues(outRows);

    // Clear any leftover trailing rows below the compacted table
    const lastExistingRow = ws.getLastRow();
    const newLastRow = firstDataRow + outRows.length - 1;
    if (lastExistingRow > newLastRow) {
      ws.getRange(newLastRow + 1, 1, lastExistingRow - newLastRow, 16).clearContent();
    }

    // Anchor summary formulas in Rows 1..(headerRow-1) from firstDataRow -> headerRow so inserting new rows at top never shifts them out
    const topRange = ws.getRange(1, 1, headerIdx, Math.min(16, ws.getLastColumn()));
    const topFormulas = topRange.getFormulas();
    const rangeRe = new RegExp("(\\$?[A-P])\\$?" + firstDataRow + ":(\\$?[A-P])(?![0-9])", "g");
    const fixedRangeRe = new RegExp("(\\$?[A-P])\\$?" + firstDataRow + ":(\\$?[A-P])\\$?(\\d+)", "g");
    for (let r = 0; r < topFormulas.length; r++) {
      for (let c = 0; c < topFormulas[r].length; c++) {
        const f = topFormulas[r][c];
        if (!f) continue;
        // Fix A5 (Settled Bankroll) if it looked up the bottom of Col N
        if (r === 4 && c === 0 && (/LOOKUP|INDEX|OFFSET/i.test(f) || /\bN\d/i.test(f))) {
          ws.getRange(5, 1).setFormula("=ROUND(100 + SUM(M" + headerRow + ":M), 2)");
          Logger.log("Updated A5 formula -> =ROUND(100 + SUM(M" + headerRow + ":M), 2)");
          continue;
        }
        // Anchor SUM/SUMIF/SUMIFS/COUNTIF/COUNTIFS ranges to headerRow (ignores text headers safely)
        if (/(?:SUM|COUNTIF)/i.test(f) && !/COUNTA\(/i.test(f)) {
          let updated = f.replace(rangeRe, "$1" + headerRow + ":$2");
          updated = updated.replace(fixedRangeRe, "$1" + headerRow + ":$2$3");
          if (updated !== f) {
            ws.getRange(r + 1, c + 1).setFormula(updated);
            Logger.log("Anchored summary cell R" + (r + 1) + "C" + (c + 1) + ": " + f + " -> " + updated);
          }
        }
      }
    }

    SpreadsheetApp.flush();
    const afterData = buildDashboardData_();
    Logger.log(
      "Migration complete! Reversed " + outRows.length + " bets (Top: " + outRows[0][0] + ", Bottom: " + outRows[outRows.length - 1][0] + "). " +
      "Settled Bankroll Before: $" + beforeBankroll.toFixed(2) + " | After: $" + afterData.bankroll.toFixed(2)
    );
  } finally {
    lock.releaseLock();
  }
}

function downloadTelegramFileBase64_(botToken, fileId) {
  try {
    const r1 = UrlFetchApp.fetch("https://api.telegram.org/bot" + botToken + "/getFile?file_id=" + encodeURIComponent(fileId), { muteHttpExceptions: true });
    if (r1.getResponseCode() !== 200) return null;
    const j1 = JSON.parse(r1.getContentText());
    const filePath = j1 && j1.result && j1.result.file_path;
    if (!filePath) return null;
    const r2 = UrlFetchApp.fetch("https://api.telegram.org/file/bot" + botToken + "/" + filePath, { muteHttpExceptions: true });
    if (r2.getResponseCode() !== 200) return null;
    return Utilities.base64Encode(r2.getContent());
  } catch (e) {
    return null;
  }
}

function sendLiveStatusTelegram_(botToken, chatId) {
  const d = buildDashboardData_();
  const lines = d.pending_tickets.map(p => "• *" + p.bet_id + "* ($" + p.stake.toFixed(2) + ") — " + p.selections + " (" + p.odds + ")");
  const msg = [
    "📊 *Live Challenge Tracker Status*",
    "Settled Bankroll: *$" + d.bankroll.toFixed(2) + "* | Pending: *$" + d.pending_total.toFixed(2) + "* | Avail: *$" + d.available_cash.toFixed(2) + "*",
    "",
    "*Active Pending (" + d.pending_tickets.length + "):*",
    lines.length ? lines.join("\n") : "• None",
  ].join("\n");
  sendTelegramMessage_(botToken, chatId, msg);
}

function sendTelegramReceipt_(trackerWs, notes, header) {
  const botToken = getProp_("TELEGRAM_BOT_TOKEN");
  const chatId = getProp_("TELEGRAM_CHAT_ID");
  if (!botToken || !chatId) return;
  const d = buildDashboardData_();
  const msg = [
    header || "📋 *Spreadsheet Updated:*",
    notes.join("\n"),
    "",
    "💰 *Bankroll:* $" + d.bankroll.toFixed(2) + " | *Pending:* $" + d.pending_total.toFixed(2) + " | *Avail:* $" + d.available_cash.toFixed(2) + " | *Active:* " + d.pending_tickets.length,
  ].join("\n");
  sendTelegramMessage_(botToken, chatId, msg);
}

function sendTelegramMessage_(botToken, chatId, text) {
  try {
    UrlFetchApp.fetch("https://api.telegram.org/bot" + botToken + "/sendMessage", {
      method: "post",
      contentType: "application/json",
      muteHttpExceptions: true,
      payload: JSON.stringify({
        chat_id: chatId,
        text: text,
        parse_mode: "Markdown",
        disable_web_page_preview: true,
      }),
    });
  } catch (e) {}
}

function answerCallback_(botToken, callbackId, text) {
  try {
    UrlFetchApp.fetch("https://api.telegram.org/bot" + botToken + "/answerCallbackQuery", {
      method: "post",
      contentType: "application/json",
      muteHttpExceptions: true,
      payload: JSON.stringify({ callback_query_id: callbackId, text: text }),
    });
  } catch (e) {}
}

// ============================================================================
// SMART GAME-FINISH AUTO-SETTLER (Dynamic Finish Triggers + 15m Watchdog)
// ============================================================================

const ESPN_LEAGUE_ENDPOINTS_ = {
  CFB: { path: "football/college-football", groups: "80" },
  NCAAF: { path: "football/college-football", groups: "80" },
  NFL: { path: "football/nfl", groups: null },
  NBA: { path: "basketball/nba", groups: null },
  NCAAM: { path: "basketball/mens-college-basketball", groups: "50" },
  CBB: { path: "basketball/mens-college-basketball", groups: "50" },
  NHL: { path: "hockey/nhl", groups: null },
  MLB: { path: "baseball/mlb", groups: null },
  EPL: { path: "soccer/eng.1", groups: null },
  SOCCER: { path: "soccer/eng.1", groups: null },
  UCL: { path: "soccer/uefa.champions", groups: null },
};

const ALLOWED_SETTLER_TAGS_ = [
  "WIN: Correlated Home Favorite + Alt Total Cushion",
  "WIN: Fatigue / Back-to-Back Fade",
  "WIN: Trench / Pace Mismatch",
  "WIN: Disciplined Straight / Floor Play",
  "FAIL: Totals Blowout / Late Garbage Time",
  "FAIL: Road Favorite Upset (Parlay Bust)",
  "FAIL: Hook / Half-Point Variance",
  "FAIL: Juice Parlay / Multi-Leg Bust",
];

/**
 * 1-Click Setup: Installs the lightweight 15-minute watchdog trigger (exits in ~50ms when 0 bets are pending)
 * and schedules exact one-shot finish-time triggers for all currently pending bets.
 */
function setupAutoSettlerTriggers() {
  ensureWatchdogTrigger_();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ws = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
  const state = getSheetState_(ws);
  scheduleFinishTriggers_(state.pending);
  Logger.log("Auto-settler triggers active! Pending bets scheduled: " + state.pending.length);
}

function removeAutoSettlerTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  let removed = 0;
  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "checkAndAutoSettle") {
      ScriptApp.deleteTrigger(triggers[i]);
      removed++;
    }
  }
  PropertiesService.getScriptProperties().deleteProperty("SCHEDULED_FINISH_TIMES");
  Logger.log("Removed " + removed + " checkAndAutoSettle trigger(s).");
}

function ensureWatchdogTrigger_() {
  const triggers = ScriptApp.getProjectTriggers();
  const hasRepeating = triggers.some(t =>
    t.getHandlerFunction() === "checkAndAutoSettle" &&
    t.getTriggerSource() === ScriptApp.TriggerSource.CLOCK &&
    !isOneShotTrigger_(t)
  );
  if (!hasRepeating) {
    ScriptApp.newTrigger("checkAndAutoSettle").timeBased().everyMinutes(15).create();
  }
}

function isOneShotTrigger_(trigger) {
  // Repeating triggers have no saved timestamp in our SCHEDULED_FINISH_TIMES map
  const map = getScheduledFinishMap_();
  return Boolean(map[trigger.getUniqueId()]);
}

function getScheduledFinishMap_() {
  try {
    return JSON.parse(getProp_("SCHEDULED_FINISH_TIMES") || "{}");
  } catch (e) {
    return {};
  }
}

function saveScheduledFinishMap_(map) {
  PropertiesService.getScriptProperties().setProperty("SCHEDULED_FINISH_TIMES", JSON.stringify(map));
}

function cleanupExpiredOneShotTriggers_() {
  const nowMs = Date.now();
  const map = getScheduledFinishMap_();
  const triggers = ScriptApp.getProjectTriggers();
  const liveIds = {};
  for (let i = 0; i < triggers.length; i++) {
    const t = triggers[i];
    const id = t.getUniqueId();
    liveIds[id] = true;
    if (map[id] && map[id] < nowMs - 5 * 60 * 1000) {
      try { ScriptApp.deleteTrigger(t); } catch (e) {}
      delete map[id];
    }
  }
  Object.keys(map).forEach(id => {
    if (!liveIds[id]) delete map[id];
  });
  saveScheduledFinishMap_(map);
}

function estimateFinishDate_(bet) {
  const sel = String(bet.selections || "");
  let m = sel.match(/(\d{1,2}):(\d{2})\s*(AM|PM)/i);
  let ampm = m ? m[3].toUpperCase() : "";
  if (!m) {
    m = sel.match(/Window\s*\d+\s*:\s*(\d{1,2}):(\d{2})/i);
    if (!m) return null;
  }

  let hour = parseInt(m[1], 10);
  const minute = parseInt(m[2], 10);
  const sp = String(bet.sport || "").toUpperCase();
  if (!ampm) {
    if ((sp === "EPL" || sp === "UCL" || sp === "SOCCER") && hour >= 7 && hour <= 11) {
      ampm = "AM";
    } else {
      ampm = "PM";
    }
  }
  if (ampm === "PM" && hour !== 12) hour += 12;
  if (ampm === "AM" && hour === 12) hour = 0;

  const dateStr = String(bet.date || Utilities.formatDate(new Date(), TIMEZONE, "yyyy-MM-dd")).trim();
  const dm = dateStr.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!dm) return null;

  // Build America/New_York offset cleanly (handles EST -05:00 and EDT -04:00)
  const probe = new Date(parseInt(dm[1], 10), parseInt(dm[2], 10) - 1, parseInt(dm[3], 10), 12, 0, 0);
  const tzOffset = Utilities.formatDate(probe, TIMEZONE, "XXX"); // e.g. "-05:00" or "-04:00"
  const hh = String(hour).padStart(2, "0");
  const mm = String(minute).padStart(2, "0");
  const startDate = new Date(dateStr + "T" + hh + ":" + mm + ":00" + tzOffset);
  if (isNaN(startDate.getTime())) return null;

  let durationMin = 195; // default 3h 15m
  if (sp === "EPL" || sp === "UCL" || sp === "SOCCER") durationMin = 135; // 2h 15m
  else if (sp === "NBA" || sp === "NHL" || sp === "NCAAM" || sp === "CBB" || sp === "ATP" || sp === "WTA") durationMin = 165; // 2h 45m
  else if (sp === "CFB" || sp === "NCAAF" || sp === "NFL" || sp === "MLB") durationMin = 225; // 3h 45m

  return new Date(startDate.getTime() + durationMin * 60 * 1000);
}

function scheduleFinishTriggers_(bets) {
  if (!bets || bets.length === 0) return;
  cleanupExpiredOneShotTriggers_();
  ensureWatchdogTrigger_();

  const nowMs = Date.now();
  const map = getScheduledFinishMap_();
  const existingTimes = Object.values(map);

  for (let i = 0; i < bets.length; i++) {
    const finishDate = estimateFinishDate_(bets[i]);
    if (!finishDate) continue;
    const finishMs = finishDate.getTime();
    if (finishMs <= nowMs + 2 * 60 * 1000) continue; // Already past or imminent (watchdog handles)
    // Skip if another one-shot trigger is already within +/- 10 minutes
    const alreadyNear = existingTimes.some(ts => Math.abs(ts - finishMs) < 10 * 60 * 1000);
    if (alreadyNear) continue;
    // Stay comfortably under Apps Script's 20-trigger cap
    if (ScriptApp.getProjectTriggers().length >= 16) break;

    const trig = ScriptApp.newTrigger("checkAndAutoSettle").timeBased().at(finishDate).create();
    map[trig.getUniqueId()] = finishMs;
    existingTimes.push(finishMs);
  }
  saveScheduledFinishMap_(map);
}

/**
 * Dashboard API action: triggered when user taps "Settle Refresh" in the web app.
 * Grades any completed ESPN games for pending bets, recalculates remaining bets' wager amounts
 * for the day when the bankroll changes, and optionally dispatches daily_curator.yml on GitHub.
 */
function handleSettleBetsApi_(body) {
  body = body || {};
  return runAutoSettlementCore_({
    forceRecalc: true,
    notifyTelegram: body.notify_telegram !== false,
    stage: body.stage || "settler",
  });
}

/**
 * Trigger handler: checks ESPN only for pending bets' sports/dates.
 * Grades immediately with Gemini when a pending game goes FINAL, updates the sheet,
 * recalculates remaining bets for the day if bankroll changed, sends a Telegram receipt,
 * and optionally dispatches daily_curator.yml if GITHUB_PAT is set.
 */
function checkAndAutoSettle() {
  runAutoSettlementCore_({
    forceRecalc: false,
    notifyTelegram: true,
    stage: "settler",
  });
}

function runAutoSettlementCore_(opts) {
  opts = opts || {};
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(opts.forceRecalc ? 20000 : 15000)) {
    if (opts.forceRecalc) {
      return { ok: false, error: "Another settlement or sync operation is currently in progress. Try again in a moment." };
    }
    return null;
  }
  try {
    cleanupExpiredOneShotTriggers_();
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const ws = ss.getSheetByName(TRACKER_SHEET_NAME) || ss.getSheets()[0];
    const beforeDash = buildDashboardData_();
    const prevBankroll = beforeDash.bankroll;
    const state = getSheetState_(ws);

    let updatedCount = 0;
    let wins = 0;
    let losses = 0;
    let pushes = 0;
    const settledLines = [];

    if (state.pending.length > 0) {
      const completedGames = fetchCompletedEspnGames_(state.pending);
      if (completedGames.length > 0) {
        const settlements = gradePendingBetsWithGemini_(state.pending, completedGames);
        if (settlements && settlements.length > 0) {
          const freshState = getSheetState_(ws);
          for (let i = 0; i < settlements.length; i++) {
            const s = settlements[i];
            const betId = String(s.bet_id || "").trim();
            const newRes = String(s.result || "Pending").trim();
            if (newRes !== "Won" && newRes !== "Lost" && newRes !== "Push") continue;

            const rIdx = betId ? findRowByBetId_(freshState.allVals, freshState.headerIdx, betId) : parseInt(s.row_index, 10);
            if (!rIdx || rIdx <= freshState.headerIdx + 1) continue;

            // Double-check still Pending so we never re-grade
            const curStatus = String(freshState.allVals[rIdx - 1][10] || "").trim().toLowerCase();
            if (curStatus !== "pending") continue;

            ws.getRange("K" + rIdx).setValue(newRes);
            const note = String(s.verification_note || "").trim();
            if (note) {
              const oldNote = String(freshState.allVals[rIdx - 1][14] || "").trim();
              ws.getRange("O" + rIdx).setValue((oldNote ? oldNote + " | " : "") + "[Settled: " + note + "]");
            }
            const tag = String(s.edge_tag || "").trim();
            if (tag && ALLOWED_SETTLER_TAGS_.indexOf(tag) !== -1) {
              ws.getRange("P" + rIdx).setValue(tag);
            }

            if (newRes === "Won") wins++;
            else if (newRes === "Lost") losses++;
            else pushes++;

            const icon = newRes === "Won" ? "🟢" : (newRes === "Lost" ? "🔴" : "⚪");
            settledLines.push(icon + " *" + betId + "* (" + newRes + ") — " + note);
            updatedCount++;
          }
        }
      }
    }

    if (updatedCount > 0) {
      SpreadsheetApp.flush();
    }

    let afterDash = buildDashboardData_();
    const bankrollChanged = Math.abs(afterDash.bankroll - prevBankroll) >= 0.01;
    let adjustedPlays = [];

    if (updatedCount > 0 || bankrollChanged || opts.forceRecalc) {
      adjustedPlays = recalculateCuratorFeedInSheet_(ss, afterDash);
      afterDash = buildDashboardData_();
    }

    let ghTriggered = false;
    if (updatedCount > 0) {
      const receiptLines = settledLines.slice();
      if (adjustedPlays && adjustedPlays.length > 0) {
        receiptLines.push("");
        receiptLines.push("🔄 *Adjusted Wagers for Remaining Bets Today ($" + afterDash.bankroll.toFixed(2) + " BR):*");
        for (let a = 0; a < adjustedPlays.length; a++) {
          const ap = adjustedPlays[a];
          const wasTxt = (ap.changed && ap.old_stake > 0) ? (" • was $" + ap.old_stake.toFixed(2)) : "";
          receiptLines.push("• *" + ap.selection + "* — *$" + ap.new_stake.toFixed(2) + "* (" + ap.wager_pct + "% BR" + wasTxt + ")");
        }
      }
      if (opts.notifyTelegram !== false) {
        const header = "✅ *Auto-Settlement Complete* (" + wins + "W - " + losses + "L" + (pushes ? " - " + pushes + "P" : "") + ")";
        sendTelegramReceipt_(ws, receiptLines, header);
      }
      ghTriggered = triggerGithubRefresh_(opts.stage || "settler");
    } else if (opts.forceRecalc) {
      ghTriggered = triggerGithubRefresh_(opts.stage || "settler");
    }

    return {
      ok: true,
      settled_count: updatedCount,
      settled_lines: settledLines,
      prev_bankroll: prevBankroll,
      new_bankroll: afterDash.bankroll,
      bankroll_changed: bankrollChanged,
      adjusted_plays: adjustedPlays,
      github_triggered: ghTriggered,
      dashboard: afterDash,
    };
  } finally {
    lock.releaseLock();
  }
}

function fetchCompletedEspnGames_(pendingBets) {
  const neededSports = {};
  const datesSet = {};
  const now = new Date();
  datesSet[Utilities.formatDate(now, TIMEZONE, "yyyyMMdd")] = true;
  datesSet[Utilities.formatDate(new Date(now.getTime() - 24 * 3600 * 1000), TIMEZONE, "yyyyMMdd")] = true;

  const pendingText = pendingBets.map(b => (b.sport + " " + b.selections).toLowerCase()).join(" ");
  const hasPropKw = /(rebound|assist|point|shot|sog|goal|pra|over |under )/i.test(pendingText);

  for (let i = 0; i < pendingBets.length; i++) {
    const sp = String(pendingBets[i].sport || "").toUpperCase();
    if (ESPN_LEAGUE_ENDPOINTS_[sp]) {
      neededSports[sp] = ESPN_LEAGUE_ENDPOINTS_[sp];
    } else if (sp === "MULTI-SPORT" || !sp) {
      ["CFB", "NFL", "NBA", "NHL", "NCAAM", "EPL"].forEach(k => { neededSports[k] = ESPN_LEAGUE_ENDPOINTS_[k]; });
    }
    const cleanD = String(pendingBets[i].date || "").replace(/-/g, "");
    if (/^\d{8}$/.test(cleanD)) {
      datesSet[cleanD] = true;
    }
  }

  const completedMatches = [];
  const seenEventIds = {};
  const sportsKeys = Object.keys(neededSports);
  const dateKeys = Object.keys(datesSet);

  for (let s = 0; s < sportsKeys.length; s++) {
    const league = sportsKeys[s];
    const info = neededSports[league];
    for (let d = 0; d < dateKeys.length; d++) {
      let url = "https://site.api.espn.com/apis/site/v2/sports/" + info.path + "/scoreboard?dates=" + dateKeys[d];
      if (info.groups) url += "&groups=" + info.groups;
      try {
        const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
        if (resp.getResponseCode() !== 200) continue;
        const events = (JSON.parse(resp.getContentText()).events) || [];
        for (let e = 0; e < events.length; e++) {
          const ev = events[e];
          const evId = String(ev.id || "");
          if (evId && seenEventIds[league + ":" + evId]) continue;
          const stType = (ev.status && ev.status.type) || {};
          if (!stType.completed) continue;

          const comp = (ev.competitions && ev.competitions[0]) || null;
          const competitors = (comp && comp.competitors) || [];
          if (competitors.length < 2) continue;
          const home = competitors.find(c => c.homeAway === "home");
          const away = competitors.find(c => c.homeAway === "away");
          if (!home || !away) continue;

          const hTeam = home.team || {};
          const aTeam = away.team || {};
          const tokens = [
            hTeam.shortDisplayName, hTeam.name, hTeam.displayName, hTeam.abbreviation,
            aTeam.shortDisplayName, aTeam.name, aTeam.displayName, aTeam.abbreviation,
          ].map(t => String(t || "").toLowerCase()).filter(t => t.length >= 3);

          const isRelevant = tokens.some(tok => pendingText.indexOf(tok) !== -1);
          if (!isRelevant) continue;

          if (evId) seenEventIds[league + ":" + evId] = true;
          const hScore = parseInt(parseFloat(home.score || "0"), 10) || 0;
          const aScore = parseInt(parseFloat(away.score || "0"), 10) || 0;
          const rec = {
            league: league,
            date: dateKeys[d],
            matchup: (aTeam.displayName || "") + " @ " + (hTeam.displayName || ""),
            away_team: aTeam.displayName || "",
            away_score: aScore,
            home_team: hTeam.displayName || "",
            home_score: hScore,
            total_points: hScore + aScore,
            completed: true,
            status: stType.description || stType.state || "Final",
          };
          if (hasPropKw && evId && (league === "NBA" || league === "NHL" || league === "NCAAM" || league === "CBB")) {
            const box = fetchEspnBoxscorePlayers_(info.path, evId, league);
            if (box && Object.keys(box).length > 0) rec.player_boxscore = box;
          }
          completedMatches.push(rec);
        }
      } catch (err) {
        // Continue checking other leagues/dates
      }
    }
  }
  return completedMatches;
}

function fetchEspnBoxscorePlayers_(path, eventId, league) {
  const out = {};
  try {
    const resp = UrlFetchApp.fetch("https://site.api.espn.com/apis/site/v2/sports/" + path + "/summary?event=" + encodeURIComponent(eventId), { muteHttpExceptions: true });
    if (resp.getResponseCode() !== 200) return out;
    const players = ((JSON.parse(resp.getContentText()).boxscore || {}).players) || [];
    for (let t = 0; t < players.length; t++) {
      const groups = players[t].statistics || [];
      for (let g = 0; g < groups.length; g++) {
        const labels = (groups[g].labels || []).map(l => String(l).toUpperCase());
        const athletes = groups[g].athletes || [];
        for (let a = 0; a < athletes.length; a++) {
          const name = (athletes[a].athlete && athletes[a].athlete.displayName) || "";
          const vals = athletes[a].stats || [];
          if (!name || vals.length !== labels.length) continue;
          const map = {};
          for (let k = 0; k < labels.length; k++) map[labels[k]] = vals[k];
          if (league === "NHL" && (map.G !== undefined || map.S !== undefined)) {
            const gl = parseInt(map.G || 0, 10) || 0;
            const as = parseInt(map.A || 0, 10) || 0;
            const sog = parseInt(map.S || map.SOG || 0, 10) || 0;
            out[name] = { G: gl, A: as, PTS: gl + as, SOG: sog };
          } else if ((league === "NBA" || league === "NCAAM" || league === "CBB") && map.PTS !== undefined) {
            const pts = parseInt(map.PTS || 0, 10) || 0;
            const reb = parseInt(map.REB || 0, 10) || 0;
            const ast = parseInt(map.AST || 0, 10) || 0;
            out[name] = { PTS: pts, REB: reb, AST: ast, PRA: pts + reb + ast };
          }
        }
      }
    }
  } catch (e) {}
  return out;
}

function gradePendingBetsWithGemini_(pendingBets, completedGames) {
  const apiKey = getProp_("GEMINI_API_KEY");
  if (!apiKey) return [];

  const prompt = [
    "You are a deterministic sportsbook settlement auditor.",
    "Verify each pending bet against the official completed ESPN scoreboard results below.",
    "GRADING RULES:",
    "1. Parlay/SGP is Won ONLY if every leg is completed and won; Lost if ANY leg completed and lost; else Pending.",
    "2. Keep status Pending if a game is not present in the completed ESPN data.",
    "3. Totals (Over/Under): compare total_points vs line; exact tie on whole number = Push.",
    "4. Moneylines (ML / TO WIN): selected team score > opponent score. For Soccer/EPL/UCL 3-way ML, a draw is Lost.",
    "5. Point spreads (e.g. -7.5, +4.5, -7.0): (selected_team_score + spread) vs opponent_score; > = Won, < = Lost, == = Push.",
    "6. Player props: check player_boxscore stats strictly against the line.",
    "7. Pick best matching WIN:/FAIL: tag from ALLOWED_EDGE_TAGS (or \"\" for Pending/Push).",
    "",
    "ALLOWED_EDGE_TAGS: " + JSON.stringify(ALLOWED_SETTLER_TAGS_),
    "PENDING BETS: " + JSON.stringify(pendingBets),
    "COMPLETED ESPN GAMES: " + JSON.stringify(completedGames),
    "",
    "Respond ONLY with JSON: {\"settlements\": [{\"row_index\": 17, \"bet_id\": \"BET-052\", \"result\": \"Won|Lost|Push|Pending\", \"edge_tag\": \"...\", \"verification_note\": \"Short score proof\"}]}",
  ].join("\n");

  for (let i = 0; i < GEMINI_MODELS.length; i++) {
    const model = GEMINI_MODELS[i];
    try {
      const url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent?key=" + encodeURIComponent(apiKey);
      const resp = UrlFetchApp.fetch(url, {
        method: "post",
        contentType: "application/json",
        muteHttpExceptions: true,
        payload: JSON.stringify({
          contents: [{ parts: [{ text: prompt }] }],
          generationConfig: { responseMimeType: "application/json", temperature: 0.0 },
        }),
      });
      if (resp.getResponseCode() !== 200) continue;
      const json = JSON.parse(resp.getContentText());
      const outText = json && json.candidates && json.candidates[0] && json.candidates[0].content && json.candidates[0].content.parts && json.candidates[0].content.parts[0] && json.candidates[0].content.parts[0].text;
      if (outText) {
        return JSON.parse(outText).settlements || [];
      }
    } catch (e) {}
  }
  return [];
}

function triggerGithubRefresh_(stage) {
  const pat = getProp_("GITHUB_PAT");
  if (!pat) return false;
  const repo = getProp_("GITHUB_REPO") || "iannuzzi8100/betting-curator";
  try {
    const resp = UrlFetchApp.fetch("https://api.github.com/repos/" + repo + "/actions/workflows/daily_curator.yml/dispatches", {
      method: "post",
      contentType: "application/json",
      muteHttpExceptions: true,
      headers: {
        Authorization: "Bearer " + pat,
        Accept: "application/vnd.github+json",
      },
      payload: JSON.stringify({ ref: "main", inputs: { stage: stage || "settler" } }),
    });
    const code = resp.getResponseCode();
    return code >= 200 && code < 300;
  } catch (e) {
    return false;
  }
}

// ============================================================================
// UTILITY HELPERS
// ============================================================================

function getSheetState_(trackerWs) {
  const allVals = trackerWs.getDataRange().getDisplayValues();
  const headerIdx = findHeaderIdx_(allVals);
  const pending = [];
  const recent = [];
  for (let idx = headerIdx + 1; idx < allVals.length; idx++) {
    const r = allVals[idx];
    if (!r || !String(r[0] || "").trim().startsWith("BET-")) continue;
    const item = {
      row_index: idx + 1,
      bet_id: String(r[0]).trim(),
      date: String(r[1] || "").trim(),
      sport: String(r[2] || "").trim(),
      bet_type: String(r[3] || "").trim(),
      selections: String(r[5] || "").trim(),
      odds: String(r[6] || "").trim(),
      stake: String(r[8] || "").trim(),
      result: String(r[10] || "").trim(),
    };
    recent.push(item);
    if (item.result.toLowerCase() === "pending") pending.push(item);
  }
  recent.sort((a, b) => parseBetNum_(a.bet_id) - parseBetNum_(b.bet_id));
  return { allVals: allVals, headerIdx: headerIdx, pending: pending, recent: recent.slice(-10) };
}

function parseBetNum_(betId) {
  const m = String(betId || "").match(/BET-(\d+)/i);
  return m ? parseInt(m[1], 10) : 0;
}

function findHeaderIdx_(allRows) {
  for (let i = 0; i < Math.min(allRows.length, 30); i++) {
    const r = allRows[i];
    if (r.indexOf("Bet ID") !== -1 && r.indexOf("Result") !== -1) return i;
  }
  return 15;
}

function findRowByBetId_(allVals, headerIdx, betId) {
  for (let i = headerIdx + 1; i < allVals.length; i++) {
    if (allVals[i] && String(allVals[i][0] || "").trim() === betId) return i + 1;
  }
  return null;
}

function getProp_(key) {
  return PropertiesService.getScriptProperties().getProperty(key) || "";
}

function verifyDashboardPin_(pin) {
  const expected = getProp_("DASHBOARD_PIN");
  if (!expected) return false;
  return String(pin || "").trim() === String(expected).trim();
}

function parseNum_(val, fallback) {
  if (val === null || val === undefined || val === "") return fallback || 0.0;
  let s = String(val).replace(/[$,%\s]/g, "");
  if (s.startsWith("(") && s.endsWith(")")) s = "-" + s.slice(1, -1);
  const n = parseFloat(s);
  return isNaN(n) ? (fallback || 0.0) : n;
}

function cleanFloat_(val, fallback) {
  return parseNum_(val, fallback);
}

function cleanInt_(val, fallback) {
  if (val === null || val === undefined || val === "") return fallback;
  const s = String(val).replace(/[+,\s]/g, "");
  const n = parseInt(parseFloat(s), 10);
  return isNaN(n) ? fallback : n;
}

function round1_(n) { return Math.round(n * 10) / 10; }
function round2_(n) { return Math.round(n * 100) / 100; }

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
