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
  const todayStr = Utilities.formatDate(now, TIMEZONE, "yyyy-MM-dd");
  const yesterdayStr = Utilities.formatDate(yesterday, TIMEZONE, "yyyy-MM-dd");
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

  // Newest-first order for Pending, Today, and Yesterday ticket lists
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
    }

    if (e.bDate === todayStr && (resL === "won" || resL === "lost" || resL === "push")) {
      if (resL === "won") todayWins++;
      else if (resL === "lost") todayLosses++;
      else if (resL === "push") todayPushes++;
      todayPl += e.netPl;
      todayTickets.push({
        bet_id: e.bet_id,
        sport: e.sport,
        selections: e.sel,
        odds: e.odds,
        stake: round2_(e.stake),
        result: e.res,
        net_pl: round2_(e.netPl),
      });
    }

    if (e.bDate === yesterdayStr && (resL === "won" || resL === "lost" || resL === "push")) {
      if (resL === "won") yestWins++;
      else if (resL === "lost") yestLosses++;
      else if (resL === "push") yestPushes++;
      yestPl += e.netPl;
      yestTickets.push({
        bet_id: e.bet_id,
        sport: e.sport,
        selections: e.sel,
        odds: e.odds,
        stake: round2_(e.stake),
        result: e.res,
        net_pl: round2_(e.netPl),
      });
    }
  }

  const overallTotal = overallWins + overallLosses;
  const overallWinPct = overallTotal > 0 ? round1_((overallWins / overallTotal) * 100.0) : 0.0;
  const overallRoi = totalStakedSettled > 0 ? round1_((totalNetPl / totalStakedSettled) * 100.0) : 0.0;

  const todayTotal = todayWins + todayLosses;
  const todayWinPct = todayTotal > 0 ? round1_((todayWins / todayTotal) * 100.0) : 0.0;

  const yestTotal = yestWins + yestLosses;
  const yestWinPct = yestTotal > 0 ? round1_((yestWins / yestTotal) * 100.0) : 0.0;

  const curated = getLatestCuratedPlays_(feedWs, todayStr);

  return {
    live_mode: true,
    updated_at: updatedAtStr,
    today: todayStr,
    yesterday: yesterdayStr,
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
    pending_tickets: pendingTickets,
    curated_date: curated.date,
    curated_plays: curated.plays,
    curated_raw: curated.raw,
    equity_curve: equityCurve,
  };
}

function getLatestCuratedPlays_(feedWs, fallbackDate) {
  if (!feedWs) return { date: fallbackDate, plays: [], raw: "" };
  const vals = feedWs.getDataRange().getDisplayValues();
  let latestText = "";
  let latestDate = fallbackDate;
  for (let i = vals.length - 1; i >= 0; i--) {
    const r = vals[i];
    if (r.length >= 12 && String(r[11] || "").trim()) {
      latestDate = String(r[0] || "").trim() || fallbackDate;
      latestText = String(r[11]).trim();
      break;
    }
  }
  const plays = parseCuratedPlaysText_(latestText);
  return { date: latestDate, plays: plays, raw: latestText };
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
      const cleanTitle = m[1].replace(/\*/g, "").trim();
      const parsed = parseCuratedTitleFields_(cleanTitle);
      curPlay = {
        title: cleanTitle,
        edge: "",
        confidence_pct: null,
        confidence_tier: "",
        sport: parsed.sport,
        selection: parsed.selection,
        odds: parsed.odds,
        stake: parsed.stake,
        game_time: parsed.game_time,
      };
    } else if (curPlay && (s.startsWith("- ") || s.startsWith("• "))) {
      const rawBullet = s.replace(/^[-•]\s*/, "").replace(/[*`]/g, "").trim();
      if (/^confidence:/i.test(rawBullet)) {
        const confBody = rawBullet.replace(/^confidence:\s*/i, "").replace(/^[\s\[\]]+|[\s\[\]]+$/g, "");
        const pm = confBody.match(/(\d{2,3})\s*%/);
        if (pm) curPlay.confidence_pct = parseInt(pm[1], 10);
        const parts = confBody.split("|").map(p => p.trim()).filter(Boolean);
        curPlay.confidence_tier = parts.length >= 3 ? parts[parts.length - 1] : confBody;
      } else {
        const edgeTxt = rawBullet.replace(/^edge:\s*/i, "").trim();
        curPlay.edge = (curPlay.edge + " " + edgeTxt).trim();
      }
    }
  }
  if (curPlay) plays.push(curPlay);
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
    const parts = inside.split(/[—–-]/).map(s => s.trim()).filter(Boolean);
    if (parts.length >= 2) {
      gameTime = parts[0];
      sport = parts[parts.length - 1].toUpperCase();
    } else if (parts.length === 1) {
      sport = parts[0].toUpperCase();
    }
  }

  let odds = -110;
  const oddsMatch = rest.match(/\(([+-]\d{3,4})\)/);
  if (oddsMatch) {
    odds = parseInt(oddsMatch[1], 10);
  }

  let stake = 0.0;
  const stakeMatch = rest.match(/\$(\d+(?:\.\d{1,2})?)/);
  if (stakeMatch) {
    stake = parseFloat(stakeMatch[1]);
  }

  let pick = rest;
  if (oddsMatch) {
    pick = rest.slice(0, rest.indexOf(oddsMatch[0])).trim();
  } else if (rest.indexOf("—") !== -1) {
    pick = rest.slice(0, rest.indexOf("—")).trim();
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

    if (res.notes.length > 0 && body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "🔄 *Bet Updated via Live Dashboard:*");
    }

    return { ok: true, notes: res.notes, dashboard: buildDashboardData_() };
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

    if (res.notes.length > 0 && body.notify_telegram !== false) {
      sendTelegramReceipt_(trackerWs, res.notes, "✏️ *Bet Edited via Live Dashboard:*");
    }

    return { ok: true, notes: res.notes, dashboard: buildDashboardData_() };
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
  const freedSlots = [];

  // 1. Removals / cashouts
  for (let i = 0; i < removals.length; i++) {
    const rem = removals[i];
    let rIdx = rem.row_index ? parseInt(rem.row_index, 10) : null;
    const bId = String(rem.bet_id || "").trim();
    const isFull = rem.is_full_refund !== false;
    const cRet = rem.cashout_return;
    const reason = String(rem.reason || "Cashed out").trim();

    if (!rIdx && bId) {
      rIdx = findRowByBetId_(allVals, headerIdx, bId);
    }
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

  // 2. In-place edits
  for (let i = 0; i < updates.length; i++) {
    const upd = updates[i];
    let rIdx = upd.row_index ? parseInt(upd.row_index, 10) : null;
    const bId = String(upd.bet_id || "").trim();
    if (!rIdx && bId) {
      rIdx = findRowByBetId_(allVals, headerIdx, bId);
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
      actionNotes.push("✅ *Logged " + betId + "* ($" + stake.toFixed(2) + ") — " + sel + " (" + (odds >= 0 ? "+" + odds : odds) + ")");
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
