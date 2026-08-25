"""Las URLs que hay que pegar en el panel de Meta, y el enlace para conectar.

El panel de la app pide cuatro URLs repartidas en tres pantallas distintas, y
todas tienen que apuntar al mismo host público con el que Meta alcanza a VVI.
Escribirlas a mano es donde se pierde media tarde: una barra final de más en la
«URL de redireccionamiento» y el canje del código falla con un mensaje que no
dice cuál de las tres copias difiere.

Esto las imprime ya armadas a partir de `DASHBOARD_URL`, avisa de lo que Meta va
a rechazar antes de que lo rechace, y da el enlace con el que se conecta la
cuenta.

Uso:

    python deploy/instagram/conectar.py            # ver las URLs
    python deploy/instagram/conectar.py --abrir    # y abrir el navegador

Con VVI corriendo y alcanzable desde internet en `DASHBOARD_URL`. Para trabajar
sin Meta de por medio, el otro script:  python deploy/instagram/probar_local.py
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))

from app.channels import instagram_bot, instagram_login  # noqa: E402
from app.config import settings  # noqa: E402


def revisar() -> list[str]:
    """Lo que Meta va a rechazar, dicho antes de intentarlo."""
    problemas: list[str] = []
    partes = urlparse(settings.url_publica)

    if partes.scheme != "https":
        problemas.append(
            f"DASHBOARD_URL es {settings.url_publica!r}. Meta exige HTTPS en la URL de "
            "redireccionamiento y no admite http:// ni localhost. Necesitas el host "
            "público —el túnel o el despliegue— con el que ya verificaste el webhook."
        )
    elif partes.hostname in ("localhost", "127.0.0.1"):
        problemas.append("DASHBOARD_URL apunta a la propia máquina: Meta no puede llegar ahí.")

    if not settings.instagram_app_id:
        problemas.append(
            "Falta INSTAGRAM_APP_ID. Es el id de la app de INSTAGRAM (panel → API con "
            "inicio de sesión de Instagram → Configuración de la app de Instagram), no "
            "el id de la app de Meta que se ve arriba del panel."
        )
    if not settings.secreto_login_instagram:
        problemas.append(
            "Falta el secreto de la app: pon INSTAGRAM_LOGIN_APP_SECRET (el que sale "
            "junto al app id de Instagram) o, si Meta muestra ahí el mismo valor, basta "
            "con INSTAGRAM_APP_SECRET."
        )
    if "ejemplo.com" in settings.politica_privacidad_url:
        problemas.append(
            "POLITICA_PRIVACIDAD_URL sigue siendo la de ejemplo. Meta la abre durante la "
            "revisión y devuelve la solicitud si no carga una política real."
        )
    return problemas


def main() -> int:
    # La consola de Windows llega en cp1252 y las URLs se imprimen entre reglas
    # y viñetas que no existen en esa tabla. Igual que en `probar_local`.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--abrir", action="store_true", help="abre el enlace de conexión")
    args = p.parse_args()

    print()
    print("  PARA PEGAR EN EL PANEL DE META")
    print("  ─────────────────────────────────────────────────────────────────")
    for etiqueta, url in (
        ("Webhook · URL de devolución de llamada", instagram_bot.url_webhook()),
        ("Inicio de sesión · URL de redireccionamiento", instagram_login.url_redireccion()),
        ("Revisión · Cancelación de autorización", instagram_login.url_desautorizacion()),
        ("Revisión · Solicitud de eliminación de datos", instagram_login.url_eliminacion()),
        ("Revisión · Política de privacidad", settings.politica_privacidad_url),
    ):
        print(f"  {etiqueta}\n    {url}\n")

    print("  PARA CONECTAR LA CUENTA (ábrelo tú, no va al panel)")
    print("  ─────────────────────────────────────────────────────────────────")
    print(f"    {instagram_login.url_login()}\n")
    print(f"  Permisos que pedirá: {', '.join(instagram_login.PERMISOS)}")
    print("  Al terminar, la página muestra el token de 60 días para el .env.\n")

    problemas = revisar()
    if problemas:
        print("  REVISA ESTO ANTES")
        print("  ─────────────────────────────────────────────────────────────────")
        for problema in problemas:
            print(f"  · {problema}\n")
        return 1

    if args.abrir:
        print("  Abriendo el navegador…\n")
        webbrowser.open(instagram_login.url_login())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
