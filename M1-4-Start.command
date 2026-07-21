#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

(sleep 4; open http://localhost:3000) &
bash scripts/run.sh
