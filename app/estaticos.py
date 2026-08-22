"""Huella de los archivos estáticos, para invalidar la caché del navegador.

El problema
-----------
`<link href="/static/estilos.css">` es una URL que nunca cambia, y StaticFiles
no manda `cache-control`. El navegador aplica entonces su caché heurística y
sigue pintando la hoja anterior: un despliegue con cambios de maquetación se ve
igual que antes, y desde el lado del usuario el arreglo sencillamente «no
funcionó». Ya pasó una vez con la fila de filtros de la vitrina.

La solución
-----------
Colgarle a la URL una huella del contenido: `/static/estilos.css?v=a1b2c3d4`.
Mientras el archivo no cambie la URL es la misma y la caché sirve —que es lo
que queremos—; en cuanto cambia, la URL es otra y el navegador la pide de
nuevo. Sin cabeceras, sin despliegues especiales y sin desactivar la caché,
que sería tirar el bebé con el agua.

Se calcula una sola vez al importar: el archivo no cambia mientras el proceso
vive, y hacerlo por petición sería leer el disco en cada página.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.config import RAIZ

log = logging.getLogger(__name__)

_ESTATICOS = RAIZ / "app" / "static"


def _huella(archivo: Path) -> str:
    """Ocho caracteres del SHA-256 del contenido. Suficiente para una caché.

    Si el archivo no está —un despliegue a medias, un volumen mal montado— se
    devuelve una huella fija en vez de reventar: una hoja de estilos con la
    versión equivocada es un problema estético; que no arranque la aplicación,
    no.
    """
    try:
        return hashlib.sha256(archivo.read_bytes()).hexdigest()[:8]
    except OSError:
        log.warning("No se pudo leer %s para la huella de caché.", archivo.name)
        return "0"


#: Versión de la hoja de estilos. Las plantillas la reciben como `v_estilos`.
VERSION_ESTILOS = _huella(_ESTATICOS / "estilos.css")


def registrar(plantillas) -> None:
    """Deja `v_estilos` disponible en un entorno de plantillas.

    Lo llaman los tres routers que sirven HTML. Está aquí y no repetido en cada
    uno para que añadir un cuarto sea una línea y no una decisión.
    """
    plantillas.env.globals["v_estilos"] = VERSION_ESTILOS
