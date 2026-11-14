package risk

import (
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// The Python half is tests/test_risk_parity.py. Both read the same fixtures, so
// a change to either sizing implementation that moves a verdict or a quantity
// fails on one side. Intentional differences are in docs/DESIGN.md, record 8.
const parityFixture = "../../../tests/fixtures/risk_parity.json"

type parityFile struct {
	Comment   string       `json:"comment"`
	Tolerance float64      `json:"tolerance"`
	Cases     []parityCase `json:"cases"`
}

type parityCase struct {
	Name               string                    `json:"name"`
	Config             parityConfig              `json:"config"`
	Equity             float64                   `json:"equity"`
	SessionStartEquity *float64                  `json:"session_start_equity"`
	Price              float64                   `json:"price"`
	Positions          map[string]parityPosition `json:"positions"`
	Decision           struct {
		Symbol     string  `json:"symbol"`
		Side       string  `json:"side"`
		Confidence float64 `json:"confidence"`
		SizeHint   float64 `json:"size_hint"`
		Veto       bool    `json:"veto"`
	} `json:"decision"`
	Expected struct {
		Approved   bool    `json:"approved"`
		Verdict    string  `json:"verdict"`
		Qty        float64 `json:"qty"`
		StopLoss   float64 `json:"stop_loss"`
		TakeProfit float64 `json:"take_profit"`
	} `json:"expected"`
}

type parityPosition struct {
	Qty float64 `json:"qty"`
}

type parityConfig struct {
	MaxPositionPct      float64 `json:"max_position_pct"`
	RiskPerTradePct     float64 `json:"risk_per_trade_pct"`
	StopLossPct         float64 `json:"stop_loss_pct"`
	TakeProfitPct       float64 `json:"take_profit_pct"`
	MaxDailyDrawdownPct float64 `json:"max_daily_drawdown_pct"`
	MaxOpenPositions    int     `json:"max_open_positions"`
	MinConfidence       float64 `json:"min_confidence"`
}

// round8 matches the Python engine, which rounds order fields to 8 decimals.
func round8(v float64) float64 { return math.Round(v*1e8) / 1e8 }

func TestParityWithPythonFixtures(t *testing.T) {
	raw, err := os.ReadFile(parityFixture)
	if err != nil {
		t.Fatalf("read shared fixtures: %v", err)
	}
	var fx parityFile
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatalf("decode fixtures: %v", err)
	}
	if len(fx.Cases) < 20 {
		t.Fatalf("only %d fixture cases; the shared file looks truncated", len(fx.Cases))
	}

	seen := map[Verdict]bool{}
	for _, c := range fx.Cases {
		t.Run(c.Name, func(t *testing.T) {
			eng := New(Config{
				MaxPositionPct:      c.Config.MaxPositionPct,
				RiskPerTradePct:     c.Config.RiskPerTradePct,
				StopLossPct:         c.Config.StopLossPct,
				TakeProfitPct:       c.Config.TakeProfitPct,
				MaxDailyDrawdownPct: c.Config.MaxDailyDrawdownPct,
				MaxOpenPositions:    c.Config.MaxOpenPositions,
				MinConfidence:       c.Config.MinConfidence,
			})
			start := c.Equity
			if c.SessionStartEquity != nil {
				start = *c.SessionStartEquity
			}
			eng.StartDay(start)
			eng.CheckKillSwitch(c.Equity)

			positions := map[string]types.Position{}
			for sym, p := range c.Positions {
				positions[sym] = types.Position{Symbol: sym, Qty: p.Qty}
			}
			d := types.Decision{
				TS: time.Date(2024, 1, 1, 0, 0, 0, 0, time.UTC), Symbol: c.Decision.Symbol,
				Side: types.Side(c.Decision.Side), Confidence: c.Decision.Confidence,
				SizeHint: c.Decision.SizeHint, Rationale: "parity", Veto: c.Decision.Veto,
			}
			res := eng.Evaluate(d, Account{Equity: c.Equity, Positions: positions}, c.Price)
			seen[res.Verdict] = true

			if string(res.Verdict) != c.Expected.Verdict {
				t.Fatalf("verdict %s, want %s (%s)", res.Verdict, c.Expected.Verdict, res.Reason)
			}
			if res.Verdict.OK() != c.Expected.Approved {
				t.Fatalf("approved %v, want %v", res.Verdict.OK(), c.Expected.Approved)
			}
			if !c.Expected.Approved {
				return
			}
			for name, pair := range map[string][2]float64{
				"qty":         {round8(res.Order.Qty), c.Expected.Qty},
				"stop_loss":   {round8(res.Order.StopLoss), c.Expected.StopLoss},
				"take_profit": {round8(res.Order.TakeProfit), c.Expected.TakeProfit},
			} {
				if math.Abs(pair[0]-pair[1]) > fx.Tolerance {
					t.Errorf("%s = %.12f, want %.12f", name, pair[0], pair[1])
				}
			}
		})
	}

	for _, v := range []Verdict{Approved, RejectHalted, RejectVeto, RejectConfidence,
		RejectMaxPositions, RejectAtCap, RejectZeroSize} {
		if !seen[v] {
			t.Errorf("fixtures never exercise verdict %s", v)
		}
	}
}
