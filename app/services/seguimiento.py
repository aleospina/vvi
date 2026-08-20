"""Seguimiento al comprador tras la presentación (PRD §10, HU-09).

El modelo de negocio cobra un 3% sobre venta confirmada, y la confirmación la
hace el asesor. Ahí está el hueco: el único que puede reportar el cierre es el
único que gana callándoselo. `leads.alertas_seguimiento` ya expone al asesor que
deja un negocio sin desenlace, pero un silencio prolongado no prueba nada.

Este módulo va por el otro lado de la mesa. **Al comprador no le cuesta nada
decir la verdad**: no le debe comisión a nadie, y a los quince días de haber
visitado un lote contesta con naturalidad si lo compró o no. Así que el sistema
le escribe —por el mismo canal por el que llegó y con la autorización que ya
dio— unos días después de haberlo presentado, y guarda lo que conteste.

Lo que se obtiene no es una prueba de fraude: es una **alerta temprana**. Si el
comprador dice que ya cerró y en el sistema no hay venta registrada, hay algo
que preguntar mientras la escritura todavía no se ha firmado.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.prompts import PLANTILLAS
from app.models import (
    ESTADOS_TERMINALES,
    Direccion,
    Prospecto,
    Seguimiento,
    Solicitud,
    Venta,
    ahora,
)
from app.services.compliance import auditar, tiene_consentimiento_vigente

log = logging.getLogger(__name__)

#: Resultados posibles de una pregunta de seguimiento.
CERRO = "cerro"
NO_CERRO = "no_cerro"
#: Hito de una declaración que llegó sola, sin que se le preguntara nada.
ESPONTANEO = -1
#: El hito venció sin preguntarse nada, porque otro más reciente lo adelantó.
OMITIDO = "omitido"


# ─────────────────────────── Qué toca preguntar ───────────────────────────


def _inmueble(solicitud: Solicitud) -> str:
    p = solicitud.propiedad
    if p is None:
        return "el inmueble que viste"
    zona = (p.zona or "").strip() or p.ciudad
    return f"el {p.tipo} de {zona}"


def redactar(solicitud: Solicitud) -> str:
    """La pregunta que se le manda al comprador.

    Una sola pregunta, abierta y sin rodeos. Preguntar "¿ya cerraste?" y nada
    más suena a cobrador; ofrecer seguir ayudando es lo que hace que conteste
    también quien todavía no ha comprado, que es la otra mitad de lo que
    interesa saber.
    """
    return PLANTILLAS["seguimiento_pregunta"].format(inmueble=_inmueble(solicitud))


def _ya_declaro_cierre(db: Session, prospecto: Prospecto) -> bool:
    return db.scalar(
        select(Seguimiento.id)
        .where(Seguimiento.prospecto_id == prospecto.id, Seguimiento.resultado == CERRO)
        .limit(1)
    ) is not None


def _se_le_puede_preguntar(db: Session, prospecto: Prospecto) -> bool:
    """¿Tiene sentido —y permiso— escribirle a este comprador?"""
    if not tiene_consentimiento_vigente(prospecto):
        return False
    if prospecto.estado_enum in ESTADOS_TERMINALES:
        # Vendido o perdido: el desenlace ya está declarado, preguntar sobra.
        return False
    return not _ya_declaro_cierre(db, prospecto)


def _hitos_vencidos(solicitud: Solicitud, ya_registrados: set[int], momento: datetime) -> list[int]:
    return [
        dias
        for dias in settings.hitos_seguimiento
        if dias not in ya_registrados
        and solicitud.creado_en + timedelta(days=dias) <= momento
    ]


def pendientes(db: Session, momento: datetime | None = None) -> list[tuple[Solicitud, list[int]]]:
    """Presentaciones a las que les toca pregunta, con los hitos que vencieron.

    Devuelve *todos* los hitos vencidos y no solo el que se va a preguntar: si
    el proceso estuvo caído dos semanas, los tres pueden estar vencidos a la vez
    y hay que dejar constancia de los tres, aunque solo se mande un mensaje.
    """
    momento = momento or ahora()
    if not settings.hitos_seguimiento:
        return []

    listas: list[tuple[Solicitud, list[int]]] = []
    for solicitud in db.scalars(select(Solicitud).order_by(Solicitud.creado_en)):
        registrados = set(
            db.scalars(
                select(Seguimiento.hito).where(Seguimiento.solicitud_id == solicitud.id)
            )
        )
        vencidos = _hitos_vencidos(solicitud, registrados, momento)
        if not vencidos:
            continue
        if not _se_le_puede_preguntar(db, solicitud.prospecto):
            continue
        listas.append((solicitud, vencidos))
    return listas


# ─────────────────────────── Preguntar ───────────────────────────


def _anotar(db: Session, solicitud: Solicitud, hito: int, *, enviado: bool) -> Seguimiento:
    registro = Seguimiento(
        prospecto_id=solicitud.prospecto_id,
        solicitud_id=solicitud.id,
        hito=hito,
        enviado_en=ahora() if enviado else None,
        resultado=None if enviado else OMITIDO,
    )
    db.add(registro)
    return registro


def ejecutar(db: Session, enviar=None) -> int:
    """Manda las preguntas que toquen. Devuelve cuántas salieron.

    De varios hitos vencidos a la vez se pregunta **el más reciente** y los
    demás se anotan como omitidos: tres mensajes seguidos al mismo comprador no
    son tres oportunidades de enterarse, son un motivo para que bloquee el
    número.
    """
    if enviar is None:  # import perezoso: `salida` arrastra los canales enteros
        from app.channels.salida import enviar as enviar_por_canal

        enviar = enviar_por_canal

    enviados = 0
    for solicitud, vencidos in pendientes(db):
        a_preguntar = vencidos[-1]
        for hito in vencidos:
            if hito != a_preguntar:
                _anotar(db, solicitud, hito, enviado=False)

        prospecto = solicitud.prospecto
        texto = redactar(solicitud)
        if not enviar(prospecto, texto):
            # Sin fila: un canal caído no puede consumir el hito en silencio, o
            # el comprador se quedaría sin la pregunta para siempre.
            db.flush()
            continue

        _anotar(db, solicitud, a_preguntar, enviado=True)
        # Import local: `leads` importa este módulo para el corte del turno.
        from app.services.leads import registrar_mensaje

        registrar_mensaje(db, prospecto, Direccion.SALIENTE, texto)
        auditar(
            db,
            actor="sistema",
            accion="seguimiento_enviado",
            entidad="prospecto",
            entidad_id=prospecto.codigo,
            detalle=f"solicitud={solicitud.id} hito={a_preguntar}d",
        )
        enviados += 1

    db.flush()
    return enviados


# ─────────────────────────── Escuchar la respuesta ───────────────────────────


def esperando_respuesta(db: Session, prospecto: Prospecto) -> Seguimiento | None:
    """La pregunta más reciente que se le hizo y que sigue sin contestar."""
    return db.scalar(
        select(Seguimiento)
        .where(
            Seguimiento.prospecto_id == prospecto.id,
            Seguimiento.enviado_en.is_not(None),
            Seguimiento.respondido_en.is_(None),
        )
        .order_by(Seguimiento.enviado_en.desc())
        .limit(1)
    )


def registrar_respuesta(db: Session, prospecto: Prospecto, resultado: str) -> Seguimiento | None:
    """Anota lo que el comprador declaró sobre el cierre.

    Se acepta aunque no haya pregunta abierta: un "ya compramos" espontáneo vale
    exactamente lo mismo que el que contesta al seguimiento, y perderlo por no
    haber preguntado primero sería absurdo.
    """
    registro = esperando_respuesta(db, prospecto)
    if registro is None:
        ultima = db.scalar(
            select(Solicitud)
            .where(Solicitud.prospecto_id == prospecto.id)
            .order_by(Solicitud.creado_en.desc())
            .limit(1)
        )
        if ultima is None:
            return None
        registro = db.scalar(
            select(Seguimiento).where(
                Seguimiento.solicitud_id == ultima.id,
                Seguimiento.hito == ESPONTANEO,
            )
        )
        if registro is None:
            registro = Seguimiento(
                prospecto_id=prospecto.id,
                solicitud_id=ultima.id,
                hito=ESPONTANEO,
                enviado_en=None,
            )
            db.add(registro)

    registro.respondido_en = ahora()
    registro.resultado = resultado
    db.flush()

    auditar(
        db,
        actor=f"titular:{prospecto.codigo}",
        accion="seguimiento_respondido",
        entidad="seguimiento",
        entidad_id=registro.id,
        detalle=f"resultado={resultado} solicitud={registro.solicitud_id}",
    )
    return registro


def cierres_declarados(db: Session) -> list[Seguimiento]:
    """Compradores que dicen haber cerrado y no tienen venta registrada.

    Es la única lista de este módulo que el operador tiene que mirar todos los
    días: cada fila es un negocio que ocurrió y que el sistema no vio.
    """
    # Las ventas se consultan de frente y no por la relación del prospecto: una
    # colección ya cargada en la sesión no se entera de la venta que se acaba de
    # registrar, y la alerta seguiría encendida sobre un negocio ya reportado.
    con_venta = set(db.scalars(select(Venta.prospecto_id)))
    return [
        s
        for s in db.scalars(
            select(Seguimiento)
            .where(Seguimiento.resultado == CERRO)
            .order_by(Seguimiento.respondido_en.desc())
        )
        if s.prospecto_id not in con_venta
    ]
