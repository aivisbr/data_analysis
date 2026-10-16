r"""Windows CMD: .venv\Scripts\python.exe -m app"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokāla diktāta salīdzināšanas lietotne.")
    parser.add_argument("--port", type=int, default=8000, help="Ports (noklusējums: 8000).")
    parser.add_argument("--reload", action="store_true", help="Pārlādēt pēc koda izmaiņām.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Portam jābūt no 1 līdz 65535.")
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1",
                port=args.port, reload=args.reload, access_log=False)


if __name__ == "__main__":
    main()
