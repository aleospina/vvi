"""Motor conversacional y de clasificación (SRS §2.2 · ADR-03).

Híbrido: reglas duras (determinísticas, sin costo) + LLM (cobertura de lenguaje
natural). Si no hay LLM configurado el módulo funciona igual, solo con reglas.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from app.config import settings
from app.llm.client import cliente
from app.models import Etiqueta, TipoInmueble

log = logging.getLogger(__name__)

# ─────────────────────────── Utilidades de texto ───────────────────────────


def normalizar(texto: str) -> str:
    """Minúsculas sin tildes, para comparar de forma robusta."""
    sin_tildes = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")


CIUDADES = {
    "medellin": "Medellín",
    "envigado": "Medellín",
    "sabaneta": "Medellín",
    "itagui": "Medellín",
    "bello": "Medellín",
    "la estrella": "Medellín",
    "poblado": "Medellín",
    "laureles": "Medellín",
    "belen": "Medellín",
    "pereira": "Pereira",
    "dosquebradas": "Pereira",
    "cerritos": "Pereira",
    "pinares": "Pereira",
    "alamos": "Pereira",
}

#: Cuáles de esas claves son un **municipio** y no un barrio. La distinción
#: importa: "apartamentos en Pereira" no debe traer Dosquebradas, pero
#: "apartamentos en Laureles" sí es una búsqueda dentro de Medellín y no puede
#: acotarse a un municipio que no existe.
MUNICIPIOS = {
    "medellin": "Medellín",
    "envigado": "Envigado",
    "sabaneta": "Sabaneta",
    "itagui": "Itagüí",
    "bello": "Bello",
    "la estrella": "La Estrella",
    "pereira": "Pereira",
    "dosquebradas": "Dosquebradas",
}

#: Ciudades que el comprador puede nombrar y que hoy NO cubrimos. Reconocerlas es
#: lo que permite responderle con transparencia en vez de seguir preguntando (CU-4).
CIUDADES_FUERA = (
    "bogota", "cali", "barranquilla", "cartagena", "bucaramanga", "santa marta",
    "cucuta", "ibague", "manizales", "armenia", "villavicencio", "neiva", "pasto",
    "monteria", "popayan", "tunja", "sincelejo", "valledupar", "riohacha", "quibdo",
    "florencia", "yopal", "arauca", "leticia", "rionegro", "buenaventura",
    "miami", "madrid", "panama", "mexico",
)

TIPOS = {
    TipoInmueble.APARTAMENTO.value: ("apartamento", "apto", "aptos", "apartaestudio", "apartamentos"),
    TipoInmueble.CASA.value: ("casa", "casas", "casa campestre", "finca"),
    TipoInmueble.LOTE.value: ("lote", "lotes", "terreno", "terrenos", "parcela"),
}

PLAZOS = {
    "inmediato": ("ya", "de una", "inmediato", "esta semana", "urgente", "cuanto antes"),
    "1-3 meses": ("este mes", "proximo mes", "un mes", "dos meses", "tres meses", "1-3 meses"),
    "3-6 meses": ("tres a seis", "3 a 6", "seis meses", "medio ano", "semestre"),
    "6-12 meses": ("fin de ano", "este ano", "un ano", "doce meses"),
    "solo explorando": ("solo mirando", "curioseando", "explorando", "averiguando", "por ahora no"),
}

PIDE_VISITA = (
    "visita", "visitar", "verla", "verlo", "conocerla", "conocerlo", "agendar",
    "cita", "asesor", "hablar con alguien", "me llamen", "llamenme", "contactenme",
    "mi telefono", "mi numero", "whatsapp",
)

_RE_AREA = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:m2|m²|mts2?|metros(?:\s+cuadrados)?)\b")
_RE_HABS = re.compile(
    r"\b(\d{1,2})\s*(?:hab(?:itacion(?:es)?)?|alcobas?|cuartos?|dormitorios?|habs?)\b"
)
_RE_HABS_TEXTO = re.compile(
    r"\b(una|dos|tres|cuatro|cinco)\s*(?:hab(?:itacion(?:es)?)?|alcobas?|cuartos?|dormitorios?)\b"
)
_NUM_PALABRA = {"una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5}

_RE_MILES = re.compile(r"\$?\s*(\d{1,3}(?:\.\d{3})+)(?!\d)")
_RE_MILLONES = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:millones|millon|mills?|mm|m)\b", re.IGNORECASE
)
#: "entre 300 y 450 millones": la unidad va al final y aplica a ambos números.
_RE_RANGO = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:y|a|hasta|-)\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:millones|millon|mills?|mm|m)\b",
    re.IGNORECASE,
)
_RE_ENTERO = re.compile(r"\b(\d{4,})\b")

MONTO_MINIMO_RAZONABLE = 20_000_000


def _a_pesos(valor: float) -> int:
    """Interpreta '450' como 450 millones y '450000000' como sí mismo."""
    if valor < 10_000:
        return int(valor * 1_000_000)
    return int(valor)


def extraer_montos(texto: str) -> list[int]:
    """Devuelve los montos en pesos mencionados, en orden de aparición."""
    limpio = _RE_AREA.sub(" ", normalizar(texto))
    limpio = _RE_HABS.sub(" ", limpio)

    # Un rango explícito ("entre 300 y 450 millones") es inequívoco: resuélvelo
    # primero, porque el primer número no lleva unidad y se perdería.
    if (rango := _RE_RANGO.search(limpio)) is not None:
        return sorted(
            _a_pesos(float(g.replace(",", "."))) for g in rango.groups()
        )

    montos: list[tuple[int, int]] = []  # (posición, valor)

    for m in _RE_MILES.finditer(limpio):
        montos.append((m.start(), int(m.group(1).replace(".", ""))))
    consumido = limpio
    for m in _RE_MILES.finditer(limpio):
        consumido = consumido.replace(m.group(0), " " * len(m.group(0)), 1)

    for m in _RE_MILLONES.finditer(consumido):
        montos.append((m.start(), _a_pesos(float(m.group(1).replace(",", ".")))))
        consumido = consumido[: m.start()] + " " * len(m.group(0)) + consumido[m.end() :]

    for m in _RE_ENTERO.finditer(consumido):
        valor = int(m.group(1))
        if valor >= MONTO_MINIMO_RAZONABLE:
            montos.append((m.start(), valor))

    return [v for _, v in sorted(montos) if v >= 1_000_000]


def extraer_presupuesto(texto: str) -> tuple[int | None, int | None]:
    """Deduce (mínimo, máximo) del presupuesto a partir del lenguaje usado."""
    montos = extraer_montos(texto)
    if not montos:
        return None, None

    plano = normalizar(texto)
    if len(montos) >= 2:
        return min(montos[:2]), max(montos[:2])

    unico = montos[0]
    if any(p in plano for p in ("desde", "minimo", "mas de", "arriba de", "por encima")):
        return unico, None
    # Un solo monto (con o sin "hasta"/"máximo") se lee como tope.
    return None, unico


def extraer_slots(texto: str) -> dict:
    """Extracción determinística sobre un mensaje suelto (sin costo de tokens)."""
    plano = normalizar(texto)
    slots: dict = {}

    for clave, ciudad in CIUDADES.items():
        if re.search(rf"\b{clave}\b", plano):
            slots["ciudad"] = ciudad
            # El municipio es lo que de verdad acota la búsqueda. Nombrar la
            # cabecera ("apartamentos en Pereira") lo fija en la cabecera y así
            # deja fuera Dosquebradas; nombrar un municipio del área lo fija en
            # ese. Sin esto, `ciudad` los mete a todos en el mismo saco y el
            # comprador recibe inmuebles de un municipio por el que no preguntó.
            if clave in MUNICIPIOS:
                slots["municipio"] = MUNICIPIOS[clave]
            if clave not in ("medellin", "pereira"):
                slots["zona"] = clave.capitalize()
            break
    else:
        # Ciudad reconocible pero fuera de cobertura: la registramos tal cual para
        # que la regla dura la detecte y el bot lo diga de frente.
        for fuera in CIUDADES_FUERA:
            if re.search(rf"\b{re.escape(fuera)}\b", plano):
                slots["ciudad"] = fuera.title()
                break

    for tipo, alias in TIPOS.items():
        if any(re.search(rf"\b{re.escape(a)}\b", plano) for a in alias):
            slots["tipo"] = tipo
            break

    # Un presupuesto mencionado en este mensaje reemplaza al anterior por completo:
    # "desde 200 millones" abre el techo que fijó un turno previo, no se suma a él.
    # Por eso se escriben ambos extremos, incluso cuando uno queda en None.
    if pide_sin_tope(texto):
        slots["presupuesto_min"] = None
        slots["presupuesto_max"] = None
    else:
        pmin, pmax = extraer_presupuesto(texto)
        if pmin or pmax:
            slots["presupuesto_min"] = pmin
            slots["presupuesto_max"] = pmax

    if (m := _RE_HABS.search(plano)) is not None:
        slots["habitaciones"] = int(m.group(1))
    elif (m := _RE_HABS_TEXTO.search(plano)) is not None:
        slots["habitaciones"] = _NUM_PALABRA[m.group(1)]

    for plazo, alias in PLAZOS.items():
        if any(a in plano for a in alias):
            slots["plazo_compra"] = plazo
            break

    return slots


def pide_visita(texto: str) -> bool:
    plano = normalizar(texto)
    return any(p in plano for p in PIDE_VISITA)


PIDE_LISTADO = (
    "todos", "todas", "todo lo que", "que tengas", "que tenga", "que tengan",
    "lista completa", "listado completo", "el catalogo", "la cartera",
    "disponibles", "opciones que",
)


def pide_listado_completo(texto: str) -> bool:
    """El comprador pidió ver la cartera, no una terna curada (RF-09)."""
    plano = normalizar(texto)
    return any(p in plano for p in PIDE_LISTADO)


SIN_TOPE = (
    "sin tope", "sin limite", "sin presupuesto", "sin filtro de precio",
    "cualquier precio", "todos los precios", "no importa el precio",
    "sin importar el precio", "quita el presupuesto", "quitame el presupuesto",
    "olvida el presupuesto", "borra el presupuesto",
)


def pide_sin_tope(texto: str) -> bool:
    """El comprador quiere ver la cartera sin el filtro de precio que arrastra."""
    plano = normalizar(texto)
    return any(p in plano for p in SIN_TOPE)


#: Palabras que nunca sirven para señalar un inmueble concreto: conectores,
#: cortesías, el vocabulario del negocio (que ya viaja en los slots) y los
#: municipios (que ya acotan por su cuenta). Los **barrios** no están aquí a
#: propósito: "el de Laureles" tiene que poder recortar la cartera.
_PALABRAS_VACIAS = frozenset(
    """
    para pero como donde cual cuales cuando cuanto cuanta cuantos porque
    tambien sobre entre desde hasta este esta esto estos estas esos esas
    aquel aquella algo algun alguna alguno nada todo toda todos todas
    mucho mucha muchos muy mas menos bien ahora luego aqui alla ademas
    otro otra otros otras cada solo solamente unicamente mismo misma
    quiero quisiera queria puedes podrias podria dime digame decirme
    hablame hablar cuentame contarme muestrame mostrar ensename saber
    conocer tienes tiene tengo tienen haber hacer sirve serviria queda
    quedan estoy estas estan busco buscar buscando necesito interesa
    interesado interesada gracias buenas buenos dias tardes noches hola
    perfecto listo vale valen cuesta cuestan bueno bien favor ayuda
    ayudar mira mire oiga señor senor señora senora amigo claro
    amable autorizo acepto confirmo mejor peor cerca cerquita dentro
    fuera tope limite gustaria encanta
    casa casas apartamento apartamentos apto aptos apartaestudio finca
    lote lotes terreno terrenos parcela inmueble inmuebles propiedad
    propiedades vivienda proyecto precio precios valor costo millones
    millon pesos plata presupuesto metros cuadrados area habitacion
    habitaciones alcoba alcobas cuarto cuartos dormitorio banos bano
    zona zonas barrio barrios sector sectores ciudad municipio
    visita visitar verla verlo conocerla conocerlo agendar cita asesor
    contacto telefono numero whatsapp llamen llamar escribir
    informacion info detalle detalles ficha fichas opcion opciones
    foto fotos imagen imagenes ubicacion direccion mapa
    disponible disponibles catalogo cartera lista listado inventario
    medellin envigado sabaneta itagui bello estrella caldas copacabana
    girardota barbosa pereira dosquebradas virginia santa rosa cabal
    """.split()
)

#: Un término útil tiene al menos cuatro letras: por debajo son artículos y
#: preposiciones ("de", "la", "el") que están en todas las fichas.
_RE_TERMINO = re.compile(r"[a-z0-9][a-z0-9-]{3,}")


def terminos_de_mencion(texto: str) -> tuple[str, ...]:
    """Palabras con las que el comprador puede estar nombrando un inmueble.

    "Háblame solo de la ferretería de La Reforma" deja ("ferreteria", "reforma"):
    lo demás son conectores o vocabulario que ya viaja en los slots. Se descartan
    los números porque un precio o un área no señalan a un inmueble concreto, y
    el código de la ficha sobrevive entero ("prop-per-003") porque a veces el
    comprador copia y pega justo eso.
    """
    plano = normalizar(texto)
    terminos: list[str] = []
    for bruto in _RE_TERMINO.findall(plano):
        termino = bruto.strip("-")
        if len(termino) < 4 or termino.isdigit() or termino in _PALABRAS_VACIAS:
            continue
        if termino not in terminos:
            terminos.append(termino)
    return tuple(terminos)


#: Palabras tras las que un número es una posición en la lista y no una medida.
#: En plural no: "los 6" es pedir los seis, no el sexto.
_SENALADORES = (
    "el", "la", "lote", "casa", "apartamento", "apto", "terreno", "inmueble",
    "propiedad", "ficha", "opcion", "numero", "num", "item", "punto",
)

#: Unidades que devuelven el número a lo que siempre fue: un dato del inmueble.
#: "3 habitaciones" o "hasta 20 millones" no señalan la tercera ni la vigésima.
_UNIDADES = (
    "millones", "millon", "mills", "mill", "hab", "habs", "habitacion",
    "habitaciones", "alcoba", "alcobas", "cuarto", "cuartos", "dormitorio",
    "dormitorios", "bano", "banos", "m2", "mts", "mt2", "metros", "mil",
    "pesos", "piso", "pisos", "ano", "anos", "meses",
)

_RE_INDICE = re.compile(
    r"\b(?:" + "|".join(_SENALADORES) + r")\s*#?\s*(\d{1,2})\b"
)

_ORDINALES = {
    "primero": 1, "primera": 1, "segundo": 2, "segunda": 2,
    "tercero": 3, "tercera": 3, "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6,
    "septimo": 7, "septima": 7, "octavo": 8, "octava": 8,
    "noveno": 9, "novena": 9, "decimo": 10, "decima": 10,
}
#: El artículo es obligatorio: "el cuarto" es una posición, "cuarto" a secas es
#: una habitación.
_RE_ORDINAL = re.compile(r"\b(?:el|la)\s+(" + "|".join(_ORDINALES) + r")\b")

#: Una lista de chat no pasa de unas pocas fichas; por encima de eso el número
#: es cualquier otra cosa.
TOPE_INDICE = 20


def indice_mencionado(texto: str) -> int | None:
    """La posición del listado a la que se refiere el comprador, si la hay.

    "El lote 6", "la opción 2", "el 3" o un "6" a secas son la forma natural de
    señalar una ficha de las que acaba de recibir numeradas. Sin esto el número
    no significaba nada y el bot le devolvía otra vez la lista entera.

    Devuelve None ante cualquier duda: un número mal leído como posición le
    escondería la cartera al comprador por haber dicho "3 habitaciones".
    """
    plano = normalizar(texto)
    solo = plano.strip(" .,;:!¡?¿*-")
    if solo.isdigit():
        return int(solo) if 1 <= int(solo) <= TOPE_INDICE else None

    for m in _RE_INDICE.finditer(plano):
        siguiente = plano[m.end():].lstrip().split(" ")[0].strip(".,;:!?")
        if siguiente in _UNIDADES:
            continue
        posicion = int(m.group(1))
        if 1 <= posicion <= TOPE_INDICE:
            return posicion

    if (m := _RE_ORDINAL.search(plano)) is not None:
        return _ORDINALES[m.group(1)]
    return None


#: Formas en que un comprador cuenta que el negocio ya se hizo. Todas en
#: pasado o con "ya": la diferencia entre "quiero comprar" y "ya compré" es la
#: única que importa aquí, y confundirlas dispararía una alerta de cierre no
#: reportado contra un asesor que no ha hecho nada.
_DICE_QUE_CERRO = (
    "ya compre", "ya lo compre", "ya la compre", "ya compramos", "ya lo compramos",
    "ya cerre", "ya cerramos", "ya lo cerre", "cerre el negocio", "cerramos el negocio",
    "ya firme", "ya firmamos", "ya escriture", "ya escrituramos", "ya hicimos el negocio",
    "hice el negocio", "ya negociamos", "ya lo tome", "ya me quede con",
    "ya es mio", "ya somos duenos", "ya soy dueno", "ya quedo comprado",
    "ya hice la compra", "ya hicimos la compra",
)

#: Formas de decir que no. Se miran primero porque casi todas contienen una
#: forma de cierre dentro ("todavía no he comprado" contiene "comprado").
_DICE_QUE_NO = (
    "todavia no", "aun no", "aun estoy", "todavia estoy", "no he comprado",
    "no compre", "no cerre", "no cerramos", "no hemos cerrado", "no lo compre",
    "sigo buscando", "sigo mirando", "seguimos buscando", "sigo interesado",
    "en eso estoy", "en eso estamos", "no me decidi", "no me he decidido",
    "desisti", "ya no", "no me interesa", "lo deje", "me arrepenti",
)

#: Negaciones que, pegadas a una forma de cierre, la invierten.
_NIEGA = ("no", "nunca", "tampoco")


def respuesta_de_cierre(texto: str) -> str | None:
    """¿El comprador está diciendo que ya cerró el negocio? (PRD §10).

    Devuelve "cerro", "no_cerro" o None cuando el mensaje no habla del asunto.

    Ante la duda, None. Un falso positivo levanta una alerta de venta oculta
    contra un asesor honesto, y esa acusación cuesta más que la venta que se
    quería vigilar.
    """
    plano = normalizar(texto)
    if any(p in plano for p in _DICE_QUE_NO):
        return "no_cerro"
    for patron in _DICE_QUE_CERRO:
        posicion = plano.find(patron)
        if posicion < 0:
            continue
        # "no, todavía no compré" ya salió arriba; esto atrapa lo que quede.
        previo = plano[max(0, posicion - 12) : posicion].split()
        if previo and previo[-1] in _NIEGA:
            return "no_cerro"
        return "cerro"
    return None


def _respuesta_plana(texto: str) -> str:
    """Normaliza una respuesta corta para compararla contra una lista cerrada.

    La puntuación interna se trata como separador, no como vocabulario nuevo:
    "Sí, autorizo" y "sí autorizo" son la misma respuesta. Importa desde que
    WhatsApp es un canal: allí no hay botones fiables, así que el consentimiento
    llega escrito a mano y con la coma que pone cualquiera.

    Las listas siguen siendo de coincidencia exacta a propósito: la autorización
    debe ser inequívoca, y "no sé si autorizo" no puede colarse como un sí.
    """
    plano = normalizar(texto).strip(" .!¡¿?")
    for signo in (",", ";", ":"):
        plano = plano.replace(signo, " ")
    return " ".join(plano.split())


def es_afirmativo(texto: str) -> bool:
    return _respuesta_plana(texto) in {
        "si", "si acepto", "acepto", "claro", "dale", "ok", "listo",
        "de acuerdo", "autorizo", "si autorizo", "sip", "yes", "s",
    }


def es_negativo(texto: str) -> bool:
    return _respuesta_plana(texto) in {
        "no", "no acepto", "nope", "negativo", "no autorizo", "n",
    }


# ─────────────────────────── Reglas duras ───────────────────────────


def fuera_de_alcance(slots: dict, precio_minimo_cartera: int | None = None) -> str | None:
    """Filtros no negociables previos al LLM (SRS §2.2). Devuelve el motivo o None."""
    ciudad = slots.get("ciudad")
    if ciudad and ciudad not in settings.ciudades_cobertura:
        return f"ciudad_no_cubierta:{ciudad}"
    pmax = slots.get("presupuesto_max")
    if pmax and precio_minimo_cartera and pmax < precio_minimo_cartera:
        return f"presupuesto_bajo:{pmax}"
    return None


def score_por_reglas(slots: dict, n_mensajes: int, visita: bool) -> int:
    """Score determinístico 0-100 (RF-06)."""
    score = 0
    if slots.get("ciudad"):
        score += 20
    if slots.get("tipo"):
        score += 15
    if slots.get("presupuesto_max"):
        score += 25
    if slots.get("habitaciones"):
        score += 5
    plazo = slots.get("plazo_compra")
    score += {"inmediato": 25, "1-3 meses": 20, "3-6 meses": 12,
              "6-12 meses": 6, "solo explorando": -10}.get(plazo, 0)
    if visita:
        score += 20
    score += min(n_mensajes, 5)  # engagement real en la conversación
    return max(0, min(100, score))


def etiquetar(score: int) -> Etiqueta:
    if score >= 70:
        return Etiqueta.CALIENTE
    if score >= 40:
        return Etiqueta.TIBIO
    return Etiqueta.FRIO


# ─────────────────────────── Resultado del análisis ───────────────────────────


@dataclass
class Analisis:
    slots: dict = field(default_factory=dict)
    score: int = 0
    etiqueta: Etiqueta = Etiqueta.FRIO
    faltan_datos: list[str] = field(default_factory=list)
    pide_visita: bool = False
    respuesta_sugerida: str = ""
    motivo_fuera_alcance: str | None = None
    fuente: str = "reglas"  # reglas | reglas+llm


CAMPOS_REQUERIDOS = ("ciudad", "tipo", "presupuesto_max")


def tiene_presupuesto(slots: dict) -> bool:
    """Cualquiera de los dos extremos acota la búsqueda: 'desde 200 millones' basta."""
    return bool(slots.get("presupuesto_min") or slots.get("presupuesto_max"))


def faltantes(slots: dict) -> list[str]:
    """Slots que impiden emparejar. El presupuesto cuenta como uno solo."""
    faltan = [c for c in ("ciudad", "tipo") if not slots.get(c)]
    if not tiene_presupuesto(slots):
        faltan.append("presupuesto_max")
    return faltan

_VALIDOS_TIPO = {t.value for t in TipoInmueble}


def _sanear_slots(brutos: dict) -> dict:
    """Descarta lo que el LLM haya alucinado fuera del dominio permitido."""
    limpio: dict = {}
    ciudad = brutos.get("ciudad")
    if ciudad in settings.ciudades_cobertura:
        limpio["ciudad"] = ciudad
    tipo = brutos.get("tipo")
    if tipo in _VALIDOS_TIPO:
        limpio["tipo"] = tipo
    for campo in ("presupuesto_min", "presupuesto_max", "habitaciones"):
        valor = brutos.get(campo)
        if isinstance(valor, (int, float)) and valor > 0:
            limpio[campo] = int(valor)
    for campo in ("zona", "plazo_compra"):
        valor = brutos.get(campo)
        if isinstance(valor, str) and valor.strip():
            limpio[campo] = valor.strip()[:60]
    if (
        limpio.get("presupuesto_min")
        and limpio.get("presupuesto_max")
        and limpio["presupuesto_min"] > limpio["presupuesto_max"]
    ):
        limpio["presupuesto_min"], limpio["presupuesto_max"] = (
            limpio["presupuesto_max"],
            limpio["presupuesto_min"],
        )
    return limpio


def analizar(
    mensaje: str,
    historial: list[dict],
    perfil_actual: dict,
    *,
    precio_minimo_cartera: int | None = None,
    cartera_contexto: str = "",
) -> Analisis:
    """Analiza el turno actual combinando reglas y LLM (ADR-03)."""
    slots = {k: v for k, v in perfil_actual.items() if v not in (None, "", 0)}
    visita = pide_visita(mensaje)
    fuente = "reglas"

    # 1) LLM sobre toda la conversación (cobertura de lenguaje natural).
    score_llm: int | None = None
    sugerida = ""
    if cliente.disponible:
        try:
            datos = cliente.clasificar(historial, slots, cartera_contexto)
            slots.update(_sanear_slots(datos.get("slots") or {}))
            score_llm = max(0, min(100, int(datos.get("score_intencion") or 0)))
            visita = visita or bool(datos.get("pide_visita"))
            sugerida = str(datos.get("respuesta_sugerida") or "")
            fuente = "reglas+llm"
        except Exception as exc:  # degradación deliberada a reglas
            log.warning("Clasificación por LLM no disponible, se usan reglas: %s", exc)

    # 2) Reglas sobre el último mensaje: mandan sobre el LLM porque es el dato
    #    más fresco y su extracción es determinística.
    slots.update(extraer_slots(mensaje))

    motivo = fuera_de_alcance(slots, precio_minimo_cartera)

    score_reglas = score_por_reglas(slots, len(historial), visita)
    score = score_reglas if score_llm is None else round(0.6 * score_reglas + 0.4 * score_llm)
    if motivo:
        score = min(score, 30)

    faltan = faltantes(slots)

    return Analisis(
        slots=slots,
        score=score,
        etiqueta=etiquetar(score),
        faltan_datos=faltan,
        pide_visita=visita,
        respuesta_sugerida=sugerida,
        motivo_fuera_alcance=motivo,
        fuente=fuente,
    )


def pregunta_siguiente(faltan: list[str]) -> str:
    """Pregunta corta y única para completar el dato que falta (RF-08)."""
    return {
        "ciudad": "¿En qué ciudad buscas: Medellín o Pereira? 🏙️",
        "tipo": "¿Buscas *casa*, *apartamento* o *lote*?",
        "presupuesto_max": "¿Hasta qué presupuesto aproximado quieres llegar? 💰",
    }.get(faltan[0] if faltan else "", "Cuéntame un poco más de lo que buscas.")
