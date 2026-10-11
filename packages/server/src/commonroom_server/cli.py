"""Command line: `commonroom-server run`, `add-agent`, the passwords, `import-glb` and `remove-house`."""

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
    if agents.admin_password:
        print(f"admin:  http://{args.host}:{args.port}/admin  (admin password)")
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


def _set_admin_password(args: argparse.Namespace) -> None:
    path = Path(args.agents or Path(args.world) / "agents.yaml")
    password = args.password or secrets.token_urlsafe(16)
    if len(password) < 8:
        sys.exit("the admin password must be at least 8 characters")
    raw = _read_agents_file(path)
    if password == raw.get("viewer_password") or any(str(a.get("token")) == password for a in raw["agents"]):
        sys.exit("the admin password must differ from the viewer password and every agent's token")
    raw["admin_password"] = password
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    print(f"admin password saved to {path}")
    print(f"password: {password}")
    print("Keep it to yourself: it can move and suspend AIs at /admin. Restart the server to pick it up.")


def _import_glb(args: argparse.Namespace) -> None:
    import shutil

    from .config import _read_yaml
    from .config import FIXTURE
    from .scene_import import ImportProblem, TypeInfo, dump_yaml, import_town, install_house, type_info

    world_dir = Path(args.world)
    glb = Path(args.file)
    world_path = world_dir / "world.yaml"
    world = _read_yaml(world_path) if world_path.exists() else {"format_version": 2, "name": world_dir.name.title()}
    objects_path = world_dir / "objects.yaml"
    known_types = type_info((_read_yaml(objects_path) if objects_path.exists() else {}).get("types"))
    known_types.setdefault(FIXTURE, TypeInfo(state=set()))
    check = args.check

    def place(target: Path) -> str:
        """Copy the .glb into the world directory if it isn't there already; return its relative path."""
        if check:
            return target.relative_to(world_dir).as_posix()
        target.parent.mkdir(parents=True, exist_ok=True)
        if glb.resolve() != target.resolve():
            shutil.copyfile(glb, target)
            print(f"copied {glb} -> {target}")
        return target.relative_to(world_dir).as_posix()

    try:
        if args.owner and args.shared:
            sys.exit("a house has either an --owner or is --shared, not both")
        if args.owner or args.shared:
            hid = args.owner or args.shared
            house = install_house(world_dir, glb, hid, args.plot, write=not check,
                                  shared=bool(args.shared), name=args.name)
            warnings = house.warnings
            if check:
                print(f"{glb}: {house.rooms} rooms, {house.objects} objects (nothing written)")
                _report(warnings, check)
                return
            target = world_dir / "houses" / f"{hid}.glb"
            if glb.resolve() != target.resolve():
                print(f"copied {glb} -> {target}")
            print(f"wrote {world_dir / 'houses' / (hid + '.yaml')}: {house.rooms} rooms, {house.objects} objects")
            if house.new:
                what = f"the shared house {hid!r}" if args.shared else f"{hid}'s house"
                print(f"added {what} on plot {house.plot} to {world_path}")
        else:
            world, warnings = import_town(glb, world, known_types)
            if check:
                print(f"{glb}: {len(world['rooms'])} town rooms, {len(world['plots'])} plots, "
                      f"{len(world['objects'])} objects (nothing written)")
                _report(warnings, check)
                return
            world["scene"] = place(world_dir / "town.glb")
            world_path.write_text(dump_yaml(world))
            print(f"wrote {world_path}: {len(world['rooms'])} town rooms, {len(world['plots'])} plots, "
                  f"{len(world['objects'])} objects")
    except ImportProblem as exc:
        sys.exit(f"can't import {glb}: {exc}")
    _report(warnings, check)
    print("Check the object types (new objects get 'fixture' unless their id matches a type), "
          "then restart the server.")


def _remove_house(args: argparse.Namespace) -> None:
    from .scene_import import ImportProblem, uninstall_house

    try:
        moved = uninstall_house(args.world, args.house)
    except ImportProblem as exc:
        sys.exit(f"can't remove {args.house!r}: {exc}")
    print(f"took {args.house}'s house out of {Path(args.world) / 'world.yaml'}; its plot is free")
    for path in moved:
        print(f"moved its file to {Path(args.world) / path}")
    print("Restart the server: anyone inside goes home or to the start room, and the house's things go with it. "
          "(On a running server, the admin page does this without a restart.)")


def _report(warnings: list[str], check: bool) -> None:
    for w in warnings:
        print(f"warning: {w}")
    if check:
        if warnings:
            sys.exit(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
        print("no problems found")


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

    adm = sub.add_parser("set-admin-password", help="set (or generate) the password for the host's /admin page")
    adm.add_argument("password", nargs="?", help="at least 8 characters (default: generate one)")
    adm.add_argument("--world", required=True)
    adm.add_argument("--agents", help="agents.yaml (default: <world>/agents.yaml)")
    adm.set_defaults(func=_set_admin_password)

    imp = sub.add_parser("import-glb", help="read a town or house .glb and write its rooms, exits and objects as YAML")
    imp.add_argument("file", help="the exported .glb (see docs/3d-spec.md)")
    imp.add_argument("--world", required=True, help="world directory, e.g. worlds/town")
    imp.add_argument("--owner", help="for a house: the AI's id (writes houses/<owner>.yaml); omit for the town")
    imp.add_argument("--shared", metavar="ID",
                     help="for a shared house that belongs to no AI, like a library or a hall: its id "
                          "(writes houses/<id>.yaml)")
    imp.add_argument("--name", help="with --shared: what the house is called (default: from its id)")
    imp.add_argument("--plot", help="for a new house: which plot it stands on (default: the first free one)")
    imp.add_argument("--check", action="store_true",
                     help="only report problems (exit status 1 if there are any); write nothing")
    imp.set_defaults(func=_import_glb)

    rm = sub.add_parser("remove-house", help="take a house out of the town and free its plot "
                                             "(its files move to houses/removed/)")
    rm.add_argument("house", help="the AI's id for its house, or a shared house's id")
    rm.add_argument("--world", required=True, help="world directory, e.g. worlds/town")
    rm.set_defaults(func=_remove_house)

    args = parser.parse_args(argv)
    if getattr(args, "viewer", "unset") is None:
        from .app import DEFAULT_VIEWER_DIR
        args.viewer = DEFAULT_VIEWER_DIR
    args.func(args)


if __name__ == "__main__":
    main()
