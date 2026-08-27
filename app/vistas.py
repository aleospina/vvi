"""Entorno de plantillas y contexto de las páginas públicas.

La vitrina (`routers.catalogo`) y la portada (`routers.inicio`) pintan el mismo
cascarón —cabecera, isotipo, pie, aviso de datos personales— y comparten los
mismos filtros de Jinja. Vivían en el router del catálogo, que fue donde
nacieron; al aparecer una segunda página pública la alternativa era duplicar el
registro de filtros, y eso significa que cambiar cómo se escribe un precio
arregla la mitad del sitio.

Aquí no se decide nada de negocio: solo se monta el entorno y se arma el
diccionario que toda página pública necesita.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import estaticos
from app.channels.gateway import pesos
from app.config import RAIZ, settings
from app.security.sesion import quien_mira
from app.services import portfolio
from app.tiempo import fecha

plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"))
# Cuelga la huella del CSS de la URL, o el navegador sigue pintando la hoja
# anterior después de un despliegue (ver `app.estaticos`).
estaticos.registrar(plantillas)
plantillas.env.filters["pesos"] = pesos
# Las plantillas no arman URLs ni deducen municipios por su cuenta: usan las
# mismas funciones que el resto del sistema, para que cambiar la forma de la
# URL canónica siga siendo un cambio en un solo archivo.
plantillas.env.filters["ruta"] = portfolio.ruta_publica
plantillas.env.filters["municipio"] = portfolio.municipio_de
# Todo se guarda en UTC; al comprador hay que mostrarle su hora. Un inmueble
# publicado a las 10 de la noche figuraba como del día siguiente.
plantillas.env.filters["fecha"] = fecha


def contexto(request: Request, **extra) -> dict:
    """Lo que toda página pública necesita saber para pintarse.

    Ninguna de estas páginas autentica nada —son públicas y así deben seguir—,
    pero sí cambian lo que ofrecen cuando quien mira ya tiene sesión: el camino
    de vuelta al panel y el atajo a la ficha interna del inmueble que está
    viendo. Sin esto, el operador que abre la vitrina queda encerrado.
    """
    usuario, es_operador = quien_mira(request.cookies)
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
        # La portada existe siempre; la vitrina puede estar apagada. Las
        # plantillas comunes —cabecera y pie— tienen que poder dejar de ofrecer
        # enlaces a un catálogo que responde 404.
        "hay_vitrina": settings.catalogo_publico,
        **extra,
    }
