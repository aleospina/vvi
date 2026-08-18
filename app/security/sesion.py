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
import time

from app.security.crypto import _cargar_hmac

log = logging.getLogger(__name__)

COOKIE = "vvi_sesion"
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
