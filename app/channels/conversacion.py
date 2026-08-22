"""Orquestación de la conversación, sin saber en qué canal ocurre (ADR-02).

`gateway` resuelve un turno cuando ya hay consentimiento y recibe la sesión de
base de datos desde fuera: es lógica pura. Este módulo pone lo que falta para
atender a una persona de principio a fin —la puerta de consentimiento, los
derechos de habeas data y la sesión de base de datos— **sin una sola referencia
a Telegram o a WhatsApp**.

Antes esto vivía dentro del bot de Telegram. Mientras hubo un solo canal daba
igual; con dos, tener la máquina de consentimiento duplicada significa que
arreglar un derecho del titular en un canal lo deja roto en el otro. Un adaptador
de canal ahora solo traduce transporte: recibe un mensaje, llama aquí, envía los
textos que le devuelvan.

Invariante de cumplimiento (RF-19): mientras el titular no autorice, su
conversación no toca la base de datos. Los identificadores en espera viven solo
en memoria y **como índice ciego**, nunca en claro: en WhatsApp el identificador
de canal es el número de teléfono, que es dato personal — en Telegram era un
`chat_id` anónimo y esa distinción es fácil de pasar por alto.
"""

from __future__ import annotations

import logging

from app.channels import gateway
from app.channels.gateway import MensajeEntrante
from app.config import settings
from app.db import sesion
from app.llm.prompts import PLANTILLAS
from app.models import Direccion
from app.security.crypto import enmascarar, indice_ciego
from app.services import leads
from app.services.compliance import revocar_y_anonimizar, tiene_consentimiento_vigente
from app.tiempo import fecha

log = logging.getLogger(__name__)

#: Identificadores a los que ya se les envió el aviso de IA y de los que se
#: espera respuesta de consentimiento. Solo en memoria y solo el índice ciego.
_PENDIENTES: set[tuple[str, str]] = set()


def _clave(canal: str, cid: str) -> tuple[str, str]:
    return (canal, indice_ciego(cid))


def esta_pendiente(canal: str, cid: str) -> bool:
    """¿Se le pidió autorización a este identificador y aún no responde?"""
    return _clave(canal, cid) in _PENDIENTES


def marcar_pendiente(canal: str, cid: str) -> None:
    _PENDIENTES.add(_clave(canal, cid))


def olvidar_pendiente(canal: str, cid: str) -> None:
    _PENDIENTES.discard(_clave(canal, cid))


# ─────────────────────────── Consentimiento ───────────────────────────


def iniciar(canal: str, cid: str) -> list[str]:
    """Arranque de conversación (/start o equivalente): aviso de IA y solicitud."""
    marcar_pendiente(canal, cid)
    return [gateway.mensaje_bienvenida()]


def aceptar_consentimiento(
    canal: str,
    cid: str,
    *,
    nombre: str | None = None,
    usuario: str | None = None,
    telefono: str | None = None,
) -> list[str]:
    """Registra la autorización y da de alta al prospecto (RF-16).

    Es el único camino por el que la PII entra a la base de datos.
    """
    olvidar_pendiente(canal, cid)
    with sesion() as db:
        entrante = MensajeEntrante(
            canal=canal,
            canal_id=cid,
            texto="",
            nombre=nombre,
            telefono=telefono,
            usuario_canal=usuario,
            red_origen=canal,
        )
        prospecto = gateway.alta_con_consentimiento(
            db,
            entrante,
            evidencia=f"{canal}:canal_id_hash · respuesta afirmativa del titular",
        )
        salidas = [
            "¡Gracias! Tu autorización quedó registrada. 🔐",
            gateway.pregunta_de_calificacion(prospecto),
        ]
        for s in salidas:
            leads.registrar_mensaje(db, prospecto, Direccion.SALIENTE, s)
        return salidas


def rechazar_consentimiento(canal: str, cid: str) -> list[str]:
    olvidar_pendiente(canal, cid)
    return [gateway.rechazo_consentimiento()]


# ─────────────────────────── Turno completo ───────────────────────────


def turno(
    canal: str,
    cid: str,
    texto: str,
    *,
    nombre: str | None = None,
    usuario: str | None = None,
    telefono: str | None = None,
) -> list[str]:
    """Un turno de conversación, incluida la puerta de consentimiento.

    Bloqueante: abre sesión de base de datos y puede llamar al LLM. Los canales
    asíncronos deben invocarlo fuera del bucle de eventos.
    """
    with sesion() as db:
        prospecto = leads.buscar_por_canal(db, canal, cid)
        # Hay conversación abierta solo si autorizó **y** no se ha despedido.
        # Quien cerró con un "hasta luego" vuelve por la misma puerta que un
        # desconocido: su siguiente mensaje es el primero de otra conversación.
        if (
            prospecto is not None
            and tiene_consentimiento_vigente(prospecto)
            and not gateway.conversacion_cerrada(prospecto)
        ):
            return gateway.procesar(db, prospecto, texto).textos

    # A partir de aquí no hay conversación abierta: o nunca autorizó, o él mismo
    # la cerró. En el primer caso, además, nada se persiste.
    from app.services.nlu_engine import es_afirmativo, es_despedida, es_negativo

    if esta_pendiente(canal, cid):
        if es_afirmativo(texto):
            return aceptar_consentimiento(
                canal, cid, nombre=nombre, usuario=usuario, telefono=telefono
            )
        if es_negativo(texto):
            return rechazar_consentimiento(canal, cid)
        # Despedirse en la puerta también es una respuesta: insistir con la
        # autorización a quien ya dijo "gracias, chao" es no escucharlo.
        if es_despedida(texto):
            olvidar_pendiente(canal, cid)
            return [PLANTILLAS["despedida"]]
        return [
            "Necesito tu autorización explícita para continuar. ¿Autorizas el "
            "tratamiento de tus datos? Responde *Sí* o *No*."
        ]

    return iniciar(canal, cid)


# ─────────────────────────── Derechos del titular ───────────────────────────


def mis_datos(canal: str, cid: str) -> str:
    """Derecho de consulta del titular (habeas data)."""
    with sesion() as db:
        fichas = leads.buscar_todos_por_canal(db, canal, cid)
        if not fichas:
            return "No tengo ningún dato tuyo almacenado. 🙌"
        p = fichas[-1]
        # Quien ya compró y volvió a buscar tiene más de una ficha. Callarse las
        # anteriores sería contestar a medias justo la pregunta que el titular
        # tiene derecho a hacer.
        anteriores = (
            f"\n\nGuardo además {len(fichas) - 1} búsqueda(s) anterior(es) tuya(s); "
            "/borrar las elimina todas."
            if len(fichas) > 1
            else ""
        )
        return (
            f"*Tus datos en {settings.empresa_nombre}* (código {p.codigo})\n\n"
            f"• Nombre: {enmascarar(p.nombre)}\n"
            f"• Usuario: {enmascarar(p.usuario_canal)}\n"
            f"• Teléfono: {enmascarar(p.telefono)}\n"
            f"• Ciudad: {p.ciudad or '—'} · Tipo: {p.tipo or '—'}\n"
            f"• Presupuesto: {gateway.pesos(p.presupuesto_max) if p.presupuesto_max else '—'}\n"
            f"• Estado: {p.estado}\n"
            f"• Autorización: {'vigente desde ' + fecha(p.consentimiento_ts, '%d/%m/%Y') if p.consentimiento_ts else 'no otorgada'}\n\n"
            f"Tus datos de contacto están cifrados. Política: {settings.politica_privacidad_url}\n"
            "Para eliminarlos escribe /borrar." + anteriores
        )


def borrar_datos(canal: str, cid: str) -> str:
    """Revocación y anonimización a petición del titular (habeas data).

    Alcanza a **todas** sus fichas, no solo a la de la conversación en curso:
    quien ya compró y volvió a buscar tiene dos, y un borrado que dejara la
    primera intacta no sería un borrado.
    """
    with sesion() as db:
        fichas = leads.buscar_todos_por_canal(db, canal, cid)
        if not fichas:
            return "No tengo datos tuyos que eliminar. 🙌"
        for p in fichas:
            revocar_y_anonimizar(db, p, actor=f"titular:{p.codigo}")
        olvidar_pendiente(canal, cid)
        return (
            "Listo: eliminé tus datos de contacto y revoqué la autorización. "
            "Queda únicamente el registro de auditoría exigido por la ley, sin datos "
            "que te identifiquen. Si algún día quieres volver, escribe /start."
        )


def pedir_asesor(canal: str, cid: str) -> str:
    """Handoff explícito a una persona (RF-12)."""
    with sesion() as db:
        p = leads.buscar_por_canal(db, canal, cid)
        if p is None or not tiene_consentimiento_vigente(p):
            return (
                "Para pasarte con un asesor necesito primero tu autorización. "
                "Escribe /start y seguimos. 🙌"
            )
        leads.solicitar_handoff(db, p, tipo="asesor", detalle="Solicitado por el titular")
        return PLANTILLAS["handoff"].format(empresa=settings.empresa_nombre)
