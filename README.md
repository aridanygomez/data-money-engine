# data-money-engine

Motor autónomo que se ejecuta **cada día a las 06:00 UTC** desde GitHub Actions (gratis e ilimitado en repos públicos).

## ¿Qué hace?

1. **Descarga precios** de 150+ APIs de LLMs desde OpenRouter (sin API key)
2. **Detecta cambios de precio** y los registra
3. **Genera descripciones SEO** únicas para cada modelo (Gemini Flash, gratis)
4. **Genera ideas de contenido** del día (tweets + post Reddit) listos para copiar
5. **Hace commit automático** de todos los archivos actualizados → siempre tienes datos frescos en GitHub

## Archivos generados a diario

```
data/
  models.json          — Precios de 150+ LLMs (actualizado cada día)
  comparisons.json     — 300+ pares de modelos para páginas de comparación
  descriptions.json    — Descripciones SEO por modelo (se van acumulando)
  site_data.json       — Datos optimizados para el sitio Astro
  prices_history.json  — Historial mensual de precios
  daily_log.json       — Log de las últimas 90 ejecuciones

output/
  content_ideas.md     — Ideas de tweets y posts generadas hoy con Gemini
```

---

## Setup (5 minutos)

### Paso 1: Fork o sube a GitHub

Si ya tienes este repo en tu PC:
```bash
cd "e:\patente google\data-money-engine"
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/data-money-engine.git
git push -u origin main
```

Asegúrate de que el repo sea **público** (GitHub Actions es gratis e ilimitado en repos públicos).

### Paso 2: Añadir la API key de Gemini como Secret

1. En GitHub, ve a tu repo → **Settings** → **Secrets and variables** → **Actions**
2. Haz clic en **New repository secret**
3. Nombre: `GEMINI_API_KEY`
4. Valor: tu API key de [aistudio.google.com](https://aistudio.google.com) (gratis)
5. Haz clic en **Add secret**

### Paso 3: Ejecutar por primera vez

1. Ve a **Actions** → **Ejecutor Autonomo de Contenido**
2. Haz clic en **Run workflow** → **Run workflow**
3. Espera 1-2 minutos
4. Revisa la carpeta `data/` y `output/` — ya tienes datos frescos

---

## Uso de los datos generados

### Para el sitio Astro (llm-pricing.dev)

Los archivos `data/models.json` y `data/comparisons.json` alimentan directamente el sitio estático. Apunta el sitio Astro a este repo o copia los JSONs.

### Para ideas de contenido

Cada mañana revisa `output/content_ideas.md` — tienes tweets y posts listos para publicar sobre los cambios de precio del día.

### Ejemplo de lo que genera

```markdown
# Ideas de contenido — 2026-02-28

## Datos del día
- Modelos tracked: 156
- Gratuitos: 23
- Más barato pago: deepseek/deepseek-r1 — $0.0015/1M tokens

## ⚠️ Cambios de precio detectados
- GPT-4o: ⬇️ BAJÓ 15% ($15.00 → $12.75/1M)

## TWEET 1
GPT-4o just dropped 15% in price. Still not the cheapest option.
Llama 3.1 70B on Groq costs $0.64/1M tokens — 20x less.
Most teams don't need GPT-4 for 80% of their requests.
stormrouter.dev auto-routes to save you the difference.
```

---

## Costes

| Componente | Límite gratis | Uso estimado |
|------------|--------------|--------------|
| GitHub Actions | Ilimitado (repo público) | ~2 min/día |
| OpenRouter API | Sin límite para listar modelos | 1 req/día |
| Gemini Flash | 1,000 req/día | ~20 req/día |
| **Total** | | **€0/mes** |

---

## Conectar con StormRouter

Cada pieza de contenido generada incluye una mención suave a [stormrouter.dev](https://stormrouter.dev). El sitio de precios es el top-of-funnel orgánico; StormRouter es la conversión.
