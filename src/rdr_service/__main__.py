import argparse
import json

from .backend import RDRBackend
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="RDR skill service (local HTTP v1)")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Freeze an existing compiled skill")
    prepare.add_argument("skill_dir")
    prepare.add_argument("--store", default="experiments/snapshots")
    prepare.add_argument("--output", help="Write the public snapshot receipt")
    serve = commands.add_parser("serve")
    serve.add_argument("--store", default="experiments/snapshots")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    backend = RDRBackend(args.store)
    if args.command == "prepare":
        receipt = backend.prepare(args.skill_dir)
        text = json.dumps(receipt, indent=2)
        if args.output:
            from pathlib import Path
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        print(text)
    else:
        server = make_server(backend, port=args.port)
        print(f"Listening on http://127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
