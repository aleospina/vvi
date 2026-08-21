"""Municipios de Antioquia y Risaralda, y su plaza de cobertura.

Dos conceptos que antes viajaban en el mismo campo y ahora se separan:

· **Municipio** — dónde está el inmueble de verdad. Es lo que el operador
  escribe en la cartera y lo que el comprador reconoce: Sabaneta es Sabaneta,
  no "Medellín". Vive en `Propiedad.ciudad`.

· **Plaza** — el área metropolitana dentro de la cual el negocio opera y en la
  que dos municipios son intercambiables para un comprador (ADR-03). Solo hay
  dos, Medellín y Pereira, y es lo que usa el emparejamiento para decidir qué
  inventario puede ofrecerse a quien dijo "busco en Medellín".

La distinción importa en los dos sentidos. Sin ella, o el operador no puede
registrar dónde queda el inmueble, o el bot deja de ofrecer un apartamento de
Envigado a quien busca en el Valle de Aburrá.

Un municipio fuera de las dos áreas metropolitanas —Urrao, Quinchía, Turbo— se
puede cargar y aparece en la cartera con su nombre, pero `plaza_de` devuelve
None: el bot no lo ofrece, porque nadie lo busca por el canal conversacional.
Es información, no un error, y `Propiedad.fuera_de_plaza` lo hace visible.
"""

from __future__ import annotations

import re
import unicodedata

# ─────────────────────────── Municipios ───────────────────────────
#
# Los 125 de Antioquia y los 14 de Risaralda, por subregión. La lista completa
# existe para que el operador no tenga que forzar un inmueble de Rionegro a
# decir "Medellín": lo que se registra es dónde está.

MUNICIPIOS_ANTIOQUIA: tuple[str, ...] = (
    # Valle de Aburrá
    "Medellín", "Bello", "Itagüí", "Envigado", "Caldas", "La Estrella",
    "Sabaneta", "Copacabana", "Girardota", "Barbosa",
    # Oriente
    "Abejorral", "Alejandría", "Argelia", "Carmen de Viboral", "Cocorná",
    "Concepción", "El Peñol", "El Retiro", "El Santuario", "Granada", "Guarne",
    "Guatapé", "La Ceja", "La Unión", "Marinilla", "Nariño", "Rionegro",
    "San Carlos", "San Francisco", "San Luis", "San Rafael", "San Vicente",
    "Sonsón",
    # Norte
    "Angostura", "Belmira", "Briceño", "Campamento", "Carolina del Príncipe",
    "Don Matías", "Entrerríos", "Gómez Plata", "Guadalupe", "Ituango",
    "San Andrés de Cuerquia", "San José de la Montaña",
    "San Pedro de los Milagros", "Santa Rosa de Osos", "Toledo", "Valdivia",
    "Yarumal",
    # Nordeste
    "Amalfi", "Anorí", "Cisneros", "Remedios", "San Roque", "Santo Domingo",
    "Segovia", "Vegachí", "Yalí", "Yolombó",
    # Bajo Cauca
    "Cáceres", "Caucasia", "El Bagre", "Nechí", "Tarazá", "Zaragoza",
    # Magdalena Medio
    "Caracolí", "Maceo", "Puerto Berrío", "Puerto Nare", "Puerto Triunfo",
    "Yondó",
    # Occidente
    "Abriaquí", "Anzá", "Armenia", "Buriticá", "Caicedo", "Cañasgordas",
    "Dabeiba", "Ebéjico", "Frontino", "Giraldo", "Heliconia", "Liborina",
    "Olaya", "Peque", "Sabanalarga", "San Jerónimo", "Santa Fe de Antioquia",
    "Sopetrán", "Uramita",
    # Suroeste
    "Amagá", "Andes", "Angelópolis", "Betania", "Betulia", "Caramanta",
    "Ciudad Bolívar", "Concordia", "Fredonia", "Hispania", "Jardín", "Jericó",
    "La Pintada", "Montebello", "Pueblorrico", "Salgar", "Santa Bárbara",
    "Támesis", "Tarso", "Titiribí", "Urrao", "Valparaíso", "Venecia",
    # Urabá
    "Apartadó", "Arboletes", "Carepa", "Chigorodó", "Murindó", "Mutatá",
    "Necoclí", "San Juan de Urabá", "San Pedro de Urabá", "Turbo",
    "Vigía del Fuerte",
)

MUNICIPIOS_RISARALDA: tuple[str, ...] = (
    "Pereira", "Dosquebradas", "Santa Rosa de Cabal", "La Virginia",
    "Apía", "Balboa", "Belén de Umbría", "Guática", "La Celia", "Marsella",
    "Mistrató", "Pueblo Rico", "Quinchía", "Santuario",
)

#: Todos, en el orden en que conviene ofrecerlos: primero las dos áreas
#: metropolitanas —que es de donde sale casi toda la cartera— y luego el resto
#: alfabético. Un desplegable que arranca por "Abejorral" hace teclear de más
#: en el 95% de los casos.
def _ordenados() -> tuple[str, ...]:
    metro = [m for p in PLAZAS for m in MUNICIPIOS_POR_PLAZA[p]]
    resto = sorted(
        (m for m in MUNICIPIOS_ANTIOQUIA + MUNICIPIOS_RISARALDA if m not in metro),
        key=lambda m: _plano(m),
    )
    return tuple(metro + resto)


# ─────────────────────────── Plazas ───────────────────────────
#
# Qué municipios componen cada área metropolitana. Es la lista que el motor de
# emparejamiento expande cuando el comprador dice "Medellín": para él, Envigado
# y Sabaneta son la misma búsqueda, y esconderle esos inmuebles sería mentirle
# por un tecnicismo de nomenclatura.

PLAZAS: tuple[str, ...] = ("Medellín", "Pereira")

MUNICIPIOS_POR_PLAZA: dict[str, tuple[str, ...]] = {
    # Valle de Aburrá completo.
    "Medellín": (
        "Medellín", "Bello", "Itagüí", "Envigado", "Caldas", "La Estrella",
        "Sabaneta", "Copacabana", "Girardota", "Barbosa",
    ),
    # Área Metropolitana Centro Occidente y su vecino inmediato.
    "Pereira": ("Pereira", "Dosquebradas", "Santa Rosa de Cabal", "La Virginia"),
}


def _plano(texto: str) -> str:
    """Minúsculas sin tildes ni puntuación: 'Itagüí' e 'itagui' son lo mismo."""
    sin_tildes = unicodedata.normalize("NFD", (texto or "").strip().lower())
    limpio = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", limpio.replace(".", " ").replace("-", " ")).strip()


#: Nombre normalizado → nombre canónico, para los 139.
_CANONICO: dict[str, str] = {
    _plano(m): m for m in MUNICIPIOS_ANTIOQUIA + MUNICIPIOS_RISARALDA
}

#: Cómo los escribe la gente de verdad. "Santa Rosa" a secas es Santa Rosa de
#: Cabal y no Santa Rosa de Osos: la primera está en la plaza de Pereira y la
#: segunda a tres horas, así que la ambigüedad se resuelve hacia donde hay
#: cartera. Si algún día se opera en el Norte de Antioquia, esto se revisa.
_ALIAS: dict[str, str] = {
    "santa rosa": "Santa Rosa de Cabal",
    "virginia": "La Virginia",
    "el carmen de viboral": "Carmen de Viboral",
    "santa fe": "Santa Fe de Antioquia",
    "santafe de antioquia": "Santa Fe de Antioquia",
    "san vicente ferrer": "San Vicente",
    "ciudad bolivar antioquia": "Ciudad Bolívar",
    "penol": "El Peñol",
    "retiro": "El Retiro",
    "santuario risaralda": "Santuario",
}

#: Departamentos y ciudades que delatan que el aviso NO es de la cobertura,
#: aunque el nombre del municipio coincida con uno nuestro. "Armenia, Quindío"
#: y "Santuario, Antioquia" existen; sin este filtro, el primero entraría como
#: el Armenia del Occidente antioqueño.
_FUERA = (
    "quindio", "caldas departamento", "valle del cauca", "cundinamarca",
    "santander", "tolima", "huila", "boyaca", "narino", "cauca", "choco",
    "cordoba", "sucre", "bolivar departamento", "atlantico", "magdalena",
    "cesar", "guajira", "meta", "casanare", "arauca", "putumayo",
    "bogota", "cali", "barranquilla", "cartagena", "bucaramanga", "manizales",
    "ibague", "villavicencio", "neiva", "pasto", "monteria", "popayan",
)

MUNICIPIOS: tuple[str, ...] = _ordenados()


def normalizar_municipio(valor: str) -> str | None:
    """Nombre canónico del municipio, o None si no es de Antioquia/Risaralda.

    Acepta lo que trae una fuente real: "Envigado, Antioquia", "ITAGUI",
    "Santa Rosa". Devuelve el municipio —no la plaza—, porque lo que se guarda
    es dónde está el inmueble.
    """
    plano = _plano(valor)
    if not plano:
        return None
    if plano in _ALIAS:
        return _ALIAS[plano]
    if plano in _CANONICO:
        return _CANONICO[plano]

    # "Armenia, Quindío" no es el Armenia de Antioquia. Se descarta antes de
    # buscar por partes, o el nombre suelto se lleva la coincidencia.
    if any(re.search(rf"\b{re.escape(f)}\b", plano) for f in _FUERA):
        return None

    # "Envigado, Antioquia" o "Municipio de La Ceja": se busca el nombre más
    # largo que aparezca completo, para que "La Unión" gane sobre "Unión".
    for clave in sorted(_CANONICO, key=len, reverse=True):
        if re.search(rf"\b{re.escape(clave)}\b", plano):
            return _CANONICO[clave]
    for clave in sorted(_ALIAS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(clave)}\b", plano):
            return _ALIAS[clave]
    return None


def plaza_de(valor: str) -> str | None:
    """Plaza de cobertura del municipio, o None si está fuera de las dos áreas.

    None no significa "dato inválido": significa que el inmueble se guarda y se
    ve en la cartera, pero el bot no lo ofrece porque nadie lo busca por ahí.
    """
    municipio = normalizar_municipio(valor)
    if municipio is None:
        return None
    for plaza, municipios in MUNICIPIOS_POR_PLAZA.items():
        if municipio in municipios:
            return plaza
    return None


def municipios_de_plaza(plaza: str | None) -> tuple[str, ...]:
    """Municipios que componen la plaza; vacío si no es una plaza conocida.

    Es lo que el emparejamiento expande: quien pide "Medellín" tiene que ver
    también Envigado y Sabaneta, que para él son el mismo mercado.
    """
    if not plaza:
        return ()
    return MUNICIPIOS_POR_PLAZA.get(plaza, (plaza,) if plaza in _CANONICO.values() else ())
