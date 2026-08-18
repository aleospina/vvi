"""Gestión de la cartera de propiedades (SRS §2.4 · RF-10)."""

from __future__ import annotations

import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EstadoPropiedad, FuentePropiedad, Propiedad
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
    """Piso de precio de la cartera; alimenta la regla dura de presupuesto."""
    consulta = select(func.min(Propiedad.precio)).where(Propiedad.estado == "disponible")
    if ciudad:
        consulta = consulta.where(Propiedad.ciudad == ciudad)
    return db.scalar(consulta)


def siguiente_codigo(db: Session, ciudad: str) -> str:
    prefijo = f"PROP-{'MED' if ciudad == 'Medellín' else 'PER'}"
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


def conteo_por_municipio(db: Session) -> list[tuple[str, str, int]]:
    """(ciudad, municipio, cuántos), ordenado por ciudad y luego por municipio.

    Solo aparecen municipios con inventario: una pestaña que siempre da cero es
    ruido que el operador aprende a ignorar.
    """
    conteo: dict[tuple[str, str], int] = {}
    for p in db.scalars(select(Propiedad)):
        clave = (p.ciudad, municipio_de(p))
        conteo[clave] = conteo.get(clave, 0) + 1
    return sorted(
        ((ciudad, municipio, n) for (ciudad, municipio), n in conteo.items()),
        key=lambda f: (f[0], f[1] != f[0], f[1]),  # la ciudad de cabeza, luego alfabético
    )


def conteo_por_tipo(db: Session) -> dict[str, int]:
    """Cuántos inmuebles hay de cada tipo en toda la cartera.

    Alimenta las pestañas del filtro del dashboard. Va sobre la cartera
    completa a propósito: si contara solo lo filtrado, las pestañas inactivas
    dirían cero y el filtro parecería vacío.
    """
    filas = db.execute(
        select(Propiedad.tipo, func.count()).group_by(Propiedad.tipo)
    ).all()
    return {tipo: n for tipo, n in filas}
