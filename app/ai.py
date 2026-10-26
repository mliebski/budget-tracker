"""Client for the local Ollama server. Budget data is only ever sent to this address.

Ollama runs natively on the box; from inside the app container the host is
reachable as host.docker.internal (see docker-compose.yml and the README).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from sqlalchemy.orm import Session

from .services import get_setting

DEFAULT_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
REPORT_TIMEOUT = 900   # a CPU-only box can take minutes for a long structured answer
CHAT_TIMEOUT = 600
KEEP_ALIVE = "15m"     # keep the model in memory between questions


class AiError(Exception):
    pass


def config(db: Session) -> tuple[str, str]:
    """(server address, model name); the model is empty until one is chosen in Settings."""
    return (get_setting(db, "ai_url") or DEFAULT_URL).rstrip("/"), get_setting(db, "ai_model")


def _open(url: str, path: str, payload: dict | None = None, timeout: float = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{url}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    # Never go through a proxy: the server is on the home network.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise AiError(f"Ollama said: {detail[:300]}")
    except (OSError, ValueError) as e:
        raise AiError(f"Ollama is not reachable at {url} ({getattr(e, 'reason', e)}).")


STATUS_TIMEOUT = 2
FAILURE_TTL = 30  # seconds a failed check is remembered, so a down server doesn't slow every page load
_failed: dict[str, tuple[float, dict]] = {}


def status(url: str) -> dict:
    """{"ok", "version", "models": [{"name", "size"}], "detail"} for the Settings page."""
    cached = _failed.get(url)
    if cached and time.monotonic() - cached[0] < FAILURE_TTL:
        return cached[1]
    try:
        with _open(url, "/api/version", timeout=STATUS_TIMEOUT) as r:
            version = json.load(r).get("version", "")
        with _open(url, "/api/tags", timeout=STATUS_TIMEOUT) as r:
            models = [{"name": m["name"], "size": m.get("details", {}).get("parameter_size", "")}
                      for m in json.load(r).get("models", [])]
    except AiError as e:
        result = {"ok": False, "version": "", "models": [], "detail": str(e)}
        _failed[url] = (time.monotonic(), result)
        return result
    _failed.pop(url, None)
    return {"ok": True, "version": version, "models": sorted(models, key=lambda m: m["name"]),
            "detail": f"Ollama {version}, {len(models)} model{'s' if len(models) != 1 else ''} installed"}


def context_size(messages: list[dict]) -> int:
    """Ollama's default context window silently cuts long prompts; size it to the data sent."""
    chars = sum(len(m["content"]) for m in messages)
    needed = chars // 3 + 4096  # ~3 characters per token for numbers, plus room for the answer
    return min(32768, max(8192, 1 << (needed - 1).bit_length()))


THINK = re.compile(r"<think>.*?</think>", re.S)


def chat(url: str, model: str, messages: list[dict], schema: dict | None = None) -> str:
    """One complete answer. With a JSON schema, Ollama constrains the answer to it."""
    if not model:
        raise AiError("No AI model chosen yet. Pick one in Settings.")
    payload = {"model": model, "messages": messages, "stream": False, "keep_alive": KEEP_ALIVE,
               "options": {"temperature": 0.2, "num_ctx": context_size(messages)}}
    if schema:
        payload["format"] = schema
    with _open(url, "/api/chat", payload, timeout=REPORT_TIMEOUT) as r:
        answer = json.load(r)
    return THINK.sub("", answer.get("message", {}).get("content", "")).strip()


def _lines(response, url: str) -> Iterator[bytes]:
    """The lines of a streamed answer; a dropped connection or timeout becomes an AiError."""
    try:
        yield from response
    except OSError as e:
        raise AiError(f"The connection to Ollama at {url} was lost ({getattr(e, 'reason', e)}).")


def chat_stream(url: str, model: str, messages: list[dict]) -> Iterator[tuple[str, str]]:
    """Yields ("thinking" | "text", chunk) while the model writes its answer."""
    if not model:
        raise AiError("No AI model chosen yet. Pick one in Settings.")
    payload = {"model": model, "messages": messages, "stream": True, "keep_alive": KEEP_ALIVE,
               "options": {"temperature": 0.3, "num_ctx": context_size(messages)}}
    in_think = False
    with _open(url, "/api/chat", payload, timeout=CHAT_TIMEOUT) as r:
        for line in _lines(r, url):
            if not line.strip():
                continue
            try:
                part = json.loads(line)
            except ValueError:
                raise AiError("Ollama sent an answer that could not be read. Try again.")
            if part.get("error"):
                raise AiError(f"Ollama said: {part['error']}")
            message = part.get("message", {})
            if message.get("thinking"):
                yield "thinking", message["thinking"]
            text = message.get("content", "")
            # Older Ollama versions put a reasoning model's thoughts inline in <think> tags.
            if "<think>" in text:
                before, text = text.split("<think>", 1)
                in_think = True
                if before.strip():
                    yield "text", before
            if in_think:
                if "</think>" not in text:
                    yield "thinking", text
                    continue
                thought, text = text.split("</think>", 1)
                in_think = False
                yield "thinking", thought
            if text:
                yield "text", text
            if part.get("done"):
                break
