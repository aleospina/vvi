"""Arranque del sistema:  python run.py"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="VVI — Vendedor Virtual Inmobiliario")
    # En local se escucha solo en la máquina; una plataforma de despliegue
    # necesita 0.0.0.0 y el puerto que ella asigna en $PORT, o el enrutador
    # nunca alcanza el proceso y el servicio queda como caído.
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="Recarga en caliente (desarrollo)")
    args = parser.parse_args()

    print("\n  VVI — Vendedor Virtual Inmobiliario")
    print(f"  Dashboard : http://{args.host}:{args.port}/dashboard")
    print(f"  API docs  : http://{args.host}:{args.port}/docs\n")

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
