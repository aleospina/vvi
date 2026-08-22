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
from app.models import Canal, Direccion, Prospecto, ahora
from app.services import leads, matching_engine, notificaciones, seguimiento
from app.services.compliance import (
    aviso_ia,
    registrar_consentimiento,
    tiene_consentimiento_vigente,
    texto_consentimiento,
)
from app.services.matching_engine import Match
from app.services.nlu_engine import (
    analizar,
    es_afirmativo,
    es_despedida,
    es_negativo,
    es_saludo,
    extraer_slots,
    pide_listado_completo,
    pide_sin_tope,
    pide_visita,
    pregunta_siguiente,
    respuesta_de_cierre,
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
    #: El comprador declaró que el negocio ya se cerró (PRD §10).
    cierre_declarado: bool = False
    #: El titular se despidió: este turno cierra la conversación y el siguiente
    #: mensaje suyo arranca una nueva, con su propia autorización.
    conversacion_cerrada: bool = False
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
            # El hash sirve para encontrarlo cuando él escribe; para escribirle a
            # él hace falta el identificador en claro (cifrado en reposo).
            canal_id_claro=entrante.canal_id,
            nombre=entrante.nombre,
            telefono=entrante.telefono,
            usuario_canal=entrante.usuario_canal,
            campana=entrante.campana,
            red_origen=entrante.red_origen,
        )
    else:
        # Reingreso de un titular conocido: completamos lo que falte.
        prospecto.canal_id = prospecto.canal_id or entrante.canal_id
        prospecto.nombre = prospecto.nombre or entrante.nombre
        prospecto.telefono = prospecto.telefono or entrante.telefono
        prospecto.usuario_canal = prospecto.usuario_canal or entrante.usuario_canal

    # Autorizar es, por definición, abrir conversación: si venía de una
    # despedida, esta es la nueva y el recorte de la anterior no la gobierna.
    # El perfil de búsqueda sí se conserva —ciudad, tipo, presupuesto—: hacerle
    # repetir en cada visita lo que ya nos contó no es empezar de cero, es
    # tratarlo como un desconocido.
    prospecto.conversacion_cerrada_ts = None
    prospecto.foco = None

    registrar_consentimiento(db, prospecto, canal=canal, evidencia=evidencia)
    return prospecto


def rechazo_consentimiento() -> str:
    return PLANTILLAS["consentimiento_negado"]


# ─────────────────────── Apertura y cierre de conversación ───────────────────────


def conversacion_cerrada(prospecto: Prospecto) -> bool:
    """¿El titular dio por terminada la conversación y aún no ha vuelto a autorizar?"""
    return prospecto.conversacion_cerrada_ts is not None


def cerrar_conversacion(db: Session, prospecto: Prospecto) -> None:
    """Marca el cierre pedido por el titular ("gracias", "hasta luego").

    No borra nada ni revoca la autorización archivada: el titular se despidió,
    no ejerció su derecho de supresión —eso es /borrar—. Lo único que cambia es
    que la próxima vez que escriba se le vuelve a pedir permiso, porque será
    otra conversación.
    """
    prospecto.conversacion_cerrada_ts = ahora()
    # El recorte a un inmueble concreto pertenece a la conversación que termina.
    prospecto.foco = None
    db.flush()


def _busqueda_guardada(perfil: dict) -> str:
    """"apartamentos en Envigado", o cadena vacía si aún no sabemos qué busca."""
    tipo, ciudad = perfil.get("tipo"), perfil.get("municipio") or perfil.get("ciudad")
    return f"{_plural(tipo, 2)} en {ciudad}" if tipo and ciudad else ""


def _texto_saludo(perfil: dict) -> str:
    """Saludo, con la búsqueda de antes si la hay."""
    busqueda = _busqueda_guardada(perfil)
    if busqueda:
        return PLANTILLAS["saludo_retomar"].format(busqueda=busqueda)
    return PLANTILLAS["saludo"]


def pregunta_de_calificacion(prospecto: Prospecto) -> str:
    """Lo que se pregunta justo después de que el titular autoriza.

    A quien vuelve tras despedirse no se le pregunta desde cero: la
    autorización es nueva, su búsqueda no.
    """
    busqueda = _busqueda_guardada(leads.perfil(prospecto))
    if busqueda:
        return PLANTILLAS["calificacion_retomar"].format(busqueda=busqueda)
    return PLANTILLAS["calificacion"]


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


def _nota_foco(db: Session, perfil: dict, matches: list[Match]) -> str:
    """Hace visible el recorte por lo que el comprador nombró, y cómo deshacerlo.

    Un filtro que no se anuncia es indistinguible de una cartera pobre. Quien
    pidió "solo la ferretería de La Reforma" y dos mensajes después pregunta
    otra cosa tiene que poder volver a verlo todo sin adivinar la palabra
    mágica, así que la nota se la dice.

    Con un solo inmueble se nombra el inmueble y no los términos: cuando el
    recorte vino de un "el lote 6", el término guardado es su código interno y
    devolvérselo no le dice nada.
    """
    terminos = matching_engine.terminos_foco(perfil)
    if not terminos:
        return ""
    que = _ubicacion(matches[0].propiedad) if len(matches) == 1 else ", ".join(terminos)
    nota = f"🎯 Te estoy mostrando solo *{que}*."
    total = matching_engine.conteo(db, {**perfil, "foco": None})[1]
    if total > 1:
        nota += f" Dime *todos* y te muestro los {total} otra vez."
    return nota


def _plural(tipo: str, n: int) -> str:
    """'apartamento' → 'apartamentos'. Los tres tipos pluralizan con -s."""
    return f"{tipo}s" if n != 1 else tipo


def _ubicacion(propiedad) -> str:
    """'Centro, Pereira'. Sin repetir el municipio cuando la zona ya es él.

    La cartera guarda la zona con la convención "Barrio, Municipio", pero un
    inmueble cargado sin barrio deja zona = municipio y el resultado era
    "Pereira, Pereira", que parece un error de datos.
    """
    zona = (propiedad.zona or "").strip()
    ciudad = (propiedad.ciudad or "").strip()
    if not zona:
        return ciudad
    if not ciudad or zona.casefold().endswith(ciudad.casefold()):
        return zona
    return f"{zona}, {ciudad}"


def _especificaciones(propiedad) -> str:
    """Los datos duros de la ficha, saltando los que el inmueble no tiene.

    Un lote no tiene habitaciones, y escribir "0 hab" hacía dudar de toda la
    ficha. El área sí va siempre con separador de miles: "2462 m²" se lee peor
    que "2.462 m²" justo cuando el número es el argumento de venta.
    """
    partes = []
    if propiedad.habitaciones:
        partes.append(f"{propiedad.habitaciones} hab")
    if propiedad.area_m2:
        partes.append(f"{propiedad.area_m2:,.0f} m²".replace(",", "."))
    return "".join(f" · {p}" for p in partes)


#: La descripción en el listado va a una línea: es la que distingue un lote de
#: otro, pero completa (y multiplicada por diez fichas) tapa el mensaje.
TOPE_DESCRIPCION = 120


def _resumen(propiedad) -> str:
    """Primera frase de la descripción, recortada."""
    texto = " ".join((propiedad.descripcion or "").split())
    if not texto:
        return "Disponible para visita."
    frase = texto.split(".")[0].strip() or texto
    if len(frase) > TOPE_DESCRIPCION:
        frase = frase[:TOPE_DESCRIPCION].rsplit(" ", 1)[0] + "…"
    return frase


#: La ficha de un solo inmueble sí puede traer la descripción entera: no
#: compite con otras nueve. El tope es contra el pegote de tres pantallas que a
#: veces trae un aviso copiado de otro portal.
TOPE_FICHA = 600


def _ficha_completa(propiedad) -> str:
    """La descripción tal como la escribió el operador, apenas recortada."""
    texto = " ".join((propiedad.descripcion or "").split())
    if not texto:
        return "Disponible para visita."
    if len(texto) > TOPE_FICHA:
        texto = texto[:TOPE_FICHA].rsplit(" ", 1)[0] + "…"
    return texto


def _ficha_unica(propiedad) -> str:
    """El mensaje cuando la búsqueda deja un solo inmueble.

    Preguntar por uno concreto y recibir "te dejo 1 opción(es)" con la
    descripción cortada a una línea es responder menos de lo que se preguntó.
    Sin encabezado, sin numeración y con la ficha completa.
    """
    return "\n\n".join(
        [
            PLANTILLAS["ficha_unica"].format(
                ubicacion=_ubicacion(propiedad),
                tipo=propiedad.tipo.capitalize(),
                especificaciones=_especificaciones(propiedad),
                precio=pesos(propiedad.precio).lstrip("$"),
                descripcion=_ficha_completa(propiedad),
            ),
            PLANTILLAS["matches_pie"],
        ]
    )


def _formatear_matches(
    matches: list[Match], *, listado: bool = False, municipio: str | None = None
) -> str:
    """Arma el mensaje de resultados.

    Dos formas para dos intenciones distintas. La curada (tres opciones) lleva
    la frase de venta de cada inmueble, porque el trabajo ahí es convencer. El
    listado completo la cambia por la descripción real recortada: con ocho
    fichas del mismo tipo y municipio, lo único que las separa es lo que tiene
    cada una, y una frase de venta repetida ocho veces no se lee.

    En ambas, cada inmueble va en su propio bloque separado por una línea en
    blanco: en un chat, un párrafo continuo con ocho direcciones no se lee.
    """
    if len(matches) == 1:
        return _ficha_unica(matches[0].propiedad)

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
            ubicacion=_ubicacion(m.propiedad),
            tipo=m.propiedad.tipo.capitalize(),
            especificaciones=_especificaciones(m.propiedad),
            precio=pesos(m.propiedad.precio).lstrip("$"),
            descripcion=_resumen(m.propiedad),
            frase=m.frase_venta,
        )
        for i, m in enumerate(matches, start=1)
    ]
    return "\n\n".join([encabezado, *items, PLANTILLAS["matches_pie"]])


#: Slots que cambian lo que la cartera devuelve. `plazo_compra` no está: saber
#: que quiere comprar ya no cambia qué inmuebles le sirven.
SLOTS_DE_BUSQUEDA = (
    "ciudad", "municipio", "zona", "tipo",
    "presupuesto_min", "presupuesto_max", "habitaciones",
)


def _cambia_la_busqueda(texto: str, perfil_previo: dict) -> bool:
    """¿Este mensaje cambia lo que la cartera va a devolver?

    Se compara contra el perfil con el que entró el turno, no contra el vacío:
    nombrar lo que ya estaba puesto no es una búsqueda nueva. Nadie contesta
    "visita" a secas —contesta "visita al lote", "quiero ver el apartamento"—,
    y leer ese "lote" como si acabara de pedir lotes es exactamente lo que hace
    que le vuelva el mismo listado que ya tiene arriba.
    """
    if pide_listado_completo(texto) or pide_sin_tope(texto):
        return True
    slots = extraer_slots(texto)
    return any(c in slots and slots[c] != perfil_previo.get(c) for c in SLOTS_DE_BUSQUEDA)


def _nombra_la_busqueda(texto: str) -> bool:
    """¿El mensaje vuelve a decir qué se busca, aunque no cambie ningún valor?

    "Lotes en Pereira", después de haber acotado a uno, es pedir el conjunto
    otra vez —y `_cambia_la_busqueda` dice que no, porque lote y Pereira ya
    estaban puestos—. Sin esta distinción el comprador quedaba encerrado en la
    ficha que pidió, y solo la palabra *todos* lo sacaba de ahí.

    No sirve para decidir el handoff: ahí "visita al lote" tiene que seguir
    contando como respuesta y no como búsqueda nueva.
    """
    return any(c in extraer_slots(texto) for c in SLOTS_DE_BUSQUEDA)


def _leer_declaracion(db: Session, prospecto: Prospecto, texto: str) -> bool:
    """¿Este mensaje dice si el negocio se cerró? Lo anota y avisa (PRD §10).

    Se mira en **todos** los mensajes, no solo cuando hay una pregunta de
    seguimiento abierta: un "ya compramos, gracias" espontáneo vale igual, y
    perderlo por no haber preguntado primero sería absurdo.

    Un *sí* o un *no* a secas solo cuentan si venían de una pregunta nuestra;
    sueltos no significan nada.
    """
    esperando = seguimiento.esperando_respuesta(db, prospecto)
    resultado = respuesta_de_cierre(texto)
    if resultado is None and esperando is not None:
        if es_afirmativo(texto):
            resultado = seguimiento.CERRO
        elif es_negativo(texto):
            resultado = seguimiento.NO_CERRO
    if resultado is None:
        return False

    registro = seguimiento.registrar_respuesta(db, prospecto, resultado)
    if resultado != seguimiento.CERRO or registro is None:
        return False

    # El aviso nunca puede tumbar el turno: el dato ya quedó guardado y la lista
    # de cierres declarados del dashboard es la fuente de verdad.
    try:
        notificaciones.notificar_cierre_declarado(prospecto, registro.solicitud)
    except Exception:  # noqa: BLE001 - degradación deliberada
        log.warning("No se pudo avisar del cierre declarado por %s", prospecto.codigo,
                    exc_info=True)
    return True


def _turno_fijo(db: Session, prospecto: Prospecto, texto: str, **campos) -> Respuesta:
    """Cierra el turno con una respuesta fija, dejándola en el historial."""
    leads.registrar_mensaje(db, prospecto, Direccion.SALIENTE, texto)
    return Respuesta(textos=[texto], prospecto=prospecto, **campos)


def procesar(db: Session, prospecto: Prospecto, texto: str) -> Respuesta:
    """Procesa un mensaje entrante de un titular que YA autorizó (DF-1 a DF-4)."""
    if not tiene_consentimiento_vigente(prospecto):
        return Respuesta(textos=[mensaje_bienvenida()], pide_consentimiento=True, prospecto=prospecto)

    # El titular se despidió: lo que llegue ahora abre una conversación nueva y
    # empieza por la autorización, no por donde quedó la anterior. Los canales
    # lo resuelven antes de llegar aquí; esto cubre a quien entre por la API.
    if conversacion_cerrada(prospecto):
        return Respuesta(
            textos=[mensaje_bienvenida()], pide_consentimiento=True, prospecto=prospecto
        )

    leads.registrar_mensaje(db, prospecto, Direccion.ENTRANTE, texto)

    # Un saludo o una despedida se resuelven aquí, sin pasar por el
    # clasificador: no hay nada que clasificar y la respuesta no depende de la
    # cartera. Van antes que todo lo demás porque un "hola" a secas caía en la
    # rama de "faltan datos" y abría preguntando por presupuesto, y un "gracias"
    # sin más devolvía el catálogo otra vez, que es lo contrario de despedirse.
    if es_saludo(texto):
        return _turno_fijo(db, prospecto, _texto_saludo(leads.perfil(prospecto)))

    if es_despedida(texto):
        cerrar_conversacion(db, prospecto)
        return _turno_fijo(
            db, prospecto, PLANTILLAS["despedida"], conversacion_cerrada=True
        )

    declara_cierre = _leer_declaracion(db, prospecto, texto)

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

    # ¿Este mensaje mueve la búsqueda? Lo necesitan dos decisiones del turno: el
    # foco —cambiar de búsqueda lo suelta— y el handoff, más abajo.
    cambia_busqueda = _cambia_la_busqueda(texto, perfil_previo)

    # Acotar la conversación a un inmueble concreto ("háblame solo de la
    # ferretería de La Reforma") es una petición tan legítima como el municipio o
    # el presupuesto, pero no cabe en ningún slot: lo que lo identifica está
    # escrito en la zona o en la descripción de la ficha. Sin esto, el turno
    # volvía a listar los seis lotes del municipio y el comprador leía que el
    # bot no le había entendido.
    # Volver a nombrar qué se busca suelta el recorte, aunque nombre lo mismo
    # que ya estaba. Pedir visita no: "visita al lote" es contestar el pie del
    # mensaje anterior, no pedir la cartera de lotes.
    suelta_el_foco = cambia_busqueda or (
        _nombra_la_busqueda(texto) and not pide_visita(texto)
    )
    foco = matching_engine.foco_del_turno(
        db, texto, leads.perfil(prospecto), limpiar=suelta_el_foco
    )
    if foco is not None:
        prospecto.foco = foco or None
        db.flush()

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
    #
    # Un estado terminal no cancela la petición. Antes, un lead en `vendido` o
    # `perdido` que pedía visita no disparaba nada: el turno se iba al bloque 3
    # y le repetía el catálogo, así que el comprador quedaba pidiendo un asesor
    # que nadie iba a llamar. La máquina de estados no necesitaba esa guardia
    # —`solicitar_handoff` ya decide por su cuenta si el estado puede moverse, y
    # desde un terminal no lo mueve—, y quien vuelve después de cerrada la
    # ficha, comprador o lead dado por perdido, es justo a quien más conviene
    # pasarle un humano.
    handoff_en_cola = leads.tiene_solicitud_pendiente(db, prospecto)
    hara_handoff = analisis.pide_visita and not handoff_en_cola
    # Para el recordatorio se mira SOLO el mensaje de ahora, no el análisis
    # acumulado: si no, el aviso se pegaría a cada respuesta durante el resto
    # de la conversación.
    repite_peticion = pide_visita(texto) and handoff_en_cola

    # Contestar "visita" o "asesor" a la pregunta del pie —"¿Quieres agendar una
    # *visita* o hablar con un *asesor*?"— no es una búsqueda nueva: es la
    # respuesta a lo que el bot acaba de preguntar. Volverle a mandar los mismos
    # apartamentos o lotes que ya tiene arriba hace parecer que el bot no
    # entendió, y entierra la confirmación, que es lo único que importa en ese
    # turno. Si el mensaje sí mueve la búsqueda ("mejor quiero ver los de
    # Medellín"), la cartera vuelve a salir: ahí sí preguntó por algo.
    solo_handoff = (
        (hara_handoff or repite_peticion)
        and pide_visita(texto)
        and not cambia_busqueda
    )

    # Quien acaba de decir que ya compró no necesita ver la cartera otra vez:
    # se le agradece y se cierra. Lo que sigue —verificar si esa venta está
    # registrada— es trabajo del operador, no de él.
    if declara_cierre:
        respuesta.cierre_declarado = True
        respuesta.textos.append(PLANTILLAS["seguimiento_cierre"])

    elif solo_handoff:
        pass  # el turno entero lo cierra el bloque 4

    # 1) Regla dura: fuera de cobertura, se dice con transparencia (CU-4).
    elif analisis.motivo_fuera_alcance:
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
                            _nota_foco(db, perfil, matches),
                            _nota_filtro(perfil, en_rango, total),
                        ],
                    )
                )
            )
        else:
            # Sin resultados, la nota del foco es lo único que explica por qué:
            # sin ella, "no tengo nada" con un recorte activo parece una cartera
            # vacía y no una búsqueda de una sola palabra.
            respuesta.textos.append(
                "\n\n".join(
                    filter(None, [PLANTILLAS["sin_matches"], _nota_foco(db, perfil, [])])
                )
            )

    # 4) Handoff a humano si lo pidió (RF-12). No cuando acaba de declarar el
    #    cierre: mandarle un asesor a quien ya compró es no haberlo escuchado.
    if hara_handoff and not declara_cierre:
        propiedad_id = (
            respuesta.matches[0].propiedad.id
            if respuesta.matches
            # "Quiero visitar la ferretería de La Reforma" cierra el turno sin
            # volver a emparejar, así que el inmueble sale del foco: el último
            # mostrado sería el mejor puntuado de la tanda anterior, que es otro.
            else matching_engine.enfocada(db, perfil_actual)
            or matching_engine.ultimo_mostrado(db, prospecto)
        )
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
