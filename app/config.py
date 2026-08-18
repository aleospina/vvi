"""Configuración del sistema. Todos los secretos vienen de variables de entorno (RNF-05)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=RAIZ / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identidad del negocio
    empresa_nombre: str = "Inmobiliaria Demo"
    politica_privacidad_url: str = "https://ejemplo.com/politica-de-tratamiento-de-datos"
    comision_pct: float = 0.03

    # Seguridad
    fernet_key: str = ""
    hmac_key: str = ""

    # Almacenamiento
    database_url: str = f"sqlite:///{(RAIZ / 'data' / 'vvi.db').as_posix()}"
    #: Dónde se guardan las fotos de los inmuebles. Vacío = junto al código.
    #: En un despliegue hay que apuntarlo al volumen persistente, o cada
    #: publicación borraría las imágenes de los propietarios.
    fotos_dir: str = ""

    # Canal
    telegram_bot_token: str = ""

    # LLM
    llm_provider: str = "kimi"  # kimi | claude | reglas
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.ai/v1"
    moonshot_model: str = "kimi-k2.6"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    # Dashboard
    dashboard_user: str = "operador"
    dashboard_password: str = "cambia-esta-clave"
    #: URL pública del dashboard, para los enlaces que van en las notificaciones.
    dashboard_url: str = "http://127.0.0.1:8000"

    # Notificación al asesor humano cuando entra una solicitud (RF-12)
    notificaciones_activas: bool = True
    #: chat_id numérico del asesor en Telegram. Se obtiene escribiéndole /chatid
    #: al bot desde la cuenta que debe recibir los avisos.
    asesor_telegram_chat_id: str = ""
    asesor_email: str = ""
    #: SMTP para el aviso por correo. Sin host configurado, el correo se omite
    #: (el aviso por Telegram sigue funcionando por su cuenta).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_usuario: str = ""
    smtp_clave: str = ""
    smtp_desde: str = ""
    smtp_tls: bool = True

    # Captación opt-in
    meta_verify_token: str = "cambia-este-token"
    mercadolibre_shared_secret: str = ""

    # Cumplimiento y operación
    retencion_dias: int = 365
    dias_alerta_seguimiento: int = 7

    # Reglas duras de negocio (ADR-03)
    ciudades_cobertura: tuple[str, ...] = ("Medellín", "Pereira")

    @property
    def tiene_llm(self) -> bool:
        return bool(self.moonshot_api_key or self.anthropic_api_key)

    @property
    def ruta_fotos(self) -> Path:
        """Directorio de fotos ya resuelto, con el valor por defecto aplicado."""
        return Path(self.fotos_dir) if self.fotos_dir else RAIZ / "app" / "static" / "fotos"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
