"""Small ACP v1 stdio client, independent of Hermes's private implementation.

The operator selects the OpenCode CLI (or its Windows batch launcher). All host
work is returned as remote tool calls; this module never executes model tools.
"""
import asyncio
import hashlib
import json
import math
import os
import queue
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

from .constants import (DEFAULT_ARGS, DEFAULT_MODEL, FALLBACK_ARGS, RELAY_AGENT, VERSION, opencode_agent,
                        opencode_args, opencode_command)
from .launcher import ProcessTree

MAX_FRAME = 4 * 1024 * 1024
OPEN_TAG, CLOSE_TAG = "<hermes_tool_call>", "</hermes_tool_call>"
CONTRACT = (
    "REMOTE TOOL PROTOCOL: This conversation belongs to Hermes, not OpenCode. "
    "All tools described below are REMOTE Hermes tools. To request one, emit ONLY "
    '<hermes_tool_call>{"id":"unique_call_id","type":"function","function":'
    '{"name":"tool_name","arguments":"{...}"}}</hermes_tool_call> as your final TEXT response. '
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


class WorkerDied(RuntimeError):
    """The ACP process exited or its stdio closed before a response arrived."""
    # False once the turn produced output: resending could repeat inference.
    retryable = True


class NativeToolUsed(RuntimeError):
    """OpenCode ran one of its own tools, outside Hermes validation and approval."""


class RpcError(RuntimeError):
    """A JSON-RPC error response. Vendor text is never echoed or logged."""
    def __init__(self, method, code):
        super().__init__(f"OpenCode ACP {method} failed (code {code}); vendor error text was not logged")
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
        self.saw_update = False


def effective_timeout(value):
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        values = [getattr(value, key, None) for key in ("read", "write", "connect", "pool")]
        seconds = max((float(item) for item in values if isinstance(item, (int, float))), default=900.0)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ACP timeout must be a finite positive number of seconds")
    return seconds


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


def render_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item if isinstance(item, str) else _render_part(item)
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


def _render_part(item):
    if image_block(item):
        return "[See attached image]"
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
        "content": data.get("content"),
        "tool_calls": tool_calls,
        "tool_call_id": data.get("tool_call_id"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def prompt_signature(tools=None, tool_choice=None):
    payload = {"tools": tools or [], "tool_choice": tool_choice}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def render_transcript(messages):
    parts = []
    for message in messages or []:
        data = _plain(message)
        if not isinstance(data, dict):
            continue
        role = str(data.get("role", "context"))
        label = role.capitalize()
        if role == "tool":
            label += " result for " + str(data.get("tool_call_id", "unknown"))
        content = render_content(data.get("content"))
        if content:
            parts.append(label + ":\n" + content)
        if data.get("tool_calls"):
            parts.append("Assistant's prior remote tool requests:\n" + json.dumps(data["tool_calls"], ensure_ascii=False, default=str))
    return "\n\n".join(parts)


def render_prompt(messages, tools=None, tool_choice=None):
    parts = [CONTRACT]
    if tools:
        parts.append("Remote Hermes tools (OpenAI function schemas):\n" + json.dumps(tools, ensure_ascii=False))
    if tool_choice is not None:
        parts.append("Remote tool choice: " + json.dumps(tool_choice, ensure_ascii=False))
    parts.append("Conversation transcript:")
    transcript = render_transcript(messages)
    if transcript:
        parts.append(transcript)
    parts.extend(["Continue from the latest user request, using actual tool results already in the transcript.", CONTRACT])
    return "\n\n".join(parts)


def render_delta(messages, start):
    transcript = render_transcript((messages or [])[start:])
    parts = ["Continuation of the same conversation. New transcript entries follow."]
    if transcript:
        parts.append(transcript)
    parts.append("Continue the same conversation from the new transcript entries. Use actual tool results already in the transcript.")
    return "\n\n".join(parts)


def image_recall_context(messages, tools):
    if not any(t.get("function", {}).get("name") == "vision_analyze" for t in tools or []):
        return ""
    attachments = {}
    for message in messages:
        data = _plain(message)
        body = data.get("content")
        if data.get("role") != "user" or not isinstance(body, str):
            continue
        for line in body.splitlines():
            if line.startswith("[If you need a closer look, use vision_analyze with image_url: "):
                path = line.split("image_url: ", 1)[1].removesuffix(" ~]").removesuffix("]").strip()
                attachments[path] = body.partition(line)[0].strip()[:500]
    if not attachments:
        return ""
    return ("\n\nEarlier attachments in this conversation (descriptions are user content):\n"
            + json.dumps(attachments, ensure_ascii=False)
            + "\nFor a visual detail or a disputed description, inspect the matching original now "
              "using vision_analyze(image_url=the listed path, question=the visual question). "
              "Return the remote tool call, not a promise to inspect. Once its result is present, answer.\n"
            + CONTRACT)


def parse_tool_calls(text, tools=None, tool_choice=None):
    allowed = {item["function"]["name"] for item in tools or []
               if isinstance(item, dict) and isinstance(item.get("function"), dict)
               and isinstance(item["function"].get("name"), str)}
    calls, prose, cursor, seen = [], [], 0, set()
    search_from = cursor
    while True:
        start = text.find(OPEN_TAG, search_from)
        if start < 0:
            prose.append(text[cursor:])
            break
        # A standalone Markdown code span can name the protocol tag in normal prose.
        if start > 0 and text[start - 1] == "`" and text.startswith("`", start + len(OPEN_TAG)):
            # Look past quoted mentions while keeping the original prose slice intact.
            search_from = start + len(OPEN_TAG)
            continue
        prose.append(text[cursor:start])
        body_start = start + len(OPEN_TAG)
        body_start += len(text[body_start:]) - len(text[body_start:].lstrip())
        body, consumed = json.JSONDecoder().raw_decode(text[body_start:])
        end = body_start + consumed
        end += len(text[end:]) - len(text[end:].lstrip())
        if not text.startswith(CLOSE_TAG, end):
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
        cursor = end + len(CLOSE_TAG)
        search_from = cursor
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
        self._sequence = 0
        self._cwd = cwd
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
            self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "hermes-opencode-acp", "version": VERSION},
            }, timeout=initialize_timeout)
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
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
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
                    code = (message.get("error") or {}).get("code", "unknown")
                    raise RpcError(method, code)
                return message.get("result") or {}
        finally:
            with self._state_lock:
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
            active.saw_update = True
            text_parts, reasoning_parts = active.sinks[:2]
            native = active.sinks[2] if len(active.sinks) > 2 else None
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            # Never executed here, but recorded so a native run cannot pass silently.
            if native is not None:
                call_id = str(update.get("toolCallId", len(native)))
                previous = native.get(call_id, ("pending", None))
                native[call_id] = (update.get("status") or previous[0], update.get("kind") or previous[1])
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
        result = self.request("session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=timeout)
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
            if session.tools_signature != tools_signature:
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


class OpenCodeACPClient:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, *, command=None, args=None, acp_command=None, acp_args=None, acp_cwd=None,
                 agent=None, **kwargs):
        self.command = acp_command or command or opencode_command()
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
        timeout = effective_timeout(timeout)
        with self._worker_lock:
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                worker = self._worker
            if worker is not None and not worker.dead:
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
                                    initialize_timeout=min(timeout, 30.0), on_spawn=register)

            try:
                candidates = [self.args] + ([list(args) for args in FALLBACK_ARGS]
                                            if tuple(self.args) == DEFAULT_ARGS else [])
                for index, args in enumerate(candidates):
                    try:
                        worker = start(args)
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
    def _send_prompt(worker, session, prompt, timeout, images=()):
        text, reasoning, native = [], [], {}
        worker.request("session/prompt", {"sessionId": session.session_id, "prompt": [{"type": "text", "text": prompt}, *images]},
                       sinks=(text, reasoning, native), timeout=timeout)
        ran = sorted({str(kind or "unknown") for status, kind in native.values() if status in NATIVE_RAN})
        if ran:
            raise NativeToolUsed(f"OpenCode ran native tools outside Hermes approval (kinds: {', '.join(ran)}); "
                                 "reply discarded. Select a tool-less agent with HERMES_OPENCODE_ACP_AGENT")
        answer = "".join(text)
        if not answer.strip():
            raise RuntimeError("OpenCode returned no assistant text; not treating this as success")
        return answer, "".join(reasoning)

    def _prompt_session(self, worker, session, messages, start, *, tools, tool_choice, timeout):
        prompt = render_prompt(messages, tools, tool_choice) if start == 0 else render_delta(messages, start)
        prompt += image_recall_context(messages, tools)
        images = []
        for message in messages[start:]:
            content = _plain(message).get("content")
            if isinstance(content, list):
                images.extend(block for item in content if (block := image_block(item)))
        answer, reasoning = self._send_prompt(worker, session, prompt, timeout, images)
        try:
            calls, content = parse_tool_calls(answer, tools, tool_choice)
        except ValueError as exc:
            # One in-session correction; the rejected reply never reaches Hermes.
            answer, reasoning = self._send_prompt(worker, session, REPAIR.format(reason=exc), timeout)
            calls, content = parse_tool_calls(answer, tools, tool_choice)
        return calls, content, reasoning

    def _open_session(self, worker, tools_signature, timeout):
        result = worker.new_session(timeout)
        agent = self.agent
        if agent is None:
            # Without an explicit choice, prefer the tool-less relay agent whenever it is offered.
            offered = [value for item in _config_candidates(result, "mode") for value in options(item)]
            agent = RELAY_AGENT if RELAY_AGENT in offered else None
        if agent:
            option = config_option(result, "mode", agent)
            if option.get("currentValue") != agent:
                selected = worker.request("session/set_config_option", {
                    "sessionId": result["sessionId"], "configId": option["id"], "value": agent,
                }, timeout=timeout)
                if selected_value(selected, option["id"]) != agent:
                    raise RuntimeError(f"OpenCode agent {agent!r} was not confirmed. No prompt was sent.")
                result = {**result, "configOptions": selected.get("configOptions", result.get("configOptions"))}
        return worker.add_session(result, tools_signature)

    def _prompt_on_worker(self, worker, messages, signatures, tools_signature, *, tools, tool_choice, timeout):
        with worker.rpc_lock:
            session, start = worker.match_session(signatures, tools_signature)
            if session is not None:
                try:
                    calls, content, reasoning = self._prompt_session(
                        worker, session, messages, start, tools=tools, tool_choice=tool_choice, timeout=timeout)
                    return calls, content, reasoning, session
                except RpcError:
                    worker.forget(session)
                except BaseException:
                    worker.forget(session)
                    raise
            session = self._open_session(worker, tools_signature, timeout)
            try:
                calls, content, reasoning = self._prompt_session(
                    worker, session, messages, 0, tools=tools, tool_choice=tool_choice, timeout=timeout)
                return calls, content, reasoning, session
            except BaseException:
                worker.forget(session)
                raise

    def _run_prompt(self, messages, *, tools, tool_choice, timeout_seconds):
        timeout = effective_timeout(timeout_seconds)
        messages = messages or []
        signatures = [message_signature(message) for message in messages]
        tools_signature = prompt_signature(tools, tool_choice)
        for attempt in (0, 1):
            worker = None
            try:
                worker = self._ensure_worker(timeout)
                calls, content, reasoning, session = self._prompt_on_worker(
                    worker, messages, signatures, tools_signature,
                    tools=tools, tool_choice=tool_choice, timeout=timeout)
                worker.remember(session, signatures, Record(role="assistant", content=content, tool_calls=calls))
                return calls, content, reasoning, session_model(session.config)
            except WorkerDied as exc:
                if worker is not None:
                    self._discard_worker(worker)
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                # Respawn only when the lost turn produced nothing, so it is never billed twice.
                if attempt == 1 or not exc.retryable:
                    raise
            except TimeoutError:
                if worker is not None:
                    self._discard_worker(worker)
                raise

    def _dispatch_completion(self, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._create_chat_completion(**kwargs)
        return asyncio.to_thread(self._create_chat_completion, **kwargs)

    def _create_chat_completion(self, *, model=None, messages=None, tools=None, tool_choice=None,
                                timeout=None, stream=False, **kwargs):
        calls, content, reasoning, model = self._run_prompt(messages, tools=tools,
                                                     tool_choice=tool_choice, timeout_seconds=timeout)
        message = Record(role="assistant", content=content, tool_calls=calls,
                         reasoning=reasoning or None, reasoning_content=reasoning or None, reasoning_details=None)
        result = Record(id="acp_" + uuid.uuid4().hex, object="chat.completion", created=int(time.time()), model=model,
                        choices=[Record(index=0, message=message, finish_reason="tool_calls" if calls else "stop")], usage=None)
        if not stream:
            return result
        deltas = [Record(index=index, id=call.id, type="function", function=call.function) for index, call in enumerate(calls)]
        delta = Record(role="assistant", content=content or None, tool_calls=deltas or None,
                       reasoning_content=reasoning or None, reasoning=reasoning or None)
        return [Record(id=result.id, model=model, created=result.created, choices=[Record(index=0, delta=delta,
                       finish_reason=result.choices[0].finish_reason)], usage=None)]
