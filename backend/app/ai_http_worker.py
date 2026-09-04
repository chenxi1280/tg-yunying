"""Single HTTP exchange worker. Standard library only; no application state."""
import base64
import json
import socket
import sys
import urllib.error
import urllib.request


class RedirectResultUnknown(RuntimeError):
    pass


class ObservedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirected = False

    def redirect_request(self, *args, **kwargs):
        redirected = super().redirect_request(*args, **kwargs)
        self.redirected = redirected is not None
        return redirected


def exchange(spec):
    raw = base64.b64decode(spec["body"]) if spec["body"] is not None else None
    request = urllib.request.Request(spec["url"], data=raw, headers=spec["headers"], method=spec["method"])
    http_error = False
    redirect_handler = ObservedRedirectHandler()
    try:
        response = urllib.request.build_opener(redirect_handler).open(request, timeout=spec["timeout"])
    except urllib.error.HTTPError as error:
        response = error
        http_error = True
    except urllib.error.URLError as error:
        if redirect_handler.redirected:
            raise RedirectResultUnknown("ai_http_redirect_result_unknown") from error
        raise
    with response:
        return {"status": response.status, "http_error": http_error, "headers": list(response.headers.items()),
                "body": base64.b64encode(response.read()).decode("ascii")}


def main():
    try:
        result = exchange(json.loads(sys.stdin.buffer.read()))
    except urllib.error.URLError as error:
        before_call = isinstance(error.reason, (ConnectionRefusedError, socket.gaierror))
        result = {"error": "connection_not_started" if before_call else "network_result_unknown",
                  "error_type": type(error.reason).__name__}
    except Exception as error:
        result = {"error": "http_exchange_result_unknown", "error_type": type(error).__name__}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
