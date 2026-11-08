from __future__ import annotations

import asyncio
import json
import re
import secrets

from .network import HTTPS, NetworkError


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0, *, not_modified: bool = False):
        # Never retain Bot API descriptions: they can echo content or private URLs.
        super().__init__(f"Telegram devolvió el código {code}.")
        self.code, self.retry_after = code, retry_after
        self.not_modified = not_modified

    @property
    def uncertain(self) -> bool:
        return self.code >= 500


class Telegram:
    def __init__(self, token: str, timeout: int = 20, transport=None):
        self._token, self.timeout = token, timeout
        self.transport = transport or HTTPS()

    async def call(self, method: str, *, upload: bytes | None = None, request_timeout: int | None = None, **params):
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("Método de Telegram no válido.")
        if upload is None:
            body = json.dumps(params, ensure_ascii=False).encode()
            content_type = "application/json"
        else:
            boundary = secrets.token_hex(24)
            parts = []
            for key, value in params.items():
                rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{rendered}\r\n'.encode())
            parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="oferta.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode(),
                          upload, f"\r\n--{boundary}--\r\n".encode()])
            body = b"".join(parts)
            content_type = f"multipart/form-data; boundary={boundary}"
        response = await asyncio.to_thread(
            self.transport.request, "POST", f"https://api.telegram.org/bot{self._token}/{method}",
            body=body, headers={"Content-Type": content_type}, timeout=request_timeout or self.timeout,
            max_bytes=5_000_000)
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError):
            raise NetworkError("Telegram devolvió una respuesta ilegible.", uncertain=True) from None
        if not payload.get("ok"):
            raise TelegramError(int(payload.get("error_code", response.status)),
                                int(payload.get("parameters", {}).get("retry_after", 0)),
                                not_modified=str(payload.get("description", "")).startswith("Bad Request: message is not modified"))
        return payload.get("result")

    async def message(self, chat_id, text, keyboard=None, entities=None, *, reply_markup=None):
        params = {"chat_id": chat_id, "text": text}
        if keyboard is not None and reply_markup is not None:
            raise ValueError("El mensaje no puede tener dos teclados a la vez.")
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        elif reply_markup is not None:
            params["reply_markup"] = reply_markup
        if entities:
            params["entities"] = entities
        return await self.call("sendMessage", **params)

    async def photo(self, chat_id, photo: bytes | str, caption: str, entities: list, keyboard=None):
        params = {"chat_id": chat_id, "caption": caption, "caption_entities": entities}
        if keyboard is not None:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        if isinstance(photo, str):
            return await self.call("sendPhoto", photo=photo, **params)
        return await self.call("sendPhoto", upload=photo, **params)

    async def download_photo(self, file_id: str, max_bytes: int) -> bytes:
        record = await self.call("getFile", file_id=file_id)
        if record.get("file_size", 0) > max_bytes:
            raise ValueError("La foto supera el tamaño permitido.")
        path = record.get("file_path", "")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path or path.startswith("/"):
            raise ValueError("Telegram no devolvió una ruta de foto válida.")
        response = await asyncio.to_thread(self.transport.request, "GET", f"https://api.telegram.org/file/bot{self._token}/{path}",
                                           timeout=self.timeout, max_bytes=max_bytes)
        if response.status != 200:
            raise ValueError("No se pudo descargar la foto de Telegram.")
        return response.body
