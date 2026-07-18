#!/usr/bin/env python3
"""关键词词库外部编辑器本地服务。

直接读写 MaiBot 插件数据目录下的 ``keywords.json``。

用法::

    python editor/server.py --data-dir "path/to/MaiBot/data/plugins/maibot_plugin.keywords_reply"
    python editor/server.py --data-dir "..." --port 8765
    python editor/server.py --data-dir "..." --host 0.0.0.0 --token "your-secret"

开启 ``--token`` 后，用 ``http://ip:port/?token=你的密码`` 访问；校验通过后写入 Cookie，后续请求自动带鉴权。
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.merge_rules import merge_keywords_file  # noqa: E402
from modules.store import KeywordsStore  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
_COOKIE_NAME = "kr_editor_token"


class EditorHandler(SimpleHTTPRequestHandler):
    data_dir: Path = Path(".")
    access_token: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[editor] {self.address_string()} - {format % args}")

    def _token_required(self) -> bool:
        return bool(str(self.access_token or "").strip())

    def _expected_token(self) -> str:
        return str(self.access_token or "").strip()

    def _query_token(self) -> str:
        parsed = urlparse(self.path)
        values = parse_qs(parsed.query).get("token") or []
        return str(values[0] if values else "").strip()

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(_COOKIE_NAME)
        if morsel is None:
            return ""
        return str(morsel.value or "").strip()

    def _token_matches(self, provided: str) -> bool:
        expected = self._expected_token()
        if not expected or not provided:
            return False
        return secrets.compare_digest(provided, expected)

    def _is_authorized(self) -> bool:
        if not self._token_required():
            return True
        return self._token_matches(self._query_token()) or self._token_matches(self._cookie_token())

    def _wants_json(self) -> bool:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return True
        accept = (self.headers.get("Accept") or "").lower()
        return "application/json" in accept and "text/html" not in accept

    def _send_unauthorized(self) -> None:
        if self._wants_json():
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "error": "unauthorized",
                    "hint": "请使用 ?token=密码 访问，或在启动参数中核对 --token",
                },
            )
            return
        body = (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>需要访问密码</title></head><body>"
            "<h1>无效的密码</h1>"
            "<p>请在启动脚本editor.bat的输出查看访问链接</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _maybe_set_auth_cookie(self) -> None:
        """查询参数 token 正确时写入 Cookie，便于后续 API / 静态资源请求。"""

        if not self._token_required():
            return
        query_token = self._query_token()
        if not self._token_matches(query_token):
            return
        cookie = SimpleCookie()
        cookie[_COOKIE_NAME] = query_token
        cookie[_COOKIE_NAME]["path"] = "/"
        cookie[_COOKIE_NAME]["httponly"] = True
        cookie[_COOKIE_NAME]["samesite"] = "Lax"
        # 30 天
        cookie[_COOKIE_NAME]["max-age"] = 30 * 24 * 60 * 60
        self.send_header("Set-Cookie", cookie[_COOKIE_NAME].OutputString())

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if status < 400:
            self._maybe_set_auth_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_unauthorized()
        return False

    def do_GET(self) -> None:
        if not self._require_auth():
            return
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
                    "auth_required": self._token_required(),
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

        # 静态页：若用 ?token= 进入，写入 Cookie 后便于后续 fetch
        if self._token_required() and self._token_matches(self._query_token()):
            # 走父类发送前无法插 Cookie，这里对首页单独处理更稳
            if path in {"", "/", "/index.html"}:
                index_path = STATIC_DIR / "index.html"
                body = index_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._maybe_set_auth_cookie()
                self.end_headers()
                self.wfile.write(body)
                return

        return super().do_GET()

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path != "/api/merge-duplicates":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        try:
            data_file = self.data_dir / "keywords.json"
            result = merge_keywords_file(data_file)
            store = KeywordsStore(self.data_dir)
            store.setup()
            result["data"] = store.data
            result["keyword_count"] = len(store.data.get("command_triggered", []))
            result["detect_count"] = len(store.data.get("auto_detect", []))
            self._send_json(HTTPStatus.OK, result)
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
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


def _format_access_url(host: str, port: int, token: str, *, ip: Optional[str] = None) -> str:
    display_host = ip or ("127.0.0.1" if host in {"0.0.0.0", "::"} else host)
    base = f"http://{display_host}:{port}/"
    if token:
        return f"{base}?token={token}"
    return base


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
        help="监听地址（默认 127.0.0.1）",
    )
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument(
        "--token",
        default="",
        help="访问密码；设置后需用 http://主机:端口/?token=密码 打开（会写入 Cookie）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    access_token = str(args.token or "").strip()

    handler_cls = type(
        "BoundEditorHandler",
        (EditorHandler,),
        {"data_dir": data_dir, "access_token": access_token},
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    print(f"词库编辑器已启动: {_format_access_url(args.host, args.port, access_token)}")
    if args.host in {"0.0.0.0", "::"}:
        for ip in _lan_ipv4_addresses():
            print(f"  {_format_access_url(args.host, args.port, access_token, ip=ip)}")
    if access_token:
        print("已启用访问密码（--token）；请用上方带 ?token= 的地址打开。")
    else:
        print("""
		--------------------------------------
		   未设置 token , 当前无需密码即可访问
		--------------------------------------
		""")
    print(f"数据目录: {data_dir}")
    print("保存后请在 MaiBot 群聊执行 /重载词库，或重启 MaiBot 使改动生效。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
