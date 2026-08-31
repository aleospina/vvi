"""La puerta de WhatsApp de la vitrina (`/wa`).

Lo que se protege aquí es una promesa de dos mitades:

  · **Siempre hay alguien al otro lado.** El botón ya no depende de que alguien
    haya escrito un teléfono en la configuración: si el asistente virtual está
    vinculado, la consulta va a su número; si no lo está, cae en el respaldo
    humano. Nunca lleva a un WhatsApp que nadie lee.
  · **El teléfono no se publica.** En el HTML no hay ningún número: hay un
    enlace a `/wa`, y el destino se decide en el momento del clic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channels import whatsapp_evo
from app.config import settings
from app.db import inicializar
from app.routers import contacto


@pytest.fixture()
def web(monkeypatch):
    """Visitante cualquiera: sin sesión y sin seguir las redirecciones."""
    monkeypatch.setattr(settings, "catalogo_publico", True)
    inicializar(seed=False)
    from app.main import app

    with TestClient(app, follow_redirects=False) as cli:
        yield cli


@pytest.fixture(autouse=True)
def sin_cache():
    """El número vinculado se cachea cinco minutos; entre pruebas, no."""
    whatsapp_evo._numero_cache = ("", 0.0)
    yield
    whatsapp_evo._numero_cache = ("", 0.0)


# ═════════════════════ A quién van a parar las consultas ═════════════════════

def test_sin_asistente_vinculado_cae_en_el_respaldo(web, monkeypatch):
    """Sin canal montado, la consulta la atiende una persona."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "")

    r = web.get("/wa")

    assert r.status_code == 302
    assert r.headers["location"].startswith(f"https://wa.me/{settings.whatsapp_respaldo}?")


def test_con_asistente_vinculado_manda_al_asistente(web, monkeypatch):
    """Escaneado el QR, el destino es el número que atiende la instancia."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "573001234567")

    r = web.get("/wa")

    assert r.headers["location"].startswith("https://wa.me/573001234567?")


def test_el_numero_configurado_a_mano_manda_sobre_todo(web, monkeypatch):
    """`WHATSAPP_CONTACTO` es el anulador: si está puesto, decide él."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "573009998877")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "573001234567")

    r = web.get("/wa")

    assert r.headers["location"].startswith("https://wa.me/573009998877?")


def test_el_destino_no_se_cachea(web, monkeypatch):
    """Vincular otro número no puede quedar tapado por una redirección guardada."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "")

    r = web.get("/wa")

    assert r.headers["cache-control"] == "no-store"


# ═══════════════════════ El mensaje que llega escrito ═══════════════════════

def test_sin_ficha_el_saludo_es_general(web, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_contacto", "573009998877")

    r = web.get("/wa")

    assert "inmueble" in r.headers["location"]


def test_desde_una_ficha_el_mensaje_lleva_el_codigo(web, monkeypatch):
    """El asesor sabe desde qué inmueble escribieron sin preguntarlo."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "573009998877")

    r = web.get("/wa?ref=PROP-MED-001")

    assert "PROP-MED-001" in r.headers["location"]


def test_la_referencia_no_puede_escribir_el_mensaje():
    """`ref` viene de la URL: solo entra de ahí un código, no un texto."""
    texto = contacto._texto("A-1 <b>hola</b> ¿y esto? &text=otro")

    assert "<b>" not in texto and "&" not in texto
    assert "A-1" in texto


def test_la_referencia_larga_se_recorta():
    texto = contacto._texto("X" * 300)

    assert len(texto) < len(contacto.SALUDO) + contacto.LARGO_REF + 60


# ═══════════════════ El teléfono no se escribe en la página ═══════════════════

@pytest.mark.parametrize("ruta", ["/", "/inmuebles", "/publicar"])
def test_la_pagina_publica_no_publica_ningun_numero(web, monkeypatch, ruta):
    monkeypatch.setattr(settings, "whatsapp_contacto", "573009998877")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "573001234567")

    html = web.get(ruta).text

    assert "573009998877" not in html
    assert "573001234567" not in html
    assert settings.whatsapp_respaldo not in html
    assert "wa.me" not in html


def test_el_boton_flotante_esta_siempre(web, monkeypatch):
    """Antes desaparecía cuando no había número configurado."""
    monkeypatch.setattr(settings, "whatsapp_contacto", "")
    monkeypatch.setattr(whatsapp_evo, "numero_vinculado", lambda: "")

    html = web.get("/").text

    assert 'class="wasap" href="/wa"' in html


# ═════════════ Quién atiende hoy: se lo preguntamos a Evolution ═════════════

class _RespuestaFalsa:
    status_code = 200

    def __init__(self, datos):
        self._datos = datos

    def raise_for_status(self):
        pass

    def json(self):
        return self._datos


class _ClienteFalso:
    """El mínimo de `httpx.Client` que usa `numero_vinculado`."""

    def __init__(self, datos, llamadas):
        self._datos = datos
        self._llamadas = llamadas

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, ruta):
        self._llamadas.append(ruta)
        return _RespuestaFalsa(self._datos)

    def delete(self, ruta):
        self._llamadas.append(ruta)
        return _RespuestaFalsa({})


@pytest.fixture()
def canal(monkeypatch):
    """Evolution configurado, para que `tiene_whatsapp` sea cierto."""
    monkeypatch.setattr(settings, "evolution_url", "http://evolution.pruebas")
    monkeypatch.setattr(settings, "evolution_api_key", "apikey-de-pruebas")
    monkeypatch.setattr(settings, "evolution_webhook_token", "token-de-pruebas")
    monkeypatch.setattr(settings, "evolution_instancia", "vvi")


def _responde(monkeypatch, datos) -> list[str]:
    llamadas: list[str] = []
    monkeypatch.setattr(
        whatsapp_evo, "_cliente", lambda timeout=None: _ClienteFalso(datos, llamadas)
    )
    return llamadas


def test_lee_el_numero_de_la_instancia_vinculada(canal, monkeypatch):
    _responde(monkeypatch, [
        {"name": "otra", "connectionStatus": "open", "ownerJid": "573999999999@s.whatsapp.net"},
        {"name": "vvi", "connectionStatus": "open", "ownerJid": "573001234567@s.whatsapp.net"},
    ])

    assert whatsapp_evo.numero_vinculado() == "573001234567"


def test_tambien_entiende_la_forma_antigua_de_evolution(canal, monkeypatch):
    """La v1 anida los mismos datos bajo `instance` y les cambia el nombre."""
    _responde(monkeypatch, [
        {"instance": {"instanceName": "vvi", "status": "open",
                      "owner": "573001234567@s.whatsapp.net"}},
    ])

    assert whatsapp_evo.numero_vinculado() == "573001234567"


def test_con_la_sesion_cerrada_no_hay_numero(canal, monkeypatch):
    """El número sigue figurando, pero ahí ya no lee nadie: mejor el respaldo."""
    _responde(monkeypatch, [
        {"name": "vvi", "connectionStatus": "close", "ownerJid": "573001234567@s.whatsapp.net"},
    ])

    assert whatsapp_evo.numero_vinculado() == ""


def test_no_se_pregunta_una_vez_por_visitante(canal, monkeypatch):
    """El número solo cambia al escanear otro QR; la vitrina lo pregunta mucho."""
    llamadas = _responde(monkeypatch, [
        {"name": "vvi", "connectionStatus": "open", "ownerJid": "573001234567@s.whatsapp.net"},
    ])

    for _ in range(5):
        whatsapp_evo.numero_vinculado()

    assert len(llamadas) == 1


def test_si_evolution_no_contesta_no_se_cae_nada(canal, monkeypatch):
    def _revienta(timeout=None):
        raise whatsapp_evo.httpx.ConnectError("sin ruta al servidor")

    monkeypatch.setattr(whatsapp_evo, "_cliente", _revienta)

    assert whatsapp_evo.numero_vinculado() == ""


def test_sin_canal_montado_ni_se_pregunta(monkeypatch):
    monkeypatch.setattr(settings, "evolution_url", "")
    llamadas = _responde(monkeypatch, [])

    assert whatsapp_evo.numero_vinculado() == ""
    assert llamadas == []


def test_desvincular_olvida_el_numero(canal, monkeypatch):
    """Cerrar la sesión es el instante en que ese número deja de atender."""
    _responde(monkeypatch, [
        {"name": "vvi", "connectionStatus": "open", "ownerJid": "573001234567@s.whatsapp.net"},
    ])
    assert whatsapp_evo.numero_vinculado() == "573001234567"

    whatsapp_evo.desvincular()

    assert whatsapp_evo._numero_cache == ("", 0.0)
