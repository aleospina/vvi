"""Control de comisión: ventana de protección y seguimiento al comprador (PRD §10).

Los dos mecanismos atacan el mismo riesgo desde lados distintos. La **ventana**
fija por escrito y con fecha que esa presentación genera comisión durante N
meses, aunque el cierre ocurra por fuera. El **seguimiento** pregunta a la única
persona del negocio que no gana nada callándose: el comprador.

Lo que más se cuida aquí es el falso positivo. Una alerta de "cierre no
reportado" contra un asesor honesto cuesta más que la venta que se quería
vigilar, así que ante la duda el sistema no dice nada.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.channels import gateway
from app.config import settings
from app.models import EstadoProspecto, Propiedad, Seguimiento, Solicitud, ahora
from app.services import commission, leads, seguimiento
from app.services.nlu_engine import respuesta_de_cierre


class TestLeerLaDeclaracion:
    """Distinguir "ya compré" de "quiero comprar" es todo el mecanismo."""

    @pytest.mark.parametrize(
        "frase",
        [
            "Sí, ya compramos ese lote la semana pasada",
            "ya cerramos el negocio, gracias",
            "ya firmamos la promesa",
            "listo, ya lo compré",
            "ya escrituramos",
        ],
    )
    def test_declara_el_cierre(self, frase):
        assert respuesta_de_cierre(frase) == "cerro"

    @pytest.mark.parametrize(
        "frase",
        [
            "todavía no he comprado",
            "aún no, sigo mirando",
            "no compré nada por ahora",
            "no cerramos, se cayó el negocio",
            "sigo buscando algo más grande",
            "desistí",
        ],
    )
    def test_declara_que_no(self, frase):
        assert respuesta_de_cierre(frase) == "no_cerro"

    @pytest.mark.parametrize(
        "frase",
        [
            "quiero comprar un lote en Pereira",
            "estoy buscando lote para comprar",
            "¿me puedes mostrar más opciones?",
            "hola",
            "cuánto vale el de La Reforma",
            "quiero agendar una visita",
        ],
    )
    def test_ante_la_duda_no_dice_nada(self, frase):
        """Un falso positivo acusa a un asesor que no ha hecho nada."""
        assert respuesta_de_cierre(frase) is None


class TestVentanaDeProteccion:
    def test_la_presentacion_abre_la_ventana(self, db, prospecto_consentido):
        solicitud = leads.solicitar_handoff(db, prospecto_consentido, tipo="visita")
        db.commit()

        esperado = ahora() + timedelta(days=settings.dias_proteccion)
        assert solicitud.protegido_hasta is not None
        assert abs((solicitud.protegido_hasta - esperado).total_seconds()) < 60
        assert solicitud.proteccion_vigente is True

    def test_una_ventana_vencida_se_ve_vencida(self, db, prospecto_consentido):
        solicitud = leads.solicitar_handoff(db, prospecto_consentido, tipo="visita")
        solicitud.protegido_hasta = ahora() - timedelta(days=1)
        db.commit()

        assert solicitud.proteccion_vigente is False

    def test_la_fecha_no_se_recalcula_al_cambiar_la_politica(
        self, db, prospecto_consentido, monkeypatch
    ):
        """Una fecha que se mueve sola no prueba nada ante el propietario."""
        solicitud = leads.solicitar_handoff(db, prospecto_consentido, tipo="visita")
        db.commit()
        congelada = solicitud.protegido_hasta

        monkeypatch.setattr(settings, "dias_proteccion", 365)
        db.refresh(solicitud)

        assert solicitud.protegido_hasta == congelada


class TestPreguntarleAlComprador:
    @pytest.fixture()
    def presentado(self, db, prospecto_consentido):
        """Un comprador ya presentado al asesor por un inmueble concreto."""
        db.add(
            Propiedad(
                id="LOT-SEG-1", ciudad="Pereira", zona="La Reforma", tipo="lote",
                habitaciones=0, banos=0, area_m2=640, precio=90_000_000,
                estado="disponible", descripcion="Lote donde funciona la ferretería.",
            )
        )
        db.flush()
        # Como en una conversación real: el prospecto se califica y la petición
        # de visita lo empuja a `visita`. Desde `nuevo` el handoff no mueve nada
        # y el escenario no se parecería al que se quiere probar.
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        leads.solicitar_handoff(
            db, prospecto_consentido, tipo="visita", propiedad_id="LOT-SEG-1"
        )
        db.commit()
        return prospecto_consentido

    @staticmethod
    def _envejecer(db, prospecto, dias: int) -> Solicitud:
        solicitud = prospecto.solicitudes[-1]
        solicitud.creado_en = ahora() - timedelta(days=dias)
        db.commit()
        return solicitud

    @staticmethod
    def _correo() -> tuple[list, callable]:
        """Un canal de mentira que anota lo que se le manda."""
        buzon: list = []
        return buzon, lambda prospecto, texto: bool(buzon.append((prospecto.codigo, texto)) is None)

    def test_no_se_pregunta_antes_del_primer_hito(self, db, presentado):
        self._envejecer(db, presentado, 3)
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 0
        assert buzon == []

    def test_a_los_siete_dias_se_pregunta_una_vez(self, db, presentado):
        self._envejecer(db, presentado, 8)
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 1
        assert "La Reforma" in buzon[0][1]

        # Una segunda ronda el mismo día no vuelve a escribirle.
        assert seguimiento.ejecutar(db, enviar=canal) == 0

    def test_varios_hitos_vencidos_son_un_solo_mensaje(self, db, presentado):
        """Si el proceso estuvo caído, no se despacha el atraso de golpe."""
        self._envejecer(db, presentado, 50)
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 1
        db.commit()

        registros = {s.hito: s for s in db.query(Seguimiento).all()}
        assert set(registros) == {7, 21, 45}
        assert registros[45].enviado_en is not None
        assert registros[7].resultado == seguimiento.OMITIDO
        assert registros[21].resultado == seguimiento.OMITIDO

    def test_un_canal_caido_no_consume_el_hito(self, db, presentado):
        """Si el mensaje no salió, el comprador tiene que seguir en la cola."""
        self._envejecer(db, presentado, 8)

        assert seguimiento.ejecutar(db, enviar=lambda p, t: False) == 0
        db.commit()

        buzon, canal = self._correo()
        assert seguimiento.ejecutar(db, enviar=canal) == 1

    def test_no_se_le_pregunta_a_quien_ya_tiene_desenlace(self, db, presentado):
        self._envejecer(db, presentado, 8)
        leads.cambiar_estado(db, presentado, EstadoProspecto.PERDIDO, actor="operador")
        db.commit()
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 0

    def test_no_se_le_pregunta_a_quien_revoco(self, db, presentado):
        from app.services.compliance import revocar_y_anonimizar

        self._envejecer(db, presentado, 8)
        revocar_y_anonimizar(db, presentado, actor="titular")
        db.commit()
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 0

    def test_quien_ya_dijo_que_cerro_no_recibe_mas(self, db, presentado):
        self._envejecer(db, presentado, 50)
        seguimiento.registrar_respuesta(db, presentado, seguimiento.CERRO)
        db.commit()
        buzon, canal = self._correo()

        assert seguimiento.ejecutar(db, enviar=canal) == 0


class TestElCompradorContesta:
    @pytest.fixture()
    def presentado(self, db, prospecto_consentido):
        db.add(
            Propiedad(
                id="LOT-SEG-2", ciudad="Pereira", zona="La Reforma", tipo="lote",
                habitaciones=0, banos=0, area_m2=640, precio=90_000_000,
                estado="disponible", descripcion="Lote donde funciona la ferretería.",
            )
        )
        db.flush()
        # Como en una conversación real: el prospecto se califica y la petición
        # de visita lo empuja a `visita`. Desde `nuevo` el handoff no mueve nada
        # y el escenario no se parecería al que se quiere probar.
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        leads.solicitar_handoff(
            db, prospecto_consentido, tipo="visita", propiedad_id="LOT-SEG-2"
        )
        db.commit()
        return prospecto_consentido

    def test_el_cierre_declarado_corta_el_turno(self, db, presentado):
        """A quien ya compró no se le vuelve a mostrar la cartera."""
        r = gateway.procesar(db, presentado, "Sí, ya compramos ese lote")
        db.commit()

        assert r.cierre_declarado is True
        assert not r.matches, "no hay cartera que mostrarle a quien ya cerró"
        assert len(r.textos) == 1
        assert "felicitaciones" in r.textos[0].lower()

    def test_queda_en_la_lista_que_mira_el_operador(self, db, presentado):
        gateway.procesar(db, presentado, "ya cerramos el negocio")
        db.commit()

        cierres = seguimiento.cierres_declarados(db)
        assert [c.prospecto.codigo for c in cierres] == [presentado.codigo]
        assert cierres[0].solicitud.propiedad_id == "LOT-SEG-2"

    def test_con_la_venta_registrada_la_alerta_desaparece(self, db, presentado):
        """La alerta es "cerró y no lo vimos", no "cerró"."""
        gateway.procesar(db, presentado, "ya compramos")
        db.commit()
        assert seguimiento.cierres_declarados(db)

        commission.confirmar_venta(
            db,
            prospecto=presentado,
            propiedad=db.get(Propiedad, "LOT-SEG-2"),
            precio_venta=90_000_000,
            operador="operador",
        )
        db.commit()

        assert seguimiento.cierres_declarados(db) == []

    def test_decir_que_no_deja_la_conversacion_viva(self, db, presentado):
        """Quien sigue buscando es un lead, no un caso cerrado."""
        r = gateway.procesar(db, presentado, "todavía no, sigo buscando")
        db.commit()

        assert r.cierre_declarado is False
        assert seguimiento.cierres_declarados(db) == []

    def test_un_si_suelto_solo_cuenta_si_se_le_pregunto(self, db, presentado):
        """Sin pregunta abierta, un "sí" no declara nada."""
        gateway.procesar(db, presentado, "sí")
        db.commit()
        assert seguimiento.cierres_declarados(db) == []

        # Con la pregunta ya enviada, el mismo "sí" sí significa que cerró.
        solicitud = presentado.solicitudes[-1]
        solicitud.creado_en = ahora() - timedelta(days=8)
        db.commit()
        seguimiento.ejecutar(db, enviar=lambda p, t: True)
        db.commit()

        gateway.procesar(db, presentado, "sí")
        db.commit()
        assert len(seguimiento.cierres_declarados(db)) == 1
