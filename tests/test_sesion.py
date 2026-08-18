"""Sesión del operador en el dashboard: ingreso, cierre y firma de la cookie.

El punto de todo esto es que cerrar sesión funcione de verdad, cosa que HTTP
Basic no permite.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.security.sesion import COOKIE, crear_token, validar_token


@pytest.fixture()
def cli():
    from app.main import app

    with TestClient(app, follow_redirects=False) as cliente:
        yield cliente


class TestToken:
    def test_ida_y_vuelta(self):
        assert validar_token(crear_token("operador")) == "operador"

    def test_token_vencido_no_vale(self):
        assert validar_token(crear_token("operador", duracion=-1)) is None

    def test_firma_alterada_no_vale(self):
        token = crear_token("operador")
        carga, _, _firma = token.rpartition(".")
        assert validar_token(f"{carga}.{'0' * 64}") is None

    def test_no_se_puede_falsificar_el_usuario(self):
        """Cambiar el usuario invalida la firma: la cookie no es un campo libre."""
        usuario_b64, expira, firma = crear_token("operador").split(".")
        import base64

        otro = base64.urlsafe_b64encode(b"admin").decode().rstrip("=")
        assert validar_token(f"{otro}.{expira}.{firma}") is None

    def test_no_se_puede_extender_el_vencimiento(self):
        usuario_b64, expira, firma = crear_token("operador").split(".")
        futuro = int(time.time()) + 999_999
        assert validar_token(f"{usuario_b64}.{futuro}.{firma}") is None

    @pytest.mark.parametrize("basura", ["", None, "x", "a.b", "a.b.c.d", "...."])
    def test_tokens_malformados_no_revientan(self, basura):
        assert validar_token(basura) is None


class TestCicloDeSesion:
    def test_sin_sesion_redirige_al_login(self, cli):
        r = cli.get("/dashboard")
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"

    def test_el_login_se_ve_sin_sesion(self, cli):
        r = cli.get("/dashboard/login")
        assert r.status_code == 200
        assert "Iniciar sesión" in r.text

    def test_credenciales_malas_no_dan_cookie(self, cli):
        r = cli.post("/dashboard/login", data={"usuario": "operador", "clave": "nope"})
        assert r.status_code == 401
        assert COOKIE not in r.cookies
        assert "incorrectos" in r.text

    def test_el_error_no_dice_cual_campo_fallo(self, cli):
        """No debe permitir enumerar usuarios válidos."""
        malo_usuario = cli.post(
            "/dashboard/login", data={"usuario": "noexiste", "clave": settings.dashboard_password}
        ).text
        mala_clave = cli.post(
            "/dashboard/login", data={"usuario": settings.dashboard_user, "clave": "nope"}
        ).text
        assert "incorrectos" in malo_usuario and "incorrectos" in mala_clave

    def test_ingreso_correcto_abre_sesion(self, cli):
        r = cli.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard"
        assert cli.cookies.get(COOKIE)
        assert cli.get("/dashboard").status_code == 200

    def test_la_cookie_es_httponly_y_samesite(self, cli):
        r = cli.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        cabecera = r.headers["set-cookie"].lower()
        assert "httponly" in cabecera
        assert "samesite=lax" in cabecera

    def test_cerrar_sesion_deja_de_dar_acceso(self, cli):
        cli.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        assert cli.get("/dashboard").status_code == 200

        r = cli.post("/dashboard/logout")
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"

        # Lo que importa: después de salir, el panel ya no se abre.
        assert cli.get("/dashboard").status_code == 303

    def test_con_sesion_el_login_redirige_al_panel(self, cli):
        cli.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        r = cli.get("/dashboard/login")
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard"

    def test_cookie_falsificada_no_abre_el_panel(self, cli):
        cli.cookies.set(COOKIE, "b3BlcmFkb3I.99999999999.deadbeef", path="/dashboard")
        assert cli.get("/dashboard").status_code == 303

    @pytest.mark.parametrize(
        "ruta",
        ["/dashboard", "/dashboard/propiedades", "/dashboard/captacion", "/dashboard/auditoria"],
    )
    def test_todas_las_vistas_exigen_sesion(self, cli, ruta):
        assert cli.get(ruta).status_code == 303


class TestBarra:
    def test_con_sesion_muestra_cerrar_sesion(self, cli):
        cli.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        html = cli.get("/dashboard").text
        assert "Cerrar sesión" in html
        assert "/dashboard/logout" in html
