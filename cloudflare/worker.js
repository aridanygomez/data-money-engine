/**
 * ═══════════════════════════════════════════════════════════════════
 *  Smart AI Proxy Gateway  ·  LLM Pricing SaaS
 *  Cloudflare Worker
 * ═══════════════════════════════════════════════════════════════════
 *
 *  PROXY (OpenAI-compatible):
 *    POST /v1/chat/completions   → proxy al modelo más barato o elegido
 *    GET  /v1/models             → lista de modelos disponibles
 *
 *  PRICING API:
 *    GET  /api/v1/models         → todos los modelos con precios
 *    GET  /api/v1/cheapest?n=10  → N más baratos
 *    GET  /api/v1/history/:id    → historial de precios (Starter+)
 *    GET  /api/v1/usage          → uso del mes (autenticado)
 *    GET  /api/v1/latency        → latencia promedio por modelo
 *    GET  /api/v1/keys/me        → info de mi API key
 *
 *  ALERTAS:
 *    POST /api/v1/alerts/register
 *
 *  INTERNOS (X-Internal-Secret):
 *    POST /internal/sync         → bot sube precios frescos
 *    POST /internal/reinvest     → acredita 20% de ingresos Stripe al pool libre
 *
 *  WEBHOOKS:
 *    POST /webhooks/stripe
 *    POST /webhooks/polar
 *
 *  ENV:
 *    OPENROUTER_API_KEY  — clave real (nunca expuesta al cliente)
 *    INTERNAL_SECRET, STRIPE_WEBHOOK_SECRET, POLAR_WEBHOOK_SECRET
 *    DB  — D1 binding
 */

// ─── Planes ──────────────────────────────────────────────────────────────────

const PLANS = {
  free:    { monthly_tokens: 100_000,    markup: 0,    history_days: 0,  alerts: false },
  starter: { monthly_tokens: 2_000_000,  markup: 0.30, history_days: 30, alerts: true  },
  pro:     { monthly_tokens: 20_000_000, markup: 0.20, history_days: 90, alerts: true  },
};

const PRICING_URL        = 'https://aridanygomez.github.io/data-money-engine/pricing.html';
const SITE_URL           = 'https://aridanygomez.github.io/data-money-engine';
const FREE_PROXY_MONTHLY = 100_000;
const REINVESTMENT_PCT   = 0.20;

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-API-Key, Authorization',
};

const FREE_DAILY_LIMIT = 100;
const FREE_MAX_MODELS  = 20;

// ─── Router ──────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    if (method === 'OPTIONS') return new Response(null, { headers: CORS_HEADERS });

    try {
      // ── OpenAI-Compatible Proxy ───────────────────────────────────────────
      if (path === '/v1/chat/completions' && method === 'POST')
        return await handleChatCompletions(request, url, env, ctx);
      if (path === '/v1/models' && method === 'GET')
        return await handleGatewayModels(env);

      // ── Internal (bot → D1 sync) ──────────────────────────────────────────
      if (path === '/internal/sync' && method === 'POST')
        return await handleInternalSync(request, env);
      if (path === '/internal/reinvest' && method === 'POST')
        return await handleReinvestment(request, env);

      // ── Polar.sh webhooks ────────────────────────────────────────────────
      if (path === '/webhooks/polar' && method === 'POST')
        return await handlePolarWebhook(request, env);

      // ── Stripe webhooks ──────────────────────────────────────────────────
      if (path === '/webhooks/stripe' && method === 'POST')
        return await handleStripeWebhook(request, env);

      // ── Public API ────────────────────────────────────────────────────────
      if (path.startsWith('/api/v1/'))
        return await handleAPI(request, url, path, env);

      return json({ error: 'Not found' }, 404);
    } catch (e) {
      console.error(e.stack || e.message);
      return json({ error: 'Internal server error', detail: e.message }, 500);
    }
  },
};

// ─── API Handler ─────────────────────────────────────────────────────────────

async function handleAPI(request, url, path, env) {
  const method = request.method;

  // Identificar subscriber (puede ser anónimo)
  const apiKey = request.headers.get('X-API-Key') || 'anonymous';
  const subscriber = apiKey !== 'anonymous' ? await getSubscriber(env, apiKey) : null;

  // Rate limit para anonymous/free
  if (!subscriber) {
    const limited = await checkRateLimit(env, 'anon:' + getIP(request), FREE_DAILY_LIMIT);
    if (limited) return json({ error: 'Rate limit exceeded. Get an API key at llm-pricing.dev' }, 429);
  }

  // GET /api/v1/models
  if (path === '/api/v1/models' && method === 'GET') {
    const limit = subscriber?.plan === 'pro' ? 9999 : FREE_MAX_MODELS;
    const provider = url.searchParams.get('provider');
    const freeOnly = url.searchParams.get('free') === '1';

    let query = 'SELECT * FROM models WHERE 1=1';
    const params = [];
    if (provider) { query += ' AND provider = ?'; params.push(provider); }
    if (freeOnly) { query += ' AND is_free = 1'; }
    query += ' ORDER BY prompt_price_per_1m ASC LIMIT ?';
    params.push(limit);

    const { results } = await env.DB.prepare(query).bind(...params).all();
    return json({ models: results, count: results.length, limit_applied: !subscriber });
  }

  // GET /api/v1/cheapest
  if (path === '/api/v1/cheapest' && method === 'GET') {
    const n = Math.min(parseInt(url.searchParams.get('n') || '10'), subscriber ? 50 : 5);
    const { results } = await env.DB.prepare(
      'SELECT id, name, provider, prompt_price_per_1m, completion_price_per_1m, context_length FROM models WHERE is_free = 0 AND prompt_price_per_1m > 0 ORDER BY prompt_price_per_1m ASC LIMIT ?'
    ).bind(n).all();
    return json({ cheapest: results, as_of: new Date().toISOString().slice(0, 10) });
  }

  // GET /api/v1/models/:id
  const modelMatch = path.match(/^\/api\/v1\/models\/(.+)$/);
  if (modelMatch && method === 'GET') {
    const modelId = decodeURIComponent(modelMatch[1]);
    const model = await env.DB.prepare('SELECT * FROM models WHERE id = ? OR slug = ?').bind(modelId, modelId).first();
    if (!model) return json({ error: 'Model not found' }, 404);
    return json({ model });
  }

  // GET /api/v1/history/:id  →  solo Pro
  const histMatch = path.match(/^\/api\/v1\/history\/(.+)$/);
  if (histMatch && method === 'GET') {
    if (!subscriber || subscriber.plan !== 'pro') {
      return json({ error: 'Pro plan required. Upgrade at llm-pricing.dev/pricing' }, 403);
    }
    const modelId = decodeURIComponent(histMatch[1]);
    const days = Math.min(parseInt(url.searchParams.get('days') || '30'), 90);
    const { results } = await env.DB.prepare(
      'SELECT recorded_at, prompt_price_per_1m, completion_price_per_1m FROM price_history WHERE model_id = ? ORDER BY recorded_at DESC LIMIT ?'
    ).bind(modelId, days).all();
    return json({ model_id: modelId, history: results });
  }

  // POST /api/v1/alerts/register  →  Starter+
  if (path === '/api/v1/alerts/register' && method === 'POST') {
    if (!subscriber) return json({ error: 'API key required. Get one at llm-pricing.dev/pricing' }, 401);

    const body = await request.json();
    const { webhook_url, platform, threshold_pct } = body;

    if (!webhook_url || !platform) return json({ error: 'webhook_url and platform required' }, 400);
    if (!['slack', 'discord'].includes(platform)) return json({ error: 'platform must be slack or discord' }, 400);

    await env.DB.prepare(
      'UPDATE subscribers SET webhook_url = ?, webhook_platform = ?, alert_threshold_pct = ? WHERE api_key = ?'
    ).bind(webhook_url, platform, threshold_pct || 10.0, apiKey).run();

    return json({ success: true, message: `Alerts configured for ${platform}. You'll be notified when any model price changes ≥${threshold_pct || 10}%` });
  }

  // GET /api/v1/usage  →  uso del mes actual
  if (path === '/api/v1/usage' && method === 'GET') {
    if (!subscriber) return json({ error: 'API key required' }, 401);
    const month = url.searchParams.get('month') || new Date().toISOString().slice(0, 7);
    const { results } = await env.DB.prepare(
      `SELECT model_id, provider,
         SUM(prompt_tokens) as prompt_tokens,
         SUM(completion_tokens) as completion_tokens,
         SUM(total_tokens) as total_tokens,
         SUM(cost_billed_usd) as cost_billed_usd,
         COUNT(*) as requests
       FROM usage_logs WHERE subscriber_id = ? AND month = ?
       GROUP BY model_id, provider ORDER BY total_tokens DESC`
    ).bind(subscriber.id, month).all();
    const totals = results.reduce(
      (a, r) => ({ total_tokens: a.total_tokens + r.total_tokens, cost_billed_usd: a.cost_billed_usd + r.cost_billed_usd, requests: a.requests + r.requests }),
      { total_tokens: 0, cost_billed_usd: 0, requests: 0 }
    );
    const plan   = subscriber.plan || 'starter';
    const quota  = PLANS[plan]?.monthly_tokens ?? FREE_PROXY_MONTHLY;
    return json({
      month, plan, quota_tokens: quota,
      used_tokens: totals.total_tokens,
      pct_used: Math.round(totals.total_tokens / quota * 100),
      cost_billed_usd: +totals.cost_billed_usd.toFixed(4),
      requests: totals.requests,
      by_model: results,
    });
  }

  // GET /api/v1/latency  →  latencia promedio de los últimos N horas
  if (path === '/api/v1/latency' && method === 'GET') {
    const hours = Math.min(parseInt(url.searchParams.get('hours') || '24'), 168);
    const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
    const { results } = await env.DB.prepare(
      `SELECT model_id, provider,
         AVG(latency_ms) as avg_latency_ms,
         MIN(latency_ms) as min_latency_ms,
         MAX(latency_ms) as max_latency_ms,
         COUNT(*) as samples
       FROM usage_logs
       WHERE created_at > ? AND latency_ms IS NOT NULL AND latency_ms > 0
       GROUP BY model_id, provider HAVING samples >= 3
       ORDER BY avg_latency_ms ASC`
    ).bind(since).all();
    return json({ hours, since, latency: results, generated_at: new Date().toISOString() });
  }

  // GET /api/v1/keys/me
  if (path === '/api/v1/keys/me' && method === 'GET') {
    if (!subscriber) return json({ error: 'API key required' }, 401);
    const plan = subscriber.plan || 'starter';
    return json({
      plan, active: subscriber.active === 1,
      expires_at:       subscriber.expires_at,
      api_key_prefix:   (subscriber.api_key || '').slice(0, 12) + '...',
      monthly_tokens:   PLANS[plan]?.monthly_tokens ?? FREE_PROXY_MONTHLY,
      gateway_endpoint: 'https://llm-pricing-api.aridany-91.workers.dev/v1',
    });
  }

  return json({ error: 'Not found' }, 404);
}

// ─── Internal Sync (bot → D1) ─────────────────────────────────────────────────

async function handleInternalSync(request, env) {
  // Verificar secret
  const secret = request.headers.get('X-Internal-Secret');
  if (secret !== env.INTERNAL_SECRET) {
    return json({ error: 'Unauthorized' }, 401);
  }

  const { models, date } = await request.json();
  if (!models?.length) return json({ error: 'No models provided' }, 400);

  // Detectar cambios ANTES de actualizar
  const priceChanges = await detectPriceChanges(env, models);

  // Upsert modelos en batch (250 por vez para respetar límites D1)
  const BATCH = 250;
  for (let i = 0; i < models.length; i += BATCH) {
    const batch = models.slice(i, i + BATCH);
    const stmt = env.DB.prepare(`
      INSERT INTO models (id, slug, name, provider, context_length, prompt_price_per_1m, completion_price_per_1m, total_price_per_1m, is_free, openrouter_url, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET
        slug = excluded.slug,
        name = excluded.name,
        provider = excluded.provider,
        context_length = excluded.context_length,
        prompt_price_per_1m = excluded.prompt_price_per_1m,
        completion_price_per_1m = excluded.completion_price_per_1m,
        total_price_per_1m = excluded.total_price_per_1m,
        is_free = excluded.is_free,
        updated_at = excluded.updated_at
    `);

    await env.DB.batch(batch.map(m => stmt.bind(
      m.id, m.slug, m.name, m.provider,
      m.context_length || 0,
      m.prompt_price_per_1m || 0,
      m.completion_price_per_1m || 0,
      m.total_price_per_1m || 0,
      m.is_free ? 1 : 0,
      m.openrouter_url || '',
      date
    )));
  }

  // Guardar historial del día (INSERT OR IGNORE para no duplicar)
  const histStmt = env.DB.prepare(`
    INSERT OR IGNORE INTO price_history (model_id, prompt_price_per_1m, completion_price_per_1m, recorded_at)
    VALUES (?, ?, ?, ?)
  `);
  for (let i = 0; i < models.length; i += BATCH) {
    const batch = models.slice(i, i + BATCH);
    await env.DB.batch(batch.map(m => histStmt.bind(
      m.id, m.prompt_price_per_1m || 0, m.completion_price_per_1m || 0, date
    )));
  }

  // Disparar alertas si hay cambios
  let alertsFired = 0;
  if (priceChanges.length > 0) {
    alertsFired = await dispatchAlerts(env, priceChanges, date);
  }

  return json({
    success: true,
    models_synced: models.length,
    price_changes: priceChanges.length,
    alerts_fired: alertsFired,
    date,
  });
}

// ─── Price Change Detection ───────────────────────────────────────────────────

async function detectPriceChanges(env, newModels) {
  const changes = [];
  for (const m of newModels) {
    const old = await env.DB.prepare(
      'SELECT prompt_price_per_1m FROM models WHERE id = ?'
    ).bind(m.id).first();

    if (!old) continue;
    const oldPrice = old.prompt_price_per_1m;
    const newPrice = m.prompt_price_per_1m || 0;

    if (oldPrice <= 0 || newPrice <= 0) continue;
    const changePct = Math.abs((newPrice - oldPrice) / oldPrice) * 100;
    if (changePct < 0.5) continue; // ignorar micro-ruido

    changes.push({
      model_id: m.id,
      model_name: m.name,
      provider: m.provider,
      old_price: oldPrice,
      new_price: newPrice,
      change_pct: Math.round(changePct * 10) / 10,
      direction: newPrice < oldPrice ? 'DOWN' : 'UP',
    });
  }
  return changes;
}

// ─── Alert Dispatch (Slack / Discord) ────────────────────────────────────────

async function dispatchAlerts(env, priceChanges, date) {
  const { results: subscribers } = await env.DB.prepare(
    'SELECT * FROM subscribers WHERE active = 1 AND webhook_url IS NOT NULL'
  ).all();

  let fired = 0;

  for (const sub of subscribers) {
    const threshold = sub.alert_threshold_pct || 10;
    const relevantChanges = priceChanges.filter(c => c.change_pct >= threshold);
    if (!relevantChanges.length) continue;

    // Deduplicar: no enviar si ya se alertó hoy
    const toSend = [];
    for (const change of relevantChanges) {
      const alreadySent = await env.DB.prepare(
        'SELECT id FROM alerts_sent WHERE subscriber_id = ? AND model_id = ? AND sent_at = ?'
      ).bind(sub.id, change.model_id, date).first();
      if (!alreadySent) toSend.push(change);
    }
    if (!toSend.length) continue;

    // Construir mensaje
    const payload = buildWebhookPayload(sub.webhook_platform, toSend, threshold);

    try {
      await fetch(sub.webhook_url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      // Registrar alertas enviadas
      await env.DB.batch(toSend.map(c =>
        env.DB.prepare(
          'INSERT OR IGNORE INTO alerts_sent (subscriber_id, model_id, old_price, new_price, change_pct, sent_at) VALUES (?, ?, ?, ?, ?, ?)'
        ).bind(sub.id, c.model_id, c.old_price, c.new_price, c.change_pct, date)
      ));

      fired += toSend.length;
    } catch (e) {
      console.error(`Alert dispatch failed for subscriber ${sub.id}:`, e.message);
    }
  }

  return fired;
}

function buildWebhookPayload(platform, changes, threshold) {
  const lines = changes.map(c => {
    const arrow = c.direction === 'DOWN' ? '⬇️' : '⬆️';
    const sign = c.direction === 'DOWN' ? '-' : '+';
    return `${arrow} **${c.model_name}** (${c.provider}): $${c.old_price.toFixed(4)} → $${c.new_price.toFixed(4)}/1M (${sign}${c.change_pct}%)`;
  }).join('\n');

  if (platform === 'discord') {
    return {
      username: 'LLM Price Tracker',
      avatar_url: 'https://aridanygomez.github.io/data-money-engine/og-image.svg',
      embeds: [{
        title: `🔔 ${changes.length} LLM Price Change${changes.length > 1 ? 's' : ''} Detected`,
        description: lines,
        color: changes.some(c => c.direction === 'DOWN') ? 0x2ecc71 : 0xe74c3c,
        footer: { text: `Threshold: ≥${threshold}% · llm-pricing.dev` },
        timestamp: new Date().toISOString(),
      }],
    };
  }

  // Slack
  return {
    blocks: [
      {
        type: 'header',
        text: { type: 'plain_text', text: `🔔 ${changes.length} LLM Price Change${changes.length > 1 ? 's' : ''}` },
      },
      {
        type: 'section',
        text: { type: 'mrkdwn', text: lines.replace(/\*\*/g, '*') },
      },
      {
        type: 'context',
        elements: [{ type: 'mrkdwn', text: `Threshold: ≥${threshold}% · <https://aridanygomez.github.io/data-money-engine/|View all prices>` }],
      },
    ],
  };
}

// ─── Polar.sh Webhook Handler ─────────────────────────────────────────────────

async function handlePolarWebhook(request, env) {
  // Verificar firma HMAC de Polar
  const body = await request.text();
  const signature = request.headers.get('webhook-signature') || '';

  const valid = await verifyPolarSignature(body, signature, env.POLAR_WEBHOOK_SECRET);
  if (!valid) return json({ error: 'Invalid signature' }, 401);

  const event = JSON.parse(body);
  const type = event.type;
  const data = event.data;

  console.log(`Polar event: ${type}`);

  if (type === 'subscription.created' || type === 'subscription.updated') {
    await handleSubscriptionActive(env, data);
  } else if (type === 'subscription.canceled' || type === 'subscription.revoked') {
    await handleSubscriptionCanceled(env, data);
  }

  return json({ received: true });
}

async function handleSubscriptionActive(env, data) {
  const customerId = data.customer_id || data.customer?.id;
  const email = data.customer?.email || data.email;
  const planName = data.product?.name?.toLowerCase().includes('pro') ? 'pro' : 'starter';
  const expiresAt = data.ends_at || data.current_period_end;

  if (!customerId || !email) return;

  // Generar API key única
  const apiKey = await generateApiKey(customerId);

  await env.DB.prepare(`
    INSERT INTO subscribers (polar_customer_id, email, plan, api_key, active, created_at, expires_at)
    VALUES (?, ?, ?, ?, 1, ?, ?)
    ON CONFLICT(polar_customer_id) DO UPDATE SET
      plan = excluded.plan,
      api_key = excluded.api_key,
      active = 1,
      expires_at = excluded.expires_at
  `).bind(customerId, email, planName, apiKey, new Date().toISOString().slice(0, 10), expiresAt || null).run();

  console.log(`Subscriber activated: ${email} (${planName}) -> key: ${apiKey.slice(0, 8)}...`);
}

async function handleSubscriptionCanceled(env, data) {
  const customerId = data.customer_id || data.customer?.id;
  if (!customerId) return;

  await env.DB.prepare(
    'UPDATE subscribers SET active = 0 WHERE polar_customer_id = ?'
  ).bind(customerId).run();
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function getSubscriber(env, apiKey) {
  return await env.DB.prepare(
    'SELECT * FROM subscribers WHERE api_key = ? AND active = 1'
  ).bind(apiKey).first();
}

async function checkRateLimit(env, key, limit) {
  const today = new Date().toISOString().slice(0, 10);
  const row = await env.DB.prepare(
    'SELECT count FROM rate_limits WHERE api_key = ? AND date = ?'
  ).bind(key, today).first();

  const count = (row?.count || 0) + 1;
  if (count > limit) return true;

  await env.DB.prepare(`
    INSERT INTO rate_limits (api_key, date, count) VALUES (?, ?, 1)
    ON CONFLICT(api_key, date) DO UPDATE SET count = count + 1
  `).bind(key, today).run();

  return false;
}

async function generateApiKey(seed) {
  const msgBuffer = new TextEncoder().encode(seed + Date.now() + Math.random());
  const hashBuffer = await crypto.subtle.digest('SHA-256', msgBuffer);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return 'llmp_' + hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}

// ─── Stripe Webhook Handler ─────────────────────────────────────────────────

async function handleStripeWebhook(request, env) {
  const body = await request.text();
  const sigHeader = request.headers.get('stripe-signature') || '';

  const valid = await verifyStripeSignature(body, sigHeader, env.STRIPE_WEBHOOK_SECRET);
  if (!valid) return json({ error: 'Invalid Stripe signature' }, 401);

  const event = JSON.parse(body);
  const type = event.type;
  const obj = event.data?.object;

  console.log(`Stripe event: ${type}`);

  if (type === 'customer.subscription.created' || type === 'customer.subscription.updated') {
    await handleStripeSubscriptionActive(env, obj);
  } else if (type === 'customer.subscription.deleted') {
    await handleStripeSubscriptionCanceled(env, obj);
  } else if (type === 'invoice.payment_failed') {
    // Marcar como inactivo si falla el pago
    const custId = obj?.customer;
    if (custId) {
      await env.DB.prepare(
        'UPDATE subscribers SET active = 0 WHERE stripe_customer_id = ?'
      ).bind(custId).run();
    }
  } else if (type === 'invoice.payment_succeeded') {
    // Reinversión automática: 20% del ingreso → pool de tokens gratuitos
    const amountPaid = (obj?.amount_paid || 0) / 100; // centavos → USD
    if (amountPaid > 0) {
      // llamada interna al mismo worker
      fetch(`https://llm-pricing-api.aridany-91.workers.dev/internal/reinvest`, {
        method: 'POST',
        headers: { 'X-Internal-Secret': env.INTERNAL_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount_usd: amountPaid, source: 'stripe' }),
      }).catch(e => console.error('Reinvest call failed:', e.message));
    }
  }

  return json({ received: true });
}

async function handleStripeSubscriptionActive(env, sub) {
  if (!sub) return;
  const custId = sub.customer;
  const status = sub.status; // active, trialing, past_due...
  if (!custId) return;

  // Obtener email del customer desde metadata o items
  const email = sub.customer_email || custId; // Stripe no incluye email en sub object directamente
  const priceId = sub.items?.data?.[0]?.price?.id || '';

  // Identificar plan por price_amount (400 = starter, 1200 = pro)
  const amount = sub.items?.data?.[0]?.price?.unit_amount || 0;
  const planName = amount >= 1200 ? 'pro' : 'starter';
  const isActive = ['active', 'trialing'].includes(status) ? 1 : 0;

  const apiKey = await generateApiKey(custId);
  const now = new Date().toISOString().slice(0, 10);
  const expiresAt = sub.current_period_end
    ? new Date(sub.current_period_end * 1000).toISOString().slice(0, 10)
    : null;

  await env.DB.prepare(`
    INSERT INTO subscribers (stripe_customer_id, email, plan, api_key, active, created_at, expires_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(stripe_customer_id) DO UPDATE SET
      plan = excluded.plan,
      api_key = CASE WHEN api_key IS NULL THEN excluded.api_key ELSE api_key END,
      active = excluded.active,
      expires_at = excluded.expires_at
  `).bind(custId, email, planName, apiKey, isActive, now, expiresAt).run().catch(async () => {
    // Si falla por columna faltante, usar polar_customer_id
    await env.DB.prepare(`
      INSERT INTO subscribers (polar_customer_id, email, plan, api_key, active, created_at, expires_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(polar_customer_id) DO UPDATE SET
        plan = excluded.plan,
        active = excluded.active,
        expires_at = excluded.expires_at
    `).bind(custId, email, planName, apiKey, isActive, now, expiresAt).run();
  });

  console.log(`Stripe subscriber activated: ${custId} (${planName})`);
}

async function handleStripeSubscriptionCanceled(env, sub) {
  if (!sub) return;
  const custId = sub.customer;
  if (!custId) return;
  await env.DB.prepare(
    'UPDATE subscribers SET active = 0 WHERE polar_customer_id = ?'
  ).bind(custId).run();
}

async function verifyStripeSignature(body, sigHeader, secret) {
  if (!secret || !sigHeader) return false;
  try {
    const parts = Object.fromEntries(sigHeader.split(',').map(p => p.split('=')));
    const timestamp = parts['t'];
    const sig = parts['v1'];
    if (!timestamp || !sig) return false;

    const payload = `${timestamp}.${body}`;
    const key = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(secret),
      { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
    );
    const signatureBuffer = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload));
    const expectedSig = Array.from(new Uint8Array(signatureBuffer))
      .map(b => b.toString(16).padStart(2, '0')).join('');

    // Timing-safe comparison
    if (expectedSig.length !== sig.length) return false;
    let mismatch = 0;
    for (let i = 0; i < expectedSig.length; i++) {
      mismatch |= expectedSig.charCodeAt(i) ^ sig.charCodeAt(i);
    }
    return mismatch === 0;
  } catch {
    return false;
  }
}

async function verifyPolarSignature(body, signature, secret) {
  if (!secret || !signature) return false;
  try {
    const key = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(secret),
      { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']
    );
    // Polar usa formato "v1,<hex>"
    const sigHex = signature.split(',').pop();
    const sigBytes = new Uint8Array(sigHex.match(/.{2}/g).map(h => parseInt(h, 16)));
    return await crypto.subtle.verify('HMAC', key, sigBytes, new TextEncoder().encode(body));
  } catch {
    return false;
  }
}

function getIP(request) {
  return request.headers.get('CF-Connecting-IP') || 'unknown';
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// SMART AI PROXY GATEWAY — nuevas funciones
// ═══════════════════════════════════════════════════════════════════════════

/**
 * POST /v1/chat/completions
 * OpenAI-compatible proxy que enruta a OpenRouter.
 * model aliases:  "auto" → más barato   "auto:long" → contexto ≥ 100K
 */
async function handleChatCompletions(request, url, env, ctx) {
  // 1. Auth
  const apiKey     = request.headers.get('X-API-Key')
    || (request.headers.get('Authorization') || '').replace(/^Bearer\s+/, '');
  const subscriber = apiKey ? await getSubscriber(env, apiKey) : null;
  if (!apiKey) return json({ error: 'X-API-Key or Authorization Bearer required. Get a free key at ' + PRICING_URL }, 401);
  if (!subscriber) return json({ error: 'Invalid or expired API key' }, 401);

  const plan = subscriber.plan || 'starter';

  // 2. Parse body
  let body;
  try   { body = await request.json(); }
  catch { return json({ error: 'Invalid JSON body' }, 400); }

  const { messages, stream = false } = body;
  let modelReq = body.model || 'auto';
  if (!messages?.length) return json({ error: '`messages` array is required' }, 400);

  // 3. Resolver modelo → ID real de OpenRouter
  const { model: resolvedModel, modelMeta } = await resolveGatewayModel(modelReq, env, plan);
  if (!resolvedModel) {
    return json({ error: `Model "${modelReq}" not available on your plan (${plan}). Upgrade at ${PRICING_URL}` }, 403);
  }

  // 4. Verificar cuota mensual
  const quotaErr = await checkTokenQuota(subscriber, plan, env);
  if (quotaErr) return json({ error: quotaErr, upgrade_url: PRICING_URL }, 429);

  // 5. Construir payload para OpenRouter
  const upstream = {
    model:       resolvedModel,
    messages,
    stream,
    temperature:  body.temperature  ?? 0.7,
    max_tokens:   body.max_tokens   ?? 2000,
    top_p:        body.top_p        ?? 1,
  };

  const t0 = Date.now();
  const orResp = await fetch('https://openrouter.ai/api/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.OPENROUTER_API_KEY}`,
      'Content-Type':  'application/json',
      'HTTP-Referer':   SITE_URL,
      'X-Title':       'LLM Pricing Gateway',
    },
    body: JSON.stringify(upstream),
  });
  const latencyMs = Date.now() - t0;

  // 6. Streaming path
  if (stream && orResp.ok) {
    ctx.waitUntil(logProxyUsage(env, subscriber, resolvedModel, modelMeta, 0, 0, 0, 0, latencyMs, 'streaming', plan));
    return new Response(orResp.body, {
      status: orResp.status,
      headers: {
        ...CORS_HEADERS,
        'Content-Type':     'text/event-stream',
        'Cache-Control':    'no-cache',
        'X-Model-Used':     resolvedModel,
        'X-Model-Requested': modelReq,
        'X-Gateway':        'llm-pricing-gateway',
      },
    });
  }

  // 7. Non-streaming: parse + billing
  let orData;
  try   { orData = await orResp.json(); }
  catch { return json({ error: 'Upstream returned invalid JSON', upstream_status: orResp.status }, 502); }

  if (!orResp.ok) {
    return json({ error: orData?.error?.message || 'Upstream error', upstream_status: orResp.status }, orResp.status);
  }

  const usage       = orData.usage || {};
  const promptT     = usage.prompt_tokens     || 0;
  const outputT     = usage.completion_tokens || 0;
  const totalT      = promptT + outputT;
  const markup      = PLANS[plan]?.markup ?? 0.3;
  const costReal    = calcCost(modelMeta, promptT, outputT);
  const costBilled  = costReal * (1 + markup);

  ctx.waitUntil(logProxyUsage(env, subscriber, resolvedModel, modelMeta, promptT, outputT, costReal, costBilled, latencyMs, 'ok', plan));

  orData.gateway = {
    model_used:      resolvedModel,
    model_requested: modelReq,
    cost_real_usd:   +costReal.toFixed(6),
    cost_billed_usd: +costBilled.toFixed(6),
    tokens_total:    totalT,
    latency_ms:      latencyMs,
    plan,
  };

  return new Response(JSON.stringify(orData), {
    status: 200,
    headers: {
      ...CORS_HEADERS,
      'Content-Type':      'application/json',
      'X-Model-Used':      resolvedModel,
      'X-Model-Requested': modelReq,
      'X-Tokens-Used':     String(totalT),
      'X-Cost-USD':        costBilled.toFixed(6),
      'X-Latency-Ms':      String(latencyMs),
      'X-Gateway':         'llm-pricing-gateway',
    },
  });
}

/**
 * GET /v1/models  —  OpenAI-compatible model list
 */
async function handleGatewayModels(env) {
  const { results } = await env.DB.prepare(
    'SELECT id, name, provider, context_length, prompt_price_per_1m, completion_price_per_1m FROM models WHERE is_free = 0 ORDER BY total_price_per_1m ASC LIMIT 60'
  ).all();
  const data = [
    { id: 'auto',        object: 'model', owned_by: 'gateway', description: 'Auto-routes to cheapest available model' },
    { id: 'auto:coding', object: 'model', owned_by: 'gateway', description: 'Cheapest model with ≥16K context' },
    { id: 'auto:long',   object: 'model', owned_by: 'gateway', description: 'Cheapest model with ≥100K context' },
    ...results.map(m => ({
      id: m.id, object: 'model', owned_by: m.provider,
      meta: {
        context_length: m.context_length,
        price_prompt_per_1m_usd:  m.prompt_price_per_1m,
        price_output_per_1m_usd:  m.completion_price_per_1m,
      },
    })),
  ];
  return json({ object: 'list', data });
}

/**
 * Resuelve el model alias o ID al ID real de OpenRouter.
 */
async function resolveGatewayModel(modelReq, env, plan) {
  if (modelReq === 'auto' || !modelReq) {
    const m = await env.DB.prepare(
      'SELECT * FROM models WHERE is_free = 0 AND total_price_per_1m > 0 ORDER BY total_price_per_1m ASC LIMIT 1'
    ).first();
    return { model: m?.id, modelMeta: m };
  }
  if (modelReq === 'auto:coding') {
    const m = await env.DB.prepare(
      'SELECT * FROM models WHERE is_free = 0 AND context_length >= 16000 AND total_price_per_1m > 0 ORDER BY total_price_per_1m ASC LIMIT 1'
    ).first();
    return { model: m?.id, modelMeta: m };
  }
  if (modelReq === 'auto:long') {
    const m = await env.DB.prepare(
      'SELECT * FROM models WHERE context_length >= 100000 AND total_price_per_1m > 0 ORDER BY total_price_per_1m ASC LIMIT 1'
    ).first();
    return { model: m?.id, modelMeta: m };
  }
  const m = await env.DB.prepare(
    'SELECT * FROM models WHERE id = ? OR slug = ? LIMIT 1'
  ).bind(modelReq, modelReq).first();
  if (!m) return { model: null, modelMeta: null };
  // Free only gets cheapest
  if (plan === 'free') {
    const cheapest = await env.DB.prepare(
      'SELECT id FROM models WHERE is_free = 0 AND total_price_per_1m > 0 ORDER BY total_price_per_1m ASC LIMIT 1'
    ).first();
    if (cheapest?.id !== m.id) return { model: null, modelMeta: null };
  }
  return { model: m.id, modelMeta: m };
}

/**
 * Verifica que el subscriber no haya superado su cuota mensual.
 */
async function checkTokenQuota(subscriber, plan, env) {
  const limit = PLANS[plan]?.monthly_tokens ?? FREE_PROXY_MONTHLY;
  if (plan === 'pro') return null; // Pro goes metered, never blocked
  const month = new Date().toISOString().slice(0, 7);
  const row = await env.DB.prepare(
    'SELECT COALESCE(SUM(total_tokens), 0) as used FROM usage_logs WHERE subscriber_id = ? AND month = ?'
  ).bind(subscriber.id, month).first();
  const used = row?.used || 0;
  if (used >= limit) {
    const mM = Math.round(limit / 1_000_000);
    return `Monthly token quota exhausted (${used.toLocaleString()}/${mM}M tokens). Upgrade at ${PRICING_URL} or wait until next month.`;
  }
  return null;
}

function calcCost(modelMeta, promptT, outputT) {
  if (!modelMeta) return 0;
  const pIn  = (modelMeta.prompt_price_per_1m     || 0) / 1_000_000;
  const pOut = (modelMeta.completion_price_per_1m || 0) / 1_000_000;
  return pIn * promptT + pOut * outputT;
}

async function logProxyUsage(env, subscriber, model, modelMeta, promptT, outputT, costReal, costBilled, latencyMs, status, plan) {
  if (!subscriber) return;
  const month = new Date().toISOString().slice(0, 7);
  try {
    await env.DB.prepare(`
      INSERT INTO usage_logs
        (subscriber_id, model_id, provider, prompt_tokens, completion_tokens, total_tokens,
         cost_real_usd, cost_billed_usd, latency_ms, month, status, plan, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      subscriber.id,
      model,
      modelMeta?.provider || '',
      promptT, outputT, promptT + outputT,
      +costReal.toFixed(8),
      +costBilled.toFixed(8),
      latencyMs,
      month,
      status,
      plan,
      new Date().toISOString(),
    ).run();
  } catch (e) {
    console.error('Usage log error:', e.message);
  }
}

/**
 * POST /internal/reinvest
 * Recibe un importe de pago Stripe y añade el 20% como créditos al pool libre.
 */
async function handleReinvestment(request, env) {
  if (request.headers.get('X-Internal-Secret') !== env.INTERNAL_SECRET)
    return json({ error: 'Unauthorized' }, 401);
  const { amount_usd, source } = await request.json();
  const credits_usd = +(amount_usd * REINVESTMENT_PCT).toFixed(4);
  // ~$0.10/1M tokens en el modelo más barato
  const tokens_added = Math.round(credits_usd / 0.10 * 1_000_000);
  try {
    await env.DB.prepare(
      `INSERT INTO global_config (key, value) VALUES ('free_pool_tokens', ?)
       ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT)`
    ).bind(String(tokens_added), tokens_added).run();
    await env.DB.prepare(
      'INSERT INTO reinvestment_log (amount_received_usd, credits_added_usd, tokens_added, source, created_at) VALUES (?, ?, ?, ?, ?)'
    ).bind(amount_usd, credits_usd, tokens_added, source || 'stripe', new Date().toISOString()).run();
  } catch (e) {
    console.error('Reinvestment log error:', e.message);
  }
  return json({ success: true, amount_usd, credits_usd, tokens_added, pct: REINVESTMENT_PCT });
}

