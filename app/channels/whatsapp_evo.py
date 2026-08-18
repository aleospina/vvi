"""Canal WhatsApp a través de Evolution API (ADR-02b).

Evolution API es un servicio aparte (Node) que habla el protocolo de WhatsApp
Web mediante Baileys y expone HTTP. Este módulo es el lado *saliente*: traduce
"mandar texto a alguien" en la llamada correspondiente. Lo entrante llega por
webhook a `app.routers.whatsapp`.

Por qué contra Evolution y no contra Baileys directo: Evolution soporta la misma
API con `integration: WHATSAPP-BAILEYS` (no oficial, sin verificación) y con
`WHATSAPP-BUSINESS` (Cloud API oficial de Meta). El día que salga el WABA se
cambia el tipo de instancia y **este archivo no cambia**. Esa es la salida
documentada del riesgo que ADR-02 rechazó en su momento.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)

#: Un envío no puede colgar el turno conversacional indefinidamente.
TIMEOUT = 15.0

#: Sufijos de JID que WhatsApp usa para cosas que NO son un chat 1 a 1.
JID_GRUPO = "@g.us"
JID_ESTADOS = "status@broadcast"


def numero_de_jid(jid: str) -> str:
    """`573001234567@s.whatsapp.net` → `573001234567`.

    Evolution acepta el número pelado en los envíos y así el identificador de
    canal queda estable aunque el sufijo del JID cambie entre versiones.
    """
    return jid.split("@", 1)[0].split(":", 1)[0]


def es_chat_individual(jid: str) -> bool:
    """Descarta grupos y estados.

    Sin este filtro, meter el número a un grupo hace que el bot le conteste a
    todo el grupo: un incidente de datos personales, no una molestia.
    """
    return bool(jid) and not jid.endswith(JID_GRUPO) and jid != JID_ESTADOS


def texto_de_mensaje(mensaje: dict) -> str | None:
    """Extrae el texto de un mensaje de WhatsApp, o None si no es texto.

    WhatsApp lo entrega en dos formas según haya o no contexto (respuesta,
    enlace, mención): `conversation` a secas o `extendedTextMessage.text`.
    """
    if not isinstance(mensaje, dict):
        return None
    plano = mensaje.get("conversation")
    if isinstance(plano, str) and plano.strip():
        return plano.strip()
    extendido = mensaje.get("extendedTextMessage") or {}
    texto = extendido.get("text") if isinstance(extendido, dict) else None
    return texto.strip() if isinstance(texto, str) and texto.strip() else None


def _cliente() -> httpx.Client:
    return httpx.Client(
        base_url=settings.evolution_url.rstrip("/"),
        headers={"apikey": settings.evolution_api_key},
        timeout=TIMEOUT,
    )


def enviar_texto(numero: str, texto: str) -> bool:
    """Envía un mensaje. Devuelve False si el canal no está configurado."""
    if not settings.tiene_whatsapp:
        log.warning("WhatsApp no configurado: mensaje descartado.")
        return False
    with _cliente() as c:
        r = c.post(
            f"/message/sendText/{settings.evolution_instancia}",
            json={
                "number": numero,
                "text": texto,
                "delay": settings.evolution_delay_ms,
            },
        )
        r.raise_for_status()
    return True


def escribiendo(numero: str, ms: int | None = None) -> None:
    """Muestra 'escribiendo…' al destinatario.

    Es cosmético para la persona y operativo para el número: el ritmo humano es
    una de las señales que separa una cuenta viva de una que WhatsApp restringe.
    Nunca debe tumbar el turno, así que los fallos se tragan.
    """
    if not settings.tiene_whatsapp:
        return
    try:
        with _cliente() as c:
            c.post(
                f"/chat/sendPresence/{settings.evolution_instancia}",
                json={
                    "number": numero,
                    "presence": "composing",
                    "delay": ms if ms is not None else settings.evolution_delay_ms,
                },
            )
    except httpx.HTTPError as e:  # noqa: BLE001 - señal cosmética
        log.debug("No se pudo enviar presencia a WhatsApp: %s", e)


def estado_conexion() -> str:
    """Estado de la instancia: open | connecting | close | no_configurado | error.

    `close` significa que hay que volver a escanear el QR y que el canal está
    caído: es lo que dispara el aviso al asesor.
    """
    if not settings.tiene_whatsapp:
        return "no_configurado"
    try:
        with _cliente() as c:
            r = c.get(f"/instance/connectionState/{settings.evolution_instancia}")
            r.raise_for_status()
            datos = r.json()
        return str(datos.get("instance", {}).get("state") or datos.get("state") or "desconocido")
    except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
        log.warning("No se pudo consultar el estado de WhatsApp: %s", e)
        return "error"


def qr_de_conexion() -> dict:
    """Devuelve el QR (base64) o el código de pareo para vincular la instancia.

    Lo consume el dashboard: sin una vista de QR, reconectar el canal significa
    entrar por consola al servidor a las once de la noche.
    """
    if not settings.tiene_whatsapp:
        return {}
    with _cliente() as c:
        r = c.get(f"/instance/connect/{settings.evolution_instancia}")
        r.raise_for_status()
        return r.json()


def qr_data_uri(datos: dict) -> str | None:
    """Normaliza el QR de Evolution a algo que un `<img src>` pueda pintar.

    Según versión y momento, el campo llega en la raíz o dentro de `qrcode`, y
    unas veces trae ya el prefijo `data:image/png;base64,` y otras no.
    """
    b64 = datos.get("base64") or (datos.get("qrcode") or {}).get("base64") or ""
    if not b64:
        return None
    return b64 if b64.startswith("data:") else f"data:image/png;base64,{b64}"


# ─────────────────────────── Aprovisionamiento ───────────────────────────

#: Solo lo que VVI sabe atender. Suscribirse a todo llena el log de ruido y
#: manda a la app eventos que únicamente puede descartar.
EVENTOS = ["MESSAGES_UPSERT", "CONNECTION_UPDATE", "QRCODE_UPDATED"]


def base_webhook() -> str:
    """De dónde cuelga la URL del webhook que se le declara a Evolution.

    En un despliegue es la URL pública de VVI. En desarrollo, Evolution corre en
    Docker y VVI en el host, así que `localhost` desde el contenedor apunta al
    propio contenedor: hay que declarar `host.docker.internal` a mano en
    EVOLUTION_WEBHOOK_BASE. Es el error de configuración más común de todo esto.
    """
    return (settings.evolution_webhook_base or settings.dashboard_url).rstrip("/")


def url_webhook() -> str:
    return f"{base_webhook()}/webhooks/whatsapp/{settings.evolution_webhook_token}"


def crear_instancia() -> str:
    """Crea la instancia si no existe. Devuelve 'creada' o 'existente'."""
    with _cliente() as c:
        r = c.post(
            "/instance/create",
            json={
                "instanceName": settings.evolution_instancia,
                # El día que salga el WABA, esto pasa a "WHATSAPP-BUSINESS" y
                # nada más en VVI cambia (ADR-02b).
                "integration": "WHATSAPP-BAILEYS",
                "qrcode": True,
            },
        )
    if r.status_code in (200, 201):
        return "creada"
    if r.status_code in (400, 403, 409) or "already in use" in r.text.lower():
        return "existente"
    r.raise_for_status()
    return "existente"


def configurar_webhook() -> str:
    """Le dice a Evolution a dónde empujar los eventos. Devuelve la URL fijada."""
    url = url_webhook()
    with _cliente() as c:
        r = c.post(
            f"/webhook/set/{settings.evolution_instancia}",
            json={
                "webhook": {
                    "enabled": True,
                    "url": url,
                    "byEvents": False,  # un solo endpoint, no una ruta por evento
                    "base64": False,
                    "events": EVENTOS,
                }
            },
        )
        r.raise_for_status()
    return url


#: Cache del token propio de la instancia: (valor, momento en que se leyó).
_token_cache: tuple[str, float] = ("", 0.0)
TTL_TOKEN = 300.0
#: Mínimo entre refrescos forzados, para que una apikey falsa no dispare
#: una consulta a Evolution por cada intento.
MIN_REFRESCO = 10.0


def token_instancia(refrescar: bool = False) -> str:
    """`apikey` con la que Evolution firma los webhooks de esta instancia.

    No es la clave global de la API: al crear una instancia, Evolution le genera
    un token propio y **ese** es el que viaja en el cuerpo de cada evento. Darlo
    por equivalente a la clave global hace que el webhook rechace absolutamente
    todo, y el síntoma es un canal mudo sin ningún error del lado de VVI.

    Se cachea porque llega un evento por mensaje y no se va a consultar la lista
    de instancias en cada uno.
    """
    global _token_cache
    valor, leido = _token_cache
    edad = time.monotonic() - leido
    if valor and not refrescar and edad < TTL_TOKEN:
        return valor
    # Cortafuegos del refresco forzado: un tercero que mande apikeys falsas no
    # puede convertir cada intento en una consulta a Evolution.
    if refrescar and valor and edad < MIN_REFRESCO:
        return valor

    try:
        with _cliente() as c:
            r = c.get("/instance/fetchInstances")
            r.raise_for_status()
            for fila in r.json():
                nombre = fila.get("name") or (fila.get("instance") or {}).get("instanceName")
                if nombre != settings.evolution_instancia:
                    continue
                token = (
                    fila.get("token")
                    or fila.get("apikey")
                    or (fila.get("instance") or {}).get("apikey")
                    or ""
                )
                _token_cache = (str(token), time.monotonic())
                return _token_cache[0]
    except (httpx.HTTPError, ValueError, TypeError) as e:
        log.warning("No se pudo leer el token de la instancia de WhatsApp: %s", e)
    return ""


def desvincular() -> None:
    """Cierra la sesión de WhatsApp sin borrar la instancia.

    Es la salida limpia cuando se cambia de número o se termina una prueba: deja
    el dispositivo desvinculado del teléfono en vez de abandonar la sesión viva.
    """
    with _cliente() as c:
        r = c.delete(f"/instance/logout/{settings.evolution_instancia}")
        if r.status_code not in (200, 201, 404):
            r.raise_for_status()
