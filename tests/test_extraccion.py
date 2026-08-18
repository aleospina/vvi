"""Extracción de avisos en texto libre con el LLM (RF-10).

El LLM se simula: lo que se prueba aquí es el contrato entre lo que devuelve el
extractor y lo que el sistema acepta en cartera. La garantía central es que un
aviso incompleto se descarta en vez de completarse con datos inventados.
"""

from __future__ import annotations

import pytest

from app.models import EstadoPropiedad, Propiedad
from app.services import ingesta
from app.services.ingesta import ExtraccionFallida


def extraccion(**cambios) -> dict:
    """Respuesta típica del extractor para un aviso limpio."""
    base = {
        "ciudad": "Pereira", "zona": "Pinares", "tipo": "apartamento",
        "precio": 420_000_000, "habitaciones": 3, "banos": 2, "area_m2": 95.0,
        "descripcion": "Tercer piso con ascensor", "propietario": "Luis",
        "telefono": "3105557788", "confianza": "alta", "faltantes": [],
    }
    base.update(cambios)
    return base


@pytest.fixture()
def llm(monkeypatch):
    """Sustituye el cliente LLM por uno controlado por el test."""
    def instalar(respuesta, *, disponible=True):
        monkeypatch.setattr(ingesta.cliente.__class__, "disponible", property(lambda _: disponible))
        if isinstance(respuesta, Exception):
            monkeypatch.setattr(
                ingesta.cliente, "extraer_inmueble",
                lambda _t: (_ for _ in ()).throw(respuesta),
            )
        else:
            monkeypatch.setattr(ingesta.cliente, "extraer_inmueble", lambda _t: respuesta)
    return instalar


AVISO = "Vendo apartamento en Pinares, Pereira. 3 alcobas, 95 mts2. $420 millones."


class TestExtraccion:
    def test_aviso_limpio_produce_publicacion(self, llm):
        llm(extraccion())
        pub, crudo = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        assert pub.ciudad == "Pereira"
        assert pub.tipo == "apartamento"
        assert pub.precio == 420_000_000
        assert pub.propietario_telefono == "3105557788"
        assert crudo["confianza"] == "alta"

    def test_la_zona_resuelve_la_ciudad_cuando_falta(self, llm):
        """'Lote campestre en Cerritos' no nombra Pereira, pero es de Pereira."""
        llm(extraccion(ciudad=None, zona="Cerritos", tipo="lote"))
        pub, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        assert ingesta.normalizar_ciudad(pub.ciudad) == "Pereira"

    def test_sin_llm_no_se_inventa_nada(self, llm):
        llm(extraccion(), disponible=False)
        with pytest.raises(ExtraccionFallida, match="No hay LLM"):
            ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")

    def test_aviso_vacio_se_rechaza(self, llm):
        llm(extraccion())
        with pytest.raises(ExtraccionFallida):
            ingesta.publicacion_desde_texto("   ", mandato_evidencia="prueba")

    def test_fallo_del_extractor_no_se_propaga_crudo(self, llm):
        llm(ConnectionError("timeout"))
        with pytest.raises(ExtraccionFallida, match="no respondió"):
            ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")

    def test_el_mismo_aviso_no_se_duplica(self, llm):
        llm(extraccion())
        a, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        b, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        assert a.externo_id == b.externo_id


class TestNoInventar:
    """Un aviso al que le falta lo esencial se descarta, no se completa."""

    @pytest.mark.parametrize(
        ("cambios", "motivo"),
        [
            ({"precio": None}, "precio"),          # proyecto sobre planos, solo cuota inicial
            ({"tipo": None}, "tipo"),              # el aviso no era de un inmueble
            ({"ciudad": None, "zona": None}, "ciudad"),
            ({"ciudad": "Bogotá", "zona": None}, "ciudad"),
        ],
    )
    def test_aviso_incompleto_no_entra_a_cartera(self, db, llm, cambios, motivo):
        llm(extraccion(**cambios))
        pub, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        with pytest.raises(ValueError, match=motivo):
            ingesta.ingerir_una(db, pub)
        assert db.query(Propiedad).count() == 6  # la cartera no creció

    def test_la_administracion_no_se_toma_como_precio(self, db, llm):
        """Regresión del prompt: 95.000 de administración no es el precio."""
        llm(extraccion(precio=95_000))
        pub, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="prueba")
        with pytest.raises(ValueError, match="precio fuera de rango"):
            ingesta.ingerir_una(db, pub)


class TestFlujoCompleto:
    def test_el_aviso_extraido_queda_pendiente_de_revision(self, db, llm):
        llm(extraccion())
        pub, _ = ingesta.publicacion_desde_texto(
            AVISO, mandato_evidencia="Aviso cargado por operador; declara mandato"
        )
        p = ingesta.ingerir_una(db, pub, actor="operador")
        assert p.estado == EstadoPropiedad.PENDIENTE.value
        assert p in ingesta.pendientes(db)

    def test_sin_mandato_el_llm_no_alcanza(self, db, llm):
        """Extraer bien no autoriza: el mandato es un acto humano, no una deducción."""
        llm(extraccion())
        pub, _ = ingesta.publicacion_desde_texto(AVISO, mandato_evidencia="   ")
        with pytest.raises(ingesta.MandatoAusente):
            ingesta.ingerir_una(db, pub)
