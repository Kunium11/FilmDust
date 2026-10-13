#!/usr/bin/env python3
"""Loopback-only browser app. All images stay in RAM; exports download locally."""
import argparse
import errno
import io
import json
import secrets
import threading
import traceback
import urllib.parse
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from engine import Photo, png_bytes

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
SESSIONS = OrderedDict()
GLOBAL_LOCK = threading.RLock()
MAX_UPLOAD = 200 * 1024 * 1024
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith("GET /api/image"):
            return
        super().log_message(fmt, *args)

    def send(self, data, status=200, mime="application/json", headers=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.valid_host():
            self.send({"error": "只允许本机访问。"}, 403)
            return
        try:
            u = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/app.js", "/style.css"):
                filename = "index.html" if u.path == "/" else u.path[1:]
                data = (ROOT / "web" / filename).read_bytes()
                mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[filename]
                if filename == "index.html":
                    data = data.replace(b"__TOKEN__", TOKEN.encode())
                self.send(data, mime=mime)
            elif u.path == "/api/health":
                self.send({"ok": True, "local": True, "version": "1.7.0", "ai": False, "automatic": True})
            elif u.path == "/api/image":
                with GLOBAL_LOCK:
                    photo = SESSIONS.get(query.get("id", [""])[0])
                    if photo is None:
                        raise ValueError("照片已关闭，请重新导入。")
                    kind = query.get("kind", ["original"])[0]
                    if kind == "mask":
                        self.send(png_bytes(photo.mask), mime="image/png")
                    elif kind == "overlay":
                        overlay = np.zeros((photo.height, photo.width, 4), np.uint8)
                        overlay[photo.pending_mask > 0] = [242, 195, 79, 180]
                        overlay[photo.mask > 0] = [244, 91, 66, 155]
                        self.send(png_bytes(overlay), mime="image/png")
                    elif kind == "original":
                        self.send(png_bytes(photo.preview), mime="image/png")
                    elif kind == "result":
                        self.send(png_bytes(photo.display(photo.heal())), mime="image/png")
                    else:
                        raise ValueError("未知预览类型。")
            else:
                self.send({"error": "页面不存在。"}, 404)
        except ValueError as e:
            self.send({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.send({"error": "预览失败，请重新导入照片。"}, 500)

    def do_POST(self):
        if not self.valid_host() or self.headers.get("X-FilmDust-Token") != TOKEN:
            self.send({"error": "请求无效，请刷新本地页面。"}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
            self.send({"error": "只允许本机页面发起操作。"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > MAX_UPLOAD:
                raise ValueError("单张照片不能超过 200 MB。")
            raw = self.rfile.read(length)
            u = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(u.query)
            with GLOBAL_LOCK:
                if u.path in ("/api/upload", "/api/sample"):
                    if u.path == "/api/sample":
                        raw = (ROOT / "examples" / "sample.jpeg").read_bytes()
                        name = "示例扫描照片.jpeg"
                    else:
                        name = query.get("name", ["scan"])[0]
                    photo = Photo(raw, name)
                    ident = secrets.token_urlsafe(18)
                    # One active document, previous one retained for a failed switch.
                    while len(SESSIONS) >= 2:
                        SESSIONS.popitem(last=False)
                    SESSIONS[ident] = photo
                    self.send(dict(id=ident, **photo.info()))
                    return
                body = json.loads(raw or b"{}")
                photo = SESSIONS.get(body.get("id"))
                if photo is None:
                    raise ValueError("照片已关闭，请重新导入。")
                if u.path == "/api/process":
                    photo.process(body.get("level", "gentle"), body.get("options"))
                elif u.path == "/api/detect":
                    photo.detect(body.get("sensitivity", 50), body.get("size", 30), body.get("hairs", True), body.get("protect", True), body.get("polarity", "both"))
                elif u.path == "/api/edit":
                    photo.edit(body.get("mode"), body.get("points", []), body.get("diameter", 18))
                elif u.path == "/api/clone":
                    photo.clone(body.get("source"), body.get("points", []), body.get("diameter", 18))
                elif u.path == "/api/keep":
                    photo.keep_regions(body.get("points", []))
                elif u.path == "/api/undo":
                    photo.undo()
                elif u.path == "/api/clear":
                    photo.clear()
                elif u.path == "/api/heal":
                    photo.heal()
                elif u.path == "/api/export":
                    data, mime, suffix = photo.export(body.get("format", "tiff"))
                    name = Path(photo.name).stem + "_除尘" + suffix
                    self.send(data, mime=mime, headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name)})
                    return
                else:
                    self.send({"error": "操作不存在。"}, 404)
                    return
                self.send(photo.info())
        except (ValueError, TypeError, KeyError) as e:
            self.send({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.send({"error": "处理失败。请使用 RGB／灰度 JPEG、PNG 或单页 TIFF；压缩 TIFF 可能需要额外解码组件。"}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                continue
            raise SystemExit("无法启动本地服务：" + str(e))
    else:
        raise SystemExit("启动失败：本机端口被占用。")
    print(f"\nFilmDust 已启动：http://127.0.0.1:{server.server_port}\n照片只在本机处理。保持此窗口打开；按 Control+C 停止。\n", flush=True)
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
