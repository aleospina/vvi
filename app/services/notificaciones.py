"""Aviso al asesor humano cuando entra una solicitud (RF-12).

Sin esto, una solicitud de visita solo existe en la base esperando a que alguien
abra el dashboard. En el piloto con una persona atenta funciona; con volumen
real se pierden visitas, que es justo la conversión que sostiene la comisión.

Dos canales independientes, ambos opcionales:
  · Telegram — inmediato, es el que usa el asesor en la calle.
  · Correo   — respaldo con trazabilidad.

Principio de minimización (RF-17): el aviso NO lleva los datos de contacto del
titular. Lleva el código del prospecto y un enlace al dashboard, donde el asesor
se autentica y el sistema descifra lo necesario dejando registro. Un mensaje de
Telegram o un correo son canales que no controlamos: mandar ahí el teléfono de
un titular multiplica la superficie de exposición sin necesidad.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config import settings
from app.models import Prospecto, Solicitud

log = logging.getLogger(__name__)

#: Un aviso no puede demorar el turno conversacional del comprador.
TIMEOUT = 5.0


def _texto(solicitud: Solicitud, prospecto: Prospecto) -> tuple[str, str]:
    """Devuelve (asunto, cuerpo) del aviso, sin PII del titular."""
    propiedad = solicitud.propiedad
    if propiedad is not None:
        # El separador de miles se formatea aparte: aplicar el replace sobre la
        # frase completa convertiría "Pinares, Pereira" en "Pinares. Pereira".
        precio = f"${propiedad.precio:,}".replace(",", ".")
        inmueble = f"{propiedad.zona}, {propiedad.ciudad} — {propiedad.tipo} {precio}"
    else:
        inmueble = "sin inmueble asociado"

    asunto = f"Nueva solicitud de {solicitud.tipo} — {prospecto.codigo}"
    cuerpo = (
        f"Solicitud de *{solicitud.tipo}* — {prospecto.codigo}\n\n"
        f"Inmueble: {inmueble}\n"
        f"Interés: {prospecto.ciudad or '—'} · {prospecto.tipo or '—'}\n"
        f"Score: {prospecto.score_intencion} ({prospecto.etiqueta})\n"
        f"Canal: {prospecto.canal}\n"
        f"Recibida: {solicitud.creado_en.strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Conversación y datos de contacto:\n"
        f"{settings.dashboard_url}/dashboard/prospecto/{prospecto.codigo}"
    )
    return asunto, cuerpo


def _enviar_telegram(cuerpo: str) -> bool:
    if not (settings.telegram_bot_token and settings.asesor_telegram_chat_id):
        return False
    respuesta = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={
            "chat_id": settings.asesor_telegram_chat_id,
            "text": cuerpo,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    respuesta.raise_for_status()
    return True


def _enviar_correo(asunto: str, cuerpo: str) -> bool:
    if not (settings.smtp_host and settings.asesor_email):
        return False
    mensaje = EmailMessage()
    mensaje["Subject"] = asunto
    mensaje["From"] = settings.smtp_desde or settings.smtp_usuario or settings.asesor_email
    mensaje["To"] = settings.asesor_email
    mensaje.set_content(cuerpo.replace("*", ""))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT) as servidor:
        if settings.smtp_tls:
            servidor.starttls()
        if settings.smtp_usuario:
            servidor.login(settings.smtp_usuario, settings.smtp_clave)
        servidor.send_message(mensaje)
    return True


def notificar_solicitud(solicitud: Solicitud, prospecto: Prospecto) -> list[str]:
    """Avisa al asesor por los canales configurados.

    Nunca propaga una excepción: un fallo de red no puede tumbar la solicitud de
    visita, que es el dato de negocio que sí importa. El aviso es una comodidad;
    la cola del dashboard sigue siendo la fuente de verdad.
    """
    if not settings.notificaciones_activas:
        return []

    asunto, cuerpo = _texto(solicitud, prospecto)
    enviados: list[str] = []

    for canal, envio in (
        ("telegram", lambda: _enviar_telegram(cuerpo)),
        ("correo", lambda: _enviar_correo(asunto, cuerpo)),
    ):
        try:
            if envio():
                enviados.append(canal)
        except Exception as exc:  # noqa: BLE001 - degradación deliberada
            log.warning("No se pudo notificar al asesor por %s: %s", canal, exc)

    if not enviados:
        log.info(
            "Solicitud %s registrada sin aviso: no hay canal de notificación "
            "configurado (ASESOR_TELEGRAM_CHAT_ID / SMTP_HOST).",
            solicitud.id,
        )
    return enviados
