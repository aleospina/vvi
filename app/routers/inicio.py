"""Portada pública del sitio (`/`).

Hasta ahora `/` redirigía al panel, así que lo primero que veía cualquiera que
escribiera la dirección era un formulario de contraseña. Para un operador eso
es cómodo; para un comprador —que es quien llega desde una pauta, un enlace en
la biografía de Instagram o un listado de Marketplace— era una puerta cerrada
sin explicación, y no había forma de contarle qué es Inmoclick.

Esta página no autentica nada y no puede empezar a hacerlo: aplican las mismas
reglas que la vitrina (`routers.catalogo`).

  · **Solo entra lo publicable.** Los destacados salen de
    `portfolio.buscar_publicas`, que exige disponible y con mandato.
  · **Nada de PII.** Aquí no llega ni `propietario` ni su teléfono.
  · **Sin cartera no hay silencio.** Si el catálogo está apagado o vacío, la
    portada sigue existiendo y se queda con lo que sí es cierto: quiénes somos
    y cómo contactarnos. Una página en blanco se lee como un sitio roto.
"""

from __future__ import annotations

from typing import NamedTuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Propiedad, TipoInmueble
from app.routers.catalogo import ETIQUETAS_NEGOCIO, ETIQUETAS_TIPO
from app.services import portfolio
from app.vistas import contexto, plantillas

router = APIRouter(tags=["portada"])

#: Cuántas fichas se asoman en la portada. Seis llenan dos filas de tres en
#: escritorio; con más, la portada empieza a competir con la vitrina, que es
#: donde están los filtros.
DESTACADOS = 6

#: Cuántos municipios se ofrecen como acceso rápido. Los que no caben siguen
#: estando en el desplegable de la vitrina.
ZONAS = 8

#: Láminas dibujadas. Son el **respaldo**: se pintan solo mientras la cartera no
#: tenga fotografías propias —una instalación recién montada, o el catálogo
#: apagado—. Una portada sin imagen se lee como una página rota, y un dibujo de
#: la ciudad es mejor respuesta que un hueco gris. En cuanto hay fotos de
#: inmuebles reales, mandan ellas.
RESPALDO_CIMA = "/static/img/portada-ciudad.svg"
RESPALDO_PORQUE = "/static/img/fachada-alta.svg"
RESPALDO_ZONAS = (
    "/static/img/vista-torres.svg",
    "/static/img/vista-ladera.svg",
    "/static/img/vista-fachada.svg",
    "/static/img/vista-lotes.svg",
)


class Lamina(NamedTuple):
    """Una imagen grande de la portada y de dónde salió.

    `es_foto` no es decorativo: una fotografía y un dibujo no se recortan igual.
    El dibujo tiene la ciudad en la mitad de abajo y se encuadra bajo; una
    fotografía de una casa tiene el asunto en el centro. La plantilla usa esto
    para elegir el encuadre, no para decidir si la pinta.
    """

    url: str
    es_foto: bool


def _con_foto(propiedades: list[Propiedad]) -> list[Propiedad]:
    """Candidatas a lámina: las que tienen foto, casas primero y por importe.

    Dos criterios, y el orden entre ellos importa. **Primero la casa**: es lo
    que se reconoce como vivienda, y la foto de un terreno preside bien un
    listado de lotes pero no la portada de una inmobiliaria. **Después el
    importe**, que es un apaño honesto —no sabemos qué foto es mejor, pero el
    inmueble más caro suele ser el mejor fotografiado—.

    El desempate va dentro de cada grupo y no sobre la lista entera. Ordenarla
    entera por precio parece lo mismo y no lo es: en la cartera de hoy el
    inmueble más caro es un lote de 1.800 millones, así que el titular del sitio
    habría sido la fotografía de un terreno.

    Si no hay casas fotografiadas sirve cualquier inmueble: es una preferencia,
    no un filtro.
    """
    def por_importe(grupo):
        return sorted(grupo, key=lambda p: p.precio or 0, reverse=True)

    con = [p for p in propiedades if p.fotos]
    casas = [p for p in con if p.tipo == TipoInmueble.CASA.value]
    resto = [p for p in con if p.tipo != TipoInmueble.CASA.value]
    return por_importe(casas) + por_importe(resto)


def _laminas_grandes(publicables: list[Propiedad], usados: set[str]) -> tuple[Lamina, Lamina]:
    """Las dos fotografías grandes de la portada: el titular y el «por qué».

    Salen de la cartera y no de un nombre de archivo escrito a mano. Las fotos
    viven en un volumen con nombres aleatorios, así que una portada que apunta a
    uno concreto se rompe el día que el operador borra esa foto o reordena la
    galería —y se rompe en la primera pantalla del sitio, que es el peor sitio
    donde puede romperse algo—.

    De cada inmueble se toma **su portada**, que es la foto que un humano ya
    eligió como la que mejor lo representa. Cambiar la imagen del titular es
    entonces un clic en el panel, no un despliegue.
    """
    ordenadas = _con_foto(publicables)
    if not ordenadas:
        return Lamina(RESPALDO_CIMA, False), Lamina(RESPALDO_PORQUE, False)

    cima = ordenadas[0].fotos[0].url
    usados.add(ordenadas[0].id)

    # La segunda lámina busca otro inmueble: repetir la misma fotografía dos
    # veces en la misma página se lee como que solo hay una.
    if len(ordenadas) > 1:
        porque = ordenadas[1].fotos[0].url
        usados.add(ordenadas[1].id)
    else:
        galeria = ordenadas[0].fotos
        porque = (galeria[1] if len(galeria) > 1 else galeria[0]).url

    return Lamina(cima, True), Lamina(porque, True)


def _zonas(db: Session, publicables: list[Propiedad], demo: bool, usados: set[str]) -> list[dict]:
    """Los municipios con inventario, cada uno con una foto suya.

    La foto es de un inmueble que está de verdad en ese municipio, así que el
    mosaico dejó de ser decorativo: enseña la zona. Cuando no queda ninguna
    fotografía libre, cae al dibujo, que no afirma nada del municipio.
    """
    conteos = portfolio.conteo_publico_por_municipio(db, incluir_demo=demo)[:ZONAS]
    zonas = []
    for i, (ciudad, municipio, cuantos) in enumerate(conteos):
        # Se prefiere una foto que no esté ya en la página; si todas lo están,
        # se repite antes que caer al dibujo: la foto real informa más.
        aqui = [p for p in _con_foto(publicables) if portfolio.municipio_de(p) == municipio]
        libres = [p for p in aqui if p.id not in usados]
        elegido = (libres or aqui or [None])[0]
        if elegido is not None:
            # La miniatura basta: el mosaico pinta el mayor a 380px de ancho.
            lamina = elegido.portada
            usados.add(elegido.id)
        else:
            lamina = RESPALDO_ZONAS[i % len(RESPALDO_ZONAS)]
        zonas.append({
            "ciudad": ciudad,
            "municipio": municipio,
            "total": cuantos,
            "lamina": lamina,
        })
    return zonas


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def portada(request: Request, db: Session = Depends(get_db)):
    """Página de entrada. Pública: ni pide sesión ni la mira para decidir qué hay."""
    destacados: list = []
    zonas: list = []
    total = 0
    lamina_cima = Lamina(RESPALDO_CIMA, False)
    lamina_porque = Lamina(RESPALDO_PORQUE, False)

    # Con la vitrina apagada no se consulta la cartera siquiera: la portada no
    # puede enseñar inmuebles a los que luego responde 404.
    if settings.catalogo_publico:
        demo = settings.catalogo_muestra_demo
        publicables = portfolio.buscar_publicas(db, orden="nuevo", incluir_demo=demo)
        total = len(publicables)
        destacados = publicables[:DESTACADOS]
        # `usados` viaja entre las dos: lo que ya preside el titular no vuelve a
        # salir en el mosaico de zonas mientras quede otra cosa que enseñar.
        usados: set[str] = set()
        lamina_cima, lamina_porque = _laminas_grandes(publicables, usados)
        # Los municipios vienen ya contados y ordenados por volumen: la portada
        # ofrece los que de verdad tienen inventario, no una lista de deseos.
        zonas = _zonas(db, publicables, demo, usados)

    return plantillas.TemplateResponse(
        request,
        "inicio.html",
        contexto(
            request,
            destacados=destacados,
            zonas=zonas,
            total=total,
            lamina_cima=lamina_cima,
            lamina_porque=lamina_porque,
            etiquetas_tipo=ETIQUETAS_TIPO,
            etiquetas_negocio=ETIQUETAS_NEGOCIO,
            comision_pct=settings.comision_pct,
        ),
    )
