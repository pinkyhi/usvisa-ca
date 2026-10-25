"""Small STOMP 1.2 codec; WebSocket framing/TLS is handled by websocket-client."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    command: str
    headers: dict[str, str]
    body: bytes = b""


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace(":", "\\c")


def _unescape(value):
    result, index = [], 0
    codes = {"r": "\r", "n": "\n", "c": ":", "\\": "\\"}
    while index < len(value):
        if value[index] == "\\":
            index += 1
            if index == len(value) or value[index] not in codes:
                raise ValueError("Invalid STOMP header escape")
            result.append(codes[value[index]])
        else:
            result.append(value[index])
        index += 1
    return "".join(result)


def encode(command, headers=None, body=b"") -> str:
    body = body.encode("utf-8") if isinstance(body, str) else body
    headers = dict(headers or {})
    headers["content-length"] = str(len(body))
    escape = str if command in {"CONNECT", "CONNECTED"} else _escape
    lines = [command] + [f"{escape(key)}:{escape(value)}" for key, value in headers.items()]
    return "\n".join(lines) + "\n\n" + body.decode("utf-8") + "\x00"


class Decoder:
    def __init__(self):
        self.buffer = b""

    def feed(self, data) -> list[Frame]:
        self.buffer += data.encode("utf-8") if isinstance(data, str) else data
        if len(self.buffer) > 262_144:
            raise ValueError("STOMP buffer limit exceeded")
        frames = []
        while True:
            self.buffer = self.buffer.lstrip(b"\r\n")
            end = self.buffer.find(b"\n\n")
            delimiter = 2
            if end < 0:
                end = self.buffer.find(b"\r\n\r\n")
                delimiter = 4
            if end < 0:
                return frames
            lines = self.buffer[:end].decode("utf-8").splitlines()
            if not lines:
                raise ValueError("Missing STOMP command")
            command, headers = lines[0], {}
            for line in lines[1:]:
                key, separator, value = line.partition(":")
                if not separator:
                    raise ValueError("Invalid STOMP header")
                if command not in {"CONNECT", "CONNECTED"}:
                    key, value = _unescape(key), _unescape(value)
                headers.setdefault(key, value)
            start = end + delimiter
            if "content-length" in headers:
                length = int(headers["content-length"])
                if not 0 <= length <= 131_072:
                    raise ValueError("Invalid STOMP body length")
                finish = start + length
                if len(self.buffer) <= finish:
                    return frames
                if self.buffer[finish] != 0:
                    raise ValueError("Missing STOMP frame terminator")
            else:
                finish = self.buffer.find(b"\x00", start)
                if finish < 0:
                    return frames
            frames.append(Frame(command, headers, self.buffer[start:finish]))
            self.buffer = self.buffer[finish + 1:]
