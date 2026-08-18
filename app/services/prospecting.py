"""Captación de prospectos en redes sociales — modelo opt-in (ADR-01).

Qué hace este módulo
--------------------
Trae prospectos *desde* Instagram, Facebook/Marketplace, OLX y Mercado Libre
hacia el sistema, con autorización del titular, y mide qué red produce
compradores reales:

  1. Enlaces de campaña por red (link-in-bio, publicaciones, listados) que
     llevan a una landing con casilla de consentimiento.
  2. Webhook de Meta Lead Ads: el formulario de Instagram/Facebook trae el
     opt-in del usuario.
  3. Ingesta de preguntas de Mercado Libre: el comprador inició el contacto.
  4. Carga manual por el operador de un inbound recibido por Marketplace/OLX.
  5. Radar de canales: rendimiento agregado por red para saber dónde invertir.

Qué NO hace, por diseño
-----------------------
No rastrea (scrapea) perfiles, no compra bases de datos y no contacta en frío.
La Ley 1581/2012 exige autorización previa y los ToS de las plataformas prohíben
la automatización no autorizada. El punto de entrada de PII es uno solo y exige
evidencia de consentimiento: `ingerir_lead`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import (
    Campana,
    Canal,
    Direccion,
    EstadoProspecto,
    Etiqueta,
    Prospecto,
    Solicitud,
    Venta,
)
from app.channels import gateway
from app.channels.gateway import MensajeEntrante
from app.services import leads
from app.services.compliance import auditar

log = logging.getLogger(__name__)


class ConsentimientoAusente(PermissionError):
    """Se intentó ingerir un lead sin evidencia de autorización (RF-19, ADR-01)."""


#: Redes desde las que aceptamos entrada inbound, con su canal técnico.
REDES = {
    "instagram": Canal.META_LEAD_ADS,
    "facebook": Canal.META_LEAD_ADS,
    "marketplace": Canal.MARKETPLACE,
    "olx": Canal.OLX,
    "mercado_libre": Canal.MERCADO_LIBRE,
    "telegram": Canal.TELEGRAM,
    "web": Canal.LANDING,
}


@dataclass
class ResultadoIngesta:
    prospecto: Prospecto
    respuesta: gateway.Respuesta | None = None
    nuevo: bool = True


# ─────────────────────────── Campañas ───────────────────────────


def campana_por_slug(db: Session, slug: str) -> Campana | None:
    return db.scalar(select(Campana).where(Campana.slug == slug))


def registrar_visita(db: Session, slug: str) -> Campana | None:
    """Cuenta una visita al enlace de campaña. No guarda nada del visitante."""
    campana = campana_por_slug(db, slug)
    if campana:
        campana.visitas = (campana.visitas or 0) + 1
        db.flush()
    return campana


def crear_campana(db: Session, *, slug: str, nombre: str, red: str, actor: str = "operador") -> Campana:
    campana = Campana(slug=slug.strip().lower(), nombre=nombre, red=red)
    db.add(campana)
    db.flush()
    auditar(
        db, actor=actor, accion="campana_creada", entidad="campana",
        entidad_id=campana.slug, detalle=f"red={red}",
    )
    return campana


# ─────────────────────────── Ingesta de leads ───────────────────────────


def ingerir_lead(
    db: Session,
    *,
    red: str,
    canal_id: str,
    consentimiento: bool,
    evidencia: str,
    nombre: str | None = None,
    telefono: str | None = None,
    usuario_canal: str | None = None,
    campana: str | None = None,
    mensaje: str | None = None,
    actor: str = "sistema",
) -> ResultadoIngesta:
    """Punto único de entrada de PII desde redes sociales.

    `consentimiento` debe venir de un acto afirmativo del titular (casilla
    marcada en el formulario, pregunta que él mismo escribió, autorización
    verbal registrada por el operador). Sin él no se persiste absolutamente
    nada de la persona.
    """
    if not consentimiento:
        auditar(
            db, actor=actor, accion="lead_rechazado_sin_consentimiento",
            entidad="captacion", entidad_id=red,
            detalle=f"campana={campana or '-'} — no se almacenó ningún dato personal",
        )
        raise ConsentimientoAusente(
            "El lead no trae autorización del titular; no puede almacenarse ni contactarse."
        )
    if not evidencia.strip():
        raise ConsentimientoAusente("Todo consentimiento debe quedar con evidencia registrada.")

    canal = REDES.get(red, Canal.LANDING)
    existente = leads.buscar_por_canal(db, canal.value, canal_id)

    entrante = MensajeEntrante(
        canal=canal,
        canal_id=canal_id,
        texto=mensaje or "",
        nombre=nombre,
        telefono=telefono,
        usuario_canal=usuario_canal,
        campana=campana,
        red_origen=red,
    )
    prospecto = gateway.alta_con_consentimiento(db, entrante, evidencia=evidencia)
    if prospecto.campana is None and campana:
        prospecto.campana = campana
    if prospecto.red_origen is None:
        prospecto.red_origen = red

    auditar(
        db, actor=actor, accion="lead_captado", entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle=f"red={red} campana={campana or '-'} evidencia={evidencia[:120]}",
    )

    respuesta = None
    if mensaje:
        respuesta = gateway.procesar(db, prospecto, mensaje)
    else:
        leads.registrar_mensaje(
            db, prospecto, Direccion.SALIENTE,
            "Prospecto captado por formulario opt-in; pendiente de primer contacto.",
        )

    return ResultadoIngesta(prospecto=prospecto, respuesta=respuesta, nuevo=existente is None)


def parsear_lead_ads(payload: dict) -> list[dict]:
    """Extrae leads de un payload de webhook de Meta Lead Ads.

    Meta envía `leadgen_id`; los campos del formulario se recuperan luego con la
    Graph API (Fase 2). Si el payload ya trae `field_data` — como en las pruebas
    de la consola de Meta — lo aprovechamos directamente.
    """
    encontrados: list[dict] = []
    for entrada in payload.get("entry", []):
        for cambio in entrada.get("changes", []):
            valor = cambio.get("value", {}) or {}
            campos = {
                c.get("name"): (c.get("values") or [None])[0]
                for c in valor.get("field_data", [])
            }
            encontrados.append(
                {
                    "leadgen_id": valor.get("leadgen_id"),
                    "form_id": valor.get("form_id"),
                    "page_id": valor.get("page_id"),
                    "red": "instagram" if valor.get("ad_id") else "facebook",
                    "nombre": campos.get("full_name") or campos.get("nombre"),
                    "telefono": campos.get("phone_number") or campos.get("telefono"),
                    "email": campos.get("email"),
                    "ciudad": campos.get("city") or campos.get("ciudad"),
                    "mensaje": campos.get("mensaje") or campos.get("comentarios"),
                    "tiene_campos": bool(campos),
                }
            )
    return encontrados


# ─────────────────────────── Radar de canales ───────────────────────────


def rendimiento_canales(db: Session) -> list[dict]:
    """Rendimiento agregado por red de origen.

    Esta es la respuesta compliant a '¿dónde están mis prospectos?': en vez de
    raspar personas, medimos qué red inbound genera compradores calificados y
    ventas, para concentrar la pauta y los listados ahí.
    """
    es_caliente = case((Prospecto.etiqueta == Etiqueta.CALIENTE.value, 1), else_=0)
    es_calificado = case(
        (
            Prospecto.estado.notin_(
                [EstadoProspecto.NUEVO.value, EstadoProspecto.FUERA_DE_ALCANCE.value]
            ),
            1,
        ),
        else_=0,
    )

    filas = db.execute(
        select(
            func.coalesce(Prospecto.red_origen, Prospecto.canal).label("red"),
            func.count(Prospecto.id),
            func.sum(es_caliente),
            func.sum(es_calificado),
            func.avg(Prospecto.score_intencion),
        ).group_by("red")
    ).all()

    visitas = dict(
        db.execute(
            select(
                func.coalesce(Prospecto.red_origen, Prospecto.canal),
                func.count(Solicitud.id),
            )
            .join(Solicitud, Solicitud.prospecto_id == Prospecto.id)
            .group_by(func.coalesce(Prospecto.red_origen, Prospecto.canal))
        ).all()
    )
    ventas = {
        red: (n, int(com or 0))
        for red, n, com in db.execute(
            select(
                func.coalesce(Prospecto.red_origen, Prospecto.canal),
                func.count(Venta.id),
                func.sum(Venta.comision_valor),
            )
            .join(Venta, Venta.prospecto_id == Prospecto.id)
            .group_by(func.coalesce(Prospecto.red_origen, Prospecto.canal))
        ).all()
    }

    resultado = []
    for red, total, calientes, calificados, score_medio in filas:
        n_ventas, comision = ventas.get(red, (0, 0))
        resultado.append(
            {
                "red": red or "desconocido",
                "prospectos": total,
                "calificados": int(calificados or 0),
                "calientes": int(calientes or 0),
                "score_promedio": round(float(score_medio or 0), 1),
                "visitas": visitas.get(red, 0),
                "ventas": n_ventas,
                "comision": comision,
                "conversion": round((calificados or 0) / total * 100, 1) if total else 0.0,
            }
        )
    resultado.sort(key=lambda r: (-r["comision"], -r["calientes"], -r["prospectos"]))
    return resultado


def campanas_con_metricas(db: Session) -> list[dict]:
    conteos = dict(
        db.execute(
            select(Prospecto.campana, func.count()).group_by(Prospecto.campana)
        ).all()
    )
    salida = []
    for c in db.scalars(select(Campana).order_by(Campana.red, Campana.nombre)):
        captados = conteos.get(c.slug, 0)
        salida.append(
            {
                "campana": c,
                "captados": captados,
                "conversion_visita": round(captados / c.visitas * 100, 1) if c.visitas else 0.0,
            }
        )
    return salida
