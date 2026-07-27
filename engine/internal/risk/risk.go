// Package risk implements the deterministic gate that stands between a
// proposed trade and the market.
//
// The research layer (Python, LLM-driven) only ever proposes. This package
// decides. Nothing reaches an exchange without passing every check here, and
// no amount of model confidence can bypass a limit. It is deliberately
// allocation-free on the hot path so the gate never becomes the bottleneck --
// see BenchmarkEngine_Evaluate.
package risk

import (
	"math"
	"sync"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// Verdict is the machine-readable outcome of a risk evaluation. Typed
// verdicts (rather than bare error strings) let the service count rejections
// by cause and alert on distribution shifts.
type Verdict string

const (
	Approved           Verdict = "approved"
	RejectHalted       Verdict = "rejected_halted"
	RejectVeto         Verdict = "rejected_veto"
	RejectConfidence   Verdict = "rejected_low_confidence"
	RejectMaxPositions Verdict = "rejected_max_open_positions"
	RejectAtCap        Verdict = "rejected_position_at_max_notional"
	RejectZeroSize     Verdict = "rejected_zero_size"
	RejectInvalid      Verdict = "rejected_invalid_decision"
)

// OK reports whether the verdict permits trading.
func (v Verdict) OK() bool { return v == Approved }

// Config holds the hard limits. Zero values are not sensible defaults --
// construct with Balanced, Conservative, or Aggressive.
type Config struct {
	MaxPositionPct      float64 // max fraction of equity in a single position
	RiskPerTradePct     float64 // fraction of equity risked to the stop
	StopLossPct         float64 // stop distance from entry
	TakeProfitPct       float64 // target distance from entry
	MaxDailyDrawdownPct float64 // kill switch threshold for the session
	MaxOpenPositions    int
	MinConfidence       float64
}

// Balanced is the default risk posture.
func Balanced() Config {
	return Config{
		MaxPositionPct:      0.20,
		RiskPerTradePct:     0.01,
		StopLossPct:         0.02,
		TakeProfitPct:       0.04,
		MaxDailyDrawdownPct: 0.05,
		MaxOpenPositions:    3,
		MinConfidence:       0.55,
	}
}

// Conservative trades smaller and halts sooner. It survives longer losing
// streaks than Balanced at the cost of slower compounding.
func Conservative() Config {
	c := Balanced()
	c.MaxPositionPct = 0.10
	c.RiskPerTradePct = 0.005
	c.StopLossPct = 0.015
	c.TakeProfitPct = 0.03
	c.MaxDailyDrawdownPct = 0.03
	c.MinConfidence = 0.65
	return c
}

// Aggressive raises position size and the daily loss limit.
//
// This raises the probability of ruin. It does not raise expected return:
// larger bets amplify whatever edge exists in both directions, and a normal
// losing streak at this size can take a small account to zero. Use only with
// capital you can afford to lose entirely.
func Aggressive() Config {
	c := Balanced()
	c.MaxPositionPct = 0.35
	c.RiskPerTradePct = 0.02
	c.StopLossPct = 0.03
	c.TakeProfitPct = 0.09
	c.MaxDailyDrawdownPct = 0.10
	c.MinConfidence = 0.50
	return c
}

// Profile resolves a named risk posture. Unknown names fall back to Balanced
// with ok=false so callers can surface a typo rather than silently trading a
// posture the user did not choose.
func Profile(name string) (Config, bool) {
	switch name {
	case "conservative":
		return Conservative(), true
	case "balanced", "":
		return Balanced(), true
	case "aggressive":
		return Aggressive(), true
	default:
		return Balanced(), false
	}
}

// Account is the risk engine's view of the book at evaluation time.
type Account struct {
	Equity    float64
	Positions map[string]types.Position
}

// Result is the outcome of evaluating one decision.
type Result struct {
	Verdict Verdict
	Order   types.Order // valid only when Verdict.OK()
	Reason  string      // human-readable detail for logs
}

// Engine applies Config to incoming decisions. It is safe for concurrent use:
// the day-session state (starting equity, halt flag) is mutex-guarded, and
// evaluation itself reads only its arguments.
type Engine struct {
	cfg Config

	mu             sync.RWMutex
	dayStartEquity float64
	dayStarted     bool
	halted         bool
}

// New returns an Engine enforcing cfg.
func New(cfg Config) *Engine { return &Engine{cfg: cfg} }

// Config returns the limits this engine enforces.
func (e *Engine) Config() Config { return e.cfg }

// StartDay resets the session baseline used by the drawdown kill switch and
// clears any halt from the previous session.
func (e *Engine) StartDay(equity float64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.dayStartEquity = equity
	e.dayStarted = true
	e.halted = false
}

// Halted reports whether the kill switch has tripped.
func (e *Engine) Halted() bool {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.halted
}

// Drawdown returns the current session drawdown as a fraction of the starting
// equity. Negative values mean the session is up.
func (e *Engine) Drawdown(equity float64) float64 {
	e.mu.RLock()
	start, started := e.dayStartEquity, e.dayStarted
	e.mu.RUnlock()
	if !started || start <= 0 {
		return 0
	}
	return (start - equity) / start
}

// CheckKillSwitch trips the halt if the session drawdown has breached the
// configured limit. It is idempotent and reports the post-check halt state.
//
// The first call also establishes the session baseline if StartDay was never
// called, so a misconfigured caller fails safe rather than trading unbounded.
func (e *Engine) CheckKillSwitch(equity float64) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.dayStarted {
		e.dayStartEquity = equity
		e.dayStarted = true
	}
	if e.dayStartEquity > 0 {
		dd := (e.dayStartEquity - equity) / e.dayStartEquity
		if dd >= e.cfg.MaxDailyDrawdownPct {
			e.halted = true
		}
	}
	return e.halted
}

// Evaluate turns a proposed decision into a risk-bounded order, or rejects it.
//
// Checks run cheapest-first so a rejected decision costs as little as
// possible. The order of checks is also the order of precedence: a halted
// engine rejects even a perfect decision.
func (e *Engine) Evaluate(d types.Decision, acct Account, price float64) Result {
	if e.Halted() {
		return Result{Verdict: RejectHalted, Reason: "daily drawdown kill switch active"}
	}
	if err := d.Validate(); err != nil {
		return Result{Verdict: RejectInvalid, Reason: err.Error()}
	}
	if price <= 0 || math.IsNaN(price) || math.IsInf(price, 0) {
		return Result{Verdict: RejectInvalid, Reason: "non-positive or non-finite price"}
	}
	if d.Veto || d.Side == types.Flat {
		return Result{Verdict: RejectVeto, Reason: "research layer vetoed or proposed flat"}
	}
	if d.Confidence < e.cfg.MinConfidence {
		return Result{Verdict: RejectConfidence, Reason: "confidence below minimum"}
	}
	if acct.Equity <= 0 {
		return Result{Verdict: RejectZeroSize, Reason: "non-positive equity"}
	}

	pos := acct.Positions[d.Symbol]

	// Opening a new symbol is gated by the concurrent-position limit; adding
	// to an existing position is not, since it consumes no new slot.
	if pos.IsFlat() {
		open := 0
		for _, p := range acct.Positions {
			if !p.IsFlat() {
				open++
			}
		}
		if open >= e.cfg.MaxOpenPositions {
			return Result{Verdict: RejectMaxPositions, Reason: "max concurrent positions reached"}
		}
	}

	// Fixed-fractional sizing: risk a fixed slice of equity, and let the stop
	// distance determine how many units that buys.
	stopDist := price * e.cfg.StopLossPct
	if stopDist <= 0 {
		return Result{Verdict: RejectInvalid, Reason: "non-positive stop distance"}
	}
	qtyByRisk := (acct.Equity * e.cfg.RiskPerTradePct) / stopDist

	// Independently cap the gross notional of any single position.
	maxNotional := acct.Equity * e.cfg.MaxPositionPct
	qtyByNotional := maxNotional / price

	qty := math.Min(qtyByRisk, qtyByNotional) * clamp(d.SizeHint, 0, 1)

	// Subtract exposure already held in the same direction. Without this,
	// repeated same-side signals stack a position far past MaxPositionPct --
	// each order looks compliant in isolation while the book quietly is not.
	if sameDirection(pos.Qty, d.Side) {
		remaining := qtyByNotional - math.Abs(pos.Qty)
		if remaining <= 0 {
			return Result{Verdict: RejectAtCap, Reason: "position already at max notional"}
		}
		qty = math.Min(qty, remaining)
	}

	if qty <= 0 || math.IsNaN(qty) {
		return Result{Verdict: RejectZeroSize, Reason: "computed size is zero"}
	}

	stop, target := e.bracket(d.Side, price)
	return Result{
		Verdict: Approved,
		Reason:  "ok",
		Order: types.Order{
			TS:         d.TS,
			Symbol:     d.Symbol,
			Side:       d.Side,
			Qty:        qty,
			StopLoss:   stop,
			TakeProfit: target,
			Tag:        d.Rationale,
		},
	}
}

// bracket returns the protective stop and profit target for an entry at price.
func (e *Engine) bracket(side types.Side, price float64) (stop, target float64) {
	if side == types.Buy {
		return price * (1 - e.cfg.StopLossPct), price * (1 + e.cfg.TakeProfitPct)
	}
	return price * (1 + e.cfg.StopLossPct), price * (1 - e.cfg.TakeProfitPct)
}

// sameDirection reports whether adding side to a position of qty increases
// exposure rather than reducing or flipping it.
func sameDirection(qty float64, side types.Side) bool {
	return (qty > 0 && side == types.Buy) || (qty < 0 && side == types.Sell)
}

func clamp(v, lo, hi float64) float64 {
	if math.IsNaN(v) {
		return lo
	}
	return math.Max(lo, math.Min(hi, v))
}
