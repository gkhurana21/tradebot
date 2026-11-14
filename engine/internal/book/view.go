package book

import "github.com/gkhurana21/tradebot/engine/internal/types"

// View is one consistent read of the account: equity, cash, every non-flat
// position and the marks, all taken under a single lock.
type View struct {
	Equity    float64
	Cash      float64
	Positions map[string]types.Position
	Marks     map[string]float64
}

// View returns a consistent copy of the account for reporting.
func (b *Book) View() View {
	b.mu.RLock()
	defer b.mu.RUnlock()
	v := View{
		Equity:    b.equityLocked(),
		Cash:      b.cash,
		Positions: make(map[string]types.Position, len(b.positions)),
		Marks:     make(map[string]float64, len(b.marks)),
	}
	for sym, pos := range b.positions {
		if !pos.IsFlat() {
			v.Positions[sym] = pos
		}
	}
	for sym, m := range b.marks {
		v.Marks[sym] = m
	}
	return v
}
