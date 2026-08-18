"""Fixtures compartidos. Cada test corre contra una base en memoria y llaves propias."""

from __future__ import annotations

import atexit
import base64
import os
import sys
import tempfile
from pathlib import Path

# Aísla los tests de cualquier .env real ANTES de importar la app.
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from cryptography.fernet import Fernet  # noqa: E402

os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["HMAC_KEY"] = base64.urlsafe_b64encode(b"clave-de-pruebas-32-bytes-aqui!!").decode()
# Los tests de servicio traen su propia sesión (fixture `db`), pero los que
# levantan la app por HTTP usan el engine del módulo. Ahí no sirve `sqlite://`:
# sin StaticPool, cada conexión abre una base en memoria distinta y las tablas
# que creó el arranque no existen para la siguiente. Un archivo temporal, que se
# borra al terminar, le da a la app una base real y compartida.
_BD_TEMPORAL = Path(tempfile.gettempdir()) / f"vvi_tests_{os.getpid()}.db"
_BD_TEMPORAL.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_BD_TEMPORAL.as_posix()}"


def _limpiar_bd_temporal() -> None:
    """Cierra el engine antes de borrar: Windows no permite unlink de un archivo abierto."""
    try:
        from app.db import engine

        engine.dispose()
    except Exception:  # noqa: BLE001 - la app pudo no llegar a importarse
        pass
    try:
        _BD_TEMPORAL.unlink(missing_ok=True)
    except OSError:
        pass  # queda en el temporal del sistema; se recicla solo


atexit.register(_limpiar_bd_temporal)
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["MOONSHOT_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "reglas"
os.environ["EMPRESA_NOMBRE"] = "Inmobiliaria de Pruebas"
os.environ["DASHBOARD_PASSWORD"] = "clave-de-pruebas"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models import Base, Propiedad  # noqa: E402

PROPIEDADES = [
    dict(id="PROP-MED-001", ciudad="Medellín", zona="Laureles", tipo="apartamento",
         habitaciones=3, banos=2, area_m2=92, precio=520_000_000,
         descripcion="Apto remodelado cerca del Estadio."),
    dict(id="PROP-MED-002", ciudad="Medellín", zona="Belén", tipo="apartamento",
         habitaciones=3, banos=2, area_m2=85, precio=385_000_000,
         descripcion="Apto familiar con piscina."),
    dict(id="PROP-MED-003", ciudad="Medellín", zona="Envigado", tipo="casa",
         habitaciones=4, banos=3, area_m2=180, precio=890_000_000,
         descripcion="Casa de dos plantas con patio."),
    dict(id="PROP-MED-004", ciudad="Medellín", zona="Bello", tipo="apartamento",
         habitaciones=2, banos=1, area_m2=62, precio=215_000_000,
         descripcion="Apto cerca a transporte masivo."),
    dict(id="PROP-PER-001", ciudad="Pereira", zona="Pinares", tipo="casa",
         habitaciones=3, banos=2, area_m2=160, precio=420_000_000,
         descripcion="Casa de dos niveles con estudio."),
    dict(id="PROP-PER-002", ciudad="Pereira", zona="Álamos", tipo="casa",
         habitaciones=3, banos=3, area_m2=145, precio=395_000_000,
         descripcion="Casa en conjunto cerrado."),
]


@pytest.fixture()
def db():
    """Sesión contra SQLite en memoria, con la cartera de prueba cargada."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Sesion = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sesion = Sesion()
    sesion.add_all(Propiedad(**p) for p in PROPIEDADES)
    sesion.commit()
    try:
        yield sesion
    finally:
        sesion.close()
        engine.dispose()


@pytest.fixture()
def prospecto_consentido(db):
    """Prospecto de Telegram con consentimiento ya otorgado."""
    from app.channels.gateway import MensajeEntrante, alta_con_consentimiento

    entrante = MensajeEntrante(
        canal="telegram", canal_id="99887766", texto="",
        nombre="Andrés Prueba", usuario_canal="@andresprueba", red_origen="telegram",
    )
    p = alta_con_consentimiento(db, entrante, evidencia="respondió Sí en el bot")
    db.commit()
    return p
