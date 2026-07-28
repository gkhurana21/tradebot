package venue

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// stubDoer records every request and replays scripted responses, so request
// shaping and retry behavior are verified without a network or an account.
type stubDoer struct {
	mu       sync.Mutex
	requests []recorded
	replies  []reply
	calls    int
}

type recorded struct {
	method string
	path   string // decoded
	// wirePath is URL.RequestURI() -- the bytes actually sent. A crypto
	// symbol's slash must stay percent-encoded here or the request lands on
	// a different endpoint, and only this field shows that.
	wirePath string
	body     string
	keyID    string
}

type reply struct {
	status int
	body   string
	err    error
}

func (s *stubDoer) Do(req *http.Request) (*http.Response, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	body := ""
	if req.Body != nil {
		raw, _ := io.ReadAll(req.Body)
		body = string(raw)
	}
	s.requests = append(s.requests, recorded{
		method:   req.Method,
		path:     req.URL.Path,
		wirePath: req.URL.RequestURI(),
		body:     body,
		keyID:    req.Header.Get("APCA-API-KEY-ID"),
	})

	i := s.calls
	s.calls++
	if i >= len(s.replies) {
		i = len(s.replies) - 1
	}
	r := s.replies[i]
	if r.err != nil {
		return nil, r.err
	}
	return &http.Response{
		StatusCode: r.status,
		Body:       io.NopCloser(strings.NewReader(r.body)),
		Header:     make(http.Header),
	}, nil
}

func (s *stubDoer) snapshot() []recorded {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]recorded, len(s.requests))
	copy(out, s.requests)
	return out
}

func newTestAlpaca(t *testing.T, stub *stubDoer) *Alpaca {
	t.Helper()
	t.Setenv("APCA_API_KEY_ID", "test-key")
	t.Setenv("APCA_API_SECRET_KEY", "test-secret")
	a, err := NewAlpacaPaper(WithHTTPClient(stub), WithMaxRetries(2))
	if err != nil {
		t.Fatalf("constructing client: %v", err)
	}
	return a
}

const filledOrderJSON = `{
  "id":"o-1","client_order_id":"key-1","symbol":"BTC/USD","side":"buy",
  "status":"filled","qty":"0.05","filled_qty":"0.05",
  "filled_avg_price":"64000.5","filled_at":"2026-07-28T02:00:00Z"
}`

func TestRequiresCredentials(t *testing.T) {
	t.Setenv("APCA_API_KEY_ID", "")
	t.Setenv("APCA_API_SECRET_KEY", "")
	if _, err := NewAlpacaPaper(); !errors.Is(err, ErrNotConfigured) {
		t.Fatalf("err = %v, want ErrNotConfigured", err)
	}
}

func TestPaperAndLiveAreDistinguishable(t *testing.T) {
	t.Setenv("APCA_API_KEY_ID", "k")
	t.Setenv("APCA_API_SECRET_KEY", "s")

	paper, _ := NewAlpacaPaper()
	if paper.IsLive() || paper.Name() != "alpaca-paper" || paper.baseURL != AlpacaPaperURL {
		t.Errorf("paper client misconfigured: live=%v name=%q url=%q",
			paper.IsLive(), paper.Name(), paper.baseURL)
	}

	live, _ := NewAlpacaLive()
	if !live.IsLive() || live.Name() != "alpaca-live" || live.baseURL != AlpacaLiveURL {
		t.Errorf("live client misconfigured: live=%v name=%q url=%q",
			live.IsLive(), live.Name(), live.baseURL)
	}
}

func TestPlaceSendsCorrectWireFormat(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: filledOrderJSON}}}
	a := newTestAlpaca(t, stub)

	fill, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "key-1")
	if err != nil {
		t.Fatalf("Place: %v", err)
	}

	reqs := stub.snapshot()
	if len(reqs) != 1 {
		t.Fatalf("made %d requests, want 1", len(reqs))
	}
	got := reqs[0]

	if got.method != http.MethodPost || got.path != "/v2/orders" {
		t.Errorf("sent %s %s, want POST /v2/orders", got.method, got.path)
	}
	if got.keyID != "test-key" {
		t.Errorf("auth header = %q, want the configured key", got.keyID)
	}
	// Crypto symbols must be translated: Alpaca rejects "BTC-USD".
	if !strings.Contains(got.body, `"symbol":"BTC/USD"`) {
		t.Errorf("body %s: want symbol translated to BTC/USD", got.body)
	}
	// Crypto accepts only gtc or ioc; "day" is rejected by the venue.
	if !strings.Contains(got.body, `"time_in_force":"gtc"`) {
		t.Errorf("body %s: want time_in_force gtc", got.body)
	}
	if !strings.Contains(got.body, `"client_order_id":"key-1"`) {
		t.Errorf("body %s: want the idempotency key sent as client_order_id", got.body)
	}
	if !strings.Contains(got.body, `"side":"buy"`) {
		t.Errorf("body %s: want lowercase side", got.body)
	}

	if fill.Symbol != "BTC-USD" {
		t.Errorf("fill symbol = %q, want the engine format BTC-USD", fill.Symbol)
	}
	if fill.Qty != 0.05 || fill.Price != 64000.5 {
		t.Errorf("fill = %+v, want qty 0.05 @ 64000.5", fill)
	}
}

// A timeout on a POST leaves the outcome unknown. Retrying is only safe
// because the same client_order_id is reused -- the venue deduplicates, so at
// most one order can exist.
func TestRetryReusesIdempotencyKey(t *testing.T) {
	stub := &stubDoer{replies: []reply{
		{status: 503, body: `{"message":"service unavailable"}`},
		{status: 200, body: filledOrderJSON},
	}}
	a := newTestAlpaca(t, stub)

	if _, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "stable-key"); err != nil {
		t.Fatalf("Place: %v", err)
	}

	reqs := stub.snapshot()
	if len(reqs) != 2 {
		t.Fatalf("made %d requests, want 2 (one retry)", len(reqs))
	}
	for i, r := range reqs {
		if !strings.Contains(r.body, `"client_order_id":"stable-key"`) {
			t.Errorf("request %d used a different idempotency key: %s", i, r.body)
		}
	}
}

// A 4xx means the request itself is wrong. Retrying cannot help, and on an
// order endpoint a blind retry risks a duplicate if the venue's rejection was
// itself spurious -- so it must not happen.
func TestClientErrorIsNotRetried(t *testing.T) {
	stub := &stubDoer{replies: []reply{
		{status: 422, body: `{"code":40010001,"message":"insufficient balance"}`},
	}}
	a := newTestAlpaca(t, stub)

	_, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "key-1")
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("err = %v, want ErrRejected", err)
	}
	if !strings.Contains(err.Error(), "insufficient balance") {
		t.Errorf("err = %v, want the venue's message surfaced", err)
	}
	if n := len(stub.snapshot()); n != 1 {
		t.Fatalf("made %d requests, want exactly 1 (no retry on 4xx)", n)
	}
}

func TestTransientFailureExhaustsRetries(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 500, body: `{"message":"boom"}`}}}
	a := newTestAlpaca(t, stub)

	_, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "key-1")
	if !errors.Is(err, ErrTransient) {
		t.Fatalf("err = %v, want ErrTransient", err)
	}
	// Initial attempt plus two retries.
	if n := len(stub.snapshot()); n != 3 {
		t.Fatalf("made %d attempts, want 3", n)
	}
}

func TestRejectedOrderStatusIsAnError(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: `{
	  "id":"o-2","symbol":"BTC/USD","side":"buy","status":"rejected",
	  "qty":"0.05","filled_qty":"0","filled_avg_price":""
	}`}}}
	a := newTestAlpaca(t, stub)

	// HTTP 200 with a rejected status is still a rejection.
	_, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "key-1")
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("err = %v, want ErrRejected", err)
	}
}

// An accepted-but-unfilled order must report zero quantity rather than
// pretending it filled; claiming a fill would desync the local book.
func TestUnfilledOrderReportsZeroQuantity(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: `{
	  "id":"o-3","symbol":"BTC/USD","side":"buy","status":"accepted",
	  "qty":"0.05","filled_qty":"0","filled_avg_price":""
	}`}}}
	a := newTestAlpaca(t, stub)

	fill, err := a.Place(context.Background(), types.Order{
		Symbol: "BTC-USD", Side: types.Buy, Qty: 0.05,
	}, "key-1")
	if err != nil {
		t.Fatalf("Place: %v", err)
	}
	if fill.Qty != 0 {
		t.Errorf("fill qty = %v, want 0 for an unfilled order", fill.Qty)
	}
}

func TestPositionsAreTranslatedAndSigned(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: `[
	  {"symbol":"BTC/USD","qty":"0.5","avg_entry_price":"64000","side":"long"},
	  {"symbol":"AAPL","qty":"-10","avg_entry_price":"180.25","side":"short"}
	]`}}}
	a := newTestAlpaca(t, stub)

	positions, err := a.Positions(context.Background())
	if err != nil {
		t.Fatalf("Positions: %v", err)
	}
	btc, ok := positions["BTC-USD"]
	if !ok {
		t.Fatalf("positions keyed by %v, want the engine format BTC-USD", keys(positions))
	}
	if btc.Qty != 0.5 || btc.AvgPrice != 64000 {
		t.Errorf("btc = %+v, want qty 0.5 @ 64000", btc)
	}
	if aapl := positions["AAPL"]; aapl.Qty != -10 {
		t.Errorf("short qty = %v, want -10", aapl.Qty)
	}
}

func TestCloseEscapesTheSymbolPath(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: filledOrderJSON}}}
	a := newTestAlpaca(t, stub)

	if _, err := a.Close(context.Background(), "BTC-USD", "stop"); err != nil {
		t.Fatalf("Close: %v", err)
	}
	got := stub.snapshot()[0]
	if got.method != http.MethodDelete {
		t.Errorf("method = %s, want DELETE", got.method)
	}
	// An unescaped "BTC/USD" would be read as a nested path and hit the
	// wrong endpoint entirely. Assert on the wire form: URL.Path is the
	// decoded view and always shows a bare slash.
	if !strings.Contains(got.wirePath, "BTC%2FUSD") {
		t.Errorf("wire path %q: symbol slash must be percent-encoded", got.wirePath)
	}
}

func TestAccountParsesStringNumbers(t *testing.T) {
	stub := &stubDoer{replies: []reply{{status: 200, body: `{
	  "equity":"10250.75","cash":"9000.25","buying_power":"18000.50","currency":"USD"
	}`}}}
	a := newTestAlpaca(t, stub)

	acct, err := a.Account(context.Background())
	if err != nil {
		t.Fatalf("Account: %v", err)
	}
	if acct.Equity != 10250.75 || acct.Cash != 9000.25 || acct.BuyingPower != 18000.50 {
		t.Errorf("account = %+v, want the string fields parsed", acct)
	}
}

// Preflight is the guard that keeps impossible orders off the wire. Spot
// crypto cannot be shorted, so a sell with no inventory must be caught
// locally rather than rejected by the venue.
func TestPreflight(t *testing.T) {
	spot := Capabilities{SupportsShort: false, SupportsFractional: true}

	tests := []struct {
		name    string
		order   types.Order
		held    types.Position
		caps    Capabilities
		wantErr bool
	}{
		{
			name:  "buy is always allowed",
			order: types.Order{Symbol: "BTC-USD", Side: types.Buy, Qty: 0.5},
			caps:  spot,
		},
		{
			name:    "sell with no inventory would open a short",
			order:   types.Order{Symbol: "BTC-USD", Side: types.Sell, Qty: 0.5},
			held:    types.Position{Symbol: "BTC-USD", Qty: 0},
			caps:    spot,
			wantErr: true,
		},
		{
			name:  "selling held inventory is a close, not a short",
			order: types.Order{Symbol: "BTC-USD", Side: types.Sell, Qty: 0.5},
			held:  types.Position{Symbol: "BTC-USD", Qty: 0.5},
			caps:  spot,
		},
		{
			name:    "selling more than held would go short",
			order:   types.Order{Symbol: "BTC-USD", Side: types.Sell, Qty: 0.9},
			held:    types.Position{Symbol: "BTC-USD", Qty: 0.5},
			caps:    spot,
			wantErr: true,
		},
		{
			name:  "shorting is fine where the venue supports it",
			order: types.Order{Symbol: "AAPL", Side: types.Sell, Qty: 10},
			caps:  Capabilities{SupportsShort: true, SupportsFractional: true},
		},
		{
			name:    "fractional quantity on a whole-unit venue",
			order:   types.Order{Symbol: "AAPL", Side: types.Buy, Qty: 1.5},
			caps:    Capabilities{SupportsShort: true, SupportsFractional: false},
			wantErr: true,
		},
		{
			name:    "non-positive quantity",
			order:   types.Order{Symbol: "BTC-USD", Side: types.Buy, Qty: 0},
			caps:    spot,
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := Preflight(tc.order, tc.held, tc.caps)
			if tc.wantErr {
				if err == nil {
					t.Fatal("expected a preflight error")
				}
				if !errors.Is(err, ErrUnsupported) {
					t.Errorf("err = %v, want it to wrap ErrUnsupported", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected preflight error: %v", err)
			}
		})
	}
}

func TestAlpacaAdvertisesSpotCryptoLimits(t *testing.T) {
	t.Setenv("APCA_API_KEY_ID", "k")
	t.Setenv("APCA_API_SECRET_KEY", "s")
	a, _ := NewAlpacaPaper()

	caps := a.Capabilities()
	if caps.SupportsShort {
		t.Error("spot crypto cannot be shorted; capability must say so")
	}
	if caps.SupportsBrackets {
		t.Error("Alpaca does not accept brackets on crypto; the engine must hold them locally")
	}
	if !caps.SupportsFractional {
		t.Error("crypto is fractionable")
	}
}

func TestSymbolTranslationRoundTrips(t *testing.T) {
	for _, sym := range []string{"BTC-USD", "ETH-USD", "AAPL"} {
		if got := toEngineSymbol(toVenueSymbol(sym)); got != sym {
			t.Errorf("round trip of %q gave %q", sym, got)
		}
	}
	if got := toVenueSymbol("BTC-USD"); got != "BTC/USD" {
		t.Errorf("toVenueSymbol(BTC-USD) = %q, want BTC/USD", got)
	}
	if got := toVenueSymbol("AAPL"); got != "AAPL" {
		t.Errorf("equity symbol should pass through, got %q", got)
	}
}

func keys(m map[string]types.Position) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
