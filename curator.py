import os
import json
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google import genai

# Environment / GitHub Secrets
GCP_SA_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
SHEET_ID = os.environ.get("MASTER_SHEET_ID", "")
INTEL_DOC_ID = os.environ.get("INTEL_DOC_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_google_clients():
    creds_dict = json.loads(GCP_SA_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/documents",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    docs_service = build("docs", "v1", credentials=creds)
    return gc, docs_service


def read_google_doc_text(docs_service, doc_id):
    doc = docs_service.documents().get(documentId=doc_id).execute()
    chunks = []
    for elem in doc.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if para:
            text = "".join(r.get("textRun", {}).get("content", "") for r in para.get("elements", []))
            chunks.append(text)
    return "".join(chunks)


def read_tracker_context(gc):
    sh = gc.open_by_key(SHEET_ID)
    tracker_ws = sh.worksheet("Challenge Tracker ($100-$1k)")
    rules_ws = sh.worksheet("Unit & Tier Rules")
    audit_ws = sh.worksheet("Strategy Edge & Mistake Audit")

    tracker_rows = tracker_ws.get_all_values()
    rules_rows = rules_ws.get_all_values()
    audit_rows = audit_ws.get_all_values()

    summary_block = "\n".join([" | ".join(r[:15]) for r in tracker_rows[:12]])
    recent_bets = "\n".join([" | ".join(r[:16]) for r in tracker_rows[-15:]])
    rules_text = "\n".join([" | ".join(r[:8]) for r in rules_rows])
    audit_text = "\n".join([" | ".join(r[:8]) for r in audit_rows])

    return summary_block, recent_bets, rules_text, audit_text


def run_argon_curation(today_str, summary_block, recent_bets
