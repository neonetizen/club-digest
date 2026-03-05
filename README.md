# club-digest

An autonomous AI agent that checks a Gmail inbox on a schedule, summarizes every unread email using Claude Haiku, and delivers a prioritized digest to a Discord channel, all running free on GitHub Actions.

Built because, as president of my university's AI club, the email was not checked as frequently as I'd like. Rather than manually triaging and remembering to check this email, I automated it.

---

## What it does

Every morning, the agent:

1. Connects to Gmail via IMAP and fetches unread emails
2. Sends them to Claude Haiku with instructions to triage by urgency
3. Posts a structured digest to a Discord channel with three categories:
   - 🔴 **Action needed** — emails that require a reply or decision
   - 🟡 **FYI only** — informational, no response needed
   - 🗑️ **Probably ignore** — spam, automated notifications, irrelevant

---

## Stack

- **Python** with `imaplib` (stdlib) for Gmail access — no extra email library needed
- **Anthropic Python SDK** for Claude Haiku summarization
- **GitHub Actions** for free scheduled execution
- **Actions Cache** for state persistence between runs (no sensitive data committed to the repo)
- **Discord webhooks** for delivery

---

## Setup

### 1. Gmail app password

You need an app password — not your real Gmail password. This lets the script authenticate without OAuth and can be revoked independently.

1. Enable 2-Step Verification on the Gmail account: [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to **App passwords** (bottom of the 2-Step Verification page)
3. Name it anything (e.g. "digest bot") and generate
4. Copy the 16-character password — you only see it once

### 2. Discord webhook

1. Right click a Discord channel → **Edit Channel → Integrations → Webhooks → New Webhook**
2. Copy the webhook URL

### 3. GitHub secrets

In your fork: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | API key from [console.anthropic.com](https://console.anthropic.com) |
| `CLUB_GMAIL_ADDRESS` | The Gmail address to monitor |
| `CLUB_GMAIL_APP_PASSWORD` | The 16-char app password from step 1 |
| `DISCORD_WEBHOOK_URL` | The webhook URL from step 2 |

### 4. Create the workflow file

Copy the YAML block from the bottom of `club_email_digest.py` into:
```
.github/workflows/club_email_digest.yml
```

### 5. Initialize the project with uv

```bash
uv init
uv add anthropic requests
```

### 6. Push and test

```bash
git add .
git commit -m "initial setup"
git push
```

Go to **Actions → Club Email Digest → Run workflow** to trigger a manual test run before waiting for the scheduled cron.

---

## Customizing the schedule

The default cron runs at 8am PST (16:00 UTC). Edit the cron line in the workflow YAML:

```yaml
- cron: '0 16 * * *'   # daily at 8am PST
```

---

## Customizing the triage logic

The summarization behavior is controlled by the system prompt in `summarize_emails()`. Edit it to change tone, add categories, or give Claude more context about what kind of emails your inbox receives.

---

## Privacy note

No email content is ever committed to this repository. State is persisted using GitHub Actions Cache (private to the repo) and contains only IMAP UIDs - not subject lines, bodies, or sender addresses. All sensitive credentials are stored as GitHub Secrets and never appear in logs or code.
