"""Catálogo público de inmuebles (/inmuebles).

Lo que de verdad se prueba aquí no son los filtros: es la compuerta. La vitrina
es la única superficie sin sesión del sistema, así que un inmueble sin mandato
—demo o referencia— o que todavía no aprobó un humano no puede aparecer ni en
el listado, ni en la ficha directa, ni en los similares, ni en los conteos. Ese
es el bloque `TestCompuerta`, y es el que no puede ponerse en amarillo.

El resto cubre lo que rompería la vitrina de forma silenciosa: filtros que se
pierden al paginar, rangos de precio al revés, y PII del propietario filtrada
en la ficha.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import EstadoPropiedad, FuentePropiedad, Propiedad
from app.services import portfolio

#: (id, ciudad, zona, tipo, hab, precio, estado, fuente)
CARTERA = [
    # Publicables: disponibles y con mandato.
    ("CAT-01", "Pereira", "Pinares", "casa", 3, 420_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.MANUAL),
    ("CAT-02", "Pereira", "Frailes, Dosquebradas", "apartamento", 2, 265_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.MANUAL),
    ("CAT-03", "Medellín", "Zúñiga, Envigado", "apartamento", 4, 690_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.CAPTACION_PROPIETARIO),
    # NO publicables, cada uno por una razón distinta.
    ("CAT-04", "Pereira", "Álamos", "casa", 3, 390_000_000,
     EstadoPropiedad.PENDIENTE, FuentePropiedad.FEED_ALIADO),      # sin validar
    ("CAT-05", "Medellín", "Laureles", "apartamento", 3, 520_000_000,
     EstadoPropiedad.INACTIVA, FuentePropiedad.MANUAL),            # retirada
    ("CAT-06", "Medellín", "El Poblado", "apartamento", 3, 950_000_000,
     EstadoPropiedad.VENDIDA, FuentePropiedad.MANUAL),             # ya vendida
    ("CAT-07", "Pereira", "Cerritos", "casa", 5, 1_200_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.DEMO),            # inventada
    ("CAT-08", "Medellín", "Sabaneta", "casa", 3, 610_000_000,
     EstadoPropiedad.DISPONIBLE, FuentePropiedad.REFERENCIA),      # aviso ajeno
]

PUBLICABLES = {"CAT-01", "CAT-02", "CAT-03"}
OCULTOS = {"CAT-04", "CAT-05", "CAT-06", "CAT-07", "CAT-08"}


def _limpiar(db) -> None:
    """Borra lo que dejan estos tests, respetando el orden de las claves foráneas.

    El formulario de interés crea prospectos, y procesarlos genera
    emparejamientos que apuntan a estos inmuebles: borrar las propiedades sin
    quitar antes lo que las referencia revienta con FOREIGN KEY constraint.
    """
    from app.models import Emparejamiento, Prospecto, Solicitud

    db.query(Emparejamiento).filter(
        Emparejamiento.propiedad_id.like("CAT-%")
    ).delete(synchronize_session=False)
    db.query(Solicitud).filter(
        Solicitud.propiedad_id.like("CAT-%")
    ).delete(synchronize_session=False)
    # Los mensajes y consentimientos del prospecto caen por cascada.
    for p in db.query(Prospecto).filter(Prospecto.campana.like("inmueble:CAT-%")):
        db.delete(p)
    db.query(Propiedad).filter(Propiedad.id.like("CAT-%")).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def cartera():
    inicializar(seed=False)
    db = SessionLocal()
    try:
        _limpiar(db)
        for pid, ciudad, zona, tipo, hab, precio, estado, fuente in CARTERA:
            db.add(Propiedad(
                id=pid, ciudad=ciudad, zona=zona, tipo=tipo, precio=precio,
                habitaciones=hab, banos=2, area_m2=100,
                descripcion=f"Inmueble de prueba en {zona}.",
                estado=estado.value, fuente=fuente.value,
                propietario="Marta Propietaria",
                propietario_telefono="+573001112233",
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
    monkeypatch.setattr(settings, "catalogo_por_pagina", 12)
    from app.main import app

    with TestClient(app, follow_redirects=False) as cli:
        yield cli


def _ids_visibles(texto: str) -> set[str]:
    return {pid for pid, *_ in CARTERA if pid in texto}


# ═══════════════════════════ La compuerta ═══════════════════════════


class TestCompuerta:
    """Nada sin mandato o sin aprobar puede llegar a un desconocido."""

    @pytest.mark.parametrize("pid", sorted(PUBLICABLES))
    def test_lo_publicable_entra(self, cartera, pid):
        assert pid in {p.id for p in portfolio.publicables(cartera)}

    @pytest.mark.parametrize("pid", sorted(OCULTOS))
    def test_lo_no_publicable_no_entra(self, cartera, pid):
        assert pid not in {p.id for p in portfolio.publicables(cartera)}

    def test_el_listado_solo_muestra_lo_publicable(self, web, cartera):
        r = web.get("/inmuebles")
        assert r.status_code == 200
        assert _ids_visibles(r.text) == PUBLICABLES

    @pytest.mark.parametrize("pid", sorted(OCULTOS))
    def test_la_ficha_directa_responde_404(self, web, cartera, pid):
        """404 y no 403: un 403 confirmaría que el código existe."""
        assert web.get(f"/inmuebles/{pid}").status_code == 404
        assert web.get(f"/inmuebles/lo-que-sea/{pid}").status_code == 404

    def test_los_similares_tampoco_los_cuelan(self, cartera):
        base = portfolio.obtener(cartera, "CAT-01")
        vecinos = portfolio.similares(cartera, base, limite=20)
        assert {p.id for p in vecinos} & OCULTOS == set()

    def test_los_conteos_no_los_cuentan(self, cartera):
        tipos = portfolio.conteo_publico_por_tipo(cartera)
        # CAT-07 (demo) y CAT-08 (referencia) son casas disponibles: si el
        # conteo las sumara, la pestaña prometería inventario inexistente.
        assert tipos.get("casa") == 1
        munis = {m for _, m, _ in portfolio.conteo_publico_por_municipio(cartera)}
        assert "Cerritos" not in munis and "Sabaneta" not in munis

    def test_la_busqueda_por_texto_no_es_una_puerta_trasera(self, web, cartera):
        """Buscar el código exacto de un inmueble oculto no lo saca a la luz.

        Se comprueba que no haya enlace, no que no aparezca el texto: el
        buscador repite lo tecleado dentro del `value` del formulario, así que
        el código sí está en el HTML —y debe estar, o el campo se vaciaría—.
        """
        r = web.get("/inmuebles?q=CAT-07")
        assert "/CAT-07" not in r.text
        assert "0 inmuebles" in r.text

    def test_con_la_puerta_de_demo_abierta_entran_y_queda_noindex(
        self, web, cartera, monkeypatch
    ):
        monkeypatch.setattr(settings, "catalogo_muestra_demo", True)
        r = web.get("/inmuebles")
        assert {"CAT-07", "CAT-08"} <= _ids_visibles(r.text)
        assert 'name="robots" content="noindex' in r.text
        assert "Cartera de demostración" in r.text
        # Lo no aprobado o retirado sigue fuera: la puerta es solo para mandato.
        assert _ids_visibles(r.text) & {"CAT-04", "CAT-05", "CAT-06"} == set()

    def test_sin_noindex_cuando_la_cartera_es_real(self, web, cartera):
        assert "noindex" not in web.get("/inmuebles").text

    def test_el_catalogo_se_puede_apagar_entero(self, web, cartera, monkeypatch):
        monkeypatch.setattr(settings, "catalogo_publico", False)
        assert web.get("/inmuebles").status_code == 404
        assert web.get("/inmuebles/CAT-01").status_code == 404


class TestSinDatosPersonales:
    def test_la_ficha_no_expone_al_propietario(self, web, cartera):
        r = web.get(portfolio.ruta_publica(portfolio.obtener(cartera, "CAT-01")))
        assert r.status_code == 200
        assert "Marta Propietaria" not in r.text
        assert "3001112233" not in r.text.replace(" ", "").replace("-", "")

    def test_el_listado_tampoco(self, web, cartera):
        assert "Marta Propietaria" not in web.get("/inmuebles").text


# ═══════════════════════════ Navegación ═══════════════════════════


class TestUrlCanonica:
    def test_el_codigo_solo_redirige_a_la_url_legible(self, web, cartera):
        r = web.get("/inmuebles/CAT-02")
        assert r.status_code == 301
        assert r.headers["location"] == "/inmuebles/apartamento-venta-frailes-dosquebradas/CAT-02"

    def test_un_slug_viejo_redirige_al_vigente(self, web, cartera):
        r = web.get("/inmuebles/casa-en-otro-barrio/CAT-01")
        assert r.status_code == 301
        assert r.headers["location"] == "/inmuebles/casa-venta-pinares-pereira/CAT-01"

    def test_el_slug_sale_del_municipio_deducido(self, cartera):
        """'Frailes, Dosquebradas' es zona + municipio, no un barrio de Pereira."""
        p = portfolio.obtener(cartera, "CAT-02")
        assert portfolio.slug_de(p) == "apartamento-venta-frailes-dosquebradas"

    def test_la_ficha_canonica_responde(self, web, cartera):
        r = web.get("/inmuebles/casa-venta-pinares-pereira/CAT-01")
        assert r.status_code == 200
        assert "CAT-01" in r.text and "Pinares" in r.text


class TestFiltros:
    def test_por_tipo(self, web, cartera):
        assert _ids_visibles(web.get("/inmuebles?tipo=casa").text) == {"CAT-01"}

    def test_por_municipio_deducido(self, web, cartera):
        assert _ids_visibles(web.get("/inmuebles?municipio=Dosquebradas").text) == {"CAT-02"}

    def test_rango_de_precio_con_puntos(self, web, cartera):
        """El comprador escribe '400.000.000', no '400000000'."""
        r = web.get("/inmuebles?min=400.000.000&max=700.000.000")
        assert _ids_visibles(r.text) == {"CAT-01", "CAT-03"}

    def test_el_importe_vuelve_con_separadores_de_miles(self, web, cartera):
        """«600000000» es ilegible y a ojo se confunde con 60 o 6.000 millones."""
        r = web.get("/inmuebles?min=300000000&max=700000000")
        assert 'value="300.000.000"' in r.text
        assert 'value="700.000.000"' in r.text
        assert 'value="300000000"' not in r.text

    def test_lo_que_no_es_un_importe_no_reaparece_filtrando(self, web, cartera):
        """Se devuelve lo interpretado, no lo tecleado: si no era número, nada."""
        r = web.get("/inmuebles?min=abc")
        assert r.status_code == 200
        assert "abc" not in r.text
        assert _ids_visibles(r.text) == PUBLICABLES

    def test_da_igual_como_se_escriba_el_importe(self, web, cartera):
        """Con puntos, con espacios o pelado: el filtro entiende lo mismo."""
        esperado = _ids_visibles(web.get("/inmuebles?max=500000000").text)
        for escrito in ("500.000.000", "500 000 000", "$500.000.000"):
            r = web.get(f"/inmuebles?max={escrito}")
            assert _ids_visibles(r.text) == esperado, escrito
            # Y siempre se devuelve en la misma forma canónica.
            assert 'value="500.000.000"' in r.text

    def test_rango_invertido_se_endereza(self, web, cartera):
        """Desde 700 hasta 400 es un lapsus, no una petición de cero resultados."""
        r = web.get("/inmuebles?min=700000000&max=400000000")
        assert _ids_visibles(r.text) == {"CAT-01", "CAT-03"}

    def test_alcobas_es_un_minimo(self, web, cartera):
        assert _ids_visibles(web.get("/inmuebles?hab=3").text) == {"CAT-01", "CAT-03"}

    def test_texto_libre_sin_tildes(self, web, cartera):
        assert _ids_visibles(web.get("/inmuebles?q=zuniga").text) == {"CAT-03"}

    def test_una_busqueda_sin_resultados_conserva_los_filtros(self, web, cartera):
        """Heredado de la rejilla del panel, donde fue una regresión real.

        La página decidía si mostrar los filtros mirando la lista ya filtrada,
        así que una combinación válida sin resultados los borraba —dejando al
        visitante sin forma de volver— y encima anunciaba que no había
        inventario, que era falso.
        """
        r = web.get("/inmuebles?tipo=lote&municipio=Dosquebradas")
        assert r.status_code == 200
        assert _ids_visibles(r.text) == set(), "esa combinación no tiene inventario"
        assert 'class="filtros"' in r.text, "sin filtros no hay forma de volver"
        assert "No encontramos inmuebles con esos criterios" in r.text
        assert "Quitar filtros" in r.text

    def test_un_valor_inventado_no_vacia_la_vitrina(self, web, cartera):
        """Una pantalla en blanco se lee como «no tienen nada», no como «filtro raro»."""
        r = web.get("/inmuebles?tipo=castillo&municipio=Narnia&orden=carisimo")
        assert r.status_code == 200
        assert _ids_visibles(r.text) == PUBLICABLES

    def test_ordenar_por_precio(self, web, cartera):
        orden = [p.id for p in portfolio.buscar_publicas(cartera, orden="precio_asc")]
        assert orden == ["CAT-02", "CAT-01", "CAT-03"]
        assert [p.id for p in portfolio.buscar_publicas(cartera, orden="precio_desc")] == orden[::-1]


class TestPaginacion:
    def test_los_filtros_sobreviven_al_cambio_de_pagina(self, web, cartera, monkeypatch):
        """El enlace a la página 2 tiene que arrastrar tipo y orden.

        Se sigue el enlace de verdad en vez de buscar los parámetros como
        texto: un `&` doblemente escapado deja `tipo=apartamento` visible en el
        HTML pero convierte el resto en un parámetro llamado `amp;orden`, y el
        visitante aterriza en una lista distinta de la que venía viendo.
        """
        import html
        import re

        monkeypatch.setattr(settings, "catalogo_por_pagina", 1)
        r = web.get("/inmuebles?tipo=apartamento&orden=precio_asc")
        assert r.status_code == 200

        enlaces = {
            html.unescape(h) for h in re.findall(r'href="(/inmuebles\?[^"]*)"', r.text)
        }
        pagina_2 = [e for e in enlaces if "pagina=2" in e]
        assert pagina_2, "no hay enlace a la página siguiente"

        segunda = web.get(pagina_2[0])
        assert segunda.status_code == 200
        # El más barato de los dos apartamentos está en la página 1; en la 2
        # tiene que estar el otro, y ninguna casa.
        assert _ids_visibles(segunda.text) == {"CAT-03"}

    def test_una_pagina_fuera_de_rango_cae_en_la_ultima(self, web, cartera, monkeypatch):
        monkeypatch.setattr(settings, "catalogo_por_pagina", 1)
        r = web.get("/inmuebles?pagina=99")
        assert r.status_code == 200
        assert len(_ids_visibles(r.text) & PUBLICABLES) == 1


class TestPuenteConElPanel:
    """La Cartera dejó de listar inmuebles: la vitrina es ahora el inventario.

    Eso obliga a que el operador pueda volver, y a que desde un inmueble llegue
    a su ficha interna — donde están la edición, las fotos y los comentarios—.
    """

    @pytest.fixture()
    def panel(self, web):
        r = web.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        assert r.status_code == 303
        return web

    def test_siempre_hay_salida_hacia_el_panel(self, web, cartera):
        """Incluso sin cookie. Atar la salida a la sesión resultó ser una trampa.

        El navegador deja de mandar la cookie por motivos que no son «no haber
        entrado»: una cookie emitida con otra ruta, o abrir la vitrina en
        `localhost` habiendo iniciado sesión en `127.0.0.1`. En cualquiera de
        esos casos el operador quedaba encerrado en la vitrina.
        """
        texto = web.get("/inmuebles").text
        assert "Acceso operador" in texto
        assert 'href="/dashboard/propiedades"' in texto

    def test_sin_sesion_el_enlace_invita_a_entrar_y_no_a_volver(self, web, cartera):
        texto = web.get("/inmuebles").text
        assert "Acceso operador" in texto
        assert "Volver al panel" not in texto

    def test_el_operador_vuelve_al_panel_desde_la_vitrina(self, panel, cartera):
        texto = panel.get("/inmuebles").text
        assert "← Volver al panel" in texto
        assert 'href="/dashboard/propiedades"' in texto

    def test_la_cabecera_ofrece_una_sola_puerta_de_vuelta(self, panel, cartera):
        """Hubo «Cartera» y «Panel» a la vez: dos botones para la misma intención.

        Son pantallas distintas, pero desde la vitrina las dos significan salir
        de aquí, y dentro del panel el menú ya lleva a cualquier sitio.
        """
        import re

        cabecera = re.search(
            r'<div class="quien">(.*?)</div>', panel.get("/inmuebles").text, re.S
        )
        assert cabecera, "no se encontró la cabecera de la vitrina"
        assert cabecera.group(1).count("/dashboard") == 1

    def test_la_ficha_ofrece_el_atajo_a_la_ficha_interna(self, panel, cartera):
        texto = panel.get("/inmuebles/casa-venta-pinares-pereira/CAT-01").text
        assert "/dashboard/propiedades/CAT-01" in texto
        assert "Editar ficha interna" in texto

    def test_la_cookie_de_sesion_llega_hasta_la_vitrina(self, panel, cartera):
        """El puente depende de que la cookie no esté limitada a /dashboard."""
        from app.security.sesion import COOKIE

        assert COOKIE in panel.cookies
        assert "← Volver al panel" in panel.get("/inmuebles").text

    def test_con_sesion_la_ficha_publica_sigue_sin_soltar_PII(self, panel, cartera):
        """Tener sesión pinta enlaces, no abre datos: para eso está el dashboard."""
        texto = panel.get("/inmuebles/casa-venta-pinares-pereira/CAT-01").text
        assert "Marta Propietaria" not in texto
        assert "3001112233" not in texto.replace(" ", "").replace("-", "")


# ═══════════════════════════ Conversión ═══════════════════════════


class TestFormularioDeInteres:
    RUTA = "/inmuebles/casa-venta-pinares-pereira/CAT-01"

    def test_sin_autorizacion_no_se_guarda_nada(self, web, cartera):
        from app.models import Prospecto

        antes = cartera.query(Prospecto).count()
        r = web.post(self.RUTA, data={"nombre": "Ana", "telefono": "3005556677"})
        assert r.status_code == 200
        assert "autorización" in r.text
        cartera.expire_all()
        assert cartera.query(Prospecto).count() == antes

    def test_con_autorizacion_crea_el_prospecto_atribuido_al_inmueble(self, web, cartera):
        from app.models import Prospecto

        r = web.post(
            self.RUTA,
            data={"nombre": "Ana Compradora", "telefono": "3009998877",
                  "mensaje": "¿Se puede visitar el sábado?", "autorizo": "on"},
        )
        assert r.status_code == 200
        assert "Recibimos tus datos" in r.text

        cartera.expire_all()
        p = cartera.query(Prospecto).order_by(Prospecto.id.desc()).first()
        assert p.consentimiento is True
        assert p.campana == "inmueble:CAT-01"
        assert p.red_origen == "catalogo"

    def test_el_prospecto_queda_con_el_inmueble_en_la_conversacion(self, web, cartera):
        """El asesor tiene que saber por cuál preguntan sin abrir el hilo."""
        from app.models import Mensaje, Prospecto

        web.post(
            self.RUTA,
            data={"nombre": "Luis", "telefono": "3007776655", "autorizo": "on"},
        )
        cartera.expire_all()
        p = cartera.query(Prospecto).order_by(Prospecto.id.desc()).first()
        textos = " ".join(
            m.texto for m in cartera.query(Mensaje).filter(Mensaje.prospecto_id == p.id)
        )
        assert "CAT-01" in textos

    def test_no_se_puede_dejar_lead_sobre_un_inmueble_oculto(self, web, cartera):
        r = web.post(
            "/inmuebles/casa-venta-cerritos-pereira/CAT-07",
            data={"nombre": "Ana", "telefono": "3001112233", "autorizo": "on"},
        )
        assert r.status_code == 404
