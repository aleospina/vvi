"""Canal Instagram a través de la Messaging API de Meta (ADR-02c).

Este módulo es el lado *saliente*: traduce "mandar texto a alguien" en la
llamada correspondiente. Lo entrante llega por webhook a `app.routers.instagram`.
La máquina de consentimiento, los derechos de habeas data y el acceso a la base
de datos viven en `conversacion`, compartidos con Telegram y WhatsApp.

A diferencia de WhatsApp (ADR-02b), esto **sí es API oficial**: no hay riesgo de
que la cuenta se restrinja por usar un cliente no soportado. A cambio, Instagram
impone tres límites que ni Telegram ni WhatsApp tienen, y que son la razón de que
este archivo no sea una copia del de Telegram:

  1. **1000 bytes por mensaje** — bytes UTF-8, no caracteres. En español eso se
     gasta rápido: `²` y `·` ocupan 2 bytes y cada emoji 4. Un listado de diez
     inmuebles pasa de 2000 bytes con facilidad, así que hay que trocearlo, y
     hay que trocearlo por la costura correcta: partir una ficha a la mitad es
     peor que no mandarla.
  2. **Sin Markdown.** Las plantillas escriben `*negrita*`, que Telegram
     renderiza y WhatsApp entiende de forma nativa. Instagram muestra los
     asteriscos tal cual, así que aquí se limpian.
  3. **Ventana de 24 horas.** Solo se puede escribir dentro de las 24 h desde el
     último mensaje del titular; con la etiqueta `HUMAN_AGENT` se estira a 7
     días y ahí se acaba. Eso lo aplica `channels.salida`, que es quien inicia
     conversaciones; contestar un webhook siempre cae dentro de la ventana.

Los tres son problemas de transporte y por eso se resuelven aquí: el gateway
sigue devolviendo los mismos textos para los tres canales.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

import httpx

from app.config import settings

log = logging.getLogger(__name__)

#: Un envío no puede colgar el turno conversacional indefinidamente.
TIMEOUT = 15.0

#: Tope de un DM de Instagram, en **bytes** UTF-8. El nombre lo dice porque
#: medirlo con `len()` es el error que hace que el mensaje se rechace justo
#: cuando trae acentos o emojis, es decir, casi siempre.
TOPE_BYTES = 1000

#: Etiqueta que permite escribir fuera de la ventana de 24 h, hasta 7 días. Es
#: para respuestas de un agente humano: el seguimiento al comprador es
#: exactamente eso —una pregunta de la inmobiliaria sobre un negocio en curso,
#: no una promoción—. Pasados los 7 días no hay etiqueta que valga.
ETIQUETA_HUMANO = "HUMAN_AGENT"

#: Respuestas rápidas para la puerta de consentimiento. Es el equivalente del
#: teclado inline de Telegram: en WhatsApp no hay botones fiables y la
#: autorización llega escrita, pero aquí sí los hay y un toque se equivoca menos
#: que un "sí" mecanografiado. El título no puede pasar de 20 caracteres.
RESPUESTAS_CONSENTIMIENTO = [
    {"content_type": "text", "title": "✅ Sí, autorizo", "payload": "consent:si"},
    {"content_type": "text", "title": "❌ No", "payload": "consent:no"},
]


# ─────────────────────────── Formato de salida ───────────────────────────

#: Marcado que las plantillas escriben pensando en Telegram y WhatsApp. Se
#: desenvuelve solo cuando está emparejado: un asterisco suelto en una
#: descripción que cargó el operador es texto suyo, no marcado nuestro.
_NEGRITA = re.compile(r"\*(\S(?:[^*\n]*\S)?)\*")
_CURSIVA = re.compile(r"(?<!\w)_(\S(?:[^_\n]*\S)?)_(?!\w)")
_CODIGO = re.compile(r"`([^`\n]+)`")


def a_texto_plano(texto: str) -> str:
    """Quita el marcado que Instagram no renderiza.

    Sin esto el comprador lee `*Centro, Pereira*` con los asteriscos a la vista
    en cada línea de cada ficha, que es peor que no tener negrita.
    """
    for patron in (_NEGRITA, _CURSIVA, _CODIGO):
        texto = patron.sub(r"\1", texto)
    return texto


def _bytes(texto: str) -> int:
    return len(texto.encode("utf-8"))


def _cortar_duro(texto: str, tope: int) -> list[str]:
    """Último recurso: partir por bytes sin romper un carácter por la mitad.

    Aquí solo llega una palabra suelta de más de 1000 bytes —una URL enorme, un
    pegote sin espacios—. Cortar el `bytes` a pelo dejaría media secuencia UTF-8
    en cada mensaje y ninguno de los dos se podría decodificar.
    """
    trozos: list[str] = []
    bruto = texto.encode("utf-8")
    while bruto:
        cabeza, bruto = bruto[:tope], bruto[tope:]
        while bruto and (bruto[0] & 0xC0) == 0x80:  # byte de continuación
            bruto = cabeza[-1:] + bruto
            cabeza = cabeza[:-1]
        trozos.append(cabeza.decode("utf-8"))
    return trozos


def _agrupar(piezas: list[str], separador: str, tope: int) -> list[str]:
    """Junta piezas consecutivas mientras quepan, reponiendo el separador."""
    salida: list[str] = []
    actual = ""
    for pieza in piezas:
        candidato = f"{actual}{separador}{pieza}" if actual else pieza
        if _bytes(candidato) <= tope:
            actual = candidato
            continue
        if actual:
            salida.append(actual)
            actual = ""
        if _bytes(pieza) <= tope:
            actual = pieza
        else:
            # La pieza sola tampoco cabe: se busca una costura más fina dentro.
            finos = trocear(pieza, tope)
            salida.extend(finos[:-1])
            actual = finos[-1]
    if actual:
        salida.append(actual)
    return salida


def trocear(texto: str, tope: int = TOPE_BYTES) -> list[str]:
    """Parte un texto en mensajes que quepan en un DM.

    Se corta por la costura más gruesa que sirva: primero entre bloques (línea
    en blanco), que es justo el separador con el que el gateway arma las fichas;
    solo si un bloque sigue sin caber se baja a líneas y luego a palabras. Así un
    listado largo llega como varias fichas enteras y no como fichas mutiladas.
    """
    if _bytes(texto) <= tope:
        return [texto]
    for separador in ("\n\n", "\n", " "):
        if separador in texto:
            return _agrupar(texto.split(separador), separador, tope)
    return _cortar_duro(texto, tope)


# ─────────────────────────── Cliente HTTP ───────────────────────────


def _cliente() -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.instagram_api_base.rstrip('/')}/{settings.instagram_version}",
        headers={"Authorization": f"Bearer {settings.instagram_token}"},
        timeout=TIMEOUT,
    )


def _ruta_mensajes() -> str:
    """El id explícito de la cuenta si está configurado; `me` lo resuelve igual."""
    return f"/{settings.instagram_cuenta_id or 'me'}/messages"


def enviar_texto(
    igsid: str,
    texto: str,
    *,
    respuestas: list[dict] | None = None,
    etiqueta: str | None = None,
) -> bool:
    """Envía un mensaje, troceado si hace falta. False si el canal no está configurado.

    Los trozos van en llamadas secuenciales para que lleguen en orden: Instagram
    no garantiza el orden de envíos concurrentes, y una ficha que aparece antes
    que su encabezado se lee como si fuera de otro inmueble.
    """
    if not settings.tiene_instagram:
        log.warning("Instagram no configurado: mensaje descartado.")
        return False

    trozos = trocear(a_texto_plano(texto))
    with _cliente() as c:
        for i, trozo in enumerate(trozos):
            mensaje: dict = {"text": trozo}
            # Las respuestas rápidas solo en el último trozo: repetidas en cada
            # uno, el comprador ve tres veces el mismo par de botones.
            if respuestas and i == len(trozos) - 1:
                mensaje["quick_replies"] = respuestas
            cuerpo: dict = {"recipient": {"id": igsid}, "message": mensaje}
            if etiqueta:
                cuerpo["messaging_type"] = "MESSAGE_TAG"
                cuerpo["tag"] = etiqueta
            r = c.post(_ruta_mensajes(), json=cuerpo)
            r.raise_for_status()
    return True


def escribiendo(igsid: str) -> None:
    """Marca el mensaje como visto y muestra 'escribiendo…'.

    Cosmético, y por eso los fallos se tragan: el turno no puede caerse porque
    un indicador de actividad no llegara.
    """
    if not settings.tiene_instagram:
        return
    try:
        with _cliente() as c:
            for accion in ("mark_seen", "typing_on"):
                c.post(
                    _ruta_mensajes(),
                    json={"recipient": {"id": igsid}, "sender_action": accion},
                )
    except httpx.HTTPError as e:  # noqa: BLE001 - señal cosmética
        log.debug("No se pudo enviar presencia a Instagram: %s", e)


def perfil_usuario(igsid: str) -> dict:
    """Nombre y usuario de quien escribe, o {} si no se pudo consultar.

    Instagram no entrega teléfono ni correo: lo único que se puede saber de un
    titular es lo que él mismo publica en su perfil. Es menos PII de la que llega
    por WhatsApp, no más.
    """
    if not settings.tiene_instagram:
        return {}
    try:
        with _cliente() as c:
            r = c.get(f"/{igsid}", params={"fields": "name,username"})
            r.raise_for_status()
            datos = r.json()
        return datos if isinstance(datos, dict) else {}
    except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
        log.debug("No se pudo leer el perfil de Instagram: %s", e)
        return {}


def diagnostico() -> dict:
    """Estado del canal en una sola llamada, para el panel del operador.

    `estado` es: conectado | token_invalido | no_configurado | error. La
    distinción entre los dos primeros importa: un token caducado deja el canal
    mudo sin ningún error del lado de VVI, exactamente el mismo síntoma que tuvo
    WhatsApp cuando se comparaba contra la apikey equivocada.
    """
    if not settings.tiene_instagram:
        return {"estado": "no_configurado"}
    try:
        with _cliente() as c:
            r = c.get("/me", params={"fields": "user_id,username"})
            if r.status_code in (400, 401, 403):
                return {"estado": "token_invalido", "detalle": r.text[:200]}
            r.raise_for_status()
            datos = r.json()
    except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
        log.warning("No se pudo consultar el estado de Instagram: %s", e)
        return {"estado": "error", "detalle": str(e)[:200]}
    return {
        "estado": "conectado",
        "usuario": str(datos.get("username") or ""),
        "cuenta_id": str(datos.get("user_id") or datos.get("id") or ""),
    }


# ─────────────────────────── Entrante ───────────────────────────


def url_webhook() -> str:
    """La URL que hay que declarar en el panel de la app de Meta."""
    return f"{settings.url_publica}/webhooks/instagram"


def firma_valida(cuerpo: bytes, cabecera: str | None) -> bool:
    """Verifica el `X-Hub-Signature-256` sobre el cuerpo **crudo**.

    Meta sí firma sus webhooks, que es la diferencia con Evolution y la razón de
    que aquí no haga falta esconder la ruta: la firma es la barrera. Se calcula
    sobre los bytes tal como llegaron —volver a serializar el JSON cambia
    espacios y orden, y la firma deja de coincidir sin que nada lo explique.

    Sin app secret configurado devuelve False: un webhook público sin verificar
    es una puerta abierta para que cualquiera inyecte conversaciones.
    """
    if not settings.instagram_app_secret:
        return False
    if not cabecera or not cabecera.startswith("sha256="):
        return False
    esperada = hmac.new(
        settings.instagram_app_secret.encode("utf-8"), cuerpo, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(esperada, cabecera.split("=", 1)[1])


def es_eco(mensaje: dict) -> bool:
    """¿Es un mensaje que mandamos nosotros? (el `fromMe` de WhatsApp).

    Meta devuelve por webhook los mensajes salientes de la propia cuenta. Sin
    este filtro el bot se contesta a sí mismo, en bucle.
    """
    return bool(mensaje.get("is_echo"))


def texto_de_mensaje(mensaje: dict) -> str | None:
    """El texto del mensaje, o None si no es texto.

    Una respuesta a una historia llega igual que un DM normal, con el texto en
    `text` y una referencia en `reply_to`: es un mensaje de texto y se atiende
    como tal —además es de los mejores inbound que da la red—. Lo que no trae
    texto (una foto, un audio, un reel compartido) vale None y el router contesta
    que por ahora solo leemos texto.
    """
    if not isinstance(mensaje, dict):
        return None
    texto = mensaje.get("text")
    return texto.strip() if isinstance(texto, str) and texto.strip() else None


def respuesta_rapida(mensaje: dict) -> str | None:
    """El `payload` del botón que pulsó el titular, si pulsó uno."""
    rapida = mensaje.get("quick_reply")
    if not isinstance(rapida, dict):
        return None
    payload = rapida.get("payload")
    return str(payload) if payload else None
