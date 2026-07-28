#!/usr/bin/env bash
# Builds and runs the TradebotKit test suite.
set -euo pipefail
cd "$(dirname "$0")"
./build.sh > /dev/null
exec ./.build/TradebotKitTests
