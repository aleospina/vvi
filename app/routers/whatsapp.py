"""Webhook entrante de WhatsApp vía Evolution API (ADR-02b).

Evolution empuja cada evento de la instancia a esta ruta. El contrato relevante
es `messages.upsert`: un mensaje que entró (o que salió, si `fromMe`).

Tres decisiones que no son opcionales aquí:

1. **Responder 200 de inmediato.** El turno conversacional puede llamar al LLM y
   tardar segundos; Evolution reintenta si el webhook demora, y el comprador
   recibiría la misma respuesta dos veces. El trabajo va a `BackgroundTasks`.
2. **Idempotencia por `key.id`.** Los reintentos existen igual. Sin deduplicar,
   se duplican también los mensajes en la auditoría del prospecto.
3. **Autenticación por capas.** Evolution no firma sus webhooks con HMAC, así
   que se combinan un segmento secreto en la ruta y la verificación del `apikey`
   que viene en el propio cuerpo. Ninguna de las dos sola es suficiente.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.channels import conversacion, whatsapp_evo
from app.config import settings
from app.models import Canal
from app.services import ajustes, notificaciones

log = logging.getLogger(__name__)
router = APIRouter(tags=["whatsapp"])

CANAL = Canal.WHATSAPP.value

#: Mensajes ya atendidos, para descartar reintentos de Evolution.
#: En memoria a propósito: con una sola réplica (railway.toml) alcanza, y un
#: duplicado tras un reinicio es un daño menor comparado con meter otra tabla.
_VISTOS: OrderedDict[str, float] = OrderedDict()
VENTANA_DEDUPE = 600.0  # segundos
MAX_VISTOS = 2000


def _ya_visto(mensaje_id: str) -> bool:
    ahora = time.monotonic()
    while _VISTOS and (ahora - next(iter(_VISTOS.values())) > VENTANA_DEDUPE):
        _VISTOS.popitem(last=False)
    if mensaje_id in _VISTOS:
        return True
    _VISTOS[mensaje_id] = ahora
    while len(_VISTOS) > MAX_VISTOS:
        _VISTOS.popitem(last=False)
    return False


def _apikey_valida(enviada: str) -> bool:
    """¿La `apikey` del cuerpo corresponde a este Evolution?

    Hay dos claves válidas y confundirlas deja el canal mudo: la global de la
    API, y el **token propio de la instancia**, que es con el que Evolution
    firma los eventos. Comparar solo contra la global rechaza todos los webhooks
    con un 404 que en los logs de VVI no aparece como error de nada.
    """
    if not enviada:
        return True  # hay versiones que no la mandan; la ruta secreta ya autenticó
    if settings.evolution_api_key and secrets.compare_digest(
        str(enviada), settings.evolution_api_key
    ):
        return True
    propia = whatsapp_evo.token_instancia()
    if propia and secrets.compare_digest(str(enviada), propia):
        return True
    # Si la instancia se recrea, su token cambia y el que tenemos en caché queda
    # viejo: sin este reintento, el canal se cae durante todo el TTL de la caché
    # y los logs de VVI no muestran nada — el 404 solo se ve del lado de Evolution.
    propia = whatsapp_evo.token_instancia(refrescar=True)
    return bool(propia) and secrets.compare_digest(str(enviada), propia)


def _autenticado(token: str, cuerpo: dict) -> bool:
    """Ruta secreta (la barrera real, 256 bits) + apikey del cuerpo."""
    if not settings.evolution_webhook_token:
        return False
    if not secrets.compare_digest(token, settings.evolution_webhook_token):
        return False
    return _apikey_valida(cuerpo.get("apikey") or "")


def atender(numero: str, texto: str, nombre: str | None) -> None:
    """Procesa un mensaje y responde. Corre fuera del ciclo de la petición."""
    whatsapp_evo.escribiendo(numero)
    try:
        textos = conversacion.turno(
            CANAL, numero, texto, nombre=nombre, telefono=numero
        )
    except Exception:
        log.exception("Error procesando mensaje de WhatsApp")
        textos = ["Uy, tuve un problema técnico procesando tu mensaje. ¿Lo intentas de nuevo?"]

    for salida in textos:
        try:
            whatsapp_evo.enviar_texto(numero, salida)
        except Exception:  # noqa: BLE001 - un envío fallido no debe cortar el resto
            log.exception("No se pudo enviar una respuesta por WhatsApp")


def _huella(numero: str) -> str:
    """Últimos dos dígitos, para correlacionar en el log sin escribir el teléfono.

    Un log es un sitio del que la PII no sale nunca más (RF-17): con esto se
    distingue un remitente de otro sin dejar el número escrito en disco.
    """
    return f"…{numero[-2:]}" if len(numero) >= 2 else "?"


def _mensaje_entrante(datos: dict, tareas: BackgroundTasks) -> None:
    clave = datos.get("key") or {}
    jid = str(clave.get("remoteJid") or "")
    numero = whatsapp_evo.numero_de_jid(jid)

    # Cada descarte se registra con su motivo. Sin esto, un canal que no
    # responde es indistinguible de un canal que no recibe nada: fue justo lo
    # que dejó a ciegas la primera prueba real.
    if clave.get("fromMe"):
        log.debug("WhatsApp: descartado mensaje propio (saliente).")
        return
    if not whatsapp_evo.es_chat_individual(jid):
        # DEBUG y no INFO: si el número vinculado pertenece a una persona con
        # vida social, los grupos generan decenas de eventos por minuto y ahogan
        # el log. Es el caso normal, no una anomalía que merezca atención.
        log.debug("WhatsApp: descartado por no ser chat individual (grupo o estado).")
        return
    if _ya_visto(str(clave.get("id") or "")):
        log.info("WhatsApp: descartado duplicado de %s (reintento de Evolution).", _huella(numero))
        return

    # Modo pruebas: con lista blanca configurada, el bot calla ante cualquier
    # otro número. La lista se lee en cada mensaje porque se puede cambiar desde
    # el dashboard sin reiniciar: leerla una vez al arrancar haría que el cambio
    # solo surtiera efecto en el próximo despliegue, que es justo lo que se
    # quería evitar. Es lo que permite vincular un teléfono personal sin que un
    # familiar reciba el flujo de consentimiento — y sin que su mensaje entre al
    # motor. El silencio es deliberado: responder "no estás autorizado" sería
    # contestarle igual a quien no debía recibir nada.
    permitidos = ajustes.numeros_prueba()
    if permitidos and numero not in permitidos:
        log.info(
            "WhatsApp: %s no está en la lista de pruebas (%d autorizados): ignorado.",
            _huella(numero),
            len(permitidos),
        )
        return

    texto = whatsapp_evo.texto_de_mensaje(datos.get("message") or {})

    if not texto:
        # Audio, imagen, ubicación, sticker. Decirlo es mejor que el silencio:
        # la persona cree que la están ignorando y se va.
        log.info(
            "WhatsApp: mensaje de %s sin texto (%s): se responde que solo leo texto.",
            _huella(numero),
            datos.get("messageType") or "tipo desconocido",
        )
        tareas.add_task(
            whatsapp_evo.enviar_texto,
            numero,
            "Por ahora solo puedo leer mensajes de texto 🙏 ¿Me lo escribes?",
        )
        return

    log.info("WhatsApp: mensaje de %s aceptado, procesando turno.", _huella(numero))
    tareas.add_task(atender, numero, texto, datos.get("pushName"))


def _conexion(datos: dict) -> None:
    estado = str(datos.get("state") or datos.get("statusReason") or "")
    if estado == "close":
        log.error("La sesión de WhatsApp se cerró: hay que volver a escanear el QR.")
        notificaciones.avisar_operador(
            "WhatsApp desconectado",
            "⚠️ La sesión de WhatsApp se cerró. El canal está caído hasta que "
            "alguien vuelva a vincular el número:\n"
            "`python deploy/evolution/configurar.py`",
        )
    else:
        log.info("Estado de la conexión de WhatsApp: %s", estado or "desconocido")


@router.post("/webhooks/whatsapp/{token}", include_in_schema=False)
async def entrante(token: str, request: Request, tareas: BackgroundTasks):
    try:
        cuerpo = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="cuerpo no es JSON")

    if not isinstance(cuerpo, dict) or not _autenticado(token, cuerpo):
        # 404 y no 403: a quien tantea rutas no se le confirma que existe.
        raise HTTPException(status_code=404)

    evento = str(cuerpo.get("event") or "").lower().replace("_", ".")
    datos = cuerpo.get("data")

    if evento == "messages.upsert" and isinstance(datos, dict):
        _mensaje_entrante(datos, tareas)
    elif evento == "connection.update" and isinstance(datos, dict):
        _conexion(datos)
    elif evento == "qrcode.updated" and isinstance(datos, dict):
        # Evolution rota el código solo. Guardarlo aquí es lo que permite al
        # panel enseñar uno fresco sin pedir otra conexión: pedirla reinicia el
        # socket y arruina el emparejamiento en curso.
        whatsapp_evo.guardar_qr(datos)
    else:
        # Evolution puede mandar eventos a los que no estamos suscritos. Verlos
        # en el log es la diferencia entre "no llega nada" y "llega otra cosa".
        log.info("WhatsApp: evento %r ignorado.", evento or "sin nombre")

    # Siempre 200: un error aquí solo provoca reintentos que no arreglan nada.
    return {"ok": True}
