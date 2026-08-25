"""Canal Instagram vía la Messaging API de Meta (ADR-02c).

Todo corre sin cuenta de Meta y sin red: el webhook es un POST firmado, así que
el flujo completo —firma, filtros, deduplicación, consentimiento, respuesta— se
verifica con payloads con la forma real y el cliente HTTP interceptado.

Los tres límites del canal (1000 bytes por mensaje, sin Markdown, ventana de
24 h) son de transporte y se prueban aparte, contra `instagram_bot`: son la
razón de que este canal no sea una copia del de Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.channels import conversacion, instagram_bot, instagram_login
from app.config import settings
from app.routers import instagram
from app.services import ajustes

APP_SECRET = "secreto-de-la-app-de-pruebas"
VERIFY_TOKEN = "token-de-verificacion-de-pruebas"
IGSID = "17841400000000001"


@pytest.fixture(autouse=True)
def canal_configurado(monkeypatch):
    """Deja el canal 'configurado' sin que exista ninguna cuenta de Meta."""
    monkeypatch.setattr(settings, "instagram_token", "token-de-pruebas")
    monkeypatch.setattr(settings, "instagram_app_secret", APP_SECRET)
    monkeypatch.setattr(settings, "instagram_verify_token", VERIFY_TOKEN)
    monkeypatch.setattr(settings, "instagram_api_base", "https://graph.pruebas")
    instagram._VISTOS.clear()
    conversacion._PENDIENTES.clear()
    yield
    instagram._VISTOS.clear()
    conversacion._PENDIENTES.clear()


@pytest.fixture()
def enviados(monkeypatch):
    """Captura lo que el bot habría mandado por Instagram."""
    salidas: list[tuple[str, str]] = []

    def _falso_enviar(igsid: str, texto: str, **kwargs) -> bool:
        salidas.append((igsid, texto))
        return True

    monkeypatch.setattr(instagram_bot, "enviar_texto", _falso_enviar)
    monkeypatch.setattr(instagram_bot, "escribiendo", lambda *a, **k: None)
    monkeypatch.setattr(
        instagram_bot,
        "perfil_usuario",
        lambda igsid: {"name": "Andrés Prueba", "username": "andres.prueba"},
    )
    return salidas


@pytest.fixture()
def cliente():
    from app.main import app

    with TestClient(app) as c:
        yield c


#: Los tests que llegan a la base comparten el archivo temporal del conftest, así
#: que cada uno necesita su propio IGSID o el prospecto del anterior lo altera.
_SECUENCIA = itertools.count(1)


@pytest.fixture()
def igsid() -> str:
    return f"178414000000{next(_SECUENCIA):05d}"


def evento(texto="Hola", *, mensaje_id="MSG-1", igsid=IGSID, eco=False, boton=None) -> dict:
    """Payload de `entry[].messaging[]` con la forma que envía Meta."""
    mensaje: dict = {"mid": mensaje_id}
    if texto is not None:
        mensaje["text"] = texto
    if eco:
        mensaje["is_echo"] = True
    if boton:
        mensaje["quick_reply"] = {"payload": boton}
    return {
        "object": "instagram",
        "entry": [
            {
                "id": "17841400000000000",
                "time": 1735689600,
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": "17841400000000000"},
                        "timestamp": 1735689600,
                        "message": mensaje,
                    }
                ],
            }
        ],
    }


def firmar(crudo: bytes, secreto: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secreto.encode("utf-8"), crudo, hashlib.sha256).hexdigest()


def entregar(cliente, cuerpo: dict, *, firma: str | None = None):
    """POST al webhook firmado como lo firma Meta: sobre los bytes que van.

    Serializar aquí y mandar `content=` no es un detalle: si se dejara que el
    cliente vuelva a serializar el dict, la firma se calcularía sobre unos bytes
    y se verificaría sobre otros.
    """
    crudo = json.dumps(cuerpo).encode("utf-8")
    return cliente.post(
        "/webhooks/instagram",
        content=crudo,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": firma or firmar(crudo),
        },
    )


# ─────────────────────────── Transporte ───────────────────────────


class TestFormato:
    """Instagram no renderiza Markdown: los asteriscos se verían tal cual."""

    def test_quita_el_marcado_de_las_plantillas(self):
        limpio = instagram_bot.a_texto_plano("*Centro, Pereira* — _85 m²_ y `PROP-1`")
        assert limpio == "Centro, Pereira — 85 m² y PROP-1"

    def test_un_asterisco_suelto_es_texto_del_operador(self):
        """En una descripción cargada a mano, `2*1` es una multiplicación."""
        assert (
            instagram_bot.a_texto_plano("Promo 2*1 en administración")
            == "Promo 2*1 en administración"
        )


class TestTroceo:
    def test_lo_que_cabe_va_en_un_solo_mensaje(self):
        assert instagram_bot.trocear("Hola, ¿qué buscas?") == ["Hola, ¿qué buscas?"]

    def test_el_tope_se_mide_en_bytes_y_no_en_caracteres(self):
        """600 emojis son 600 caracteres y 2400 bytes.

        Con `len()` esto "cabía" y Meta rechazaba el mensaje justo cuando traía
        acentos o emojis, es decir, casi siempre.
        """
        texto = "🏠" * 600
        trozos = instagram_bot.trocear(texto)
        assert len(trozos) > 1
        assert all(len(t.encode("utf-8")) <= instagram_bot.TOPE_BYTES for t in trozos)
        assert "".join(trozos) == texto, "no se puede perder ni duplicar nada"

    def test_corta_entre_fichas_y_no_por_la_mitad_de_una(self):
        """Partir una ficha a la mitad es peor que mandar dos mensajes."""
        fichas = [
            f"🏠 PROP-{i}\nLaureles · 92 m² · $520.000.000\nTres alcobas." for i in range(30)
        ]
        texto = "\n\n".join(fichas)

        trozos = instagram_bot.trocear(texto)
        assert len(trozos) > 1
        for trozo in trozos:
            assert all(pieza in fichas for pieza in trozo.split("\n\n"))
        assert "\n\n".join(trozos) == texto

    def test_una_palabra_gigante_se_parte_sin_romper_un_caracter(self):
        """Una URL enorme no tiene costura: se corta por bytes, y el corte no
        puede dejar media secuencia UTF-8 en cada mensaje."""
        texto = "á" * 1500  # 3000 bytes sin un solo espacio
        trozos = instagram_bot.trocear(texto)
        assert len(trozos) > 1
        assert all(len(t.encode("utf-8")) <= instagram_bot.TOPE_BYTES for t in trozos)
        assert "".join(trozos) == texto


class TestEnvio:
    @pytest.fixture()
    def peticiones(self, monkeypatch) -> list[dict]:
        """Intercepta el cliente HTTP y guarda los cuerpos que se habrían enviado."""
        capturadas: list[dict] = []

        def _responder(peticion: httpx.Request) -> httpx.Response:
            capturadas.append(json.loads(peticion.content))
            return httpx.Response(200, json={"message_id": "mid.1"})

        monkeypatch.setattr(
            instagram_bot,
            "_cliente",
            lambda: httpx.Client(
                base_url="https://graph.pruebas/v23.0",
                transport=httpx.MockTransport(_responder),
            ),
        )
        return capturadas

    def test_los_botones_van_solo_en_el_ultimo_trozo(self, peticiones):
        """Repetidos en cada trozo, el comprador ve tres veces el mismo par."""
        instagram_bot.enviar_texto(
            IGSID, "línea\n\n" * 400, respuestas=instagram_bot.RESPUESTAS_CONSENTIMIENTO
        )
        assert len(peticiones) > 1
        assert all("quick_replies" not in p["message"] for p in peticiones[:-1])
        assert (
            peticiones[-1]["message"]["quick_replies"]
            == instagram_bot.RESPUESTAS_CONSENTIMIENTO
        )

    def test_la_etiqueta_de_agente_humano_viaja_en_el_cuerpo(self, peticiones):
        """Es lo que permite el seguimiento fuera de la ventana de 24 h."""
        instagram_bot.enviar_texto(
            IGSID, "¿Cerraste el negocio?", etiqueta=instagram_bot.ETIQUETA_HUMANO
        )
        assert peticiones[0]["messaging_type"] == "MESSAGE_TAG"
        assert peticiones[0]["tag"] == "HUMAN_AGENT"

    def test_el_marcado_se_limpia_antes_de_salir(self, peticiones):
        instagram_bot.enviar_texto(IGSID, "*Belén* tiene 3 alcobas")
        assert peticiones[0]["message"]["text"] == "Belén tiene 3 alcobas"

    def test_el_diagnostico_distingue_token_caducado_de_canal_apagado(self, monkeypatch):
        """Un token vencido deja el canal mudo sin ningún error del lado de VVI."""
        monkeypatch.setattr(
            instagram_bot,
            "_cliente",
            lambda: httpx.Client(
                base_url="https://graph.pruebas/v23.0",
                transport=httpx.MockTransport(
                    lambda p: httpx.Response(401, json={"error": {"message": "expirado"}})
                ),
            ),
        )
        assert instagram_bot.diagnostico()["estado"] == "token_invalido"


class TestSinConfigurar:
    def test_sin_token_no_se_envia_nada(self, monkeypatch):
        """Con el canal apagado, un envío no revienta: devuelve False."""
        monkeypatch.setattr(settings, "instagram_token", "")
        assert settings.tiene_instagram is False
        assert instagram_bot.enviar_texto(IGSID, "hola") is False

    def test_el_token_solo_no_alcanza(self, monkeypatch):
        """Sin app secret el webhook rechaza todo: un canal que solo habla no sirve."""
        monkeypatch.setattr(settings, "instagram_app_secret", "")
        assert settings.tiene_instagram is False

    def test_url_del_webhook(self, monkeypatch):
        monkeypatch.setattr(settings, "dashboard_url", "https://vvi.ejemplo.com/")
        assert instagram_bot.url_webhook() == "https://vvi.ejemplo.com/webhooks/instagram"


# ─────────────────────────── Entrante ───────────────────────────


class TestVerificacion:
    """El handshake sin el cual Meta no deja ni guardar la suscripción."""

    def test_devuelve_el_reto_en_texto_plano(self, cliente):
        r = cliente.get(
            "/webhooks/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )
        assert r.status_code == 200
        assert r.text == "1158201444"

    def test_con_otro_token_no_se_verifica(self, cliente):
        r = cliente.get(
            "/webhooks/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-inventado",
                "hub.challenge": "1158201444",
            },
        )
        assert r.status_code == 403


class TestFirma:
    def test_sin_firma_da_404(self, cliente, enviados):
        r = cliente.post("/webhooks/instagram", json=evento())
        assert r.status_code == 404, "a quien tantea rutas no se le confirma que existe"
        assert enviados == []

    def test_una_firma_de_otro_secreto_da_404(self, cliente, enviados):
        crudo = json.dumps(evento()).encode("utf-8")
        r = entregar(cliente, evento(), firma=firmar(crudo, "otro-secreto"))
        assert r.status_code == 404
        assert enviados == []

    def test_la_firma_se_verifica_sobre_el_cuerpo_crudo(self, cliente, enviados):
        """Volver a serializar el JSON cambia espacios y orden: la firma dejaría
        de coincidir sin que nada lo explique."""
        cuerpo = evento()
        crudo = json.dumps(cuerpo).encode("utf-8")
        otra_serializacion = json.dumps(cuerpo, indent=2).encode("utf-8")

        r = cliente.post(
            "/webhooks/instagram",
            content=otra_serializacion,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": firmar(crudo),
            },
        )
        assert r.status_code == 404

    def test_sin_app_secret_configurado_no_entra_nada(self, cliente, enviados, monkeypatch):
        """Un webhook público sin verificar es una puerta abierta."""
        monkeypatch.setattr(settings, "instagram_app_secret", "")
        assert entregar(cliente, evento(), firma="sha256=loquesea").status_code == 404
        assert enviados == []


class TestFiltros:
    def test_ignora_los_mensajes_de_la_propia_cuenta(self, cliente, enviados):
        """Meta devuelve por webhook lo que sale; sin esto el bot se contesta solo."""
        assert entregar(cliente, evento(eco=True)).status_code == 200
        assert enviados == []

    def test_avisa_cuando_el_mensaje_no_es_texto(self, cliente, enviados):
        """Una foto o un reel compartido no se procesan, pero el silencio se lee
        como que la están ignorando."""
        entregar(cliente, evento(texto=None))
        assert len(enviados) == 1
        assert "texto" in enviados[0][1].lower()

    def test_otro_objeto_no_rompe_nada(self, cliente, enviados):
        """La misma app de Meta sirve Lead Ads, que tiene su propia ruta."""
        cuerpo = evento()
        cuerpo["object"] = "page"
        assert entregar(cliente, cuerpo).status_code == 200
        assert enviados == []

    def test_una_entrada_sin_mensajeria_no_revienta(self, cliente, enviados):
        cuerpo = {
            "object": "instagram",
            "entry": [{"id": "1", "changes": [{"field": "comments"}]}],
        }
        assert entregar(cliente, cuerpo).status_code == 200
        assert enviados == []


class TestIdempotencia:
    def test_el_reintento_no_duplica_la_respuesta(self, cliente, enviados):
        """Meta reenvía si el webhook tarda; el comprador no debe verlo dos veces."""
        for _ in range(3):
            entregar(cliente, evento(mensaje_id="MID-REPE"))
        assert len(enviados) == 1

    def test_mensajes_distintos_si_se_atienden(self, cliente, enviados):
        entregar(cliente, evento(mensaje_id="MID-A"))
        n = len(enviados)
        entregar(cliente, evento(mensaje_id="MID-B"))
        assert len(enviados) > n


class TestListaBlanca:
    """El mismo freno que en WhatsApp, para el día en que Meta apruebe la app."""

    def test_solo_responde_a_las_cuentas_de_la_lista(self, cliente, enviados, monkeypatch):
        monkeypatch.setattr(settings, "instagram_usuarios_prueba", IGSID)

        entregar(cliente, evento(igsid=IGSID))
        assert len(enviados) == 1

        entregar(cliente, evento(mensaje_id="OTRO", igsid="17841499999999999"))
        assert len(enviados) == 1, "a una cuenta fuera de la lista no se le contesta"

    def test_la_lista_admite_el_usuario_que_el_operador_conoce(
        self, cliente, enviados, monkeypatch
    ):
        """El operador sabe el @usuario; el IGSID no lo ha visto en su vida."""
        monkeypatch.setattr(settings, "instagram_usuarios_prueba", "@Andres.Prueba")
        entregar(cliente, evento(igsid=IGSID))
        assert len(enviados) == 1

    def test_al_ajeno_ni_se_le_contesta_ni_se_le_guarda(self, cliente, enviados, monkeypatch):
        """El silencio es deliberado: un 'no autorizado' ya sería contestarle."""
        from app.db import sesion
        from app.services import leads

        monkeypatch.setattr(settings, "instagram_usuarios_prueba", "@otra.cuenta")
        ajeno = "17841400000099999"
        entregar(cliente, evento(igsid=ajeno))

        assert enviados == []
        assert not conversacion._PENDIENTES
        with sesion() as db:
            assert leads.buscar_por_canal(db, "instagram", ajeno) is None

    def test_sin_lista_responde_a_todos(self, cliente, enviados, monkeypatch):
        """Vacía es el modo producción: el canal atiende a quien escriba."""
        monkeypatch.setattr(settings, "instagram_usuarios_prueba", "")
        entregar(cliente, evento(igsid="17841400000088888"))
        assert len(enviados) == 1

    def test_normaliza_la_arroba_y_las_mayusculas(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_usuarios_prueba", "@InmoDemo, socio.Pruebas ")
        assert ajustes.usuarios_prueba() == {"inmodemo", "socio.pruebas"}


class TestConsentimiento:
    def test_el_primer_mensaje_pide_autorizacion(self, cliente, enviados, igsid):
        entregar(cliente, evento("Hola, busco apartamento", igsid=igsid))
        assert len(enviados) == 1
        destino, texto = enviados[0]
        assert destino == igsid
        assert "autoriz" in texto.lower()

    def test_el_igsid_no_se_guarda_antes_del_consentimiento(self, cliente, enviados, igsid):
        """RF-19: sin autorización, el identificador no entra a la base de datos."""
        from app.db import sesion
        from app.services import leads

        entregar(cliente, evento("Hola", igsid=igsid))
        with sesion() as db:
            assert leads.buscar_por_canal(db, "instagram", igsid) is None

    def test_el_pendiente_vive_como_indice_ciego(self, cliente, enviados, igsid):
        """El IGSID identifica a una persona igual que un teléfono."""
        entregar(cliente, evento("Hola", igsid=igsid))
        crudos = {cid for _, cid in conversacion._PENDIENTES}
        assert crudos
        assert igsid not in crudos

    def test_el_boton_de_si_da_de_alta_al_prospecto(self, cliente, enviados, igsid):
        """Aquí sí hay botones: un toque se equivoca menos que un 'sí' escrito."""
        from app.db import sesion
        from app.services import leads
        from app.services.compliance import tiene_consentimiento_vigente

        entregar(cliente, evento("Hola", mensaje_id="C1", igsid=igsid))
        entregar(cliente, evento(None, mensaje_id="C2", igsid=igsid, boton="consent:si"))

        with sesion() as db:
            p = leads.buscar_por_canal(db, "instagram", igsid)
            assert p is not None
            assert tiene_consentimiento_vigente(p)
            assert p.canal == "instagram"
            assert p.usuario_canal == "andres.prueba"

    def test_el_boton_de_no_cierra_sin_guardar_nada(self, cliente, enviados, igsid):
        from app.db import sesion
        from app.services import leads

        entregar(cliente, evento("Hola", mensaje_id="R1", igsid=igsid))
        entregar(cliente, evento(None, mensaje_id="R2", igsid=igsid, boton="consent:no"))
        with sesion() as db:
            assert leads.buscar_por_canal(db, "instagram", igsid) is None

    def test_el_si_escrito_tambien_vale(self, cliente, enviados, igsid):
        """No todo el mundo pulsa el botón: hay quien contesta escribiendo."""
        from app.db import sesion
        from app.services import leads
        from app.services.compliance import tiene_consentimiento_vigente

        entregar(cliente, evento("Hola", mensaje_id="E1", igsid=igsid))
        entregar(cliente, evento("Sí, autorizo", mensaje_id="E2", igsid=igsid))
        with sesion() as db:
            p = leads.buscar_por_canal(db, "instagram", igsid)
            assert p is not None and tiene_consentimiento_vigente(p)


class TestAyudantes:
    def test_texto_de_mensaje(self):
        assert instagram_bot.texto_de_mensaje({"text": " hola "}) == "hola"
        assert instagram_bot.texto_de_mensaje({"text": "   "}) is None
        assert instagram_bot.texto_de_mensaje({"attachments": [{"type": "image"}]}) is None

    def test_una_respuesta_a_una_historia_es_un_mensaje_de_texto(self):
        """Es de los mejores inbound que da la red: se atiende como un DM normal."""
        mensaje = {"mid": "m", "text": "¿Todavía está?", "reply_to": {"story": {"id": "s1"}}}
        assert instagram_bot.texto_de_mensaje(mensaje) == "¿Todavía está?"

    def test_respuesta_rapida(self):
        assert (
            instagram_bot.respuesta_rapida({"quick_reply": {"payload": "consent:si"}})
            == "consent:si"
        )
        assert instagram_bot.respuesta_rapida({"text": "hola"}) is None


class TestConexionOAuth:
    """Inicio de sesión de empresa: lo que se declara en el panel de Meta.

    El flujo entero se prueba sin Instagram: la pantalla de consentimiento es una
    URL que se arma, y el canje son dos llamadas HTTP interceptables.
    """

    @pytest.fixture(autouse=True)
    def app_de_instagram(self, monkeypatch):
        monkeypatch.setattr(settings, "instagram_app_id", "999888777")
        monkeypatch.setattr(settings, "dashboard_url", "https://vvi.pruebas")

    def test_las_urls_del_panel_cuelgan_del_host_publico(self):
        """Si no coinciden con lo declarado, el canje falla sin decir por qué."""
        assert instagram_login.url_redireccion() == "https://vvi.pruebas/webhooks/instagram/oauth"
        assert (
            instagram_login.url_desautorizacion()
            == "https://vvi.pruebas/webhooks/instagram/desautorizar"
        )
        assert (
            instagram_login.url_eliminacion()
            == "https://vvi.pruebas/webhooks/instagram/eliminar-datos"
        )

    def test_el_login_manda_a_instagram_con_los_permisos_de_mensajeria(self, cliente):
        r = cliente.get("/webhooks/instagram/login", follow_redirects=False)
        destino = r.headers["location"]
        assert destino.startswith("https://www.instagram.com/oauth/authorize?")
        assert "client_id=999888777" in destino
        assert "instagram_business_manage_messages" in destino
        assert "state=" in destino

    def test_sin_app_id_lo_dice_en_vez_de_mandar_a_una_url_rota(self, cliente, monkeypatch):
        monkeypatch.setattr(settings, "instagram_app_id", "")
        r = cliente.get("/webhooks/instagram/login", follow_redirects=False)
        assert r.status_code == 503
        assert "INSTAGRAM_APP_ID" in r.text

    def test_un_codigo_sin_state_valido_no_se_canjea(self, cliente):
        """Es la barrera anti-CSRF: sin ella se conecta la cuenta de un tercero."""
        r = cliente.get("/webhooks/instagram/oauth?code=AQB123&state=inventado")
        assert r.status_code == 400

    def test_el_state_solo_sirve_una_vez(self, cliente, monkeypatch):
        monkeypatch.setattr(instagram_login, "canjear_codigo", lambda c: {})
        estado = instagram._nuevo_estado()
        assert cliente.get(f"/webhooks/instagram/oauth?code=A&state={estado}").status_code == 502
        assert cliente.get(f"/webhooks/instagram/oauth?code=A&state={estado}").status_code == 400

    def test_el_canje_limpia_el_sufijo_que_pega_instagram(self, monkeypatch):
        """El `#_` del final es la causa clásica de «código inválido»."""
        enviados: list[dict] = []

        def _post(url, data, timeout):
            enviados.append(data)
            return httpx.Response(200, json={"data": [{"access_token": "corto", "user_id": 42}]})

        monkeypatch.setattr(instagram_login.httpx, "post", _post)
        assert instagram_login.canjear_codigo("AQB123#_")["access_token"] == "corto"
        assert enviados[0]["code"] == "AQB123"
        assert enviados[0]["redirect_uri"] == instagram_login.url_redireccion()

    def test_la_pagina_final_entrega_el_token_largo(self, cliente, monkeypatch):
        """El corto dura una hora: dejar ese en el .env es volver mañana."""
        monkeypatch.setattr(
            instagram_login,
            "canjear_codigo",
            lambda c: {"access_token": "corto", "user_id": "17841400000000042", "permissions": ""},
        )
        monkeypatch.setattr(
            instagram_login,
            "alargar_token",
            lambda t: {"access_token": "IGAA-largo", "expires_in": 5184000},
        )
        r = cliente.get(f"/webhooks/instagram/oauth?code=A&state={instagram._nuevo_estado()}")
        assert r.status_code == 200
        assert "IGAA-largo" in r.text and "corto" not in r.text
        assert "60 días" in r.text


class TestPeticionesFirmadas:
    """Cancelación de autorización y eliminación de datos, que Meta exige."""

    def firmar_carga(self, carga: dict, secreto: str = APP_SECRET) -> str:
        import base64

        crudo = base64.urlsafe_b64encode(json.dumps(carga).encode()).decode().rstrip("=")
        firma = hmac.new(secreto.encode(), crudo.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(firma).decode().rstrip("=") + "." + crudo

    def test_una_firma_de_otro_secreto_no_borra_nada(self, cliente):
        """Sin verificar, cualquiera borraría los datos de un tercero con su IGSID."""
        peticion = self.firmar_carga({"user_id": IGSID}, secreto="otro-secreto")
        r = cliente.post("/webhooks/instagram/eliminar-datos", data={"signed_request": peticion})
        assert r.status_code == 404

    def test_la_eliminacion_responde_lo_que_meta_espera(self, cliente, igsid):
        r = cliente.post(
            "/webhooks/instagram/eliminar-datos",
            data={"signed_request": self.firmar_carga({"user_id": igsid})},
        )
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["confirmation_code"]
        assert cuerpo["url"].endswith(cuerpo["confirmation_code"])
        assert cliente.get(cuerpo["url"].replace("http://testserver", "")).status_code == 200

    def test_la_desautorizacion_revoca_al_titular(self, cliente, enviados, igsid):
        from app.db import sesion
        from app.services import leads
        from app.services.compliance import tiene_consentimiento_vigente

        entregar(cliente, evento("Hola", mensaje_id="D1", igsid=igsid))
        entregar(cliente, evento(None, mensaje_id="D2", igsid=igsid, boton="consent:si"))
        cliente.post(
            "/webhooks/instagram/desautorizar",
            data={"signed_request": self.firmar_carga({"user_id": igsid})},
        )
        with sesion() as db:
            p = leads.buscar_por_canal(db, "instagram", igsid)
            assert p is None or not tiene_consentimiento_vigente(p)

    def test_el_canje_usa_el_secreto_de_la_app_de_instagram(self, monkeypatch):
        """El id de Instagram y su secreto van en pareja: mezclarlos no canjea."""
        monkeypatch.setattr(settings, "instagram_login_app_secret", "secreto-de-instagram")
        enviados: list[dict] = []
        monkeypatch.setattr(
            instagram_login.httpx,
            "post",
            lambda url, data, timeout: (
                enviados.append(data),
                httpx.Response(200, json={"access_token": "corto"}),
            )[1],
        )
        instagram_login.canjear_codigo("AQB")
        assert enviados[0]["client_secret"] == "secreto-de-instagram"

    def test_sin_secreto_propio_se_reutiliza_el_de_la_app_de_meta(self, monkeypatch):
        """Meta muestra el mismo valor en las dos pantallas en muchas apps."""
        monkeypatch.setattr(settings, "instagram_login_app_secret", "")
        assert settings.secreto_login_instagram == APP_SECRET


class TestSecretosEmparejados:
    def test_la_firma_vale_con_cualquiera_de_los_dos_secretos(self, monkeypatch):
        """La cancelación la firma Meta con el secreto del producto que la emite."""
        import base64

        monkeypatch.setattr(settings, "instagram_login_app_secret", "secreto-de-instagram")
        for secreto in (APP_SECRET, "secreto-de-instagram"):
            crudo = base64.urlsafe_b64encode(json.dumps({"user_id": "7"}).encode())
            crudo = crudo.decode().rstrip("=")
            firma = hmac.new(secreto.encode(), crudo.encode(), hashlib.sha256).digest()
            peticion = base64.urlsafe_b64encode(firma).decode().rstrip("=") + "." + crudo
            assert instagram_login.datos_firmados(peticion) == {"user_id": "7"}

    def test_un_tercero_sigue_sin_poder_firmar(self, monkeypatch):
        import base64

        monkeypatch.setattr(settings, "instagram_login_app_secret", "secreto-de-instagram")
        crudo = base64.urlsafe_b64encode(json.dumps({"user_id": "7"}).encode()).decode().rstrip("=")
        firma = hmac.new(b"ninguno-de-los-dos", crudo.encode(), hashlib.sha256).digest()
        peticion = base64.urlsafe_b64encode(firma).decode().rstrip("=") + "." + crudo
        assert instagram_login.datos_firmados(peticion) == {}
