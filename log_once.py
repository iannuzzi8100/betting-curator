import bet_logger


def main():
    sh = bet_logger.get_workbook()
    ws = sh.worksheets()[0]
    all_vals, header_idx, pending_rows, _ = bet_logger.get_current_sheet_state(ws)

    ticket_id = "6150930737825710359"
    sel = "Liverpool ML (Liverpool vs Manchester United • 11:30 AM EST)"
    odds = -135
    stake = 22.00
    date_str = "2026-11-22"

    existing_text = "\n".join(" | ".join(r) for r in all_vals[header_idx + 1 :])
    if ticket_id in existing_text:
        print(f"Ticket {ticket_id} already logged; skipping.")
        return

    # If a 2026-11-22 pending Liverpool bet was already logged via 1-tap button, update it with exact ticket details
    for p in pending_rows:
        if p.get("date") == date_str and "liverpool" in str(p.get("selections", "")).lower():
            r_idx = p["row_index"]
            b_id = p["bet_id"]
            notes = bet_logger.apply_actions_to_sheet(
                ws,
                {
                    "updates": [
                        {
                            "row_index": r_idx,
                            "bet_id": b_id,
                            "sport": "EPL",
                            "selections": sel,
                            "odds": odds,
                            "stake": stake,
                        }
                    ]
                },
            )
            cur_note = all_vals[r_idx - 1][14] if len(all_vals[r_idx - 1]) > 14 else ""
            if ticket_id not in cur_note:
                ws.update_acell(f"O{r_idx}", f"Hard Rock Bet Ticket: {sel} ({odds:+d}) | Stake: ${stake:.2f} (Hard Rock ID: {ticket_id})")
            print("Updated existing pending Liverpool ticket:", notes)
            return

    notes = bet_logger.apply_actions_to_sheet(
        ws,
        {
            "tickets": [
                {
                    "date": date_str,
                    "sport": "EPL",
                    "bet_type": "Straight",
                    "selections": sel,
                    "odds": odds,
                    "stake": stake,
                    "ticket_id": ticket_id,
                }
            ]
        },
    )
    print("Logged Liverpool ticket:", notes)


if __name__ == "__main__":
    main()
