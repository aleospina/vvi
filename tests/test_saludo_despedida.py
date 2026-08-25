"""Cortesía y cierre de conversación en el bot.

Tres reglas que se prueban aquí:

  1. Un saludo a secas se contesta saludando, no con un formulario.
  2. Una despedida o un "gracias" final se contestan despidiéndose, y ahí
     termina el turno: no se le devuelve el catálogo a quien dijo adiós.
  3. Despedirse NO parte la ficha en dos. La única conversación que se cierra
     de verdad es la del lead vendido, y lo que venga después es otro lead.

La tercera es la que de verdad importa, y es del operador: una ficha con la
venta registrada no puede seguir engordando con la búsqueda siguiente.
"""

from __future__ import annotations

import itertools

import pytest

from app.channels import conversacion, gateway
from app.llm.prompts import PLANTILLAS
from app.services import leads
from app.services.nlu_engine import es_despedida, es_saludo

CANAL = "telegram"


# ─────────────────────────── Detección ───────────────────────────


class TestReconocerSaludo:
    @pytest.mark.parametrize(
        "texto",
        [
            "hola",
            "Hola",
            "¡Hola!",
            "hola, ¿qué tal?",
            "buenos días",
            "Buenas tardes",
            "buenas noches",
            "buenas",
            "Hola 👋",
            "Hola, buenos días",
            "quiubo",
            # Quien vuelve tras despedirse saluda así.
            "Hola de nuevo",
            "hola otra vez",
        ],
    )
    def test_saluda(self, texto):
        assert es_saludo(texto)

    @pytest.mark.parametrize(
        "texto",
        [
            # Un saludo con la pregunta pegada es una pregunta: tratarlo como
            # saludo se traga lo único que el comprador quería preguntar.
            "hola, busco apartamento",
            "Buenos días, ¿tienen lotes en Pereira?",
            "casas en Medellín",
            "sí",
            "listo",
            "",
            "👋",
        ],
    )
    def test_no_saluda(self, texto):
        assert not es_saludo(texto)


class TestReconocerDespedida:
    @pytest.mark.parametrize(
        "texto",
        [
            "gracias",
            "Muchas gracias",
            "mil gracias",
            "Gracias por todo",
            "hasta pronto",
            "Hasta luego",
            "adiós",
            "chao",
            "nos vemos",
            "listo, gracias",
            "no gracias",
            "eso es todo, gracias",
            "Muchas gracias, hasta luego",
        ],
    )
    def test_se_despide(self, texto):
        assert es_despedida(texto)

    @pytest.mark.parametrize(
        "texto",
        [
            # "Gracias" al principio de una pregunta es cortesía, no un adiós.
            "gracias, ¿y cuánto vale el de Laureles?",
            "gracias, muéstrame los de Pereira",
            # Ya compró: eso lo lee el seguimiento, no la despedida.
            "ya compramos, gracias",
            # En la puerta del consentimiento, "ok" es un sí y no un adiós.
            "ok",
            "listo",
            "sí",
            "no",
        ],
    )
    def test_no_se_despide(self, texto):
        assert not es_despedida(texto)


# ─────────────────────── El turno, con consentimiento ───────────────────────


class TestSaludoEnElTurno:
    def test_el_bot_saluda_de_vuelta(self, db, prospecto_consentido):
        r = gateway.procesar(db, prospecto_consentido, "Hola")
        assert len(r.textos) == 1
        assert r.textos[0].startswith("¡Hola!")
        assert not r.matches

    def test_saludar_no_cierra_nada(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Buenos días")
        assert not gateway.conversacion_cerrada(prospecto_consentido)

    def test_a_quien_ya_conocemos_le_propone_retomar(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Busco apartamento en Medellín")
        r = gateway.procesar(db, prospecto_consentido, "Hola")
        assert "Hola de nuevo" in r.textos[0]
        assert "apartamentos en Medellín" in r.textos[0]

    def test_el_saludo_queda_en_el_historial(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Hola")
        historial = leads.historial(prospecto_consentido)
        assert historial[-2]["texto"] == "Hola"
        assert historial[-1]["direccion"] == "saliente"

    def test_saludar_con_pregunta_pegada_sigue_siendo_pregunta(
        self, db, prospecto_consentido
    ):
        """El caso que no puede romperse: la búsqueda manda sobre la cortesía."""
        r = gateway.procesar(db, prospecto_consentido, "Hola, busco casa en Pereira")
        assert r.matches


class TestDespedidaEnElTurno:
    def test_el_bot_se_despide(self, db, prospecto_consentido):
        r = gateway.procesar(db, prospecto_consentido, "Muchas gracias, hasta luego")
        assert r.textos == [PLANTILLAS["despedida"]]
        assert not r.matches, "a quien dijo adiós no se le devuelve el catálogo"

    def test_no_promete_una_autorizacion_nueva(self, db, prospecto_consentido):
        """La ficha sigue siendo la misma, así que la despedida no puede decir
        que al volver se le pedirá permiso otra vez: no ocurre."""
        r = gateway.procesar(db, prospecto_consentido, "Chao")
        assert "autoriza" not in r.textos[0].lower()

    def test_despedirse_no_parte_la_ficha(self, db, prospecto_consentido):
        """Un "gracias" y volver al rato es la misma conversación y el mismo lead."""
        gateway.procesar(db, prospecto_consentido, "gracias")
        assert not gateway.conversacion_cerrada(prospecto_consentido)

        r = gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira")
        assert not r.pide_consentimiento
        assert r.matches, "retoma donde lo dejó, sin volver a pedir permiso"

    def test_despedirse_suelta_el_foco(self, db, prospecto_consentido):
        """El recorte a un inmueble sí muere con la despedida.

        Quien vuelva mañana no debería encontrarse la cartera acotada por algo
        que ya no recuerda haber pedido.
        """
        prospecto_consentido.foco = "ferreteria la reforma"
        db.flush()
        gateway.procesar(db, prospecto_consentido, "gracias")
        assert prospecto_consentido.foco is None

    def test_no_revoca_el_consentimiento_archivado(self, db, prospecto_consentido):
        """Despedirse no es ejercer el derecho de supresión: eso es /borrar."""
        gateway.procesar(db, prospecto_consentido, "adiós")
        assert prospecto_consentido.consentimiento
        assert prospecto_consentido.nombre

    def test_un_gracias_con_pregunta_se_contesta(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira")
        r = gateway.procesar(db, prospecto_consentido, "Gracias, ¿y cuánto vale?")
        assert PLANTILLAS["despedida"] not in r.textos

    def test_declarar_que_ya_compro_no_es_despedirse(self, db, prospecto_consentido):
        """'Ya compramos, gracias' habla del negocio, y eso lo lee el seguimiento.

        Si la cortesía se lo comiera, la declaración de cierre —lo único que
        destapa una venta no reportada— se perdería por venir con un gracias.
        """
        r = gateway.procesar(db, prospecto_consentido, "ya compramos, gracias")
        assert PLANTILLAS["despedida"] not in r.textos


# ─────────────────── El ciclo completo, canal incluido ───────────────────

#: La base de los tests de canal es la del conftest, compartida: cada test
#: necesita su propio identificador o hereda el prospecto del anterior.
_SECUENCIA = itertools.count(1)


@pytest.fixture()
def cid() -> str:
    return f"99{next(_SECUENCIA):08d}"


@pytest.fixture(autouse=True)
def base_lista():
    from app.db import inicializar

    inicializar(seed=False)
    conversacion._PENDIENTES.clear()
    yield
    conversacion._PENDIENTES.clear()


def _autorizar(cid: str) -> None:
    conversacion.turno(CANAL, cid, "Hola")  # aviso de IA + solicitud
    conversacion.turno(CANAL, cid, "Sí, autorizo")


class TestCicloDeConversacion:
    def test_al_despedirse_el_bot_se_despide(self, cid):
        _autorizar(cid)
        textos = conversacion.turno(CANAL, cid, "Muchas gracias, hasta luego")
        assert textos == [PLANTILLAS["despedida"]]

    def test_volver_a_escribir_retoma_el_mismo_hilo(self, cid):
        """Lo que el operador ve: una sola ficha, no dos trozos del mismo señor.

        Despedirse y volver no puede pedir permiso otra vez ni abrir un lead:
        anunciarlo y no hacerlo era peor todavía, porque el mensaje prometía un
        corte que en la ficha no existía.
        """
        from app.db import sesion

        _autorizar(cid)
        conversacion.turno(CANAL, cid, "Busco apartamento en Medellín")
        conversacion.turno(CANAL, cid, "gracias")

        textos = conversacion.turno(CANAL, cid, "Hola de nuevo")
        assert "¿Autorizas?" not in textos[0]
        assert not conversacion.esta_pendiente(CANAL, cid)

        with sesion() as db:
            fichas = leads.buscar_todos_por_canal(db, CANAL, cid)
            assert len(fichas) == 1
            assert len(fichas[0].consentimientos) == 1

    def test_la_busqueda_sigue_atendiendose_despues_del_adios(self, cid):
        _autorizar(cid)
        conversacion.turno(CANAL, cid, "chao")

        textos = conversacion.turno(CANAL, cid, "Busco apartamento en Medellín")
        assert "¿Autorizas?" not in textos[0]

    def test_a_quien_llega_por_primera_vez_si_le_pregunta_todo(self, cid):
        textos = conversacion.turno(CANAL, cid, "Hola")
        assert "¿Autorizas?" in textos[0]
        textos = conversacion.turno(CANAL, cid, "Sí")
        assert textos[-1] == PLANTILLAS["calificacion"]

    def test_despedirse_en_la_puerta_del_consentimiento_tambien_cierra(self, cid):
        """A quien dice 'gracias, chao' no se le insiste con la autorización."""
        conversacion.turno(CANAL, cid, "Hola")
        textos = conversacion.turno(CANAL, cid, "no gracias")
        assert textos == [PLANTILLAS["despedida"]]
        assert not conversacion.esta_pendiente(CANAL, cid)


# ─────────────────────── Un lead vendido no sigue creciendo ───────────────────────


class TestLeadVendido:
    """Confirmar la venta cierra la conversación; volver a escribir abre otro lead.

    Encontrado en producción: LEAD-000001 quedó en `vendido` y el bot le seguía
    colgando mensajes. Una ficha cerrada que engorda es una ficha en la que el
    operador ya no mira, y la búsqueda nueva quedaba sin poder atribuirse.
    """

    def test_vender_cierra_la_conversacion(self, db, prospecto_consentido):
        prospecto_consentido.estado = "vendido"
        db.flush()
        assert gateway.conversacion_cerrada(prospecto_consentido)

    def test_ya_no_se_le_cuelgan_mensajes(self, db, prospecto_consentido):
        prospecto_consentido.estado = "vendido"
        db.flush()
        antes = len(prospecto_consentido.mensajes)

        r = gateway.procesar(db, prospecto_consentido, "Busco lote en Pereira")

        assert r.pide_consentimiento
        assert not r.matches
        assert len(prospecto_consentido.mensajes) == antes, "la ficha cerrada no crece"

    def test_volver_a_escribir_abre_un_lead_nuevo(self, cid):
        from app.db import sesion

        _autorizar(cid)
        with sesion() as db:
            vendido = leads.buscar_por_canal(db, CANAL, cid)
            vendido.estado = "vendido"
            codigo_vendido = vendido.codigo

        conversacion.turno(CANAL, cid, "Hola, busco otra cosa")
        conversacion.turno(CANAL, cid, "Sí")

        with sesion() as db:
            fichas = leads.buscar_todos_por_canal(db, CANAL, cid)
            assert len(fichas) == 2
            assert fichas[0].codigo == codigo_vendido
            assert fichas[0].estado == "vendido"
            # El vigente es el nuevo, y arranca limpio: su búsqueda es otra.
            actual = leads.buscar_por_canal(db, CANAL, cid)
            assert actual.codigo != codigo_vendido
            assert actual.estado == "nuevo"
            assert actual.ciudad is None

    def test_la_venta_registrada_no_se_toca(self, cid):
        """La ficha cerrada conserva su estado, su comisión y su atribución."""
        from app.db import sesion

        _autorizar(cid)
        with sesion() as db:
            leads.buscar_por_canal(db, CANAL, cid).estado = "vendido"

        conversacion.turno(CANAL, cid, "Hola")
        conversacion.turno(CANAL, cid, "Sí")
        conversacion.turno(CANAL, cid, "Busco lote en Pereira")

        with sesion() as db:
            vendido = leads.buscar_todos_por_canal(db, CANAL, cid)[0]
            assert vendido.estado == "vendido"
            assert vendido.ciudad is None, "la búsqueda nueva no se le cuelga encima"

    def test_borrar_alcanza_tambien_al_lead_vendido(self, cid):
        """Un borrado que dejara la ficha vendida intacta no sería un borrado."""
        from app.db import sesion

        _autorizar(cid)
        with sesion() as db:
            leads.buscar_por_canal(db, CANAL, cid).estado = "vendido"

        conversacion.turno(CANAL, cid, "Hola")
        conversacion.turno(CANAL, cid, "Sí")
        conversacion.borrar_datos(CANAL, cid)

        with sesion() as db:
            assert leads.buscar_por_canal(db, CANAL, cid) is None
