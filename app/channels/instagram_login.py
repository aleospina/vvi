"""Inicio de sesión de empresa de Instagram: el OAuth de la cuenta (ADR-02c).

`instagram_bot` habla con la API **ya teniendo** un token; esto es lo de antes:
cómo se consigue ese token sin copiarlo a mano del panel de Meta.

Existe por dos razones, y la segunda es la que lo hizo urgente:

1. **El token del panel caduca a los 60 días.** Renovarlo a mano significa
   entrar a developers.facebook.com, generarlo, pegarlo en `.env` y reiniciar.
   Aquí el mismo trámite es abrir una URL.
2. **Meta pide la URL de redireccionamiento para dejar cerrar la revisión de la
   app**, y sin revisión aprobada la cuenta solo atiende a los testers. Además,
   completar este flujo con el permiso de mensajería es lo que registra la app
   como *herramienta conectada con acceso a los mensajes* en el perfil de
   Instagram — que es la hipótesis viva de por qué la cuenta real recibe eventos
   `read` pero nunca `messages`. El token que se genera desde el panel no crea
   ese vínculo del mismo modo.

Tres detalles del protocolo que no son opcionales:

  · **El `client_id` es el id de la app de *Instagram*, no el de la app de
    Meta.** Son dos números distintos y el panel los muestra en pantallas
    distintas; con el de Meta, Instagram responde `Invalid platform app`. El
    `client_secret` va emparejado: el secreto de la app de Instagram, que
    tampoco tiene por qué ser el de la app de Meta con el que se verifican los
    webhooks. De ahí que sean dos ajustes y no uno.
  · **La `redirect_uri` tiene que coincidir carácter por carácter** con la
    declarada en el panel, tanto al autorizar como al canjear. Una barra final
    de más y el canje falla sin decir cuál de las tres difiere.
  · **Instagram devuelve el `code` con un `#_` pegado al final.** Enviarlo tal
    cual da «código inválido» y nada más.

El token que sale de aquí es de larga duración (60 días) y renovable. Se muestra
para pegarlo en `INSTAGRAM_TOKEN`: guardarlo solo tendría sentido con varias
inmobiliarias en la misma instancia, que no es el caso todavía.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from urllib.parse import urlencode

import httpx

from app.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0

#: Host del canje del código. Es `api.instagram.com`, no `graph.instagram.com`:
#: el primero emite tokens y el segundo los consume. Constante y no ajuste, para
#: que los tests lo intercepten sin añadir superficie de configuración.
OAUTH_BASE = "https://api.instagram.com"

#: Lo mínimo que el canal necesita: leer el perfil de quien escribe y responder
#: DMs. Pedir más permisos de los que se usan es la forma más segura de que la
#: revisión de la app vuelva con observaciones.
PERMISOS = ("instagram_business_basic", "instagram_business_manage_messages")


# ─────────────────────────── Las URLs del panel ───────────────────────────


def url_redireccion() -> str:
    """«URL de redireccionamiento» del inicio de sesión de empresa."""
    return f"{settings.url_publica}/webhooks/instagram/oauth"


def url_login() -> str:
    """Por dónde se arranca el flujo. No va al panel: se abre en el navegador."""
    return f"{settings.url_publica}/webhooks/instagram/login"


def url_desautorizacion() -> str:
    """«URL de cancelación de autorización». La revisión de la app la exige."""
    return f"{settings.url_publica}/webhooks/instagram/desautorizar"


def url_eliminacion() -> str:
    """«URL de solicitud de eliminación de datos». También exigida en revisión."""
    return f"{settings.url_publica}/webhooks/instagram/eliminar-datos"


def url_autorizacion(estado: str) -> str:
    """Pantalla de consentimiento de Instagram, con el `state` anti-CSRF."""
    parametros = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": url_redireccion(),
        "response_type": "code",
        "scope": ",".join(PERMISOS),
        "state": estado,
    }
    return f"https://www.instagram.com/oauth/authorize?{urlencode(parametros)}"


# ─────────────────────────── Canje del código ───────────────────────────


def _limpiar_codigo(codigo: str) -> str:
    """Instagram pega un `#_` al final del código. Con él, el canje falla."""
    return codigo.split("#", 1)[0]


def canjear_codigo(codigo: str) -> dict:
    """Código de autorización → token corto (1 hora). {} si Instagram lo rechaza.

    La respuesta ha cambiado de forma entre versiones: unas veces los campos
    vienen sueltos y otras dentro de `data[0]`. Se admiten las dos porque la
    alternativa es que el flujo se rompa el día que Meta cambie de opinión.
    """
    try:
        r = httpx.post(
            f"{OAUTH_BASE}/oauth/access_token",
            data={
                "client_id": settings.instagram_app_id,
                "client_secret": settings.secreto_login_instagram,
                "grant_type": "authorization_code",
                "redirect_uri": url_redireccion(),
                "code": _limpiar_codigo(codigo),
            },
            timeout=TIMEOUT,
        )
        cuerpo = r.json()
    except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
        log.warning("Instagram: no se pudo canjear el código: %s", e)
        return {}

    if isinstance(cuerpo, dict) and isinstance(cuerpo.get("data"), list) and cuerpo["data"]:
        cuerpo = cuerpo["data"][0]
    if not isinstance(cuerpo, dict) or not cuerpo.get("access_token"):
        log.warning("Instagram: canje rechazado (%s) %s", r.status_code, str(cuerpo)[:300])
        return {}
    return cuerpo


def alargar_token(token_corto: str) -> dict:
    """Token de 1 hora → token de 60 días. {} si no se pudo.

    Va contra `graph.instagram.com` y **sin** el prefijo de versión, a
    diferencia del resto de llamadas del canal.
    """
    try:
        r = httpx.get(
            f"{settings.instagram_api_base.rstrip('/')}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.secreto_login_instagram,
                "access_token": token_corto,
            },
            timeout=TIMEOUT,
        )
        cuerpo = r.json()
    except (httpx.HTTPError, ValueError) as e:  # noqa: BLE001
        log.warning("Instagram: no se pudo alargar el token: %s", e)
        return {}

    if not isinstance(cuerpo, dict) or not cuerpo.get("access_token"):
        log.warning("Instagram: alargue rechazado (%s) %s", r.status_code, str(cuerpo)[:300])
        return {}
    return cuerpo


# ─────────────────────────── Peticiones firmadas ───────────────────────────


def _base64url(dato: str) -> bytes:
    """Base64 de Meta: alfabeto URL y sin relleno."""
    return base64.urlsafe_b64decode(dato + "=" * (-len(dato) % 4))


def codigo_confirmacion(igsid: str) -> str:
    """Acuse de una solicitud de eliminación, para que el titular la rastree.

    Se deriva del IGSID con el app secret en vez de guardarse: el borrado es
    síncrono, así que no hay nada que consultar después, y una tabla de acuses
    sería justo lo contrario de lo que pide una solicitud de supresión —guardar
    un identificador más de quien pidió no aparecer en ninguna parte—.
    """
    return hmac.new(
        settings.instagram_app_secret.encode("utf-8"),
        f"eliminacion:{igsid}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def datos_firmados(peticion: str) -> dict:
    """Verifica y abre un `signed_request` de Meta. {} si la firma no cuadra.

    Es el formato con el que llegan la cancelación de autorización y la
    solicitud de eliminación de datos: `firma.carga`, ambas en base64url, con la
    firma calculada sobre la carga **codificada**, no sobre el JSON. Calcularla
    sobre el JSON ya decodificado es el error clásico, y en estas dos rutas
    significa borrar los datos de alguien a petición de un extraño.
    """
    if "." not in peticion:
        return {}
    # Meta firma con el secreto del producto que emite la llamada, y aquí hay
    # dos en juego: el de la app de Meta y el de la app de Instagram. Se admiten
    # los dos porque ambos son secretos de la misma app —nadie de fuera conoce
    # ninguno— y acertar cuál toca no vale una ruta que rechaza en silencio.
    candidatos = {settings.instagram_app_secret, settings.secreto_login_instagram} - {""}
    if not candidatos:
        return {}
    firma_b64, carga_b64 = peticion.split(".", 1)
    try:
        firma = _base64url(firma_b64)
        if not any(
            hmac.compare_digest(
                firma,
                hmac.new(s.encode("utf-8"), carga_b64.encode("utf-8"), hashlib.sha256).digest(),
            )
            for s in candidatos
        ):
            log.warning("Instagram: signed_request con firma que no coincide.")
            return {}
        carga = json.loads(_base64url(carga_b64))
    except (ValueError, TypeError) as e:  # noqa: BLE001
        log.warning("Instagram: signed_request ilegible: %s", e)
        return {}
    return carga if isinstance(carga, dict) else {}
