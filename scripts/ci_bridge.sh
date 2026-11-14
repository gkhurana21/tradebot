#!/usr/bin/env bash
# Python -> Go bridge, end to end, against a real engine. Used by the
# `bridge` job in .github/workflows/ci.yml and runnable locally from the repo
# root. Stages, in the order CI runs them:
#
#   scripts/ci_bridge.sh start    build the engine, start it on 127.0.0.1:8081
#   scripts/ci_bridge.sh run      300 bars through `tradebot live --engine`,
#                                 then check what Python recorded against /stats
#                                 and the engine's journal
#   (the workflow runs the Swift probe here)
#   scripts/ci_bridge.sh verify   same checks again, now with the probe's output
#   scripts/ci_bridge.sh kill     start a second run, kill the engine mid-run:
#                                 Python must exit 2 and record no fill after it
#   scripts/ci_bridge.sh stop     stop the engine if it is still running
#
# Environment: PORT (8081), OUT (/tmp/bridge), PYTHON (python3).
set -euo pipefail

PORT="${PORT:-8081}"
URL="http://127.0.0.1:${PORT}"
OUT="${OUT:-/tmp/bridge}"
PYTHON="${PYTHON:-python3}"
ITERATIONS=300
mkdir -p "$OUT"
export PYTHONPATH="${PYTHONPATH:-src}"

now_utc() { "$PYTHON" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())'; }

live() {  # live EVENTS_FILE [extra args...]
  local events="$1"; shift
  "$PYTHON" -m tradebot live --engine "$URL" --source synthetic --symbols BTC-USD \
    --poll 0 --no-llm --events-out "$events" "$@"
}

case "${1:-}" in
  start)
    # Build first, then launch the binary with nohup: `go run` keeps a parent
    # process that can be torn down when the step's shell exits.
    (cd engine && go build -o "$OUT/engine-bin" ./cmd/engine)
    # The engine owns sizing, so the risk profile is set here, not on the Python side.
    nohup "$OUT/engine-bin" -venue sim -addr "127.0.0.1:${PORT}" -cash 25000 \
      -profile conservative > "$OUT/engine.log" 2>&1 &
    echo $! > "$OUT/engine.pid"
    for _ in $(seq 1 30); do
      if curl -sf "$URL/health" > /dev/null; then
        echo "engine is up: $(curl -s "$URL/health")"; exit 0
      fi
      sleep 1
    done
    echo "engine failed to start"; cat "$OUT/engine.log"; exit 1
    ;;

  run)
    live "$OUT/events.jsonl" --iterations "$ITERATIONS" > "$OUT/live.stdout"
    echo "python exited 0; $(wc -l < "$OUT/events.jsonl") events"
    "$PYTHON" tests/ci_bridge_check.py verify "$OUT/events.jsonl" "$URL" --ticks "$ITERATIONS"
    ;;

  verify)
    args=(verify "$OUT/events.jsonl" "$URL" --ticks "$ITERATIONS")
    [ -f "$OUT/probe.txt" ] && args+=(--probe-output "$OUT/probe.txt")
    "$PYTHON" tests/ci_bridge_check.py "${args[@]}"
    ;;

  kill)
    pid="$(cat "$OUT/engine.pid")"
    rm -f "$OUT/kill-events.jsonl"
    # A long run: the kill, not the iteration count, ends it.
    live "$OUT/kill-events.jsonl" --iterations 100000 --poll 0.02 \
      > "$OUT/kill.stdout" 2> "$OUT/kill.stderr" &
    live_pid=$!
    for _ in $(seq 1 300); do   # up to 30 s for 30 decisions
      n=$(grep -c '"event": "verdict"' "$OUT/kill-events.jsonl" 2>/dev/null || true)
      [ "${n:-0}" -ge 30 ] && break
      sleep 0.1
    done
    [ "${n:-0}" -ge 30 ] || { echo "python never got going"; kill "$live_pid" 2>/dev/null; exit 1; }
    kill -9 "$pid"
    while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done
    killed_at="$(now_utc)"
    echo "engine killed at $killed_at after $n decisions"
    code=0
    for _ in $(seq 1 300); do   # Python must notice and stop by itself
      kill -0 "$live_pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$live_pid" 2>/dev/null; then
      kill -9 "$live_pid"; echo "python did not stop within 30 s of the kill"; exit 1
    fi
    wait "$live_pid" || code=$?
    echo "python exit code: $code"; cat "$OUT/kill.stderr"
    "$PYTHON" tests/ci_bridge_check.py killed "$OUT/kill-events.jsonl" \
      --exit-code "$code" --killed-at "$killed_at"
    ;;

  stop)
    [ -f "$OUT/engine.pid" ] && kill "$(cat "$OUT/engine.pid")" 2>/dev/null || true
    ;;

  *)
    echo "usage: $0 start|run|verify|kill|stop" >&2; exit 64
    ;;
esac
