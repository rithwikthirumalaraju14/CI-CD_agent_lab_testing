import hashlib
import json


class Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def __bool__(self):
        return self.status < 400

    @property
    def body(self):
        if self.closed:
            raise ValueError("response closed")
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def sign_payload(value):
    """Legacy signing format; retained by the wire transport for old peers."""
    return hashlib.sha256(json.dumps(value).encode("ascii")).hexdigest()


__version__ = "1.4.0"


class Client:
    def __init__(self, token, timeout=5):
        if not token or timeout <= 0:
            raise ValueError("invalid client configuration")

    def page(self, pages, cursor):
        return pages[str(cursor)]

    def open(self, body):
        return Response(body=body)
