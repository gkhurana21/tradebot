package engine

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
	"github.com/gkhurana21/tradebot/engine/internal/types"
	"github.com/gkhurana21/tradebot/engine/internal/venue"
)

// fakeVenue records what the engine sends and returns scripted outcomes.
type fakeVenue struct {
	mu     sync.Mutex
	placed []types.Order
	keys   []string
	closed []string

	caps      venue.Capabilities
	live      bool
	placeErr  error
	closeErr  error
	fillRatio float64 // 1.0 = full fill; 0 = accepted but unfilled
}

func newFakeVenue() *fakeVenue {
	return &fakeVenue{
		caps:      venue.Capabilities{SupportsShort: false, SupportsFractional: true},
		fillRatio: 1.0,
	}
}

func (f *fakeVenue) Name() string                     { return "fake" }
func (f *fakeVenue) IsLive() bool                     { return f.live }
func (f *fakeVenue) Capabilities() venue.Capabilities { return f.caps }

func (f *fakeVenue) Account(ctx context.Context) (venue.Account, error) {
	return venue.Account{Equity: 10_000, Cash: 10_000, BuyingPower: 10_000}, nil
}

func (f *fakeVenue) Positions(ctx context.Context) (map[string]types.Position, error) {
	return map[string]types.Position{}, nil
}

func (f *fakeVenue) Place(ctx context.Context, order types.Order, key string) (types.Fill, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.placed = append(f.placed, order)
	f.keys = append(f.keys, key)
	if f.placeErr != nil {
		return types.Fill{}, f.placeErr
	}
	return types.Fill{
		TS: time.Now().UTC(), Symbol: order.Symbol, Side: order.Side,
		Qty: order.Qty * f.fillRatio, Price: 100, Reason: "venue",
	}, nil
}

func (f *fakeVenue) Close(ctx context.Context, symbol, reason string) (types.Fill, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed = append(f.closed, symbol)
	if f.closeErr != nil {
		return types.Fill{}, f.closeErr
	}
	return types.Fill{
		TS: time.Now().UTC(), Symbol: symbol, Side: types.Sell,
		Qty: 1, Price: 100, Reason: reason,
	}, nil
}

func (f *fakeVenue) snapshot() ([]types.Order, []string, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	o := make([]types.Order, len(f.placed))
	copy(o, f.placed)
	k := make([]string, len(f.keys))
	copy(k, f.keys)
	c := make([]string, len(f.closed))
	copy(c, f.closed)
	return o, k, c
}

func venueEngine(t *testing.T, fv *fakeVenue) (*Engine, *book.Book) {
	t.Helper()
	b := book.New(book.Options{StartingCash: 10_000})
	e := New(b, risk.New(risk.Balanced()), WithVenue(fv))
	ctx, cancel := context.WithCancel(context.Background())
	e.Start(ctx)
	t.Cleanup(func() { e.Stop(); cancel() })
	drain(e)
	return e, b
}

func TestApprovedOrderReachesTheVenue(t *testing.T) {
	fv := newFakeVenue()
	e, b := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	res, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if !res.Verdict.OK() {
		t.Fatalf("verdict = %q (%s)", res.Verdict, res.Reason)
	}

	placed, keys, _ := fv.snapshot()
	if len(placed) != 1 {
		t.Fatalf("venue received %d orders, want 1", len(placed))
	}
	if keys[0] == "" {
		t.Error("no idempotency key sent; a retry could double-fill")
	}
	// The book must reflect the venue's fill, not a simulated one.
	if b.Position("BTC-USD").IsFlat() {
		t.Error("book did not record the venue fill")
	}
	if e.Stats().Fills != 1 {
		t.Errorf("fills = %d, want 1", e.Stats().Fills)
	}
}

// The risk gate runs before the venue. A rejected decision must never reach a
// broker at all -- that ordering is the whole safety argument.
func TestRejectedDecisionNeverReachesTheVenue(t *testing.T) {
	fv := newFakeVenue()
	e, _ := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	res, _ := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.05)) // below min confidence
	if res.Verdict.OK() {
		t.Fatal("expected rejection")
	}
	if placed, _, _ := fv.snapshot(); len(placed) != 0 {
		t.Fatalf("venue received %d orders from a rejected decision, want 0", len(placed))
	}
}

// Spot crypto cannot be shorted. A sell with no inventory must be stopped by
// preflight rather than sent and rejected by the broker.
func TestShortIsBlockedBeforeSending(t *testing.T) {
	fv := newFakeVenue() // SupportsShort: false
	e, _ := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	sell := buy("BTC-USD", 0.9)
	sell.Side = types.Sell
	res, _ := e.SubmitDecision(context.Background(), sell)

	if res.Verdict.OK() {
		t.Fatal("engine approved a short on a venue that cannot short")
	}
	if placed, _, _ := fv.snapshot(); len(placed) != 0 {
		t.Fatalf("venue received %d short orders, want 0", len(placed))
	}
}

// A venue that accepts without filling must not be counted as a fill; the
// local book would otherwise claim a position the account does not hold.
func TestAcceptedButUnfilledIsNotAFill(t *testing.T) {
	fv := newFakeVenue()
	fv.fillRatio = 0
	e, b := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	if got := e.Stats().Fills; got != 0 {
		t.Errorf("fills = %d, want 0 for an unfilled order", got)
	}
	if !b.Position("BTC-USD").IsFlat() {
		t.Error("book recorded a position the venue never filled")
	}
}

func TestVenueRejectionIsReportedNotSwallowed(t *testing.T) {
	fv := newFakeVenue()
	fv.placeErr = errors.New("insufficient buying power")
	e, b := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	res, _ := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))
	if res.Verdict.OK() {
		t.Fatal("a venue error must not report success")
	}
	if !b.Position("BTC-USD").IsFlat() {
		t.Error("book recorded a position for an order the venue refused")
	}
}

// The dangerous case: a close that fails at the venue. Risk would believe the
// position is gone while the broker still holds it, so the book must NOT be
// updated and the failure must surface.
func TestFailedCloseLeavesTheBookHonest(t *testing.T) {
	fv := newFakeVenue()
	e, b := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	qtyBefore := b.Position("BTC-USD").Qty
	if qtyBefore == 0 {
		t.Fatal("setup failed: no position to close")
	}

	// Now break the venue and trip the stop.
	fv.mu.Lock()
	fv.closeErr = errors.New("venue unreachable")
	fv.mu.Unlock()

	stop := b.Position("BTC-USD").StopLoss
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: stop - 1, High: 100, Low: stop - 1, TS: time.Now()})
	waitFor(t, func() bool {
		_, _, closed := fv.snapshot()
		return len(closed) > 0
	})

	if got := b.Position("BTC-USD").Qty; got != qtyBefore {
		t.Errorf("book qty = %v, want %v unchanged: the close failed, so the "+
			"position is still open at the venue", got, qtyBefore)
	}
	if e.Stats().Exits != 0 {
		t.Errorf("exits = %d, want 0 when the venue close failed", e.Stats().Exits)
	}
}

// Brackets are enforced locally because the venue does not hold them for
// crypto -- a touched stop must still produce a real close order.
func TestBracketExitSendsARealCloseOrder(t *testing.T) {
	fv := newFakeVenue()
	e, b := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))

	stop := b.Position("BTC-USD").StopLoss
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: stop - 1, High: 100, Low: stop - 1, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Exits >= 1 })

	_, _, closed := fv.snapshot()
	if len(closed) == 0 || closed[0] != "BTC-USD" {
		t.Fatalf("venue close calls = %v, want a close for BTC-USD", closed)
	}
}

func TestIdempotencyKeysAreUnique(t *testing.T) {
	fv := newFakeVenue()
	e, _ := venueEngine(t, fv)

	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })

	for i := 0; i < 3; i++ {
		e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))
	}

	_, keys, _ := fv.snapshot()
	seen := map[string]bool{}
	for _, k := range keys {
		if seen[k] {
			t.Fatalf("duplicate idempotency key %q: distinct orders would be "+
				"deduplicated into one", k)
		}
		seen[k] = true
	}
}

func TestSimulatedModeIsUnaffected(t *testing.T) {
	// No venue: the engine must behave exactly as before.
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	if e.Venue() != nil {
		t.Fatal("expected no venue in simulated mode")
	}
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	if _, err := e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9)); err != nil {
		t.Fatal(err)
	}
	if b.Position("BTC-USD").IsFlat() {
		t.Error("simulated execution stopped working")
	}
}

func TestStatsReportWhereOrdersGo(t *testing.T) {
	sim := New(book.New(book.Options{StartingCash: 10_000}), risk.New(risk.Balanced()))
	if s := sim.Stats(); s.Venue != "sim" || s.Live {
		t.Errorf("no venue: got venue=%q live=%v, want sim/false", s.Venue, s.Live)
	}

	paper := newFakeVenue()
	e, _ := venueEngine(t, paper)
	if s := e.Stats(); s.Venue != "fake" || s.Live {
		t.Errorf("paper venue: got venue=%q live=%v, want fake/false", s.Venue, s.Live)
	}

	live := newFakeVenue()
	live.live = true
	e, _ = venueEngine(t, live)
	if s := e.Stats(); !s.Live {
		t.Error("a live venue must be reported as live")
	}
}

func TestFlipIsRefusedWhenTheCloseLegFails(t *testing.T) {
	fv := newFakeVenue()
	e, b := venueEngine(t, fv)
	e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	waitFor(t, func() bool { return e.Stats().Ticks >= 1 })
	e.SubmitDecision(context.Background(), buy("BTC-USD", 0.9))
	held := b.Position("BTC-USD").Qty
	if held <= 0 {
		t.Fatal("setup failed: no long position")
	}
	fv.mu.Lock()
	fv.closeErr = errors.New("venue unreachable")
	fv.mu.Unlock()

	out, err := e.SubmitDecisionFull(context.Background(), sell("BTC-USD", 0.9), "")
	if err != nil {
		t.Fatal(err)
	}
	if out.Result.Verdict.OK() {
		t.Fatalf("verdict = %s, want a rejection when the flip close failed", out.Result.Verdict)
	}
	if got := b.Position("BTC-USD").Qty; got != held {
		t.Errorf("book qty = %v, want %v: nothing should have been entered", got, held)
	}
}
