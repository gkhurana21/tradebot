package risk

import (
	"math"
	"strconv"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// Explain renders res as a readable one-liner with the numbers behind it, for
// the journal and the HTTP response. It allocates, so it is kept out of
// Evaluate (which must stay allocation-free) and is called by the owner after
// the verdict is known. The typed Verdict is unchanged; only the text differs.
// Verdicts with no useful numbers keep Evaluate's own reason.
func (e *Engine) Explain(d types.Decision, acct Account, price float64, res Result) string {
	switch res.Verdict {
	case Approved:
		if acct.Equity <= 0 {
			return res.Reason
		}
		risked := math.Abs(res.Order.Qty) * price * e.cfg.StopLossPct / acct.Equity * 100
		return "sized " + sig3(math.Abs(res.Order.Qty)) + " " + d.Symbol +
			" · risk " + strconv.FormatFloat(risked, 'f', 1, 64) + "% to stop"
	case RejectHalted:
		return "halted: daily drawdown " + strconv.FormatFloat(e.Drawdown(acct.Equity)*100, 'f', 1, 64) +
			"% ≥ " + pct(e.cfg.MaxDailyDrawdownPct) + "%"
	case RejectVeto:
		if d.Veto {
			return "vetoed by the research layer"
		}
		return "proposed FLAT, nothing to trade"
	case RejectConfidence:
		return "confidence " + strconv.FormatFloat(d.Confidence, 'f', 2, 64) +
			" < min " + strconv.FormatFloat(e.cfg.MinConfidence, 'f', 2, 64)
	case RejectMaxPositions:
		return "max open positions " + strconv.Itoa(e.cfg.MaxOpenPositions) + " reached"
	case RejectAtCap:
		return "position at max notional " + pct(e.cfg.MaxPositionPct) + "%"
	}
	return res.Reason
}

// HaltReason is the journal text for the moment the kill switch trips.
func (e *Engine) HaltReason(equity float64) string {
	return "halted: daily drawdown " + strconv.FormatFloat(e.Drawdown(equity)*100, 'f', 1, 64) +
		"% ≥ " + pct(e.cfg.MaxDailyDrawdownPct) + "%"
}

// pct renders a fraction as a percentage number without trailing noise
// (0.05 -> "5", 0.035 -> "3.5").
func pct(frac float64) string {
	return strconv.FormatFloat(math.Round(frac*1e6)/1e4, 'f', -1, 64)
}

// sig3 formats v with three significant digits and no exponent (0.0781, 1.23,
// 12345), which reads better than %g across the range of crypto quantities.
func sig3(v float64) string {
	if v <= 0 || math.IsNaN(v) || math.IsInf(v, 0) {
		return strconv.FormatFloat(v, 'f', -1, 64)
	}
	decimals := 2 - int(math.Floor(math.Log10(v)))
	if decimals < 0 {
		decimals = 0
	}
	if decimals > 12 {
		decimals = 12
	}
	return strconv.FormatFloat(v, 'f', decimals, 64)
}
