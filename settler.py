import time
from google.genai import errors

def grade_pending_bets(pending_bets, final_games):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are a deterministic sportsbook settlement auditor.
Verify each pending bet against the official ESPN scoreboard results provided below.

RULES:
1. Check EVERY leg of a parlay or Same Game Parlay (SGP).
2. A parlay/SGP is "Won" ONLY if EVERY leg has completed AND won.
3. A parlay/SGP is "Lost" if ANY single leg has completed AND lost (even if other legs are still in progress).
4. Keep the status "Pending" if any required game is not yet completed ("completed": false) and no leg has lost yet, or if a game cannot be matched in the scoreboard data.
5. For totals (e.g., "Under 57.5"), compare "total_points" (away_score + home_score) strictly against the line.
6. For moneylines (e.g., "Toledo ML", "Pennsylvania ML"), verify the selected team's score is strictly greater than the opponent's score.

PENDING BETS:
{json.dumps(pending_bets, indent=2)}

ESPN SCOREBOARD DATA:
{json.dumps(final_games, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "settlements": [
    {{
      "row_index": 17,
      "bet_id": "BET-003",
      "result": "Won",
      "verification_note": "Toledo defeated UMass 38-14 (52 total pts < 57.5)."
    }}
  ]
}}
"""

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview"
    ]

    response = None
    for attempt, model_name in enumerate(models_to_try, start=1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            break
        except errors.ServerError as e:
            print(f"Attempt {attempt} ({model_name}) hit 503 overload: {e}. Retrying in 10s...", flush=True)
            time.sleep(10)

    if response is None:
        print("All model attempts experienced 503 overload. Skipping settlement until next run.", flush=True)
        return []

    try:
        return json.loads(response.text).get("settlements", [])
    except Exception as e:
        print(f"Error parsing Gemini settlement response: {e}", flush=True)
        print("Raw model response:", response.text, flush=True)
        return []
