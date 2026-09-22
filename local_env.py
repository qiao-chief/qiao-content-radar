#!/usr/bin/env python3
"""本机运行 radar.py 用：从本机已有的配置文件里读三把钥匙，注入环境变量后运行。

用法：python3 local_env.py            正式跑（写表、发消息）
      python3 local_env.py --dry-run  试跑（不写表、不发消息）
钥匙来源：小K → ~/.lark-channel/config-xiaok.json；DeepSeek → 知识库 工具/凭证/deepseek.md；
得到大脑 → ~/.getnote/config.json（getnote 命令行工具自己读，不用注入）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
XIAOK_CONFIG = Path.home() / ".lark-channel" / "config-xiaok.json"
DEEPSEEK_MD = Path("/Users/qiaozhanglong/Downloads/code/乔帮主知识库/工具/凭证/deepseek.md")


def load_env() -> dict[str, str]:
    app = json.loads(XIAOK_CONFIG.read_text())["accounts"]["app"]
    key = re.search(r"API Key \| `(sk-[A-Za-z0-9]+)`", DEEPSEEK_MD.read_text(encoding="utf-8"))
    if not key:
        raise SystemExit(f"在 {DEEPSEEK_MD} 里没找到 DeepSeek API Key")
    return {"XIAOK_APP_ID": app["id"], "XIAOK_APP_SECRET": app["secret"], "DEEPSEEK_API_KEY": key.group(1)}


def main() -> int:
    env = {**os.environ, **load_env()}
    env["PATH"] = f"{Path.home()}/.npm-global/bin:" + env.get("PATH", "")
    if "--dry-run" in sys.argv:
        env["RADAR_DRY_RUN"] = "1"
    return subprocess.run([sys.executable, str(HERE / "radar.py")], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
