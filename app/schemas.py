"""Contratos de la API pública (Pydantic)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import Canal, TipoInmueble, TipoNegocio


class PropiedadIn(BaseModel):
    id: str | None = None
    ciudad: str
    zona: str = ""
    tipo: TipoInmueble
    negocio: TipoNegocio = TipoNegocio.VENTA
    habitaciones: int = 0
    banos: int = 0
    area_m2: float = 0
    precio: int = Field(gt=0)
    descripcion: str = ""
    foto_url: str = ""
    propietario: str = ""


class PropiedadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ciudad: str
    zona: str
    tipo: str
    negocio: str
    habitaciones: int
    banos: int
    area_m2: float
    precio: int
    estado: str
    descripcion: str
    foto_url: str


class LeadManualIn(BaseModel):
    """Carga de un inbound que llegó por fuera del bot (RF-02).

    `consentimiento` debe ser True y `evidencia` describir cómo se obtuvo:
    sin eso el sistema rechaza el alta (ADR-01).
    """

    red: str = Field(description="instagram | facebook | marketplace | olx | mercado_libre | web")
    canal_id: str = Field(description="Identificador del contacto en su plataforma de origen")
    consentimiento: bool
    evidencia: str = Field(min_length=5, description="Cómo autorizó el titular")
    nombre: str | None = None
    telefono: str | None = None
    usuario_canal: str | None = None
    campana: str | None = None
    mensaje: str | None = Field(default=None, description="Texto con el que el comprador inició")


class MensajeIn(BaseModel):
    """Turno conversacional manual sobre un prospecto ya existente."""

    canal: Canal = Canal.MANUAL
    canal_id: str
    texto: str = Field(min_length=1)


class ProspectoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo: str
    canal: str
    red_origen: str | None
    campana: str | None
    consentimiento: bool
    consentimiento_ts: datetime | None
    ciudad: str | None
    zona: str | None
    tipo: str | None
    presupuesto_min: int | None
    presupuesto_max: int | None
    habitaciones: int | None
    plazo_compra: str | None
    score_intencion: int
    etiqueta: str
    estado: str
    creado_en: datetime


class RespuestaBot(BaseModel):
    prospecto: str
    estado: str
    score_intencion: int
    etiqueta: str
    textos: list[str]
    propiedades: list[str] = []
    handoff: bool = False


class VentaIn(BaseModel):
    codigo_prospecto: str
    propiedad_id: str
    precio_venta: int = Field(gt=0)
    operador: str = Field(min_length=2)


class VentaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    codigo: str
    propiedad_id: str
    canal_origen: str
    campana_origen: str | None
    precio_venta: int
    comision_pct: float
    comision_valor: int
    operador: str
    fecha: datetime
