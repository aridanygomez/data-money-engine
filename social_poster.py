"""
social_poster.py — Autonomous Social Media Publisher
Reads content_ideas.md + price data → posts to X, Reddit, LinkedIn
Runs automatically after bot_generador.py in GitHub Actions
"""

import os
import json
import re
import sys
import time
from pathlib import Path
from datetime import date

# ─── PATHS ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
CONTENT_IDEAS_FILE = DOCS_DIR / "content_ideas.md"
SITE_URL = "https://aridanygomez.github.io/data-money-engine"
TODAY = date.today().isoformat()

# ─── ENV SECRETS (set in GitHub Actions Secrets) ─────────────────────────────
TWITTER_API_KEY             = os.getenv("TWITTER_API_KEY", "")
TWITTER_API_SECRET          = os.getenv("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN        = os.getenv("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_SECRET       = os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "")
TWITTER_BEARER_TOKEN        = os.getenv("TWITTER_BEARER_TOKEN", "")

REDDIT_CLIENT_ID            = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET        = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME             = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD             = os.getenv("REDDIT_PASSWORD", "")

LINKEDIN_ACCESS_TOKEN       = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
LINKEDIN_PERSON_URN         = os.getenv("LINKEDIN_PERSON_URN", "")  # urn:li:person:XXXX


# ─── PARSER: Extraer modelos del JSON de datos ────────────────────────────────

def load_price_data() -> list[dict]:
    """Carga modelos con precios desde el JSON generado por el bot."""
    models_file = DATA_DIR / "site_data.json"
    if not models_file.exists():
        models_file = DATA_DIR / "models.json"
    if not models_file.exists():
        print("⚠️  No hay datos de precios disponibles.")
        return []
    try:
        data = json.loads(models_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # site_data.json tiene cheapest_paid + priority_models
            return (
                data.get("cheapest_paid") or
                data.get("priority_models") or
                data.get("top_models") or []
            )
    except Exception as e:
        print(f"⚠️  Error leyendo datos: {e}")
    return []


def get_cheapest_models(models: list[dict]) -> tuple[dict | None, dict | None]:
    """Retorna (más barato, segundo más barato) de los modelos con precio > 0."""
    def _p(m):
        return float(
            m.get("prompt_price_per_1m") or
            m.get("prompt_price_per_m") or
            (m.get("pricing", {}).get("prompt", 0) if isinstance(m.get("pricing"), dict) else 0)
            or 0
        )
    paid = [m for m in models if 0 < _p(m) < 999999]
    paid.sort(key=_p)
    return (paid[0] if len(paid) > 0 else None,
            paid[1] if len(paid) > 1 else None)


def get_most_expensive(models: list[dict]) -> dict | None:
    """Retorna el modelo más caro (bueno para comparativas de ahorro)."""
    def _p(m):
        return float(
            m.get("prompt_price_per_1m") or
            m.get("prompt_price_per_m") or 0
        )
    paid = [m for m in models if 0 < _p(m) < 999999]
    if not paid:
        return None
    paid.sort(key=_p, reverse=True)
    return paid[0]


def load_content_ideas() -> str:
    """Lee el archivo markdown de ideas de contenido."""
    if CONTENT_IDEAS_FILE.exists():
        return CONTENT_IDEAS_FILE.read_text(encoding="utf-8")
    return ""


def extract_price_drop(content: str) -> str:
    """Extrae el modelo con mayor bajada de precio del markdown."""
    match = re.search(r"(?i)(drop|baj|reduc|cheap)[^\n]*?([a-zA-Z0-9\-\.]+)\s*[\:\-]\s*\$?([\d\.]+)", content)
    if match:
        return match.group(0)
    return ""


def make_model_slug(model_id: str) -> str:
    """Convierte model ID a slug de URL (igual que bot_generador)."""
    return re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")


def make_compare_url(model_a: dict, model_b: dict) -> str:
    """Genera URL de comparativa."""
    slug_a = make_model_slug(model_a.get("id", "model-a"))
    slug_b = make_model_slug(model_b.get("id", "model-b"))
    return f"{SITE_URL}/compare/{slug_a}--vs--{slug_b}.html"


# ─── TWITTER / X ─────────────────────────────────────────────────────────────

def post_twitter_thread(cheapest: dict, runner_up: dict | None, compare_url: str) -> bool:
    """Publica un hilo en X con los datos del día."""
    try:
        import tweepy
    except ImportError:
        print("⚠️  tweepy no instalado. Saltando Twitter.")
        return False

    if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET]):
        print("⚠️  Faltan credenciales de Twitter (X). Saltando.")
        return False

    try:
        client = tweepy.Client(
            bearer_token=TWITTER_BEARER_TOKEN,
            consumer_key=TWITTER_API_KEY,
            consumer_secret=TWITTER_API_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_SECRET,
        )

        name_a = cheapest.get("name", cheapest.get("id", "Unknown"))
        price_a = float(cheapest.get("prompt_price_per_m", 0) or 0)
        
        # Tweet 1 — Gancho principal
        tweet1_text = (
            f"🧵 LLM Price Report — {TODAY}\n\n"
            f"The cheapest LLM right now costs just ${price_a:.4f}/1M tokens.\n\n"
            f"Here's what every developer should know about API pricing today 🧵"
        )
        r1 = client.create_tweet(text=tweet1_text)
        t1_id = r1.data["id"]
        time.sleep(2)

        # Tweet 2 — El más barato
        tweet2_text = (
            f"🥇 Cheapest model today:\n"
            f"→ {name_a}\n"
            f"→ Input: ${price_a:.4f}/1M tokens\n\n"
        )
        if runner_up:
            name_b = runner_up.get("name", runner_up.get("id", "Unknown"))
            price_b = float(runner_up.get("prompt_price_per_m", 0) or 0)
            savings_pct = round((1 - price_a / price_b) * 100) if price_b > 0 else 0
            tweet2_text += f"vs #{2} {name_b} at ${price_b:.4f}/1M → {savings_pct}% cheaper"
        
        r2 = client.create_tweet(text=tweet2_text, in_reply_to_tweet_id=t1_id)
        t2_id = r2.data["id"]
        time.sleep(2)

        # Tweet 3 — CTA con link + hashtags nicho AI
        tweet3_text = (
            f"📊 Full comparison — 300+ models ranked by real-time price:\n"
            f"{compare_url}\n\n"
            f"Bookmark it. Token deflation won't stop in 2026. 🔖\n\n"
            f"#LLM #AI #MachineLearning #GenerativeAI #OpenAI #Anthropic "
            f"#DeepSeek #AIEngineering #MLOps #DevTools #BuildInPublic"
        )
        client.create_tweet(text=tweet3_text, in_reply_to_tweet_id=t2_id)

        print(f"✅ Twitter: hilo publicado (tweet ID {t1_id})")
        return True

    except Exception as e:
        print(f"❌ Twitter error: {e}")
        return False


# ─── REDDIT ──────────────────────────────────────────────────────────────────

def _get_price(m: dict) -> float:
    """Extrae precio de prompt con compatibilidad de campos."""
    return float(
        m.get("prompt_price_per_1m") or
        m.get("prompt_price_per_m") or
        (m.get("pricing", {}).get("prompt", 0) if isinstance(m.get("pricing"), dict) else 0)
        or 0
    )


def build_reddit_table(models: list[dict], top_n: int = 7) -> str:
    """
    Construye una tabla Markdown nativa de Reddit con los N modelos más baratos.
    ESTRATEGIA ANTI-BAN: sin links en el body, datos puros como valor.
    """
    # Filtrar modelos con precio real positivo
    paid = [m for m in models if 0 < _get_price(m) < 999999]
    paid.sort(key=_get_price)
    top = paid[:top_n]

    lines = [
        f"| # | Model | Provider | Input $/1M | Context |",
        f"|---|-------|----------|-----------|---------|" 
    ]
    for i, m in enumerate(top, 1):
        name     = m.get("name", m.get("id", "?"))[:35]
        provider = m.get("provider", m.get("id","?").split("/")[0])[:15]
        price    = _get_price(m)
        ctx_k    = int(m.get("context_length", 0) / 1024)
        ctx_str  = f"{ctx_k}K" if ctx_k else "—"
        lines.append(f"| {i} | {name} | {provider} | ${price:.4f} | {ctx_str} |")

    return "\n".join(lines)


def post_reddit(models: list[dict], cheapest: dict, expensive: dict | None, compare_url: str) -> bool:
    """
    Publica en r/LocalLLaMA usando la estrategia 'Oráculo de Datos':
    - Tabla nativa Markdown (sin links directos en el body)
    - Link sutil al final como «fuente» con texto, no URL cruda
    - Pregunta abierta para fomentar debate orgánico
    - El link real va en el perfil del usuario, no en el post
    """
    try:
        import praw
    except ImportError:
        print("⚠️  praw no instalado. Saltando Reddit.")
        return False

    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        print("⚠️  Faltan credenciales de Reddit. Saltando.")
        return False

    try:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            username=REDDIT_USERNAME,
            password=REDDIT_PASSWORD,
            user_agent=f"DataResearchBot/1.0 by u/{REDDIT_USERNAME}",
        )

        name_a  = cheapest.get("name", cheapest.get("id", "Unknown"))
        price_a = _get_price(cheapest)

        # Tabla nativa con top 7 modelos
        table = build_reddit_table(models, top_n=7)

        # Contexto de precio comparativo (sin links, solo datos)
        context_block = ""
        if expensive:
            name_exp  = expensive.get("name", expensive.get("id", "Unknown"))
            price_exp = _get_price(expensive)
            if price_a > 0 and price_exp > 0:
                ratio   = round(price_exp / price_a)
                saving  = round((1 - price_a / price_exp) * 100)
                context_block = (
                    f"\n**Cost gap today:** {name_exp} costs **{ratio}x more** than {name_a} "
                    f"for the same prompt volume — a {saving}% overpay if you're not benchmarking regularly.\n"
                )

        # Título que genera debate (sin ventas)
        title = (
            f"I scraped OpenRouter pricing for all {len(models)} models today "
            f"— here's the cheapest 7 for RAG/agents [{TODAY}]"
        )

        body = "\n".join([
            f"Been running a daily scraper on OpenRouter's API to track price changes. "
            f"Thought the data might be useful here since people ask about this a lot.\n",

            f"**Today's cheapest models by input price ($/1M tokens):**\n",

            table,

            context_block,

            f"\n**Methodology:** prices pulled directly from OpenRouter's public `/api/v1/models` endpoint, "
            f"no affiliation. Context window and pricing update in real-time so these numbers are from today.",

            f"\n**Question for the community:** at what $/1M threshold does it make sense to "
            f"switch from a frontier model to one of these cheaper options for production RAG? "
            f"I've been testing {name_a} for summarization and the quality is surprisingly solid.",

            f"\n---",
            f"*Data source: I maintain a price tracker — link in my profile if anyone wants the full table.*",
        ])

        subreddit = reddit.subreddit("LocalLLaMA")
        submission = subreddit.submit(title=title, selftext=body)
        print(f"✅ Reddit: post publicado → https://reddit.com{submission.permalink}")
        return True

    except Exception as e:
        print(f"❌ Reddit error: {e}")
        return False


# ─── LINKEDIN ─────────────────────────────────────────────────────────────────

def post_linkedin(cheapest: dict, models_count: int) -> bool:
    """Publica un post profesional en LinkedIn via API v2."""
    if not all([LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_URN]):
        print("⚠️  Faltan credenciales de LinkedIn. Saltando.")
        return False

    try:
        import requests

        name_a = cheapest.get("name", cheapest.get("id", "Unknown"))
        price_a = float(cheapest.get("prompt_price_per_m", 0) or 0)

        post_text = (
            f"📉 Token Deflation is the story of 2026.\n\n"
            f"I've been running an open-source bot that tracks {models_count}+ LLM APIs in real-time. "
            f"Today's most affordable model: {name_a} at ${price_a:.4f} per million tokens.\n\n"
            f"Six months ago, the same quality of inference cost 10x more.\n\n"
            f"What this means for businesses:\n"
            f"→ AI integration costs are dropping faster than cloud compute did in 2015\n"
            f"→ The ROI threshold for AI projects is shrinking every week\n"
            f"→ Teams not tracking these prices are leaving real money on the table\n\n"
            f"I built a free public tracker so teams can benchmark and optimize their AI spend:\n"
            f"{SITE_URL}\n\n"
            f"The winners in AI 2026 won't just be those who use AI — they'll be those who use it cheapest.\n\n"
            f"#ArtificialIntelligence #MachineLearning #LLM #AI #TechLeadership #CostOptimization"
        )

        payload = {
            "author": LINKEDIN_PERSON_URN,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": post_text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }

        headers = {
            "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

        r = requests.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers=headers,
            json=payload,
            timeout=15,
        )

        if r.status_code in (200, 201):
            post_id = r.headers.get("X-RestLi-Id", "unknown")
            print(f"✅ LinkedIn: post publicado (ID: {post_id})")
            return True
        else:
            print(f"❌ LinkedIn error {r.status_code}: {r.text[:200]}")
            return False

    except Exception as e:
        print(f"❌ LinkedIn error: {e}")
        return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n📣 Social Poster — {TODAY}")
    print("=" * 50)

    # Cargar datos
    models = load_price_data()
    if not models:
        print("❌ Sin datos de modelos. Abortando.")
        sys.exit(0)  # exit 0 para no romper el workflow

    cheapest, runner_up = get_cheapest_models(models)
    expensive = get_most_expensive(models)

    if not cheapest:
        print("❌ No se encontraron modelos con precio. Abortando.")
        sys.exit(0)

    compare_url = make_compare_url(cheapest, runner_up) if runner_up else f"{SITE_URL}/compare/"
    
    print(f"📊 Cheapest: {cheapest.get('name', cheapest.get('id'))}")
    print(f"📊 Compare URL: {compare_url}")
    print(f"📊 Total models: {len(models)}")
    print()

    results = {}

    # Twitter
    print("[1/3] Publicando en X (Twitter)...")
    results["twitter"] = post_twitter_thread(cheapest, runner_up, compare_url)

    # Reddit
    print("[2/3] Publicando en Reddit...")
    results["reddit"] = post_reddit(models, cheapest, expensive, compare_url)

    # LinkedIn
    print("[3/3] Publicando en LinkedIn...")
    results["linkedin"] = post_linkedin(cheapest, len(models))

    # Resumen
    print()
    print("=" * 50)
    ok = sum(1 for v in results.values() if v)
    print(f"✅ Social posting completado: {ok}/3 plataformas")
    for platform, success in results.items():
        icon = "✅" if success else "⏭️ " 
        print(f"  {icon} {platform.capitalize()}")


if __name__ == "__main__":
    main()
