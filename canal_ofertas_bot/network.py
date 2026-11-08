from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit
import zlib


class NetworkError(Exception):
    def __init__(self, message: str = "No se pudo conectar con el servicio remoto.", *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def public_addresses(host: str, port: int = 443) -> list[str]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(value).is_global or
                            ipaddress.ip_address(value).is_multicast for value in addresses):
        raise NetworkError("La dirección remota no pertenece a una red pública permitida.")
    return addresses


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the already validated IP, retaining hostname verification and SNI.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class HTTPS:
    """No proxy environment, no automatic redirects, bounded decompressed response."""

    def request(self, method: str, url: str, *, body: bytes | None = None,
                headers: dict | None = None, timeout: float = 20, max_bytes: int = 5_000_000) -> Response:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise NetworkError("Solo se permiten conexiones HTTPS verificadas.")
        started = time.monotonic()
        connection = None
        attempted_send = False
        try:
            addresses = public_addresses(parsed.hostname)
            for address in addresses:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                connection = PinnedConnection(parsed.hostname, address, remaining)
                try:
                    connection.connect()
                    break
                except OSError:
                    connection.close()
                    connection = None
            if connection is None:
                raise OSError
            request_headers = {"Accept-Encoding": "gzip", "User-Agent": "AmazonOfertasTelegram/1.0"}
            request_headers.update(headers or {})
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            connection.sock.settimeout(remaining)
            attempted_send = True
            connection.request(method, (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), body=body, headers=request_headers)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            connection.sock.settimeout(remaining)
            response = connection.getresponse()
            values = {k.lower(): v for k, v in response.getheaders()}
            length = values.get("content-length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise NetworkError("La respuesta excede el tamaño permitido.", uncertain=attempted_send)
            encoding = values.get("content-encoding", "identity").lower()
            if encoding not in ("identity", "gzip"):
                raise NetworkError("Compresión remota no admitida.", uncertain=attempted_send)
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            data = bytearray()
            wire_size = 0
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1))
                if not chunk:
                    break
                wire_size += len(chunk)
                if wire_size > max_bytes:
                    raise NetworkError("La respuesta excede el tamaño permitido.", uncertain=attempted_send)
                data.extend(decoder.decompress(chunk, max_bytes - len(data) + 1) if decoder else chunk)
                if len(data) > max_bytes or (decoder and decoder.unconsumed_tail):
                    raise NetworkError("La respuesta descomprimida excede el tamaño permitido.", uncertain=attempted_send)
            if decoder and not decoder.eof:
                raise NetworkError("Respuesta comprimida incompleta.", uncertain=attempted_send)
            return Response(response.status, values, bytes(data))
        except NetworkError:
            raise
        except (OSError, http.client.HTTPException, ValueError, zlib.error):
            raise NetworkError("Conexión interrumpida o respuesta remota inválida.", uncertain=attempted_send) from None
        finally:
            if connection:
                connection.close()
