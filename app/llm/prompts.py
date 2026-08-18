"""Prompts y contrato JSON del clasificador (SRS §4.4 y §9.3).

Editables sin recompilar (RNF-08): estos son datos, no lógica.
"""

from __future__ import annotations

from app.config import settings

#: Contrato de salida exigido al LLM. Se usa tal cual como JSON Schema con
#: Anthropic y se incrusta en el prompt para el modo JSON de Kimi.
ESQUEMA_CLASIFICADOR: dict = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "object",
            "properties": {
                # Un `enum` junto a un tipo unión (`["string", "null"]`) lo rechaza
                # la validación de salida estructurada de Anthropic: hay que
                # separar el caso nulo en una rama propia con `anyOf`.
                "ciudad": {
                    "anyOf": [
                        {"type": "string", "enum": ["Medellín", "Pereira"]},
                        {"type": "null"},
                    ]
                },
                "zona": {"type": ["string", "null"]},
                "tipo": {
                    "anyOf": [
                        {"type": "string", "enum": ["casa", "apartamento", "lote"]},
                        {"type": "null"},
                    ]
                },
                "presupuesto_min": {"type": ["integer", "null"]},
                "presupuesto_max": {"type": ["integer", "null"]},
                "habitaciones": {"type": ["integer", "null"]},
                "plazo_compra": {"type": ["string", "null"]},
            },
            "required": [
                "ciudad",
                "zona",
                "tipo",
                "presupuesto_min",
                "presupuesto_max",
                "habitaciones",
                "plazo_compra",
            ],
            "additionalProperties": False,
        },
        # El rango 0-100 se pide en el prompt, no en el esquema: la salida
        # estructurada de Anthropic no admite `minimum`/`maximum`. Se acota en
        # `nlu_engine.analizar` antes de mezclarlo con el score por reglas.
        "score_intencion": {"type": "integer"},
        "etiqueta": {"type": "string", "enum": ["frío", "tibio", "caliente"]},
        "faltan_datos": {"type": "array", "items": {"type": "string"}},
        "pide_visita": {"type": "boolean"},
        "respuesta_sugerida": {"type": "string"},
    },
    "required": [
        "slots",
        "score_intencion",
        "etiqueta",
        "faltan_datos",
        "pide_visita",
        "respuesta_sugerida",
    ],
    "additionalProperties": False,
}


#: Contrato de extracción de un inmueble a partir de un aviso en texto libre.
#: `anyOf` en vez de `enum` sobre tipo unión, y sin `minimum`/`maximum`: son las
#: dos restricciones de la salida estructurada de Anthropic.
ESQUEMA_INMUEBLE: dict = {
    "type": "object",
    "properties": {
        "ciudad": {"type": ["string", "null"]},
        "zona": {"type": ["string", "null"]},
        "tipo": {
            "anyOf": [
                {"type": "string", "enum": ["casa", "apartamento", "lote"]},
                {"type": "null"},
            ]
        },
        "precio": {"type": ["integer", "null"]},
        "habitaciones": {"type": ["integer", "null"]},
        "banos": {"type": ["integer", "null"]},
        "area_m2": {"type": ["number", "null"]},
        "descripcion": {"type": ["string", "null"]},
        "propietario": {"type": ["string", "null"]},
        "telefono": {"type": ["string", "null"]},
        "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        "faltantes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "ciudad", "zona", "tipo", "precio", "habitaciones", "banos",
        "area_m2", "descripcion", "propietario", "telefono",
        "confianza", "faltantes",
    ],
    "additionalProperties": False,
}


def sistema_extractor_inmueble() -> str:
    ciudades = " y ".join(settings.ciudades_cobertura)
    return f"""Extraes datos estructurados de avisos inmobiliarios colombianos para {settings.empresa_nombre}.

Recibes el texto de un aviso tal como lo escribió un propietario o una
inmobiliaria y devuelves únicamente los campos que el texto realmente contiene.

REGLA INNEGOCIABLE: no inventes ni completes nada. Si un dato no está en el
texto, devuélvelo como null y anótalo en `faltantes`. Un inmueble con datos
inventados es peor que uno ausente, porque se le mostraría a un comprador como
si fuera real. Es preferible devolver medio aviso vacío que uno adivinado.

Precios en Colombia:
- "350 millones", "350M", "$350.000.000" y "350'000.000" son 350000000.
- "1.200 millones" y "1200 millones" son 1200000000.
- Si el aviso menciona administración, cuota inicial o avalúo, NO los confundas
  con el precio de venta. Si no hay precio de venta claro, devuelve null.
- Devuelve el precio como entero en pesos, sin puntos ni símbolos.

Ubicación:
- `ciudad` es el municipio tal como aparece (Envigado, Dosquebradas, Pereira…).
  No lo traduzcas al área metropolitana: de eso se encarga el sistema.
- `zona` es el barrio o sector.
- Cobertura del negocio: {ciudades} y sus áreas metropolitanas. Si el aviso es
  de otra ciudad, extráelo igual y deja que el sistema lo descarte.

Área: `area_m2` en metros cuadrados. "72 m2", "72mts", "72 metros" son 72.

`confianza` refleja qué tan explícito era el aviso:
- "alta": ciudad, tipo y precio estaban escritos sin ambigüedad.
- "media": alguno se dedujo del contexto.
- "baja": el texto es confuso, incompleto o podría no ser un aviso inmobiliario.

Responde solo el JSON del esquema."""


def usuario_extractor_inmueble(texto: str) -> str:
    return f"AVISO A EXTRAER:\n\n{texto.strip()[:4000]}"


def sistema_clasificador() -> str:
    ciudades = " y ".join(settings.ciudades_cobertura)
    return f"""Eres el asistente inmobiliario con IA de {settings.empresa_nombre}.

TU TRABAJO
Conversar con un comprador de vivienda, extraer sus criterios de búsqueda y
estimar qué tan real es su intención de compra.

REGLAS INNEGOCIABLES
- Responde SIEMPRE en español colombiano natural, cálido y directo. Mensajes
  cortos: máximo 3 frases y una sola pregunta por turno.
- NUNCA inventes propiedades, precios, zonas ni disponibilidad. Solo puedes
  mencionar inmuebles de la cartera que se te entregue explícitamente. Si no
  tienes cartera en el contexto, no menciones inmuebles concretos.
- Solo operamos en {ciudades}. Si el comprador busca en otra ciudad, dilo con
  transparencia y no fuerces el contacto.
- No prometas descuentos, financiación ni fechas que no estén en el contexto.
- Nunca pidas datos que no necesitas para calificar y contactar (nada de
  cédula, salario, datos bancarios ni información de terceros).
- Si el comprador ya dio un dato, no lo vuelvas a preguntar.
- NUNCA le pidas su nombre ni su número de contacto: el canal por el que te
  escribe ya nos los entrega, y el asesor le responde por ese mismo chat.
  Pedírselos hace que crea que el contacto depende de que él conteste algo más.
- Cuando pida visita o asesor, NO hagas ninguna pregunta: el sistema ya cierra
  el turno confirmándole que un asesor humano lo va a contactar.

CÓMO PUNTUAR LA INTENCIÓN (0-100)
- 0-39 "frío": curiosea, sin presupuesto ni plazo, respuestas vagas.
- 40-69 "tibio": tiene criterios claros pero plazo lejano o presupuesto difuso.
- 70-100 "caliente": ciudad + tipo + presupuesto definidos, plazo <= 6 meses,
  o pide explícitamente visita/asesor.

FORMATO DE SALIDA
Devuelve EXCLUSIVAMENTE un objeto JSON válido con las claves
{{slots, score_intencion, etiqueta, faltan_datos, pide_visita, respuesta_sugerida}}.
Sin markdown, sin ``` y sin texto adicional.
- `slots`: usa null en lo que el comprador no haya dicho. Los montos en pesos
  colombianos como enteros (450 millones -> 450000000).
- `faltan_datos`: nombres de los slots que aún faltan para poder emparejar.
- `pide_visita`: true solo si pidió visita, asesor humano o dejar sus datos.
- `respuesta_sugerida`: el mensaje que le enviarías ahora al comprador."""


def usuario_clasificador(historial: list[dict], perfil: dict, cartera: str = "") -> str:
    """Arma el turno de usuario con el historial y lo que ya sabemos."""
    conversacion = "\n".join(
        f"{'Comprador' if m['direccion'] == 'entrante' else 'Asistente'}: {m['texto']}"
        for m in historial[-12:]
    )
    conocido = {k: v for k, v in perfil.items() if v not in (None, "", 0)}
    bloques = [
        "CONVERSACIÓN HASTA AHORA:",
        conversacion or "(sin mensajes previos)",
        "",
        f"DATOS YA CONFIRMADOS DEL COMPRADOR: {conocido or 'ninguno'}",
    ]
    if cartera:
        bloques += [
            "",
            "CARTERA DISPONIBLE (única fuente permitida para mencionar inmuebles):",
            cartera,
        ]
    bloques += ["", "Devuelve el JSON del contrato."]
    return "\n".join(bloques)


def sistema_pitch() -> str:
    return (
        "Eres redactor comercial inmobiliario. Recibes fichas REALES de inmuebles y el "
        "perfil de un comprador. Para cada inmueble escribe UNA frase de venta de máximo "
        "18 palabras, en español colombiano, que conecte un atributo real de la ficha con "
        "lo que busca el comprador. Prohibido inventar datos que no estén en la ficha "
        "(nada de 'cerca al metro' si la ficha no lo dice). Devuelve EXCLUSIVAMENTE un "
        'JSON: {"frases": [{"id": "PROP-XXX-000", "frase": "..."}]} sin markdown.'
    )


#: Plantillas de contacto (SRS §9.2). Se rellenan con `str.format`.
PLANTILLAS = {
    "bienvenida": (
        "{aviso_ia}\n\n"
        "Antes de empezar necesito tu permiso para tratar tus datos según nuestra "
        "política de privacidad: {politica}\n\n"
        "¿Autorizas? Responde *Sí* o *No*."
    ),
    "consentimiento_negado": (
        "Entendido, no guardaré ningún dato tuyo. 🙌 Si más adelante quieres que te "
        "ayude a buscar, escríbeme /start y arrancamos."
    ),
    "calificacion": (
        "¡Listo, gracias! Para mostrarte lo que de verdad te sirve: ¿en qué *ciudad y "
        "zona* buscas, qué *tipo* (casa/apto/lote) y cuál es tu *presupuesto* aproximado?"
    ),
    "fuera_de_alcance": (
        "Por ahora solo manejo propiedades en {ciudades}, así que no quiero hacerte "
        "perder el tiempo. Si te sirve, te aviso apenas ampliemos cobertura."
    ),
    "sin_matches": (
        "Con esos criterios no tengo nada disponible en este momento y prefiero decírtelo "
        "de frente en vez de mostrarte algo que no encaja. ¿Ampliamos presupuesto o zona?"
    ),
    "matches_encabezado": "Con eso en mente, te dejo {n} opción(es) que sí encajan:",
    "matches_item": (
        "{i}. *{zona}, {ciudad}* — {tipo} de {habitaciones} hab · {area:.0f} m²\n"
        "   💰 ${precio}\n"
        "   {frase}"
    ),
    #: Listado completo: ya sabemos ciudad y tipo, así que el encabezado los
    #: nombra y cada ficha se aligera. Con ocho inmuebles, repetir la frase de
    #: venta en cada uno convierte el mensaje en un muro que nadie lee.
    "matches_encabezado_listado": "Tengo {n} {tipos} en {ciudad}:",
    "matches_item_listado": (
        "{i}. *{zona}, {ciudad}* — {tipo} · {habitaciones} hab · {area:.0f} m²\n"
        "   💰 ${precio}"
    ),
    "matches_pie": "¿Quieres agendar una *visita* o hablar con un *asesor*?",
    "handoff": (
        "¡Perfecto! Ya le pasé tus datos a un asesor humano de {empresa}. Te contacta "
        "muy pronto para coordinar. 🙌"
    ),
    #: Cuando ya hay una solicitud en la cola. Repetir el mensaje de arriba haría
    #: creer que se pidió otra vez; callar, que la primera se perdió.
    "handoff_en_cola": (
        "Tu solicitud ya está con el asesor 🙌 Mientras te contacta, sigue "
        "preguntándome lo que quieras de la cartera."
    ),
}
