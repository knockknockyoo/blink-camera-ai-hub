#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.blink-camera-ai-hub.native-ai"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT_DIR/data/logs"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

/usr/bin/python3 - "$PLIST" "$ROOT_DIR" "$LOG_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
root = Path(sys.argv[2])
logs = Path(sys.argv[3])
payload = {
    "Label": "com.blink-camera-ai-hub.native-ai",
    "ProgramArguments": [
        "/bin/bash",
        str(root / "scripts" / "run_native_ai.sh"),
    ],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Interactive",
    "StandardOutPath": str(logs / "native-ai.log"),
    "StandardErrorPath": str(logs / "native-ai.log"),
}
with plist_path.open("wb") as stream:
    plistlib.dump(payload, stream)
PY

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "Native Apple GPU service enabled."
echo "Status: launchctl print gui/$UID/$LABEL"
echo "Logs:   tail -f '$LOG_DIR/native-ai.log'"
