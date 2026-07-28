// Package venue connects the execution engine to a real trading venue.
//
// A Venue sits *behind* the risk gate: the engine evaluates a decision, the
// gate sizes and approves it, and only then does an order reach a venue. That
// ordering is the point -- no venue implementation can be reached without
// passing risk, because nothing else calls it.
//
// The simulated venue and a live broker satisfy the same interface, so moving
// from paper to real money is a wiring change, never a code path change.
package venue

import (
	"context"
	"errors"
	"fmt"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// Errors a venue can return. Callers branch on these rather than parsing
// messages, so a venue swap does not break error handling.
var (
	// ErrUnsupported means the venue cannot express this order at all --
	// shorting spot crypto, say. Never retry it.
	ErrUnsupported = errors.New("venue: order not supported")

	// ErrRejected means the venue refused the order (insufficient funds, bad
	// symbol, market closed). Never retry it; the request itself is wrong.
	ErrRejected = errors.New("venue: order rejected")

	// ErrTransient means the attempt failed but the request was valid --
	// a timeout, rate limit, or 5xx. Safe to retry with the same
	// idempotency key.
	ErrTransient = errors.New("venue: transient failure")

	// ErrNotConfigured means credentials are missing.
	ErrNotConfigured = errors.New("venue: credentials not configured")
)

// Account is the venue's view of buying power and equity.
type Account struct {
	Equity      float64
	Cash        float64
	BuyingPower float64
	Currency    string
}

// Capabilities describes what a venue can actually do.
//
// These are not cosmetic. A venue that cannot hold brackets means the engine
// must monitor stops itself; a venue that cannot short means a sell decision
// with no inventory is impossible rather than merely unwise. The engine reads
// these to avoid sending orders that would be rejected on arrival.
type Capabilities struct {
	// SupportsShort reports whether a sell can open a negative position.
	SupportsShort bool

	// SupportsBrackets reports whether stop-loss and take-profit can be
	// attached to an order and held by the venue. When false, the engine
	// keeps monitoring brackets locally and exits with its own orders.
	SupportsBrackets bool

	// SupportsFractional reports whether non-integer quantities are allowed.
	SupportsFractional bool
}

// Venue is a place orders can be sent.
//
// Implementations must be safe for concurrent use. Every method takes a
// context so the engine can bound how long it waits on a network call.
type Venue interface {
	// Name identifies the venue in logs, e.g. "alpaca-paper".
	Name() string

	// IsLive reports whether orders risk real money. The engine logs this
	// loudly at startup, and it is the flag operators actually look for.
	IsLive() bool

	// Capabilities describes what this venue supports.
	Capabilities() Capabilities

	// Account fetches current equity and buying power.
	Account(ctx context.Context) (Account, error)

	// Positions fetches open positions, keyed by the engine's symbol format.
	Positions(ctx context.Context) (map[string]types.Position, error)

	// Place submits an order. The idempotencyKey must be stable across
	// retries of the same logical order so a timeout cannot double-fill.
	Place(ctx context.Context, order types.Order, idempotencyKey string) (types.Fill, error)

	// Close flattens a position at market.
	Close(ctx context.Context, symbol, reason string) (types.Fill, error)
}

// PreflightError explains why an order cannot be sent to a venue, before any
// network call is made. Catching these locally keeps invalid orders out of the
// venue's rejection statistics and off the wire.
type PreflightError struct {
	Reason string
}

func (e *PreflightError) Error() string { return "venue preflight: " + e.Reason }
func (e *PreflightError) Unwrap() error { return ErrUnsupported }

// Preflight checks an order against a venue's capabilities and the currently
// held position.
//
// The short check needs the existing position because "sell" is only a short
// when there is no inventory to sell: selling 1 BTC you already hold is a
// close, which every spot venue allows.
func Preflight(order types.Order, held types.Position, caps Capabilities) error {
	if order.Qty <= 0 {
		return &PreflightError{Reason: "non-positive quantity"}
	}
	if !caps.SupportsFractional && order.Qty != float64(int64(order.Qty)) {
		return &PreflightError{
			Reason: fmt.Sprintf("venue requires whole units, order is %g", order.Qty),
		}
	}
	if !caps.SupportsShort && order.Side == types.Sell {
		// Selling more than is held would open a short.
		if held.Qty <= 0 {
			return &PreflightError{Reason: "venue does not support short selling and no position is held"}
		}
		if order.Qty > held.Qty {
			return &PreflightError{
				Reason: fmt.Sprintf("sell of %g exceeds the %g held; venue does not support shorting",
					order.Qty, held.Qty),
			}
		}
	}
	return nil
}
