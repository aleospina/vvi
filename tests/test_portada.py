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
from app.models import EstadoPropiedad, FotoPropiedad, FuentePropiedad, Propiedad

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
    db.query(FotoPropiedad).filter(
        FotoPropiedad.propiedad_id.like("POR-%")
    ).delete(synchronize_session=False)
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


# ══════════════════ Las láminas salen de la cartera ══════════════════


@pytest.fixture()
def con_fotos(cartera):
    """La casa es más barata que el apartamento, y los dos tienen foto.

    Es el caso que importa: si la elección se hiciera solo por importe, la
    portada la presidiría el apartamento. Reproduce la cartera real, donde el
    inmueble más caro es un lote.
    """
    cartera.add_all([
        FotoPropiedad(propiedad_id="POR-01", archivo="por-01-casa.jpg", orden=0),
        FotoPropiedad(propiedad_id="POR-01", archivo="por-01-segunda.jpg", orden=1),
        FotoPropiedad(propiedad_id="POR-02", archivo="por-02-apto.jpg", orden=0),
    ])
    cartera.commit()
    return cartera


class TestLaminas:
    def test_el_titular_es_una_foto_de_la_cartera(self, web, con_fotos):
        texto = web.get("/").text
        assert "/static/fotos/por-01-casa.jpg" in texto
        # Y ya no el dibujo: es lo que se pidió quitar del sitio.
        assert "portada-ciudad.svg" not in texto

    def test_la_casa_gana_al_inmueble_mas_caro(self, web, con_fotos):
        """POR-02 vale 690 y POR-01 vale 430, pero la casa es la que preside.

        Ordenar la lista entera por precio parece equivalente y no lo es: en la
        cartera real el inmueble más caro es un lote, y el titular del sitio
        habría acabado siendo la fotografía de un terreno.
        """
        texto = web.get("/").text
        cima = texto.index('class="cima-lamina')
        recorte = texto[cima:cima + 260]
        assert "por-01-casa.jpg" in recorte
        assert "por-02-apto.jpg" not in recorte

    def test_las_dos_laminas_grandes_son_de_inmuebles_distintos(self, web, con_fotos):
        """Repetir la misma foto arriba y abajo se lee como que solo hay una."""
        texto = web.get("/").text
        assert "/static/fotos/por-02-apto.jpg" in texto

    def test_el_titular_usa_la_version_grande_y_no_la_miniatura(self, web, con_fotos):
        """La miniatura son 640px: estirada a pantalla completa se ve el grano."""
        texto = web.get("/").text
        cima = texto.index('class="cima-lamina')
        assert "por-01-casa-min.jpg" not in texto[cima:cima + 260]

    def test_sin_fotos_la_portada_cae_al_dibujo(self, web, cartera):
        """Una instalación recién montada no puede quedarse con un hueco gris."""
        texto = web.get("/").text
        assert "portada-ciudad.svg" in texto

    def test_la_zona_se_ilustra_con_un_inmueble_suyo(self, web, con_fotos):
        """El mosaico deja de ser decorativo: enseña el municipio que rotula."""
        texto = web.get("/").text
        zona = texto.index('class="zona"')
        assert "/static/fotos/" in texto[zona:zona + 400]
