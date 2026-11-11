"""Portable command line: python -m iacpaas_mcp [serve|start|stop|sync]."""
import argparse
import asyncio
import json
from .paths import load_environment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "start", "stop", "sync"])
    parser.add_argument("--force", action="store_true")
    args, extra = parser.parse_known_args()
    load_environment(override=True)
    if args.command == "serve":
        from .web import main as serve
        import sys
        sys.argv = [sys.argv[0], *extra]
        serve()
    elif args.command in {"start", "stop"}:
        from .lifecycle import start_app, stop_app
        print(start_app(progress=print) if args.command == "start" else stop_app(print))
    else:
        from .server import build_server
        from mcp import Client
        async def sync():
            async with Client(build_server()) as client:
                result = await client.call_tool("sync_resources", {"force": args.force})
                print(json.dumps(result.structured_content, ensure_ascii=False, indent=2))
        asyncio.run(sync())


if __name__ == "__main__":
    main()
