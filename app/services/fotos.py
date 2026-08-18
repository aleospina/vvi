"""Carga de imágenes de inmuebles (RF-10).

Los archivos viven en `app/static/fotos/` y en la base solo queda la ruta.

Seguridad
---------
`/publicar` es un formulario **público**: cualquiera en internet puede subir
archivos por ahí. Por eso:

  · Se valida la **firma binaria**, no la extensión ni el `Content-Type`, que
    los pone el cliente y se falsifican trivialmente.
  · Solo JPEG, PNG y WebP. **SVG queda fuera a propósito**: admite `<script>`
    dentro, y al servirse desde nuestro mismo origen sería XSS almacenado.
  · El nombre lo genera el servidor. Usar el del cliente permite `../../` y
    sobrescribir archivos ajenos.
  · Hay tope de tamaño y de cantidad; se lee por trozos para que un archivo
    enorme no se cargue entero en memoria antes de rechazarlo.
"""

from __future__ import annotations

import io
import logging
import secrets
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.config import RAIZ, settings
from app.models import FotoPropiedad, Propiedad
from app.services.compliance import auditar

log = logging.getLogger(__name__)

#: Configurable para poder apuntarlo al volumen persistente del despliegue.
DIRECTORIO = settings.ruta_fotos

MAX_BYTES = 6 * 1024 * 1024      # 6 MB de entrada por imagen
MAX_POR_INMUEBLE = 12
TROZO = 64 * 1024

#: Una foto de celular llega en 4000 px y 6 MB. Servirla tal cual hace que la
#: cartera cargue lentísima, así que se guardan dos versiones: la grande para
#: la ficha y una miniatura para las tarjetas, que es donde se ven muchas a la
#: vez. El sufijo va por convención en el nombre, sin columna nueva en la base.
ANCHO_MAXIMO = 1600
ANCHO_MINIATURA = 640
CALIDAD = 82
SUFIJO_MINIATURA = "-min"

#: Firma binaria → extensión. La clave es una tupla (desplazamiento, bytes).
FIRMAS: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "jpg"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (8, b"WEBP", "webp"),          # RIFF....WEBP
)


class ImagenInvalida(ValueError):
    """El archivo no es una imagen admitida, o excede los límites."""


def _extension(cabecera: bytes) -> str | None:
    for desplazamiento, firma, extension in FIRMAS:
        if cabecera[desplazamiento : desplazamiento + len(firma)] == firma:
            return extension
    return None


def _leer_validando(archivo) -> tuple[bytes, str]:
    """Lee el archivo completo comprobando firma y tamaño. Devuelve (datos, ext)."""
    partes: list[bytes] = []
    total = 0
    while trozo := archivo.file.read(TROZO):
        total += len(trozo)
        if total > MAX_BYTES:
            raise ImagenInvalida(
                f"'{archivo.filename}' supera el máximo de {MAX_BYTES // 1024 // 1024} MB."
            )
        partes.append(trozo)

    datos = b"".join(partes)
    if not datos:
        raise ImagenInvalida(f"'{archivo.filename}' está vacío.")

    extension = _extension(datos[:16])
    if extension is None:
        raise ImagenInvalida(
            f"'{archivo.filename}' no es una imagen JPG, PNG o WebP. "
            "No se admiten otros formatos (los SVG pueden llevar código)."
        )
    return datos, extension


def _procesar(datos: bytes, destino: Path, ancho: int) -> None:
    """Reencodifica a JPEG con el ancho máximo dado.

    `exif_transpose` es imprescindible: las fotos de celular llevan la
    orientación en los metadatos, y sin aplicarla media cartera saldría
    acostada. Al reencodificar también se descartan los EXIF, que suelen
    incluir coordenadas GPS de la vivienda del propietario — no es dato que
    debamos republicar.
    """
    with Image.open(io.BytesIO(datos)) as imagen:
        imagen = ImageOps.exif_transpose(imagen)
        if imagen.mode not in ("RGB", "L"):
            # JPEG no admite alfa: el fondo transparente se compone sobre blanco.
            fondo = Image.new("RGB", imagen.size, (255, 255, 255))
            fondo.paste(imagen, mask=imagen.split()[-1] if "A" in imagen.mode else None)
            imagen = fondo
        if imagen.width > ancho:
            alto = round(imagen.height * ancho / imagen.width)
            imagen = imagen.resize((ancho, alto), Image.LANCZOS)
        imagen.save(destino, "JPEG", quality=CALIDAD, optimize=True, progressive=True)


def _escribir_versiones(datos: bytes, base: str) -> None:
    """Guarda la versión grande y la miniatura de una imagen."""
    _procesar(datos, DIRECTORIO / f"{base}.jpg", ANCHO_MAXIMO)
    _procesar(datos, DIRECTORIO / f"{base}{SUFIJO_MINIATURA}.jpg", ANCHO_MINIATURA)


def guardar(
    db: Session, propiedad: Propiedad, archivos: list, *, actor: str = "sistema"
) -> list[FotoPropiedad]:
    """Guarda las imágenes válidas y las asocia al inmueble.

    Un archivo inválido no cancela los demás: se descarta con log. Subir cinco
    fotos y perderlas todas porque una estaba corrupta sería peor experiencia
    que subir cuatro.
    """
    utiles = [a for a in archivos if a is not None and getattr(a, "filename", "")]
    if not utiles:
        return []

    DIRECTORIO.mkdir(parents=True, exist_ok=True)
    guardadas: list[FotoPropiedad] = []

    for archivo in utiles:
        if len(propiedad.fotos) >= MAX_POR_INMUEBLE:
            log.warning(
                "Se alcanzó el máximo de %d fotos en %s; se ignoran las demás.",
                MAX_POR_INMUEBLE, propiedad.id,
            )
            break
        try:
            datos, _extension = _leer_validando(archivo)
        except ImagenInvalida as exc:
            log.warning("Imagen descartada en %s: %s", propiedad.id, exc)
            continue

        # Todo se normaliza a JPEG: la extensión de entrada ya no importa.
        base = f"{propiedad.id.lower()}-{secrets.token_hex(8)}"
        try:
            _escribir_versiones(datos, base)
        except OSError as exc:  # imagen con firma válida pero contenido roto
            log.warning("No se pudo procesar '%s' en %s: %s", archivo.filename, propiedad.id, exc)
            for sobrante in DIRECTORIO.glob(f"{base}*"):
                sobrante.unlink(missing_ok=True)
            continue

        foto = FotoPropiedad(archivo=f"{base}.jpg", orden=len(propiedad.fotos))
        # Se anexa por la relación y no con `db.add`: así la colección en memoria
        # queda consistente en el mismo turno —`propiedad.portada` acierta sin
        # recargar— y el borrado en cascada del inmueble sí arrastra estas fotos.
        propiedad.fotos.append(foto)
        guardadas.append(foto)

    if guardadas:
        db.flush()
        auditar(
            db, actor=actor, accion="fotos_cargadas", entidad="propiedad",
            entidad_id=propiedad.id, detalle=f"{len(guardadas)} imagen(es)",
        )
    return guardadas


def diagnostico(db: Session) -> dict:
    """Estado del almacenamiento de imágenes.

    Existe porque el fallo típico del despliegue es mudo: si el directorio de
    fotos no cae dentro del volumen, los registros sobreviven al redespliegue
    pero los archivos no, y el inmueble aparece con el nombre y sin imagen. Sin
    esto hay que deducirlo mirando síntomas.
    """
    registradas = list(db.scalars(select(FotoPropiedad)))
    faltantes = [f.archivo for f in registradas if not (DIRECTORIO / f.archivo).exists()]
    # Si el directorio está dentro del árbol del código, cada despliegue lo
    # reemplaza: es efímero aunque el servicio tenga un volumen montado.
    try:
        efimero = DIRECTORIO.resolve().is_relative_to(RAIZ.resolve())
    except (OSError, ValueError):
        efimero = False
    return {
        "directorio": str(DIRECTORIO),
        "efimero": efimero,
        "registradas": len(registradas),
        "faltantes": faltantes,
    }


def eliminar(db: Session, foto: FotoPropiedad, actor: str) -> None:
    """Borra la foto y sus dos archivos. Si ya no están, sigue igual."""
    propiedad_id, archivo = foto.propiedad_id, foto.archivo
    base = archivo.rsplit(".", 1)[0]
    for nombre in (archivo, f"{base}{SUFIJO_MINIATURA}.jpg"):
        try:
            (DIRECTORIO / nombre).unlink(missing_ok=True)
        except OSError as exc:  # disco de solo lectura, permisos…
            log.warning("No se pudo borrar el archivo %s: %s", nombre, exc)

    # Se quita por la relación —con `delete-orphan` eso la borra— en vez de con
    # `db.delete`: así la colección en memoria queda sin ella antes de renumerar.
    # Con `db.delete` la lista seguía incluyéndola y el orden quedaba con huecos.
    propiedad = foto.propiedad
    if propiedad is not None:
        propiedad.fotos.remove(foto)
        _renumerar(propiedad)
    else:
        db.delete(foto)
    db.flush()
    auditar(
        db, actor=actor, accion="foto_eliminada", entidad="propiedad",
        entidad_id=propiedad_id, detalle=archivo,
    )


def _renumerar(propiedad: Propiedad) -> None:
    """Deja el orden en 0,1,2… sin huecos tras borrar o mover."""
    for posicion, foto in enumerate(propiedad.fotos):
        foto.orden = posicion


def reordenar(
    db: Session, propiedad: Propiedad, ids: list[int], actor: str
) -> list[FotoPropiedad]:
    """Aplica el orden recibido. La primera queda como portada.

    Solo se reordena lo que llegue; cualquier foto que el cliente no haya
    mencionado se conserva al final, para que un envío incompleto no borre
    posiciones ni deje huérfanas.
    """
    por_id = {f.id: f for f in propiedad.fotos}
    ordenadas = [por_id.pop(i) for i in ids if i in por_id]
    ordenadas.extend(por_id.values())

    for posicion, foto in enumerate(ordenadas):
        foto.orden = posicion
    db.flush()
    auditar(
        db, actor=actor, accion="fotos_reordenadas", entidad="propiedad",
        entidad_id=propiedad.id,
        detalle=f"portada: {ordenadas[0].archivo if ordenadas else '—'}",
    )
    return ordenadas


def hacer_portada(db: Session, foto: FotoPropiedad, actor: str) -> None:
    """Sube una foto al primer lugar. Un clic, sin arrastrar."""
    propiedad = foto.propiedad
    resto = [f.id for f in propiedad.fotos if f.id != foto.id]
    reordenar(db, propiedad, [foto.id, *resto], actor=actor)
