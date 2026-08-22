"""Acceso a la base de datos SQLite (ADR-04)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RAIZ, settings
from app.models import Base, Campana, Propiedad

log = logging.getLogger(__name__)

_kwargs: dict = {"future": True}
if settings.database_url.startswith("sqlite"):
    # SQLite: el bot (hilo aparte) y FastAPI comparten el archivo.
    _kwargs["connect_args"] = {"check_same_thread": False}
    ruta = settings.database_url.replace("sqlite:///", "")
    if ruta and ruta != ":memory:":
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _pragmas(dbapi_connection, connection_record) -> None:
    """WAL + claves foráneas: concurrencia razonable para el piloto."""
    if not settings.database_url.startswith("sqlite"):
        return
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@contextmanager
def sesion() -> Iterator[Session]:
    """Sesión transaccional: commit al salir bien, rollback si algo falla."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """Dependencia de FastAPI."""
    with sesion() as s:
        yield s


CAMPANAS_SEMILLA = [
    ("ig-bio-medellin", "Link in bio Instagram — Medellín", "instagram"),
    ("fb-marketplace-med", "Publicaciones Marketplace — Medellín", "marketplace"),
    ("ig-bio-pereira", "Link in bio Instagram — Pereira", "instagram"),
    ("olx-listados", "Listados OLX", "olx"),
    ("ml-publicaciones", "Publicaciones Mercado Libre", "mercado_libre"),
]


#: Columnas añadidas después de la primera versión del esquema. `create_all` no
#: toca tablas existentes, y el MVP no usa Alembic (ADR-04: SQLite y sin
#: dependencias extra), así que las agregamos aquí de forma idempotente.
COLUMNAS_NUEVAS: dict[str, list[tuple[str, str]]] = {
    "prospectos": [
        ("municipio", "VARCHAR(64)"),
        # Sin valor por defecto: mientras el prospecto no diga a qué viene, el
        # emparejamiento asume venta. Rellenarlo aquí afirmaría un dato que
        # nadie declaró.
        ("negocio", "VARCHAR(12)"),
        ("foco", "VARCHAR(160)"),
        ("canal_id", "VARCHAR(400)"),
    ],
    "solicitudes": [
        ("protegido_hasta", "DATETIME"),
    ],
    "propiedades": [
        ("fuente", "VARCHAR(40) DEFAULT 'manual'"),
        # Toda la cartera anterior a esta columna es de venta: el sistema no
        # sabía hacer otra cosa. El DEFAULT lo deja escrito en las filas viejas
        # en lugar de dejar nulos que cada consulta tendría que interpretar.
        ("negocio", "VARCHAR(12) DEFAULT 'venta'"),
        ("externo_id", "VARCHAR(120)"),
        ("url_origen", "VARCHAR(300) DEFAULT ''"),
        ("actualizada_en", "DATETIME"),
        ("mandato", "BOOLEAN DEFAULT 0"),
        ("mandato_evidencia", "TEXT DEFAULT ''"),
        ("propietario_telefono", "VARCHAR(300)"),
        ("propietario_telefono_hash", "VARCHAR(64)"),
    ],
}


#: Columnas que existieron y ya no las lee nadie. Se dejan caer al arrancar:
#: una columna huérfana es ruido en cada `PRAGMA table_info` y en cada
#: inspección del esquema, y quien lea la tabla dentro de seis meses no tiene
#: modo de saber que está muerta.
COLUMNAS_RETIRADAS: dict[str, list[str]] = {
    # Duró un commit. La despedida marcaba aquí el cierre de la conversación;
    # ahora la única que cierra es la venta, y eso se deriva del estado del
    # prospecto sin necesidad de guardar nada.
    "prospectos": ["conversacion_cerrada_ts"],
}


def _migrar(conexion) -> None:
    """Añade columnas faltantes sin tocar los datos existentes."""
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        existentes = {
            fila[1] for fila in conexion.exec_driver_sql(f"PRAGMA table_info({tabla})")
        }
        if not existentes:
            continue  # la tabla la acaba de crear create_all, ya viene completa
        for nombre, definicion in columnas:
            if nombre not in existentes:
                conexion.exec_driver_sql(
                    f"ALTER TABLE {tabla} ADD COLUMN {nombre} {definicion}"
                )


def _retirar_columnas() -> None:
    """Deja caer las columnas muertas, sin poder tumbar el arranque.

    `ALTER TABLE ... DROP COLUMN` existe en SQLite desde la 3.35 (2021), y falla
    si la columna cuelga de un índice, una vista o un trigger. En cualquiera de
    esos casos la columna se queda donde está: es inerte, y perder el arranque
    de la aplicación por limpiar ruido sería un cambio peor que el ruido.

    Cada retirada va en su propia transacción, para que un fallo no arrastre a
    las demás ni a las columnas que `_migrar` acaba de añadir.
    """
    for tabla, columnas in COLUMNAS_RETIRADAS.items():
        for nombre in columnas:
            try:
                with engine.begin() as conexion:
                    existentes = {
                        fila[1]
                        for fila in conexion.exec_driver_sql(f"PRAGMA table_info({tabla})")
                    }
                    if nombre not in existentes:
                        continue
                    conexion.exec_driver_sql(
                        f"ALTER TABLE {tabla} DROP COLUMN {nombre}"
                    )
                log.info("Columna retirada: %s.%s", tabla, nombre)
            except Exception:  # noqa: BLE001 - degradación deliberada
                log.warning(
                    "No se pudo retirar %s.%s; se deja como está.",
                    tabla, nombre, exc_info=True,
                )


def _sembrar_cartera_demo() -> bool:
    """¿Hay que cargar la cartera de ejemplo?

    Los inmuebles de `seed_propiedades.json` son inventados. Sembrarlos solos
    cuando la tabla está vacía era razonable para demostrar el MVP, pero en
    operación real significa que datos falsos reaparecen tras cualquier
    limpieza, y el bot se los ofrece a un comprador como si existieran.

    Ahora hay que pedirlos a propósito: `SEMBRAR_DEMO=1`.
    """
    return os.getenv("SEMBRAR_DEMO", "").strip().lower() in ("1", "true", "si", "sí")


def inicializar(seed: bool = True) -> None:
    """Crea el esquema y las campañas. La cartera de demo ya no se siembra sola."""
    Base.metadata.create_all(engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conexion:
            _migrar(conexion)
        _retirar_columnas()
    if not seed:
        return

    with sesion() as s:
        archivo = RAIZ / "data" / "seed_propiedades.json"
        if _sembrar_cartera_demo() and archivo.exists():
            if s.scalar(select(Propiedad).limit(1)) is None:
                datos = json.loads(archivo.read_text(encoding="utf-8"))
                s.add_all(Propiedad(**p) for p in datos)
                log.warning(
                    "Cartera de DEMO sembrada (%d inmuebles inventados) por "
                    "SEMBRAR_DEMO=1. No usar en operación real.",
                    len(datos),
                )

        existentes = set(s.scalars(select(Campana.slug)).all())
        for slug, nombre, red in CAMPANAS_SEMILLA:
            if slug not in existentes:
                s.add(Campana(slug=slug, nombre=nombre, red=red))
