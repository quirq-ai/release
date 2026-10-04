"""The canary fixture service: GET /health and GET /greet?name=... on $PORT."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from greet import greeting

HEALTHY = True   # the planted bad canary flips this


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            status, body = (200, {"status": "ok"}) if HEALTHY else (500, {"status": "broken"})
        elif url.path == "/greet":
            status, body = 200, {"message": greeting(parse_qs(url.query).get("name", [""])[0])}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
