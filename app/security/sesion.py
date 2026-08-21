"""Sesión del operador en el dashboard: cookie firmada (HU-08).

Por qué no HTTP Basic
---------------------
El dashboard usaba autenticación básica, que no admite cierre de sesión: el
navegador cachea las credenciales y las reenvía en cada petición, y no existe
forma estándar de pedirle que las olvide. Un botón "salir" sobre Basic es
decorativo.

Una cookie firmada sí tiene estado que el servidor puede invalidar: cerrar
sesión es borrarla.

Por qué HMAC propio y no una librería de sesiones
-------------------------------------------------
`SessionMiddleware` de Starlette exige `itsdangerous`, que no está instalado, y
ADR-04 mantiene el MVP sin dependencias extra. El proyecto ya firma con
HMAC-SHA256 para los índices ciegos, así que reutilizamos esa primitiva con un
dominio de uso distinto —nunca la misma clave derivada para dos propósitos—.

La cookie no guarda la contraseña: solo el usuario, un vencimiento y la firma.
No es cifrada sino firmada, que es lo que corresponde: su contenido no es
secreto, lo que importa es que no se pueda falsificar.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

from app.config import settings
from app.security.crypto import _cargar_hmac

log = logging.getLogger(__name__)

#: Roles del panel. `operador` modifica; `invitado` solo mira y comenta.
OPERADOR = "operador"
INVITADO = "invitado"

COOKIE = "vvi_sesion"
#: Alcance de la cookie. Estuvo limitada a `/dashboard` mientras el panel era
#: lo único autenticado; con la vitrina pública en `/inmuebles` eso dejaba al
#: operador sin forma de volver al panel ni de saltar a la ficha interna desde
#: el inmueble que está mirando, porque el navegador no envía la cookie fuera de
#: su ruta. Sigue siendo `httponly` y `samesite=lax`: lo que cambia es dónde
#: viaja, no quién puede leerla ni falsificarla.
RUTA_COOKIE = "/"
#: Ruta que tuvo la cookie hasta la aparición de la vitrina. Se conserva solo
#: para poder borrarla: las sesiones abiertas antes del cambio quedaron con
#: este alcance y no las pisa la nueva, porque una cookie con otra ruta es otra
#: cookie distinta para el navegador. Se puede eliminar cuando no queden
#: sesiones vivas de esa época (ocho horas después de desplegar).
RUTA_COOKIE_ANTERIOR = "/dashboard"
#: Ocho horas: una jornada del operador sin volver a autenticarse.
DURACION_SEGUNDOS = 8 * 60 * 60

#: Separa esta clave de la de los índices ciegos. Reutilizar la misma clave
#: derivada para firmar sesiones y para indexar PII acopla dos riesgos que no
#: tienen por qué viajar juntos.
_DOMINIO = b"vvi-sesion-dashboard-v1"


def _clave() -> bytes:
    return hmac.new(_cargar_hmac(), _DOMINIO, hashlib.sha256).digest()


def _firmar(carga: bytes) -> str:
    return hmac.new(_clave(), carga, hashlib.sha256).hexdigest()


def _b64(dato: str) -> str:
    return base64.urlsafe_b64encode(dato.encode("utf-8")).decode("ascii").rstrip("=")


def _deb64(dato: str) -> str:
    relleno = "=" * (-len(dato) % 4)
    return base64.urlsafe_b64decode(dato + relleno).decode("utf-8")


def crear_token(usuario: str, duracion: int = DURACION_SEGUNDOS) -> str:
    """Emite el valor de la cookie de sesión para `usuario`."""
    expira = int(time.time()) + duracion
    carga = f"{_b64(usuario)}.{expira}"
    return f"{carga}.{_firmar(carga.encode('ascii'))}"


def rol_de(usuario: str) -> str | None:
    """Rol del usuario, o None si ya no corresponde a ninguna cuenta.

    Se deriva en cada petición en vez de guardarse en la cookie: así, si se
    cambia o deshabilita una cuenta en la configuración, las sesiones abiertas
    pierden el acceso de inmediato en lugar de sobrevivir hasta vencer.
    """
    if not usuario:
        return None
    if secrets.compare_digest(usuario, settings.dashboard_user):
        return OPERADOR
    # Sin contraseña configurada, la cuenta de invitado no existe.
    if settings.invitado_password and secrets.compare_digest(usuario, settings.invitado_user):
        return INVITADO
    return None


def credenciales_validas(usuario: str, clave: str) -> str | None:
    """Devuelve el rol si el par usuario/clave es correcto; si no, None."""
    if secrets.compare_digest(usuario, settings.dashboard_user) and secrets.compare_digest(
        clave, settings.dashboard_password
    ):
        return OPERADOR
    if (
        settings.invitado_password
        and secrets.compare_digest(usuario, settings.invitado_user)
        and secrets.compare_digest(clave, settings.invitado_password)
    ):
        return INVITADO
    return None


def validar_token(token: str | None) -> str | None:
    """Devuelve el usuario si el token es auténtico y vigente; si no, None."""
    if not token:
        return None
    try:
        usuario_b64, expira_txt, firma = token.split(".")
        carga = f"{usuario_b64}.{expira_txt}"
    except ValueError:
        return None

    # compare_digest: la comparación no debe filtrar en cuánto difieren.
    if not hmac.compare_digest(firma, _firmar(carga.encode("ascii"))):
        log.warning("Cookie de sesión con firma inválida: se ignora.")
        return None

    try:
        if int(expira_txt) < time.time():
            return None
        return _deb64(usuario_b64)
    except (ValueError, UnicodeDecodeError):
        return None
