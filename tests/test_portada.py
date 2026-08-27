"""Portada pública (`/`).

Antes `/` redirigía al panel, así que lo primero que veía cualquiera que
escribiera la dirección era un formulario de contraseña. Lo que se prueba aquí
es que eso no vuelva:

  · `/` responde 200 sin cookie y sin redirigir a ningún sitio.
  · La compuerta del catálogo sigue puesta: los destacados y los conteos de la
    portada salen de `portfolio.buscar_publicas`, así que un inmueble sin
    mandato —demo o referencia— o sin aprobar no puede asomarse. Es la misma
    regla de la vitrina y por las mismas razones (ADR-01, RF-17).
  · No hay PII: el nombre y el teléfono del propietario no llegan a la página.
  · Sin cartera, la portada sigue existiendo. Un 404 o una página en blanco en
    la puerta del sitio se lee como que la inmobiliaria cerró.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import EstadoPropiedad, FuentePropiedad, Propiedad

#: (id, ciudad, zona, tipo, precio, estado, fuente)
CARTERA = [
    ("POR-01", "Pereira", "Pinares", "casa", 430_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.MANUAL),
    ("POR-02", "Medellín", "Zúñiga, Envigado", "apartamento", 690_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.CAPTACION_PROPIETARIO),
    # No publicables, cada uno por su motivo.
    ("POR-03", "Pereira", "Cerritos", "casa", 1_200_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.DEMO),        # inventada
    ("POR-04", "Medellín", "Sabaneta", "casa", 610_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.REFERENCIA),  # aviso ajeno
    ("POR-05", "Pereira", "Álamos", "casa", 390_000_000,
     EstadoPropiedad.PENDIENTE, FuentePropiedad.FEED_ALIADO),  # sin validar
]

PUBLICABLES = {"POR-01", "POR-02"}
OCULTOS = {"POR-03", "POR-04", "POR-05"}

PROPIETARIO = "Marta Propietaria"
TELEFONO = "+573001112233"


def _limpiar(db) -> None:
    db.query(Propiedad).filter(Propiedad.id.like("POR-%")).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def cartera():
    inicializar(seed=False)
    db = SessionLocal()
    try:
        _limpiar(db)
        for pid, ciudad, zona, tipo, precio, estado, fuente in CARTERA:
            db.add(Propiedad(
                id=pid, ciudad=ciudad, zona=zona, tipo=tipo, precio=precio,
                habitaciones=3, banos=2, area_m2=100,
                descripcion=f"Inmueble de prueba en {zona}.",
                estado=estado.value, fuente=fuente.value,
                propietario=PROPIETARIO, propietario_telefono=TELEFONO,
            ))
        db.commit()
        yield db
    finally:
        _limpiar(db)
        db.close()


@pytest.fixture()
def web(monkeypatch):
    """Cliente sin sesión: es exactamente lo que tiene un visitante cualquiera."""
    monkeypatch.setattr(settings, "catalogo_publico", True)
    monkeypatch.setattr(settings, "catalogo_muestra_demo", False)
    from app.main import app

    with TestClient(app, follow_redirects=False) as cli:
        yield cli


# ═══════════════════════ La puerta está abierta ═══════════════════════


class TestSinContrasena:
    def test_la_raiz_responde_sin_sesion(self, web, cartera):
        r = web.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_la_raiz_no_redirige_al_panel(self, web, cartera):
        """La regresión concreta: `/` mandaba a `/dashboard`, que pide clave."""
        r = web.get("/")
        assert r.status_code not in (301, 302, 303, 307, 308)
        assert "location" not in {k.lower() for k in r.headers}

    def test_el_panel_sigue_pidiendo_clave(self, web, cartera):
        """Abrir la portada no puede haber abierto nada más."""
        r = web.get("/dashboard")
        assert r.status_code in (303, 401, 403)

    def test_sin_cartera_la_portada_sigue_en_pie(self, web, monkeypatch):
        monkeypatch.setattr(settings, "catalogo_publico", False)
        r = web.get("/")
        assert r.status_code == 200
        # Sin vitrina no se ofrece un enlace a un catálogo que responde 404.
        assert 'href="/inmuebles"' not in r.text


# ═══════════════════════════ La compuerta ═══════════════════════════


class TestCompuerta:
    """La portada enseña inventario, así que hereda las reglas de la vitrina."""

    @pytest.mark.parametrize("pid", sorted(PUBLICABLES))
    def test_lo_publicable_puede_asomarse(self, web, cartera, pid):
        assert pid in web.get("/").text

    @pytest.mark.parametrize("pid", sorted(OCULTOS))
    def test_lo_no_publicable_no_asoma(self, web, cartera, pid):
        assert pid not in web.get("/").text

    def test_el_conteo_solo_cuenta_lo_publicable(self, web, cartera):
        """El número de la portada es una promesa: tiene que dar lo mismo que la vitrina."""
        assert "Ver los 2 inmuebles" in web.get("/").text

    def test_las_zonas_son_las_que_tienen_inventario(self, web, cartera):
        texto = web.get("/").text
        assert "Envigado" in texto            # POR-02, publicable
        assert "municipio=Cerritos" not in texto   # POR-03, inventada
        assert "municipio=Sabaneta" not in texto   # POR-04, aviso ajeno

    def test_la_demo_solo_entra_con_la_puerta_de_demo_abierta(
        self, web, cartera, monkeypatch
    ):
        monkeypatch.setattr(settings, "catalogo_muestra_demo", True)
        assert "POR-03" in web.get("/").text


# ═════════════════════════════ Sin PII ═════════════════════════════


class TestSinDatosPersonales:
    def test_no_aparece_el_propietario(self, web, cartera):
        texto = web.get("/").text
        assert PROPIETARIO not in texto
        assert TELEFONO not in texto
        assert "3001112233" not in texto
