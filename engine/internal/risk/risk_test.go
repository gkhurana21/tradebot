package risk

import (
	"math"
	"sync"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

func decision(side types.Side, conf, size float64) types.Decision {
	return types.Decision{
		TS: time.Now(), Symbol: "BTC-USD", Side: side,
		Confidence: conf, SizeHint: size,
	}
}

func account(equity float64, positions ...types.Position) Account {
	m := make(map[string]types.Position, len(positions))
	for _, p := range positions {
		m[p.Symbol] = p
	}
	return Account{Equity: equity, Positions: m}
}

func TestEvaluate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Config
		dec     types.Decision
		acct    Account
		price   float64
		want    Verdict
		checkFn func(*testing.T, Result)
	}{
		{
			name:  "approves a well-formed decision",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(10_000),
			price: 100,
			want:  Approved,
			checkFn: func(t *testing.T, r Result) {
				// Fixed-fractional: 1% of 10k risked over a 2% stop = 50 units,
				// but the 20% notional cap allows only 20 units at $100.
				if got := r.Order.Qty; math.Abs(got-20) > 1e-9 {
					t.Errorf("qty = %v, want 20 (notional cap)", got)
				}
				if r.Order.StopLoss >= 100 || r.Order.TakeProfit <= 100 {
					t.Errorf("long bracket inverted: stop=%v target=%v",
						r.Order.StopLoss, r.Order.TakeProfit)
				}
			},
		},
		{
			name:  "rejects confidence below the floor",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.10, 1.0),
			acct:  account(10_000),
			price: 100,
			want:  RejectConfidence,
		},
		{
			name:  "honors an explicit veto",
			cfg:   Balanced(),
			dec:   types.Decision{Symbol: "BTC-USD", Side: types.Buy, Confidence: 0.99, SizeHint: 1, Veto: true},
			acct:  account(10_000),
			price: 100,
			want:  RejectVeto,
		},
		{
			name:  "rejects a flat proposal",
			cfg:   Balanced(),
			dec:   decision(types.Flat, 0.99, 1.0),
			acct:  account(10_000),
			price: 100,
			want:  RejectVeto,
		},
		{
			name:  "rejects opening beyond the concurrent-position limit",
			cfg:   func() Config { c := Balanced(); c.MaxOpenPositions = 1; return c }(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(10_000, types.Position{Symbol: "ETH-USD", Qty: 1, AvgPrice: 2000}),
			price: 100,
			want:  RejectMaxPositions,
		},
		{
			name: "allows adding to an existing symbol at the position limit",
			cfg:  func() Config { c := Balanced(); c.MaxOpenPositions = 1; return c }(),
			dec:  decision(types.Buy, 0.9, 1.0),
			// Small existing position in the *same* symbol consumes no new slot.
			acct:  account(10_000, types.Position{Symbol: "BTC-USD", Qty: 1, AvgPrice: 100}),
			price: 100,
			want:  Approved,
		},
		{
			name: "rejects stacking past the notional cap",
			cfg:  Balanced(),
			dec:  decision(types.Buy, 0.9, 1.0),
			// 20 units at $100 already equals the 20%-of-10k cap.
			acct:  account(10_000, types.Position{Symbol: "BTC-USD", Qty: 20, AvgPrice: 100}),
			price: 100,
			want:  RejectAtCap,
		},
		{
			name:  "sizes only the remaining headroom when partially filled",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(10_000, types.Position{Symbol: "BTC-USD", Qty: 15, AvgPrice: 100}),
			price: 100,
			want:  Approved,
			checkFn: func(t *testing.T, r Result) {
				if got := r.Order.Qty; math.Abs(got-5) > 1e-9 {
					t.Errorf("qty = %v, want 5 (20 cap - 15 held)", got)
				}
			},
		},
		{
			name: "does not net an opposing position against the cap",
			cfg:  Balanced(),
			dec:  decision(types.Buy, 0.9, 1.0),
			// A short position is not same-direction exposure for a buy.
			acct:  account(10_000, types.Position{Symbol: "BTC-USD", Qty: -20, AvgPrice: 100}),
			price: 100,
			want:  Approved,
		},
		{
			name:  "scales size by the research layer's hint",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 0.5),
			acct:  account(10_000),
			price: 100,
			want:  Approved,
			checkFn: func(t *testing.T, r Result) {
				if got := r.Order.Qty; math.Abs(got-10) > 1e-9 {
					t.Errorf("qty = %v, want 10 (half of the 20 cap)", got)
				}
			},
		},
		{
			name:  "brackets a short trade the other way up",
			cfg:   Balanced(),
			dec:   decision(types.Sell, 0.9, 1.0),
			acct:  account(10_000),
			price: 100,
			want:  Approved,
			checkFn: func(t *testing.T, r Result) {
				if r.Order.StopLoss <= 100 || r.Order.TakeProfit >= 100 {
					t.Errorf("short bracket inverted: stop=%v target=%v",
						r.Order.StopLoss, r.Order.TakeProfit)
				}
			},
		},
		{
			name:  "rejects a non-positive price",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(10_000),
			price: 0,
			want:  RejectInvalid,
		},
		{
			name:  "rejects a NaN price",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(10_000),
			price: math.NaN(),
			want:  RejectInvalid,
		},
		{
			name:  "rejects a malformed confidence",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 1.5, 1.0),
			acct:  account(10_000),
			price: 100,
			want:  RejectInvalid,
		},
		{
			name:  "rejects a blown-up account",
			cfg:   Balanced(),
			dec:   decision(types.Buy, 0.9, 1.0),
			acct:  account(0),
			price: 100,
			want:  RejectZeroSize,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			e := New(tc.cfg)
			e.StartDay(tc.acct.Equity)
			got := e.Evaluate(tc.dec, tc.acct, tc.price)
			if got.Verdict != tc.want {
				t.Fatalf("verdict = %q (%s), want %q", got.Verdict, got.Reason, tc.want)
			}
			if tc.checkFn != nil {
				tc.checkFn(t, got)
			}
		})
	}
}

func TestKillSwitch(t *testing.T) {
	e := New(Balanced()) // 5% daily drawdown limit
	e.StartDay(10_000)

	if e.CheckKillSwitch(9_600) { // -4%, still inside the limit
		t.Fatal("halted early at 4% drawdown")
	}
	if !e.CheckKillSwitch(9_400) { // -6%, breached
		t.Fatal("failed to halt at 6% drawdown")
	}
	if !e.Halted() {
		t.Fatal("halt did not latch")
	}

	// A halted engine rejects even a perfect decision.
	res := e.Evaluate(decision(types.Buy, 0.99, 1.0), account(9_400), 100)
	if res.Verdict != RejectHalted {
		t.Fatalf("verdict = %q, want %q", res.Verdict, RejectHalted)
	}

	// Recovering equity must not silently un-halt the session.
	if e.CheckKillSwitch(10_500); !e.Halted() {
		t.Fatal("halt cleared without an explicit StartDay")
	}
	e.StartDay(10_500)
	if e.Halted() {
		t.Fatal("StartDay did not clear the halt")
	}
}

// The kill switch must engage even if a caller forgets StartDay, rather than
// treating an unbaselined session as unlimited.
func TestKillSwitchWithoutStartDay(t *testing.T) {
	e := New(Balanced())
	e.CheckKillSwitch(10_000) // establishes the baseline
	if !e.CheckKillSwitch(9_000) {
		t.Fatal("failed to halt without an explicit StartDay")
	}
}

func TestProfilesOrderedByAggression(t *testing.T) {
	c, b, a := Conservative(), Balanced(), Aggressive()
	if !(c.MaxPositionPct < b.MaxPositionPct && b.MaxPositionPct < a.MaxPositionPct) {
		t.Error("position sizing is not ordered conservative < balanced < aggressive")
	}
	if !(c.MaxDailyDrawdownPct < b.MaxDailyDrawdownPct && b.MaxDailyDrawdownPct < a.MaxDailyDrawdownPct) {
		t.Error("drawdown limits are not ordered conservative < balanced < aggressive")
	}
	if _, ok := Profile("nonsense"); ok {
		t.Error("unknown profile reported as valid")
	}
	if cfg, ok := Profile("aggressive"); !ok || cfg.MaxPositionPct != a.MaxPositionPct {
		t.Error("Profile did not resolve a known name")
	}
}

// The engine must tolerate concurrent evaluation; run with -race.
func TestConcurrentEvaluate(t *testing.T) {
	e := New(Balanced())
	e.StartDay(10_000)
	acct := account(10_000)

	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				e.Evaluate(decision(types.Buy, 0.9, 1.0), acct, 100)
				e.CheckKillSwitch(10_000)
				_ = e.Drawdown(9_900)
			}
		}()
	}
	wg.Wait()
}

// Evaluate sits in the hot path between signal and order, so it must stay far
// below network latency. Run: go test -bench=. ./internal/risk
func BenchmarkEvaluate(b *testing.B) {
	e := New(Balanced())
	e.StartDay(10_000)
	acct := account(10_000, types.Position{Symbol: "ETH-USD", Qty: 2, AvgPrice: 2000})
	d := decision(types.Buy, 0.9, 1.0)

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if res := e.Evaluate(d, acct, 100); !res.Verdict.OK() {
			b.Fatalf("unexpected rejection: %s", res.Reason)
		}
	}
}

func BenchmarkEvaluateParallel(b *testing.B) {
	e := New(Balanced())
	e.StartDay(10_000)
	acct := account(10_000)
	d := decision(types.Buy, 0.9, 1.0)

	b.ReportAllocs()
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			e.Evaluate(d, acct, 100)
		}
	})
}
