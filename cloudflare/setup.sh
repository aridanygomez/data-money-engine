#!/usr/bin/env bash
# ─── LLM Pricing SaaS — Cloudflare Setup ──────────────────────────────────────
# Ejecutar UNA SOLA VEZ para crear la infraestructura en Cloudflare.
# Requisito: npm install -g wrangler && wrangler login

set -e

echo "🚀 Setting up LLM Pricing SaaS on Cloudflare..."

# 1. Crear base de datos D1
echo ""
echo "[1/4] Creating D1 database..."
DB_OUTPUT=$(wrangler d1 create llm-pricing 2>&1)
echo "$DB_OUTPUT"
DB_ID=$(echo "$DB_OUTPUT" | grep 'database_id' | awk '{print $3}' | tr -d '"')

if [ -z "$DB_ID" ]; then
  echo "⚠️  Could not auto-extract database_id. Check output above and update wrangler.toml manually."
else
  echo "✅ D1 created. DB ID: $DB_ID"
  # Actualizar wrangler.toml automáticamente
  sed -i "s/REPLACE_WITH_YOUR_D1_ID/$DB_ID/" wrangler.toml
  echo "✅ wrangler.toml updated with database_id"
fi

# 2. Aplicar schema SQL
echo ""
echo "[2/4] Applying database schema..."
wrangler d1 execute llm-pricing --file=cloudflare/schema.sql
echo "✅ Schema applied"

# 3. Configurar secrets
echo ""
echo "[3/4] Configuring secrets..."
echo ""

echo "Enter INTERNAL_SECRET (random string to authenticate the bot — generate one):"
echo "  Suggestion: $(openssl rand -hex 32)"
read -p "INTERNAL_SECRET: " INTERNAL_SECRET
wrangler secret put INTERNAL_SECRET <<< "$INTERNAL_SECRET"

echo ""
echo "Enter POLAR_WEBHOOK_SECRET (from Polar.sh → Settings → Webhooks):"
read -p "POLAR_WEBHOOK_SECRET: " POLAR_SECRET
wrangler secret put POLAR_WEBHOOK_SECRET <<< "$POLAR_SECRET"

# 4. Deploy worker
echo ""
echo "[4/4] Deploying Cloudflare Worker..."
wrangler deploy
WORKER_URL=$(wrangler deployments list 2>/dev/null | grep 'https://' | head -1 | awk '{print $NF}' || echo "")

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "✅ SETUP COMPLETE"
echo ""
echo "Worker URL: ${WORKER_URL:-Check Cloudflare Dashboard}"
echo ""
echo "📋 NEXT STEPS:"
echo ""
echo "1. Add these GitHub Action Secrets (repo → Settings → Secrets):"
echo "   CF_WORKER_URL = ${WORKER_URL:-<your-worker>.workers.dev}"
echo "   CF_INTERNAL_SECRET = $INTERNAL_SECRET"
echo ""
echo "2. Set up Polar.sh:"
echo "   → Create products: 'Starter' (\$9/mo) and 'Pro' (\$29/mo)"
echo "   → Webhook URL: ${WORKER_URL:-<your-worker>.workers.dev}/webhooks/polar"
echo "   → Add POLAR_WEBHOOK_SECRET to wrangler secrets (done above)"
echo ""
echo "3. Test the API:"
echo "   curl ${WORKER_URL:-<your-worker>.workers.dev}/api/v1/cheapest?n=5"
echo ""
echo "4. Test sync (with real secret):"
echo "   curl -X POST ${WORKER_URL:-<your-worker>.workers.dev}/internal/sync \\"
echo "     -H 'X-Internal-Secret: $INTERNAL_SECRET' \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"models\": [], \"date\": \"2026-02-27\"}'"
echo "═══════════════════════════════════════════════════════════════"
