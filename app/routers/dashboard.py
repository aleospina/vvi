"""CRM ligero y dashboard del operador (SRS §2.5 · HU-08, HU-09, HU-10)."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

import httpx

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import RAIZ, settings
from app.db import get_db
from app.models import (
    NEGOCIO_POR_DEFECTO,
    EstadoProspecto,
    FotoPropiedad,
    FuentePropiedad,
    ComentarioPropiedad,
    LogAuditoria,
    Propiedad,
    Prospecto,
    Solicitud,
    TipoInmueble,
    TipoNegocio,
    Venta,
)
from app.channels import whatsapp_evo
from app.channels.gateway import pesos
from app.security.crypto import enmascarar, indice_ciego
from app.security.sesion import (
    COOKIE,
    DURACION_SEGUNDOS,
    INVITADO,
    OPERADOR,
    RUTA_COOKIE,
    RUTA_COOKIE_ANTERIOR,
    credenciales_validas,
    crear_token,
    rol_de,
    validar_token,
)
from app.services import (
    ajustes, commission, fotos, geografia, ingesta, leads, portfolio, prospecting,
    seguimiento,
)
from app.services.compliance import auditar, verificar_cadena
from app.tiempo import fecha

log = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
plantillas = Jinja2Templates(directory=str(RAIZ / "app" / "templates"))
plantillas.env.filters["pesos"] = pesos
# Todo lo que se guarda está en UTC; el operador tiene que leer su hora.
plantillas.env.filters["fecha"] = fecha
plantillas.env.filters["enmascarar"] = enmascarar


def _sesion(request: Request) -> tuple[str, str]:
    """(usuario, rol) de la sesión vigente. Sin ella, al formulario de ingreso.

    Se responde con una redirección y no con 401 a propósito: un 401 haría que
    el navegador abriera su propio diálogo de credenciales, que es justo el
    comportamiento sin cierre de sesión del que venimos.
    """
    usuario = validar_token(request.cookies.get(COOKIE))
    rol = rol_de(usuario) if usuario else None
    if usuario is None or rol is None:
        raise HTTPException(
            status.HTTP_303_SEE_OTHER,
            "Sesión no iniciada",
            headers={"Location": "/dashboard/login"},
        )
    return usuario, rol


def cualquiera(request: Request) -> str:
    """Exige sesión de operador. El invitado sale hacia la vitrina.

    El invitado es una cuenta de consulta, y desde que existe `/inmuebles` su
    sitio es ese: allí ve el inventario tal como lo ve un comprador. El panel
    dejó de listar inmuebles y solo contiene herramientas de operador —ingesta,
    alta, aprobación, purga—, así que no le queda nada que hacer aquí.

    Se responde con una redirección y no con 403 porque no es un intento de
    hacer algo prohibido: es alguien que llegó a la puerta equivocada.
    """
    usuario, rol = _sesion(request)
    if rol == INVITADO:
        raise HTTPException(
            status.HTTP_303_SEE_OTHER,
            "El invitado consulta el inventario en la vitrina.",
            headers={"Location": "/inmuebles"},
        )
    return usuario


def operador(request: Request) -> str:
    """Exige rol de operador. Es la barrera de todo lo que modifica datos.

    Vive en una dependencia y no en comprobaciones dispersas para que añadir una
    ruta de escritura sin protegerla sea un olvido visible: si no pide `Operador`,
    no compila la intención.
    """
    usuario, rol = _sesion(request)
    if rol != OPERADOR:
        log.warning("El invitado %r intentó una acción de escritura", usuario[:40])
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Tu cuenta es de solo lectura: puedes consultar la cartera y comentar, "
            "pero no modificar inmuebles ni fotos.",
        )
    return usuario


def con_sesion(request: Request) -> str:
    """Cualquier cuenta identificada, invitado incluido.

    Existe solo para comentar. Es la única escritura que el invitado conserva
    tras mudarse a la vitrina, y por eso no puede pasar por `cualquiera`, que
    ahora lo devuelve a `/inmuebles`.
    """
    return _sesion(request)[0]


Operador = Annotated[str, Depends(operador)]
Cualquiera = Annotated[str, Depends(cualquiera)]
ConSesion = Annotated[str, Depends(con_sesion)]


def _prospecto(db: Session, codigo: str) -> Prospecto:
    p = db.scalar(select(Prospecto).where(Prospecto.codigo == codigo))
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prospecto no encontrado")
    return p


def _base(request: Request, quien: str, **extra) -> dict:
    rol = rol_de(quien)
    return {
        "request": request,
        "operador": quien,
        "rol": rol,
        # Las plantillas deciden qué mostrar con esto. La barrera real está en
        # las dependencias del router: ocultar un botón no protege una ruta.
        "puede_editar": rol == OPERADOR,
        "empresa": settings.empresa_nombre,
        "comision_pct": settings.comision_pct,
        **extra,
    }


# ─────────────────────────── Sesión ───────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    """Formulario de ingreso. Si ya hay sesión, no tiene sentido mostrarlo."""
    if validar_token(request.cookies.get(COOKIE)):
        return RedirectResponse("/dashboard", status_code=303)
    return plantillas.TemplateResponse(
        request, "login.html", {"empresa": settings.empresa_nombre}
    )


@router.post("/login", response_class=HTMLResponse)
def login_envio(
    request: Request,
    usuario: str = Form(...),
    clave: str = Form(...),
):
    rol = credenciales_validas(usuario, clave)
    if rol is None:
        log.warning("Intento de ingreso fallido al dashboard con usuario %r", usuario[:40])
        return plantillas.TemplateResponse(
            request,
            "login.html",
            {
                "empresa": settings.empresa_nombre,
                # Mensaje único: decir cuál de los dos campos falló ayuda a
                # enumerar usuarios válidos.
                "error": "Usuario o contraseña incorrectos.",
            },
            status_code=401,
        )

    # El invitado no entra al panel: su sitio es la vitrina, que es donde se ve
    # el inventario. El operador va al tablero de prospectos.
    destino = "/dashboard" if rol == OPERADOR else "/inmuebles"
    respuesta = RedirectResponse(destino, status_code=303)
    # Barre la cookie de la ruta antigua. Una cookie no se sobrescribe entre
    # rutas distintas: sin esto quedarían dos `vvi_sesion` vivas a la vez —la
    # de `/dashboard` y la nueva—, el navegador enviaría ambas al panel y el
    # servidor tomaría una cualquiera, que puede ser la caducada.
    respuesta.delete_cookie(COOKIE, path=RUTA_COOKIE_ANTERIOR)
    respuesta.set_cookie(
        COOKIE,
        crear_token(usuario),
        max_age=DURACION_SEGUNDOS,
        httponly=True,          # inaccesible a JavaScript
        samesite="lax",         # no viaja en peticiones de otros sitios
        secure=request.url.scheme == "https",
        path=RUTA_COOKIE,
    )
    return respuesta


@router.post("/logout")
def logout():
    """Cierra la sesión borrando la cookie."""
    respuesta = RedirectResponse("/dashboard/login", status_code=303)
    respuesta.delete_cookie(COOKIE, path=RUTA_COOKIE)
    # Y también la de la ruta antigua, o una sesión abierta antes del cambio
    # sobreviviría al botón de cerrar sesión.
    respuesta.delete_cookie(COOKIE, path=RUTA_COOKIE_ANTERIOR)
    return respuesta


# ─────────────────────────── Vistas ───────────────────────────


@router.get("", response_class=HTMLResponse)
def inicio(request: Request, quien: Operador, db: Session = Depends(get_db)):
    """Tablero principal: prospectos ordenados por intención (HU-08)."""
    prospectos = list(
        db.scalars(
            select(Prospecto)
            .order_by(Prospecto.score_intencion.desc(), desc(Prospecto.actualizado_en))
            .limit(200)
        )
    )
    return plantillas.TemplateResponse(
        request,
        "dashboard.html",
        _base(
            request,
            quien,
            prospectos=prospectos,
            metricas={**leads.resumen_metricas(db), **commission.resumen(db)},
            alertas=leads.alertas_seguimiento(db),
            solicitudes=list(
                db.scalars(
                    select(Solicitud)
                    .where(Solicitud.estado == "pendiente")
                    .order_by(desc(Solicitud.creado_en))
                    .limit(20)
                )
            ),
            # Compradores que dicen haber cerrado sin venta registrada: es la
            # lista que hay que mirar todos los días (PRD §10).
            cierres=seguimiento.cierres_declarados(db),
        ),
    )


@router.get("/prospecto/{codigo}", response_class=HTMLResponse)
def detalle(codigo: str, request: Request, quien: Operador, db: Session = Depends(get_db)):
    p = _prospecto(db, codigo)
    return plantillas.TemplateResponse(
        request,
        "prospecto.html",
        _base(
            request,
            quien,
            p=p,
            historial=p.mensajes,
            consentimientos=p.consentimientos,
            solicitudes=p.solicitudes,
            atribuibles=commission.propiedades_atribuibles(db, p),
            estados=[e.value for e in EstadoProspecto],
            venta=db.scalar(select(Venta).where(Venta.prospecto_id == p.id)),
        ),
    )


@router.post("/prospecto/{codigo}/estado")
def cambiar_estado(
    codigo: str,
    quien: Operador,
    nuevo_estado: str = Form(...),
    motivo: str = Form(""),
    db: Session = Depends(get_db),
):
    p = _prospecto(db, codigo)
    try:
        leads.cambiar_estado(db, p, nuevo_estado, actor=quien, motivo=motivo)
    except (leads.TransicionInvalida, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RedirectResponse(f"/dashboard/prospecto/{codigo}", status_code=303)


@router.post("/prospecto/{codigo}/venta")
def registrar_venta(
    codigo: str,
    quien: Operador,
    propiedad_id: str = Form(...),
    precio_venta: int = Form(...),
    db: Session = Depends(get_db),
):
    """Confirmación humana de la venta → comisión del 3% (RF-14, ADR-05)."""
    p = _prospecto(db, codigo)
    propiedad = db.get(Propiedad, propiedad_id)
    if propiedad is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Propiedad no encontrada")
    try:
        commission.confirmar_venta(
            db, prospecto=p, propiedad=propiedad, precio_venta=precio_venta, operador=quien
        )
    except commission.VentaInvalida as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RedirectResponse(f"/dashboard/prospecto/{codigo}", status_code=303)


@router.post("/seguimiento/ejecutar")
def ejecutar_seguimiento(quien: Operador, db: Session = Depends(get_db)):
    """Dispara una ronda de preguntas sin esperar al reloj.

    La tarea de fondo corre cada hora; esto existe para poder probar el circuito
    completo —y para el día que haya que empujarlo a mano— sin reiniciar el
    proceso.
    """
    enviados = seguimiento.ejecutar(db)
    log.info("Ronda de seguimiento manual de %s: %d envío(s).", quien, enviados)
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/solicitud/{solicitud_id}/atender")
def atender(solicitud_id: int, quien: Operador, db: Session = Depends(get_db)):
    solicitud = db.get(Solicitud, solicitud_id)
    if solicitud is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Solicitud no encontrada")
    leads.atender_solicitud(db, solicitud, actor=quien)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/propiedades", response_class=HTMLResponse)
def cartera(request: Request, quien: Cualquiera, db: Session = Depends(get_db)):
    """Panel de entrada del inventario. **No lista inmuebles**: eso es la vitrina.

    La separación es deliberada. Aquí se hace lo que solo puede hacer un
    operador —importar un aviso con IA, dar de alta a mano, aprobar lo que llegó
    por ingesta, purgar lo no vendible— y se ve el inventario como cifras. Para
    mirar inmuebles uno por uno está `/inmuebles`, que además es exactamente lo
    que ve un comprador: si algo se publicó por error, se nota allí. Por eso los
    filtros por tipo y municipio se fueron con la rejilla: viven en la vitrina.

    Lo retirado (inactivo, vendido, rechazado) sí se lista aquí, en tabla: no
    sale en la vitrina y sin este listado no habría forma de volver a abrirlo.
    """
    publicables = portfolio.publicables(db, incluir_demo=settings.catalogo_muestra_demo)
    return plantillas.TemplateResponse(
        request,
        "propiedades.html",
        _base(
            request,
            quien,
            total=db.scalar(select(func.count()).select_from(Propiedad)) or 0,
            publicadas=len(publicables),
            conteo_tipos=portfolio.conteo_por_tipo(db),
            fuera=portfolio.fuera_de_vitrina(db),
            ciudades=geografia.MUNICIPIOS,
            pendientes=ingesta.pendientes(db),
            referencias=ingesta.referencias(db),
            almacenamiento=fotos.diagnostico(db),
            base_datos=settings.database_url,
            catalogo_publico=settings.catalogo_publico,
        ),
    )


def _propiedad(db: Session, propiedad_id: str) -> Propiedad:
    propiedad = portfolio.obtener(db, propiedad_id)
    if propiedad is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Propiedad no encontrada")
    return propiedad


@router.post("/propiedades/importar")
def importar_aviso(
    request: Request,
    quien: Operador,
    aviso: str = Form(...),
    origen: str = Form(""),
    modo: str = Form("mandato"),
    confirmo_mandato: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Pega el texto de un aviso y el LLM lo estructura (RF-10).

    Dos modos. Con mandato, el operador declara que el propietario autorizó y el
    inmueble es comercializable. Como referencia, es un aviso real de un tercero
    cargado para probar: entra marcado, no genera comisión y se purga de un golpe.

    En ambos casos pasa por la cola de validación: nadie publica a ciegas lo que
    dedujo el modelo.
    """
    referencia = modo == "referencia"
    if not referencia and confirmo_mandato.lower() not in ("on", "true", "1", "si", "sí"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Debes confirmar que el propietario autorizó comercializar el inmueble, "
            "o cargarlo como referencia si es un aviso de un tercero.",
        )
    try:
        publicacion, crudo = ingesta.publicacion_desde_texto(
            aviso,
            fuente=(
                FuentePropiedad.REFERENCIA.value if referencia
                else FuentePropiedad.CAPTACION_PROPIETARIO.value
            ),
            url_origen=origen,
            mandato_evidencia=(
                f"CARGA DE REFERENCIA por {quien}: aviso real de un tercero, sin "
                f"mandato de comercialización, cargado para pruebas. "
                f"Origen: {origen or 'no indicado'}"
                if referencia
                else f"Aviso cargado por {quien}; declara mandato del propietario. "
                     f"Origen: {origen or 'no indicado'}"
            ),
        )
        propiedad = ingesta.ingerir_una(db, publicacion, actor=quien)
    except (ingesta.ExtraccionFallida, ingesta.MandatoAusente, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    log.info(
        "Aviso importado por %s → %s (confianza=%s, faltantes=%s)",
        quien, propiedad.id, crudo.get("confianza"), crudo.get("faltantes"),
    )
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.get("/propiedades/{propiedad_id}", response_class=HTMLResponse)
def ficha_propiedad(
    propiedad_id: str, request: Request, quien: Cualquiera, db: Session = Depends(get_db)
):
    """Ficha del inmueble. Es donde el operador corrige lo que el LLM dedujo mal."""
    return plantillas.TemplateResponse(
        request,
        "propiedad.html",
        _base(
            request,
            quien,
            p=_propiedad(db, propiedad_id),
            tipos=[t.value for t in TipoInmueble],
            negocios=[n.value for n in TipoNegocio],
            ciudades=geografia.MUNICIPIOS,
        ),
    )


@router.post("/propiedades/{propiedad_id}/comentarios")
def comentar_propiedad(
    propiedad_id: str,
    request: Request,
    quien: ConSesion,
    texto: str = Form(...),
    volver: str = Form(""),
    db: Session = Depends(get_db),
):
    """Añade un comentario al hilo. Es lo único que el invitado puede escribir."""
    propiedad = _propiedad(db, propiedad_id)
    contenido = texto.strip()
    if not contenido:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El comentario está vacío.")

    propiedad.comentarios.append(
        ComentarioPropiedad(autor=quien, rol=rol_de(quien) or INVITADO, texto=contenido[:2000])
    )
    db.flush()
    auditar(
        db, actor=quien, accion="comentario_agregado", entidad="propiedad",
        entidad_id=propiedad_id, detalle=contenido[:120],
    )
    # El hilo vive en dos sitios —la ficha interna y la pública— y hay que
    # devolver a la persona a la que estaba mirando. `volver` se acota a rutas
    # propias: aceptar cualquier destino sería una redirección abierta.
    if volver.startswith("/inmuebles/"):
        return RedirectResponse(f"{volver}#comentarios", status_code=303)
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}#comentarios", status_code=303)


@router.post("/propiedades/{propiedad_id}/comentarios/{comentario_id}/eliminar")
def eliminar_comentario(
    propiedad_id: str, comentario_id: int, quien: Operador, db: Session = Depends(get_db)
):
    """Solo el operador borra comentarios: moderar es una acción de escritura."""
    comentario = db.get(ComentarioPropiedad, comentario_id)
    if comentario is None or comentario.propiedad_id != propiedad_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comentario no encontrado")
    db.delete(comentario)
    db.flush()
    auditar(
        db, actor=quien, accion="comentario_eliminado", entidad="propiedad",
        entidad_id=propiedad_id, detalle=f"de {comentario.autor}",
    )
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}#comentarios", status_code=303)


@router.post("/propiedades/{propiedad_id}/editar")
def editar_propiedad(
    propiedad_id: str,
    quien: Operador,
    ciudad: str = Form(...),
    zona: str = Form(""),
    tipo: str = Form(...),
    negocio: str = Form(NEGOCIO_POR_DEFECTO),
    precio: int = Form(...),
    habitaciones: int = Form(0),
    banos: int = Form(0),
    area_m2: float = Form(0),
    descripcion: str = Form(""),
    propietario: str = Form(""),
    propietario_telefono: str = Form(""),
    url_origen: str = Form(""),
    db: Session = Depends(get_db),
):
    """Corrige los datos de un inmueble (RF-10).

    Deliberadamente NO se pueden editar `fuente`, `externo_id`, `mandato` ni
    `estado`. Son la procedencia y la autorización: si el formulario permitiera
    cambiar `fuente`, un inmueble de referencia podría convertirse en inventario
    vendible con un `select`, y el bloqueo de comisión dejaría de valer. El
    estado se mueve con aprobar / rechazar / inactivar, que sí quedan auditados
    como lo que son.
    """
    propiedad = _propiedad(db, propiedad_id)

    ciudad_ok = ingesta.normalizar_ciudad(ciudad)
    if ciudad_ok is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{ciudad}' no es un municipio de Antioquia ni de Risaralda.",
        )
    tipo_ok = ingesta.normalizar_tipo(tipo)
    if tipo_ok is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Tipo no reconocido: '{tipo}'.")
    negocio_ok = ingesta.normalizar_negocio(negocio)
    # El precio lo pone el operador tal cual: el rango razonable de la ingesta
    # existe para atajar un extractor que leyó mal un aviso, no para discutirle
    # a quien tiene el inmueble delante. Solo se rechaza lo que no es un precio.
    if precio <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El precio debe ser mayor que cero.")

    cambios = {
        "ciudad": ciudad_ok, "zona": zona.strip()[:80], "tipo": tipo_ok,
        "negocio": negocio_ok,
        "precio": int(precio), "habitaciones": int(habitaciones), "banos": int(banos),
        "area_m2": float(area_m2), "descripcion": descripcion.strip(),
        "propietario": propietario.strip()[:120], "url_origen": url_origen.strip()[:300],
    }
    portfolio.actualizar(db, propiedad, cambios, actor=quien)

    # El teléfono va aparte: al cambiarlo hay que rehacer el índice ciego, o las
    # búsquedas por dueño dejarían de encontrarlo.
    nuevo_tel = propietario_telefono.strip() or None
    if nuevo_tel != propiedad.propietario_telefono:
        propiedad.propietario_telefono = nuevo_tel
        propiedad.propietario_telefono_hash = indice_ciego(nuevo_tel) if nuevo_tel else None
        db.flush()

    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/eliminar")
def eliminar_propiedad(propiedad_id: str, quien: Operador, db: Session = Depends(get_db)):
    """Borra el inmueble con sus fotos y comentarios. Solo el operador."""
    try:
        ingesta.eliminar_inmueble(db, _propiedad(db, propiedad_id), actor=quien)
    except ingesta.TieneVenta as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.post("/propiedades/purgar-referencias")
def purgar_referencias(quien: Operador, db: Session = Depends(get_db)):
    """Elimina de un golpe todo lo cargado como referencia (antes de producción)."""
    ingesta.purgar_referencias(db, actor=quien)
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.post("/propiedades/{propiedad_id}/fotos")
def subir_fotos(
    propiedad_id: str,
    quien: Operador,
    imagenes: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Añade imágenes al inmueble. La primera es la portada de la tarjeta."""
    fotos.guardar(db, _propiedad(db, propiedad_id), imagenes, actor=quien)
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/fotos/orden")
def reordenar_fotos(
    propiedad_id: str,
    quien: Operador,
    orden: str = Form(""),
    db: Session = Depends(get_db),
):
    """Recibe el nuevo orden como ids separados por coma."""
    ids = [int(p) for p in orden.split(",") if p.strip().isdigit()]
    if ids:
        fotos.reordenar(db, _propiedad(db, propiedad_id), ids, actor=quien)
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/fotos/{foto_id}/portada")
def marcar_portada(
    propiedad_id: str, foto_id: int, quien: Operador, db: Session = Depends(get_db)
):
    """Sube una foto al primer lugar sin necesidad de arrastrar."""
    foto = db.get(FotoPropiedad, foto_id)
    if foto is None or foto.propiedad_id != propiedad_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foto no encontrada")
    fotos.hacer_portada(db, foto, actor=quien)
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/fotos/{foto_id}/eliminar")
def eliminar_foto(
    propiedad_id: str, foto_id: int, quien: Operador, db: Session = Depends(get_db)
):
    foto = db.get(FotoPropiedad, foto_id)
    if foto is None or foto.propiedad_id != propiedad_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foto no encontrada")
    fotos.eliminar(db, foto, actor=quien)
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/reactivar")
def reactivar_propiedad(propiedad_id: str, quien: Operador, db: Session = Depends(get_db)):
    """Vuelve a publicar un inmueble que se había inactivado o rechazado."""
    try:
        ingesta.reactivar(db, _propiedad(db, propiedad_id), actor=quien)
    except (ingesta.MandatoAusente, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RedirectResponse(f"/dashboard/propiedades/{propiedad_id}", status_code=303)


@router.post("/propiedades/{propiedad_id}/aprobar")
def aprobar_propiedad(propiedad_id: str, quien: Operador, db: Session = Depends(get_db)):
    """Revisión humana antes de que el inmueble llegue a un comprador (ADR-05)."""
    try:
        ingesta.aprobar(db, _propiedad(db, propiedad_id), actor=quien)
    except ingesta.MandatoAusente as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.post("/propiedades/{propiedad_id}/rechazar")
def rechazar_propiedad(
    propiedad_id: str,
    quien: Operador,
    motivo: str = Form(""),
    db: Session = Depends(get_db),
):
    ingesta.rechazar(db, _propiedad(db, propiedad_id), actor=quien, motivo=motivo)
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.post("/propiedades")
def crear_propiedad(
    quien: Operador,
    ciudad: str = Form(...),
    zona: str = Form(...),
    tipo: str = Form(...),
    negocio: str = Form(NEGOCIO_POR_DEFECTO),
    habitaciones: int = Form(0),
    banos: int = Form(0),
    area_m2: float = Form(0),
    precio: int = Form(...),
    descripcion: str = Form(""),
    propietario: str = Form(""),
    db: Session = Depends(get_db),
):
    ciudad_ok = ingesta.normalizar_ciudad(ciudad)
    if ciudad_ok is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{ciudad}' no es un municipio de Antioquia ni de Risaralda.",
        )
    tipo_ok = ingesta.normalizar_tipo(tipo)
    if tipo_ok is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Tipo no reconocido: '{tipo}'.")
    # Igual que al editar: el precio es el que diga el operador.
    if precio <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El precio debe ser mayor que cero.")

    portfolio.crear(
        db,
        {
            "ciudad": ciudad_ok, "zona": zona, "tipo": tipo_ok,
            "negocio": ingesta.normalizar_negocio(negocio),
            "habitaciones": habitaciones, "banos": banos, "area_m2": area_m2,
            "precio": precio, "descripcion": descripcion, "propietario": propietario,
        },
        actor=quien,
    )
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.post("/propiedades/{propiedad_id}/inactivar")
def inactivar_propiedad(propiedad_id: str, quien: Operador, db: Session = Depends(get_db)):
    portfolio.inactivar(db, _propiedad(db, propiedad_id), actor=quien)
    return RedirectResponse("/dashboard/propiedades", status_code=303)


@router.get("/captacion", response_class=HTMLResponse)
def captacion(request: Request, quien: Operador, db: Session = Depends(get_db)):
    """Radar de captación: qué red social está trayendo compradores reales."""
    return plantillas.TemplateResponse(
        request,
        "captacion.html",
        _base(
            request,
            quien,
            canales=prospecting.rendimiento_canales(db),
            campanas=prospecting.campanas_con_metricas(db),
            base_url=str(request.base_url).rstrip("/"),
        ),
    )


@router.post("/captacion/campana")
def nueva_campana(
    quien: Operador,
    slug: str = Form(...),
    nombre: str = Form(...),
    red: str = Form(...),
    db: Session = Depends(get_db),
):
    prospecting.crear_campana(db, slug=slug, nombre=nombre, red=red, actor=quien)
    return RedirectResponse("/dashboard/captacion", status_code=303)


@router.get("/auditoria", response_class=HTMLResponse)
def auditoria(request: Request, quien: Operador, db: Session = Depends(get_db)):
    """Bitácora consultable y verificación de integridad de la cadena (HU-10)."""
    integra, roto_en = verificar_cadena(db)
    registros = list(
        db.scalars(select(LogAuditoria).order_by(desc(LogAuditoria.id)).limit(300))
    )
    return plantillas.TemplateResponse(
        request,
        "auditoria.html",
        _base(request, quien, registros=registros, integra=integra, roto_en=roto_en),
    )


# ─────────────────────────── Canal WhatsApp (ADR-02b) ───────────────────────────
#
# Solo operador: el QR de vinculación es una credencial de sesión de WhatsApp.
# Quien lo escanea queda con el número en la mano, así que el invitado de solo
# lectura no debe verlo siquiera.


@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp(request: Request, quien: Operador, db: Session = Depends(get_db)):
    """Estado del canal y vinculación del número."""
    return plantillas.TemplateResponse(
        request,
        "whatsapp.html",
        _base(
            request,
            quien,
            configurado=settings.tiene_whatsapp,
            instancia=settings.evolution_instancia,
            evolution_url=settings.evolution_url,
            url_webhook=whatsapp_evo.url_webhook() if settings.tiene_whatsapp else "",
            numeros_prueba=sorted(ajustes.numeros_prueba(db)),
            numeros_del_entorno=ajustes.desde_el_entorno(db),
            estado=whatsapp_evo.estado_conexion(),
        ),
    )


@router.post("/whatsapp/numeros-prueba")
def whatsapp_numeros_prueba(
    quien: Operador,
    numeros: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    """Cambia la lista blanca de pruebas sin tocar el `.env` ni reiniciar.

    Vaciar el campo es una decisión válida y significa producción: el bot le
    responde a todo el que escriba. Por eso se guarda el vacío en vez de volver
    al valor del entorno.
    """
    ajustes.guardar(db, ajustes.NUMEROS_PRUEBA, ajustes.normalizar_lista(numeros), actor=quien)
    return RedirectResponse("/dashboard/whatsapp", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/whatsapp/estado")
def whatsapp_estado(quien: Operador):
    """Estado en JSON, para que la página se actualice sola mientras se escanea."""
    return {"estado": whatsapp_evo.estado_conexion()}


@router.get("/whatsapp/qr-actual")
def whatsapp_qr_actual(quien: Operador):
    """El QR vigente, sin tocar la conexión.

    Existe para que el panel pueda refrescar la imagen mientras el operador
    busca el teléfono. La versión anterior refrescaba llamando otra vez a
    `/instance/connect`, y eso no pide otro código: reinicia el socket de
    Baileys. Si el reinicio caía mientras alguien escaneaba, el teléfono
    respondía «no se pudo vincular el dispositivo». Aquí solo se lee lo que
    Evolution ya empujó por el webhook.
    """
    return {"qr": whatsapp_evo.ultimo_qr(), "estado": whatsapp_evo.estado_conexion()}


@router.post("/whatsapp/qr")
def whatsapp_qr(quien: Operador, db: Session = Depends(get_db)):
    """Un QR fresco en JSON, para que la página lo renueve sin recargarse.

    El QR de WhatsApp vive segundos: el operador pulsaba «Vincular», iba a
    buscar el teléfono y volvía a un código ya muerto que la página no tenía
    forma de reemplazar. Parecía que el QR no cargaba nunca. Con este endpoint
    la vista se renueva sola mientras el teléfono aparece.

    Se llama **una sola vez por intento**, al pulsar el botón: abre la conexión y
    devuelve el primer código. Los siguientes los empuja Evolution por webhook y
    el panel los lee de `/whatsapp/qr-actual`.
    """
    if not settings.tiene_whatsapp:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "El canal no está configurado: faltan EVOLUTION_URL, EVOLUTION_API_KEY "
            "o EVOLUTION_WEBHOOK_TOKEN.",
        )
    try:
        alta = whatsapp_evo.crear_instancia()
        whatsapp_evo.configurar_webhook()
        datos = whatsapp_evo.qr_de_conexion()
    except httpx.HTTPError as e:
        log.warning("No se pudo pedir el QR de WhatsApp: %s", e)
        return {
            "error": (
                f"No se pudo hablar con Evolution API en {settings.evolution_url}. "
                "¿Está levantado el gateway?"
            )
        }

    auditar(
        db, actor=quien, accion="whatsapp_vinculacion_iniciada", entidad="canal",
        entidad_id=settings.evolution_instancia, detalle=f"instancia {alta}",
    )

    return {
        "qr": whatsapp_evo.qr_data_uri(datos),
        "codigo_pareo": datos.get("pairingCode"),
        "estado": whatsapp_evo.estado_conexion(),
    }


@router.post("/whatsapp/vincular", response_class=HTMLResponse)
def whatsapp_vincular(request: Request, quien: Operador, db: Session = Depends(get_db)):
    """Crea la instancia si hace falta, fija el webhook y muestra el QR.

    Es idempotente a propósito: el operador va a apretar este botón cada vez que
    el canal se caiga, y no tiene por qué saber si la instancia ya existía.
    """
    if not settings.tiene_whatsapp:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "El canal no está configurado: faltan EVOLUTION_URL, EVOLUTION_API_KEY "
            "o EVOLUTION_WEBHOOK_TOKEN en el .env.",
        )

    contexto = {
        "configurado": True,
        "instancia": settings.evolution_instancia,
        "evolution_url": settings.evolution_url,
        "url_webhook": whatsapp_evo.url_webhook(),
        "numeros_prueba": sorted(ajustes.numeros_prueba(db)),
        "numeros_del_entorno": ajustes.desde_el_entorno(db),
    }

    try:
        alta = whatsapp_evo.crear_instancia()
        whatsapp_evo.configurar_webhook()
        datos = whatsapp_evo.qr_de_conexion()
    except httpx.HTTPError as e:
        log.warning("No se pudo vincular WhatsApp: %s", e)
        return plantillas.TemplateResponse(
            request,
            "whatsapp.html",
            _base(
                request, quien, **contexto,
                estado="error",
                error=(
                    f"No se pudo hablar con Evolution API en {settings.evolution_url}. "
                    "¿Está levantado el contenedor?"
                ),
            ),
        )

    auditar(
        db, actor=quien, accion="whatsapp_vinculacion_iniciada", entidad="canal",
        entidad_id=settings.evolution_instancia, detalle=f"instancia {alta}",
    )

    return plantillas.TemplateResponse(
        request,
        "whatsapp.html",
        _base(
            request, quien, **contexto,
            estado=whatsapp_evo.estado_conexion(),
            qr=whatsapp_evo.qr_data_uri(datos),
            codigo_pareo=datos.get("pairingCode"),
        ),
    )


@router.post("/whatsapp/desvincular")
def whatsapp_desvincular(quien: Operador, db: Session = Depends(get_db)):
    """Cierra la sesión de WhatsApp. El número queda libre; la instancia, en pie."""
    try:
        whatsapp_evo.desvincular()
    except httpx.HTTPError as e:
        log.warning("No se pudo desvincular WhatsApp: %s", e)
    auditar(
        db, actor=quien, accion="whatsapp_desvinculado", entidad="canal",
        entidad_id=settings.evolution_instancia,
    )
    return RedirectResponse("/dashboard/whatsapp", status_code=303)
