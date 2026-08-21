"""Punto de entrada: API + dashboard + bot de Telegram en un solo proceso."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import RAIZ, settings
from app.db import inicializar
from app.channels.telegram_bot import aviso_de_red, construir_app
from app.routers import api, captacion, catalogo, dashboard, whatsapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s · %(message)s",
    datefmt="%H:%M:%S",
)
# httpx registra cada petición con su URL completa, y el long-polling de Telegram
# lleva el token del bot dentro de la ruta: en INFO el secreto queda escrito en
# los logs una vez cada diez segundos (RNF-05).
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("vvi")


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    inicializar()
    log.info("Base de datos lista (%s)", settings.database_url)

    if settings.tiene_llm:
        log.info("LLM de runtime: %s (primario)", settings.llm_provider)
    else:
        log.warning(
            "Sin llaves de LLM: la clasificación funciona solo con reglas. "
            "Configura MOONSHOT_API_KEY o ANTHROPIC_API_KEY para la versión completa."
        )

    if settings.tiene_whatsapp:
        log.info("Canal WhatsApp activo (Evolution API en %s).", settings.evolution_url)

    bot = construir_app()
    app.state.bot = bot
    # El bot arranca en segundo plano a propósito. Antes se esperaba aquí, y
    # como `bootstrap_retries` es 0 por defecto, un `get_me` fallido por falta
    # de red propagaba y tumbaba el arranque entero: sin dashboard y sin la
    # landing `/publicar`, que no necesitan internet para nada.
    app.state.tarea_bot = asyncio.create_task(_levantar_bot(bot)) if bot else None

    try:
        yield
    finally:
        await _detener_bot(app.state.tarea_bot, bot)


async def _levantar_bot(bot) -> None:
    """Conecta el bot y se queda escuchando. Reintenta el arranque sin límite."""
    try:
        await bot.initialize()
        await bot.start()
        await bot.updater.start_polling(
            drop_pending_updates=True,
            bootstrap_retries=-1,        # sin tope: si no hay red, sigue intentando
            error_callback=aviso_de_red,
        )
        log.info("Bot de Telegram escuchando (long-polling).")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "El bot de Telegram no pudo iniciar. La API, el dashboard y la landing "
            "siguen operando con normalidad."
        )


async def _detener_bot(tarea, bot) -> None:
    """Cierra el bot sin dejar que un fallo suyo estropee el apagado."""
    if tarea is not None and not tarea.done():
        tarea.cancel()
        with suppress(asyncio.CancelledError):
            await tarea
    if bot is None:
        return
    try:
        if bot.updater is not None and bot.updater.running:
            await bot.updater.stop()
        if bot.running:
            await bot.stop()
        await bot.shutdown()
        log.info("Bot de Telegram detenido.")
    except Exception:  # noqa: BLE001 - apagar nunca debe reventar
        log.warning("El bot de Telegram no cerró limpiamente.", exc_info=True)


app = FastAPI(
    title="VVI — Vendedor Virtual Inmobiliario",
    version=__version__,
    summary="Agente de IA que atiende, califica y empareja compradores de vivienda.",
    description=(
        "MVP inbound / opt-in para Medellín y Pereira.\n\n"
        "**Cumplimiento:** el sistema no rastrea ni contacta en frío a personas en redes "
        "sociales (Ley 1581/2012 y ToS de las plataformas). Toda PII entra con "
        "autorización registrada, se cifra en reposo y toda acción queda auditada. "
        "La comisión solo se genera cuando un operador humano confirma la venta."
    ),
    lifespan=ciclo_vida,
)

# El montaje de fotos va PRIMERO: Starlette resuelve rutas en orden, y si
# `/static` se monta antes, tapa a `/static/fotos` y las imágenes del volumen
# darían 404. El directorio se crea al arrancar porque StaticFiles exige que
# exista, y en un despliegue nuevo el volumen viene vacío.
settings.ruta_fotos.mkdir(parents=True, exist_ok=True)
app.mount("/static/fotos", StaticFiles(directory=str(settings.ruta_fotos)), name="fotos")
app.mount("/static", StaticFiles(directory=str(RAIZ / "app" / "static")), name="static")
app.include_router(api.router)
app.include_router(captacion.router)
app.include_router(catalogo.router)
app.include_router(dashboard.router)
app.include_router(whatsapp.router)


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/dashboard")


@app.get("/health", tags=["operación"])
def salud():
    return {
        "estado": "ok",
        "version": __version__,
        "canal_telegram": bool(settings.telegram_bot_token),
        # Solo si está configurado: consultar el estado real a Evolution en
        # cada healthcheck de la plataforma sería una llamada de red por minuto.
        "canal_whatsapp": settings.tiene_whatsapp,
        "llm": settings.llm_provider if settings.tiene_llm else "reglas",
        "comision_pct": settings.comision_pct,
        "ciudades": list(settings.ciudades_cobertura),
    }
