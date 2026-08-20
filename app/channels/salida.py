"""Mensajes que el sistema inicia, no que contesta (ADR-02).

Todo lo demás en `channels/` es reactivo: llega un mensaje, se responde por el
mismo hilo que lo trajo. El seguimiento al comprador es lo contrario —nadie
escribió, y aun así hay que escribirle—, y eso necesita saber cómo alcanzar a
una persona partiendo solo de su ficha.

Dos reglas que no se negocian:

  · **Consentimiento vigente.** Un mensaje saliente a quien revocó su
    autorización es exactamente el contacto no autorizado que el sistema entero
    está construido para impedir (RF-19).
  · **Nunca revienta.** Que el canal esté caído, mal configurado o que el
    número ya no exista no puede tumbar la tarea que recorre la cola. Se
    registra y se sigue.
"""

from __future__ import annotations

import logging

import httpx

from app.channels import whatsapp_evo
from app.config import settings
from app.models import Prospecto
from app.services.compliance import tiene_consentimiento_vigente

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)


def _telegram(chat_id: str, texto: str) -> bool:
    """Envío directo por la API de Telegram, sin depender del bot en ejecución.

    La tarea de seguimiento corre en el mismo proceso que el bot, pero alcanzar
    su instancia desde una capa de servicio ataría el envío a que el long-polling
    esté vivo. Un POST es más simple y falla solo por lo que de verdad importa.
    """
    if not settings.telegram_bot_token:
        return False
    respuesta = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": texto, "parse_mode": "Markdown"},
        timeout=TIMEOUT,
    )
    respuesta.raise_for_status()
    return True


def destino(prospecto: Prospecto) -> str | None:
    """Por dónde se le puede escribir, o None si no hay forma.

    En WhatsApp el identificador de canal es el número. En Telegram es el
    `chat_id`, que solo existe para los prospectos dados de alta después de que
    se empezó a guardar cifrado: a los anteriores no hay cómo escribirles y el
    seguimiento simplemente no los toca.
    """
    return prospecto.canal_id or prospecto.telefono


def enviar(prospecto: Prospecto, texto: str) -> bool:
    """Escribe al prospecto por su canal. False si no se pudo (nunca lanza)."""
    if not tiene_consentimiento_vigente(prospecto):
        log.warning(
            "Envío saliente bloqueado: %s no tiene consentimiento vigente.",
            prospecto.codigo,
        )
        return False

    a_donde = destino(prospecto)
    if not a_donde:
        return False

    try:
        if prospecto.canal == "whatsapp":
            return whatsapp_evo.enviar_texto(a_donde, texto)
        if prospecto.canal == "telegram":
            return _telegram(a_donde, texto)
    except Exception:  # noqa: BLE001 - un canal caído no puede parar la cola
        log.warning("No se pudo escribir a %s por %s.", prospecto.codigo, prospecto.canal,
                    exc_info=True)
        return False

    log.warning("Canal desconocido para %s: %s", prospecto.codigo, prospecto.canal)
    return False
