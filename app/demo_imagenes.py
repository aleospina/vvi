"""Ilustraciones para la cartera de demostración: `python -m app.demo_imagenes`

Por qué ilustraciones y no fotos
--------------------------------
Estos inmuebles son inventados. Pegarles fotografías reales de casas en Pinares
o El Poblado produciría avisos falsos indistinguibles de los auténticos —con la
casa de una persona real ilustrando una publicación que no existe—, que es
justo el problema que la marca `fuente="demo"` existe para evitar.

Así que se generan: composiciones que varían con la zona, el tipo y el precio,
con la paleta de la aplicación, y con el rótulo DEMO impreso encima. La cartera
se ve poblada y variada para demostrar la interfaz, y nadie puede confundirlas
con el inmueble de alguien.

Las imágenes entran por `fotos.guardar`, el mismo camino que una carga real:
así se validan, se reescalan y generan miniatura igual que el resto.
"""

from __future__ import annotations

import io
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from app.db import sesion
from app.models import FuentePropiedad, Propiedad
from app.services import fotos

ANCHO, ALTO = 1600, 1200

TINTA = (14, 26, 23)
PAPEL = (247, 245, 241)

#: Un tono por ciudad, y una variante por inmueble para que la rejilla no se vea
#: repetida. Medellín en verdes; Pereira en tierras cafeteras.
TONOS = {
    "Medellín": [(11, 93, 80), (18, 118, 100), (26, 74, 82), (14, 104, 88), (30, 90, 70)],
    "Pereira": [(150, 82, 45), (176, 108, 52), (128, 92, 48), (162, 96, 38), (140, 74, 56)],
}


class _Archivo:
    """Adaptador con la forma que espera `fotos.guardar`."""

    def __init__(self, nombre: str, datos: bytes):
        self.filename = nombre
        self.file = io.BytesIO(datos)


def _fuente(tam: int) -> ImageFont.FreeTypeFont:
    # Pillow trae una fuente escalable: no depende de que el contenedor tenga
    # tipografías instaladas, que en la imagen de despliegue no las hay.
    return ImageFont.load_default(size=tam)


#: La fuente incluida en Pillow no tiene glifos acentuados: "Medellín" salía
#: como "Medell▯n". Empaquetar una tipografía con acentos significaría
#: redistribuir las de Windows —propietarias— en un repositorio público, así que
#: se transcribe a ASCII. Se pierde el acento, pero el resultado es idéntico en
#: desarrollo y en el contenedor, y nunca aparece un cuadro vacío.
_EQUIVALENCIAS = {"²": "2", "³": "3", "·": "-", "—": "-", "–": "-"}


def _ascii(texto: str) -> str:
    for origen, destino in _EQUIVALENCIAS.items():
        texto = texto.replace(origen, destino)
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _mezclar(color, hacia, t: float):
    """Interpola dos colores.

    Sumar a cada canal para aclarar —`c + 150`— desplaza el matiz y saca cianes
    neón de un verde. Mezclar hacia el papel mantiene el tono de la paleta.
    """
    return tuple(round(a + (b - a) * t) for a, b in zip(color, hacia))


def _degradado(dib: ImageDraw.ImageDraw, alto: int, arriba, abajo) -> None:
    for y in range(alto):
        t = y / max(alto - 1, 1)
        dib.line(
            [(0, y), (ANCHO, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(arriba, abajo)),
        )


def _pesos(valor: int) -> str:
    return f"${valor:,}".replace(",", ".")


def _rotulo(dib: ImageDraw.ImageDraw, p: Propiedad) -> None:
    """Banda inferior con los datos y el sello DEMO."""
    alto_banda = 250
    y0 = ALTO - alto_banda
    dib.rectangle([0, y0, ANCHO, ALTO], fill=TINTA)

    dib.text((70, y0 + 42), _ascii(p.zona.upper()), font=_fuente(40), fill=(255, 255, 255))
    especificaciones = f"{p.ciudad} · {p.tipo}"
    if p.habitaciones:
        especificaciones += f" · {p.habitaciones} hab · {p.banos} baños"
    especificaciones += f" · {p.area_m2:.0f} m²"
    dib.text((70, y0 + 100), _ascii(especificaciones), font=_fuente(30), fill=(160, 185, 176))
    dib.text((70, y0 + 152), _pesos(p.precio), font=_fuente(52), fill=(18, 200, 160))

    # Sello: que nadie pueda tomar esto por una foto de un inmueble real.
    sello = _ascii("DEMO · INMUEBLE INVENTADO")
    ancho_sello = dib.textlength(sello, font=_fuente(24))
    dib.rectangle(
        [ANCHO - ancho_sello - 96, y0 + 44, ANCHO - 48, y0 + 92], fill=(184, 80, 44)
    )
    dib.text((ANCHO - ancho_sello - 72, y0 + 56), sello, font=_fuente(24), fill=(255, 255, 255))


def _fachada(p: Propiedad, tono) -> bytes:
    """Silueta arquitectónica según el tipo, sobre un cielo degradado."""
    imagen = Image.new("RGB", (ANCHO, ALTO), PAPEL)
    dib = ImageDraw.Draw(imagen)
    claro = _mezclar(tono, PAPEL, 0.72)
    _degradado(dib, ALTO - 250, claro, tono)

    suelo = ALTO - 250
    if p.tipo == "lote":
        # Terreno: perímetro en perspectiva y el área como protagonista.
        dib.polygon(
            [(300, suelo - 60), (1300, suelo - 60), (1470, suelo - 330), (130, suelo - 330)],
            fill=tuple(max(0, c - 30) for c in tono), outline=(255, 255, 255), width=6,
        )
        texto = _ascii(f"{p.area_m2:.0f} m²")
        dib.text(
            ((ANCHO - dib.textlength(texto, font=_fuente(96))) / 2, suelo - 250),
            texto, font=_fuente(96), fill=(255, 255, 255),
        )
    elif p.tipo == "casa":
        base, techo = suelo - 60, suelo - 430
        dib.polygon([(420, base - 250), (800, techo), (1180, base - 250)],
                    fill=tuple(max(0, c - 55) for c in tono))
        dib.rectangle([460, base - 250, 1140, base], fill=(255, 255, 255))
        for i in range(2):
            for j in range(2):
                x, y = 540 + i * 340, base - 200 + j * 90
                dib.rectangle([x, y, x + 110, y + 62], fill=_mezclar(tono, PAPEL, 0.45))
        dib.rectangle([760, base - 110, 860, base], fill=tuple(max(0, c - 40) for c in tono))
    else:
        # Apartamento: torre con pisos; más pisos si el precio es más alto.
        pisos = 4 + min(6, p.precio // 150_000_000)
        alto_piso = 62
        base = suelo - 60
        cima = base - int(pisos) * alto_piso
        dib.rectangle([560, cima, 1040, base], fill=(255, 255, 255))
        for n in range(int(pisos)):
            y = base - (n + 1) * alto_piso + 12
            for k in range(4):
                x = 600 + k * 105
                dib.rectangle([x, y, x + 74, y + 38],
                              fill=_mezclar(tono, PAPEL, 0.5) if (n + k) % 3 else tono)
        dib.rectangle([740, base - 70, 860, base], fill=tuple(max(0, c - 40) for c in tono))

    _rotulo(dib, p)
    memoria = io.BytesIO()
    imagen.save(memoria, "PNG")
    return memoria.getvalue()


def _plano(p: Propiedad, tono) -> bytes:
    """Segunda imagen: esquema de distribución, para variar la galería."""
    imagen = Image.new("RGB", (ANCHO, ALTO), PAPEL)
    dib = ImageDraw.Draw(imagen)
    dib.rectangle([0, 0, ANCHO, ALTO - 250], fill=_mezclar(tono, PAPEL, 0.86))

    margen, alto_util = 140, ALTO - 250
    caja = [margen, 120, ANCHO - margen, alto_util - 120]
    dib.rectangle(caja, fill=(255, 255, 255), outline=tono, width=10)

    if p.tipo == "lote":
        dib.line([caja[0], caja[1], caja[2], caja[3]], fill=tono, width=4)
        dib.line([caja[0], caja[3], caja[2], caja[1]], fill=tono, width=4)
        texto = "LOTE SIN CONSTRUIR"
    else:
        # Rejilla de espacios: tantos como habitaciones más zonas comunes.
        espacios = max(2, int(p.habitaciones) + 2)
        columnas = 3 if espacios > 4 else 2
        filas = -(-espacios // columnas)
        ancho_c = (caja[2] - caja[0]) // columnas
        alto_f = (caja[3] - caja[1]) // filas
        nombres = ["Sala", "Cocina"] + [f"Alcoba {i}" for i in range(1, int(p.habitaciones) + 1)]
        for i in range(espacios):
            cx, cy = caja[0] + (i % columnas) * ancho_c, caja[1] + (i // columnas) * alto_f
            dib.rectangle([cx + 14, cy + 14, cx + ancho_c - 14, cy + alto_f - 14],
                          fill=_mezclar(tono, PAPEL, 0.93), outline=tono, width=4)
            if i < len(nombres):
                dib.text((cx + 40, cy + 40), _ascii(nombres[i]), font=_fuente(30), fill=TINTA)
        texto = _ascii(f"DISTRIBUCIÓN APROXIMADA · {p.area_m2:.0f} m²")

    dib.text(((ANCHO - dib.textlength(texto, font=_fuente(34))) / 2, alto_util - 90),
             texto, font=_fuente(34), fill=TINTA)
    _rotulo(dib, p)
    memoria = io.BytesIO()
    imagen.save(memoria, "PNG")
    return memoria.getvalue()


def cargar() -> tuple[int, int]:
    """Genera dos imágenes por inmueble demo que aún no tenga fotos."""
    con_imagenes = 0
    total = 0
    with sesion() as db:
        demo = list(
            db.scalars(
                select(Propiedad).where(Propiedad.fuente == FuentePropiedad.DEMO.value)
            )
        )
        for indice, p in enumerate(demo):
            if p.fotos:                       # idempotente: no duplica
                continue
            tono = TONOS.get(p.ciudad, TONOS["Medellín"])[indice % 5]
            guardadas = fotos.guardar(
                db, p,
                [
                    _Archivo(f"{p.id}-fachada.png", _fachada(p, tono)),
                    _Archivo(f"{p.id}-plano.png", _plano(p, tono)),
                ],
                actor="app.demo_imagenes",
            )
            if guardadas:
                con_imagenes += 1
                total += len(guardadas)
    return con_imagenes, total


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    inmuebles, imagenes = cargar()
    print(f"Inmuebles ilustrados: {inmuebles} · imágenes generadas: {imagenes}")
    print(
        "\nSon ILUSTRACIONES, no fotografías: llevan el sello «DEMO · INMUEBLE\n"
        "INVENTADO» impreso para que no puedan confundirse con un inmueble real."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
