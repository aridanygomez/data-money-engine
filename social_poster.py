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
TWITTER_ACCESS_SECRET       = os.getenv("TWITTER_ACCESS_SECRET", "")
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
        # Fallback: leer models.json directo
        models_file = DATA_DIR / "models.json"
    if not models_file.exists():
        print("⚠️  No hay datos de precios disponibles.")
        return []
    try:
        data = json.loads(models_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "top_models" in data:
            return data["top_models"]
        if isinstance(data, list):
            return data
    except Exception as e:
        print(f"⚠️  Error leyendo datos: {e}")
    return []


def get_cheapest_models(models: list[dict]) -> tuple[dict | None, dict | None]:
    """Retorna (más barato, segundo más barato) de los modelos con precio > 0."""
    paid = [m for m in models if float(m.get("prompt_price_per_m", m.get("pricing", {}).get("prompt", 0) if isinstance(m.get("pricing"), dict) else 0) or 0) > 0]
    paid.sort(key=lambda m: float(m.get("prompt_price_per_m", 0) or 0))
    return (paid[0] if len(paid) > 0 else None,
            paid[1] if len(paid) > 1 else None)


def get_most_expensive(models: list[dict]) -> dict | None:
    """Retorna el modelo más caro (bueno para comparativas de ahorro)."""
    paid = [m for m in models if float(m.get("prompt_price_per_m", 0) or 0) > 0]
    if not paid:
        return None
    paid.sort(key=lambda m: float(m.get("prompt_price_per_m", 0) or 0), reverse=True)
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

        # Tweet 3 — CTA con link
        tweet3_text = (
            f"📊 Full comparison + 300+ models ranked by real-time price:\n"
            f"{compare_url}\n\n"
            f"Bookmark this. Token deflation is accelerating in 2026. 🔖\n\n"
            f"#AI #LLM #MachineLearning #AITools #DevTools"
        )
        client.create_tweet(text=tweet3_text, in_reply_to_tweet_id=t2_id)

        print(f"✅ Twitter: hilo publicado (tweet ID {t1_id})")
        return True

    except Exception as e:
        print(f"❌ Twitter error: {e}")
        return False


# ─── REDDIT ──────────────────────────────────────────────────────────────────

def post_reddit(cheapest: dict, expensive: dict | None, compare_url: str) -> bool:
    """Publica en r/LocalLLaMA y r/MachineLearning."""
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
            user_agent=f"LLMPricingBot/1.0 by u/{REDDIT_USERNAME}",
        )

        name_a = cheapest.get("name", cheapest.get("id", "Unknown"))
        price_a = float(cheapest.get("prompt_price_per_m", 0) or 0)

        title = f"Token Deflation 2026: {name_a} now at ${price_a:.4f}/1M tokens — is API pricing in freefall?"

        body_lines = [
            f"I've been tracking LLM API prices daily via an open-source bot and today's data is wild.",
            f"",
            f"**Cheapest model right now:** {name_a} at **${price_a:.4f} per 1M input tokens**",
        ]
        if expensive:
            name_exp = expensive.get("name", expensive.get("id", "Unknown"))
            price_exp = float(expensive.get("prompt_price_per_m", 0) or 0)
            ratio = round(price_exp / price_a) if price_a > 0 else "∞"
            body_lines += [
                f"",
                f"**Comparison:** {name_exp} is **{ratio}x more expensive** at ${price_exp:.2f}/1M",
                f"",
                f"That's not a rounding error — that's an order-of-magnitude difference for similar quality tasks.",
            ]

        body_lines += [
            f"",
            f"**Real-time tracker (300+ models):** {compare_url}",
            f"",
            f"My question for this community: at what price point does it stop making sense to self-host? "
            f"If {name_a} is this cheap, is running your own GPU cluster still worth it in 2026?",
            f"",
            f"Happy to share the full dataset if anyone wants to dig into the numbers.",
        ]

        body = "\n".join(body_lines)

        # Postear en r/LocalLLaMA (más tolerante a discusiones de precios)
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
    results["reddit"] = post_reddit(cheapest, expensive, compare_url)

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
