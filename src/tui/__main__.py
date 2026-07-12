import argparse
import json
from pathlib import Path

from src.tui.app import PrometheusApp

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONF_PATH = _PROJECT_ROOT / "conf" / "tui.conf.json"


def load_config() -> dict:
    try:
        return json.loads(_CONF_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    conf = load_config()
    default_poll = conf.get("poll_interval", 2)
    default_port = conf.get("launcher_port", 9421)

    parser = argparse.ArgumentParser(
        prog="src.tui",
        description="Prometheus TUI — terminal control panel",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"launcher API port (default: {default_port})",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=default_poll,
        help=f"refresh interval in seconds (default: {default_poll})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    app = PrometheusApp(launcher_port=args.port, poll_interval=args.poll)
    app.run()


if __name__ == "__main__":
    main()
