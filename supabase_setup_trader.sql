-- ============================================================
-- Alpaca Paper Trader — Supabase Schema
-- ============================================================
-- Run this in the Supabase SQL Editor:
--   https://app.supabase.com/project/YOUR_PROJECT/_/sql
--
-- Replace 'admin' with your own owner string if needed.
-- All three tables share the same owner field so RLS policies
-- can be scoped per-user in a multi-user setup.
-- ============================================================

-- ── 1. trader_trades ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.trader_trades (
    id          BIGSERIAL PRIMARY KEY,
    owner       TEXT    NOT NULL DEFAULT 'admin',
    -- Mirrors logs/trades.jsonl fields
    timestamp   TIMESTAMPTZ,
    action      TEXT,   -- 'open' | 'close' | 'error'
    strategy    TEXT,   -- 'wheel_options' | 'stock_swing' | 'credit_spread' | etc.
    symbol      TEXT,
    qty         INTEGER,
    price       NUMERIC,
    side        TEXT,   -- 'buy' | 'sell'
    pnl         NUMERIC,
    reason      TEXT,
    hold_duration NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.trader_trades IS
    'Trade event log — written by trader sync, read by dashboard.';

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_trades_owner       ON public.trader_trades(owner);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp   ON public.trader_trades(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy    ON public.trader_trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_action      ON public.trader_trades(action);

-- RLS
ALTER TABLE public.trader_trades ENABLE ROW LEVEL SECURITY;

-- Owner sees only their own rows (simple single-user: owner='admin')
CREATE POLICY "admin_read_trades" ON public.trader_trades
    FOR SELECT USING (owner = current_user);

CREATE POLICY "admin_insert_trades" ON public.trader_trades
    FOR INSERT WITH CHECK (true);

-- Upsert: update if same owner+timestamp+symbol+action, else insert
CREATE POLICY "admin_upsert_trades" ON public.trader_trades
    FOR INSERT WITH CHECK (owner = current_user);

-- ── 2. trader_status ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.trader_status (
    id          BIGSERIAL PRIMARY KEY,
    owner       TEXT    NOT NULL DEFAULT 'admin',
    -- Mirrors data/status.json fields
    last_run    TIMESTAMPTZ,
    status      TEXT,   -- 'ok' | 'error'
    duration_sec NUMERIC,
    extra       JSONB   DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.trader_status IS
    'Trader run status — written by trader sync, read by dashboard.';

CREATE INDEX IF NOT EXISTS idx_status_owner   ON public.trader_status(owner);
CREATE INDEX IF NOT EXISTS idx_status_lastrun ON public.trader_status(last_run DESC);

ALTER TABLE public.trader_status ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admin_read_status" ON public.trader_status
    FOR SELECT USING (owner = current_user);

CREATE POLICY "admin_insert_status" ON public.trader_status
    FOR INSERT WITH CHECK (true);

CREATE POLICY "admin_update_status" ON public.trader_status
    FOR UPDATE USING (owner = current_user);

-- ── 3. trader_config ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.trader_config (
    id          BIGSERIAL PRIMARY KEY,
    owner       TEXT    NOT NULL DEFAULT 'admin',
    -- Stores config/strategy.yaml as JSONB
    config_json JSONB   NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.trader_config IS
    'Strategy YAML config snapshot — written by trader sync, read by dashboard.';

CREATE INDEX IF NOT EXISTS idx_config_owner ON public.trader_config(owner);

ALTER TABLE public.trader_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admin_read_config" ON public.trader_config
    FOR SELECT USING (owner = current_user);

CREATE POLICY "admin_insert_config" ON public.trader_config
    FOR INSERT WITH CHECK (true);

CREATE POLICY "admin_update_config" ON public.trader_config
    FOR UPDATE USING (owner = current_user);

-- ── Function: upsert_trade ──────────────────────────────────
-- Lightweight upsert to avoid duplicate log lines.
-- Uses timestamp + symbol + action + strategy as unique key.
CREATE OR REPLACE FUNCTION public.upsert_trade(p JSONB)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    row_id BIGINT;
BEGIN
    INSERT INTO public.trader_trades
        (owner, timestamp, action, strategy, symbol, qty, price, side, pnl, reason, hold_duration)
    VALUES (
        p->>'owner',
        (p->>'timestamp')::TIMESTAMPTZ,
        p->>'action',
        p->>'strategy',
        p->>'symbol',
        (p->>'qty')::INTEGER,
        (p->>'price')::NUMERIC,
        p->>'side',
        (p->>'pnl')::NUMERIC,
        p->>'reason',
        (p->>'hold_duration')::NUMERIC
    )
    ON CONFLICT DO NOTHING
    RETURNING id INTO row_id;
    RETURN row_id;
END;
$$;

-- ── Function: upsert_status ─────────────────────────────────
CREATE OR REPLACE FUNCTION public.upsert_status(p JSONB)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    row_id BIGINT;
BEGIN
    INSERT INTO public.trader_status
        (owner, last_run, status, duration_sec, extra)
    VALUES (
        p->>'owner',
        (p->>'last_run')::TIMESTAMPTZ,
        p->>'status',
        (p->>'duration_sec')::NUMERIC,
        COALESCE((p->>'extra')::JSONB, '{}')
    )
    ON CONFLICT DO NOTHING
    RETURNING id INTO row_id;
    RETURN row_id;
END;
$$;

-- ── Function: upsert_config ─────────────────────────────────
-- Upserts full config JSON for this owner (one active config row per owner).
CREATE OR REPLACE FUNCTION public.upsert_config(p JSONB)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    row_id BIGINT;
    cfg JSONB := p->>'config_json';
BEGIN
    UPDATE public.trader_config
       SET config_json = cfg,
           updated_at  = NOW()
     WHERE owner = (p->>'owner')
    RETURNING id INTO row_id;

    IF row_id IS NULL THEN
        INSERT INTO public.trader_config (owner, config_json)
        VALUES (p->>'owner', cfg)
        RETURNING id INTO row_id;
    END IF;

    RETURN row_id;
END;
$$;

-- ============================================================
-- NOTES for multi-user extension (future):
--   - Add a 'users' table with auth.uid() mapping to owner
--   - Change current_user to a subquery lookup
--   - For anon key access, enable anon on the tables above
-- ============================================================
