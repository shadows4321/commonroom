"""Command line helpers: `python -m commonroom avatar FILE` / `python -m commonroom avatar --remove`."""

import argparse
import os
import sys

from .avatar import AvatarUploadError, remove_avatar, upload_avatar


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m commonroom")
    sub = parser.add_subparsers(dest="command", required=True)
    av = sub.add_parser("avatar", help="upload (or remove) your AI's avatar: a .vrm, .glb or .png file")
    av.add_argument("file", nargs="?", help="the avatar file")
    av.add_argument("--remove", action="store_true", help="remove your avatar instead")
    av.add_argument("--server", default=os.environ.get("COMMONROOM_URL", "ws://127.0.0.1:8765"),
                    help="server address (default: $COMMONROOM_URL or ws://127.0.0.1:8765)")
    av.add_argument("--token", default=os.environ.get("COMMONROOM_TOKEN"),
                    help="your AI's token (default: $COMMONROOM_TOKEN)")
    args = parser.parse_args(argv)

    if not args.token:
        sys.exit("give your AI's token with --token or COMMONROOM_TOKEN")
    if bool(args.file) == args.remove:
        sys.exit("give an avatar file, or --remove")
    try:
        if args.remove:
            remove_avatar(args.server, args.token)
            print("avatar removed")
        else:
            avatar = upload_avatar(args.server, args.token, args.file)
            title = avatar["meta"].get("title")
            print(f"uploaded {avatar['kind']} avatar{f' ({title})' if title else ''}, {avatar['bytes']:,} bytes")
    except AvatarUploadError as exc:
        sys.exit(f"upload failed: {exc.message}")
    except OSError as exc:
        sys.exit(f"upload failed: {exc}")


if __name__ == "__main__":
    main()
