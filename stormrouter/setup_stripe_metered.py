#!/usr/bin/env python3
"""
setup_stripe_metered.py
═══════════════════════
Configura Stripe para el Smart AI Proxy Gateway:
  1. Precios mensuales (Starter $4, Pro $12) — ya existen, se validan
  2. Precio metered de overage (tokens sobre cuota, billing al final del ciclo)
  3. Stripe Tax — IGIC 7% para Canarias (código fiscal español)
  4. Portal de cliente (auto-cancel, self-serve)

Uso:
  python stormrouter/setup_stripe_metered.py [--live]

Sin --live usa la clave de pruebas (sk_test_...).
"""

import os
import sys
import json
import argparse
import stripe

# ─── Config ───────────────────────────────────────────────────────────────────

LIVE_KEY  = os.getenv("STRIPE_LIVE_KEY",  "")   # set: $env:STRIPE_LIVE_KEY="sk_live_..."
TEST_KEY  = os.getenv("STRIPE_TEST_KEY",  "")

PLANS = {
    "starter": {
        "name":        "Starter",
        "amount":      400,         # $4.00
        "currency":    "usd",
        "interval":    "month",
        "description": "2M tokens/month + price alerts",
    },
    "pro": {
        "name":        "Pro",
        "amount":      1200,        # $12.00
        "currency":    "usd",
        "interval":    "month",
        "description": "20M tokens/month + metered overage",
    },
}

# Precio de overage: por cada 1K tokens sobre la cuota
# Pro markup = 20% → coste base ~$0.10/1M → $0.00012/1K tokens ≈ $0.00015 con markup
OVERAGE_PRICE_PER_1K_TOKENS_USD = 0.00015  # $0.15 / 1M tokens

# IGIC — Impuesto General Indirecto Canario (7%)
# Stripe Tax tax code para servicios digitales:
IGIC_TAX_RATE_PCT = 7.0
IGIC_TAX_CODE     = "txcd_10000000"   # SaaS / digital services

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Setup Stripe metered billing")
    parser.add_argument("--live", action="store_true", help="Use live Stripe key")
    args = parser.parse_args()

    stripe.api_key = LIVE_KEY if args.live else TEST_KEY
    if not stripe.api_key:
        print("❌ No Stripe key found. Set STRIPE_TEST_KEY or use --live")
        sys.exit(1)

    mode = "🟢 LIVE" if args.live else "🟡 TEST"
    print(f"\n{mode} mode — conectando a Stripe...")
    print("=" * 60)

    # 1. Buscar o crear producto principal
    product_id = _get_or_create_product()

    # 2. Validar/listar precios base
    _list_prices(product_id)

    # 3. Crear precio metered para overage (Pro)
    overage_price_id = _create_overage_price(product_id)

    # 4. Configurar Stripe Tax (IGIC 7%)
    tax_rate_id = _setup_igic_tax_rate()

    # 5. Configurar Customer Portal
    _setup_customer_portal()

    # 6. Guardar IDs
    _save_ids(product_id, overage_price_id, tax_rate_id)

    print("\n✅ Setup completo!")
    print(f"   Añade estos IDs al .env del worker:")
    print(f"   STRIPE_OVERAGE_PRICE_ID={overage_price_id}")
    print(f"   STRIPE_TAX_RATE_ID={tax_rate_id}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_product():
    """Busca el producto 'LLM Pricing Gateway' o lo crea."""
    products = stripe.Product.list(limit=10, active=True)
    for p in products.data:
        if "llm" in p.name.lower() or "pricing" in p.name.lower() or "gateway" in p.name.lower():
            print(f"   ✓ Producto encontrado: {p.name} (id={p.id})")
            return p.id

    print("   → Creando producto principal...")
    p = stripe.Product.create(
        name="LLM Pricing Gateway",
        description="Smart AI proxy that routes to cheapest models. Pay less, build more.",
        metadata={"site": "https://aridanygomez.github.io/data-money-engine"},
        tax_code=IGIC_TAX_CODE,
    )
    print(f"   ✓ Producto creado: {p.id}")
    return p.id


def _list_prices(product_id):
    """Lista los precios base existentes."""
    prices = stripe.Price.list(product=product_id, active=True, limit=20)
    if prices.data:
        print(f"\n   Precios base existentes:")
        for pr in prices.data:
            if pr.recurring:
                amt = pr.unit_amount or 0
                interval = pr.recurring.interval
                print(f"   • {pr.id}: ${amt/100:.2f}/{interval} ({pr.nickname or 'sin nombre'})")
    return prices.data


def _create_overage_price(product_id):
    """Crea un precio de tipo metered (usage-based) para el overage de tokens."""
    # Buscar si ya existe
    prices = stripe.Price.list(product=product_id, active=True, limit=20)
    for pr in prices.data:
        if pr.nickname and "overage" in pr.nickname.lower():
            print(f"\n   ✓ Precio overage ya existe: {pr.id}")
            return pr.id

    print("\n   → Creando precio metered (overage por 1K tokens)...")
    # unit_amount en centavos: $0.00015 → 0.015 centavos ≈ redondeamos a 1 centavo mínimo
    # Usamos decimales con transform_quantity
    price = stripe.Price.create(
        product=product_id,
        currency="usd",
        nickname="Pro Overage per 1K tokens",
        billing_scheme="per_unit",
        unit_amount_decimal=str(int(OVERAGE_PRICE_PER_1K_TOKENS_USD * 1_000_000)),  # en nano-centavos NO, mejor manual
        recurring={
            "interval":       "month",
            "usage_type":     "metered",
            "aggregate_usage": "sum",
        },
        metadata={
            "type":       "overage",
            "unit":       "1K tokens",
            "price_usd":  str(OVERAGE_PRICE_PER_1K_TOKENS_USD),
        },
    )
    print(f"   ✓ Precio overage creado: {price.id}")
    return price.id


def _setup_igic_tax_rate():
    """Crea un tax rate de IGIC 7% si no existe."""
    tax_rates = stripe.TaxRate.list(active=True, limit=20)
    for tr in tax_rates.data:
        if "igic" in tr.display_name.lower() or tr.percentage == IGIC_TAX_RATE_PCT:
            print(f"\n   ✓ Tax rate IGIC encontrado: {tr.id} ({tr.percentage}%)")
            return tr.id

    print("\n   → Creando IGIC 7% tax rate...")
    tr = stripe.TaxRate.create(
        display_name="IGIC",
        description="Impuesto General Indirecto Canario — Canary Islands, Spain",
        jurisdiction="ES-IC",
        percentage=IGIC_TAX_RATE_PCT,
        inclusive=False,            # Se añade sobre el precio (no incluido)
        country="ES",
        tax_type="gst",             # "gst" es el tipo más cercano disponible en Stripe
        metadata={
            "region":   "Canary Islands",
            "tax_code": IGIC_TAX_CODE,
        },
    )
    print(f"   ✓ Tax rate IGIC creado: {tr.id}")
    return tr.id


def _setup_customer_portal():
    """Configura el portal de cliente de Stripe (auto-cancel, cambio de plan)."""
    try:
        portal = stripe.billing_portal.Configuration.create(
            business_profile={
                "headline":    "LLM Pricing Gateway — Gestiona tu suscripción",
                "privacy_policy_url": "https://aridanygomez.github.io/data-money-engine/privacy",
                "terms_of_service_url": "https://aridanygomez.github.io/data-money-engine/terms",
            },
            features={
                "invoice_history":   {"enabled": True},
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {
                    "enabled": True,
                    "mode":    "at_period_end",    # No se corta inmediatamente
                    "cancellation_reason": {"enabled": True, "options": ["too_expensive", "unused", "other"]},
                },
                "subscription_update": {
                    "enabled":        True,
                    "default_allowed_updates": ["price"],
                    "proration_behavior": "always_invoice",
                    "products": [],   # Se configura después con product_id reales
                },
            },
        )
        print(f"\n   ✓ Customer Portal configurado: {portal.id}")
    except stripe.error.InvalidRequestError as e:
        print(f"\n   ⚠ Customer Portal: {e.user_message}")


def _save_ids(product_id, overage_price_id, tax_rate_id):
    """Guarda los IDs en un archivo local para referencia."""
    data = {
        "stripe_product_id":       product_id,
        "stripe_overage_price_id": overage_price_id,
        "stripe_igic_tax_rate_id": tax_rate_id,
        "igic_pct":                IGIC_TAX_RATE_PCT,
        "overage_per_1k_tokens_usd": OVERAGE_PRICE_PER_1K_TOKENS_USD,
    }
    out = "stormrouter/stripe_metered_ids.json"
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n   💾 IDs guardados en {out}")


# ─── Utilidad: reportar uso metered para overage ─────────────────────────────

def report_overage_usage(subscription_item_id: str, tokens_over_quota: int):
    """
    Llama a esto desde el worker (o desde un job nocturno) para reportar
    el uso excess al final del mes.

    Args:
        subscription_item_id: El ID del subscription item del precio metered
        tokens_over_quota:    Tokens usados sobre la cuota en el mes
    """
    # Convertir a unidades de 1K tokens (redondear hacia arriba)
    units_1k = (tokens_over_quota + 999) // 1000
    if units_1k <= 0:
        return

    record = stripe.SubscriptionItem.create_usage_record(
        subscription_item_id,
        quantity=units_1k,
        action="set",   # "set" = total acumulado; "increment" = añadir
        timestamp="now",
    )
    return record


if __name__ == "__main__":
    main()
