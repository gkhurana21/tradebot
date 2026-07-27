// Command engine runs the tradebot execution service.
//
// It owns the position book and the risk gate, accepts market data and trade
// decisions over HTTP, and streams every fill, exit, and rejection as
// structured JSON logs.
//
//	go run ./cmd/engine -addr :8080 -profile balanced -cash 10000
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gkhurana21/tradebot/engine/internal/api"
	"github.com/gkhurana21/tradebot/engine/internal/book"
	"github.com/gkhurana21/tradebot/engine/internal/engine"
	"github.com/gkhurana21/tradebot/engine/internal/risk"
)

func main() {
	var (
		addr      = flag.String("addr", ":8080", "HTTP listen address")
		profile   = flag.String("profile", "balanced", "risk profile: conservative | balanced | aggressive")
		cash      = flag.Float64("cash", 10_000, "starting cash")
		feePct    = flag.Float64("fee", 0.001, "taker fee as a fraction of notional")
		slipPct   = flag.Float64("slippage", 0.0005, "modeled adverse slippage")
		logFormat = flag.String("log", "text", "log format: text | json")
	)
	flag.Parse()

	log := newLogger(*logFormat)

	cfg, ok := risk.Profile(*profile)
	if !ok {
		// Fail loudly: silently trading a posture the operator did not choose
		// is exactly the class of mistake this service exists to prevent.
		log.Error("unknown risk profile", "profile", *profile,
			"valid", "conservative, balanced, aggressive")
		os.Exit(2)
	}

	b := book.New(book.Options{
		StartingCash: *cash,
		FeePct:       *feePct,
		SlippagePct:  *slipPct,
	})
	eng := engine.New(b, risk.New(cfg))

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	eng.Start(ctx)
	go logEvents(eng, log)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(eng, b, log).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	serveErr := make(chan error, 1)
	go func() {
		log.Info("execution engine listening",
			"addr", *addr, "profile", *profile, "cash", *cash,
			"max_position_pct", cfg.MaxPositionPct,
			"max_daily_drawdown_pct", cfg.MaxDailyDrawdownPct)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			serveErr <- err
		}
	}()

	select {
	case err := <-serveErr:
		log.Error("server failed", "err", err)
		eng.Stop()
		os.Exit(1)
	case <-ctx.Done():
		log.Info("shutdown signal received, draining")
	}

	// Stop accepting new work, then let in-flight requests finish before the
	// engine drains -- a trade half-through the gate must not be abandoned.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error("graceful shutdown failed", "err", err)
	}
	eng.Stop()

	stats := eng.Stats()
	log.Info("stopped",
		"ticks", stats.Ticks, "decisions", stats.Decisions,
		"fills", stats.Fills, "rejected", stats.Rejected,
		"exits", stats.Exits, "final_equity", stats.Equity)
}

// logEvents drains the engine event stream to structured logs. It exits when
// the engine closes the channel.
func logEvents(eng *engine.Engine, log *slog.Logger) {
	for ev := range eng.Events() {
		attrs := []any{"event", string(ev.Kind), "equity", round2(ev.Equity)}
		if ev.Symbol != "" {
			attrs = append(attrs, "symbol", ev.Symbol)
		}
		if ev.Side != "" {
			attrs = append(attrs, "side", string(ev.Side))
		}
		if ev.Qty != 0 {
			attrs = append(attrs, "qty", ev.Qty, "price", round2(ev.Price))
		}
		if ev.Verdict != "" {
			attrs = append(attrs, "verdict", string(ev.Verdict))
		}
		if ev.Reason != "" {
			attrs = append(attrs, "reason", ev.Reason)
		}
		switch ev.Kind {
		case engine.EventHalt:
			log.Warn("trading halted", attrs...)
		case engine.EventRejected:
			log.Debug("decision rejected", attrs...)
		default:
			log.Info("engine event", attrs...)
		}
	}
}

func newLogger(format string) *slog.Logger {
	opts := &slog.HandlerOptions{Level: slog.LevelInfo}
	if format == "json" {
		return slog.New(slog.NewJSONHandler(os.Stdout, opts))
	}
	return slog.New(slog.NewTextHandler(os.Stdout, opts))
}

func round2(v float64) float64 { return float64(int64(v*100+0.5)) / 100 }
