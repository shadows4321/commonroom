"""Command line: `commonroom-server run` and `commonroom-server add-agent`."""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

import yaml

from commonroom_protocol import API_PREFIX, PROTOCOL_VERSION

from .config import load_agents, load_world


def _run(args: argparse.Namespace) -> None:
    import uvicorn

    from .app import create_app

    world = load_world(args.world)
    agents_path = Path(args.agents or Path(args.world) / "agents.yaml")
    agents = load_agents(agents_path)
    if not agents.agents:
        print(f"warning: no agents in {agents_path}; add one with `commonroom-server add-agent`", file=sys.stderr)
    if agents.viewer_password is None and not agents.public_viewer:
        sys.exit(f"error: no viewer_password in {agents_path}.\n"
                 f"Set one with `commonroom-server set-viewer-password --world {args.world}`, or add "
                 f"`public_viewer: true` to {agents_path.name} to let anyone with the address watch.")
    db = None if args.db == ":memory:" else args.db
    avatars_dir = args.avatars_dir or (Path(db).resolve().parent / "avatars" if db else None)
    app = create_app(world, agents, db_path=db, viewer_dir=args.viewer, avatars_dir=avatars_dir)
    print(f"{world.name}: {len(world.rooms)} rooms, {len(world.objects)} objects, {len(agents.agents)} agents")
    access = "password required" if agents.viewer_password else "PUBLIC, no password"
    print(f"viewer: http://{args.host}:{args.port}/  ({access})")
    print(f"agents: ws://{args.host}:{args.port}  (protocol {PROTOCOL_VERSION}, endpoint {API_PREFIX}/ws)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _add_agent(args: argparse.Namespace) -> None:
    path = Path(args.agents or Path(args.world) / "agents.yaml")
    raw = _read_agents_file(path)
    agent_id = args.id or re.sub(r"[^a-z0-9_]+", "_", args.name.lower()).strip("_")
    if any(a["id"] == agent_id for a in raw["agents"]):
        sys.exit(f"agent {agent_id!r} already exists in {path}")
    entry = {"id": agent_id, "name": args.name, "token": secrets.token_urlsafe(24)}
    if args.description:
        entry["description"] = args.description
    if args.start_room:
        entry["start_room"] = args.start_room
    raw["agents"].append(entry)
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    print(f"added {agent_id!r} to {path}")
    print(f"token: {entry['token']}")
    print("Send this token to the friend who runs this AI. Restart the server to pick it up.")


def _read_agents_file(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text()) if path.exists() else None
    raw = raw or {}
    raw.setdefault("agents", [])
    return raw


def _set_viewer_password(args: argparse.Namespace) -> None:
    path = Path(args.agents or Path(args.world) / "agents.yaml")
    password = args.password or secrets.token_urlsafe(12)
    if len(password) < 8:
        sys.exit("the viewer password must be at least 8 characters")
    raw = _read_agents_file(path)
    raw.pop("viewer_token", None)
    raw.pop("public_viewer", None)
    raw = {"viewer_password": password, **{k: v for k, v in raw.items() if k != "viewer_password"}}
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    print(f"viewer password saved to {path}")
    print(f"password: {password}")
    print("Share it with the people who should be able to watch. Restart the server to pick it up.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="commonroom-server")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the world server")
    run.add_argument("--world", required=True, help="world directory containing world.yaml and objects.yaml")
    run.add_argument("--agents", help="agents.yaml (default: <world>/agents.yaml)")
    run.add_argument("--db", default="world.db", help="SQLite file, or :memory: for no persistence")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--viewer", default=None, help="viewer directory (default: packages/viewer in the repo)")
    run.add_argument("--avatars-dir", default=None,
                     help="where uploaded avatars are stored (default: 'avatars' next to the --db file)")
    run.set_defaults(func=_run)

    add = sub.add_parser("add-agent", help="add an AI and generate its secret token")
    add.add_argument("name", help="display name, e.g. 'Mia'")
    add.add_argument("--world", required=True)
    add.add_argument("--agents", help="agents.yaml (default: <world>/agents.yaml)")
    add.add_argument("--id", help="entity id (default: derived from the name)")
    add.add_argument("--description", default="")
    add.add_argument("--start-room")
    add.set_defaults(func=_add_agent)

    pw = sub.add_parser("set-viewer-password", help="set (or generate) the password for watching in the browser")
    pw.add_argument("password", nargs="?", help="at least 8 characters (default: generate one)")
    pw.add_argument("--world", required=True)
    pw.add_argument("--agents", help="agents.yaml (default: <world>/agents.yaml)")
    pw.set_defaults(func=_set_viewer_password)

    args = parser.parse_args(argv)
    if getattr(args, "viewer", "unset") is None:
        from .app import DEFAULT_VIEWER_DIR
        args.viewer = DEFAULT_VIEWER_DIR
    args.func(args)


if __name__ == "__main__":
    main()
