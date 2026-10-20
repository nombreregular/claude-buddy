"""Tiny raw-socket HTTP and server-sent-events client for the screen's tests.

Raw on purpose: the tests need to send things a browser never would (bad headers, a wrong Host,
half a request) and to read the stream block by block."""
import asyncio
import json


def _port(target):
    return target if isinstance(target, int) else target.port


async def http(target, method="GET", path="/", *, headers=None, body=b"", host=None, raw=None):
    """One raw HTTP/1.1 exchange. Returns (status, headers, body)."""
    port = _port(target)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        if raw is None:
            hdrs = {"Host": host or f"127.0.0.1:{port}", "Connection": "close"}
            hdrs.update(headers or {})
            if body:
                hdrs.setdefault("Content-Length", str(len(body)))
            head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
            raw = head.encode("latin-1") + body
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(-1), 5)
    finally:
        writer.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines[0] else 0
    got = {k.lower(): v for k, _, v in (line.partition(": ") for line in lines[1:])}
    return status, got, payload


class Stream:
    """A page's /events connection."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.status = 0
        self.headers = {}

    @classmethod
    async def open(cls, target, path="/events"):
        port = _port(target)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        stream = cls(reader, writer)
        head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)).decode("latin-1").split("\r\n")
        stream.status = int(head[0].split(" ")[1])
        stream.headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in head[1:] if line)}
        return stream

    async def raw_block(self, timeout=3.0):
        return (await asyncio.wait_for(self.reader.readuntil(b"\n\n"), timeout)).decode("ascii")

    async def event(self, timeout=3.0):
        """The next (name, data) pair; blocks with only retry:/comments are skipped."""
        while True:
            block = await self.raw_block(timeout)
            fields = dict(line.split(": ", 1) for line in block.strip().split("\n") if ": " in line)
            if "event" in fields:
                return fields["event"], fields.get("data", "")

    async def state(self, pred=lambda v: True, timeout=3.0):
        end = asyncio.get_running_loop().time() + timeout
        while True:
            name, data = await self.event(max(0.05, end - asyncio.get_running_loop().time()))
            if name == "state" and pred(json.loads(data)):
                return json.loads(data)

    def close(self):
        self.writer.close()
