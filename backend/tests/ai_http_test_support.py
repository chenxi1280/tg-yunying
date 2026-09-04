import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HTTP_IO_TEST_BUDGET_SECONDS = 2
HTTP_SCHEDULING_TOLERANCE_SECONDS = 0.5


@contextmanager
def local_http_server(*, on_request=None, response_body=None):
    stopped = threading.Event()
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            observed.append((self.path, self.headers.get("Authorization")))
            if on_request is not None:
                on_request(self.path)
            if self.path.startswith("/stall"):
                stopped.wait(3)
                return
            self._respond()

        do_GET = do_POST

        def _respond(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            slow = self.path.startswith(("/drip", "/error-drip"))
            status = 429 if self.path.startswith("/error") else 200
            body = _response_body(self.path) if response_body is None else response_body(self.path)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "2")
            self.end_headers()
            try:
                if slow:
                    for byte in body:
                        if stopped.wait(0.025):
                            return
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _response_body(path):
    if path.startswith("/gateway"):
        content = json.dumps({"drafts": [{"content": "QA 自动化测试回复"}]})
        return json.dumps({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 1}}).encode()
    return json.dumps({"ok": True, "message": "QA response " * 20}).encode()
