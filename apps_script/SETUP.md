# Instant Live Bet Tracker Setup (3 Minutes)

This replaces slow/unreliable GitHub Actions Telegram polling with a **100% free, instant, always-on** backend bound directly to your Google Sheet.

Once set up, you get:
- **1-Tap "✓ Log Bet"** on every curated play inside your mobile dashboard (~1 second write + instant refresh)
- **Manual "+ Log Bet" & "📸 Upload Screenshot"** inside the dashboard
- **1-Tap Edit / Cash Out / Void** on every pending ticket
- **Instant Telegram Webhook (~2–4 seconds)** for screenshots, text commands, and **1-tap inline buttons** under every curated slip
- **Optional iPhone Share Sheet Shortcut** to share any Hard Rock Bet screenshot straight from the app

---

## Step 1: Paste `Code.gs` into Your Google Sheet
1. Open your Google Sheet: **`Betting Tracker & Strategy Log - $100 to $1,000 Challenge`**
2. Click **Extensions → Apps Script**
3. Delete any default `myFunction()` code in `Code.gs`, and paste the full contents of [`apps_script/Code.gs`](./Code.gs)
4. Click **Save** (Cmd/Ctrl + S)

---

## Step 2: Set Script Properties (Secrets)
In the Apps Script editor, click the **⚙️ Project Settings** icon (left sidebar) → scroll down to **Script Properties** → click **Add script property** for each:

| Property | Value |
|---|---|
| `DASHBOARD_PIN` | Any secret passcode you pick (e.g. `8100-hrb-live`) |
| `TELEGRAM_BOT_TOKEN` | Your existing bot token from GitHub Secrets |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram chat ID |
| `GEMINI_API_KEY` | Your existing Gemini API key |

Click **Save script properties**.

---

## Step 3: Deploy as a Web App
1. Click **Deploy → New deployment** (top right)
2. Click the gear icon next to *Select type* → choose **Web app**
3. Set:
   - **Description:** `Betting Curator Live API v1`
   - **Execute as:** `Me`
   - **Who has access:** `Anyone`
4. Click **Deploy** → authorize when Google prompts (*Advanced → Go to ... (unsafe)* is normal for personal scripts)
5. Copy the **Web app URL** (`https://script.google.com/macros/s/.../exec`)

---

## Step 4: Connect Your Mobile Dashboard (10 Seconds)
1. Open your mobile dashboard on your iPhone (or desktop)
2. Tap **⚙️ Live Sync** in the top-right header
3. Paste your **Web app URL** and your **`DASHBOARD_PIN`** → tap **Save & Connect**
4. The header pill turns green (**`● LIVE SHEET`**). Your dashboard now reads & writes your Google Sheet directly in real time!

---

## Step 5: Enable Instant Telegram Webhook (1 Click)
1. Go back to Apps Script → **⚙️ Project Settings → Script Properties**
2. Add one more property:
   - `WEB_APP_URL` = the exact `/exec` URL you copied in Step 3
3. Switch back to the **Editor (`<>`)**, select **`setupTelegramWebhook`** from the function dropdown at the top, and click **Run**
4. Check the execution log: you should see `"ok":true, "description":"Webhook was set"`

That's it! From now on:
- Sending a screenshot or text to your Telegram bot logs it in **~2–4 seconds**
- Tapping any **✅ Log #1** inline button under a curated Telegram slip logs it in **~1–2 seconds**
- `bet_logger.py` in GitHub Actions automatically detects the active webhook and steps out of the way cleanly

*(If you ever want to switch back to polling, just run `removeTelegramWebhook` in Apps Script.)*

---

## Bonus: iPhone Share Sheet Shortcut ("Log Bet Screenshot")
Want to share a screenshot straight from Hard Rock Bet without even opening Telegram?
1. Open the **Shortcuts** app on your iPhone → tap **+** → name it **`Log Hard Rock Bet`**
2. Tap the **ⓘ** info icon → toggle **Show in Share Sheet** → set *Receive* to **Images**
3. Add these 4 actions:
   1. **Base64 Encode** → *Encode* `Shortcut Input` (*Line Breaks: None*)
   2. **Get Contents of URL**:
      - **URL:** your `/exec` Web app URL
      - **Method:** `POST`
      - **Headers:** `Content-Type` = `text/plain`
      - **Request Body:** `JSON` (or `File` with raw JSON text):
        ```json
        {
          "pin": "YOUR_DASHBOARD_PIN",
          "action": "log_screenshot",
          "mime_type": "image/jpeg",
          "image_base64": "<Base64 Encoded variable>"
        }
        ```
   3. **Get Dictionary Value** for key `ok` from `Contents of URL`
   4. **Show Notification** → `Bet synced to Live Tracker!`
4. Next time you place a bet in Hard Rock Bet: screenshot → tap Share → **Log Hard Rock Bet**!
