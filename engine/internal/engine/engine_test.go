package engine

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
	"github.com/gkhurana21/tradebot/engine/internal/types"
)

func newTestEngine(t *testing.T, cash float64, cfg risk.Config) (*Engine, *book.Book, context.CancelFunc) {
	t.Helper()
	b := book.New(book.Options{StartingCash: cash})
	e := New(b, risk.New(cfg))
	ctx, cancel := context.WithCancel(context.Background())
	e.Start(ctx)
	t.Cleanup(func() { e.Stop(); cancel() })
	return e, b, cancel
}

// drain consumes events so the buffered channel never blocks the engine.
func drain(e *Engine) *[]Event {
	var mu sync.Mutex
	got := []Event{}
	go func() {
		for ev := range e.Events() {
			mu.Lock()
			got = append(got, ev)
			mu.Unlock()
		}
	}()
	return &got
}

func buy(symbol string, conf float64) types.Decision {
	return types.Decision{
		TS: time.Now().UTC(), Symbol: symbol, Side: types.Buy,
		Confidence: conf, SizeHint: 1, Rationale: "test",
	}
}

func TestDecisionFlowsThroughRiskToFill(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	if err := e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()}); err != nil {
		t.Fatalf("submit tick: %v", err)
	}
	// A tick must be processed before a decision can be priced.
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	res, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))
	if err != nil {
		t.Fatalf("submit decision: %v", err)
	}
	if !res.Verdict.OK() {
		t.Fatalf("verdict = %q (%s), want approved", res.Verdict, res.Reason)
	}
	if pos := b.Position("BTC-USD"); pos.IsFlat() {
		t.Fatal("no position opened after an approved decision")
	}
	if got := e.Stats().Fills; got != 1 {
		t.Fatalf("fills = %d, want 1", got)
	}
}

func TestDecisionWithoutMarketDataIsRejected(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	res, err := e.SubmitDecision(context.Background(), buy("NOPE-USD", 0.9))
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if res.Verdict.OK() {
		t.Fatal("approved a decision with no market data")
	}
}

func TestStopLossExitsThePosition(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	if _, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9)); err != nil {
		t.Fatal(err)
	}

	stop := b.Position("BTC-USD").StopLoss
	if stop <= 0 {
		t.Fatal("no protective stop attached to the position")
	}

	// A bar whose low pierces the stop must flatten the position.
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: stop - 1, High: 100, Low: stop - 1, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Exits >= 1 })

	if !b.Position("BTC-USD").IsFlat() {
		t.Fatal("position survived a stop-loss breach")
	}
}

func TestTakeProfitExitsThePosition(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	target := b.Position("BTC-USD").TakeProfit
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: target + 1, High: target + 1, Low: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Exits >= 1 })

	if !b.Position("BTC-USD").IsFlat() {
		t.Fatal("position survived a take-profit touch")
	}
	if b.Cash() <= 10_000 {
		t.Fatalf("cash = %v, want a profit after hitting target", b.Cash())
	}
}

// When one bar straddles both the stop and the target we cannot know which
// printed first, so the engine must assume the loss.
func TestStraddlingBarPrefersTheStop(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	pos := b.Position("BTC-USD")
	e.SubmitTick(Tick{
		Symbol: "BTC-USD", Price: 100,
		High: pos.TakeProfit + 1, Low: pos.StopLoss - 1, TS: time.Now(),
	})
	waitFor(t, func() bool { return e.Stats().Exits >= 1 })

	if b.Cash() >= 10_000 {
		t.Fatalf("cash = %v, want a loss (stop assumed first)", b.Cash())
	}
}

func TestOpposingDecisionFlipsThePosition(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	sell := buy("BTC-USD", 0.9)
	sell.Side = types.Sell
	if _, err := e.SubmitDecision(context.Background(), sell); err != nil {
		t.Fatal(err)
	}
	if got := b.Position("BTC-USD").Qty; got >= 0 {
		t.Fatalf("qty = %v, want a net short after the flip", got)
	}
}

func TestKillSwitchHaltsTrading(t *testing.T) {
	cfg := risk.Balanced()
	// The stop loss caps a single trade's loss well below 1% of equity, so a
	// one-trade test needs a threshold under the per-trade risk to trip at all.
	// That the two interact this way is the point: brackets are the first line
	// of defense and the kill switch is the backstop.
	cfg.MaxDailyDrawdownPct = 0.002
	e, _, _ := newTestEngine(t, 10_000, cfg)
	drain(e)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	// Collapse the price so the drawdown limit is breached.
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 50, High: 100, Low: 50, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Halted })

	res, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.99))
	if err != nil {
		t.Fatal(err)
	}
	if res.Verdict != risk.RejectHalted {
		t.Fatalf("verdict = %q, want %q", res.Verdict, risk.RejectHalted)
	}
}

func TestSubmitAfterStopIsRejected(t *testing.T) {
	b := book.New(book.Options{StartingCash: 10_000})
	e := New(b, risk.New(risk.Balanced()))
	e.Start(context.Background())
	drain(e)
	e.Stop()

	if err := e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100}); err != ErrClosed {
		t.Errorf("SubmitTick after Stop = %v, want ErrClosed", err)
	}
	if _, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9)); err != ErrClosed {
		t.Errorf("SubmitDecision after Stop = %v, want ErrClosed", err)
	}
	e.Stop() // must be idempotent
}

// The engine must survive concurrent producers; run with -race.
func TestConcurrentSubmitters(t *testing.T) {
	e, _, _ := newTestEngine(t, 1_000_000, risk.Balanced())
	drain(e)

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 250; j++ {
				e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100 + float64(j%7), TS: time.Now()})
				if j%25 == 0 {
					ctx, cancel := context.WithTimeout(context.Background(), time.Second)
					e.SubmitDecision(ctx, buy("BTC-USD", 0.9))
					cancel()
				}
			}
		}(i)
	}
	wg.Wait()

	if got := e.Stats().Ticks; got == 0 {
		t.Fatal("no ticks processed under concurrency")
	}
}

// waitFor polls cond until it holds or the deadline passes.
func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("timed out waiting for engine state")
}

// BenchmarkPipeline measures end-to-end decision throughput including the
// channel hop to the owner goroutine.
func BenchmarkPipeline(b *testing.B) {
	bk := book.New(book.Options{StartingCash: 10_000_000})
	e := New(bk, risk.New(risk.Balanced()))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e.Start(ctx)
	defer e.Stop()
	go func() {
		for range e.Events() {
		}
	}()

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	time.Sleep(10 * time.Millisecond)
	d := buy("BTC-USD", 0.9)

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		e.SubmitDecision(ctx, d)
	}
}
