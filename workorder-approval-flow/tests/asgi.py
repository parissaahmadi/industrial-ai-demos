"""A tiny in-process ASGI client (the sandbox has no httpx). Same wire format a real server uses."""

import asyncio
import json
from urllib.parse import urlsplit


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.raw = status, headers, body

    @property
    def json(self):
        return json.loads(self.raw) if self.raw else None


class Client:
    def __init__(self, app, token=None):
        self.app, self.token = app, token
        self.crashed = None

    def as_user(self, token):
        c = Client(self.app, token)
        return c

    def request(self, method, url, json_body=None, headers=None, raw=None, token="__default__"):
        parts = urlsplit(url)
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        tok = self.token if token == "__default__" else token
        if tok:
            hdrs.setdefault("authorization", f"Bearer {tok}")
        body = raw if raw is not None else (json.dumps(json_body).encode() if json_body is not None else b"")
        if body:
            hdrs.setdefault("content-type", "application/json")
        hdrs.setdefault("content-length", str(len(body)))
        if hdrs["content-length"] is None:  # simulate a chunked upload: no declared length
            del hdrs["content-length"]
        return asyncio.run(self._call(method, parts.path, parts.query, hdrs, body))

    async def _call(self, method, path, query, headers, body):
        sent, msgs = [], [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                 "client": ("test", 1), "server": ("test", 80), "scheme": "http", "root_path": ""}  # fmt: skip
        try:
            await self.app(scope, receive, send)
        except Exception as e:  # the server layer re-raises after answering 500, like a real server logs it
            self.crashed = e
        start = next(m for m in sent if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return Response(start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, data)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, json_body=None, **kw):
        return self.request("POST", url, json_body, **kw)

    def patch(self, url, json_body=None, **kw):
        return self.request("PATCH", url, json_body, **kw)
