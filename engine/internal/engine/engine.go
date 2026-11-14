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

// ErrBusy is returned when a synchronous tick cannot be queued because the
// tick buffer is full. The caller must treat the tick as not applied.
var ErrBusy = errors.New("engine: tick buffer full")

// simDecisionTimeout bounds a decision when execution is simulated: the owner
// goroutine answers immediately, so this only guards a wedged engine.
const simDecisionTimeout = 5 * time.Second

// idempotencyWindow is how many recent client_ids are remembered.
const idempotencyWindow = 4096

// Tick is a market data update.
type Tick struct {
	Symbol string
	Price  float64
	High   float64 // intrabar extremes; zero means "use Price"
	Low    float64
	TS     time.Time

	// reply, when set, receives the outcome once the owner goroutine has
	// applied the tick. Buffered (cap 1) so the owner never blocks on it.
	reply chan TickResult
}

// TickResult is the outcome of a synchronously applied tick.
type TickResult struct {
	Equity float64      `json:"equity"`
	Halted bool         `json:"halted"`
	Exits  []types.Fill `json:"exits"`
}

// Limits are the hard risk limits in force, as configured fractions
// (0.05 means 5%).
type Limits struct {
	MaxDailyDrawdownPct float64 `json:"max_daily_drawdown_pct"`
	MaxOpenPositions    int     `json:"max_open_positions"`
	MinConfidence       float64 `json:"min_confidence"`
	MaxPositionPct      float64 `json:"max_position_pct"`
}

// DecisionOutcome is everything one decision produced: the verdict and every
// fill it caused, including the close leg of a flip.
type DecisionOutcome struct {
	Result   risk.Result
	Fills    []types.Fill
	ClientID string
	// Replayed is true when this is the stored outcome of an earlier request
	// with the same client_id; no new order was considered.
	Replayed bool
}

// pendingDecision carries one decision through the owner goroutine and lets
// any number of callers wait for its outcome. done is closed after out is set.
type pendingDecision struct {
	decision types.Decision
	clientID string
	done     chan struct{}
	out      DecisionOutcome
}

// idempotency remembers the last idempotencyWindow client_ids so a retried
// request returns the original outcome instead of placing a second order. It
// has its own mutex because HTTP handlers touch it concurrently; it never
// touches book or risk state.
type idempotency struct {
	mu   sync.Mutex
	byID map[string]*pendingDecision
	ring [idempotencyWindow]*pendingDecision
	next int
}

// claim registers p under its client_id. If the id is already known it
// returns the earlier entry and true.
func (m *idempotency) claim(p *pendingDecision) (*pendingDecision, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if prev, ok := m.byID[p.clientID]; ok {
		return prev, true
	}
	if old := m.ring[m.next]; old != nil && m.byID[old.clientID] == old {
		delete(m.byID, old.clientID)
	}
	m.ring[m.next] = p
	m.next = (m.next + 1) % idempotencyWindow
	m.byID[p.clientID] = p
	return p, false
}

// forget removes p when it never reached the owner goroutine, so a retry can
// run fresh.
func (m *idempotency) forget(p *pendingDecision) {
	m.mu.Lock()
	if m.byID[p.clientID] == p {
		delete(m.byID, p.clientID)
	}
	m.mu.Unlock()
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
	// Venue names where approved orders go ("sim" when simulated) and Live
	// says whether they risk real money, so a monitor never has to guess.
	Venue string `json:"venue"`
	Live  bool   `json:"live"`
	// SessionStartEquity is the kill-switch baseline (process lifetime).
	SessionStartEquity float64 `json:"session_start_equity"`
	Limits             Limits  `json:"limits"`
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
	decisions chan *pendingDecision
	events    chan Event

	journal *journal
	idem    idempotency

	startOnce sync.Once
	stopOnce  sync.Once
	done      chan struct{}
	wg        sync.WaitGroup
	closed    atomic.Bool

	nTicks, nDecisions, nFills, nRejected, nExits atomic.Uint64
	nOrders                                       atomic.Uint64
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
		decisions: make(chan *pendingDecision, 256),
		events:    make(chan Event, 1024),
		done:      make(chan struct{}),
		journal:   newJournal(),
		idem:      idempotency{byID: make(map[string]*pendingDecision)},
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

// SubmitTickWait delivers market data and blocks until the owner goroutine has
// applied it, returning the exits it caused and the halt state. Unlike
// SubmitTick it never drops: a full buffer returns ErrBusy so the caller knows
// the tick was not applied. If ctx expires after the tick was queued it is
// still applied; only the reply is lost.
func (e *Engine) SubmitTickWait(ctx context.Context, t Tick) (TickResult, error) {
	if e.closed.Load() {
		return TickResult{}, ErrClosed
	}
	t.reply = make(chan TickResult, 1)
	select {
	case e.ticks <- t:
	default:
		return TickResult{}, ErrBusy
	}
	select {
	case res := <-t.reply:
		return res, nil
	case <-ctx.Done():
		return TickResult{}, ctx.Err()
	case <-e.done:
		return TickResult{}, ErrClosed
	}
}

// SubmitDecision evaluates a decision and blocks until the owner goroutine
// returns the verdict, or ctx is cancelled.
func (e *Engine) SubmitDecision(ctx context.Context, d types.Decision) (risk.Result, error) {
	out, err := e.SubmitDecisionFull(ctx, d, "")
	return out.Result, err
}

// SubmitDecisionFull is SubmitDecision with a client_id and the full outcome
// (fills included). A non-empty clientID is recorded on arrival: a repeat
// returns the stored outcome (waiting for the original if it is still in
// flight) and never evaluates the decision a second time.
func (e *Engine) SubmitDecisionFull(ctx context.Context, d types.Decision, clientID string) (DecisionOutcome, error) {
	if e.closed.Load() {
		return DecisionOutcome{}, ErrClosed
	}
	p := &pendingDecision{decision: d, clientID: clientID, done: make(chan struct{})}
	if clientID != "" {
		if prev, replay := e.idem.claim(p); replay {
			out, err := e.await(ctx, prev)
			out.Replayed = true
			return out, err
		}
	}
	select {
	case e.decisions <- p:
	case <-ctx.Done():
		e.idem.forget(p)
		return DecisionOutcome{}, ctx.Err()
	case <-e.done:
		e.idem.forget(p)
		return DecisionOutcome{}, ErrClosed
	}
	return e.await(ctx, p)
}

func (e *Engine) await(ctx context.Context, p *pendingDecision) (DecisionOutcome, error) {
	select {
	case <-p.done:
		return p.out, nil
	case <-ctx.Done():
		return DecisionOutcome{}, ctx.Err()
	case <-e.done:
		return DecisionOutcome{}, ErrClosed
	}
}

// DecisionTimeout is how long an HTTP caller should wait for a decision. In
// simulation the owner answers immediately. With a venue the worst case is a
// flip: two venue calls (close, then place), each bounded by venueTimeout --
// retries happen inside that bound, they do not extend it -- plus margin.
func (e *Engine) DecisionTimeout() time.Duration {
	if e.venue == nil {
		return simDecisionTimeout
	}
	return 2*venueTimeout + 2*time.Second
}

// Journal returns recent outcomes with seq >= since (all retained when since
// is nil).
func (e *Engine) Journal(since *uint64) JournalPage { return e.journal.read(since) }

// Stats returns a consistent counter snapshot.
func (e *Engine) Stats() Stats {
	s := Stats{
		Ticks:     e.nTicks.Load(),
		Decisions: e.nDecisions.Load(),
		Fills:     e.nFills.Load(),
		Rejected:  e.nRejected.Load(),
		Exits:     e.nExits.Load(),
		Halted:    e.risk.Halted(),
		Equity:    e.book.Equity(),
		Venue:     "sim",

		SessionStartEquity: e.risk.SessionStartEquity(),
	}
	cfg := e.risk.Config()
	s.Limits = Limits{
		MaxDailyDrawdownPct: cfg.MaxDailyDrawdownPct,
		MaxOpenPositions:    cfg.MaxOpenPositions,
		MinConfidence:       cfg.MinConfidence,
		MaxPositionPct:      cfg.MaxPositionPct,
	}
	if e.venue != nil {
		s.Venue, s.Live = e.venue.Name(), e.venue.IsLive()
	}
	return s
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
			res := e.handleTick(t)
			if t.reply != nil {
				t.reply <- res
			}
		case p := <-e.decisions:
			p.out = e.handleDecision(p)
			close(p.done)
		}
	}
}

func (e *Engine) handleTick(t Tick) TickResult {
	e.nTicks.Add(1)
	e.book.Mark(t.Symbol, t.Price)
	exits := []types.Fill{}
	if fill, ok := e.checkBrackets(t); ok {
		exits = append(exits, fill)
	}

	wasHalted := e.risk.Halted()
	equity := e.book.Equity()
	halted := e.risk.CheckKillSwitch(equity)
	if halted {
		e.emit(Event{
			Kind: EventHalt, TS: t.TS, Equity: equity,
			Reason: "daily drawdown kill switch tripped",
		})
		if !wasHalted {
			e.journal.append(JournalEntry{
				Kind: JournalHalt, Reason: e.risk.HaltReason(equity),
			})
		}
	}
	return TickResult{Equity: equity, Halted: halted, Exits: exits}
}

// checkBrackets closes a position whose stop or target was touched this tick.
// The stop is evaluated first: when a bar straddles both levels we cannot know
// which came first, so we assume the adverse one to avoid flattering results.
func (e *Engine) checkBrackets(t Tick) (types.Fill, bool) {
	pos := e.book.Position(t.Symbol)
	if pos.IsFlat() {
		return types.Fill{}, false
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
		return e.exitAt(t, pos.StopLoss, "stop")
	}
	hitTarget := pos.TakeProfit > 0 && ((long && high >= pos.TakeProfit) || (!long && low <= pos.TakeProfit))
	if hitTarget {
		return e.exitAt(t, pos.TakeProfit, "target")
	}
	return types.Fill{}, false
}

// exitAt flattens a position whose bracket was touched.
//
// With a venue attached the exit is a real market order -- Alpaca does not
// hold brackets on crypto, so the engine is the only thing watching the stop.
// Simulated mode fills at the exact bracket level instead.
func (e *Engine) exitAt(t Tick, level float64, reason string) (types.Fill, bool) {
	if e.venue != nil {
		fill, ok := e.closePosition(t.Symbol, reason)
		if ok {
			e.emit(Event{
				Kind: EventExit, TS: t.TS, Symbol: t.Symbol,
				Equity: e.book.Equity(), Reason: reason,
			})
			e.journalExit("", fill)
		}
		return fill, ok
	}

	e.book.Mark(t.Symbol, level)
	fill, ok := e.book.Close(t.Symbol, reason)
	e.book.Mark(t.Symbol, t.Price)
	if !ok {
		return types.Fill{}, false
	}
	e.nExits.Add(1)
	e.emit(Event{
		Kind: EventExit, TS: t.TS, Symbol: t.Symbol, Side: fill.Side,
		Qty: fill.Qty, Price: fill.Price, Equity: e.book.Equity(), Reason: reason,
	})
	e.journalExit("", fill)
	return fill, true
}

// journalExit records a position close (bracket exit or flip leg).
func (e *Engine) journalExit(clientID string, f types.Fill) {
	qty, price := f.Qty, f.Price
	e.journal.append(JournalEntry{
		Kind: JournalExit, ClientID: clientID, Symbol: f.Symbol, Side: f.Side,
		Reason: fmt.Sprintf("%s: closed %s @ %.2f", f.Reason, f.Symbol, f.Price),
		Qty:    &qty, Price: &price,
	})
}

// journalDecision records one decision outcome. fill is the entry fill, if any.
func (e *Engine) journalDecision(p *pendingDecision, res risk.Result, fill *types.Fill) {
	conf, approved := p.decision.Confidence, res.Verdict.OK()
	entry := JournalEntry{
		Kind: JournalDecision, ClientID: p.clientID, Symbol: p.decision.Symbol,
		Side: p.decision.Side, Confidence: &conf, Verdict: string(res.Verdict),
		Approved: &approved, Reason: res.Reason,
	}
	if fill != nil {
		qty, price := fill.Qty, fill.Price
		entry.Qty, entry.Price = &qty, &price
	}
	e.journal.append(entry)
}

func (e *Engine) handleDecision(p *pendingDecision) DecisionOutcome {
	d := p.decision
	e.nDecisions.Add(1)
	out := DecisionOutcome{ClientID: p.clientID, Fills: []types.Fill{}}

	price, ok := e.book.Price(d.Symbol)
	if !ok {
		res := risk.Result{Verdict: risk.RejectInvalid, Reason: "no market data for symbol"}
		e.nRejected.Add(1)
		e.emit(Event{
			Kind: EventRejected, TS: d.TS, Symbol: d.Symbol,
			Verdict: res.Verdict, Reason: res.Reason, Equity: e.book.Equity(),
		})
		e.journalDecision(p, res, nil)
		out.Result = res
		return out
	}

	equity, positions := e.book.Snapshot()
	acct := risk.Account{Equity: equity, Positions: positions}
	res := e.risk.Evaluate(d, acct, price)
	res.Reason = e.risk.Explain(d, acct, price, res)
	if !res.Verdict.OK() {
		e.nRejected.Add(1)
		e.emit(Event{
			Kind: EventRejected, TS: d.TS, Symbol: d.Symbol, Side: d.Side,
			Verdict: res.Verdict, Reason: res.Reason, Equity: equity,
		})
		e.journalDecision(p, res, nil)
		out.Result = res
		return out
	}

	// Flip: flatten an opposing position before entering the other way, so we
	// never hold both directions at once. The close leg is reported to the
	// caller and journaled; it is an exit, not a fill, in the counters.
	if pos := positions[d.Symbol]; !pos.IsFlat() && d.Side.Opposes(pos.Qty) {
		if closeFill, closed := e.closePosition(d.Symbol, "flip"); closed {
			out.Fills = append(out.Fills, closeFill)
			e.journalExit(p.clientID, closeFill)
		}
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
			e.journalDecision(p, res, nil)
			out.Result = res
			return out
		}
		// A venue can accept an order without filling it yet. Report that
		// honestly rather than counting a fill that has not happened.
		if fill.Qty <= 0 {
			res.Reason = "accepted by venue, not yet filled"
			e.journalDecision(p, res, nil)
			out.Result = res
			return out
		}
		e.book.RecordFill(fill, res.Order.StopLoss, res.Order.TakeProfit)
	} else {
		var ok bool
		fill, ok = e.book.Execute(res.Order, res.Order.Tag)
		if !ok {
			res.Verdict, res.Reason = risk.RejectInvalid, "execution rejected by book"
			e.nRejected.Add(1)
			e.journalDecision(p, res, nil)
			out.Result = res
			return out
		}
	}

	e.nFills.Add(1)
	e.emit(Event{
		Kind: EventFill, TS: fill.TS, Symbol: fill.Symbol, Side: fill.Side,
		Qty: fill.Qty, Price: fill.Price, Equity: e.book.Equity(),
		Verdict: res.Verdict, Reason: res.Order.Tag,
	})
	out.Fills = append(out.Fills, fill)
	e.journalDecision(p, res, &fill)
	out.Result = res
	return out
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
// the local book otherwise. It returns the closing fill when one happened.
func (e *Engine) closePosition(symbol, reason string) (types.Fill, bool) {
	if e.venue == nil {
		fill, closed := e.book.Close(symbol, reason)
		if closed {
			e.nExits.Add(1)
		}
		return fill, closed
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
		return types.Fill{}, false
	}
	if fill.Qty > 0 {
		if fill.Reason == "" {
			fill.Reason = reason
		}
		e.book.RecordFill(fill, 0, 0)
		e.nExits.Add(1)
		return fill, true
	}
	return types.Fill{}, false
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
