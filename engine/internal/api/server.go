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
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/engine"
	"github.com/gkhurana21/tradebot/engine/internal/types"
)

// Server routes HTTP requests into the engine.
type Server struct {
	eng   *engine.Engine
	book  *book.Book
	log   *slog.Logger
	start time.Time
}

// NewServer returns a Server bound to eng and b.
func NewServer(eng *engine.Engine, b *book.Book, log *slog.Logger) *Server {
	return &Server{eng: eng, book: b, log: log, start: time.Now()}
}

// Routes returns the HTTP handler for the service.
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /stats", s.handleStats)
	mux.HandleFunc("GET /positions", s.handlePositions)
	mux.HandleFunc("POST /tick", s.handleTick)
	mux.HandleFunc("POST /decision", s.handleDecision)
	return s.withLogging(mux)
}

type healthResponse struct {
	Status string `json:"status"`
	Uptime string `json:"uptime"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, healthResponse{
		Status: "ok",
		Uptime: time.Since(s.start).Round(time.Second).String(),
	})
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.eng.Stats())
}

type positionsResponse struct {
	Equity    float64                   `json:"equity"`
	Cash      float64                   `json:"cash"`
	Positions map[string]types.Position `json:"positions"`
}

func (s *Server) handlePositions(w http.ResponseWriter, r *http.Request) {
	equity, _ := s.book.Snapshot()
	writeJSON(w, http.StatusOK, positionsResponse{
		Equity:    equity,
		Cash:      s.book.Cash(),
		Positions: s.book.Positions(),
	})
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
	if err := s.eng.SubmitTick(engine.Tick{
		Symbol: req.Symbol, Price: req.Price,
		High: req.High, Low: req.Low, TS: time.Now().UTC(),
	}); err != nil {
		writeError(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "accepted"})
}

type decisionResponse struct {
	Verdict  string       `json:"verdict"`
	Approved bool         `json:"approved"`
	Reason   string       `json:"reason"`
	Order    *types.Order `json:"order,omitempty"`
}

func (s *Server) handleDecision(w http.ResponseWriter, r *http.Request) {
	var d types.Decision
	if err := decodeJSON(r, &d); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if d.TS.IsZero() {
		d.TS = time.Now().UTC()
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	res, err := s.eng.SubmitDecision(ctx, d)
	if err != nil {
		if errors.Is(err, engine.ErrClosed) {
			writeError(w, http.StatusServiceUnavailable, "engine is shutting down")
			return
		}
		writeError(w, http.StatusGatewayTimeout, err.Error())
		return
	}

	resp := decisionResponse{
		Verdict:  string(res.Verdict),
		Approved: res.Verdict.OK(),
		Reason:   res.Reason,
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
