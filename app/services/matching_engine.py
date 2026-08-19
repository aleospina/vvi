"""Motor de emparejamiento (SRS §2.3 · RF-09, RF-11).

Filtro determinístico sobre la cartera + ranking. El LLM solo redacta la frase
de venta a partir de fichas reales; nunca decide qué inmueble existe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.llm.client import cliente
from app.models import Emparejamiento, Propiedad, Prospecto
from app.services.nlu_engine import normalizar
from app.services.geografia import municipios_de_plaza
from app.services.portfolio import como_dict, municipio_de, plano

log = logging.getLogger(__name__)

TOPE_RESULTADOS = 3
#: Cuando el comprador pide ver *todo* lo disponible, la terna curada estorba.
#: Sigue habiendo un límite: un mensaje de chat con 40 fichas no se lee.
TOPE_LISTADO = 10
#: El rango de precio se respeta al peso. Antes había una holgura del 5% sobre el
#: techo —"mostrar algo poquito arriba es útil"—, pero quien dice "entre 200 y 300"
#: y recibe uno de 314 no siente una cortesía: siente que el filtro no funciona.
#: Si quiere estirar la banda, la nota de filtro le dice cómo pedirlo.


@dataclass
class Match:
    propiedad: Propiedad
    puntaje: float
    frase_venta: str = ""


def _frase_por_plantilla(p: Propiedad, perfil: dict) -> str:
    """Frase de respaldo, construida solo con datos reales de la ficha."""
    partes = []
    if perfil.get("zona") and normalizar(perfil["zona"]) in normalizar(p.zona):
        partes.append(f"justo en {p.zona}")
    if perfil.get("habitaciones") and p.habitaciones >= int(perfil["habitaciones"]):
        partes.append(f"{p.habitaciones} habitaciones")
    if perfil.get("presupuesto_max") and p.precio <= int(perfil["presupuesto_max"]):
        partes.append("dentro de tu presupuesto")
    if partes:
        return f"Encaja porque está {', '.join(partes)}."
    return (p.descripcion or "").split(".")[0][:110] or "Disponible para visita."


def _puntuar(p: Propiedad, perfil: dict) -> float:
    """Cercanía al tope del presupuesto + coincidencia de zona (SRS §2.3)."""
    puntaje = 0.0

    tope = perfil.get("presupuesto_max")
    if tope:
        # 100 si aprovecha todo el presupuesto, decae al alejarse hacia abajo.
        puntaje += max(0.0, 100.0 - abs(int(tope) - p.precio) / int(tope) * 100.0)

    zona = perfil.get("zona")
    if zona and normalizar(zona) in normalizar(p.zona):
        puntaje += 40.0

    habs = perfil.get("habitaciones")
    if habs and p.habitaciones == int(habs):
        puntaje += 15.0
    elif habs and p.habitaciones > int(habs):
        puntaje += 5.0

    return round(puntaje, 2)


def _de_la_plaza(perfil: dict) -> tuple[str, ...]:
    """Municipios que cuentan como la plaza que pidió el comprador.

    `Propiedad.ciudad` guarda el municipio real desde que la cartera dejó de
    forzar todo a "Medellín" o "Pereira". Comparar por igualdad escondería
    Envigado a quien busca en Medellín, así que la plaza se expande.

    Si el perfil trae un municipio suelto que no es plaza, se usa tal cual.
    """
    pedida = perfil.get("ciudad") or ""
    return municipios_de_plaza(pedida) or (pedida,)


def _del_municipio(candidatas: list[Propiedad], perfil: dict) -> list[Propiedad]:
    """Acota a un municipio concreto del área metropolitana.

    `ciudad` es la plaza de cobertura y mete en el mismo saco a Pereira y
    Dosquebradas, o a Medellín, Envigado y Sabaneta. Pero quien pregunta por
    "apartamentos en Dosquebradas" no quiere ver Pereira, y quien pregunta por
    Pereira no quiere Dosquebradas: son municipios distintos y el comprador los
    vive como tales.

    El municipio de cada inmueble se deduce de su zona (`portfolio.municipio_de`),
    con la convención "Barrio, Municipio" de la cartera. Sin municipio en el
    perfil no se filtra nada: la búsqueda sigue siendo de toda la plaza.
    """
    municipio = perfil.get("municipio")
    if not municipio:
        return candidatas
    objetivo = plano(municipio)
    return [p for p in candidatas if plano(municipio_de(p)) == objetivo]


def buscar(db: Session, perfil: dict, limite: int = TOPE_RESULTADOS) -> list[Match]:
    """Filtra la cartera por los criterios del prospecto y rankea (RF-09)."""
    # El presupuesto filtra, pero no habilita: quién decide si ya hay datos para
    # emparejar es el gateway (`faltan_datos`). Exigirlo aquí impedía atender un
    # "muéstrame todo lo de Medellín" perfectamente válido.
    if not (perfil.get("ciudad") and perfil.get("tipo")):
        return []

    piso = int(perfil.get("presupuesto_min") or 0)

    consulta = (
        select(Propiedad)
        .where(
            # `ciudad` guarda el municipio, así que la plaza que pidió el
            # comprador se expande a los suyos: quien dice "Medellín" tiene que
            # ver Envigado y Sabaneta, que para él son el mismo mercado. Luego
            # `_del_municipio` acota si nombró uno concreto.
            Propiedad.ciudad.in_(_de_la_plaza(perfil)),
            Propiedad.tipo == perfil["tipo"],
            Propiedad.estado == "disponible",
            Propiedad.precio >= piso,
        )
    )
    # Sin techo declarado ("desde 200 millones") no se acota por arriba: filtrar
    # con un tope inventado es justo lo que dejaba fuera la cartera cara.
    if perfil.get("presupuesto_max"):
        consulta = consulta.where(Propiedad.precio <= int(perfil["presupuesto_max"]))
    if perfil.get("habitaciones"):
        consulta = consulta.where(Propiedad.habitaciones >= int(perfil["habitaciones"]))

    candidatas = _del_municipio(list(db.scalars(consulta)), perfil)
    matches = [Match(propiedad=p, puntaje=_puntuar(p, perfil)) for p in candidatas]
    matches.sort(key=lambda m: (-m.puntaje, m.propiedad.precio))
    return matches[:limite]


def conteo(db: Session, perfil: dict) -> tuple[int, int]:
    """(cuántos caben en el presupuesto, cuántos hay para esa ciudad y tipo).

    Alimenta el aviso de filtro activo: una cartera de 4 que llega recortada a 1
    parece un error del bot si no se dice qué la recortó.
    """
    if not (perfil.get("ciudad") and perfil.get("tipo")):
        return 0, 0
    # El total cuenta el mismo universo que se muestra: si la búsqueda es de
    # Dosquebradas, decir "tengo 8 en total" contando Pereira haría creer que el
    # bot esconde inmuebles que en realidad son de otro municipio.
    de_la_plaza = list(
        db.scalars(
            select(Propiedad).where(
                Propiedad.ciudad.in_(_de_la_plaza(perfil)),
                Propiedad.tipo == perfil["tipo"],
                Propiedad.estado == "disponible",
            )
        )
    )
    total = len(_del_municipio(de_la_plaza, perfil))
    return len(buscar(db, perfil, limite=total or 1)), total


def redactar_frases(matches: list[Match], perfil: dict) -> list[Match]:
    """Añade la frase de venta a cada match, con respaldo por plantilla (RF-11)."""
    if not matches:
        return matches

    frases: dict[str, str] = {}
    if cliente.disponible:
        try:
            frases = cliente.frases_venta([como_dict(m.propiedad) for m in matches], perfil)
        except Exception as exc:  # degradación deliberada
            log.warning("No se pudieron generar frases con LLM: %s", exc)

    for m in matches:
        m.frase_venta = frases.get(m.propiedad.id) or _frase_por_plantilla(m.propiedad, perfil)
    return matches


def registrar(db: Session, prospecto: Prospecto, matches: list[Match]) -> None:
    """Guarda qué se mostró, para justificar después la atribución (RF-15)."""
    existentes = set(
        db.scalars(
            select(Emparejamiento.propiedad_id).where(
                Emparejamiento.prospecto_id == prospecto.id
            )
        )
    )
    for m in matches:
        if m.propiedad.id in existentes:
            continue
        db.add(
            Emparejamiento(
                prospecto_id=prospecto.id,
                propiedad_id=m.propiedad.id,
                puntaje=m.puntaje,
                frase_venta=m.frase_venta,
            )
        )
    db.flush()


def emparejar(
    db: Session, prospecto: Prospecto, perfil: dict, limite: int = TOPE_RESULTADOS
) -> list[Match]:
    """Flujo completo: filtrar → rankear → redactar → registrar."""
    matches = redactar_frases(buscar(db, perfil, limite), perfil)
    if matches:
        registrar(db, prospecto, matches)
    return matches


def contexto_cartera(db: Session, perfil: dict, limite: int = 8) -> str:
    """Resumen textual de la cartera pertinente, para anclar al LLM (RF-07).

    Respeta la banda de precio por la misma razón que `buscar`: esta lista es la
    única fuente que el modelo tiene permitido nombrar, así que dejar dentro un
    inmueble fuera del rango es autorizarlo a ofrecer lo que el filtro escondió.
    """
    consulta = select(Propiedad).where(Propiedad.estado == "disponible")
    if perfil.get("ciudad"):
        consulta = consulta.where(Propiedad.ciudad.in_(_de_la_plaza(perfil)))
    if perfil.get("tipo"):
        consulta = consulta.where(Propiedad.tipo == perfil["tipo"])
    if perfil.get("presupuesto_min"):
        consulta = consulta.where(Propiedad.precio >= int(perfil["presupuesto_min"]))
    if perfil.get("presupuesto_max"):
        consulta = consulta.where(Propiedad.precio <= int(perfil["presupuesto_max"]))
    filas = list(db.scalars(consulta.order_by(Propiedad.precio).limit(limite)))
    return "\n".join(
        f"- {p.id} | {p.tipo} | {p.zona}, {p.ciudad} | {p.habitaciones} hab | "
        f"{p.area_m2:.0f} m² | ${p.precio:,}".replace(",", ".")
        for p in filas
    )


def ultimo_mostrado(db: Session, prospecto: Prospecto) -> str | None:
    """La propiedad que mejor puntuó de la última tanda que se le enseñó.

    Cuando el comprador contesta "visita" a secas, el turno ya no vuelve a
    emparejar —no hay nada nuevo que buscar—, así que la solicitud se quedaba
    sin referencia al inmueble y el asesor recibía un aviso sin saber por cuál
    llamaba. El emparejamiento sí quedó registrado en el turno anterior: basta
    con leerlo.
    """
    return db.scalar(
        select(Emparejamiento.propiedad_id)
        .where(Emparejamiento.prospecto_id == prospecto.id)
        .order_by(Emparejamiento.creado_en.desc(), Emparejamiento.puntaje.desc())
        .limit(1)
    )
