"""Confirmación de venta y comisión (RF-14, RF-15 · ADR-05).

La IA nunca cierra una venta ni genera comisión. Un operador humano confirma el
precio; solo entonces el sistema calcula el 3% y lo atribuye al prospecto, canal
y propiedad de origen.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Emparejamiento, EstadoProspecto, Propiedad, Prospecto, Venta
from app.services.compliance import auditar
from app.services.leads import TransicionInvalida, cambiar_estado


class VentaInvalida(ValueError):
    """Datos insuficientes o inconsistentes para registrar la venta."""


def calcular_comision(precio_venta: int, pct: float | None = None) -> int:
    """Comisión = precio × pct (3% por defecto). Se redondea a peso entero."""
    if precio_venta <= 0:
        raise VentaInvalida("El precio de venta debe ser mayor que cero.")
    return int(round(precio_venta * (settings.comision_pct if pct is None else pct)))


def siguiente_codigo(db: Session) -> str:
    total = db.scalar(select(func.count()).select_from(Venta)) or 0
    return f"SALE-{total + 1:04d}"


def confirmar_venta(
    db: Session,
    *,
    prospecto: Prospecto,
    propiedad: Propiedad,
    precio_venta: int,
    operador: str,
) -> Venta:
    """Registra la venta confirmada por un humano y su comisión (RF-14)."""
    if prospecto.estado_enum == EstadoProspecto.VENDIDO:
        raise VentaInvalida(f"El prospecto {prospecto.codigo} ya tiene una venta registrada.")
    if not operador or not operador.strip():
        raise VentaInvalida("Toda venta debe quedar atribuida a un operador identificado.")
    if propiedad.es_referencia:
        # Es la garantía que sostiene el modo referencia: un aviso ajeno cargado
        # para probar puede verse en el bot, pero no puede convertirse en una
        # comisión. Sin este bloqueo, dato de prueba y negocio real se mezclan.
        raise VentaInvalida(
            f"{propiedad.id} es un inmueble de referencia (fuente '{propiedad.fuente}'): "
            "se cargó para pruebas y no hay mandato de comercialización, así que no "
            "puede registrarse una venta ni generar comisión."
        )

    comision = calcular_comision(precio_venta)
    venta = Venta(
        codigo=siguiente_codigo(db),
        prospecto_id=prospecto.id,
        propiedad_id=propiedad.id,
        canal_origen=prospecto.canal,
        campana_origen=prospecto.campana,
        precio_venta=int(precio_venta),
        comision_pct=settings.comision_pct,
        comision_valor=comision,
        operador=operador.strip(),
    )
    db.add(venta)

    propiedad.estado = "vendida"
    try:
        cambiar_estado(
            db, prospecto, EstadoProspecto.VENDIDO, actor=operador,
            motivo=f"venta {venta.codigo}",
        )
    except TransicionInvalida as exc:
        # Una venta que la máquina de estados rechaza es una venta inválida, no
        # un fallo del servidor. Antes salía por arriba sin que nadie la
        # atrapara y el operador veía un «Internal server error» al confirmar,
        # sin más pista de qué había pasado. Traducirla aquí arregla las dos
        # puertas —el panel y la API— en vez de una.
        raise VentaInvalida(str(exc)) from exc
    db.flush()

    auditar(
        db,
        actor=operador,
        accion="venta_confirmada",
        entidad="venta",
        entidad_id=venta.codigo,
        detalle=(
            f"prospecto={prospecto.codigo} propiedad={propiedad.id} "
            f"canal={prospecto.canal} campana={prospecto.campana or '-'} "
            f"precio={precio_venta} comision={comision} "
            f"({settings.comision_pct * 100:.1f}%)"
        ),
    )
    return venta


def propiedades_atribuibles(db: Session, prospecto: Prospecto) -> list[Propiedad]:
    """Inmuebles que el sistema efectivamente le mostró a este prospecto.

    Restringir el cierre a estos justifica la comisión ante el propietario: hay
    traza de que la venta salió del emparejamiento (RF-15).
    """
    return list(
        db.scalars(
            select(Propiedad)
            .join(Emparejamiento, Emparejamiento.propiedad_id == Propiedad.id)
            .where(Emparejamiento.prospecto_id == prospecto.id)
            .order_by(Emparejamiento.puntaje.desc())
        )
    )


def resumen(db: Session) -> dict:
    total_ventas = db.scalar(select(func.count()).select_from(Venta)) or 0
    monto = db.scalar(select(func.coalesce(func.sum(Venta.precio_venta), 0))) or 0
    comisiones = db.scalar(select(func.coalesce(func.sum(Venta.comision_valor), 0))) or 0
    por_canal = db.execute(
        select(Venta.canal_origen, func.count(), func.sum(Venta.comision_valor))
        .group_by(Venta.canal_origen)
        .order_by(func.sum(Venta.comision_valor).desc())
    ).all()
    return {
        "ventas": total_ventas,
        "monto_vendido": int(monto),
        "comision_generada": int(comisiones),
        "por_canal": [
            {"canal": c, "ventas": n, "comision": int(v or 0)} for c, n, v in por_canal
        ],
    }
