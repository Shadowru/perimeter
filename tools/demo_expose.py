#!/usr/bin/env python3
"""Временный доступ к демо-интерфейсу снаружи — ТОЛЬКО для показа.

    python3 tools/demo_expose.py --port 8080 --target 8091

Зачем отдельный инструмент, а не флаг продукта: веб-интерфейс «Периметра»
слушает loopback и не имеет аутентификации — так и задумано, доступ к нему
даёт полный доступ к данным 1С. Выставлять его наружу в рабочей установке
нельзя; для этого есть SSH-туннель или обратный прокси заказчика с его же
контролем доступа.

Этот скрипт нужен, чтобы быстро показать демо на вымышленных данных. Он:
  • слушает указанный внешний адрес и проксирует на локальный интерфейс;
  • требует HTTP Basic-аутентификацию со сгенерированным паролем;
  • печатает громкое предупреждение и адрес со всеми данными для входа.

Останавливать сразу после показа.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import secrets
import socketserver
import sys
import urllib.error
import urllib.request

REALM = "Perimeter demo"


def make_handler(target: str, user: str, password: str):
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _unauthorized(self) -> None:
            body = b"Perimeter demo: authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{REALM}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == expected:
                return True
            self._unauthorized()
            return False

        def _forward(self, body: bytes | None) -> None:
            req = urllib.request.Request(
                target + self.path, data=body, method=self.command,
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
            try:
                with urllib.request.urlopen(req, timeout=1800) as resp:
                    self.send_response(resp.status)
                    ctype = resp.headers.get("Content-Type", "text/plain")
                    self.send_header("Content-Type", ctype)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    # Потоковая передача: ответы агента идут по мере генерации
                    while True:
                        chunk = resp.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
            except (urllib.error.URLError, OSError) as e:
                msg = f"демо недоступно: {e}".encode()
                self.send_response(502)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

        def do_GET(self) -> None:
            if self._authorized():
                self._forward(None)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length", 0))
            self._forward(self.rfile.read(length))

    return Proxy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--target", type=int, default=8091, help="порт локального интерфейса")
    ap.add_argument("--user", default="demo")
    args = ap.parse_args()

    password = secrets.token_urlsafe(12)
    handler = make_handler(f"http://127.0.0.1:{args.target}", args.user, password)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.host, args.port), handler) as srv:
        print(f"""
!!! ВНИМАНИЕ: демо открыто в интернет. Данные вымышленные, но это рабочий
!!! агент. Остановите сразу после показа (Ctrl+C) и удалите сервер.

    адрес:  http://<внешний-IP>:{args.port}/
    логин:  {args.user}
    пароль: {password}
""", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
