"""Modelo de datos (SRS §4). SQLite + cifrado de campo (ADR-04)."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.security.crypto import PII


def ahora() -> datetime:
    """UTC sin tzinfo.

    SQLite guarda la fecha sin desplazamiento horario, así que un datetime con
    tzinfo entra y sale distinto — lo que rompería el encadenado de hashes de la
    auditoría. Trabajamos en UTC ingenuo y anexamos la zona solo al comparar.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ─────────────────────────── Enumeraciones ───────────────────────────


class EstadoProspecto(str, enum.Enum):
    """Máquina de estados del prospecto (RF-13)."""

    NUEVO = "nuevo"
    CALIFICADO = "calificado"
    EMPAREJADO = "emparejado"
    CONTACTADO = "contactado"
    VISITA = "visita"
    OFERTA = "oferta"
    VENDIDO = "vendido"
    PERDIDO = "perdido"
    FUERA_DE_ALCANCE = "fuera_de_alcance"


#: Transiciones permitidas. Las de cierre las dispara SIEMPRE un humano (ADR-05).
TRANSICIONES: dict[EstadoProspecto, set[EstadoProspecto]] = {
    EstadoProspecto.NUEVO: {
        EstadoProspecto.CALIFICADO,
        EstadoProspecto.FUERA_DE_ALCANCE,
        EstadoProspecto.PERDIDO,
    },
    EstadoProspecto.CALIFICADO: {
        EstadoProspecto.EMPAREJADO,
        EstadoProspecto.CONTACTADO,
        # Un prospecto calificado puede pedir visita sin haber visto matches.
        EstadoProspecto.VISITA,
        EstadoProspecto.FUERA_DE_ALCANCE,
        EstadoProspecto.PERDIDO,
    },
    # `vendido` se admite desde cualquier etapa con negocio abierto: si un asesor
    # cerró sin pasar por el flujo, reportarlo debe ser trivial. Poner fricción
    # aquí incentivaría justo lo que queremos evitar (ventas no reportadas).
    EstadoProspecto.EMPAREJADO: {
        EstadoProspecto.CONTACTADO,
        EstadoProspecto.VISITA,
        EstadoProspecto.VENDIDO,
        EstadoProspecto.PERDIDO,
    },
    EstadoProspecto.CONTACTADO: {
        EstadoProspecto.VISITA,
        EstadoProspecto.OFERTA,
        EstadoProspecto.VENDIDO,
        EstadoProspecto.PERDIDO,
    },
    EstadoProspecto.VISITA: {
        EstadoProspecto.OFERTA,
        EstadoProspecto.VENDIDO,
        EstadoProspecto.PERDIDO,
    },
    EstadoProspecto.OFERTA: {EstadoProspecto.VENDIDO, EstadoProspecto.PERDIDO},
    EstadoProspecto.VENDIDO: set(),
    EstadoProspecto.PERDIDO: set(),
    EstadoProspecto.FUERA_DE_ALCANCE: {EstadoProspecto.CALIFICADO, EstadoProspecto.PERDIDO},
}

#: Estados en los que el negocio ya está "en la calle" y por tanto exigen desenlace
#: registrado en el sistema (control anti-elusión de comisión, HU-09).
ESTADOS_EN_CURSO = {EstadoProspecto.VISITA, EstadoProspecto.OFERTA}
ESTADOS_TERMINALES = {EstadoProspecto.VENDIDO, EstadoProspecto.PERDIDO}


class Etiqueta(str, enum.Enum):
    FRIO = "frío"
    TIBIO = "tibio"
    CALIENTE = "caliente"


class TipoInmueble(str, enum.Enum):
    CASA = "casa"
    APARTAMENTO = "apartamento"
    LOTE = "lote"


class EstadoPropiedad(str, enum.Enum):
    """Ciclo de vida de un inmueble en la cartera.

    `pendiente_validacion` es la puerta de entrada de todo lo que llega por
    ingesta automática: el motor de emparejamiento solo consulta `disponible`,
    así que nada llega a un comprador sin que un operador lo haya revisado
    (ADR-05, intervención humana).
    """

    PENDIENTE = "pendiente_validacion"
    DISPONIBLE = "disponible"
    INACTIVA = "inactiva"
    VENDIDA = "vendida"
    RECHAZADA = "rechazada"


class FuentePropiedad(str, enum.Enum):
    """De dónde salió el registro. Determina qué evidencia de mandato exigimos."""

    MANUAL = "manual"                       # el operador la cargó a mano
    CAPTACION_PROPIETARIO = "captacion_propietario"   # el dueño la publicó él mismo
    FEED_ALIADO = "feed_aliado"             # inmobiliaria con convenio (XML/CSV)
    MERCADO_LIBRE = "mercado_libre"         # API oficial, requiere OAuth
    #: Aviso real de un tercero, cargado para probar el sistema. No hay mandato,
    #: así que no se puede vender ni genera comisión. Se purga antes de producción.
    REFERENCIA = "referencia"
    #: Inmueble **inventado** para poblar demostraciones. No existe: ni se vende
    #: ni se muestra a un comprador real sin purgarlo antes.
    DEMO = "demo"


#: Fuentes que NO habilitan comercialización. Están aquí y no en una condición
#: suelta para que cualquier control nuevo tenga un único lugar donde mirar.
#: Todo lo que caiga aquí queda bloqueado para venta y entra en el purgado.
FUENTES_SIN_MANDATO = frozenset(
    {FuentePropiedad.REFERENCIA.value, FuentePropiedad.DEMO.value}
)


class Canal(str, enum.Enum):
    """Canales de entrada. Todos inbound / opt-in (ADR-01)."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"        # Evolution API (ADR-02b)
    LANDING = "landing"           # formulario web con casilla de consentimiento
    META_LEAD_ADS = "meta_lead_ads"   # Instagram / Facebook (formulario opt-in)
    MERCADO_LIBRE = "mercado_libre"   # el comprador pregunta en la publicación
    MARKETPLACE = "marketplace"
    OLX = "olx"
    MANUAL = "manual"             # carga por el operador de un inbound recibido


class Direccion(str, enum.Enum):
    ENTRANTE = "entrante"
    SALIENTE = "saliente"


# ─────────────────────────── Tablas ───────────────────────────


class Propiedad(Base):
    """Cartera de propiedades (RF-10). Datos mockeados en el MVP."""

    __tablename__ = "propiedades"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ciudad: Mapped[str] = mapped_column(String(64), index=True)
    zona: Mapped[str] = mapped_column(String(80))
    tipo: Mapped[str] = mapped_column(String(20), index=True)
    habitaciones: Mapped[int] = mapped_column(Integer, default=0)
    banos: Mapped[int] = mapped_column(Integer, default=0)
    area_m2: Mapped[float] = mapped_column(Float, default=0)
    precio: Mapped[int] = mapped_column(Integer, index=True)
    estado: Mapped[str] = mapped_column(String(20), default="disponible")
    descripcion: Mapped[str] = mapped_column(Text, default="")
    foto_url: Mapped[str] = mapped_column(String(300), default="")
    propietario: Mapped[str] = mapped_column(String(120), default="")
    creada_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    # ── Procedencia (ingesta automática) ──────────────────────────────
    fuente: Mapped[str] = mapped_column(
        String(40), default=FuentePropiedad.MANUAL.value, index=True
    )
    #: Identificador del inmueble en la fuente. Junto con `fuente` es la clave de
    #: deduplicación: reingerir el mismo aviso actualiza, no duplica.
    externo_id: Mapped[str | None] = mapped_column(String(120), index=True, default=None)
    url_origen: Mapped[str] = mapped_column(String(300), default="")
    actualizada_en: Mapped[datetime] = mapped_column(DateTime, default=ahora, onupdate=ahora)

    # ── Mandato de comercialización ───────────────────────────────────
    #: Sin mandato no hay comisión que cobrar, así que tampoco hay razón para
    #: tener el inmueble en cartera. `ingesta.ingerir` lo exige.
    mandato: Mapped[bool] = mapped_column(Boolean, default=False)
    mandato_evidencia: Mapped[str] = mapped_column(Text, default="")

    # ── Contacto del propietario (PII: cifrada en reposo, RF-17) ──────
    propietario_telefono: Mapped[str | None] = mapped_column(PII(300), default=None)
    #: Índice ciego del teléfono, para detectar el mismo dueño sin descifrar.
    propietario_telefono_hash: Mapped[str | None] = mapped_column(
        String(64), index=True, default=None
    )

    @property
    def disponible(self) -> bool:
        return self.estado == EstadoPropiedad.DISPONIBLE.value

    @property
    def pendiente(self) -> bool:
        return self.estado == EstadoPropiedad.PENDIENTE.value

    fotos: Mapped[list["FotoPropiedad"]] = relationship(
        back_populates="propiedad",
        cascade="all, delete-orphan",
        order_by="FotoPropiedad.orden, FotoPropiedad.id",
    )
    comentarios: Mapped[list["ComentarioPropiedad"]] = relationship(
        back_populates="propiedad",
        cascade="all, delete-orphan",
        order_by="ComentarioPropiedad.creado_en",
    )

    @property
    def es_referencia(self) -> bool:
        """Inmueble real de un tercero, cargado solo para probar. No vendible."""
        return self.fuente in FUENTES_SIN_MANDATO

    @property
    def portada(self) -> str:
        """Imagen de la tarjeta: la miniatura de la primera foto, o el marcador."""
        if self.fotos:
            return self.fotos[0].miniatura
        return self.foto_url or "/static/img/placeholder.svg"


class FotoPropiedad(Base):
    """Imágenes de un inmueble (RF-10).

    Se guarda solo la ruta: el archivo vive en `app/static/fotos/`. Meter
    binarios en SQLite haría crecer la base sin necesidad y complicaría los
    respaldos, que ya son delicados por el WAL.
    """

    __tablename__ = "fotos_propiedad"

    id: Mapped[int] = mapped_column(primary_key=True)
    propiedad_id: Mapped[str] = mapped_column(
        ForeignKey("propiedades.id", ondelete="CASCADE"), index=True
    )
    #: Nombre del archivo dentro de `app/static/fotos/`. Nunca es el nombre que
    #: envió el cliente: se genera aleatorio para evitar traversal y colisiones.
    archivo: Mapped[str] = mapped_column(String(120))
    orden: Mapped[int] = mapped_column(Integer, default=0)
    creada_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    propiedad: Mapped[Propiedad] = relationship(back_populates="fotos")

    @property
    def url(self) -> str:
        return f"/static/fotos/{self.archivo}"

    @property
    def miniatura(self) -> str:
        """Versión liviana, para tarjetas y galería. Convención de nombre."""
        base = self.archivo.rsplit(".", 1)[0]
        return f"/static/fotos/{base}-min.jpg"


class ComentarioPropiedad(Base):
    """Hilo de comentarios sobre un inmueble.

    Es la vía por la que una cuenta de solo lectura puede aportar sin tocar la
    cartera: pregunta o señala algo, y un operador responde en el mismo hilo.
    El texto lo escribe personal del negocio, no un titular, así que no es PII
    de un tercero y no va cifrado.
    """

    __tablename__ = "comentarios_propiedad"

    id: Mapped[int] = mapped_column(primary_key=True)
    propiedad_id: Mapped[str] = mapped_column(
        ForeignKey("propiedades.id", ondelete="CASCADE"), index=True
    )
    autor: Mapped[str] = mapped_column(String(80))
    #: Rol del autor al escribir. Se guarda en el momento porque un usuario puede
    #: cambiar de rol después, y el hilo debe leerse como ocurrió.
    rol: Mapped[str] = mapped_column(String(20), default="invitado")
    texto: Mapped[str] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    propiedad: Mapped[Propiedad] = relationship(back_populates="comentarios")

    @property
    def es_respuesta(self) -> bool:
        """Los comentarios del operador son las respuestas del hilo."""
        return self.rol == "operador"


class Prospecto(Base):
    """Perfil del prospecto (SRS §4.2). El contacto va cifrado (RF-17)."""

    __tablename__ = "prospectos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    # Canal y atribución (RF-15)
    canal: Mapped[str] = mapped_column(String(30), index=True)
    canal_id_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    campana: Mapped[str | None] = mapped_column(String(80), index=True)
    red_origen: Mapped[str | None] = mapped_column(String(40), index=True)

    # Contacto — CIFRADO EN REPOSO
    nombre: Mapped[str | None] = mapped_column(PII(400))
    telefono: Mapped[str | None] = mapped_column(PII(400))
    usuario_canal: Mapped[str | None] = mapped_column(PII(400))

    # Consentimiento (RF-16)
    consentimiento: Mapped[bool] = mapped_column(Boolean, default=False)
    consentimiento_ts: Mapped[datetime | None] = mapped_column(DateTime)

    # Slots de calificación (RF-05)
    ciudad: Mapped[str | None] = mapped_column(String(64))
    #: Municipio concreto del área metropolitana: Dosquebradas, Envigado…
    #: `ciudad` guarda la plaza de cobertura (Medellín o Pereira) porque de ella
    #: dependen las reglas duras; el municipio es lo que el comprador dijo y lo
    #: que de verdad delimita su búsqueda.
    municipio: Mapped[str | None] = mapped_column(String(64))
    zona: Mapped[str | None] = mapped_column(String(80))
    tipo: Mapped[str | None] = mapped_column(String(20))
    presupuesto_min: Mapped[int | None] = mapped_column(Integer)
    presupuesto_max: Mapped[int | None] = mapped_column(Integer)
    habitaciones: Mapped[int | None] = mapped_column(Integer)
    plazo_compra: Mapped[str | None] = mapped_column(String(60))

    # Clasificación (RF-06)
    score_intencion: Mapped[int] = mapped_column(Integer, default=0)
    etiqueta: Mapped[str] = mapped_column(String(12), default=Etiqueta.FRIO.value)

    estado: Mapped[str] = mapped_column(String(24), default=EstadoProspecto.NUEVO.value, index=True)
    notas: Mapped[str] = mapped_column(Text, default="")

    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora, onupdate=ahora)

    mensajes: Mapped[list["Mensaje"]] = relationship(
        back_populates="prospecto", cascade="all, delete-orphan", order_by="Mensaje.creado_en"
    )
    consentimientos: Mapped[list["Consentimiento"]] = relationship(
        back_populates="prospecto", cascade="all, delete-orphan"
    )
    solicitudes: Mapped[list["Solicitud"]] = relationship(
        back_populates="prospecto", cascade="all, delete-orphan"
    )
    ventas: Mapped[list["Venta"]] = relationship(back_populates="prospecto")

    @property
    def estado_enum(self) -> EstadoProspecto:
        return EstadoProspecto(self.estado)

    @property
    def datos_completos(self) -> bool:
        """Mínimo para emparejar: ciudad, tipo y al menos un extremo de presupuesto."""
        return bool(self.ciudad and self.tipo and (self.presupuesto_min or self.presupuesto_max))


class Mensaje(Base):
    """Historial conversacional. El texto es contenido del titular: va cifrado."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospecto_id: Mapped[int] = mapped_column(ForeignKey("prospectos.id", ondelete="CASCADE"), index=True)
    direccion: Mapped[str] = mapped_column(String(10))
    texto: Mapped[str] = mapped_column(PII(4000))
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    prospecto: Mapped[Prospecto] = relationship(back_populates="mensajes")


class Consentimiento(Base):
    """Registro de autorización de tratamiento de datos (RF-16, Art. 9 Ley 1581)."""

    __tablename__ = "consentimientos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospecto_id: Mapped[int] = mapped_column(ForeignKey("prospectos.id", ondelete="CASCADE"), index=True)
    otorgado: Mapped[bool] = mapped_column(Boolean, default=True)
    texto_autorizacion: Mapped[str] = mapped_column(Text)
    version_politica: Mapped[str] = mapped_column(String(20), default="1.0")
    url_politica: Mapped[str] = mapped_column(String(300), default="")
    canal: Mapped[str] = mapped_column(String(30))
    evidencia: Mapped[str] = mapped_column(String(200), default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    prospecto: Mapped[Prospecto] = relationship(back_populates="consentimientos")


class Solicitud(Base):
    """Handoff al operador: visita o asesor humano (RF-12)."""

    __tablename__ = "solicitudes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospecto_id: Mapped[int] = mapped_column(ForeignKey("prospectos.id", ondelete="CASCADE"), index=True)
    propiedad_id: Mapped[str | None] = mapped_column(ForeignKey("propiedades.id"))
    tipo: Mapped[str] = mapped_column(String(20), default="visita")  # visita | asesor
    estado: Mapped[str] = mapped_column(String(20), default="pendiente", index=True)
    detalle: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)
    atendida_en: Mapped[datetime | None] = mapped_column(DateTime)

    prospecto: Mapped[Prospecto] = relationship(back_populates="solicitudes")
    propiedad: Mapped[Propiedad | None] = relationship()


class Emparejamiento(Base):
    """Qué propiedades se le mostraron a qué prospecto (trazabilidad de atribución)."""

    __tablename__ = "emparejamientos"
    __table_args__ = (UniqueConstraint("prospecto_id", "propiedad_id", name="uq_emparejamiento"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospecto_id: Mapped[int] = mapped_column(ForeignKey("prospectos.id", ondelete="CASCADE"), index=True)
    propiedad_id: Mapped[str] = mapped_column(ForeignKey("propiedades.id"), index=True)
    puntaje: Mapped[float] = mapped_column(Float, default=0)
    frase_venta: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    propiedad: Mapped[Propiedad] = relationship()


class Venta(Base):
    """Venta confirmada por un humano y su comisión (RF-14, ADR-05)."""

    __tablename__ = "ventas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    codigo: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    prospecto_id: Mapped[int] = mapped_column(ForeignKey("prospectos.id"), index=True)
    propiedad_id: Mapped[str] = mapped_column(ForeignKey("propiedades.id"), index=True)
    canal_origen: Mapped[str] = mapped_column(String(30))
    campana_origen: Mapped[str | None] = mapped_column(String(80))
    precio_venta: Mapped[int] = mapped_column(Integer)
    comision_pct: Mapped[float] = mapped_column(Float)
    comision_valor: Mapped[int] = mapped_column(Integer)
    operador: Mapped[str] = mapped_column(String(60))
    fecha: Mapped[datetime] = mapped_column(DateTime, default=ahora)

    prospecto: Mapped[Prospecto] = relationship(back_populates="ventas")
    propiedad: Mapped[Propiedad] = relationship()


class Campana(Base):
    """Campaña de captación opt-in en una red social (ADR-01, Fase 2 del PRD).

    No almacena personas: define el enlace público con consentimiento y acumula
    métricas agregadas por red para saber qué canal genera prospectos reales.
    """

    __tablename__ = "campanas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    red: Mapped[str] = mapped_column(String(40), index=True)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    visitas: Mapped[int] = mapped_column(Integer, default=0)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=ahora)


class LogAuditoria(Base):
    """Bitácora append-only encadenada por hash (RF-18, RNF-09).

    Cada registro incluye el hash del anterior; alterar una fila rompe la cadena
    y la verificación lo detecta.
    """

    __tablename__ = "log_auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=ahora, index=True)
    actor: Mapped[str] = mapped_column(String(60), index=True)
    accion: Mapped[str] = mapped_column(String(60), index=True)
    entidad: Mapped[str] = mapped_column(String(40))
    entidad_id: Mapped[str] = mapped_column(String(40), index=True)
    detalle: Mapped[str] = mapped_column(Text, default="")
    hash_prev: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")
