"""Arranque del sistema:  python run.py"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="VVI — Vendedor Virtual Inmobiliario")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Recarga en caliente (desarrollo)")
    args = parser.parse_args()

    print("\n  VVI — Vendedor Virtual Inmobiliario")
    print(f"  Dashboard : http://{args.host}:{args.port}/dashboard")
    print(f"  API docs  : http://{args.host}:{args.port}/docs\n")

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
