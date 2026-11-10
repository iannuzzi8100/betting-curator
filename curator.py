# --- 3. GEMINI REASONING & RESEARCH ENGINE ---
def run_ai_curation(board):
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are an expert sports quantitative analyst managing a strict bankroll challenge from $100 to $1,000.
Current Bankroll Tier: Tier 1 ($100 - $249.99).
Base Unit (1U): ${BASE_UNIT:.2f}

STRATEGY CONSTRAINTS:
1. Play 1 (Core Tier 1): Stake = ${CORE_STAKE:.2f} (1.75U). Target Odds: -135 to -110.
   - Primary high-probability bankroll builder.
   - Correlated SGP (Heavy ML Favorite -250 to -600 + padded alternate under +7 to +10 pts) OR an elite high-floor player prop / alt line with a proven 75%+ hit rate and dominant matchup advantage.
2. Play 2 (Multi-Window Tier 2): Stake = ${SECONDARY_STAKE:.2f} (0.75U). Target Odds: +160 to +250.
   - 2-to-3 leg parlay across different games or sports (can include moneylines or props).
3. ABSOLUTE EXCLUSION: Never bet on or against Kentucky/Wildcats.
4. Total Daily Risk: Max ${CORE_STAKE + SECONDARY_STAKE:.2f} across both plays combined.

Here is the upcoming 36-hour slate scraped from ESPN:
{json.dumps(board, indent=2)}

TASK:
Analyze the board. Leverage current team injuries, pace, defense vs position (DvP), and matchup trends.
Select the optimal Play 1 (Core Tier 1) and Play 2 (Tier 2 Parlay).

Respond ONLY with valid JSON matching this exact structure:
{{
  "plays": [
    {{
      "play_type": "Core Tier 1 (SGP)",
      "matchup": "Away @ Home",
      "kickoff": "YYYY-MM-DD HH:MM UTC",
      "consensus_total": "49.5",
      "target_under": "Under 56.5",
      "favorite": "Team Name",
      "favorite_ml": "-300",
      "target_combined_odds": "-120",
      "rec_stake": {CORE_STAKE},
      "deep_dive_analysis": "Comprehensive statistical reasoning, matchup trench analysis, pace, and hit rate justification."
    }},
    {{
      "play_type": "Multi-Window Tier 2 (Cross-Sport Parlay)",
      "matchup": "Multiple Slates",
      "kickoff": "YYYY-MM-DD HH:MM UTC",
      "consensus_total": "N/A",
      "target_under": "N/A",
      "favorite": "Leg 1 + Leg 2",
      "favorite_ml": "Multiple",
      "target_combined_odds": "+185",
      "rec_stake": {SECONDARY_STAKE},
      "deep_dive_analysis": "Detailed rationale for each leg, usage trends, and why this parlay holds an edge."
    }}
  ]
}}
"""

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )

    try:
        return json.loads(response.text).get("plays", [])
    except Exception as e:
        print(f"Error parsing Gemini JSON response: {e}")
        print("Raw model response:", response.text)
        return []
