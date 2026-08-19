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
        assert all(m.propiedad.precio <= 400_000_000 for m in matches)

    def test_el_techo_de_precio_no_tiene_holgura(self, db):
        """Regresión: había un 5% de cortesía sobre el tope.

        Quien pide "hasta 380 millones" y recibe uno de 385 no lee una cortesía,
        lee un filtro que no funciona.
        """
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "presupuesto_max": 380_000_000}
        ids = [m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=10)]
        assert "PROP-MED-002" not in ids, "385 M está por encima del techo de 380 M"

    def test_el_rango_solo_muestra_lo_que_cae_dentro(self, db):
        perfil = {
            "ciudad": "Medellín", "tipo": "apartamento",
            "presupuesto_min": 300_000_000, "presupuesto_max": 400_000_000,
        }
        matches = matching_engine.buscar(db, perfil, limite=10)
        assert [m.propiedad.id for m in matches] == ["PROP-MED-002"]

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


class TestFichaDeInmueble:
    """Qué se lee de cada inmueble en el mensaje del comprador.

    La ficha vieja mostraba "Pereira, Pereira — lote · 0 hab · 2462 m²": el
    municipio repetido, una cifra de habitaciones que un lote nunca tendrá y
    ninguna pista de qué es el lote. Lo que distingue un inmueble de otro es su
    descripción, y esa era justo la que faltaba.
    """

    @staticmethod
    def _lote() -> Propiedad:
        return Propiedad(
            id="FICHA-1", ciudad="Pereira", zona="Pereira", tipo="lote",
            habitaciones=0, banos=0, area_m2=2462, precio=350_000_000,
            descripcion="Lote plano con servicios y vía pavimentada. Escritura al día.",
        )

    def _listado(self, propiedad: Propiedad) -> str:
        return gateway._formatear_matches(
            [matching_engine.Match(propiedad=propiedad, puntaje=1.0)],
            listado=True,
            municipio="Pereira",
        )

    def test_el_lote_no_anuncia_cero_habitaciones(self):
        assert "0 hab" not in self._listado(self._lote())

    def test_el_municipio_no_se_repite(self):
        texto = self._listado(self._lote())
        assert "Pereira, Pereira" not in texto
        assert "*Pereira*" in texto

    def test_lleva_tipo_area_precio_y_descripcion(self):
        texto = self._listado(self._lote())
        assert "Lote" in texto
        assert "2.462 m²" in texto           # con separador de miles
        assert "$350.000.000" in texto
        assert "Lote plano con servicios y vía pavimentada" in texto

    def test_el_apartamento_sí_muestra_habitaciones(self):
        apto = Propiedad(
            id="FICHA-2", ciudad="Pereira", zona="Álamos, Pereira", tipo="apartamento",
            habitaciones=3, banos=2, area_m2=82, precio=290_000_000,
            descripcion="Con balcón y parqueadero cubierto.",
        )
        texto = self._listado(apto)
        assert "3 hab" in texto
        assert "Álamos, Pereira" in texto

    def test_la_descripcion_larga_se_recorta(self):
        largo = self._lote()
        largo.descripcion = "Lote " + "muy amplio " * 40
        linea = [l for l in self._listado(largo).splitlines() if "Lote muy amplio" in l][0]
        assert len(linea.strip()) <= gateway.TOPE_DESCRIPCION + 5
        assert linea.rstrip().endswith("…")


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

    def test_el_handoff_no_pregunta_y_confirma_a_la_vez(self, db, prospecto_consentido):
        """RF-12: pedir asesor cierra el turno; no puede quedar una pregunta abierta.

        Ocurrió en producción: el bot preguntó "¿me confirmas tu nombre y un
        número de contacto?" y en el mismo segundo respondió "ya le pasé tus
        datos a un asesor". El comprador no sabía si contestar o esperar.
        """
        r = gateway.procesar(db, prospecto_consentido, "Asesor")
        db.commit()

        assert r.handoff is True
        assert len(r.textos) == 1, f"el handoff debe hablar solo: {r.textos}"
        assert "asesor" in r.textos[0].lower()
        assert "?" not in r.textos[0], "no se le puede pedir nada más al comprador"

    def test_el_handoff_convive_con_los_inmuebles(self, db, prospecto_consentido):
        """Mostrar opciones y pasar al asesor no se contradice: solo preguntar sí."""
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira hasta 420 millones")
        r = gateway.procesar(db, prospecto_consentido, "Quiero agendar una visita")
        db.commit()

        assert r.handoff is True
        assert r.matches, "debe seguir mostrando la cartera emparejada"
        assert any("asesor" in t.lower() for t in r.textos)

    def test_con_ciudad_y_tipo_lista_todo_numerado_y_separado(self, db, prospecto_consentido):
        """Una pregunta concreta merece el inventario completo, no una terna.

        Recortar a tres cuando el comprador pide "apartamentos en Medellín"
        hacía parecer que la cartera estaba casi vacía.
        """
        from app.models import Propiedad
        from app.services.portfolio import municipio_de

        # Por municipio, no por ciudad: un apartamento en Bello no es de Medellín.
        hay = sum(
            1
            for p in db.query(Propiedad).filter(
                Propiedad.ciudad == "Medellín", Propiedad.tipo == "apartamento"
            )
            if municipio_de(p) == "Medellín"
        )

        r = gateway.procesar(db, prospecto_consentido, "Apartamentos en Medellín")
        db.commit()

        assert len(r.matches) == hay, f"debe listar los {hay} que hay, no {len(r.matches)}"
        texto = r.textos[0]
        assert texto.startswith("Tengo "), texto[:60]
        assert "apartamentos" in texto and "Medellín" in texto

        # Numeración con punto y cada inmueble en su propio bloque.
        for i in range(1, len(r.matches) + 1):
            assert f"\n{i}. *" in f"\n{texto}", f"falta el ítem {i}"
        assert "\n\n1. *" in f"\n\n{texto}", "los ítems van separados por línea en blanco"
        assert ") *" not in texto, "la numeración vieja con paréntesis no debe volver"

    def test_el_municipio_acota_la_busqueda(self, db, prospecto_consentido):
        """Pereira y Dosquebradas son municipios distintos para el comprador.

        `ciudad` los mete en el mismo saco porque es la plaza de cobertura, pero
        quien pide Dosquebradas no quiere ver Pereira, ni al revés.
        """
        from app.models import Propiedad
        from app.services.portfolio import municipio_de

        db.add(
            Propiedad(
                id="MUN-DOS-1", ciudad="Pereira", zona="Los Naranjos, Dosquebradas",
                tipo="apartamento", habitaciones=2, banos=1, area_m2=55,
                precio=155_000_000, estado="disponible", descripcion="En Dosquebradas",
            )
        )
        db.add(
            Propiedad(
                id="MUN-PER-1", ciudad="Pereira", zona="Centro", tipo="apartamento",
                habitaciones=2, banos=1, area_m2=58, precio=195_000_000,
                estado="disponible", descripcion="En Pereira",
            )
        )
        db.flush()

        r = gateway.procesar(db, prospecto_consentido, "Apartamentos en Dosquebradas")
        db.commit()

        assert r.matches, "debe encontrar el de Dosquebradas"
        assert all(municipio_de(m.propiedad) == "Dosquebradas" for m in r.matches), [
            m.propiedad.zona for m in r.matches
        ]
        assert "Dosquebradas" in r.textos[0], "el encabezado nombra el municipio preguntado"

    def test_cambiar_de_municipio_reemplaza_el_filtro(self, db, prospecto_consentido):
        """El municipio del turno nuevo manda: no se acumula con el anterior."""
        from app.services.portfolio import municipio_de

        gateway.procesar(db, prospecto_consentido, "Casas en Dosquebradas")
        r = gateway.procesar(db, prospecto_consentido, "Casas en Pereira")
        db.commit()

        assert prospecto_consentido.municipio == "Pereira"
        if r.matches:
            assert all(municipio_de(m.propiedad) == "Pereira" for m in r.matches)

    def test_el_perfil_difuso_sigue_recibiendo_la_terna_curada(self, db, prospecto_consentido):
        """Sin tipo definido no hay lista que valga: se curan tres con su frase."""
        r = gateway.procesar(db, prospecto_consentido, "Busco algo en Medellín")
        db.commit()

        if r.matches:
            assert len(r.matches) <= 3
            assert "Con eso en mente" in r.textos[0]

    def test_pedir_asesor_dos_veces_no_duplica_la_solicitud(self, db, prospecto_consentido):
        """El asesor no puede ver al mismo comprador cuatro veces en la cola.

        `pide_visita` sigue en true en los turnos siguientes porque la petición
        queda en el historial; sin deduplicar, cada mensaje posterior creaba otra
        solicitud y otro aviso.
        """
        gateway.procesar(db, prospecto_consentido, "Asesor")
        gateway.procesar(db, prospecto_consentido, "Asesor")
        gateway.procesar(db, prospecto_consentido, "Quiero hablar con alguien")
        db.commit()

        assert len(prospecto_consentido.solicitudes) == 1

    def test_con_asesor_en_camino_el_bot_sigue_mostrando_cartera(self, db, prospecto_consentido):
        """Regresión: tras pedir asesor, el bot dejó de responder a las búsquedas.

        Se quedaba repitiendo "ya le pasé tus datos" ante cualquier pregunta, y
        el comprador perdía el catálogo justo cuando más ganas tiene de mirarlo.
        """
        gateway.procesar(db, prospecto_consentido, "Asesor")
        r = gateway.procesar(db, prospecto_consentido, "Casas en Pereira hasta 420 millones")
        db.commit()

        assert r.matches, f"debe seguir emparejando: {r.textos}"
        assert len(prospecto_consentido.solicitudes) == 1

    def test_reinsistir_recuerda_sin_crear_otra_solicitud(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Asesor")
        r = gateway.procesar(db, prospecto_consentido, "Asesor")
        db.commit()

        junto = " ".join(r.textos).lower()
        assert "ya está con el asesor" in junto, junto
        assert "ya le pasé tus datos" not in junto, "repetirlo sugiere una solicitud nueva"
        assert len(prospecto_consentido.solicitudes) == 1

    def test_nunca_se_le_piden_nombre_ni_telefono(self, db, prospecto_consentido):
        """El canal ya entrega ambos: pedirlos es PII innecesaria en texto libre."""
        for mensaje in ("Asesor", "Quiero que me llamen", "Busco casa"):
            r = gateway.procesar(db, prospecto_consentido, mensaje)
            junto = " ".join(r.textos).lower()
            assert "número de contacto" not in junto, mensaje
            assert "tu nombre" not in junto, mensaje
        db.commit()

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
