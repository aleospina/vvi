"""Catálogo público de inmuebles (vitrina en `/inmuebles`).

Es la única parte del sistema que responde sin sesión, y por eso tiene sus
propias reglas:

  · **Solo entra lo publicable.** Toda consulta pasa por `portfolio.es_publicable`
    —disponible y con mandato—. Un inmueble de demostración o de referencia no
    existe para un desconocido, y responder 404 en vez de 403 evita además que
    alguien enumere la cartera probando códigos.
  · **Nada de PII.** Las plantillas de esta vitrina no reciben `propietario` ni
    `propietario_telefono`. El contacto va siempre por nuestro formulario, no
    exponiendo el teléfono del dueño.
  · **La entrada de datos sigue siendo opt-in.** El formulario «Me interesa»
    desemboca en `prospecting.ingerir_lead`, el mismo punto único de entrada de
    PII que usan la landing de campaña y Meta Lead Ads (ADR-01, RF-16).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.channels.gateway import pesos
from app.config import RAIZ, settings
from app.db import get_db
from app.models import ROTULO_PRECIO, TipoInmueble, TipoNegocio
from app.services import portfolio, prospecting
from app.security.sesion import COOKIE, OPERADOR, rol_de, validar_token
from app.services.compliance import texto_consentimiento
from app.services.prospecting import ConsentimientoAusente

log = logging.getLogger(__name__)

router = APIRouter(prefix="/inmuebles", tags=["catálogo público"])
plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"))
plantillas.env.filters["pesos"] = pesos
# Las plantillas no arman URLs ni deducen municipios por su cuenta: usan las
# mismas funciones que el resto del sistema, para que cambiar la forma de la
# URL canónica siga siendo un cambio en un solo archivo.
plantillas.env.filters["ruta"] = portfolio.ruta_publica
plantillas.env.filters["municipio"] = portfolio.municipio_de

#: Etiquetas de los tipos en plural, para las pestañas de la vitrina.
ETIQUETAS_TIPO = {"casa": "Casas", "apartamento": "Apartamentos", "lote": "Lotes"}

#: Negocios como los nombra el comprador. El orden es el de la pestaña.
ETIQUETAS_NEGOCIO = {
    TipoNegocio.VENTA.value: "En venta",
    TipoNegocio.ARRIENDO.value: "En arriendo",
    TipoNegocio.PERMUTA.value: "En permuta",
}

ETIQUETAS_ORDEN = (
    ("nuevo", "Más nuevo"),
    ("precio_asc", "Menor precio"),
    ("precio_desc", "Mayor precio"),
)


def vitrina_activa() -> None:
    """Con el catálogo apagado, la vitrina entera deja de existir.

    Un 404 y no un 503: si el negocio decidió no tener escaparate público, no
    hay nada que anunciar sobre él.
    """
    if not settings.catalogo_publico:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")


Vitrina = Depends(vitrina_activa)


def _entero(texto: str) -> int | None:
    """'480.000.000', '$480 millones' → 480000000. Vacío o sin dígitos → None.

    El comprador escribe el precio como lo lee, con puntos y con signo de pesos.
    Rechazar eso con un error de validación sería castigarlo por escribir bien.
    """
    digitos = "".join(c for c in (texto or "") if c.isdigit())
    return int(digitos) if digitos else None


def _quien_mira(request: Request) -> tuple[str | None, bool]:
    """(usuario, es_operador) de la sesión, si la hay.

    La vitrina no autentica nada —es pública y así debe seguir—, pero sí cambia
    lo que ofrece cuando quien mira ya tiene sesión: el camino de vuelta al
    panel y el atajo a la ficha interna del inmueble que está viendo. Sin esto,
    el operador que abre la vitrina queda en un callejón sin salida.
    """
    usuario = validar_token(request.cookies.get(COOKIE))
    rol = rol_de(usuario) if usuario else None
    if usuario is None or rol is None:
        return None, False
    return usuario, rol == OPERADOR


def _contexto(request: Request, **extra) -> dict:
    usuario, es_operador = _quien_mira(request)
    return {
        "request": request,
        # Solo deciden qué enlaces se pintan. No abren ningún dato: la ficha
        # pública no recibe PII ni con sesión, y editar sigue exigiendo pasar
        # por las dependencias del router del dashboard.
        "sesion": usuario,
        "puede_editar": es_operador,
        "empresa": settings.empresa_nombre,
        "politica": settings.politica_privacidad_url,
        "ciudades": settings.ciudades_cobertura,
        "whatsapp": settings.whatsapp_contacto,
        # Con la puerta de demo abierta la vitrina muestra inmuebles que no
        # existen: se marca `noindex` para que ningún buscador los recoja, y la
        # plantilla pinta un aviso permanente. Es deliberadamente difícil de
        # dejar puesto sin darse cuenta.
        "modo_demo": settings.catalogo_muestra_demo,
        "base_publica": settings.url_publica,
        **extra,
    }


# ─────────────────────────── Listado ───────────────────────────


@router.get("", response_class=HTMLResponse, dependencies=[Vitrina])
def vitrina(
    request: Request,
    tipo: str = Query("", description="casa | apartamento | lote"),
    negocio: str = Query("", description="venta | arriendo | permuta"),
    municipio: str = Query("", description="Pereira, Dosquebradas, Envigado…"),
    # Los alias son cortos porque van en una URL que la gente comparte; los
    # nombres de Python son largos para no tapar los builtins `min` y `max`.
    precio_desde: str = Query("", alias="min", description="Precio desde"),
    precio_hasta: str = Query("", alias="max", description="Precio hasta"),
    hab: int = Query(0, ge=0, le=20, description="Alcobas mínimas"),
    banos: int = Query(0, ge=0, le=20, description="Baños mínimos"),
    q: str = Query("", max_length=120, description="Código, barrio o texto libre"),
    orden: str = Query("nuevo"),
    pagina: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    """Vitrina con filtros, orden y paginación.

    Un valor inventado en la URL nunca vacía el listado en silencio: se ignora
    y se muestra todo, que es lo que el visitante espera al ver la pestaña
    «Todos» resaltada. Una pantalla en blanco sin explicación se lee como que
    la inmobiliaria no tiene inventario.
    """
    demo = settings.catalogo_muestra_demo

    tipo = tipo.strip().lower()
    if tipo not in {t.value for t in TipoInmueble}:
        tipo = ""

    negocios = portfolio.conteo_publico_por_negocio(db, incluir_demo=demo)
    negocio = negocio.strip().lower()
    if negocio not in negocios:
        negocio = ""
    # Con inventario de un solo negocio, la pestaña sobra: ofrecer «En arriendo»
    # sabiendo que da cero es prometer algo que no hay. Se fija el único que
    # existe para que el rango de precio tenga una escala definida.
    if not negocio and len(negocios) == 1:
        negocio = next(iter(negocios))

    municipios = portfolio.conteo_publico_por_municipio(db, incluir_demo=demo)
    municipio = municipio.strip()
    if municipio and municipio not in {m for _, m, _ in municipios}:
        municipio = ""

    if orden not in portfolio.ORDENES:
        orden = "nuevo"

    precio_min, precio_max = _entero(precio_desde), _entero(precio_hasta)
    # Rango al revés (desde 500, hasta 300) no devuelve nada y parece un error
    # del sitio. Se endereza, que es lo que la persona quiso decir.
    if precio_min is not None and precio_max is not None and precio_min > precio_max:
        precio_min, precio_max = precio_max, precio_min

    encontrados = portfolio.buscar_publicas(
        db,
        tipo=tipo or None,
        negocio=negocio or None,
        municipio=municipio or None,
        precio_min=precio_min,
        precio_max=precio_max,
        habitaciones=hab or None,
        banos=banos or None,
        texto=q or None,
        orden=orden,
        incluir_demo=demo,
    )

    por_pagina = settings.catalogo_por_pagina if settings.catalogo_por_pagina > 0 else 12
    # Siempre hay al menos una página, aunque esté vacía: la vitrina sin
    # resultados tiene que poder explicarse, no desaparecer.
    paginas = max(1, -(-len(encontrados) // por_pagina))
    pagina = min(pagina, paginas)
    desde = (pagina - 1) * por_pagina

    return plantillas.TemplateResponse(
        request,
        "catalogo.html",
        _contexto(
            request,
            inmuebles=encontrados[desde : desde + por_pagina],
            total=len(encontrados),
            pagina=pagina,
            paginas=paginas,
            conteo_tipos=portfolio.conteo_publico_por_tipo(db, incluir_demo=demo),
            conteo_negocios=negocios,
            municipios=municipios,
            etiquetas_tipo=ETIQUETAS_TIPO,
            etiquetas_negocio=ETIQUETAS_NEGOCIO,
            # Sin negocio elegido el rango de precio compara cánones con
            # precios de venta, así que la etiqueta lo dice en vez de fingir
            # que "desde/hasta" significa lo mismo para todo el listado.
            rotulo_precio=(
                ROTULO_PRECIO.get(negocio, "Precio") if negocio else "Precio"
            ),
            ordenes=ETIQUETAS_ORDEN,
            filtros={
                "tipo": tipo, "negocio": negocio, "municipio": municipio,
                "min": precio_desde.strip(), "max": precio_hasta.strip(),
                "hab": hab, "banos": banos, "q": q.strip(), "orden": orden,
            },
            hay_filtro=bool(
                tipo or municipio or precio_min or precio_max or hab or banos or q.strip()
                # El negocio autoseleccionado por ser el único no cuenta como
                # filtro puesto: «Quitar filtros» no debe insinuar que esconde
                # inventario que no existe.
                or (negocio and len(negocios) > 1)
            ),
        ),
    )


# ─────────────────────────── Ficha ───────────────────────────


def _buscar_o_404(db: Session, codigo: str):
    propiedad = portfolio.publicada(
        db, codigo, incluir_demo=settings.catalogo_muestra_demo
    )
    if propiedad is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese inmueble ya no está publicado.")
    return propiedad


@router.get("/{codigo}", response_class=HTMLResponse, dependencies=[Vitrina])
def ficha_sin_slug(codigo: str, db: Session = Depends(get_db)):
    """`/inmuebles/PROP-PER-001` → la URL canónica con su parte legible.

    Es la forma corta que resulta cómoda de dictar por teléfono o de pegar en
    un chat; el 301 la convierte en la única versión que indexa un buscador.
    """
    return RedirectResponse(
        portfolio.ruta_publica(_buscar_o_404(db, codigo)),
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


def _ficha(request: Request, db: Session, slug: str, codigo: str, **extra):
    propiedad = _buscar_o_404(db, codigo)
    canonica = portfolio.ruta_publica(propiedad)

    # El slug es decorativo, pero si cambió la zona el enlace viejo sigue vivo
    # y apuntando a un texto que ya no describe el inmueble. Redirigir mantiene
    # una sola URL indexable y evita contenido duplicado.
    if slug != portfolio.slug_de(propiedad):
        return RedirectResponse(canonica, status_code=status.HTTP_301_MOVED_PERMANENTLY)

    datos = {
        "p": propiedad,
        "municipio": portfolio.municipio_de(propiedad),
        "rotulo_precio": ROTULO_PRECIO.get(propiedad.negocio, "Precio"),
        "etiquetas_negocio": ETIQUETAS_NEGOCIO,
        "canonica": canonica,
        "similares": portfolio.similares(
            db, propiedad, incluir_demo=settings.catalogo_muestra_demo
        ),
        "texto_consentimiento": texto_consentimiento(),
        "enviado": False,
        "error": "",
        # `extra` va al final para que la respuesta del formulario pueda
        # sobrescribir `enviado` y `error` sin chocar con estos valores.
        **extra,
    }
    return plantillas.TemplateResponse(
        request, "catalogo_ficha.html", _contexto(request, **datos)
    )


@router.get("/{slug}/{codigo}", response_class=HTMLResponse, dependencies=[Vitrina])
def ficha(slug: str, codigo: str, request: Request, db: Session = Depends(get_db)):
    """Ficha pública. No muestra propietario ni su teléfono: eso es PII (RF-17)."""
    return _ficha(request, db, slug, codigo)


@router.post("/{slug}/{codigo}", response_class=HTMLResponse, dependencies=[Vitrina])
def interes(
    slug: str,
    codigo: str,
    request: Request,
    nombre: str = Form(...),
    telefono: str = Form(...),
    mensaje: str = Form(""),
    autorizo: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """«Me interesa este inmueble» → prospecto con consentimiento registrado.

    Mismo contrato que la landing de campaña: sin la casilla marcada no se
    guarda absolutamente nada del visitante.
    """
    propiedad = _buscar_o_404(db, codigo)

    if autorizo.lower() not in ("on", "true", "1", "si", "sí"):
        return _ficha(
            request, db, slug, codigo,
            error=(
                "Necesitamos tu autorización expresa para poder contactarte. "
                "Sin ella no guardamos ningún dato."
            ),
        )

    # El código del inmueble encabeza el mensaje para que el asesor sepa por
    # cuál preguntan sin abrir la conversación, y para que el motor de
    # calificación arranque con la zona y el precio ya sobre la mesa.
    contexto_inmueble = (
        f"Interesado en el inmueble {propiedad.id}: {propiedad.tipo} en "
        f"{propiedad.zona or portfolio.municipio_de(propiedad)}, "
        f"{propiedad.ciudad} por {pesos(propiedad.precio)}."
    )

    try:
        resultado = prospecting.ingerir_lead(
            db,
            red="catalogo",
            canal_id=f"catalogo:{codigo}:{telefono}",
            consentimiento=True,
            evidencia=(
                f"Casilla de autorización marcada en la ficha pública "
                f"/inmuebles/{slug}/{codigo}"
            ),
            nombre=nombre,
            telefono=telefono,
            campana=f"inmueble:{codigo}",
            mensaje=f"{contexto_inmueble} {mensaje}".strip(),
            actor="catalogo",
        )
    except ConsentimientoAusente as exc:
        return _ficha(request, db, slug, codigo, error=str(exc))

    return _ficha(
        request, db, slug, codigo,
        enviado=True,
        codigo_prospecto=resultado.prospecto.codigo,
    )
