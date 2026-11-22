import os
import json
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

GCP_SA_KEY = os.environ.get("GCP_SA_KEY") or os.environ.get("GCP_SERVICE_ACCOUNT_JSON") or ""
SHEET_ID = (
    os.environ.get("SHEET_ID")
    or os.environ.get("MASTER_SHEET_ID")
    or os.environ.get("SPREADSHEET_ID")
    or os.environ.get("TRACKER_SHEET_ID")
    or os.environ.get("GOOGLE_SHEET_ID")
    or "1TClaBQb82kDz1ddgileMesO3IZ3ePrAp-8G-ScxYkP8"
)
SHEET_NAME = "Betting Tracker & Strategy Log - $100 to $1,000 Challenge"


def get_workbook():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if GCP_SA_KEY:
        creds = Credentials.from_service_account_info(json.loads(GCP_SA_KEY), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    gc = gspread.authorize(creds)
    try:
        return gc.open(SHEET_NAME)
    except Exception:
        return gc.open_by_key(SHEET_ID)


def bet_num(bet_id):
    try:
        return int(str(bet_id).strip().split("-")[1])
    except Exception:
        return 0


def parse_num(v):
    try:
        s = str(v).replace("$", "").replace(",", "").replace("+", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        return float(s)
    except Exception:
        return 0.0


def std_formulas_for_row(r):
    col_j = f'=IF(OR(ISBLANK(I{r}), ISBLANK(G{r}), I{r}="", G{r}=""), "", IF(G{r}>0, ROUND(I{r}*(1+G{r}/100), 2), ROUND(I{r}*(1+100/ABS(G{r})), 2)))'
    col_l = f'=IF(K{r}="Won", J{r}, IF(K{r}="Lost", 0, IF(K{r}="Push", I{r}, "")))'
    col_m = f'=IF(OR(K{r}="", K{r}="Pending"), "", IF(K{r}="Lost", -I{r}, IF(ISNUMBER(L{r}), L{r}-I{r}, "")))'
    col_n = f'=IF(OR(ISBLANK(I{r}), I{r}=""), "", ROUND(100 + SUM(M{r}:M), 2))'
    return col_j, col_l, col_m, col_n


def main():
    sh = get_workbook()
    ws = sh.worksheets()[0]

    disp = ws.get_all_values()
    raw_formulas = ws.get_all_values(value_render_option="FORMULA")

    header_idx = next((i for i, r in enumerate(disp) if "Bet ID" in r and "Result" in r), 15)
    header_row = header_idx + 1
    first_data_row = header_idx + 2

    rows_by_key = []
    bet_004_date = "2026-11-11"
    for i in range(header_idx + 1, len(disp)):
        r_disp = disp[i]
        if not r_disp or not r_disp[0].strip().startswith("BET-"):
            continue
        r_raw = raw_formulas[i] if i < len(raw_formulas) else r_disp
        row16 = [(r_raw[c] if c < len(r_raw) else "") for c in range(16)]
        bid = str(r_disp[0]).strip()
        sel = str(r_disp[5]).strip() if len(r_disp) > 5 else ""
        if bid == "BET-004" and len(r_disp) > 1 and r_disp[1].strip():
            bet_004_date = r_disp[1].strip()
        # Preserve whether L (11) or M (12) had a hardcoded manual cashout number (non-formula)
        is_formula_l = str(row16[11]).startswith("=") or str(row16[11]).strip() == ""
        is_formula_m = str(row16[12]).startswith("=") or str(row16[12]).strip() == ""
        rows_by_key.append({
            "bet_id": bid,
            "sel": sel,
            "row": row16,
            "is_formula_l": is_formula_l,
            "is_formula_m": is_formula_m,
        })

    # 1. Fix mislabeled BET-050 Toledo -> BET-003 with BET-004's date
    for item in rows_by_key:
        if item["bet_id"] == "BET-050" and "toledo" in item["sel"].lower():
            item["bet_id"] = "BET-003"
            item["row"][0] = "BET-003"
            item["row"][1] = bet_004_date
            print(f"Relabeled mislabeled Toledo BET-050 -> BET-003 (date={bet_004_date})")

    existing_ids = {item["bet_id"] for item in rows_by_key}
    existing_sels = " | ".join(item["sel"].lower() for item in rows_by_key)

    # 2. Restore missing BET-001 (-$7.00 Lost) and BET-002 (-$3.00 Lost)
    if "BET-001" not in existing_ids:
        rows_by_key.append({
            "bet_id": "BET-001",
            "sel": "Opening Challenge Ticket (Restored)",
            "row": ["BET-001", bet_004_date, "CFB", "Straight", "Core Tier 1", "Opening Challenge Ticket (Restored)", -110, "1.75U", 7.00, "", "Lost", "", "", "", "Restored after sheet reversal validation error (-$7.00)", ""],
            "is_formula_l": True,
            "is_formula_m": True,
        })
        print("Restored missing BET-001 (-$7.00 Lost)")

    if "BET-002" not in existing_ids:
        rows_by_key.append({
            "bet_id": "BET-002",
            "sel": "Opening Challenge Ticket #2 (Restored)",
            "row": ["BET-002", bet_004_date, "CFB", "Straight", "Core Tier 1", "Opening Challenge Ticket #2 (Restored)", -110, "0.75U", 3.00, "", "Lost", "", "", "", "Restored after sheet reversal validation error (-$3.00)", ""],
            "is_formula_l": True,
            "is_formula_m": True,
        })
        print("Restored missing BET-002 (-$3.00 Lost)")

    # 3. Restore missing BET-050 Bournemouth (+$5.55 Won) on 2026-11-21
    if "bournemouth" not in existing_sels:
        rows_by_key.append({
            "bet_id": "BET-050",
            "sel": "Bournemouth (Bournemouth vs Nottingham Forest • 10:00 AM EST)",
            "row": [
                "BET-050", "2026-11-21", "EPL", "Straight", "Core Tier 1",
                "Bournemouth (Bournemouth vs Nottingham Forest • 10:00 AM EST)",
                -110, "1.53U", 6.10, "", "Won", "", "", "",
                "Hard Rock Bet Ticket: Bournemouth (-110) | Stake: $6.10 | [Settled: Bournemouth won]",
                "WIN: Disciplined Straight / Floor Play",
            ],
            "is_formula_l": True,
            "is_formula_m": True,
        })
        print("Restored missing BET-050 Bournemouth (+$5.55 Won)")

    # 4. Add missed BET-054 Stanford TO WIN (-275, $8.00 -> $10.91, +$2.91 Won) on 2026-11-21
    if "stanford" not in existing_sels and "1093895900090204434" not in " ".join(str(x["row"][14]) for x in rows_by_key):
        rows_by_key.append({
            "bet_id": "BET-054",
            "sel": "Stanford TO WIN (Stanford @ Cal • 7:30 PM EST)",
            "row": [
                "BET-054", "2026-11-21", "CFB", "Straight", "Core Tier 1",
                "Stanford TO WIN (Stanford @ Cal • 7:30 PM EST)",
                -275, "2.00U", 8.00, "", "Won", "", "", "",
                "Hard Rock Bet Ticket: Stanford TO WIN (-275) | Stake: $8.00 (Hard Rock ID: 1093895900090204434) | [Settled: Stanford defeated Cal 44-28]",
                "WIN: Disciplined Straight / Floor Play",
            ],
            "is_formula_l": True,
            "is_formula_m": True,
        })
        print("Added missed BET-054 Stanford TO WIN (+$2.91 Won)")

    # 5. Deduplicate any accidental duplicate BET-IDs (keep first occurrence)
    deduped = []
    seen_ids = set()
    for item in rows_by_key:
        if item["bet_id"] in seen_ids:
            print(f"Skipping duplicate {item['bet_id']} ({item['sel']})")
            continue
        seen_ids.add(item["bet_id"])
        deduped.append(item)

    # Sort DESCENDING (newest-first: BET-057 at row 17 -> BET-001 at bottom)
    deduped.sort(key=lambda x: bet_num(x["bet_id"]), reverse=True)

    out_rows = []
    for idx, item in enumerate(deduped):
        r = first_data_row + idx
        row = list(item["row"][:16])
        while len(row) < 16:
            row.append("")
        col_j, col_l, col_m, col_n = std_formulas_for_row(r)
        row[9] = col_j
        if item["is_formula_l"]:
            row[11] = col_l
        if item["is_formula_m"]:
            row[12] = col_m
        row[13] = col_n
        out_rows.append(row)

    end_row = first_data_row + len(out_rows) - 1
    ws.update(range_name=f"A{first_data_row}:P{end_row}", values=out_rows, value_input_option="USER_ENTERED")

    # Clear any leftover trailing rows
    if len(disp) > end_row:
        clear_count = len(disp) - end_row
        ws.update(
            range_name=f"A{end_row + 1}:P{len(disp)}",
            values=[[""] * 16 for _ in range(clear_count)],
            value_input_option="USER_ENTERED",
        )

    # Ensure A5 uses anchored SUM(M{header_row}:M)
    ws.update_acell("A5", f"=ROUND(100 + SUM(M{header_row}:M), 2)")

    refreshed = ws.get_all_values()
    top_br = refreshed[first_data_row - 1][13] if len(refreshed) >= first_data_row else "N/A"
    a5_br = refreshed[4][0] if len(refreshed) > 4 else "N/A"
    print(f"Reconciliation complete! Total bets: {len(out_rows)} | Top row ({out_rows[0][0]}) Col N: {top_br} | Cell A5: {a5_br}")


if __name__ == "__main__":
    main()
