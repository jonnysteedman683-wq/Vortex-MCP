"""``python -m vortex`` runs the MCP server; ``python -m vortex migrate`` prepares Postgres."""
import sys


def main() -> int:
    if sys.argv[1:2] == ["migrate"]:
        from .migrate import main as migrate

        return migrate()
    from .server import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
