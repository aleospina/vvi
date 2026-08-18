"""Bot de Telegram: canal único del MVP (RF-01, ADR-02).

Long-polling, sin infraestructura ni verificación de negocio. El bot es una capa
delgada: toda la lógica conversacional vive en `gateway`.

Cumplimiento: los chats que aún no han autorizado el tratamiento viven solo en
memoria (`_PENDIENTES`); nada de ellos toca la base de datos.
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
from app.db import sesion
from app.llm.prompts import PLANTILLAS
from app.models import Canal, Direccion
from app.channels import gateway
from app.channels.gateway import MensajeEntrante
from app.security.crypto import enmascarar
from app.services import leads
from app.services.compliance import (
    revocar_y_anonimizar,
    tiene_consentimiento_vigente,
)
from app.services.nlu_engine import es_afirmativo, es_negativo

log = logging.getLogger(__name__)

CANAL = Canal.TELEGRAM.value

#: Chats a los que ya se les envió el aviso de IA y esperan respuesta de
#: consentimiento. Solo en memoria y solo el identificador de chat.
_PENDIENTES: set[str] = set()

TECLADO_CONSENTIMIENTO = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("✅ Sí, autorizo", callback_data="consent:si"),
            InlineKeyboardButton("❌ No", callback_data="consent:no"),
        ]
    ]
)


# ─────────────────────────── Trabajo síncrono con la BD ───────────────────────────


def _aceptar_consentimiento(cid: str, nombre: str | None, usuario: str | None) -> list[str]:
    with sesion() as db:
        entrante = MensajeEntrante(
            canal=CANAL, canal_id=cid, texto="", nombre=nombre, usuario_canal=usuario,
            red_origen="telegram",
        )
        prospecto = gateway.alta_con_consentimiento(
            db, entrante, evidencia=f"telegram:chat_id_hash · respuesta afirmativa del titular"
        )
        salidas = [
            "¡Gracias! Tu autorización quedó registrada. 🔐",
            PLANTILLAS["calificacion"],
        ]
        for s in salidas:
            leads.registrar_mensaje(db, prospecto, Direccion.SALIENTE, s)
        return salidas


def _turno(cid: str, texto: str, nombre: str | None, usuario: str | None) -> list[str]:
    """Un turno completo de conversación. Se ejecuta fuera del bucle asíncrono."""
    with sesion() as db:
        prospecto = leads.buscar_por_canal(db, CANAL, cid)

        if prospecto is not None and tiene_consentimiento_vigente(prospecto):
            return gateway.procesar(db, prospecto, texto).textos

    # A partir de aquí no hay consentimiento vigente: nada se persiste.
    if cid in _PENDIENTES:
        if es_afirmativo(texto):
            _PENDIENTES.discard(cid)
            return _aceptar_consentimiento(cid, nombre, usuario)
        if es_negativo(texto):
            _PENDIENTES.discard(cid)
            return [gateway.rechazo_consentimiento()]
        return [
            "Necesito tu autorización explícita para continuar. ¿Autorizas el "
            "tratamiento de tus datos? Responde *Sí* o *No*."
        ]

    _PENDIENTES.add(cid)
    return [gateway.mensaje_bienvenida()]


def _mis_datos(cid: str) -> str:
    """Derecho de consulta del titular (habeas data)."""
    with sesion() as db:
        p = leads.buscar_por_canal(db, CANAL, cid)
        if p is None:
            return "No tengo ningún dato tuyo almacenado. 🙌"
        return (
            f"*Tus datos en {settings.empresa_nombre}* (código {p.codigo})\n\n"
            f"• Nombre: {enmascarar(p.nombre)}\n"
            f"• Usuario: {enmascarar(p.usuario_canal)}\n"
            f"• Teléfono: {enmascarar(p.telefono)}\n"
            f"• Ciudad: {p.ciudad or '—'} · Tipo: {p.tipo or '—'}\n"
            f"• Presupuesto: {gateway.pesos(p.presupuesto_max) if p.presupuesto_max else '—'}\n"
            f"• Estado: {p.estado}\n"
            f"• Autorización: {'vigente desde ' + p.consentimiento_ts.strftime('%d/%m/%Y') if p.consentimiento_ts else 'no otorgada'}\n\n"
            f"Tus datos de contacto están cifrados. Política: {settings.politica_privacidad_url}\n"
            "Para eliminarlos escribe /borrar."
        )


def _borrar_datos(cid: str) -> str:
    with sesion() as db:
        p = leads.buscar_por_canal(db, CANAL, cid)
        if p is None:
            return "No tengo datos tuyos que eliminar. 🙌"
        revocar_y_anonimizar(db, p, actor=f"titular:{p.codigo}")
        _PENDIENTES.discard(cid)
        return (
            "Listo: eliminé tus datos de contacto y revoqué la autorización. "
            "Queda únicamente el registro de auditoría exigido por la ley, sin datos "
            "que te identifiquen. Si algún día quieres volver, escribe /start."
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
    _PENDIENTES.add(cid)
    await _responder(update, [gateway.mensaje_bienvenida()], TECLADO_CONSENTIMIENTO)


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
    await _responder(update, [await asyncio.to_thread(_mis_datos, cid)])


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    await _responder(update, [await asyncio.to_thread(_borrar_datos, cid)])


def _pedir_asesor(cid: str) -> str:
    with sesion() as db:
        p = leads.buscar_por_canal(db, CANAL, cid)
        if p is None or not tiene_consentimiento_vigente(p):
            return (
                "Para pasarte con un asesor necesito primero tu autorización. "
                "Escribe /start y seguimos. 🙌"
            )
        leads.solicitar_handoff(db, p, tipo="asesor", detalle="Solicitado con /asesor")
        return PLANTILLAS["handoff"].format(empresa=settings.empresa_nombre)


async def cmd_asesor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = str(update.effective_chat.id)
    await _responder(update, [await asyncio.to_thread(_pedir_asesor, cid)])


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
        _PENDIENTES.discard(cid)
        textos = await asyncio.to_thread(
            _aceptar_consentimiento, cid, usuario.full_name, usuario.username
        )
    else:
        _PENDIENTES.discard(cid)
        textos = [gateway.rechazo_consentimiento()]

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
            _turno, cid, texto, usuario.full_name, usuario.username
        )
    except Exception:
        log.exception("Error procesando mensaje de Telegram")
        textos = [
            "Uy, tuve un problema técnico procesando tu mensaje. ¿Lo intentas de nuevo?"
        ]

    teclado = TECLADO_CONSENTIMIENTO if cid in _PENDIENTES else None
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
