#!/usr/bin/env python3
"""MCP stdio 冒烟测试。

真实启动 ``run_mcp.py`` 子进程，通过 stdio 发送 JSON-RPC 消息并校验响应。
**这是"真实执行"证据**，不是模拟。

用法：
    .venv/bin/python scripts/smoke_mcp_stdio.py
退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


class StdioClient:
    """极简 MCP stdio 客户端（换行分隔 JSON-RPC）。"""

    def __init__(self, cmd: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._id = 0

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"子进程未返回数据；stderr:\n{err}")
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        while True:
            msg = self._read()
            if msg.get("id") == self._id:
                return msg

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    if not PYTHON.exists():
        print(f"FAIL: 未找到虚拟环境解释器 {PYTHON}")
        return 1

    env = dict(os.environ)
    env["MCPM_DATA_DIR"] = str(PROJECT_ROOT / "var" / "smoke")
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    client = StdioClient([str(PYTHON), str(PROJECT_ROOT / "run_mcp.py")], env)
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            failures.append(name)

    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        )
        result = init.get("result", {})
        server_info = result.get("serverInfo", {})
        check("initialize 成功", "result" in init, f"serverInfo={server_info}")
        check("服务名正确", server_info.get("name") == "mcp-manager")

        client.notify("notifications/initialized")

        tools = client.request("tools/list", {})
        names = sorted(t["name"] for t in tools.get("result", {}).get("tools", []))
        check("tools/list 返回 4 个工具", len(names) == 4, f"{names}")
        for expected in ("list_plugins", "get_plugin", "get_stats", "request_install"):
            check(f"包含工具 {expected}", expected in names)

        call = client.request("tools/call", {"name": "get_stats", "arguments": {}})
        content = call.get("result", {}).get("content", [])
        payload = json.loads(content[0]["text"]) if content else {}
        check("get_stats 返回 ok", payload.get("ok") is True, str(payload)[:120])

        # 关键安全断言：Agent 调用 request_install 只能**申请**，绝不能执行安装，
        # 也绝不能拿到任何确认令牌。未知条目必须显式失败（not_found），不得伪成功。
        call2 = client.request(
            "tools/call",
            {
                "name": "request_install",
                "arguments": {"plugin_id": "github:x/y:deadbeef", "reason": "smoke"},
            },
        )
        content2 = call2.get("result", {}).get("content", [])
        payload2 = json.loads(content2[0]["text"]) if content2 else {}
        check(
            "Agent 的安装申请对未知条目显式失败",
            payload2.get("ok") is False and payload2.get("error") == "not_found",
            str(payload2)[:120],
        )
        check(
            "Agent 响应中不含任何确认令牌",
            "confirmation_token" not in payload2 and "token" not in payload2,
            str(sorted(payload2.keys())),
        )
    finally:
        client.close()

    print()
    if failures:
        print(f"结果：{len(failures)} 项失败 -> {failures}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())