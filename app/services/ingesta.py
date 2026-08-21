"""Ingesta automática de inmuebles hacia la cartera (RF-10, ADR-01).

Qué hace este módulo
--------------------
Normaliza publicaciones que llegan de cualquier origen a un único formato, las
valida, deduplica y las deja en la cartera **pendientes de validación** para que
un operador humano las apruebe antes de que un comprador las vea.

Añadir una fuente nueva es escribir un adaptador que devuelva `Publicacion`; el
núcleo (validación, dedupe, auditoría, estados) no se toca.

Qué NO hace, por diseño
-----------------------
No raspa Facebook, Marketplace, Instagram ni OLX. Los ToS de esas plataformas
prohíben la automatización no autorizada (ADR-01), no exponen API de lectura de
avisos ajenos, y —lo decisivo para el negocio— un aviso raspado no viene con
mandato: no se puede cobrar comisión sobre un inmueble que no representamos.

Las fuentes admitidas son las que traen mandato o convenio:
  · `captacion_propietario` — el dueño publica y autoriza (implementada aquí).
  · `feed_aliado`           — inmobiliaria con convenio (XML/CSV).
  · `mercado_libre`         — API oficial con OAuth.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.client import cliente
from app.models import (
    FUENTES_SIN_MANDATO,
    NEGOCIO_POR_DEFECTO,
    Emparejamiento,
    EstadoPropiedad,
    FuentePropiedad,
    Propiedad,
    Solicitud,
    TipoInmueble,
    TipoNegocio,
    Venta,
)
from app.security.crypto import indice_ciego
from app.services.compliance import auditar
from app.services import geografia
from app.services.nlu_engine import CIUDADES
from app.services.portfolio import siguiente_codigo

log = logging.getLogger(__name__)

#: Un inmueble por debajo de esto casi siempre es un error de digitación (un
#: precio en miles, o el valor de la administración en vez del de venta).
PRECIO_MINIMO = 20_000_000
PRECIO_MAXIMO = 50_000_000_000

#: Rango razonable del importe según el negocio. El de venta es el histórico y
#: no cambia; el de arriendo existe porque un canon de 900.000 es perfectamente
#: normal y el rango de venta lo rechazaría como «fuera de rango», dejando la
#: cartera de arriendos vacía sin decir por qué.
RANGO_PRECIO = {
    TipoNegocio.VENTA.value: (PRECIO_MINIMO, PRECIO_MAXIMO),
    TipoNegocio.PERMUTA.value: (PRECIO_MINIMO, PRECIO_MAXIMO),
    TipoNegocio.ARRIENDO.value: (300_000, 200_000_000),
}


def rango_precio(negocio: str | None) -> tuple[int, int]:
    """(mínimo, máximo) admisible del importe para ese negocio."""
    return RANGO_PRECIO.get(negocio or NEGOCIO_POR_DEFECTO, (PRECIO_MINIMO, PRECIO_MAXIMO))


def precio_razonable(precio: int, negocio: str | None) -> bool:
    minimo, maximo = rango_precio(negocio)
    return minimo <= precio <= maximo


class MandatoAusente(PermissionError):
    """Se intentó ingerir un inmueble sin autorización para comercializarlo.

    Es el equivalente, del lado de la oferta, a `ConsentimientoAusente` en la
    captación de prospectos: un único punto de entrada que no se puede rodear.
    """


@dataclass
class Publicacion:
    """Inmueble normalizado, común a toda fuente de ingesta."""

    fuente: str
    externo_id: str
    ciudad: str
    tipo: str
    precio: int
    #: venta | arriendo | permuta. Vacío = venta, ver `normalizar_negocio`.
    negocio: str = NEGOCIO_POR_DEFECTO
    zona: str = ""
    habitaciones: int = 0
    banos: int = 0
    area_m2: float = 0.0
    descripcion: str = ""
    foto_url: str = ""
    url_origen: str = ""
    propietario: str = ""
    propietario_telefono: str | None = None
    mandato: bool = False
    mandato_evidencia: str = ""
    #: La escribió una persona campo por campo, con el inmueble delante, y no un
    #: raspador ni el extractor de avisos. Solo la marca `publicacion_de_formulario`.
    #: A quien la marca no se le acota el precio ni se le exige área metropolitana:
    #: sabe lo que registra, y lo suyo pasa por revisión antes de publicarse.
    de_formulario: bool = False


class FuenteIngesta(Protocol):
    """Contrato de un adaptador de fuente."""

    nombre: str

    def obtener(self) -> Iterable[Publicacion]:
        """Devuelve las publicaciones disponibles en el origen."""
        ...


@dataclass
class ResultadoIngesta:
    creadas: list[str] = field(default_factory=list)
    actualizadas: list[str] = field(default_factory=list)
    descartadas: list[tuple[str, str]] = field(default_factory=list)  # (externo_id, motivo)

    @property
    def total(self) -> int:
        return len(self.creadas) + len(self.actualizadas) + len(self.descartadas)


# ─────────────────────────── Normalización ───────────────────────────


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").strip()


# Los municipios y sus plazas viven en `app.services.geografia`, que es la
# única lista. Antes este diccionario mapeaba municipio → plaza, y por eso un
# inmueble de Envigado se guardaba como "Medellín": se perdía dónde estaba.

#: Barrios y corregimientos con ciudad inequívoca, tomados del motor
#: conversacional para no mantener dos listas en paralelo.
ZONAS_CONOCIDAS = CIUDADES

#: Cómo viene escrito el negocio en un aviso real. Se comprueba en este orden:
#: "permuto mi casa" también dice "casa", y "se vende o se arrienda" resuelve a
#: lo primero que aparezca en esta tabla, que es la permuta y luego el arriendo.
ALIAS_NEGOCIO = {
    "permuta": TipoNegocio.PERMUTA.value,
    "permuto": TipoNegocio.PERMUTA.value,
    "permutar": TipoNegocio.PERMUTA.value,
    "arriendo": TipoNegocio.ARRIENDO.value,
    "arrienda": TipoNegocio.ARRIENDO.value,
    "arrendar": TipoNegocio.ARRIENDO.value,
    "arrendamiento": TipoNegocio.ARRIENDO.value,
    "alquiler": TipoNegocio.ARRIENDO.value,
    "alquila": TipoNegocio.ARRIENDO.value,
    "renta": TipoNegocio.ARRIENDO.value,
    "canon": TipoNegocio.ARRIENDO.value,
    "venta": TipoNegocio.VENTA.value,
    "vende": TipoNegocio.VENTA.value,
    "vendo": TipoNegocio.VENTA.value,
    "vender": TipoNegocio.VENTA.value,
}

ALIAS_TIPO = {
    "apartamento": TipoInmueble.APARTAMENTO.value,
    "apto": TipoInmueble.APARTAMENTO.value,
    "apartaestudio": TipoInmueble.APARTAMENTO.value,
    "casa": TipoInmueble.CASA.value,
    "casa campestre": TipoInmueble.CASA.value,
    "finca": TipoInmueble.CASA.value,
    "lote": TipoInmueble.LOTE.value,
    "terreno": TipoInmueble.LOTE.value,
    "parcela": TipoInmueble.LOTE.value,
}


def normalizar_negocio(valor: str) -> str:
    """Negocio a partir de texto libre. Lo no reconocido cae en venta.

    Cae en venta y no en None a propósito: es el negocio central, la cartera
    entera lo era antes de existir esta columna, y un inmueble sin negocio no
    tiene precio interpretable. Si el aviso dice arriendo, el operador lo ve en
    la cola de validación y puede corregirlo antes de publicar.
    """
    plano = _normalizar(valor)
    if plano in ALIAS_NEGOCIO:
        return ALIAS_NEGOCIO[plano]
    for alias, negocio in ALIAS_NEGOCIO.items():
        if re.search(rf"\b{re.escape(alias)}", plano):
            return negocio
    return NEGOCIO_POR_DEFECTO


def normalizar_ciudad(valor: str) -> str | None:
    """Municipio canónico para `Propiedad.ciudad`, o None si no es de la región.

    Devuelve el municipio y no la plaza: un inmueble de Envigado se guarda como
    Envigado. A quién puede ofrecérsele es otra pregunta, y la responde
    `geografia.plaza_de` en el emparejamiento.
    """
    return geografia.normalizar_municipio(valor)


def ciudad_por_zona(valor: str) -> str | None:
    """Deduce la ciudad a partir de un barrio o corregimiento conocido.

    Reutiliza el diccionario del motor conversacional: los barrios que nombra un
    comprador son los mismos que aparecen en un aviso, y no tiene sentido
    mantener dos listas que se desincronizan. `geografia` solo cubre municipios;
    esto cubre 'Cerritos', 'Laureles', 'El Poblado'…
    """
    plano = _normalizar(valor)
    if not plano:
        return None
    for alias, ciudad in ZONAS_CONOCIDAS.items():
        if re.search(rf"\b{re.escape(alias)}\b", plano):
            return ciudad
    return None


def normalizar_tipo(valor: str) -> str | None:
    plano = _normalizar(valor)
    if plano in ALIAS_TIPO:
        return ALIAS_TIPO[plano]
    for alias, tipo in ALIAS_TIPO.items():
        if re.search(rf"\b{re.escape(alias)}\b", plano):
            return tipo
    return None


def validar(pub: Publicacion) -> str | None:
    """Devuelve el motivo de descarte, o None si la publicación sirve.

    Se rechaza en vez de "arreglar": un inmueble con datos inventados es peor
    que uno ausente, porque el bot se lo mostraría a un comprador como real.
    """
    # La referencia es la única fuente exenta de mandato: son avisos reales de
    # terceros cargados para probar, y `commission.confirmar_venta` los bloquea.
    # Aun así se exige evidencia: hay que poder decir de dónde salió cada uno.
    if pub.fuente not in FUENTES_SIN_MANDATO and not pub.mandato:
        return "sin mandato de comercialización"
    if not pub.mandato_evidencia.strip():
        return "mandato sin evidencia registrada"
    # Lo que sigue se mide con dos varas, según quién escribió los datos.
    #
    # Un raspador, un feed o el extractor de avisos pueden equivocarse en
    # silencio: de un aviso mal leído sale un municipio que nadie busca o la
    # cuota de administración tomada por precio, y eso entra a la cartera sin
    # que nadie lo mire. A ellos se les exige plaza y precio razonable.
    #
    # Del otro lado hay una persona llenando campos con el inmueble delante —el
    # dueño en /publicar—: sabe dónde queda y cuánto vale, discutírselo es
    # estorbarle, y lo suyo entra como `pendiente` y pasa por revisión humana.
    if pub.de_formulario:
        if normalizar_ciudad(pub.ciudad) is None:
            return f"'{pub.ciudad or '(vacía)'}' no es un municipio de Antioquia ni de Risaralda"
    elif geografia.plaza_de(pub.ciudad) is None:
        return f"ciudad fuera de cobertura: {pub.ciudad or '(vacía)'}"

    if normalizar_tipo(pub.tipo) is None:
        return f"tipo de inmueble no reconocido: {pub.tipo or '(vacío)'}"
    try:
        precio = int(pub.precio)
    except (TypeError, ValueError):
        return f"precio ilegible: {pub.precio!r}"
    negocio = normalizar_negocio(pub.negocio)
    if pub.de_formulario:
        # Al dueño no se le corrige el precio de su inmueble: solo se exige que
        # sea un número positivo.
        if precio <= 0:
            return f"el precio debe ser mayor que cero: {precio}"
    elif not precio_razonable(precio, negocio):
        # El rango depende del negocio: 900.000 es un canon normal y un precio
        # de venta imposible.
        return f"precio fuera de rango razonable para {negocio}: {precio}"
    if not pub.externo_id:
        return "falta el identificador de origen (necesario para deduplicar)"
    return None


# ─────────────────────────── Ingesta ───────────────────────────


def _existente(db: Session, pub: Publicacion) -> Propiedad | None:
    """Busca el mismo aviso ya ingerido, por (fuente, externo_id)."""
    return db.scalar(
        select(Propiedad).where(
            Propiedad.fuente == pub.fuente,
            Propiedad.externo_id == pub.externo_id,
        )
    )


def _codigo_libre(db: Session, ciudad: str) -> str:
    """Código de cartera garantizando que no choque.

    `portfolio.siguiente_codigo` numera por conteo, así que un inmueble borrado
    o rechazado deja el contador por debajo del máximo y el siguiente lote
    colisionaría. En ingesta se crean muchos de golpe: conviene asegurarlo.
    """
    codigo = siguiente_codigo(db, ciudad)
    if db.get(Propiedad, codigo) is None:
        return codigo
    raiz, _, numero = codigo.rpartition("-")
    siguiente = int(numero)
    while db.get(Propiedad, f"{raiz}-{siguiente:03d}") is not None:
        siguiente += 1
    return f"{raiz}-{siguiente:03d}"


def _campos(pub: Publicacion) -> dict:
    return {
        "ciudad": normalizar_ciudad(pub.ciudad),
        "zona": (pub.zona or "").strip()[:80],
        "tipo": normalizar_tipo(pub.tipo),
        "negocio": normalizar_negocio(pub.negocio),
        "habitaciones": int(pub.habitaciones or 0),
        "banos": int(pub.banos or 0),
        "area_m2": float(pub.area_m2 or 0),
        "precio": int(pub.precio),
        "descripcion": (pub.descripcion or "").strip(),
        "foto_url": pub.foto_url or "/static/img/placeholder.svg",
        "url_origen": pub.url_origen or "",
        "propietario": (pub.propietario or "").strip()[:120],
        "propietario_telefono": pub.propietario_telefono or None,
        "propietario_telefono_hash": (
            indice_ciego(pub.propietario_telefono) if pub.propietario_telefono else None
        ),
        # La referencia se guarda con mandato=False a propósito: en la tabla debe
        # verse que no es inventario comercializable, no solo en su `fuente`.
        "mandato": pub.fuente not in FUENTES_SIN_MANDATO,
        "mandato_evidencia": pub.mandato_evidencia.strip(),
    }


def ingerir_una(db: Session, pub: Publicacion, *, actor: str = "ingesta") -> Propiedad:
    """Punto único de entrada de inmuebles automáticos.

    Todo lo que entra queda en `pendiente_validacion`: el motor de emparejamiento
    solo consulta `disponible`, así que ningún comprador ve un inmueble que un
    operador no haya revisado antes (ADR-05).
    """
    motivo = validar(pub)
    if motivo:
        auditar(
            db, actor=actor, accion="inmueble_descartado", entidad="ingesta",
            entidad_id=f"{pub.fuente}:{pub.externo_id or '-'}", detalle=motivo,
        )
        if "mandato" in motivo:
            raise MandatoAusente(motivo)
        raise ValueError(motivo)

    campos = _campos(pub)
    propiedad = _existente(db, pub)

    if propiedad is None:
        propiedad = Propiedad(
            id=_codigo_libre(db, campos["ciudad"]),
            fuente=pub.fuente,
            externo_id=pub.externo_id,
            estado=EstadoPropiedad.PENDIENTE.value,
            **campos,
        )
        db.add(propiedad)
        accion, detalle = "inmueble_ingerido", f"fuente={pub.fuente}"
    else:
        # Reingesta del mismo aviso: se refresca el contenido, pero NO el estado.
        # Si el operador ya lo aprobó o lo rechazó, esa decisión manda.
        for campo, valor in campos.items():
            setattr(propiedad, campo, valor)
        accion, detalle = "inmueble_actualizado", f"fuente={pub.fuente}"

    db.flush()
    auditar(
        db, actor=actor, accion=accion, entidad="propiedad",
        entidad_id=propiedad.id,
        detalle=f"{detalle} externo_id={pub.externo_id} mandato={pub.mandato_evidencia[:80]}",
    )
    return propiedad


def ingerir(
    db: Session, publicaciones: Iterable[Publicacion], *, actor: str = "ingesta"
) -> ResultadoIngesta:
    """Ingiere un lote. Un registro malo no tumba el resto del lote."""
    resultado = ResultadoIngesta()
    for pub in publicaciones:
        ya_estaba = _existente(db, pub) is not None
        try:
            propiedad = ingerir_una(db, pub, actor=actor)
        except (MandatoAusente, ValueError) as exc:
            resultado.descartadas.append((pub.externo_id or "-", str(exc)))
            continue
        (resultado.actualizadas if ya_estaba else resultado.creadas).append(propiedad.id)
    return resultado


def ejecutar(db: Session, fuente: FuenteIngesta, *, actor: str = "ingesta") -> ResultadoIngesta:
    """Corre un adaptador completo y deja constancia del barrido."""
    resultado = ingerir(db, fuente.obtener(), actor=actor)
    auditar(
        db, actor=actor, accion="ingesta_ejecutada", entidad="ingesta",
        entidad_id=fuente.nombre,
        detalle=(
            f"creadas={len(resultado.creadas)} "
            f"actualizadas={len(resultado.actualizadas)} "
            f"descartadas={len(resultado.descartadas)}"
        ),
    )
    log.info(
        "Ingesta %s: %d nuevas, %d actualizadas, %d descartadas",
        fuente.nombre, len(resultado.creadas), len(resultado.actualizadas),
        len(resultado.descartadas),
    )
    return resultado


# ─────────────────────────── Validación por el operador ───────────────────────────


def pendientes(db: Session) -> list[Propiedad]:
    """Cola de inmuebles esperando revisión humana."""
    return list(
        db.scalars(
            select(Propiedad)
            .where(Propiedad.estado == EstadoPropiedad.PENDIENTE.value)
            .order_by(Propiedad.creada_en)
        )
    )


def aprobar(db: Session, propiedad: Propiedad, actor: str) -> Propiedad:
    """El operador da por bueno el inmueble y lo publica a los compradores."""
    if not propiedad.mandato and not propiedad.es_referencia:
        raise MandatoAusente(
            "No se puede publicar un inmueble sin mandato de comercialización."
        )
    propiedad.estado = EstadoPropiedad.DISPONIBLE.value
    db.flush()
    auditar(
        db, actor=actor, accion="inmueble_aprobado", entidad="propiedad",
        entidad_id=propiedad.id, detalle=f"fuente={propiedad.fuente}",
    )
    return propiedad


#: Estados desde los que un inmueble puede volver a publicarse. `vendida` no
#: está: tiene una venta y una comisión atribuidas, y volver a ofrecerlo sin
#: deshacer eso dejaría el negocio en dos estados contradictorios.
ESTADOS_REACTIVABLES = frozenset(
    {EstadoPropiedad.INACTIVA.value, EstadoPropiedad.RECHAZADA.value}
)


def reactivar(db: Session, propiedad: Propiedad, actor: str) -> Propiedad:
    """Vuelve a poner en cartera un inmueble inactivado o rechazado.

    Vive aquí y no en `portfolio` —donde está `inactivar`— porque comparte con
    `aprobar` la regla de que sin mandato no se publica, y esa regla debe tener
    un solo lugar donde vivir. `portfolio` no puede importar este módulo sin
    crear un ciclo.
    """
    if propiedad.estado not in ESTADOS_REACTIVABLES:
        raise ValueError(
            f"No se puede reactivar un inmueble en estado '{propiedad.estado}'. "
            f"Solo desde: {', '.join(sorted(ESTADOS_REACTIVABLES))}."
        )
    if not propiedad.mandato and not propiedad.es_referencia:
        raise MandatoAusente(
            "No se puede publicar un inmueble sin mandato de comercialización."
        )

    anterior = propiedad.estado
    propiedad.estado = EstadoPropiedad.DISPONIBLE.value
    db.flush()
    auditar(
        db, actor=actor, accion="inmueble_reactivado", entidad="propiedad",
        entidad_id=propiedad.id, detalle=f"{anterior} -> disponible",
    )
    return propiedad


def referencias(db: Session) -> list[Propiedad]:
    """Inmuebles cargados solo para probar. Deben desaparecer antes de producción."""
    return list(
        db.scalars(
            select(Propiedad)
            .where(Propiedad.fuente.in_(FUENTES_SIN_MANDATO))
            .order_by(Propiedad.creada_en)
        )
    )


class TieneVenta(ValueError):
    """El inmueble sostiene una venta con comisión atribuida."""


def _desligar(db: Session, ids: list[str]) -> None:
    """Suelta las referencias que impiden borrar un inmueble.

    Las solicitudes se conservan con el puntero en nulo —una petición de visita
    es evidencia de que un titular pidió contacto, y eso no se tira—. Los
    emparejamientos sí se borran: su clave foránea no admite nulo y sin el
    inmueble no significan nada.
    """
    db.execute(
        update(Solicitud).where(Solicitud.propiedad_id.in_(ids)).values(propiedad_id=None)
    )
    db.execute(delete(Emparejamiento).where(Emparejamiento.propiedad_id.in_(ids)))


def eliminar_inmueble(db: Session, propiedad: Propiedad, actor: str) -> None:
    """Borra un inmueble de la cartera, con sus fotos y comentarios.

    Se niega si tiene una venta registrada: la clave foránea de `Venta` no admite
    nulo, así que borrarlo exigiría destruir el registro de comisión y su
    trazabilidad. Para retirarlo de circulación sin perder historia está
    `inactivar`.
    """
    venta = db.scalar(select(Venta).where(Venta.propiedad_id == propiedad.id))
    if venta is not None:
        raise TieneVenta(
            f"{propiedad.id} tiene la venta {venta.codigo} registrada con una comisión "
            f"atribuida. Bórrala primero o usa «Inactivar» para retirarlo de la cartera "
            f"sin perder el historial."
        )

    detalle = (
        f"{propiedad.tipo} en {propiedad.zona}, {propiedad.ciudad} por {propiedad.precio}. "
        f"fuente={propiedad.fuente}"
    )
    _desligar(db, [propiedad.id])
    db.delete(propiedad)          # arrastra fotos y comentarios por cascada
    db.flush()
    auditar(
        db, actor=actor, accion="inmueble_eliminado", entidad="propiedad",
        entidad_id=propiedad.id, detalle=detalle,
    )


def purgar_referencias(db: Session, actor: str) -> int:
    """Borra de un golpe todo lo cargado como referencia.

    Es la contraparte del modo: se puede probar con avisos reales de terceros
    porque existe un botón que los saca todos antes de salir a producción. Sin
    esta salida, el modo referencia sería otra forma de dejar datos que no son
    tuyos mezclados con tu inventario.
    """
    a_borrar = referencias(db)
    if not a_borrar:
        return 0
    # Igual que en el borrado individual: hay que soltar emparejamientos y
    # solicitudes antes, o la clave foránea aborta el purgado completo.
    _desligar(db, [p.id for p in a_borrar])
    for p in a_borrar:
        auditar(
            db, actor=actor, accion="referencia_purgada", entidad="propiedad",
            entidad_id=p.id,
            detalle=f"{p.tipo} en {p.zona}, {p.ciudad}. fuente={p.fuente}",
        )
        db.delete(p)
    db.flush()
    log.info("Purgadas %d propiedades de referencia por %s", len(a_borrar), actor)
    return len(a_borrar)


def rechazar(db: Session, propiedad: Propiedad, actor: str, motivo: str = "") -> Propiedad:
    """Descarta el inmueble. Se conserva el registro para no reingerirlo en bucle."""
    propiedad.estado = EstadoPropiedad.RECHAZADA.value
    db.flush()
    auditar(
        db, actor=actor, accion="inmueble_rechazado", entidad="propiedad",
        entidad_id=propiedad.id, detalle=motivo or "sin motivo registrado",
    )
    return propiedad


# ─────────────────────────── Adaptador: captación de propietarios ───────────────────────────


class CaptacionPropietarios:
    """Fuente alimentada por el formulario público de propietarios.

    A diferencia de las demás, no sale a buscar: recibe. Cada publicación entra
    por `/publicar` con la casilla de mandato marcada, y esa marca es la
    evidencia. Es la única vía que produce inventario sobre el que se puede
    cobrar comisión, porque el dueño autorizó expresamente comercializarlo.
    """

    nombre = "captacion_propietario"

    def __init__(self, pendientes: Iterable[Publicacion] = ()) -> None:
        self._pendientes = list(pendientes)

    def obtener(self) -> Iterable[Publicacion]:
        return self._pendientes


class ExtraccionFallida(ValueError):
    """El LLM no pudo sacar del texto lo mínimo para tener un inmueble."""


def publicacion_desde_texto(
    texto: str,
    *,
    fuente: str = FuentePropiedad.CAPTACION_PROPIETARIO.value,
    externo_id: str | None = None,
    url_origen: str = "",
    mandato_evidencia: str,
    telefono: str | None = None,
) -> tuple[Publicacion, dict]:
    """Convierte un aviso en texto libre en una `Publicacion`, con el LLM (RF-10).

    Devuelve además el crudo del extractor, para que el operador vea qué se
    dedujo y con qué confianza antes de aprobar.

    El LLM estructura; no autoriza. `mandato_evidencia` la aporta quien carga el
    aviso —el propietario al marcar la casilla, o el operador al declarar de
    dónde salió— y sin ella `ingerir_una` rechaza igual.
    """
    if not cliente.disponible:
        raise ExtraccionFallida(
            "No hay LLM configurado: la extracción de avisos en texto libre "
            "requiere ANTHROPIC_API_KEY o MOONSHOT_API_KEY."
        )
    if not texto.strip():
        raise ExtraccionFallida("El aviso está vacío.")

    try:
        datos = cliente.extraer_inmueble(texto)
    except Exception as exc:  # noqa: BLE001 - se traduce a un error de dominio
        raise ExtraccionFallida(f"El extractor no respondió: {exc}") from exc

    # El identificador de origen se deriva del propio texto cuando la fuente no
    # trae uno: reingerir el mismo aviso actualiza en vez de duplicar.
    huella = externo_id or indice_ciego(_normalizar(texto))[:32]

    ciudad = datos.get("ciudad") or ""
    zona = datos.get("zona") or ""
    # "Lote campestre en Cerritos" no nombra a Pereira, pero Cerritos es un
    # corregimiento suyo: si la ciudad no resuelve y la zona sí, la zona manda.
    # Sin esto se descartaban avisos válidos por no repetir el municipio.
    if normalizar_ciudad(ciudad) is None and (resuelta := ciudad_por_zona(zona)):
        ciudad = resuelta

    publicacion = Publicacion(
        fuente=fuente,
        externo_id=huella,
        ciudad=ciudad,
        zona=zona,
        tipo=datos.get("tipo") or "",
        # El extractor devuelve null cuando el aviso no dice de qué negocio se
        # trata; el texto crudo suele decirlo igual ("se arrienda…"), así que
        # `normalizar_negocio` tiene una segunda oportunidad antes del defecto.
        negocio=datos.get("negocio") or normalizar_negocio(texto),
        precio=int(datos.get("precio") or 0),
        habitaciones=int(datos.get("habitaciones") or 0),
        banos=int(datos.get("banos") or 0),
        area_m2=float(datos.get("area_m2") or 0),
        descripcion=(datos.get("descripcion") or texto.strip())[:1000],
        propietario=(datos.get("propietario") or "")[:120],
        propietario_telefono=telefono or datos.get("telefono") or None,
        url_origen=url_origen,
        mandato=True,
        mandato_evidencia=mandato_evidencia,
    )
    return publicacion, datos


def publicacion_de_formulario(
    *,
    telefono: str,
    ciudad: str,
    tipo: str,
    precio: int,
    negocio: str = NEGOCIO_POR_DEFECTO,
    zona: str = "",
    habitaciones: int = 0,
    banos: int = 0,
    area_m2: float = 0.0,
    descripcion: str = "",
    propietario: str = "",
    autoriza_mandato: bool,
    origen: str = "landing /publicar",
) -> Publicacion:
    """Construye la publicación a partir del formulario del propietario.

    El `externo_id` se deriva del teléfono con índice ciego: si el mismo dueño
    vuelve a enviar el mismo inmueble, se actualiza en vez de duplicarse, y sin
    guardar el teléfono en claro dentro del identificador.
    """
    if not autoriza_mandato:
        raise MandatoAusente(
            "El propietario debe autorizar expresamente la comercialización del "
            "inmueble. Sin esa autorización no se almacena nada."
        )
    huella = indice_ciego(f"{telefono}|{_normalizar(ciudad)}|{_normalizar(zona)}|{precio}")
    return Publicacion(
        fuente=FuentePropiedad.CAPTACION_PROPIETARIO.value,
        externo_id=huella[:32],
        ciudad=ciudad,
        zona=zona,
        tipo=tipo,
        negocio=negocio,
        precio=precio,
        habitaciones=habitaciones,
        banos=banos,
        area_m2=area_m2,
        descripcion=descripcion,
        propietario=propietario,
        propietario_telefono=telefono,
        mandato=True,
        mandato_evidencia=(
            f"Casilla de autorización de comercialización marcada por el "
            f"propietario en {origen} — {settings.empresa_nombre}"
        ),
        de_formulario=True,
    )
