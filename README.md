[SETUP_README.md](https://github.com/user-attachments/files/34990544/SETUP_README.md)
# Betting Curator Pipeline Upgrade ($100 to $1,000 Challenge)

## 1. Google Drive Resources
- **Master Tracker Sheet:** `Betting Tracker & Strategy Log - $100 to $1,000 Challenge`
- **Rolling 7-Day Intel Doc:** `Daily_Slate_Intel_Log`

> **Important:** Share `Daily_Slate_Intel_Log` (Editor access) with your existing Google Cloud Service Account email (`client_email` inside your `GCP_SERVICE_ACCOUNT_JSON` secret) so `scraper.py` and `curator.py` can read/write it.

## 2. Free Telegram Bot Setup (2 Minutes)
1. Open Telegram on your iPhone 17 Pro and message **@BotFather**.
2. Send `/newbot`, give it a name (e.g., `HardRockBet Curator`), and copy the **HTTP API Token** -> save as GitHub Secret `TELEGRAM_BOT_TOKEN`.
3. Message your new bot once (`hi`), then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in your browser to copy your numeric `chat.id` -> save as GitHub Secret `TELEGRAM_CHAT_ID`.

## 3. GitHub Repository Secrets (`iannuzzi8100/betting-curator`)
Add or verify these secrets under **Settings -> Secrets and variables -> Actions**:
- `INTEL_DOC_ID`: Document ID of `Daily_Slate_Intel_Log` (from its Google Docs URL)
- `MASTER_SHEET_ID`: Spreadsheet ID of `Betting Tracker & Strategy Log - $100 to $1,000 Challenge`
- `GCP_SERVICE_ACCOUNT_JSON`: Existing Google Service Account JSON
- `GEMINI_API_KEY`: Your Gemini / Argon API key
- `ODDS_API_KEY`: Free key from the-odds-api.com (for `hardrockbet` lines)
- `TELEGRAM_BOT_TOKEN`: Token from @BotFather
- `TELEGRAM_CHAT_ID`: Your personal Telegram numeric chat ID

## 4. Daily Automated Schedule (EST)
- **03:12 AM EST:** Existing `settler.py` grades finished games and locks daily bankroll & tier.
- **08:30 AM EST:** `scraper.py` scrapes Hard Rock Bet vs. sharp odds, ESPN breaking news/injuries, and transcripts from your 14 YouTube channels into `Daily_Slate_Intel_Log`, pruning entries older than 7 days.
- **09:12 AM EST:** `curator.py` runs the Argon analysis prompt against your rules, mistake audit, and rolling intel doc, then pushes the bulleted bet slip straight to your phone via Telegram.
