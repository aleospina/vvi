"""Gestión de la cartera de propiedades (SRS §2.4 · RF-10)."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    FUENTES_SIN_MANDATO,
    NEGOCIO_POR_DEFECTO,
    EstadoPropiedad,
    FuentePropiedad,
    Propiedad,
    TipoInmueble,
    ahora,
)
from app.services.geografia import municipios_de_plaza, plaza_de
from app.services.compliance import auditar


def listar(
    db: Session,
    *,
    ciudad: str | None = None,
    tipo: str | None = None,
    municipio: str | None = None,
    solo_disponibles: bool = False,
) -> list[Propiedad]:
    consulta = select(Propiedad)
    if ciudad:
        consulta = consulta.where(Propiedad.ciudad == ciudad)
    if tipo:
        consulta = consulta.where(Propiedad.tipo == tipo)
    if solo_disponibles:
        consulta = consulta.where(Propiedad.estado == "disponible")
    filas = list(db.scalars(consulta.order_by(Propiedad.ciudad, Propiedad.precio)))

    # El municipio se deduce del texto de la zona, así que se filtra en Python.
    # La cartera de una inmobiliaria de barrio cabe holgadamente en memoria; el
    # día que no quepa, esto se convierte en una columna y un índice.
    if municipio:
        objetivo = plano(municipio)
        filas = [p for p in filas if plano(municipio_de(p)) == objetivo]
    return filas


def obtener(db: Session, propiedad_id: str) -> Propiedad | None:
    return db.get(Propiedad, propiedad_id)


def precio_minimo(
    db: Session, ciudad: str | None = None, negocio: str | None = None
) -> int | None:
    """Piso de precio de la cartera; alimenta la regla dura de presupuesto.

    Se acota SIEMPRE a un tipo de negocio, con venta por defecto. Sin eso, quien
    busca arriendo con dos millones al mes se compararía contra el inmueble en
    venta más barato de la cartera y la regla dura lo declararía «presupuesto
    bajo»: el bot despacharía por pobre a un cliente perfectamente solvente.

    `ciudad` puede ser una plaza ("Medellín") o un municipio suelto: si es plaza
    se expande a los suyos, o el piso saldría de un universo más pequeño que el
    que el emparejamiento va a ofrecer y el bot descartaría compradores que sí
    alcanzaban algo.
    """
    consulta = select(func.min(Propiedad.precio)).where(
        Propiedad.estado == EstadoPropiedad.DISPONIBLE.value,
        Propiedad.negocio == (negocio or NEGOCIO_POR_DEFECTO),
    )
    if ciudad:
        consulta = consulta.where(Propiedad.ciudad.in_(municipios_de_plaza(ciudad) or (ciudad,)))
    return db.scalar(consulta)


def siguiente_codigo(db: Session, ciudad: str) -> str:
    """Código correlativo por plaza, no por municipio.

    Un inmueble de Sabaneta sigue siendo PROP-MED-xxx: la numeración agrupa por
    plaza, y abrir un prefijo por municipio fragmentaría la serie en decenas de
    contadores casi vacíos.
    """
    prefijo = f"PROP-{'MED' if plaza_de(ciudad) != 'Pereira' else 'PER'}"
    total = db.scalar(
        select(func.count()).select_from(Propiedad).where(Propiedad.id.like(f"{prefijo}%"))
    )
    return f"{prefijo}-{(total or 0) + 1:03d}"


def crear(db: Session, datos: dict, actor: str = "operador") -> Propiedad:
    propiedad = Propiedad(
        id=datos.get("id") or siguiente_codigo(db, datos["ciudad"]),
        ciudad=datos["ciudad"],
        zona=datos.get("zona", ""),
        tipo=datos["tipo"],
        negocio=datos.get("negocio") or NEGOCIO_POR_DEFECTO,
        habitaciones=int(datos.get("habitaciones") or 0),
        banos=int(datos.get("banos") or 0),
        area_m2=float(datos.get("area_m2") or 0),
        precio=int(datos["precio"]),
        estado=datos.get("estado", EstadoPropiedad.DISPONIBLE.value),
        descripcion=datos.get("descripcion", ""),
        foto_url=datos.get("foto_url") or "/static/img/placeholder.svg",
        propietario=datos.get("propietario", ""),
        fuente=datos.get("fuente", FuentePropiedad.MANUAL.value),
        # La carga manual la hace un operador identificado: su acto es la
        # evidencia del mandato, igual que en la ingesta automática lo es la
        # casilla del propietario.
        mandato=True,
        mandato_evidencia=datos.get("mandato_evidencia") or f"Carga manual por {actor}",
    )
    db.add(propiedad)
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="propiedad_creada",
        entidad="propiedad",
        entidad_id=propiedad.id,
        detalle=(
            f"{propiedad.tipo} en {propiedad.negocio} en {propiedad.zona}, "
            f"{propiedad.ciudad} por ${propiedad.precio:,}"
        ),
    )
    return propiedad


def actualizar(db: Session, propiedad: Propiedad, cambios: dict, actor: str = "operador") -> Propiedad:
    aplicados = []
    for campo, valor in cambios.items():
        if hasattr(propiedad, campo) and campo != "id" and valor is not None:
            setattr(propiedad, campo, valor)
            aplicados.append(campo)
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="propiedad_actualizada",
        entidad="propiedad",
        entidad_id=propiedad.id,
        detalle=f"campos: {', '.join(aplicados)}",
    )
    return propiedad


def inactivar(db: Session, propiedad: Propiedad, actor: str = "operador") -> Propiedad:
    propiedad.estado = "inactiva"
    db.flush()
    auditar(
        db,
        actor=actor,
        accion="propiedad_inactivada",
        entidad="propiedad",
        entidad_id=propiedad.id,
    )
    return propiedad


def como_dict(p: Propiedad) -> dict:
    return {
        "id": p.id,
        "ciudad": p.ciudad,
        "zona": p.zona,
        "tipo": p.tipo,
        # Va al LLM que redacta la frase de venta: sin esto ofrecería «tuya por
        # $2.500.000» sobre un arriendo.
        "negocio": p.negocio,
        "habitaciones": p.habitaciones,
        "banos": p.banos,
        "area_m2": p.area_m2,
        "precio": p.precio,
        "descripcion": p.descripcion,
    }


# ─────────────────────────── Municipios del área ───────────────────────────
#
# `ciudad` solo distingue Medellín de Pereira: son las dos plazas de cobertura
# (ADR-03). El municipio real vive dentro de `zona`, con la convención
# "Barrio, Municipio" cuando no coincide con la ciudad —"Frailes, Dosquebradas",
# "Zúñiga, Envigado"—. Un comprador no pregunta por "Medellín" cuando quiere
# Sabaneta, así que el filtro del operador tiene que hablar su mismo idioma.

#: Municipios del área metropolitana que a veces se escriben sin coma, como si
#: fueran un barrio ("La Estrella"). Sin esta lista quedarían absorbidos por su
#: ciudad y no aparecerían como opción propia.
MUNICIPIOS_AREA = {
    "sabaneta", "envigado", "itagui", "bello", "la estrella", "caldas",
    "copacabana", "girardota", "barbosa", "dosquebradas", "la virginia",
    "santa rosa de cabal",
}


def plano(texto: str) -> str:
    """Minúsculas sin tildes: 'Itagüí' y 'itagui' deben ser el mismo municipio."""
    sin_tildes = unicodedata.normalize("NFD", (texto or "").strip().lower())
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")


def municipio_de(propiedad: Propiedad) -> str:
    """Municipio real del inmueble, deducido de la zona.

    Devuelve la ciudad cuando la zona es un barrio de la propia ciudad.
    """
    zona = (propiedad.zona or "").strip()
    if "," in zona:
        return zona.rsplit(",", 1)[1].strip() or propiedad.ciudad
    if plano(zona) in MUNICIPIOS_AREA:
        return zona
    return propiedad.ciudad


def conteo_por_municipio(db: Session, *, tipo: str | None = None) -> list[tuple[str, str, int]]:
    """(ciudad, municipio, cuántos), ordenado por ciudad y luego por municipio.

    Qué municipios aparecen lo decide la cartera completa; `tipo` solo cambia
    el número. Un municipio sin lotes muestra «0» en lugar de desaparecer al
    marcar «Lotes»: una opción que se esfuma deja al operador sin saber si el
    filtro se aplicó o si esa opción nunca existió.

    El filtro de municipio no se aplica aquí a propósito —cada pestaña cuenta
    lo suyo, no lo ya filtrado—; si no, las inactivas dirían siempre cero.
    """
    objetivo = plano(tipo) if tipo else ""
    conteo: dict[tuple[str, str], int] = {}
    for p in db.scalars(select(Propiedad)):
        # Se agrupa por plaza y no por `ciudad`: desde que `ciudad` guarda el
        # municipio, agrupar por ella pondría a cada municipio en su propio
        # grupo y la fila de pestañas perdería el orden por área metropolitana.
        clave = (plaza_de(p.ciudad) or p.ciudad, municipio_de(p))
        cuenta = not objetivo or plano(p.tipo) == objetivo
        # La clave se crea aunque el inmueble no cuente para el tipo elegido:
        # el municipio sigue siendo una opción del filtro, solo que en cero.
        conteo[clave] = conteo.get(clave, 0) + (1 if cuenta else 0)
    return sorted(
        ((ciudad, municipio, n) for (ciudad, municipio), n in conteo.items()),
        key=lambda f: (f[0], f[1] != f[0], f[1]),  # la ciudad de cabeza, luego alfabético
    )


def conteo_por_tipo(db: Session, *, municipio: str | None = None) -> dict[str, int]:
    """Cuántos inmuebles hay de cada tipo, acotado al municipio elegido.

    Siempre devuelve los tres tipos: uno sin inventario muestra «0» en vez de
    perder su pestaña, por el mismo motivo que en `conteo_por_municipio`.

    El filtro de tipo no se aplica aquí, también a propósito: si contara solo
    lo ya filtrado, las pestañas inactivas dirían cero y el filtro parecería
    vacío. Un tipo fuera del enum no tiene pestaña pero sí suma en el total,
    que es lo que la vista usa para «Todos».
    """
    conteo: dict[str, int] = {t.value: 0 for t in TipoInmueble}
    for p in listar(db, municipio=municipio):
        conteo[p.tipo] = conteo.get(p.tipo, 0) + 1
    return conteo

# ═══════════════════════════ Catálogo público ═══════════════════════════
#
# Todo lo que sigue alimenta la vitrina de /inmuebles, la única superficie que
# ve alguien sin sesión. La regla que la gobierna está en `es_publicable` y en
# ningún otro sitio: cualquier consulta pública nueva debe pasar por ahí, para
# que publicar un inmueble que no se puede vender exija cambiar esa función y
# no simplemente olvidar un WHERE.

#: Ordenamientos que acepta la vitrina. Son los que ofrece la competencia local,
#: que es lo que el comprador ya sabe usar.
ORDENES = ("nuevo", "precio_asc", "precio_desc")


def es_publicable(propiedad: Propiedad, *, incluir_demo: bool = False) -> bool:
    """¿Puede este inmueble aparecer ante un desconocido?

    Dos condiciones, y las dos son de negocio antes que de presentación:

    · `disponible` — lo pendiente de validación todavía no lo ha mirado un
      humano, y lo inactivo o vendido ya no está en el mercado.
    · con mandato — un inmueble de `FUENTES_SIN_MANDATO` es un aviso ajeno o un
      registro inventado. Mostrarlo es ofrecer algo que no podemos vender.

    `incluir_demo` es la única puerta para lo segundo y existe para poder probar
    en local, donde la cartera entera es de demostración. Quien la abre asume
    que la vitrina queda `noindex` y con aviso a la vista.
    """
    if propiedad.estado != EstadoPropiedad.DISPONIBLE.value:
        return False
    if propiedad.fuente in FUENTES_SIN_MANDATO:
        return incluir_demo
    return True


def publicables(db: Session, *, incluir_demo: bool = False) -> list[Propiedad]:
    """Cartera visible al público, sin filtrar por criterios de búsqueda."""
    consulta = select(Propiedad).where(Propiedad.estado == EstadoPropiedad.DISPONIBLE.value)
    if not incluir_demo:
        consulta = consulta.where(Propiedad.fuente.not_in(tuple(FUENTES_SIN_MANDATO)))
    # Se vuelve a pasar por `es_publicable` aunque el WHERE ya filtre: así no
    # existen dos definiciones distintas de lo mismo que puedan separarse.
    return [p for p in db.scalars(consulta) if es_publicable(p, incluir_demo=incluir_demo)]


def _coincide_texto(propiedad: Propiedad, buscado: str) -> bool:
    """Búsqueda libre sobre código, zona, ciudad, tipo y descripción.

    Se compara sin tildes porque el comprador escribe "itagui" y "dosquebradas"
    tanto como la forma correcta.
    """
    heno = plano(
        " ".join(
            (
                propiedad.id,
                propiedad.zona or "",
                propiedad.ciudad or "",
                propiedad.tipo or "",
                propiedad.descripcion or "",
            )
        )
    )
    return all(termino in heno for termino in buscado.split())


def _clave_orden(orden: str):
    if orden == "precio_asc":
        return lambda p: (p.precio, p.id)
    if orden == "precio_desc":
        return lambda p: (-p.precio, p.id)
    # "nuevo": lo último que entró va primero. `creada_en` empata en una carga
    # masiva, así que el id desempata y el orden queda estable entre páginas.
    return lambda p: (-(p.creada_en or ahora()).timestamp(), p.id)


def buscar_publicas(
    db: Session,
    *,
    tipo: str | None = None,
    negocio: str | None = None,
    municipio: str | None = None,
    precio_min: int | None = None,
    precio_max: int | None = None,
    habitaciones: int | None = None,
    banos: int | None = None,
    texto: str | None = None,
    orden: str = "nuevo",
    incluir_demo: bool = False,
) -> list[Propiedad]:
    """Vitrina filtrada y ordenada. `habitaciones` y `banos` son mínimos.

    Un rango de precio sin `negocio` mezcla escalas —cánones mensuales junto a
    precios de venta— y devuelve una lista que no significa nada. La vitrina
    resuelve eso fijando el negocio antes de ofrecer el rango; aquí se deja
    pasar porque la firma también sirve a búsquedas sin precio.

    Los filtros se aplican en Python y no en SQL a propósito: el municipio se
    deduce del texto de la zona y la búsqueda libre ignora tildes, así que la
    mitad de los criterios no son expresables en un WHERE de SQLite. La cartera
    de una inmobiliaria de barrio cabe holgadamente en memoria; el día que no
    quepa, `municipio` pasa a columna y esto se parte en dos.
    """
    filas = publicables(db, incluir_demo=incluir_demo)

    if tipo:
        filas = [p for p in filas if p.tipo == tipo]
    if negocio:
        filas = [p for p in filas if (p.negocio or NEGOCIO_POR_DEFECTO) == negocio]
    if precio_min is not None:
        filas = [p for p in filas if p.precio >= precio_min]
    if precio_max is not None:
        filas = [p for p in filas if p.precio <= precio_max]
    if habitaciones:
        filas = [p for p in filas if (p.habitaciones or 0) >= habitaciones]
    if banos:
        filas = [p for p in filas if (p.banos or 0) >= banos]
    if municipio:
        objetivo = plano(municipio)
        filas = [p for p in filas if plano(municipio_de(p)) == objetivo]
    if texto and texto.strip():
        buscado = plano(texto)
        filas = [p for p in filas if _coincide_texto(p, buscado)]

    return sorted(filas, key=_clave_orden(orden if orden in ORDENES else "nuevo"))


def publicada(db: Session, propiedad_id: str, *, incluir_demo: bool = False) -> Propiedad | None:
    """Un inmueble por código, solo si le corresponde estar en la vitrina.

    Devuelve `None` —y la ruta responde 404— cuando existe pero no es
    publicable: para un desconocido, un inmueble sin mandato no existe. Un 403
    delataría que el código es válido y con eso se puede enumerar la cartera.
    """
    propiedad = db.get(Propiedad, propiedad_id)
    if propiedad is None or not es_publicable(propiedad, incluir_demo=incluir_demo):
        return None
    return propiedad


def similares(
    db: Session, propiedad: Propiedad, *, limite: int = 3, incluir_demo: bool = False
) -> list[Propiedad]:
    """Otros inmuebles que le pueden servir a quien está viendo este.

    Prioriza mismo municipio y mismo tipo, y desempata por cercanía de precio:
    quien mira una casa de 400 millones no está buscando una de 2.000.
    """
    municipio = plano(municipio_de(propiedad))
    negocio = propiedad.negocio or NEGOCIO_POR_DEFECTO
    candidatos = [
        p
        for p in publicables(db, incluir_demo=incluir_demo)
        # El negocio filtra, no puntúa: a quien mira un arriendo no se le
        # ofrece «también te puede servir» una casa de 900 millones en venta.
        if p.id != propiedad.id and (p.negocio or NEGOCIO_POR_DEFECTO) == negocio
    ]

    def afinidad(p: Propiedad) -> tuple:
        return (
            plano(municipio_de(p)) != municipio,   # primero el mismo municipio
            p.tipo != propiedad.tipo,              # luego el mismo tipo
            abs(p.precio - propiedad.precio),      # y el precio más parecido
        )

    return sorted(candidatos, key=afinidad)[:limite]


# Las tres facetas siguen la misma regla, la que ya usa el panel del operador:
# cada fila se cuenta con los **otros** filtros aplicados, nunca con el suyo.
# Contar con el propio filtro puesto dejaría todas las opciones inactivas en
# cero y la fila parecería rota; no contar los otros promete inventario que al
# pulsar no aparece —«Apartamentos 25» que al abrirlo son 9 porque el resto son
# arriendos—. Y una opción en cero se muestra apagada en vez de esconderse: que
# diga «0» es una respuesta, que desaparezca parece que el filtro se rompió.


def _encaja(propiedad: Propiedad, *, negocio=None, tipo=None, municipio=None) -> bool:
    """¿Cumple este inmueble los filtros indicados? Los omitidos no filtran."""
    if negocio and (propiedad.negocio or NEGOCIO_POR_DEFECTO) != negocio:
        return False
    if tipo and propiedad.tipo != tipo:
        return False
    if municipio and plano(municipio_de(propiedad)) != plano(municipio):
        return False
    return True


def conteo_publico_por_negocio(
    db: Session,
    *,
    tipo: str | None = None,
    municipio: str | None = None,
    incluir_demo: bool = False,
) -> dict[str, int]:
    """Cuántos hay en venta, en arriendo y en permuta, según tipo y municipio.

    A diferencia de las otras dos, aquí solo aparecen los negocios que la
    cartera tiene: una pestaña «En arriendo» sin un solo arriendo en todo el
    inventario no es información, es una promesa. De eso depende además que la
    vitrina pueda fijar el negocio cuando solo existe uno.
    """
    conteo: dict[str, int] = {}
    for p in publicables(db, incluir_demo=incluir_demo):
        clave = p.negocio or NEGOCIO_POR_DEFECTO
        conteo.setdefault(clave, 0)
        if _encaja(p, tipo=tipo, municipio=municipio):
            conteo[clave] += 1
    return conteo


def conteo_publico_por_tipo(
    db: Session,
    *,
    negocio: str | None = None,
    municipio: str | None = None,
    incluir_demo: bool = False,
) -> dict[str, int]:
    """Cuántos hay de cada tipo, acotado al negocio y municipio elegidos.

    Siempre devuelve los tres: uno sin inventario muestra «0» en vez de perder
    su pestaña.
    """
    conteo: dict[str, int] = {t.value: 0 for t in TipoInmueble}
    for p in publicables(db, incluir_demo=incluir_demo):
        if _encaja(p, negocio=negocio, municipio=municipio):
            conteo[p.tipo] = conteo.get(p.tipo, 0) + 1
    return conteo


def conteo_publico_por_municipio(
    db: Session,
    *,
    negocio: str | None = None,
    tipo: str | None = None,
    incluir_demo: bool = False,
) -> list[tuple[str, str, int]]:
    """(plaza, municipio, cuántos), con la cabecera de plaza de primera.

    Se agrupa por plaza y no por `ciudad`: desde que `ciudad` guarda el
    municipio, agrupar por ella pondría a cada municipio en su propio grupo y la
    fila perdería el orden por área metropolitana. Un municipio fuera de las dos
    plazas se agrupa bajo sí mismo, que es lo único honesto que puede decirse.

    Qué municipios aparecen lo decide la cartera publicable completa; el negocio
    y el tipo solo cambian el número. Si un municipio desapareciera al elegir
    «En arriendo», el visitante perdería la opción y el camino de vuelta.
    """
    conteo: dict[tuple[str, str], int] = {}
    for p in publicables(db, incluir_demo=incluir_demo):
        clave = (plaza_de(p.ciudad) or p.ciudad, municipio_de(p))
        conteo.setdefault(clave, 0)
        if _encaja(p, negocio=negocio, tipo=tipo):
            conteo[clave] += 1
    return sorted(
        ((ciudad, municipio, n) for (ciudad, municipio), n in conteo.items()),
        key=lambda f: (f[0], f[1] != f[0], f[1]),
    )


# ─────────────────────────── URLs legibles ───────────────────────────


def babosa(texto: str) -> str:
    """Fragmento de URL a partir de texto libre: 'Zúñiga, Envigado' -> 'zuniga-envigado'."""
    return re.sub(r"[^a-z0-9]+", "-", plano(texto)).strip("-")


def slug_de(propiedad: Propiedad) -> str:
    """Parte legible de la URL pública: `apartamento-pinares-pereira`.

    Es decorativa —el código es lo que identifica— pero es lo que lee un humano
    en el enlace que el bot le manda por WhatsApp, y lo que indexa un buscador.

    El negocio va en la URL —`casa-arriendo-pinares-pereira`— porque es lo
    primero que necesita saber quien recibe el enlace: el mismo inmueble puede
    estar en venta y en arriendo, y el precio no se entiende sin ese dato.

    Las palabras repetidas se descartan: la zona ya trae el municipio dentro
    cuando sigue la convención "Barrio, Municipio", y sin esto quedarían URLs
    como `apartamento-frailes-dosquebradas-dosquebradas`.
    """
    partes: list[str] = []
    for crudo in (
        propiedad.tipo,
        propiedad.negocio or NEGOCIO_POR_DEFECTO,
        propiedad.zona or "",
        municipio_de(propiedad),
    ):
        for palabra in babosa(crudo).split("-"):
            if palabra and palabra not in partes:
                partes.append(palabra)
    return "-".join(partes) or "inmueble"


def ruta_publica(propiedad: Propiedad) -> str:
    """Ruta canónica de la ficha pública. Único sitio donde se arma esta URL."""
    return f"/inmuebles/{slug_de(propiedad)}/{propiedad.id}"


def fuera_de_vitrina(db: Session) -> list[Propiedad]:
    """Inmuebles que ya no se publican: inactivos, rechazados y vendidos.

    Existen porque la Cartera dejó de listar inventario —eso lo hace la vitrina—
    y sin esta consulta quedarían inalcanzables: no salen en /inmuebles, no
    están en la cola de pendientes y nadie recuerda un código de memoria.
    """
    retirados = (
        EstadoPropiedad.INACTIVA.value,
        EstadoPropiedad.VENDIDA.value,
        EstadoPropiedad.RECHAZADA.value,
    )
    return list(
        db.scalars(
            select(Propiedad)
            .where(Propiedad.estado.in_(retirados))
            .order_by(Propiedad.actualizada_en.desc())
        )
    )
