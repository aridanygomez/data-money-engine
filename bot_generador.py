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
import sys
import re
import json
import time
import requests
from datetime import datetime, date
from pathlib import Path

# Forzar UTF-8 en Windows (arregla emojis en consola cp1252)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─── Paths ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "docs"
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
SITE_URL = "https://aridanygomez.github.io/data-money-engine"
SITE_NAME = "LLM Pricing — Real-Time API Cost Comparison"

# ─── Niches ───────────────────────────────────────────────────────────────────

NICHES: dict[str, dict] = {
    "chatbots": {
        "label": "Chatbots & Conversational AI",
        "slug": "chatbots",
        "icon": "💬",
        "desc": "Best cost-efficient models for multi-turn conversation and virtual assistants.",
        "long_desc": "Chatbots need low latency, consistent quality, and low cost at scale. These models offer the best balance of price and quality for conversational use cases.",
        "filter": lambda m: m["context_length"] >= 8000 and not m["is_free"] and m["total_price_per_1m"] > 0,
        "sort_key": lambda m: m["total_price_per_1m"],
        "sort_label": "cheapest per 1M tokens",
    },
    "rag": {
        "label": "RAG & Document Q&A",
        "slug": "rag",
        "icon": "📚",
        "desc": "Models with large context windows for document retrieval and Q&A systems.",
        "long_desc": "RAG systems need large context windows to ingest retrieved documents. These models offer maximum context per dollar, minimizing chunking complexity.",
        "filter": lambda m: m["context_length"] >= 64000 and not m["is_free"] and m["total_price_per_1m"] > 0,
        "sort_key": lambda m: -(m["context_length"] / max(m["total_price_per_1m"], 0.001)),
        "sort_label": "best context-per-dollar",
    },
    "coding": {
        "label": "Code Generation & Review",
        "slug": "coding",
        "icon": "⚙️",
        "desc": "Top models for code completion, debugging, and technical documentation.",
        "long_desc": "Code generation requires strong reasoning and large context. These models are trusted by engineering teams for automated coding tasks at the lowest API cost.",
        "filter": lambda m: m["context_length"] >= 16000 and not m["is_free"] and m["total_price_per_1m"] > 0,
        "sort_key": lambda m: m["total_price_per_1m"],
        "sort_label": "cheapest per 1M tokens",
    },
    "long-context": {
        "label": "Long-Context Processing",
        "slug": "long-context",
        "icon": "📜",
        "desc": "Models able to process massive documents, codebases, or entire books in one call.",
        "long_desc": "Long-context processing is essential for legal documents, research papers, and large codebases. These models offer the largest windows at competitive prices.",
        "filter": lambda m: m["context_length"] >= 100_000,
        "sort_key": lambda m: -m["context_length"],
        "sort_label": "longest context window",
    },
    "high-volume": {
        "label": "High-Volume Batch Processing",
        "slug": "high-volume",
        "icon": "⚡",
        "desc": "Cheapest models for large-scale data pipelines, classification, and extraction.",
        "long_desc": "When processing millions of tokens daily, cost is everything. These are the most cost-effective paid models for batch workloads like classification, extraction, and summarization.",
        "filter": lambda m: not m["is_free"] and 0 < m["total_price_per_1m"] < 2.0,
        "sort_key": lambda m: m["total_price_per_1m"],
        "sort_label": "cheapest per 1M tokens",
    },
    "enterprise": {
        "label": "Enterprise & Production APIs",
        "slug": "enterprise",
        "icon": "🏢",
        "desc": "Reliable, well-documented models from major providers for production workloads.",
        "long_desc": "Enterprise teams need SLAs, compliance, and stability. These models come from providers with proven uptime, SOC 2 compliance, and enterprise support tiers.",
        "filter": lambda m: m["provider"] in {"openai", "anthropic", "google"} and not m["is_free"] and m["total_price_per_1m"] > 0,
        "sort_key": lambda m: m["total_price_per_1m"],
        "sort_label": "cheapest enterprise option",
    },
}

# ─── HTML Templates ───────────────────────────────────────────────────────────

# Logo SVG vectorial (inline, no depende de red)
_LOGO_SVG = '<svg viewBox="0 0 24 24" width="22" height="22" fill="#3b82f6" aria-hidden="true"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>'

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Geist+Mono:wght@400;600&display=swap');
:root {
  --bg: #09090b; --surface: #111113; --card: #18181b; --border: #27272a;
  --accent: #3b82f6; --accent-hover: #2563eb;
  --success: #10b981; --success-dim: rgba(16,185,129,0.12);
  --danger: #f87171;
  --text: #fafafa; --text-muted: #71717a; --text-sub: #a1a1aa;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.6; -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); text-decoration: underline; }

/* ── Nav ── */
.nav {
  position: sticky; top: 0; z-index: 50;
  background: rgba(9,9,11,0.75); backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 0 2rem; height: 60px;
  display: flex; justify-content: space-between; align-items: center;
}
.nav-logo {
  display: flex; align-items: center; gap: 8px;
  font-weight: 800; color: #fff; text-decoration: none;
  letter-spacing: -0.04em; font-size: 1.05rem;
}
.nav-logo:hover { text-decoration: none; color: #fff; }
.nav-links { display: flex; gap: 24px; font-size: 0.875rem; color: var(--text-sub); }
.nav-links a { color: var(--text-sub); }
.nav-links a:hover { color: var(--text); text-decoration: none; }
.nav-cta {
  font-size: 0.8rem; font-weight: 600;
  background: var(--accent); color: #fff;
  padding: 6px 14px; border-radius: 6px;
}
.nav-cta:hover { background: var(--accent-hover); text-decoration: none; color: #fff; }

/* ── Hero ── */
.hero { text-align: center; padding: 5rem 1rem 3.5rem; }
.hero-badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.3);
  color: #93c5fd; padding: 4px 12px; border-radius: 99px; margin-bottom: 1.5rem;
}
.hero-badge::before { content: ''; width: 6px; height: 6px; background: #3b82f6; border-radius: 50%; }
h1 {
  font-size: clamp(2.2rem, 5vw, 3.8rem); font-weight: 800;
  letter-spacing: -0.05em; line-height: 1.1;
  background: linear-gradient(180deg, #fff 0%, #71717a 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 1rem;
}
h2 { font-size: 1.4rem; font-weight: 700; color: #fff; margin: 2.5rem 0 1rem; letter-spacing: -0.03em; }
h3 { font-size: 1.05rem; font-weight: 600; color: #e4e4e7; margin: 1.25rem 0 0.5rem; }
.subtitle { color: var(--text-muted); font-size: 1.1rem; max-width: 520px; margin: 0 auto 3rem; }

/* ── Layout ── */
.main { max-width: 1120px; margin: 0 auto; padding: 0 1.5rem 5rem; }

/* ── Stat Cards ── */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2.5rem; }
.stat-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 1.25rem 1.5rem;
  transition: border-color 0.2s;
}
.stat-card:hover { border-color: #3f3f46; }
.stat-val { font-size: 1.75rem; font-weight: 800; display: block; color: #fff; letter-spacing: -0.04em; }
.stat-val.green { color: var(--success); }
.stat-val.blue { color: var(--accent); }
.stat-lbl { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; margin-top: 2px; }

/* ── Table ── */
.table-wrap {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden; margin-bottom: 2rem;
}
table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
thead { background: var(--surface); }
th {
  padding: 12px 16px; text-align: left;
  font-size: 0.7rem; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.06em;
  cursor: pointer; user-select: none; white-space: nowrap;
  border-bottom: 1px solid var(--border);
}
th[data-col]:hover { color: var(--text-sub); }
th[data-col]:hover::after { content: ' ↕'; }
td { padding: 11px 16px; border-bottom: 1px solid #1c1c1f; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.02); }
.price-in { color: var(--success); font-family: 'Geist Mono', monospace; font-weight: 600; }
.price-out { color: #60a5fa; font-family: 'Geist Mono', monospace; }
.price-tot { color: #fff; font-family: 'Geist Mono', monospace; font-weight: 700; }
.model-link { color: var(--text); font-weight: 500; }
.model-link:hover { color: var(--accent); text-decoration: none; }
.provider { color: var(--text-muted); font-size: 0.78rem; }
.ctx { color: var(--text-sub); font-family: 'Geist Mono', monospace; font-size: 0.82rem; }

/* ── Badges ── */
.badge-free {
  background: var(--success-dim); color: var(--success);
  border: 1px solid rgba(16,185,129,0.3);
  padding: 2px 8px; border-radius: 99px;
  font-size: 0.7rem; font-weight: 700;
}
.badge-cheap {
  background: rgba(59,130,246,0.1); color: #93c5fd;
  border: 1px solid rgba(59,130,246,0.25);
  padding: 2px 8px; border-radius: 99px;
  font-size: 0.7rem; font-weight: 600;
}
.badge-winner {
  background: var(--success-dim); color: var(--success);
  border: 1px solid rgba(16,185,129,0.3);
  padding: 2px 8px; border-radius: 99px;
  font-size: 0.72rem; font-weight: 700;
}

/* ── CTA ── */
.cta {
  background: linear-gradient(135deg, rgba(59,130,246,0.08) 0%, transparent 100%);
  border: 1px solid rgba(59,130,246,0.25);
  border-radius: 16px; padding: 2.5rem 2rem;
  text-align: center; margin: 2.5rem 0;
}
.cta h2 { margin-top: 0; }
.cta p { color: var(--text-muted); margin: 0.75rem 0 1.5rem; }
.btn {
  display: inline-block; background: var(--accent); color: #fff;
  font-weight: 700; padding: 10px 24px;
  border-radius: 8px; font-size: 0.9rem;
  transition: background 0.15s, transform 0.1s;
}
.btn:hover { background: var(--accent-hover); text-decoration: none; color: #fff; transform: translateY(-1px); }

/* ── Compare cards ── */
.compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin: 1.5rem 0; }
.compare-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 16px; padding: 1.75rem;
  position: relative; overflow: hidden;
}
.compare-card.winner {
  border-color: rgba(16,185,129,0.4);
  background: linear-gradient(135deg, rgba(16,185,129,0.06) 0%, var(--card) 100%);
}
.compare-card.winner::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--success), transparent);
}
.vs-divider {
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 0.85rem; color: var(--text-muted);
  letter-spacing: 0.1em; margin: 0.5rem 0;
}
.price-big {
  font-size: 2.2rem; font-weight: 800;
  font-family: 'Geist Mono', monospace;
  letter-spacing: -0.04em; color: #fff;
}
.price-big.cheaper { color: var(--success); }
.scenario-table { width: 100%; border-collapse: collapse; font-size: 0.875rem; margin-top: 1.5rem; }
.scenario-table th {
  background: var(--surface); padding: 10px 14px;
  text-align: right; font-size: 0.7rem; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.06em; cursor: default;
}
.scenario-table th:first-child { text-align: left; }
.scenario-table td {
  padding: 10px 14px; border-top: 1px solid var(--border);
  text-align: right; font-family: 'Geist Mono', monospace; font-size: 0.85rem;
}
.scenario-table td:first-child { text-align: left; color: var(--text-sub); font-family: inherit; }
.win { color: var(--success); font-weight: 700; }
.loss { color: var(--text-muted); }

/* ── Calculator widget ── */
.calc-widget {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 2rem; margin: 2rem 0;
}
.calc-widget h2 { margin-top: 0; }
.calc-label { display: block; font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.calc-select {
  width: 100%; background: var(--surface); border: 1px solid var(--border);
  color: var(--text); padding: 8px 12px; border-radius: 8px;
  font-size: 0.875rem; outline: none;
}
.calc-select:focus { border-color: var(--accent); }
.calc-input {
  flex: 1; background: var(--surface); border: 1px solid var(--border);
  color: var(--text); padding: 8px 12px; border-radius: 8px;
  font-size: 0.875rem; outline: none; min-width: 0;
}
.calc-input:focus { border-color: var(--accent); }
.calc-results {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1rem; margin-top: 1.5rem;
}
.calc-result-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 1rem 1.25rem;
}
.calc-result-card.winner { border-color: rgba(16,185,129,0.4); }
.calc-result-card.highlight {
  border-color: rgba(59,130,246,0.4); background: rgba(59,130,246,0.05);
}
.calc-result-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 4px; }
.calc-result-val { font-size: 1.5rem; font-weight: 800; font-family: 'Geist Mono', monospace; color: #fff; letter-spacing: -0.04em; }
.calc-result-val.green { color: var(--success); }
.calc-savings-pct { font-size: 0.8rem; font-weight: 600; margin-top: 2px; }

/* ── Top 5 niche cards ── */
.top5-list { display: flex; flex-direction: column; gap: 1rem; margin: 1.5rem 0; }
.top5-card {
  display: flex; align-items: flex-start; gap: 1.25rem;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 1.5rem;
}
.top5-card.winner { border-color: rgba(16,185,129,0.4); }
.top5-rank { font-size: 1.75rem; flex-shrink: 0; line-height: 1; padding-top: 2px; }
.top5-body { flex: 1; min-width: 0; }
.top5-name { font-size: 1.05rem; font-weight: 600; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.top5-name a { color: var(--text); }
.top5-name a:hover { color: var(--accent); }
.top5-desc { color: var(--text-muted); font-size: 0.85rem; margin: 4px 0 8px; line-height: 1.5; }
.top5-meta { font-size: 0.82rem; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.top5-price { text-align: right; flex-shrink: 0; }
@media (max-width: 660px) {
  .top5-card { flex-wrap: wrap; }
  .top5-price { width: 100%; text-align: left; border-top: 1px solid var(--border); padding-top: .75rem; margin-top: .5rem; }
  .calc-results { grid-template-columns: 1fr; }
}

/* ── Misc ── */
.breadcrumb { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 1.5rem; }
.breadcrumb a { color: var(--text-muted); }
.breadcrumb a:hover { color: var(--text-sub); }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 1rem 0; }
.tag {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 3px 10px;
  font-size: 0.78rem; color: var(--text-muted);
}
.change-up { color: var(--danger); }
.change-down { color: var(--success); }
.desc { color: var(--text-sub); line-height: 1.7; margin: 1rem 0; }

/* ── Footer ── */
footer {
  text-align: center; color: var(--text-muted); font-size: 0.8rem;
  padding: 3rem 1rem; border-top: 1px solid var(--border); margin-top: 3rem;
}
footer a { color: var(--text-muted); }
footer a:hover { color: var(--text-sub); }

/* ── Responsive ── */
@media (max-width: 660px) {
  .nav { padding: 0 1rem; }
  .compare-grid { grid-template-columns: 1fr; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
}
"""  # noqa: E501

_SORT_JS = """
<script>
function sortTable(table, col, asc) {
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    const av = a.cells[col].dataset.val || a.cells[col].innerText;
    const bv = b.cells[col].dataset.val || b.cells[col].innerText;
    return asc ? (isNaN(av) ? av.localeCompare(bv) : av - bv)
               : (isNaN(av) ? bv.localeCompare(av) : bv - av);
  });
  rows.forEach(r => tbody.appendChild(r));
}
document.querySelectorAll('th[data-col]').forEach(th => {
  let asc = true;
  th.addEventListener('click', () => {
    sortTable(th.closest('table'), +th.dataset.col, asc = !asc);
  });
});
</script>
"""

def _html(title: str, body: str, desc: str = "", canonical: str = "") -> str:
    canon_tag = f'<link rel="canonical" href="{canonical}" />' if canonical else ""
    og_desc = desc or title
    url = canonical or SITE_URL
    schema = json.dumps({
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": title,
        "description": og_desc[:155],
        "url": url,
        "datePublished": TODAY,
        "dateModified": TODAY,
        "author": {"@type": "Organization", "name": "LLM Pricing Engine", "url": SITE_URL},
        "publisher": {"@type": "Organization", "name": "LLM Pricing Engine", "url": SITE_URL},
        "image": f"{SITE_URL}/og-image.svg",
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
    }, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="google-site-verification" content="google986f63de7999948a" />
<title>{title}</title>
<meta name="description" content="{og_desc[:155]}" />
{canon_tag}
<meta property="og:title" content="{title}" />
<meta property="og:description" content="{og_desc[:155]}" />
<meta property="og:type" content="website" />
<meta property="og:url" content="{url}" />
<meta property="og:image" content="{SITE_URL}/og-image.svg" />
<meta property="og:site_name" content="LLM Pricing" />
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="{title}" />
<meta name="twitter:description" content="{og_desc[:155]}" />
<meta name="twitter:image" content="{SITE_URL}/og-image.svg" />
<link rel="sitemap" href="{SITE_URL}/sitemap.xml" />
<script type="application/ld+json">{schema}</script>
<style>{CSS}</style>
</head>
<body>
<nav class="nav">
  <a class="nav-logo" href="{SITE_URL}/">
    {_LOGO_SVG}
    LLM Pricing
  </a>
  <div class="nav-links">
    <a href="{SITE_URL}/">Models</a>
    <a href="{SITE_URL}/compare/">Compare</a>
    <a href="{SITE_URL}/for/">Use Cases</a>
    <a href="{SITE_URL}/providers/">Providers</a>
    <a href="{SITE_URL}/pricing.html" style="color:#a78bfa">Get Alerts</a>
    <a class="nav-cta" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter →</a>
  </div>
</nav>
<div class="main">
{body}
</div>
<footer>
  <p style="margin-bottom:6px">LLM Pricing — Real-time API cost comparison for ML engineers &amp; developers</p>
  <p>Data from <a href="https://openrouter.ai">OpenRouter</a> · Updated daily ·
  Route automatically with <a href="{STORMROUTER_URL}">StormRouter</a></p>
  <p style="margin-top:8px;opacity:.5">Last updated: {TODAY}</p>
</footer>
{_SORT_JS}
</body>
</html>"""

def _fmt(price: float) -> str:
    if price == 0:
        return "Free"
    if price < 0.001:
        return f"${price:.6f}"
    if price < 0.1:
        return f"${price:.4f}"
    return f"${price:.3f}"


def _monthly(price_per_1m: float, tokens: int) -> str:
    if price_per_1m == 0:
        return "$0.00"
    c = (price_per_1m / 1_000_000) * tokens
    return f"${c:,.2f}" if c >= 0.01 else "<$0.01"


def _calc_html(models: list[dict], preselected_id: str = "") -> str:
    """Widget de calculadora de ahorro anual — se incrusta en model/compare/niche pages."""
    paid = [m for m in models if not m["is_free"] and m["total_price_per_1m"] > 0]
    paid.sort(key=lambda m: m["total_price_per_1m"])
    options = "".join(
        f'<option value="{m["id"]}" {"selected" if m["id"] == preselected_id else ""}>{m["name"]} — ${m["total_price_per_1m"]:.4f}/1M</option>'
        for m in paid[:50]
    )
    models_json = json.dumps([
        {"id": m["id"], "name": m["name"], "total": m["total_price_per_1m"], "is_free": m["is_free"]}
        for m in paid[:50]
    ])
    return f"""<div class="calc-widget">
  <h2>💰 Annual Savings Calculator</h2>
  <p style="color:var(--text-muted);font-size:0.9rem;margin-bottom:1.5rem">Pick your current model and monthly token volume — see how much you'd save by switching to the cheapest alternative.</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.5rem">
    <div>
      <label class="calc-label">Your current model</label>
      <select id="calc-current" class="calc-select" onchange="runCalc()">{options}</select>
    </div>
    <div>
      <label class="calc-label">Monthly token usage</label>
      <div style="display:flex;gap:8px">
        <input id="calc-tokens" class="calc-input" type="number" value="10" min="0.01" step="0.1" oninput="runCalc()" />
        <select id="calc-unit" class="calc-select" style="width:82px" onchange="runCalc()">
          <option value="M" selected>M</option>
          <option value="K">K</option>
          <option value="B">B</option>
        </select>
      </div>
    </div>
  </div>
  <div class="calc-results">
    <div class="calc-result-card">
      <div class="calc-result-label">Current monthly cost</div>
      <div class="calc-result-val" id="calc-current-cost">—</div>
    </div>
    <div class="calc-result-card winner">
      <div class="calc-result-label">With cheapest alternative</div>
      <div class="calc-result-val green" id="calc-cheapest-cost">—</div>
      <div style="font-size:0.78rem;color:var(--text-muted);margin-top:4px" id="calc-cheapest-name">—</div>
    </div>
    <div class="calc-result-card highlight">
      <div class="calc-result-label">Annual savings</div>
      <div class="calc-result-val green" id="calc-savings">—</div>
      <div class="calc-savings-pct" id="calc-savings-pct"></div>
    </div>
  </div>
  <script>
  (function(){{
    var MD={models_json};
    function fmt(n){{return n>=0.01?'$'+n.toFixed(2):'<$0.01';}}
    function run(){{
      var t=parseFloat(document.getElementById('calc-tokens').value)||0;
      var mu={{K:1e3,M:1e6,B:1e9}}[document.getElementById('calc-unit').value]||1e6;
      var tot=t*mu;
      var cid=document.getElementById('calc-current').value;
      var cur=MD.find(function(m){{return m.id===cid;}});
      var paid=MD.filter(function(m){{return !m.is_free&&m.total>0;}}).sort(function(a,b){{return a.total-b.total;}});
      var cheap=paid[0];
      if(!cur||!cheap)return;
      var cc=(cur.total/1e6)*tot;
      var chc=(cheap.total/1e6)*tot;
      var sav=Math.max(0,cc-chc);
      var pct=cc>0?Math.round(sav/cc*100):0;
      document.getElementById('calc-current-cost').textContent=fmt(cc)+'/mo';
      document.getElementById('calc-cheapest-cost').textContent=fmt(chc)+'/mo';
      document.getElementById('calc-cheapest-name').textContent=cheap.name;
      document.getElementById('calc-savings').textContent=fmt(sav*12)+'/yr';
      var pe=document.getElementById('calc-savings-pct');
      pe.textContent=pct>0?pct+'% savings':'Already using cheapest';
      pe.style.color=pct>0?'var(--success)':'var(--text-muted)';
    }}
    window.runCalc=run; run();
  }})();
  </script>
</div>"""


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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
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


# ─── Step 6: Push data to Cloudflare D1 (micro-SaaS backbone) ───────────────

def push_to_d1(models: list[dict], price_changes: list[dict]) -> bool:
    """
    Envia los precios frescos al Cloudflare Worker (/internal/sync).
    El Worker hace upsert en D1, registra historial y dispara alertas.
    Requiere env vars: CF_WORKER_URL, CF_INTERNAL_SECRET
    """
    worker_url = os.environ.get("CF_WORKER_URL", "")
    secret = os.environ.get("CF_INTERNAL_SECRET", "")

    if not worker_url or not secret:
        print("  [D1] CF_WORKER_URL / CF_INTERNAL_SECRET no configurados — skip")
        return False

    print(f"[6/6] Sincronizando {len(models)} modelos con Cloudflare D1...")
    try:
        resp = requests.post(
            f"{worker_url.rstrip('/')}/internal/sync",
            headers={"X-Internal-Secret": secret, "Content-Type": "application/json"},
            json={"models": models, "date": TODAY},
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"  ✅ D1 sync OK — {result.get('models_synced')} modelos, "
              f"{result.get('price_changes')} cambios, "
              f"{result.get('alerts_fired')} alertas disparadas")
        return True
    except Exception as e:
        print(f"  ⚠️  D1 sync failed (no crítico): {e}")
        return False


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

    # 5. Site data JSON
    generate_site_data(models, descriptions)

    # 6. Generate static HTML site
    pages_index   = generate_index_html(models, price_changes, descriptions)
    pages_models  = generate_model_pages(models, descriptions)
    pages_compare = generate_comparison_pages(models, comparisons)
    pages_niches  = generate_niche_pages(models, descriptions)
    pages_providers = generate_provider_pages(models, descriptions)
    generate_sitemap(models, comparisons)
    generate_static_assets(len(models), len(comparisons))
    total_pages = pages_index + pages_models + pages_compare + pages_niches + pages_providers
    log_entry["html_pages_generated"] = total_pages

    # Log final
    elapsed = round(time.time() - start, 1)
    log_entry["success"] = True
    log_entry["duration_seconds"] = elapsed
    _save_log(log_entry)

    print("=" * 50)
    print(f"✅ Bot completado en {elapsed}s")
    print(f"   📊 {len(models)} modelos · {len(comparisons)} comparaciones")
    print(f"   🌐 {total_pages} páginas HTML generadas en output/")
    print(f"   ✍️  {len(descriptions)} descripciones generadas")
    print(f"   💡 Ideas de contenido en output/content_ideas.md")
    print(f"   {'⚠️  ' + str(len(price_changes)) + ' cambios de precio' if price_changes else '✅ Sin cambios de precio'}")

    # 7. Push a Cloudflare D1 (activa alertas Slack/Discord para suscriptores)
    push_to_d1(models, price_changes)


# ─── HTML Generation ─────────────────────────────────────────────────────────

def generate_static_assets(model_count: int, compare_count: int):
    """Genera robots.txt, og-image.svg y 404.html para GitHub Pages."""
    # robots.txt
    (OUTPUT_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )

    # OG image (SVG — no depende de Pillow, se renderea como imagen en Twitter/Discord)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#09090b"/>
  <rect x="0" y="0" width="1200" height="3" fill="#3b82f6"/>
  <text x="80" y="160" font-family="system-ui,sans-serif" font-size="72" font-weight="800"
        fill="white" letter-spacing="-3">LLM API Pricing</text>
  <text x="80" y="240" font-family="system-ui,sans-serif" font-size="36" fill="#71717a">Real-time cost comparison for {model_count}+ AI models</text>
  <rect x="80" y="310" width="340" height="120" rx="16" fill="#18181b" stroke="#27272a" stroke-width="1"/>
  <text x="100" y="355" font-family="monospace" font-size="14" fill="#71717a">Models tracked</text>
  <text x="100" y="410" font-family="monospace" font-size="52" font-weight="800" fill="#3b82f6">{model_count}+</text>
  <rect x="460" y="310" width="340" height="120" rx="16" fill="#18181b" stroke="#27272a" stroke-width="1"/>
  <text x="480" y="355" font-family="monospace" font-size="14" fill="#71717a">Comparisons</text>
  <text x="480" y="410" font-family="monospace" font-size="52" font-weight="800" fill="#10b981">{compare_count}+</text>
  <text x="80" y="560" font-family="system-ui,sans-serif" font-size="24" fill="#3f3f46">{SITE_URL}</text>
</svg>"""
    (OUTPUT_DIR / "og-image.svg").write_text(svg, encoding="utf-8")

    # 404.html (GitHub Pages lo usa automáticamente)
    html_404 = _html(
        title="Page Not Found — LLM Pricing",
        body=f"""<div class="hero" style="text-align:center;padding:8rem 1rem">
  <div class="hero-badge">404</div>
  <h1 style="font-size:2.5rem">Page not found</h1>
  <p class="subtitle">This model or comparison might have been removed.</p>
  <a class="btn" href="{SITE_URL}/">Back to all models &rarr;</a>
</div>""",
        desc="Page not found — return to LLM API pricing comparison.",
        canonical=f"{SITE_URL}/",
    )
    (OUTPUT_DIR / "404.html").write_text(html_404, encoding="utf-8")

def generate_index_html(models: list[dict], price_changes: list[dict], descriptions: dict) -> int:
    """Genera output/index.html — homepage con tabla completa de precios."""
    print("[HTML 1/4] Generando index.html...")
    (OUTPUT_DIR / "models").mkdir(exist_ok=True)
    (OUTPUT_DIR / "compare").mkdir(exist_ok=True)

    paid = [m for m in models if not m["is_free"] and m["total_price_per_1m"] > 0]
    free = [m for m in models if m["is_free"]]
    priority = [m for m in models if m["id"] in PRIORITY_MODELS]

    stats_html = f"""
    <div class="stats-grid">
      <div class="stat-card"><span class="stat-val blue">{len(models)}</span><div class="stat-lbl">Models tracked</div></div>
      <div class="stat-card"><span class="stat-val green">{len(free)}</span><div class="stat-lbl">Free APIs</div></div>
      <div class="stat-card"><span class="stat-val green">{_fmt(paid[0]["total_price_per_1m"]) if paid else "-"}</span><div class="stat-lbl">Cheapest paid /1M</div></div>
      <div class="stat-card"><span class="stat-val">{len(set(m["provider"] for m in models))}</span><div class="stat-lbl">Providers</div></div>
    </div>"""

    # Price-change banner
    changes_html = ""
    if price_changes:
        items = "".join(
            f"<li><strong>{c['model']}</strong>: {c['direction']} {abs(c['change_pct'])}% — "
            f"{_fmt(c['old_price'])} → <strong>{_fmt(c['new_price'])}</strong>/1M tokens</li>"
            for c in price_changes[:8]
        )
        changes_html = f"""<div style="background:#1c1208;border:1px solid #854d0e;border-radius:10px;padding:16px 20px;margin-bottom:24px;">
      <h3 style="color:#fbbf24;margin-bottom:8px">⚠️ Price changes detected today</h3>
      <ul style="color:#fde68a;font-size:0.9rem;padding-left:16px;">{items}</ul></div>"""

    def model_row(m: dict) -> str:
        slug = m['slug']
        badge = '<span class="badge-free">FREE</span>' if m['is_free'] else ''
        return (
            f"<tr>"
            f"<td data-val='{m['name']}'><a class='model-link' href='{SITE_URL}/models/{slug}.html'>{m['name']}</a><br>"
            f"<span class='provider'>{m['provider']}</span></td>"
            f"<td class='price-in' data-val='{m['prompt_price_per_1m']}'>{_fmt(m['prompt_price_per_1m'])}</td>"
            f"<td class='price-out' data-val='{m['completion_price_per_1m']}'>{_fmt(m['completion_price_per_1m'])}</td>"
            f"<td class='price-tot' data-val='{m['total_price_per_1m']}'>{_fmt(m['total_price_per_1m'])} {badge}</td>"
            f"<td class='ctx' data-val='{m['context_length']}'>{m['context_length']//1000}K</td>"
            f"<td><a href='{SITE_URL}/models/{slug}.html' style='font-size:0.8rem;color:var(--text-muted)'>Details &rarr;</a></td>"
            f"</tr>"
        )

    all_rows = "".join(model_row(m) for m in models)
    priority_rows = "".join(model_row(m) for m in priority)

    table = lambda rows: f"""<div class="table-wrap">
    <table id="main-table">
      <thead><tr>
        <th data-col="0">Model ↕</th>
        <th data-col="1">Input /1M ↕</th>
        <th data-col="2">Output /1M ↕</th>
        <th data-col="3">Total /1M ↕</th>
        <th data-col="4">Context ↕</th>
        <th></th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table></div>"""

    cta = f"""<div class="cta">
    <h2>Stop choosing models manually — automate it</h2>
    <p>StormRouter routes each request to the cheapest model that meets your quality bar.<br>
    Teams save <strong>60–80%</strong> on LLM API costs without changing their code.</p>
    <a class="btn" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter free — 14 days →</a>
    </div>"""

    body = f"""<div class="hero">
      <div class="hero-badge">Updated {TODAY} &middot; {len(models)} models</div>
      <h1>LLM API Pricing<br>Real-Time Cost Comparison</h1>
      <p class="subtitle">Compare {len(models)}+ AI model APIs. Find the cheapest option for your stack. Data refreshed daily.</p>
    </div>
    {stats_html}
    {changes_html}
    {cta}
    <h2>Most Popular Models</h2>
    {table(priority_rows)}
    <h2>All Models &mdash; Full Comparison ({len(models)} total)</h2>
    <p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:1rem">Click any column header to sort &nbsp;&middot;&nbsp; <a href="{SITE_URL}/compare/">See head-to-head comparisons &rarr;</a></p>
    {table(all_rows)}"""

    html = _html(
        title="LLM API Pricing 2026 — Compare OpenAI, Anthropic, Google, Meta & More",
        body=body,
        desc=f"Real-time pricing for {len(models)}+ LLM APIs. Compare GPT-4o, Claude, Gemini, Llama, Mistral and 150+ more. Find the cheapest model for chatbots, RAG, code generation.",
        canonical=f"{SITE_URL}/",
    )
    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  ✅ index.html ({len(models)} modelos)")
    return len(models)


def generate_model_pages(models: list[dict], descriptions: dict) -> int:
    """Genera output/models/<slug>.html para cada modelo."""
    print("[HTML 2/4] Generando páginas individuales de modelos...")
    models_dir = OUTPUT_DIR / "models"
    models_dir.mkdir(exist_ok=True)
    model_map = {m["id"]: m for m in models}
    count = 0

    SCENARIOS = [
        ("100K tokens/month", 100_000),
        ("1M tokens/month", 1_000_000),
        ("10M tokens/month", 10_000_000),
        ("100M tokens/month", 100_000_000),
    ]

    for m in models:
        slug = m["slug"]
        desc = descriptions.get(m["id"], "")
        paid = [x for x in models if not x["is_free"] and x["total_price_per_1m"] > 0]
        rank = next((i+1 for i, x in enumerate(paid) if x["slug"] == slug), None)

        rank_html = f'<span class="badge-cheap">#{rank} cheapest paid</span>' if rank and rank <= 20 else ""
        free_badge = '<span class="badge-free">FREE</span>' if m["is_free"] else ""

        scenarios_rows = "".join(
            f"<tr><td>{label}</td><td class='win'>{_monthly(m['prompt_price_per_1m']*0.5 + m['completion_price_per_1m']*0.5, t)}</td></tr>"
            for label, t in SCENARIOS
        )

        # Similar models (same provider or similar price)
        similar = [x for x in models if x["slug"] != slug and (
            x["provider"] == m["provider"] or
            abs(x["total_price_per_1m"] - m["total_price_per_1m"]) < max(m["total_price_per_1m"] * 0.5, 0.5)
        )][:6]
        similar_html = "".join(
            f"<a href='{SITE_URL}/models/{s['slug']}.html' style='display:flex;justify-content:space-between;align-items:center;"
            f"padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:8px;text-decoration:none;'>"
            f"<span style='color:var(--text);font-weight:500'>{s['name']}</span>"
            f"<span style='color:var(--success);font-family:monospace;font-size:0.85rem;font-weight:600'>{_fmt(s['total_price_per_1m'])}/1M</span></a>"
            for s in similar
        ) if similar else ""

        # Top comparison links
        comp_links = ""
        for other_id in PRIORITY_MODELS[:6]:
            other = model_map.get(other_id)
            if not other or other["slug"] == slug:
                continue
            a_slug, b_slug = sorted([slug, other["slug"]])
            comp_links += (f"<a href='{SITE_URL}/compare/{a_slug}--vs--{b_slug}.html' "
                            f"style='display:inline-block;margin:4px;padding:6px 14px;background:var(--card);"
                            f"border:1px solid var(--border);border-radius:6px;font-size:0.82rem;color:var(--text-sub);'>"
                            f"{m['name']} vs {other['name']} &rarr;</a>")

        body = f"""<div class="breadcrumb"><a href="{SITE_URL}/">LLM Pricing</a> › <span style="text-transform:capitalize">{m['provider']}</span> › {m['name']}</div>
    <h1>{m['name']} API Pricing {free_badge}</h1>
    <p class="subtitle">by {m['provider']} &middot; {m['context_length']//1000}K context window &middot; {rank_html}</p>
    <div class="stats-grid">
      <div class="stat-card"><span class="stat-val price-in">{_fmt(m['prompt_price_per_1m'])}</span><div class="stat-lbl">Input /1M tokens</div></div>
      <div class="stat-card"><span class="stat-val" style="color:#60a5fa">{_fmt(m['completion_price_per_1m'])}</span><div class="stat-lbl">Output /1M tokens</div></div>
      <div class="stat-card"><span class="stat-val">{m['context_length']//1000}K</span><div class="stat-lbl">Context window</div></div>
    </div>
    {f'<p class="desc">{desc}</p>' if desc else ''}
    <h2>Monthly Cost Examples</h2>
    <p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:1rem">Assuming 50% input / 50% output token split</p>
    <div class="table-wrap"><table class="scenario-table">
      <thead><tr><th>Usage</th><th>Monthly cost</th></tr></thead>
      <tbody>{scenarios_rows}</tbody>
    </table></div>
    {'<h2>Compare with other models</h2>' + comp_links if comp_links else ''}
    {_calc_html(models, m['id'])}
    <div class="cta">
      <h2>Automate your model selection</h2>
      <p>StormRouter sends each request to the cheapest model that can handle it.<br>Only use {m['name']} when your quality requirements demand it.</p>
      <a class="btn" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter free &rarr;</a>
    </div>
    {'<h2>Similar models</h2>' + similar_html if similar_html else ''}"""

        html = _html(
            title=f"{m['name']} API Pricing — Cost Per Token & Calculator 2026",
            body=body,
            desc=f"{m['name']} costs {_fmt(m['prompt_price_per_1m'])}/1M input tokens and {_fmt(m['completion_price_per_1m'])}/1M output tokens. {m['context_length']//1000}K context. Monthly cost examples and comparisons.",
            canonical=f"{SITE_URL}/models/{slug}.html",
        )
        (models_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        count += 1

    print(f"  ✅ {count} páginas de modelo en output/models/")
    return count


def generate_comparison_pages(models: list[dict], comparisons: list[dict]) -> int:
    """Genera output/compare/<a-vs-b>.html para cada par de modelos."""
    print("[HTML 3/4] Generando páginas de comparación...")
    compare_dir = OUTPUT_DIR / "compare"
    compare_dir.mkdir(exist_ok=True)
    model_map = {m["slug"]: m for m in models}
    count = 0

    SCENARIOS = [
        ("1M tokens", 1_000_000),
        ("10M tokens", 10_000_000),
        ("100M tokens", 100_000_000),
        ("1B tokens", 1_000_000_000),
    ]

    # Index page for /compare/
    comp_index_links = ""

    for comp in comparisons:
        a = model_map.get(comp["model_a_slug"])
        b = model_map.get(comp["model_b_slug"])
        if not a or not b:
            continue

        cheap, expensive = (a, b) if a["total_price_per_1m"] <= b["total_price_per_1m"] else (b, a)
        savings_pct = 0
        if expensive["total_price_per_1m"] > 0:
            savings_pct = round((1 - cheap["total_price_per_1m"] / expensive["total_price_per_1m"]) * 100, 0)

        def card(m: dict) -> str:
            is_win = m["slug"] == cheap["slug"]
            border = "winner" if is_win else ""
            win_badge = '<span class="badge-winner">\u2713 CHEAPER</span>' if is_win else ''
            price_class = "price-big cheaper" if is_win else "price-big"
            return (
                f"<div class='compare-card {border}'>"
                f"<div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem'>"
                f"<div><h3 style='margin:0'>{m['name']}</h3><p class='provider' style='margin-top:2px'>{m['provider']}</p></div>"
                f"{win_badge}</div>"
                f"<div style='margin:1rem 0 0.5rem'>"
                f"<div style='font-size:0.78rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px'>Total /1M tokens</div>"
                f"<span class='{price_class}'>{_fmt(m['total_price_per_1m'])}</span></div>"
                f"<div style='font-size:0.82rem;color:var(--text-muted);border-top:1px solid var(--border);padding-top:0.75rem;margin-top:0.75rem'>"
                f"Input <span class='price-in'>{_fmt(m['prompt_price_per_1m'])}</span> "
                f"&nbsp;&middot;&nbsp; Output <span class='price-out'>{_fmt(m['completion_price_per_1m'])}</span><br>"
                f"<span style='margin-top:4px;display:inline-block'>Context: {m['context_length']//1000}K tokens</span></div>"
                f"<a href='{SITE_URL}/models/{m['slug']}.html' style='display:block;margin-top:1rem;font-size:0.8rem;color:var(--accent)'>Full details &rarr;</a>"
                f"</div>"
            )

        scenario_rows = ""
        for label, tokens in SCENARIOS:
            cost_a = (a["total_price_per_1m"] / 1_000_000) * tokens
            cost_b = (b["total_price_per_1m"] / 1_000_000) * tokens
            diff = abs(cost_a - cost_b)
            ca_str = _monthly(a["total_price_per_1m"], tokens)
            cb_str = _monthly(b["total_price_per_1m"], tokens)
            ca_html = f"<span class='win'>{ca_str}</span>" if a["slug"] == cheap["slug"] else ca_str
            cb_html = f"<span class='win'>{cb_str}</span>" if b["slug"] == cheap["slug"] else cb_str
            scenario_rows += f"<tr><td>{label}/month</td><td>{ca_html}</td><td>{cb_html}</td><td class='win' style='font-weight:700'>${diff:,.2f}</td></tr>"

        slug = comp["slug"]
        comp_index_links += (f"<a href='{slug}.html' style='display:flex;justify-content:space-between;align-items:center;"
                               f"padding:10px 16px;background:var(--card);border:1px solid var(--border);border-radius:10px;"
                               f"margin-bottom:8px;font-size:0.875rem;color:var(--text);text-decoration:none;transition:border-color .15s;'>"
                               f"<span>{a['name']} <span style='color:var(--text-muted)'>vs</span> {b['name']}</span>"
                               f"<span style='color:var(--success);font-family:monospace;font-size:0.8rem;font-weight:600'>"
                               f"{savings_pct:.0f}% cheaper option &rarr;</span></a>")

        body = f"""<div class="breadcrumb"><a href="/">LLM Pricing</a> › <a href="/compare/">Compare</a> › {a['name']} vs {b['name']}</div>
    <h1>{a['name']} vs {b['name']} — API Pricing Comparison</h1>
    <p class="subtitle">{cheap['name']} is <span style="color:#4ade80;font-weight:700">{savings_pct:.0f}% cheaper</span> than {expensive['name']} at the same token volume. Data as of {TODAY}.</p>
    <div class="compare-grid">{card(a)}{card(b)}</div>
    <h2>Monthly Cost Comparison</h2>
    <div class="table-wrap"><table class="scenario-table">
      <thead><tr>
        <th>Monthly usage</th>
        <th style="text-align:right">{a['name']}</th>
        <th style="text-align:right">{b['name']}</th>
        <th style="text-align:right">Savings with {cheap['name']}</th>
      </tr></thead>
      <tbody>{scenario_rows}</tbody>
    </table></div>
    {_calc_html(models, cheap['id'])}
    <div class="cta">
      <h2>Use both automatically — let AI decide</h2>
      <p>StormRouter routes each prompt to the cheapest model that meets your quality requirements.<br>
      Use {cheap['name']} for simple tasks, {expensive['name']} only when complexity demands it.</p>
      <a class="btn" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter free →</a>
    </div>"""

        html = _html(
            title=f"{a['name']} vs {b['name']} — API Cost Comparison 2026",
            body=body,
            desc=f"{a['name']} costs {_fmt(a['total_price_per_1m'])}/1M tokens vs {b['name']} at {_fmt(b['total_price_per_1m'])}/1M. {cheap['name']} is {savings_pct:.0f}% cheaper. Full cost breakdown with monthly examples.",
            canonical=f"{SITE_URL}/compare/{slug}.html",
        )
        (compare_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        count += 1

    # Write compare index
    index_body = f"""<div class="hero">
      <div class="hero-badge">Head-to-head pricing battles</div>
      <h1>LLM Model<br>Comparisons</h1>
      <p class="subtitle">{len(comparisons)} head-to-head API cost comparisons. Find the cheapest model for your use case.</p>
    </div>
    <div>{comp_index_links}</div>"""
    (compare_dir / "index.html").write_text(
        _html("LLM API Comparisons 2026 — Head-to-Head Pricing", index_body,
              desc="Compare LLM API prices head-to-head. GPT-4o vs Claude, Gemini vs Llama, and 300+ more pairs."),
        encoding="utf-8",
    )

    print(f"  ✅ {count} páginas de comparación en output/compare/")
    return count


# ─── Niche pages: Top 5 models for [use-case] ────────────────────────────────

def generate_niche_pages(models: list[dict], descriptions: dict) -> int:
    """Genera páginas 'Top 5 for [niche]' en output/for/<slug>.html"""
    print("[HTML 5/6] Generando páginas por nicho (Top 5)...")
    niche_dir = OUTPUT_DIR / "for"
    niche_dir.mkdir(exist_ok=True)
    count = 0
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    for niche_key, niche in NICHES.items():
        candidates = [m for m in models if niche["filter"](m)]
        candidates.sort(key=niche["sort_key"])
        top5 = candidates[:5]
        if not top5:
            continue

        def top5_row(i: int, m: dict) -> str:
            desc = descriptions.get(m["id"], "")[:120]
            if i > 0:
                a_s, b_s = sorted([top5[0]["slug"], m["slug"]])
                vs_link = f'<a href="{SITE_URL}/compare/{a_s}--vs--{b_s}.html" style="margin-left:12px;font-size:0.8rem;color:var(--accent)">vs #1 →</a>'
            else:
                vs_link = ""
            return (
                f'<div class="top5-card {"winner" if i == 0 else ""}">'
                f'<div class="top5-rank">{medals[i]}</div>'
                f'<div class="top5-body">'
                f'<div class="top5-name"><a href="{SITE_URL}/models/{m["slug"]}.html">{m["name"]}</a>'
                f'<span class="tag">{m["provider"]}</span></div>'
                f'{f"<p class=\'top5-desc\'>{desc}</p>" if desc else ""}'
                f'<div class="top5-meta">'
                f'<span class="price-in">${m["prompt_price_per_1m"]:.4f}/1M in</span>'
                f'<span class="price-out"> · ${m["completion_price_per_1m"]:.4f}/1M out</span>'
                f'<span style="color:var(--text-muted)"> · {m["context_length"]//1000}K ctx</span>'
                f'{vs_link}</div></div>'
                f'<div class="top5-price">'
                f'<div class="price-big {"cheaper" if i == 0 else ""}">${m["total_price_per_1m"]:.4f}</div>'
                f'<div style="font-size:0.72rem;color:var(--text-muted)">per 1M tokens</div>'
                f'</div></div>'
            )

        top5_html = "".join(top5_row(i, m) for i, m in enumerate(top5))

        comp_links = ""
        for i in range(len(top5)):
            for j in range(i + 1, len(top5)):
                a, b = top5[i], top5[j]
                a_s, b_s = sorted([a["slug"], b["slug"]])
                comp_links += f'<a href="{SITE_URL}/compare/{a_s}--vs--{b_s}.html" class="tag" style="color:var(--accent)">{a["name"]} vs {b["name"]} →</a>'

        related = "".join(
            f'<a href="{SITE_URL}/for/{n["slug"]}.html" class="tag" style="color:var(--text-sub)">{n["icon"]} {n["label"]}</a>'
            for k, n in NICHES.items() if k != niche_key
        )

        calc = _calc_html(models, top5[0]["id"])
        body = (
            f'<div class="breadcrumb"><a href="{SITE_URL}/">LLM Pricing</a> › <a href="{SITE_URL}/for/">Use Cases</a> › {niche["label"]}</div>'
            f'<div class="hero" style="text-align:left;padding:3rem 0 2rem">'
            f'<div class="hero-badge">{niche["icon"]} Top 5 Models · Updated {TODAY}</div>'
            f'<h1>Best LLMs for<br>{niche["label"]}</h1>'
            f'<p class="subtitle" style="text-align:left;max-width:640px">{niche["long_desc"]}</p></div>'
            f'<div class="top5-list">{top5_html}</div>'
            f'{calc}'
            f'{"<h2>Head-to-head comparisons</h2><div class=\'tags\'>" + comp_links + "</div>" if comp_links else ""}'
            f'<h2>Other use cases</h2><div class="tags">{related}</div>'
            f'<div class="cta"><h2>Route automatically by use case</h2>'
            f'<p>StormRouter detects the intent of each prompt and routes it to the cheapest model that can handle it.</p>'
            f'<a class="btn" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter free →</a></div>'
        )

        html = _html(
            title=f"Top 5 LLMs for {niche['label']} — Pricing & Comparison 2026",
            body=body,
            desc=f"{niche['desc']} Ranked by {niche['sort_label']}. Full pricing breakdown with annual savings calculator.",
            canonical=f"{SITE_URL}/for/{niche['slug']}.html",
        )
        (niche_dir / f"{niche['slug']}.html").write_text(html, encoding="utf-8")
        count += 1

    # Niche index page
    niche_cards = "".join(
        f'<a href="{n["slug"]}.html" style="display:flex;align-items:center;gap:14px;padding:18px 22px;'
        f'background:var(--card);border:1px solid var(--border);border-radius:12px;text-decoration:none;margin-bottom:10px;">'
        f'<span style="font-size:2rem">{n["icon"]}</span>'
        f'<div><div style="font-weight:600;color:var(--text)">{n["label"]}</div>'
        f'<div style="font-size:0.82rem;color:var(--text-muted)">{n["desc"]}</div></div>'
        f'<span style="margin-left:auto;color:var(--accent);font-size:0.85rem;flex-shrink:0">Top 5 →</span></a>'
        for _, n in NICHES.items()
    )
    index_body = (
        f'<div class="hero"><div class="hero-badge">LLM Use Case Guide</div>'
        f'<h1>Best AI Models<br>by Use Case</h1>'
        f'<p class="subtitle">Find the most cost-efficient LLM for your specific application.</p></div>'
        f'{niche_cards}'
    )
    (niche_dir / "index.html").write_text(
        _html("Best LLMs by Use Case 2026 — Cheapest AI Models for Every Task", index_body,
              desc="Best and cheapest LLMs for chatbots, RAG, coding, long-context, batch processing, and enterprise. Updated daily."),
        encoding="utf-8",
    )
    print(f"  ✅ {count} páginas de nicho en output/for/")
    return count


# ─── Provider cluster pages ───────────────────────────────────────────────────

def generate_provider_pages(models: list[dict], descriptions: dict) -> int:
    """Genera páginas de proveedor en output/providers/<slug>.html con interlinking."""
    print("[HTML 6/6] Generando páginas de proveedor...")
    from collections import defaultdict
    provider_dir = OUTPUT_DIR / "providers"
    provider_dir.mkdir(exist_ok=True)

    by_provider: dict[str, list[dict]] = defaultdict(list)
    for m in models:
        by_provider[m["provider"]].append(m)

    priority_providers = {m.split("/")[0] for m in PRIORITY_MODELS}
    to_gen = {p: ms for p, ms in by_provider.items()
              if p in priority_providers or len(ms) >= 3}

    count = 0
    provider_index_links = ""

    for provider, pmodels in sorted(to_gen.items(), key=lambda x: (-len(x[1]), x[0])):
        pmodels_sorted = sorted(pmodels, key=lambda m: m["total_price_per_1m"])
        free_count = sum(1 for m in pmodels_sorted if m["is_free"])
        cheapest_paid = next((m for m in pmodels_sorted if not m["is_free"] and m["total_price_per_1m"] > 0), None)
        slug = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-")

        rows = "".join(
            f"<tr>"
            f"<td><a class='model-link' href='{SITE_URL}/models/{m['slug']}.html'>{m['name']}</a></td>"
            f"<td class='price-in' data-val='{m['prompt_price_per_1m']}'>{_fmt(m['prompt_price_per_1m'])}</td>"
            f"<td class='price-out' data-val='{m['completion_price_per_1m']}'>{_fmt(m['completion_price_per_1m'])}</td>"
            f"<td class='price-tot' data-val='{m['total_price_per_1m']}'>{_fmt(m['total_price_per_1m'])}"
            f"{'<span class=\"badge-free\" style=\"margin-left:6px\">FREE</span>' if m['is_free'] else ''}</td>"
            f"<td class='ctx' data-val='{m['context_length']}'>{m['context_length']//1000}K</td>"
            f"</tr>"
            for m in pmodels_sorted
        )

        # Cross-provider comparison links
        xprov_links = ""
        other_providers = [p2 for p2 in priority_providers if p2 != provider and p2 in by_provider][:5]
        for other_p in other_providers:
            other_best = min(
                (m for m in by_provider[other_p] if not m["is_free"] and m["total_price_per_1m"] > 0),
                key=lambda m: m["total_price_per_1m"], default=None
            )
            if cheapest_paid and other_best:
                a_s, b_s = sorted([cheapest_paid["slug"], other_best["slug"]])
                xprov_links += (
                    f'<a href="{SITE_URL}/compare/{a_s}--vs--{b_s}.html" class="tag" style="color:var(--accent)">'
                    f'{provider.title()} vs {other_p.title()} →</a>'
                )

        calc = _calc_html(models, cheapest_paid["id"] if cheapest_paid else "")

        body = (
            f'<div class="breadcrumb"><a href="{SITE_URL}/">LLM Pricing</a> › <a href="{SITE_URL}/providers/">Providers</a> › {provider.title()}</div>'
            f'<h1>{provider.title()} API Pricing — All Models 2026</h1>'
            f'<div class="stats-grid">'
            f'<div class="stat-card"><span class="stat-val blue">{len(pmodels_sorted)}</span><div class="stat-lbl">Total models</div></div>'
            f'<div class="stat-card"><span class="stat-val green">{free_count}</span><div class="stat-lbl">Free models</div></div>'
            f'<div class="stat-card"><span class="stat-val green">{_fmt(cheapest_paid["total_price_per_1m"]) if cheapest_paid else "—"}</span><div class="stat-lbl">Cheapest paid /1M</div></div>'
            f'<div class="stat-card"><span class="stat-val">{len(pmodels_sorted) - free_count}</span><div class="stat-lbl">Paid models</div></div>'
            f'</div>'
            f'<div class="table-wrap"><table>'
            f'<thead><tr>'
            f'<th data-col="0">Model ↕</th><th data-col="1">Input /1M ↕</th>'
            f'<th data-col="2">Output /1M ↕</th><th data-col="3">Total /1M ↕</th><th data-col="4">Context ↕</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
            f'{calc}'
            f'{"<h2>Compare with other providers</h2><div class=\'tags\'>" + xprov_links + "</div>" if xprov_links else ""}'
            f'<div class="cta"><h2>Don\'t lock into one provider</h2>'
            f'<p>StormRouter switches between {provider.title()} and other providers dynamically based on real-time price and availability.</p>'
            f'<a class="btn" href="{STORMROUTER_URL}" target="_blank" rel="noopener">Try StormRouter free →</a></div>'
        )

        html = _html(
            title=f"{provider.title()} LLM API Pricing 2026 — All Models & Costs",
            body=body,
            desc=f"{provider.title()} offers {len(pmodels_sorted)} LLM APIs. Cheapest at {_fmt(cheapest_paid['total_price_per_1m']) if cheapest_paid else 'free'}/1M tokens. Full pricing breakdown and annual savings calculator.",
            canonical=f"{SITE_URL}/providers/{slug}.html",
        )
        (provider_dir / f"{slug}.html").write_text(html, encoding="utf-8")

        provider_index_links += (
            f'<a href="{slug}.html" style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:14px 18px;background:var(--card);border:1px solid var(--border);border-radius:10px;'
            f'margin-bottom:8px;text-decoration:none;">'
            f'<span style="font-weight:600;color:var(--text)">{provider.title()}</span>'
            f'<span style="color:var(--text-muted);font-size:0.82rem">{len(pmodels_sorted)} models · '
            f'from {_fmt(cheapest_paid["total_price_per_1m"]) if cheapest_paid else "Free"}/1M</span>'
            f'<span style="color:var(--accent);font-size:0.82rem;flex-shrink:0">View all →</span></a>'
        )
        count += 1

    index_body = (
        f'<div class="hero"><div class="hero-badge">Provider Directory</div>'
        f'<h1>LLM Providers<br>Pricing Directory</h1>'
        f'<p class="subtitle">All AI model providers ranked by model count. Find the best deal from each ecosystem.</p></div>'
        f'{provider_index_links}'
    )
    (provider_dir / "index.html").write_text(
        _html("LLM API Providers 2026 — Full Pricing Directory", index_body,
              desc="Compare AI model providers: OpenAI, Anthropic, Google, Meta, Mistral, DeepSeek and more. Prices, model counts, and annual savings calculator."),
        encoding="utf-8",
    )
    print(f"  ✅ {count} páginas de proveedor en output/providers/")
    return count


def generate_sitemap(models: list[dict], comparisons: list[dict]):
    """Genera output/sitemap.xml con clusters inteligentes por proveedor y nicho."""
    print("[HTML sitemap] Generando sitemap.xml inteligente...")

    # ─ Cluster 1: Core pages ─
    core = [
        (f"{SITE_URL}/", "1.0"),
        (f"{SITE_URL}/compare/", "0.85"),
        (f"{SITE_URL}/for/", "0.85"),
        (f"{SITE_URL}/providers/", "0.80"),
    ]
    # ─ Cluster 2: Niche pages (alta autoridad — Topic clusters) ─
    niche_urls = [(f"{SITE_URL}/for/{n['slug']}.html", "0.80") for n in NICHES.values()]

    # ─ Cluster 3: Provider pages ─
    priority_providers = {m.split("/")[0] for m in PRIORITY_MODELS}
    provider_slugs = {re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in
                     (m["provider"] for m in models) if p in priority_providers}
    provider_urls = [(f"{SITE_URL}/providers/{s}.html", "0.75") for s in sorted(provider_slugs)]

    # ─ Cluster 4: Priority model pages (high-traffic head queries) ─
    priority_slugs = {m["slug"] for m in models if m["id"] in set(PRIORITY_MODELS)}
    model_urls = []
    for m in models:
        p = "0.70" if m["slug"] in priority_slugs else "0.55"
        model_urls.append((f"{SITE_URL}/models/{m['slug']}.html", p))

    # ─ Cluster 5: Compare pages (long-tail, high-intent) ─
    compare_urls = [(f"{SITE_URL}/compare/{c['slug']}.html", "0.60") for c in comparisons]

    all_urls = core + niche_urls + provider_urls + model_urls + compare_urls

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url, priority in all_urls:
        xml += f"  <url><loc>{url}</loc><lastmod>{TODAY}</lastmod><priority>{priority}</priority></url>\n"
    xml += "</urlset>"
    (OUTPUT_DIR / "sitemap.xml").write_text(xml, encoding="utf-8")
    print(f"  ✅ sitemap.xml ({len(all_urls)} URLs en 5 clusters)")


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

