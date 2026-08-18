"""Cliente LLM dual (ADR-06).

Primario: Kimi K2.6 vía API compatible con OpenAI (`api.moonshot.ai/v1`).
Fallback: Claude (Anthropic API).
Si ninguno está configurado, el sistema sigue funcionando solo con reglas
(ver `app/services/nlu_engine.py`), lo que permite demostrar el MVP sin llaves.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.llm.prompts import (
    ESQUEMA_CLASIFICADOR,
    ESQUEMA_INMUEBLE,
    sistema_clasificador,
    sistema_extractor_inmueble,
    sistema_pitch,
    usuario_clasificador,
    usuario_extractor_inmueble,
)

log = logging.getLogger(__name__)

_MAX_TOKENS = 2048


class LLMNoDisponible(RuntimeError):
    """Ningún proveedor pudo responder; el llamador debe degradar a reglas."""


def _extraer_json(texto: str) -> dict[str, Any]:
    """Parsea la respuesta del modelo tolerando envolturas accidentales."""
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto, flags=re.DOTALL)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        inicio, fin = texto.find("{"), texto.rfind("}")
        if inicio != -1 and fin > inicio:
            return json.loads(texto[inicio : fin + 1])
        raise


class ClienteLLM:
    """Orquesta proveedores con degradación ordenada."""

    def __init__(self) -> None:
        self._kimi = None
        self._anthropic = None

    # ── Construcción perezosa de los SDK ──────────────────────────────

    @property
    def kimi(self):
        if self._kimi is None and settings.moonshot_api_key:
            from openai import OpenAI  # Moonshot es compatible con la API de OpenAI

            self._kimi = OpenAI(
                api_key=settings.moonshot_api_key,
                base_url=settings.moonshot_base_url,
                timeout=20.0,
                max_retries=1,
            )
        return self._kimi

    @property
    def anthropic(self):
        if self._anthropic is None and settings.anthropic_api_key:
            import anthropic

            self._anthropic = anthropic.Anthropic(
                api_key=settings.anthropic_api_key, timeout=20.0, max_retries=1
            )
        return self._anthropic

    @property
    def disponible(self) -> bool:
        return bool(settings.moonshot_api_key or settings.anthropic_api_key)

    def _orden_proveedores(self) -> list[str]:
        preferido = settings.llm_provider.lower()
        orden = ["claude", "kimi"] if preferido == "claude" else ["kimi", "claude"]
        if preferido == "reglas":
            return []
        return [p for p in orden if (p == "kimi" and settings.moonshot_api_key)
                or (p == "claude" and settings.anthropic_api_key)]

    # ── Llamadas por proveedor ────────────────────────────────────────

    def _completar_kimi(self, sistema: str, usuario: str) -> dict[str, Any]:
        respuesta = self.kimi.chat.completions.create(
            model=settings.moonshot_model,
            max_tokens=_MAX_TOKENS,
            temperature=0.2,  # baja para clasificación (SRS §5.1)
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
        )
        return _extraer_json(respuesta.choices[0].message.content or "")

    def _completar_claude(
        self, sistema: str, usuario: str, esquema: dict | None
    ) -> dict[str, Any]:
        formato = (
            {"type": "json_schema", "schema": esquema}
            if esquema
            else {"type": "json_schema", "schema": {"type": "object", "additionalProperties": True}}
        )
        parametros: dict[str, Any] = {
            "model": settings.anthropic_model,
            "max_tokens": _MAX_TOKENS,
            # El prompt de sistema es estable: se cachea para bajar el costo por
            # conversación (RNF-07).
            "system": [
                {"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": usuario}],
            # Clasificar y responder en pocas frases no necesita razonamiento
            # extendido; esfuerzo bajo mantiene la latencia bajo el objetivo
            # de 5 s p95 (RNF-01). La salida estructurada garantiza el formato.
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low", "format": formato},
        }

        import anthropic

        try:
            respuesta = self.anthropic.beta.messages.create(
                **parametros,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.BadRequestError, TypeError) as exc:
            # La cuenta no tiene habilitado el fallback del lado del servidor, o
            # el SDK instalado aún no conoce el parámetro. Seguimos por la ruta
            # estándar en vez de perder la conversación.
            log.warning("Fallback del servidor no disponible (%s); usando ruta estándar.", exc)
            respuesta = self.anthropic.messages.create(**parametros)

        if respuesta.stop_reason == "refusal":
            categoria = getattr(getattr(respuesta, "stop_details", None), "category", None)
            raise LLMNoDisponible(f"Claude rechazó la solicitud (categoría: {categoria})")

        texto = next((b.text for b in respuesta.content if b.type == "text"), "")
        return _extraer_json(texto)

    def _completar(self, sistema: str, usuario: str, esquema: dict | None = None) -> dict[str, Any]:
        errores: list[str] = []
        for proveedor in self._orden_proveedores():
            try:
                if proveedor == "kimi":
                    return self._completar_kimi(sistema, usuario)
                return self._completar_claude(sistema, usuario, esquema)
            except Exception as exc:  # noqa: BLE001 - degradamos a propósito
                log.warning("Proveedor %s falló: %s", proveedor, exc)
                errores.append(f"{proveedor}: {exc}")
        raise LLMNoDisponible("; ".join(errores) or "sin proveedores configurados")

    # ── API pública ───────────────────────────────────────────────────

    def clasificar(
        self, historial: list[dict], perfil: dict, cartera: str = ""
    ) -> dict[str, Any]:
        """Extrae slots, calcula score y propone respuesta (RF-05/06)."""
        return self._completar(
            sistema_clasificador(),
            usuario_clasificador(historial, perfil, cartera),
            ESQUEMA_CLASIFICADOR,
        )

    def extraer_inmueble(self, texto: str) -> dict[str, Any]:
        """Extrae los campos de un aviso en texto libre (RF-10).

        El LLM solo lee y estructura; no decide si el inmueble entra a la
        cartera. Eso lo resuelve `ingesta.validar`, que descarta lo incompleto
        o fuera de cobertura, y después un operador lo aprueba.
        """
        return self._completar(
            sistema_extractor_inmueble(),
            usuario_extractor_inmueble(texto),
            ESQUEMA_INMUEBLE,
        )

    def frases_venta(self, propiedades: list[dict], perfil: dict) -> dict[str, str]:
        """Una frase honesta por inmueble (RF-11). Devuelve {id: frase}."""
        esquema = {
            "type": "object",
            "properties": {
                "frases": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "frase": {"type": "string"}},
                        "required": ["id", "frase"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["frases"],
            "additionalProperties": False,
        }
        usuario = (
            f"PERFIL DEL COMPRADOR: {json.dumps(perfil, ensure_ascii=False, default=str)}\n\n"
            f"FICHAS REALES:\n{json.dumps(propiedades, ensure_ascii=False, indent=2)}"
        )
        datos = self._completar(sistema_pitch(), usuario, esquema)
        return {f["id"]: f["frase"] for f in datos.get("frases", []) if f.get("id")}


cliente = ClienteLLM()
