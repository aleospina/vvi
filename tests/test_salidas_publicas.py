"""Salidas de las páginas públicas: nadie se queda encerrado tras enviar.

`/publicar` y `/c/{slug}` no extienden la plantilla pública, así que no tienen
cabecera ni menú: lo único que había tras confirmar era el botón «atrás» del
navegador, que reenvía el formulario. Estos tests fijan que siempre haya una
salida y que apunte a donde corresponde según quién la usó.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import Propiedad, Prospecto

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
        # La landing crea prospectos; se borran uno a uno para que sus mensajes
        # y consentimientos caigan por la cascada de la relación.
        for p in db.query(Prospecto).filter(Prospecto.campana == "ig-bio-pereira"):
            db.delete(p)
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


class TestLandingDeCampana:
    """`/c/{slug}` tenía el mismo callejón sin salida, del lado del comprador."""

    RUTA = "/c/ig-bio-pereira"
    LEAD = {"nombre": "Ana Compradora", "telefono": "3005551122", "autorizo": "on"}

    def _enviar(self, cli):
        r = cli.post(self.RUTA, data=self.LEAD)
        assert r.status_code == 200, r.text[:300]
        assert "¡Listo!" in r.text, "no llegó a la confirmación"
        return r.text

    def test_tras_dejar_los_datos_puede_ver_la_cartera(self, web):
        html = self._enviar(web)
        assert 'href="/inmuebles"' in html

    def test_el_formulario_ofrece_mirar_antes_de_dejar_datos(self, web):
        """Quien no quiere dar su teléfono todavía tiene dónde ir."""
        assert 'href="/inmuebles"' in web.get(self.RUTA).text

    def test_al_comprador_no_se_le_ofrece_el_panel(self, web):
        assert "/dashboard" not in self._enviar(web)

    def test_sin_vitrina_no_se_enlaza_una_ruta_que_da_404(self, web, monkeypatch):
        monkeypatch.setattr(settings, "catalogo_publico", False)
        assert 'href="/inmuebles"' not in self._enviar(web)


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
