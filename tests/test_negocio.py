"""Matching, máquina de estados, comisión y captación opt-in.

Cubre RF-09, RF-13, RF-14, RF-15 y ADR-01/ADR-05.
"""

from __future__ import annotations

import pytest

from app.channels import gateway
from app.models import EstadoProspecto, Propiedad, Venta
from app.services import commission, leads, matching_engine, prospecting
from app.services.prospecting import ConsentimientoAusente


class TestMatching:
    def test_filtra_por_ciudad_tipo_y_presupuesto(self, db):
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "presupuesto_max": 400_000_000}
        matches = matching_engine.buscar(db, perfil)
        ids = [m.propiedad.id for m in matches]

        assert "PROP-MED-002" in ids          # 385 M, apto en Medellín
        assert "PROP-MED-003" not in ids      # es casa
        assert "PROP-PER-001" not in ids      # es Pereira
        assert all(m.propiedad.precio <= 400_000_000 * 1.05 for m in matches)

    def test_devuelve_maximo_tres(self, db):
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "presupuesto_max": 900_000_000}
        assert len(matching_engine.buscar(db, perfil)) <= 3

    def test_rankea_lo_mas_cercano_al_tope(self, db):
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "presupuesto_max": 400_000_000}
        matches = matching_engine.buscar(db, perfil)
        assert matches[0].propiedad.id == "PROP-MED-002"  # 385 M aprovecha el presupuesto

    def test_respeta_habitaciones_minimas(self, db):
        perfil = {
            "ciudad": "Medellín", "tipo": "apartamento",
            "presupuesto_max": 600_000_000, "habitaciones": 3,
        }
        assert all(m.propiedad.habitaciones >= 3 for m in matching_engine.buscar(db, perfil))

    def test_perfil_incompleto_no_empareja(self, db):
        assert matching_engine.buscar(db, {"ciudad": "Medellín"}) == []

    def test_sin_presupuesto_devuelve_toda_la_cartera(self, db):
        """El presupuesto filtra, no habilita: sin banda se ve el catálogo completo."""
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        ids = [m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=10)]
        assert set(ids) == {"PROP-MED-001", "PROP-MED-002", "PROP-MED-004"}

    def test_conteo_explica_el_recorte_por_precio(self, db):
        """Regresión: una banda heredada dejaba 1 de 3 sin decir por qué."""
        perfil = {
            "ciudad": "Medellín", "tipo": "apartamento",
            "presupuesto_min": 200_000_000, "presupuesto_max": 300_000_000,
        }
        en_rango, total = matching_engine.conteo(db, perfil)
        assert (en_rango, total) == (1, 3)

        nota = gateway._nota_filtro(perfil, en_rango, total)
        assert "$200.000.000" in nota and "$300.000.000" in nota
        assert "2 quedan fuera" in nota
        assert "sin tope" in nota

    def test_sin_resultados_devuelve_lista_vacia(self, db):
        perfil = {"ciudad": "Pereira", "tipo": "lote", "presupuesto_max": 100_000_000}
        assert matching_engine.buscar(db, perfil) == []

    def test_frases_de_venta_no_inventan(self, db):
        """Sin LLM, la frase se arma solo con datos reales de la ficha (RF-07)."""
        perfil = {
            "ciudad": "Medellín", "tipo": "apartamento",
            "presupuesto_max": 400_000_000, "habitaciones": 3, "zona": "Belén",
        }
        matches = matching_engine.redactar_frases(matching_engine.buscar(db, perfil), perfil)
        assert matches[0].frase_venta
        assert "Belén" in matches[0].frase_venta or "habitaciones" in matches[0].frase_venta

    def test_registra_emparejamientos_para_atribucion(self, db, prospecto_consentido):
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "presupuesto_max": 400_000_000}
        matching_engine.emparejar(db, prospecto_consentido, perfil)
        db.commit()
        atribuibles = commission.propiedades_atribuibles(db, prospecto_consentido)
        assert len(atribuibles) >= 1


class TestMaquinaEstados:
    def test_transicion_valida(self, db, prospecto_consentido):
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        assert prospecto_consentido.estado == "calificado"

    def test_transicion_invalida_se_rechaza(self, db, prospecto_consentido):
        with pytest.raises(leads.TransicionInvalida):
            leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.VENDIDO)

    def test_no_se_sale_de_un_estado_terminal(self, db, prospecto_consentido):
        for estado in (
            EstadoProspecto.CALIFICADO, EstadoProspecto.EMPAREJADO,
            EstadoProspecto.VISITA, EstadoProspecto.PERDIDO,
        ):
            leads.cambiar_estado(db, prospecto_consentido, estado)
        with pytest.raises(leads.TransicionInvalida):
            leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.VISITA)

    def test_handoff_crea_solicitud_y_mueve_estado(self, db, prospecto_consentido):
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        solicitud = leads.solicitar_handoff(db, prospecto_consentido, tipo="visita")
        db.commit()
        assert solicitud.estado == "pendiente"
        assert prospecto_consentido.estado == "visita"


class TestComision:
    def test_calculo_del_tres_por_ciento(self):
        assert commission.calcular_comision(420_000_000) == 12_600_000

    def test_precio_invalido(self):
        with pytest.raises(commission.VentaInvalida):
            commission.calcular_comision(0)

    def _preparar(self, db, prospecto):
        perfil = {"ciudad": "Pereira", "tipo": "casa", "presupuesto_max": 430_000_000}
        matching_engine.emparejar(db, prospecto, perfil)
        for estado in (EstadoProspecto.CALIFICADO, EstadoProspecto.EMPAREJADO, EstadoProspecto.VISITA):
            leads.cambiar_estado(db, prospecto, estado)
        return db.get(Propiedad, "PROP-PER-001")

    def test_confirmar_venta_calcula_y_atribuye(self, db, prospecto_consentido):
        """CU-3: el humano confirma, el sistema calcula y atribuye (RF-14/15)."""
        propiedad = self._preparar(db, prospecto_consentido)
        venta = commission.confirmar_venta(
            db, prospecto=prospecto_consentido, propiedad=propiedad,
            precio_venta=420_000_000, operador="op_marta",
        )
        db.commit()

        assert venta.comision_valor == 12_600_000
        assert venta.comision_pct == 0.03
        assert venta.canal_origen == "telegram"      # atribución al canal de origen
        assert venta.operador == "op_marta"
        assert prospecto_consentido.estado == "vendido"
        assert propiedad.estado == "vendida"

    def test_no_se_puede_vender_dos_veces(self, db, prospecto_consentido):
        propiedad = self._preparar(db, prospecto_consentido)
        commission.confirmar_venta(
            db, prospecto=prospecto_consentido, propiedad=propiedad,
            precio_venta=420_000_000, operador="op_marta",
        )
        with pytest.raises(commission.VentaInvalida):
            commission.confirmar_venta(
                db, prospecto=prospecto_consentido, propiedad=propiedad,
                precio_venta=400_000_000, operador="op_marta",
            )

    def test_la_venta_exige_operador_identificado(self, db, prospecto_consentido):
        propiedad = self._preparar(db, prospecto_consentido)
        with pytest.raises(commission.VentaInvalida):
            commission.confirmar_venta(
                db, prospecto=prospecto_consentido, propiedad=propiedad,
                precio_venta=420_000_000, operador="   ",
            )

    def test_resumen_agrega_por_canal(self, db, prospecto_consentido):
        propiedad = self._preparar(db, prospecto_consentido)
        commission.confirmar_venta(
            db, prospecto=prospecto_consentido, propiedad=propiedad,
            precio_venta=420_000_000, operador="op_marta",
        )
        db.commit()
        resumen = commission.resumen(db)
        assert resumen["ventas"] == 1
        assert resumen["comision_generada"] == 12_600_000
        assert resumen["por_canal"][0]["canal"] == "telegram"

    def test_alerta_de_seguimiento_para_negocios_estancados(self, db, prospecto_consentido):
        """HU-09: una oportunidad en visita sin desenlace queda visible."""
        from datetime import datetime, timedelta, timezone

        self._preparar(db, prospecto_consentido)
        prospecto_consentido.actualizado_en = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

        alertas = leads.alertas_seguimiento(db)
        assert len(alertas) == 1
        assert alertas[0]["prospecto"].codigo == prospecto_consentido.codigo
        assert alertas[0]["severidad"] == "alta"


class TestCaptacionOptIn:
    def test_lead_sin_consentimiento_se_rechaza_y_no_persiste(self, db):
        """ADR-01: sin autorización no entra PII al sistema."""
        with pytest.raises(ConsentimientoAusente):
            prospecting.ingerir_lead(
                db, red="instagram", canal_id="ig_12345",
                consentimiento=False, evidencia="ninguna",
                nombre="Laura", telefono="3001112233",
            )
        db.commit()
        from app.models import Prospecto

        assert db.query(Prospecto).count() == 0

    def test_consentimiento_sin_evidencia_se_rechaza(self, db):
        with pytest.raises(ConsentimientoAusente):
            prospecting.ingerir_lead(
                db, red="instagram", canal_id="ig_12345",
                consentimiento=True, evidencia="   ", nombre="Laura",
            )

    def test_lead_con_optin_entra_y_queda_atribuido(self, db):
        resultado = prospecting.ingerir_lead(
            db, red="instagram", canal_id="ig_12345",
            consentimiento=True,
            evidencia="Casilla marcada en formulario de Lead Ads",
            nombre="Laura Gómez", telefono="3001112233", campana="ig-bio-medellin",
            mensaje="Busco apartamento en Medellín hasta 400 millones",
        )
        db.commit()

        p = resultado.prospecto
        assert p.red_origen == "instagram"
        assert p.campana == "ig-bio-medellin"
        assert p.consentimiento is True
        assert p.ciudad == "Medellín"          # el mensaje inicial ya calificó
        assert p.presupuesto_max == 400_000_000
        assert resultado.respuesta is not None and resultado.respuesta.textos

    def test_radar_de_canales_agrega_por_red(self, db):
        for i, red in enumerate(["instagram", "instagram", "olx"]):
            prospecting.ingerir_lead(
                db, red=red, canal_id=f"{red}_{i}",
                consentimiento=True, evidencia="opt-in registrado",
                nombre=f"Persona {i}",
                mensaje="Busco casa en Pereira de 3 habitaciones hasta 420 millones para este mes",
            )
        db.commit()

        radar = {c["red"]: c for c in prospecting.rendimiento_canales(db)}
        assert radar["instagram"]["prospectos"] == 2
        assert radar["olx"]["prospectos"] == 1
        assert radar["instagram"]["calificados"] >= 1

    def test_parseo_de_payload_de_meta(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "leadgen_id": "555",
                                "form_id": "form_1",
                                "ad_id": "ad_9",
                                "field_data": [
                                    {"name": "full_name", "values": ["Laura Gómez"]},
                                    {"name": "phone_number", "values": ["3001112233"]},
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        leads_meta = prospecting.parsear_lead_ads(payload)
        assert leads_meta[0]["nombre"] == "Laura Gómez"
        assert leads_meta[0]["red"] == "instagram"
        assert leads_meta[0]["tiene_campos"] is True


class TestConversacionCompleta:
    def test_flujo_calificar_emparejar_y_pedir_visita(self, db, prospecto_consentido):
        """CU-1 + CU-2 de punta a punta, sin LLM."""
        r1 = gateway.procesar(db, prospecto_consentido, "Hola, busco apartamento")
        assert r1.textos and not r1.matches
        assert prospecto_consentido.tipo == "apartamento"

        r2 = gateway.procesar(
            db, prospecto_consentido, "En Medellín, 3 habitaciones, hasta 400 millones"
        )
        assert r2.matches, "con el perfil completo debe emparejar"
        assert prospecto_consentido.estado in ("emparejado", "calificado")

        r3 = gateway.procesar(db, prospecto_consentido, "Quiero agendar una visita")
        db.commit()
        assert r3.handoff is True
        assert prospecto_consentido.estado == "visita"
        assert len(prospecto_consentido.solicitudes) == 1

    def test_fuera_de_alcance_se_dice_con_transparencia(self, db, prospecto_consentido):
        """CU-4: no forzamos el contacto fuera de cobertura."""
        r = gateway.procesar(db, prospecto_consentido, "Busco casa en Cali de 600 millones")
        db.commit()
        assert "Medellín" in r.textos[0] and "Pereira" in r.textos[0]
        assert prospecto_consentido.estado == "fuera_de_alcance"

    def test_toda_la_conversacion_queda_registrada(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira hasta 420 millones")
        db.commit()
        direcciones = [m.direccion for m in prospecto_consentido.mensajes]
        assert "entrante" in direcciones and "saliente" in direcciones
