"""Cortesía y cierre de conversación en el bot.

Tres reglas que se prueban aquí:

  1. Un saludo a secas se contesta saludando, no con un formulario.
  2. Una despedida o un "gracias" final cierran la conversación y se anuncia.
  3. Volver a escribir después de esa despedida es una conversación nueva, y
     empieza por donde empiezan todas: pidiendo la autorización de datos.

La tercera es la que de verdad importa: es una regla de cumplimiento, no de
cortesía, y por eso el cierre se guarda en la base y no en memoria.
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
    def test_el_bot_anuncia_que_cierra(self, db, prospecto_consentido):
        r = gateway.procesar(db, prospecto_consentido, "Muchas gracias, hasta luego")
        assert r.textos == [PLANTILLAS["despedida"]]
        assert r.conversacion_cerrada

    def test_el_cierre_queda_guardado(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Chao")
        assert gateway.conversacion_cerrada(prospecto_consentido)
        assert prospecto_consentido.conversacion_cerrada_ts is not None

    def test_despedirse_suelta_el_foco(self, db, prospecto_consentido):
        """El recorte a un inmueble pertenece a la conversación que termina."""
        prospecto_consentido.foco = "ferreteria la reforma"
        db.flush()
        gateway.procesar(db, prospecto_consentido, "gracias")
        assert prospecto_consentido.foco is None

    def test_no_revoca_el_consentimiento_archivado(self, db, prospecto_consentido):
        """Despedirse no es ejercer el derecho de supresión: eso es /borrar."""
        gateway.procesar(db, prospecto_consentido, "adiós")
        assert prospecto_consentido.consentimiento
        assert prospecto_consentido.nombre

    def test_no_cierra_con_un_gracias_que_trae_pregunta(self, db, prospecto_consentido):
        gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira")
        r = gateway.procesar(db, prospecto_consentido, "Gracias, ¿y cuánto vale?")
        assert not r.conversacion_cerrada
        assert not gateway.conversacion_cerrada(prospecto_consentido)

    def test_declarar_que_ya_compro_no_es_despedirse(self, db, prospecto_consentido):
        """'Ya compramos, gracias' habla del negocio, y eso lo lee el seguimiento.

        Si la cortesía se lo comiera, la declaración de cierre —lo único que
        destapa una venta no reportada— se perdería por venir con un gracias.
        """
        r = gateway.procesar(db, prospecto_consentido, "ya compramos, gracias")
        assert not r.conversacion_cerrada
        assert PLANTILLAS["despedida"] not in r.textos

    def test_con_la_conversacion_cerrada_no_se_atiende(self, db, prospecto_consentido):
        """Backstop para quien entre por la API sin pasar por el canal."""
        gateway.procesar(db, prospecto_consentido, "gracias")
        r = gateway.procesar(db, prospecto_consentido, "Busco casa en Pereira")
        assert r.pide_consentimiento
        assert not r.matches


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
    def test_al_despedirse_se_cierra_y_se_avisa(self, cid):
        _autorizar(cid)
        textos = conversacion.turno(CANAL, cid, "Muchas gracias, hasta luego")
        assert textos == [PLANTILLAS["despedida"]]

    def test_volver_a_escribir_vuelve_a_pedir_la_autorizacion(self, cid):
        """El requisito de fondo: cada conversación nueva pide permiso otra vez."""
        _autorizar(cid)
        conversacion.turno(CANAL, cid, "gracias")

        textos = conversacion.turno(CANAL, cid, "Hola de nuevo")
        assert len(textos) == 1
        assert "¿Autorizas?" in textos[0]
        assert conversacion.esta_pendiente(CANAL, cid)

    def test_no_se_atiende_la_busqueda_hasta_que_vuelva_a_autorizar(self, cid):
        _autorizar(cid)
        conversacion.turno(CANAL, cid, "chao")

        textos = conversacion.turno(CANAL, cid, "Busco apartamento en Medellín")
        assert "¿Autorizas?" in textos[0]

    def test_al_autorizar_de_nuevo_la_conversacion_se_reabre(self, cid):
        _autorizar(cid)
        conversacion.turno(CANAL, cid, "hasta pronto")
        conversacion.turno(CANAL, cid, "Hola")
        conversacion.turno(CANAL, cid, "Sí")

        textos = conversacion.turno(CANAL, cid, "Hola")
        assert "Hola" in textos[0]
        assert "¿Autorizas?" not in textos[0]

    def test_la_segunda_autorizacion_queda_archivada(self, cid):
        """Dos conversaciones, dos consentimientos: la evidencia es por sesión."""
        from app.db import sesion

        _autorizar(cid)
        conversacion.turno(CANAL, cid, "gracias")
        conversacion.turno(CANAL, cid, "Hola")
        conversacion.turno(CANAL, cid, "Sí")

        with sesion() as db:
            p = leads.buscar_por_canal(db, CANAL, cid)
            assert len(p.consentimientos) == 2
            assert p.conversacion_cerrada_ts is None

    def test_reabrir_conserva_lo_que_ya_nos_habia_contado(self, cid):
        """Empezar de cero no es tratarlo como un desconocido."""
        from app.db import sesion

        _autorizar(cid)
        conversacion.turno(CANAL, cid, "Busco apartamento en Medellín")
        conversacion.turno(CANAL, cid, "gracias")
        conversacion.turno(CANAL, cid, "Hola")
        conversacion.turno(CANAL, cid, "Sí")

        with sesion() as db:
            p = leads.buscar_por_canal(db, CANAL, cid)
            assert p.ciudad == "Medellín"
            assert p.tipo == "apartamento"

    def test_al_reabrir_no_le_pregunta_lo_que_ya_sabe(self, cid):
        """La autorización es nueva; su búsqueda no. No se pregunta desde cero."""
        _autorizar(cid)
        conversacion.turno(CANAL, cid, "Busco apartamento en Medellín")
        conversacion.turno(CANAL, cid, "gracias")
        conversacion.turno(CANAL, cid, "Hola")
        textos = conversacion.turno(CANAL, cid, "Sí")

        assert "apartamentos en Medellín" in textos[-1]

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
