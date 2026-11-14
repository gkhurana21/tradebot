package engine

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
	"github.com/gkhurana21/tradebot/engine/internal/types"
)

func sell(symbol string, conf float64) types.Decision {
	d := buy(symbol, conf)
	d.Side = types.Sell
	return d
}

func TestSubmitTickWaitIsAppliedBeforeReturning(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	res, err := e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100, TS: time.Now()})
	if err != nil {
		t.Fatalf("SubmitTickWait: %v", err)
	}
	if res.Halted || res.Equity != 10_000 || res.Exits == nil || len(res.Exits) != 0 {
		t.Fatalf("result = %+v, want equity 10000, not halted, empty non-nil exits", res)
	}
	// No waitFor: the mark must already be there, so a decision prices off it.
	if p, ok := b.Price("BTC-USD"); !ok || p != 100 {
		t.Fatalf("mark after sync tick = %v,%v, want 100,true", p, ok)
	}
	out, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "")
	if err != nil || !out.Result.Verdict.OK() {
		t.Fatalf("decision right after sync tick: %+v, %v", out.Result, err)
	}
}

func TestSubmitTickWaitReportsBracketExit(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	if _, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), ""); err != nil {
		t.Fatal(err)
	}

	res, err := e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 99, High: 100, Low: 97})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Exits) != 1 || res.Exits[0].Reason != "stop" || res.Exits[0].Symbol != "BTC-USD" {
		t.Fatalf("exits = %+v, want one stop exit", res.Exits)
	}
	if res.Exits[0].Side != types.Sell || res.Exits[0].Qty <= 0 || res.Exits[0].Price != 98 {
		t.Fatalf("exit fill = %+v, want sell at the 98 stop", res.Exits[0])
	}
}

func TestSubmitTickWaitFullBufferIsBusyNotDropped(t *testing.T) {
	// Engine is never started, so nothing drains the tick buffer.
	b := book.New(book.Options{StartingCash: 10_000})
	e := New(b, risk.New(risk.Balanced()))
	for i := 0; i < cap(e.ticks); i++ {
		if err := e.SubmitTick(Tick{Symbol: "BTC-USD", Price: 100}); err != nil {
			t.Fatal(err)
		}
	}
	_, err := e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	if err != ErrBusy {
		t.Fatalf("err = %v, want ErrBusy", err)
	}
	if len(e.ticks) != cap(e.ticks) {
		t.Fatalf("a rejected sync tick must not be queued; len = %d", len(e.ticks))
	}
}

func TestSubmitTickWaitAfterStop(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	e.Stop()
	if _, err := e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100}); err != ErrClosed {
		t.Fatalf("err = %v, want ErrClosed", err)
	}
}

func TestClientIDRepeatReturnsStoredResultAndPlacesOneOrder(t *testing.T) {
	e, b, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})

	first, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "c-1")
	if err != nil || !first.Result.Verdict.OK() || len(first.Fills) != 1 || first.Replayed {
		t.Fatalf("first = %+v, %v", first, err)
	}
	qty := b.Position("BTC-USD").Qty

	again, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "c-1")
	if err != nil {
		t.Fatal(err)
	}
	if !again.Replayed || again.Result.Verdict != first.Result.Verdict || len(again.Fills) != 1 ||
		again.Fills[0].Price != first.Fills[0].Price || again.ClientID != "c-1" {
		t.Fatalf("replay = %+v, want the stored result", again)
	}
	if got := b.Position("BTC-USD").Qty; got != qty {
		t.Fatalf("position qty %v -> %v: a repeat client_id placed a second order", qty, got)
	}
	if s := e.Stats(); s.Decisions != 1 || s.Fills != 1 {
		t.Fatalf("stats = %+v, want 1 decision and 1 fill", s)
	}
}

func TestClientIDConcurrentRepeatsPlaceOneOrder(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})

	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "same")
		}()
	}
	wg.Wait()
	if s := e.Stats(); s.Decisions != 1 || s.Fills != 1 {
		t.Fatalf("stats = %+v, want exactly 1 decision and 1 fill", s)
	}
}

// A repeat that arrives while the original is still queued waits for it
// rather than racing it. The engine is not started, so the original is
// guaranteed pending when the repeat arrives.
func TestClientIDRepeatWaitsForPendingOriginal(t *testing.T) {
	b := book.New(book.Options{StartingCash: 10_000})
	e := New(b, risk.New(risk.Balanced()))
	b.Mark("BTC-USD", 100)

	type result struct {
		out DecisionOutcome
		err error
	}
	first := make(chan result, 1)
	go func() {
		out, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "p-1")
		first <- result{out, err}
	}()
	for len(e.decisions) == 0 { // wait until the original is queued
		time.Sleep(time.Millisecond)
	}
	second := make(chan result, 1)
	go func() {
		out, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "p-1")
		second <- result{out, err}
	}()
	select {
	case <-second:
		t.Fatal("repeat returned before the original was processed")
	case <-time.After(50 * time.Millisecond):
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	e.Start(ctx)
	defer e.Stop()
	drain(e)

	r1, r2 := <-first, <-second
	if r1.err != nil || r2.err != nil || !r2.out.Replayed || r2.out.Result.Verdict != r1.out.Result.Verdict {
		t.Fatalf("first=%+v second=%+v", r1, r2)
	}
	if s := e.Stats(); s.Decisions != 1 || s.Fills != 1 {
		t.Fatalf("stats = %+v, want 1 decision and 1 fill", s)
	}
}

func TestClientIDStoredResultSurvivesCallerTimeout(t *testing.T) {
	b := book.New(book.Options{StartingCash: 10_000})
	e := New(b, risk.New(risk.Balanced()))
	b.Mark("BTC-USD", 100)

	// The caller gives up while the decision is queued (engine not running).
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := e.SubmitDecisionFull(ctx, buy("BTC-USD", 0.9), "t-1"); err == nil {
		t.Fatal("expected a timeout")
	}

	// The engine then processes it anyway; the retry must find the outcome.
	runCtx, stop := context.WithCancel(context.Background())
	defer stop()
	e.Start(runCtx)
	defer e.Stop()
	drain(e)

	out, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "t-1")
	if err != nil || !out.Replayed || !out.Result.Verdict.OK() {
		t.Fatalf("retry = %+v, %v", out, err)
	}
	if s := e.Stats(); s.Decisions != 1 || s.Fills != 1 {
		t.Fatalf("stats = %+v, want 1 decision and 1 fill", s)
	}
}

func TestClientIDWindowIsBounded(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)

	// No market data -> cheap rejections; enough of them to evict the first id.
	for i := 0; i < idempotencyWindow+5; i++ {
		if _, err := e.SubmitDecisionFull(context.Background(), buy("NOPE-USD", 0.9), fmt.Sprintf("id-%d", i)); err != nil {
			t.Fatal(err)
		}
	}
	e.idem.mu.Lock()
	n := len(e.idem.byID)
	_, oldest := e.idem.byID["id-0"]
	_, newest := e.idem.byID[fmt.Sprintf("id-%d", idempotencyWindow+4)]
	e.idem.mu.Unlock()
	if n != idempotencyWindow || oldest || !newest {
		t.Fatalf("map size=%d oldest-present=%v newest-present=%v, want %d,false,true", n, oldest, newest, idempotencyWindow)
	}
}

func TestFlipReportsTheCloseLeg(t *testing.T) {
	cfg := risk.Balanced()
	e, b, _ := newTestEngine(t, 10_000, cfg)
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	if _, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "open"); err != nil {
		t.Fatal(err)
	}

	out, err := e.SubmitDecisionFull(context.Background(), sell("BTC-USD", 0.9), "flip")
	if err != nil || !out.Result.Verdict.OK() {
		t.Fatalf("flip: %+v, %v", out.Result, err)
	}
	if len(out.Fills) != 2 || out.Fills[0].Reason != "flip" || out.Fills[0].Side != types.Sell ||
		out.Fills[1].Side != types.Sell {
		t.Fatalf("fills = %+v, want [flip close, short entry]", out.Fills)
	}
	if pos := b.Position("BTC-USD"); pos.Qty >= 0 {
		t.Fatalf("position after flip = %+v, want short", pos)
	}

	j := e.Journal(nil)
	var kinds []string
	for _, it := range j.Items {
		kinds = append(kinds, string(it.Kind))
	}
	// open decision, flip exit, flip decision -- the close leg precedes its decision.
	if strings.Join(kinds, ",") != "decision,exit,decision" {
		t.Fatalf("journal kinds = %v", kinds)
	}
	if j.Items[1].ClientID != "flip" || j.Items[1].Qty == nil || j.Items[1].Price == nil {
		t.Fatalf("flip exit entry = %+v", j.Items[1])
	}
}

func TestJournalRecordsDecisionsWithReadableReasons(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})

	e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.30), "low")
	e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.90), "ok")

	j := e.Journal(nil)
	if len(j.Items) != 2 || j.NextSeq != 3 || j.OldestSeq != 1 || j.Gap {
		t.Fatalf("page = %+v", j)
	}
	low, ok := j.Items[0], j.Items[1]
	if low.Seq != 1 || low.Kind != JournalDecision || low.ClientID != "low" || low.Verdict != "rejected_low_confidence" ||
		low.Approved == nil || *low.Approved || low.Reason != "confidence 0.30 < min 0.55" ||
		low.Confidence == nil || *low.Confidence != 0.30 || low.Qty != nil {
		t.Fatalf("low = %+v", low)
	}
	if ok.Approved == nil || !*ok.Approved || ok.Qty == nil || ok.Price == nil {
		t.Fatalf("ok = %+v", ok)
	}
	// 10k equity, 20% notional cap at price 100 -> 20 units; 20*100*2%/10k = 0.4% of equity at risk.
	if want := "sized 20.0 BTC-USD · risk 0.4% to stop"; ok.Reason != want {
		t.Fatalf("approval reason = %q, want %q", ok.Reason, want)
	}
	if ok.At.IsZero() || ok.At.Location() != time.UTC {
		t.Fatalf("at = %v, want UTC wall clock", ok.At)
	}
}

func TestJournalRecordsBracketExitAndHaltOnce(t *testing.T) {
	cfg := risk.Balanced()
	cfg.StopLossPct = 0.5           // wide stop so a plain drop does not exit
	cfg.MaxDailyDrawdownPct = 0.005 // 0.5% session drawdown trips the switch
	e, _, _ := newTestEngine(t, 10_000, cfg)
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	if _, err := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), ""); err != nil {
		t.Fatal(err)
	}

	res, _ := e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 60})
	if !res.Halted {
		t.Fatalf("expected halt, result = %+v", res)
	}
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 59}) // still halted: no 2nd halt entry

	halts := 0
	for _, it := range e.Journal(nil).Items {
		if it.Kind == JournalHalt {
			halts++
			if !strings.HasPrefix(it.Reason, "halted: daily drawdown ") || !strings.HasSuffix(it.Reason, "≥ 0.5%") {
				t.Fatalf("halt reason = %q", it.Reason)
			}
		}
	}
	if halts != 1 {
		t.Fatalf("halt entries = %d, want 1 (transition only)", halts)
	}

	out, _ := e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "")
	if out.Result.Verdict != risk.RejectHalted || !strings.HasPrefix(out.Result.Reason, "halted: daily drawdown ") {
		t.Fatalf("halted decision = %+v", out.Result)
	}
}

func TestJournalBracketExitEntry(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	drain(e)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "")
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 105, High: 105, Low: 100})

	items := e.Journal(nil).Items
	last := items[len(items)-1]
	if last.Kind != JournalExit || last.Symbol != "BTC-USD" || !strings.HasPrefix(last.Reason, "target: closed BTC-USD") ||
		last.Qty == nil || last.Price == nil || *last.Price != 104 {
		t.Fatalf("exit entry = %+v", last)
	}
}

func TestJournalRingWrapAndGap(t *testing.T) {
	j := newJournal()
	for i := 0; i < JournalSize+44; i++ { // 300 entries, 256 kept
		j.append(JournalEntry{Kind: JournalDecision, Reason: fmt.Sprint(i)})
	}
	since := func(n uint64) *uint64 { return &n }

	p := j.read(nil)
	if p.NextSeq != 301 || p.OldestSeq != 45 || p.Gap || len(p.Items) != JournalSize || p.Items[0].Seq != 45 {
		t.Fatalf("read(nil): next=%d oldest=%d gap=%v n=%d first=%d", p.NextSeq, p.OldestSeq, p.Gap, len(p.Items), p.Items[0].Seq)
	}
	p = j.read(since(10)) // overwritten
	if !p.Gap || p.Items[0].Seq != 45 {
		t.Fatalf("read(10): gap=%v first=%d, want gap from 45", p.Gap, p.Items[0].Seq)
	}
	p = j.read(since(45)) // exactly the oldest: nothing lost
	if p.Gap || len(p.Items) != JournalSize {
		t.Fatalf("read(45): gap=%v n=%d", p.Gap, len(p.Items))
	}
	p = j.read(since(300))
	if p.Gap || len(p.Items) != 1 || p.Items[0].Reason != "299" {
		t.Fatalf("read(300): %+v", p)
	}
	p = j.read(since(301)) // caught up
	if p.Gap || len(p.Items) != 0 || p.Items == nil {
		t.Fatalf("read(301): gap=%v items=%v", p.Gap, p.Items)
	}
	for i, it := range j.read(nil).Items {
		if it.Seq != uint64(45+i) {
			t.Fatalf("seq order broken at %d: %d", i, it.Seq)
		}
	}
}

func TestJournalEmptyAndNoSpuriousGap(t *testing.T) {
	j := newJournal()
	zero := uint64(0)
	p := j.read(&zero)
	if p.NextSeq != 1 || p.OldestSeq != 1 || p.Gap || len(p.Items) != 0 {
		t.Fatalf("empty: %+v", p)
	}
	j.append(JournalEntry{Kind: JournalHalt})
	p = j.read(&zero)
	if p.Gap || len(p.Items) != 1 {
		t.Fatalf("since=0 before any wrap must not be a gap: %+v", p)
	}
}

func TestDecisionTimeoutDerivedFromVenue(t *testing.T) {
	e, _, _ := newTestEngine(t, 10_000, risk.Balanced())
	if got := e.DecisionTimeout(); got != 5*time.Second {
		t.Fatalf("sim timeout = %v, want 5s", got)
	}
	ve, _ := venueEngine(t, newFakeVenue())
	// A flip is two venue calls of venueTimeout each, plus 2s margin.
	if got, want := ve.DecisionTimeout(), 2*venueTimeout+2*time.Second; got != want {
		t.Fatalf("venue timeout = %v, want %v", got, want)
	}
}

func TestStatsCarriesSessionBaselineAndLimits(t *testing.T) {
	cfg := risk.Balanced()
	e, _, _ := newTestEngine(t, 10_000, cfg)
	s := e.Stats()
	if s.SessionStartEquity != 10_000 || s.Venue != "sim" || s.Live {
		t.Fatalf("stats = %+v", s)
	}
	want := Limits{
		MaxDailyDrawdownPct: cfg.MaxDailyDrawdownPct, MaxOpenPositions: cfg.MaxOpenPositions,
		MinConfidence: cfg.MinConfidence, MaxPositionPct: cfg.MaxPositionPct,
	}
	if s.Limits != want {
		t.Fatalf("limits = %+v, want %+v", s.Limits, want)
	}
}

func TestVenueFlipReportsCloseLegAndJournalsIt(t *testing.T) {
	fv := newFakeVenue()
	fv.caps.SupportsShort = true
	e, _ := venueEngine(t, fv)
	e.SubmitTickWait(context.Background(), Tick{Symbol: "BTC-USD", Price: 100})
	e.SubmitDecisionFull(context.Background(), buy("BTC-USD", 0.9), "")

	out, err := e.SubmitDecisionFull(context.Background(), sell("BTC-USD", 0.9), "f")
	if err != nil || len(out.Fills) < 1 || out.Fills[0].Reason != "flip" {
		t.Fatalf("out = %+v, %v", out, err)
	}
}
