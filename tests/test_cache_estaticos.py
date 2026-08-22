"""La hoja de estilos se invalida sola tras un despliegue.

`/static/estilos.css` es una URL que nunca cambiaba y StaticFiles no manda
`cache-control`, así que el navegador seguía pintando la hoja anterior: un
cambio de maquetación se veía igual que antes y parecía no haberse desplegado.
Estos tests fijan que toda página HTML pida la hoja con su huella.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import RAIZ, settings
from app.estaticos import VERSION_ESTILOS, _huella

#: Toda página que sirva HTML completo. Si se añade una y se olvida la versión,
#: esa pantalla es la que se queda con los estilos viejos.
PAGINAS = ["/inmuebles", "/publicar", "/c/ig-bio-pereira", "/dashboard/login"]


@pytest.fixture()
def web(monkeypatch):
    monkeypatch.setattr(settings, "catalogo_publico", True)
    from app.db import inicializar
    from app.main import app

    inicializar(seed=False)
    with TestClient(app, follow_redirects=False) as cli:
        yield cli


@pytest.mark.parametrize("ruta", PAGINAS)
def test_la_hoja_va_versionada(web, ruta):
    r = web.get(ruta)
    assert r.status_code == 200, ruta
    assert f"/static/estilos.css?v={VERSION_ESTILOS}" in r.text


@pytest.mark.parametrize("ruta", PAGINAS)
def test_ninguna_pagina_la_pide_sin_versión(web, ruta):
    """Una sola sin versionar basta para que esa pantalla se quede atrás."""
    html = web.get(ruta).text
    assert 'href="/static/estilos.css"' not in html


def test_la_huella_sale_del_contenido_del_archivo(tmp_path):
    """Si no cambiara con el contenido, no serviría de nada."""
    a = tmp_path / "a.css"
    a.write_text("body{color:red}", encoding="utf-8")
    antes = _huella(a)
    a.write_text("body{color:blue}", encoding="utf-8")
    assert _huella(a) != antes
    # Y es estable mientras el contenido no cambie.
    assert _huella(a) == _huella(a)


def test_un_archivo_ausente_no_tumba_la_aplicacion(tmp_path):
    """Prefiero estilos con la versión equivocada a un arranque fallido."""
    assert _huella(tmp_path / "no-existe.css") == "0"


def test_la_huella_corresponde_a_la_hoja_real():
    assert VERSION_ESTILOS == _huella(RAIZ / "app" / "static" / "estilos.css")
    assert re.fullmatch(r"[0-9a-f]{8}", VERSION_ESTILOS)
