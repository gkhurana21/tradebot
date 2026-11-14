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
	"fmt"
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
	"github.com/gkhurana21/tradebot/engine/internal/venue"
)

// defaultAddr binds loopback only: the engine has no authentication, so it must
// not be reachable from the network unless the operator opts in with -addr.
const defaultAddr = "127.0.0.1:8080"

func main() {
	var (
		addr      = flag.String("addr", defaultAddr, "HTTP listen address (loopback by default; pass :8080 to expose)")
		profile   = flag.String("profile", "balanced", "risk profile: conservative | balanced | aggressive")
		cash      = flag.Float64("cash", 10_000, "starting cash (simulated venue only)")
		feePct    = flag.Float64("fee", 0.001, "taker fee as a fraction of notional")
		slipPct   = flag.Float64("slippage", 0.0005, "modeled adverse slippage")
		logFormat = flag.String("log", "text", "log format: text | json")
		venueName = flag.String("venue", "sim",
			"execution venue: sim | alpaca-paper | alpaca-live")
		confirmLive = flag.Bool("i-understand-this-trades-real-money", false,
			"required acknowledgement for -venue alpaca-live")
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

	v, err := resolveVenue(*venueName, *confirmLive)
	if err != nil {
		log.Error("venue setup failed", "venue", *venueName, "err", err)
		os.Exit(2)
	}

	opts := []engine.Option{}
	if v != nil {
		opts = append(opts, engine.WithVenue(v))
	}
	eng := engine.New(b, risk.New(cfg), opts...)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Adopt the venue's account state before trading. The venue knows about
	// fills from other sessions and anything that happened while this process
	// was down; starting from a stale local guess would size the first orders
	// against a book that does not exist.
	if v != nil {
		if err := reconcile(ctx, v, b, log); err != nil {
			log.Error("could not reconcile with the venue; refusing to trade blind",
				"venue", v.Name(), "err", err)
			os.Exit(1)
		}
	}

	eng.Start(ctx)
	go logEvents(eng, log)

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(eng, b, log).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      eng.DecisionTimeout() + 5*time.Second, // must outlast the slowest decision
		IdleTimeout:       60 * time.Second,
	}

	serveErr := make(chan error, 1)
	go func() {
		venueLabel, live := "sim", false
		if v != nil {
			venueLabel, live = v.Name(), v.IsLive()
		}
		if live {
			// The single most important line in the log. An operator must
			// never have to infer that real money is at stake.
			log.Warn("!!! LIVE TRADING ENABLED — orders will use real money !!!",
				"venue", venueLabel)
		}
		log.Info("execution engine listening",
			"addr", *addr, "profile", *profile, "venue", venueLabel, "live", live,
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

// resolveVenue turns the -venue flag into a client. A nil venue means
// simulated execution.
//
// Live trading requires naming it explicitly *and* passing a second
// acknowledgement flag. Two independent actions are needed because a single
// mistyped flag should never be the difference between paper and real money.
func resolveVenue(name string, confirmLive bool) (venue.Venue, error) {
	switch name {
	case "", "sim", "simulated":
		return nil, nil

	case "alpaca-paper":
		return venue.NewAlpacaPaper()

	case "alpaca-live":
		if !confirmLive {
			return nil, errors.New(
				"live trading requires -i-understand-this-trades-real-money; " +
					"use -venue alpaca-paper to trade the same code with no money at risk")
		}
		return venue.NewAlpacaLive()

	default:
		return nil, fmt.Errorf("unknown venue %q (want sim, alpaca-paper, or alpaca-live)", name)
	}
}

// reconcile adopts the venue's account and positions as the starting truth.
func reconcile(ctx context.Context, v venue.Venue, b *book.Book, log *slog.Logger) error {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	acct, err := v.Account(ctx)
	if err != nil {
		return fmt.Errorf("fetching account: %w", err)
	}
	positions, err := v.Positions(ctx)
	if err != nil {
		return fmt.Errorf("fetching positions: %w", err)
	}

	b.Reconcile(positions, acct.Cash)
	log.Info("reconciled with venue",
		"venue", v.Name(), "live", v.IsLive(),
		"equity", acct.Equity, "cash", acct.Cash,
		"buying_power", acct.BuyingPower, "open_positions", len(positions))
	for symbol, pos := range positions {
		log.Info("adopted existing position",
			"symbol", symbol, "qty", pos.Qty, "avg_price", pos.AvgPrice)
	}
	return nil
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
