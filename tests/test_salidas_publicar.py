"""Salidas de `/publicar`: nadie se queda encerrado tras publicar.

La página no extiende la plantilla pública, así que no tiene cabecera ni menú:
lo único que había tras confirmar era el botón «atrás» del navegador, que
reenvía el formulario. Estos tests fijan que siempre haya una salida y que
apunte a donde corresponde según quién publicó.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import Propiedad

#: Un inmueble válido mínimo para que el POST llegue a la confirmación.
FORMULARIO = {
    "propietario": "Marta Dueña",
    "telefono": "3001234567",
    "ciudad": "Pereira",
    "zona": "Pinares",
    "tipo": "apartamento",
    "negocio": "venta",
    "precio": "350000000",
    "autorizo": "on",
}


@pytest.fixture()
def web(monkeypatch):
    monkeypatch.setattr(settings, "catalogo_publico", True)
    inicializar(seed=False)
    from app.main import app

    with TestClient(app, follow_redirects=False) as cli:
        yield cli

    db = SessionLocal()
    try:
        db.query(Propiedad).filter(Propiedad.propietario == "Marta Dueña").delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _publicar(cli) -> str:
    r = cli.post("/publicar", data=FORMULARIO)
    assert r.status_code == 200, r.text[:300]
    assert "Recibido" in r.text, "no llegó a la confirmación"
    return r.text


class TestDueno:
    """Quien publica sin sesión es el propietario: su portada es la vitrina."""

    def test_la_confirmacion_ofrece_volver(self, web):
        html = _publicar(web)
        assert 'href="/inmuebles"' in html
        assert "Publicar otro inmueble" in html

    def test_no_se_le_ofrece_el_panel(self, web):
        """El dueño no tiene panel; enseñárselo sería mandarlo a un login."""
        assert "/dashboard" not in _publicar(web)

    def test_el_formulario_tambien_tiene_salida(self, web):
        """Quien se arrepiente antes de enviar también necesita salir."""
        assert 'href="/inmuebles"' in web.get("/publicar").text

    def test_sin_vitrina_no_se_promete_una_salida_rota(self, web, monkeypatch):
        """Con el catálogo apagado, /inmuebles responde 404: no se enlaza."""
        monkeypatch.setattr(settings, "catalogo_publico", False)
        html = _publicar(web)
        assert 'href="/inmuebles"' not in html
        # Y aun así queda algo que hacer.
        assert "Publicar otro inmueble" in html


class TestOperador:
    @pytest.fixture()
    def panel(self, web):
        r = web.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        assert r.status_code == 303
        return web

    def test_vuelve_a_su_panel_y_no_a_la_vitrina(self, panel):
        html = _publicar(panel)
        assert 'href="/dashboard/propiedades"' in html
        assert 'href="/inmuebles"' not in html
