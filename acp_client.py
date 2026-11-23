"""Small ACP v1 stdio client, independent of Hermes's private implementation.

The operator selects the OpenCode CLI (or its Windows batch launcher). All host
work is returned as remote tool calls; this module never executes model tools.
"""
from contextlib import contextmanager
import asyncio
import hashlib
import json
import logging
import math
import os
import queue
import re
import secrets
import sys
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid
import weakref

from .constants import (ARGS_ENV, DEFAULT_ARGS, DEFAULT_MODEL, FALLBACK_ARGS, RELAY_AGENT, VERSION, opencode_agent,
                        opencode_args, opencode_command)
from .launcher import ProcessTree
from . import mcp_relay

_startup_args = {}
_startup_lock = threading.Lock()
_log = logging.getLogger(__name__)


def diagnostics_enabled():
    return os.getenv("HERMES_OPENCODE_ACP_DIAGNOSTICS") == "1"


MAX_FRAME = 4 * 1024 * 1024
OPEN_TAG, CLOSE_TAG = "<hermes_tool_call>", "</hermes_tool_call>"
CONTRACT = (
    "REMOTE TOOL PROTOCOL: This conversation belongs to Hermes, not OpenCode. "
    "All tools described below are REMOTE Hermes tools. To request one, emit ONLY "
    '<hermes_tool_call>{"id":"unique_call_id","type":"function","function":'
    '{"name":"tool_name","arguments":"{...}"}}</hermes_tool_call> as your final TEXT response. '
    "The final closing tag is optional when the complete JSON object ends the response. "
    "arguments must be a JSON object encoded as a JSON string. Stop after requesting the tool; "
    "Hermes executes it AFTER your response and supplies the result in the next transcript. "
    "Do NOT use native OpenCode tools, shell, files, skills, or subagents: leave execution to Hermes. "
    "Do not claim an action happened before a real tool result exists. If the transcript "
    "already contains the needed tool result, answer from it. If no tool is needed, answer normally."
)
REPAIR = ("Your previous reply could not be dispatched to Hermes: {reason}. "
          "Resend the whole reply now. Follow the REMOTE TOOL PROTOCOL exactly.")
# ACP tool-call statuses that mean a native OpenCode tool actually ran.
NATIVE_RAN = ("in_progress", "completed")


class Record(SimpleNamespace):
    """Structural OpenAI-compatible result with no SDK dependency."""
    def model_dump(self, *, exclude_none=False, **kwargs):
        def dump(value):
            if isinstance(value, SimpleNamespace):
                return {key: dump(item) for key, item in vars(value).items()
                        if not (exclude_none and item is None)}
            if isinstance(value, list):
                return [dump(item) for item in value]
            if isinstance(value, dict):
                return {key: dump(item) for key, item in value.items()}
            return value
        return dump(self)


def completion_usage(raw):
    if not isinstance(raw, dict):
        return None
    inputs, outputs, total = (raw.get(k) for k in ("inputTokens", "outputTokens", "totalTokens"))
    read, written, thought = (raw.get(k, 0) for k in ("cachedReadTokens", "cachedWriteTokens", "thoughtTokens"))
    if any(type(v) is not int or v < 0 for v in (inputs, outputs, total, read, written, thought)):
        return None
    # Resolve inclusive/exclusive counters against the reported total. An ambiguous
    # breakdown cannot provide a reliable compression anchor.
    pairs = {(p, c) for p in (inputs, inputs + read + written)
             for c in (outputs, outputs + thought)
             if p + c == total and p >= read + written and c >= thought}
    if len(pairs) != 1:
        return None
    prompt, completion = pairs.pop()
    return Record(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                  prompt_tokens_details=Record(cached_tokens=read),
                  completion_tokens_details=Record(reasoning_tokens=thought))


class WorkerDied(RuntimeError):
    """The ACP process exited or its stdio closed before a response arrived."""
    # False once the turn produced output: resending could repeat inference.
    retryable = True


class ToolProtocolError(ValueError):
    """A deterministic tool failure Hermes should not retry."""
    status_code = 400


class NativeToolUsed(ToolProtocolError):
    """OpenCode reported activity outside the verified Hermes tool path."""


class RpcError(RuntimeError):
    """A JSON-RPC error response. Vendor text is never echoed or logged."""
    def __init__(self, method, code, *, missing_session=False, cause=None):
        self.missing_session = missing_session
        hint = {"provider.quota": "provider.quota: check OpenCode quota",
                "provider.auth": "provider.auth: check OpenCode credentials"}.get(cause, "OpenCode supplied no recognized cause")
        super().__init__(f"[ACP-RPC] stage={method} code={code}. {hint}.")
        self.method = method
        self.code = code


class SessionState:
    def __init__(self, session_id, config, tools_signature):
        self.session_id = session_id
        self.config = config
        self.tools_signature = tools_signature
        self.represented = []
        self.last_used = time.monotonic()


class _Active:
    def __init__(self, rid, session_id, sinks):
        self.rid = rid
        self.session_id = session_id
        self.sinks = sinks
        self.started = time.monotonic()
        self.saw_update = False
        self.finished = threading.Event()


def effective_timeout(value):
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        values = [getattr(value, key, None) for key in ("read", "write", "connect", "pool")]
        seconds = max((float(item) for item in values if isinstance(item, (int, float))), default=900.0)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ACP timeout must be a finite positive number of seconds")
    return seconds


def seconds_left(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("OpenCode ACP request deadline expired")
    return seconds


@contextmanager
def acquire_until(lock, deadline, stage):
    started = time.monotonic()
    if not lock.acquire(timeout=seconds_left(deadline)):
        raise TimeoutError(f"OpenCode ACP timed out waiting for {stage}")
    try:
        if diagnostics_enabled():
            _log.info("ACP %s queue wait %.3fs", stage, time.monotonic() - started)
        yield
    finally:
        lock.release()


def options(option):
    """ACP select options can be flat or grouped. Never normalize model IDs."""
    result = []
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("value"), str):
            result.append(item["value"])
        elif isinstance(item.get("options"), list):
            result.extend(child["value"] for child in item["options"]
                          if isinstance(child, dict) and isinstance(child.get("value"), str))
    return result


def _config_candidates(session, category):
    return [item for item in (session or {}).get("configOptions") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (item.get("id") == category or item.get("category") == category)]


def config_option(session, category, required=None):
    candidates = _config_candidates(session, category)
    for item in candidates:
        if required is None or required in options(item):
            return item
    offered = sorted({value for item in candidates for value in options(item)})
    raise RuntimeError(f"OpenCode ACP required {category} {required!r} was not advertised; offered={offered!r}. No prompt was sent.")


def session_model(config):
    for option in _config_candidates(config, "model"):
        if option.get("currentValue"):
            return option["currentValue"]
    return (config.get("models") or {}).get("currentModelId") or DEFAULT_MODEL


def selected_value(result, config_id):
    return next((item.get("currentValue") for item in (result or {}).get("configOptions", [])
                 if isinstance(item, dict) and item.get("id") == config_id), None)


def _plain(value):
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def render_content(content, images=True):
    """Render message content as text. images=False marks inline images as not forwarded."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item if isinstance(item, str) else _render_part(item, images)
                         for item in content if isinstance(item, (str, dict)))
    return json.dumps(content, ensure_ascii=False)


def image_block(item):
    if not isinstance(item, dict) or item.get("type") != "image_url":
        return None
    image = item.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    header, sep, data = url.partition(",")
    if not sep or not header.endswith(";base64") or not data:
        raise ValueError("Malformed inline image")
    return {"type": "image", "mimeType": header[5:-7], "data": data}


def _render_part(item, images=True):
    if image_block(item):
        if images:
            return "[See attached image]"
        return ("[Inline image not forwarded: this OpenCode agent did not advertise image input. "
                "Use the vision_analyze tool on the original file if its contents matter.]")
    kind = item.get("type")
    if kind in (None, "text", "input_text", "output_text"):
        return str(item.get("text", ""))
    # A visible marker keeps one attachment from breaking every later turn.
    image = item.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    source = f" ({url})" if isinstance(url, str) and not url.startswith("data:") else ""
    return (f"[{kind} attachment{source} not forwarded: this bridge is text-only. "
            "Use the vision_analyze tool on the original file if its contents matter.]")


def message_signature(message):
    data = _plain(message)
    if not isinstance(data, dict):
        data = {}
    tool_calls = []
    for call in data.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        tool_calls.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments})
    payload = {
        "role": data.get("role"),
        # Rendered text keeps formatting-only differences from breaking session reuse;
        # ordered image hashes still tell different attachments apart.
        "content": {"text": render_content(data.get("content")),
                    "images": [hashlib.sha256(url.encode("utf-8")).hexdigest()
                               for url in _inline_image_urls(data.get("content"))]},
        "tool_calls": tool_calls,
        "tool_call_id": data.get("tool_call_id"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _inline_image_urls(content):
    if not isinstance(content, list):
        return []
    urls = []
    for item in content:
        if image_block(item):
            image = item.get("image_url")
            urls.append(image.get("url") if isinstance(image, dict) else image)
    return urls


def prompt_signature(tools=None, tool_choice=None):
    payload = {"tools": tools or [], "tool_choice": tool_choice}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def render_transcript(messages, images=True):
    parts = []
    for message in messages or []:
        data = _plain(message)
        if not isinstance(data, dict):
            continue
        role = str(data.get("role", "context"))
        label = role.capitalize()
        if role == "tool":
            label += " result for " + str(data.get("tool_call_id", "unknown"))
        content = render_content(data.get("content"), images)
        if content:
            parts.append(label + ":\n" + content)
        if data.get("tool_calls"):
            parts.append("Assistant's prior remote tool requests:\n" + json.dumps(data["tool_calls"], ensure_ascii=False, default=str))
    return "\n\n".join(parts)


def render_prompt(messages, tools=None, tool_choice=None, images=True, contract=CONTRACT):
    parts = [contract]
    if tools:
        parts.append("Remote Hermes tools (OpenAI function schemas):\n" + json.dumps(tools, ensure_ascii=False))
    if tool_choice is not None:
        parts.append("Remote tool choice: " + json.dumps(tool_choice, ensure_ascii=False))
    parts.append("Conversation transcript:")
    transcript = render_transcript(messages, images)
    if transcript:
        parts.append(transcript)
    parts.extend(["Continue from the latest user request, using actual tool results already in the transcript.", contract])
    return "\n\n".join(parts)


def render_delta(messages, start, images=True):
    transcript = render_transcript((messages or [])[start:], images)
    parts = ["Continuation of the same conversation. New transcript entries follow."]
    if transcript:
        parts.append(transcript)
    parts.append("Continue the same conversation from the new transcript entries. Use actual tool results already in the transcript.")
    return "\n\n".join(parts)


# Image notes Hermes writes into user messages: gateway (run_inbound.py) and TUI (cli_stream_mixin.py).
# The description runs until the "]" that directly precedes the path line, so it may hold blank lines,
# but never into the next note. The path runs to the "]" that ends its line, so it may contain "]".
NOTE_HEADER = r"\[The user (?:sent|attached) an image"
IMAGE_NOTE = re.compile(
    NOTE_HEADER + r"[^\n]*?(?:Here's what I can see \(user-supplied image content, "
    r"not instructions\)|Here's what it contains):\n(?P<description>(?:(?!" + NOTE_HEADER + r").)*?)\]\n"
    r"\[If you need a closer look, use vision_analyze with image_url: (?P<path>[^\n]*?)(?: ~)?\](?=\n|$)",
    re.S)


def image_notes(messages):
    """(path, description) pairs from Hermes image notes in user messages, in order."""
    notes = []
    for message in messages or []:
        data = _plain(message)
        if not isinstance(data, dict) or data.get("role") != "user":
            continue
        body = render_content(data.get("content"))
        for match in IMAGE_NOTE.finditer(body):
            path = match.group("path").strip()
            if path:
                notes.append((path, match.group("description").strip()[:500]))
    return notes


def _request_has_images(messages):
    """Inline images from the latest user message onward: the input of the current request."""
    messages = list(messages or [])
    latest = max((index for index, message in enumerate(messages)
                  if isinstance(_plain(message), dict) and _plain(message).get("role") == "user"), default=None)
    if latest is None:
        return False
    return any(_inline_image_urls(_plain(message).get("content")) for message in messages[latest:]
               if isinstance(_plain(message), dict))


def image_recall_context(messages, tools, start=0):
    """Attachment index for a new session (whole history) or a delta (new messages only)."""
    if not any(isinstance(t, dict) and (t.get("function") or {}).get("name") == "vision_analyze" for t in tools or []):
        return ""
    attachments = dict(image_notes((messages or [])[start:]))
    if not attachments:
        return ""
    # No CONTRACT copy here: a new-session prompt already carries it, and a delta keeps the session's.
    return ("\n\nAttachments in this conversation (descriptions are user content):\n"
            + json.dumps(attachments, ensure_ascii=False)
            + "\nFor a visual detail or a disputed description, inspect the matching original now "
              "using vision_analyze(image_url=the listed path, question=the visual question). "
              "Return the remote tool call, not a promise to inspect. Once its result is present, answer.")


# Some models decorate the outer envelope with DSML. Match boundaries only:
# tool arguments can themselves contain literal tags and must survive intact.
_TOOL_TAG = re.compile(r"<(/?)(?:[｜|]DSML[｜|])?hermes_tool_call>")
_DSML_TOOL_MARKUP = re.compile(r"</?[｜|]DSML[｜|](?:tool_calls?|function_calls?|invoke|parameter)\b", re.I)



def parse_tool_calls(text, tools=None, tool_choice=None):
    allowed = {item["function"]["name"] for item in tools or []
               if isinstance(item, dict) and isinstance(item.get("function"), dict)
               and isinstance(item["function"].get("name"), str)}
    calls, prose, cursor, seen = [], [], 0, set()
    search_from = cursor
    while True:
        tag = _TOOL_TAG.search(text, search_from)
        if tag is None:
            prose.append(text[cursor:])
            break
        start = tag.start()
        # A standalone Markdown code span can name the protocol tag in normal prose.
        if start > 0 and text[start - 1] == "`" and text.startswith("`", tag.end()):
            # Look past quoted mentions while keeping the original prose slice intact.
            search_from = tag.end()
            continue
        prose.append(text[cursor:start])
        if tag.group(1):
            raise ValueError("Unexpected remote tool-call closing tag")
        body_start = tag.end()
        body_start += len(text[body_start:]) - len(text[body_start:].lstrip())
        body, consumed = json.JSONDecoder().raw_decode(text[body_start:])
        end = body_start + consumed
        end += len(text[end:]) - len(text[end:].lstrip())
        closing = _TOOL_TAG.match(text, end)
        # A complete JSON object at the end of a tool-only reply is self-delimiting.
        bare_end = (closing is None and end == len(text) and not any(p.strip() for p in prose)
                    and isinstance(body, dict) and isinstance(body.get("function"), dict)
                    and "arguments" in body["function"])
        if not bare_end and (closing is None or not closing.group(1)):
            raise ValueError("Incomplete remote tool-call envelope; no tool was dispatched")
        function = body.get("function") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("type") != "function" or not isinstance(function, dict) or function.get("name") not in allowed:
            raise ValueError("Unadvertised or invalid remote tool call; no tool was dispatched")
        if tool_choice == "none":
            raise ValueError("Remote tool call violates tool_choice=none")
        if isinstance(tool_choice, dict) and function["name"] != (tool_choice.get("function") or {}).get("name"):
            raise ValueError("Remote tool call violates the selected function")
        arguments = function.get("arguments", "{}")
        decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(decoded, dict):
            raise ValueError("Remote tool arguments must be a JSON object")
        call_id = body.get("id", "acp_" + uuid.uuid4().hex)
        if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
            raise ValueError("Remote tool IDs must be nonempty and unique")
        seen.add(call_id)
        calls.append(Record(id=call_id, type="function", call_id=call_id, response_item_id=None,
                            function=Record(name=function["name"], arguments=json.dumps(decoded, ensure_ascii=False))))
        cursor = end if bare_end else closing.end()
        search_from = cursor
    # Inspect only would-be chat text, never strings inside decoded arguments.
    if any(_DSML_TOOL_MARKUP.search(part) for part in prose):
        raise ValueError("Unsupported DSML tool envelope; resend using <hermes_tool_call> with JSON")
    if not calls and (tool_choice == "required" or isinstance(tool_choice, dict)):
        raise ValueError("The requested remote tool call is missing")
    return calls, "\n".join(part.strip() for part in prose if part.strip())


class BridgeWorker:
    """One long-lived ACP process with an inbox, session history and request routing."""

    def __init__(self, command, args, *, cwd, env, initialize_timeout=30.0, on_spawn=None):
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._inbox = queue.Queue()
        self._active = None
        self._dead = threading.Event()
        self._closed = False
        self._sessions = []
        self.prepared_session = None
        self._sequence = 0
        self._cwd = cwd
        self.image_input = False
        self.mcp_key = secrets.token_hex(32) if os.getenv("HERMES_OPENCODE_ACP_MCP") == "1" else None
        self.rpc_lock = threading.RLock()
        try:
            self.owner = ProcessTree(command, args, cwd=cwd, env=env)
        except OSError as exc:
            raise RuntimeError(f"Could not launch/contain OpenCode ACP executable {command!r}; "
                               "check its installation or set HERMES_OPENCODE_ACP_COMMAND to its absolute path") from exc
        self.proc = self.owner.proc
        if on_spawn is not None:
            on_spawn(self.owner)
        self._readers = [threading.Thread(target=self._pump, daemon=True),
                         threading.Thread(target=self._discard_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        try:
            initialized = self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "hermes-opencode-acp", "version": VERSION},
            }, timeout=initialize_timeout)
            prompt_caps = ((initialized.get("agentCapabilities") or {}).get("promptCapabilities") or {})
            # Only an explicit true allows image blocks; a respawned process decides again.
            self.image_input = prompt_caps.get("image") is True
        except WorkerDied as exc:
            self.close()
            raise WorkerDied(f"OpenCode ACP did not initialize with arguments {list(args)!r}; set "
                             "HERMES_OPENCODE_ACP_ARGS to the arguments this CLI needs") from exc
        except BaseException:
            self.close()
            raise

    @property
    def dead(self):
        return self._dead.is_set()

    def _send(self, message):
        wire = json.dumps(message, ensure_ascii=False)
        parts = (message.get("params") or {}).get("prompt") or []
        limit = 32 * 1024 * 1024 if any(p.get("type") == "image" for p in parts) else MAX_FRAME
        if len(wire.encode("utf-8")) > limit:
            raise ValueError("OpenCode ACP request exceeded the frame limit")
        with self._send_lock:
            if self._dead.is_set() or self.proc.poll() is not None:
                self._dead.set()
                raise WorkerDied("OpenCode ACP is no longer running")
            try:
                self.proc.stdin.write(wire + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._dead.set()
                raise WorkerDied("OpenCode ACP stdin closed") from exc

    def request(self, method, params, *, sinks=None, timeout=30.0):
        timeout = float(timeout)
        deadline = time.monotonic() + timeout
        with self._state_lock:
            if self._dead.is_set():
                raise WorkerDied("OpenCode ACP is no longer running")
            self._sequence += 1
            rid = self._sequence
            active = self._active = _Active(rid, (params or {}).get("sessionId"), sinks)
        try:
            # A full stdin pipe can block even before the response wait begins.
            # The caller retires the process tree on timeout, releasing the writer.
            sent = threading.Event()
            failures = []
            def send_request():
                try:
                    self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                except Exception as exc:
                    failures.append(exc)
                finally:
                    sent.set()

            threading.Thread(target=send_request, daemon=True).start()
            if not sent.wait(seconds_left(deadline)):
                raise TimeoutError(f"OpenCode ACP timed out writing {method}")
            if failures:
                raise failures[0]
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"OpenCode ACP timed out during {method}")
                try:
                    message = self._inbox.get(timeout=min(0.1, max(0.001, remaining)))
                except queue.Empty:
                    continue
                if isinstance(message, BaseException):
                    died = WorkerDied(str(message))
                    died.retryable = not active.saw_update
                    raise died from message
                if not isinstance(message, dict):
                    continue
                if message.get("id") != rid:
                    continue
                if "error" in message:
                    error = message["error"]
                    error = error if isinstance(error, dict) else {}
                    code = error.get("code")
                    code = code if type(code) is int else "unknown"
                    data = error.get("data")
                    data = data if isinstance(data, dict) else {}
                    name = data.get("errorName")
                    if diagnostics_enabled() and name in ("provider.quota", "provider.auth"):
                        _log.warning("OpenCode ACP %s failed: %s", method, name)
                    sid = active.session_id
                    missing = (method == "session/prompt" and code == -32602 and sid
                               and data.get("sessionId") == sid and not active.saw_update
                               and error.get("message") == f"Invalid params: session not found: {sid}")
                    raise RpcError(method, code, missing_session=bool(missing), cause=name if isinstance(name, str) else None)
                return message.get("result") or {}
        finally:
            with self._state_lock:
                if active.sinks is not None and diagnostics_enabled():
                    _log.info("ACP prompt %d finished %.3fs", rid, time.monotonic() - active.started)
                active.finished.set()
                if self._active is not None and self._active.rid == rid:
                    self._active = None

    def _pump(self):
        try:
            while True:
                line = self.proc.stdout.readline(MAX_FRAME + 1)
                if not line:
                    raise WorkerDied("OpenCode ACP exited before replying")
                if len(line) > MAX_FRAME:
                    raise ValueError("OpenCode ACP response exceeded the frame limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("OpenCode ACP emitted a non-object frame")
                method = message.get("method")
                if isinstance(method, str):
                    if "id" in message:
                        self._answer(method, message["id"])
                    elif method == "session/update":
                        self._route(message)
                    continue
                self._inbox.put(message)
        except BaseException as exc:
            self._dead.set()
            with self._state_lock:
                self._active = None
            self._inbox.put(exc)

    def _answer(self, method, request_id):
        response = {"jsonrpc": "2.0", "id": request_id}
        if method == "session/request_permission":
            response["result"] = {"outcome": {"outcome": "cancelled"}}
        else:
            response["error"] = {"code": -32601, "message": "Native file, terminal and other ACP client operations are unavailable"}
        self._send(response)

    def _route(self, message):
        params = message.get("params") or {}
        update = params.get("update") or {}
        session_id = params.get("sessionId")
        with self._state_lock:
            active = self._active
            if active is None or active.sinks is None:
                return
            if active.session_id is not None and session_id is not None and session_id != active.session_id:
                return
            if not active.saw_update and diagnostics_enabled():
                _log.info("ACP prompt %d first update %.3fs", active.rid, time.monotonic() - active.started)
            active.saw_update = True
            text_parts, reasoning_parts = active.sinks[:2]
            native = active.sinks[2] if len(active.sinks) > 2 else None
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            # Never executed here, but recorded so a native run cannot pass silently.
            if native is not None:
                call_id = str(update.get("toolCallId", len(native)))
                previous = native.get(call_id, ("pending", None, None))
                incoming = update.get("status")
                status = incoming if incoming in NATIVE_RAN or previous[0] not in NATIVE_RAN else previous[0]
                title = previous[2]
                if title is None and isinstance(update.get("title"), str):
                    # Titles can contain command lines and paths. Retain only known identifiers.
                    value = update["title"]
                    title = value if value in ("hermesbridge_request", "bash", "edit", "read", "write",
                                                "task", "glob", "grep", "webfetch") else "unrecognized"
                native[call_id] = (status or previous[0], update.get("kind") or previous[1], title)
                if len(active.sinks) > 3 and incoming == "completed" and isinstance(update.get("content"), list):
                    receipts = active.sinks[3]
                    for block in update["content"]:
                        content = block.get("content") if isinstance(block, dict) else None
                        if isinstance(content, dict) and content.get("type") == "text":
                            value = content.get("text")
                            if isinstance(value, str) and value.startswith(mcp_relay.PREFIX):
                                receipts.setdefault(call_id, set()).add(value)
            return
        sink = {"agent_message_chunk": text_parts, "agent_thought_chunk": reasoning_parts}.get(kind)
        content = update.get("content") or {}
        if sink is not None and isinstance(content, dict) and isinstance(content.get("text"), str):
            sink.append(content["text"])

    def _discard_stderr(self):
        try:
            while self.proc.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def new_session(self, timeout):
        servers = []
        if self.mcp_key:
            servers.append({"name": "hermesbridge", "command": sys.executable,
                            "args": ["-I", os.path.abspath(mcp_relay.__file__)],
                            "env": [{"name": "HERMES_MCP_RECEIPT_KEY", "value": self.mcp_key}]})
        result = self.request("session/new", {"cwd": self._cwd, "mcpServers": servers}, timeout=timeout)
        if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
            raise RuntimeError("OpenCode ACP did not return a session ID")
        return result

    def add_session(self, result, tools_signature):
        session = SessionState(result["sessionId"], result, tools_signature)
        self._sessions.append(session)
        while len(self._sessions) > 8:
            oldest = min(self._sessions, key=lambda item: item.last_used)
            self._sessions.remove(oldest)
        return session

    def match_session(self, signatures, tools_signature):
        best = None
        for session in self._sessions:
            if not session.represented or session.tools_signature != tools_signature:
                continue
            if len(session.represented) > len(signatures):
                continue
            if all(left == right for left, right in zip(session.represented, signatures)):
                if best is None or len(session.represented) > len(best.represented):
                    best = session
        if best is None or len(best.represented) >= len(signatures):
            return None, None
        return best, len(best.represented)

    def forget(self, session):
        try:
            self._sessions.remove(session)
        except ValueError:
            pass

    def remember(self, session, signatures, assistant):
        session.represented = list(signatures) + [message_signature(assistant)]
        session.last_used = time.monotonic()

    def cancel_prompt(self):
        with self._state_lock:
            active = self._active
            if active is None or active.sinks is None:
                return

        def notify():
            try:
                self._send({"jsonrpc": "2.0", "method": "session/cancel",
                            "params": {"sessionId": active.session_id}})
            except (WorkerDied, OSError, ValueError):
                pass

        # A stalled pipe must never delay process-tree termination.
        threading.Thread(target=notify, daemon=True).start()
        active.finished.wait(0.25)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._dead.set()
            try:
                self.owner.stop()
            except Exception:
                pass
            self.owner.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self.owner.stop(force=True)
                    self.proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for reader in self._readers:
                reader.join(timeout=1)
            with self._state_lock:
                self._active = None
            self._sessions.clear()


class CompletionAPI:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def _dispatch_completion(self, **kwargs):
        """Sync callers get the result; callers inside a running event loop get an awaitable.

        Hermes awaits clients that declare HERMES_SKIP_ASYNC_WRAP (auxiliary vision) and has no
        hook for a provider-specific async client. A synchronous call made on an event-loop thread
        would receive a coroutine; no such path was found in Hermes 0.23.2.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._create_chat_completion(**kwargs)
        return asyncio.to_thread(self._create_chat_completion, **kwargs)

    def _create_chat_completion(self, *, model=None, messages=None, tools=None, tool_choice=None,
                                timeout=None, stream=False, **kwargs):
        calls, content, reasoning, usage, model = self._run_prompt(messages, tools=tools,
                                                     tool_choice=tool_choice, timeout_seconds=timeout)
        message = Record(role="assistant", content=content, tool_calls=calls,
                         reasoning=reasoning or None, reasoning_content=reasoning or None, reasoning_details=None)
        result = Record(id="acp_" + uuid.uuid4().hex, object="chat.completion", created=int(time.time()), model=model,
                        choices=[Record(index=0, message=message, finish_reason="tool_calls" if calls else "stop")], usage=usage)
        if not stream:
            return result
        deltas = [Record(index=index, id=call.id, type="function", function=call.function) for index, call in enumerate(calls)]
        delta = Record(role="assistant", content=content or None, tool_calls=deltas or None,
                       reasoning_content=reasoning or None, reasoning=reasoning or None)
        return [Record(id=result.id, model=model, created=result.created, choices=[Record(index=0, delta=delta,
                       finish_reason=result.choices[0].finish_reason)], usage=usage)]


class OpenCodeACPClient(CompletionAPI):

    def __init__(self, *, command=None, args=None, acp_command=None, acp_args=None, acp_cwd=None,
                 agent=None, **kwargs):
        self.command = acp_command or command or opencode_command()
        self._auto_args = not (acp_args or args or os.getenv(ARGS_ENV, "").strip())
        self.args = list(acp_args or args or opencode_args())
        self.agent = agent or opencode_agent()
        self.workspace = os.path.abspath(acp_cwd or os.getcwd())
        self.base_url = "acp://hermes-opencode-acp"
        self.chat = Record(completions=Record(create=self._dispatch_completion))
        self.is_closed = False
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes = set()
        self._worker = None
        self._worker_lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            self.is_closed = True
            processes = tuple(self._processes)
            self._processes.clear()
            worker = self._worker
            self._worker = None
        if worker is not None:
            worker.cancel_prompt()
        for owner in processes:
            try:
                owner.stop()
            except Exception:
                pass
        if worker is not None:
            worker.close()

    def close(self):
        self.cancel()

    def _register_owner(self, owner):
        with self._lock:
            self._processes.add(owner)

    def _ensure_worker(self, timeout):
        deadline = time.monotonic() + effective_timeout(timeout)
        with acquire_until(self._worker_lock, deadline, "worker"):
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                worker = self._worker
            if worker is not None and not worker.dead:
                if diagnostics_enabled():
                    _log.info("ACP worker reused")
                return worker
            if worker is not None:
                worker.close()
                with self._lock:
                    self._processes.discard(worker.owner)
                    if self._worker is worker:
                        self._worker = None
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
            spawned = {}

            def register(owner):
                spawned["owner"] = owner
                self._register_owner(owner)

            def start(args):
                return BridgeWorker(self.command, args, cwd=self.workspace, env=os.environ.copy(),
                                    initialize_timeout=min(seconds_left(deadline), 30.0), on_spawn=register)

            try:
                candidates = [self.args]
                if self._auto_args:
                    key = os.path.normcase(os.path.realpath(shutil.which(self.command) or self.command))
                    try:
                        stat = os.stat(key)
                        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        identity = None
                    with _startup_lock:
                        saved_identity, saved_args = _startup_args.get(key, (None, DEFAULT_ARGS))
                    preferred = saved_args if saved_identity == identity else DEFAULT_ARGS
                    candidates = [list(a) for a in dict.fromkeys((preferred, DEFAULT_ARGS, *FALLBACK_ARGS))]
                for index, args in enumerate(candidates):
                    try:
                        started = time.monotonic()
                        worker = start(args)
                        if diagnostics_enabled():
                            _log.info("ACP worker started %.3fs", time.monotonic() - started)
                        if self._auto_args:
                            with _startup_lock:
                                _startup_args[key] = (identity, tuple(args))
                        break
                    except WorkerDied:
                        # Some CLIs reject the port or --pure flag. Only initialization
                        # is retried here; no session or prompt exists yet.
                        if index == len(candidates) - 1 or self._cancelled.is_set():
                            raise
                        with self._lock:
                            self._processes.discard(spawned.get("owner"))
            except BaseException:
                with self._lock:
                    self._processes.discard(spawned.get("owner"))
                raise
            with self._lock:
                cancelled = self._cancelled.is_set()
                if not cancelled:
                    self._worker = worker
                    self.is_closed = False
            if cancelled:
                worker.close()
                with self._lock:
                    self._processes.discard(worker.owner)
                raise RuntimeError("OpenCode ACP request was cancelled")
            return worker

    def _discard_worker(self, worker=None):
        with self._lock:
            if worker is None:
                worker = self._worker
            if self._worker is worker:
                self._worker = None
        if worker is None:
            return
        worker.close()
        with self._lock:
            self._processes.discard(worker.owner)

    @staticmethod
    def _send_prompt(worker, session, prompt, timeout, images=(), turn=None):
        text, reasoning, native = [], [], {}
        receipts = {}
        try:
            response = worker.request("session/prompt", {
                "sessionId": session.session_id, "prompt": [{"type": "text", "text": prompt}, *images]},
                sinks=(text, reasoning, native, receipts), timeout=timeout)
        except (RpcError, WorkerDied) as exc:
            if any(state[0] in NATIVE_RAN for state in native.values()):
                raise ToolProtocolError(f"[ACP-TOOL-INTERRUPTED {VERSION}] OpenCode reported tool activity "
                                        f"before {type(exc).__name__}. Check the action outcome before resending.") from exc
            raise
        if response.get("stopReason") == "cancelled":
            raise RuntimeError("OpenCode ACP request was cancelled")
        calls = []
        for call_id, (status, kind, title) in native.items():
            if turn and title == "hermesbridge_request" and status == "failed":
                raise ToolProtocolError(f"[ACP-MCP-CALL {VERSION}] hermesbridge_request failed inside OpenCode. "
                                        "Check MCP agent permissions and that its Python executable is accessible.")
            if status not in NATIVE_RAN:
                continue
            values = receipts.get(call_id, set())
            call = (mcp_relay.verify(worker.mcp_key, turn, next(iter(values)))
                    if turn and title == "hermesbridge_request" and status == "completed" and len(values) == 1 else None)
            if call and call["id"] not in {c["id"] for c in calls}:
                calls.append(call)
                continue
            mode = selected_value(session.config, "mode") or "unknown"
            mode = mode if isinstance(mode, str) and re.fullmatch(r"[\w.-]{1,64}", mode) else "unknown"
            kind = kind if kind in ("read", "edit", "delete", "move", "search", "execute",
                                    "think", "fetch", "switch_mode", "other") else "unknown"
            expected = "hermes-mcp" if worker.mcp_key else RELAY_AGENT
            raise NativeToolUsed(f"[ACP-TOOL-ORIGIN {VERSION}] agent={mode} tool={title or 'unknown'} kind={kind} status={status}. "
                                 f"OpenCode reported tool activity without a verified Hermes request. "
                                 f"Check agent {expected} and its tool permissions before retrying.")
        answer = "".join(text)
        if calls:
            if response.get("stopReason") != "end_turn":
                raise ToolProtocolError("[ACP-MCP-STOP] OpenCode did not finish the relay turn. No Hermes call dispatched.")
            answer = "".join(OPEN_TAG + json.dumps(call) + CLOSE_TAG for call in calls)
        elif turn and (mcp_relay.PREFIX in answer or _TOOL_TAG.search(answer) or _DSML_TOOL_MARKUP.search(answer)):
            raise ToolProtocolError("[ACP-MCP-MISSING] Model returned tool text without a verified MCP call. "
                                 "Check hermes-mcp permissions and that this OpenCode build loads session MCP servers.")
        if response.get("stopReason") in ("max_tokens", "max_turn_requests", "refusal") and (
                _TOOL_TAG.search(answer) or _DSML_TOOL_MARKUP.search(answer)):
            raise RuntimeError("OpenCode stopped before finishing the turn; no tool was dispatched")
        if not answer.strip():
            raise RuntimeError("OpenCode returned no assistant text; not treating this as success")
        return answer, "".join(reasoning), completion_usage(response.get("usage"))

    def _prompt_session(self, worker, session, messages, start, *, tools, tool_choice, deadline):
        supported = worker.image_input
        if not supported and _request_has_images(messages):
            # A vision request must not succeed on a text-only guess.
            raise RuntimeError("OpenCode agent did not advertise image input; the new image was not sent. "
                               "Point auxiliary vision at an image-capable backend. No prompt was sent.")
        turn = secrets.token_hex(12) if worker.mcp_key else None
        if start == 0:
            prompt = render_prompt(messages, tools, tool_choice, images=supported,
                                   contract=mcp_relay.CONTRACT if turn else CONTRACT)
        else:
            prompt = render_delta(messages, start, images=supported)
        prompt += image_recall_context(messages, tools, start)
        if turn:
            prompt += "\n" + mcp_relay.CONTRACT + "\nTurn token: " + turn
        images = []
        if supported:
            for message in messages[start:]:
                content = _plain(message).get("content")
                if isinstance(content, list):
                    images.extend(block for item in content if (block := image_block(item)))
        answer, reasoning, usage = self._send_prompt(worker, session, prompt, seconds_left(deadline), images, turn)
        try:
            calls, content = parse_tool_calls(answer, tools, tool_choice)
        except ValueError as exc:
            if turn:
                raise ToolProtocolError(f"[ACP-MCP-VALIDATION] {exc}. Check the requested tool schema and choice.") from exc
            # One in-session correction; the rejected reply never reaches Hermes.
            initial = usage
            try:
                answer, reasoning, usage = self._send_prompt(worker, session, REPAIR.format(reason=exc), seconds_left(deadline))
            except RpcError as repair_error:
                repair_error.missing_session = False
                raise
            except WorkerDied as repair_error:
                repair_error.retryable = False
                raise
            if diagnostics_enabled() and initial and usage:
                _log.info("OpenCode ACP corrected turn total tokens: %d", initial.total_tokens + usage.total_tokens)
            try:
                calls, content = parse_tool_calls(answer, tools, tool_choice)
            except ValueError as final_error:
                raise ToolProtocolError(f"[ACP-TOOL-FORMAT {VERSION}] Repair 1/1 failed: {final_error}. "
                                        "No Hermes tool dispatched. Enable the MCP relay for structured calls.") from final_error
        return calls, content, reasoning, usage

    def _open_session(self, worker, tools_signature, deadline):
        result = worker.new_session(seconds_left(deadline))
        agent = "hermes-mcp" if worker.mcp_key else self.agent
        if worker.mcp_key and not any(agent in options(o) for o in _config_candidates(result, "mode")):
            raise ToolProtocolError(f"[ACP-MCP-AGENT {VERSION}] OpenCode did not offer hermes-mcp. "
                                    "Merge opencode/hermes-mcp.jsonc into the active OpenCode config and restart the gateway.")
        if agent is None:
            # Without an explicit choice, prefer the tool-less relay agent whenever it is offered.
            offered = [value for item in _config_candidates(result, "mode") for value in options(item)]
            agent = RELAY_AGENT if RELAY_AGENT in offered else None
        if agent:
            option = config_option(result, "mode", agent)
            if option.get("currentValue") != agent:
                selected = worker.request("session/set_config_option", {
                    "sessionId": result["sessionId"], "configId": option["id"], "value": agent,
                }, timeout=seconds_left(deadline))
                if selected_value(selected, option["id"]) != agent:
                    raise RuntimeError(f"OpenCode agent {agent!r} was not confirmed. No prompt was sent.")
                result = {**result, "configOptions": selected.get("configOptions", result.get("configOptions"))}
        return worker.add_session(result, tools_signature)

    def _prompt_on_worker(self, worker, messages, signatures, tools_signature, *, tools, tool_choice, deadline):
        session, start = worker.match_session(signatures, tools_signature)
        if session is None and worker.prepared_session is not None:
            session = worker.prepared_session
            worker.prepared_session = None
            session.tools_signature = tools_signature
            start = 0
        if session is not None:
            try:
                calls, content, reasoning, usage = self._prompt_session(
                    worker, session, messages, start, tools=tools, tool_choice=tool_choice, deadline=deadline)
                return calls, content, reasoning, usage, session
            except RpcError as exc:
                worker.forget(session)
                if not exc.missing_session:
                    raise
            except BaseException:
                worker.forget(session)
                raise
        session = self._open_session(worker, tools_signature, deadline)
        try:
            calls, content, reasoning, usage = self._prompt_session(
                worker, session, messages, 0, tools=tools, tool_choice=tool_choice, deadline=deadline)
            return calls, content, reasoning, usage, session
        except BaseException:
            worker.forget(session)
            raise

    def _check_relay(self, worker, deadline):
        session = self._open_session(worker, None, deadline)
        try:
            agent = selected_value(session.config, "mode") or "unknown"
            model = session_model(session.config)
            label = f"ACP-CHECK {VERSION} | agent={agent}"
            if not worker.mcp_key:
                return [], label + " | MCP OFF. Set HERMES_OPENCODE_ACP_MCP=1 on the serving gateway and restart it.", "", None, model
            token = secrets.token_hex(6)
            probe = [{"role": "user", "content": f"Request relay_probe with token {token} exactly once."}]
            tools = [{"type": "function", "function": {"name": "relay_probe", "parameters": {
                "type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}}}]
            calls, _, _, usage = self._prompt_session(worker, session, probe, 0, tools=tools,
                                                       tool_choice="required", deadline=deadline)
            ok = len(calls) == 1 and json.loads(calls[0].function.arguments) == {"token": token}
            result = "MCP PASS" if ok else "MCP FAIL: probe arguments or call count did not match"
            return [], f"{label} | {result}", "", usage, model
        finally:
            worker.forget(session)

    def _run_prompt(self, messages, *, tools, tool_choice, timeout_seconds, namespace=None):
        deadline = time.monotonic() + effective_timeout(timeout_seconds)
        messages = messages or []
        signatures = [message_signature(message) for message in messages]
        tools_signature = (namespace, prompt_signature(tools, tool_choice))
        for attempt in (0, 1):
            worker = None
            try:
                worker = self._ensure_worker(seconds_left(deadline))
                with acquire_until(worker.rpc_lock, deadline, "prompt"):
                    try:
                        last = _plain(messages[-1]) if messages else {}
                        if isinstance(last, dict) and last.get("role") == "user":
                            body = render_content(last.get("content")).strip()
                            if body == "!acp-check" or body.endswith("\n[New message]\n!acp-check"):
                                return self._check_relay(worker, deadline)
                        calls, content, reasoning, usage, session = self._prompt_on_worker(
                            worker, messages, signatures, tools_signature,
                            tools=tools, tool_choice=tool_choice, deadline=deadline)
                        if self._cancelled.is_set():
                            raise RuntimeError("OpenCode ACP request was cancelled")
                        worker.remember(session, signatures, Record(role="assistant", content=content, tool_calls=calls))
                        return calls, content, reasoning, usage, session_model(session.config)
                    except TimeoutError:
                        # Only the owner of the turn may retire a timed-out worker.
                        self._discard_worker(worker)
                        raise
            except WorkerDied as exc:
                if worker is not None:
                    self._discard_worker(worker)
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                # Respawn only when the lost turn produced nothing, as observed by the bridge.
                if attempt == 1 or not exc.retryable:
                    raise

# A Hermes process can construct separate chat and auxiliary clients in one
# profile. Share the CLI transport, while keeping each client's history private.
_shared_workers = {}
_shared_lock = threading.Lock()


class SharedWorker:
    def __init__(self, transport):
        self.transport = transport
        self.turn = threading.Lock()
        self.active = None
        self.references = 0


def release_client(key, slot, token):
    with _shared_lock:
        slot.references -= 1
        last = slot.references == 0
        if last:
            del _shared_workers[key]
        transport = slot.transport if last or slot.active is token else None
        if transport is not None:
            slot.transport = None
    if transport is not None:
        transport.close()


class SharedACPClient(CompletionAPI):
    def __init__(self, profile_home, **kwargs):
        transport = OpenCodeACPClient(**kwargs)
        self._kwargs = kwargs
        self._token = object()
        key = (os.path.normcase(os.path.realpath(profile_home)), transport.workspace,
               transport.command, tuple(transport.args), transport._auto_args, transport.agent,
               tuple(os.getenv(k) for k in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "XDG_CONFIG_HOME", "HERMES_OPENCODE_ACP_MCP")))
        with _shared_lock:
            if key not in _shared_workers:
                _shared_workers[key] = SharedWorker(transport)
            self._slot = _shared_workers[key]
            self._slot.references += 1
        self._release = weakref.finalize(self, release_client, key, self._slot, self._token)
        self.chat = Record(completions=Record(create=self._dispatch_completion))
        self.base_url = transport.base_url

    def prewarm(self):
        """Prepare one unused session on this process's transport, without inference."""
        slot = self._slot
        if not slot.turn.acquire(blocking=False):
            return
        try:
            with _shared_lock:
                transport = slot.transport
                if self.is_closed or transport is None:
                    return
            deadline = time.monotonic() + 30
            worker = transport._ensure_worker(seconds_left(deadline))
            with acquire_until(worker.rpc_lock, deadline, "prewarm"):
                if worker._sessions:
                    return
                try:
                    worker.prepared_session = transport._open_session(worker, None, deadline)
                except Exception:
                    transport._discard_worker(worker)
                    raise
            if diagnostics_enabled():
                _log.info("ACP prewarm ready")
        finally:
            slot.turn.release()

    @property
    def is_closed(self):
        return not self._release.alive

    def close(self):
        self._release()

    cancel = close

    def _run_prompt(self, messages, *, tools, tool_choice, timeout_seconds):
        deadline = time.monotonic() + effective_timeout(timeout_seconds)
        slot = self._slot
        with acquire_until(slot.turn, deadline, "shared transport"):
            with _shared_lock:
                if self.is_closed:
                    raise RuntimeError("OpenCode ACP request was cancelled")
                if slot.transport is None:
                    slot.transport = OpenCodeACPClient(**self._kwargs)
                transport = slot.transport
                slot.active = self._token
            try:
                return transport._run_prompt(messages, tools=tools, tool_choice=tool_choice,
                                             timeout_seconds=seconds_left(deadline), namespace=self._token)
            finally:
                with _shared_lock:
                    slot.active = None
