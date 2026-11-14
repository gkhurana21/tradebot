package book

import (
	"testing"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

func TestViewIsOneConsistentSnapshot(t *testing.T) {
	b := New(Options{StartingCash: 10_000})
	b.Mark("BTC-USD", 100)
	if _, ok := b.Execute(types.Order{Symbol: "BTC-USD", Side: types.Buy, Qty: 10}, "t"); !ok {
		t.Fatal("execute failed")
	}
	b.Mark("BTC-USD", 110)
	b.Mark("ETH-USD", 2000) // marked, never held

	v := b.View()
	if v.Equity != b.Equity() || v.Cash != b.Cash() {
		t.Fatalf("view equity/cash = %v/%v, want %v/%v", v.Equity, v.Cash, b.Equity(), b.Cash())
	}
	if len(v.Positions) != 1 || v.Marks["BTC-USD"] != 110 || v.Marks["ETH-USD"] != 2000 {
		t.Fatalf("view = %+v", v)
	}
	v.Marks["BTC-USD"] = 1 // callers get copies
	if p, _ := b.Price("BTC-USD"); p != 110 {
		t.Fatalf("View leaked internal state: mark = %v", p)
	}
}
