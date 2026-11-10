"""Experimental single-turn Gemini processSession WebChannel transport.

Protocol adapted from gemini-web2api-go (MIT), copyright 2026
 gemini-web2api-go contributors; see THIRD_PARTY_NOTICES.md.
"""
import base64
import io
import json
import queue
import random
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

from .config import CONFIG
from .gemini import _build_payload, _build_headers, load_cookie, make_sapisidhash, clean_text

ENDPOINT = "https://geminiweb-pa.clients6.google.com/v1/processSession"
ARGON_ID = "95a4c0abcba24364"


def _model_header(level):
    return base64.b64encode(b"\x08\x01\x2a\x10" + ARGON_ID.encode() + b"\x78\x08\x80\x01" + bytes([level])).decode()


def _frames(stream):
    """Decode WebChannel length-prefixed JSON frames (length is UTF-16 units)."""
    reader = io.TextIOWrapper(stream, encoding="utf-8")
    while True:
        length = reader.readline()
        if not length:
            return
        try:
            remaining = int(length.strip())
        except ValueError as exc:
            raise RuntimeError("Invalid WebChannel frame length") from exc
        if remaining < 0 or remaining > 16 * 1024 * 1024:
            raise RuntimeError("Invalid WebChannel frame size")
        chars = []
        while remaining:
            c = reader.read(1)
            if not c:
                raise RuntimeError("Truncated WebChannel frame")
            remaining -= 2 if ord(c) > 0xffff else 1
            if remaining < 0:
                raise RuntimeError("Invalid WebChannel UTF-16 length")
            chars.append(c)
        for item in json.loads("".join(chars)):
            if isinstance(item, list) and len(item) >= 2:
                yield item[0], item[1]


def _event(payload):
    if payload == ["noop"]:
        return None, False, False, False
    if payload == ["close"] or payload == [[None, None, None, []]]:
        return None, False, False, True
    if payload == [[[]]]:
        return None, True, False, False
    if isinstance(payload, dict) and "__sm__" in payload:
        raise RuntimeError("Gemini processSession rejected the request")
    try:
        candidate = payload[0][1][6][0]
        inner = candidate[3][3]
        return inner, False, candidate[0] == 1, False
    except (IndexError, KeyError, TypeError):
        return None, False, False, False


def _text(inner):
    try:
        return max((s for part in inner[4] for s in part[1] if isinstance(s, str)), key=len, default="")
    except (IndexError, TypeError):
        return ""


def _reported_model(inner):
    """Return the server-reported model label, if this frame has one."""
    if not isinstance(inner, list):
        return None
    for i in range(len(inner) - 3):
        if (isinstance(inner[i], str) and re.fullmatch(r"[0-9a-f]{16}", inner[i])
                and inner[i + 1:i + 3] == [None, None]
                and isinstance(inner[i + 3], str)):
            return inner[i + 3]
    return None


def generate_argon(prompt, level=1, file_refs=None):
    """Generate one Argon response; yields incremental text, fails on empty output."""
    cookie, sapisid = load_cookie()
    if not cookie or not sapisid:
        raise RuntimeError("Gemini 4 Argon requires a configured account cookie with SAPISID")
    if file_refs:
        raise RuntimeError("Gemini 4 Argon image input is not yet supported")
    timeout = CONFIG.get("request_timeout_sec", 180)
    deadline = time.monotonic() + timeout
    proxy = CONFIG.get("proxy")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}) if proxy else urllib.request.ProxyHandler())
    common = {"Origin": "https://gemini.google.com", "Referer": "https://gemini.google.com/", "User-Agent": _build_headers()["User-Agent"], "Cookie": cookie}

    def send(method, url, body=None):
        headers = dict(common)
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Gemini processSession timed out")
        return opener.open(urllib.request.Request(url, data=body.encode() if body is not None else None, headers=headers, method=method), timeout=remaining)

    with send("GET", "https://gemini.google.com/app") as resp:
        page = resp.read().decode("utf-8", "replace")
    match = re.search(r'"d2zJAe":"(AIza[0-9A-Za-z_-]{35})"', page)
    if not match:
        raise RuntimeError("Could not find the Gemini processSession API key in the app page")
    ext_headers = ("X-Goog-Api-Key:" + match.group(1) + "\r\n" +
                   "x-goog-ext-525001261-bin:" + _model_header(level) + "\r\n" +
                   "X-WebChannel-Content-Type:application/json+protobuf\r\n" +
                   "Authorization:" + make_sapisidhash(sapisid) + "\r\n" +
                   "X-Goog-AuthUser:" + str(CONFIG.get("auth_user") or 0) + "\r\n")
    rid = random.randint(10000, 89999)
    def url(params):
        return ENDPOINT + "?" + urllib.parse.urlencode(params)
    with send("POST", url({"VER": 8, "RID": rid, "CVER": 22, "X-HTTP-Session-Id": "gsessionid", "$httpHeaders": ext_headers, "t": 1}), "count=0") as response:
        gsid = response.headers.get("X-HTTP-Session-Id")
        sid = next((p[1] for _, p in _frames(response) if isinstance(p, list) and len(p) > 1 and p[0] == "c"), None)
    if not sid or not gsid:
        raise RuntimeError("Gemini processSession handshake did not return a session")
    session = {"gsessionid": gsid, "SID": sid}
    events = queue.Queue()
    ready = threading.Event()
    stop = threading.Event()
    state = {"aid": 0}

    def read_downstream():
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                params = dict(session, VER=8, RID="rpc", AID=state["aid"], CI=0, TYPE="xmlhttp", t=1)
                with send("GET", url(params)) as response:
                    ready.set()
                    for aid, payload in _frames(response):
                        if isinstance(aid, int):
                            state["aid"] = max(aid, state["aid"])
                        ev = _event(payload)
                        events.put(ev)
                        if ev[3] or stop.is_set():
                            return
        except Exception as exc:
            events.put(exc)
            ready.set()

    threading.Thread(target=read_downstream, daemon=True).start()
    if not ready.wait(max(0, deadline - time.monotonic())):
        raise TimeoutError("Gemini processSession downlink timed out")
    def post(ofs, data):
        nonlocal rid
        rid += 1
        params = dict(session, VER=8, RID=rid, AID=state["aid"], t=1)
        body = urllib.parse.urlencode({"count": 1, "ofs": ofs, "req0___data__": json.dumps(data, ensure_ascii=False, separators=(",", ":"))})
        with send("POST", url(params), body) as response:
            response.read()
    try:
        outer = json.loads(urllib.parse.parse_qs(_build_payload("", 8, 4))["f.req"][0])
        inner = json.loads(outer[1])
        inner[80] = level
        meta = [1, ARGON_ID, "en", str(uuid.uuid4())]
        post(0, [[meta, 0, None, None, None, None, None, [None, None, None, None, [inner, None, None, 1]]]])
        sent = False
        ack_deadline = time.monotonic() + 3
        emitted = ""
        pending = ""
        model_verified = False
        closed = False
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=max(0.01, min(deadline - time.monotonic(), ack_deadline - time.monotonic() if not sent else deadline)))
            except queue.Empty:
                event = (None, True, False, False)
            if isinstance(event, Exception):
                raise event
            data, ack, done, end = event
            if ack and not sent:
                time.sleep(0.5)
                post(1, [None, [1, None, None, None, None, None, None, None, [prompt]]])
                sent = True
            if data is not None:
                label = _reported_model(data)
                if label:
                    if not re.search(r"\b4\s+argon\b", label, re.IGNORECASE):
                        raise RuntimeError("Gemini routed the request to a different model: " + label)
                    model_verified = True
                current = _text(data)
                if current and current != pending:
                    if not current.startswith(pending):
                        raise RuntimeError("Gemini processSession response changed unexpectedly")
                    pending = current
                if model_verified and pending != emitted:
                    delta = clean_text(pending[len(emitted):], strip=False)
                    emitted = pending
                    if delta:
                        yield delta
            if done and not closed:
                closed = True
                post(2, [None, None, None, [1]])
            if end:
                if not model_verified:
                    raise RuntimeError("Gemini did not report the actual model; refusing an unverified Argon response")
                if not emitted:
                    raise RuntimeError("Gemini processSession returned no response")
                return
        raise TimeoutError("Gemini processSession timed out")
    finally:
        stop.set()
