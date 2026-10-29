"""登录专用挑战处理：保持同一 HTTP 会话、指纹和代理完成挑战及重放。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from urllib.parse import urljoin, urlsplit

from .auth_errors import AuthFlowError
from .cloudflare.solver import solve_challenge


def resolve_node_binary() -> str:
    configured = os.getenv("CHATGPT_LOGIN_NODE_PATH") or os.getenv("OPENAI_SENTINEL_NODE_PATH")
    if configured:
        return configured
    root = Path(__file__).resolve().parent.parent
    executable = "node.exe" if os.name == "nt" else "node"
    candidates = [root / ".tools" / "node" / executable]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "playwright" / "driver" / executable)
    for package in ("playwright", "patchright"):
        spec = importlib.util.find_spec(package)
        if spec and spec.origin:
            candidates.append(Path(spec.origin).parent / "driver" / executable)
    return next((str(path) for path in candidates if path.is_file()), shutil.which("node") or "node")


def is_cloudflare_challenge(response) -> bool:
    headers = response.headers
    mitigated = headers.get("cf-mitigated", "") or headers.get("x-cf-mitigated", "")
    if "challenge" in mitigated.lower():
        return True
    body = str(response.text or "").lower()
    content_type = headers.get("content-type", "").lower()
    challenge = any(marker in body for marker in ("just a moment", "cdn-cgi", "challenge-platform", "cf-challenge"))
    html = "text/html" in content_type or bool(re.search(r"<(?:!doctype\s+html|html|head|body)\b", body))
    return (response.status_code == 403 and (html or challenge)
            or response.status_code in (400, 409) and html and challenge)


def rotate_proxy_session(proxy: str) -> str | None:
    """仅替换上游支持的代理会话字段；固定代理由用户切换出口。"""
    patterns = (r"(?<=-sid-)[A-Za-z0-9]+(?=-t-\d+)", r"(?<=-[A-Z]{2}-)[A-Za-z0-9]+(?=-\d+m(?:@|$))")
    for pattern in patterns:
        if re.search(pattern, proxy):
            return re.sub(pattern, secrets.token_hex(4), proxy, count=1)
    return None


class LoginChallengeSession:
    def __init__(self, session, profile, *, log, check_budget, deadline: float,
                 retries: int = 3, retry_delay: float = 1, solver=None):
        self.raw = session
        self.profile = profile
        self.log = log
        self.check_budget = check_budget
        self.deadline = deadline
        self.retries = retries
        self.retry_delay = retry_delay
        self.solver = solver or solve_challenge
        self.closed = threading.Event()

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        self.closed.set()
        self.raw.close()

    def check(self):
        self.check_budget()
        if self.closed.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError("登录请求已关闭或超时")

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        follow = kwargs.pop("allow_redirects", True)
        history = []
        for _hop in range(13):
            response = self._request_one(method, url, kwargs)
            location = response.headers.get("location") or response.headers.get("Location")
            if not follow or response.status_code not in (301, 302, 303, 307, 308) or not location:
                if hasattr(response, "history"):
                    response.history = history
                return response
            history.append(response)
            next_url = urljoin(str(response.url or url), location)
            if urlsplit(next_url).scheme not in ("http", "https"):
                raise RuntimeError("登录重定向返回不支持的协议")
            kwargs = dict(kwargs)
            kwargs.pop("params", None)
            if response.status_code == 303 or response.status_code in (301, 302) and method.upper() == "POST":
                method = "GET"
                for name in ("data", "json", "files"):
                    kwargs.pop(name, None)
                kwargs["headers"] = {k: v for k, v in kwargs.get("headers", {}).items()
                                     if k.lower() not in ("content-type", "content-length")}
            if urlsplit(next_url).netloc != urlsplit(url).netloc:
                kwargs["headers"] = {k: v for k, v in kwargs.get("headers", {}).items()
                                     if k.lower() not in ("authorization", "cookie", "host", "origin")}
            url = next_url
        raise RuntimeError("登录重定向次数超过上限")

    def _request_one(self, method, url, kwargs):
        if urlsplit(url).hostname == "auth.openai.com" and method.upper() not in ("GET", "HEAD"):
            headers = {k: v for k, v in kwargs.get("headers", {}).items()
                       if k.lower() != "x-access-flow-invocation-id"}
            headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
            kwargs = dict(kwargs, headers=headers)
        solved = False
        for attempt in range(self.retries + 1):
            self.check()
            request = dict(kwargs, allow_redirects=False)
            requested_timeout = request.get("timeout", 30)
            request["timeout"] = max(0.1, min(float(requested_timeout), self.deadline - time.monotonic()))
            response = self.raw.request(method, url, **request)
            self.check()
            if not is_cloudflare_challenge(response):
                return response
            if not solved and "_cf_chl_opt" in str(response.text or ""):
                solved = True
                self.log(f"遇到 Cloudflare 挑战（{urlsplit(url).hostname}），使用当前代理和登录会话处理")
                try:
                    result = self.solver(
                        self.raw, challenge_url=str(response.url or url),
                        challenge_html=response.text, challenge_size=len(response.content),
                        user_agent=self.profile.user_agent, browser_identity={
                            "userAgent": self.profile.user_agent,
                            "acceptLanguage": self.profile.accept_language,
                            "secChUa": self.profile.sec_ch_ua,
                            "secChUaMobile": "?0",
                            "secChUaPlatform": self.profile.sec_ch_ua_platform,
                            "platform": self.profile.navigator_platform,
                            "locale": self.profile.language,
                            "languages": list(self.profile.languages),
                        },
                        verification_url=urljoin(str(response.url or url), "/"),
                        node_command=resolve_node_binary(),
                        timeout_seconds=min(70, self.deadline - time.monotonic()),
                        check_budget=self.check, log=self.log,
                    )
                except Exception as exc:
                    self.check()
                    # 异常正文可能含挑战 token；任务只记录类型。
                    self.log(f"Cloudflare 挑战处理失败：{type(exc).__name__}")
                else:
                    if result.get("ok"):
                        self.log("Cloudflare 挑战已通过，使用原会话重放请求")
                        self.check()
                        request["timeout"] = max(0.1, min(float(requested_timeout), self.deadline - time.monotonic()))
                        response = self.raw.request(method, url, **request)
                        self.check()
                        if not is_cloudflare_challenge(response):
                            return response
                    else:
                        status = result.get("status")
                        detail = f"（验证 HTTP {status}）" if isinstance(status, int) else ""
                        self.log(f"Cloudflare 挑战未通过{detail}，继续同一代理重试")
            if attempt >= self.retries:
                error = AuthFlowError.from_response("登录请求", response)
                error.error_code = "cloudflare_challenge"
                raise error
            self.log(f"Cloudflare 仍拦截请求，保持同一代理重试 {attempt + 1}/{self.retries}")
            retry_until = min(self.deadline, time.monotonic() + self.retry_delay)
            while time.monotonic() < retry_until:
                self.check()
                self.closed.wait(min(0.1, retry_until - time.monotonic()))
