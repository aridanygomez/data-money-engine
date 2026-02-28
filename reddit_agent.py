"""
reddit_agent.py — Growth Hacking Agent (LangGraph)
=====================================================
Monitorea r/LocalLLaMA y r/SaaS en busca de discusiones sobre costes de API LLM,
genera respuestas con valor real (tablas de nuestra BD) y menciona nuestra
herramienta solo si es estrictamente relevante.

Dependencias:
    pip install langgraph langchain-google-genai praw requests

Variables de entorno:
    GEMINI_API_KEY
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USERNAME
    REDDIT_PASSWORD
    CF_WORKER_URL          (https://llm-pricing-api.aridany-91.workers.dev)
    CF_API_KEY             (tu X-API-Key Pro, si tienes)

Ejecución:
    python reddit_agent.py            # modo dry-run (no postea)
    python reddit_agent.py --post     # postea respuestas aprobadas
"""

from __future__ import annotations

import os
import re
import json
import logging
import argparse
from datetime import datetime, timezone
from typing import Annotated, TypedDict, Literal

import praw
import requests
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_google_genai import ChatGoogleGenerativeAI

# ─── Config ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_agent")

WORKER_URL = os.getenv("CF_WORKER_URL", "https://llm-pricing-api.aridany-91.workers.dev")
CF_API_KEY  = os.getenv("CF_API_KEY", "")
SITE_URL    = "https://aridanygomez.github.io/data-money-engine"
PRICING_URL = f"{SITE_URL}/pricing.html"

# Subreddits y límite de posts por ciclo
TARGET_SUBREDDITS = ["LocalLLaMA", "SaaS"]
POST_LIMIT = 25          # posts recientes a escanear por subreddit
COMMENT_LIMIT = 10       # comentarios recientes a escanear por subreddit
MIN_RELEVANCE = 0.55     # umbral mínimo para generar respuesta
MAX_POSTS_PER_RUN = 3    # máximo de respuestas a redactar / postear por ejecución

# ─── Keyword scoring ─────────────────────────────────────────────────────────

# Pares (regex, peso) — suma >= MIN_RELEVANCE para procesar
KEYWORD_RULES: list[tuple[str, float]] = [
    # Costes directos
    (r"\bapi\s+cost[s]?\b",         0.30),
    (r"\btoken[s]?\s+cost",         0.30),
    (r"\bpricing\b",                0.25),
    (r"\bexpensive\b",              0.20),
    (r"\bcheap(?:er|est)?\b",       0.20),
    (r"\bsave\s+money\b",           0.25),
    (r"\bcost[s]?\s+(?:per|of)\b",  0.25),
    (r"\bbudget\b",                 0.15),
    (r"\bbilling\b",                0.20),
    (r"\binvoice\b",                0.15),

    # Comparación de modelos
    (r"\bvs\.?\s+(?:gpt|claude|gemini|llama)",  0.25),
    (r"\bcompare\s+(?:model|api|llm)",           0.25),
    (r"\bwhich\s+(?:model|llm|api)\b",           0.20),
    (r"\bcheapest\s+(?:model|llm|api)",          0.35),

    # Token efficiency
    (r"\btoken\s+(?:usage|limit|count)\b",  0.20),
    (r"\bcontext\s+window\b",               0.15),
    (r"\bprompt\s+(?:cost|caching)\b",      0.25),
    (r"\boutput\s+token",                   0.20),
    (r"\binput\s+token",                    0.20),

    # Modelos concretos (mención = más probable que estén comparando costes)
    (r"\bgpt-4o?\b",       0.10),
    (r"\bclaude\s*3",      0.10),
    (r"\bgemini\s*1\.5\b", 0.10),
    (r"\bdeepseek\b",      0.10),
    (r"\bllama\s*3",       0.10),
]

# Si el hilo ya tiene respuesta de nuestra cuenta → skipear
OUR_USERNAME = os.getenv("REDDIT_USERNAME", "")


# ─── State Schema ─────────────────────────────────────────────────────────────

class RedditPost(TypedDict):
    """Unidad mínima de contenido de Reddit."""
    id:          str
    subreddit:   str
    kind:        Literal["post", "comment"]  # post submission o comment
    title:       str                         # solo en posts
    body:        str
    url:         str
    author:      str
    score:       int
    created_utc: float
    permalink:   str


class DraftResponse(TypedDict):
    post:            RedditPost
    relevance_score: float
    pricing_context: list[dict]   # modelos de nuestra API relevantes al hilo
    draft:           str          # respuesta en Markdown
    should_mention_tool: bool
    quality_ok:      bool         # pasa el filtro anti-spam


class AgentState(TypedDict):
    """Estado completo del agente LangGraph."""

    # ── Fase 1: Monitoreo ──────────────────────────────────────────────────
    raw_posts:      list[RedditPost]   # todos los posts/comments recogidos
    filtered_posts: list[RedditPost]   # solo los relevantes (score >= threshold)

    # ── Fase 2: Proceso (post actual en curso) ─────────────────────────────
    queue:          list[RedditPost]   # cola de posts pendientes de procesar
    current_post:   RedditPost | None  # post que se está procesando ahora

    # ── Fase 3: Generación ────────────────────────────────────────────────
    pricing_context: list[dict]        # datos de precios de nuestra API
    relevance_score: float

    # ── Fase 4: Respuesta ─────────────────────────────────────────────────
    drafts:          list[DraftResponse]  # borradores generados
    approved:        list[DraftResponse]  # aprobados para postear
    posted_ids:      list[str]            # IDs ya respondidos (deduplicación)

    # ── Control ───────────────────────────────────────────────────────────
    dry_run:         bool
    errors:          list[str]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _score_text(text: str) -> float:
    """Devuelve un score de relevancia 0.0–1.0 basado en keyword rules."""
    if not text:
        return 0.0
    text_lower = text.lower()
    score = 0.0
    for pattern, weight in KEYWORD_RULES:
        if re.search(pattern, text_lower):
            score += weight
    return min(score, 1.0)


def _praw_client() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=f"LLMPricingBot/1.0 by u/{os.environ['REDDIT_USERNAME']}",
    )


def _fetch_pricing_data(keywords: list[str]) -> list[dict]:
    """
    Llama a nuestra API para obtener los N modelos más baratos.
    Enriquece con datos relevantes al contexto del post.
    """
    try:
        headers = {"Accept": "application/json"}
        if CF_API_KEY:
            headers["X-API-Key"] = CF_API_KEY

        resp = requests.get(
            f"{WORKER_URL}/api/v1/cheapest",
            params={"n": 10},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("cheapest", [])
    except Exception as e:
        log.warning(f"No se pudo obtener pricing data: {e}")
    return []


def _build_comparison_table(models: list[dict]) -> str:
    """Genera tabla Markdown de precios desde nuestra API."""
    if not models:
        return ""

    rows = []
    for m in models[:8]:  # máximo 8 filas para no ser spam
        name     = m.get("name", m.get("id", "?"))
        provider = m.get("provider", "?")
        prompt   = m.get("prompt_price_per_1m", 0) or 0
        complete = m.get("completion_price_per_1m", 0) or 0
        rows.append(f"| {provider} | {name} | ${prompt:.4f} | ${complete:.4f} |")

    header = (
        "| Provider | Model | Input ($/1M tokens) | Output ($/1M tokens) |\n"
        "|----------|-------|--------------------|-----------------------|"
    )
    return header + "\n" + "\n".join(rows)


# ─── LLM ──────────────────────────────────────────────────────────────────────

def _get_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        google_api_key=os.environ["GEMINI_API_KEY"],
        temperature=0.4,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# NODOS DEL GRAFO
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Nodo 1: fetch_posts ──────────────────────────────────────────────────────

def fetch_posts(state: AgentState) -> AgentState:
    """
    Recoge posts y comentarios recientes de TARGET_SUBREDDITS.
    Salta los hilos donde ya hemos respondido.
    """
    log.info("📡 Fetching Reddit posts...")
    reddit = _praw_client()
    already_replied = set(state.get("posted_ids", []))
    raw: list[RedditPost] = []

    for sub_name in TARGET_SUBREDDITS:
        subreddit = reddit.subreddit(sub_name)

        # — Submissions recientes —
        for submission in subreddit.new(limit=POST_LIMIT):
            if submission.id in already_replied:
                continue
            # Saltar si ya respondemos en este hilo
            if OUR_USERNAME:
                submission.comments.replace_more(limit=0)
                authors = {c.author.name for c in submission.comments.list() if c.author}
                if OUR_USERNAME in authors:
                    continue

            raw.append(RedditPost(
                id=submission.id,
                subreddit=sub_name,
                kind="post",
                title=submission.title,
                body=submission.selftext[:1500],
                url=f"https://reddit.com{submission.permalink}",
                author=str(submission.author),
                score=submission.score,
                created_utc=submission.created_utc,
                permalink=submission.permalink,
            ))

        # — Comentarios recientes (busca dudas directas) —
        for comment in subreddit.comments(limit=COMMENT_LIMIT):
            if comment.id in already_replied:
                continue
            if OUR_USERNAME and str(comment.author) == OUR_USERNAME:
                continue  # nuestros propios comentarios
            raw.append(RedditPost(
                id=comment.id,
                subreddit=sub_name,
                kind="comment",
                title="",
                body=comment.body[:1500],
                url=f"https://reddit.com{comment.permalink}",
                author=str(comment.author),
                score=comment.score,
                created_utc=comment.created_utc,
                permalink=comment.permalink,
            ))

    log.info(f"  Recogidos {len(raw)} items de {TARGET_SUBREDDITS}")
    return {**state, "raw_posts": raw}


# ─── Nodo 2: score_relevance ─────────────────────────────────────────────────

def score_relevance(state: AgentState) -> AgentState:
    """
    Filtra posts por score de relevancia.
    Combina título + cuerpo + limita duplicados de autor.
    """
    log.info("🔍 Scoring relevance...")
    filtered: list[RedditPost] = []
    seen_authors: dict[str, int] = {}  # author → nº de posts incluidos

    for post in state["raw_posts"]:
        combined = f"{post['title']} {post['body']}"
        score = _score_text(combined)

        # Limitar a 1 post por autor por ejecución (evitar seguir a trolls)
        author_count = seen_authors.get(post["author"], 0)
        if author_count >= 1:
            continue

        if score >= MIN_RELEVANCE:
            filtered.append(post)
            seen_authors[post["author"]] = author_count + 1
            log.info(
                f"  ✅ [{post['subreddit']}] score={score:.2f} — "
                f"{post['title'][:60] or post['body'][:60]}"
            )
        else:
            log.debug(f"  ❌ score={score:.2f} — {combined[:60]}")

    # Ordenar por score desc, limitar cola
    filtered.sort(key=lambda p: _score_text(f"{p['title']} {p['body']}"), reverse=True)
    queue = filtered[:MAX_POSTS_PER_RUN]

    log.info(f"  {len(filtered)} relevantes → encolando {len(queue)}")
    return {**state, "filtered_posts": filtered, "queue": queue}


# ─── Nodo 3: pick_next ────────────────────────────────────────────────────────

def pick_next(state: AgentState) -> AgentState:
    """Saca el siguiente post de la cola para procesarlo."""
    queue = list(state.get("queue", []))
    if not queue:
        return {**state, "current_post": None}
    current = queue.pop(0)
    log.info(f"⚙️  Procesando: {current['id']} ({current['subreddit']})")
    return {**state, "queue": queue, "current_post": current}


# ─── Nodo 4: fetch_context ────────────────────────────────────────────────────

def fetch_context(state: AgentState) -> AgentState:
    """
    Extrae keywords del post actual y trae precios de nuestra API.
    """
    post = state["current_post"]
    if not post:
        return state

    # Extraer modelos mencionados para contextualizar la respuesta
    text = f"{post['title']} {post['body']}".lower()
    keywords = []
    model_patterns = {
        "gpt-4o": ["gpt-4o", "gpt4o"],
        "claude-3-5-sonnet": ["claude", "sonnet"],
        "gemini-1.5-pro": ["gemini", "google"],
        "llama-3.1": ["llama", "meta"],
        "deepseek": ["deepseek"],
        "mistral": ["mistral"],
    }
    for model_key, patterns in model_patterns.items():
        if any(p in text for p in patterns):
            keywords.append(model_key)

    pricing = _fetch_pricing_data(keywords)
    relevance_score = _score_text(f"{post['title']} {post['body']}")

    log.info(f"  💰 {len(pricing)} modelos de precio obtenidos")
    return {**state, "pricing_context": pricing, "relevance_score": relevance_score}


# ─── Nodo 5: draft_response ──────────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un experto en costes de LLMs que ayuda en Reddit de forma genuina.

REGLAS:
1. Responde DIRECTAMENTE la pregunta del usuario con datos reales.
2. Incluye SIEMPRE una tabla comparativa en Markdown si los datos están disponibles.
3. Menciona nuestra herramienta (LLM Pricing Monitor) SOLO si:
   - El usuario pregunta explícitamente dónde ver precios actualizados, O
   - La mención añade valor real al contexto (no como spam).
4. Tono: Técnico pero accesible. Nada de auto-promoción descarada.
5. Longitud: 150-400 palabras. Reddit no es un blog.
6. NO uses frases como "Gran pregunta!" o intro vacía.
7. Cierra con una pregunta o insight útil, no con CTA agresivo.

Si decides mencionar la herramienta, úsala así (una sola vez):
> "If you want live alerts when any of these prices change: [LLM Pricing Monitor]({url}) — free price history, paid alerts from $4/mo."
""".format(url=PRICING_URL)


def draft_response(state: AgentState) -> AgentState:
    """
    Genera la respuesta con Gemini usando datos de nuestra API como contexto.
    """
    post = state["current_post"]
    if not post:
        return state

    pricing_table = _build_comparison_table(state.get("pricing_context", []))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    user_prompt = f"""
Reddit post from r/{post['subreddit']}:
TITLE: {post['title']}
BODY: {post['body']}

Today's LLM pricing data ({today}):
{pricing_table if pricing_table else "No pricing data available."}

Write a helpful Reddit reply (Markdown).
Score relevante del post: {state.get('relevance_score', 0):.2f}

Decide si mencionar la herramienta y devuelve JSON con este esquema:
{{
  "reply": "<markdown reply>",
  "should_mention_tool": <true|false>,
  "mention_reason": "<por qué sí o no>"
}}
"""

    llm = _get_llm()
    response = llm.invoke([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])

    # Parsear JSON del modelo
    raw = response.content.strip()
    # Quitar fences de código si existen
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: usar el texto como reply directo
        parsed = {
            "reply": raw,
            "should_mention_tool": False,
            "mention_reason": "JSON parse failed",
        }

    draft = DraftResponse(
        post=post,
        relevance_score=state.get("relevance_score", 0.0),
        pricing_context=state.get("pricing_context", []),
        draft=parsed.get("reply", ""),
        should_mention_tool=parsed.get("should_mention_tool", False),
        quality_ok=False,  # se evalúa en el siguiente nodo
    )

    log.info(
        f"  📝 Draft generado. Menciona herramienta: {draft['should_mention_tool']} "
        f"({parsed.get('mention_reason', '')})"
    )

    drafts = list(state.get("drafts", []))
    drafts.append(draft)
    return {**state, "drafts": drafts}


# ─── Nodo 6: quality_gate ────────────────────────────────────────────────────

# Patrones que indican spam o respuesta de baja calidad
SPAM_PATTERNS = [
    r"check out my",
    r"click here",
    r"buy now",
    r"limited offer",
    r"DM me",
    r"affiliate",
]

MIN_DRAFT_WORDS = 50
MAX_TOOL_MENTIONS = 1


def quality_gate(state: AgentState) -> AgentState:
    """
    Valida el borrador generado:
    - Longitud mínima
    - No spam
    - Máximo 1 mención de la herramienta
    - Tabla de precios presente si había datos
    """
    drafts = list(state.get("drafts", []))
    if not drafts:
        return state

    last = drafts[-1]
    draft_text = last["draft"].lower()
    issues = []

    # 1. Longitud mínima
    word_count = len(last["draft"].split())
    if word_count < MIN_DRAFT_WORDS:
        issues.append(f"Demasiado corto ({word_count} palabras)")

    # 2. Patrones de spam
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, draft_text, re.IGNORECASE):
            issues.append(f"Patrón spam detectado: '{pattern}'")

    # 3. Exceso de menciones de la herramienta
    tool_mentions = draft_text.count("llm pricing monitor")
    if tool_mentions > MAX_TOOL_MENTIONS:
        issues.append(f"Demasiadas menciones de la herramienta ({tool_mentions})")

    # 4. Tabla presente si había datos de precios
    has_data = bool(state.get("pricing_context"))
    has_table = "|" in last["draft"] and "---" in last["draft"]
    if has_data and not has_table:
        issues.append("Falta tabla de precios a pesar de tener datos")

    quality_ok = len(issues) == 0
    last = {**last, "quality_ok": quality_ok}

    if quality_ok:
        log.info("  ✅ Quality gate: PASSED")
    else:
        log.warning(f"  ❌ Quality gate: FAILED — {'; '.join(issues)}")

    drafts[-1] = last
    approved = list(state.get("approved", []))
    if quality_ok:
        approved.append(last)

    return {**state, "drafts": drafts, "approved": approved}


# ─── Nodo 7: post_to_reddit ──────────────────────────────────────────────────

def post_to_reddit(state: AgentState) -> AgentState:
    """
    En modo --post: publica los borradores aprobados.
    En dry-run: imprime a consola.
    """
    approved = state.get("approved", [])
    dry_run = state.get("dry_run", True)
    posted_ids = list(state.get("posted_ids", []))
    errors = list(state.get("errors", []))

    if not approved:
        log.info("ℹ️  Sin borradores aprobados para publicar.")
        return state

    if dry_run:
        log.info("=" * 60)
        log.info("🧪 DRY-RUN — Respuestas que se publicarían:")
        for d in approved:
            log.info(f"\n📌 [{d['post']['subreddit']}] {d['post']['url']}")
            log.info(f"{'─'*40}\n{d['draft']}\n{'─'*40}")
        return state

    # Modo real
    reddit = _praw_client()
    for d in approved:
        post = d["post"]
        try:
            target_id = f"t1_{post['id']}" if post["kind"] == "comment" else f"t3_{post['id']}"
            thing = reddit.comment(post["id"]) if post["kind"] == "comment" else reddit.submission(id=post["id"])
            thing.reply(d["draft"])
            posted_ids.append(post["id"])
            log.info(f"  ✅ Respuesta publicada en {post['url']}")
        except Exception as e:
            msg = f"Error al postear en {post['id']}: {e}"
            log.error(f"  ❌ {msg}")
            errors.append(msg)

    return {**state, "posted_ids": posted_ids, "errors": errors}


# ─── Nodo de control: ¿quedan posts? ─────────────────────────────────────────

def should_continue(state: AgentState) -> Literal["pick_next", "post"]:
    """Edge condicional: sigue procesando si hay cola, o pasa a postear."""
    queue = state.get("queue", [])
    if queue:
        return "pick_next"
    return "post"


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DEL GRAFO
# ═══════════════════════════════════════════════════════════════════════════════

def build_graph() -> StateGraph:
    """
    Grafo de estados:

       fetch_posts
           │
       score_relevance
           │
       pick_next ◄──────────────────────────────────────┐
           │                                             │
       fetch_context                                     │
           │                                         (loop)
       draft_response                                    │
           │                                             │
       quality_gate                                      │
           │                                             │
       should_continue ── "pick_next" ──────────────────┘
           │
       "post"
           │
       post_to_reddit
           │
          END
    """
    g = StateGraph(AgentState)

    # Añadir nodos
    g.add_node("fetch_posts",    fetch_posts)
    g.add_node("score_relevance", score_relevance)
    g.add_node("pick_next",      pick_next)
    g.add_node("fetch_context",  fetch_context)
    g.add_node("draft_response", draft_response)
    g.add_node("quality_gate",   quality_gate)
    g.add_node("post",           post_to_reddit)

    # Edges lineales
    g.set_entry_point("fetch_posts")
    g.add_edge("fetch_posts",    "score_relevance")
    g.add_edge("score_relevance", "pick_next")
    g.add_edge("pick_next",      "fetch_context")
    g.add_edge("fetch_context",  "draft_response")
    g.add_edge("draft_response", "quality_gate")
    g.add_edge("post",            END)

    # Edge condicional: ¿más posts en cola o ir a publicar?
    g.add_conditional_edges(
        "quality_gate",
        should_continue,
        {
            "pick_next": "pick_next",
            "post":      "post",
        },
    )

    return g.compile()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def load_posted_ids(path: str = "data/reddit_posted_ids.json") -> list[str]:
    """Carga IDs ya respondidos para deduplicar entre ejecuciones."""
    try:
        return json.loads(open(path).read())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_posted_ids(ids: list[str], path: str = "data/reddit_posted_ids.json") -> None:
    with open(path, "w") as f:
        json.dump(ids, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Reddit Growth Hacking Agent")
    parser.add_argument("--post", action="store_true", help="Publicar respuestas (default: dry-run)")
    parser.add_argument("--limit", type=int, default=MAX_POSTS_PER_RUN, help="Máx respuestas por ejecución")
    args = parser.parse_args()

    dry_run = not args.post

    if dry_run:
        log.info("🧪 Modo DRY-RUN — no se publicará nada")
    else:
        log.warning("🚀 Modo REAL — se publicarán respuestas en Reddit")

    graph = build_graph()

    initial_state: AgentState = {
        "raw_posts":      [],
        "filtered_posts": [],
        "queue":          [],
        "current_post":   None,
        "pricing_context": [],
        "relevance_score": 0.0,
        "drafts":         [],
        "approved":       [],
        "posted_ids":     load_posted_ids(),
        "dry_run":        dry_run,
        "errors":         [],
    }

    final_state = graph.invoke(initial_state)

    # Persistir IDs si realmente posteamos
    if not dry_run and final_state.get("posted_ids"):
        save_posted_ids(final_state["posted_ids"])
        log.info(f"💾 {len(final_state['posted_ids'])} IDs guardados")

    # Resumen
    log.info("\n" + "═" * 60)
    log.info(f"📊 RESUMEN DEL CICLO")
    log.info(f"   Posts escaneados: {len(final_state.get('raw_posts', []))}")
    log.info(f"   Relevantes:       {len(final_state.get('filtered_posts', []))}")
    log.info(f"   Borradores:       {len(final_state.get('drafts', []))}")
    log.info(f"   Aprobados:        {len(final_state.get('approved', []))}")
    log.info(f"   Errores:          {len(final_state.get('errors', []))}")

    if final_state.get("errors"):
        for err in final_state["errors"]:
            log.error(f"   ⚠️  {err}")


if __name__ == "__main__":
    main()
