"""Carga de imágenes de inmuebles (RF-10).

`/publicar` es público, así que lo que más importa aquí es lo que NO se acepta:
un archivo que dice ser imagen y no lo es, un SVG con script, o un nombre con
`../` para escribir fuera del directorio.
"""

from __future__ import annotations

import io

import pytest

from app.models import FotoPropiedad, FuentePropiedad
from app.services import fotos, ingesta
from app.services.fotos import ImagenInvalida
from app.services.ingesta import Publicacion

def _imagen(formato: str, ancho: int = 900, alto: int = 600) -> bytes:
    """Imagen real: ahora Pillow tiene que poder abrirla y reescalarla."""
    from PIL import Image

    memoria = io.BytesIO()
    Image.new("RGB", (ancho, alto), (11, 93, 80)).save(memoria, formato)
    return memoria.getvalue()


JPG = _imagen("JPEG")
PNG = _imagen("PNG")
WEBP = _imagen("WEBP")
SVG_MALICIOSO = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class ArchivoFalso:
    """Imita el UploadFile de Starlette con lo que usa el servicio."""

    def __init__(self, nombre: str, datos: bytes):
        self.filename = nombre
        self.file = io.BytesIO(datos)


@pytest.fixture()
def propiedad(db):
    return ingesta.ingerir_una(
        db,
        Publicacion(
            fuente=FuentePropiedad.CAPTACION_PROPIETARIO.value, externo_id="fotos-1",
            ciudad="Pereira", zona="Pinares", tipo="apartamento", precio=420_000_000,
            mandato=True, mandato_evidencia="prueba",
        ),
    )


@pytest.fixture(autouse=True)
def limpiar_directorio():
    """Borra lo que escriban los tests: no deben dejar basura en static/."""
    previos = set(fotos.DIRECTORIO.glob("*")) if fotos.DIRECTORIO.exists() else set()
    yield
    if fotos.DIRECTORIO.exists():
        for f in set(fotos.DIRECTORIO.glob("*")) - previos:
            f.unlink(missing_ok=True)


class TestFormatosAdmitidos:
    @pytest.mark.parametrize(("datos", "ext"), [(JPG, "jpg"), (PNG, "png"), (WEBP, "webp")])
    def test_se_aceptan_jpg_png_webp(self, db, propiedad, datos, ext):
        guardadas = fotos.guardar(db, propiedad, [ArchivoFalso(f"casa.{ext}", datos)])
        assert len(guardadas) == 1
        assert guardadas[0].archivo.endswith(".jpg")  # todo se normaliza a JPEG
        assert (fotos.DIRECTORIO / guardadas[0].archivo).exists()

    def test_la_extension_mentirosa_no_engana(self, db, propiedad):
        """Se valida la firma binaria, no el nombre: un .jpg de mentira se cae."""
        assert fotos.guardar(db, propiedad, [ArchivoFalso("truco.jpg", b"esto no es una imagen")]) == []

    def test_el_svg_se_rechaza(self, db, propiedad):
        """Admite <script> y al servirse desde nuestro origen sería XSS almacenado."""
        assert fotos.guardar(db, propiedad, [ArchivoFalso("logo.svg", SVG_MALICIOSO)]) == []

    def test_archivo_vacio_se_rechaza(self, db, propiedad):
        assert fotos.guardar(db, propiedad, [ArchivoFalso("vacio.jpg", b"")]) == []


class TestLimites:
    def test_se_rechaza_lo_que_excede_el_tamano(self, db, propiedad, monkeypatch):
        monkeypatch.setattr(fotos, "MAX_BYTES", 1024)
        grande = _imagen("JPEG", 3000, 2000)
        with pytest.raises(ImagenInvalida, match="máximo"):
            fotos._leer_validando(ArchivoFalso("grande.jpg", grande))

    def test_tope_de_fotos_por_inmueble(self, db, propiedad, monkeypatch):
        monkeypatch.setattr(fotos, "MAX_POR_INMUEBLE", 3)
        fotos.guardar(db, propiedad, [ArchivoFalso(f"f{i}.jpg", JPG) for i in range(6)])
        assert len(propiedad.fotos) == 3


class TestNombreDeArchivo:
    def test_el_nombre_del_cliente_se_descarta(self, db, propiedad):
        """`../` en el nombre permitiría escribir fuera del directorio."""
        guardadas = fotos.guardar(
            db, propiedad, [ArchivoFalso("../../../etc/passwd.jpg", JPG)]
        )
        assert ".." not in guardadas[0].archivo
        assert "/" not in guardadas[0].archivo and "\\" not in guardadas[0].archivo
        assert guardadas[0].archivo.startswith(propiedad.id.lower())

    def test_dos_archivos_iguales_no_se_pisan(self, db, propiedad):
        a, b = fotos.guardar(
            db, propiedad, [ArchivoFalso("foto.jpg", JPG), ArchivoFalso("foto.jpg", JPG)]
        )
        assert a.archivo != b.archivo


class TestTolerancia:
    def test_una_imagen_mala_no_pierde_las_buenas(self, db, propiedad):
        guardadas = fotos.guardar(
            db,
            propiedad,
            [
                ArchivoFalso("ok1.jpg", JPG),
                ArchivoFalso("rota.jpg", b"basura"),
                ArchivoFalso("ok2.png", PNG),
            ],
        )
        assert len(guardadas) == 2

    def test_sin_archivos_no_hace_nada(self, db, propiedad):
        assert fotos.guardar(db, propiedad, []) == []
        assert fotos.guardar(db, propiedad, [ArchivoFalso("", b"")]) == []


class TestPortadaYBorrado:
    def test_la_portada_es_la_primera(self, db, propiedad):
        assert propiedad.portada == "/static/img/placeholder.svg"
        guardadas = fotos.guardar(
            db, propiedad, [ArchivoFalso("a.jpg", JPG), ArchivoFalso("b.png", PNG)]
        )
        db.refresh(propiedad)
        # La tarjeta usa la miniatura, no la versión grande.
        assert propiedad.portada == guardadas[0].miniatura

    def test_eliminar_borra_registro_y_archivo(self, db, propiedad):
        foto = fotos.guardar(db, propiedad, [ArchivoFalso("a.jpg", JPG)])[0]
        ruta = fotos.DIRECTORIO / foto.archivo
        assert ruta.exists()

        fotos.eliminar(db, foto, actor="operador")
        assert not ruta.exists()
        assert db.get(FotoPropiedad, foto.id) is None

    def test_borrar_el_inmueble_arrastra_sus_fotos(self, db, propiedad):
        fotos.guardar(db, propiedad, [ArchivoFalso("a.jpg", JPG)])
        db.delete(propiedad)
        db.flush()
        assert db.query(FotoPropiedad).count() == 0


class TestRedimensionado:
    def test_una_foto_grande_se_reduce(self, db, propiedad):
        """Una foto de celular sin reducir hace que la cartera cargue lentísima."""
        from PIL import Image

        original = _imagen("JPEG", 4000, 3000)
        foto = fotos.guardar(db, propiedad, [ArchivoFalso("celular.jpg", original)])[0]

        with Image.open(fotos.DIRECTORIO / foto.archivo) as grande:
            assert grande.width == fotos.ANCHO_MAXIMO
            assert grande.height == 1200          # proporción conservada
        assert (fotos.DIRECTORIO / foto.archivo).stat().st_size < len(original)

    def test_se_genera_la_miniatura(self, db, propiedad):
        from PIL import Image

        foto = fotos.guardar(db, propiedad, [ArchivoFalso("a.jpg", _imagen("JPEG", 2000, 1500))])[0]
        ruta = fotos.DIRECTORIO / f"{foto.archivo.rsplit('.', 1)[0]}-min.jpg"
        assert ruta.exists()
        with Image.open(ruta) as chica:
            assert chica.width == fotos.ANCHO_MINIATURA
        assert foto.miniatura.endswith("-min.jpg")

    def test_una_foto_pequena_no_se_agranda(self, db, propiedad):
        from PIL import Image

        foto = fotos.guardar(db, propiedad, [ArchivoFalso("chica.jpg", _imagen("JPEG", 400, 300))])[0]
        with Image.open(fotos.DIRECTORIO / foto.archivo) as img:
            assert img.width == 400

    def test_el_png_con_transparencia_no_revienta(self, db, propiedad):
        """JPEG no admite alfa: debe componerse sobre blanco, no fallar."""
        from PIL import Image

        memoria = io.BytesIO()
        Image.new("RGBA", (800, 600), (11, 93, 80, 128)).save(memoria, "PNG")
        assert len(fotos.guardar(db, propiedad, [ArchivoFalso("t.png", memoria.getvalue())])) == 1

    def test_la_portada_usa_la_miniatura(self, db, propiedad):
        fotos.guardar(db, propiedad, [ArchivoFalso("a.jpg", JPG)])
        db.refresh(propiedad)
        assert propiedad.portada.endswith("-min.jpg")

    def test_borrar_se_lleva_las_dos_versiones(self, db, propiedad):
        foto = fotos.guardar(db, propiedad, [ArchivoFalso("a.jpg", JPG)])[0]
        base = foto.archivo.rsplit(".", 1)[0]
        fotos.eliminar(db, foto, actor="operador")
        assert not (fotos.DIRECTORIO / f"{base}.jpg").exists()
        assert not (fotos.DIRECTORIO / f"{base}-min.jpg").exists()


class TestReordenar:
    def _tres(self, db, propiedad):
        return fotos.guardar(
            db, propiedad,
            [ArchivoFalso("a.jpg", JPG), ArchivoFalso("b.jpg", JPG), ArchivoFalso("c.jpg", JPG)],
        )

    def test_el_orden_recibido_se_aplica(self, db, propiedad):
        a, b, c = self._tres(db, propiedad)
        fotos.reordenar(db, propiedad, [c.id, a.id, b.id], actor="operador")
        db.refresh(propiedad)
        assert [f.id for f in propiedad.fotos] == [c.id, a.id, b.id]
        assert propiedad.portada == c.miniatura

    def test_hacer_portada_sube_una_sola(self, db, propiedad):
        a, b, c = self._tres(db, propiedad)
        fotos.hacer_portada(db, c, actor="operador")
        db.refresh(propiedad)
        assert [f.id for f in propiedad.fotos] == [c.id, a.id, b.id]

    def test_un_envio_incompleto_no_pierde_fotos(self, db, propiedad):
        """Si el cliente manda solo dos ids, la tercera va al final, no se borra."""
        a, b, c = self._tres(db, propiedad)
        fotos.reordenar(db, propiedad, [c.id, a.id], actor="operador")
        db.refresh(propiedad)
        assert [f.id for f in propiedad.fotos] == [c.id, a.id, b.id]

    def test_ids_ajenos_o_basura_se_ignoran(self, db, propiedad):
        a, b, c = self._tres(db, propiedad)
        fotos.reordenar(db, propiedad, [99999, b.id], actor="operador")
        db.refresh(propiedad)
        assert [f.id for f in propiedad.fotos] == [b.id, a.id, c.id]

    def test_borrar_renumera_sin_huecos(self, db, propiedad):
        a, b, c = self._tres(db, propiedad)
        fotos.eliminar(db, b, actor="operador")
        db.refresh(propiedad)
        assert [f.orden for f in propiedad.fotos] == [0, 1]
