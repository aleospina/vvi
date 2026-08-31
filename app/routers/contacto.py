"""La puerta de WhatsApp del sitio público (`/wa`).

Toda consulta que sale de la vitrina pasa por aquí en vez de llevar el teléfono
escrito en el `href`. No es un rodeo gratuito; compra tres cosas:

  · **El número no se publica.** En la página no hay ningún teléfono que copiar,
    ni en el texto ni en el enlace. Quien pulsa el botón habla con nosotros; los
    robots que recorren el sitio buscando números para venderlos, no.
  · **El destino se decide al pulsar, no al pintar.** El asistente virtual vive
    en el número que se vinculó con el QR, y ese número cambia sin desplegar
    nada. Resolverlo en el momento del clic significa que una página guardada en
    caché ayer sigue llevando a quien atiende hoy.
  · **Siempre hay alguien al otro lado.** Si el asistente no está vinculado
    —canal caído, QR sin escanear— la consulta cae en el número de respaldo, que
    contesta una persona. El botón nunca lleva a un WhatsApp muerto.

Es una ruta pública y anónima: no lee la sesión, no escribe nada y no registra
quién pulsó. Lo único que hace es mandar al visitante a wa.me.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from app.channels import whatsapp_evo
from app.config import settings

router = APIRouter(tags=["contacto"])

#: Lo que va escrito en la casilla de WhatsApp cuando se abre la conversación.
#: El asistente entiende cualquier cosa, pero un mensaje ya redactado quita el
#: peor momento de todos —la pantalla en blanco— y hace que el primer turno
#: llegue con intención en vez de un «hola» suelto.
SALUDO = "Hola, quiero información sobre un inmueble."

#: Cuánto de un código de inmueble se acepta. El código viaja dentro del texto
#: del mensaje, así que se recorta y se limpia: aquí no entra nada que no sea el
#: identificador de una ficha.
LARGO_REF = 40


def destino() -> str:
    """A qué número van hoy las consultas.

    Tres escalones, y el orden importa: lo que un humano configuró a mano manda
    sobre lo que se deduce, y lo deducido manda sobre el último recurso.
    """
    if settings.whatsapp_contacto:
        return settings.whatsapp_contacto
    return whatsapp_evo.numero_vinculado() or settings.whatsapp_respaldo


def _texto(ref: str) -> str:
    """El mensaje ya escrito, con la ficha desde la que se pulsó si la hay."""
    codigo = "".join(c for c in ref if c.isalnum() or c == "-")[:LARGO_REF]
    if not codigo:
        return SALUDO
    return f"Hola, quiero información sobre el inmueble {codigo}."


@router.get("/wa", include_in_schema=False)
def escribir(ref: str = Query("", description="Código del inmueble desde el que se consulta")):
    """Manda a WhatsApp con la conversación ya abierta."""
    respuesta = RedirectResponse(
        f"https://wa.me/{destino()}?text={quote(_texto(ref))}",
        status_code=302,
    )
    # Un 302 es cacheable por defecto en algunos intermediarios, y este destino
    # caduca en cuanto se vincula otro número. Nadie debe guardarlo.
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta
