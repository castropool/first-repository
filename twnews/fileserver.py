"""Optional tiny HTTP server so text-only push channels (Bark, ServerChan, PushPlus) can link to
the .docx. Serves only briefing files, under an unguessable token path."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from .briefing import FILENAME_RE

log = logging.getLogger("twnews.fileserver")


def make_handler(directory: Path, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TwNewsMonitor"
        sys_version = ""

        def do_GET(self):  # noqa: N802
            parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
            if len(parts) != 2 or parts[0] != token or not FILENAME_RE.match(parts[1]):
                self.send_error(404)
                return
            path = directory / parts[1]
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(parts[1])}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.info("file request", extra={"client": self.client_address[0], "request": fmt % args})

    return Handler


class FileServer:
    def __init__(self, directory: str, bind: str, port: int, token: str):
        if len(token) < 16:
            raise ValueError("links.file_server.token must be at least 16 characters")
        self.httpd = ThreadingHTTPServer((bind, port), make_handler(Path(directory), token))
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fileserver", daemon=True)

    def start(self) -> None:
        self.thread.start()
        log.info("file server listening", extra={"address": "%s:%s" % self.httpd.server_address[:2]})

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
