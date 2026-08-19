"""Municipio contra plaza de cobertura.

La cartera guarda en `Propiedad.ciudad` el municipio real —Sabaneta es
Sabaneta— y no la plaza. Lo que se prueba aquí es que esa distinción no le
quita inventario al comprador: quien pregunta por "Medellín" tiene que seguir
viendo Envigado y Sabaneta, porque para él son el mismo mercado.

Es la garantía que hace segura la lista larga de municipios en el formulario.
"""

from __future__ import annotations

import pytest

from app.models import EstadoPropiedad, Propiedad
from app.services import geografia, matching_engine


class TestListas:
    def test_estan_los_dos_departamentos_completos(self):
        assert len(geografia.MUNICIPIOS_ANTIOQUIA) == 125
        assert len(geografia.MUNICIPIOS_RISARALDA) == 14
        assert len(geografia.MUNICIPIOS) == 139

    def test_no_hay_repetidos(self):
        assert len(set(geografia.MUNICIPIOS)) == len(geografia.MUNICIPIOS)

    def test_las_areas_metropolitanas_van_primero(self):
        """Un desplegable que arranca en «Abejorral» hace teclear de más en el
        95% de los casos: la cartera sale del Valle de Aburrá y de Pereira."""
        assert geografia.MUNICIPIOS[:3] == ("Medellín", "Bello", "Itagüí")
        assert "Pereira" in geografia.MUNICIPIOS[:14]

    def test_los_que_pidio_el_operador_estan_todos(self):
        for m in ("Medellín", "Bello", "Itagüí", "Caldas", "Sabaneta",
                  "Pereira", "Dosquebradas", "Santa Rosa de Cabal", "La Virginia"):
            assert m in geografia.MUNICIPIOS, m


class TestNormalizacion:
    @pytest.mark.parametrize(
        ("escrito", "esperado"),
        [
            ("Medellin", "Medellín"),
            ("SABANETA", "Sabaneta"),
            ("itagui", "Itagüí"),
            ("Envigado, Antioquia", "Envigado"),
            ("Santa Rosa", "Santa Rosa de Cabal"),   # la de Cabal: es donde hay cartera
            ("Virginia", "La Virginia"),
            ("Urrao", "Urrao"),
            ("quinchia", "Quinchía"),
        ],
    )
    def test_como_lo_escribe_la_gente(self, escrito, esperado):
        assert geografia.normalizar_municipio(escrito) == esperado

    @pytest.mark.parametrize("fuera", ["Bogotá", "Cali", "Manizales", "", "   ", "Narnia"])
    def test_lo_de_fuera_no_entra(self, fuera):
        assert geografia.normalizar_municipio(fuera) is None

    def test_el_homonimo_de_otro_departamento_no_cuela(self):
        """Armenia existe en Antioquia, pero «Armenia, Quindío» no es esa."""
        assert geografia.normalizar_municipio("Armenia") == "Armenia"
        assert geografia.normalizar_municipio("Armenia, Quindío") is None

    def test_un_corregimiento_no_es_municipio(self):
        """Cerritos es de Pereira y va en la zona, no en el municipio."""
        assert geografia.normalizar_municipio("Cerritos") is None


class TestPlaza:
    @pytest.mark.parametrize(
        ("municipio", "plaza"),
        [
            ("Sabaneta", "Medellín"), ("Envigado", "Medellín"), ("Bello", "Medellín"),
            ("Medellín", "Medellín"), ("Dosquebradas", "Pereira"),
            ("Santa Rosa de Cabal", "Pereira"), ("La Virginia", "Pereira"),
            ("Pereira", "Pereira"),
        ],
    )
    def test_el_area_metropolitana_es_una_sola_plaza(self, municipio, plaza):
        assert geografia.plaza_de(municipio) == plaza

    @pytest.mark.parametrize("lejano", ["Urrao", "Turbo", "Quinchía", "Rionegro", "La Ceja"])
    def test_fuera_del_area_no_hay_plaza(self, lejano):
        """Se puede cargar y se ve en la cartera; el bot no lo ofrece, porque
        nadie llega al canal conversacional buscando en Urrao."""
        assert geografia.normalizar_municipio(lejano) is not None
        assert geografia.plaza_de(lejano) is None


def _inmueble(db, pid, ciudad, zona="Centro", precio=300_000_000):
    db.add(Propiedad(
        id=pid, ciudad=ciudad, zona=zona, tipo="apartamento", precio=precio,
        habitaciones=3, banos=2, area_m2=90, estado=EstadoPropiedad.DISPONIBLE.value,
        descripcion="Prueba de plaza",
    ))
    db.commit()


class TestNoSePierdeInventario:
    """La regresión que haría peligroso guardar el municipio en `ciudad`."""

    def test_quien_busca_en_medellin_ve_el_area_metropolitana(self, db):
        _inmueble(db, "PLZ-01", "Sabaneta")
        _inmueble(db, "PLZ-02", "Envigado")
        ids = {m.propiedad.id
               for m in matching_engine.buscar(db, {"ciudad": "Medellín", "tipo": "apartamento"},
                                               limite=20)}
        assert {"PLZ-01", "PLZ-02"} <= ids

    def test_quien_busca_en_pereira_no_ve_medellin(self, db):
        _inmueble(db, "PLZ-03", "Sabaneta")
        ids = {m.propiedad.id
               for m in matching_engine.buscar(db, {"ciudad": "Pereira", "tipo": "apartamento"},
                                               limite=20)}
        assert "PLZ-03" not in ids

    def test_pedir_un_municipio_concreto_sigue_acotando(self, db):
        """Quien dice «Sabaneta» no quiere Bello, aunque compartan plaza."""
        _inmueble(db, "PLZ-04", "Sabaneta")
        _inmueble(db, "PLZ-05", "Bello")
        perfil = {"ciudad": "Medellín", "tipo": "apartamento", "municipio": "Sabaneta"}
        ids = {m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=20)}
        assert "PLZ-04" in ids and "PLZ-05" not in ids

    def test_fuera_de_plaza_no_se_ofrece(self, db):
        _inmueble(db, "PLZ-06", "Urrao")
        for plaza in ("Medellín", "Pereira"):
            ids = {m.propiedad.id
                   for m in matching_engine.buscar(db, {"ciudad": plaza, "tipo": "apartamento"},
                                                   limite=20)}
            assert "PLZ-06" not in ids, plaza

    def test_el_conteo_cuadra_con_lo_que_se_muestra(self, db):
        """Si el total contara otro universo, el aviso de «filtro activo» del
        bot diría que esconde inmuebles que en realidad no existen."""
        _inmueble(db, "PLZ-07", "Itagüí")
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        caben, total = matching_engine.conteo(db, perfil)
        assert total == len(matching_engine.buscar(db, perfil, limite=99))
        assert caben <= total
