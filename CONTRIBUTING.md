# Contributing

Contributions are welcome. Please keep changes focused and avoid including any
real Blink account data or camera footage.

## Local setup

```bash
bash scripts/setup.sh
cp .env.example .env
```

Connect a Blink account only when integration testing is necessary. Most logic
can be tested without an account:

```bash
.venv/bin/python -m unittest tests.test_core
npm run lint
```

## Pull requests

1. Explain the problem and the intended behavior.
2. Add or update tests for behavior changes.
3. Keep credentials, tokens, account identifiers, logs, databases, model
   weights, and video clips out of commits.
4. Mention any new dependency and its license.
5. Confirm that the Python tests and relevant frontend checks pass.

Bug reports should include sanitized logs only. Replace camera names, account
IDs, network IDs, Sync Module IDs, URLs, and local paths with placeholders.
