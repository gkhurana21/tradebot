// Package book maintains the live position and cash state.
//
// The book is the single source of truth for what the system holds. Every
// mutation goes through Apply, which keeps cash, average price, and realized
// P&L consistent -- there is no path that updates one without the others.
package book

import (
	"math"
	"sync"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// Book is a concurrent-safe account: cash, positions, and mark prices.
// All exported methods are safe to call from multiple goroutines.
type Book struct {
	mu        sync.RWMutex
	cash      float64
	positions map[string]types.Position
	marks     map[string]float64
	fills     []types.Fill

	feePct      float64 // taker fee as a fraction of notional
	slippagePct float64 // adverse price movement modeled on every fill
}

// Options configures a Book. Zero values mean frictionless, which is only
// appropriate for unit tests -- real venues charge both.
type Options struct {
	StartingCash float64
	FeePct       float64
	SlippagePct  float64
}

// New returns an empty book funded with opts.StartingCash.
func New(opts Options) *Book {
	return &Book{
		cash:        opts.StartingCash,
		positions:   make(map[string]types.Position),
		marks:       make(map[string]float64),
		feePct:      opts.FeePct,
		slippagePct: opts.SlippagePct,
	}
}

// Mark records the latest price for a symbol. Equity is derived from marks,
// so a symbol with no mark contributes only its realized cash.
func (b *Book) Mark(symbol string, price float64) {
	if price <= 0 || math.IsNaN(price) {
		return
	}
	b.mu.Lock()
	b.marks[symbol] = price
	b.mu.Unlock()
}

// Price returns the last mark for symbol, and whether one exists.
func (b *Book) Price(symbol string) (float64, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	p, ok := b.marks[symbol]
	return p, ok
}

// Cash returns settled cash, excluding unrealized P&L.
func (b *Book) Cash() float64 {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.cash
}

// Equity is cash plus the unrealized P&L of every marked position.
func (b *Book) Equity() float64 {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.equityLocked()
}

func (b *Book) equityLocked() float64 {
	eq := b.cash
	for sym, pos := range b.positions {
		if pos.IsFlat() {
			continue
		}
		if mark, ok := b.marks[sym]; ok {
			eq += pos.Unrealized(mark)
		}
	}
	return eq
}

// Position returns a copy of the position in symbol. A missing symbol yields
// a zero-valued (flat) position, so callers never need a nil check.
func (b *Book) Position(symbol string) types.Position {
	b.mu.RLock()
	defer b.mu.RUnlock()
	pos, ok := b.positions[symbol]
	if !ok {
		return types.Position{Symbol: symbol}
	}
	return pos
}

// Positions returns a snapshot copy of every non-flat position.
func (b *Book) Positions() map[string]types.Position {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make(map[string]types.Position, len(b.positions))
	for sym, pos := range b.positions {
		if !pos.IsFlat() {
			out[sym] = pos
		}
	}
	return out
}

// Snapshot returns equity and a position copy atomically, so a caller never
// sees equity from one instant and positions from another.
func (b *Book) Snapshot() (equity float64, positions map[string]types.Position) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make(map[string]types.Position, len(b.positions))
	for sym, pos := range b.positions {
		out[sym] = pos
	}
	return b.equityLocked(), out
}

// Fills returns a copy of the fill history.
func (b *Book) Fills() []types.Fill {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make([]types.Fill, len(b.fills))
	copy(out, b.fills)
	return out
}

// Execute applies an order to the book at the current mark, modeling adverse
// slippage and fees, and returns the resulting fill.
//
// It reports ok=false when the symbol has no mark -- executing against an
// unknown price is never safe, so the book refuses rather than guessing.
func (b *Book) Execute(order types.Order, reason string) (types.Fill, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()

	mark, ok := b.marks[order.Symbol]
	if !ok || order.Qty <= 0 {
		return types.Fill{}, false
	}

	// Slippage always works against the taker: buys fill higher, sells lower.
	fillPrice := mark * (1 + b.slippagePct)
	if order.Side == types.Sell {
		fillPrice = mark * (1 - b.slippagePct)
	}
	fee := order.Qty * fillPrice * b.feePct

	signed := order.Qty
	if order.Side == types.Sell {
		signed = -order.Qty
	}
	b.applyLocked(order.Symbol, signed, fillPrice, fee, order.StopLoss, order.TakeProfit)

	ts := order.TS
	if ts.IsZero() {
		ts = time.Now().UTC()
	}
	fill := types.Fill{
		TS: ts, Symbol: order.Symbol, Side: order.Side,
		Qty: order.Qty, Price: fillPrice, Fee: fee, Reason: reason,
	}
	b.fills = append(b.fills, fill)
	return fill, true
}

// Close flattens the position in symbol at the current mark.
// It reports ok=false when the position is already flat or has no mark.
func (b *Book) Close(symbol, reason string) (types.Fill, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()

	pos := b.positions[symbol]
	mark, hasMark := b.marks[symbol]
	if pos.IsFlat() || !hasMark {
		return types.Fill{}, false
	}

	side := types.Sell
	fillPrice := mark * (1 - b.slippagePct)
	if pos.Qty < 0 {
		side = types.Buy
		fillPrice = mark * (1 + b.slippagePct)
	}
	qty := math.Abs(pos.Qty)
	fee := qty * fillPrice * b.feePct

	b.applyLocked(symbol, -pos.Qty, fillPrice, fee, 0, 0)

	fill := types.Fill{
		TS: time.Now().UTC(), Symbol: symbol, Side: side,
		Qty: qty, Price: fillPrice, Fee: fee, Reason: reason,
	}
	b.fills = append(b.fills, fill)
	return fill, true
}

// RecordFill applies a fill that already happened at a real venue.
//
// Distinct from Execute, which *simulates* a fill at the current mark. When a
// venue is attached the venue decides the price and quantity -- possibly a
// partial fill, possibly zero -- and the book's job is to record that truth,
// not to re-derive it. Recording a fill the venue did not give us is how a
// local book silently diverges from the real account.
//
// A zero-quantity fill (order accepted but not yet filled) is ignored.
func (b *Book) RecordFill(fill types.Fill, stop, target float64) {
	if fill.Qty <= 0 || fill.Price <= 0 {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()

	signed := fill.Qty
	if fill.Side == types.Sell {
		signed = -fill.Qty
	}
	b.applyLocked(fill.Symbol, signed, fill.Price, fill.Fee, stop, target)
	b.marks[fill.Symbol] = fill.Price
	b.fills = append(b.fills, fill)
}

// Reconcile replaces local positions and cash with the venue's view.
//
// The venue is authoritative: it knows about fills from other sessions, manual
// intervention, liquidations, and anything that happened while this process
// was down. Called at startup so the engine never begins trading against a
// stale picture of the account.
func (b *Book) Reconcile(positions map[string]types.Position, cash float64) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.cash = cash
	b.positions = make(map[string]types.Position, len(positions))
	for symbol, pos := range positions {
		b.positions[symbol] = pos
		// Seed a mark from the entry price so equity is computable before the
		// first tick arrives; a live price overwrites it moments later.
		if _, ok := b.marks[symbol]; !ok && pos.AvgPrice > 0 {
			b.marks[symbol] = pos.AvgPrice
		}
	}
}

// applyLocked mutates cash and the position for a signed quantity change.
// The caller must hold b.mu.
func (b *Book) applyLocked(symbol string, signedQty, price, fee, stop, target float64) {
	pos, ok := b.positions[symbol]
	if !ok {
		pos = types.Position{Symbol: symbol}
	}
	newQty := pos.Qty + signedQty

	// Realize P&L on whatever portion of this trade reduces the position.
	if pos.Qty != 0 && (pos.Qty > 0) != (signedQty > 0) {
		closing := math.Min(math.Abs(signedQty), math.Abs(pos.Qty))
		direction := 1.0
		if pos.Qty < 0 {
			direction = -1.0
		}
		b.cash += closing * (price - pos.AvgPrice) * direction
	}

	// Adding in the same direction re-averages the entry price.
	if pos.Qty == 0 || (pos.Qty > 0) == (signedQty > 0) {
		if math.Abs(newQty) > 1e-12 {
			total := pos.AvgPrice*math.Abs(pos.Qty) + price*math.Abs(signedQty)
			pos.AvgPrice = total / math.Abs(newQty)
		} else {
			pos.AvgPrice = 0
		}
	}

	pos.Qty = newQty
	if math.Abs(newQty) < 1e-12 {
		pos.Qty = 0
		pos.AvgPrice, pos.StopLoss, pos.TakeProfit = 0, 0, 0
	} else {
		pos.StopLoss, pos.TakeProfit = stop, target
	}

	b.cash -= fee
	b.positions[symbol] = pos
}
