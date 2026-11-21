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
