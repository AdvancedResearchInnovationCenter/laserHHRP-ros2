"""Operator UI backend entry point.

    ros2 run laser_hhrp_ui server                         # live ROS (core started with holes:=false ui:=false)
    ros2 run laser_hhrp_ui server --mock                  # mock ROS source, no ROS needed
    ros2 run laser_hhrp_ui server --static ui_web/dist    # also serve the built frontend

Binds 127.0.0.1 by default (plan section 3). With live ROS the backend subscribes read-only and runs the
selected detector itself; until Phase 3 adds scan identity, live detections are a preview (not recorded).
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


class _SupervisorService:
    def __init__(self, supervisor):
        self.supervisor = supervisor

    def start(self):
        self.supervisor.cleanup_orphan()

    def stop(self):
        self.supervisor.shutdown()


def build(db: str | Path, mock: bool = False, mock_speed: float = 1.0, static_dir=None,
          warn_after_s: float = 10.0, fail_after_s: float = 120.0, clock: Clock | None = None,
          ros: bool = False, data_root: str | Path | None = None):
    """Wire repository, engine, hub, ingress, the event source (mock or ROS) and the app (also for tests)."""
    if mock and ros:
        raise ValueError("--mock and the ROS bridge are exclusive")
    clock = clock or Clock()
    repo = Repository(db)
    hub = EventHub(clock)
    engine = Engine(repo, clock, emit=hub.publish, warn_after_s=warn_after_s, fail_after_s=fail_after_s,
                    source="mock" if mock else "ros" if ros else "none")
    ingress = Ingress(engine)
    mock_ros, services = None, []
    if mock:
        from .mock_ros import MockRos
        mock_ros = MockRos(ingress.submit, speed=mock_speed)
        engine.detector_control = mock_ros
    if ros:
        from .detector_supervisor import DetectorSupervisor, RosGraph
        from .ros_bridge import RosBridge
        root = Path(data_root) if data_root else Path(db).parent
        bridge = RosBridge(ingress.submit)
        supervisor = DetectorSupervisor(ingress.submit, RosGraph(lambda: bridge.node),
                                        out_root=root / "detector-output", log_dir=root / "logs")
        engine.detector_control = supervisor
        services = [bridge, _SupervisorService(supervisor)]   # stop order: supervisor, then bridge
    app = create_app(engine, hub, ingress, mock=mock_ros, static_dir=static_dir, services=services)
    return app, engine, hub, ingress, mock_ros


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(default_db_path()), help="SQLite file (default: %(default)s)")
    ap.add_argument("--static", default=None, help="built frontend (ui_web/dist); default: installed share/web")
    ap.add_argument("--mock", action="store_true", help="drive the backend from the mock ROS source")
    ap.add_argument("--no-ros", action="store_true", help="neither mock nor ROS (API only)")
    ap.add_argument("--data-root", default=None, help="detector output and logs (default: next to --db)")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="mock time scale (2 = twice as fast)")
    ap.add_argument("--analysis-warn-s", type=float, default=10.0)
    ap.add_argument("--analysis-fail-s", type=float, default=120.0)
    ap.add_argument("--log-level", default="info")
    args, _ = ap.parse_known_args(argv)   # tolerate --ros-args from ros2 run
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    static = args.static or default_static()
    ros = not args.mock and not args.no_ros
    app, *_ = build(args.db, mock=args.mock, mock_speed=args.mock_speed, static_dir=static,
                    warn_after_s=args.analysis_warn_s, fail_after_s=args.analysis_fail_s, ros=ros,
                    data_root=args.data_root)
    logging.getLogger("laser_hhrp_ui").info("database %s, static %s, source %s", args.db, static,
                                            "mock" if args.mock else "ros" if ros else "none")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
