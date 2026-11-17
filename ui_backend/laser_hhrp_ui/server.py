"""Operator UI backend entry point.

    ros2 run laser_hhrp_ui server --mock                  # Phase 1: mock ROS source
    ros2 run laser_hhrp_ui server --static ui_web/dist    # also serve the built frontend

Binds 127.0.0.1 by default (plan section 3). The live ROS bridge arrives in Phase 2; without --mock the
backend runs but reports "ROS bridge not running" and never shows Ready.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .api import create_app
from .clock import Clock
from .hub import EventHub
from .ingress import Ingress
from .repository import Repository, default_db_path
from .state import Engine


def default_static() -> Path | None:
    try:
        from ament_index_python.packages import get_package_share_directory
        web = Path(get_package_share_directory("laser_hhrp_ui")) / "web"
        return web if web.is_dir() else None
    except Exception:
        return None


def build(db: str | Path, mock: bool = False, mock_speed: float = 1.0, static_dir=None,
          warn_after_s: float = 10.0, fail_after_s: float = 120.0, clock: Clock | None = None):
    """Wire repository, engine, hub, ingress, optional mock and the app (also used by tests)."""
    clock = clock or Clock()
    repo = Repository(db)
    hub = EventHub(clock)
    engine = Engine(repo, clock, emit=hub.publish, warn_after_s=warn_after_s, fail_after_s=fail_after_s,
                    source="mock" if mock else "none")
    ingress = Ingress(engine)
    mock_ros = None
    if mock:
        from .mock_ros import MockRos
        mock_ros = MockRos(ingress.submit, speed=mock_speed)
        engine.detector_control = mock_ros
    app = create_app(engine, hub, ingress, mock=mock_ros, static_dir=static_dir)
    return app, engine, hub, ingress, mock_ros


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(default_db_path()), help="SQLite file (default: %(default)s)")
    ap.add_argument("--static", default=None, help="built frontend (ui_web/dist); default: installed share/web")
    ap.add_argument("--mock", action="store_true", help="drive the backend from the mock ROS source")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="mock time scale (2 = twice as fast)")
    ap.add_argument("--analysis-warn-s", type=float, default=10.0)
    ap.add_argument("--analysis-fail-s", type=float, default=120.0)
    ap.add_argument("--log-level", default="info")
    args, _ = ap.parse_known_args(argv)   # tolerate --ros-args from ros2 run
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    static = args.static or default_static()
    app, *_ = build(args.db, mock=args.mock, mock_speed=args.mock_speed, static_dir=static,
                    warn_after_s=args.analysis_warn_s, fail_after_s=args.analysis_fail_s)
    logging.getLogger("laser_hhrp_ui").info("database %s, static %s, mock %s", args.db, static, args.mock)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
