"""Ajustes operativos que se cambian sin volver a desplegar.

Casi toda la configuración vive en variables de entorno y está bien que así sea:
son secretos, o decisiones que se toman una vez. Pero unas pocas cambian en
medio de una prueba —«ahora quiero probar con el celular de mi socio»— y
obligar a editar el `.env` y reiniciar el proceso para eso convierte un cambio
de treinta segundos en un despliegue.

Estos ajustes se guardan en la base y **pisan** al `.env`. La variable de
entorno sigue siendo el valor de arranque: mientras nadie toque el ajuste desde
la interfaz, manda ella.

La distinción entre "no hay fila" y "hay fila vacía" es deliberada: sin fila se
usa el `.env`; con fila vacía se respeta el vacío, porque borrar la lista desde
la interfaz es una decisión, no un descuido.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.db import sesion
from app.models import Ajuste, ahora
from app.services.compliance import auditar

log = logging.getLogger(__name__)

#: Lista blanca de números de prueba del canal de WhatsApp.
NUMEROS_PRUEBA = "evolution_numeros_prueba"

#: Indicativo de Colombia. Un celular local escrito como lo escribe cualquiera
#: —«3001234567»— no coincide con el «573001234567» que manda WhatsApp, y el
#: resultado es que el operador pone su número y el bot lo sigue ignorando.
INDICATIVO = "57"


def _leer(db: Session, clave: str) -> str | None:
    ajuste = db.get(Ajuste, clave)
    return None if ajuste is None else ajuste.valor


def guardar(db: Session, clave: str, valor: str, *, actor: str) -> None:
    """Fija el ajuste y deja constancia de quién lo cambió."""
    ajuste = db.get(Ajuste, clave)
    if ajuste is None:
        ajuste = Ajuste(clave=clave, valor=valor)
        db.add(ajuste)
    else:
        ajuste.valor = valor
    ajuste.actualizado_por = actor
    ajuste.actualizado_en = ahora()
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="ajuste_cambiado",
        entidad="ajuste",
        entidad_id=clave,
        detalle=f"valor={valor or '(vacío)'}",
    )


def normalizar_numero(bruto: str) -> str:
    """Deja solo dígitos y le pone el indicativo a un celular colombiano.

    "+57 300 123 4567", "300 123 4567" y "573001234567" son el mismo teléfono, y
    el único que sirve para comparar contra lo que llega de WhatsApp es el
    último.
    """
    digitos = "".join(c for c in bruto if c.isdigit())
    if len(digitos) == 10 and digitos.startswith("3"):
        return INDICATIVO + digitos
    return digitos


def normalizar_lista(bruto: str) -> str:
    """Normaliza una lista separada por comas, sin repetidos y en orden."""
    numeros = {
        normalizar_numero(trozo) for trozo in bruto.split(",") if any(c.isdigit() for c in trozo)
    }
    return ",".join(sorted(n for n in numeros if n))


def numeros_prueba(db: Session | None = None) -> frozenset[str]:
    """La lista blanca vigente: la de la base si existe, si no la del `.env`."""
    if db is None:
        with sesion() as propia:
            guardado = _leer(propia, NUMEROS_PRUEBA)
    else:
        guardado = _leer(db, NUMEROS_PRUEBA)

    bruto = settings.evolution_numeros_prueba if guardado is None else guardado
    return frozenset(n for n in normalizar_lista(bruto).split(",") if n)


def desde_el_entorno(db: Session | None = None) -> bool:
    """¿La lista vigente sigue siendo la del `.env` y nadie la ha tocado?"""
    if db is None:
        with sesion() as propia:
            return _leer(propia, NUMEROS_PRUEBA) is None
    return _leer(db, NUMEROS_PRUEBA) is None
