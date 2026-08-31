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

import base64
import logging
import time
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger(__name__)

#: Un envío no puede colgar el turno conversacional indefinidamente.
TIMEOUT = 15.0

#: Subir una imagen no se parece a mandar una línea de texto: van cientos de
#: kilobytes en base64 y el enlace de salida del servidor puede ser lento. Con
#: el timeout de texto, envíos que iban bien se abortarían a medias.
TIMEOUT_MEDIA = 60.0

#: Sufijos de JID que WhatsApp usa para cosas que NO son un chat 1 a 1.
JID_GRUPO = "@g.us"
JID_ESTADOS = "status@broadcast"

#: Identificador opaco del esquema nuevo de WhatsApp (*LID*). No es un teléfono:
#: un `remoteJid` así se ve igual que uno normal pero el número que lleva dentro
#: no sirve ni para llamar ni para enviar.
JID_LID = "@lid"

#: Dónde buscar el teléfono real, en orden. El primero es el caso normal —el
#: `remoteJid` ya es el número—; los demás son donde Baileys lo deja cuando el
#: chat llega como LID, y cambian de nombre entre versiones: por eso se prueban
#: todos en vez de fijar uno solo.
CAMPOS_TELEFONO = (
    "remoteJid",
    "senderPn",
    "remoteJidAlt",
    "participantPn",
    "participantAlt",
)


def numero_de_jid(jid: str) -> str:
    """`573001234567@s.whatsapp.net` → `573001234567`.

    Evolution acepta el número pelado en los envíos y así el identificador de
    canal queda estable aunque el sufijo del JID cambie entre versiones.
    """
    return jid.split("@", 1)[0].split(":", 1)[0]


def es_lid(jid: str) -> bool:
    """¿El remitente llegó con el identificador opaco en vez del teléfono?"""
    return jid.split(":", 1)[0].endswith(JID_LID)


def destino_de_jid(jid: str) -> str:
    """A quién dirigir la respuesta, en la forma que Evolution sabe rutear.

    Con un JID normal se manda el número pelado, como siempre. Con un LID hay
    que mandar el **JID completo**: pelarlo deja un número que no es de nadie y
    Evolution rechaza el envío con `400 Bad Request` — es decir, el bot procesa
    el turno, redacta la respuesta y la entrega falla sin que el comprador vea
    nada. Evolution reenvía tal cual cualquier destinatario que ya traiga `@`.
    """
    if es_lid(jid):
        return jid.split(":", 1)[0]
    return numero_de_jid(jid)


def telefono_de_clave(clave: dict) -> str | None:
    """El teléfono real del remitente, o None si el evento no lo trae.

    Devolver None es una respuesta legítima y no un fallo: el teléfono termina
    guardado como PII del prospecto y es con el que el asesor devuelve la
    llamada. Meter ahí un LID sería peor que dejarlo vacío — un lead con un
    número inventado se descubre recién cuando alguien intenta marcarlo.
    """
    for campo in CAMPOS_TELEFONO:
        crudo = str(clave.get(campo) or "")
        if not crudo or es_lid(crudo):
            continue
        candidato = numero_de_jid(crudo)
        # Un celular colombiano con indicativo son 12 dígitos; el rango ancho
        # deja pasar otros países sin dar por bueno un identificador largo.
        if candidato.isdigit() and 10 <= len(candidato) <= 15:
            return candidato
    return None


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


def _cliente(timeout: float = TIMEOUT) -> httpx.Client:
    return httpx.Client(
        base_url=settings.evolution_url.rstrip("/"),
        headers={"apikey": settings.evolution_api_key},
        timeout=timeout,
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


def enviar_imagen(numero: str, ruta: Path, *, caption: str = "") -> bool:
    """Envía una imagen del disco. Devuelve False si el canal no está configurado.

    Va en **base64 y no como URL** a propósito. La alternativa —pasarle a
    Evolution un enlace a `/static/fotos/…`— obliga a que VVI sea alcanzable
    desde el contenedor con una URL pública y correcta. En desarrollo no lo es,
    porque Evolution corre en Docker y `localhost` allí es el propio contenedor;
    en producción el fallo sería mudo: Evolution no logra bajar el archivo, VVI
    no se entera, y el comprador recibe la ficha sin ninguna foto.

    Todo lo que guarda `services.fotos` está normalizado a JPEG, así que el
    `mimetype` no depende de lo que subiera el operador.
    """
    if not settings.tiene_whatsapp:
        log.warning("WhatsApp no configurado: imagen descartada.")
        return False
    datos = base64.b64encode(ruta.read_bytes()).decode("ascii")
    with _cliente(TIMEOUT_MEDIA) as c:
        r = c.post(
            f"/message/sendMedia/{settings.evolution_instancia}",
            json={
                "number": numero,
                "mediatype": "image",
                "mimetype": "image/jpeg",
                "media": datos,
                "fileName": ruta.name,
                "caption": caption,
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


#: Cache del teléfono que atiende la instancia: (valor, momento en que se leyó).
#: La vitrina pregunta por él cada vez que alguien pulsa el botón de WhatsApp, y
#: ese número solo cambia cuando alguien escanea otro QR: consultarlo en cada
#: clic sería pagar una llamada a Evolution por visitante.
_numero_cache: tuple[str, float] = ("", 0.0)
TTL_NUMERO = 300.0
#: El vacío se recuerda mucho menos: si el canal acaba de vincularse, quien
#: entre al minuto siguiente ya tiene que caer en el asistente y no seguir yendo
#: al número de respaldo durante cinco minutos.
TTL_NUMERO_VACIO = 60.0
#: Averiguar quién atiende no puede colgar un clic. Si Evolution no contesta en
#: dos segundos y medio se responde con el respaldo, que para el visitante es
#: una conversación igual de válida.
TIMEOUT_NUMERO = 2.5


def numero_vinculado() -> str:
    """El teléfono del asistente virtual, o cadena vacía si no hay ninguno.

    Es el número que se escaneó con el QR: no está escrito en ninguna variable
    de entorno —vincular otro aparato lo cambia sin desplegar nada—, así que la
    única forma de saberlo es preguntárselo a Evolution.

    Se exige `open` a propósito. Con la sesión cerrada el número sigue figurando
    en la instancia, pero mandar ahí a un comprador es mandarlo a un WhatsApp
    que nadie está leyendo; sin número, quien llama cae en el respaldo humano.

    Nunca levanta: un fallo aquí no puede dejar sin botón a la vitrina.
    """
    global _numero_cache
    valor, leido = _numero_cache
    edad = time.monotonic() - leido
    if leido and edad < (TTL_NUMERO if valor else TTL_NUMERO_VACIO):
        return valor

    numero = ""
    if settings.tiene_whatsapp:
        try:
            with _cliente(TIMEOUT_NUMERO) as c:
                r = c.get("/instance/fetchInstances")
                r.raise_for_status()
                for fila in r.json():
                    if not isinstance(fila, dict):
                        continue
                    # Los nombres de los campos cambiaron entre las versiones
                    # mayores de Evolution: la v2 los pone en la raíz y la v1
                    # los anida bajo `instance`. Se prueban los dos.
                    dentro = fila.get("instance")
                    dentro = dentro if isinstance(dentro, dict) else {}
                    if (fila.get("name") or dentro.get("instanceName")) != settings.evolution_instancia:
                        continue
                    estado = fila.get("connectionStatus") or dentro.get("status") or ""
                    jid = fila.get("ownerJid") or dentro.get("owner") or ""
                    if estado == "open" and isinstance(jid, str):
                        # Solo dígitos: lo que sale de aquí termina dentro de una
                        # URL de wa.me, y un JID raro no puede colarse en ella.
                        numero = "".join(d for d in numero_de_jid(jid) if d.isdigit())
                    break
        except (httpx.HTTPError, ValueError, TypeError) as e:  # noqa: BLE001
            log.warning("No se pudo saber qué número atiende WhatsApp: %s", e)

    _numero_cache = (numero, time.monotonic())
    return numero


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


#: Último QR que Evolution empujó por webhook, y el instante en que llegó.
#: Evolution rota el código solo cada pocos segundos y lo anuncia por
#: `QRCODE_UPDATED`. Guardarlo aquí es lo que permite que el panel muestre uno
#: fresco **sin volver a llamar a `/instance/connect`**, que no pide otro código:
#: reinicia el socket entero y aborta el emparejamiento que el operador esté
#: haciendo en ese momento.
#:
#: En memoria y sin persistir, a propósito: es una credencial de sesión que
#: caduca en segundos. Guardarla en la base sería conservar la llave del número
#: mucho después de que dejara de servir para nada.
_ULTIMO_QR: tuple[str, float] = ("", 0.0)

#: Pasado este tiempo, el código guardado se considera muerto y no se sirve.
VIGENCIA_QR_SEG = 90


def guardar_qr(datos: dict) -> None:
    """Anota el QR que acaba de llegar por webhook."""
    global _ULTIMO_QR
    uri = qr_data_uri(datos if isinstance(datos, dict) else {})
    if uri:
        _ULTIMO_QR = (uri, time.time())


def ultimo_qr() -> str | None:
    """El QR vigente que empujó Evolution, o None si no hay o ya venció."""
    uri, cuando = _ULTIMO_QR
    if not uri or time.time() - cuando > VIGENCIA_QR_SEG:
        return None
    return uri


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
    global _numero_cache
    with _cliente() as c:
        r = c.delete(f"/instance/logout/{settings.evolution_instancia}")
        if r.status_code not in (200, 201, 404):
            r.raise_for_status()
    # Desvincular es el momento exacto en que el número deja de atender. Sin
    # esto, la vitrina seguiría mandando compradores a un WhatsApp apagado
    # durante los cinco minutos que dura el recuerdo.
    _numero_cache = ("", 0.0)
