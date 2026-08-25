"""Las fotos del inmueble en la conversación (RF-10).

Preguntar por una propiedad y recibir solo su descripción es contestar a
medias: lo que vende una casa es verla. Aquí se comprueban las tres piezas de
eso, que son independientes entre sí:

  · **cuáles** se mandan — `fotos.rutas_para_enviar`, que ordena, topea y
    sobrevive a un archivo que ya no está en disco;
  · **cuándo** — decisión del gateway, no del canal: solo la ficha única;
  · **cómo** — el transporte de cada canal, que sí es cosa suya: Telegram
    agrupa en un álbum y WhatsApp manda una imagen por mensaje.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from app.channels import conversacion, gateway, telegram_bot, whatsapp_evo
from app.config import settings
from app.models import FotoPropiedad, Propiedad
from app.routers import whatsapp
from app.services import fotos


def _jpeg(color=(11, 93, 80)) -> bytes:
    from PIL import Image

    memoria = io.BytesIO()
    Image.new("RGB", (120, 90), color).save(memoria, "JPEG")
    return memoria.getvalue()


JPG = _jpeg()


@pytest.fixture()
def album(tmp_path, monkeypatch):
    """Deja el directorio de fotos en un temporal y devuelve un cargador.

    Los tests no deben escribir en `app/static/fotos/`: ahí vive la cartera real
    del desarrollador.
    """
    monkeypatch.setattr(fotos, "DIRECTORIO", tmp_path)

    def cargar(db, propiedad_id: str, cuantas: int, *, en_disco: int | None = None):
        """Registra `cuantas` fotos y escribe en disco las primeras `en_disco`."""
        propiedad = db.get(Propiedad, propiedad_id)
        escribir = cuantas if en_disco is None else en_disco
        for i in range(cuantas):
            nombre = f"{propiedad_id.lower()}-{i}.jpg"
            if i < escribir:
                (tmp_path / nombre).write_bytes(JPG)
            propiedad.fotos.append(FotoPropiedad(archivo=nombre, orden=i))
        db.flush()
        return propiedad

    return cargar


# ─────────────────────────── Cuáles ───────────────────────────


class TestQueFotosSalen:
    def test_van_en_el_orden_de_la_portada(self, db, album):
        propiedad = album(db, "PROP-MED-003", 3)
        rutas = fotos.rutas_para_enviar(propiedad)
        assert [r.name for r in rutas] == [f.archivo for f in propiedad.fotos]

    def test_no_pasa_del_tope(self, db, album):
        propiedad = album(db, "PROP-MED-003", 9)
        assert len(fotos.rutas_para_enviar(propiedad)) == fotos.TOPE_EN_CHAT

    def test_el_tope_se_llena_con_las_que_si_estan(self, db, album):
        """Que falte la segunda no debe dejar la galería en cuatro.

        El tope cuenta fotos entregadas, no posiciones recorridas.
        """
        propiedad = album(db, "PROP-MED-003", 9)
        (fotos.DIRECTORIO / propiedad.fotos[1].archivo).unlink()
        rutas = fotos.rutas_para_enviar(propiedad)
        assert len(rutas) == fotos.TOPE_EN_CHAT
        assert propiedad.fotos[1].archivo not in [r.name for r in rutas]

    def test_la_foto_que_no_esta_en_disco_se_omite(self, db, album):
        """El caso del redespliegue: quedan las filas y se van los archivos.

        Mandar tres es peor que mandar cinco, pero infinitamente mejor que
        tumbar el turno y dejar al comprador sin respuesta.
        """
        propiedad = album(db, "PROP-MED-003", 5, en_disco=3)
        rutas = fotos.rutas_para_enviar(propiedad)
        assert len(rutas) == 3
        assert all(r.exists() for r in rutas)

    def test_un_inmueble_sin_fotos_no_manda_nada(self, db):
        assert fotos.rutas_para_enviar(db.get(Propiedad, "PROP-MED-003")) == []


# ─────────────────────────── Cuándo ───────────────────────────


class TestCuandoLasManda:
    def test_la_ficha_unica_trae_las_fotos(self, db, album, prospecto_consentido):
        album(db, "PROP-MED-003", 3)
        respuesta = gateway.procesar(db, prospecto_consentido, "casa en Envigado")
        assert len(respuesta.matches) == 1
        assert len(respuesta.fotos) == 3

    def test_un_listado_de_varios_no_trae_fotos(self, db, album, prospecto_consentido):
        """Ocho inmuebles con cinco fotos cada uno son cuarenta imágenes.

        En un listado la foto no ayuda a elegir: tapa la lista que sí lo hace.
        """
        album(db, "PROP-PER-001", 3)
        album(db, "PROP-PER-002", 3)
        respuesta = gateway.procesar(db, prospecto_consentido, "casas en Pereira")
        assert len(respuesta.matches) > 1
        assert respuesta.fotos == []

    def test_las_rutas_sobreviven_al_cierre_de_la_sesion(self, db, album, prospecto_consentido):
        """El canal las lee cuando la sesión que las cargó ya no existe.

        Si `fotos` guardara registros en vez de rutas, aquí saltaría un
        `DetachedInstanceError` en producción y nunca en un test que no cierre.
        """
        album(db, "PROP-MED-003", 2)
        respuesta = gateway.procesar(db, prospecto_consentido, "casa en Envigado")
        db.close()
        assert all(r.exists() for r in respuesta.fotos)


# ─────────────────────────── Cómo: WhatsApp ───────────────────────────


@pytest.fixture()
def canal_whatsapp(monkeypatch):
    monkeypatch.setattr(settings, "evolution_url", "http://evolution.pruebas")
    monkeypatch.setattr(settings, "evolution_api_key", "apikey-de-pruebas")
    monkeypatch.setattr(settings, "evolution_webhook_token", "token-de-pruebas")
    monkeypatch.setattr(settings, "evolution_instancia", "vvi")


class TestTransporteWhatsApp:
    def test_las_fotos_van_antes_que_la_ficha(self, monkeypatch, tmp_path):
        """Se ve el inmueble y después se lee de qué se trata.

        Al revés, la pregunta con la que cierra la ficha —«¿agendamos una
        visita?»— queda enterrada bajo cinco imágenes y nadie la contesta.
        """
        ruta = tmp_path / "a.jpg"
        ruta.write_bytes(JPG)
        orden: list[str] = []
        monkeypatch.setattr(
            conversacion, "turno",
            lambda *a, **k: conversacion.Turno(["La ficha"], [ruta]),
        )
        monkeypatch.setattr(whatsapp_evo, "escribiendo", lambda *a, **k: None)
        monkeypatch.setattr(
            whatsapp_evo, "enviar_imagen", lambda *a, **k: orden.append("foto")
        )
        monkeypatch.setattr(
            whatsapp_evo, "enviar_texto", lambda *a, **k: orden.append("texto")
        )

        whatsapp.atender("573001234567", "casa en Envigado", "Ana")

        assert orden == ["foto", "texto"]

    def test_una_foto_que_falla_no_se_lleva_la_ficha(self, monkeypatch, tmp_path):
        """Sin fotos la respuesta empeora; sin ficha desaparece."""
        ruta = tmp_path / "a.jpg"
        ruta.write_bytes(JPG)
        enviados: list[str] = []

        def _revienta(*a, **k):
            raise RuntimeError("Evolution no responde")

        monkeypatch.setattr(
            conversacion, "turno",
            lambda *a, **k: conversacion.Turno(["La ficha"], [ruta]),
        )
        monkeypatch.setattr(whatsapp_evo, "escribiendo", lambda *a, **k: None)
        monkeypatch.setattr(whatsapp_evo, "enviar_imagen", _revienta)
        monkeypatch.setattr(
            whatsapp_evo, "enviar_texto", lambda n, t: enviados.append(t)
        )

        whatsapp.atender("573001234567", "casa en Envigado", "Ana")

        assert enviados == ["La ficha"]

    def test_la_imagen_viaja_en_base64_y_no_como_enlace(
        self, monkeypatch, tmp_path, canal_whatsapp
    ):
        """Un enlace obligaría a que Evolution alcance a VVI por HTTP.

        En desarrollo no puede —corre en Docker— y en producción el fallo sería
        mudo: la ficha llegaría sin fotos y en los logs de VVI no habría nada.
        """
        import base64

        ruta = tmp_path / "casa.jpg"
        ruta.write_bytes(JPG)
        capturado: dict = {}

        class _Respuesta:
            def raise_for_status(self):
                return None

        class _Cliente:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                capturado["url"] = url
                capturado["json"] = json
                return _Respuesta()

        monkeypatch.setattr(whatsapp_evo, "_cliente", lambda *a, **k: _Cliente())

        assert whatsapp_evo.enviar_imagen("573001234567", ruta) is True
        assert capturado["url"] == "/message/sendMedia/vvi"
        assert capturado["json"]["mediatype"] == "image"
        assert base64.b64decode(capturado["json"]["media"]) == JPG

    def test_sin_canal_configurado_no_intenta_enviar(self, tmp_path):
        ruta = tmp_path / "casa.jpg"
        ruta.write_bytes(JPG)
        assert whatsapp_evo.enviar_imagen("573001234567", ruta) is False


# ─────────────────────────── Cómo: Telegram ───────────────────────────


class _ChatFalso:
    def __init__(self):
        self.fotos: list[bytes] = []
        self.albumes: list[list] = []

    async def send_photo(self, foto):
        self.fotos.append(foto)

    async def send_media_group(self, medios):
        self.albumes.append(medios)


class _UpdateFalso:
    def __init__(self, chat):
        self.effective_chat = chat


class TestTransporteTelegram:
    def _enviar(self, rutas):
        chat = _ChatFalso()
        asyncio.run(telegram_bot._enviar_fotos(_UpdateFalso(chat), rutas))
        return chat

    def test_varias_fotos_van_como_un_solo_album(self, tmp_path):
        """Cinco envíos sueltos son cinco notificaciones; el álbum es una."""
        rutas = []
        for i in range(3):
            ruta = tmp_path / f"{i}.jpg"
            ruta.write_bytes(JPG)
            rutas.append(ruta)

        chat = self._enviar(rutas)

        assert chat.fotos == []
        assert len(chat.albumes) == 1
        assert len(chat.albumes[0]) == 3

    def test_una_sola_foto_no_se_manda_como_album(self, tmp_path):
        """Un álbum de uno se ve peor que la foto sola."""
        ruta = tmp_path / "unica.jpg"
        ruta.write_bytes(JPG)

        chat = self._enviar([ruta])

        assert chat.albumes == []
        assert chat.fotos == [JPG]

    def test_sin_fotos_no_toca_a_telegram(self):
        chat = self._enviar([])
        assert chat.fotos == [] and chat.albumes == []

    def test_un_archivo_ilegible_no_tumba_el_turno(self, tmp_path):
        """La ficha en texto sigue siendo la respuesta aunque la galería falle."""
        chat = self._enviar([tmp_path / "no-existe.jpg"])
        assert chat.fotos == [] and chat.albumes == []


# ─────────────────────────── Dónde se guardan ───────────────────────────


class TestAlmacenamientoEfimero:
    """El fallo mudo del despliegue: las filas sobreviven y los archivos no.

    Pasa cuando se configura `DATABASE_URL` contra el volumen y se olvida
    `FOTOS_DIR`. El síntoma llega un despliegue más tarde y no se parece a su
    causa —el inmueble sigue en la cartera, con su nombre y sin imagen—, así que
    lo que importa es que se diga antes, no que se deduzca después.
    """

    def test_el_directorio_junto_al_codigo_es_efimero(self, monkeypatch):
        from app.config import RAIZ

        monkeypatch.setattr(fotos, "DIRECTORIO", RAIZ / "app" / "static" / "fotos")
        assert fotos.es_efimero() is True

    def test_el_directorio_del_volumen_no_lo_es(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fotos, "DIRECTORIO", tmp_path)
        assert fotos.es_efimero() is False

    def test_health_lo_reporta(self, monkeypatch, tmp_path):
        """Es el endpoint que la plataforma consulta cada minuto."""
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setattr(fotos, "DIRECTORIO", tmp_path)
        with TestClient(app) as cli:
            assert cli.get("/health").json()["fotos_efimeras"] is False

    def test_la_base_en_volumen_con_las_fotos_en_el_codigo_avisa(self, monkeypatch):
        from app import main

        monkeypatch.setattr(fotos, "es_efimero", lambda: True)
        monkeypatch.setattr(settings, "database_url", "sqlite:////data/vvi.db")
        assert main._base_junto_al_codigo() is False

    def test_en_local_no_avisa_de_nada(self, monkeypatch):
        """Junto al código está bien en un portátil: un aviso ahí es ruido.

        Un aviso que salta en cada arranque enseña a ignorar los avisos.
        """
        from app import main
        from app.config import RAIZ

        monkeypatch.setattr(
            settings, "database_url", f"sqlite:///{(RAIZ / 'data' / 'vvi.db').as_posix()}"
        )
        assert main._base_junto_al_codigo() is True

    def test_una_base_que_no_es_sqlite_esta_siempre_fuera(self, monkeypatch):
        monkeypatch.setattr(settings, "database_url", "postgresql://host/vvi")
        from app import main

        assert main._base_junto_al_codigo() is False

    def test_el_aviso_sale_en_el_arranque(self, monkeypatch, caplog):
        """Lo que importa no es el helper, es que la línea aparezca en el log."""
        import logging

        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setattr(fotos, "es_efimero", lambda: True)
        monkeypatch.setattr(settings, "database_url", "sqlite:////data/vvi.db")
        with caplog.at_level(logging.WARNING):
            with TestClient(app):
                pass
        assert any("FOTOS_DIR" in r.message for r in caplog.records)

    def test_en_local_el_arranque_calla(self, monkeypatch, caplog):
        import logging

        from fastapi.testclient import TestClient

        from app.config import RAIZ
        from app.main import app

        monkeypatch.setattr(fotos, "es_efimero", lambda: True)
        monkeypatch.setattr(
            settings, "database_url", f"sqlite:///{(RAIZ / 'data' / 'vvi.db').as_posix()}"
        )
        with caplog.at_level(logging.WARNING):
            with TestClient(app):
                pass
        assert not any("FOTOS_DIR" in r.message for r in caplog.records)
