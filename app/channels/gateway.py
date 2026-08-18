"""Gateway de canal (SRS §2.1).

Normaliza la entrada de cualquier canal y orquesta el turno conversacional.
La lógica vive aquí, no en el bot: añadir WhatsApp en Fase 2 no reescribe nada
(ADR-02).

Invariante de cumplimiento: **no se persiste PII antes del consentimiento**.
Mientras el titular no autorice, la conversación no existe en la base de datos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import settings
from app.llm.prompts import PLANTILLAS
from app.models import Canal, Direccion, EstadoProspecto, Prospecto
from app.services import leads, matching_engine
from app.services.compliance import (
    aviso_ia,
    registrar_consentimiento,
    tiene_consentimiento_vigente,
    texto_consentimiento,
)
from app.services.matching_engine import Match
from app.services.nlu_engine import (
    analizar,
    pide_listado_completo,
    pide_sin_tope,
    pregunta_siguiente,
)
from app.services.portfolio import precio_minimo

log = logging.getLogger(__name__)


def pesos(valor: int | float) -> str:
    """Formato de moneda colombiano: $520.000.000."""
    return f"${int(valor):,}".replace(",", ".")


@dataclass
class MensajeEntrante:
    canal: Canal | str
    canal_id: str
    texto: str
    nombre: str | None = None
    telefono: str | None = None
    usuario_canal: str | None = None
    campana: str | None = None
    red_origen: str | None = None


@dataclass
class Respuesta:
    textos: list[str] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    pide_consentimiento: bool = False
    handoff: bool = False
    prospecto: Prospecto | None = None


# ─────────────────────────── Consentimiento ───────────────────────────


def mensaje_bienvenida() -> str:
    """Primer mensaje obligatorio: aviso de IA + solicitud de autorización (RF-04)."""
    return PLANTILLAS["bienvenida"].format(
        aviso_ia=aviso_ia(), politica=settings.politica_privacidad_url
    )


def texto_autorizacion_completo() -> str:
    return texto_consentimiento()


def alta_con_consentimiento(
    db: Session,
    entrante: MensajeEntrante,
    *,
    evidencia: str = "",
) -> Prospecto:
    """Crea el prospecto y archiva su autorización (RF-16).

    Es el único camino por el que la PII entra a la base de datos.
    """
    canal = entrante.canal.value if isinstance(entrante.canal, Canal) else str(entrante.canal)
    prospecto = leads.buscar_por_canal(db, canal, entrante.canal_id)
    if prospecto is None:
        prospecto = leads.crear(
            db,
            canal=canal,
            canal_id=entrante.canal_id,
            nombre=entrante.nombre,
            telefono=entrante.telefono,
            usuario_canal=entrante.usuario_canal,
            campana=entrante.campana,
            red_origen=entrante.red_origen,
        )
    else:
        # Reingreso de un titular conocido: completamos lo que falte.
        prospecto.nombre = prospecto.nombre or entrante.nombre
        prospecto.telefono = prospecto.telefono or entrante.telefono
        prospecto.usuario_canal = prospecto.usuario_canal or entrante.usuario_canal

    registrar_consentimiento(db, prospecto, canal=canal, evidencia=evidencia)
    return prospecto


def rechazo_consentimiento() -> str:
    return PLANTILLAS["consentimiento_negado"]


# ─────────────────────────── Turno conversacional ───────────────────────────


def _rango_presupuesto(perfil: dict) -> str:
    """Describe en palabras la banda de precio vigente, o cadena vacía si no hay."""
    pmin, pmax = perfil.get("presupuesto_min"), perfil.get("presupuesto_max")
    if pmin and pmax:
        return f"entre {pesos(pmin)} y {pesos(pmax)}"
    if pmax:
        return f"hasta {pesos(pmax)}"
    if pmin:
        return f"desde {pesos(pmin)}"
    return ""


def _nota_filtro(perfil: dict, en_rango: int, total: int) -> str:
    """Hace visible el filtro de precio que arrastra la conversación.

    El presupuesto se acumula de turnos anteriores, así que el comprador puede
    pedir "apartamentos en Medellín" y recibir uno solo sin entender por qué.
    Decirle qué banda está aplicando —y cómo quitarla— es la diferencia entre un
    recorte legítimo y lo que parece un catálogo vacío.
    """
    rango = _rango_presupuesto(perfil)
    if not rango:
        return ""
    fuera = total - en_rango
    nota = f"🔎 Estoy filtrando {rango}."
    if fuera > 0:
        nota += (
            f" Tengo {total} en total y {fuera} quedan fuera de esa banda; "
            "dime *sin tope* o un presupuesto nuevo para verlos."
        )
    return nota


def _formatear_matches(matches: list[Match]) -> str:
    encabezado = PLANTILLAS["matches_encabezado"].format(n=len(matches))
    items = [
        PLANTILLAS["matches_item"].format(
            i=i,
            zona=m.propiedad.zona,
            ciudad=m.propiedad.ciudad,
            tipo=m.propiedad.tipo,
            habitaciones=m.propiedad.habitaciones,
            area=m.propiedad.area_m2,
            precio=pesos(m.propiedad.precio).lstrip("$"),
            frase=m.frase_venta,
        )
        for i, m in enumerate(matches, start=1)
    ]
    return "\n\n".join([encabezado, *items, PLANTILLAS["matches_pie"]])


def procesar(db: Session, prospecto: Prospecto, texto: str) -> Respuesta:
    """Procesa un mensaje entrante de un titular que YA autorizó (DF-1 a DF-4)."""
    if not tiene_consentimiento_vigente(prospecto):
        return Respuesta(textos=[mensaje_bienvenida()], pide_consentimiento=True, prospecto=prospecto)

    leads.registrar_mensaje(db, prospecto, Direccion.ENTRANTE, texto)

    perfil_previo = leads.perfil(prospecto)
    analisis = analizar(
        texto,
        leads.historial(prospecto),
        perfil_previo,
        precio_minimo_cartera=precio_minimo(db, perfil_previo.get("ciudad")),
        cartera_contexto=matching_engine.contexto_cartera(db, perfil_previo),
    )
    leads.aplicar_analisis(db, prospecto, analisis)

    listado = pide_listado_completo(texto) or pide_sin_tope(texto)
    respuesta = Respuesta(prospecto=prospecto)

    # 1) Regla dura: fuera de cobertura, se dice con transparencia (CU-4).
    if analisis.motivo_fuera_alcance:
        respuesta.textos.append(
            PLANTILLAS["fuera_de_alcance"].format(
                ciudades=" y ".join(settings.ciudades_cobertura)
            )
        )

    # 2) Faltan datos para emparejar: una sola pregunta. Salvo que el comprador
    #    haya pedido ver la cartera de frente y ya sepamos ciudad y tipo: ahí
    #    mostrarla y preguntar después es mejor que negarle el catálogo.
    elif analisis.faltan_datos and not (
        listado and analisis.slots.get("ciudad") and analisis.slots.get("tipo")
    ):
        respuesta.textos.append(
            analisis.respuesta_sugerida or pregunta_siguiente(analisis.faltan_datos)
        )

    # 3) Emparejar contra la cartera.
    else:
        perfil = leads.perfil(prospecto)
        limite = matching_engine.TOPE_LISTADO if listado else matching_engine.TOPE_RESULTADOS
        matches = matching_engine.emparejar(db, prospecto, perfil, limite)
        if matches:
            leads.marcar_emparejado(db, prospecto)
            respuesta.matches = matches
            en_rango, total = matching_engine.conteo(db, perfil)
            respuesta.textos.append(
                "\n\n".join(
                    filter(None, [_formatear_matches(matches), _nota_filtro(perfil, en_rango, total)])
                )
            )
        else:
            respuesta.textos.append(PLANTILLAS["sin_matches"])

    # 4) Handoff a humano si lo pidió (RF-12).
    if analisis.pide_visita and prospecto.estado_enum not in (
        EstadoProspecto.VENDIDO,
        EstadoProspecto.PERDIDO,
    ):
        propiedad_id = respuesta.matches[0].propiedad.id if respuesta.matches else None
        leads.solicitar_handoff(
            db, prospecto, tipo="visita", propiedad_id=propiedad_id, detalle=texto[:400]
        )
        respuesta.handoff = True
        respuesta.textos.append(PLANTILLAS["handoff"].format(empresa=settings.empresa_nombre))

    for salida in respuesta.textos:
        leads.registrar_mensaje(db, prospecto, Direccion.SALIENTE, salida)

    return respuesta
