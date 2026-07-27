package book

import (
	"math"
	"sync"
	"testing"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

const tol = 1e-9

func frictionless(cash float64) *Book {
	return New(Options{StartingCash: cash})
}

func order(side types.Side, qty float64) types.Order {
	return types.Order{Symbol: "BTC-USD", Side: side, Qty: qty}
}

func TestLongRoundTripRealizesProfit(t *testing.T) {
	b := frictionless(10_000)
	b.Mark("BTC-USD", 100)

	if _, ok := b.Execute(order(types.Buy, 10), "entry"); !ok {
		t.Fatal("execute rejected a valid order")
	}
	if got := b.Position("BTC-USD").Qty; math.Abs(got-10) > tol {
		t.Fatalf("qty = %v, want 10", got)
	}

	b.Mark("BTC-USD", 110)
	if got := b.Equity(); math.Abs(got-10_100) > tol {
		t.Fatalf("unrealized equity = %v, want 10100", got)
	}

	b.Close("BTC-USD", "exit")
	if !b.Position("BTC-USD").IsFlat() {
		t.Fatal("position not flat after close")
	}
	if got := b.Cash(); math.Abs(got-10_100) > tol {
		t.Fatalf("realized cash = %v, want 10100", got)
	}
}

func TestShortProfitsWhenPriceFalls(t *testing.T) {
	b := frictionless(10_000)
	b.Mark("BTC-USD", 100)
	b.Execute(order(types.Sell, 5), "short")

	if got := b.Position("BTC-USD").Qty; math.Abs(got+5) > tol {
		t.Fatalf("qty = %v, want -5", got)
	}
	b.Mark("BTC-USD", 90)
	if got := b.Equity(); math.Abs(got-10_050) > tol {
		t.Fatalf("equity = %v, want 10050", got)
	}
	b.Close("BTC-USD", "cover")
	if got := b.Cash(); math.Abs(got-10_050) > tol {
		t.Fatalf("cash = %v, want 10050", got)
	}
}

func TestAveragesEntryOnAdd(t *testing.T) {
	b := frictionless(10_000)
	b.Mark("BTC-USD", 100)
	b.Execute(order(types.Buy, 10), "first")
	b.Mark("BTC-USD", 120)
	b.Execute(order(types.Buy, 10), "add")

	pos := b.Position("BTC-USD")
	if math.Abs(pos.Qty-20) > tol {
		t.Fatalf("qty = %v, want 20", pos.Qty)
	}
	if math.Abs(pos.AvgPrice-110) > tol {
		t.Fatalf("avg price = %v, want 110", pos.AvgPrice)
	}
}

func TestPartialCloseRealizesProportionally(t *testing.T) {
	b := frictionless(10_000)
	b.Mark("BTC-USD", 100)
	b.Execute(order(types.Buy, 10), "entry")
	b.Mark("BTC-USD", 110)
	b.Execute(order(types.Sell, 4), "trim") // realize 4 * $10

	if got := b.Cash(); math.Abs(got-10_040) > tol {
		t.Fatalf("cash = %v, want 10040", got)
	}
	pos := b.Position("BTC-USD")
	if math.Abs(pos.Qty-6) > tol {
		t.Fatalf("qty = %v, want 6", pos.Qty)
	}
	if math.Abs(pos.AvgPrice-100) > tol {
		t.Fatalf("avg price = %v, want 100 (unchanged by a reduction)", pos.AvgPrice)
	}
}

func TestFeesAndSlippageCostMoney(t *testing.T) {
	b := New(Options{StartingCash: 10_000, FeePct: 0.001, SlippagePct: 0.0005})
	b.Mark("BTC-USD", 100)
	fill, _ := b.Execute(order(types.Buy, 10), "entry")

	if fill.Price <= 100 {
		t.Errorf("buy filled at %v, expected adverse slippage above 100", fill.Price)
	}
	if fill.Fee <= 0 {
		t.Errorf("fee = %v, want positive", fill.Fee)
	}
	if b.Cash() >= 10_000 {
		t.Errorf("cash = %v, want reduced by fees", b.Cash())
	}
}

func TestRefusesToTradeWithoutAMark(t *testing.T) {
	b := frictionless(10_000)
	if _, ok := b.Execute(order(types.Buy, 1), "blind"); ok {
		t.Fatal("executed without a mark price")
	}
	if _, ok := b.Close("BTC-USD", "exit"); ok {
		t.Fatal("closed a flat position")
	}
}

func TestSnapshotIsAnIndependentCopy(t *testing.T) {
	b := frictionless(10_000)
	b.Mark("BTC-USD", 100)
	b.Execute(order(types.Buy, 5), "entry")

	_, positions := b.Snapshot()
	positions["BTC-USD"] = types.Position{Symbol: "BTC-USD", Qty: 9999}

	if got := b.Position("BTC-USD").Qty; math.Abs(got-5) > tol {
		t.Fatalf("book mutated through a snapshot: qty = %v, want 5", got)
	}
}

// Run with -race to prove the book is safe under concurrent access.
func TestConcurrentAccess(t *testing.T) {
	b := New(Options{StartingCash: 100_000, FeePct: 0.001, SlippagePct: 0.0005})
	b.Mark("BTC-USD", 100)

	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				b.Mark("BTC-USD", 100+float64(j%10))
				b.Execute(order(types.Buy, 0.01), "buy")
				_ = b.Equity()
				_ = b.Positions()
				if j%10 == 0 {
					b.Close("BTC-USD", "flat")
				}
			}
		}(i)
	}
	wg.Wait()

	if math.IsNaN(b.Equity()) {
		t.Fatal("equity became NaN under concurrency")
	}
}
