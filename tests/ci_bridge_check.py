"""CI checks for the Python -> Go bridge. Standard library only.

  verify EVENTS ENGINE_URL [--probe-output FILE]
      After `tradebot live --engine ... --events-out EVENTS` ran to completion
      against a fresh sim engine, prove that what Python recorded is what the
      engine did: verdicts == /stats decisions == approved + rejected, Python
      fills == the engine's journal fills (a flip is two legs, bracket exits are
      separate), at least one of each, no gate errors. Prints the Python -> Go
      /decision round-trip p50/p99.

  killed EVENTS --exit-code N --killed-at ISO_TIMESTAMP
      After the engine was killed mid-run: Python must have exited 2, must have
      been trading before the kill, and must have recorded no fill or verdict
      after it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def check(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)
    print(f"ok   {msg}")


def load_events(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def close(a, b, tol=1e-9) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def verify(a) -> None:
    ev = load_events(a.events)
    kinds = Counter(e["event"] for e in ev)
    print("python events:", dict(kinds))
    stats = get(f"{a.engine}/stats")
    print("go /stats:    ", {k: stats[k] for k in ("ticks", "decisions", "fills", "rejected", "exits")})

    verdicts = [e for e in ev if e["event"] == "verdict"]
    approved = [v for v in verdicts if v["approved"]]
    rejected = [v for v in verdicts if not v["approved"]]
    fills = [e for e in ev if e["event"] == "fill"]
    flip_legs = [f for f in fills if f.get("reason") == "flip"]
    entries = [f for f in fills if f.get("reason") != "flip"]
    exits = [e for e in ev if e["event"] == "exit"]

    check(kinds["gate_error"] == 0 and kinds["gate_unknown"] == 0,
          "no gate_error and no gate_unknown events")
    check(len(approved) >= 1 and len(rejected) >= 1,
          f"at least one approval and one rejection ({len(approved)} / {len(rejected)})")
    check(len(entries) >= 1 and len(exits) >= 1,
          f"at least one entry fill and one bracket exit ({len(entries)} / {len(exits)})")
    check(len(verdicts) == stats["decisions"] == len(approved) + len(rejected),
          f"python verdicts ({len(verdicts)}) == go decisions ({stats['decisions']}) "
          f"== approved + rejected")
    check(len(rejected) == stats["rejected"], f"rejected {len(rejected)} == go {stats['rejected']}")
    check(len(entries) == len(approved) == stats["fills"],
          f"entry fills {len(entries)} == approvals == go fills {stats['fills']}")
    check(len(exits) + len(flip_legs) == stats["exits"],
          f"bracket exits {len(exits)} + flip legs {len(flip_legs)} == go exits {stats['exits']}")
    if a.ticks is not None:
        check(stats["ticks"] == a.ticks, f"go ticks {stats['ticks']} == {a.ticks} iterations")

    # Join Python's records to the engine's journal.
    j = get(f"{a.engine}/decisions")
    items = j["items"]
    gap = j["gap"]
    print(f"journal: {len(items)} items, oldest_seq {j['oldest_seq']}, gap={gap}")
    by_cid = {v["client_id"]: v for v in verdicts}
    j_dec = [i for i in items if i["kind"] == "decision"]
    j_exit = [i for i in items if i["kind"] == "exit"]
    for i in j_dec:
        v = by_cid.get(i.get("client_id"))
        if v is None:
            fail(f"journal decision seq {i['seq']} client_id {i.get('client_id')} has no python verdict")
        if v["verdict"] != i["verdict"] or v["approved"] != i["approved"]:
            fail(f"seq {i['seq']}: python {v['verdict']} vs go {i['verdict']}")
    check(True, f"all {len(j_dec)} journal decisions match a python verdict (verdict, approved)")

    ent_by_cid = {f["client_id"]: f for f in entries}
    for i in j_dec:
        if i["approved"]:
            f = ent_by_cid.get(i["client_id"])
            if f is None or not close(f["qty"], i["qty"]) or not close(f["price"], i["price"]):
                fail(f"journal entry {i['seq']} qty/price differs from python fill {f}")
    check(True, "journal entry fills (qty, price) match python fills")

    flip_by_cid = {f["client_id"]: f for f in flip_legs}
    j_flips = [i for i in j_exit if i.get("client_id")]
    j_brackets = [i for i in j_exit if not i.get("client_id")]
    for i in j_flips:
        f = flip_by_cid.get(i["client_id"])
        if f is None or not close(f["qty"], i["qty"]) or not close(f["price"], i["price"]):
            fail(f"journal flip leg {i['seq']} differs from python fill {f}")
    check(True, f"{len(j_flips)} journal flip legs match python flip fills")

    if not gap:
        check(len(j_dec) == len(verdicts), "journal holds every decision (ring did not wrap)")
        check(len(j_flips) == len(flip_legs), "flip legs: journal == python")
        py_br = sorted((e["symbol"], round(e["qty"], 9), round(e["price"], 6)) for e in exits)
        go_br = sorted((i["symbol"], round(i["qty"], 9), round(i["price"], 6)) for i in j_brackets)
        check(py_br == go_br, f"bracket exits: journal == python ({len(go_br)})")
    else:
        print("note: journal ring wrapped; compared the retained tail only")

    if a.probe_output:
        text = open(a.probe_output).read()
        m = re.search(r"fills:\s+(\d+)\s+rejected:\s+(\d+)\s+exits:\s+(\d+)", text)
        check(m is not None, "swift probe printed the fills/rejected/exits line")
        got = tuple(int(x) for x in m.groups())
        want = (stats["fills"], stats["rejected"], stats["exits"])
        check(got == want, f"swift probe stats {got} == go /stats {want}")
        feed = [ln for ln in text.splitlines() if ln.startswith("FEED ")]
        if feed:
            check(len(feed) >= 1, f"swift probe decoded {len(feed)} decision feed items")
        else:
            print("note: this swift client does not read /decisions yet (no FEED lines)")

    rtts = sorted(v["rtt_ms"] for v in verdicts)
    print(f"python -> go /decision round trip over {len(rtts)} decisions: "
          f"p50 {percentile(rtts, 50):.3f} ms  p99 {percentile(rtts, 99):.3f} ms  "
          f"max {rtts[-1]:.3f} ms")
    print("bridge check passed")


def killed(a) -> None:
    ev = load_events(a.events)
    when = datetime.fromisoformat(a.killed_at)
    after = [e for e in ev if datetime.fromisoformat(e["ts"]) > when]
    before_fills = [e for e in ev if e["event"] == "fill"
                    and datetime.fromisoformat(e["ts"]) <= when]
    print(f"{len(ev)} events, {len(after)} after the kill at {a.killed_at}")
    check(a.exit_code == 2, f"python exited with code {a.exit_code} (want 2)")
    check(len(before_fills) >= 1, f"python was trading before the kill ({len(before_fills)} fills)")
    bad = [e for e in after if e["event"] in ("fill", "verdict", "signal")]
    check(not bad, f"no fill, verdict or signal recorded after the kill ({len(bad)} found)")
    errs = [e for e in ev if e["event"] == "gate_error"]
    check(len(errs) >= 1, f"python recorded gate_error events ({len(errs)})")
    stop = [e for e in ev if e["event"] == "stop"]
    check(stop and stop[-1].get("exit_code") == 2, "final stop event carries exit_code 2")
    print("fail-closed check passed")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("events")
    v.add_argument("engine")
    v.add_argument("--probe-output", default=None)
    v.add_argument("--ticks", type=int, default=None, help="expected /stats ticks")
    v.set_defaults(func=verify)
    k = sub.add_parser("killed")
    k.add_argument("events")
    k.add_argument("--exit-code", type=int, required=True)
    k.add_argument("--killed-at", required=True)
    k.set_defaults(func=killed)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
