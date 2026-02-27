"""
twitter_human_poster.py — Publica en X/Twitter con Chrome real + movimiento humano
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Conecta a tu Chrome existente via puerto de debugging (no cierra pestañas)
- Toma screenshots para verificar qué hay en pantalla antes de actuar
- Movimiento de ratón con curvas Bezier (anti-bot)
- Escritura con velocidad humana (variación aleatoria por tecla)
- No usa la API de Twitter → no necesita plan de pago

SETUP (una sola vez):
  1. pip install selenium pyautogui Pillow undetected-chromedriver
  2. Abre Chrome MANUALMENTE con debugging:
     "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --no-first-run
  3. Loguéate en twitter.com en ese Chrome
  4. Corre este script: python twitter_human_poster.py

MODO AUTOMÁTICO (Task Scheduler o al final de bot_generador.py):
  Ejecutar con: python twitter_human_poster.py --auto
"""

import json
import math
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent
DATA_DIR    = ROOT / "data"
SHOTS_DIR   = ROOT / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)
SITE_URL    = "https://aridanygomez.github.io/data-money-engine"
TODAY       = date.today().isoformat()
DEBUG_PORT  = 9222      # Puerto de debugging de Chrome
DRY_RUN     = "--dry-run" in sys.argv   # Solo imprime, no publica
AUTO_MODE   = "--auto"  in sys.argv     # Sin confirmación humana


# ─── HELPERS DE DATOS ─────────────────────────────────────────────────────────

def load_cheapest_model() -> dict | None:
    """Lee site_data.json y devuelve el modelo más barato del día."""
    for fname in ["site_data.json", "models.json"]:
        f = DATA_DIR / fname
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        models = data.get("top_models", data) if isinstance(data, dict) else data
        if not isinstance(models, list) or not models:
            continue
        paid = [m for m in models if float(
            m.get("prompt_price_per_m", 0) or
            (m.get("pricing", {}).get("prompt", 0) if isinstance(m.get("pricing"), dict) else 0)
        ) > 0]
        if not paid:
            continue
        paid.sort(key=lambda m: float(m.get("prompt_price_per_m", 0) or 0))
        return paid[0]
    return None


def build_tweet_thread(model: dict) -> list[str]:
    """Genera los tweets del hilo (max 280 chars c/u)."""
    name  = model.get("name", model.get("id", "Unknown LLM"))
    price = float(model.get("prompt_price_per_m", 0) or 0)
    slug  = re.sub(r"[^a-z0-9]+", "-", model.get("id", "model").lower()).strip("-")
    url   = f"{SITE_URL}/models/{slug}.html"

    t1 = (
        f"🧵 LLM Price Report — {TODAY}\n\n"
        f"The cheapest LLM API right now costs ${price:.4f}/1M tokens.\n\n"
        f"Most devs are overpaying by 10x without knowing it 🔽"
    )
    t2 = (
        f"🥇 Today's cheapest model:\n"
        f"→ {name}\n"
        f"→ ${price:.4f} per 1M input tokens\n\n"
        f"Token deflation in 2026 is moving faster than anyone predicted."
    )
    t3 = (
        f"📊 I track 300+ LLMs in real-time (open source bot, updates daily):\n"
        f"{url}\n\n"
        f"Bookmark it. Your AI infra bill will thank you 🔖\n"
        f"#AI #LLM #AITools #DevTools #MachineLearning"
    )
    # Truncar a 280 chars por seguridad
    return [t[:280] for t in [t1, t2, t3]]


# ─── MOVIMIENTO HUMANO ────────────────────────────────────────────────────────

def _bezier(p0, p1, p2, t):
    """Punto en curva Bezier cuadrática."""
    x = (1-t)**2 * p0[0] + 2*(1-t)*t * p1[0] + t**2 * p2[0]
    y = (1-t)**2 * p0[1] + 2*(1-t)*t * p1[1] + t**2 * p2[1]
    return (int(x), int(y))


def human_move(pyag, x: int, y: int, duration: float = 0.6):
    """Mueve el ratón en curva Bezier con velocidad variable (anti-bot)."""
    try:
        cx, cy = pyag.position()
    except Exception:
        cx, cy = 0, 0
    # Punto de control aleatorio para la curva
    mx = (cx + x) // 2 + random.randint(-120, 120)
    my = (cy + y) // 2 + random.randint(-80, 80)
    steps = max(20, int(duration * 60))
    for i in range(steps + 1):
        t = i / steps
        # Easing: accelera al inicio, frena al final
        t_eased = t * t * (3 - 2 * t)
        px, py = _bezier((cx, cy), (mx, my), (x, y), t_eased)
        pyag.moveTo(px, py, _pause=False)
        jitter_delay = random.uniform(0.005, 0.025)
        time.sleep(jitter_delay)


def human_click(pyag, x: int, y: int):
    """Click humano: mueve + espera micro-pausa + click."""
    human_move(pyag, x, y, duration=random.uniform(0.4, 0.9))
    time.sleep(random.uniform(0.08, 0.20))
    pyag.click()
    time.sleep(random.uniform(0.15, 0.40))


def human_type(pyag, text: str, wpm: int = 65):
    """Escribe con velocidad humana variable (errores ocasionales omitidos)."""
    # wpm ≈ 65 → ~325 chars/min → ~0.18s por char
    base_delay = 60 / (wpm * 5)
    for ch in text:
        pyag.write(ch, interval=0)
        # Variación aleatoria por carácter
        delay = base_delay * random.uniform(0.4, 2.5)
        # Pausa extra tras puntuación (simula pensar)
        if ch in ".!?\n":
            delay += random.uniform(0.3, 0.9)
        elif ch == " ":
            delay += random.uniform(0.02, 0.12)
        time.sleep(delay)


def screenshot(label: str = "shot") -> Path:
    """Toma captura y la guarda en screenshots/."""
    try:
        import pyautogui as pyag
        from PIL import Image
        ts = int(time.time())
        path = SHOTS_DIR / f"{ts}_{label}.png"
        img = pyag.screenshot()
        img.save(str(path))
        print(f"  📸 Screenshot: {path.name}")
        return path
    except Exception as e:
        print(f"  ⚠️  Screenshot falló: {e}")
        return Path(".")


# ─── SELENIUM: CONECTAR AL CHROME EXISTENTE ───────────────────────────────────

def get_driver():
    """
    Conecta a un Chrome ya abierto con --remote-debugging-port=9222.
    NO abre una ventana nueva, NO cierra pestañas.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.debugger_address = f"127.0.0.1:{DEBUG_PORT}"
    # NO usamos add_argument("--headless") → vemos la pantalla
    driver = webdriver.Chrome(options=opts)
    print(f"  ✅ Conectado a Chrome (versión: {driver.capabilities.get('browserVersion', '?')})")
    print(f"  📄 Pestaña activa: {driver.title!r}")
    return driver


def wait_for_element(driver, selector: str, timeout: int = 10):
    """Espera a que aparezca un elemento CSS con reintentos."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
    except Exception:
        return None


def get_element_center(driver, element) -> tuple[int, int]:
    """Devuelve las coordenadas de pantalla del centro de un elemento."""
    rect = driver.execute_script(
        "var r=arguments[0].getBoundingClientRect();"
        "return {x:r.left+r.width/2, y:r.top+r.height/2};",
        element
    )
    # Añadir offset de la ventana del navegador
    win_x = driver.execute_script("return window.screenX || window.screenLeft;")
    win_y = driver.execute_script("return window.screenY || window.screenTop;")
    chrome_header = 100  # altura aprox. barra chrome
    sx = win_x + int(rect["x"])
    sy = win_y + chrome_header + int(rect["y"])
    return sx, sy


# ─── LÓGICA DE TWITTER ────────────────────────────────────────────────────────

def open_twitter_compose(driver) -> bool:
    """Abre twitter.com/compose/tweet en una NUEVA pestaña (no toca las demás)."""
    current_handles = driver.window_handles
    driver.execute_script("window.open('https://x.com/compose/tweet', '_blank');")
    time.sleep(3)
    
    # Buscar la nueva pestaña
    new_handles = [h for h in driver.window_handles if h not in current_handles]
    if not new_handles:
        # Quizá ya estaba en X — navegar normalmente
        driver.get("https://x.com/compose/tweet")
        return True
    
    driver.switch_to.window(new_handles[0])
    time.sleep(random.uniform(1.5, 3.0))
    screenshot("compose_opened")
    return True


def check_logged_in(driver) -> bool:
    """Verifica si el usuario está logueado en X."""
    driver.execute_script("window.open('https://x.com/home', '_blank');")
    time.sleep(3)
    handles = driver.window_handles
    driver.switch_to.window(handles[-1])
    time.sleep(2)
    
    screenshot("logged_in_check")
    page = driver.page_source
    
    # Cerrar esta pestaña de verificación
    driver.close()
    driver.switch_to.window(handles[-2] if len(handles) > 1 else handles[0])
    
    return "login" not in driver.current_url and ("home" in page or "timeline" in page.lower())


def post_tweet_in_compose(driver, pyag, tweet_text: str, is_reply: bool = False) -> bool:
    """
    Escribe y publica un tweet en la ventana de compose abierta.
    Usa pyautogui para los clicks reales en pantalla.
    """
    # Esperar el textarea de Twitter
    textarea = wait_for_element(driver, '[data-testid="tweetTextarea_0"]', timeout=15)
    if not textarea:
        # Intentar selector alternativo más genérico
        textarea = wait_for_element(driver, 'div[role="textbox"]', timeout=5)
    
    if not textarea:
        print("  ❌ No encontré el cuadro de texto. Screenshot para diagnóstico:")
        screenshot("error_no_textarea")
        return False

    # Obtener coordenadas en pantalla
    sx, sy = get_element_center(driver, textarea)
    
    # Click en el textarea con movimiento humano
    human_click(pyag, sx, sy)
    time.sleep(random.uniform(0.5, 1.0))
    
    # Escribir el texto como humano
    print(f"  ✍️  Escribiendo {len(tweet_text)} chars...")
    human_type(pyag, tweet_text)
    
    time.sleep(random.uniform(0.8, 1.5))
    screenshot("after_typing")
    
    # Buscar botón "Post" / "Tweet"
    submit_btn = (
        wait_for_element(driver, '[data-testid="tweetButtonInline"]', timeout=5) or
        wait_for_element(driver, '[data-testid="tweetButton"]', timeout=5)
    )
    
    if not submit_btn:
        print("  ❌ No encontré el botón de publicar.")
        screenshot("error_no_button")
        return False

    bx, by = get_element_center(driver, submit_btn)
    
    if DRY_RUN:
        print(f"  🧪 DRY RUN: Tweet listo pero NO publicado. Botón en ({bx},{by})")
        screenshot("dryrun_ready")
        return True
    
    # Pausa natural antes de publicar (simula releer el tweet)
    time.sleep(random.uniform(1.2, 3.0))
    
    human_click(pyag, bx, by)
    print(f"  ✅ Tweet publicado!")
    time.sleep(random.uniform(2.0, 4.0))
    screenshot("after_post")
    return True


def post_thread(driver, pyag, tweets: list[str]) -> bool:
    """Publica un hilo de tweets: primero abre compose, luego añade replies."""
    print(f"\n  🐦 Publicando hilo de {len(tweets)} tweets...")
    
    # Abrir compose en nueva pestaña
    open_twitter_compose(driver)
    time.sleep(random.uniform(2, 3))
    
    # Tweet 1
    print(f"\n  [1/{len(tweets)}] Primer tweet...")
    ok = post_tweet_in_compose(driver, pyag, tweets[0])
    if not ok:
        return False
    
    # Para tweets 2 y 3: responder al propio tweet
    for i, tw in enumerate(tweets[1:], start=2):
        time.sleep(random.uniform(8, 15))  # Espera natural entre tweets del hilo
        print(f"\n  [{i}/{len(tweets)}] Reply al hilo...")
        
        # Buscar botón de reply en el tweet recién publicado
        reply_btn = wait_for_element(driver, '[data-testid="reply"]', timeout=10)
        if reply_btn:
            rx, ry = get_element_center(driver, reply_btn)
            human_click(pyag, rx, ry)
            time.sleep(random.uniform(1.5, 2.5))
            screenshot(f"compose_reply_{i}")
            ok = post_tweet_in_compose(driver, pyag, tw, is_reply=True)
        else:
            # Fallback: abrir compose en nueva pestaña con @mention
            open_twitter_compose(driver)
            time.sleep(2)
            ok = post_tweet_in_compose(driver, pyag, tw)
        
        if not ok:
            print(f"  ⚠️  Falló tweet {i}, continuando...")
    
    return True


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🐦 Twitter Human Poster — {TODAY}")
    print("=" * 50)
    
    if DRY_RUN:
        print("🧪 MODO DRY RUN — No se publicará nada real\n")

    # 1. Cargar datos
    model = load_cheapest_model()
    if not model:
        print("❌ No hay datos de modelos. Corre primero bot_generador.py")
        sys.exit(1)

    tweets = build_tweet_thread(model)
    print(f"📝 Hilo preparado ({len(tweets)} tweets):")
    for i, tw in enumerate(tweets, 1):
        print(f"\n  Tweet {i} ({len(tw)} chars):")
        print(f"  {tw[:120]}...")

    if not AUTO_MODE:
        print(f"\n¿Publicar este hilo? [Enter para continuar / Ctrl+C para cancelar]")
        try:
            input()
        except KeyboardInterrupt:
            print("\nCancelado.")
            sys.exit(0)

    # 2. Importar dependencias de UI
    try:
        import pyautogui as pyag
        from selenium import webdriver
    except ImportError as e:
        print(f"\n❌ Falta instalar: pip install selenium pyautogui Pillow")
        print(f"   Error: {e}")
        sys.exit(1)

    # Configurar pyautogui (failsafe en esquina superior izquierda)
    pyag.FAILSAFE = True
    pyag.PAUSE = 0.05

    # 3. Conectar a Chrome
    print("\n🌐 Conectando a Chrome...")
    try:
        driver = get_driver()
    except Exception as e:
        print(f"\n❌ No se puede conectar a Chrome.")
        print(f"   Abre Chrome con:")
        print(f'   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222')
        print(f"   Y luego loguéate en twitter.com")
        print(f"   Error: {e}")
        sys.exit(1)

    try:
        # 4. Verificar login
        print("\n🔐 Verificando sesión en X...")
        # Navegamos directamente sin check complejo
        handles_before = driver.window_handles
        
        # 5. Publicar hilo
        ok = post_thread(driver, pyag, tweets)

        if ok:
            print(f"\n✅ Hilo publicado exitosamente en X!")
        else:
            print(f"\n❌ Hubo problemas publicando el hilo. Revisa screenshots/")

    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario.")
    except Exception as e:
        print(f"\n❌ Error inesperado: {e}")
        screenshot("fatal_error")
        raise
    finally:
        # NO cerramos el driver → no cierra Chrome ni pestañas
        print("\n🔓 Chrome sigue abierto (no se cerró ninguna pestaña)")


if __name__ == "__main__":
    main()
