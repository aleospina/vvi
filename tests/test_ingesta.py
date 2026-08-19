"""Ingesta de inmuebles: normalización, mandato, deduplicación y validación humana.

Cubre RF-10 y el principio de ADR-01 aplicado al lado de la oferta: nada entra a
la cartera sin autorización del propietario, y nada llega a un comprador sin que
un operador lo apruebe.
"""

from __future__ import annotations

import pytest

from app.models import EstadoPropiedad, FuentePropiedad, Propiedad
from app.services import geografia, ingesta, matching_engine
from app.services.ingesta import MandatoAusente, Publicacion


def publicacion(**cambios) -> Publicacion:
    base = dict(
        fuente=FuentePropiedad.CAPTACION_PROPIETARIO.value,
        externo_id="ext-1",
        ciudad="Envigado",
        tipo="apto",
        precio=380_000_000,
        zona="Zúñiga",
        habitaciones=3,
        area_m2=88,
        propietario="Marta Ruiz",
        propietario_telefono="3001234567",
        mandato=True,
        mandato_evidencia="Casilla marcada en /publicar",
    )
    base.update(cambios)
    return Publicacion(**base)


class TestNormalizacion:
    def test_el_municipio_se_guarda_como_lo_que_es(self):
        """Ya no se aplasta contra la plaza: Envigado se guarda Envigado."""
        assert ingesta.normalizar_ciudad("Envigado") == "Envigado"
        assert ingesta.normalizar_ciudad("dosquebradas") == "Dosquebradas"
        assert ingesta.normalizar_ciudad("Medellín, Antioquia") == "Medellín"

    def test_la_plaza_sigue_agrupando_el_area_metropolitana(self):
        """Lo que se perdía al aplastar, ahora lo responde `plaza_de`."""
        assert geografia.plaza_de("Envigado") == "Medellín"
        assert geografia.plaza_de("dosquebradas") == "Pereira"
        assert geografia.plaza_de("Urrao") is None, "fuera del área: el bot no lo ofrece"

    def test_ciudad_fuera_de_cobertura_no_mapea(self):
        assert ingesta.normalizar_ciudad("Bogotá") is None
        assert ingesta.normalizar_ciudad("") is None

    def test_alias_de_tipo(self):
        assert ingesta.normalizar_tipo("apto") == "apartamento"
        assert ingesta.normalizar_tipo("Casa campestre") == "casa"
        assert ingesta.normalizar_tipo("terreno") == "lote"
        assert ingesta.normalizar_tipo("bodega") is None


class TestMandato:
    def test_sin_mandato_no_se_ingiere(self, db):
        with pytest.raises(MandatoAusente):
            ingesta.ingerir_una(db, publicacion(mandato=False))
        assert db.query(Propiedad).count() == 6  # solo la cartera semilla

    def test_mandato_sin_evidencia_tampoco(self, db):
        with pytest.raises(MandatoAusente):
            ingesta.ingerir_una(db, publicacion(mandato_evidencia="   "))

    def test_formulario_sin_autorizacion_no_construye_publicacion(self):
        with pytest.raises(MandatoAusente):
            ingesta.publicacion_de_formulario(
                telefono="3001234567", ciudad="Medellín", tipo="casa",
                precio=300_000_000, autoriza_mandato=False,
            )


class TestValidacion:
    @pytest.mark.parametrize(
        "cambios",
        [
            {"ciudad": "Bogotá"},
            {"tipo": "bodega"},
            {"precio": 500_000},          # precio irrisorio: error de digitación
            {"precio": 90_000_000_000},   # precio absurdo
            {"externo_id": ""},
        ],
    )
    def test_publicaciones_invalidas_se_descartan(self, db, cambios):
        with pytest.raises(ValueError):
            ingesta.ingerir_una(db, publicacion(**cambios))

    def test_un_registro_malo_no_tumba_el_lote(self, db):
        resultado = ingesta.ingerir(
            db,
            [
                publicacion(externo_id="ok-1"),
                publicacion(externo_id="malo", ciudad="Cali"),
                publicacion(externo_id="ok-2", precio=420_000_000),
            ],
        )
        assert len(resultado.creadas) == 2
        assert len(resultado.descartadas) == 1
        assert resultado.descartadas[0][0] == "malo"


class TestFormularioDelPropietario:
    """Quién escribió el dato decide con qué vara se mide.

    El dueño en /publicar llena campo por campo con el inmueble delante. El
    extractor de avisos usa la misma fuente pero lee de un texto, y ahí sí hay
    que atajar la cuota de administración tomada por precio.
    """

    def test_el_dueno_puede_estar_fuera_del_area_metropolitana(self):
        pub = publicacion(ciudad="Urrao", de_formulario=True)
        assert ingesta.validar(pub) is None

    @pytest.mark.parametrize("precio", [1_000_000, 385_500_000, 90_000_000_000])
    def test_el_dueno_pone_el_precio_que_quiera(self, precio):
        assert ingesta.validar(publicacion(precio=precio, de_formulario=True)) is None

    def test_pero_cero_no_es_un_precio(self):
        assert ingesta.validar(publicacion(precio=0, de_formulario=True))

    def test_ni_se_acepta_fuera_de_la_region(self):
        assert ingesta.validar(publicacion(ciudad="Bogotá", de_formulario=True))

    @pytest.mark.parametrize("cambios", [{"ciudad": "Urrao"}, {"precio": 500_000}])
    def test_lo_que_no_llenó_una_persona_sigue_acotado(self, cambios):
        """Un raspador o el extractor con IA se equivocan en silencio."""
        assert ingesta.validar(publicacion(**cambios)) is not None

    def test_solo_el_formulario_pone_la_marca(self):
        """No se hereda de la fuente: `publicacion_desde_texto` la comparte."""
        pub = ingesta.publicacion_de_formulario(
            telefono="3001234567", ciudad="Sabaneta", tipo="casa", precio=385_500_000,
            zona="Aves María", autoriza_mandato=True,
        )
        assert pub.de_formulario is True
        assert ingesta.validar(pub) is None


class TestIngesta:
    def test_ingerir_normaliza_y_queda_pendiente(self, db):
        p = ingesta.ingerir_una(db, publicacion())
        assert p.ciudad == "Envigado"      # se guarda el municipio, no la plaza
        assert p.tipo == "apartamento"     # "apto" se normalizó
        assert p.estado == EstadoPropiedad.PENDIENTE.value
        assert p.mandato is True

    def test_lo_pendiente_es_invisible_para_el_comprador(self, db):
        """La garantía central: el bot solo consulta `disponible`."""
        ingesta.ingerir_una(db, publicacion(precio=250_000_000, zona="Sabaneta"))
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        ids = [m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=20)]
        assert all(db.get(Propiedad, i).estado == "disponible" for i in ids)
        assert len(ids) == 3  # los 3 de la cartera semilla, ninguno nuevo

    def test_aprobar_lo_publica_al_bot(self, db):
        p = ingesta.ingerir_una(db, publicacion(precio=250_000_000))
        ingesta.aprobar(db, p, actor="operador")
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        assert p.id in [m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=20)]

    def test_rechazar_lo_mantiene_fuera(self, db):
        p = ingesta.ingerir_una(db, publicacion())
        ingesta.rechazar(db, p, actor="operador", motivo="precio no coincide")
        assert p.estado == EstadoPropiedad.RECHAZADA.value
        assert p not in ingesta.pendientes(db)

    def test_reingesta_actualiza_y_no_duplica(self, db):
        primera = ingesta.ingerir_una(db, publicacion(precio=380_000_000))
        segunda = ingesta.ingerir_una(db, publicacion(precio=395_000_000))
        assert primera.id == segunda.id
        assert segunda.precio == 395_000_000
        assert db.query(Propiedad).filter(Propiedad.externo_id == "ext-1").count() == 1

    def test_reingesta_respeta_la_decision_del_operador(self, db):
        """Si ya lo aprobó, un refresco de precio no debe devolverlo a la cola."""
        p = ingesta.ingerir_una(db, publicacion())
        ingesta.aprobar(db, p, actor="operador")
        ingesta.ingerir_una(db, publicacion(precio=399_000_000))
        assert p.estado == EstadoPropiedad.DISPONIBLE.value

    def test_el_telefono_del_propietario_queda_cifrado(self, db):
        p = ingesta.ingerir_una(db, publicacion())
        db.commit()
        # SQL crudo a propósito: por el ORM el TypeDecorator descifraría y el
        # test pasaría sin comprobar nada.
        guardado, huella = db.connection().exec_driver_sql(
            "SELECT propietario_telefono, propietario_telefono_hash "
            "FROM propiedades WHERE id = ?",
            (p.id,),
        ).one()
        assert guardado != "3001234567"
        assert guardado.startswith("gAAAAA")          # token Fernet
        assert p.propietario_telefono == "3001234567"  # el ORM lo descifra
        assert huella and "3001234567" not in huella   # índice ciego, no reversible


class TestAdaptador:
    def test_ejecutar_una_fuente_completa(self, db):
        fuente = ingesta.CaptacionPropietarios(
            [publicacion(externo_id="a"), publicacion(externo_id="b", precio=500_000_000)]
        )
        resultado = ingesta.ejecutar(db, fuente)
        assert len(resultado.creadas) == 2
        assert len(ingesta.pendientes(db)) == 2

    def test_formulario_produce_publicacion_con_evidencia(self):
        pub = ingesta.publicacion_de_formulario(
            telefono="3001234567", ciudad="Pereira", tipo="casa",
            precio=300_000_000, zona="Pinares", autoriza_mandato=True,
        )
        assert pub.mandato is True
        assert "autorización" in pub.mandato_evidencia.lower()
        assert pub.externo_id and "3001234567" not in pub.externo_id  # no va en claro


class TestModoReferencia:
    """Avisos reales de terceros para probar: visibles, pero nunca vendibles."""

    def referencia(self, **cambios):
        base = dict(fuente=FuentePropiedad.REFERENCIA.value, mandato=False,
                    mandato_evidencia="CARGA DE REFERENCIA: aviso de Marketplace")
        base.update(cambios)
        return publicacion(**base)

    def test_entra_sin_mandato(self, db):
        """Es la excepción deliberada: sin mandato, pero con evidencia de origen."""
        p = ingesta.ingerir_una(db, self.referencia())
        assert p.es_referencia
        assert p.mandato is False
        assert p.estado == EstadoPropiedad.PENDIENTE.value

    def test_sigue_exigiendo_evidencia_de_origen(self, db):
        with pytest.raises(ingesta.MandatoAusente):
            ingesta.ingerir_una(db, self.referencia(mandato_evidencia="  "))

    def test_se_puede_aprobar_para_probar_el_bot(self, db):
        p = ingesta.ingerir_una(db, self.referencia(precio=250_000_000))
        ingesta.aprobar(db, p, actor="operador")
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        assert p.id in [m.propiedad.id for m in matching_engine.buscar(db, perfil, limite=20)]

    def test_no_puede_generar_venta_ni_comision(self, db, prospecto_consentido):
        """La garantía que sostiene todo el modo."""
        from app.services import commission

        p = ingesta.ingerir_una(db, self.referencia())
        ingesta.aprobar(db, p, actor="operador")
        with pytest.raises(commission.VentaInvalida, match="referencia"):
            commission.confirmar_venta(
                db, prospecto=prospecto_consentido, propiedad=p,
                precio_venta=380_000_000, operador="op_marta",
            )

    def test_el_inventario_con_mandato_si_vende(self, db, prospecto_consentido):
        """Contraprueba: el bloqueo es por referencia, no por ingesta."""
        from app.models import EstadoProspecto
        from app.services import commission, leads

        # 'vendido' no es alcanzable desde 'nuevo' (RF-13): se avanza primero.
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.EMPAREJADO)

        p = ingesta.ingerir_una(db, publicacion(externo_id="con-mandato"))
        venta = commission.confirmar_venta(
            db, prospecto=prospecto_consentido, propiedad=p,
            precio_venta=380_000_000, operador="op_marta",
        )
        assert venta.comision_valor > 0

    def test_purgar_las_borra_todas_y_respeta_el_resto(self, db):
        ingesta.ingerir_una(db, self.referencia(externo_id="ref-1"))
        ingesta.ingerir_una(db, self.referencia(externo_id="ref-2", precio=500_000_000))
        propia = ingesta.ingerir_una(db, publicacion(externo_id="mia"))

        assert ingesta.purgar_referencias(db, actor="operador") == 2
        assert ingesta.referencias(db) == []
        assert db.get(Propiedad, propia.id) is not None


class TestReactivar:
    """Un inmueble inactivado debe poder volver a cartera (no era posible)."""

    def test_desde_inactiva_vuelve_a_disponible(self, db):
        from app.services import portfolio

        p = ingesta.ingerir_una(db, publicacion())
        ingesta.aprobar(db, p, actor="operador")
        portfolio.inactivar(db, p, actor="operador")
        assert p.estado == EstadoPropiedad.INACTIVA.value

        ingesta.reactivar(db, p, actor="operador")
        assert p.estado == EstadoPropiedad.DISPONIBLE.value

    def test_desde_rechazada_tambien(self, db):
        p = ingesta.ingerir_una(db, publicacion())
        ingesta.rechazar(db, p, actor="operador", motivo="me equivoqué")
        ingesta.reactivar(db, p, actor="operador")
        assert p.estado == EstadoPropiedad.DISPONIBLE.value

    def test_el_bot_vuelve_a_verlo(self, db):
        from app.services import portfolio

        p = ingesta.ingerir_una(db, publicacion(precio=250_000_000))
        ingesta.aprobar(db, p, actor="operador")
        portfolio.inactivar(db, p, actor="operador")
        perfil = {"ciudad": "Medellín", "tipo": "apartamento"}
        assert p.id not in [m.propiedad.id for m in matching_engine.buscar(db, perfil, 20)]

        ingesta.reactivar(db, p, actor="operador")
        assert p.id in [m.propiedad.id for m in matching_engine.buscar(db, perfil, 20)]

    def test_una_vendida_no_se_reactiva(self, db):
        """Tiene venta y comisión atribuidas: reofrecerla sería contradictorio."""
        p = ingesta.ingerir_una(db, publicacion())
        p.estado = EstadoPropiedad.VENDIDA.value
        with pytest.raises(ValueError, match="vendida"):
            ingesta.reactivar(db, p, actor="operador")

    def test_una_pendiente_se_aprueba_no_se_reactiva(self, db):
        p = ingesta.ingerir_una(db, publicacion())
        with pytest.raises(ValueError, match="pendiente_validacion"):
            ingesta.reactivar(db, p, actor="operador")

    def test_queda_auditado(self, db):
        from app.models import LogAuditoria
        from app.services import portfolio

        p = ingesta.ingerir_una(db, publicacion())
        ingesta.aprobar(db, p, actor="operador")
        portfolio.inactivar(db, p, actor="operador")
        ingesta.reactivar(db, p, actor="operador")
        acciones = [r.accion for r in db.query(LogAuditoria).all()]
        assert "inmueble_reactivado" in acciones


class TestEliminarInmueble:
    def test_borra_con_sus_fotos_y_comentarios(self, db):
        from app.models import ComentarioPropiedad, FotoPropiedad

        p = ingesta.ingerir_una(db, publicacion())
        p.comentarios.append(ComentarioPropiedad(autor="invitado", rol="invitado", texto="hola"))
        db.flush()

        ingesta.eliminar_inmueble(db, p, actor="operador")
        assert db.get(Propiedad, p.id) is None
        assert db.query(ComentarioPropiedad).count() == 0
        assert db.query(FotoPropiedad).count() == 0

    def test_conserva_la_solicitud_sin_puntero(self, db, prospecto_consentido):
        """Una petición de visita es evidencia del titular: no se tira."""
        from app.models import Solicitud

        p = ingesta.ingerir_una(db, publicacion())
        s = Solicitud(prospecto_id=prospecto_consentido.id, propiedad_id=p.id, tipo="visita")
        db.add(s)
        db.flush()

        ingesta.eliminar_inmueble(db, p, actor="operador")
        db.refresh(s)
        assert s.id is not None
        assert s.propiedad_id is None

    def test_se_niega_si_hay_venta(self, db, prospecto_consentido):
        """Borrarlo exigiría destruir el registro de comisión."""
        from app.models import EstadoProspecto
        from app.services import commission, leads

        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.CALIFICADO)
        leads.cambiar_estado(db, prospecto_consentido, EstadoProspecto.EMPAREJADO)
        p = ingesta.ingerir_una(db, publicacion())
        commission.confirmar_venta(
            db, prospecto=prospecto_consentido, propiedad=p,
            precio_venta=380_000_000, operador="op",
        )
        with pytest.raises(ingesta.TieneVenta, match="comisión"):
            ingesta.eliminar_inmueble(db, p, actor="operador")
        assert db.get(Propiedad, p.id) is not None

    def test_purgar_no_se_atora_con_emparejamientos(self, db, prospecto_consentido):
        """Regresión: la clave foránea abortaba el purgado completo."""
        from app.models import Emparejamiento

        p = ingesta.ingerir_una(
            db, publicacion(fuente=FuentePropiedad.DEMO.value, mandato=False)
        )
        db.add(Emparejamiento(prospecto_id=prospecto_consentido.id, propiedad_id=p.id, puntaje=1))
        db.flush()

        assert ingesta.purgar_referencias(db, actor="operador") == 1
        assert db.get(Propiedad, p.id) is None


class TestCarteraDemo:
    def test_los_inmuebles_demo_no_son_vendibles(self, db):
        """Son inventados: el bloqueo de venta debe cubrirlos igual que a referencia."""
        p = ingesta.ingerir_una(
            db, publicacion(fuente=FuentePropiedad.DEMO.value, mandato=False)
        )
        assert p.es_referencia          # cae en FUENTES_SIN_MANDATO
        assert p in ingesta.referencias(db)

    def test_el_catalogo_demo_es_coherente(self):
        from app.demo import CARTERA
        from app.services.ingesta import PRECIO_MAXIMO, PRECIO_MINIMO

        assert len(CARTERA) == 30
        assert len({fila[0] for fila in CARTERA}) == 30      # ids únicos
        for pid, ciudad, zona, tipo, hab, banos, area, precio, desc in CARTERA:
            assert ciudad in ("Medellín", "Pereira"), pid
            assert ingesta.normalizar_tipo(tipo) == tipo, pid
            assert PRECIO_MINIMO <= precio <= PRECIO_MAXIMO, pid
            assert zona and desc, pid
            # Un lote no tiene habitaciones; una vivienda sí.
            assert (hab == 0) == (tipo == "lote"), pid
