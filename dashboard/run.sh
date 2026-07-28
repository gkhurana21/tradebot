#!/usr/bin/env bash
# Launches the dashboard, building first if needed.
#   ./run.sh                          watches http://127.0.0.1:8080
#   ./run.sh --url http://host:9000   watches a different engine
set -euo pipefail
cd "$(dirname "$0")"
[ -d .build/TradebotDashboard.app ] || ./build.sh
if [ $# -gt 0 ]; then
  open .build/TradebotDashboard.app --args "$@"
else
  open .build/TradebotDashboard.app
fi
echo "Dashboard launched. It polls the engine once per second."
