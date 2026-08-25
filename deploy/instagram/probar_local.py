"""Conversa con el bot por el canal de Instagram sin tocar Instagram (ADR-02c).

Meta exige una URL pública con HTTPS para mandar webhooks, así que probar el
canal contra la cuenta real implica montar un túnel y esperar la revisión de la
app. Eso sirve para la prueba final, pero no para trabajar: cada cambio de una
plantilla no puede costar un despliegue.

Esto levanta las dos puntas que faltan y deja el circuito cerrado en la máquina:

  · **La entrada.** Firma cada mensaje con `INSTAGRAM_APP_SECRET` exactamente
    como lo firma Meta —HMAC-SHA256 sobre el cuerpo crudo— y lo empuja al
    webhook local. Es el mismo camino que recorre un DM de verdad: firma,
    deduplicación, lista blanca, consentimiento y turno conversacional.

  · **La salida.** Un servidor mínimo que hace de `graph.instagram.com`: recibe
    lo que el bot envía y lo imprime. Apuntando `INSTAGRAM_API_BASE` aquí, se ve
    en pantalla lo que le llegaría al comprador —troceado en mensajes de 1000
    bytes y sin Markdown, como lo verá de verdad—.

Uso:

    python deploy/instagram/probar_local.py --revisar   # ¿está todo en su sitio?
    python deploy/instagram/probar_local.py             # conversar

Con VVI corriendo aparte (`python run.py`). Dentro de la conversación, `/ayuda`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import settings  # noqa: E402

#: Puerto del falso `graph.instagram.com`. No es el 8000 porque ahí está VVI.
PUERTO_SALIDA = 8099

#: Quién escribe, mientras no se cambie con `/quien`. El IGSID es un número
#: largo como el que manda Meta; el usuario es lo que devolvería su perfil.
IGSID = "17841400000000042"
USUARIO = "comprador.pruebas"
NOMBRE = "Comprador de Pruebas"


# ─────────────────────────── Salida: el falso Meta ───────────────────────────


class _Recibidos:
    """Lo que el bot ha ido enviando, compartido entre el servidor y la consola."""

    def __init__(self) -> None:
        self.total = 0
        self.ultimo = 0.0
        self._candado = threading.Lock()

    def anotar(self) -> None:
        with self._candado:
            self.total += 1
            self.ultimo = time.monotonic()


RECIBIDOS = _Recibidos()


def _pintar_salida(cuerpo: dict) -> None:
    """Imprime un mensaje del bot tal como lo recibiría el comprador."""
    mensaje = cuerpo.get("message") or {}
    texto = mensaje.get("text")
    if not texto:
        return  # `sender_action`: el "escribiendo…", que no se ve como mensaje

    etiqueta = f"  [{cuerpo['tag']}]" if cuerpo.get("tag") else ""
    bytes_ = len(texto.encode("utf-8"))
    print(f"\n🤖 ({bytes_} bytes){etiqueta}")
    for linea in texto.splitlines():
        print(f"   {linea}")

    botones = mensaje.get("quick_replies") or []
    if botones:
        print("   " + "   ".join(f"[ {b.get('title', '')} ]" for b in botones))
    RECIBIDOS.anotar()


class _ManejadorMeta(BaseHTTPRequestHandler):
    """Las tres llamadas que `instagram_bot` le hace a Meta, y nada más."""

    def do_POST(self) -> None:  # noqa: N802 - lo impone BaseHTTPRequestHandler
        largo = int(self.headers.get("content-length") or 0)
        crudo = self.rfile.read(largo)
        try:
            cuerpo = json.loads(crudo or b"{}")
        except ValueError:
            cuerpo = {}
        if self.path.endswith("/messages") and isinstance(cuerpo, dict):
            _pintar_salida(cuerpo)
        self._responder({"message_id": f"mid.local.{RECIBIDOS.total}"})

    def do_GET(self) -> None:  # noqa: N802
        # `/me` es el diagnóstico del canal; `/{igsid}` es el perfil de quien
        # escribe. Instagram no entrega ni teléfono ni correo: solo esto.
        if urlparse(self.path).path.rstrip("/").endswith("/me"):
            self._responder({"user_id": "17841400000000000", "username": "inmobiliaria.pruebas"})
        else:
            self._responder({"name": NOMBRE, "username": USUARIO})

    def _responder(self, datos: dict) -> None:
        cuerpo = json.dumps(datos).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, formato: str, *args) -> None:
        """Silencio: el log de acceso taparía la conversación."""


def _levantar_salida(puerto: int) -> HTTPServer:
    servidor = HTTPServer(("127.0.0.1", puerto), _ManejadorMeta)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


# ─────────────────────────── Entrada: el webhook firmado ───────────────────────────


def _firmar(crudo: bytes) -> str:
    return "sha256=" + hmac.new(
        settings.instagram_app_secret.encode("utf-8"), crudo, hashlib.sha256
    ).hexdigest()


def _evento(igsid: str, *, texto: str | None = None, boton: str | None = None) -> dict:
    mensaje: dict = {"mid": f"mid.local.{time.time_ns()}"}
    if texto is not None:
        mensaje["text"] = texto
    if boton is not None:
        mensaje["quick_reply"] = {"payload": boton}
    return {
        "object": "instagram",
        "entry": [
            {
                "id": "17841400000000000",
                "time": int(time.time()),
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": "17841400000000000"},
                        "timestamp": int(time.time() * 1000),
                        "message": mensaje,
                    }
                ],
            }
        ],
    }


def _empujar(vvi: str, evento: dict) -> None:
    """Manda el evento al webhook local, firmado como lo firmaría Meta."""
    crudo = json.dumps(evento).encode("utf-8")
    r = httpx.post(
        f"{vvi.rstrip('/')}/webhooks/instagram",
        content=crudo,
        headers={"content-type": "application/json", "x-hub-signature-256": _firmar(crudo)},
        timeout=30.0,
    )
    if r.status_code == 404:
        sys.exit(
            "\nEl webhook devolvió 404: la firma no cuadra.\n"
            "El INSTAGRAM_APP_SECRET de este proceso y el de VVI tienen que ser el mismo;\n"
            "si acabas de cambiarlo en .env, reinicia `python run.py`."
        )
    r.raise_for_status()


def _esperar_respuesta(espera: float = 25.0, silencio: float = 1.0) -> None:
    """Deja que el turno termine.

    VVI contesta 200 de inmediato y atiende el mensaje en segundo plano (Meta
    reintenta si el webhook demora), así que las respuestas llegan después. Se
    espera a la primera y luego a que pasen unos segundos sin más: un listado
    llega troceado en varios mensajes y cortar en el primero mostraría media
    respuesta.
    """
    antes = RECIBIDOS.total
    limite = time.monotonic() + espera
    while time.monotonic() < limite:
        time.sleep(0.15)
        if RECIBIDOS.total > antes and time.monotonic() - RECIBIDOS.ultimo > silencio:
            return
    if RECIBIDOS.total == antes:
        print(
            "\n(sin respuesta; si el mensaje venía de una cuenta fuera de "
            "INSTAGRAM_USUARIOS_PRUEBA, el silencio es lo correcto)"
        )


# ─────────────────────────── Comprobaciones ───────────────────────────


def _revisar(vvi: str, puerto: int) -> bool:
    """Todo lo que tiene que estar en su sitio, dicho de una vez."""
    ok = True

    faltan = [
        nombre
        for nombre, valor in (
            ("INSTAGRAM_TOKEN", settings.instagram_token),
            ("INSTAGRAM_APP_SECRET", settings.instagram_app_secret),
            ("INSTAGRAM_VERIFY_TOKEN", settings.instagram_verify_token),
        )
        if not valor
    ]
    if faltan:
        ok = False
        print("✗ Falta en .env: " + ", ".join(faltan))
        print("    Para probar en local sirve cualquier valor; no tiene que ser de Meta:")
        print('    INSTAGRAM_TOKEN="local"')
        print('    INSTAGRAM_APP_SECRET="local-secreto"')
        print('    INSTAGRAM_VERIFY_TOKEN="local-verificacion"')
    else:
        print("✓ Canal configurado (token, app secret y verify token).")

    esperada = f"http://127.0.0.1:{puerto}"
    if settings.instagram_api_base.rstrip("/") != esperada:
        ok = False
        print(f"✗ INSTAGRAM_API_BASE apunta a {settings.instagram_api_base}")
        print(f'    Para probar en local tiene que ser:  INSTAGRAM_API_BASE="{esperada}"')
        print("    Si no, el bot le escribiría a Meta de verdad y aquí no verías nada.")
    else:
        print(f"✓ La salida del bot va al simulador ({esperada}).")

    try:
        r = httpx.get(
            f"{vvi.rstrip('/')}/webhooks/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.instagram_verify_token,
                "hub.challenge": "prueba-local",
            },
            timeout=10.0,
        )
        if r.status_code == 200 and r.text == "prueba-local":
            print("✓ El handshake de verificación responde (es el que hace Meta al guardar).")
        else:
            ok = False
            print(f"✗ El handshake respondió {r.status_code}: revisa INSTAGRAM_VERIFY_TOKEN.")
    except httpx.HTTPError:
        ok = False
        print(f"✗ No hay nadie en {vvi}. Levanta VVI con:  python run.py")

    permitidos = _lista_blanca()
    if permitidos:
        print(f"· Lista de pruebas activa: solo responde a {', '.join(sorted(permitidos))}.")
        if IGSID not in permitidos and USUARIO not in permitidos:
            print(f"    El comprador simulado ({USUARIO}) NO está en ella: el bot callará.")
    else:
        print("· Sin lista de pruebas: responde a cualquiera (modo producción).")

    return ok


def _lista_blanca() -> frozenset[str]:
    from app.services import ajustes

    return ajustes.usuarios_prueba()


def _cartera() -> None:
    """Qué hay hoy para ofrecer. Sin esto, un 'no encontré nada' parece un error."""
    from sqlalchemy import func, select

    from app.db import sesion
    from app.models import Propiedad
    from app.services.geografia import plaza_de

    with sesion() as db:
        filas = db.execute(
            select(Propiedad.ciudad, Propiedad.tipo, Propiedad.negocio, func.count())
            .where(Propiedad.estado == "disponible")
            .group_by(Propiedad.ciudad, Propiedad.tipo, Propiedad.negocio)
            .order_by(Propiedad.ciudad, Propiedad.tipo)
        ).all()

    if not filas:
        print("\n⚠️  La cartera está vacía: el bot no tiene nada que ofrecer.")
        print("    Carga inmuebles en /dashboard/cartera antes de probar.\n")
        return

    print("\n  Cartera disponible (es lo único que el bot puede ofrecer):")
    for ciudad, tipo, negocio, cuantos in filas:
        plaza = plaza_de(ciudad or "")
        donde = f"{ciudad}" + (f" · plaza {plaza}" if plaza and plaza != ciudad else "")
        fuera = "" if plaza else "  ← fuera de plaza: el bot NO lo ofrece"
        print(f"    {cuantos:>3} × {tipo:<12} en {negocio:<9} · {donde}{fuera}")
    print()


# ─────────────────────────── Conversación ───────────────────────────

AYUDA = """
  Escribe como escribiría el comprador. Además:

    /si  /no      responder la autorización con el botón (como en el DM real)
    /quien NN     cambiar de comprador: usa otro IGSID y empieza de cero
    /cartera      qué inmuebles hay disponibles ahora mismo
    /ayuda        esto
    /salir        terminar
"""


def _conversar(vvi: str) -> None:
    igsid = IGSID
    print(AYUDA)
    print(f"  Escribiendo como {USUARIO} (IGSID {igsid}). Ctrl-C para salir.\n")

    while True:
        try:
            entrada = input("🧑 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not entrada:
            continue

        if entrada in ("/salir", "/exit", "/q"):
            return
        if entrada == "/ayuda":
            print(AYUDA)
            continue
        if entrada == "/cartera":
            _cartera()
            continue
        if entrada.startswith("/quien"):
            sufijo = entrada.split(maxsplit=1)[1].strip() if " " in entrada else "1"
            igsid = f"1784140000000{sufijo:0>4}"
            print(f"  Ahora escribe otro comprador (IGSID {igsid}), sin consentimiento previo.\n")
            continue
        if entrada in ("/si", "/no"):
            _empujar(vvi, _evento(igsid, boton=f"consent:{entrada[1:]}"))
            _esperar_respuesta()
            continue

        _empujar(vvi, _evento(igsid, texto=entrada))
        _esperar_respuesta()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vvi", default="http://127.0.0.1:8000", help="dónde corre VVI")
    parser.add_argument("--puerto", type=int, default=PUERTO_SALIDA, help="puerto del falso Meta")
    parser.add_argument("--revisar", action="store_true", help="solo comprobar la configuración")
    args = parser.parse_args()

    print("\n  Canal de Instagram — prueba local (ADR-02c)\n")
    if args.revisar:
        sys.exit(0 if _revisar(args.vvi, args.puerto) else 1)

    _levantar_salida(args.puerto)
    if not _revisar(args.vvi, args.puerto):
        sys.exit("\nCorrige lo anterior y vuelve a intentarlo.")
    _cartera()
    _conversar(args.vvi)


if __name__ == "__main__":
    main()
