package engine

import (
	"sync"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// JournalSize is the number of entries the journal retains.
const JournalSize = 256

// JournalKind classifies a journal entry.
type JournalKind string

const (
	JournalDecision JournalKind = "decision"
	JournalExit     JournalKind = "exit"
	JournalHalt     JournalKind = "halt"
)

// JournalEntry is one observable outcome. Optional fields are pointers or
// omitted so a consumer can tell "absent" from "zero".
type JournalEntry struct {
	Seq        uint64      `json:"seq"`
	At         time.Time   `json:"at"` // engine wall clock, RFC3339Nano
	Kind       JournalKind `json:"kind"`
	ClientID   string      `json:"client_id,omitempty"`
	Symbol     string      `json:"symbol,omitempty"`
	Side       types.Side  `json:"side,omitempty"`
	Confidence *float64    `json:"confidence,omitempty"`
	Verdict    string      `json:"verdict,omitempty"`
	Approved   *bool       `json:"approved,omitempty"`
	Reason     string      `json:"reason"`
	Qty        *float64    `json:"qty,omitempty"`
	Price      *float64    `json:"price,omitempty"`
}

// JournalPage is the result of reading the journal.
type JournalPage struct {
	NextSeq   uint64         `json:"next_seq"`
	OldestSeq uint64         `json:"oldest_seq"`
	Gap       bool           `json:"gap"`
	Items     []JournalEntry `json:"items"`
}

// journal is a fixed-size ring of recent outcomes. Only the owner goroutine
// appends; HTTP handlers read under the RWMutex. It is append-only telemetry
// and never feeds back into book or risk state.
type journal struct {
	mu   sync.RWMutex
	buf  [JournalSize]JournalEntry
	next uint64 // seq of the next entry; starts at 1
}

func newJournal() *journal { return &journal{next: 1} }

func (j *journal) append(e JournalEntry) {
	j.mu.Lock()
	e.Seq = j.next
	if e.At.IsZero() {
		e.At = time.Now().UTC()
	}
	j.buf[e.Seq%JournalSize] = e
	j.next++
	j.mu.Unlock()
}

// read returns entries with seq >= since. A nil since means "everything
// retained". Gap is true when entries the caller asked for have been
// overwritten (since < oldest_seq and something was in fact dropped).
func (j *journal) read(since *uint64) JournalPage {
	j.mu.RLock()
	defer j.mu.RUnlock()

	oldest := uint64(1)
	if j.next > JournalSize+1 {
		oldest = j.next - JournalSize
	}
	if j.next == 1 {
		oldest = 1
	}
	page := JournalPage{NextSeq: j.next, OldestSeq: oldest, Items: []JournalEntry{}}

	from := oldest
	if since != nil {
		if *since < oldest && oldest > 1 {
			page.Gap = true
		}
		if *since > oldest {
			from = *since
		}
	}
	for seq := from; seq < j.next; seq++ {
		page.Items = append(page.Items, j.buf[seq%JournalSize])
	}
	return page
}
