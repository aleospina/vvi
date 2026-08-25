"""Webhook entrante de Instagram vía la Messaging API de Meta (ADR-02c).

Meta empuja aquí cada evento de la cuenta profesional. El contrato relevante es
`entry[].messaging[]`: un mensaje que entró (o que salió, si `is_echo`).

Cuatro decisiones que no son opcionales:

1. **Firma sobre el cuerpo crudo.** Meta firma con `X-Hub-Signature-256`. Hay que
   verificarla sobre los bytes tal como llegaron, antes de parsear: volver a
   serializar el JSON cambia espacios y orden y la firma deja de coincidir. Esta
   es la barrera real, y por eso la ruta no necesita el segmento secreto que sí
   lleva la de WhatsApp —Evolution no firma nada, Meta sí—.
2. **Responder 200 de inmediato.** El turno puede llamar al LLM y tardar
   segundos; Meta reintenta si el webhook demora y el comprador recibiría la
   misma respuesta dos veces. El trabajo va a `BackgroundTasks`.
3. **Idempotencia por `mid`.** Los reintentos existen igual. Sin deduplicar se
   duplican también los mensajes en la auditoría del prospecto.
4. **El handshake de verificación.** Meta hace un GET con `hub.challenge` al
   registrar la URL y espera el reto en texto plano. Sin eso no se puede ni
   guardar la suscripción.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.channels import conversacion, instagram_bot, instagram_login
from app.config import settings
from app.models import Canal
from app.services import ajustes

log = logging.getLogger(__name__)
router = APIRouter(tags=["instagram"])

CANAL = Canal.INSTAGRAM.value

#: Mensajes ya atendidos, para descartar reintentos de Meta. En memoria a
#: propósito, igual que en WhatsApp: con una sola réplica alcanza, y un duplicado
#: tras un reinicio es un daño menor comparado con meter otra tabla.
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


def _huella(igsid: str) -> str:
    """Últimos dos dígitos, para correlacionar en el log sin escribir el id.

    Un log es un sitio del que la PII no sale nunca más (RF-17). El IGSID
    identifica a una persona igual que un teléfono, así que se trata igual.
    """
    return f"…{igsid[-2:]}" if len(igsid) >= 2 else "?"


# ─────────────────────────── Lista blanca de pruebas ───────────────────────────


def _permitido(igsid: str) -> bool:
    """¿Este remitente entra, con la lista de pruebas que haya configurada?

    Mientras la app de Meta esté en modo desarrollo solo pueden escribir las
    cuentas con rol en la app, pero eso no cubre la fase siguiente: aprobada la
    revisión, la cuenta atiende a todo el mundo de golpe. Esta lista es el mismo
    freno que `EVOLUTION_NUMEROS_PRUEBA` en WhatsApp.

    Se admite tanto el IGSID numérico como el `@usuario`, porque el operador
    conoce el segundo y no el primero. Resolver el usuario cuesta una llamada a
    Meta, así que solo se pide cuando la lista trae nombres — en producción la
    lista está vacía y esto no toca la red.
    """
    permitidos = ajustes.usuarios_prueba()
    if not permitidos:
        return True
    if igsid in permitidos:
        return True
    if not any(not p.isdigit() for p in permitidos):
        return False
    usuario = str(instagram_bot.perfil_usuario(igsid).get("username") or "").lower()
    return bool(usuario) and usuario in permitidos


# ─────────────────────────── Atención del turno ───────────────────────────


def _responder(igsid: str, textos: list[str]) -> None:
    """Envía los textos, con los botones de consentimiento si toca.

    Los botones van solo cuando la conversación está esperando la autorización:
    en cualquier otro turno serían un par de opciones sin pregunta a la que
    respondan.
    """
    respuestas = (
        instagram_bot.RESPUESTAS_CONSENTIMIENTO
        if conversacion.esta_pendiente(CANAL, igsid)
        else None
    )
    for i, salida in enumerate(textos):
        try:
            instagram_bot.enviar_texto(
                igsid,
                salida,
                respuestas=respuestas if i == len(textos) - 1 else None,
            )
        except Exception:  # noqa: BLE001 - un envío fallido no corta el resto
            log.exception("No se pudo enviar una respuesta por Instagram")


def atender(igsid: str, texto: str) -> None:
    """Procesa un mensaje y responde. Corre fuera del ciclo de la petición."""
    instagram_bot.escribiendo(igsid)
    perfil = instagram_bot.perfil_usuario(igsid)
    try:
        textos = conversacion.turno(
            CANAL,
            igsid,
            texto,
            nombre=perfil.get("name"),
            usuario=perfil.get("username"),
        )
    except Exception:
        log.exception("Error procesando mensaje de Instagram")
        textos = ["Uy, tuve un problema técnico procesando tu mensaje. ¿Lo intentas de nuevo?"]

    _responder(igsid, textos)


def atender_consentimiento(igsid: str, payload: str) -> None:
    """Resuelve la puerta de consentimiento cuando llega por botón.

    Es el equivalente del `CallbackQueryHandler` de Telegram. Se atiende aparte
    del texto libre porque el payload es inequívoco: no hay que interpretar si
    "dale" significa sí.
    """
    perfil = instagram_bot.perfil_usuario(igsid)
    try:
        if payload == "consent:si":
            textos = conversacion.aceptar_consentimiento(
                CANAL, igsid, nombre=perfil.get("name"), usuario=perfil.get("username")
            )
        else:
            textos = conversacion.rechazar_consentimiento(CANAL, igsid)
    except Exception:
        log.exception("Error resolviendo el consentimiento de Instagram")
        return
    _responder(igsid, textos)


def _mensaje_entrante(evento: dict, tareas: BackgroundTasks) -> None:
    igsid = str((evento.get("sender") or {}).get("id") or "")
    mensaje = evento.get("message")
    if not igsid or not isinstance(mensaje, dict):
        return

    # Cada descarte se registra con su motivo. Sin esto, un canal que no responde
    # es indistinguible de un canal que no recibe nada.
    if instagram_bot.es_eco(mensaje):
        log.debug("Instagram: descartado mensaje propio (eco).")
        return
    if _ya_visto(str(mensaje.get("mid") or "")):
        log.info("Instagram: descartado duplicado de %s (reintento de Meta).", _huella(igsid))
        return
    if not _permitido(igsid):
        # El silencio es deliberado: responder "no estás autorizado" ya sería
        # contestarle a quien no debía recibir nada.
        log.info("Instagram: %s no está en la lista de pruebas: ignorado.", _huella(igsid))
        return

    payload = instagram_bot.respuesta_rapida(mensaje)
    if payload and payload.startswith("consent:"):
        log.info("Instagram: %s respondió el consentimiento con botón.", _huella(igsid))
        tareas.add_task(atender_consentimiento, igsid, payload)
        return

    texto = instagram_bot.texto_de_mensaje(mensaje)
    if not texto:
        # Foto, audio, reel compartido. Decirlo es mejor que el silencio: la
        # persona cree que la están ignorando y se va.
        log.info("Instagram: mensaje de %s sin texto: se responde que solo leo texto.",
                 _huella(igsid))
        tareas.add_task(
            instagram_bot.enviar_texto,
            igsid,
            "Por ahora solo puedo leer mensajes de texto 🙏 ¿Me lo escribes?",
        )
        return

    log.info("Instagram: mensaje de %s aceptado, procesando turno.", _huella(igsid))
    tareas.add_task(atender, igsid, texto)


# ─────────────────────────── Rutas ───────────────────────────


@router.get("/webhooks/instagram", response_class=PlainTextResponse, include_in_schema=False)
def verificar(request: Request):
    """Handshake que Meta exige al registrar la URL del webhook."""
    params = request.query_params
    enviado = params.get("hub.verify_token") or ""
    esperado = settings.instagram_verify_token
    if (
        params.get("hub.mode") == "subscribe"
        and esperado
        and secrets.compare_digest(enviado, esperado)
    ):
        return PlainTextResponse(params.get("hub.challenge") or "")
    log.warning("Instagram: verificación de webhook rechazada (token que no coincide).")
    raise HTTPException(status_code=403, detail="verificación fallida")


@router.post("/webhooks/instagram", include_in_schema=False)
async def entrante(request: Request, tareas: BackgroundTasks):
    crudo = await request.body()
    if not instagram_bot.firma_valida(crudo, request.headers.get("x-hub-signature-256")):
        # 404 y no 403: a quien tantea rutas no se le confirma que existe.
        raise HTTPException(status_code=404)

    try:
        cuerpo = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="cuerpo no es JSON")

    objeto = str(cuerpo.get("object") or "") if isinstance(cuerpo, dict) else ""
    if objeto != "instagram":
        # La misma app de Meta sirve Lead Ads (`object: page`), que tiene su
        # propia ruta. Verlo en el log distingue "no llega nada" de "llega otra
        # cosa" — que fue justo lo que costó diagnosticar en WhatsApp.
        log.info("Instagram: objeto %r ignorado (no es de mensajería).", objeto or "sin nombre")
        return {"ok": True}

    for entrada in cuerpo.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for evento in entrada.get("messaging") or []:
            if isinstance(evento, dict) and "message" in evento:
                _mensaje_entrante(evento, tareas)

    # Siempre 200: un error aquí solo provoca reintentos que no arreglan nada.
    return {"ok": True}


# ─────────────── Inicio de sesión de empresa de Instagram (OAuth) ───────────────

#: `state` emitidos y aún sin usar. Es la barrera del flujo de conexión: sin él,
#: un tercero podría hacer que el operador canjeara un código ajeno y dejar
#: conectada una cuenta que no es la suya. De un solo uso y en memoria, porque
#: el trayecto entre `/login` y `/oauth` dura segundos.
_ESTADOS: OrderedDict[str, float] = OrderedDict()
VENTANA_ESTADO = 600.0  # segundos
MAX_ESTADOS = 50


def _nuevo_estado() -> str:
    ahora = time.monotonic()
    while _ESTADOS and (ahora - next(iter(_ESTADOS.values())) > VENTANA_ESTADO):
        _ESTADOS.popitem(last=False)
    estado = secrets.token_urlsafe(24)
    _ESTADOS[estado] = ahora
    while len(_ESTADOS) > MAX_ESTADOS:
        _ESTADOS.popitem(last=False)
    return estado


def _consumir_estado(estado: str) -> bool:
    """Valida y quema el `state`. Un código solo se canjea una vez."""
    nacido = _ESTADOS.pop(estado, None)
    return nacido is not None and (time.monotonic() - nacido) <= VENTANA_ESTADO


def _pagina(titulo: str, cuerpo: str, *, codigo: int = 200) -> HTMLResponse:
    """Página mínima para el operador. No usa las plantillas del dashboard a
    propósito: esto se abre antes de que el canal exista y no debe depender de
    que la sesión, el tema o el layout del panel estén en su sitio."""
    return HTMLResponse(
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>{html.escape(titulo)}</title>"
        "<style>body{font:16px/1.6 system-ui,sans-serif;max-width:44rem;margin:3rem auto;"
        "padding:0 1.2rem;color:#1c1c1c}code{background:#f1f1f1;padding:.15em .35em;"
        "border-radius:4px;word-break:break-all}pre{background:#f1f1f1;padding:1rem;"
        "border-radius:8px;white-space:pre-wrap;word-break:break-all}</style>"
        f"<h1>{html.escape(titulo)}</h1>{cuerpo}",
        status_code=codigo,
    )


@router.get("/webhooks/instagram/login", include_in_schema=False)
def login():
    """Arranca la conexión de la cuenta profesional.

    Es la puerta que el operador abre en el navegador; Instagram muestra su
    pantalla de consentimiento y vuelve a `/webhooks/instagram/oauth`.
    """
    if not settings.tiene_login_instagram:
        return _pagina(
            "Falta configurar la conexión",
            "<p>Para conectar la cuenta hacen falta <code>INSTAGRAM_APP_ID</code> y "
            "<code>INSTAGRAM_APP_SECRET</code> en el <code>.env</code>.</p>"
            "<p>El <em>app id</em> es el de la app de <strong>Instagram</strong> "
            "(panel de Meta → API con inicio de sesión de Instagram → Configuración de "
            "la app de Instagram), no el de la app de Meta.</p>",
            codigo=503,
        )
    return RedirectResponse(instagram_login.url_autorizacion(_nuevo_estado()))


@router.get("/webhooks/instagram/oauth", include_in_schema=False)
def oauth(request: Request):
    """Vuelta de Instagram: canjea el código y muestra el token de 60 días.

    Esta es la URL que se declara como «URL de redireccionamiento» en el panel.
    El token se muestra en pantalla en vez de guardarse porque hoy hay una sola
    cuenta y vive en `INSTAGRAM_TOKEN`; el día que sean varias inmobiliarias,
    aquí es donde se persiste.
    """
    params = request.query_params

    if params.get("error"):
        detalle = params.get("error_description") or params.get("error") or ""
        return _pagina(
            "Conexión cancelada",
            f"<p>Instagram no autorizó la conexión: {html.escape(detalle)}</p>"
            "<p><a href='/webhooks/instagram/login'>Volver a intentar</a></p>",
            codigo=400,
        )

    codigo = params.get("code") or ""
    if not codigo or not _consumir_estado(params.get("state") or ""):
        # Sirve tanto para el enlace vencido como para el código de un tercero.
        return _pagina(
            "Enlace de conexión vencido",
            "<p>El enlace solo vale una vez y caduca a los 10 minutos.</p>"
            "<p><a href='/webhooks/instagram/login'>Empezar de nuevo</a></p>",
            codigo=400,
        )

    corto = instagram_login.canjear_codigo(codigo)
    if not corto:
        return _pagina(
            "Instagram rechazó el código",
            "<p>Suele ser que la <strong>URL de redireccionamiento</strong> del panel no "
            "coincida carácter por carácter con esta:</p>"
            f"<pre>{html.escape(instagram_login.url_redireccion())}</pre>"
            "<p>O que <code>INSTAGRAM_APP_ID</code> sea el id de la app de Meta y no el "
            "de la app de Instagram. El detalle exacto está en el log del servidor.</p>",
            codigo=502,
        )

    largo = instagram_login.alargar_token(str(corto["access_token"]))
    token = str(largo.get("access_token") or corto["access_token"])
    dias = int(largo.get("expires_in") or 0) // 86400
    igsid = str(corto.get("user_id") or "")
    permisos = str(corto.get("permissions") or "")

    log.info(
        "Instagram: cuenta %s conectada por OAuth con permisos [%s]; token de %d días.",
        _huella(igsid),
        permisos,
        dias,
    )

    aviso = (
        f"<p>Token de larga duración, válido {dias} días.</p>"
        if dias
        else "<p><strong>Ojo:</strong> no se pudo alargar el token, así que este es el "
        "corto y caduca en una hora. Sirve para probar; para dejarlo puesto, "
        "revisa el log y vuelve a conectar.</p>"
    )
    return _pagina(
        "Cuenta conectada",
        f"<p>Cuenta <code>{html.escape(igsid)}</code> · permisos "
        f"<code>{html.escape(permisos)}</code></p>"
        "<p>Pega esto en el <code>.env</code> y reinicia VVI:</p>"
        f"<pre>INSTAGRAM_TOKEN=\"{html.escape(token)}\"\n"
        f"INSTAGRAM_CUENTA_ID=\"{html.escape(igsid)}\"</pre>"
        f"{aviso}"
        "<p>Este token es una credencial: no lo compartas ni lo dejes en una captura.</p>",
    )


# ─────── Cancelación de autorización y eliminación de datos (revisión) ───────
#
# Las dos las exige Meta para aprobar la revisión de la app y las llama
# servidor a servidor, con un `signed_request` firmado con el app secret. Son
# públicas por obligación, así que la firma es la única barrera: sin verificarla
# cualquiera podría borrar los datos de un tercero mandando su IGSID.


@router.post("/webhooks/instagram/desautorizar", include_in_schema=False)
def desautorizar(signed_request: str = Form(default="")):
    """El titular quitó la app desde Instagram: se revoca lo que hubiera."""
    datos = instagram_login.datos_firmados(signed_request)
    if not datos:
        raise HTTPException(status_code=404)
    igsid = str(datos.get("user_id") or "")
    if igsid:
        conversacion.borrar_datos(CANAL, igsid)
        log.info("Instagram: %s canceló la autorización; datos revocados.", _huella(igsid))
    return {"ok": True}


@router.post("/webhooks/instagram/eliminar-datos", include_in_schema=False)
def eliminar_datos(signed_request: str = Form(default="")):
    """Solicitud de supresión (habeas data, RF-17). Se atiende de inmediato.

    Meta espera exactamente `url` y `confirmation_code`: la primera es donde el
    titular verifica que se hizo, el segundo su acuse.
    """
    datos = instagram_login.datos_firmados(signed_request)
    if not datos:
        raise HTTPException(status_code=404)
    igsid = str(datos.get("user_id") or "")
    if igsid:
        conversacion.borrar_datos(CANAL, igsid)
        log.info("Instagram: eliminación de datos atendida para %s.", _huella(igsid))
    acuse = instagram_login.codigo_confirmacion(igsid)
    return {
        "url": f"{instagram_login.url_eliminacion()}/{acuse}",
        "confirmation_code": acuse,
    }


@router.get("/webhooks/instagram/eliminar-datos/{acuse}", include_in_schema=False)
def estado_eliminacion(acuse: str):
    """Página de verificación del acuse. La abre el titular, no Meta."""
    return _pagina(
        "Solicitud de eliminación de datos",
        f"<p>Solicitud <code>{html.escape(acuse[:64])}</code>: "
        "<strong>completada</strong>.</p>"
        f"<p>{html.escape(settings.empresa_nombre)} eliminó los datos de contacto "
        "asociados a esa cuenta de Instagram y revocó la autorización de tratamiento. "
        "Solo queda el registro de auditoría que exige la ley, sin datos que "
        "identifiquen al titular.</p>"
        f"<p><a href='{html.escape(settings.politica_privacidad_url)}'>"
        "Política de tratamiento de datos</a></p>",
    )
