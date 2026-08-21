"""Ajustes operativos editables desde la interfaz.

El caso concreto es la lista blanca del canal de WhatsApp. Cambiarla obligaba a
editar el `.env` y reiniciar el proceso: media hora de despliegue para probar
con otro celular. Ahora se cambia desde la pantalla del canal y surte efecto en
el mensaje siguiente, sin reiniciar nada.

La regla que sostiene todo: el `.env` es el valor de arranque, y la base lo pisa
en cuanto alguien toca el ajuste.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.main import app
from app.models import Ajuste
from app.services import ajustes


@pytest.fixture(autouse=True)
def base_limpia():
    """Sin fila de ajuste: cada test decide si la crea."""
    inicializar(seed=False)
    db = SessionLocal()
    try:
        existente = db.get(Ajuste, ajustes.NUMEROS_PRUEBA)
        if existente is not None:
            db.delete(existente)
            db.commit()
    finally:
        db.close()
    yield


@pytest.fixture()
def operador():
    cli = TestClient(app, follow_redirects=False)
    cli.__enter__()
    r = cli.post(
        "/dashboard/login",
        data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
    )
    assert r.status_code == 303
    yield cli
    cli.__exit__(None, None, None)


class TestNormalizacion:
    @pytest.mark.parametrize(
        ("escrito", "guardado"),
        [
            ("573001234567", "573001234567"),
            ("+57 300 123 4567", "573001234567"),
            ("300 123 4567", "573001234567"),      # a la colombiana, sin indicativo
            ("57-310-987-6543", "573109876543"),
            ("3001234567, +57 300 123 4567", "573001234567"),  # el mismo dos veces
        ],
    )
    def test_todo_termina_en_el_formato_de_whatsapp(self, escrito, guardado):
        assert ajustes.normalizar_lista(escrito) == guardado

    def test_una_lista_se_ordena_y_no_repite(self):
        assert ajustes.normalizar_lista("3109876543, 3001234567, 3109876543") == (
            "573001234567,573109876543"
        )

    def test_el_campo_vacio_es_una_lista_vacia(self):
        assert ajustes.normalizar_lista("  ,  ") == ""


class TestQuienManda:
    def test_sin_ajuste_manda_el_entorno(self, monkeypatch):
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")
        assert ajustes.numeros_prueba() == {"573001234567"}
        assert ajustes.desde_el_entorno() is True

    def test_lo_guardado_pisa_al_entorno(self, monkeypatch):
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")
        db = SessionLocal()
        try:
            ajustes.guardar(db, ajustes.NUMEROS_PRUEBA, "573109876543", actor="operador")
            db.commit()
        finally:
            db.close()

        assert ajustes.numeros_prueba() == {"573109876543"}
        assert ajustes.desde_el_entorno() is False

    def test_una_lista_vaciada_a_mano_no_vuelve_al_entorno(self, monkeypatch):
        """Vaciar el campo es decir "producción", no "usa el .env"."""
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")
        db = SessionLocal()
        try:
            ajustes.guardar(db, ajustes.NUMEROS_PRUEBA, "", actor="operador")
            db.commit()
        finally:
            db.close()

        assert ajustes.numeros_prueba() == frozenset()


class TestDesdeLaPantalla:
    def test_el_operador_cambia_el_numero(self, operador, monkeypatch):
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")

        r = operador.post(
            "/dashboard/whatsapp/numeros-prueba", data={"numeros": "310 987 6543"}
        )
        assert r.status_code == 303
        assert ajustes.numeros_prueba() == {"573109876543"}

    def test_el_cambio_queda_auditado(self, operador):
        from sqlalchemy import desc, select

        from app.models import LogAuditoria

        operador.post("/dashboard/whatsapp/numeros-prueba", data={"numeros": "3001112233"})

        db = SessionLocal()
        try:
            ultimo = db.scalar(select(LogAuditoria).order_by(desc(LogAuditoria.id)).limit(1))
        finally:
            db.close()
        assert ultimo.accion == "ajuste_cambiado"
        assert "573001112233" in ultimo.detalle

    def test_la_pantalla_muestra_lo_que_esta_vigente(self, operador, monkeypatch):
        monkeypatch.setattr(settings, "evolution_url", "http://evolution.pruebas")
        monkeypatch.setattr(settings, "evolution_api_key", "apikey")
        monkeypatch.setattr(settings, "evolution_webhook_token", "token")

        operador.post("/dashboard/whatsapp/numeros-prueba", data={"numeros": "300 111 2233"})
        html = operador.get("/dashboard/whatsapp").text

        assert "573001112233" in html

    def test_el_invitado_no_puede_cambiarlo(self, monkeypatch):
        """La lista blanca decide a quién le habla el bot: es del operador."""
        monkeypatch.setattr(settings, "invitado_password", "invitado")
        cli = TestClient(app, follow_redirects=False)
        with cli:
            cli.post("/dashboard/login", data={"usuario": "invitado", "clave": "invitado"})
            r = cli.post(
                "/dashboard/whatsapp/numeros-prueba", data={"numeros": "3009998877"}
            )
        assert r.status_code == 403
        assert ajustes.numeros_prueba() != {"573009998877"}
