"""Cartera de demostración: `python -m app.demo`

Estos 30 inmuebles son **inventados**. No corresponden a ninguna publicación
real y no deben mostrarse a un comprador.

Por eso entran con `fuente="demo"`, que está en `FUENTES_SIN_MANDATO`: el
sistema bloquea registrar una venta sobre ellos, la cartera los marca en rojo y
el botón de purgado del dashboard los borra todos de un golpe. Es el mismo
mecanismo del modo referencia, y existe justamente porque una cartera poblada
con datos falsos indistinguibles de los reales ya causó confusión antes.

Las zonas y los rangos de precio sí son verosímiles para el Área Metropolitana
del Valle de Aburrá y para Pereira/Dosquebradas: sirven para probar el
emparejamiento con cifras que se comportan como las del mercado.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.db import sesion
from app.models import EstadoPropiedad, FuentePropiedad, Propiedad
from app.services.compliance import auditar

EVIDENCIA = (
    "Inmueble INVENTADO de la cartera de demostración. No existe, no tiene "
    "propietario y no puede comercializarse. Purgar antes de operar."
)

#: (id, ciudad, zona, tipo, hab, baños, m², precio, descripción)
CARTERA: list[tuple] = [
    # ── Pereira y Dosquebradas ──────────────────────────────────────────
    ("DEMO-PER-01", "Pereira", "Pinares", "apartamento", 3, 2, 96, 480_000_000,
     "Piso 8 con vista a la Circunvalar, dos parqueaderos y cuarto útil."),
    ("DEMO-PER-02", "Pereira", "Álamos", "apartamento", 2, 2, 72, 315_000_000,
     "Unidad cerrada con piscina y gimnasio, cerca de la Universidad Tecnológica."),
    ("DEMO-PER-03", "Pereira", "Circunvalar", "apartamento", 3, 3, 128, 690_000_000,
     "Remodelado, cocina abierta y balcón amplio sobre la avenida."),
    ("DEMO-PER-04", "Pereira", "Centro", "apartamento", 2, 1, 58, 195_000_000,
     "Tercer piso sin ascensor, ideal para renta, a dos cuadras de la Plaza de Bolívar."),
    ("DEMO-PER-05", "Pereira", "Cuba", "apartamento", 3, 2, 68, 225_000_000,
     "Conjunto con portería 24 horas y zona de juegos infantiles."),
    ("DEMO-PER-06", "Pereira", "La Julita", "casa", 4, 3, 210, 820_000_000,
     "Casa de dos plantas con patio interior, estudio y garaje doble."),
    ("DEMO-PER-07", "Pereira", "Los Alpes", "casa", 3, 2, 145, 430_000_000,
     "Casa esquinera con terraza y local independiente en el primer piso."),
    ("DEMO-PER-08", "Pereira", "Belmonte", "casa", 3, 2, 132, 385_000_000,
     "Barrio residencial tranquilo, cerca del Terminal de Transportes."),
    ("DEMO-PER-09", "Pereira", "Cerritos", "casa", 5, 4, 320, 1_450_000_000,
     "Casa campestre con piscina, kiosco y 2.000 m² de lote arborizado."),
    ("DEMO-PER-10", "Pereira", "Cerritos", "lote", 0, 0, 1_800, 540_000_000,
     "Lote plano escriturado sobre vía principal, servicios completos."),
    ("DEMO-PER-11", "Pereira", "Villa Verde", "lote", 0, 0, 320, 185_000_000,
     "Lote urbano en proyecto consolidado, apto para vivienda de dos pisos."),
    ("DEMO-PER-12", "Pereira", "La Pradera, Dosquebradas", "apartamento", 3, 2, 74, 245_000_000,
     "Apartamento con balcón en unidad con salón social, vía Turín."),
    ("DEMO-PER-13", "Pereira", "Los Naranjos, Dosquebradas", "apartamento", 2, 1, 55, 155_000_000,
     "Cerca del Viaducto, transporte público a la puerta."),
    ("DEMO-PER-14", "Pereira", "Santa Mónica, Dosquebradas", "casa", 4, 3, 168, 465_000_000,
     "Casa de tres niveles con local comercial en la planta baja."),
    ("DEMO-PER-15", "Pereira", "Frailes, Dosquebradas", "lote", 0, 0, 640, 265_000_000,
     "Lote con pendiente leve, buena vista al valle, listo para construir."),

    # ── Medellín y Área Metropolitana ───────────────────────────────────
    ("DEMO-MED-01", "Medellín", "El Poblado", "apartamento", 3, 3, 142, 1_180_000_000,
     "Unidad con vista abierta, gimnasio, piscina y tres parqueaderos."),
    ("DEMO-MED-02", "Medellín", "El Poblado", "apartamento", 1, 1, 48, 465_000_000,
     "Apartaestudio amoblado cerca del Parque Lleras, alta rotación en renta."),
    ("DEMO-MED-03", "Medellín", "Laureles", "apartamento", 3, 2, 105, 720_000_000,
     "Segundo piso con ascensor, sector Estadio, cerca del metro."),
    ("DEMO-MED-04", "Medellín", "Conquistadores", "apartamento", 2, 2, 82, 545_000_000,
     "Remodelado, pisos en madera, balcón con vista a la avenida Nutibara."),
    ("DEMO-MED-05", "Medellín", "Belén Rosales", "apartamento", 3, 2, 88, 395_000_000,
     "Piso 4 con ascensor, unidad con piscina y zona de juegos."),
    ("DEMO-MED-06", "Medellín", "Robledo", "apartamento", 3, 2, 70, 265_000_000,
     "Cerca de la Universidad Nacional, transporte masivo a dos cuadras."),
    ("DEMO-MED-07", "Medellín", "La América", "casa", 4, 3, 175, 620_000_000,
     "Casa de dos plantas con patio y garaje, barrio tradicional."),
    ("DEMO-MED-08", "Medellín", "Laureles", "casa", 5, 4, 290, 1_650_000_000,
     "Casa de esquina con jardín, apta para vivienda o sede empresarial."),
    ("DEMO-MED-09", "Medellín", "Niquía, Bello", "apartamento", 3, 2, 66, 215_000_000,
     "Frente a la estación Niquía del metro, unidad con portería."),
    ("DEMO-MED-10", "Medellín", "Cabañas, Bello", "casa", 3, 2, 120, 335_000_000,
     "Casa con terraza y posibilidad de ampliar un tercer nivel."),
    ("DEMO-MED-11", "Medellín", "Zúñiga, Envigado", "apartamento", 3, 3, 118, 880_000_000,
     "Unidad campestre con senderos, cancha y vigilancia permanente."),
    ("DEMO-MED-12", "Medellín", "La Paz, Envigado", "casa", 4, 3, 195, 745_000_000,
     "Casa con estudio independiente, patio y dos parqueaderos cubiertos."),
    ("DEMO-MED-13", "Medellín", "Aves María, Sabaneta", "apartamento", 2, 2, 64, 305_000_000,
     "Unidad con piscina y salón social, cerca de la Autopista Sur."),
    ("DEMO-MED-14", "Medellín", "Ditaires, Itagüí", "apartamento", 3, 2, 76, 285_000_000,
     "Frente al parque Ditaires, unidad cerrada con juegos infantiles."),
    ("DEMO-MED-15", "Medellín", "La Estrella", "lote", 0, 0, 1_200, 420_000_000,
     "Lote con acceso vehicular y servicios en la vía, uso mixto."),
]


def cargar() -> int:
    """Inserta los inmuebles que falten. Es idempotente: se puede repetir."""
    creados = 0
    with sesion() as db:
        existentes = set(db.scalars(select(Propiedad.id)).all())
        for (
            pid, ciudad, zona, tipo, hab, banos, area, precio, descripcion
        ) in CARTERA:
            if pid in existentes:
                continue
            db.add(
                Propiedad(
                    id=pid, ciudad=ciudad, zona=zona, tipo=tipo,
                    habitaciones=hab, banos=banos, area_m2=float(area), precio=precio,
                    descripcion=descripcion,
                    estado=EstadoPropiedad.DISPONIBLE.value,
                    fuente=FuentePropiedad.DEMO.value,
                    mandato=False,
                    mandato_evidencia=EVIDENCIA,
                    foto_url="/static/img/placeholder.svg",
                )
            )
            creados += 1

        if creados:
            db.flush()
            auditar(
                db, actor="app.demo", accion="cartera_demo_cargada", entidad="ingesta",
                entidad_id="demo", detalle=f"{creados} inmuebles inventados, no vendibles",
            )
    return creados


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    creados = cargar()
    print(f"Inmuebles de demostración cargados: {creados}")
    print(
        "\nSon INVENTADOS: la cartera los marca en rojo, no pueden generar venta\n"
        "ni comisión, y el botón «Purgar» del dashboard los elimina todos."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
