"""Pluggable push channels: Bark, ServerChan, PushPlus, Telegram, generic webhook."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import httpx

log = logging.getLogger("twnews.push")


@dataclass
class PushMessage:
    title: str
    body: str               # plain text, a few lines
    kind: str               # regular | flash | sample
    file_path: Path | None = None
    file_url: str | None = None  # public link to the briefing, if one is configured


class PushError(Exception):
    pass


class Channel:
    type: ClassVar[str] = ""
    default_max_file_bytes: ClassVar[int] = 0  # 0 = channel cannot carry files

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("name") or self.type
        self.max_file_bytes = int(cfg.get("max_file_bytes", self.default_max_file_bytes))

    def can_attach(self, msg: PushMessage) -> bool:
        return (self.max_file_bytes > 0 and msg.file_path is not None and msg.file_path.exists()
                and msg.file_path.stat().st_size <= self.max_file_bytes)

    def text_with_link(self, msg: PushMessage, attached: bool) -> str:
        body = msg.body
        if attached:
            return body
        if msg.file_url:
            return f"{body}\n\n完整简报：{msg.file_url}"
        if msg.file_path:
            return f"{body}\n\n完整简报已保存：{msg.file_path.name}"
        return body

    async def send(self, client: httpx.AsyncClient, msg: PushMessage) -> str:
        raise NotImplementedError

    @staticmethod
    def _check(resp: httpx.Response, ok: bool | None = None) -> None:
        if resp.status_code >= 400 or ok is False:
            raise PushError(f"HTTP {resp.status_code}: {resp.text[:300]}")


class BarkChannel(Channel):
    """https://bark.day.app — iOS. Text only; the briefing is delivered as a tap-through URL."""
    type = "bark"

    async def send(self, client, msg):
        server = self.cfg.get("server", "https://api.day.app").rstrip("/")
        payload = {
            "device_key": self.cfg["device_key"],
            "title": msg.title,
            "body": self.text_with_link(msg, attached=False),
            "group": self.cfg.get("group", "台湾新闻"),
            "level": "timeSensitive" if msg.kind == "flash" else self.cfg.get("level", "active"),
        }
        if msg.file_url:
            payload["url"] = msg.file_url
        if self.cfg.get("sound"):
            payload["sound"] = self.cfg["sound"]
        if self.cfg.get("icon"):
            payload["icon"] = self.cfg["icon"]
        resp = await client.post(f"{server}/push", json=payload)
        self._check(resp, resp.json().get("code") == 200 if resp.status_code < 400 else None)
        return "link" if msg.file_url else "text"


class ServerChanChannel(Channel):
    """https://sct.ftqq.com — WeChat via ServerChan Turbo. Markdown text only."""
    type = "serverchan"

    async def send(self, client, msg):
        key = self.cfg["sendkey"]
        m = re.match(r"^sctp(\d+)t", key)  # ServerChan³ keys embed the user id
        url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m else f"https://sctapi.ftqq.com/{key}.send"
        desp = self.text_with_link(msg, attached=False).replace("\n", "\n\n")
        resp = await client.post(url, data={"title": msg.title[:32], "desp": desp})
        self._check(resp, resp.json().get("code") == 0 if resp.status_code < 400 else None)
        return "link" if msg.file_url else "text"


class PushPlusChannel(Channel):
    """https://www.pushplus.plus — WeChat. Markdown text only."""
    type = "pushplus"

    async def send(self, client, msg):
        payload = {
            "token": self.cfg["token"],
            "title": msg.title,
            "content": self.text_with_link(msg, attached=False).replace("\n", "\n\n"),
            "template": "markdown",
        }
        if self.cfg.get("topic"):
            payload["topic"] = self.cfg["topic"]
        if self.cfg.get("channel"):
            payload["channel"] = self.cfg["channel"]
        resp = await client.post("https://www.pushplus.plus/send", json=payload)
        self._check(resp, resp.json().get("code") == 200 if resp.status_code < 400 else None)
        return "link" if msg.file_url else "text"


class TelegramChannel(Channel):
    """Telegram bot: sends the .docx itself (bots may upload up to 50 MB)."""
    type = "telegram"
    default_max_file_bytes = 50 * 1024 * 1024

    async def send(self, client, msg):
        api = f"{self.cfg.get('api_base', 'https://api.telegram.org').rstrip('/')}/bot{self.cfg['bot_token']}"
        chat_id = str(self.cfg["chat_id"])
        if self.can_attach(msg):
            caption = f"{msg.title}\n\n{msg.body}"[:1024]
            with msg.file_path.open("rb") as fh:
                resp = await client.post(f"{api}/sendDocument", data={"chat_id": chat_id, "caption": caption},
                                         files={"document": (msg.file_path.name, fh, _DOCX_MIME)})
            self._check(resp, resp.json().get("ok") if resp.status_code < 400 else None)
            return "file"
        text = f"{msg.title}\n\n{self.text_with_link(msg, attached=False)}"[:4096]
        resp = await client.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": text,
                                                            "disable_web_page_preview": True})
        self._check(resp, resp.json().get("ok") if resp.status_code < 400 else None)
        return "link" if msg.file_url else "text"


class WebhookChannel(Channel):
    """Generic HTTP webhook. Sends multipart (JSON 'payload' field + 'file') when the file fits,
    otherwise a JSON body containing `file_url`."""
    type = "webhook"
    default_max_file_bytes = 10 * 1024 * 1024

    async def send(self, client, msg):
        url = self.cfg["url"]
        headers = dict(self.cfg.get("headers") or {})
        payload = {"title": msg.title, "body": msg.body, "kind": msg.kind, "file_url": msg.file_url,
                   "file_name": msg.file_path.name if msg.file_path else None}
        if self.cfg.get("send_file", True) and self.can_attach(msg):
            with msg.file_path.open("rb") as fh:
                resp = await client.post(url, headers=headers, data={"payload": json.dumps(payload, ensure_ascii=False)},
                                         files={"file": (msg.file_path.name, fh, _DOCX_MIME)})
            self._check(resp)
            return "file"
        payload["body"] = self.text_with_link(msg, attached=False)
        resp = await client.post(url, headers=headers, json=payload)
        self._check(resp)
        return "link" if msg.file_url else "text"


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CHANNEL_TYPES: dict[str, type[Channel]] = {
    c.type: c for c in (BarkChannel, ServerChanChannel, PushPlusChannel, TelegramChannel, WebhookChannel)
}


def build_channels(channel_cfgs: list[dict]) -> list[Channel]:
    channels = []
    for cfg in channel_cfgs:
        if not cfg.get("enabled", True):
            continue
        cls = CHANNEL_TYPES.get(cfg["type"])
        if cls is None:
            raise ValueError(f"unknown push channel type {cfg['type']!r}; known: {sorted(CHANNEL_TYPES)}")
        channels.append(cls(cfg))
    return channels


class Pusher:
    def __init__(self, channels: list[Channel], user_agent: str, retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.channels = channels
        self.retries = retries
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), headers={"User-Agent": user_agent},
                                        transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _send_one(self, ch: Channel, msg: PushMessage) -> tuple[str, bool, str]:
        delay = 2.0
        for attempt in range(self.retries + 1):
            try:
                mode = await ch.send(self.client, msg)
                log.info("push sent", extra={"channel": ch.name, "mode": mode, "kind": msg.kind})
                return ch.name, True, mode
            except (httpx.HTTPError, PushError, ValueError, KeyError) as exc:
                if attempt >= self.retries or isinstance(exc, KeyError):
                    log.error("push failed", extra={"channel": ch.name, "error": repr(exc)})
                    return ch.name, False, repr(exc)
                await asyncio.sleep(delay + random.uniform(0, 1))
                delay = min(delay * 2, 60)
        return ch.name, False, "unreachable"

    async def push(self, msg: PushMessage) -> list[tuple[str, bool, str]]:
        if not self.channels:
            log.info("no push channels enabled; skipping push")
            return []
        return list(await asyncio.gather(*(self._send_one(ch, msg) for ch in self.channels)))
