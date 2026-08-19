"""Gestión de la cartera de propiedades (SRS §2.4 · RF-10)."""

from __future__ import annotations

import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EstadoPropiedad, FuentePropiedad, Propiedad, TipoInmueble
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


def precio_minimo(db: Session, ciudad: str | None = None) -> int | None:
    """Piso de precio de la cartera; alimenta la regla dura de presupuesto.

    `ciudad` puede ser una plaza ("Medellín") o un municipio suelto: si es plaza
    se expande a los suyos, o el piso saldría de un universo más pequeño que el
    que el emparejamiento va a ofrecer y el bot descartaría compradores que sí
    alcanzaban algo.
    """
    consulta = select(func.min(Propiedad.precio)).where(Propiedad.estado == "disponible")
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
        detalle=f"{propiedad.tipo} en {propiedad.zona}, {propiedad.ciudad} por ${propiedad.precio:,}",
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
