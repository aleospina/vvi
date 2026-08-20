"""Hora del negocio: se guarda en UTC, se muestra en Colombia.

`models.ahora` guarda UTC ingenuo y no puede dejar de hacerlo: de esa marca sale
el texto que encadena los hashes de la auditoría (`compliance.sello`), y una
fecha con desplazamiento entraría y saldría distinta de SQLite. Pero lo que se
guarda y lo que se lee son cosas distintas: al operador —y al comprador— hay que
mostrarles su hora. Una solicitud recibida a las 10:47 pm figuraba como "20/08
03:47", de madrugada y del día siguiente, así que ni el operador la reconocía
como suya.

Todo lo que se pinte en una pantalla o se mande por chat pasa por aquí.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Colombia no aplica horario de verano desde 1993, así que el desplazamiento es
#: fijo. Un `ZoneInfo("America/Bogota")` diría exactamente lo mismo y a cambio
#: exigiría la base de datos IANA, que Windows no trae de fábrica.
ZONA_COLOMBIA = timezone(timedelta(hours=-5), "COT")

FORMATO = "%d/%m/%Y %H:%M"


def en_local(momento: datetime) -> datetime:
    """Pasa un instante guardado a la hora de Colombia.

    Lo que viene sin zona se lee como UTC, que es como lo guarda `models.ahora`.
    """
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(ZONA_COLOMBIA)


def fecha(momento: datetime | None, formato: str = FORMATO) -> str:
    """La hora local ya formateada. Sin fecha, un guion: nunca "None"."""
    if momento is None:
        return "—"
    return en_local(momento).strftime(formato)
