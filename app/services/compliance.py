"""Consentimiento y auditoría (SRS §2.6 · RF-16, RF-18, RF-19, RF-20).

Reglas que este módulo hace cumplir:
  1. Sin consentimiento vigente no se persiste PII ni se despacha nada saliente.
  2. Toda acción sobre datos personales queda en una bitácora append-only
     encadenada por hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Consentimiento, LogAuditoria, Prospecto, ahora

VERSION_POLITICA = "1.0"


def texto_consentimiento() -> str:
    """Texto exacto de la autorización que se muestra y se archiva (Art. 9 Ley 1581)."""
    return (
        f"Autorizo a {settings.empresa_nombre} a recolectar, almacenar y usar mis datos "
        "de contacto (nombre, teléfono/usuario) con la finalidad exclusiva de mostrarme "
        "inmuebles que se ajusten a mi búsqueda y contactarme para agendar visitas o "
        "asesoría comercial. Conozco que puedo consultar, actualizar o solicitar la "
        "supresión de mis datos en cualquier momento, y que el tratamiento se rige por "
        f"la política de privacidad publicada en {settings.politica_privacidad_url}."
    )


def aviso_ia() -> str:
    """Aviso de transparencia: el interlocutor es una IA (RF-04)."""
    return (
        f"👋 Hola, soy el *asistente virtual con IA* de {settings.empresa_nombre}. "
        "No soy una persona. Te ayudo a encontrar casa, apartamento o lote en "
        f"{' o '.join(settings.ciudades_cobertura)}."
    )


# ─────────────────────────── Auditoría ───────────────────────────


def _ts_canonico(ts: datetime) -> str:
    """Representación estable de la fecha, idéntica antes y después de SQLite."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")


def _huella(registro: dict, hash_prev: str) -> str:
    payload = json.dumps(registro, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{hash_prev}|{payload}".encode("utf-8")).hexdigest()


def auditar(
    db: Session,
    *,
    actor: str,
    accion: str,
    entidad: str,
    entidad_id: str | int,
    detalle: str = "",
) -> LogAuditoria:
    """Escribe una entrada encadenada en la bitácora (RF-18)."""
    previo = db.scalar(select(LogAuditoria).order_by(desc(LogAuditoria.id)).limit(1))
    hash_prev = previo.hash if previo else ""

    entrada = LogAuditoria(
        ts=ahora(),
        actor=actor,
        accion=accion,
        entidad=entidad,
        entidad_id=str(entidad_id),
        detalle=detalle,
        hash_prev=hash_prev,
    )
    entrada.hash = _huella(
        {
            "ts": _ts_canonico(entrada.ts),
            "actor": actor,
            "accion": accion,
            "entidad": entidad,
            "entidad_id": str(entidad_id),
            "detalle": detalle,
        },
        hash_prev,
    )
    db.add(entrada)
    db.flush()
    return entrada


def verificar_cadena(db: Session) -> tuple[bool, int | None]:
    """Recorre la bitácora y valida la cadena de hashes.

    Devuelve (íntegra, id_del_primer_registro_roto).
    """
    hash_prev = ""
    for e in db.scalars(select(LogAuditoria).order_by(LogAuditoria.id)):
        esperado = _huella(
            {
                "ts": _ts_canonico(e.ts),
                "actor": e.actor,
                "accion": e.accion,
                "entidad": e.entidad,
                "entidad_id": e.entidad_id,
                "detalle": e.detalle,
            },
            hash_prev,
        )
        if e.hash_prev != hash_prev or e.hash != esperado:
            return False, e.id
        hash_prev = e.hash
    return True, None


# ─────────────────────────── Consentimiento ───────────────────────────


def registrar_consentimiento(
    db: Session,
    prospecto: Prospecto,
    *,
    canal: str,
    evidencia: str = "",
    otorgado: bool = True,
) -> Consentimiento:
    """Archiva la autorización con su texto, timestamp y canal (RF-16)."""
    registro = Consentimiento(
        prospecto_id=prospecto.id,
        otorgado=otorgado,
        texto_autorizacion=texto_consentimiento(),
        version_politica=VERSION_POLITICA,
        url_politica=settings.politica_privacidad_url,
        canal=canal,
        evidencia=evidencia,
    )
    db.add(registro)

    prospecto.consentimiento = otorgado
    prospecto.consentimiento_ts = ahora() if otorgado else None
    db.flush()

    auditar(
        db,
        actor=f"titular:{prospecto.codigo}",
        accion="consentimiento_otorgado" if otorgado else "consentimiento_revocado",
        entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle=f"canal={canal} politica=v{VERSION_POLITICA}",
    )
    return registro


def tiene_consentimiento_vigente(prospecto: Prospecto) -> bool:
    """True si el titular autorizó y la autorización no ha caducado (RNF-06)."""
    if not prospecto.consentimiento or not prospecto.consentimiento_ts:
        return False
    ts = prospecto.consentimiento_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts <= timedelta(days=settings.retencion_dias)


class ConsentimientoRequerido(PermissionError):
    """Se intentó tratar datos o contactar sin autorización vigente (RF-19)."""


def exigir_consentimiento(prospecto: Prospecto) -> None:
    if not tiene_consentimiento_vigente(prospecto):
        raise ConsentimientoRequerido(
            f"El prospecto {prospecto.codigo} no tiene consentimiento vigente; "
            "no se permite contactarlo ni tratar sus datos."
        )


def revocar_y_anonimizar(db: Session, prospecto: Prospecto, actor: str = "titular") -> None:
    """Habeas data: revoca el consentimiento y borra la PII (RF-20).

    Se conservan los campos no identificatorios (ciudad, presupuesto, estado) y
    la bitácora, que es la evidencia de cumplimiento ante la SIC.
    """
    prospecto.nombre = None
    prospecto.telefono = None
    prospecto.usuario_canal = None
    prospecto.canal_id_hash = None
    prospecto.consentimiento = False
    prospecto.consentimiento_ts = None
    prospecto.notas = (prospecto.notas or "") + "\n[Datos suprimidos a solicitud del titular]"
    for m in prospecto.mensajes:
        m.texto = "[suprimido]"
    db.flush()

    auditar(
        db,
        actor=actor,
        accion="supresion_datos",
        entidad="prospecto",
        entidad_id=prospecto.codigo,
        detalle="PII suprimida y consentimiento revocado a solicitud del titular",
    )
