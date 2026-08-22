"""Migración del esquema al arrancar (ADR-04: SQLite y sin Alembic).

`create_all` no toca una tabla que ya existe, así que el arranque añade las
columnas nuevas y deja caer las que murieron. Lo que se prueba aquí es que
ninguna de las dos cosas pierda datos ni pueda tumbar el arranque: una base de
operación real pasa por este código en cada despliegue.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from app import db as modulo_db

COLUMNA_MUERTA = "conversacion_cerrada_ts"


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Una base en disco con el esquema puesto, y el módulo apuntando a ella."""
    ruta = tmp_path / "migracion.db"
    engine = create_engine(f"sqlite:///{ruta.as_posix()}", future=True)
    modulo_db.Base.metadata.create_all(engine)
    monkeypatch.setattr(modulo_db, "engine", engine)
    try:
        yield ruta
    finally:
        engine.dispose()


def columnas(ruta, tabla="prospectos") -> list[str]:
    con = sqlite3.connect(ruta)
    try:
        return [fila[1] for fila in con.execute(f"PRAGMA table_info({tabla})")]
    finally:
        con.close()


def _prospecto_con_columna_muerta(ruta) -> None:
    """Reproduce la base desplegada: la columna existe y con dato dentro."""
    con = sqlite3.connect(ruta)
    con.execute(f"ALTER TABLE prospectos ADD COLUMN {COLUMNA_MUERTA} DATETIME")
    con.execute(
        "INSERT INTO prospectos "
        "(codigo, canal, consentimiento, score_intencion, etiqueta, estado, notas,"
        f" creado_en, actualizado_en, {COLUMNA_MUERTA}) "
        "VALUES ('LEAD-000008', 'telegram', 1, 50, 'tibio', 'nuevo', '',"
        " '2026-08-21 09:00:00', '2026-08-21 09:00:00', '2026-08-21 10:00:00')"
    )
    con.commit()
    con.close()


class TestRetirarColumnas:
    def test_la_columna_muerta_se_va(self, base):
        _prospecto_con_columna_muerta(base)
        assert COLUMNA_MUERTA in columnas(base)

        modulo_db._retirar_columnas()

        assert COLUMNA_MUERTA not in columnas(base)

    def test_los_datos_de_al_lado_no_se_tocan(self, base):
        """Es una base de operación: el lead tiene que salir entero del otro lado."""
        _prospecto_con_columna_muerta(base)
        modulo_db._retirar_columnas()

        con = sqlite3.connect(base)
        fila = con.execute(
            "SELECT codigo, estado, etiqueta, score_intencion FROM prospectos"
        ).fetchall()
        con.close()
        assert fila == [("LEAD-000008", "nuevo", "tibio", 50)]

    def test_es_idempotente(self, base):
        """Cada despliegue vuelve a pasar por aquí; el segundo no puede quejarse."""
        _prospecto_con_columna_muerta(base)
        modulo_db._retirar_columnas()
        modulo_db._retirar_columnas()

        assert COLUMNA_MUERTA not in columnas(base)

    def test_sobre_una_base_que_nunca_la_tuvo_no_hace_nada(self, base):
        antes = columnas(base)
        modulo_db._retirar_columnas()
        assert columnas(base) == antes

    def test_un_drop_imposible_no_tumba_el_arranque(self, base, monkeypatch):
        """SQLite viejo, o una columna atada a un índice: se queda y se avisa.

        `codigo` es clave primaria e indexada, así que SQLite se niega a
        soltarla. Perder el arranque de la aplicación por limpiar ruido sería un
        cambio peor que el ruido.
        """
        _prospecto_con_columna_muerta(base)
        monkeypatch.setitem(modulo_db.COLUMNAS_RETIRADAS, "prospectos", ["codigo"])

        modulo_db._retirar_columnas()  # no propaga

        assert "codigo" in columnas(base), "la columna imposible sigue en su sitio"
