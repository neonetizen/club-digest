#!/usr/bin/env python3
"""
Club Email Digest Agent
-----------------------
Fetches unread emails from a Gmail inbox, summarizes each one using Claude
Haiku, and sends a digest to a Discord channel via webhook.

The script is structured as three swappable layers:

    SOURCE  →  fetch_unread_emails()   read from anywhere (IMAP, RSS, Slack, API...)
    AI      →  summarize_emails()      triage / summarize / extract with Claude
    DELIVER →  send_to_discord()       post anywhere (Discord, Slack, email, SMS...)

To fork this for a different use case, you mostly just replace the SOURCE
function and tweak the system prompt. The state/dedup logic and delivery layer
transfer almost unchanged.

Setup:
    uv sync

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
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import anthropic


# ── Config ────────────────────────────────────────────────────────────────────
# If you're adapting this for a different source, replace the first two vars
# with whatever credentials that source needs (API key, OAuth token, etc.).
# DISCORD_WEBHOOK and ANTHROPIC_KEY stay the same regardless of source.

GMAIL_ADDRESS    = os.environ.get("CLUB_GMAIL_ADDRESS")
GMAIL_PASSWORD   = os.environ.get("CLUB_GMAIL_APP_PASSWORD")
DISCORD_WEBHOOK  = os.environ.get("DISCORD_WEBHOOK_URL")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")

IMAP_SERVER      = "imap.gmail.com"
IMAP_PORT        = 993

# Caps how much Claude processes per run — keeps token costs predictable and
# prevents a flooded inbox from burning through your budget on day one.
MAX_EMAILS_PER_RUN = 15

# Only look at emails from the last 6 months. Useful on first run when there's
# years of backlog you don't care about. Adjust or remove as needed.
EMAIL_CUTOFF = datetime.now(timezone.utc) - timedelta(days=180)

# Persisted between runs via GitHub Actions Cache (see the workflow YAML).
# If you run this locally on a server, it just lives on disk — same idea.
STATE_FILE = os.path.join(os.path.dirname(__file__), ".email_agent_state.json")


# ── State ─────────────────────────────────────────────────────────────────────
# "Have I seen this before?" — the core of any periodic agent.
#
# The state file stores IDs of already-processed items so re-running never
# double-reports. For email it's IMAP UIDs; for RSS you'd store GUIDs; for
# GitHub notifications you'd store event IDs; for Reddit, post IDs. Same shape
# either way: a list of strings inside a JSON object.
#
# The "first_run" flag is a one-time safety valve: on initial execution the
# inbox might have hundreds of old unread emails. Without it you'd flood
# Discord and burn API credits on noise. After the first run it's ignored.

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_uids": [], "first_run": True}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Source: Gmail IMAP ────────────────────────────────────────────────────────
# This is the layer to replace when adapting for a different data source.
#
# Whatever you swap in, the function should return a list of dicts with at
# least a unique "uid" key (used for dedup) and whatever fields the AI prompt
# will reference. For an RSS feed that might be {"uid", "title", "link",
# "summary"}; for Slack it might be {"uid", "channel", "author", "text"}.
#
# The two helper functions below (decode_str, get_body) are IMAP-specific —
# replace them with whatever parsing your source needs.

def decode_str(value: str) -> str:
    """Decode encoded email header strings (e.g. base64 subject lines)."""
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
    """Extract plain text body, falling back to HTML stripped of tags.

    Capped at 1500 chars — enough context for Claude to summarize, not so
    much that a newsletter doubles your token bill.
    """
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
    """Connect to Gmail via IMAP and return emails not yet processed.

    To swap in a different source, replace this function entirely.
    Keep the same signature: takes seen_uids (list of string IDs already
    processed), returns a list of dicts each with at least a "uid" field.
    """
    print(f"📬 Connecting to Gmail as {GMAIL_ADDRESS}...")
    seen_set = set(seen_uids)
    emails = []

    with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) as imap:
        imap.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        imap.select("INBOX")

        # "UNSEEN" is Gmail's unread filter. Other useful IMAP search terms:
        # "ALL", "FROM someaddress@example.com", "SINCE 01-Jan-2025", etc.
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

            # Skip emails older than EMAIL_CUTOFF. Remove this block if you
            # want to process everything regardless of age.
            try:
                from email.utils import parsedate_to_datetime
                msg_date = parsedate_to_datetime(msg.get("Date", ""))
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date < EMAIL_CUTOFF:
                    continue
            except Exception:
                pass

            emails.append({
                "uid":     uid_str,
                "from":    decode_str(msg.get("From", "")),
                "to":      decode_str(msg.get("To", "")),
                "subject": decode_str(msg.get("Subject", "(no subject)")),
                "date":    msg.get("Date", ""),
                "body":    get_body(msg),
            })

    return emails


# ── AI: Claude Summarization ──────────────────────────────────────────────────
# This is where the agent's "personality" lives. The system_prompt is the main
# thing to change when adapting for a new use case.
#
# Some other things you could do here instead of triage/summarization:
#   - Extract action items or deadlines from a project management channel
#   - Categorize support tickets by topic and estimate resolution effort
#   - Summarize a day's worth of Hacker News comments on a specific thread
#   - Pull out names/orgs/amounts from a batch of invoices or receipts
#
# Model note: claude-haiku-4-5-20251001 is fast and cheap — good for high-
# volume triage. Swap for claude-sonnet-4-6 if you need more nuanced reasoning
# (e.g., understanding technical content or multi-step instructions).

def summarize_emails(emails: list[dict]) -> str:
    """Run the email batch through Claude and return a formatted digest."""
    if not ANTHROPIC_KEY:
        # Graceful fallback — useful for testing the fetch/parse layer without
        # burning API credits.
        return json.dumps(emails, indent=2)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # The system prompt is the most important thing to customize. This one is
    # tuned for a club president who wants fast triage. The three-bucket
    # structure (action / FYI / ignore) maps well to Discord's eye-scan UX.
    # If your use case is different — say, a daily news briefing or a bug
    # report summary — rewrite this section entirely.
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


# ── Delivery: Discord ─────────────────────────────────────────────────────────
# This is the other layer to swap out. Discord webhooks are the simplest
# delivery mechanism — just a POST with {"content": "..."}.
#
# To send to Slack instead: same structure, but the payload key is "text"
#   requests.post(SLACK_WEBHOOK, json={"text": chunk})
#
# To send via email instead: swap requests.post for smtplib or sendgrid/mailgun.
#
# To write to a file or Notion page instead: skip HTTP entirely and write to
# disk or hit the Notion API.
#
# The 1900-char chunking is Discord-specific (Discord's limit is 2000 chars).
# Remove or adjust for other delivery targets.

def send_to_discord(content: str, email_count: int):
    """Post the digest to Discord, splitting into chunks to stay under the char limit."""
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
# The pipeline is intentionally flat here so you can see the whole flow in one
# glance: validate → load state → fetch → summarize → deliver → save state.
# Each step is a function call you can swap independently.

def main():
    print(f"📧 Club Email Digest Agent — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC\n")

    # All four vars are required. If you swap the source or delivery layer,
    # update this check to match your new required variables.
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

    # Mark these items as seen so they're skipped on the next run.
    # The [-1000:] cap keeps the state file from growing forever — 1000 IDs
    # is plenty of lookback for a low-volume inbox.
    state["seen_uids"].extend([e["uid"] for e in emails])
    state["seen_uids"] = state["seen_uids"][-1000:]
    state["first_run"] = False
    save_state(state)

    print(f"\n📝 Processed {len(emails)} email(s). State saved.")


if __name__ == "__main__":
    main()
