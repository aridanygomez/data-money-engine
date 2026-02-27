"""
bot_generador.py — Motor autónomo diario de contenido y datos

Ejecuta cada día desde GitHub Actions:
  1. Descarga precios actuales de 150+ LLMs (OpenRouter API — gratis, sin key)
  2. Genera descripciones SEO únicas para cada modelo (Gemini Flash — gratis)
  3. Genera ideas de contenido del día (tweets, posts Reddit)
  4. Actualiza todos los JSONs de datos del repo
  5. GitHub Actions hace commit automático de los cambios

Variables de entorno requeridas (GitHub Secrets):
  GEMINI_API_KEY  — de aistudio.google.com (gratis, 1,000 req/día)
"""

import os
import json
import time
import requests
from datetime import datetime, date
from pathlib import Path
import re

# ─── Paths ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MODELS_FILE = DATA_DIR / "models.json"
COMPARISONS_FILE = DATA_DIR / "comparisons.json"
DAILY_LOG_FILE = DATA_DIR / "daily_log.json"
CONTENT_IDEAS_FILE = OUTPUT_DIR / "content_ideas.md"
PRICES_HISTORY_FILE = DATA_DIR / "prices_history.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TODAY = date.today().isoformat()
NOW = datetime.now().isoformat()

# Modelos más buscados (para priorizar contenido)
PRIORITY_MODELS = [
    "openai/gpt-4o", "openai/gpt-4o-mini", "openai/o1",
    "anthropic/claude-3-5-sonnet", "anthropic/claude-3-5-haiku", "anthropic/claude-3-opus",
    "google/gemini-pro-1.5", "google/gemini-flash-1.5", "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.1-70b-instruct", "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-large", "deepseek/deepseek-r1", "deepseek/deepseek-chat",
    "qwen/qwen-2.5-72b-instruct", "cohere/command-r-plus",
]

STORMROUTER_URL = "https://stormrouter.dev"


# ─── Step 1: Fetch LLM prices from OpenRouter ────────────────────────────────

def fetch_llm_prices() -> list[dict]:
    """Descarga precios de 150+ modelos de OpenRouter. Sin API key requerida."""
    print("[1/5] Descargando precios de LLMs desde OpenRouter...")
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=20)
        r.raise_for_status()
        raw = r.json().get("data", [])
    except Exception as e:
        print(f"  ERROR: No se pudo conectar a OpenRouter: {e}")
        # Si falla, usar datos existentes
        if MODELS_FILE.exists():
            print("  Usando datos existentes.")
            return json.loads(MODELS_FILE.read_text())
        return []

    models = []
    for m in raw:
        pricing = m.get("pricing", {})
        prompt_price = float(pricing.get("prompt", 0) or 0)
        completion_price = float(pricing.get("completion", 0) or 0)

        model_id = m.get("id", "")
        parts = model_id.split("/", 1)
        provider = parts[0] if len(parts) > 1 else "unknown"

        slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")

        prompt_pm = round(prompt_price * 1_000_000, 6)
        completion_pm = round(completion_price * 1_000_000, 6)

        models.append({
            "id": model_id,
            "slug": slug,
            "name": m.get("name", model_id),
            "provider": provider,
            "context_length": m.get("context_length", 0),
            "prompt_price_per_1m": prompt_pm,
            "completion_price_per_1m": completion_pm,
            "total_price_per_1m": round(prompt_pm + completion_pm, 6),
            "is_free": prompt_pm == 0 and completion_pm == 0,
            "openrouter_url": f"https://openrouter.ai/{model_id}",
            "fetched_at": TODAY,
        })

    models.sort(key=lambda x: x["total_price_per_1m"])
    print(f"  ✅ {len(models)} modelos obtenidos ({sum(1 for m in models if m['is_free'])} gratuitos)")
    return models


def detect_price_changes(new_models: list[dict]) -> list[dict]:
    """Compara precios nuevos con los anteriores, detecta cambios."""
    if not MODELS_FILE.exists():
        return []

    old_models = {m["id"]: m for m in json.loads(MODELS_FILE.read_text())}
    changes = []
    for m in new_models:
        old = old_models.get(m["id"])
        if old and abs(old["total_price_per_1m"] - m["total_price_per_1m"]) > 0.001:
            pct = ((m["total_price_per_1m"] - old["total_price_per_1m"]) / max(old["total_price_per_1m"], 0.001)) * 100
            changes.append({
                "model": m["name"],
                "provider": m["provider"],
                "old_price": old["total_price_per_1m"],
                "new_price": m["total_price_per_1m"],
                "change_pct": round(pct, 1),
                "direction": "⬆️ SUBIÓ" if pct > 0 else "⬇️ BAJÓ",
            })
    return changes


def save_price_history(models: list[dict]):
    """Guarda snapshot mensual de precios para tracking histórico."""
    history = {}
    if PRICES_HISTORY_FILE.exists():
        history = json.loads(PRICES_HISTORY_FILE.read_text())

    month_key = TODAY[:7]  # "2026-02"
    if month_key not in history:
        history[month_key] = {
            "date": TODAY,
            "model_count": len(models),
            "avg_price_gpt4_class": 0,
            "cheapest_paid": None,
            "free_count": sum(1 for m in models if m["is_free"]),
        }
        paid = [m for m in models if not m["is_free"] and m["total_price_per_1m"] > 0]
        if paid:
            history[month_key]["cheapest_paid"] = {
                "name": paid[0]["name"],
                "price": paid[0]["total_price_per_1m"],
            }
        PRICES_HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ─── Step 2: Generate comparison pairs ───────────────────────────────────────

def generate_comparison_pairs(models: list[dict]) -> list[dict]:
    """Genera pares de modelos para páginas de comparación."""
    print("[2/5] Generando pares de comparación...")
    priority_set = set(PRIORITY_MODELS)
    priority_models = [m for m in models if m["id"] in priority_set]

    pairs = []
    seen = set()

    # Todas las combinaciones entre modelos prioritarios
    for i, a in enumerate(priority_models):
        for b in priority_models[i+1:]:
            key = tuple(sorted([a["slug"], b["slug"]]))
            if key not in seen:
                seen.add(key)
                pairs.append({
                    "slug": f"{min(a['slug'], b['slug'])}--vs--{max(a['slug'], b['slug'])}",
                    "model_a_id": a["id"],
                    "model_b_id": b["id"],
                    "model_a_slug": a["slug"],
                    "model_b_slug": b["slug"],
                    "name_a": a["name"],
                    "name_b": b["name"],
                })

    # Cada modelo prioritario vs los 5 más baratos
    cheapest = [m for m in models if not m["is_free"]][:5]
    for top in priority_models[:8]:
        for cheap in cheapest:
            if cheap["id"] in priority_set:
                continue
            key = tuple(sorted([top["slug"], cheap["slug"]]))
            if key not in seen:
                seen.add(key)
                pairs.append({
                    "slug": f"{min(top['slug'], cheap['slug'])}--vs--{max(top['slug'], cheap['slug'])}",
                    "model_a_id": top["id"],
                    "model_b_id": cheap["id"],
                    "model_a_slug": top["slug"],
                    "model_b_slug": cheap["slug"],
                    "name_a": top["name"],
                    "name_b": cheap["name"],
                })

    print(f"  ✅ {len(pairs)} pares de comparación generados")
    return pairs


# ─── Step 3: Generate content with Gemini ────────────────────────────────────

def gemini(prompt: str, max_tokens: int = 600) -> str:
    """Llama a Gemini Flash (gratis: 1,000 req/día, 15 req/min)."""
    if not GEMINI_API_KEY:
        return ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 429:
            print("  [Gemini] Rate limit — esperando 60s...")
            time.sleep(60)
            return gemini(prompt, max_tokens)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  [Gemini] Error: {e}")
        return ""


def generate_model_descriptions(models: list[dict], limit: int = 20) -> dict:
    """Genera descripciones SEO para los modelos prioritarios."""
    print(f"[3/5] Generando descripciones de modelos (máx {limit} hoy)...")
    if not GEMINI_API_KEY:
        print("  ⚠️  GEMINI_API_KEY no configurado — saltando generación de texto")
        return {}

    # Cargar descripciones existentes
    desc_file = DATA_DIR / "descriptions.json"
    descriptions = {}
    if desc_file.exists():
        descriptions = json.loads(desc_file.read_text())

    # Generar solo para los que no tienen descripción aún
    todo = [m for m in models if m["id"] not in descriptions]
    # Priorizar los más buscados
    todo.sort(key=lambda m: 0 if m["id"] in PRIORITY_MODELS else 1)
    todo = todo[:limit]

    for i, m in enumerate(todo):
        is_free_str = "completely free (0 cost)" if m["is_free"] else f"${m['total_price_per_1m']:.4f} per 1M tokens"
        prompt = f"""Write a 120-150 word SEO description for a page about {m['name']} API pricing.

Key facts:
- Input: ${m['prompt_price_per_1m']:.5f}/1M tokens
- Output: ${m['completion_price_per_1m']:.5f}/1M tokens
- Cost: {is_free_str}
- Context: {m['context_length']:,} tokens
- Provider: {m['provider']}

Target audience: ML engineers comparing LLM APIs. Include a realistic monthly cost example.
Plain paragraph only. No headers, no bullet points. Be specific and data-driven."""

        desc = gemini(prompt, max_tokens=250)
        if desc:
            descriptions[m["id"]] = desc
            print(f"  [{i+1}/{len(todo)}] {m['name']} ✅")
        time.sleep(1.2)  # 15 req/min free tier → 1 req/4s safe

    desc_file.write_text(json.dumps(descriptions, indent=2, ensure_ascii=False))
    print(f"  ✅ {len(descriptions)} descripciones totales en cache")
    return descriptions


def generate_daily_content_ideas(models: list[dict], price_changes: list[dict]) -> str:
    """Genera ideas de tweets y posts para hoy usando Gemini."""
    print("[4/5] Generando ideas de contenido para hoy...")
    if not GEMINI_API_KEY:
        return _fallback_content_ideas(models, price_changes)

    # Datos interesantes del día
    free_count = sum(1 for m in models if m["is_free"])
    paid = [m for m in models if not m["is_free"] and m["total_price_per_1m"] > 0]
    cheapest = paid[0] if paid else None
    most_expensive = max(paid, key=lambda x: x["total_price_per_1m"]) if paid else None

    changes_text = ""
    if price_changes:
        changes_text = "Price changes detected today:\n" + "\n".join(
            f"- {c['model']}: {c['direction']} {abs(c['change_pct'])}%"
            for c in price_changes[:5]
        )

    price_ratio = ""
    if cheapest and most_expensive and cheapest["total_price_per_1m"] > 0:
        ratio = most_expensive["total_price_per_1m"] / cheapest["total_price_per_1m"]
        price_ratio = f"Most expensive model is {ratio:.0f}x the price of the cheapest paid option."

    prompt = f"""You are a developer-focused content creator who writes about LLM API costs.

Today's data ({TODAY}):
- Total models tracked: {len(models)}
- Free models available: {free_count}
- Cheapest paid: {cheapest['name'] if cheapest else 'N/A'} at ${cheapest['total_price_per_1m']:.4f}/1M tokens
- Most expensive: {most_expensive['name'] if most_expensive else 'N/A'} at ${most_expensive['total_price_per_1m']:.2f}/1M tokens
- {price_ratio}
{changes_text}

Generate 3 tweet ideas (max 280 chars each) and 1 short Reddit post hook for r/MachineLearning about LLM pricing.
End each with a subtle mention of StormRouter ({STORMROUTER_URL}) as a solution for routing costs.
Format: clearly labeled TWEET 1, TWEET 2, TWEET 3, REDDIT POST."""

    content = gemini(prompt, max_tokens=600)
    if not content:
        return _fallback_content_ideas(models, price_changes)

    return f"""# 📣 Ideas de contenido — {TODAY}

> Auto-generado por bot_generador.py · {NOW}

## Datos del día
- **Modelos tracked:** {len(models)}
- **Modelos gratuitos:** {free_count}
- **Más barato (pago):** {cheapest['name'] if cheapest else 'N/A'} — ${cheapest['total_price_per_1m']:.4f}/1M tokens
- **Más caro:** {most_expensive['name'] if most_expensive else 'N/A'} — ${most_expensive['total_price_per_1m']:.2f}/1M tokens

{"## ⚠️ Cambios de precio detectados" + chr(10) + chr(10).join(f"- **{c['model']}**: {c['direction']} {abs(c['change_pct'])}% (${c['old_price']:.4f} → ${c['new_price']:.4f}/1M)" for c in price_changes) if price_changes else ""}

## 🐦 Ideas de contenido (listos para copiar-pegar)

{content}

---
*Fuente de datos: OpenRouter API · Sitio: llm-pricing.dev · Producto: stormrouter.dev*
"""


def _fallback_content_ideas(models: list[dict], price_changes: list[dict]) -> str:
    """Ideas de contenido sin Gemini (datos en bruto)."""
    free_count = sum(1 for m in models if m["is_free"])
    paid = [m for m in models if not m["is_free"] and m["total_price_per_1m"] > 0]
    cheapest = paid[0] if paid else None

    tweet = f"There are now {free_count} free LLM APIs available (no rate limits on some). "
    if cheapest:
        tweet += f"The cheapest paid option: {cheapest['name']} at ${cheapest['total_price_per_1m']:.4f}/1M tokens. Most teams overpay by 10x. {STORMROUTER_URL}"

    return f"""# 📣 Ideas de contenido — {TODAY}

## Datos del día
- Modelos tracked: {len(models)}
- Gratuitos: {free_count}
- Más barato pago: {cheapest['name'] if cheapest else 'N/A'} — ${cheapest['total_price_per_1m']:.4f}/1M tokens

{"## Cambios de precio" + chr(10) + chr(10).join(f"- {c['model']}: {c['direction']} {abs(c['change_pct'])}%" for c in price_changes) if price_changes else ""}

## Tweet sugerido
{tweet[:280]}

---
*GEMINI_API_KEY no configurado — ideas básicas generadas automáticamente*
"""


# ─── Step 4: Generate index page data ────────────────────────────────────────

def generate_site_data(models: list[dict], descriptions: dict):
    """Genera JSON optimizado para el sitio Astro estático."""
    print("[5/5] Generando datos para el sitio...")

    # Top 50 modelos para la homepage
    top_paid = [m for m in models if not m["is_free"]][:30]
    free_models = [m for m in models if m["is_free"]][:20]
    priority_models = [m for m in models if m["id"] in PRIORITY_MODELS]

    site_data = {
        "generated_at": NOW,
        "date": TODAY,
        "stats": {
            "total_models": len(models),
            "free_models": len([m for m in models if m["is_free"]]),
            "paid_models": len([m for m in models if not m["is_free"]]),
            "providers": len(set(m["provider"] for m in models)),
        },
        "priority_models": [{**m, "description": descriptions.get(m["id"], "")} for m in priority_models],
        "cheapest_paid": [{**m, "description": descriptions.get(m["id"], "")} for m in top_paid[:15]],
        "free_models": [{**m, "description": descriptions.get(m["id"], "")} for m in free_models[:10]],
    }

    site_data_file = DATA_DIR / "site_data.json"
    site_data_file.write_text(json.dumps(site_data, indent=2, ensure_ascii=False))
    print(f"  ✅ site_data.json generado ({len(site_data['priority_models'])} modelos prioritarios)")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🤖 Bot Generador — {TODAY}")
    print("=" * 50)

    start = time.time()
    log_entry = {
        "date": TODAY,
        "timestamp": NOW,
        "success": False,
        "models_fetched": 0,
        "descriptions_generated": 0,
        "price_changes": [],
        "errors": [],
    }

    # 1. Fetch prices
    models = fetch_llm_prices()
    if not models:
        log_entry["errors"].append("No se pudieron obtener precios")
        _save_log(log_entry)
        return

    log_entry["models_fetched"] = len(models)

    # Detectar cambios de precio antes de guardar
    price_changes = detect_price_changes(models)
    if price_changes:
        print(f"  ⚠️  {len(price_changes)} cambios de precio detectados hoy:")
        for c in price_changes:
            print(f"     {c['direction']} {c['model']}: ${c['old_price']:.4f} → ${c['new_price']:.4f}/1M")
    log_entry["price_changes"] = price_changes

    # Guardar modelos y historial
    MODELS_FILE.write_text(json.dumps(models, indent=2, ensure_ascii=False))
    save_price_history(models)

    # 2. Comparaciones
    comparisons = generate_comparison_pairs(models)
    COMPARISONS_FILE.write_text(json.dumps(comparisons, indent=2, ensure_ascii=False))

    # 3. Descripciones con Gemini
    # Solo 15 por día para no agotar el free tier (1,000 req/día total con ideas de contenido)
    descriptions = generate_model_descriptions(models, limit=15)
    log_entry["descriptions_generated"] = len(descriptions)

    # 4. Ideas de contenido
    content_ideas = generate_daily_content_ideas(models, price_changes)
    CONTENT_IDEAS_FILE.write_text(content_ideas, encoding="utf-8")
    print(f"  ✅ Ideas de contenido guardadas en output/content_ideas.md")

    # 5. Site data
    generate_site_data(models, descriptions)

    # Log final
    elapsed = round(time.time() - start, 1)
    log_entry["success"] = True
    log_entry["duration_seconds"] = elapsed
    _save_log(log_entry)

    print("=" * 50)
    print(f"✅ Bot completado en {elapsed}s")
    print(f"   📊 {len(models)} modelos · {len(comparisons)} comparaciones")
    print(f"   ✍️  {len(descriptions)} descripciones generadas")
    print(f"   💡 Ideas de contenido en output/content_ideas.md")
    print(f"   {'⚠️  ' + str(len(price_changes)) + ' cambios de precio' if price_changes else '✅ Sin cambios de precio'}")


def _save_log(entry: dict):
    log = []
    if DAILY_LOG_FILE.exists():
        try:
            log = json.loads(DAILY_LOG_FILE.read_text())
        except Exception:
            pass
    log.insert(0, entry)
    log = log[:90]  # Mantener últimos 90 días
    DAILY_LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
