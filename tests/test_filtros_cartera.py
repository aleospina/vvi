"""Filtros de la cartera por tipo y municipio (dashboard del operador).

El municipio no es una columna: `ciudad` solo distingue las dos plazas de
cobertura y el municipio real vive dentro de `zona` con la convención
"Barrio, Municipio". Lo que se prueba aquí es esa deducción, que es donde está
el riesgo — no el `WHERE` por tipo.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import FuentePropiedad, Propiedad
from app.services import portfolio

#: (id, ciudad, zona, tipo, precio) — la convención real de la cartera.
CARTERA = [
    ("FIL-01", "Pereira", "Pinares", "casa", 420_000_000),
    ("FIL-02", "Pereira", "Frailes, Dosquebradas", "lote", 265_000_000),
    ("FIL-03", "Pereira", "Santa Mónica, Dosquebradas", "apartamento", 310_000_000),
    ("FIL-04", "Medellín", "Zúñiga, Envigado", "apartamento", 690_000_000),
    ("FIL-05", "Medellín", "Ditaires, Itagüí", "casa", 480_000_000),
    ("FIL-06", "Medellín", "La Estrella", "lote", 420_000_000),
    ("FIL-07", "Medellín", "El Poblado", "apartamento", 950_000_000),
]


@pytest.fixture()
def cartera():
    inicializar(seed=False)
    db = SessionLocal()
    try:
        db.query(Propiedad).filter(Propiedad.id.like("FIL-%")).delete(synchronize_session=False)
        for pid, ciudad, zona, tipo, precio in CARTERA:
            db.add(Propiedad(
                id=pid, ciudad=ciudad, zona=zona, tipo=tipo, precio=precio,
                habitaciones=3, banos=2, area_m2=100, descripcion="Prueba de filtros",
                estado="disponible", fuente=FuentePropiedad.MANUAL.value,
            ))
        db.commit()
        yield db
    finally:
        db.query(Propiedad).filter(Propiedad.id.like("FIL-%")).delete(synchronize_session=False)
        db.commit()
        db.close()


class TestMunicipioDeducido:
    @pytest.mark.parametrize(
        "zona,ciudad,esperado",
        [
            ("Frailes, Dosquebradas", "Pereira", "Dosquebradas"),
            ("Zúñiga, Envigado", "Medellín", "Envigado"),
            ("Ditaires, Itagüí", "Medellín", "Itagüí"),
            ("Pinares", "Pereira", "Pereira"),          # barrio de la propia ciudad
            ("El Poblado", "Medellín", "Medellín"),
            ("La Estrella", "Medellín", "La Estrella"),  # municipio escrito sin coma
            ("", "Pereira", "Pereira"),                  # zona vacía: no revienta
        ],
    )
    def test_deduccion(self, zona, ciudad, esperado):
        p = Propiedad(id="X", ciudad=ciudad, zona=zona, tipo="casa", precio=1)
        assert portfolio.municipio_de(p) == esperado


class TestListar:
    def test_filtra_por_tipo(self, cartera):
        ids = {p.id for p in portfolio.listar(cartera, tipo="lote") if p.id.startswith("FIL-")}
        assert ids == {"FIL-02", "FIL-06"}

    def test_filtra_por_municipio_del_area(self, cartera):
        """Dosquebradas no es una ciudad en la base: sale de la zona."""
        ids = {p.id for p in portfolio.listar(cartera, municipio="Dosquebradas")}
        assert ids == {"FIL-02", "FIL-03"}

    def test_la_cabecera_no_arrastra_su_area(self, cartera):
        """Pedir Pereira no debe traer Dosquebradas: son municipios distintos."""
        ids = {p.id for p in portfolio.listar(cartera, municipio="Pereira") if p.id.startswith("FIL-")}
        assert ids == {"FIL-01"}

    def test_tolera_tildes_y_mayusculas(self, cartera):
        for escrito in ("Itagüí", "itagui", "ITAGUI"):
            ids = {p.id for p in portfolio.listar(cartera, municipio=escrito)}
            assert ids == {"FIL-05"}, escrito

    def test_los_filtros_se_combinan(self, cartera):
        ids = {p.id for p in portfolio.listar(cartera, tipo="apartamento", municipio="Dosquebradas")}
        assert ids == {"FIL-03"}


class TestConteo:
    def test_solo_aparecen_municipios_con_inventario(self, cartera):
        filas = portfolio.conteo_por_municipio(cartera)
        munis = {m for _, m, _ in filas}
        assert {"Dosquebradas", "Envigado", "Itagüí", "La Estrella"} <= munis
        assert "Sabaneta" not in munis, "sin inventario, no debe ofrecerse la pestaña"

    def test_la_cabecera_va_primero_en_su_ciudad(self, cartera):
        filas = [f for f in portfolio.conteo_por_municipio(cartera) if f[0] == "Pereira"]
        assert filas[0][1] == "Pereira"


class TestConteoCruzado:
    """Cada fila de pestañas cuenta con el filtro de la *otra* aplicado.

    Lo que se protege es que una opción nunca desaparezca por quedar en cero:
    una pestaña que se esfuma deja al operador sin saber si el filtro se aplicó
    o si esa opción no existía, y sin forma de volver salvo editando la URL.
    """

    def test_los_municipios_no_desaparecen_al_filtrar_por_tipo(self, cartera):
        todos = {m for _, m, _ in portfolio.conteo_por_municipio(cartera)}
        for tipo in ("casa", "apartamento", "lote"):
            munis = {m for _, m, _ in portfolio.conteo_por_municipio(cartera, tipo=tipo)}
            assert munis == todos, f"{tipo} se llevó municipios de la lista"

    def test_el_municipio_sin_ese_tipo_queda_en_cero(self, cartera):
        """Envigado tiene apartamento pero no lote: la pestaña dice 0, no se va."""
        por_muni = {m: n for _, m, n in portfolio.conteo_por_municipio(cartera, tipo="lote")}
        assert por_muni["Dosquebradas"] == 1      # FIL-02
        assert por_muni["Envigado"] == 0

    def test_el_conteo_por_tipo_siempre_trae_los_tres(self, cartera):
        conteo = portfolio.conteo_por_tipo(cartera, municipio="Dosquebradas")
        assert {"casa", "apartamento", "lote"} <= set(conteo)
        assert conteo["casa"] == 0, "el tipo sin inventario conserva su pestaña"

    def test_las_dos_filas_cuadran(self, cartera):
        """Sumar los municipios del tipo activo tiene que dar el mismo total
        que su pestaña en la fila de arriba; si no, uno de los dos miente."""
        for tipo in ("casa", "apartamento", "lote"):
            suma = sum(n for _, _, n in portfolio.conteo_por_municipio(cartera, tipo=tipo))
            assert suma == portfolio.conteo_por_tipo(cartera)[tipo], tipo


class TestVista:
    @pytest.fixture()
    def panel(self):
        from app.main import app

        cli = TestClient(app, follow_redirects=False)
        cli.__enter__()
        r = cli.post("/dashboard/login",
                     data={"usuario": settings.dashboard_user,
                           "clave": settings.dashboard_password})
        assert r.status_code == 303
        yield cli
        cli.__exit__(None, None, None)

    def test_la_cartera_ya_no_lista_inventario(self, panel, cartera):
        """Cartera alimenta el inventario; verlo es tarea de la vitrina.

        Los FIL- están todos `disponible`, así que su sitio es /inmuebles. Que
        no aparezcan aquí es la separación buscada, no una regresión.
        """
        r = panel.get("/dashboard/propiedades")
        assert r.status_code == 200
        assert "FIL-01" not in r.text and "FIL-02" not in r.text
        # Y tiene que quedar el camino hacia donde sí se ven.
        assert 'href="/inmuebles"' in r.text

    def test_la_cartera_resume_el_inventario_en_cifras(self, panel, cartera):
        """Sin rejilla, el operador todavía necesita saber qué tiene."""
        r = panel.get("/dashboard/propiedades")
        assert "en la vitrina" in r.text
        assert "casas" in r.text and "lotes" in r.text

    def test_lo_retirado_sigue_siendo_alcanzable(self, panel, cartera):
        """Un inactivo no sale en la vitrina: si tampoco saliera aquí, se pierde."""
        p = cartera.query(Propiedad).filter(Propiedad.id == "FIL-01").one()
        p.estado = "inactiva"
        cartera.commit()
        try:
            r = panel.get("/dashboard/propiedades")
            assert "Fuera de la vitrina" in r.text
            assert "FIL-01" in r.text
        finally:
            p.estado = "disponible"
            cartera.commit()

    def test_el_invitado_no_se_queda_sin_donde_mirar(self, panel, cartera, monkeypatch):
        """Al invitado se le movió el inventario a la vitrina; debe llegar a ella.

        Su cuenta existe para consultar inmuebles y comentarlos. Si la Cartera
        deja de listarlos y la vitrina no lo reconoce, se queda sin producto.
        """
        monkeypatch.setattr(settings, "invitado_user", "invitado")
        monkeypatch.setattr(settings, "invitado_password", "invitado")
        monkeypatch.setattr(settings, "catalogo_publico", True)
        from app.main import app

        with TestClient(app, follow_redirects=False) as cli:
            r = cli.post("/dashboard/login", data={"usuario": "invitado", "clave": "invitado"})
            assert r.status_code == 303
            # El ingreso lo deja directamente donde puede trabajar.
            assert r.headers["location"] == "/inmuebles"

            vitrina = cli.get("/inmuebles")
            assert vitrina.status_code == 200
            assert "FIL-01" in vitrina.text
            # No se le ofrece «volver al panel»: esa ruta lo devolvería aquí y
            # el botón sería un bucle. Sí se le ofrece salir.
            assert "Volver al panel" not in vitrina.text
            assert "Cerrar sesión" in vitrina.text
