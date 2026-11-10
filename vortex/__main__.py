"""``python -m vortex`` runs the MCP server; ``migrate`` prepares Postgres; ``relay`` feeds the ledger;
``keys`` issues, revokes and lists agent API keys."""
import sys


def main() -> int:
    if sys.argv[1:2] == ["migrate"]:
        from .migrate import main as migrate

        return migrate()
    if sys.argv[1:2] == ["relay"]:
        from .relay import main as relay

        return relay()
    if sys.argv[1:2] == ["keys"]:
        from . import keys

        return keys.main(sys.argv[2:])
    from .server import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
