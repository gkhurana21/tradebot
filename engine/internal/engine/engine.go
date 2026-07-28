// Package engine wires market data and trade decisions into the risk gate and
// the book.
//
// The concurrency model is deliberately simple: a single owner goroutine
// serializes every state mutation, and all input arrives over channels. That
// means the book and risk engine are never contended, ordering is
// deterministic, and there is no lock to forget -- the design removes the
// class of bug rather than defending against it.
package engine

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
	"github.com/gkhurana21/tradebot/engine/internal/types"
	"github.com/gkhurana21/tradebot/engine/internal/venue"
)

// venueTimeout bounds any single call to a trading venue. A hung broker must
// not wedge the engine's owner goroutine, which serializes all state.
const venueTimeout = 10 * time.Second

// ErrClosed is returned when work is submitted to a stopped engine.
var ErrClosed = errors.New("engine: closed")

// Tick is a market data update.
type Tick struct {
	Symbol string
	Price  float64
	High   float64 // intrabar extremes; zero means "use Price"
	Low    float64
	TS     time.Time
}

// EventKind classifies an engine event.
type EventKind string

const (
	EventFill     EventKind = "fill"
	EventExit     EventKind = "exit"
	EventRejected EventKind = "rejected"
	EventHalt     EventKind = "halt"
)

// Event is an observable outcome. Events are emitted on a buffered channel and
// dropped if no consumer keeps up -- telemetry must never stall trading.
type Event struct {
	Kind    EventKind    `json:"event"`
	TS      time.Time    `json:"ts"`
	Symbol  string       `json:"symbol,omitempty"`
	Side    types.Side   `json:"side,omitempty"`
	Qty     float64      `json:"qty,omitempty"`
	Price   float64      `json:"price,omitempty"`
	Equity  float64      `json:"equity"`
	Verdict risk.Verdict `json:"verdict,omitempty"`
	Reason  string       `json:"reason,omitempty"`
}

// Stats are cumulative counters for observability.
type Stats struct {
	Ticks     uint64  `json:"ticks"`
	Decisions uint64  `json:"decisions"`
	Fills     uint64  `json:"fills"`
	Rejected  uint64  `json:"rejected"`
	Exits     uint64  `json:"exits"`
	Halted    bool    `json:"halted"`
	Equity    float64 `json:"equity"`
}

// Engine owns the book and risk gate and serializes all mutations.
type Engine struct {
	book *book.Book
	risk *risk.Engine

	// venue is nil for simulated execution. When set, approved orders are
	// sent to it and the book records the fills it actually reports.
	venue   venue.Venue
	baseCtx context.Context

	ticks     chan Tick
	decisions chan decisionReq
	events    chan Event

	startOnce sync.Once
	stopOnce  sync.Once
	done      chan struct{}
	wg        sync.WaitGroup
	closed    atomic.Bool

	nTicks, nDecisions, nFills, nRejected, nExits atomic.Uint64
	nOrders                                       atomic.Uint64
}

type decisionReq struct {
	decision types.Decision
	result   chan risk.Result // optional; nil for fire-and-forget
}

// Option configures an Engine.
type Option func(*Engine)

// WithVenue routes approved orders to a real trading venue instead of the
// simulated book.
//
// There is deliberately no way to set this after construction: whether a
// process can move real money is decided once, at startup, by the operator --
// never toggled at runtime by a request.
func WithVenue(v venue.Venue) Option {
	return func(e *Engine) { e.venue = v }
}

// New returns an engine bound to b and r. Call Start before submitting work.
func New(b *book.Book, r *risk.Engine, opts ...Option) *Engine {
	e := &Engine{
		book:      b,
		risk:      r,
		baseCtx:   context.Background(),
		ticks:     make(chan Tick, 1024),
		decisions: make(chan decisionReq, 256),
		events:    make(chan Event, 1024),
		done:      make(chan struct{}),
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// Venue returns the attached venue, or nil when execution is simulated.
func (e *Engine) Venue() venue.Venue { return e.venue }

// Events returns the read-only event stream.
func (e *Engine) Events() <-chan Event { return e.events }

// Start launches the owner goroutine. It is safe to call more than once; only
// the first call has an effect.
func (e *Engine) Start(ctx context.Context) {
	e.startOnce.Do(func() {
		e.baseCtx = ctx
		e.risk.StartDay(e.book.Equity())
		e.wg.Add(1)
		go e.run(ctx)
	})
}

// Stop shuts the engine down and waits for in-flight work to drain. It is
// idempotent and safe to call from any goroutine.
func (e *Engine) Stop() {
	e.stopOnce.Do(func() {
		e.closed.Store(true)
		close(e.done)
		e.wg.Wait()
		close(e.events)
	})
}

// SubmitTick delivers market data. It never blocks: if the buffer is full the
// tick is dropped, because stale prices are worse than missing ones.
func (e *Engine) SubmitTick(t Tick) error {
	if e.closed.Load() {
		return ErrClosed
	}
	select {
	case e.ticks <- t:
		return nil
	default:
		return nil // intentionally dropped under backpressure
	}
}

// SubmitDecision evaluates a decision and blocks until the owner goroutine
// returns the verdict, or ctx is cancelled.
func (e *Engine) SubmitDecision(ctx context.Context, d types.Decision) (risk.Result, error) {
	if e.closed.Load() {
		return risk.Result{}, ErrClosed
	}
	reply := make(chan risk.Result, 1)
	select {
	case e.decisions <- decisionReq{decision: d, result: reply}:
	case <-ctx.Done():
		return risk.Result{}, ctx.Err()
	case <-e.done:
		return risk.Result{}, ErrClosed
	}
	select {
	case res := <-reply:
		return res, nil
	case <-ctx.Done():
		return risk.Result{}, ctx.Err()
	case <-e.done:
		return risk.Result{}, ErrClosed
	}
}

// Stats returns a consistent counter snapshot.
func (e *Engine) Stats() Stats {
	return Stats{
		Ticks:     e.nTicks.Load(),
		Decisions: e.nDecisions.Load(),
		Fills:     e.nFills.Load(),
		Rejected:  e.nRejected.Load(),
		Exits:     e.nExits.Load(),
		Halted:    e.risk.Halted(),
		Equity:    e.book.Equity(),
	}
}

// run is the single owner goroutine. Every book and risk mutation happens
// here, so no lock is required between them.
func (e *Engine) run(ctx context.Context) {
	defer e.wg.Done()
	for {
		select {
		case <-ctx.Done():
			return
		case <-e.done:
			return
		case t := <-e.ticks:
			e.handleTick(t)
		case req := <-e.decisions:
			res := e.handleDecision(req.decision)
			if req.result != nil {
				req.result <- res
			}
		}
	}
}

func (e *Engine) handleTick(t Tick) {
	e.nTicks.Add(1)
	e.book.Mark(t.Symbol, t.Price)
	e.checkBrackets(t)

	if e.risk.CheckKillSwitch(e.book.Equity()) {
		e.emit(Event{
			Kind: EventHalt, TS: t.TS, Equity: e.book.Equity(),
			Reason: "daily drawdown kill switch tripped",
		})
	}
}

// checkBrackets closes a position whose stop or target was touched this tick.
// The stop is evaluated first: when a bar straddles both levels we cannot know
// which came first, so we assume the adverse one to avoid flattering results.
func (e *Engine) checkBrackets(t Tick) {
	pos := e.book.Position(t.Symbol)
	if pos.IsFlat() {
		return
	}
	high, low := t.High, t.Low
	if high <= 0 {
		high = t.Price
	}
	if low <= 0 {
		low = t.Price
	}
	long := pos.Qty > 0

	hitStop := pos.StopLoss > 0 && ((long && low <= pos.StopLoss) || (!long && high >= pos.StopLoss))
	if hitStop {
		e.exitAt(t, pos.StopLoss, "stop")
		return
	}
	hitTarget := pos.TakeProfit > 0 && ((long && high >= pos.TakeProfit) || (!long && low <= pos.TakeProfit))
	if hitTarget {
		e.exitAt(t, pos.TakeProfit, "target")
	}
}

// exitAt flattens a position whose bracket was touched.
//
// With a venue attached the exit is a real market order -- Alpaca does not
// hold brackets on crypto, so the engine is the only thing watching the stop.
// Simulated mode fills at the exact bracket level instead.
func (e *Engine) exitAt(t Tick, level float64, reason string) {
	if e.venue != nil {
		before := e.nExits.Load()
		e.closePosition(t.Symbol, reason)
		if e.nExits.Load() > before {
			e.emit(Event{
				Kind: EventExit, TS: t.TS, Symbol: t.Symbol,
				Equity: e.book.Equity(), Reason: reason,
			})
		}
		return
	}

	e.book.Mark(t.Symbol, level)
	fill, ok := e.book.Close(t.Symbol, reason)
	e.book.Mark(t.Symbol, t.Price)
	if !ok {
		return
	}
	e.nExits.Add(1)
	e.emit(Event{
		Kind: EventExit, TS: t.TS, Symbol: t.Symbol, Side: fill.Side,
		Qty: fill.Qty, Price: fill.Price, Equity: e.book.Equity(), Reason: reason,
	})
}

func (e *Engine) handleDecision(d types.Decision) risk.Result {
	e.nDecisions.Add(1)

	price, ok := e.book.Price(d.Symbol)
	if !ok {
		res := risk.Result{Verdict: risk.RejectInvalid, Reason: "no market data for symbol"}
		e.nRejected.Add(1)
		e.emit(Event{
			Kind: EventRejected, TS: d.TS, Symbol: d.Symbol,
			Verdict: res.Verdict, Reason: res.Reason, Equity: e.book.Equity(),
		})
		return res
	}

	equity, positions := e.book.Snapshot()
	res := e.risk.Evaluate(d, risk.Account{Equity: equity, Positions: positions}, price)
	if !res.Verdict.OK() {
		e.nRejected.Add(1)
		e.emit(Event{
			Kind: EventRejected, TS: d.TS, Symbol: d.Symbol, Side: d.Side,
			Verdict: res.Verdict, Reason: res.Reason, Equity: equity,
		})
		return res
	}

	// Flip: flatten an opposing position before entering the other way, so we
	// never hold both directions at once.
	if pos := positions[d.Symbol]; !pos.IsFlat() && d.Side.Opposes(pos.Qty) {
		e.closePosition(d.Symbol, "flip")
	}

	var fill types.Fill
	if e.venue != nil {
		var err error
		fill, err = e.placeAtVenue(res.Order, positions[d.Symbol])
		if err != nil {
			res.Verdict, res.Reason = risk.RejectInvalid, err.Error()
			e.nRejected.Add(1)
			e.emit(Event{
				Kind: EventRejected, TS: d.TS, Symbol: d.Symbol, Side: d.Side,
				Verdict: res.Verdict, Reason: res.Reason, Equity: e.book.Equity(),
			})
			return res
		}
		// A venue can accept an order without filling it yet. Report that
		// honestly rather than counting a fill that has not happened.
		if fill.Qty <= 0 {
			res.Reason = "accepted by venue, not yet filled"
			return res
		}
		e.book.RecordFill(fill, res.Order.StopLoss, res.Order.TakeProfit)
	} else {
		var ok bool
		fill, ok = e.book.Execute(res.Order, res.Order.Tag)
		if !ok {
			res.Verdict, res.Reason = risk.RejectInvalid, "execution rejected by book"
			e.nRejected.Add(1)
			return res
		}
	}

	e.nFills.Add(1)
	e.emit(Event{
		Kind: EventFill, TS: fill.TS, Symbol: fill.Symbol, Side: fill.Side,
		Qty: fill.Qty, Price: fill.Price, Equity: e.book.Equity(),
		Verdict: res.Verdict, Reason: res.Order.Tag,
	})
	return res
}

// placeAtVenue sends an approved order to the live venue.
//
// Preflight runs first so orders the venue structurally cannot accept -- a
// short on spot crypto, say -- are caught here rather than becoming a rejected
// order on the account's record.
func (e *Engine) placeAtVenue(order types.Order, held types.Position) (types.Fill, error) {
	if err := venue.Preflight(order, held, e.venue.Capabilities()); err != nil {
		return types.Fill{}, err
	}

	ctx, cancel := context.WithTimeout(e.baseCtx, venueTimeout)
	defer cancel()

	// A stable key per logical order: a retry inside the venue client reuses
	// it, so a timeout cannot produce two positions.
	key := fmt.Sprintf("%s-%d-%d", order.Symbol, order.TS.UnixNano(), e.nOrders.Add(1))
	return e.venue.Place(ctx, order, key)
}

// closePosition flattens a symbol at the venue when one is attached, and in
// the local book otherwise.
func (e *Engine) closePosition(symbol, reason string) {
	if e.venue == nil {
		if _, closed := e.book.Close(symbol, reason); closed {
			e.nExits.Add(1)
		}
		return
	}

	ctx, cancel := context.WithTimeout(e.baseCtx, venueTimeout)
	defer cancel()

	fill, err := e.venue.Close(ctx, symbol, reason)
	if err != nil {
		// A failed close is the dangerous case: risk believes the position is
		// gone while the venue still holds it. Surface it loudly; do not
		// update the book to a state that is not real.
		e.emit(Event{
			Kind: EventRejected, Symbol: symbol,
			Reason: "CLOSE FAILED at venue, position may still be open: " + err.Error(),
			Equity: e.book.Equity(),
		})
		return
	}
	if fill.Qty > 0 {
		e.book.RecordFill(fill, 0, 0)
		e.nExits.Add(1)
	}
}

// emit publishes an event, dropping it if no consumer is keeping up.
func (e *Engine) emit(ev Event) {
	if ev.TS.IsZero() {
		ev.TS = time.Now().UTC()
	}
	select {
	case e.events <- ev:
	default:
	}
}
