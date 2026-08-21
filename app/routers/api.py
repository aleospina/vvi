"""API REST del MVP."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Canal, Propiedad, Prospecto
from app.channels import gateway
from app.schemas import (
    LeadManualIn,
    MensajeIn,
    PropiedadIn,
    PropiedadOut,
    ProspectoOut,
    RespuestaBot,
    VentaIn,
    VentaOut,
)
from app.services import commission, leads, portfolio, prospecting
from app.services.compliance import tiene_consentimiento_vigente
from app.services.prospecting import ConsentimientoAusente

router = APIRouter(prefix="/api", tags=["api"])


def _prospecto(db: Session, codigo: str) -> Prospecto:
    p = db.scalar(select(Prospecto).where(Prospecto.codigo == codigo))
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Prospecto {codigo} no encontrado")
    return p


# ─────────────────────────── Cartera (RF-10) ───────────────────────────


@router.get("/propiedades", response_model=list[PropiedadOut])
def listar_propiedades(
    db: Session = Depends(get_db),
    ciudad: str | None = None,
    tipo: str | None = None,
    solo_disponibles: bool = True,
):
    return portfolio.listar(db, ciudad=ciudad, tipo=tipo, solo_disponibles=solo_disponibles)


@router.post("/propiedades", response_model=PropiedadOut, status_code=status.HTTP_201_CREATED)
def crear_propiedad(datos: PropiedadIn, db: Session = Depends(get_db)):
    cuerpo = datos.model_dump(exclude_none=True)
    cuerpo["tipo"] = datos.tipo.value
    cuerpo["negocio"] = datos.negocio.value
    return portfolio.crear(db, cuerpo)


@router.delete("/propiedades/{propiedad_id}", response_model=PropiedadOut)
def inactivar_propiedad(propiedad_id: str, db: Session = Depends(get_db)):
    propiedad = portfolio.obtener(db, propiedad_id)
    if propiedad is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Propiedad no encontrada")
    return portfolio.inactivar(db, propiedad)


# ─────────────────────────── Prospectos ───────────────────────────


@router.get("/prospectos", response_model=list[ProspectoOut])
def listar_prospectos(
    db: Session = Depends(get_db),
    estado: str | None = None,
    etiqueta: str | None = None,
    limite: int = Query(default=100, le=500),
):
    consulta = select(Prospecto).order_by(Prospecto.score_intencion.desc())
    if estado:
        consulta = consulta.where(Prospecto.estado == estado)
    if etiqueta:
        consulta = consulta.where(Prospecto.etiqueta == etiqueta)
    return list(db.scalars(consulta.limit(limite)))


@router.get("/prospectos/{codigo}", response_model=ProspectoOut)
def ver_prospecto(codigo: str, db: Session = Depends(get_db)):
    return _prospecto(db, codigo)


@router.post("/leads/manual", response_model=ProspectoOut, status_code=status.HTTP_201_CREATED)
def cargar_lead_manual(datos: LeadManualIn, db: Session = Depends(get_db)):
    """Carga un inbound recibido por Marketplace/OLX/DM (RF-02).

    Exige consentimiento con evidencia: el operador declara cómo lo autorizó el
    titular. Sin eso se responde 403 y no se guarda nada.
    """
    try:
        resultado = prospecting.ingerir_lead(
            db,
            red=datos.red,
            canal_id=datos.canal_id,
            consentimiento=datos.consentimiento,
            evidencia=datos.evidencia,
            nombre=datos.nombre,
            telefono=datos.telefono,
            usuario_canal=datos.usuario_canal,
            campana=datos.campana,
            mensaje=datos.mensaje,
            actor="operador",
        )
    except ConsentimientoAusente as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return resultado.prospecto


@router.post("/mensajes", response_model=RespuestaBot)
def enviar_mensaje(datos: MensajeIn, db: Session = Depends(get_db)):
    """Turno conversacional por API (útil para pruebas y para el canal manual)."""
    canal = datos.canal.value if isinstance(datos.canal, Canal) else str(datos.canal)
    prospecto = leads.buscar_por_canal(db, canal, datos.canal_id)
    if prospecto is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No existe un prospecto con ese identificador de canal. Cárgalo primero "
            "en /api/leads/manual con su consentimiento.",
        )
    if not tiene_consentimiento_vigente(prospecto):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "El titular no tiene consentimiento vigente; no se puede continuar la conversación.",
        )

    respuesta = gateway.procesar(db, prospecto, datos.texto)
    return RespuestaBot(
        prospecto=prospecto.codigo,
        estado=prospecto.estado,
        score_intencion=prospecto.score_intencion,
        etiqueta=prospecto.etiqueta,
        textos=respuesta.textos,
        propiedades=[m.propiedad.id for m in respuesta.matches],
        handoff=respuesta.handoff,
    )


# ─────────────────────────── Ventas y comisión ───────────────────────────


@router.post("/ventas", response_model=VentaOut, status_code=status.HTTP_201_CREATED)
def confirmar_venta(datos: VentaIn, db: Session = Depends(get_db)):
    """Confirma una venta y calcula la comisión del 3% (RF-14, ADR-05)."""
    prospecto = _prospecto(db, datos.codigo_prospecto)
    propiedad = db.get(Propiedad, datos.propiedad_id)
    if propiedad is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Propiedad no encontrada")
    try:
        return commission.confirmar_venta(
            db,
            prospecto=prospecto,
            propiedad=propiedad,
            precio_venta=datos.precio_venta,
            operador=datos.operador,
        )
    except commission.VentaInvalida as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/ventas", response_model=list[VentaOut])
def listar_ventas(db: Session = Depends(get_db)):
    from app.models import Venta

    return list(db.scalars(select(Venta).order_by(Venta.fecha.desc())))


@router.get("/metricas")
def metricas(db: Session = Depends(get_db)):
    """Métricas de éxito del PRD §9 + rendimiento por red social."""
    return {
        **leads.resumen_metricas(db),
        **commission.resumen(db),
        "canales": prospecting.rendimiento_canales(db),
    }
