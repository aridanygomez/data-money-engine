-- ═══════════════════════════════════════════════════════════════════════════
--  D1 Schema v2 — Smart AI Proxy Gateway
--  Aplicar con:
--    wrangler d1 execute llm-pricing --file=cloudflare/schema_v2.sql --remote
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── 1. Tabla de logs de uso del proxy ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS usage_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  subscriber_id     INTEGER NOT NULL,
  model_id          TEXT    NOT NULL,
  provider          TEXT    NOT NULL DEFAULT '',
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  cost_real_usd     REAL    NOT NULL DEFAULT 0,
  cost_billed_usd   REAL    NOT NULL DEFAULT 0,
  latency_ms        INTEGER,
  month             TEXT    NOT NULL,        -- "2026-02" para GROUP BY rápido
  status            TEXT    NOT NULL DEFAULT 'ok',
  plan              TEXT    NOT NULL DEFAULT 'starter',
  created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_sub_month
  ON usage_logs(subscriber_id, month);

CREATE INDEX IF NOT EXISTS idx_usage_model_created
  ON usage_logs(model_id, created_at);

CREATE INDEX IF NOT EXISTS idx_usage_latency
  ON usage_logs(model_id, latency_ms) WHERE latency_ms IS NOT NULL;

-- ─── 2. Tabla de reinversión (trazabilidad 20%) ─────────────────────────────
CREATE TABLE IF NOT EXISTS reinvestment_log (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  amount_received_usd   REAL NOT NULL,
  credits_added_usd     REAL NOT NULL,
  tokens_added          INTEGER NOT NULL,
  source                TEXT NOT NULL DEFAULT 'stripe',
  created_at            TEXT NOT NULL
);

-- ─── 3. Configuración global (key-value) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS global_config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Inicializar pool de tokens gratuito (se va acumulando con reinversión)
INSERT OR IGNORE INTO global_config (key, value) VALUES ('free_pool_tokens', '0');
INSERT OR IGNORE INTO global_config (key, value) VALUES ('schema_version', '2');

-- ─── 4. Extender tabla subscribers ─────────────────────────────────────────
-- SQLite soporta ADD COLUMN (no DROP/MODIFY), así que añadimos:
ALTER TABLE subscribers ADD COLUMN tokens_used_month INTEGER DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN tokens_quota      INTEGER DEFAULT 100000;
ALTER TABLE subscribers ADD COLUMN credits_balance   INTEGER DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN stripe_price_id   TEXT;
ALTER TABLE subscribers ADD COLUMN stripe_sub_id     TEXT;

-- ─── 5. Índices adicionales en subscribers ────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_subscribers_api_key
  ON subscribers(api_key) WHERE api_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_subscribers_email
  ON subscribers(email) WHERE email IS NOT NULL;

-- ─── 6. Actualizar cuotas según plan (si ya hay filas) ────────────────────
UPDATE subscribers SET tokens_quota = 100000  WHERE plan = 'free';
UPDATE subscribers SET tokens_quota = 2000000 WHERE plan = 'starter';
UPDATE subscribers SET tokens_quota = 20000000 WHERE plan = 'pro';

-- ─── 7. Vista útil: uso del mes actual por subscriber ────────────────────
-- (SQLite no soporta MATERIALIZED VIEW, pero la vista es útil para debug)
CREATE VIEW IF NOT EXISTS v_monthly_usage AS
SELECT
  s.id            AS subscriber_id,
  s.email,
  s.plan,
  strftime('%Y-%m', 'now') AS month,
  COALESCE(SUM(u.total_tokens), 0) AS tokens_used,
  s.tokens_quota,
  ROUND(COALESCE(SUM(u.total_tokens), 0) * 100.0 / s.tokens_quota, 1) AS pct_used,
  COALESCE(SUM(u.cost_billed_usd), 0) AS revenue_usd
FROM subscribers s
LEFT JOIN usage_logs u
  ON u.subscriber_id = s.id
  AND u.month = strftime('%Y-%m', 'now')
WHERE s.active = 1
GROUP BY s.id;
