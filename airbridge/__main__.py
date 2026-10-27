"""Entry point.

  python -m airbridge                         run the service
  python -m airbridge user list               list accounts
  python -m airbridge user add NAME --role admin
  python -m airbridge user passwd NAME        reset a password (locked out? use this)
  python -m airbridge user role NAME operator
  python -m airbridge user enable|disable NAME
"""
import argparse
import getpass
import logging
import os
import sys


def _user_cli(argv):
    from .auth import ROLES, Auth, AuthError
    from .config import DATA_DIR, Config
    ap = argparse.ArgumentParser(prog="airbridge user")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--role", choices=ROLES, default="viewer")
    p = sub.add_parser("passwd"); p.add_argument("name")
    r = sub.add_parser("role"); r.add_argument("name"); r.add_argument("role", choices=ROLES)
    for c in ("enable", "disable"):
        sub.add_parser(c).add_argument("name")
    args = ap.parse_args(argv)
    auth = Auth(DATA_DIR, Config()["web"])
    users = {u["username"].lower(): u for u in auth.list_users()}

    def pw():
        a, b = getpass.getpass("New password: "), getpass.getpass("Repeat: ")
        if a != b:
            sys.exit("passwords do not match")
        return a

    try:
        if args.cmd == "list":
            for u in users.values():
                print(f"{u['username']:<20} {u['role']:<9} {'disabled' if u['disabled'] else 'active'}")
            return
        if args.cmd == "add":
            auth.create_user(args.name, pw(), args.role, by="cli", ip="local")
            print(f"created {args.name} ({args.role})")
            return
        u = users.get(args.name.lower())
        if not u:
            sys.exit(f"no user {args.name}")
        if args.cmd == "passwd":
            auth.update_user(u["id"], "cli", "local", password=pw(), disabled=False)
        elif args.cmd == "role":
            auth.update_user(u["id"], "cli", "local", role=args.role)
        else:
            auth.update_user(u["id"], "cli", "local", disabled=args.cmd == "disable")
        print("done (that user's sessions were signed out)")
    except AuthError as e:
        sys.exit(str(e))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "user":
        return _user_cli(sys.argv[2:])
    import uvicorn
    logging.basicConfig(level=os.environ.get("AIRBRIDGE_LOG", "INFO"),
                        format="%(asctime)s %(name)-6s %(levelname)-7s %(message)s")
    from .web import app, config
    uvicorn.run(app, host=config["web"]["host"], port=int(config["web"]["port"]),
                log_level="warning", ws_ping_interval=20, proxy_headers=False)


if __name__ == "__main__":
    main()
