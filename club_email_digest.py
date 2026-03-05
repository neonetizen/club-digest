#!/usr/bin/env python3
"""
Club Email Digest Agent
-----------------------
Fetches unread emails from a Gmail inbox, summarizes each one using Claude
Haiku, and sends a digest to a Discord channel via webhook.

Setup:
    pip install anthropic

Environment variables:
    ANTHROPIC_API_KEY       - Anthropic API key
    CLUB_GMAIL_ADDRESS      - Club Gmail address
    CLUB_GMAIL_APP_PASSWORD - 16-char Gmail app password
    DISCORD_WEBHOOK_URL     - Discord webhook URL for the club email channel

Run manually:
    uv run club_email_digest.py

Cron / GitHub Actions:
    See ./.github/workflows/club_email_digest.yml file.
"""

import imaplib
import email
import os
import json
import re
from email.header import decode_header
from datetime import datetime, timezone
from typing import Optional

import requests
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_ADDRESS    = os.environ.get("CLUB_GMAIL_ADDRESS")
GMAIL_PASSWORD   = os.environ.get("CLUB_GMAIL_APP_PASSWORD")
DISCORD_WEBHOOK  = os.environ.get("DISCORD_WEBHOOK_URL")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")

IMAP_SERVER      = "imap.gmail.com"
IMAP_PORT        = 993

MAX_EMAILS_PER_RUN = 15

STATE_FILE = os.path.join(os.path.dirname(__file__), ".email_agent_state.json")


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_uids": [], "first_run": True}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail IMAP ────────────────────────────────────────────────────────────────

def decode_str(value: str) -> str:
    """Decode encoded email header strings."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def get_body(msg) -> str:
    """Extract plain text body from email, falling back to HTML stripped of tags."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    break
                except Exception:
                    continue
            elif ct == "text/html" and not body and "attachment" not in cd:
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    body = re.sub(r"<[^>]+>", " ", html)
                    body = re.sub(r"\s+", " ", body).strip()
                except Exception:
                    continue
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            body = ""

    return body.strip()[:1500]


def fetch_unread_emails(seen_uids: list) -> list[dict]:
    """Connect to Gmail via IMAP and fetch unread emails not yet processed."""
    print(f"📬 Connecting to Gmail as {GMAIL_ADDRESS}...")
    seen_set = set(seen_uids)
    emails = []

    with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) as imap:
        imap.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        imap.select("INBOX")

        _, data = imap.search(None, "UNSEEN")
        uids = data[0].split()

        if not uids:
            print("   No unread emails found.")
            return []

        new_uids = [u for u in uids if u.decode() not in seen_set]
        new_uids = new_uids[:MAX_EMAILS_PER_RUN]

        print(f"   Found {len(uids)} unread, {len(new_uids)} new to process")

        for uid in new_uids:
            uid_str = uid.decode()
            _, msg_data = imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            emails.append({
                "uid":     uid_str,
                "from":    decode_str(msg.get("From", "")),
                "to":      decode_str(msg.get("To", "")),
                "subject": decode_str(msg.get("Subject", "(no subject)")),
                "date":    msg.get("Date", ""),
                "body":    get_body(msg),
            })

    return emails


# ── Claude Summarization ──────────────────────────────────────────────────────

def summarize_emails(emails: list[dict]) -> str:
    """Summarize all fetched emails using Claude Haiku."""
    if not ANTHROPIC_KEY:
        return json.dumps(emails, indent=2)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    system_prompt = """You are a helpful assistant summarizing emails for a college
AI club president. He hasn't checked this inbox in a while and wants to quickly
understand what's there without reading everything himself.

For each email:
- Write 1-3 sentences max summarizing what it's actually about
- Flag if it needs a reply or action (label it 🔴 Action needed or 🟡 FYI only)
- Note if it looks like spam or automated/irrelevant email (label 🗑️ Probably ignore)
- Be direct — no fluff, no "this email is about..."

Group the emails by priority: Action needed first, then FYI, then ignore pile.
Use markdown formatting."""

    user_content = f"""Here are {len(emails)} unread emails from the club inbox.
Summarize them:

{json.dumps(emails, indent=2)}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": user_content}],
        system=system_prompt,
    )

    return message.content[0].text


# ── Discord ───────────────────────────────────────────────────────────────────

def send_to_discord(content: str, email_count: int):
    """Send digest to Discord, splitting into chunks if needed."""
    if not DISCORD_WEBHOOK:
        print("⚠️  DISCORD_WEBHOOK_URL not set — printing to stdout only.")
        print(content)
        return

    header = (
        f"📬 **Club Email Digest** — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"*{email_count} unread email(s) processed*\n\n"
    )
    full = header + content

    chunks = [full[i:i+1900] for i in range(0, len(full), 1900)]
    for chunk in chunks:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": chunk}, timeout=10)
        resp.raise_for_status()

    print(f"✅ Digest sent to Discord ({len(chunks)} message(s))")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"📧 Club Email Digest Agent — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC\n")

    missing = [k for k, v in {
        "CLUB_GMAIL_ADDRESS":      GMAIL_ADDRESS,
        "CLUB_GMAIL_APP_PASSWORD": GMAIL_PASSWORD,
        "ANTHROPIC_API_KEY":       ANTHROPIC_KEY,
        "DISCORD_WEBHOOK_URL":     DISCORD_WEBHOOK,
    }.items() if not v]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        return

    state = load_state()

    if state.get("first_run"):
        print("👋 First run detected — capping at 15 most recent unread emails.")
        print("   Subsequent runs will only process newly arrived emails.\n")

    emails = fetch_unread_emails(state["seen_uids"])

    if not emails:
        print("✅ Nothing new to report.")
        requests.post(DISCORD_WEBHOOK, json={
            "content": f"📬 **Club Email Digest** — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n*No new emails today.*"
        }, timeout=10)
        return

    print(f"\n🤖 Summarizing {len(emails)} email(s) with Claude Haiku...")
    summary = summarize_emails(emails)

    send_to_discord(summary, len(emails))

    # Update state
    state["seen_uids"].extend([e["uid"] for e in emails])
    state["seen_uids"] = state["seen_uids"][-1000:]  # keep last 1000
    state["first_run"] = False
    save_state(state)

    print(f"\n📝 Processed {len(emails)} email(s). State saved.")


if __name__ == "__main__":
    main()