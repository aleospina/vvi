"""Cuenta de invitado: solo lectura de la cartera + comentarios.

Lo que se prueba aquí no es la interfaz sino la barrera: ocultar un botón no
protege una ruta. Cada acción de escritura tiene que rechazar al invitado
aunque la llame directamente por HTTP.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, inicializar
from app.models import ComentarioPropiedad, FuentePropiedad, Propiedad
from app.security.sesion import INVITADO, OPERADOR, credenciales_validas, rol_de
from app.services import ingesta
from app.services.ingesta import Publicacion

CLAVE_INVITADO = "invitado"


@pytest.fixture(autouse=True)
def cuenta_invitado(monkeypatch):
    monkeypatch.setattr(settings, "invitado_user", "invitado")
    monkeypatch.setattr(settings, "invitado_password", CLAVE_INVITADO)


@pytest.fixture()
def propiedad_id():
    inicializar(seed=False)
    db = SessionLocal()
    try:
        existente = db.query(Propiedad).filter(Propiedad.externo_id == "roles-1").first()
        if existente:
            return existente.id
        p = ingesta.ingerir_una(
            db,
            Publicacion(
                fuente=FuentePropiedad.CAPTACION_PROPIETARIO.value, externo_id="roles-1",
                ciudad="Pereira", zona="Pinares", tipo="apartamento", precio=420_000_000,
                mandato=True, mandato_evidencia="prueba de roles",
            ),
            actor="test",
        )
        db.commit()
        return p.id
    finally:
        db.close()


@pytest.fixture()
def ficha_publica(propiedad_id):
    """Ruta pública del inmueble de prueba. Es donde vive ahora el invitado.

    Hay que aprobarlo: la ingesta lo deja en `pendiente_validacion` y la vitrina
    solo publica lo `disponible`, así que sin este paso la ficha da 404.
    """
    from app.services import portfolio

    inicializar(seed=False)
    db = SessionLocal()
    try:
        p = portfolio.obtener(db, propiedad_id)
        if p.estado != "disponible":
            ingesta.aprobar(db, p, actor="test")
            db.commit()
        return portfolio.ruta_publica(p)
    finally:
        db.close()


def _cliente(usuario: str, clave: str):
    from app.main import app

    cli = TestClient(app, follow_redirects=False)
    cli.__enter__()
    r = cli.post("/dashboard/login", data={"usuario": usuario, "clave": clave})
    assert r.status_code == 303, "el ingreso debía funcionar"
    return cli


@pytest.fixture()
def invitado():
    cli = _cliente("invitado", CLAVE_INVITADO)
    yield cli
    cli.__exit__(None, None, None)


@pytest.fixture()
def admin():
    cli = _cliente(settings.dashboard_user, settings.dashboard_password)
    yield cli
    cli.__exit__(None, None, None)


class TestCredenciales:
    def test_cada_cuenta_da_su_rol(self):
        assert credenciales_validas(settings.dashboard_user, settings.dashboard_password) == OPERADOR
        assert credenciales_validas("invitado", CLAVE_INVITADO) == INVITADO
        assert credenciales_validas("invitado", "otra") is None
        assert credenciales_validas("nadie", CLAVE_INVITADO) is None

    def test_sin_clave_configurada_la_cuenta_no_existe(self, monkeypatch):
        """Es el interruptor para deshabilitar el invitado."""
        monkeypatch.setattr(settings, "invitado_password", "")
        assert credenciales_validas("invitado", "invitado") is None
        assert rol_de("invitado") is None

    def test_el_rol_se_deriva_en_cada_peticion(self, monkeypatch):
        """Deshabilitar la cuenta corta las sesiones abiertas, no las hereda."""
        assert rol_de("invitado") == INVITADO
        monkeypatch.setattr(settings, "invitado_password", "")
        assert rol_de("invitado") is None


class TestLoQuePuedeVer:
    def test_entra_por_la_vitrina(self, invitado):
        """Su sitio es /inmuebles: el panel ya no lista inventario."""
        r = invitado.post(
            "/dashboard/login", data={"usuario": "invitado", "clave": CLAVE_INVITADO}
        )
        assert r.headers["location"] == "/inmuebles"

    def test_ve_la_vitrina(self, invitado, propiedad_id):
        assert invitado.get("/inmuebles").status_code == 200

    @pytest.mark.parametrize("ruta", ["/dashboard/propiedades", "/dashboard/propiedades/{pid}"])
    def test_el_panel_lo_devuelve_a_la_vitrina(self, invitado, propiedad_id, ruta):
        """303 y no 403: no intenta algo prohibido, llegó a la puerta equivocada."""
        r = invitado.get(ruta.format(pid=propiedad_id))
        assert r.status_code == 303
        assert r.headers["location"] == "/inmuebles"

    @pytest.mark.parametrize(
        "ruta", ["/dashboard", "/dashboard/captacion", "/dashboard/auditoria"]
    )
    def test_no_alcanza_lo_que_tiene_datos_personales(self, invitado, ruta):
        """Prospectos, captación y auditoría exponen conversaciones y teléfonos."""
        assert invitado.get(ruta).status_code == 403

    def test_no_ve_los_controles_de_escritura(self, invitado, propiedad_id, ficha_publica):
        """En la vitrina no hay nada que editar, y tampoco se le insinúa."""
        html = invitado.get(ficha_publica).text
        assert "Editar datos" not in html
        assert "Guardar cambios" not in html
        assert "Editar ficha interna" not in html
        # Sí puede comentar: es lo único para lo que existe su cuenta.
        assert "Publicar comentario" in html


class TestLoQueNoPuedeHacer:
    """Llamando las rutas directamente, sin pasar por la interfaz."""

    def rutas(self, pid: str) -> list[tuple[str, dict]]:
        return [
            (f"/dashboard/propiedades/{pid}/editar", {"ciudad": "Pereira", "tipo": "casa", "precio": "300000000"}),
            (f"/dashboard/propiedades/{pid}/aprobar", {}),
            (f"/dashboard/propiedades/{pid}/rechazar", {"motivo": "x"}),
            (f"/dashboard/propiedades/{pid}/inactivar", {}),
            (f"/dashboard/propiedades/{pid}/reactivar", {}),
            (f"/dashboard/propiedades/{pid}/fotos", {}),
            ("/dashboard/propiedades", {"ciudad": "Pereira", "zona": "z", "tipo": "casa", "precio": "300000000"}),
            ("/dashboard/propiedades/importar", {"aviso": "algo", "confirmo_mandato": "on"}),
            ("/dashboard/propiedades/purgar-referencias", {}),
            ("/dashboard/captacion/campana", {"slug": "x", "nombre": "x", "red": "web"}),
        ]

    def test_toda_escritura_le_da_403(self, invitado, propiedad_id):
        for ruta, datos in self.rutas(propiedad_id):
            r = invitado.post(ruta, data=datos)
            assert r.status_code == 403, f"{ruta} debía rechazar al invitado, dio {r.status_code}"

    def test_no_puede_borrar_fotos_ni_comentarios(self, invitado, propiedad_id):
        assert invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/fotos/1/eliminar"
        ).status_code == 403
        assert invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios/1/eliminar"
        ).status_code == 403

    def test_la_propiedad_queda_intacta(self, invitado, propiedad_id):
        invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/editar",
            data={"ciudad": "Pereira", "zona": "HACKEADO", "tipo": "casa", "precio": "1000000000"},
        )
        db = SessionLocal()
        try:
            p = db.get(Propiedad, propiedad_id)
            assert p.zona != "HACKEADO"
            assert p.precio == 420_000_000
        finally:
            db.close()


class TestComentarios:
    def test_el_invitado_puede_comentar(self, invitado, propiedad_id):
        r = invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios",
            data={"texto": "¿El precio incluye parqueadero?"},
        )
        assert r.status_code == 303

        db = SessionLocal()
        try:
            c = db.query(ComentarioPropiedad).filter(
                ComentarioPropiedad.texto == "¿El precio incluye parqueadero?"
            ).one()
            assert c.autor == "invitado"
            assert c.rol == INVITADO
            assert c.es_respuesta is False
        finally:
            db.close()

    def test_el_operador_responde_en_el_mismo_hilo(self, admin, propiedad_id):
        admin.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios",
            data={"texto": "Sí, incluye uno cubierto."},
        )
        db = SessionLocal()
        try:
            c = db.query(ComentarioPropiedad).filter(
                ComentarioPropiedad.texto == "Sí, incluye uno cubierto."
            ).one()
            assert c.rol == OPERADOR
            assert c.es_respuesta is True
        finally:
            db.close()

    def test_un_comentario_vacio_se_rechaza(self, invitado, propiedad_id):
        r = invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios", data={"texto": "   "}
        )
        assert r.status_code == 400

    def test_el_hilo_se_ve_en_la_ficha_publica(self, invitado, propiedad_id, ficha_publica):
        """El invitado ya no entra al panel: su hilo vive en la vitrina."""
        r = invitado.post(
            f"{ficha_publica}/comentarios",
            data={"texto": "La foto 2 parece de otro inmueble"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"{ficha_publica}#comentarios"
        assert "La foto 2 parece de otro inmueble" in invitado.get(ficha_publica).text

    def test_es_el_mismo_hilo_en_las_dos_fichas(self, invitado, admin, propiedad_id, ficha_publica):
        """Lo que el invitado escribe en la vitrina lo ve el operador en el panel."""
        invitado.post(f"{ficha_publica}/comentarios", data={"texto": "¿Tiene parqueadero?"})
        interna = admin.get(f"/dashboard/propiedades/{propiedad_id}").text
        assert "¿Tiene parqueadero?" in interna

    def test_sin_sesion_no_se_puede_comentar_en_la_vitrina(self, ficha_publica):
        """404 y no 403: el hilo no existe para un desconocido, ni se le muestra."""
        from app.main import app

        with TestClient(app, follow_redirects=False) as anonimo:
            r = anonimo.post(f"{ficha_publica}/comentarios", data={"texto": "hola"})
            assert r.status_code == 404

    def test_el_publico_no_ve_las_notas_del_equipo(self, propiedad_id, ficha_publica, invitado):
        """Son notas internas: un comprador no debe leerlas."""
        invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios",
            data={"texto": "Ojo, el dueño negocia hasta 380"},
        )
        from app.main import app

        with TestClient(app, follow_redirects=False) as anonimo:
            html = anonimo.get(ficha_publica).text
            assert "Ojo, el dueño negocia hasta 380" not in html
            assert "Notas del equipo" not in html

    def test_cada_ficha_devuelve_a_la_suya(self, invitado, propiedad_id, ficha_publica):
        """Dos rutas explícitas en vez de un destino que viaje escondido.

        El campo oculto que lo indicaba no llegaba por HTTP real aunque los
        tests con TestClient pasaran, así que el destino lo fija la ruta.
        """
        desde_panel = invitado.post(
            f"/dashboard/propiedades/{propiedad_id}/comentarios", data={"texto": "desde el panel"}
        )
        assert desde_panel.headers["location"] == f"/dashboard/propiedades/{propiedad_id}#comentarios"

        desde_vitrina = invitado.post(f"{ficha_publica}/comentarios", data={"texto": "desde la vitrina"})
        assert desde_vitrina.headers["location"] == f"{ficha_publica}#comentarios"

    def test_borrar_el_inmueble_arrastra_sus_comentarios(self, db):
        from app.services import ingesta as ing

        p = ing.ingerir_una(
            db,
            Publicacion(
                fuente=FuentePropiedad.CAPTACION_PROPIETARIO.value, externo_id="com-cascada",
                ciudad="Pereira", tipo="casa", precio=300_000_000,
                mandato=True, mandato_evidencia="x",
            ),
        )
        p.comentarios.append(ComentarioPropiedad(autor="invitado", rol=INVITADO, texto="hola"))
        db.flush()
        db.delete(p)
        db.flush()
        assert db.query(ComentarioPropiedad).count() == 0
