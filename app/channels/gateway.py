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
    pide_visita,
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


def _plural(tipo: str, n: int) -> str:
    """'apartamento' → 'apartamentos'. Los tres tipos pluralizan con -s."""
    return f"{tipo}s" if n != 1 else tipo


def _formatear_matches(
    matches: list[Match], *, listado: bool = False, municipio: str | None = None
) -> str:
    """Arma el mensaje de resultados.

    Dos formas para dos intenciones distintas. La curada (tres opciones) lleva
    la frase de venta de cada inmueble, porque el trabajo ahí es convencer. El
    listado completo la omite: con ocho fichas, esas frases convierten el
    mensaje en un muro y el comprador deja de leer justo cuando por fin tiene
    todo el inventario delante.

    En ambas, cada inmueble va en su propio bloque separado por una línea en
    blanco: en un chat, un párrafo continuo con ocho direcciones no se lee.
    """
    if listado and matches:
        primera = matches[0].propiedad
        encabezado = PLANTILLAS["matches_encabezado_listado"].format(
            n=len(matches),
            tipos=_plural(primera.tipo, len(matches)),
            # El municipio que preguntó, no la plaza de cobertura: a quien pide
            # Dosquebradas, "Tengo 2 apartamentos en Pereira" le suena a error.
            ciudad=municipio or primera.ciudad,
        )
        plantilla = PLANTILLAS["matches_item_listado"]
    else:
        encabezado = PLANTILLAS["matches_encabezado"].format(n=len(matches))
        plantilla = PLANTILLAS["matches_item"]

    items = [
        plantilla.format(
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
        # El piso se mide dentro del mismo negocio: comparar un canon mensual
        # contra el inmueble en venta más barato declararía «presupuesto bajo»
        # a cualquiera que venga a arrendar.
        precio_minimo_cartera=precio_minimo(
            db, perfil_previo.get("ciudad"), perfil_previo.get("negocio")
        ),
        cartera_contexto=matching_engine.contexto_cartera(db, perfil_previo),
    )
    leads.aplicar_analisis(db, prospecto, analisis)

    listado = pide_listado_completo(texto) or pide_sin_tope(texto)
    respuesta = Respuesta(prospecto=prospecto)

    # Con ciudad y tipo sobre la mesa la pregunta ya es concreta ("apartamentos
    # en Pereira") y lo que corresponde es enseñar el inventario completo, no
    # una terna curada ni otra pregunta. Pedir el presupuesto antes de mostrar
    # nada es lo que hacía parecer que la cartera estaba vacía; el presupuesto
    # se afina después, con los inmuebles ya delante.
    perfil_actual = leads.perfil(prospecto)
    listar_todo = listado or bool(perfil_actual.get("ciudad") and perfil_actual.get("tipo"))

    # El handoff se decide ANTES de redactar el turno. Si el mensaje va a
    # terminar con "ya le pasé tus datos al asesor", el turno no puede además
    # pedirle nada al comprador: quedaría sin saber si debe responder o esperar.
    #
    # `pide_visita` sigue en true en los turnos siguientes, porque la petición
    # está en el historial que ve el LLM. Por eso el handoff solo se dispara
    # cuando NO hay ya una solicitud en la cola: si no, cada mensaje posterior
    # crearía otra solicitud, otro aviso al asesor, y el comprador dejaría de
    # recibir respuestas a lo que en realidad está preguntando.
    handoff_en_cola = leads.tiene_solicitud_pendiente(db, prospecto)
    puede_handoff = prospecto.estado_enum not in (
        EstadoProspecto.VENDIDO,
        EstadoProspecto.PERDIDO,
    )
    hara_handoff = analisis.pide_visita and puede_handoff and not handoff_en_cola
    # Para el recordatorio se mira SOLO el mensaje de ahora, no el análisis
    # acumulado: si no, el aviso se pegaría a cada respuesta durante el resto
    # de la conversación.
    repite_peticion = pide_visita(texto) and puede_handoff and handoff_en_cola

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
    elif analisis.faltan_datos and not listar_todo:
        # Callado si el turno acaba en handoff: el asesor humano preguntará lo
        # que falte. La pregunta del bot solo compite con el cierre.
        if not hara_handoff:
            respuesta.textos.append(
                analisis.respuesta_sugerida or pregunta_siguiente(analisis.faltan_datos)
            )

    # 3) Emparejar contra la cartera.
    else:
        perfil = perfil_actual
        limite = matching_engine.TOPE_LISTADO if listar_todo else matching_engine.TOPE_RESULTADOS
        matches = matching_engine.emparejar(db, prospecto, perfil, limite)
        if matches:
            leads.marcar_emparejado(db, prospecto)
            respuesta.matches = matches
            en_rango, total = matching_engine.conteo(db, perfil)
            respuesta.textos.append(
                "\n\n".join(
                    filter(
                        None,
                        [
                            _formatear_matches(
                                matches,
                                listado=listar_todo,
                                municipio=perfil.get("municipio"),
                            ),
                            _nota_filtro(perfil, en_rango, total),
                        ],
                    )
                )
            )
        else:
            respuesta.textos.append(PLANTILLAS["sin_matches"])

    # 4) Handoff a humano si lo pidió (RF-12).
    if hara_handoff:
        propiedad_id = respuesta.matches[0].propiedad.id if respuesta.matches else None
        leads.solicitar_handoff(
            db, prospecto, tipo="visita", propiedad_id=propiedad_id, detalle=texto[:400]
        )
        respuesta.handoff = True
        respuesta.textos.append(PLANTILLAS["handoff"].format(empresa=settings.empresa_nombre))

    # Ya hay un asesor en camino: se lo recordamos una línea y seguimos
    # atendiéndolo. Mientras espera puede seguir mirando la cartera, que es
    # justo cuando más ganas tiene de mirarla.
    elif repite_peticion:
        respuesta.handoff = True
        respuesta.textos.append(PLANTILLAS["handoff_en_cola"])

    for salida in respuesta.textos:
        leads.registrar_mensaje(db, prospecto, Direccion.SALIENTE, salida)

    return respuesta
