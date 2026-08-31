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
    empresa_nombre: str = "Inmoclick"
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

    # Canal WhatsApp vía Evolution API (ADR-02b). Con `evolution_url` vacío el
    # canal no se monta y la app arranca igual, como ya ocurre con Telegram.
    evolution_url: str = ""
    evolution_api_key: str = ""
    evolution_instancia: str = "vvi"
    #: Segmento secreto de la ruta del webhook. Evolution no firma sus envíos
    #: con HMAC, así que la ruta impredecible es la primera capa de defensa.
    evolution_webhook_token: str = ""
    #: Host público desde el que Evolution alcanza a VVI. Vacío = DASHBOARD_URL.
    #: En desarrollo con Evolution en Docker y VVI en el host hay que poner
    #: `http://host.docker.internal:8000`: para el contenedor, `localhost` es él
    #: mismo. Es el error de configuración más común de esta integración.
    evolution_webhook_base: str = ""
    #: Pausa simulada de escritura antes de responder, en milisegundos. Un bot
    #: que contesta en 200 ms es la señal más obvia de automatización.
    evolution_delay_ms: int = 1200
    #: Lista blanca de números para pruebas, separados por coma. Con al menos uno,
    #: el bot **solo** responde a esos y calla ante cualquier otro.
    #:
    #: Es lo que hace viable probar con un número personal: mientras el teléfono
    #: esté vinculado, todo el que le escriba llega al webhook. Sin esta lista, un
    #: familiar recibiría el aviso de IA y la solicitud de autorización, y su
    #: mensaje entraría al motor. Vacía = responde a todo el mundo (producción).
    evolution_numeros_prueba: str = ""

    # Canal Instagram vía la Messaging API de Meta (ADR-02c). Con el token
    # vacío el canal no se monta y la app arranca igual, como ya ocurre con
    # Telegram y con WhatsApp.
    instagram_token: str = ""
    #: Id de la app **de Instagram** (panel → API con inicio de sesión de
    #: Instagram → Configuración de la app de Instagram). No es el id de la app
    #: de Meta que se ve arriba en el panel: son dos números distintos, y con el
    #: de Meta el inicio de sesión responde `Invalid platform app`. Solo hace
    #: falta para el flujo de conexión de `instagram_login`; el canal funciona
    #: sin él si el token se pega a mano.
    instagram_app_id: str = ""
    #: Secreto de la app **de Instagram**, que aparece junto a `instagram_app_id`
    #: en esa misma pantalla y **no es** el de la app de Meta de abajo. El canje
    #: del código exige la pareja completa: id de Instagram con secreto de
    #: Instagram. Vacío = se reutiliza `instagram_app_secret`, que es lo
    #: correcto en las apps donde Meta muestra el mismo valor en las dos partes.
    instagram_login_app_secret: str = ""
    #: Secreto de la app de Meta. Es con lo que se verifica la firma de cada
    #: webhook, y por eso no es opcional: una ruta pública que acepta eventos
    #: sin verificar es una puerta para que cualquiera inyecte conversaciones.
    instagram_app_secret: str = ""
    #: Token del handshake `hub.verify_token`. Lo elige uno y se escribe igual
    #: aquí y en el panel de la app de Meta al declarar la URL del webhook; si
    #: no coinciden, Meta ni siquiera deja guardar la suscripción.
    instagram_verify_token: str = ""
    #: Id de la cuenta profesional. Vacío = `me`, que Meta resuelve con el
    #: propio token; ponerlo explícito solo hace falta si el token cubre varias.
    instagram_cuenta_id: str = ""
    #: Host de la API. `graph.instagram.com` es el de Instagram Login (cuenta
    #: profesional que no cuelga de una página de Facebook), que es el caso de
    #: una inmobiliaria con su propio perfil. Con Facebook Login sería
    #: `https://graph.facebook.com`.
    instagram_api_base: str = "https://graph.instagram.com"
    instagram_version: str = "v23.0"
    #: Lista blanca para pruebas: IGSID numérico o `@usuario`, separados por
    #: coma. Es el mismo freno que `evolution_numeros_prueba` y existe por lo
    #: mismo: mientras la app de Meta está en desarrollo solo escriben las
    #: cuentas con rol en ella, pero aprobada la revisión el perfil atiende a
    #: todo el mundo de golpe. Vacía = responde a todo el mundo (producción).
    instagram_usuarios_prueba: str = ""

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
    #: Cuenta de solo lectura: ve la cartera y comenta, no modifica nada. No
    #: alcanza prospectos ni auditoría, que contienen datos personales.
    #: Con la contraseña vacía, la cuenta queda deshabilitada.
    invitado_user: str = "invitado"
    invitado_password: str = ""
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

    # Control de comisión (PRD §10)
    #: Meses de protección, en días: una venta al mismo comprador sobre el mismo
    #: inmueble dentro de esta ventana genera comisión aunque se cierre por
    #: fuera. Seis meses es lo habitual en corretaje; súbelo si tu contrato dice
    #: otra cosa. Cambiarlo no afecta a las presentaciones ya hechas.
    dias_proteccion: int = 180
    #: Días después de la presentación en que se le pregunta al comprador si el
    #: negocio se cerró. Vacío desactiva el seguimiento.
    dias_seguimiento_comprador: str = "7,21,45"
    #: Cada cuánto revisa el proceso si hay preguntas por enviar. Con 0 no se
    #: levanta la tarea: útil para correr la app sin que escriba a nadie.
    intervalo_seguimiento_min: int = 60

    # Reglas duras de negocio (ADR-03)
    ciudades_cobertura: tuple[str, ...] = ("Medellín", "Pereira")

    # ── Catálogo público ──────────────────────────────────────────────
    #: Vitrina pública en /inmuebles. Es la única superficie del sistema que ve
    #: alguien sin sesión, así que se puede apagar entera desde configuración.
    catalogo_publico: bool = True
    #: Deja entrar al catálogo los inmuebles SIN mandato (demo y referencia).
    #: Existe solo para poder probar en local, donde toda la cartera es `demo`.
    #: En producción publicaría inmuebles que no existen, así que además de venir
    #: apagado fuerza `noindex` y un aviso permanente en pantalla mientras esté
    #: encendido: si alguien lo deja puesto, se ve.
    catalogo_muestra_demo: bool = False
    #: Cuántos inmuebles por página en la vitrina.
    catalogo_por_pagina: int = 12
    #: Número de WhatsApp al que van las consultas de la vitrina, en formato
    #: internacional sin signos (573001234567). Es un **anulador**: si está
    #: puesto manda sobre cualquier otra cosa. Vacío —lo normal— hace que las
    #: consultas caigan en el número que atiende el asistente virtual, que es el
    #: que se vinculó escaneando el QR de Evolution.
    whatsapp_contacto: str = ""
    #: Último recurso cuando no hay asistente vinculado: el canal se cayó, el QR
    #: no se ha escaneado todavía o esta instalación no tiene WhatsApp montado.
    #: Aquí contesta una persona. Nunca se pinta en pantalla —el visitante pulsa
    #: un botón, no lee un teléfono—, así que cambiarlo no toca ninguna
    #: plantilla: el destino se resuelve en el momento del clic.
    whatsapp_respaldo: str = "573122799808"

    @property
    def url_publica(self) -> str:
        """Base para enlaces canónicos y datos estructurados. Sin barra final."""
        return self.dashboard_url.rstrip("/")

    @property
    def hitos_seguimiento(self) -> tuple[int, ...]:
        """Los días de seguimiento, ordenados y sin repetidos ni basura."""
        dias = set()
        for trozo in self.dias_seguimiento_comprador.split(","):
            trozo = trozo.strip()
            # El 0 se admite a propósito: es la forma de probar el circuito
            # completo sin esperar una semana a que venza el primer hito.
            if trozo.isdigit():
                dias.add(int(trozo))
        return tuple(sorted(dias))

    @property
    def tiene_whatsapp(self) -> bool:
        return bool(self.evolution_url and self.evolution_api_key and self.evolution_webhook_token)

    @property
    def tiene_instagram(self) -> bool:
        """Los tres a la vez, no solo el token.

        Con token pero sin app secret el canal podría escribir y no recibir: el
        webhook rechaza todo lo que no pueda verificar. Un canal que solo habla
        no es un canal, así que se considera no configurado y se dice al
        arrancar, en vez de quedar mudo sin que nada lo explique.
        """
        return bool(
            self.instagram_token and self.instagram_app_secret and self.instagram_verify_token
        )

    @property
    def secreto_login_instagram(self) -> str:
        """El secreto que hace pareja con `instagram_app_id`, con su respaldo."""
        return self.instagram_login_app_secret or self.instagram_app_secret

    @property
    def tiene_login_instagram(self) -> bool:
        """¿Se puede conectar la cuenta por OAuth, sin pegar el token a mano?

        Es independiente de `tiene_instagram`: al conectar por primera vez
        todavía no hay token, y justo por eso se entra aquí.
        """
        return bool(self.instagram_app_id and self.secreto_login_instagram)

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
