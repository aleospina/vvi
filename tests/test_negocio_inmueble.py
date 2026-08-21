"""Tipo de negocio: venta, arriendo y permuta.

El riesgo de esta función no es que falte un filtro, es que **el precio
significa cosas distintas**: en venta es el valor total y en arriendo el canon
mensual, dos magnitudes con tres órdenes de diferencia. Todo lo que compare o
ordene importes tiene que hacerlo dentro de un mismo negocio, o el sistema
empieza a decir cosas falsas con total aplomo — un arriendo de 2.500.000
presentado como la ganga del catálogo, o un cliente solvente despachado por
«presupuesto bajo».

Eso es lo que cubre `TestNoSeMezclanLasEscalas`, que es el bloque que importa.
"""

from __future__ import annotations

import pytest

from app.models import NEGOCIO_POR_DEFECTO, Propiedad, TipoNegocio
from app.services import ingesta, matching_engine, portfolio
from app.services.nlu_engine import (
    extraer_presupuesto,
    extraer_slots,
    fuera_de_alcance,
    negocio_de,
)

VENTA = TipoNegocio.VENTA.value
ARRIENDO = TipoNegocio.ARRIENDO.value


@pytest.fixture()
def mixta(db):
    """Cartera con ventas y arriendos del mismo tipo, ciudad y zona."""
    db.add_all([
        Propiedad(id="NEG-V1", ciudad="Pereira", zona="Pinares", tipo="apartamento",
                  negocio=VENTA, precio=420_000_000, habitaciones=3, banos=2,
                  area_m2=95, estado="disponible", fuente="manual"),
        Propiedad(id="NEG-V2", ciudad="Pereira", zona="Álamos", tipo="apartamento",
                  negocio=VENTA, precio=310_000_000, habitaciones=2, banos=2,
                  area_m2=72, estado="disponible", fuente="manual"),
        Propiedad(id="NEG-A1", ciudad="Pereira", zona="Pinares", tipo="apartamento",
                  negocio=ARRIENDO, precio=2_500_000, habitaciones=3, banos=2,
                  area_m2=95, estado="disponible", fuente="manual"),
        Propiedad(id="NEG-A2", ciudad="Pereira", zona="Circunvalar", tipo="apartamento",
                  negocio=ARRIENDO, precio=1_200_000, habitaciones=2, banos=1,
                  area_m2=60, estado="disponible", fuente="manual"),
    ])
    db.commit()
    return db


# ═══════════════════ Lo que no puede fallar ═══════════════════


class TestNoSeMezclanLasEscalas:
    def test_el_piso_de_precio_se_mide_dentro_del_negocio(self, mixta):
        """El arriendo más barato es 1.200.000, no 310.000.000."""
        assert portfolio.precio_minimo(mixta, "Pereira", VENTA) == 310_000_000
        assert portfolio.precio_minimo(mixta, "Pereira", ARRIENDO) == 1_200_000

    def test_sin_negocio_el_piso_es_el_de_venta(self, mixta):
        """Compatibilidad: antes de esta columna todo era venta."""
        assert portfolio.precio_minimo(mixta, "Pereira") == 310_000_000

    def test_quien_arrienda_no_queda_fuera_de_alcance_por_pobre(self, mixta):
        """El fallo caro: 2.000.000 de canon contra el piso de venta.

        Sin acotar por negocio, la regla dura compara un canon mensual con el
        inmueble en venta más barato y despacha al cliente con un «tu
        presupuesto no alcanza» que es sencillamente falso.
        """
        slots = {"ciudad": "Pereira", "negocio": ARRIENDO, "presupuesto_max": 2_000_000}
        piso = portfolio.precio_minimo(mixta, "Pereira", ARRIENDO)
        assert fuera_de_alcance(slots, piso) is None

        # Y comprando con ese mismo tope sí está fuera de alcance, como antes.
        piso_venta = portfolio.precio_minimo(mixta, "Pereira", VENTA)
        assert fuera_de_alcance(
            {"ciudad": "Pereira", "presupuesto_max": 2_000_000}, piso_venta
        ) == "presupuesto_bajo:2000000"

    def test_el_emparejamiento_no_cruza_negocios(self, mixta):
        comprando = matching_engine.buscar(
            mixta, {"ciudad": "Pereira", "tipo": "apartamento", "negocio": VENTA}, limite=10
        )
        assert {m.propiedad.id for m in comprando} == {"NEG-V1", "NEG-V2"}

        arrendando = matching_engine.buscar(
            mixta, {"ciudad": "Pereira", "tipo": "apartamento", "negocio": ARRIENDO}, limite=10
        )
        assert {m.propiedad.id for m in arrendando} == {"NEG-A1", "NEG-A2"}

    def test_sin_negocio_declarado_el_bot_asume_venta(self, mixta):
        """Preserva el comportamiento anterior a la columna."""
        matches = matching_engine.buscar(
            mixta, {"ciudad": "Pereira", "tipo": "apartamento"}, limite=10
        )
        assert {m.propiedad.id for m in matches} == {"NEG-V1", "NEG-V2"}

    def test_el_conteo_cuenta_el_mismo_universo_que_muestra(self, mixta):
        """Decir «tengo 4» y mostrar 2 se lee como que el bot esconde inmuebles."""
        perfil = {"ciudad": "Pereira", "tipo": "apartamento", "negocio": ARRIENDO}
        _, total = matching_engine.conteo(mixta, perfil)
        assert total == 2

    def test_el_contexto_del_llm_es_del_mismo_negocio(self, mixta):
        texto = matching_engine.contexto_cartera(
            mixta, {"ciudad": "Pereira", "tipo": "apartamento", "negocio": ARRIENDO}
        )
        assert "NEG-A1" in texto and "NEG-V1" not in texto
        assert "/mes" in texto, "el canon tiene que verse como periódico"

    def test_los_similares_no_ofrecen_otro_negocio(self, mixta):
        arriendo = portfolio.obtener(mixta, "NEG-A1")
        vecinos = portfolio.similares(mixta, arriendo, limite=10)
        assert {p.id for p in vecinos} == {"NEG-A2"}

    def test_la_vitrina_filtra_por_negocio(self, mixta):
        ids = {p.id for p in portfolio.buscar_publicas(mixta, negocio=ARRIENDO)}
        assert ids == {"NEG-A1", "NEG-A2"}

    def test_el_rango_de_precio_admisible_depende_del_negocio(self):
        """900.000 es un canon normal y un precio de venta imposible."""
        assert ingesta.precio_razonable(900_000, ARRIENDO) is True
        assert ingesta.precio_razonable(900_000, VENTA) is False
        assert ingesta.precio_razonable(420_000_000, VENTA) is True
        assert ingesta.precio_razonable(420_000_000, ARRIENDO) is False


# ═══════════════════ Detección en lenguaje natural ═══════════════════


class TestDeteccion:
    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("busco apartamento en arriendo en Laureles", ARRIENDO),
            ("quiero alquilar una casa en Pereira", ARRIENDO),
            ("cuánto es el canon mensual", ARRIENDO),
            ("quiero comprar apartamento en Envigado", VENTA),
            ("tienen algo en venta en Pinares", VENTA),
            ("permuto mi apartamento por una casa", TipoNegocio.PERMUTA.value),
            ("busco apartamento de 3 alcobas", None),   # no lo dice
        ],
    )
    def test_negocio_de(self, texto, esperado):
        assert negocio_de(texto) == esperado

    def test_la_permuta_gana_a_las_otras_palabras(self):
        """'permuto mi apartamento' también contiene la idea de compra."""
        assert negocio_de("permuto mi apartamento, o lo vendo") == TipoNegocio.PERMUTA.value

    def test_el_slot_viaja_con_el_resto(self):
        slots = extraer_slots("busco apartamento en arriendo en Pereira")
        assert slots["negocio"] == ARRIENDO
        assert slots["tipo"] == "apartamento"
        assert slots["ciudad"] == "Pereira"


class TestEscalaDelPresupuesto:
    """Los mismos dígitos valen distinto según el negocio."""

    def test_un_canon_no_se_descarta_por_pequeno(self):
        """Comprando, 900.000 es ruido; arrendando, es el canon."""
        assert extraer_presupuesto("hasta 900.000", negocio=ARRIENDO) == (None, 900_000)
        assert extraer_presupuesto("hasta 900.000", negocio=VENTA) == (None, None)

    def test_entero_suelto_de_arriendo(self):
        assert extraer_presupuesto("2500000 mensuales", negocio=ARRIENDO) == (None, 2_500_000)

    def test_millones_explicitos_valen_igual_en_los_dos(self):
        """'2 millones' son dos millones se compre o se arriende."""
        assert extraer_presupuesto("hasta 2 millones", negocio=ARRIENDO) == (None, 2_000_000)
        assert extraer_presupuesto("hasta 2 millones", negocio=VENTA) == (None, 2_000_000)

    def test_la_compra_sigue_leyendose_como_antes(self):
        assert extraer_presupuesto("hasta 450 millones") == (None, 450_000_000)
        assert extraer_presupuesto("entre 300 y 450 millones") == (300_000_000, 450_000_000)

    def test_el_negocio_del_turno_previo_fija_la_escala(self):
        """Quien ya dijo «arriendo» y ahora escribe «hasta 900.000» pone un canon."""
        slots = extraer_slots("hasta 900.000", negocio_previo=ARRIENDO)
        assert slots.get("presupuesto_max") == 900_000
        # Sin ese contexto, el mismo texto no da un presupuesto de compra creíble.
        assert extraer_slots("hasta 900.000").get("presupuesto_max") is None


# ═══════════════════ Entrada de inventario ═══════════════════


class TestNormalizacion:
    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("arriendo", ARRIENDO),
            ("Se arrienda apartamento en Pinares", ARRIENDO),
            ("ALQUILER casa campestre", ARRIENDO),
            ("Vendo casa en Laureles", VENTA),
            ("permuta por finca", TipoNegocio.PERMUTA.value),
            ("", VENTA),               # sin dato: venta, el negocio central
            ("cualquier cosa", VENTA),
        ],
    )
    def test_normalizar_negocio(self, texto, esperado):
        assert ingesta.normalizar_negocio(texto) == esperado

    def test_un_arriendo_entra_con_su_canon(self, db):
        """Con el rango de venta, 1.800.000 se rechazaría como fuera de rango."""
        pub = ingesta.Publicacion(
            fuente="captacion_propietario", externo_id="neg-arr-1",
            ciudad="Pereira", zona="Pinares", tipo="apartamento",
            negocio=ARRIENDO, precio=1_800_000,
            mandato=True, mandato_evidencia="prueba",
        )
        assert ingesta.validar(pub) is None
        p = ingesta.ingerir_una(db, pub, actor="test")
        assert p.negocio == ARRIENDO
        assert p.precio == 1_800_000

    def test_un_precio_de_venta_en_un_arriendo_se_rechaza(self):
        """420 millones al mes es un error de captura, no una oferta."""
        pub = ingesta.Publicacion(
            fuente="captacion_propietario", externo_id="neg-arr-2",
            ciudad="Pereira", tipo="apartamento",
            negocio=ARRIENDO, precio=420_000_000,
            mandato=True, mandato_evidencia="prueba",
        )
        assert "fuera de rango" in (ingesta.validar(pub) or "")


class TestUrlPublica:
    def test_el_negocio_va_en_la_url(self, mixta):
        assert portfolio.slug_de(portfolio.obtener(mixta, "NEG-A1")) == (
            "apartamento-arriendo-pinares-pereira"
        )
        assert portfolio.slug_de(portfolio.obtener(mixta, "NEG-V1")) == (
            "apartamento-venta-pinares-pereira"
        )

    def test_dos_negocios_en_la_misma_zona_no_colisionan(self, mixta):
        """Mismo tipo, misma zona: sin el negocio, la URL sería la misma."""
        a = portfolio.ruta_publica(portfolio.obtener(mixta, "NEG-A1"))
        v = portfolio.ruta_publica(portfolio.obtener(mixta, "NEG-V1"))
        assert a != v

    def test_una_propiedad_sin_negocio_cae_en_venta(self, db):
        """Filas anteriores a la columna: la URL sigue siendo válida."""
        p = Propiedad(id="NEG-VIEJA", ciudad="Pereira", zona="Pinares",
                      tipo="casa", precio=300_000_000, negocio=None)
        assert NEGOCIO_POR_DEFECTO in portfolio.slug_de(p)
