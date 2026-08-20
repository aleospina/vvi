"""Extracción de slots, reglas duras y score de intención (RF-05, RF-06, ADR-03)."""

from __future__ import annotations

import pytest

from app.models import Etiqueta
from app.services.nlu_engine import (
    analizar,
    es_afirmativo,
    es_negativo,
    etiquetar,
    extraer_presupuesto,
    extraer_slots,
    fuera_de_alcance,
    pide_visita,
    score_por_reglas,
    terminos_de_mencion,
)


class TestPresupuesto:
    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("tengo 450 millones", (None, 450_000_000)),
            ("hasta 500 millones", (None, 500_000_000)),
            ("máximo $300.000.000", (None, 300_000_000)),
            ("entre 300 y 450 millones", (300_000_000, 450_000_000)),
            ("desde 200 millones", (200_000_000, None)),
            ("presupuesto de 380000000", (None, 380_000_000)),
            ("no sé todavía", (None, None)),
        ],
    )
    def test_variantes(self, texto, esperado):
        assert extraer_presupuesto(texto) == esperado

    def test_no_confunde_habitaciones_ni_area_con_dinero(self):
        pmin, pmax = extraer_presupuesto("busco algo de 3 habitaciones y 92 m2")
        assert (pmin, pmax) == (None, None)

    def test_habitaciones_y_presupuesto_en_la_misma_frase(self):
        slots = extraer_slots("quiero 3 habitaciones con 450 millones")
        assert slots["habitaciones"] == 3
        assert slots["presupuesto_max"] == 450_000_000


class TestSlots:
    def test_extrae_ciudad_tipo_y_plazo(self):
        slots = extraer_slots("Busco apartamento en Medellín, quiero comprar este mes")
        assert slots["ciudad"] == "Medellín"
        assert slots["tipo"] == "apartamento"
        assert slots["plazo_compra"] == "1-3 meses"

    def test_tolera_falta_de_tildes_y_mayusculas(self):
        assert extraer_slots("CASA EN PEREIRA")["ciudad"] == "Pereira"
        assert extraer_slots("apto en medellin")["tipo"] == "apartamento"

    def test_barrio_conocido_infiere_ciudad_y_zona(self):
        slots = extraer_slots("me interesa Laureles")
        assert slots["ciudad"] == "Medellín"
        assert slots["zona"] == "Laureles"

    def test_lote(self):
        assert extraer_slots("busco un lote en Cerritos")["tipo"] == "lote"

    def test_detecta_intencion_de_visita(self):
        assert pide_visita("quiero agendar una visita") is True
        assert pide_visita("¿me pueden llamar?") is False  # no está en el diccionario
        assert pide_visita("prefiero hablar con un asesor") is True
        assert pide_visita("solo estoy mirando precios") is False

    def test_afirmaciones_y_negaciones(self):
        assert es_afirmativo("Sí") and es_afirmativo("acepto") and es_afirmativo("dale")
        assert es_negativo("No") and es_negativo("no autorizo")
        assert not es_afirmativo("no")
        assert not es_afirmativo("quiero un apartamento")


class TestReglasDuras:
    def test_ciudad_fuera_de_cobertura(self):
        assert fuera_de_alcance({"ciudad": "Cali"}) == "ciudad_no_cubierta:Cali"

    def test_ciudad_cubierta_pasa(self):
        assert fuera_de_alcance({"ciudad": "Pereira"}) is None

    def test_presupuesto_bajo_el_piso_de_cartera(self):
        motivo = fuera_de_alcance(
            {"ciudad": "Medellín", "presupuesto_max": 50_000_000},
            precio_minimo_cartera=215_000_000,
        )
        assert motivo.startswith("presupuesto_bajo")


class TestScore:
    def test_perfil_completo_y_urgente_es_caliente(self):
        slots = {
            "ciudad": "Medellín", "tipo": "apartamento",
            "presupuesto_max": 450_000_000, "habitaciones": 3,
            "plazo_compra": "inmediato",
        }
        score = score_por_reglas(slots, n_mensajes=5, visita=True)
        assert score >= 70
        assert etiquetar(score) is Etiqueta.CALIENTE

    def test_curioso_sin_datos_es_frio(self):
        score = score_por_reglas({"plazo_compra": "solo explorando"}, n_mensajes=1, visita=False)
        assert etiquetar(score) is Etiqueta.FRIO

    def test_perfil_parcial_es_tibio(self):
        slots = {"ciudad": "Pereira", "tipo": "casa", "presupuesto_max": 400_000_000}
        score = score_por_reglas(slots, n_mensajes=3, visita=False)
        assert etiquetar(score) is Etiqueta.TIBIO

    def test_score_acotado_a_100(self):
        slots = {
            "ciudad": "Medellín", "tipo": "casa", "presupuesto_max": 900_000_000,
            "habitaciones": 4, "plazo_compra": "inmediato",
        }
        assert score_por_reglas(slots, n_mensajes=99, visita=True) == 100


class TestAnalisisSinLLM:
    """Sin llaves configuradas el análisis debe seguir funcionando (degradación)."""

    def test_extrae_y_puntua(self):
        a = analizar(
            "Quiero un apartamento en Medellín de 3 habitaciones, hasta 450 millones, para este mes",
            historial=[],
            perfil_actual={},
        )
        assert a.fuente == "reglas"
        assert a.slots["ciudad"] == "Medellín"
        assert a.slots["tipo"] == "apartamento"
        assert a.slots["presupuesto_max"] == 450_000_000
        assert a.faltan_datos == []
        assert a.etiqueta in (Etiqueta.TIBIO, Etiqueta.CALIENTE)

    def test_reporta_lo_que_falta(self):
        a = analizar("Busco casa", historial=[], perfil_actual={})
        assert "ciudad" in a.faltan_datos
        assert "presupuesto_max" in a.faltan_datos
        assert "tipo" not in a.faltan_datos

    def test_fuera_de_alcance_topa_el_score(self):
        a = analizar("Quiero casa en Cali con 800 millones", historial=[], perfil_actual={})
        assert a.motivo_fuera_alcance is not None
        assert a.score <= 30

    def test_el_mensaje_nuevo_corrige_el_perfil_previo(self):
        a = analizar(
            "Perdón, me equivoqué: es en Pereira",
            historial=[],
            perfil_actual={"ciudad": "Medellín", "tipo": "casa"},
        )
        assert a.slots["ciudad"] == "Pereira"
        assert a.slots["tipo"] == "casa"  # lo previo se conserva

    def test_un_piso_nuevo_borra_el_techo_anterior(self):
        """Regresión: 'desde X' heredaba el techo del turno previo y recortaba la cartera."""
        a = analizar(
            "Pásame los apartamentos que tengas en Medellín desde 200 millones",
            historial=[],
            perfil_actual={"ciudad": "Medellín", "tipo": "apartamento",
                           "presupuesto_max": 309_000_000},
        )
        assert a.slots["presupuesto_min"] == 200_000_000
        assert a.slots["presupuesto_max"] is None
        assert a.faltan_datos == []  # un piso solo ya permite emparejar

    def test_sin_tope_limpia_la_banda_heredada(self):
        a = analizar(
            "sin tope",
            historial=[],
            perfil_actual={"ciudad": "Medellín", "tipo": "apartamento",
                           "presupuesto_min": 200_000_000, "presupuesto_max": 300_000_000},
        )
        assert a.slots["presupuesto_min"] is None
        assert a.slots["presupuesto_max"] is None


class TestTerminosDeMencion:
    """Con qué palabras un comprador señala un inmueble concreto.

    De esto depende que "háblame solo de la ferretería de La Reforma" recorte la
    cartera. El riesgo está en el otro lado: si una frase corriente dejara
    términos, el bot escondería inventario por una palabra de cortesía.
    """

    @pytest.mark.parametrize(
        "frase",
        [
            "Hola, buenas tardes",
            "Sí, autorizo",
            "gracias, muy amable",
            "Quiero lotes en Dosquebradas",
            "En Medellín, 3 habitaciones, hasta 400 millones",
            "Me interesa, quiero una visita",
            "Muéstrame todos los lotes disponibles",
            "sin tope de precio",
            "¿Y cuánto vale?",
        ],
    )
    def test_la_conversacion_corriente_no_señala_nada(self, frase):
        assert terminos_de_mencion(frase) == ()

    def test_el_nombre_del_inmueble_sí_sale(self):
        assert terminos_de_mencion("Háblame solo de la ferretería de La Reforma") == (
            "ferreteria",
            "reforma",
        )

    def test_el_barrio_cuenta_pero_el_municipio_no(self):
        """El municipio ya acota por su cuenta; el barrio no acotaba nada."""
        assert terminos_de_mencion("apartamentos en Laureles") == ("laureles",)
        assert terminos_de_mencion("apartamentos en Dosquebradas") == ()

    def test_el_codigo_de_la_ficha_sobrevive_entero(self):
        assert terminos_de_mencion("info del PROP-PER-003") == ("prop-per-003",)

    def test_los_numeros_no_señalan_inmuebles(self):
        """Un precio o un área no identifican una ficha: la filtran."""
        assert terminos_de_mencion("de 350.000.000 y 2462 m2") == ()
