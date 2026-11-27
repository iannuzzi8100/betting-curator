import os
import re
import json
import datetime
import requests

import scraper
import curator
import quant_signals


LEAGUE_ESPN_MAP = {
    "CFB": [
        ("CFB", "football", "college-football", "80"),  # Division I FBS
        ("CFB", "football", "college-football", "81"),  # Division I FCS (e.g. FCS Playoffs)
    ],
    "NFL": [("NFL", "football", "nfl", None)],
    "NBA": [("NBA", "basketball", "nba", None)],
    "NHL": [("NHL", "hockey", "nhl", None)],
    "NCAAM": [("NCAAM", "basketball", "mens-college-basketball", "50")],
    "EPL": [("EPL", "soccer", "eng.1", None)],
    "UCL": [("UCL", "soccer", "uefa.champions", None)],
    "LALIGA": [("LALIGA", "soccer", "esp.1", None)],
}

LEAGUE_ODDS_KEY = {
    "CFB": "americanfootball_ncaaf",
    "NFL": "americanfootball_nfl",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
    "NCAAM": "basketball_ncaab",
    "EPL": "soccer_epl",
    "UCL": "soccer_uefa_champs_league",
    "LALIGA": "soccer_spain_la_liga",
}


def run_dry_run_preview(target_date_str, league_label="CFB", out_path="preview_output.md"):
    est_tz = datetime.timezone(datetime.timedelta(hours=-5))
    target_date = datetime.date.fromisoformat(target_date_str)
    target_ymd = target_date.strftime("%Y%m%d")
    lbl_u = str(league_label or "CFB").strip().upper()
    configs = LEAGUE_ESPN_MAP.get(lbl_u, LEAGUE_ESPN_MAP["CFB"])

    # Load current bankroll from docs/data.json (100% read-only; never touches Google Sheets/Docs/Telegram)
    settled_br = 54.04
    try:
        with open("docs/data.json", "r", encoding="utf-8") as f:
            d = json.load(f)
            if float(d.get("bankroll") or 0) > 0:
                settled_br = float(d["bankroll"])
    except Exception:
        pass

    quant_games = []
    marquee_meta = {}
    all_scored_games = []
    seen_event_ids = set()

    sim_target_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 12, 0, tzinfo=est_tz)
    pillar_weights, cal_note = quant_signals.load_shadow_pillar_weights()
    quant_games.append(f"- [CALIBRATION] {cal_note}")
    torvik_ratings = quant_signals.fetch_barttorvik_ratings(sim_target_dt) if lbl_u == "NCAAM" else {}
    moneypuck_data = quant_signals.fetch_moneypuck_nhl(sim_target_dt) if lbl_u == "NHL" else {"teams": {}, "goalies": {}}

    for label, sport, league, groups in configs:
        base_sb = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
        b2b_teams = set()
        games_in_3d = {}
        if label in ("NBA", "NHL", "NCAAM"):
            try:
                b2b_teams, games_in_3d = quant_signals.fetch_recent_schedule_density(
                    base_sb, sim_target_dt, groups=groups, lookback_days=3
                )
            except Exception:
                pass
        params = {"dates": target_ymd, "limit": 200}
        if groups:
            params["groups"] = groups
        try:
            events = requests.get(base_sb, params=params, timeout=15).json().get("events", [])
        except Exception as exc:
            print(f"ESPN scoreboard warning ({label} groups={groups}): {exc}", flush=True)
            continue

        div_tag = "FBS" if groups == "80" else ("FCS" if groups == "81" else label)
        for ev in events:
            ev_id = str(ev.get("id", ""))
            if not ev_id or ev_id in seen_event_ids:
                continue
            seen_event_ids.add(ev_id)

            status_state = ev.get("status", {}).get("type", {}).get("state", "")
            if status_state in ("in", "post"):
                continue

            time_est_str = "TBD EST"
            slot_tag = "[WINDOW 5: 6:30-9:00 PM PRIMETIME]"
            dt_est = None
            raw_date = ev.get("date", "")
            if raw_date:
                try:
                    dt_utc = datetime.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    dt_est = dt_utc.astimezone(est_tz)
                    if dt_est.date() != target_date:
                        continue
                    time_est_str = dt_est.strftime("%I:%M %p EST").lstrip("0")
                    hr_dec = dt_est.hour + dt_est.minute / 60.0
                    if hr_dec < 9.5:
                        slot_tag = "[WINDOW 1: 7:30 AM EARLY MORNING]"
                    elif hr_dec < 11.75:
                        slot_tag = "[WINDOW 2: 10:00 AM LATE MORNING]"
                    elif hr_dec < 14.75:
                        slot_tag = "[WINDOW 3: 12:00-2:30 PM EARLY AFTERNOON]"
                    elif hr_dec < 18.25:
                        slot_tag = "[WINDOW 4: 3:00-6:00 PM LATE AFTERNOON]"
                    elif hr_dec < 21.25:
                        slot_tag = "[WINDOW 5: 6:30-9:00 PM PRIMETIME]"
                    else:
                        slot_tag = "[WINDOW 6: 9:30 PM+ LATE NIGHT]"
                except Exception:
                    pass

            comps = ev.get("competitions", [])
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            h_name = home.get("team", {}).get("displayName", "")
            a_name = away.get("team", {}).get("displayName", "")
            h_abbr = home.get("team", {}).get("abbreviation", h_name)
            a_abbr = away.get("team", {}).get("abbreviation", a_name)
            matchup = f"{a_name} @ {h_name}"
            is_marquee, mq_reason = scraper._detect_marquee_game(label, ev, comp, home, away, dt_est)

            h_rec = next((r.get("summary", "") for r in home.get("records", []) if r.get("type") == "total"), "")
            a_rec = next((r.get("summary", "") for r in away.get("records", []) if r.get("type") == "total"), "")
            rec_str = f"({a_abbr} {a_rec} @ {h_abbr} {h_rec})" if (h_rec or a_rec) else ""

            tags = []
            odds_summary = []

            # Home vs. Away Splits
            split_info = quant_signals.extract_home_away_splits(home, away, h_abbr, a_abbr)
            if split_info.get("summary_str"):
                odds_summary.append(split_info["summary_str"])
            if split_info.get("tag"):
                tags.append(split_info["tag"])

            # Back-to-Back & 3-in-4 Density Fatigue
            a_b2b = a_name in b2b_teams
            h_b2b = h_name in b2b_teams
            density_info = {}
            if a_b2b and not h_b2b:
                tags.append(f"[⚠️ FATIGUE EDGE: {a_abbr} on Road Back-to-Back (0d rest) vs Rested {h_abbr}]")
            elif h_b2b and not a_b2b:
                tags.append(f"[⚠️ FATIGUE EDGE: {h_abbr} on Back-to-Back (0d rest) vs Rested {a_abbr}]")
            elif label in ("NBA", "NHL", "NCAAM") and games_in_3d:
                a_3in4 = games_in_3d.get(a_name, 0) >= 2
                h_3in4 = games_in_3d.get(h_name, 0) >= 2
                density_info = {a_abbr: a_3in4, h_abbr: h_3in4}
                if a_3in4 and not h_3in4:
                    tags.append(f"[⚠️ 3-IN-4 FATIGUE: {a_abbr} playing 3rd game in 4 nights vs Rested {h_abbr}]")
                elif h_3in4 and not a_3in4:
                    tags.append(f"[⚠️ 3-IN-4 FATIGUE: {h_abbr} playing 3rd game in 4 nights vs Rested {a_abbr}]")

            odds_list = comp.get("odds", [])
            h_ml_close = a_ml_close = None
            h_sp_close = None
            h_sp_odds = a_sp_odds = None
            t_close = t_under_odds = None
            h_bpi = a_bpi = None
            l5_parsed = {}
            injury_info = {}
            weather_info = {}
            net_diff_info = {}
            torvik_probs = (None, None)
            moneypuck_info = {}

            if odds_list:
                od = odds_list[0]
                details = od.get("details", "")
                ou = od.get("overUnder", "")
                if details or ou:
                    odds_summary.append(f"Spread: {details} | O/U: {ou}")
                ml = od.get("moneyline", {})
                h_ml_close = scraper._safe_int_odds(ml.get("home", {}).get("close", {}).get("odds"))
                a_ml_close = scraper._safe_int_odds(ml.get("away", {}).get("close", {}).get("odds"))
                h_ml_open = scraper._safe_int_odds(ml.get("home", {}).get("open", {}).get("odds"))
                if h_ml_close is not None and a_ml_close is not None:
                    odds_summary.append(f"ML: {a_abbr} ({a_ml_close:+d}) / {h_abbr} ({h_ml_close:+d})")
                if h_ml_open is not None and h_ml_close is not None:
                    imp_diff = (scraper.american_to_implied(h_ml_close) - scraper.american_to_implied(h_ml_open)) * 100.0
                    if abs(imp_diff) >= 3.5:
                        steamed_team = h_abbr if imp_diff > 0 else a_abbr
                        tags.append(f"[🔥 ML STEAM: Sharp action steamed {steamed_team} ML ({imp_diff:+.1f}% implied prob shift)]")

                ps = od.get("pointSpread", {})
                h_sp_close = scraper._safe_float_line(ps.get("home", {}).get("close", {}).get("line"))
                h_sp_open = scraper._safe_float_line(ps.get("home", {}).get("open", {}).get("line"))
                h_sp_odds = scraper._safe_int_odds(ps.get("home", {}).get("close", {}).get("odds"))
                a_sp_odds = scraper._safe_int_odds(ps.get("away", {}).get("close", {}).get("odds"))
                if h_sp_close is not None and h_sp_odds is not None and a_sp_odds is not None:
                    odds_summary.append(
                        f"Spread Prices: {a_abbr} {-h_sp_close:+.1f} ({a_sp_odds:+d}) / {h_abbr} {h_sp_close:+.1f} ({h_sp_odds:+d})"
                    )
                if h_sp_open is not None and h_sp_close is not None and abs(h_sp_close - h_sp_open) >= 1.0:
                    moved_toward = h_abbr if h_sp_close < h_sp_open else a_abbr
                    delta_sp = abs(h_sp_close - h_sp_open)
                    if delta_sp >= 2.5:
                        tags.append(
                            f"[⚠️ SPREAD ALREADY STEAMED ({delta_sp:.1f} pts toward {moved_toward}: {h_sp_open:+.1f} -> {h_sp_close:+.1f}) — DO NOT CHASE LATE]"
                        )
                    else:
                        tags.append(
                            f"[🔥 SPREAD STEAM: Moved {delta_sp:.1f} pts toward {moved_toward} (Open {h_abbr} {h_sp_open:+.1f} -> Now {h_sp_close:+.1f})]"
                        )

                tot = od.get("total", {})
                t_close = scraper._safe_float_line(tot.get("over", {}).get("close", {}).get("line"))
                t_open = scraper._safe_float_line(tot.get("over", {}).get("open", {}).get("line"))
                t_under_odds = scraper._safe_int_odds(tot.get("under", {}).get("close", {}).get("odds"))
                if t_open is not None and t_close is not None and abs(t_close - t_open) >= 1.0:
                    dir_lbl = "OVER" if t_close > t_open else "UNDER"
                    delta_t = abs(t_close - t_open)
                    if delta_t >= 3.5:
                        tags.append(
                            f"[⚠️ TOTAL ALREADY STEAMED ({delta_t:.1f} pts toward {dir_lbl}: {t_open} -> {t_close}) — DO NOT CHASE LATE]"
                        )
                    else:
                        tags.append(f"[🔥 TOTAL STEAM: Moved {delta_t:.1f} pts toward {dir_lbl} (Open {t_open} -> Now {t_close})]")

            # Skip unlined FCS games so we don't waste summary requests on games with no odds
            if not odds_list and groups == "81":
                continue

            # Bart Torvik NCAAM Adjusted Efficiency Win Probabilities
            if label == "NCAAM" and torvik_ratings:
                a_tr = quant_signals.lookup_torvik_team(torvik_ratings, a_name, away.get("team", {}).get("location", ""))
                h_tr = quant_signals.lookup_torvik_team(torvik_ratings, h_name, home.get("team", {}).get("location", ""))
                a_tp, h_tp = quant_signals.compute_torvik_matchup_probs(
                    a_tr, h_tr, is_neutral=bool(comp.get("neutralSite", False))
                )
                if a_tp is not None and h_tp is not None:
                    torvik_probs = (a_tp, h_tp)
                    odds_summary.append(
                        f"Torvik AdjEff: {a_abbr} {a_tp:.1f}% vs {h_abbr} {h_tp:.1f}%"
                    )

            # Fetch ESPN Summary (FPI WinProb, Injuries, Weather, NetDiff, L5 Form, MoneyPuck NHL)
            try:
                sum_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={ev_id}"
                sum_data = requests.get(sum_url, timeout=10).json()

                injury_info = quant_signals.extract_game_injury_impact(sum_data, label, a_abbr, h_abbr, a_name, h_name)
                for itag in injury_info.get("tags") or []:
                    tags.append(itag)

                weather_info = quant_signals.extract_game_weather(sum_data, label)
                if weather_info.get("tag"):
                    tags.append(weather_info["tag"])

                net_diff_info = quant_signals.extract_season_net_differential(
                    sum_data, home, away, label, a_abbr, h_abbr, a_rec, h_rec
                )
                if net_diff_info.get("summary_str"):
                    odds_summary.append(net_diff_info["summary_str"])
                for ntag in net_diff_info.get("tags") or []:
                    tags.append(ntag)

                pred = sum_data.get("predictor", {})
                h_proj = pred.get("homeTeam", {}).get("gameProjection")
                a_proj = pred.get("awayTeam", {}).get("gameProjection")
                if h_proj is not None and a_proj is not None:
                    h_bpi = float(h_proj)
                    a_bpi = float(a_proj)
                    odds_summary.append(f"ESPN BPI/FPI WinProb: {a_abbr} {a_bpi:.1f}% / {h_abbr} {h_bpi:.1f}%")
                    if label in ("NBA", "NFL", "CFB") and h_ml_close is not None and a_ml_close is not None:
                        imp_h = scraper.american_to_implied(h_ml_close)
                        imp_a = scraper.american_to_implied(a_ml_close)
                        if (imp_h + imp_a) > 0:
                            mkt_h = scraper.power_devig_prob(imp_h, imp_a) * 100.0
                            mkt_a = scraper.power_devig_prob(imp_a, imp_h) * 100.0
                            steam_str = " ".join(tags)
                            if 3.5 <= (h_bpi - mkt_h) <= 18.0 and -150 <= h_ml_close <= 110 and f"toward {a_abbr}" not in steam_str:
                                ev_h = round(((h_bpi / 100.0) * scraper.american_to_decimal(h_ml_close) - 1.0) * 100.0, 2)
                                if ev_h >= 2.5:
                                    tags.append(f"[📊 BPI/FPI +EV (Sweet-Spot): {h_abbr} ML ({h_ml_close:+d}) | BPI Win: {h_bpi:.1f}% vs PowerNoVig: {mkt_h:.1f}% | EV: +{ev_h}%]")
                            elif 3.5 <= (a_bpi - mkt_a) <= 18.0 and -150 <= a_ml_close <= 110 and f"toward {h_abbr}" not in steam_str:
                                ev_a = round(((a_bpi / 100.0) * scraper.american_to_decimal(a_ml_close) - 1.0) * 100.0, 2)
                                if ev_a >= 2.5:
                                    tags.append(f"[📊 BPI/FPI +EV (Sweet-Spot): {a_abbr} ML ({a_ml_close:+d}) | BPI Win: {a_bpi:.1f}% vs PowerNoVig: {mkt_a:.1f}% | EV: +{ev_a}%]")

                for tb in sum_data.get("lastFiveGames", []):
                    t_info = tb.get("team", {})
                    t_abbr = t_info.get("abbreviation", "")
                    t_id = str(t_info.get("id", ""))
                    evts_5 = tb.get("events", [])[:5]
                    if not t_abbr or not evts_5:
                        continue
                    w5 = l5 = 0
                    margins = []
                    for g5 in evts_5:
                        gr = str(g5.get("gameResult", "")).upper()
                        if gr == "W":
                            w5 += 1
                        elif gr == "L":
                            l5 += 1
                        try:
                            hs = int(g5.get("homeTeamScore", 0) or 0)
                            as_ = int(g5.get("awayTeamScore", 0) or 0)
                            margins.append((hs - as_) if str(g5.get("homeTeamId", "")) == t_id else (as_ - hs))
                        except Exception:
                            pass
                    avg_m = round(sum(margins) / len(margins), 1) if margins else 0.0
                    l5_parsed[t_abbr] = (w5, l5, avg_m)
                if len(l5_parsed) == 2 and a_abbr in l5_parsed and h_abbr in l5_parsed:
                    aw, al, am = l5_parsed[a_abbr]
                    hw, hl, hm = l5_parsed[h_abbr]
                    odds_summary.append(f"L5 Form: {a_abbr} {aw}-{al} ({am:+.1f}) vs {h_abbr} {hw}-{hl} ({hm:+.1f})")
                    if (hw - aw) >= 2 and (hm - am) >= 4.5:
                        tags.append(f"[📈 FORM EDGE: {h_abbr} L5 {hw}-{hl} ({hm:+.1f} avg margin) vs {a_abbr} L5 {aw}-{al} ({am:+.1f})]")
                    elif (aw - hw) >= 2 and (am - hm) >= 4.5:
                        tags.append(f"[📈 FORM EDGE: {a_abbr} L5 {aw}-{al} ({am:+.1f} avg margin) vs {h_abbr} L5 {hw}-{hl} ({hm:+.1f})]")

                if label == "NHL" and moneypuck_data:
                    mp_teams = moneypuck_data.get("teams") or {}
                    mp_goalies = moneypuck_data.get("goalies") or {}
                    xgf_pair = {}
                    gsax_pair = {}
                    a_mp_t = mp_teams.get(str(a_abbr).upper())
                    h_mp_t = mp_teams.get(str(h_abbr).upper())
                    if a_mp_t and h_mp_t:
                        xgf_pair[a_abbr] = a_mp_t["xgf_pct"]
                        xgf_pair[h_abbr] = h_mp_t["xgf_pct"]
                        odds_summary.append(f"MoneyPuck 5v5 xGF%: {a_abbr} {a_mp_t['xgf_pct']:.1f}% vs {h_abbr} {h_mp_t['xgf_pct']:.1f}%")
                    a_prob = (away.get("probables") or [{}])[0].get("athlete", {}).get("displayName", "")
                    h_prob = (home.get("probables") or [{}])[0].get("athlete", {}).get("displayName", "")
                    if a_prob and h_prob:
                        a_mg = mp_goalies.get(quant_signals._norm_exact(a_prob))
                        h_mg = mp_goalies.get(quant_signals._norm_exact(h_prob))
                        if a_mg and h_mg:
                            gsax_pair[a_abbr] = a_mg["gsax"]
                            gsax_pair[h_abbr] = h_mg["gsax"]
                            odds_summary.append(f"Goalie GSAx: {a_abbr} {a_mg['gsax']:+.1f} vs {h_abbr} {h_mg['gsax']:+.1f}")
                    if xgf_pair or gsax_pair:
                        moneypuck_info = {"xgf": xgf_pair, "gsax": gsax_pair}
            except Exception:
                pass

            extra_signals = {
                "split_info": split_info,
                "injury_info": injury_info,
                "weather_info": weather_info,
                "net_diff_info": net_diff_info,
                "torvik_probs": torvik_probs,
                "moneypuck_info": moneypuck_info,
                "density_info": density_info,
            }
            conf_tag = ""
            try:
                conf_tag = scraper._compute_matchup_confidence(
                    label=label, a_abbr=a_abbr, h_abbr=h_abbr, a_rec=a_rec, h_rec=h_rec,
                    a_ml=a_ml_close, h_ml=h_ml_close, h_sp=h_sp_close,
                    a_sp_odds=a_sp_odds, h_sp_odds=h_sp_odds,
                    a_bpi=a_bpi, h_bpi=h_bpi, l5_parsed=l5_parsed, tags=tags,
                    extra_signals=extra_signals, pillar_weights=pillar_weights,
                )
                tags.append(conf_tag)
            except Exception:
                pass

            fg_score = 0
            fg_pick = "N/A"
            fg_reasons = ""
            if conf_tag:
                m_fg = re.search(r"\[CONFIDENCE METER:\s*(\d{2,3})%\s*\|[^|]+\|\s*([^->]+?)\s*->\s*Top Play:\s*([^\]]+)\]", conf_tag)
                if m_fg:
                    fg_score = int(m_fg.group(1))
                    fg_top = m_fg.group(3).strip()
                    m_pr = re.match(r"^(.+?\([+-]\d{3,4}\))\s*\((.+)\)$", fg_top)
                    if m_pr:
                        fg_pick = m_pr.group(1).strip()
                        fg_reasons = m_pr.group(2).strip()
                    else:
                        fg_pick = fg_top
                    if a_abbr and re.match(rf"^{re.escape(a_abbr)}\b", fg_pick):
                        fg_pick = re.sub(rf"^{re.escape(a_abbr)}\b", a_name, fg_pick, count=1)
                    elif h_abbr and re.match(rf"^{re.escape(h_abbr)}\b", fg_pick):
                        fg_pick = re.sub(rf"^{re.escape(h_abbr)}\b", h_name, fg_pick, count=1)

            if is_marquee:
                tags.insert(0, f"[MARQUEE: {mq_reason}]")
                mq_entry = {
                    "label": label, "matchup": matchup, "a_name": a_name, "h_name": h_name,
                    "a_abbr": a_abbr, "h_abbr": h_abbr, "slot_tag": slot_tag, "time_est": time_est_str,
                    "reason": mq_reason, "rec_str": rec_str, "odds_summary": " | ".join(odds_summary),
                    "weather": weather_info, "candidates": [],
                }
                tags_blob = " ".join(tags)
                if conf_tag and "DO NOT CHASE LATE" not in tags_blob:
                    m_fo = re.search(r"\(([+-]\d{3,4})\)", fg_pick)
                    fg_odds = int(m_fo.group(1)) if m_fo else None
                    if fg_odds is not None and -155 <= fg_odds <= 130 and "EXCEEDS" not in fg_pick:
                        mq_entry["candidates"].append({
                            "score": fg_score, "pick": fg_pick, "market": "Full Game", "reasons": fg_reasons,
                        })
                if (
                    label in ("NFL", "CFB")
                    and weather_info.get("severe")
                    and t_close is not None
                    and t_under_odds is not None
                    and -155 <= t_under_odds <= 130
                    and "TOTAL ALREADY STEAMED" not in tags_blob
                ):
                    w_score = 54
                    w_reasons = []
                    w_mph = float(weather_info.get("wind_mph") or 0.0)
                    if w_mph >= 18.0:
                        w_score += 14
                        w_reasons.append(f"Severe Wind {int(round(w_mph))}mph (+14)")
                    elif w_mph >= 15.0:
                        w_score += 10
                        w_reasons.append(f"High Wind {int(round(w_mph))}mph (+10)")
                    else:
                        w_score += 8
                        w_reasons.append("Severe Outdoor Weather (+8)")
                    if weather_info.get("is_precip"):
                        w_score += 6
                        w_reasons.append("Rain/Snow Precip (+6)")
                    if "TOTAL STEAM: Moved" in tags_blob and "toward UNDER" in tags_blob:
                        w_score += 10
                        w_reasons.append("Sharp Under Steam (+10)")
                    w_score += 6 if (-145 <= t_under_odds <= 110) else 3
                    mq_entry["candidates"].append({
                        "score": min(96, w_score),
                        "pick": f"Under {t_close} ({t_under_odds:+d})",
                        "market": "Weather Total",
                        "reasons": ", ".join(w_reasons),
                    })
                marquee_meta[f"{label}:{matchup}"] = mq_entry

            all_scored_games.append({
                "div": div_tag, "time_est": time_est_str, "matchup": matchup, "rec": rec_str,
                "marquee": mq_reason if is_marquee else "", "score": fg_score, "pick": fg_pick,
                "reasons": fg_reasons, "chased": "DO NOT CHASE LATE" in " ".join(tags),
                "odds_summary": " | ".join(odds_summary),
                "weather_tag": weather_info.get("tag", ""),
                "injury_tags": injury_info.get("tags") or [],
            })

            if odds_summary or tags:
                quant_games.append(
                    f"- [{label} QUANT] {slot_tag} ({time_est_str}) {matchup} {rec_str} -> {' | '.join(odds_summary)}"
                    + ((" -> " + " ".join(tags)) if tags else "")
                )

    # Augment marquee games with Odds API sharp bulk + 1H check if ODDS_API_KEY is available
    sport_key = LEAGUE_ODDS_KEY.get(lbl_u)
    odds_api_note = "ESPN Quantitative Engine only (ODDS_API_KEY not set or quota low)"
    if scraper.ODDS_API_KEY and sport_key:
        try:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            resp = requests.get(
                url,
                params={
                    "apiKey": scraper.ODDS_API_KEY,
                    "markets": scraper.MARKETS_BY_SPORT.get(sport_key, "h2h,spreads,totals"),
                    "oddsFormat": "american",
                    "bookmakers": "hardrockbet," + ",".join(scraper.SHARP_BOOK_KEYS),
                },
                timeout=15,
            )
            if resp.status_code == 200:
                rem_quota = int(resp.headers.get("x-requests-remaining", "0") or 0)
                odds_api_note = f"The Odds API live Hard Rock vs Sharp check included (remaining quota: {rem_quota})"
                for g in resp.json():
                    away = g.get("away_team")
                    home = g.get("home_team")
                    mq_entry = scraper._find_marquee_entry(marquee_meta, sport_key, away, home)
                    if not mq_entry:
                        continue
                    books = g.get("bookmakers", [])
                    hr_book = next((b for b in books if b["key"] == "hardrockbet"), None)
                    sharp_books = [b for b in books if b["key"] in scraper.SHARP_BOOK_KEYS]
                    if not hr_book or not sharp_books:
                        continue
                    has_pin = any(b["key"] == "pinnacle" for b in sharp_books)
                    for m in hr_book.get("markets", []):
                        m_key = m["key"]
                        for o in m.get("outcomes", []):
                            o_name, o_pt, o_price = o["name"], o.get("point"), o["price"]
                            if o_price < -155 or o_price > 130:
                                continue
                            fps, stales = [], []
                            for sb in sharp_books:
                                sb_m = next((sm for sm in sb.get("markets", []) if sm["key"] == m_key), None)
                                if not sb_m or len(sb_m.get("outcomes", [])) != 2:
                                    continue
                                s_same = next((x for x in sb_m["outcomes"] if x["name"] == o_name), None)
                                s_other = next((x for x in sb_m["outcomes"] if x["name"] != o_name), None)
                                if not s_same or not s_other:
                                    continue
                                if s_same.get("point") == o_pt:
                                    fp, _, _ = scraper.compute_novig_ev(o_price, s_same["price"], s_other["price"])
                                    if fp > 0:
                                        fps.append(fp / 100.0)
                                elif o_pt is not None and s_same.get("point") is not None and o_price >= -125:
                                    if (m_key == "spreads" and o_pt > s_same["point"]) or (
                                        m_key == "totals" and ((o_name == "Over" and o_pt < s_same["point"]) or (o_name == "Under" and o_pt > s_same["point"]))
                                    ):
                                        stales.append(sb["key"])
                            ev_v = 0.0
                            if fps:
                                avg_fp = sum(fps) / len(fps)
                                ev_v = round(((avg_fp * scraper.american_to_decimal(o_price)) - 1.0) * 100.0, 2)
                            if ev_v >= 1.5 or len(stales) >= 2:
                                max_lay = -7.5 if lbl_u == "CFB" else -6.5
                                if not (m_key == "spreads" and o_pt is not None and o_pt < max_lay):
                                    cs, cr = scraper._score_market_candidate_confidence(
                                        ev_pct=ev_v, has_line_edge=len(stales) >= 1, stale_book_count=len(stales),
                                        is_pinnacle=has_pin, n_sharp_books=max(len(fps), len(stales)), hr_price=o_price,
                                    )
                                    if cs > 0:
                                        pk = f"{o_name} ML ({o_price:+d})" if m_key == "h2h" else (f"{o_name} {o_pt:+.1f} ({o_price:+d})" if m_key == "spreads" else f"{o_name} {o_pt} ({o_price:+d})")
                                        mq_entry["candidates"].append({"score": cs, "pick": pk, "market": f"Sharp {m_key.upper()}", "reasons": ", ".join(cr)})
        except Exception as exc:
            odds_api_note = f"Odds API skipped: {exc}"

    marquee_lines = scraper._build_marquee_best_bet_lines(marquee_meta)
    intel_text = "\n".join(marquee_lines + quant_games)

    # Build deterministic Tier 1 & Tier 2 (>=78%) mock-up slate as of 7:00 AM EST on target_date
    sim_now = datetime.datetime(target_date.year, target_date.month, target_date.day, 7, 0, tzinfo=est_tz)
    ledger_meta = {
        "settled": settled_br,
        "peak": max(100.0, settled_br),
        "is_drawdown": settled_br < 75.0,
        "pending_total": 0.0,
        "available": settled_br,
        "max_cap": round(settled_br * (0.40 if settled_br < 75.0 else 0.58), 2),
        "remaining_cap": round(settled_br * (0.40 if settled_br < 75.0 else 0.58), 2),
        "pending_lines": [],
    }
    mock_slate = curator._build_deterministic_curated_slate(
        today_str=target_date_str,
        slate_header_tag=f"DRY-RUN MOCKUP — {lbl_u} Division I Tier 1 & 2 Slate",
        intel_text=intel_text,
        ledger_meta=ledger_meta,
        est_now=sim_now,
        dow=target_date.weekday(),
    )

    tier12_all = sorted([g for g in all_scored_games if g["score"] >= 78 and not g["chased"]], key=lambda x: (-x["score"], x["time_est"]))
    near_misses = sorted([g for g in all_scored_games if 70 <= g["score"] < 78 and not g["chased"]], key=lambda x: (-x["score"], x["time_est"]))

    md = [
        f"# Dry-Run Preview: {target_date_str} {lbl_u} (All Division I)",
        f"- **Total D1 Games Scanned:** {len(all_scored_games)} (FBS + lined FCS)",
        f"- **Marquee / National TV / Top-25 Games Detected:** {len(marquee_meta)}",
        f"- **Data Sources:** ESPN Free Quantitative Engine (FPI + L5 Form + Line Steam) | {odds_api_note}",
        f"- **Sizing Base:** ${settled_br:.2f} bankroll (Drawdown Brake 12% single / 40% concurrent)",
        "",
        "## 1. Mock-Up Curated Slate (Strict Tier 1 & Tier 2 >= 78%, Governed by Half-Kelly & Exposure Cap)",
        "```",
        mock_slate,
        "```",
        "",
        "## 2. All Tier 1 & Tier 2 (>= 78%) Games Across Full D1 Slate",
    ]
    if not tier12_all:
        md.append("- None cleared 78%.")
    else:
        for g in tier12_all:
            mq_b = f" [MARQUEE: {g['marquee']}]" if g["marquee"] else ""
            md.append(f"- **{g['score']}%** | {g['time_est']} | {g['matchup']} {g['rec']}{mq_b} -> **{g['pick']}** | {g['reasons']}")

    md.append("")
    md.append("## 3. Marquee / Top-25 / National TV Games Scan Summary")
    if not marquee_lines:
        md.append("- None detected.")
    else:
        md.extend(marquee_lines)

    md.append("")
    md.append("## 4. Filtered Out Near-Misses (70–77% — Hidden Under Tier 1 & 2 Rule)")
    for g in near_misses[:15]:
        mq_b = f" [MARQUEE: {g['marquee']}]" if g["marquee"] else ""
        md.append(f"- {g['score']}% | {g['time_est']} | {g['matchup']} {g['rec']}{mq_b} -> {g['pick']} ({g['reasons']})")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"Wrote dry-run preview ({len(all_scored_games)} games) to {out_path}", flush=True)


if __name__ == "__main__":
    t_date = os.environ.get("PREVIEW_DATE", "").strip()
    if not t_date:
        est_tz = datetime.timezone(datetime.timedelta(hours=-5))
        t_date = (datetime.datetime.now(est_tz).date() + datetime.timedelta(days=1)).isoformat()
    t_league = os.environ.get("PREVIEW_LEAGUE", "CFB").strip().upper() or "CFB"
    run_dry_run_preview(t_date, t_league, out_path="preview_output.md")
