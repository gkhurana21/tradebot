package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/engine"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
)

type harness struct {
	t   *testing.T
	eng *engine.Engine
	srv *httptest.Server
	api *Server
}

// newHarness runs a real engine behind a real Server. start=false leaves the
// owner goroutine stopped so queues can be filled deterministically.
func newHarness(t *testing.T, start bool) *harness {
	t.Helper()
	b := book.New(book.Options{StartingCash: 10_000})
	eng := engine.New(b, risk.New(risk.Balanced()))
	ctx, cancel := context.WithCancel(context.Background())
	if start {
		eng.Start(ctx)
		go func() {
			for range eng.Events() {
			}
		}()
	}
	api := NewServer(eng, b, slog.New(slog.NewTextHandler(io.Discard, nil)))
	srv := httptest.NewServer(api.Routes())
	t.Cleanup(func() { srv.Close(); eng.Stop(); cancel() })
	return &harness{t: t, eng: eng, srv: srv, api: api}
}

func (h *harness) do(method, path, body string) (int, map[string]any) {
	h.t.Helper()
	req, err := http.NewRequest(method, h.srv.URL+path, bytes.NewBufferString(body))
	if err != nil {
		h.t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		h.t.Fatalf("%s %s: bad JSON: %v", method, path, err)
	}
	return resp.StatusCode, out
}

func (h *harness) tick(price float64, extra string) map[string]any {
	h.t.Helper()
	body := `{"symbol":"BTC-USD","price":` + ftoa(price) + extra + `}`
	code, out := h.do("POST", "/tick?wait=1", body)
	if code != 200 {
		h.t.Fatalf("tick wait = %d %v", code, out)
	}
	return out
}

func ftoa(f float64) string {
	b, _ := json.Marshal(f)
	return string(b)
}

func decisionBody(side string, conf float64, extra string) string {
	return `{"symbol":"BTC-USD","side":"` + side + `","confidence":` + ftoa(conf) + `,"size_hint":1,"rationale":"t"` + extra + `}`
}

func TestHealthReportsAPIVersionBootIDAndVenue(t *testing.T) {
	h := newHarness(t, true)
	code, out := h.do("GET", "/health", "")
	if code != 200 || out["status"] != "ok" || out["api"] != float64(2) || out["venue"] != "sim" || out["live"] != false {
		t.Fatalf("health = %d %v", code, out)
	}
	if _, ok := out["uptime"].(string); !ok {
		t.Fatalf("uptime missing: %v", out)
	}
	id, _ := out["boot_id"].(string)
	if !regexp.MustCompile(`^[0-9a-f]{16}$`).MatchString(id) {
		t.Fatalf("boot_id = %q, want 16 hex chars", id)
	}
	_, again := h.do("GET", "/health", "")
	if again["boot_id"] != id {
		t.Fatalf("boot_id changed within one process")
	}
	other := newHarness(t, false)
	if _, o := other.do("GET", "/health", ""); o["boot_id"] == id {
		t.Fatalf("two servers shared a boot_id")
	}
}

func TestTickWaitReturnsEquityHaltedAndExits(t *testing.T) {
	h := newHarness(t, true)
	out := h.tick(100, "")
	if out["equity"] != float64(10_000) || out["halted"] != false {
		t.Fatalf("tick = %v", out)
	}
	if exits, ok := out["exits"].([]any); !ok || len(exits) != 0 {
		t.Fatalf("exits = %#v, want empty array (not null)", out["exits"])
	}

	if code, out := h.do("POST", "/decision", decisionBody("BUY", 0.9, "")); code != 200 || out["approved"] != true {
		t.Fatalf("buy = %d %v", code, out)
	}
	out = h.tick(99, `,"high":100,"low":97`)
	exits := out["exits"].([]any)
	if len(exits) != 1 {
		t.Fatalf("exits = %v, want the stop exit", exits)
	}
	e := exits[0].(map[string]any)
	if e["symbol"] != "BTC-USD" || e["side"] != "SELL" || e["reason"] != "stop" || e["price"] != float64(98) ||
		e["qty"].(float64) <= 0 {
		t.Fatalf("exit = %v", e)
	}
	if _, ok := e["fee"]; !ok {
		t.Fatalf("exit lacks fee: %v", e)
	}
}

func TestTickWithoutWaitIsUnchanged(t *testing.T) {
	h := newHarness(t, true)
	code, out := h.do("POST", "/tick", `{"symbol":"BTC-USD","price":100}`)
	if code != http.StatusAccepted || out["status"] != "accepted" {
		t.Fatalf("tick = %d %v", code, out)
	}
	if code, _ := h.do("POST", "/tick?wait=1", `{"symbol":"","price":1}`); code != http.StatusUnprocessableEntity {
		t.Fatalf("empty symbol on wait path = %d, want 422", code)
	}
	if code, _ := h.do("POST", "/tick?wait=1", `{"symbol":"X","price":1,"ts":"2024-01-01T00:00:00Z"}`); code != http.StatusBadRequest {
		t.Fatalf("unknown field on wait path = %d, want 400", code)
	}
}

func TestTickWaitIs503WhenBufferFull(t *testing.T) {
	h := newHarness(t, false) // nothing drains the 1024-slot buffer
	for i := 0; i < 1024; i++ {
		if err := h.eng.SubmitTick(engine.Tick{Symbol: "BTC-USD", Price: 100}); err != nil {
			t.Fatal(err)
		}
	}
	code, out := h.do("POST", "/tick?wait=1", `{"symbol":"BTC-USD","price":100}`)
	if code != http.StatusServiceUnavailable || out["error"] == nil {
		t.Fatalf("full buffer on wait path = %d %v, want 503", code, out)
	}
	// The async path keeps its documented drop-and-202 behaviour.
	if code, _ := h.do("POST", "/tick", `{"symbol":"BTC-USD","price":100}`); code != http.StatusAccepted {
		t.Fatalf("async tick on full buffer = %d, want 202 (unchanged)", code)
	}
}

func TestDecisionClientIDEchoFillsAndReplay(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")

	code, first := h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"client_id":"abc-1"`))
	if code != 200 || first["approved"] != true || first["client_id"] != "abc-1" {
		t.Fatalf("first = %d %v", code, first)
	}
	fills := first["fills"].([]any)
	if len(fills) != 1 {
		t.Fatalf("fills = %v", fills)
	}
	f := fills[0].(map[string]any)
	for _, k := range []string{"symbol", "side", "qty", "price", "fee", "reason"} {
		if _, ok := f[k]; !ok {
			t.Fatalf("fill lacks %q: %v", k, f)
		}
	}
	if _, replayed := first["replayed"]; replayed {
		t.Fatalf("first response must not be marked replayed")
	}

	code, again := h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"client_id":"abc-1"`))
	if code != 200 || again["replayed"] != true || again["verdict"] != first["verdict"] ||
		again["fills"].([]any)[0].(map[string]any)["price"] != f["price"] {
		t.Fatalf("repeat = %d %v", code, again)
	}
	if _, stats := h.do("GET", "/stats", ""); stats["decisions"] != float64(1) || stats["fills"] != float64(1) {
		t.Fatalf("stats after repeat = %v, want 1 decision, 1 fill", stats)
	}
}

func TestDecisionWithoutClientIDStillWorksAndRejectsHaveEmptyFills(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")
	code, out := h.do("POST", "/decision", decisionBody("BUY", 0.3, ""))
	if code != 200 || out["approved"] != false || out["verdict"] != "rejected_low_confidence" ||
		out["reason"] != "confidence 0.30 < min 0.55" {
		t.Fatalf("reject = %d %v", code, out)
	}
	if fills, ok := out["fills"].([]any); !ok || len(fills) != 0 {
		t.Fatalf("fills = %#v, want []", out["fills"])
	}
	if _, has := out["client_id"]; has {
		t.Fatalf("client_id must be omitted when none was sent: %v", out)
	}
}

func TestDecisionFlipFillsIncludeCloseLeg(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")
	h.do("POST", "/decision", decisionBody("BUY", 0.9, ""))
	code, out := h.do("POST", "/decision", decisionBody("SELL", 0.9, `,"client_id":"flip-1"`))
	if code != 200 || out["approved"] != true {
		t.Fatalf("flip = %d %v", code, out)
	}
	fills := out["fills"].([]any)
	if len(fills) != 2 || fills[0].(map[string]any)["reason"] != "flip" {
		t.Fatalf("fills = %v, want [flip close, entry]", fills)
	}
}

func TestDecisionBootIDMismatchIs409BeforeActing(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")
	code, out := h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"client_id":"x","expect_boot_id":"deadbeefdeadbeef"`))
	if code != http.StatusConflict || out["error"] == nil {
		t.Fatalf("mismatch = %d %v, want 409", code, out)
	}
	if _, stats := h.do("GET", "/stats", ""); stats["decisions"] != float64(0) || stats["fills"] != float64(0) {
		t.Fatalf("a 409 must not evaluate the decision; stats = %v", stats)
	}
	if code, _ := h.do("GET", "/decisions", ""); code != 200 {
		t.Fatal("journal unreachable")
	}
	if _, j := h.do("GET", "/decisions", ""); len(j["items"].([]any)) != 0 {
		t.Fatalf("a 409 must not journal: %v", j)
	}

	// The same client_id with the right boot id then runs normally.
	_, health := h.do("GET", "/health", "")
	extra := `,"client_id":"x","expect_boot_id":"` + health["boot_id"].(string) + `"`
	if code, out := h.do("POST", "/decision", decisionBody("BUY", 0.9, extra)); code != 200 || out["approved"] != true {
		t.Fatalf("matching boot id = %d %v", code, out)
	}
}

func TestDecisionStillRejectsUnknownFieldsAndLongClientIDs(t *testing.T) {
	h := newHarness(t, true)
	if code, _ := h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"bogus":1`)); code != http.StatusBadRequest {
		t.Fatalf("unknown field = %d, want 400", code)
	}
	long := strings.Repeat("a", maxClientIDLen+1)
	if code, _ := h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"client_id":"`+long+`"`)); code != http.StatusBadRequest {
		t.Fatalf("overlong client_id = %d, want 400", code)
	}
}

func TestDecisionsEndpointPaginatesAndFlagsGaps(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")
	h.do("POST", "/decision", decisionBody("BUY", 0.3, `,"client_id":"a"`))
	h.do("POST", "/decision", decisionBody("BUY", 0.9, `,"client_id":"b"`))
	h.tick(99, `,"high":100,"low":97`) // stop exit

	code, page := h.do("GET", "/decisions", "")
	items := page["items"].([]any)
	if code != 200 || len(items) != 3 || page["next_seq"] != float64(4) || page["oldest_seq"] != float64(1) || page["gap"] != false {
		t.Fatalf("page = %d %v", code, page)
	}
	kinds := []string{}
	for _, it := range items {
		m := it.(map[string]any)
		kinds = append(kinds, m["kind"].(string))
		if _, err := parseRFC3339Nano(m["at"].(string)); err != nil {
			t.Fatalf("at %q: %v", m["at"], err)
		}
		if m["reason"] == "" || m["seq"] == nil {
			t.Fatalf("entry = %v", m)
		}
	}
	if strings.Join(kinds, ",") != "decision,decision,exit" {
		t.Fatalf("kinds = %v", kinds)
	}
	first := items[0].(map[string]any)
	if first["client_id"] != "a" || first["approved"] != false || first["verdict"] != "rejected_low_confidence" || first["confidence"] != 0.3 {
		t.Fatalf("first = %v", first)
	}

	_, rest := h.do("GET", "/decisions?since=3", "")
	if its := rest["items"].([]any); len(its) != 1 || its[0].(map[string]any)["kind"] != "exit" || rest["gap"] != false {
		t.Fatalf("since=3 = %v", rest)
	}
	_, caughtUp := h.do("GET", "/decisions?since=4", "")
	if len(caughtUp["items"].([]any)) != 0 || caughtUp["next_seq"] != float64(4) {
		t.Fatalf("since=4 = %v", caughtUp)
	}
	if code, _ := h.do("GET", "/decisions?since=abc", ""); code != http.StatusBadRequest {
		t.Fatalf("bad since = %d, want 400", code)
	}
}

func TestDecisionsEndpointGapAfterWrap(t *testing.T) {
	h := newHarness(t, true)
	// 300 cheap rejections (no market data) overflow the 256 ring.
	for i := 0; i < 300; i++ {
		h.do("POST", "/decision", decisionBody("BUY", 0.9, ""))
	}
	_, page := h.do("GET", "/decisions?since=5", "")
	if page["gap"] != true || page["oldest_seq"] != float64(45) || page["next_seq"] != float64(301) ||
		len(page["items"].([]any)) != 256 {
		t.Fatalf("wrapped page: gap=%v oldest=%v next=%v n=%d", page["gap"], page["oldest_seq"], page["next_seq"], len(page["items"].([]any)))
	}
	_, ok := h.do("GET", "/decisions?since=45", "")
	if ok["gap"] != false {
		t.Fatalf("since=oldest must not be a gap: %v", ok["gap"])
	}
}

func TestPositionsCarryMarkAndUnrealizedPnL(t *testing.T) {
	h := newHarness(t, true)
	h.tick(100, "")
	_, d := h.do("POST", "/decision", decisionBody("BUY", 0.9, ""))
	qty := d["order"].(map[string]any)["qty"].(float64)
	h.tick(101, `,"high":101,"low":100`)

	code, out := h.do("GET", "/positions", "")
	if code != 200 || out["equity"] == nil || out["cash"] == nil {
		t.Fatalf("positions = %d %v", code, out)
	}
	p := out["positions"].(map[string]any)["BTC-USD"].(map[string]any)
	for _, k := range []string{"symbol", "qty", "avg_price", "stop_loss", "take_profit", "mark", "unrealized_pnl"} {
		if _, ok := p[k]; !ok {
			t.Fatalf("position lacks %q: %v", k, p)
		}
	}
	if p["mark"] != float64(101) {
		t.Fatalf("mark = %v, want 101", p["mark"])
	}
	want := qty * (101 - p["avg_price"].(float64))
	if got := p["unrealized_pnl"].(float64); got < want-1e-9 || got > want+1e-9 || got <= 0 {
		t.Fatalf("unrealized_pnl = %v, want %v", got, want)
	}
}

func TestStatsCarriesSessionStartEquityAndLimits(t *testing.T) {
	h := newHarness(t, true)
	_, out := h.do("GET", "/stats", "")
	if out["session_start_equity"] != float64(10_000) || out["venue"] != "sim" || out["live"] != false {
		t.Fatalf("stats = %v", out)
	}
	for _, k := range []string{"ticks", "decisions", "fills", "rejected", "exits", "halted", "equity"} {
		if _, ok := out[k]; !ok {
			t.Fatalf("existing stats field %q lost", k)
		}
	}
	lim := out["limits"].(map[string]any)
	if lim["max_daily_drawdown_pct"] != 0.05 || lim["max_open_positions"] != float64(3) ||
		lim["min_confidence"] != 0.55 || lim["max_position_pct"] != 0.2 {
		t.Fatalf("limits = %v", lim)
	}
}

func parseRFC3339Nano(s string) (time.Time, error) { return time.Parse(time.RFC3339Nano, s) }
