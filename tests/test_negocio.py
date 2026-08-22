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

    @staticmethod
    def _acompanante() -> Propiedad:
        """Un segundo inmueble: con uno solo el mensaje ya no es un listado."""
        return Propiedad(
            id="FICHA-0", ciudad="Pereira", zona="Cuba, Pereira", tipo="lote",
            habitaciones=0, banos=0, area_m2=800, precio=180_000_000,
            descripcion="Terreno esquinero.",
        )

    def _listado(self, propiedad: Propiedad) -> str:
        return gateway._formatear_matches(
            [
                matching_engine.Match(propiedad=propiedad, puntaje=2.0),
                matching_engine.Match(propiedad=self._acompanante(), puntaje=1.0),
            ],
            listado=True,
            municipio="Pereira",
        )

    def _ficha(self, propiedad: Propiedad) -> str:
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

    def test_un_solo_inmueble_trae_la_ficha_y_no_una_lista_de_uno(self):
        """Preguntar por uno concreto y recibir "1 opción(es)" es contestar de menos."""
        largo = self._lote()
        largo.descripcion = "Lote " + "muy amplio " * 40
        texto = self._ficha(largo)

        assert "opción(es)" not in texto
        assert "1. *" not in texto, "un solo inmueble no se numera"
        # La descripción va entera: no compite con otras nueve fichas.
        assert len(texto) > gateway.TOPE_DESCRIPCION * 2
        assert len(texto) < gateway.TOPE_FICHA + 300
        assert "*Pereira*" in texto and "$350.000.000" in texto


class TestFocoEnUnInmueble:
    """"Háblame solo de la ferretería de La Reforma" tiene que hacer justo eso.

    Reportado en producción: el comprador pedía lotes en Dosquebradas, el bot le
    listaba los seis y, al pedirle que le hablara solo de uno, le volvían los
    seis. Lo que identifica ese lote —"la ferretería", "La Reforma"— no cabe en
    ningún slot: está escrito en la zona y en la descripción de la ficha, así
    que el emparejamiento lo ignoraba por completo.
    """

    CARTERA = [
        ("La Reforma, Dosquebradas", "Lote donde funciona la ferretería, sobre vía principal."),
        ("Frailes, Dosquebradas", "Lote plano con servicios."),
        ("Los Naranjos, Dosquebradas", "Lote en ladera con vista."),
        ("El Japón, Dosquebradas", "Lote para bodega."),
        ("Santa Isabel, Dosquebradas", "Lote residencial en esquina."),
        ("La Badea, Dosquebradas", "Lote grande cerca a la variante."),
    ]

    @pytest.fixture()
    def cartera(self, db):
        for i, (zona, descripcion) in enumerate(self.CARTERA, start=1):
            db.add(
                Propiedad(
                    id=f"LOT-DOS-{i:03d}", ciudad="Pereira", zona=zona, tipo="lote",
                    habitaciones=0, banos=0, area_m2=500 + i, precio=100_000_000 * i,
                    estado="disponible", descripcion=descripcion,
                )
            )
        db.flush()
        return db

    @staticmethod
    def _pedir_lotes(db, prospecto):
        return gateway.procesar(db, prospecto, "Quiero lotes en Dosquebradas")

    def test_el_punto_de_partida_son_los_seis(self, cartera, prospecto_consentido):
        r = self._pedir_lotes(cartera, prospecto_consentido)
        assert len(r.matches) == 6

    def test_nombrar_uno_deja_solo_ese(self, cartera, prospecto_consentido):
        self._pedir_lotes(cartera, prospecto_consentido)
        r = gateway.procesar(
            cartera, prospecto_consentido, "Háblame solo de la ferretería de La Reforma"
        )
        cartera.commit()

        assert [m.propiedad.id for m in r.matches] == ["LOT-DOS-001"]
        assert "ferretería" in r.textos[0]

    def test_el_mensaje_dice_como_volver_a_verlos_todos(self, cartera, prospecto_consentido):
        """Un recorte que no se anuncia es indistinguible de una cartera pobre."""
        self._pedir_lotes(cartera, prospecto_consentido)
        r = gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")

        assert "*todos*" in r.textos[0]
        assert "6" in r.textos[0]

    def test_el_foco_sobrevive_a_la_pregunta_siguiente(self, cartera, prospecto_consentido):
        """"¿Y cuánto vale?" no es renunciar a lo que acaba de pedir."""
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        r = gateway.procesar(cartera, prospecto_consentido, "¿Y cuánto vale?")
        cartera.commit()

        assert [m.propiedad.id for m in r.matches] == ["LOT-DOS-001"]

    def test_pedir_todos_suelta_el_foco(self, cartera, prospecto_consentido):
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        r = gateway.procesar(cartera, prospecto_consentido, "Muéstrame todos los lotes")
        cartera.commit()

        assert len(r.matches) == 6
        assert prospecto_consentido.foco is None

    def test_cambiar_de_busqueda_tambien_lo_suelta(self, cartera, prospecto_consentido):
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        r = gateway.procesar(cartera, prospecto_consentido, "Mejor casas en Pereira")
        cartera.commit()

        assert prospecto_consentido.foco is None
        assert all(m.propiedad.tipo == "casa" for m in r.matches)

    def test_una_palabra_que_no_esta_en_la_cartera_no_recorta(self, cartera, prospecto_consentido):
        """Callar cinco lotes por una palabra suelta es peor que ignorarla."""
        self._pedir_lotes(cartera, prospecto_consentido)
        r = gateway.procesar(cartera, prospecto_consentido, "¿Alguno con helipuerto?")
        cartera.commit()

        assert len(r.matches) == 6
        assert prospecto_consentido.foco is None

    def test_el_barrio_tambien_acota(self, cartera, prospecto_consentido):
        """"El de Frailes" es tan concreto como un municipio, y antes no filtraba."""
        self._pedir_lotes(cartera, prospecto_consentido)
        r = gateway.procesar(cartera, prospecto_consentido, "Cuéntame del de Frailes")
        cartera.commit()

        assert [m.propiedad.id for m in r.matches] == ["LOT-DOS-002"]

    def test_el_numero_de_la_lista_señala_esa_ficha(self, cartera, prospecto_consentido):
        """"Lote 6" es la sexta del listado que acaba de recibir numerado."""
        r0 = self._pedir_lotes(cartera, prospecto_consentido)
        sexto = r0.matches[5].propiedad.id

        r = gateway.procesar(cartera, prospecto_consentido, "lote 6")
        cartera.commit()

        assert [m.propiedad.id for m in r.matches] == [sexto]

    def test_el_numero_señala_incluso_con_otro_foco_puesto(self, cartera, prospecto_consentido):
        """La posición se cuenta sobre la lista completa, no sobre el recorte."""
        r0 = self._pedir_lotes(cartera, prospecto_consentido)
        tercero = r0.matches[2].propiedad.id
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")

        r = gateway.procesar(cartera, prospecto_consentido, "el 3")
        cartera.commit()

        assert [m.propiedad.id for m in r.matches] == [tercero]

    def test_la_nota_nombra_el_inmueble_y_no_su_codigo(self, cartera, prospecto_consentido):
        """El foco de "lote 6" es un id interno: devolvérselo no le dice nada."""
        self._pedir_lotes(cartera, prospecto_consentido)
        r = gateway.procesar(cartera, prospecto_consentido, "lote 6")

        assert "LOT-DOS" not in r.textos[0]
        assert "La Badea" in r.textos[0]

    def test_volver_a_nombrar_la_busqueda_suelta_el_foco(self, cartera, prospecto_consentido):
        """"Lotes en Dosquebradas" es pedir el conjunto, aunque no cambie ni un slot.

        `_cambia_la_busqueda` dice que no cambió nada —lote y Dosquebradas ya
        estaban puestos— y el comprador quedaba encerrado en la ficha que había
        pedido, sin más salida que la palabra *todos*.
        """
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        r = self._pedir_lotes(cartera, prospecto_consentido)
        cartera.commit()

        assert prospecto_consentido.foco is None
        assert len(r.matches) == 6

    def test_pedir_visita_no_suelta_el_foco(self, cartera, prospecto_consentido):
        """"Visita al lote" nombra el tipo, pero es contestar el pie del mensaje."""
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        gateway.procesar(cartera, prospecto_consentido, "quiero visita al lote")
        cartera.commit()

        assert prospecto_consentido.foco == "ferreteria reforma"
        assert prospecto_consentido.solicitudes[-1].propiedad_id == "LOT-DOS-001"

    def test_la_visita_apunta_al_inmueble_enfocado(self, cartera, prospecto_consentido):
        """El asesor tiene que recibir la ficha por la que preguntó, no otra."""
        self._pedir_lotes(cartera, prospecto_consentido)
        gateway.procesar(cartera, prospecto_consentido, "Solo la ferretería de La Reforma")
        r = gateway.procesar(cartera, prospecto_consentido, "visita")
        cartera.commit()

        assert r.handoff is True
        assert prospecto_consentido.solicitudes[-1].propiedad_id == "LOT-DOS-001"


class TestMaquinaEstados:
    def test_transicion_valida(self, db, prospecto_consentido):
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        assert prospecto_consentido.estado == "calificado"

    def test_transicion_invalida_se_rechaza(self, db, prospecto_consentido):
        """`nuevo` no salta a `oferta`: no ha visto cartera ni ha hablado con nadie.

        `vendido` sí se admite desde aquí, y a propósito: reportar una venta no
        puede depender de por dónde vaya la ficha.
        """
        with pytest.raises(leads.TransicionInvalida):
            leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.OFERTA)

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

    @pytest.mark.parametrize(
        "estado", ["nuevo", "calificado", "emparejado", "contactado", "visita", "oferta", "fuera_de_alcance"]
    )
    def test_se_puede_reportar_la_venta_desde_cualquier_etapa_abierta(
        self, db, prospecto_consentido, estado
    ):
        """Regresión: confirmar la venta de un lead en `nuevo` devolvía un 500.

        El caso no es raro sino el corriente. Quien pregunta por lotes y no
        suelta presupuesto se queda en `nuevo` para siempre, aunque ya haya
        visto la ficha del inmueble que acabó comprando; el panel le ofrece el
        formulario porque hay propiedad atribuible, y al confirmar la máquina de
        estados se negaba con una excepción que nadie atrapaba.

        Poner fricción a reportar una venta incentiva justo lo que el sistema
        existe para evitar: las que no se reportan.
        """
        matching_engine.emparejar(
            db, prospecto_consentido,
            {"ciudad": "Pereira", "tipo": "casa", "presupuesto_max": 430_000_000},
        )
        prospecto_consentido.estado = estado
        db.flush()

        venta = commission.confirmar_venta(
            db, prospecto=prospecto_consentido,
            propiedad=db.get(Propiedad, "PROP-PER-001"),
            precio_venta=290_000_001, operador="op_marta",
        )
        db.commit()

        assert venta.comision_valor == 8_700_000
        assert prospecto_consentido.estado == "vendido"

    def test_desde_un_terminal_la_venta_se_rechaza_con_motivo(self, db, prospecto_consentido):
        """No con un 500: el operador tiene que poder leer por qué no se pudo."""
        propiedad = self._preparar(db, prospecto_consentido)
        prospecto_consentido.estado = "perdido"
        db.flush()

        with pytest.raises(commission.VentaInvalida, match="perdido"):
            commission.confirmar_venta(
                db, prospecto=prospecto_consentido, propiedad=propiedad,
                precio_venta=420_000_000, operador="op_marta",
            )

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

    def test_contestar_visita_no_repite_la_cartera(self, db, prospecto_consentido):
        """El pie pregunta "¿visita o asesor?"; contestarlo no es buscar de nuevo.

        Reportado en Telegram: el comprador pedía lotes, el bot los listaba y
        cerraba preguntando si quería visita o asesor. Al contestar "visita" le
        volvían los mismos lotes, con la confirmación del asesor enterrada al
        final. Parecía que el bot no había entendido su respuesta.
        """
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira hasta 420 millones")
        r = gateway.procesar(db, prospecto_consentido, "visita")
        db.commit()

        assert r.handoff is True
        assert not r.matches, "no se vuelve a emparejar: no preguntó nada nuevo"
        assert len(r.textos) == 1, f"el handoff debe hablar solo: {r.textos}"
        assert "asesor" in r.textos[0].lower()

    def test_la_solicitud_conserva_el_inmueble_ya_mostrado(self, db, prospecto_consentido):
        """Aunque el turno no vuelva a emparejar, el asesor sabe por cuál llaman."""
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira hasta 420 millones")
        gateway.procesar(db, prospecto_consentido, "asesor")
        db.commit()

        solicitud = prospecto_consentido.solicitudes[0]
        assert solicitud.propiedad_id, "la solicitud debe apuntar a lo que ya vio"

    def test_nombrar_lo_que_ya_busca_no_es_una_busqueda_nueva(self, db, prospecto_consentido):
        """Nadie contesta "visita" a secas: contesta "visita al apartamento".

        Reportado en Telegram con el arreglo anterior ya puesto. La regla miraba
        si el mensaje mencionaba un tipo o una ciudad, no si eso cambiaba algo:
        el "apartamento" que el comprador nombra para señalar lo que quiere ver
        —y que lleva tres turnos en su perfil— se leía como una consulta nueva y
        le devolvía el mismo listado.
        """
        gateway.procesar(
            db, prospecto_consentido, "Busco apartamento en Medellín hasta 500 millones"
        )
        r = gateway.procesar(db, prospecto_consentido, "quiero agendar una visita al apartamento")
        db.commit()

        assert r.handoff is True
        assert not r.matches, "el tipo ya estaba en el perfil: no cambia la búsqueda"
        assert len(r.textos) == 1, f"el handoff debe hablar solo: {r.textos}"

    def test_un_lead_perdido_que_pide_visita_llega_al_asesor(
        self, db, prospecto_consentido
    ):
        """Un estado terminal no puede tragarse la petición en silencio.

        Desde `perdido`, cada "quiero agendar una visita al lote" volvía a listar
        los cinco lotes de Pereira y no creaba solicitud ninguna: el comprador
        pedía un asesor que nadie iba a llamar. Quien vuelve después de dado por
        perdido es justo a quien más conviene pasarle un humano.

        `vendido` es el otro caso y va aparte: ahí la conversación está cerrada y
        lo que vuelve es un lead nuevo (`test_saludo_despedida.TestLeadVendido`).
        """
        terminal = "perdido"
        gateway.procesar(db, prospecto_consentido, "Busco lote en Pereira hasta 700 millones")
        prospecto_consentido.estado = terminal
        db.flush()

        r = gateway.procesar(db, prospecto_consentido, "quiero agendar una visita al lote")
        db.commit()

        assert r.handoff is True, "la petición no puede desaparecer"
        assert not r.matches, "y tampoco puede contestarse repitiendo el catálogo"
        assert len(prospecto_consentido.solicitudes) == 1
        assert prospecto_consentido.estado == terminal, "el estado terminal no se mueve"

    def test_el_handoff_convive_con_los_inmuebles(self, db, prospecto_consentido):
        """Mostrar opciones y pasar al asesor no se contradice: solo preguntar sí.

        Cuando el mensaje sí mueve la búsqueda, la cartera vuelve a salir: ahí
        el comprador preguntó por algo, no solo contestó el pie.
        """
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira hasta 420 millones")
        r = gateway.procesar(
            db, prospecto_consentido, "Quiero visitar los apartamentos de Medellín"
        )
        db.commit()

        assert r.handoff is True
        assert r.matches, "cambió ciudad y tipo: hay cartera nueva que mostrar"
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

    def test_el_llm_solo_no_puede_abrir_un_handoff(self, db, prospecto_consentido, monkeypatch):
        """Regresión: el bot pasaba a un asesor sin que el comprador lo pidiera.

        El LLM ve la conversación entera, y ahí está el pie con el que el bot
        mismo ofrece la visita: devolvía `pide_visita` en true por habérsela
        ofrecido nosotros. Pedir "lotes en Pereira" terminaba en "ya le pasé tus
        datos a un asesor" y en una visita en la cola del operador que el
        comprador nunca pidió ni espera.
        """
        from app.services.nlu_engine import Analisis

        def analisis_contaminado(mensaje, historial, perfil, **kwargs):
            return Analisis(slots=dict(perfil), pide_visita=True)

        monkeypatch.setattr(gateway, "analizar", analisis_contaminado)
        r = gateway.procesar(db, prospecto_consentido, "Lotes en Pereira")
        db.commit()

        assert not r.handoff, r.textos
        assert prospecto_consentido.solicitudes == []
        assert "asesor" not in " ".join(r.textos).lower()

    def test_pedirlo_con_sus_palabras_si_abre_el_handoff(self, db, prospecto_consentido, monkeypatch):
        """La otra mitad: el LLM en silencio no puede impedir lo que él sí pidió."""
        from app.services.nlu_engine import Analisis

        monkeypatch.setattr(
            gateway,
            "analizar",
            lambda mensaje, historial, perfil, **kw: Analisis(
                slots=dict(perfil), pide_visita=False
            ),
        )
        r = gateway.procesar(db, prospecto_consentido, "Quiero agendar una visita")
        db.commit()

        assert r.handoff
        assert len(prospecto_consentido.solicitudes) == 1

    def test_pedir_asesor_dos_veces_no_duplica_la_solicitud(self, db, prospecto_consentido):
        """El asesor no puede ver al mismo comprador cuatro veces en la cola.

        Sin deduplicar, cada "asesor" repetido creaba otra solicitud y otro
        aviso, y el mismo comprador aparecía cuatro veces en la lista.
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


class TestHoraLocal:
    """Lo que se guarda es UTC; lo que se lee tiene que ser la hora de Colombia.

    Reportado: una solicitud recibida a las 10:47 pm figuraba en el dashboard
    como "20/08 03:47" —de madrugada y del día siguiente—, así que el operador
    no la reconocía como la que acababa de entrar.
    """

    def test_la_madrugada_utc_es_la_noche_de_ayer(self):
        from datetime import datetime

        from app.tiempo import fecha

        assert fecha(datetime(2026, 8, 20, 3, 47)) == "19/08/2026 22:47"

    def test_un_instante_con_zona_tambien_se_convierte(self):
        from datetime import datetime, timezone

        from app.tiempo import fecha

        momento = datetime(2026, 8, 20, 3, 47, tzinfo=timezone.utc)
        assert fecha(momento, "%H:%M") == "22:47"

    def test_sin_fecha_no_escribe_none(self):
        from app.tiempo import fecha

        assert fecha(None) == "—"

    def test_el_dashboard_pinta_la_hora_local(self):
        """El filtro está enganchado en el entorno real de las plantillas."""
        from datetime import datetime

        from app.routers.dashboard import plantillas

        pintado = plantillas.env.from_string("{{ x | fecha('%d/%m %H:%M') }}")
        assert pintado.render(x=datetime(2026, 8, 20, 3, 47)) == "19/08 22:47"

    def test_ninguna_plantilla_formatea_la_fecha_por_su_cuenta(self):
        """Un `.strftime` suelto en una plantilla vuelve a mostrar UTC."""
        from app.config import RAIZ

        culpables = [
            ruta.name
            for ruta in (RAIZ / "app" / "templates").glob("*.html")
            if "strftime" in ruta.read_text(encoding="utf-8")
        ]
        assert culpables == [], f"usan strftime en vez del filtro `fecha`: {culpables}"
