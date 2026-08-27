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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
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

#: Láminas de las zonas, en el orden en que se reparten. No hay una imagen por
#: municipio —serían fotos que no tenemos— sino cuatro escenas que se turnan:
#: torres, ladera, fachada y suelo por construir. Cambiar el orden cambia qué
#: escena le toca a cada zona, y da igual: ninguna afirma nada del municipio.
LAMINAS = (
    "vista-torres.svg",
    "vista-ladera.svg",
    "vista-fachada.svg",
    "vista-lotes.svg",
)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def portada(request: Request, db: Session = Depends(get_db)):
    """Página de entrada. Pública: ni pide sesión ni la mira para decidir qué hay."""
    destacados: list = []
    zonas: list = []
    total = 0

    # Con la vitrina apagada no se consulta la cartera siquiera: la portada no
    # puede enseñar inmuebles a los que luego responde 404.
    if settings.catalogo_publico:
        demo = settings.catalogo_muestra_demo
        publicables = portfolio.buscar_publicas(db, orden="nuevo", incluir_demo=demo)
        total = len(publicables)
        destacados = publicables[:DESTACADOS]
        # Los municipios vienen ya contados y ordenados por volumen: la portada
        # ofrece los que de verdad tienen inventario, no una lista de deseos.
        zonas = portfolio.conteo_publico_por_municipio(db, incluir_demo=demo)[:ZONAS]

    return plantillas.TemplateResponse(
        request,
        "inicio.html",
        contexto(
            request,
            destacados=destacados,
            zonas=zonas,
            total=total,
            laminas=LAMINAS,
            etiquetas_tipo=ETIQUETAS_TIPO,
            etiquetas_negocio=ETIQUETAS_NEGOCIO,
            comision_pct=settings.comision_pct,
        ),
    )
