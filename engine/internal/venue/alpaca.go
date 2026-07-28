package venue

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/types"
)

const (
	AlpacaPaperURL = "https://paper-api.alpaca.markets"
	AlpacaLiveURL  = "https://api.alpaca.markets"
)

// Doer is the HTTP seam. Tests substitute a stub so request shaping, retry,
// and error mapping are verified without touching the network.
type Doer interface {
	Do(*http.Request) (*http.Response, error)
}

// Alpaca trades through Alpaca's REST API.
//
// Crypto on Alpaca has three constraints this client enforces rather than
// discovers at runtime:
//   - symbols are "BTC/USD", not the engine's "BTC-USD"
//   - time-in-force is gtc or ioc; "day" is rejected
//   - brackets and short selling are unavailable, so the engine keeps
//     managing stops locally and Preflight blocks shorts before they are sent
type Alpaca struct {
	baseURL string
	keyID   string
	secret  string
	live    bool
	http    Doer

	// maxRetries bounds retries of *transient* failures only. Retries reuse
	// the caller's idempotency key, so a retried order cannot double-fill.
	maxRetries int
}

// AlpacaOption configures the client.
type AlpacaOption func(*Alpaca)

// WithHTTPClient overrides the HTTP client (used by tests).
func WithHTTPClient(d Doer) AlpacaOption {
	return func(a *Alpaca) { a.http = d }
}

// WithMaxRetries sets how many times a transient failure is retried.
func WithMaxRetries(n int) AlpacaOption {
	return func(a *Alpaca) { a.maxRetries = n }
}

// NewAlpacaPaper returns a client pointed at the paper environment.
//
// Credentials come from APCA_API_KEY_ID and APCA_API_SECRET_KEY. They are
// read from the environment rather than accepted as arguments so they cannot
// end up in a command line, a shell history, or a process listing.
func NewAlpacaPaper(opts ...AlpacaOption) (*Alpaca, error) {
	return newAlpaca(AlpacaPaperURL, false, opts...)
}

// NewAlpacaLive returns a client that trades real money.
//
// Nothing in this package calls it: reaching live execution requires an
// operator to pass an explicit flag at startup. That is deliberate -- the
// difference between paper and live should never be a default.
func NewAlpacaLive(opts ...AlpacaOption) (*Alpaca, error) {
	return newAlpaca(AlpacaLiveURL, true, opts...)
}

func newAlpaca(baseURL string, live bool, opts ...AlpacaOption) (*Alpaca, error) {
	a := &Alpaca{
		baseURL:    baseURL,
		keyID:      os.Getenv("APCA_API_KEY_ID"),
		secret:     os.Getenv("APCA_API_SECRET_KEY"),
		live:       live,
		http:       &http.Client{Timeout: 15 * time.Second},
		maxRetries: 2,
	}
	for _, opt := range opts {
		opt(a)
	}
	if a.keyID == "" || a.secret == "" {
		return nil, fmt.Errorf("%w: set APCA_API_KEY_ID and APCA_API_SECRET_KEY",
			ErrNotConfigured)
	}
	return a, nil
}

func (a *Alpaca) Name() string {
	if a.live {
		return "alpaca-live"
	}
	return "alpaca-paper"
}

func (a *Alpaca) IsLive() bool { return a.live }

func (a *Alpaca) Capabilities() Capabilities {
	return Capabilities{
		// Spot crypto cannot be sold short, and Alpaca does not accept
		// bracket orders on crypto -- only market, limit, and stop-limit.
		SupportsShort:      false,
		SupportsBrackets:   false,
		SupportsFractional: true,
	}
}

// --- symbology ------------------------------------------------------------

// toVenueSymbol converts the engine's "BTC-USD" to Alpaca's "BTC/USD".
// Equity symbols have no separator and pass through untouched.
func toVenueSymbol(symbol string) string {
	return strings.ReplaceAll(symbol, "-", "/")
}

// toEngineSymbol reverses toVenueSymbol.
func toEngineSymbol(symbol string) string {
	return strings.ReplaceAll(symbol, "/", "-")
}

// --- API types ------------------------------------------------------------

type alpacaOrderRequest struct {
	Symbol        string `json:"symbol"`
	Qty           string `json:"qty"`
	Side          string `json:"side"`
	Type          string `json:"type"`
	TimeInForce   string `json:"time_in_force"`
	ClientOrderID string `json:"client_order_id,omitempty"`
}

type alpacaOrder struct {
	ID             string `json:"id"`
	ClientOrderID  string `json:"client_order_id"`
	Symbol         string `json:"symbol"`
	Side           string `json:"side"`
	Status         string `json:"status"`
	Qty            string `json:"qty"`
	FilledQty      string `json:"filled_qty"`
	FilledAvgPrice string `json:"filled_avg_price"`
	FilledAt       string `json:"filled_at"`
}

type alpacaPosition struct {
	Symbol       string `json:"symbol"`
	Qty          string `json:"qty"`
	AvgEntry     string `json:"avg_entry_price"`
	Side         string `json:"side"`
	CurrentPrice string `json:"current_price"`
}

type alpacaAccount struct {
	Equity      string `json:"equity"`
	Cash        string `json:"cash"`
	BuyingPower string `json:"buying_power"`
	Currency    string `json:"currency"`
}

type alpacaError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// --- operations -----------------------------------------------------------

func (a *Alpaca) Account(ctx context.Context) (Account, error) {
	var out alpacaAccount
	if err := a.do(ctx, http.MethodGet, "/v2/account", nil, &out); err != nil {
		return Account{}, err
	}
	return Account{
		Equity:      parseFloat(out.Equity),
		Cash:        parseFloat(out.Cash),
		BuyingPower: parseFloat(out.BuyingPower),
		Currency:    out.Currency,
	}, nil
}

func (a *Alpaca) Positions(ctx context.Context) (map[string]types.Position, error) {
	var out []alpacaPosition
	if err := a.do(ctx, http.MethodGet, "/v2/positions", nil, &out); err != nil {
		return nil, err
	}
	positions := make(map[string]types.Position, len(out))
	for _, p := range out {
		qty := parseFloat(p.Qty)
		// Alpaca reports short positions with a negative qty and side "short";
		// trust the sign but normalize in case only the side is populated.
		if p.Side == "short" && qty > 0 {
			qty = -qty
		}
		symbol := toEngineSymbol(p.Symbol)
		positions[symbol] = types.Position{
			Symbol:   symbol,
			Qty:      qty,
			AvgPrice: parseFloat(p.AvgEntry),
		}
	}
	return positions, nil
}

// Place submits a market order and returns the resulting fill.
//
// idempotencyKey becomes the client_order_id. Alpaca rejects a duplicate, so
// a retry after a timeout cannot place a second order -- the retry either
// succeeds or comes back as a duplicate, and both mean exactly one order
// exists. Without this, a network timeout on a POST is genuinely ambiguous:
// the order may or may not have reached the venue.
func (a *Alpaca) Place(ctx context.Context, order types.Order, idempotencyKey string) (types.Fill, error) {
	if order.Qty <= 0 {
		return types.Fill{}, fmt.Errorf("%w: non-positive quantity", ErrRejected)
	}

	req := alpacaOrderRequest{
		Symbol: toVenueSymbol(order.Symbol),
		Qty:    strconv.FormatFloat(order.Qty, 'f', -1, 64),
		Side:   strings.ToLower(string(order.Side)),
		Type:   "market",
		// Crypto accepts only gtc or ioc. gtc is correct for a market order
		// that should rest until it fills.
		TimeInForce:   "gtc",
		ClientOrderID: idempotencyKey,
	}

	var out alpacaOrder
	if err := a.do(ctx, http.MethodPost, "/v2/orders", req, &out); err != nil {
		return types.Fill{}, err
	}
	return a.fillFromOrder(out, order, "entry")
}

// Close flattens a position at market.
func (a *Alpaca) Close(ctx context.Context, symbol, reason string) (types.Fill, error) {
	path := "/v2/positions/" + urlEscape(toVenueSymbol(symbol))
	var out alpacaOrder
	if err := a.do(ctx, http.MethodDelete, path, nil, &out); err != nil {
		return types.Fill{}, err
	}
	return a.fillFromOrder(out, types.Order{Symbol: symbol}, reason)
}

// fillFromOrder converts an Alpaca order response into a Fill.
//
// A market order is frequently "accepted" before it is "filled", so a zero
// filled_qty is reported honestly rather than assumed complete -- claiming a
// fill that has not happened would desynchronize the local book from reality.
func (a *Alpaca) fillFromOrder(o alpacaOrder, requested types.Order, reason string) (types.Fill, error) {
	switch o.Status {
	case "rejected", "canceled", "expired":
		return types.Fill{}, fmt.Errorf("%w: order %s is %s", ErrRejected, o.ID, o.Status)
	}

	filled := parseFloat(o.FilledQty)
	price := parseFloat(o.FilledAvgPrice)

	symbol := requested.Symbol
	if symbol == "" {
		symbol = toEngineSymbol(o.Symbol)
	}
	side := requested.Side
	if side == "" {
		if strings.EqualFold(o.Side, "sell") {
			side = types.Sell
		} else {
			side = types.Buy
		}
	}

	ts := time.Now().UTC()
	if o.FilledAt != "" {
		if parsed, err := time.Parse(time.RFC3339Nano, o.FilledAt); err == nil {
			ts = parsed
		}
	}

	return types.Fill{
		TS:     ts,
		Symbol: symbol,
		Side:   side,
		Qty:    filled,
		Price:  price,
		Reason: reason,
	}, nil
}

// --- transport ------------------------------------------------------------

// do performs a request, retrying only transient failures.
//
// A 4xx is never retried: the request itself is wrong, and repeating it just
// burns rate limit. A 429 or 5xx is retried with backoff, which is safe
// because Place carries an idempotency key.
func (a *Alpaca) do(ctx context.Context, method, path string, body, out any) error {
	var lastErr error
	for attempt := 0; attempt <= a.maxRetries; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(1<<uint(attempt-1)) * 500 * time.Millisecond
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(backoff):
			}
		}

		err := a.attempt(ctx, method, path, body, out)
		if err == nil {
			return nil
		}
		lastErr = err
		// Only a transient failure is worth another attempt.
		if !isTransient(err) {
			return err
		}
	}
	return lastErr
}

func (a *Alpaca) attempt(ctx context.Context, method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("%w: encoding request: %v", ErrRejected, err)
		}
		reader = bytes.NewReader(encoded)
	}

	req, err := http.NewRequestWithContext(ctx, method, a.baseURL+path, reader)
	if err != nil {
		return fmt.Errorf("%w: building request: %v", ErrRejected, err)
	}
	req.Header.Set("APCA-API-KEY-ID", a.keyID)
	req.Header.Set("APCA-API-SECRET-KEY", a.secret)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := a.http.Do(req)
	if err != nil {
		// A transport error leaves the outcome unknown; the idempotency key
		// is what makes retrying it safe.
		return fmt.Errorf("%w: %v", ErrTransient, err)
	}
	defer resp.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("%w: reading response: %v", ErrTransient, err)
	}

	switch {
	case resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500:
		return fmt.Errorf("%w: HTTP %d: %s", ErrTransient, resp.StatusCode, snippet(payload))
	case resp.StatusCode >= 400:
		var apiErr alpacaError
		_ = json.Unmarshal(payload, &apiErr)
		msg := apiErr.Message
		if msg == "" {
			msg = snippet(payload)
		}
		return fmt.Errorf("%w: HTTP %d: %s", ErrRejected, resp.StatusCode, msg)
	}

	if out != nil && len(payload) > 0 {
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("%w: decoding response: %v", ErrRejected, err)
		}
	}
	return nil
}

func isTransient(err error) bool {
	return err != nil && strings.Contains(err.Error(), ErrTransient.Error())
}

func parseFloat(s string) float64 {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return v
}

func snippet(b []byte) string {
	const max = 200
	if len(b) > max {
		return string(b[:max]) + "..."
	}
	return string(b)
}

// urlEscape escapes a path segment. Crypto symbols contain a slash, which
// would otherwise be read as a path separator and hit the wrong endpoint.
func urlEscape(s string) string {
	return strings.ReplaceAll(s, "/", "%2F")
}
