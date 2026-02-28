/**
 * LLM Pricing SaaS — Cloudflare Worker
 * ─────────────────────────────────────
 * Endpoints públicos:
 *   GET  /api/v1/models              → lista modelos (rate-limited gratis, full para paid)
 *   GET  /api/v1/models/:id          → modelo individual
 *   GET  /api/v1/cheapest?n=10       → N más baratos
 *   GET  /api/v1/history/:id         → historial de precios (solo Pro)
 *
 * Endpoints autenticados (API key en header X-API-Key):
 *   POST /api/v1/alerts/register     → registrar webhook Slack/Discord (Starter+)
 *   GET  /api/v1/alerts              → listar mis alertas
 *   DELETE /api/v1/alerts/:model_id  → borrar alerta
 *
 * Webhooks internos (auth via INTERNAL_SECRET header):
 *   POST /internal/sync              → bot sube precios frescos + dispara alertas
 *
 * Webhooks externos:
 *   POST /webhooks/polar             → Polar.sh subscription events
 */

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
      // ── Internal (bot → D1 sync) ──────────────────────────────────────────
      if (path === '/internal/sync' && method === 'POST') {
        return await handleInternalSync(request, env);
      }

      // ── Polar.sh webhooks ────────────────────────────────────────────────
      if (path === '/webhooks/polar' && method === 'POST') {
        return await handlePolarWebhook(request, env);
      }

      // ── Stripe webhooks ──────────────────────────────────────────────────
      if (path === '/webhooks/stripe' && method === 'POST') {
        return await handleStripeWebhook(request, env);
      }

      // ── Public API ────────────────────────────────────────────────────────
      if (path.startsWith('/api/v1/')) {
        return await handleAPI(request, url, path, env);
      }

      return json({ error: 'Not found' }, 404);
    } catch (e) {
      console.error(e);
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
