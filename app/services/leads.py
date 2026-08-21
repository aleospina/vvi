"""Ciclo de vida del prospecto: alta, conversación, estados y handoff.

Cubre RF-12, RF-13 y la parte de atribución de RF-15.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ESTADOS_EN_CURSO,
    TRANSICIONES,
    Canal,
    Direccion,
    EstadoProspecto,
    Etiqueta,
    Mensaje,
    Prospecto,
    Solicitud,
    ahora,
)
from app.security.crypto import indice_ciego
from app.services import notificaciones
from app.services.compliance import auditar, exigir_consentimiento
from app.services.nlu_engine import Analisis


class TransicionInvalida(ValueError):
    """Se intentó un salto de estado que la máquina no permite (RF-13)."""


def siguiente_codigo(db: Session) -> str:
    total = db.scalar(select(func.count()).select_from(Prospecto)) or 0
    return f"LEAD-{total + 1:06d}"


def buscar_por_canal(db: Session, canal: str, canal_id: str) -> Prospecto | None:
    """Busca por índice ciego: nunca descifra toda la tabla."""
    return db.scalar(
        select(Prospecto).where(
            Prospecto.canal == canal,
            Prospecto.canal_id_hash == indice_ciego(canal_id),
        )
    )


def crear(
    db: Session,
    *,
    canal: Canal | str,
    canal_id: str | None = None,
    canal_id_claro: str | None = None,
    nombre: str | None = None,
    telefono: str | None = None,
    usuario_canal: str | None = None,
    campana: str | None = None,
    red_origen: str | None = None,
    actor: str = "sistema",
) -> Prospecto:
    """Da de alta un prospecto. Solo debe llamarse tras registrar consentimiento."""
    canal_valor = canal.value if isinstance(canal, Canal) else str(canal)
    prospecto = Prospecto(
        codigo=siguiente_codigo(db),
        canal=canal_valor,
        canal_id_hash=indice_ciego(canal_id) if canal_id else None,
        canal_id=canal_id_claro,
        nombre=nombre,
        telefono=telefono,
        usuario_canal=usuario_canal,
        campana=campana,
        red_origen=red_origen,
        estado=EstadoProspecto.NUEVO.value,
    )
    db.add(prospecto)
    db.flush()

    auditar(
        db,
        actor=actor,
        accion="prospecto_creado",
        entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle=f"canal={canal_valor} campana={campana or '-'} red={red_origen or '-'}",
    )
    return prospecto


def registrar_mensaje(
    db: Session, prospecto: Prospecto, direccion: Direccion | str, texto: str
) -> Mensaje:
    """Persiste un turno de conversación (texto cifrado en reposo).

    Un mensaje SALIENTE exige consentimiento vigente: es la última barrera del
    bloqueo de contacto no autorizado (RF-19).
    """
    valor = direccion.value if isinstance(direccion, Direccion) else str(direccion)
    if valor == Direccion.SALIENTE.value:
        exigir_consentimiento(prospecto)

    mensaje = Mensaje(prospecto_id=prospecto.id, direccion=valor, texto=texto)
    # Se anexa por la relación (y no con `db.add`) para que la conversación en
    # memoria quede consistente en el mismo turno: el historial que lee el
    # clasificador y el que muestra el dashboard son el mismo objeto.
    prospecto.mensajes.append(mensaje)
    db.flush()
    return mensaje


def historial(prospecto: Prospecto) -> list[dict]:
    return [
        {"direccion": m.direccion, "texto": m.texto, "ts": m.creado_en}
        for m in prospecto.mensajes
    ]


def perfil(prospecto: Prospecto) -> dict:
    return {
        "ciudad": prospecto.ciudad,
        "municipio": prospecto.municipio,
        "zona": prospecto.zona,
        "foco": prospecto.foco,
        "tipo": prospecto.tipo,
        "negocio": prospecto.negocio,
        "presupuesto_min": prospecto.presupuesto_min,
        "presupuesto_max": prospecto.presupuesto_max,
        "habitaciones": prospecto.habitaciones,
        "plazo_compra": prospecto.plazo_compra,
    }


def aplicar_analisis(db: Session, prospecto: Prospecto, analisis: Analisis) -> Prospecto:
    """Vuelca slots y score del clasificador sobre el prospecto (RF-05/06)."""
    for campo in (
        "ciudad", "municipio", "zona", "tipo", "negocio",
        "presupuesto_min", "presupuesto_max", "habitaciones", "plazo_compra",
    ):
        valor = analisis.slots.get(campo)
        if valor not in (None, "", 0):
            setattr(prospecto, campo, valor)
        elif campo in ("presupuesto_min", "presupuesto_max") and campo in analisis.slots:
            # El clasificador trae el presupuesto en bloque: si este turno dejó un
            # extremo abierto, hay que borrar el que quedó del turno anterior. Sin
            # esto, un "desde 200 millones" heredaba el techo viejo y recortaba
            # la cartera a un solo inmueble.
            setattr(prospecto, campo, None)

    prospecto.score_intencion = analisis.score
    prospecto.etiqueta = analisis.etiqueta.value

    estado = prospecto.estado_enum
    if analisis.motivo_fuera_alcance and estado == EstadoProspecto.NUEVO:
        cambiar_estado(
            db, prospecto, EstadoProspecto.FUERA_DE_ALCANCE,
            actor="sistema", motivo=analisis.motivo_fuera_alcance,
        )
    elif (
        not analisis.motivo_fuera_alcance
        and prospecto.datos_completos
        and estado in (EstadoProspecto.NUEVO, EstadoProspecto.FUERA_DE_ALCANCE)
    ):
        cambiar_estado(db, prospecto, EstadoProspecto.CALIFICADO, actor="sistema")

    db.flush()
    auditar(
        db,
        actor="sistema",
        accion="perfil_actualizado",
        entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle=f"score={analisis.score} etiqueta={analisis.etiqueta.value} fuente={analisis.fuente}",
    )
    return prospecto


def cambiar_estado(
    db: Session,
    prospecto: Prospecto,
    nuevo: EstadoProspecto | str,
    *,
    actor: str = "operador",
    motivo: str = "",
) -> Prospecto:
    """Aplica una transición válida de la máquina de estados (RF-13)."""
    destino = EstadoProspecto(nuevo) if not isinstance(nuevo, EstadoProspecto) else nuevo
    actual = prospecto.estado_enum
    if destino == actual:
        return prospecto
    if destino not in TRANSICIONES[actual]:
        raise TransicionInvalida(
            f"No se puede pasar de '{actual.value}' a '{destino.value}'."
        )

    prospecto.estado = destino.value
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="cambio_estado",
        entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle=f"{actual.value} -> {destino.value}" + (f" ({motivo})" if motivo else ""),
    )
    return prospecto


def marcar_emparejado(db: Session, prospecto: Prospecto) -> None:
    if prospecto.estado_enum == EstadoProspecto.CALIFICADO:
        cambiar_estado(db, prospecto, EstadoProspecto.EMPAREJADO, actor="sistema")


def solicitar_handoff(
    db: Session,
    prospecto: Prospecto,
    *,
    tipo: str = "visita",
    propiedad_id: str | None = None,
    detalle: str = "",
) -> Solicitud:
    """Crea la tarea para el operador humano y mueve el estado (RF-12).

    La solicitud es, además, el acto que abre la **ventana de protección** de la
    comisión: desde aquí y por `dias_proteccion` días, una venta de ese inmueble
    a ese comprador es atribuible aunque se cierre por fuera del sistema. La
    fecha se congela ahora y no se recalcula nunca (PRD §10).
    """
    solicitud = Solicitud(
        prospecto_id=prospecto.id,
        propiedad_id=propiedad_id,
        tipo=tipo,
        detalle=detalle,
        protegido_hasta=ahora() + timedelta(days=settings.dias_proteccion),
    )
    db.add(solicitud)

    if prospecto.estado_enum in (
        EstadoProspecto.CALIFICADO,
        EstadoProspecto.EMPAREJADO,
        EstadoProspecto.CONTACTADO,
    ):
        destino = (
            EstadoProspecto.VISITA if tipo == "visita" else EstadoProspecto.CONTACTADO
        )
        if destino in TRANSICIONES[prospecto.estado_enum]:
            cambiar_estado(db, prospecto, destino, actor="sistema", motivo=f"solicitud de {tipo}")

    db.flush()
    auditar(
        db,
        actor=f"titular:{prospecto.codigo}",
        accion="handoff_solicitado",
        entidad="solicitud",
        entidad_id=solicitud.id,
        detalle=(
            f"tipo={tipo} propiedad={propiedad_id or '-'} "
            f"protegido_hasta={solicitud.protegido_hasta:%Y-%m-%d}"
        ),
    )

    # El aviso va después del flush para que el asesor nunca reciba un enlace a
    # algo que todavía no existe. `notificar_solicitud` no propaga excepciones:
    # si el aviso falla, la solicitud queda igual en la cola del dashboard.
    canales = notificaciones.notificar_solicitud(solicitud, prospecto)
    if canales:
        auditar(
            db,
            actor="sistema",
            accion="asesor_notificado",
            entidad="solicitud",
            entidad_id=solicitud.id,
            detalle=f"canales={','.join(canales)}",
        )
    return solicitud


def tiene_solicitud_pendiente(db: Session, prospecto: Prospecto) -> bool:
    """¿Ya hay un handoff en la cola del operador para este titular?

    El LLM sigue marcando `pide_visita` en los turnos siguientes porque la
    petición está en el historial. Sin esta comprobación, cada mensaje posterior
    crea otra solicitud y otro aviso al asesor: el mismo comprador aparece
    cuatro veces en la cola y el asesor deja de mirar los avisos.
    """
    return db.scalar(
        select(Solicitud.id)
        .where(Solicitud.prospecto_id == prospecto.id, Solicitud.estado == "pendiente")
        .limit(1)
    ) is not None


def atender_solicitud(db: Session, solicitud: Solicitud, actor: str) -> Solicitud:
    solicitud.estado = "atendida"
    solicitud.atendida_en = ahora()
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="handoff_atendido",
        entidad="solicitud",
        entidad_id=solicitud.id,
    )
    return solicitud


# ─────────────── Control anti-elusión de comisión (HU-09) ───────────────


def alertas_seguimiento(db: Session) -> list[dict]:
    """Prospectos con negocio en curso y sin desenlace registrado.

    El riesgo del modelo de comisión es que el asesor cierre por fuera y no lo
    reporte. Esta consulta expone cada oportunidad que lleva demasiado tiempo en
    'visita' u 'oferta' sin registrar 'vendido' o 'perdido': el operador queda
    obligado a declarar el desenlace, y su omisión queda visible y auditada.
    """
    limite = datetime.now(timezone.utc) - timedelta(days=settings.dias_alerta_seguimiento)
    en_curso = [e.value for e in ESTADOS_EN_CURSO]

    pendientes = db.scalars(
        select(Prospecto)
        .where(Prospecto.estado.in_(en_curso))
        .order_by(Prospecto.actualizado_en)
    )

    alertas = []
    for p in pendientes:
        actualizado = p.actualizado_en
        if actualizado.tzinfo is None:
            actualizado = actualizado.replace(tzinfo=timezone.utc)
        if actualizado > limite:
            continue
        dias = (datetime.now(timezone.utc) - actualizado).days
        alertas.append(
            {
                "prospecto": p,
                "dias_sin_avance": dias,
                "severidad": "alta" if dias >= settings.dias_alerta_seguimiento * 2 else "media",
            }
        )
    return alertas


def resumen_metricas(db: Session) -> dict:
    """Métricas de éxito del PRD §9."""
    total = db.scalar(select(func.count()).select_from(Prospecto)) or 0
    consentidos = db.scalar(
        select(func.count()).select_from(Prospecto).where(Prospecto.consentimiento.is_(True))
    ) or 0
    calificados = db.scalar(
        select(func.count()).select_from(Prospecto).where(
            Prospecto.estado.notin_(
                [EstadoProspecto.NUEVO.value, EstadoProspecto.FUERA_DE_ALCANCE.value]
            )
        )
    ) or 0
    calientes = db.scalar(
        select(func.count()).select_from(Prospecto).where(
            Prospecto.etiqueta == Etiqueta.CALIENTE.value
        )
    ) or 0
    visitas = db.scalar(
        select(func.count()).select_from(Solicitud).where(Solicitud.tipo == "visita")
    ) or 0
    pendientes = db.scalar(
        select(func.count()).select_from(Solicitud).where(Solicitud.estado == "pendiente")
    ) or 0

    return {
        "prospectos": total,
        "consentidos": consentidos,
        "tasa_consentimiento": round(consentidos / total * 100, 1) if total else 0.0,
        "calificados": calificados,
        "tasa_calificacion": round(calificados / total * 100, 1) if total else 0.0,
        "calientes": calientes,
        "visitas_solicitadas": visitas,
        "solicitudes_pendientes": pendientes,
    }
