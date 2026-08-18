"""Cifrado, consentimiento y auditoría (RF-16, RF-17, RF-18, RF-19)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.channels.gateway import MensajeEntrante, alta_con_consentimiento
from app.models import Direccion, LogAuditoria
from app.security.crypto import cifrar, descifrar, enmascarar, indice_ciego
from app.services import leads
from app.services.compliance import (
    ConsentimientoRequerido,
    auditar,
    exigir_consentimiento,
    revocar_y_anonimizar,
    tiene_consentimiento_vigente,
    verificar_cadena,
)


class TestCifrado:
    def test_ida_y_vuelta(self):
        assert descifrar(cifrar("3001234567")) == "3001234567"

    def test_el_cifrado_no_es_determinista(self):
        # Fernet incluye IV aleatorio: dos cifrados del mismo dato difieren.
        assert cifrar("Andrés") != cifrar("Andrés")

    def test_indice_ciego_es_estable_y_no_reversible(self):
        h = indice_ciego("99887766")
        assert h == indice_ciego("99887766")
        assert "99887766" not in h
        assert len(h) == 64

    def test_enmascarar_oculta_el_centro(self):
        assert enmascarar("3001234567") == "30••••••67"
        assert enmascarar(None) == "—"

    def test_pii_no_queda_en_claro_en_sqlite(self, db, prospecto_consentido):
        """RF-17: leyendo el archivo por fuera del ORM no debe verse el dato."""
        crudo = db.execute(
            text("SELECT nombre, usuario_canal FROM prospectos WHERE id = :i"),
            {"i": prospecto_consentido.id},
        ).one()
        assert "Andrés Prueba" not in str(crudo)
        assert "andresprueba" not in str(crudo)
        # Pero el ORM sí lo devuelve descifrado.
        assert prospecto_consentido.nombre == "Andrés Prueba"

    def test_mensajes_tambien_van_cifrados(self, db, prospecto_consentido):
        leads.registrar_mensaje(
            db, prospecto_consentido, Direccion.ENTRANTE, "Vivo en la calle 10 #43-25"
        )
        db.commit()
        crudo = db.execute(text("SELECT texto FROM mensajes")).scalar_one()
        assert "calle 10" not in crudo


class TestConsentimiento:
    def test_alta_registra_autorizacion_con_texto_y_evidencia(self, db, prospecto_consentido):
        registro = prospecto_consentido.consentimientos[0]
        assert registro.otorgado is True
        assert "Autorizo a" in registro.texto_autorizacion
        assert registro.evidencia == "respondió Sí en el bot"
        assert registro.url_politica
        assert prospecto_consentido.consentimiento_ts is not None

    def test_prospecto_sin_consentimiento_bloquea_saliente(self, db, prospecto_consentido):
        """RF-19: el gateway no despacha nada a quien no autorizó."""
        prospecto_consentido.consentimiento = False
        prospecto_consentido.consentimiento_ts = None
        db.flush()

        assert tiene_consentimiento_vigente(prospecto_consentido) is False
        with pytest.raises(ConsentimientoRequerido):
            exigir_consentimiento(prospecto_consentido)
        with pytest.raises(ConsentimientoRequerido):
            leads.registrar_mensaje(
                db, prospecto_consentido, Direccion.SALIENTE, "Hola, tengo opciones para ti"
            )

    def test_reingreso_no_duplica_prospecto(self, db, prospecto_consentido):
        entrante = MensajeEntrante(
            canal="telegram", canal_id="99887766", texto="", nombre="Andrés Prueba"
        )
        otra_vez = alta_con_consentimiento(db, entrante, evidencia="volvió a aceptar")
        assert otra_vez.id == prospecto_consentido.id
        assert len(otra_vez.consentimientos) == 2

    def test_habeas_data_borra_pii_y_conserva_auditoria(self, db, prospecto_consentido):
        leads.registrar_mensaje(db, prospecto_consentido, Direccion.ENTRANTE, "mi cédula es X")
        db.flush()

        revocar_y_anonimizar(db, prospecto_consentido)
        db.commit()

        assert prospecto_consentido.nombre is None
        assert prospecto_consentido.telefono is None
        assert prospecto_consentido.consentimiento is False
        assert all(m.texto == "[suprimido]" for m in prospecto_consentido.mensajes)
        acciones = [r.accion for r in db.query(LogAuditoria).all()]
        assert "supresion_datos" in acciones


class TestAuditoria:
    def test_cadena_se_encadena_y_verifica(self, db, prospecto_consentido):
        auditar(db, actor="op", accion="prueba_1", entidad="x", entidad_id="1")
        auditar(db, actor="op", accion="prueba_2", entidad="x", entidad_id="2")
        db.commit()

        registros = db.query(LogAuditoria).order_by(LogAuditoria.id).all()
        assert len(registros) >= 3
        assert registros[1].hash_prev == registros[0].hash
        integra, roto = verificar_cadena(db)
        assert integra is True and roto is None

    def test_alterar_un_registro_rompe_la_cadena(self, db, prospecto_consentido):
        auditar(db, actor="op", accion="original", entidad="x", entidad_id="1")
        db.commit()

        objetivo = db.query(LogAuditoria).order_by(LogAuditoria.id).first()
        db.execute(
            text("UPDATE log_auditoria SET detalle = 'manipulado' WHERE id = :i"),
            {"i": objetivo.id},
        )
        db.commit()
        db.expire_all()

        integra, roto = verificar_cadena(db)
        assert integra is False
        assert roto == objetivo.id
