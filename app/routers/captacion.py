"""Captación opt-in desde redes sociales: landing pública y webhooks (ADR-01).

Ninguno de estos puntos de entrada obtiene datos sin un acto afirmativo del
titular:
  · /c/{slug}   → landing con casilla de consentimiento (link-in-bio de IG/FB,
                  descripción de listados de OLX/Marketplace).
  · /webhooks/meta/leadads → formulario de Meta Lead Ads (el opt-in va en el
                  formulario que el usuario diligenció).
  · /webhooks/mercadolibre/preguntas → el comprador preguntó primero.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import RAIZ, settings
from app.db import get_db
from app.services import fotos, ingesta, prospecting
from app.services.compliance import texto_consentimiento
from app.services.prospecting import ConsentimientoAusente

log = logging.getLogger(__name__)
router = APIRouter(tags=["captación"])
plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"))


# ─────────────────────────── Landing opt-in ───────────────────────────


@router.get("/c/{slug}", response_class=HTMLResponse)
def landing(slug: str, request: Request, db: Session = Depends(get_db)):
    """Página pública a la que apuntan los enlaces de campaña de cada red."""
    campana = prospecting.registrar_visita(db, slug)
    return plantillas.TemplateResponse(
        request,
        "landing.html",
        {
            "slug": slug,
            "campana": campana,
            "empresa": settings.empresa_nombre,
            "politica": settings.politica_privacidad_url,
            "texto_consentimiento": texto_consentimiento(),
            "ciudades": settings.ciudades_cobertura,
            "enviado": False,
        },
    )


@router.post("/c/{slug}", response_class=HTMLResponse)
def landing_envio(
    slug: str,
    request: Request,
    nombre: str = Form(...),
    telefono: str = Form(...),
    mensaje: str = Form(""),
    autorizo: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Recibe el formulario. Sin la casilla marcada no se guarda nada."""
    campana = prospecting.campana_por_slug(db, slug)
    red = campana.red if campana else "web"
    contexto = {
        "slug": slug,
        "campana": campana,
        "empresa": settings.empresa_nombre,
        "politica": settings.politica_privacidad_url,
        "texto_consentimiento": texto_consentimiento(),
        "ciudades": settings.ciudades_cobertura,
        "enviado": False,
    }

    if autorizo.lower() not in ("on", "true", "1", "si", "sí"):
        contexto["error"] = (
            "Necesitamos tu autorización expresa para poder contactarte. "
            "Sin ella no guardamos ningún dato."
        )
        return plantillas.TemplateResponse(request, "landing.html", contexto, status_code=400)

    try:
        resultado = prospecting.ingerir_lead(
            db,
            red=red,
            canal_id=f"{slug}:{telefono}",
            consentimiento=True,
            evidencia=f"Casilla de autorización marcada en landing /c/{slug}",
            nombre=nombre,
            telefono=telefono,
            campana=slug,
            mensaje=mensaje or None,
            actor="landing",
        )
    except ConsentimientoAusente as exc:
        contexto["error"] = str(exc)
        return plantillas.TemplateResponse(request, "landing.html", contexto, status_code=400)

    contexto.update(enviado=True, codigo=resultado.prospecto.codigo)
    return plantillas.TemplateResponse(request, "landing.html", contexto)


# ─────────────────────────── Captación de inmuebles ───────────────────────────


def _contexto_publicar(**extra) -> dict:
    return {
        "empresa": settings.empresa_nombre,
        "politica": settings.politica_privacidad_url,
        "texto_consentimiento": texto_consentimiento(),
        "ciudades": settings.ciudades_cobertura,
        "comision_pct": settings.comision_pct,
        "enviado": False,
        **extra,
    }


@router.get("/publicar", response_class=HTMLResponse)
def publicar(request: Request):
    """Formulario público para que un propietario ofrezca su inmueble.

    Es la contraparte de `/c/{slug}`: aquella capta demanda, esta capta oferta.
    Ambas comparten el mismo principio — nada entra sin un acto afirmativo del
    titular, y aquí ese acto incluye además el mandato de comercialización.
    """
    return plantillas.TemplateResponse(request, "publicar.html", _contexto_publicar())


@router.post("/publicar", response_class=HTMLResponse)
def publicar_envio(
    request: Request,
    propietario: str = Form(...),
    telefono: str = Form(...),
    ciudad: str = Form(...),
    tipo: str = Form(...),
    negocio: str = Form("venta"),
    precio: int = Form(...),
    zona: str = Form(""),
    habitaciones: int = Form(0),
    banos: int = Form(0),
    area_m2: float = Form(0),
    descripcion: str = Form(""),
    autorizo: str = Form(default=""),
    imagenes: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Recibe el inmueble. Sin la casilla de mandato no se guarda nada."""
    contexto = _contexto_publicar()

    if autorizo.lower() not in ("on", "true", "1", "si", "sí"):
        contexto["error"] = (
            "Necesitamos tu autorización expresa para tratar tus datos y para "
            "comercializar el inmueble. Sin ella no guardamos nada."
        )
        return plantillas.TemplateResponse(request, "publicar.html", contexto, status_code=400)

    try:
        publicacion = ingesta.publicacion_de_formulario(
            telefono=telefono,
            ciudad=ciudad,
            tipo=tipo,
            negocio=negocio,
            precio=precio,
            zona=zona,
            habitaciones=habitaciones,
            banos=banos,
            area_m2=area_m2,
            descripcion=descripcion,
            propietario=propietario,
            autoriza_mandato=True,
        )
        propiedad = ingesta.ingerir_una(db, publicacion, actor="landing_publicar")
    except (ingesta.MandatoAusente, ValueError) as exc:
        contexto["error"] = str(exc)
        return plantillas.TemplateResponse(request, "publicar.html", contexto, status_code=400)

    # Las fotos van después de crear el inmueble: si una viene corrupta se
    # descarta sola, y sería absurdo perder la publicación entera por eso.
    fotos.guardar(db, propiedad, imagenes, actor="landing_publicar")

    contexto.update(enviado=True, codigo=propiedad.id)
    return plantillas.TemplateResponse(request, "publicar.html", contexto)


# ─────────────────────────── Meta Lead Ads ───────────────────────────


@router.get("/webhooks/meta/leadads", response_class=PlainTextResponse)
def verificar_meta(request: Request):
    """Handshake de verificación que Meta exige al registrar el webhook."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.meta_verify_token
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Token de verificación inválido")


@router.post("/webhooks/meta/leadads")
async def recibir_meta(request: Request, db: Session = Depends(get_db)):
    """Ingesta de leads de Instagram/Facebook (formulario con opt-in).

    El consentimiento proviene de la casilla del propio formulario de Lead Ads,
    que es responsabilidad del anunciante configurar. Registramos esa
    procedencia como evidencia.
    """
    payload = await request.json()
    procesados, pendientes = [], []

    for lead in prospecting.parsear_lead_ads(payload):
        if not lead["tiene_campos"]:
            # Meta solo mandó el leadgen_id: los campos se recuperan con la
            # Graph API (Fase 2). No inventamos datos.
            pendientes.append(lead["leadgen_id"])
            continue
        try:
            resultado = prospecting.ingerir_lead(
                db,
                red=lead["red"],
                canal_id=str(lead["leadgen_id"] or lead["telefono"]),
                consentimiento=True,
                evidencia=(
                    f"Meta Lead Ads · form_id={lead['form_id']} "
                    f"leadgen_id={lead['leadgen_id']} (opt-in en el formulario)"
                ),
                nombre=lead["nombre"],
                telefono=lead["telefono"],
                campana=lead["form_id"],
                mensaje=lead["mensaje"],
                actor="meta_lead_ads",
            )
            procesados.append(resultado.prospecto.codigo)
        except ConsentimientoAusente as exc:
            log.warning("Lead de Meta descartado: %s", exc)

    return JSONResponse(
        {"recibidos": len(procesados) + len(pendientes),
         "procesados": procesados,
         "pendientes_de_graph_api": pendientes}
    )


# ─────────────────────────── Mercado Libre ───────────────────────────


def _firma_valida(cuerpo: bytes, firma: str | None) -> bool:
    if not settings.mercadolibre_shared_secret:
        return True  # sin secreto configurado no validamos (solo desarrollo)
    if not firma:
        return False
    esperado = hmac.new(
        settings.mercadolibre_shared_secret.encode(), cuerpo, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(esperado, firma)


@router.post("/webhooks/mercadolibre/preguntas")
async def recibir_mercadolibre(request: Request, db: Session = Depends(get_db)):
    """Pregunta de un comprador en una publicación (contacto iniciado por él)."""
    cuerpo = await request.body()
    if not _firma_valida(cuerpo, request.headers.get("x-signature")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    payload = await request.json()
    pregunta = payload.get("text") or payload.get("pregunta") or ""
    autor = str(payload.get("from", {}).get("id") or payload.get("user_id") or "")
    if not autor:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta el identificador del comprador")

    resultado = prospecting.ingerir_lead(
        db,
        red="mercado_libre",
        canal_id=autor,
        consentimiento=True,
        evidencia=(
            f"Pregunta pública en Mercado Libre (item={payload.get('item_id', '-')}): "
            "el comprador inició el contacto"
        ),
        usuario_canal=payload.get("from", {}).get("nickname"),
        campana="ml-publicaciones",
        mensaje=pregunta,
        actor="mercado_libre",
    )
    respuesta = resultado.respuesta
    return {
        "prospecto": resultado.prospecto.codigo,
        "respuesta_sugerida": respuesta.textos[0] if respuesta and respuesta.textos else "",
    }
