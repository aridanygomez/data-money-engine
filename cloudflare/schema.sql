-- ─── LLM Pricing SaaS — D1 Schema ────────────────────────────────────────────
-- Run once: wrangler d1 execute llm-pricing --file=cloudflare/schema.sql

-- Modelos con precios actuales
CREATE TABLE IF NOT EXISTS models (
    id                      TEXT PRIMARY KEY,          -- "openai/gpt-4o"
    slug                    TEXT NOT NULL,             -- "openai-gpt-4o"
    name                    TEXT NOT NULL,
    provider                TEXT NOT NULL,
    context_length          INTEGER DEFAULT 0,
    prompt_price_per_1m     REAL DEFAULT 0,
    completion_price_per_1m REAL DEFAULT 0,
    total_price_per_1m      REAL DEFAULT 0,
    is_free                 INTEGER DEFAULT 0,         -- 0/1 bool
    openrouter_url          TEXT,
    updated_at              TEXT NOT NULL
);

-- Historial de precios (1 fila por modelo por día)
CREATE TABLE IF NOT EXISTS price_history (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id                TEXT NOT NULL,
    prompt_price_per_1m     REAL DEFAULT 0,
    completion_price_per_1m REAL DEFAULT 0,
    recorded_at             TEXT NOT NULL,             -- "2026-02-27"
    UNIQUE(model_id, recorded_at)
);

-- Suscriptores (gestionados por Polar.sh)
CREATE TABLE IF NOT EXISTS subscribers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    polar_customer_id       TEXT UNIQUE NOT NULL,
    email                   TEXT NOT NULL,
    plan                    TEXT NOT NULL,             -- "starter" | "pro"
    api_key                 TEXT UNIQUE NOT NULL,      -- SHA256 random hex
    webhook_url             TEXT,                     -- Slack/Discord incoming webhook
    webhook_platform        TEXT,                     -- "slack" | "discord"
    alert_threshold_pct     REAL DEFAULT 10.0,        -- % change para alertar
    active                  INTEGER DEFAULT 1,
    created_at              TEXT NOT NULL,
    expires_at              TEXT
);

-- Alertas enviadas (deduplicación — 1 alerta por modelo por día por subscriber)
CREATE TABLE IF NOT EXISTS alerts_sent (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id           INTEGER NOT NULL,
    model_id                TEXT NOT NULL,
    old_price               REAL,
    new_price               REAL,
    change_pct              REAL,
    sent_at                 TEXT NOT NULL,
    UNIQUE(subscriber_id, model_id, sent_at)
);

-- Rate limiting para tier free (KV sería mejor pero D1 funciona para MVP)
CREATE TABLE IF NOT EXISTS rate_limits (
    api_key                 TEXT NOT NULL,
    date                    TEXT NOT NULL,
    count                   INTEGER DEFAULT 0,
    PRIMARY KEY (api_key, date)
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider);
CREATE INDEX IF NOT EXISTS idx_models_price ON models(prompt_price_per_1m);
CREATE INDEX IF NOT EXISTS idx_history_model ON price_history(model_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_alerts_subscriber ON alerts_sent(subscriber_id, sent_at);
