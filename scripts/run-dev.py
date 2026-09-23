#!/usr/bin/env python3
"""按 lane 启动相互隔离的幕僚开发实例。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


LANES = {
    "ghost": {"port": "8941", "token": "ghost-lane"},
    "voice": {"port": "8942", "token": "voice-lane"},
    "integration": {"port": "8943", "token": "integration"},
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lane", choices=sorted(LANES))
    parser.add_argument("--browser", action="store_true", help="打开独立 APP 窗口")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    workspace = root.parent
    lane = LANES[args.lane]
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    shared_config = appdata / "Muliao" / "config.json"
    runtime = workspace / "muliao-runtime" / args.lane
    runtime.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "MULIAO_PORT": lane["port"],
        "MULIAO_INSTANCE_TOKEN": lane["token"],
        "MULIAO_DATA_DIR": str(runtime),
        "MULIAO_CONFIG": str(shared_config),
        "MULIAO_NO_BROWSER": "0" if args.browser else "1",
        "PYTHONIOENCODING": "utf-8",
    })
    print(f"lane={args.lane} port={lane['port']} data={runtime}")
    print(f"config={shared_config} exists={shared_config.is_file()}")
    return subprocess.call([sys.executable, str(root / "server.py")], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
