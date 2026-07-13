#!/usr/bin/env python3
"""关键词词库外部编辑器本地服务。

直接读写 MaiBot 插件数据目录下的 ``keywords.json``。

用法::

    python editor/server.py --data-dir "path/to/MaiBot/data/plugins/maibot_plugin.keywords_reply"
    python editor/server.py --data-dir "..." --port 8765
    # 允许局域网其它设备访问（需放行防火墙端口）：
    python editor/server.py --data-dir "..." --host 0.0.0.0 --port 8765

然后在浏览器打开 http://127.0.0.1:8765 （或本机局域网 IP）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, List
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.store import KeywordsStore  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


class EditorHandler(SimpleHTTPRequestHandler):
    data_dir: Path = Path(".")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[editor] {self.address_string()} - {format % args}")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            data_file = self.data_dir / "keywords.json"
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data_dir": str(self.data_dir.resolve()),
                    "data_file": str(data_file.resolve()),
                    "exists": data_file.is_file(),
                },
            )
            return
        if path == "/api/data":
            store = KeywordsStore(self.data_dir)
            store.setup()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": store.data,
                    "path": str(store.data_file.resolve()),
                },
            )
            return
        return super().do_GET()

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/data":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError("请求体须为 { data: {...} }")
            normalized = KeywordsStore.normalize(payload["data"])
            store = KeywordsStore(self.data_dir)
            store.data = normalized
            store.data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = store.data_file.with_suffix(".json.tmp")
            text = json.dumps(normalized, ensure_ascii=False, indent=2)
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(store.data_file)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "path": str(store.data_file.resolve()),
                    "keyword_count": len(normalized.get("command_triggered", [])),
                    "detect_count": len(normalized.get("auto_detect", [])),
                },
            )
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def _lan_ipv4_addresses() -> List[str]:
    """尽力枚举本机局域网 IPv4，供启动提示使用。"""

    addresses: List[str] = []
    seen: set[str] = set()

    def add(ip: str) -> None:
        text = str(ip or "").strip()
        if not text or text in seen or text.startswith("127."):
            return
        seen.add(text)
        addresses.append(text)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            add(info[4][0])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add(sock.getsockname()[0])
    except OSError:
        pass

    return addresses


def main() -> None:
    parser = argparse.ArgumentParser(description="关键词回复词库外部编辑器")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="MaiBot 插件数据目录，例如 data/plugins/maibot_plugin.keywords_reply",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址；本机访问用 127.0.0.1，局域网访问用 0.0.0.0",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    handler_cls = type("BoundEditorHandler", (EditorHandler,), {"data_dir": data_dir})
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"词库编辑器已启动: http://{args.host}:{args.port}")
    if args.host in {"0.0.0.0", "::"}:
        print("局域网访问（在其它设备浏览器打开）：")
        lan_ips = _lan_ipv4_addresses()
        if lan_ips:
            for ip in lan_ips:
                print(f"  http://{ip}:{args.port}")
        else:
            print(f"  http://<本机局域网IP>:{args.port}")
        print("注意：未做登录鉴权，仅建议在可信局域网内使用；Windows 若打不开请放行防火墙端口。")
    else:
        print("仅本机可访问。若要从局域网其它设备打开，请加参数：--host 0.0.0.0")
    print(f"数据目录: {data_dir}")
    print("保存后请在 MaiBot 群聊执行 /重载词库，或重启 MaiBot 使改动生效。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
