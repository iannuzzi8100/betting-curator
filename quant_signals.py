import csv
import io
import json
import math
import os
import re
import datetime
import requests


def _parse_wl(rec_str):
    if not rec_str:
        return 0, 0
    m = re.match(r"(\d+)-(\d+)", str(rec_str).strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def _norm_name(s):
    return re.sub(
        r"\s+",
        " ",
        re.sub(
            r"\b(?:university|college|state|st|the|fc|cf|sc|afc)\b",
            " ",
            re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower()),
        ),
    ).strip()


def _norm_exact(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower())).strip()


# ---------------------------------------------------------------------------
# 1. Home vs. Away Split Records (Zero extra API calls — inside ESPN scoreboard)
# ---------------------------------------------------------------------------
def extract_home_away_splits(home_comp, away_comp, h_abbr, a_abbr):
    """
    Extracts exact Home record for home team and Road record for away team from ESPN competitor.records.
    Returns dict with raw strings, W/L ints, and computed split edge info.
    """
    h_recs = (home_comp or {}).get("records") or []
    a_recs = (away_comp or {}).get("records") or []

    h_home_str = next(
        (r.get("summary", "") for r in h_recs if str(r.get("type", "")).lower() == "home" or str(r.get("name", "")).lower() == "home"),
        "",
    )
    a_road_str = next(
        (r.get("summary", "") for r in a_recs if str(r.get("type", "")).lower() in ("road", "away") or str(r.get("name", "")).lower() in ("road", "away")),
        "",
    )

    hw, hl = _parse_wl(h_home_str)
    aw, al = _parse_wl(a_road_str)
    h_gp = hw + hl
    a_gp = aw + al
    h_pct = (hw / h_gp) if h_gp > 0 else None
    a_pct = (aw / a_gp) if a_gp > 0 else None

    edge_side = None  # h_abbr or a_abbr
    edge_pts = 0
    opp_penalty = 0
    tag = ""

    if h_gp >= 3 and a_gp >= 3 and h_pct is not None and a_pct is not None:
        if h_pct >= 0.70 and a_pct <= 0.35:
            edge_side = h_abbr
            edge_pts = 7
            opp_penalty = -5
            tag = f"[🏠 HOME/ROAD SPLIT EDGE: {h_abbr} {h_home_str} Home vs {a_abbr} {a_road_str} Road]"
        elif a_pct >= 0.70 and h_pct <= 0.40:
            edge_side = a_abbr
            edge_pts = 6
            opp_penalty = -4
            tag = f"[✈️ ROAD WARRIOR SPLIT EDGE: {a_abbr} {a_road_str} Road vs {h_abbr} {h_home_str} Home]"

    summary_str = ""
    if h_home_str or a_road_str:
        summary_str = f"Splits: {a_abbr} {a_road_str or 'N/A'} Rd @ {h_abbr} {h_home_str or 'N/A'} Hm"

    return {
        "h_home_str": h_home_str,
        "a_road_str": a_road_str,
        "h_home_wl": (hw, hl),
        "a_road_wl": (aw, al),
        "edge_side": edge_side,
        "edge_pts": edge_pts,
        "opp_penalty": opp_penalty,
        "tag": tag,
        "summary_str": summary_str,
    }


# ---------------------------------------------------------------------------
# 2. Per-Game Key Injury Auto-Adjustment (Zero extra API calls — inside ESPN summary)
# ---------------------------------------------------------------------------
def extract_game_injury_impact(sum_data, label, a_abbr, h_abbr, a_name, h_name):
    """
    Parses per-game `injuries` from ESPN summary?event={id} to penalize teams missing
    key starters (e.g. NFL/CFB starting QB Out/Doubtful, or NBA/NHL/NCAAM rotation cluster Out).
    Returns dict keyed by side abbreviation with {penalty, opp_boost, key_outs, tag}.
    """
    lbl = str(label or "").upper()
    inj_blocks = (sum_data or {}).get("injuries") or []
    out_by_abbr = {a_abbr: [], h_abbr: []}
    qb_out_by_abbr = {a_abbr: [], h_abbr: []}

    a_low = _norm_exact(a_name)
    h_low = _norm_exact(h_name)

    for block in inj_blocks:
        t_info = block.get("team") or {}
        t_abbr = str(t_info.get("abbreviation", "") or "").strip()
        t_disp = _norm_exact(t_info.get("displayName", ""))
        side_abbr = None
        if t_abbr and t_abbr.upper() == str(a_abbr).upper():
            side_abbr = a_abbr
        elif t_abbr and t_abbr.upper() == str(h_abbr).upper():
            side_abbr = h_abbr
        elif t_disp and a_low and (t_disp in a_low or a_low in t_disp):
            side_abbr = a_abbr
        elif t_disp and h_low and (t_disp in h_low or h_low in t_disp):
            side_abbr = h_abbr
        if not side_abbr:
            continue

        for item in (block.get("injuries") or []):
            status = str(item.get("status", "") or "").strip().lower()
            if status not in ("out", "doubtful", "injured reserve", "suspension"):
                continue
            ath = item.get("athlete") or {}
            p_name = str(ath.get("displayName") or ath.get("shortName") or "").strip()
            pos = str((ath.get("position") or {}).get("abbreviation", "") or "").strip().upper()
            if not p_name:
                continue
            # Filter out long-term IR noise on NFL/CFB unless QB; focus on Out / Doubtful active impact
            if lbl in ("NFL", "CFB") and status == "injured reserve" and pos != "QB":
                continue
            out_by_abbr[side_abbr].append(f"{p_name} ({pos or 'P'})")
            if lbl in ("NFL", "CFB") and pos == "QB":
                qb_out_by_abbr[side_abbr].append(p_name)

    penalties = {a_abbr: 0, h_abbr: 0}
    boosts = {a_abbr: 0, h_abbr: 0}
    tags = []

    for side, opp in ((a_abbr, h_abbr), (h_abbr, a_abbr)):
        qbs = qb_out_by_abbr[side]
        outs = out_by_abbr[side]
        if qbs and not qb_out_by_abbr[opp]:
            penalties[side] -= 14
            boosts[opp] += 8
            tags.append(f"[🏥 INJURY IMPACT: {side} QB OUT/DOUBTFUL ({', '.join(qbs[:2])}) -> Edge {opp}]")
        elif lbl in ("NFL", "CFB") and len(outs) >= 3 and (len(outs) - len(out_by_abbr[opp])) >= 2:
            penalties[side] -= 7
            boosts[opp] += 4
            tags.append(f"[🏥 INJURY IMPACT: {side} missing {len(outs)} ({', '.join(outs[:3])}) vs healthier {opp}]")
        elif lbl in ("NBA", "NHL", "NCAAM") and len(outs) >= 2 and (len(outs) - len(out_by_abbr[opp])) >= 2:
            pen = -10 if len(outs) >= 3 else -6
            bst = 6 if len(outs) >= 3 else 4
            penalties[side] += pen
            boosts[opp] += bst
            tags.append(f"[🏥 INJURY IMPACT: {side} missing {len(outs)} ({', '.join(outs[:3])}) vs healthier {opp}]")

    return {
        "penalties": penalties,
        "boosts": boosts,
        "outs": out_by_abbr,
        "tags": tags,
    }


# ---------------------------------------------------------------------------
# 3. Outdoor Weather Impact for NFL & CFB (Zero extra API calls — inside ESPN summary)
# ---------------------------------------------------------------------------
def extract_game_weather(sum_data, label):
    """
    Reads `gameInfo.weather` and `gameInfo.venue.indoor` from ESPN summary?event={id}.
    Flags severe outdoor wind (>=15 mph), freezing temp (<=28 F), or rain/snow storms.
    """
    lbl = str(label or "").upper()
    if lbl not in ("NFL", "CFB"):
        return {"severe": False, "wind_mph": 0.0, "temp_f": None, "cond": "", "is_precip": False, "tag": ""}

    gi = (sum_data or {}).get("gameInfo") or {}
    venue = gi.get("venue") or {}
    if venue.get("indoor") is True:
        return {"severe": False, "wind_mph": 0.0, "temp_f": None, "cond": "Indoor", "is_precip": False, "tag": ""}

    w = gi.get("weather") or {}
    if not w:
        return {"severe": False, "wind_mph": 0.0, "temp_f": None, "cond": "", "is_precip": False, "tag": ""}

    temp_f = w.get("temperature")
    if temp_f is None:
        temp_f = w.get("highTemperature")
    try:
        temp_f = float(temp_f) if temp_f is not None else None
    except Exception:
        temp_f = None

    wind_mph = 0.0
    for wk in ("windSpeed", "gust"):
        if w.get(wk) is not None:
            try:
                wind_mph = max(wind_mph, float(w.get(wk)))
            except Exception:
                pass
    cond = str(w.get("displayValue") or w.get("conditionId") or "").strip()
    cond_l = cond.lower()
    m_wind = re.search(r"(\d{1,2})\s*mph", cond_l)
    if m_wind:
        wind_mph = max(wind_mph, float(m_wind.group(1)))

    is_precip = any(k in cond_l for k in ("rain", "snow", "sleet", "storm", "shower", "blizzard", "flurr"))
    is_severe = (wind_mph >= 15.0) or is_precip or (temp_f is not None and temp_f <= 28.0)

    tag = ""
    if is_severe:
        bits = []
        if temp_f is not None:
            bits.append(f"{int(round(temp_f))}°F")
        if wind_mph >= 10:
            bits.append(f"{int(round(wind_mph))} mph wind")
        if cond:
            bits.append(cond)
        tag = f"[🌬️ WEATHER ALERT: {', '.join(bits)} — Favors UNDER & Rushing]"

    return {
        "severe": is_severe,
        "wind_mph": wind_mph,
        "temp_f": temp_f,
        "cond": cond,
        "is_precip": is_precip,
        "tag": tag,
    }


# ---------------------------------------------------------------------------
# 4. Schedule Density Fatigue (B2B + 3-in-4 Nights for NBA/NHL/NCAAM)
# ---------------------------------------------------------------------------
def fetch_recent_schedule_density(base_sb, now_est, groups=None, lookback_days=3):
    """
    Scans ESPN scoreboard for the last `lookback_days` days (1..3 days ago) to detect:
    - Back-to-back (played yesterday: 0 days rest)
    - 3-in-4 nights (played >= 2 games in the last 3 days)
    Returns (b2b_teams: set, games_in_3d: dict[team_name -> int]).
    """
    b2b_teams = set()
    games_in_3d = {}
    for d_off in range(1, lookback_days + 1):
        ymd = (now_est - datetime.timedelta(days=d_off)).strftime("%Y%m%d")
        try:
            params = {"dates": ymd}
            if groups:
                params["groups"] = groups
            evts = requests.get(base_sb, params=params, timeout=9).json().get("events", [])
            day_seen = set()
            for ye in evts:
                for comp in ye.get("competitions", []):
                    for c in comp.get("competitors", []):
                        t_name = (c.get("team") or {}).get("displayName", "")
                        if t_name and t_name not in day_seen:
                            day_seen.add(t_name)
                            games_in_3d[t_name] = games_in_3d.get(t_name, 0) + 1
                            if d_off == 1:
                                b2b_teams.add(t_name)
        except Exception:
            continue
    return b2b_teams, games_in_3d


# ---------------------------------------------------------------------------
# 5. Season Scoring / Net Differential & Regression Guard (ESPN summary/standings)
# ---------------------------------------------------------------------------
def extract_season_net_differential(sum_data, home_comp, away_comp, label, a_abbr, h_abbr, a_rec, h_rec):
    """
    Extracts season points/goals scored vs. allowed (net differential per game) from ESPN summary
    standings / team statistics so lucky coin-flip records don't fool the Confidence Meter.
    Returns {a_diff, h_diff, edge_side, edge_pts, regression_sides, tags, summary_str}.
    """
    lbl = str(label or "").upper()
    a_diff = h_diff = None

    # Try ESPN summary standings.groups[].standings.entries[] for pointsFor / pointsAgainst / differential
    standings = (sum_data or {}).get("standings") or {}
    groups = standings.get("groups") or []
    abbr_map = {}
    for grp in groups:
        for ent in ((grp.get("standings") or {}).get("entries") or []):
            t_ab = str((ent.get("team") or {}).get("abbreviation", "") or "").upper()
            if not t_ab:
                continue
            st_map = {}
            for st in (ent.get("stats") or []):
                k = str(st.get("name") or st.get("abbreviation") or "").lower()
                if st.get("value") is not None:
                    try:
                        st_map[k] = float(st["value"])
                    except Exception:
                        pass
            gp = st_map.get("gamesplayed") or st_map.get("gp") or (st_map.get("wins", 0) + st_map.get("losses", 0))
            if "avgpointsdifferential" in st_map:
                abbr_map[t_ab] = st_map["avgpointsdifferential"]
            elif "differential" in st_map and gp and gp > 0:
                abbr_map[t_ab] = st_map["differential"] / gp
            elif "pointdifferential" in st_map and gp and gp > 0:
                abbr_map[t_ab] = st_map["pointdifferential"] / gp
            elif "pointsfor" in st_map and "pointsagainst" in st_map and gp and gp > 0:
                abbr_map[t_ab] = (st_map["pointsfor"] - st_map["pointsagainst"]) / gp

    if str(a_abbr).upper() in abbr_map:
        a_diff = round(abbr_map[str(a_abbr).upper()], 2)
    if str(h_abbr).upper() in abbr_map:
        h_diff = round(abbr_map[str(h_abbr).upper()], 2)

    # Fallback: check competitor.statistics on scoreboard (e.g. avgPoints / avgPointsAgainst)
    if a_diff is None or h_diff is None:
        for comp_obj, is_home in ((away_comp, False), (home_comp, True)):
            st_list = (comp_obj or {}).get("statistics") or []
            s_m = {}
            for st in st_list:
                nm = str(st.get("name") or "").lower()
                try:
                    s_m[nm] = float(st.get("displayValue"))
                except Exception:
                    pass
            if "avgpoints" in s_m and "avgpointsallowed" in s_m:
                d_val = round(s_m["avgpoints"] - s_m["avgpointsallowed"], 2)
                if is_home and h_diff is None:
                    h_diff = d_val
                elif not is_home and a_diff is None:
                    a_diff = d_val

    edge_side = None
    edge_pts = 0
    regression_sides = set()
    tags = []
    summary_str = ""

    if a_diff is not None and h_diff is not None:
        summary_str = f"Season NetDiff: {a_abbr} {a_diff:+.1f} vs {h_abbr} {h_diff:+.1f}"
        thresh = 0.65 if lbl in ("NHL", "EPL", "UCL", "LALIGA") else 5.5
        if (h_diff - a_diff) >= thresh:
            edge_side = h_abbr
            edge_pts = 6
            tags.append(f"[📐 NET-DIFF EDGE: {h_abbr} ({h_diff:+.1f}/g) vs {a_abbr} ({a_diff:+.1f}/g)]")
        elif (a_diff - h_diff) >= thresh:
            edge_side = a_abbr
            edge_pts = 6
            tags.append(f"[📐 NET-DIFF EDGE: {a_abbr} ({a_diff:+.1f}/g) vs {h_abbr} ({h_diff:+.1f}/g)]")

        # Regression guard: winning record paired with negative net point differential
        aw, al = _parse_wl(a_rec)
        hw, hl = _parse_wl(h_rec)
        reg_thresh = -0.35 if lbl in ("NHL", "EPL", "UCL", "LALIGA") else -1.5
        if (aw + al) >= 5 and aw > al and a_diff <= reg_thresh:
            regression_sides.add(a_abbr)
            tags.append(f"[⚠️ REGRESSION RISK: {a_abbr} {a_rec} despite {a_diff:+.1f}/g NetDiff]")
        if (hw + hl) >= 5 and hw > hl and h_diff <= reg_thresh:
            regression_sides.add(h_abbr)
            tags.append(f"[⚠️ REGRESSION RISK: {h_abbr} {h_rec} despite {h_diff:+.1f}/g NetDiff]")

    return {
        "a_diff": a_diff,
        "h_diff": h_diff,
        "edge_side": edge_side,
        "edge_pts": edge_pts,
        "regression_sides": regression_sides,
        "tags": tags,
        "summary_str": summary_str,
    }


# ---------------------------------------------------------------------------
# 6. Bart Torvik Free NCAAM Efficiency Model (AdjOE / AdjDE / Barthag)
# ---------------------------------------------------------------------------
def fetch_barttorvik_ratings(now_est=None):
    """
    Fetches Bart Torvik's free daily D1 NCAAM team efficiency JSON (`barthag`, `adjoe`, `adjde`, `adjt`).
    Returns dict keyed by normalized team name -> {name, adjoe, adjde, barthag, adjt}.
    Fails safely to {} if unreachable.
    """
    now_est = now_est or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5)))
    season_year = now_est.year + 1 if now_est.month >= 10 else now_est.year
    ratings = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BettingCurator/2.0)"}

    for yr in (season_year, season_year - 1):
        try:
            url = f"https://barttorvik.com/{yr}_team_results.json"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            rows = resp.json()
            if not isinstance(rows, list) or len(rows) < 50:
                continue
            for r in rows:
                if not isinstance(r, list) or len(r) < 10:
                    continue
                t_name = str(r[1] or "").strip()
                if not t_name:
                    continue
                try:
                    adjoe = float(r[4])
                    adjde = float(r[6])
                    barthag = float(r[8])
                    adjt = float(r[44]) if len(r) > 44 and r[44] is not None else 68.0
                except Exception:
                    continue
                entry = {"name": t_name, "adjoe": adjoe, "adjde": adjde, "barthag": barthag, "adjt": adjt}
                ratings[_norm_exact(t_name)] = entry
                ratings[_norm_name(t_name)] = entry
            if ratings:
                return ratings
        except Exception:
            continue
    return ratings


def lookup_torvik_team(ratings, team_display_name, team_location=""):
    if not ratings:
        return None
    for cand in (team_location, team_display_name):
        if not cand:
            continue
        k1 = _norm_exact(cand)
        if k1 in ratings:
            return ratings[k1]
        k2 = _norm_name(cand)
        if k2 in ratings:
            return ratings[k2]
    # Prefix token fallback (e.g. "Duke Blue Devils" -> "duke")
    toks = _norm_exact(team_location or team_display_name).split()
    for n in (3, 2, 1):
        if len(toks) >= n:
            sub = " ".join(toks[:n])
            if sub in ratings:
                return ratings[sub]
    return None


def compute_torvik_matchup_probs(a_rating, h_rating, is_neutral=False):
    """
    Computes Log5 + Home-Court Adjusted Win Probabilities (away_prob%, home_prob%) from Bart Torvik Barthag.
    Standard D1 college basketball home-court advantage ~ +3.2 pts (~ +0.30 logit shift).
    """
    if not a_rating or not h_rating:
        return None, None
    pa = max(0.01, min(0.99, float(a_rating["barthag"])))
    ph = max(0.01, min(0.99, float(h_rating["barthag"])))
    # Log5 base probability for home vs away
    denom = ph + pa - 2.0 * ph * pa
    base_h = (ph - ph * pa) / denom if denom > 0 else 0.5
    base_h = max(0.01, min(0.99, base_h))
    if not is_neutral:
        logit_h = math.log(base_h / (1.0 - base_h)) + 0.30
        base_h = 1.0 / (1.0 + math.exp(-logit_h))
    h_pct = round(base_h * 100.0, 1)
    a_pct = round((1.0 - base_h) * 100.0, 1)
    return a_pct, h_pct


# ---------------------------------------------------------------------------
# 7. MoneyPuck Free Daily NHL Expected Goals (xGF%) & Goalie GSAx
# ---------------------------------------------------------------------------
def fetch_moneypuck_nhl(now_est=None):
    """
    Fetches MoneyPuck's free daily NHL team 5v5 xGoals% and goalie GSAx (Goals Saved Above Expected).
    Returns {"teams": {abbr: {xgf_pct, gf, ga}}, "goalies": {norm_name: {name, gsax, games}}}.
    Fails safely to empty dicts if unreachable.
    """
    now_est = now_est or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-5)))
    season_start = now_est.year if now_est.month >= 9 else now_est.year - 1
    out = {"teams": {}, "goalies": {}}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BettingCurator/2.0)"}

    mp_to_espn = {
        "L.A": "LAK",
        "N.J": "NJD",
        "S.J": "SJS",
        "T.B": "TBL",
        "ARI": "UTA",
    }

    # 1. Teams 5v5 Expected Goals %
    for yr in (season_start, season_start - 1):
        try:
            url = f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/teams.csv"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200 or "xGoalsPercentage" not in resp.text:
                continue
            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                if str(row.get("situation", "")).lower() != "5on5":
                    continue
                ab = str(row.get("team") or row.get("name") or "").strip().upper()
                if not ab:
                    continue
                try:
                    xgf = float(row.get("xGoalsPercentage", 0) or 0)
                    if xgf <= 1.0:
                        xgf *= 100.0
                    gf = float(row.get("goalsFor", 0) or 0)
                    ga = float(row.get("goalsAgainst", 0) or 0)
                    rec = {"xgf_pct": round(xgf, 1), "gf": gf, "ga": ga}
                    out["teams"][ab] = rec
                    if ab in mp_to_espn:
                        out["teams"][mp_to_espn[ab]] = rec
                except Exception:
                    continue
            if out["teams"]:
                break
        except Exception:
            continue

    # 2. Goalies GSAx (xGoals - goals across all situations)
    for yr in (season_start, season_start - 1):
        try:
            url = f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/goalies.csv"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200 or "xGoals" not in resp.text:
                continue
            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                if str(row.get("situation", "")).lower() != "all":
                    continue
                g_name = str(row.get("name") or "").strip()
                if not g_name:
                    continue
                try:
                    xg = float(row.get("xGoals", 0) or 0)
                    ga = float(row.get("goals", 0) or 0)
                    gp = int(float(row.get("games_played", 0) or 0))
                    gsax = round(xg - ga, 2)
                    out["goalies"][_norm_exact(g_name)] = {"name": g_name, "gsax": gsax, "gp": gp}
                except Exception:
                    continue
            if out["goalies"]:
                break
        except Exception:
            continue

    return out


# ---------------------------------------------------------------------------
# 8. Self-Calibrating Pillar Weights from Shadow Grading Log
# ---------------------------------------------------------------------------
PILLAR_PATTERNS = {
    "mod_ml": r"ModFav ML",
    "bpi_ev": r"BPI/FPI \+",
    "torvik_ev": r"Torvik AdjEff",
    "l5_form": r"(?:Major L5 Form|L5 Edge|Hot L5)",
    "sharp_steam": r"Sharp Steam",
    "b2b_fatigue": r"(?:0d-Rest B2B|3-in-4 Fatigue)",
    "goalie_edge": r"(?:NHL Goalie Mismatch|Goalie GSAx)",
    "home_road_split": r"(?:Home/Road Split|Road Warrior Split)",
    "net_diff": r"Season Net-Diff",
    "moneypuck_xgf": r"MoneyPuck xGF%",
}


def compute_pillar_weights_from_rows(shadow_rows, min_total_graded=25, min_pillar_n=12):
    """
    Computes bounded Bayesian-shrunk weight multipliers [0.65, 1.25] for each Confidence Meter pillar
    from historical `Shadow Grading Log` rows (`[Date, Sport, Market, Selection, Odds, Conf%, Pillars, Execution, Result, FlatPL, ...]`).
    Returns (weights: dict[pillar_key -> float], summary_note: str).
    """
    stats = {k: {"w": 0, "l": 0} for k in PILLAR_PATTERNS}
    total_graded = 0

    for r in (shadow_rows or []):
        if not r or len(r) < 9:
            continue
        res_l = str(r[8]).strip().lower()
        if res_l not in ("won", "lost"):
            continue
        total_graded += 1
        pillars_txt = str(r[6] or "")
        for p_key, pat in PILLAR_PATTERNS.items():
            if re.search(pat, pillars_txt, flags=re.I):
                if res_l == "won":
                    stats[p_key]["w"] += 1
                else:
                    stats[p_key]["l"] += 1

    weights = {k: 1.0 for k in PILLAR_PATTERNS}
    if total_graded < min_total_graded:
        return weights, f"Calibration warming up ({total_graded}/{min_total_graded} graded shadow plays; using 1.00x neutral weights)"

    adjusted_notes = []
    prior_n = 10.0
    prior_wr = 0.55
    for p_key, st in stats.items():
        n = st["w"] + st["l"]
        if n < min_pillar_n:
            continue
        # Bayesian shrunk win rate toward 55% baseline
        shrunk_wr = (st["w"] + prior_n * prior_wr) / (n + prior_n)
        # Map +/- 10% win-rate deviation around 55% into [0.65, 1.25]
        mult = 1.0 + (shrunk_wr - prior_wr) * 2.5
        mult = round(max(0.65, min(1.25, mult)), 2)
        weights[p_key] = mult
        if abs(mult - 1.0) >= 0.05:
            raw_wr = round((st["w"] / n) * 100.0, 1)
            adjusted_notes.append(f"{p_key}:{mult:.2f}x ({st['w']}W-{st['l']}L {raw_wr}%)")

    if adjusted_notes:
        return weights, f"Self-Calibrated ({total_graded} shadow plays): " + ", ".join(adjusted_notes)
    return weights, f"Self-Calibrated ({total_graded} shadow plays): all active pillars near neutral (1.00x)"


def load_shadow_pillar_weights():
    """
    Loads `Shadow Grading Log` from Google Sheets (read-only) to compute self-calibrated pillar weights.
    Gracefully returns neutral 1.0x weights if credentials or gspread are unavailable.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        gcp_sa = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or ""
        sheet_id = (
            os.environ.get("SHEET_ID")
            or os.environ.get("MASTER_SHEET_ID")
            or os.environ.get("SPREADSHEET_ID")
            or os.environ.get("TRACKER_SHEET_ID")
            or os.environ.get("GOOGLE_SHEET_ID")
            or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
        )
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        if gcp_sa:
            creds = Credentials.from_service_account_info(json.loads(gcp_sa), scopes=scopes)
        elif os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        else:
            return {k: 1.0 for k in PILLAR_PATTERNS}, "Calibration neutral (no sheet credentials in local env)"

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet("Shadow Grading Log")
        rows = ws.get_all_values()[1:]
        return compute_pillar_weights_from_rows(rows)
    except Exception:
        return {k: 1.0 for k in PILLAR_PATTERNS}, "Calibration neutral (Shadow Grading Log not loaded)"
