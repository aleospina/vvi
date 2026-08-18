"""Vincula el número de WhatsApp desde la consola (ADR-02b).

Lo mismo que hace la pantalla `/dashboard/whatsapp`, para cuando el dashboard no
está a mano o se está montando el entorno por primera vez. La lógica vive en
`app.channels.whatsapp_evo`: aquí solo está la envoltura de línea de comandos.

    python deploy/evolution/configurar.py            # crea, apunta el webhook y saca el QR
    python deploy/evolution/configurar.py --estado   # solo consulta la conexión
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import webbrowser
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))

from app.channels import whatsapp_evo  # noqa: E402
from app.config import settings  # noqa: E402


def _verificar_config() -> None:
    faltan = [
        nombre
        for nombre, valor in (
            ("EVOLUTION_URL", settings.evolution_url),
            ("EVOLUTION_API_KEY", settings.evolution_api_key),
            ("EVOLUTION_WEBHOOK_TOKEN", settings.evolution_webhook_token),
        )
        if not valor
    ]
    if faltan:
        sys.exit(
            "Falta configurar en .env: " + ", ".join(faltan) + "\n"
            'Genera los secretos con: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )


def _avisar_de_la_base_del_webhook() -> None:
    """El error de configuración más común de toda la integración."""
    base = whatsapp_evo.base_webhook()
    if "localhost" in base or "127.0.0.1" in base:
        print(
            "⚠️  El webhook apunta a "
            f"{base}. Si Evolution corre en Docker, para el contenedor eso es él\n"
            "    mismo y no te llegará ningún mensaje. Pon en .env:\n"
            '    EVOLUTION_WEBHOOK_BASE="http://host.docker.internal:8000"\n'
        )


def _mostrar_qr() -> None:
    datos = whatsapp_evo.qr_de_conexion()

    if codigo := datos.get("pairingCode"):
        print(f"\n  Código de pareo: {codigo}\n")

    uri = whatsapp_evo.qr_data_uri(datos)
    if not uri:
        print("· Sin QR: la instancia probablemente ya está conectada.")
        return

    destino = Path(__file__).parent / "qr.png"
    destino.write_bytes(base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", uri)))
    print(f"✓ QR guardado en {destino}")
    print("  Escanéalo: WhatsApp → Ajustes → Dispositivos vinculados → Vincular dispositivo")
    webbrowser.open(destino.as_uri())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estado", action="store_true", help="solo consultar la conexión")
    args = parser.parse_args()

    _verificar_config()
    try:
        if args.estado:
            print(f"Estado: {whatsapp_evo.estado_conexion()}")
            return

        _avisar_de_la_base_del_webhook()
        print(f"✓ Instancia '{settings.evolution_instancia}': {whatsapp_evo.crear_instancia()}")
        print(f"✓ Webhook apuntando a {whatsapp_evo.configurar_webhook()}")
        _mostrar_qr()
    except httpx.ConnectError:
        sys.exit(
            f"No hay nadie escuchando en {settings.evolution_url}.\n"
            "Levanta Evolution:  docker compose --env-file .env -f deploy/evolution/docker-compose.yml up -d"
        )
    except httpx.HTTPStatusError as e:
        sys.exit(f"Evolution respondió {e.response.status_code}: {e.response.text[:400]}")


if __name__ == "__main__":
    main()
