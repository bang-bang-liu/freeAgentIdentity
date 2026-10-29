#!/usr/bin/env python3
"""toSub2 v1.7.1 challenge solver with Windows pipes and bounded cancellation."""

from __future__ import annotations

import base64
import json
import re
import os
import queue
import subprocess
import sys
import tempfile
import time
import threading
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
PARENT_RUNTIME = BASE_DIR / "cf_runtime.cjs"
CHILD_RUNTIME = BASE_DIR / "turnstile_runtime.cjs"


class InlineScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.has_src = False
        self.parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        self.inside = True
        self.has_src = bool(dict(attrs).get("src"))
        self.parts = []

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self.inside:
            return
        source = "".join(self.parts)
        if not self.has_src and source.strip():
            self.scripts.append(source)
        self.inside = False


def _log(message: str) -> None:
    print(f"[cloudflare] {message}", file=sys.stderr, flush=True)


def _browser_major(user_agent: str) -> str:
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent or "")
    return match.group(1) if match else "146"


def navigation_headers(user_agent: str, browser_identity: dict[str, Any] | None = None) -> dict[str, str]:
    identity = browser_identity or {}
    major = _browser_major(user_agent)
    accept_language = str(identity.get("acceptLanguage") or "zh-CN,zh;q=0.9,en;q=0.8")
    sec_ch_ua = str(identity.get("secChUa") or (
        f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'
    ))
    sec_ch_ua_mobile = str(identity.get("secChUaMobile") or "?0")
    sec_ch_ua_platform = str(identity.get("secChUaPlatform") or '"macOS"')
    return {
        "user-agent": user_agent,
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "accept-language": accept_language,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": sec_ch_ua_mobile,
        "sec-ch-ua-platform": sec_ch_ua_platform,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }


def performance_entry(url: str, entry_type: str, size: int, duration: float) -> dict[str, Any]:
    return {
        "name": url,
        "entryType": entry_type,
        "startTime": 0,
        "duration": duration,
        "requestStart": duration * 0.08,
        "responseStart": duration * 0.75,
        "responseEnd": duration,
        "transferSize": size,
        "encodedBodySize": size,
        "decodedBodySize": size,
    }


def start_runtime(
    script: Path,
    runtime_input: dict[str, Any],
    node_command: str,
) -> tuple[subprocess.Popen[str], Path]:
    # Windows 子进程不能重新打开仍被 NamedTemporaryFile 独占的文件。
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as input_file:
        json.dump(runtime_input, input_file)
        input_path = Path(input_file.name)
    try:
        process = subprocess.Popen(
            [node_command, str(script), str(input_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        input_path.unlink(missing_ok=True)
        raise
    return process, input_path


def request_headers(
    page_url: str,
    generated: dict[str, Any],
    user_agent: str,
    browser_identity: dict[str, Any] | None = None,
) -> dict[str, str]:
    identity = browser_identity or {}
    target = generated["url"]
    page = urlparse(page_url)
    destination = urlparse(target)
    page_origin = f"{page.scheme}://{page.netloc}"
    destination_origin = f"{destination.scheme}://{destination.netloc}"
    supplied = generated.get("headers") or {}
    headers = (
        {str(key): str(value) for key, value in supplied.items()}
        if hasattr(supplied, "items") else {}
    )
    headers.setdefault("user-agent", user_agent)
    headers.setdefault("accept-language", str(identity.get("acceptLanguage") or "zh-CN,zh;q=0.9,en;q=0.8"))
    if identity.get("secChUa"):
        headers.setdefault("sec-ch-ua", str(identity["secChUa"]))
    if identity.get("secChUaMobile"):
        headers.setdefault("sec-ch-ua-mobile", str(identity["secChUaMobile"]))
    if identity.get("secChUaPlatform"):
        headers.setdefault("sec-ch-ua-platform", str(identity["secChUaPlatform"]))
    headers.setdefault("referer", page_url)
    if generated.get("kind") == "script":
        headers.setdefault("accept", "*/*")
        headers.setdefault("sec-fetch-dest", "script")
        headers.setdefault("sec-fetch-mode", "no-cors")
    elif generated["method"].upper() == "GET":
        headers.setdefault("accept", "*/*")
        headers.setdefault("sec-fetch-dest", "empty")
        headers.setdefault("sec-fetch-mode", "cors")
    else:
        headers.setdefault("accept", "*/*")
        headers.setdefault("origin", page_origin)
        headers.setdefault("content-type", "text/plain;charset=UTF-8")
        headers.setdefault("sec-fetch-dest", "empty")
        headers.setdefault("sec-fetch-mode", "cors")
    headers.setdefault("sec-fetch-site", "same-origin" if page_origin == destination_origin else "cross-site")
    return headers


def submit_generated(
    session,
    page_url: str,
    generated: dict[str, Any],
    user_agent: str,
    browser_identity: dict[str, Any] | None = None,
    timeout: float = 45,
):
    method = generated["method"].upper()
    body = generated.get("body", "")
    response = session.request(
        method,
        generated["url"],
        headers=request_headers(page_url, generated, user_agent, browser_identity),
        data=body if method != "GET" else None,
        timeout=timeout,
        allow_redirects=False,
    )
    _log(
        f"{generated['kind']} {method} HTTP {response.status_code} "
        f"request={len(body)} response={len(response.content)}"
    )
    return response


def acknowledge(generated: dict[str, Any], response) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": generated["id"],
        "status": int(response.status_code),
        "url": str(response.url),
        "headers": dict(response.headers),
    }
    content_type = response.headers.get("content-type", "").lower()
    if content_type.startswith("image/") or "application/octet-stream" in content_type:
        result["bodyBase64"] = base64.b64encode(response.content).decode("ascii")
    else:
        result["body"] = response.text
    return result


def write_message(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("Cloudflare runtime stdin is unavailable")
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def clearance_values(session, target_url: str) -> set[str]:
    jar = getattr(session.cookies, "jar", None)
    if not jar:
        return set()
    target = urlparse(target_url)
    host = (target.hostname or "").lower()
    path = target.path or "/"
    now = time.time()
    values: set[str] = set()
    for cookie in jar:
        if cookie.name != "cf_clearance" or not cookie.value:
            continue
        domain = str(cookie.domain or "").lstrip(".").lower()
        cookie_path = str(cookie.path or "/")
        if not domain or not (host == domain or host.endswith(f".{domain}")):
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue
        if not (path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/")):
            continue
        values.add(str(cookie.value))
    return values


def solve_challenge(
    session,
    *,
    challenge_url: str,
    challenge_html: str,
    challenge_size: int,
    user_agent: str,
    browser_identity: dict[str, Any] | None = None,
    verification_url: str | None = None,
    node_command: str = "node",
    timeout_seconds: int = 58,
    check_budget=None,
    log=None,
) -> dict[str, Any]:
    if "_cf_chl_opt" not in challenge_html:
        raise RuntimeError("The response does not contain a supported Cloudflare challenge")

    initial_clearance = clearance_values(session, challenge_url)
    parent: subprocess.Popen[str] | None = None
    child: subprocess.Popen[str] | None = None
    input_files: list[Path] = []
    messages: queue.Queue = queue.Queue()
    readers: list[threading.Thread] = []
    active: set[str] = set()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    emit = log or _log

    def check():
        if check_budget:
            check_budget()
        if time.monotonic() >= deadline:
            raise TimeoutError("Cloudflare challenge timed out")

    def timeout(limit):
        check()
        return max(0.1, min(limit, deadline - time.monotonic()))

    def watch(name, process):
        # select() 在 Windows 上只支持 socket，使用独立读取线程处理管道。
        active.add(name)

        def stdout_reader():
            try:
                for line in process.stdout:
                    messages.put((name, process, line))
            finally:
                messages.put((name, process, ""))

        def stderr_reader():
            # 持续排空 stderr，避免 Node 进程因管道满而挂起。
            for _line in process.stderr:
                pass

        for target in (stdout_reader, stderr_reader):
            thread = threading.Thread(target=target, daemon=True)
            readers.append(thread)
            thread.start()

    try:
        check()
        parser = InlineScriptParser()
        parser.feed(challenge_html)
        parent_input = {
            "url": challenge_url,
            "html": challenge_html,
            "inlineScripts": parser.scripts,
            "userAgent": user_agent,
            "browserIdentity": browser_identity or {},
            "waitMs": timeout_seconds * 1000,
            "performanceEntries": [
                performance_entry(challenge_url, "navigation", challenge_size, 400),
            ],
        }
        parent, parent_file = start_runtime(PARENT_RUNTIME, parent_input, node_command)
        input_files.append(parent_file)
        if parent.stdout is None:
            raise RuntimeError("Cloudflare parent runtime stdout is unavailable")
        watch("parent", parent)

        frame_url = ""
        parent_events: dict[str, dict[str, Any]] = {}
        child_started = False
        parent_final: dict[str, Any] = {}
        child_final: dict[str, Any] = {}
        while time.monotonic() < deadline:
            check()
            try:
                source_name, process, line = messages.get(timeout=min(0.2, timeout(0.2)))
            except queue.Empty:
                if not active:
                    raise RuntimeError("Cloudflare runtime exited before completion; check Node.js and jsdom installation")
                continue
            if line:
                message = json.loads(line)
                message_type = message.get("type")
            else:
                active.discard(source_name)
                continue
            if message_type == "request":
                generated = message["request"]
                page_url = challenge_url if source_name == "parent" else frame_url
                response = submit_generated(session, page_url, generated, user_agent, browser_identity, timeout=timeout(45))
                check()
                write_message(process, acknowledge(generated, response))

            elif source_name == "parent" and message_type == "frame":
                frame_url = message["url"]
                emit("Cloudflare Turnstile 子挑战已就绪")

            elif source_name == "parent" and message_type == "frame-message":
                data = message["message"].get("data")
                if isinstance(data, dict) and data.get("event") in {"init", "extraParams", "execute"}:
                    parent_events[str(data["event"])] = data

            elif source_name == "child" and message_type == "parent-message":
                data = message["message"].get("data")
                event = data.get("event") if isinstance(data, dict) else None
                if event not in {"init", "requestExtraParams"}:
                    write_message(parent, {"type": "child-message", "data": data})

            elif message_type == "final":
                if source_name == "parent":
                    parent_final = message.get("result") or {}
                else:
                    child_final = message.get("result") or {}
                active.discard(source_name)

            if not child_started and frame_url and {"init", "extraParams", "execute"}.issubset(parent_events):
                frame_headers = {
                    "user-agent": user_agent,
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "accept-language": str((browser_identity or {}).get("acceptLanguage") or "zh-CN,zh;q=0.9,en;q=0.8"),
                    "referer": challenge_url,
                    "sec-fetch-dest": "iframe",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "cross-site",
                }
                for key in ("secChUa", "secChUaMobile", "secChUaPlatform"):
                    value = (browser_identity or {}).get(key)
                    if value:
                        frame_headers[{"secChUa": "sec-ch-ua", "secChUaMobile": "sec-ch-ua-mobile", "secChUaPlatform": "sec-ch-ua-platform"}[key]] = str(value)
                frame_response = session.get(
                    frame_url,
                    headers=frame_headers,
                    timeout=timeout(40),
                )
                if int(frame_response.status_code) != 200:
                    raise RuntimeError(f"Turnstile frame returned HTTP {frame_response.status_code}")
                child_input = {
                    "url": frame_url,
                    "parentUrl": challenge_url,
                    "html": frame_response.text,
                    "userAgent": user_agent,
                    "browserIdentity": browser_identity or {},
                    "waitMs": max(1, timeout_seconds - 5) * 1000,
                    "performanceEntries": [
                        performance_entry(frame_url, "navigation", len(frame_response.content), 700),
                    ],
                    "parentMessages": [
                        parent_events["init"], parent_events["extraParams"], parent_events["execute"],
                    ],
                }
                child, child_file = start_runtime(CHILD_RUNTIME, child_input, node_command)
                input_files.append(child_file)
                if child.stdout is None:
                    raise RuntimeError("Cloudflare child runtime stdout is unavailable")
                watch("child", child)
                child_started = True

            current_clearance = clearance_values(session, challenge_url)
            if current_clearance - initial_clearance:
                verification = session.get(
                    verification_url or challenge_url,
                    headers=navigation_headers(user_agent, browser_identity),
                    timeout=timeout(30),
                    allow_redirects=False,
                )
                status = int(verification.status_code)
                return {
                    "ok": 200 <= status < 400 and "challenge" not in str(verification.headers.get("cf-mitigated", "")).lower(),
                    "status": status,
                    "clearance": True,
                    "parentErrorCount": len(parent_final.get("errors", [])),
                    "childErrorCount": len(child_final.get("errors", [])),
                }

        raise RuntimeError("Cloudflare challenge did not issue cf_clearance before timeout")
    finally:
        for process in (child, parent):
            if process is None:
                continue
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=1)
        for process in (child, parent):
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        for input_file in input_files:
            input_file.unlink(missing_ok=True)
