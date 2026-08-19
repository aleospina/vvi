"""Edición de inmuebles desde el dashboard (RF-10).

El extractor se equivoca: el operador tiene que poder corregir zona, precio o
área antes de aprobar. Lo que NO puede hacer desde el formulario es cambiar la
procedencia, porque eso convertiría una referencia en inventario vendible.
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import FuentePropiedad, Propiedad
from app.services import ingesta
from app.services.ingesta import Publicacion


@pytest.fixture()
def cli():
    from app.main import app

    inicializar(seed=False)
    with TestClient(app, follow_redirects=False) as cliente:
        cliente.post(
            "/dashboard/login",
            data={"usuario": settings.dashboard_user, "clave": settings.dashboard_password},
        )
        yield cliente


_contador = itertools.count()


def _borrar(propiedad_id: str) -> None:
    """Saca de la base compartida el inmueble que creó un test."""
    db = SessionLocal()
    try:
        db.query(Propiedad).filter(Propiedad.id == propiedad_id).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()



def _crear(fuente: str = FuentePropiedad.CAPTACION_PROPIETARIO.value, *, marca: str = "") -> str:
    """Inserta un inmueble nuevo en la base que usa la app y devuelve su id.

    Cada llamada usa un `externo_id` distinto: los tests comparten la base de la
    app, y reutilizar el registro haría que un caso viera lo que editó el anterior.
    """
    referencia = fuente in {FuentePropiedad.REFERENCIA.value}
    pub = Publicacion(
        fuente=fuente,
        externo_id=marca or f"edicion-{fuente}-{next(_contador)}",
        ciudad="Pereira", zona="Pinares", tipo="apartamento",
        precio=420_000_000, habitaciones=3, banos=2, area_m2=95,
        propietario="Luis", propietario_telefono="3105557788",
        mandato=not referencia,
        mandato_evidencia="prueba de edición",
    )
    db = SessionLocal()
    try:
        existente = db.query(Propiedad).filter(Propiedad.externo_id == pub.externo_id).first()
        if existente:
            return existente.id
        p = ingesta.ingerir_una(db, pub, actor="test")
        db.commit()
        return p.id
    finally:
        db.close()


def _leer(propiedad_id: str) -> Propiedad:
    db = SessionLocal()
    try:
        return db.get(Propiedad, propiedad_id)
    finally:
        db.close()


FORM = {
    "ciudad": "Pereira", "zona": "Álamos", "tipo": "casa", "precio": "455000000",
    "habitaciones": "4", "banos": "3", "area_m2": "120.5",
    "descripcion": "Corregido por el operador", "propietario": "Luis Gómez",
    "propietario_telefono": "3009998877", "url_origen": "OLX 17/08",
}


class TestEdicion:
    def test_la_ficha_se_abre(self, cli):
        pid = _crear()
        r = cli.get(f"/dashboard/propiedades/{pid}")
        assert r.status_code == 200
        assert "Editar datos" in r.text

    def test_inmueble_inexistente_da_404(self, cli):
        assert cli.get("/dashboard/propiedades/PROP-NO-EXISTE").status_code == 404

    def test_guarda_los_cambios(self, cli):
        pid = _crear()
        r = cli.post(f"/dashboard/propiedades/{pid}/editar", data=FORM)
        assert r.status_code == 303

        p = _leer(pid)
        assert p.zona == "Álamos"
        assert p.tipo == "casa"
        assert p.precio == 455_000_000
        assert p.habitaciones == 4
        assert p.area_m2 == pytest.approx(120.5)
        assert p.propietario == "Luis Gómez"

    def test_el_telefono_nuevo_reindexa_la_huella(self, cli):
        """Si el índice ciego no se rehace, deja de encontrarse por dueño."""
        from app.security.crypto import indice_ciego

        pid = _crear()
        anterior = _leer(pid).propietario_telefono_hash
        cli.post(f"/dashboard/propiedades/{pid}/editar", data=FORM)

        p = _leer(pid)
        assert p.propietario_telefono == "3009998877"
        assert p.propietario_telefono_hash == indice_ciego("3009998877")
        assert p.propietario_telefono_hash != anterior

    def test_la_edicion_queda_auditada(self, cli):
        from app.models import LogAuditoria

        pid = _crear()
        cli.post(f"/dashboard/propiedades/{pid}/editar", data=FORM)
        db = SessionLocal()
        try:
            acciones = [
                r.accion for r in db.query(LogAuditoria).filter(LogAuditoria.entidad_id == pid)
            ]
        finally:
            db.close()
        assert "propiedad_actualizada" in acciones


class TestValidacion:
    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            ("ciudad", "Bogotá"),   # fuera de Antioquia y Risaralda
            ("tipo", "bodega"),     # tipo inexistente
            ("precio", "0"),        # no es un precio
            ("precio", "-5000"),    # tampoco
        ],
    )
    def test_datos_invalidos_se_rechazan(self, cli, campo, valor):
        pid = _crear()
        antes = _leer(pid).precio
        r = cli.post(f"/dashboard/propiedades/{pid}/editar", data={**FORM, campo: valor})
        assert r.status_code == 400
        assert _leer(pid).precio == antes  # no se guardó nada

    @pytest.mark.parametrize("precio", ["500000", "90000000000", "385500000"])
    def test_el_precio_es_el_que_ponga_el_operador(self, cli, precio):
        """El rango razonable de la ingesta ataja a un extractor que leyó mal un
        aviso. Aquí hay una persona con el inmueble delante: un lote de medio
        millón o una finca de 90.000 millones son suyos, no del formulario."""
        pid = _crear()
        r = cli.post(f"/dashboard/propiedades/{pid}/editar", data={**FORM, "precio": precio})
        assert r.status_code == 303
        assert _leer(pid).precio == int(precio)

    @pytest.mark.parametrize(
        ("escrito", "guardado"),
        [("Sabaneta", "Sabaneta"), ("itagui", "Itagüí"), ("Urrao", "Urrao"),
         ("Santa Rosa", "Santa Rosa de Cabal"), ("Envigado, Antioquia", "Envigado")],
    )
    def test_se_puede_editar_a_cualquier_municipio_de_la_region(self, cli, escrito, guardado):
        pid = _crear()
        try:
            r = cli.post(f"/dashboard/propiedades/{pid}/editar", data={**FORM, "ciudad": escrito})
            assert r.status_code == 303
            assert _leer(pid).ciudad == guardado
        finally:
            # Se borra: los tests comparten la base de la app y dejar un inmueble
            # en Sabaneta rompe a quien cuenta municipios dando por hecho el suyo.
            _borrar(pid)


class TestProcedenciaBlindada:
    """Lo que sostiene el modo referencia: la procedencia no se edita."""

    def test_no_se_puede_blanquear_una_referencia(self, cli):
        pid = _crear(FuentePropiedad.REFERENCIA.value, marca="edicion-referencia")
        assert _leer(pid).es_referencia

        # Se intenta colar la procedencia y el mandato por el formulario.
        cli.post(
            f"/dashboard/propiedades/{pid}/editar",
            data={
                **FORM,
                "fuente": FuentePropiedad.CAPTACION_PROPIETARIO.value,
                "mandato": "true",
                "externo_id": "falsificado",
                "estado": "disponible",
            },
        )

        p = _leer(pid)
        assert p.fuente == FuentePropiedad.REFERENCIA.value   # intacta
        assert p.mandato is False                              # intacto
        assert p.externo_id == "edicion-referencia"            # intacto
        assert p.es_referencia                                 # sigue sin poder venderse
        assert p.zona == "Álamos"                              # lo editable sí cambió
