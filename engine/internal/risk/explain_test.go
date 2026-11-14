package risk

import (
	"testing"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

func TestExplainReadableReasons(t *testing.T) {
	cfg := Balanced() // 20% cap, 1% risk, 2% stop, 5% drawdown, 3 positions, 0.55 confidence

	tests := []struct {
		name  string
		setup func(e *Engine)
		dec   types.Decision
		acct  Account
		price float64
		want  Verdict
		text  string
	}{
		{
			name: "low confidence",
			dec:  decision(types.Buy, 0.30, 1), acct: account(10_000), price: 100,
			want: RejectConfidence, text: "confidence 0.30 < min 0.55",
		},
		{
			name: "veto",
			dec:  func() types.Decision { d := decision(types.Buy, 0.9, 1); d.Veto = true; return d }(),
			acct: account(10_000), price: 100,
			want: RejectVeto, text: "vetoed by the research layer",
		},
		{
			name: "flat",
			dec:  decision(types.Flat, 0.9, 1), acct: account(10_000), price: 100,
			want: RejectVeto, text: "proposed FLAT, nothing to trade",
		},
		{
			name: "max open positions",
			dec:  decision(types.Buy, 0.9, 1),
			acct: account(10_000,
				types.Position{Symbol: "A", Qty: 1, AvgPrice: 1},
				types.Position{Symbol: "B", Qty: 1, AvgPrice: 1},
				types.Position{Symbol: "C", Qty: 1, AvgPrice: 1}),
			price: 100, want: RejectMaxPositions, text: "max open positions 3 reached",
		},
		{
			name:  "at max notional",
			dec:   decision(types.Buy, 0.9, 1),
			acct:  account(10_000, types.Position{Symbol: "BTC-USD", Qty: 20, AvgPrice: 100}),
			price: 100, want: RejectAtCap, text: "position at max notional 20%",
		},
		{
			name:  "halted",
			setup: func(e *Engine) { e.CheckKillSwitch(9_480) }, // 5.2% below the 10k baseline
			dec:   decision(types.Buy, 0.9, 1), acct: account(9_480), price: 100,
			want: RejectHalted, text: "halted: daily drawdown 5.2% ≥ 5%",
		},
		{
			name: "approved",
			dec:  decision(types.Buy, 0.9, 1), acct: account(10_000), price: 100,
			want: Approved, text: "sized 20.0 BTC-USD · risk 0.4% to stop",
		},
		{
			name: "approved at BTC prices",
			dec:  decision(types.Buy, 0.9, 1), acct: account(10_000), price: 60_000,
			want: Approved, text: "sized 0.0333 BTC-USD · risk 0.4% to stop",
		},
		{
			name: "invalid keeps the engine's own text",
			dec:  decision(types.Buy, 0.9, 1), acct: account(10_000), price: -1,
			want: RejectInvalid, text: "non-positive or non-finite price",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			e := New(cfg)
			e.StartDay(10_000)
			if tc.setup != nil {
				tc.setup(e)
			}
			res := e.Evaluate(tc.dec, tc.acct, tc.price)
			if res.Verdict != tc.want {
				t.Fatalf("verdict = %q (%s), want %q", res.Verdict, res.Reason, tc.want)
			}
			if got := e.Explain(tc.dec, tc.acct, tc.price, res); got != tc.text {
				t.Fatalf("Explain = %q, want %q", got, tc.text)
			}
		})
	}
}

func TestExplainDoesNotChangeTheTypedVerdict(t *testing.T) {
	e := New(Balanced())
	e.StartDay(10_000)
	res := e.Evaluate(decision(types.Buy, 0.1, 1), account(10_000), 100)
	before := res.Verdict
	_ = e.Explain(decision(types.Buy, 0.1, 1), account(10_000), 100, res)
	if res.Verdict != before || before != RejectConfidence {
		t.Fatalf("verdict changed: %q", res.Verdict)
	}
}

func TestPctAndSig3(t *testing.T) {
	for in, want := range map[float64]string{0.05: "5", 0.035: "3.5", 0.10: "10", 0.35: "35", 0.2: "20"} {
		if got := pct(in); got != want {
			t.Errorf("pct(%v) = %q, want %q", in, got, want)
		}
	}
	for in, want := range map[float64]string{0.078: "0.0780", 0.0781234: "0.0781", 1.2345: "1.23", 20: "20.0", 12345.6: "12346"} {
		if got := sig3(in); got != want {
			t.Errorf("sig3(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestSessionStartEquity(t *testing.T) {
	e := New(Balanced())
	if got := e.SessionStartEquity(); got != 0 {
		t.Fatalf("before start = %v, want 0", got)
	}
	e.StartDay(12_345)
	if got := e.SessionStartEquity(); got != 12_345 {
		t.Fatalf("after start = %v", got)
	}
}

func TestRiskPctNeverShowsZero(t *testing.T) {
	for in, want := range map[float64]string{0.4: "0.4", 1: "1.0", 0.034: "0.03", 0.004: "<0.01"} {
		if got := riskPct(in); got != want {
			t.Errorf("riskPct(%v) = %q, want %q", in, got, want)
		}
	}
}
