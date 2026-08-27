"""Láminas de la portada pública: `python -m app.laminas`

Por qué ilustraciones y no fotografías
--------------------------------------
La portada necesita imagen grande y la inmobiliaria todavía no tiene fotografía
propia de la ciudad. Comprar banco de imágenes resolvía el hueco y creaba otro:
las torres de archivo que se venden como «real estate» son de Miami o de Dubái,
y presidir con ellas la portada de una inmobiliaria de Medellín y Pereira es
prometer un sitio que no es este. Estas láminas dibujan lo que sí hay —el valle
mirado desde la ladera, las lomas cerrando el fondo, el caserío escalonado— con
la paleta de la interfaz, y las seis juntas pesan menos que una sola fotografía.

Ninguna afirma nada sobre un inmueble concreto: las de las zonas se turnan por
orden y no retratan al municipio que rotulan. El día que haya fotografía propia,
sustituirlas es cambiar el `src` de la plantilla.

Se regeneran a mano, no en cada arranque: la semilla del generador está fija, así
que el resultado es el mismo archivo byte a byte mientras no se toque el código.
Correrlo sobrescribe los SVG de `app/static/img/`.
"""

from __future__ import annotations

import io
import math
import os
import random

from app.config import RAIZ

SALIDA = os.path.join(str(RAIZ), "app", "static", "img")

NOCHE = "#04121b"
TINTA = "#0a3348"
PETROLEO = "#0f4661"
CIAN = "#1fd5ff"
CIAN_S = "#7fe8ff"
CALIDO = "#ffcf9a"
CALIDO_F = "#ffb066"


def grad(id_, paradas, x1=0, y1=0, x2=0, y2=900):
    t = [f'<linearGradient id="{id_}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"'
         f' gradientUnits="userSpaceOnUse">']
    for parada in paradas:
        off, color = parada[0], parada[1]
        op = f' stop-opacity="{parada[2]}"' if len(parada) > 2 else ""
        t.append(f'<stop offset="{off}" stop-color="{color}"{op}/>')
    t.append("</linearGradient>")
    return "".join(t)


def ventanas(rng, x, ancho, cima, base, paso_x=17, paso_y=23, w=8, h=11,
             prob=0.5, margen=9, opacidad=1.0):
    """Rejilla de ventanas de un edificio.

    Se dibuja ventana a ventana y no con un `<pattern>` porque lo que hace que
    una fachada parezca habitada es la irregularidad: un patrón repetido se lee
    como papel pintado.
    """
    tonos = ((CALIDO, 0.85), (CALIDO_F, 0.62), (CIAN_S, 0.5))
    pesos = (6, 2, 3)
    piezas = []
    util = ancho - 2 * margen
    cols = int((util + (paso_x - w)) // paso_x)
    if cols < 1:
        return piezas
    usado = cols * paso_x - (paso_x - w)
    x0 = x + (ancho - usado) / 2
    y = cima + 15
    while y + h < base - 6:
        for c in range(cols):
            if rng.random() > prob:
                continue
            color, alfa = rng.choices(tonos, weights=pesos)[0]
            a = round(alfa * opacidad * rng.uniform(0.5, 1.0), 2)
            if a < 0.05:
                continue
            piezas.append(
                f'<rect x="{round(x0 + c * paso_x, 1)}" y="{round(y, 1)}"'
                f' width="{w}" height="{h}" fill="{color}" opacity="{a}"/>')
        y += paso_y
    return piezas


def cresta(rng, y_base, alto, ancho=1600, alto_lienzo=900, pasos=9):
    """Perfil de montaña: la cordillera que cierra el valle en las dos ciudades."""
    puntos = []
    for i in range(pasos + 1):
        x = ancho * i / pasos
        y = y_base - alto * (0.35 + 0.65 * abs(math.sin(i * 1.7 + rng.random())))
        puntos.append((round(x, 1), round(y, 1)))
    d = [f"M0 {alto_lienzo} L0 {puntos[0][1]}"]
    for i in range(1, len(puntos)):
        x0, y0 = puntos[i - 1]
        x1, y1 = puntos[i]
        xm = round((x0 + x1) / 2, 1)
        d.append(f"C{xm} {y0} {xm} {y1} {x1} {y1}")
    d.append(f"L{ancho} {alto_lienzo} Z")
    return " ".join(d)


def cabecera(w, h, etiqueta):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"'
            f' width="{w}" height="{h}" preserveAspectRatio="xMidYMid slice"'
            f' role="img" aria-label="{etiqueta}">')


# ─────────────────────────── Portada ───────────────────────────

def portada_ciudad():
    """Lámina de fondo del titular: la ciudad al atardecer, vista desde la ladera."""
    rng = random.Random(20260827)
    W, H = 1600, 900
    p = [cabecera(W, H, "Ciudad al atardecer")]

    p.append("<defs>")
    # El cielo se aclara hacia el horizonte, que está a 660: por debajo empieza
    # la ciudad y no tiene sentido seguir subiendo de luz.
    p.append(grad("cielo", [(0, "#03101a"), (0.26, "#07293a"), (0.5, PETROLEO),
                            (0.72, "#2a7f9c"), (0.88, "#5aa8b4"), (1, "#8dc3c0")],
                  y2=700))
    p.append('<radialGradient id="ocaso" cx="0" cy="0" r="1"'
             ' gradientUnits="userSpaceOnUse"'
             ' gradientTransform="translate(1040 655) scale(880 330)">'
             f'<stop offset="0" stop-color="#ffe3bd" stop-opacity=".72"/>'
             f'<stop offset=".38" stop-color="{CALIDO}" stop-opacity=".34"/>'
             f'<stop offset=".72" stop-color="{CALIDO_F}" stop-opacity=".12"/>'
             f'<stop offset="1" stop-color="{CALIDO_F}" stop-opacity="0"/>'
             '</radialGradient>')
    p.append(grad("bruma", [(0, "#2e88a3", 0), (1, "#4fa3b4", 0.5)], y1=420, y2=700))
    p.append(grad("suelo", [(0, TINTA, 0), (1, "#03101a", 0.92)], y1=690, y2=900))
    p.append(grad("vidrio", [(0, "#0a3d55"), (1, "#04202f")], x1=1215, x2=1447, y1=0, y2=0))
    p.append(grad("ladera", [(0, "#04161f"), (1, "#010a0f")], y1=760, y2=900))
    p.append("</defs>")

    p.append(f'<rect width="{W}" height="{H}" fill="url(#cielo)"/>')
    p.append(f'<rect width="{W}" height="{H}" fill="url(#ocaso)"/>')

    # Estrellas: pocas y solo arriba, donde el cielo todavía es noche.
    for _ in range(70):
        x, y = rng.uniform(0, W), rng.uniform(10, 330)
        a = round(rng.uniform(0.15, 0.6) * (1 - y / 380), 2)
        if a > 0.05:
            p.append(f'<circle cx="{round(x)}" cy="{round(y)}"'
                     f' r="{round(rng.uniform(0.6, 1.5), 1)}" fill="#dff4ff" opacity="{a}"/>')

    # Cordillera. Es la mitad del retrato de estas dos ciudades: sin las lomas
    # cerrando el fondo, el horizonte podría ser el de cualquier sitio.
    p.append(f'<path d="{cresta(rng, 640, 210)}" fill="#3c8ba0" opacity=".38"/>')
    p.append(f'<path d="{cresta(rng, 700, 165)}" fill="#175d79" opacity=".6"/>')
    p.append(f'<rect width="{W}" height="{H}" fill="url(#bruma)"/>')

    # Tres capas de ciudad. La perspectiva aérea la hace la opacidad de las
    # ventanas y el tono del volumen, no un desenfoque: un filtro en SVG cuesta
    # caro de pintar y aquí la lámina es el fondo de la primera pantalla.
    #
    # Ninguna capa sube más allá de 380: el horizonte encendido es lo que hace
    # que esto sea un atardecer y no una noche cerrada, y si la ciudad se come
    # el cielo también se come la hora.
    capas = (
        dict(color="#0c4058", cima=(660, 760), ancho=(60, 130), prob=0.28, op=0.4),
        dict(color="#082c3f", cima=(545, 690), ancho=(80, 165), prob=0.4, op=0.72),
        dict(color="#05202f", cima=(385, 610), ancho=(105, 205), prob=0.55, op=1.0),
    )
    for capa in capas:
        x = -70.0
        while x < W + 40:
            ancho = rng.uniform(*capa["ancho"])
            cima = rng.uniform(*capa["cima"])
            p.append(f'<rect x="{round(x, 1)}" y="{round(cima, 1)}"'
                     f' width="{round(ancho, 1)}" height="{round(H - cima, 1)}"'
                     f' fill="{capa["color"]}"/>')
            # Remate: unos edificios llevan corona y otros antena. Sin esto la
            # línea del cielo es una fila de cajas y se nota.
            remate = rng.random()
            if remate < 0.25:
                p.append(f'<rect x="{round(x + ancho * 0.28, 1)}" y="{round(cima - 16, 1)}"'
                         f' width="{round(ancho * 0.44, 1)}" height="16" fill="{capa["color"]}"/>')
            elif remate < 0.38:
                p.append(f'<rect x="{round(x + ancho / 2 - 1.5, 1)}" y="{round(cima - 38, 1)}"'
                         f' width="3" height="38" fill="{capa["color"]}"/>')
                p.append(f'<circle cx="{round(x + ancho / 2, 1)}" cy="{round(cima - 38, 1)}"'
                         f' r="2.6" fill="{CALIDO_F}" opacity=".75"/>')
            p.extend(ventanas(rng, x, ancho, cima, H, prob=capa["prob"], opacidad=capa["op"]))
            x += ancho + rng.uniform(4, 26)

    # Torre protagonista: acristalada, en primer plano y a la derecha, que es
    # donde el titular de la portada ya no pisa.
    tx, ta, tc = 1215, 232, 300
    p.append(f'<rect x="{tx}" y="{tc}" width="{ta}" height="{H - tc}" fill="url(#vidrio)"/>')
    for i in range(1, 6):
        p.append(f'<rect x="{tx + i * ta // 6}" y="{tc}" width="1.5" height="{H - tc}"'
                 f' fill="{CIAN_S}" opacity=".1"/>')
    p.extend(ventanas(rng, tx, ta, tc, H, paso_x=19, paso_y=26, w=10, h=13, prob=0.5))
    p.append(f'<rect x="{tx}" y="{tc}" width="{ta}" height="4" fill="{CIAN}" opacity=".3"/>')

    p.append(f'<rect width="{W}" height="{H}" fill="url(#suelo)"/>')

    # Ladera en primer término. La toma es la que cualquiera reconoce en estas
    # dos ciudades —el valle mirado desde arriba—, y de paso da el escalón de
    # profundidad que le falta a una ciudad dibujada de frente.
    loma = ["M0 900 L0 806"]
    ondas = [(0, 806), (230, 838), (470, 800), (700, 846), (940, 812), (1180, 854),
             (1420, 818), (1600, 848)]
    for i in range(1, len(ondas)):
        x0, y0 = ondas[i - 1]
        x1, y1 = ondas[i]
        xm = (x0 + x1) / 2
        loma.append(f"C{xm} {y0} {xm} {y1} {x1} {y1}")
    loma.append("L1600 900 Z")
    p.append(f'<path d="{" ".join(loma)}" fill="url(#ladera)"/>')
    # Casas de la loma: unas pocas luces sueltas, cerca del borde.
    for _ in range(34):
        x = rng.uniform(20, W - 20)
        y = rng.uniform(848, 892)
        p.append(f'<rect x="{round(x)}" y="{round(y)}" width="3" height="3"'
                 f' fill="{CALIDO}" opacity="{round(rng.uniform(0.25, 0.75), 2)}"/>')
    p.append("</svg>")
    return "".join(p)


# ─────────────────────── Vistas de las zonas ───────────────────────

def vista_torres():
    """Zona de altura: torres nuevas contra el cielo del final de la tarde."""
    rng = random.Random(11)
    W, H = 800, 560
    p = [cabecera(W, H, "Torres de vivienda")]
    p.append("<defs>")
    p.append(grad("vt-cielo", [(0, "#0b3a52"), (0.55, "#1a6a89"), (1, "#7bb9c4")], y2=H))
    p.append(grad("vt-suelo", [(0, TINTA, 0), (1, "#04192550".replace("50", ""), 0.85)],
                  y1=380, y2=H))
    p.append("</defs>")
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vt-cielo)"/>')
    p.append(f'<path d="{cresta(rng, 430, 110, ancho=W, alto_lienzo=H, pasos=6)}"'
             f' fill="#0d4560" opacity=".6"/>')
    for color, cima, ancho, prob, op in (("#0a3348", (300, 380), (55, 95), 0.3, 0.5),
                                         ("#061f2d", (140, 290), (70, 130), 0.55, 1.0)):
        x = -40.0
        while x < W + 20:
            a = rng.uniform(*ancho)
            c = rng.uniform(*cima)
            p.append(f'<rect x="{round(x, 1)}" y="{round(c, 1)}" width="{round(a, 1)}"'
                     f' height="{round(H - c, 1)}" fill="{color}"/>')
            p.extend(ventanas(rng, x, a, c, H, paso_x=15, paso_y=21, w=7, h=10,
                              prob=prob, opacidad=op))
            x += a + rng.uniform(6, 22)
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vt-suelo)"/>')
    p.append("</svg>")
    return "".join(p)


def vista_ladera():
    """Barrio de ladera a media mañana: casas bajas escalonadas sobre el verde."""
    rng = random.Random(23)
    W, H = 800, 560
    p = [cabecera(W, H, "Barrio de ladera")]
    p.append("<defs>")
    # Media tarde y no mediodía: el cielo pálido de antes dejaba la mitad de la
    # lámina en blanco y el rótulo del municipio, que va encima, sin contraste.
    p.append(grad("vl-cielo", [(0, "#3d7f97"), (0.45, "#8ab6bd"), (1, "#e2c9a6")], y2=380))
    p.append(grad("vl-verde", [(0, "#2a5f52"), (1, "#123a34")], y1=200, y2=H))
    p.append(grad("vl-tarde", [(0, "#2b3f4d", 0), (1, "#0a2a33", 0.45)], y1=330, y2=H))
    p.append("</defs>")
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vl-cielo)"/>')
    p.append(f'<path d="{cresta(rng, 210, 105, ancho=W, alto_lienzo=H, pasos=5)}"'
             f' fill="#3c6f83" opacity=".6"/>')
    p.append(f'<path d="{cresta(rng, 300, 88, ancho=W, alto_lienzo=H, pasos=6)}"'
             f' fill="url(#vl-verde)"/>')

    # Casas escalonadas. Tres hileras y no seis: apretadas se leen como una
    # textura de tejados, y lo que tiene que reconocerse es la casa.
    tejados = ("#a8492f", "#963d26", "#b25e3e", "#7f3623")
    muros = ("#ece5da", "#ded5c7", "#f3ece1", "#cfc4b3")
    for fila, (base, escala) in enumerate(((318, 0.72), (404, 0.95), (520, 1.25))):
        x = -40.0
        while x < W + 30:
            a = rng.uniform(52, 84) * escala
            alto = rng.uniform(34, 50) * escala
            cima = base - alto
            p.append(f'<rect x="{round(x, 1)}" y="{round(cima, 1)}" width="{round(a, 1)}"'
                     f' height="{round(alto, 1)}" fill="{rng.choice(muros)}"/>')
            p.append(f'<path d="M{round(x - 5 * escala, 1)} {round(cima, 1)}'
                     f' L{round(x + a / 2, 1)} {round(cima - 16 * escala, 1)}'
                     f' L{round(x + a + 5 * escala, 1)} {round(cima, 1)} Z"'
                     f' fill="{rng.choice(tejados)}"/>')
            # Ventanas: dos por casa como mucho, o la fachada se vuelve un tablero.
            for i in range(rng.randint(1, 2)):
                p.append(f'<rect x="{round(x + 11 * escala + i * 22 * escala, 1)}"'
                         f' y="{round(cima + 13 * escala, 1)}"'
                         f' width="{round(10 * escala, 1)}" height="{round(12 * escala, 1)}"'
                         f' fill="{TINTA}" opacity=".4"/>')
            # Árboles entre casas: el verde tiene que meterse entre el caserío o
            # la ladera se lee como un pueblo de maqueta.
            hueco = rng.uniform(14, 40) * escala
            if hueco > 26 * escala:
                tx = x + a + hueco / 2
                p.append(f'<circle cx="{round(tx, 1)}" cy="{round(base - 15 * escala, 1)}"'
                         f' r="{round(13 * escala, 1)}" fill="#25604f"/>')
            x += a + hueco
        # Sombra al pie de cada hilera: es lo que da el escalón de la ladera.
        p.append(f'<rect x="0" y="{base}" width="{W}" height="{round(H - base, 1)}"'
                 f' fill="#0f322d" opacity="{round(0.24 + fila * 0.1, 2)}"/>')
    # La luz de la tarde cae sobre todo el conjunto: sin ella las tres hileras
    # se leen como tres bandas de color y no como una ladera.
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vl-tarde)"/>')
    p.append("</svg>")
    return "".join(p)


def vista_fachada():
    """Zona consolidada: fachada de vidrio en escorzo, con el cielo reflejado."""
    rng = random.Random(37)
    W, H = 800, 560
    p = [cabecera(W, H, "Fachada de vidrio")]
    p.append("<defs>")
    p.append(grad("vf-fondo", [(0, "#0d4a66"), (1, "#062330")], y2=H))
    p.append(grad("vf-reflejo", [(0, "#8fe3ff", 0.55), (0.45, "#2a8fb5", 0.25),
                                 (1, "#0a3348", 0.05)], x2=W, y2=H))
    p.append("</defs>")
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vf-fondo)"/>')
    # Losas de balcón en escorzo: la diagonal es la que sugiere la altura.
    y = -90.0
    while y < H + 120:
        sesgo = 78
        p.append(f'<path d="M0 {round(y, 1)} L{W} {round(y - sesgo, 1)}'
                 f' L{W} {round(y - sesgo + 26, 1)} L0 {round(y + 26, 1)} Z"'
                 f' fill="#0a3348" opacity=".55"/>')
        p.append(f'<path d="M0 {round(y + 26, 1)} L{W} {round(y - sesgo + 26, 1)}'
                 f' L{W} {round(y - sesgo + 30, 1)} L0 {round(y + 30, 1)} Z"'
                 f' fill="{CIAN_S}" opacity=".18"/>')
        y += 62
    # Montantes verticales.
    for x in range(0, W + 1, 46):
        p.append(f'<rect x="{x}" y="0" width="2" height="{H}" fill="#03161f" opacity=".45"/>')
    # Cristales encendidos, sueltos: es lo que hace que el edificio esté vivo.
    for _ in range(46):
        gx = rng.randrange(0, W, 46)
        gy = rng.randrange(-60, H, 62)
        p.append(f'<path d="M{gx} {round(gy - gx * 0.098, 1)} L{gx + 44} {round(gy - (gx + 44) * 0.098, 1)}'
                 f' L{gx + 44} {round(gy - (gx + 44) * 0.098 + 26, 1)} L{gx} {round(gy - gx * 0.098 + 26, 1)} Z"'
                 f' fill="{rng.choice((CALIDO, CIAN_S, CALIDO_F))}"'
                 f' opacity="{round(rng.uniform(0.1, 0.4), 2)}"/>')
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vf-reflejo)"/>')
    p.append("</svg>")
    return "".join(p)


def vista_lotes():
    """Suelo por construir: lotes y vías vistos desde arriba, al amanecer."""
    rng = random.Random(53)
    W, H = 800, 560
    p = [cabecera(W, H, "Lotes y suelo por construir")]
    p.append("<defs>")
    p.append(grad("vlo-fondo", [(0, "#2c6355"), (0.5, "#1f5148"), (1, "#123a34")], y2=H))
    p.append(grad("vlo-luz", [(0, "#ffcf9a", 0.22), (1, "#062330", 0.55)], x2=W, y2=H))
    p.append("</defs>")
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vlo-fondo)"/>')
    # Parcelario: rectángulos en dos direcciones, como un plano de loteo.
    verdes = ("#337058", "#2a614f", "#3d7f60", "#255449", "#458a67")
    y = -40.0
    while y < H + 40:
        alto = rng.uniform(48, 92)
        x = -50.0
        while x < W + 40:
            ancho = rng.uniform(55, 130)
            p.append(f'<rect x="{round(x, 1)}" y="{round(y, 1)}" width="{round(ancho, 1)}"'
                     f' height="{round(alto, 1)}" fill="{rng.choice(verdes)}"'
                     f' transform="skewX(-7)"/>')
            x += ancho + 3
        y += alto + 3
    # Vías: dos ejes claros que ordenan el conjunto.
    p.append(f'<path d="M-60 {H} L300 -40 L360 -40 L20 {H} Z" fill="#d9cdb8" opacity=".38"'
             f' transform="skewX(-7)"/>')
    p.append(f'<path d="M-60 320 L{W + 60} 250 L{W + 60} 292 L-60 362 Z" fill="#d9cdb8" opacity=".32"/>')
    # Un par de construcciones ya levantadas, para que se lea como suelo con futuro.
    for cx, cy, a, h in ((150, 380, 62, 44), (250, 420, 48, 34), (610, 200, 70, 50)):
        p.append(f'<rect x="{cx}" y="{cy}" width="{a}" height="{h}" fill="#f2ece1" opacity=".92"/>')
        p.append(f'<path d="M{cx - 4} {cy} L{cx + a / 2} {cy - 15} L{cx + a + 4} {cy} Z" fill="#a8442a"/>')
    p.append(f'<rect width="{W}" height="{H}" fill="url(#vlo-luz)"/>')
    p.append("</svg>")
    return "".join(p)


# ─────────────────────── Lamina de la seccion «por que» ───────────────────────

def fachada_alta():
    """Vertical: una torre acristalada mirada desde el pie, contra el cielo."""
    rng = random.Random(71)
    W, H = 900, 1200
    p = [cabecera(W, H, "Torre de vivienda vista desde el pie")]
    p.append("<defs>")
    p.append(grad("fa-cielo", [(0, "#0a3348"), (0.4, "#1a6a89"), (1, "#a8d6de")], y2=H))
    p.append(grad("fa-torre", [(0, "#04202f"), (0.55, "#0a3d55"), (1, "#062634")],
                  x1=0, x2=W, y1=0, y2=0))
    p.append(grad("fa-pie", [(0, "#0a3348", 0), (1, "#03121b", 0.75)], y1=900, y2=H))
    p.append("</defs>")
    p.append(f'<rect width="{W}" height="{H}" fill="url(#fa-cielo)"/>')
    # Nubes altas, apenas insinuadas.
    for cy, cr, op in ((190, 210, 0.14), (420, 260, 0.1), (700, 300, 0.08)):
        p.append(f'<ellipse cx="{rng.randint(120, 780)}" cy="{cy}" rx="{cr}" ry="{cr / 5}"'
                 f' fill="#eaf7ff" opacity="{op}"/>')

    # Torre vecina, detrás y a la izquierda: una sola no da escala ni ciudad.
    p.append('<path d="M40 330 L250 300 L300 1200 L-20 1200 Z" fill="#0a3348" opacity=".55"/>')
    for i in range(14):
        yv = 350 + i * 62
        p.append(f'<path d="M{round(45 + i * 1.2, 1)} {yv} L{round(252 + i * 3.4, 1)} {yv - 4}'
                 f' L{round(253 + i * 3.4, 1)} {yv + 26} L{round(46 + i * 1.2, 1)} {yv + 30} Z"'
                 f' fill="#04202f" opacity=".45"/>')

    # La torre en perspectiva: los pisos convergen hacia arriba. La cumbre no
    # toca el borde superior a propósito —un edificio recortado por el marco
    # pierde justo lo que se quiere enseñar, que es lo alto que es—.
    izq_pie, der_pie, izq_cima, der_cima = 150, 800, 330, 640
    cima, pie = 205, H
    # Corona: el remate y su antena, sobre el cielo limpio.
    p.append(f'<path d="M{izq_cima - 14} {cima} L{der_cima + 14} {cima}'
             f' L{der_cima + 8} {cima - 26} L{izq_cima - 8} {cima - 26} Z" fill="#04202f"/>')
    p.append(f'<rect x="{(izq_cima + der_cima) // 2 - 2}" y="{cima - 96}" width="4" height="70"'
             f' fill="#04202f"/>')
    p.append(f'<circle cx="{(izq_cima + der_cima) // 2}" cy="{cima - 96}" r="5"'
             f' fill="{CALIDO_F}" opacity=".85"/>')
    p.append(f'<path d="M{izq_cima} {cima} L{der_cima} {cima} L{der_pie} {pie} L{izq_pie} {pie} Z"'
             f' fill="url(#fa-torre)"/>')
    pisos = 26
    for i in range(pisos + 1):
        t = i / pisos
        y = cima + (pie - cima) * t
        xi = izq_cima + (izq_pie - izq_cima) * t
        xd = der_cima + (der_pie - der_cima) * t
        p.append(f'<path d="M{round(xi, 1)} {round(y, 1)} L{round(xd, 1)} {round(y, 1)}'
                 f' L{round(xd, 1)} {round(y + 7 + 9 * t, 1)} L{round(xi, 1)} {round(y + 7 + 9 * t, 1)} Z"'
                 f' fill="#03161f" opacity=".55"/>')
        # Antepecho iluminado: el filete cian es lo único que delata la hora.
        p.append(f'<path d="M{round(xi, 1)} {round(y, 1)} L{round(xd, 1)} {round(y, 1)}'
                 f' L{round(xd, 1)} {round(y + 2.5, 1)} L{round(xi, 1)} {round(y + 2.5, 1)} Z"'
                 f' fill="{CIAN_S}" opacity="{round(0.3 - 0.2 * t, 2)}"/>')
        if i < pisos:
            cols = 9
            for c in range(cols):
                if rng.random() > 0.42:
                    continue
                u0 = c / cols
                u1 = (c + 0.62) / cols
                y2 = cima + (pie - cima) * ((i + 1) / pisos)
                xi2 = izq_cima + (izq_pie - izq_cima) * ((i + 1) / pisos)
                xd2 = der_cima + (der_pie - der_cima) * ((i + 1) / pisos)
                ax, bx = xi + (xd - xi) * u0, xi + (xd - xi) * u1
                cx, dx = xi2 + (xd2 - xi2) * u0, xi2 + (xd2 - xi2) * u1
                p.append(f'<path d="M{round(ax, 1)} {round(y + 9, 1)} L{round(bx, 1)} {round(y + 9, 1)}'
                         f' L{round(dx, 1)} {round(y2 - 2, 1)} L{round(cx, 1)} {round(y2 - 2, 1)} Z"'
                         f' fill="{rng.choice((CALIDO, CALIDO, CIAN_S))}"'
                         f' opacity="{round(rng.uniform(0.12, 0.5), 2)}"/>')
    p.append(f'<rect width="{W}" height="{H}" fill="url(#fa-pie)"/>')
    p.append("</svg>")
    return "".join(p)


def escribir(nombre, contenido):
    ruta = os.path.join(SALIDA, nombre)
    io.open(ruta, "w", encoding="utf8", newline="\n").write(contenido)
    print(f"{nombre}: {len(contenido) / 1024:.0f} KB")


if __name__ == "__main__":
    escribir("portada-ciudad.svg", portada_ciudad())
    escribir("vista-torres.svg", vista_torres())
    escribir("vista-ladera.svg", vista_ladera())
    escribir("vista-fachada.svg", vista_fachada())
    escribir("vista-lotes.svg", vista_lotes())
    escribir("fachada-alta.svg", fachada_alta())
