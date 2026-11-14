// Package api exposes the execution engine over HTTP.
//
// The seam is deliberate: the Python research layer decides *what* to trade
// (slow, LLM-assisted, backtested) and posts a Decision here; this service
// decides *whether and how much* (fast, deterministic, risk-bounded). A
// research bug cannot bypass the gate, because the gate lives behind a
// process boundary it does not control.
package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/engine"
	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// APIVersion is the wire contract version reported by /health. Clients that
// need the client_id / fills / journal features refuse to run below 2.
const APIVersion = 2

// maxClientIDLen bounds a client_id so the dedupe map cannot be bloated.
const maxClientIDLen = 128

// tickWaitTimeout bounds a synchronous tick. The owner applies it in
// microseconds; this only guards a wedged engine.
const tickWaitTimeout = 5 * time.Second

// Server routes HTTP requests into the engine.
type Server struct {
	eng    *engine.Engine
	book   *book.Book
	log    *slog.Logger
	start  time.Time
	bootID string
}

// NewServer returns a Server bound to eng and b. It draws a random boot_id so
// a client can detect that the engine restarted underneath it.
func NewServer(eng *engine.Engine, b *book.Book, log *slog.Logger) *Server {
	return &Server{eng: eng, book: b, log: log, start: time.Now(), bootID: newBootID()}
}

func newBootID() string {
	var buf [8]byte
	if _, err := rand.Read(buf[:]); err != nil {
		// crypto/rand failing is essentially impossible; fall back to the
		// clock so the id is still unique per process.
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(buf[:])
}

// Routes returns the HTTP handler for the service.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /stats", s.handleStats)
	mux.HandleFunc("GET /positions", s.handlePositions)
	mux.HandleFunc("GET /decisions", s.handleDecisions)
	mux.HandleFunc("POST /tick", s.handleTick)
	mux.HandleFunc("POST /decision", s.handleDecision)
	return s.withLogging(mux)
}

type healthResponse struct {
	Status string `json:"status"`
	Uptime string `json:"uptime"`
	API    int    `json:"api"`
	BootID string `json:"boot_id"`
	Venue  string `json:"venue"`
	Live   bool   `json:"live"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	st := s.eng.Stats()
	writeJSON(w, http.StatusOK, healthResponse{
		Status: "ok",
		Uptime: time.Since(s.start).Round(time.Second).String(),
		API:    APIVersion,
		BootID: s.bootID,
		Venue:  st.Venue,
		Live:   st.Live,
	})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.eng.Stats())
}

// positionView is a position plus its mark and open P&L.
type positionView struct {
	types.Position
	Mark          float64 `json:"mark"`
	UnrealizedPnL float64 `json:"unrealized_pnl"`
}

type positionsResponse struct {
	Equity    float64                 `json:"equity"`
	Cash      float64                 `json:"cash"`
	Positions map[string]positionView `json:"positions"`
}

func (s *Server) handlePositions(w http.ResponseWriter, r *http.Request) {
	v := s.book.View() // one lock: equity, cash, positions and marks agree
	out := make(map[string]positionView, len(v.Positions))
	for sym, pos := range v.Positions {
		pv := positionView{Position: pos}
		if mark, ok := v.Marks[sym]; ok {
			pv.Mark = mark
			pv.UnrealizedPnL = pos.Unrealized(mark)
		}
		out[sym] = pv
	}
	writeJSON(w, http.StatusOK, positionsResponse{Equity: v.Equity, Cash: v.Cash, Positions: out})
}

func (s *Server) handleDecisions(w http.ResponseWriter, r *http.Request) {
	var since *uint64
	if raw := r.URL.Query().Get("since"); raw != "" {
		n, err := strconv.ParseUint(raw, 10, 64)
		if err != nil {
			writeError(w, http.StatusBadRequest, "since must be a non-negative integer")
			return
		}
		since = &n
	}
	writeJSON(w, http.StatusOK, s.eng.Journal(since))
}

type tickRequest struct {
	Symbol string  `json:"symbol"`
	Price  float64 `json:"price"`
	High   float64 `json:"high"`
	Low    float64 `json:"low"`
}

func (s *Server) handleTick(w http.ResponseWriter, r *http.Request) {
	var req tickRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Symbol == "" || req.Price <= 0 {
		writeError(w, http.StatusUnprocessableEntity, "symbol and a positive price are required")
		return
	}
	tick := engine.Tick{
		Symbol: req.Symbol, Price: req.Price,
		High: req.High, Low: req.Low, TS: time.Now().UTC(),
	}

	if wait := r.URL.Query().Get("wait"); wait == "1" || wait == "true" {
		// A tick can trigger a bracket exit, which at a venue is a real close
		// order: wait as long as the slowest decision, never less.
		wait := tickWaitTimeout
		if dt := s.eng.DecisionTimeout(); dt > wait {
			wait = dt
		}
		ctx, cancel := context.WithTimeout(r.Context(), wait)
		defer cancel()
		res, err := s.eng.SubmitTickWait(ctx, tick)
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, res)
		case errors.Is(err, engine.ErrBusy):
			// Never a silent drop on the synchronous path: the caller must
			// know the tick was not applied.
			writeError(w, http.StatusServiceUnavailable, err.Error())
		case errors.Is(err, engine.ErrClosed):
			writeError(w, http.StatusServiceUnavailable, err.Error())
		default:
			writeError(w, http.StatusGatewayTimeout, err.Error())
		}
		return
	}

	if err := s.eng.SubmitTick(tick); err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "accepted"})
}

// decisionRequest is a types.Decision plus bridge fields. Decision itself
// stays the shared domain type; the extras live only on the wire.
type decisionRequest struct {
	types.Decision
	// ClientID makes the request idempotent: a repeat returns the stored
	// outcome and never places a second order.
	ClientID string `json:"client_id,omitempty"`
	// ExpectBootID, when set, must equal this engine's boot_id or the request
	// is refused with 409 before anything is evaluated.
	ExpectBootID string `json:"expect_boot_id,omitempty"`
}

type decisionResponse struct {
	Verdict  string       `json:"verdict"`
	Approved bool         `json:"approved"`
	Reason   string       `json:"reason"`
	Order    *types.Order `json:"order,omitempty"`
	ClientID string       `json:"client_id,omitempty"`
	Fills    []types.Fill `json:"fills"`
	Replayed bool         `json:"replayed,omitempty"`
}

func (s *Server) handleDecision(w http.ResponseWriter, r *http.Request) {
	var req decisionRequest
	if err := decodeJSON(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(req.ClientID) > maxClientIDLen {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("client_id longer than %d bytes", maxClientIDLen))
		return
	}
	if req.ExpectBootID != "" && req.ExpectBootID != s.bootID {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error":   "boot_id mismatch: the engine restarted since you connected",
			"boot_id": s.bootID,
		})
		return
	}
	d := req.Decision
	if d.TS.IsZero() {
		d.TS = time.Now().UTC()
	}

	// The wait must outlast the slowest legitimate decision (a flip at a
	// venue is two venue calls); otherwise a 504 would hide a fill.
	ctx, cancel := context.WithTimeout(r.Context(), s.eng.DecisionTimeout())
	defer cancel()

	out, err := s.eng.SubmitDecisionFull(ctx, d, req.ClientID)
	if errors.Is(err, engine.ErrIDReused) {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	if err != nil {
		if errors.Is(err, engine.ErrClosed) {
			writeError(w, http.StatusServiceUnavailable, "engine is shutting down")
			return
		}
		writeError(w, http.StatusGatewayTimeout, err.Error())
		return
	}

	res := out.Result
	resp := decisionResponse{
		Verdict:  string(res.Verdict),
		Approved: res.Verdict.OK(),
		Reason:   res.Reason,
		ClientID: out.ClientID,
		Fills:    out.Fills,
		Replayed: out.Replayed,
	}
	if resp.Fills == nil {
		resp.Fills = []types.Fill{}
	}
	if res.Verdict.OK() {
		order := res.Order
		resp.Order = &order
	}
	// A rejection is a valid, expected outcome -- not an HTTP error. The gate
	// doing its job must never look like a server fault to the caller.
	writeJSON(w, http.StatusOK, resp)
}

// withLogging records method, path, status, and duration for each request.
func (s *Server) withLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		s.log.Info("request",
			"method", r.Method, "path", r.URL.Path,
			"status", rec.status, "duration", time.Since(started))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// decodeJSON reads a JSON body, rejecting unknown fields so a typo in a
// client payload surfaces immediately instead of silently defaulting.
func decodeJSON(r *http.Request, dst any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
