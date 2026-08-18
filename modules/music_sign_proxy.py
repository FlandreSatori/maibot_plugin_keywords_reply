"""NapCat musicSignUrl 兼容代理。

启动后将 NapCat 的 musicSignUrl 指向::

    http://127.0.0.1:4567/music_card/card

代理把 NapCat 音乐签名请求转换为 CZ 音乐接口请求，并原样返回 QQ Ark
音乐卡片 JSON。上游参数可通过环境变量配置：

- ``CZ_MUSIC_API``：默认 ``https://api.czcn.xyz/api/qqyykp``
- ``CZ_MUSIC_KEY``：上游 API key
- ``CZ_MUSIC_TYPE``：无平台信息时的默认平台，默认 ``qq``；支持 ``qq``、``163``、``kugou``、``kuwo``、``migu``
- ``MUSIC_PROXY_HOST`` / ``MUSIC_PROXY_PORT``：监听地址和端口

查询 URL 模板由插件配置传入，支持 ``{id}`` 和 ``{platform}`` 占位符；模板为空时跳过对应查询。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "2026-08-18-music-sign-proxy-v1"
HOST = os.environ.get("MUSIC_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("MUSIC_PROXY_PORT", "4567"))
CZ_API = os.environ.get("CZ_MUSIC_API", "https://api.czcn.xyz/api/qqyykp")
CZ_KEY = os.environ.get("CZ_MUSIC_KEY", "")
CZ_TYPE = os.environ.get("CZ_MUSIC_TYPE", "qq")
UPSTREAM_TIMEOUT = float(os.environ.get("CZ_MUSIC_TIMEOUT", "15"))
LOOKUP_URLS: dict[str, dict[str, str]] = {}
SUPPORTED_PLATFORMS = frozenset({"qq", "163", "kugou", "kuwo", "migu"})
PLATFORM_ALIASES = {
    "netease": "163",
    "网易云": "163",
    "网易云音乐": "163",
    "qq音乐": "qq",
    "咪咕": "migu",
    "酷狗": "kugou",
    "酷我": "kuwo",
}
SIGN_PATHS = {"/", "/music_card/card", "/api/music/sign", "/sign"}
HEALTH_PATHS = {"/health", "/healthz"}
_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def scalar(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        return scalar(value[0] if value else "", default)
    if isinstance(value, (dict, tuple)):
        return default
    return str(value).strip()


def _read_chunked_body(handler: BaseHTTPRequestHandler) -> bytes:
    chunks: list[bytes] = []
    while True:
        line = handler.rfile.readline()
        if not line:
            raise ValueError("chunked 请求体提前结束")
        size_line = line.strip().split(b";", 1)[0]
        if not size_line:
            continue
        try:
            size = int(size_line, 16)
        except ValueError as error:
            raise ValueError(f"非法 chunk size: {size_line!r}") from error
        if size == 0:
            while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            break
        chunk = handler.rfile.read(size)
        if len(chunk) != size:
            raise ValueError("chunk 数据不完整")
        chunks.append(chunk)
        if handler.rfile.read(2) not in (b"\r\n", b"\n"):
            raise ValueError("chunk 结束符异常")
    return b"".join(chunks)


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        return _read_chunked_body(handler)
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as error:
        raise ValueError("非法 Content-Length") from error
    return handler.rfile.read(length) if length > 0 else b""


def _collect(value: Any, result: dict[str, Any], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"type", "platform", "id", "url", "audio", "title", "singer", "content", "desc", "image", "song", "cover", "jump"}:
                if not scalar(result.get(key)):
                    result[key] = item
            _collect(item, result, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect(item, result, depth + 1)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) >= 2 and text[0] in "[{" and text[-1] in "]}":
            try:
                _collect(json.loads(text), result, depth + 1)
            except json.JSONDecodeError:
                pass


def _parse_request(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    parsed = urlparse(handler.path)
    result: dict[str, Any] = {
        key: scalar(values) for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
    }
    if handler.command in {"POST", "PUT", "PATCH"}:
        raw = _read_request_body(handler)
        text = raw.decode("utf-8-sig", errors="replace").strip()
        if text:
            try:
                body: Any = json.loads(text)
            except json.JSONDecodeError:
                body = {key: scalar(values) for key, values in parse_qs(text, keep_blank_values=True).items()}
            _collect(body, result)
            if isinstance(body, dict):
                result.update({key: value for key, value in body.items() if key not in result})
    normalized: dict[str, Any] = {}
    _collect(result, normalized)
    normalized.update({key: value for key, value in result.items() if key not in normalized})
    return normalized


def _translate(source: dict[str, Any]) -> dict[str, str]:
    redreply = any(scalar(source.get(key)) for key in ("song", "cover", "jump"))
    if redreply:
        audio = scalar(source.get("audio") or source.get("url"))
        jump_url = scalar(source.get("jump") or source.get("url"))
        title = scalar(source.get("song") or source.get("title"))
        image = scalar(source.get("cover") or source.get("image"))
    else:
        jump_url = scalar(source.get("url") or source.get("jump"))
        audio = scalar(source.get("audio"))
        title = scalar(source.get("title") or source.get("song"))
        image = scalar(source.get("image") or source.get("cover"))
    singer = scalar(source.get("singer") or source.get("content") or source.get("desc"))
    platform = scalar(source.get("platform") or source.get("type"))
    platform = PLATFORM_ALIASES.get(platform.lower(), platform.lower())
    if platform not in SUPPORTED_PLATFORMS:
        platform = CZ_TYPE.lower() if CZ_TYPE.lower() in SUPPORTED_PLATFORMS else "qq"
    return {
        "key": CZ_KEY,
        "type": platform,
        "url": jump_url,
        "audio": audio,
        "title": title,
        "desc": singer,
        "image": image,
    }


def _format_lookup_url(template: str, platform: str, song_id: str) -> str:
    try:
        return template.format(id=song_id, platform=platform)
    except (KeyError, ValueError):
        return ""


def _fetch_json(url: str) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "MaiBot-NapCat-MusicSignProxy/1.0"}
    with urlopen(Request(url, headers=headers, method="GET"), timeout=UPSTREAM_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def _find_values(value: Any, names: set[str], found: dict[str, Any] | None = None) -> dict[str, Any]:
    if found is None:
        found = {}
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in names and lowered not in found and item not in (None, "", [], {}):
                found[lowered] = item
            _find_values(item, names, found)
    elif isinstance(value, list):
        for item in value:
            _find_values(item, names, found)
    return found


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(item for item in (_first_text(item) for item in value) if item)
    if isinstance(value, dict):
        return scalar(value.get("name") or value.get("title") or value.get("url"))
    return ""


def _lookup_song(source: dict[str, Any], params: dict[str, str]) -> dict[str, str]:
    song_id = scalar(source.get("id") or source.get("song_id") or source.get("mid"))
    platform = params["type"]
    if not song_id:
        return params
    urls = LOOKUP_URLS.get(platform, {})
    detail_url = _format_lookup_url(urls.get("detail", ""), platform, song_id)
    if detail_url:
        try:
            detail = _fetch_json(detail_url)
            values = _find_values(
                detail,
                {"name", "title", "songname", "song_name", "artists", "artist", "singer", "picurl", "pic_url", "cover", "coverurl", "image", "url"},
            )
            params["title"] = params["title"] or _first_text(
                values.get("songname") or values.get("song_name") or values.get("name") or values.get("title")
            )
            params["desc"] = params["desc"] or _first_text(values.get("artists") or values.get("artist") or values.get("singer"))
            params["image"] = params["image"] or _first_text(
                values.get("picurl") or values.get("pic_url") or values.get("coverurl") or values.get("cover") or values.get("image")
            )
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
            log(f"{platform}/{song_id} 详情查询失败: {error}")
    audio_url = _format_lookup_url(urls.get("audio", ""), platform, song_id)
    if audio_url:
        try:
            audio = _fetch_json(audio_url)
            values = _find_values(audio, {"url", "audio", "playurl", "play_url", "musicurl", "music_url"})
            params["audio"] = params["audio"] or _first_text(values.get("url") or values.get("audio") or values.get("playurl") or values.get("play_url") or values.get("musicurl") or values.get("music_url"))
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
            log(f"{platform}/{song_id} 音频查询失败: {error}")
    page_url = _format_lookup_url(urls.get("page", ""), platform, song_id)
    params["url"] = params["url"] or page_url
    return params


def _is_music_payload(payload: Any) -> bool:
    if not isinstance(payload, dict) or str(payload.get("view", "")).lower() != "music":
        return False
    meta = payload.get("meta")
    return isinstance(meta, dict) and isinstance(meta.get("music"), dict)


def _call_upstream(params: dict[str, str]) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "MaiBot-NapCat-MusicSignProxy/1.0"}
    errors: list[str] = []
    query = CZ_API + ("&" if "?" in CZ_API else "?") + urlencode(params)
    try:
        with urlopen(Request(query, headers=headers, method="GET"), timeout=UPSTREAM_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
            if _is_music_payload(payload):
                return payload
            errors.append("GET 返回非 music JSON")
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"GET: {error}")
    body = urlencode(params).encode("utf-8")
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    try:
        with urlopen(Request(CZ_API, data=body, headers=headers, method="POST"), timeout=UPSTREAM_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
            if _is_music_payload(payload):
                return payload
            errors.append("POST 返回非 music JSON")
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"POST: {error}")
    raise RuntimeError("；".join(errors))


class MusicSignHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {fmt % args}")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        self._handle(urlparse(self.path).path.rstrip("/") or "/")

    def do_POST(self) -> None:
        self._handle(urlparse(self.path).path.rstrip("/") or "/")

    def _handle(self, path: str) -> None:
        if path in HEALTH_PATHS:
            self._send_json(200, {"ok": True, "version": VERSION, "type": CZ_TYPE})
            return
        if path not in SIGN_PATHS:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            source = _parse_request(self)
            params = _lookup_song(source, _translate(source))
            log(f"签名请求: title={params['title']!r}, desc={params['desc']!r}")
            payload = _call_upstream(params)
            self._send_json(200, payload)
        except ValueError as error:
            log(f"请求参数错误: {error}")
            self._send_json(400, {"ok": False, "error": str(error)})
        except Exception as error:
            log(f"签名失败: {error}")
            self._send_json(502, {"ok": False, "error": str(error)})


def start_server(
    host: str = HOST,
    port: int = PORT,
    api_url: str = CZ_API,
    api_key: str = CZ_KEY,
    music_type: str = CZ_TYPE,
    timeout: float = UPSTREAM_TIMEOUT,
    lookup_urls: dict[str, dict[str, str]] | None = None,
) -> None:
    """在后台线程启动代理；重复调用时保持现有服务。"""

    global _server, _thread, CZ_API, CZ_KEY, CZ_TYPE, UPSTREAM_TIMEOUT, LOOKUP_URLS
    if _server is not None:
        return
    CZ_API, CZ_KEY, CZ_TYPE, UPSTREAM_TIMEOUT = api_url, api_key, music_type, timeout
    LOOKUP_URLS = lookup_urls or {}
    _server = ThreadingHTTPServer((host, port), MusicSignHandler)
    _thread = threading.Thread(target=_server.serve_forever, name="music-sign-proxy", daemon=True)
    _thread.start()
    log(f"{VERSION} 已启动: http://{host}:{port}/music_card/card")
    log(f"CZ 上游: {CZ_API} | type={CZ_TYPE}")


def stop_server() -> None:
    """停止插件启动的代理服务。"""

    global _server, _thread
    server, thread = _server, _thread
    _server, _thread = None, None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None:
        thread.join(timeout=2)


def main() -> None:
    start_server()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop_server()


if __name__ == "__main__":
    main()
