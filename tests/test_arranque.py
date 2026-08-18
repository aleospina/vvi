"""Arranque de la aplicación frente a fallos de Telegram (RNF-01).

El dashboard y la landing `/publicar` no necesitan internet para nada. Que un
`get_me` fallido impidiera levantarlos era acoplar el negocio entero a que
Telegram esté disponible en ese segundo exacto.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channels import telegram_bot


class BotQueFalla:
    """Aplicación de Telegram que revienta al conectarse, como sin red."""

    def __init__(self, momento: str = "initialize"):
        self.momento = momento
        self.updater = None
        self.running = False

    async def initialize(self):
        if self.momento == "initialize":
            raise ConnectionError("[Errno 11001] getaddrinfo failed")

    async def start(self):
        if self.momento == "start":
            raise ConnectionError("[Errno 11001] getaddrinfo failed")
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        pass


@pytest.fixture()
def app_con_bot_roto(monkeypatch):
    def instalar(momento: str):
        # Sin `raising=False`: si algún día `construir_app` deja de importarse
        # en `app.main`, este test debe fallar en vez de pasar sin probar nada.
        monkeypatch.setattr("app.main.construir_app", lambda: BotQueFalla(momento))
    return instalar


class TestArranqueResiliente:
    @pytest.mark.parametrize("momento", ["initialize", "start"])
    def test_la_app_levanta_aunque_telegram_falle(self, app_con_bot_roto, momento, caplog):
        app_con_bot_roto(momento)
        from app.main import app

        with caplog.at_level("ERROR", logger="vvi"):
            with TestClient(app, follow_redirects=False) as cli:
                assert cli.get("/dashboard/login").status_code == 200
                assert cli.get("/publicar").status_code == 200
                assert cli.get("/health").status_code == 200

        # Se comprueba que realmente se recorrió la ruta de fallo, y no que el
        # bot simplemente no se intentó levantar.
        assert any("no pudo iniciar" in r.getMessage() for r in caplog.records)

    def test_el_apagado_no_revienta_con_el_bot_roto(self, app_con_bot_roto):
        """Salir mal dejaría el proceso colgado en cada reinicio."""
        app_con_bot_roto("initialize")
        from app.main import app

        with TestClient(app) as cli:
            cli.get("/health")
        # Salir del contexto ejecuta el apagado; si lanzara, este test fallaría.

    def test_sin_token_no_se_intenta_bot(self, monkeypatch):
        """Con TELEGRAM_BOT_TOKEN vacío no debe haber ni tarea de bot."""
        from app.main import app

        with TestClient(app) as cli:
            assert cli.get("/health").status_code == 200
            assert app.state.bot is None
            assert app.state.tarea_bot is None


class TestAvisoDeRed:
    def test_resume_el_error_en_una_linea(self, caplog):
        """Sin esto cada fallo vuelca ~140 líneas de traza."""
        with caplog.at_level("WARNING", logger=telegram_bot.log.name):
            telegram_bot.aviso_de_red(ConnectionError("getaddrinfo failed"))

        assert len(caplog.records) == 1
        registro = caplog.records[0]
        assert registro.exc_info is None          # sin traza
        assert "ConnectionError" in registro.getMessage()
        assert "reintenta" in registro.getMessage()
