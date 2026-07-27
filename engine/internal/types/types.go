// Package types defines the domain vocabulary shared across the execution
// engine. These mirror the Python research layer's types so a Decision
// produced by the (slow) research/LLM side can be executed by the (fast) Go
// side without translation loss.
package types

import (
	"fmt"
	"math"
	"time"
)

// Side is the direction of a signal, decision, or order.
type Side string

const (
	Buy  Side = "BUY"
	Sell Side = "SELL"
	Flat Side = "FLAT"
)

// Valid reports whether s is a recognized side.
func (s Side) Valid() bool {
	return s == Buy || s == Sell || s == Flat
}

// Opposes reports whether a position of the given quantity is on the opposite
// side of s. A flat position opposes nothing.
func (s Side) Opposes(qty float64) bool {
	switch {
	case qty > 0:
		return s == Sell
	case qty < 0:
		return s == Buy
	default:
		return false
	}
}

// Decision is what the research layer proposes. It is only ever a proposal:
// the risk engine sizes it, bounds it, and may reject it outright.
type Decision struct {
	TS         time.Time `json:"ts"`
	Symbol     string    `json:"symbol"`
	Side       Side      `json:"side"`
	Confidence float64   `json:"confidence"` // 0..1
	SizeHint   float64   `json:"size_hint"`  // 0..1, fraction of the allowed size
	Rationale  string    `json:"rationale"`
	Veto       bool      `json:"veto"`
}

// Validate checks a decision is structurally sound before the risk engine
// sees it. Returns nil when the decision is safe to evaluate.
func (d Decision) Validate() error {
	if d.Symbol == "" {
		return fmt.Errorf("decision: empty symbol")
	}
	if !d.Side.Valid() {
		return fmt.Errorf("decision: invalid side %q", d.Side)
	}
	if math.IsNaN(d.Confidence) || d.Confidence < 0 || d.Confidence > 1 {
		return fmt.Errorf("decision: confidence %v out of range [0,1]", d.Confidence)
	}
	if math.IsNaN(d.SizeHint) || d.SizeHint < 0 || d.SizeHint > 1 {
		return fmt.Errorf("decision: size_hint %v out of range [0,1]", d.SizeHint)
	}
	return nil
}

// Order is a risk-approved, fully-sized instruction to trade.
type Order struct {
	TS         time.Time `json:"ts"`
	Symbol     string    `json:"symbol"`
	Side       Side      `json:"side"`
	Qty        float64   `json:"qty"`
	StopLoss   float64   `json:"stop_loss"`
	TakeProfit float64   `json:"take_profit"`
	Tag        string    `json:"tag"`
}

// Notional is the gross cash value of the order at the given price.
func (o Order) Notional(price float64) float64 { return math.Abs(o.Qty) * price }

// Fill is the result of executing an order.
type Fill struct {
	TS     time.Time `json:"ts"`
	Symbol string    `json:"symbol"`
	Side   Side      `json:"side"`
	Qty    float64   `json:"qty"`
	Price  float64   `json:"price"`
	Fee    float64   `json:"fee"`
	Reason string    `json:"reason"`
}

// Position is a net holding in one symbol. Qty is signed: positive is long,
// negative is short.
type Position struct {
	Symbol     string  `json:"symbol"`
	Qty        float64 `json:"qty"`
	AvgPrice   float64 `json:"avg_price"`
	StopLoss   float64 `json:"stop_loss"`
	TakeProfit float64 `json:"take_profit"`
}

// IsFlat reports whether the position holds no meaningful quantity.
func (p Position) IsFlat() bool { return math.Abs(p.Qty) < epsilon }

// Unrealized is the mark-to-market P&L at the given price.
func (p Position) Unrealized(price float64) float64 { return p.Qty * (price - p.AvgPrice) }

// Exposure is the absolute notional value of the position at the given price.
func (p Position) Exposure(price float64) float64 { return math.Abs(p.Qty) * price }

// epsilon is the quantity below which a position counts as flat. Chosen to be
// well under any tradable lot size while surviving float accumulation error.
const epsilon = 1e-12
