"""Cifrado de PII en reposo (RF-17, RNF-04) e índices ciegos para búsqueda.

Modelo:
  - Los datos de contacto (nombre, teléfono, usuario de canal) se cifran con
    Fernet (AES-128-CBC + HMAC-SHA256) antes de tocar SQLite.
  - Para poder *buscar* un prospecto por su identificador de canal sin
    descifrar toda la tabla, guardamos además un índice ciego: HMAC-SHA256 del
    identificador con una clave separada. El índice no es reversible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import warnings

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, TypeDecorator

from app.config import settings

log = logging.getLogger(__name__)

_fernet: Fernet | None = None
_hmac_key: bytes | None = None


def _cargar_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        clave = settings.fernet_key.strip()
        if not clave:
            clave = Fernet.generate_key().decode()
            warnings.warn(
                "FERNET_KEY no configurada: se generó una clave efímera. Los datos "
                "cifrados no podrán leerse tras reiniciar. Configúrala en .env "
                "antes de cualquier uso real.",
                RuntimeWarning,
                stacklevel=2,
            )
        _fernet = Fernet(clave.encode() if isinstance(clave, str) else clave)
    return _fernet


def _cargar_hmac() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        clave = settings.hmac_key.strip()
        if not clave:
            clave = base64.urlsafe_b64encode(hashlib.sha256(b"vvi-dev-hmac").digest()).decode()
            warnings.warn(
                "HMAC_KEY no configurada: se usa una clave de desarrollo. "
                "Configúrala en .env antes de cualquier uso real.",
                RuntimeWarning,
                stacklevel=2,
            )
        _hmac_key = clave.encode()
    return _hmac_key


def cifrar(texto: str | None) -> str | None:
    """Cifra un valor de PII. Devuelve None si la entrada es None."""
    if texto is None:
        return None
    return _cargar_fernet().encrypt(str(texto).encode("utf-8")).decode("ascii")


def descifrar(token: str | None) -> str | None:
    """Descifra un valor de PII. Devuelve un marcador si la clave ya no corresponde."""
    if token is None:
        return None
    try:
        return _cargar_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        log.warning("No se pudo descifrar un campo de PII (¿clave rotada?).")
        return "<ilegible>"


def indice_ciego(valor: str) -> str:
    """HMAC-SHA256 hex de un identificador, para búsquedas sin descifrar."""
    return hmac.new(_cargar_hmac(), str(valor).encode("utf-8"), hashlib.sha256).hexdigest()


def enmascarar(valor: str | None) -> str:
    """Enmascara un dato de contacto para mostrarlo en listados (minimización)."""
    if not valor:
        return "—"
    valor = str(valor)
    if len(valor) <= 4:
        return "•" * len(valor)
    return f"{valor[:2]}{'•' * (len(valor) - 4)}{valor[-2:]}"


class PII(TypeDecorator):
    """Columna de texto cifrada de forma transparente en reposo.

    El ORM entrega/recibe texto plano; SQLite solo ve el token Fernet.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: D102 - contrato SQLAlchemy
        return cifrar(value)

    def process_result_value(self, value, dialect):  # noqa: D102 - contrato SQLAlchemy
        return descifrar(value)


def _cli() -> None:
    """`python -m app.security.crypto` imprime un par de claves listas para .env."""
    fernet = Fernet.generate_key().decode()
    hmac_key = base64.urlsafe_b64encode(hashlib.sha256(Fernet.generate_key()).digest()).decode()
    print("# Pega estas líneas en tu archivo .env")
    print(f"FERNET_KEY={fernet}")
    print(f"HMAC_KEY={hmac_key}")


if __name__ == "__main__":
    _cli()
