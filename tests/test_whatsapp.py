"""Canal WhatsApp vía Evolution API (ADR-02b).

Todo corre sin instancia pareada y sin red: el webhook es un POST con JSON, así
que el flujo completo —consentimiento, filtros, deduplicación, respuesta— se
verifica con payloads reales de Evolution y el cliente HTTP interceptado. Solo
el pareo del número necesita WhatsApp de verdad.
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from app.channels import conversacion, whatsapp_evo
from app.config import settings
from app.routers import whatsapp

TOKEN = "token-de-pruebas-del-webhook"
NUMERO = "573001234567"
JID = f"{NUMERO}@s.whatsapp.net"


@pytest.fixture(autouse=True)
def canal_configurado(monkeypatch):
    """Deja el canal 'configurado' sin que exista Evolution en ningún lado."""
    monkeypatch.setattr(settings, "evolution_url", "http://evolution.pruebas")
    monkeypatch.setattr(settings, "evolution_api_key", "apikey-de-pruebas")
    monkeypatch.setattr(settings, "evolution_webhook_token", TOKEN)
    whatsapp._VISTOS.clear()
    conversacion._PENDIENTES.clear()
    yield
    whatsapp._VISTOS.clear()
    conversacion._PENDIENTES.clear()


@pytest.fixture()
def enviados(monkeypatch):
    """Captura lo que el bot habría mandado por WhatsApp."""
    salidas: list[tuple[str, str]] = []

    def _falso_enviar(numero: str, texto: str) -> bool:
        salidas.append((numero, texto))
        return True

    monkeypatch.setattr(whatsapp_evo, "enviar_texto", _falso_enviar)
    monkeypatch.setattr(whatsapp_evo, "escribiendo", lambda *a, **k: None)
    return salidas


@pytest.fixture()
def cliente():
    from app.main import app

    with TestClient(app) as c:
        yield c


#: Los tests que llegan a la base comparten el archivo temporal del conftest, así
#: que cada uno necesita su propio número o el prospecto del anterior lo altera.
_SECUENCIA = itertools.count(1)


@pytest.fixture()
def numero() -> str:
    return f"5730000{next(_SECUENCIA):05d}"


def evento(texto="Hola", *, mensaje_id="MSG-1", from_me=False, jid=None, numero=NUMERO) -> dict:
    """Payload `messages.upsert` con la forma que envía Evolution API v2."""
    return {
        "event": "messages.upsert",
        "instance": "vvi",
        "apikey": "apikey-de-pruebas",
        "data": {
            "key": {
                "remoteJid": jid or f"{numero}@s.whatsapp.net",
                "fromMe": from_me,
                "id": mensaje_id,
            },
            "pushName": "Andrés Prueba",
            "messageType": "conversation",
            "message": {"conversation": texto} if texto is not None else {},
            "messageTimestamp": 1735689600,
        },
    }


class TestAutenticacion:
    def test_token_incorrecto_da_404(self, cliente, enviados):
        r = cliente.post("/webhooks/whatsapp/token-inventado", json=evento())
        assert r.status_code == 404
        assert enviados == []

    def test_apikey_que_no_corresponde_da_404(self, cliente, enviados, monkeypatch):
        """La ruta secreta sola no basta: el cuerpo también se verifica."""
        monkeypatch.setattr(
            whatsapp_evo, "token_instancia", lambda refrescar=False: "token-de-la-instancia"
        )
        cuerpo = evento()
        cuerpo["apikey"] = "otra-cosa"
        r = cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=cuerpo)
        assert r.status_code == 404
        assert enviados == []

    def test_acepta_el_token_propio_de_la_instancia(self, cliente, enviados, monkeypatch):
        """Evolution firma los eventos con el token de la instancia, no con la clave global.

        Comparar solo contra la global devuelve 404 a todo y el canal queda mudo
        sin que en VVI aparezca ningún error: es el fallo que se vio en el
        primer arranque real.
        """
        monkeypatch.setattr(
            whatsapp_evo, "token_instancia", lambda refrescar=False: "token-de-la-instancia"
        )
        cuerpo = evento()
        cuerpo["apikey"] = "token-de-la-instancia"
        r = cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=cuerpo)
        assert r.status_code == 200
        assert len(enviados) == 1

    def test_si_la_instancia_se_recreo_refresca_el_token(self, cliente, enviados, monkeypatch):
        """Recrear la instancia le cambia el token; el cacheado queda obsoleto.

        Sin el refresco, el canal queda mudo durante todo el TTL de la caché y
        en los logs de VVI no aparece nada: el 404 solo se ve en Evolution.
        """
        llamadas: list[bool] = []

        def _token(refrescar: bool = False) -> str:
            llamadas.append(refrescar)
            return "token-nuevo" if refrescar else "token-viejo"

        monkeypatch.setattr(whatsapp_evo, "token_instancia", _token)
        cuerpo = evento(mensaje_id="RECREADA")
        cuerpo["apikey"] = "token-nuevo"

        assert cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=cuerpo).status_code == 200
        assert llamadas == [False, True], "debe reintentar una vez con refresco"
        assert len(enviados) == 1

    def test_sin_apikey_la_ruta_secreta_basta(self, cliente, enviados):
        """No todas las versiones mandan apikey; el token de ruta son 256 bits."""
        cuerpo = evento(mensaje_id="SIN-KEY")
        cuerpo.pop("apikey")
        assert cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=cuerpo).status_code == 200
        assert len(enviados) == 1


class TestFiltros:
    def test_ignora_los_grupos(self, cliente, enviados):
        """Si meten el número a un grupo, el bot no le responde al grupo."""
        r = cliente.post(
            f"/webhooks/whatsapp/{TOKEN}", json=evento(jid="120363000000000000@g.us")
        )
        assert r.status_code == 200
        assert enviados == []

    def test_ignora_los_estados(self, cliente, enviados):
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(jid="status@broadcast"))
        assert enviados == []

    def test_ignora_lo_que_envio_el_propio_bot(self, cliente, enviados):
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(from_me=True))
        assert enviados == []

    def test_avisa_cuando_el_mensaje_no_es_texto(self, cliente, enviados):
        """Un audio no se procesa, pero tampoco se responde con silencio."""
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(texto=None))
        assert len(enviados) == 1
        assert "texto" in enviados[0][1].lower()

    def test_otros_eventos_no_rompen_nada(self, cliente, enviados):
        r = cliente.post(
            f"/webhooks/whatsapp/{TOKEN}",
            json={"event": "contacts.update", "apikey": "apikey-de-pruebas", "data": {"x": 1}},
        )
        assert r.status_code == 200
        assert enviados == []


class TestListaBlanca:
    """Modo pruebas: lo que hace viable vincular un número personal."""

    def test_solo_responde_a_los_numeros_de_la_lista(self, cliente, enviados, monkeypatch):
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")

        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(numero="573001234567"))
        assert len(enviados) == 1

        cliente.post(
            f"/webhooks/whatsapp/{TOKEN}",
            json=evento(mensaje_id="OTRO", numero="573159998888"),
        )
        assert len(enviados) == 1, "a un número fuera de la lista no se le contesta"

    def test_al_ajeno_ni_se_le_contesta_ni_se_le_guarda(self, cliente, enviados, monkeypatch):
        """El silencio es deliberado: un 'no autorizado' ya sería contestarle."""
        from app.db import sesion
        from app.services import leads

        monkeypatch.setattr(settings, "evolution_numeros_prueba", "573001234567")
        ajeno = "573159990000"
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(numero=ajeno))

        assert enviados == []
        assert not conversacion._PENDIENTES
        with sesion() as db:
            assert leads.buscar_por_canal(db, "whatsapp", ajeno) is None

    def test_la_lista_tolera_el_formato_que_escribe_cualquiera(self, monkeypatch):
        monkeypatch.setattr(
            settings, "evolution_numeros_prueba", "+57 300 123 4567, 57-310-987-6543"
        )
        assert settings.numeros_prueba == {"573001234567", "573109876543"}

    def test_sin_lista_responde_a_todos(self, cliente, enviados, monkeypatch):
        """Vacía es el modo producción: el canal atiende a quien escriba."""
        monkeypatch.setattr(settings, "evolution_numeros_prueba", "")
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(numero="573159991111"))
        assert len(enviados) == 1


class TestIdempotencia:
    def test_el_reintento_no_duplica_la_respuesta(self, cliente, enviados):
        """Evolution reenvía si el webhook tarda; el comprador no debe verlo dos veces."""
        for _ in range(3):
            cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(mensaje_id="MSG-REPE"))
        assert len(enviados) == 1

    def test_mensajes_distintos_si_se_atienden(self, cliente, enviados):
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(mensaje_id="MSG-A"))
        n = len(enviados)
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(mensaje_id="MSG-B"))
        assert len(enviados) > n


class TestConsentimiento:
    def test_el_primer_mensaje_pide_autorizacion(self, cliente, enviados, numero):
        cliente.post(
            f"/webhooks/whatsapp/{TOKEN}",
            json=evento("Hola, busco apartamento", numero=numero),
        )
        assert len(enviados) == 1
        destino, texto = enviados[0]
        assert destino == numero
        assert "autoriz" in texto.lower()

    def test_el_numero_no_se_guarda_antes_del_consentimiento(self, cliente, enviados, numero):
        """RF-19: sin autorización, el teléfono no entra a la base de datos."""
        from app.db import sesion
        from app.services import leads

        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("Hola", numero=numero))
        with sesion() as db:
            assert leads.buscar_por_canal(db, "whatsapp", numero) is None

    def test_el_pendiente_vive_como_indice_ciego(self, cliente, enviados, numero):
        """El número en espera es dato personal: ni en memoria queda en claro."""
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("Hola", numero=numero))
        crudos = {cid for _, cid in conversacion._PENDIENTES}
        assert crudos
        assert numero not in crudos

    @pytest.mark.parametrize("respuesta", ["Sí, autorizo", "si", "Acepto", "de acuerdo"])
    def test_el_si_da_de_alta_al_prospecto(self, cliente, enviados, numero, respuesta):
        """En WhatsApp no hay botones fiables: la autorización llega escrita."""
        from app.db import sesion
        from app.services import leads
        from app.services.compliance import tiene_consentimiento_vigente

        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("Hola", mensaje_id="M1", numero=numero))
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento(respuesta, mensaje_id="M2", numero=numero))

        with sesion() as db:
            p = leads.buscar_por_canal(db, "whatsapp", numero)
            assert p is not None
            assert tiene_consentimiento_vigente(p)
            assert p.telefono == numero
            assert p.canal == "whatsapp"

    def test_el_no_cierra_sin_guardar_nada(self, cliente, enviados, numero):
        from app.db import sesion
        from app.services import leads

        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("Hola", mensaje_id="N1", numero=numero))
        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("No", mensaje_id="N2", numero=numero))
        with sesion() as db:
            assert leads.buscar_por_canal(db, "whatsapp", numero) is None

    def test_una_respuesta_ambigua_no_cuenta_como_autorizacion(self, cliente, enviados, numero):
        """La autorización debe ser inequívoca: 'no sé' no es un sí."""
        from app.db import sesion
        from app.services import leads

        cliente.post(f"/webhooks/whatsapp/{TOKEN}", json=evento("Hola", mensaje_id="A1", numero=numero))
        cliente.post(
            f"/webhooks/whatsapp/{TOKEN}",
            json=evento("no sé, tal vez autorizo", mensaje_id="A2", numero=numero),
        )
        with sesion() as db:
            assert leads.buscar_por_canal(db, "whatsapp", numero) is None


class TestAyudantes:
    @pytest.mark.parametrize(
        "jid,esperado",
        [
            ("573001234567@s.whatsapp.net", "573001234567"),
            ("573001234567:12@s.whatsapp.net", "573001234567"),
            ("573001234567", "573001234567"),
        ],
    )
    def test_numero_de_jid(self, jid, esperado):
        assert whatsapp_evo.numero_de_jid(jid) == esperado

    def test_texto_de_mensaje_con_contexto(self):
        """WhatsApp cambia la forma del mensaje cuando hay cita o enlace."""
        assert whatsapp_evo.texto_de_mensaje({"conversation": "hola"}) == "hola"
        assert (
            whatsapp_evo.texto_de_mensaje({"extendedTextMessage": {"text": "hola con cita"}})
            == "hola con cita"
        )
        assert whatsapp_evo.texto_de_mensaje({"audioMessage": {"url": "..."}}) is None
        assert whatsapp_evo.texto_de_mensaje({"conversation": "   "}) is None


class TestSinConfigurar:
    def test_sin_evolution_no_se_envia_nada(self, monkeypatch):
        """Con el canal apagado, un envío no revienta: devuelve False."""
        monkeypatch.setattr(settings, "evolution_url", "")
        assert whatsapp_evo.enviar_texto(NUMERO, "hola") is False
        assert whatsapp_evo.estado_conexion() == "no_configurado"


class TestUrlWebhook:
    def test_por_defecto_cuelga_del_dashboard(self, monkeypatch):
        monkeypatch.setattr(settings, "evolution_webhook_base", "")
        monkeypatch.setattr(settings, "dashboard_url", "https://vvi.ejemplo.com/")
        assert whatsapp_evo.url_webhook() == f"https://vvi.ejemplo.com/webhooks/whatsapp/{TOKEN}"

    def test_la_base_explicita_manda(self, monkeypatch):
        """En desarrollo, para el contenedor `localhost` es el propio contenedor."""
        monkeypatch.setattr(settings, "evolution_webhook_base", "http://host.docker.internal:8000")
        assert whatsapp_evo.url_webhook().startswith("http://host.docker.internal:8000/webhooks/")


# ─────────────────────────── Vista del operador (Fase 3) ───────────────────────────


def _ingresar(usuario: str, clave: str):
    from app.main import app

    cli = TestClient(app, follow_redirects=False)
    cli.__enter__()
    r = cli.post("/dashboard/login", data={"usuario": usuario, "clave": clave})
    assert r.status_code == 303, "el ingreso debía funcionar"
    return cli


@pytest.fixture()
def panel():
    cli = _ingresar(settings.dashboard_user, settings.dashboard_password)
    yield cli
    cli.__exit__(None, None, None)


@pytest.fixture()
def panel_invitado(monkeypatch):
    monkeypatch.setattr(settings, "invitado_user", "invitado")
    monkeypatch.setattr(settings, "invitado_password", "invitado")
    cli = _ingresar("invitado", "invitado")
    yield cli
    cli.__exit__(None, None, None)


class TestVista:
    def test_el_invitado_no_ve_el_qr(self, panel_invitado):
        """El QR es una credencial de sesión: quien lo escanea se lleva el número."""
        for ruta in ("/dashboard/whatsapp", "/dashboard/whatsapp/estado"):
            assert panel_invitado.get(ruta).status_code == 403
        assert panel_invitado.post("/dashboard/whatsapp/vincular").status_code == 403
        assert panel_invitado.post("/dashboard/whatsapp/desvincular").status_code == 403

    def test_sin_sesion_manda_al_ingreso(self):
        from app.main import app

        with TestClient(app, follow_redirects=False) as cli:
            r = cli.get("/dashboard/whatsapp")
            assert r.status_code == 303
            assert r.headers["location"] == "/dashboard/login"

    def test_el_operador_ve_el_estado(self, panel, monkeypatch):
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "open")
        r = panel.get("/dashboard/whatsapp")
        assert r.status_code == 200
        assert "conectado" in r.text
        assert f"/webhooks/whatsapp/{TOKEN}" in r.text

    def test_con_el_canal_conectado_el_boton_sigue_generando_qr(self, panel, monkeypatch):
        """Conectado es justo cuando hace falta: para pasar el bot a otro teléfono.

        Esconder el panel del QR mientras el estado era `open` dejaba el botón
        sin nada que pintar y el JS se salía en la primera línea, porque no
        encontraba el panel. Desde el navegador se veía como un botón muerto.
        """
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "open")
        r = panel.get("/dashboard/whatsapp")
        assert r.status_code == 200
        assert 'id="btn-qr"' in r.text, "el botón debe existir en cualquier estado"
        assert 'id="panel-qr"' in r.text, "sin panel, el script no se engancha"
        assert "Generar QR" in r.text
        assert 'data-conectado="1"' in r.text, "conectado, el botón debe pedir confirmación"
        assert "desvincula el teléfono" in r.text, "hay que advertir lo que cuesta"

    def test_el_boton_de_vincular_es_visible(self, panel, monkeypatch):
        """El botón estuvo invisible en escritorio desde que existe la pantalla.

        Iba envuelto en `acciones-foto`, la clase de los controles que flotan
        sobre una miniatura de la cartera: `position:absolute` y `opacity:0`
        hasta que hay hover sobre la miniatura. Aquí no hay ninguna, así que el
        botón estaba en el DOM —se copiaba con el texto de la página— pero no se
        veía ni se podía pulsar. En móvil sí salía, por `@media (hover:none)`,
        que es lo que hacía el fallo tan difícil de creer.
        """
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "close")
        r = panel.get("/dashboard/whatsapp")
        assert "acciones-foto" not in r.text, "esa clase esconde el botón fuera de una miniatura"
        assert 'id="btn-qr"' in r.text

    def test_con_el_canal_caido_el_boton_no_pregunta_nada(self, panel, monkeypatch):
        """Sin sesión que perder, la confirmación solo sería un clic de más."""
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "close")
        r = panel.get("/dashboard/whatsapp")
        assert 'data-conectado=""' in r.text

    def test_sin_configurar_explica_qué_falta(self, panel, monkeypatch):
        monkeypatch.setattr(settings, "evolution_url", "")
        r = panel.get("/dashboard/whatsapp")
        assert r.status_code == 200
        assert "EVOLUTION_API_KEY" in r.text

    def test_el_sondeo_devuelve_el_estado(self, panel, monkeypatch):
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "connecting")
        assert panel.get("/dashboard/whatsapp/estado").json() == {"estado": "connecting"}

    def test_vincular_pinta_el_qr_y_lo_deja_auditado(self, panel, monkeypatch):
        from app.db import sesion
        from app.models import LogAuditoria

        monkeypatch.setattr(whatsapp_evo, "crear_instancia", lambda: "creada")
        monkeypatch.setattr(whatsapp_evo, "configurar_webhook", lambda: whatsapp_evo.url_webhook())
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "connecting")
        monkeypatch.setattr(
            whatsapp_evo, "qr_de_conexion", lambda: {"base64": "QUJD", "pairingCode": "ABCD-1234"}
        )

        r = panel.post("/dashboard/whatsapp/vincular")
        assert r.status_code == 200
        assert "data:image/png;base64,QUJD" in r.text
        assert "ABCD-1234" in r.text

        with sesion() as db:
            acciones = [x.accion for x in db.query(LogAuditoria).all()]
        assert "whatsapp_vinculacion_iniciada" in acciones

    def test_el_qr_se_pide_en_json_para_renovarlo_sin_recargar(self, panel, monkeypatch):
        """El código vive segundos: la página tiene que poder pedir otro sola.

        Reportado en producción: el operador pulsaba, iba por el teléfono y
        volvía a un QR ya vencido que la vista no sabía reemplazar. Desde fuera
        se veía como que el QR «no cargaba».
        """
        monkeypatch.setattr(whatsapp_evo, "crear_instancia", lambda: "existente")
        monkeypatch.setattr(whatsapp_evo, "configurar_webhook", lambda: whatsapp_evo.url_webhook())
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "connecting")
        monkeypatch.setattr(
            whatsapp_evo, "qr_de_conexion", lambda: {"base64": "QUJD", "pairingCode": "ABCD-1234"}
        )

        d = panel.post("/dashboard/whatsapp/qr").json()
        assert d["qr"] == "data:image/png;base64,QUJD"
        assert d["codigo_pareo"] == "ABCD-1234"
        assert d["estado"] == "connecting"

    def test_el_sondeo_del_qr_no_toca_la_conexion(self, panel, monkeypatch):
        """Refrescar la imagen no puede reiniciar el socket de WhatsApp.

        Encontrado en producción por el peor camino: el teléfono respondía «no
        se pudo vincular el dispositivo» al escanear. La página refrescaba el
        código llamando otra vez a `/instance/connect`, y eso no pide otro
        código —reinicia Baileys entero—, así que tumbaba el emparejamiento a
        media confirmación. Los logs de Evolution mostraban un arranque de socket
        cada 25 segundos, clavado con el sondeo.
        """
        llamadas = []
        monkeypatch.setattr(
            whatsapp_evo, "qr_de_conexion", lambda: llamadas.append("connect") or {}
        )
        monkeypatch.setattr(whatsapp_evo, "crear_instancia", lambda: llamadas.append("crear"))
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "connecting")
        whatsapp_evo.guardar_qr({"base64": "QUJD"})

        d = panel.get("/dashboard/whatsapp/qr-actual").json()
        assert d["qr"] == "data:image/png;base64,QUJD"
        assert d["estado"] == "connecting"
        assert llamadas == [], "el sondeo no puede llamar a Evolution"

    def test_el_qr_del_webhook_alimenta_al_panel(self):
        """El código que Evolution empuja es el que ve el operador."""
        whatsapp_evo.guardar_qr({"qrcode": {"base64": "data:image/png;base64,WFla"}})
        assert whatsapp_evo.ultimo_qr() == "data:image/png;base64,WFla"

    def test_un_qr_viejo_no_se_sirve(self, monkeypatch):
        """Pintar un código vencido es mandar al operador a escanear basura."""
        whatsapp_evo.guardar_qr({"base64": "QUJD"})
        ahora = __import__("time").time()
        monkeypatch.setattr(
            whatsapp_evo.time, "time", lambda: ahora + whatsapp_evo.VIGENCIA_QR_SEG + 1
        )
        assert whatsapp_evo.ultimo_qr() is None

    def test_pedir_el_qr_queda_auditado(self, panel, monkeypatch):
        """Pulsar el botón reinicia la conexión: eso sí se anota."""
        from app.db import sesion
        from app.models import LogAuditoria

        monkeypatch.setattr(whatsapp_evo, "crear_instancia", lambda: "existente")
        monkeypatch.setattr(whatsapp_evo, "configurar_webhook", lambda: whatsapp_evo.url_webhook())
        monkeypatch.setattr(whatsapp_evo, "estado_conexion", lambda: "connecting")
        monkeypatch.setattr(whatsapp_evo, "qr_de_conexion", lambda: {"base64": "QUJD"})

        def cuantas() -> int:
            with sesion() as db:
                return sum(
                    1 for x in db.query(LogAuditoria).all()
                    if x.accion == "whatsapp_vinculacion_iniciada"
                )

        antes = cuantas()
        panel.post("/dashboard/whatsapp/qr")
        assert cuantas() == antes + 1

    def test_el_invitado_tampoco_pide_el_qr_por_json(self, panel_invitado):
        assert panel_invitado.post("/dashboard/whatsapp/qr").status_code == 403
        assert panel_invitado.get("/dashboard/whatsapp/qr-actual").status_code == 403

    def test_si_evolution_no_responde_el_json_lo_dice(self, panel, monkeypatch):
        import httpx

        def _cae() -> str:
            raise httpx.ConnectError("sin ruta al host")

        monkeypatch.setattr(whatsapp_evo, "crear_instancia", _cae)
        d = panel.post("/dashboard/whatsapp/qr").json()
        assert "Evolution" in d["error"]
        assert "qr" not in d

    def test_si_evolution_no_responde_lo_dice_sin_reventar(self, panel, monkeypatch):
        import httpx

        def _cae() -> str:
            raise httpx.ConnectError("sin ruta al host")

        monkeypatch.setattr(whatsapp_evo, "crear_instancia", _cae)
        r = panel.post("/dashboard/whatsapp/vincular")
        assert r.status_code == 200
        assert "Evolution" in r.text
