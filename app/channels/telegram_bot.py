"""Bot de Telegram: adaptador de transporte (RF-01, ADR-02).

Long-polling, sin infraestructura ni verificación de negocio. Este módulo es una
capa delgada de traducción: convierte updates de Telegram en llamadas a
`conversacion` y devuelve los textos que le entreguen. La máquina de
consentimiento, los derechos de habeas data y el acceso a la base de datos viven
en `conversacion`, compartidos con los demás canales.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.models import Canal
from app.channels import conversacion
from app.channels.gateway import mensaje_bienvenida

log = logging.getLogger(__name__)

CANAL = Canal.TELEGRAM.value

TECLADO_CONSENTIMIENTO = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Sí, autorizo", callback_data="consent:si"),
            InlineKeyboardButton("❌ No", callback_data="consent:no"),
        ]
    ]
)


# ─────────────────────────── Handlers ───────────────────────────


async def _responder(update: Update, textos: list[str], teclado=None) -> None:
    destino = update.effective_chat
    for i, t in enumerate(textos):
        await destino.send_message(
            t,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=teclado if i == len(textos) - 1 else None,
            disable_web_page_preview=True,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    conversacion.marcar_pendiente(CANAL, cid)
    await _responder(update, [mensaje_bienvenida()], TECLADO_CONSENTIMIENTO)


async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _responder(
        update,
        [
            f"Soy el asistente virtual con IA de {settings.empresa_nombre}.\n\n"
            "• /start — iniciar o reiniciar la conversación\n"
            "• /misdatos — ver qué datos tuyos tengo\n"
            "• /borrar — eliminar mis datos (habeas data)\n"
            "• /asesor — hablar con una persona\n\n"
            f"Cobertura actual: {' y '.join(settings.ciudades_cobertura)}."
        ],
    )


async def cmd_misdatos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    await _responder(update, [await asyncio.to_thread(conversacion.mis_datos, CANAL, cid)])


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    await _responder(update, [await asyncio.to_thread(conversacion.borrar_datos, CANAL, cid)])


async def cmd_asesor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    await _responder(update, [await asyncio.to_thread(conversacion.pedir_asesor, CANAL, cid)])


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Devuelve el chat_id de quien pregunta, para configurar al asesor.

    No expone nada de nadie más: cada quien solo ve su propio identificador. Es
    el único modo de obtener el dato — Telegram no permite dirigirse a alguien
    por su correo ni por su usuario, solo por un chat que ya inició con el bot.
    """
    cid = str(update.effective_chat.id)
    configurado = settings.asesor_telegram_chat_id == cid
    await _responder(
        update,
        [
            f"Tu chat_id es `{cid}`\n\n"
            + (
                "✅ Ya estás configurado como asesor: recibirás los avisos de "
                "solicitudes de visita."
                if configurado
                else "Para recibir los avisos de solicitudes, pon esta línea en el "
                f"archivo `.env` y reinicia:\n\n`ASESOR_TELEGRAM_CHAT_ID={cid}`"
            )
        ],
    )


async def on_consentimiento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    consulta = update.callback_query
    await consulta.answer()
    cid = str(update.effective_chat.id)
    usuario = update.effective_user

    if consulta.data == "consent:si":
        textos = await asyncio.to_thread(
            conversacion.aceptar_consentimiento,
            CANAL,
            cid,
            nombre=usuario.full_name,
            usuario=usuario.username,
        )
    else:
        textos = conversacion.rechazar_consentimiento(CANAL, cid)

    await _responder(update, textos)


async def on_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    usuario = update.effective_user
    texto = (update.message.text or "").strip()
    if not texto:
        return

    await context.bot.send_chat_action(chat_id=cid, action="typing")
    try:
        textos = await asyncio.to_thread(
            conversacion.turno,
            CANAL,
            cid,
            texto,
            nombre=usuario.full_name,
            usuario=usuario.username,
        )
    except Exception:
        log.exception("Error procesando mensaje de Telegram")
        textos = [
            "Uy, tuve un problema técnico procesando tu mensaje. ¿Lo intentas de nuevo?"
        ]

    teclado = TECLADO_CONSENTIMIENTO if conversacion.esta_pendiente(CANAL, cid) else None
    await _responder(update, textos, teclado)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Excepción en el bot", exc_info=context.error)


def aviso_de_red(error: Exception) -> None:
    """Resume un fallo de `get_updates` en una línea.

    Sin esto, cada intento fallido vuelca unas 140 líneas de traza: una noche
    sin internet deja el log inservible para diagnosticar cualquier otra cosa.
    El corte es transitorio y python-telegram-bot reintenta solo, así que basta
    con dejar constancia de que ocurrió.
    """
    log.warning(
        "Sin conexión con Telegram (%s). Se reintenta automáticamente.",
        type(error).__name__,
    )


# ─────────────────────────── Construcción ───────────────────────────


def construir_app() -> Application | None:
    """Devuelve la aplicación de Telegram, o None si no hay token configurado."""
    if not settings.telegram_bot_token:
        log.warning(
            "TELEGRAM_BOT_TOKEN no configurado: la API y el dashboard funcionan, "
            "pero el bot no se inicia."
        )
        return None

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["ayuda", "help"], cmd_ayuda))
    app.add_handler(CommandHandler("misdatos", cmd_misdatos))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("asesor", cmd_asesor))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CallbackQueryHandler(on_consentimiento, pattern=r"^consent:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_mensaje))
    app.add_error_handler(on_error)
    return app
