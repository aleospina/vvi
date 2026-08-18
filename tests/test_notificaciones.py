"""Aviso al asesor cuando entra una solicitud (RF-12).

Lo importante aquí es lo que el aviso NO lleva: los datos de contacto del
titular no salen por Telegram ni por correo (RF-17, minimización).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import Propiedad, Solicitud
from app.services import leads, notificaciones


@pytest.fixture()
def solicitud(db, prospecto_consentido):
    """Una solicitud de visita real, con inmueble asociado."""
    p = prospecto_consentido
    p.nombre = "Andrés Prueba"
    p.telefono = "3001234567"
    p.ciudad, p.tipo, p.score_intencion, p.etiqueta = "Pereira", "apartamento", 86, "caliente"
    db.flush()
    s = Solicitud(
        prospecto_id=p.id, propiedad_id="PROP-PER-001", tipo="visita", detalle="Asesor"
    )
    db.add(s)
    db.flush()
    return s


class TestContenido:
    def test_el_aviso_no_lleva_datos_de_contacto(self, db, solicitud, prospecto_consentido):
        """El teléfono y el nombre se quedan en el dashboard, tras autenticación."""
        asunto, cuerpo = notificaciones._texto(solicitud, prospecto_consentido)
        assert "3001234567" not in cuerpo
        assert "Andrés Prueba" not in cuerpo
        assert prospecto_consentido.codigo in cuerpo
        assert prospecto_consentido.codigo in asunto

    def test_el_aviso_lleva_lo_que_el_asesor_necesita(self, db, solicitud, prospecto_consentido):
        _, cuerpo = notificaciones._texto(solicitud, prospecto_consentido)
        assert "visita" in cuerpo
        assert "Pinares, Pereira" in cuerpo          # el inmueble concreto
        assert "86" in cuerpo                        # score, para priorizar
        assert f"/dashboard/prospecto/{prospecto_consentido.codigo}" in cuerpo

    def test_solicitud_sin_inmueble_no_rompe_el_aviso(self, db, prospecto_consentido):
        s = Solicitud(prospecto_id=prospecto_consentido.id, tipo="asesor")
        db.add(s)
        db.flush()
        _, cuerpo = notificaciones._texto(s, prospecto_consentido)
        assert "sin inmueble asociado" in cuerpo


class TestEnvio:
    def test_sin_canales_configurados_no_falla(self, db, solicitud, prospecto_consentido):
        assert notificaciones.notificar_solicitud(solicitud, prospecto_consentido) == []

    def test_desactivar_notificaciones_las_omite(
        self, db, solicitud, prospecto_consentido, monkeypatch
    ):
        monkeypatch.setattr(settings, "notificaciones_activas", False)
        monkeypatch.setattr(notificaciones, "_enviar_telegram", lambda *a: pytest.fail("no debió enviar"))
        assert notificaciones.notificar_solicitud(solicitud, prospecto_consentido) == []

    def test_un_canal_caido_no_impide_el_otro(
        self, db, solicitud, prospecto_consentido, monkeypatch
    ):
        def telegram_roto(_cuerpo):
            raise ConnectionError("red caída")

        monkeypatch.setattr(notificaciones, "_enviar_telegram", telegram_roto)
        monkeypatch.setattr(notificaciones, "_enviar_correo", lambda *a: True)
        assert notificaciones.notificar_solicitud(solicitud, prospecto_consentido) == ["correo"]

    def test_el_fallo_del_aviso_no_tumba_la_solicitud(
        self, db, prospecto_consentido, monkeypatch
    ):
        """La solicitud es el dato de negocio; el aviso es una comodidad."""
        def todo_roto(*_args):
            raise ConnectionError("red caída")

        monkeypatch.setattr(notificaciones, "_enviar_telegram", todo_roto)
        monkeypatch.setattr(notificaciones, "_enviar_correo", todo_roto)

        s = leads.solicitar_handoff(
            db, prospecto_consentido, tipo="visita", propiedad_id="PROP-PER-001"
        )
        assert s.id is not None
        assert db.get(Solicitud, s.id) is not None


class TestIntegracionConElHandoff:
    def test_pedir_visita_notifica(self, db, prospecto_consentido, monkeypatch):
        enviados: list[str] = []
        monkeypatch.setattr(
            notificaciones, "_enviar_telegram", lambda cuerpo: enviados.append(cuerpo) or True
        )
        monkeypatch.setattr(notificaciones, "_enviar_correo", lambda *a: False)

        leads.solicitar_handoff(
            db, prospecto_consentido, tipo="visita", propiedad_id="PROP-PER-001"
        )
        assert len(enviados) == 1
        assert prospecto_consentido.codigo in enviados[0]

    def test_la_notificacion_queda_auditada(self, db, prospecto_consentido, monkeypatch):
        from app.models import LogAuditoria

        monkeypatch.setattr(notificaciones, "_enviar_telegram", lambda *a: True)
        monkeypatch.setattr(notificaciones, "_enviar_correo", lambda *a: False)
        leads.solicitar_handoff(db, prospecto_consentido, tipo="visita")

        acciones = [r.accion for r in db.query(LogAuditoria).all()]
        assert "asesor_notificado" in acciones
