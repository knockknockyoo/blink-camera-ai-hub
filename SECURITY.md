# Security policy

## Reporting a vulnerability

Do not open a public issue containing Blink credentials, Telegram bot tokens,
camera footage, account IDs, network IDs, Sync Module IDs, or private URLs.

Please use GitHub's private vulnerability reporting feature for the repository.
Include only the minimum information needed to reproduce the problem and redact
all personal identifiers. If private reporting is unavailable, open a public
issue that asks the maintainer for a private contact channel without including
the sensitive details.

## Protecting local data

- Never commit `.env` or anything under `data/`.
- Treat `data/blink-auth.json` like a password. It contains reusable Blink
  authentication information.
- Rotate the Telegram bot token through `@BotFather` immediately if it is
  exposed.
- Do not publish camera clips unless every visible person has consented.
- Bind the local API to `127.0.0.1`; do not expose port 8787 to the internet.

The included `.gitignore` excludes local credentials, videos, databases, logs,
model weights, and release archives. Verify `git status` before every push.
