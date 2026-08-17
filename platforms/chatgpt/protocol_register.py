"""ChatGPT email registration through the OpenAI web protocol.

Signup remains direct HTTP; a hidden Chromium page is used only to execute the
official Sentinel JavaScript required for the create-account security token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import quote, urlencode, urljoin, urlparse

from curl_cffi import requests

from .constants import (
    CHATGPT_APP,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    SENTINEL_BASE,
    SENTINEL_FRAME_URL,
    SENTINEL_REQ_URL,
    SENTINEL_SDK_URL,
)


FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Daniel", "Matthew", "Anthony", "Mary", "Linda",
    "Jennifer", "Sarah", "Jessica", "Elizabeth",
)
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Martin",
    "Lee", "White",
)


def _random_profile() -> tuple[str, str]:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    age = random.randint(24, 36)
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    return name, birthdate


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _response_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _response_error(response, payload: dict | None = None) -> str:
    data = payload or _response_json(response)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message and code not in message:
            return f"{code}: {message}"
        if message or code:
            return message or code
    if isinstance(error, str) and error:
        return error
    text = str(getattr(response, "text", "") or "").strip()
    return text[:300] or f"HTTP {getattr(response, 'status_code', 0)}"


def _decode_totp_secret(secret: str) -> bytes:
    cleaned = str(secret or "").replace(" ", "").upper().rstrip("=")
    if not cleaned or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for char in cleaned):
        raise ValueError("TOTP secret 不是有效的 Base32 字符串")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except Exception as exc:
        raise ValueError("TOTP secret Base32 解码失败") from exc


def _generate_totp_code(secret: str, *, timestamp: float | None = None) -> str:
    key = _decode_totp_secret(secret)
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(value % 1_000_000).zfill(6)


def _build_totp_otpauth(email: str, secret: str) -> str:
    label = quote(str(email or "chatgpt"), safe="")
    return (
        f"otpauth://totp/OpenAI:{label}?secret={secret}"
        "&issuer=OpenAI&algorithm=SHA1&digits=6&period=30"
    )


def _continue_url(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = str(payload.get("continue_url") or "").strip()
    if direct:
        return direct
    nested = payload.get("data")
    if isinstance(nested, dict) and nested is not payload:
        nested_url = _continue_url(nested)
        if nested_url:
            return nested_url
    page = payload.get("page")
    page_payload = page.get("payload") if isinstance(page, dict) else None
    return str(page_payload.get("url") or "").strip() if isinstance(page_payload, dict) else ""


class _SentinelTokenGenerator:
    """Generate the requirements/enforcement PoW used by OpenAI Sentinel."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return f"{value & 0xFFFFFFFF:08x}"

    @staticmethod
    def _encode(value) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _fingerprint(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                time.gmtime(),
            ),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice((4, 8, 12, 16)),
            int(time.time() * 1000 - perf_now),
        ]

    def _reference_fingerprint(self) -> list:
        """25-field fingerprint used by the current Sentinel SDK."""
        now = datetime.now().astimezone()
        perf_now = round(
            time.time() * 1000 - 1_000_000 + random.uniform(1000, 5000), 1
        )
        time_origin = round(time.time() * 1000 - 50_000, 1)
        return [
            3000,
            str(now),
            4294705152,
            0,
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            "en-US",
            "en-US,en",
            0,
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            8,
            time_origin,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    def _solve_reference_pow(self, seed: str, difficulty: str, data: list) -> str:
        started = time.perf_counter()
        target = str(difficulty or "0")
        for nonce in range(500_000):
            data[3] = nonce
            data[9] = round((time.perf_counter() - started) * 1000)
            encoded = self._encode(data)
            digest = self._fnv1a32(str(seed or "") + encoded)
            if digest[: len(target)] <= target:
                return encoded + "~S"
        return self._encode("e")

    def requirements(self) -> str:
        config = self._reference_fingerprint()
        config[3] = 1
        config[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._solve_reference_pow(
            str(random.random()), "0", config
        )

    def enforcement(self, seed: str, difficulty: str) -> str:
        return "gAAAAAB" + self._solve_reference_pow(
            seed, difficulty, self._reference_fingerprint()
        )


class _SentinelBrowserRuntime:
    """Run Sentinel in the project's Camoufox browser runtime.

    Registration requests themselves remain protocol-based.  Sentinel may
    need JavaScript/browser state for an encrypted proof; that narrow step
    must use Camoufox, not a separate Playwright Chromium installation.
    """

    _sdk_lock = threading.Lock()
    _sdk_code: str | None = None

    def __init__(self, session, *, user_agent: str, proxy: str | None):
        from camoufox.sync_api import Camoufox

        del user_agent  # Camoufox supplies a coherent browser fingerprint.
        self._camoufox = None
        self._browser = None
        self._page = None
        launch_options = {
            "headless": True,
            "locale": "en-US",
            "block_webrtc": True,
        }
        if proxy:
            parsed_proxy = urlparse(proxy)
            if parsed_proxy.scheme and parsed_proxy.hostname and parsed_proxy.port:
                proxy_config = {
                    "server": (
                        f"{parsed_proxy.scheme}://"
                        f"{parsed_proxy.hostname}:{parsed_proxy.port}"
                    )
                }
                if parsed_proxy.username:
                    proxy_config["username"] = parsed_proxy.username
                if parsed_proxy.password:
                    proxy_config["password"] = parsed_proxy.password
                launch_options["proxy"] = proxy_config
            else:
                launch_options["proxy"] = {"server": proxy}

        # Keep the context manager alive for the complete Sentinel session.
        # Camoufox guarantees that a failed launch releases its Sync API loop.
        self._camoufox = Camoufox(**launch_options)
        self._browser = self._camoufox.__enter__()
        self._page = self._browser.new_page()
        try:
            self._page.goto(
                f"{OPENAI_AUTH}/about-you",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception:
            self._page.goto("https://auth.openai.com/about-you", wait_until="domcontentloaded")

        with self._sdk_lock:
            if self._sdk_code is None:
                response = session.get(SENTINEL_SDK_URL, timeout=30)
                if getattr(response, "status_code", 0) >= 400:
                    raise RuntimeError(
                        f"Sentinel SDK 获取失败: HTTP {response.status_code}"
                    )
                code = str(getattr(response, "text", "") or "")
                if not code:
                    raise RuntimeError("Sentinel SDK 返回为空")
                self.__class__._sdk_code = code
            sdk_code = self._sdk_code
        hook = "t.token=ye,t}({});"
        replacement = (
            "t.___n=_n,t.__Nt=Nt,t.__D=D,t.__jt=jt,"
            "t.token=ye,t}({});"
        )
        if hook not in sdk_code:
            raise RuntimeError("Sentinel SDK 内部接口发生变化，无法生成 VM token")
        self._page.evaluate(
            "code => window.eval(code)", sdk_code.replace(hook, replacement)
        )
        if self._page.evaluate("typeof window.SentinelSDK") != "object":
            raise RuntimeError("Sentinel SDK 初始化失败")

    @classmethod
    def create(cls, *args, **kwargs):
        """Construct the runtime without leaking it when initialization fails."""
        runtime = cls.__new__(cls)
        try:
            cls.__init__(runtime, *args, **kwargs)
        except Exception:
            runtime.close()
            raise
        return runtime

    @staticmethod
    def _looks_like_vm_error(value: str) -> bool:
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            return False
        lowered = decoded.lower()
        return "syntaxerror" in lowered or "typeerror" in lowered or "error:" in lowered

    def vm_tokens(self, chat_req: dict, cached_proof: str) -> dict[str, str]:
        result = self._page.evaluate(
            """async ({ chatReq, cachedProof }) => {
                const sdk = window.SentinelSDK;
                sdk.__D(chatReq, cachedProof);
                const turnstile = chatReq.turnstile || {};
                const t = turnstile.dx
                    ? await sdk.___n(chatReq, turnstile.dx)
                    : null;
                let so = null;
                const observer = chatReq.so || {};
                if (observer.collector_dx && typeof sdk.__Nt === "function") {
                    so = await sdk.__Nt(observer.collector_dx);
                }
                let soFallback = null;
                if (observer.snapshot_dx && typeof sdk.__jt === "function") {
                    soFallback = await sdk.__jt(observer.snapshot_dx, cachedProof);
                }
                return { t, so, soFallback };
            }""",
            {"chatReq": chat_req, "cachedProof": cached_proof},
        )
        t_value = str((result or {}).get("t") or "")
        if (chat_req.get("turnstile", {}).get("required") and not t_value):
            raise RuntimeError("Sentinel Turnstile VM 未生成 t token")
        so_value = str((result or {}).get("so") or "")
        if so_value and self._looks_like_vm_error(so_value):
            so_value = ""
        if not so_value:
            fallback = str((result or {}).get("soFallback") or "")
            if fallback and not self._looks_like_vm_error(fallback):
                so_value = fallback
        return {"t": t_value, "so": so_value}

    def token_headers(self, flow: str) -> dict[str, str]:
        result = self._page.evaluate(
            """async flow => {
                const sdk = window.SentinelSDK;
                const token = await sdk.token(flow);
                let so = null;
                if (typeof sdk.sessionObserverToken === "function") {
                    so = await sdk.sessionObserverToken(flow);
                }
                return { token, so };
            }""",
            flow,
        )
        token = result.get("token") if isinstance(result, dict) else None
        if isinstance(token, str):
            try:
                token = json.loads(token)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Sentinel SDK 返回的 token 不是 JSON") from exc
        if not isinstance(token, dict):
            raise RuntimeError("Sentinel SDK 未返回 token")
        missing = [
            key for key in ("p", "t", "c", "id", "flow")
            if not str(token.get(key) or "")
        ]
        if missing:
            raise RuntimeError("Sentinel token 缺少字段: " + ", ".join(missing))

        headers = {
            "openai-sentinel-token": json.dumps(token, separators=(",", ":")),
        }
        so = result.get("so") if isinstance(result, dict) else None
        if isinstance(so, str):
            try:
                so = json.loads(so)
            except json.JSONDecodeError:
                so = None
        if isinstance(so, dict) and so:
            headers["openai-sentinel-so-token"] = json.dumps(
                so, separators=(",", ":")
            )
        return headers

    def close(self) -> None:
        runtime = getattr(self, "_camoufox", None)
        self._camoufox = None
        self._browser = None
        self._page = None
        if runtime is not None:
            try:
                runtime.__exit__(None, None, None)
            except Exception:
                pass


class OpenAISentinelClient:
    def __init__(
        self,
        session,
        *,
        user_agent: str,
        proxy: str | None = None,
        use_browser_runtime: bool = True,
    ):
        self.session = session
        self.user_agent = user_agent
        self.proxy = proxy
        self.use_browser_runtime = use_browser_runtime
        self._browser_runtime: _SentinelBrowserRuntime | None = None

    def build_headers(self, device_id: str, flow: str) -> dict[str, str]:
        if self.use_browser_runtime:
            generator = _SentinelTokenGenerator(self.user_agent)
            proof = generator.requirements()
            response = self.session.post(
                SENTINEL_REQ_URL,
                data=json.dumps({"p": proof, "id": device_id, "flow": flow}),
                headers={
                    "accept": "*/*",
                    "content-type": "text/plain;charset=UTF-8",
                    "origin": SENTINEL_BASE,
                    "referer": SENTINEL_FRAME_URL,
                },
            )
            chat_req = _response_json(response)
            challenge = str(chat_req.get("token") or "").strip()
            if getattr(response, "status_code", 0) >= 400 or not challenge:
                raise RuntimeError(
                    f"Sentinel challenge 获取失败: {_response_error(response, chat_req)}"
                )
            if self._browser_runtime is None:
                self._browser_runtime = _SentinelBrowserRuntime.create(
                    self.session,
                    user_agent=self.user_agent,
                    proxy=self.proxy,
                )
            vm = self._browser_runtime.vm_tokens(chat_req, proof)
            pow_info = chat_req.get("proofofwork") or {}
            if pow_info.get("required") and pow_info.get("seed"):
                enforcement = generator.enforcement(
                    str(pow_info.get("seed") or ""),
                    str(pow_info.get("difficulty") or "0"),
                )
            else:
                enforcement = proof
            token = {
                "p": enforcement,
                "t": vm.get("t") or "",
                "c": challenge,
                "id": device_id,
                "flow": flow,
            }
            headers = {
                "openai-sentinel-token": json.dumps(token, separators=(",", ":"))
            }
            if vm.get("so"):
                so_token = {
                    "so": vm["so"],
                    "c": challenge,
                    "id": device_id,
                    "flow": flow,
                }
                headers["openai-sentinel-so-token"] = json.dumps(
                    so_token, separators=(",", ":")
                )
            return headers
        return {"openai-sentinel-token": self._build_legacy_header(device_id, flow)}

    def build_header(self, device_id: str, flow: str) -> str:
        return self.build_headers(device_id, flow)["openai-sentinel-token"]

    def _build_legacy_header(self, device_id: str, flow: str) -> str:
        generator = _SentinelTokenGenerator(self.user_agent)
        proof = generator.requirements()
        response = self.session.post(
            SENTINEL_REQ_URL,
            data=json.dumps({"p": proof, "id": device_id, "flow": flow}),
            headers={
                "accept": "*/*",
                "content-type": "text/plain;charset=UTF-8",
                "origin": SENTINEL_BASE,
                "referer": SENTINEL_FRAME_URL,
            },
        )
        payload = _response_json(response)
        challenge = str(payload.get("token") or "").strip()
        if getattr(response, "status_code", 0) >= 400 or not challenge:
            raise RuntimeError(f"Sentinel challenge 获取失败: {_response_error(response, payload)}")
        pow_info = payload.get("proofofwork") or {}
        if pow_info.get("required") and pow_info.get("seed"):
            enforcement = generator.enforcement(
                str(pow_info.get("seed") or ""),
                str(pow_info.get("difficulty") or "0"),
            )
        else:
            enforcement = proof
        return json.dumps(
            {
                "p": enforcement,
                "t": "",
                "c": challenge,
                "id": device_id,
                "flow": flow,
            },
            separators=(",", ":"),
        )

    def close(self) -> None:
        if self._browser_runtime is not None:
            self._browser_runtime.close()
            self._browser_runtime = None


class ChatGPTProtocolRegister:
    """Synchronous worker compatible with ``ProtocolMailboxAdapter``."""

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        log_fn: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        impersonate: str = "firefox144",
        session=None,
        sentinel_runtime: bool = True,
    ):
        self.proxy = str(proxy or "").strip() or None
        self.otp_callback = otp_callback
        self.log = log_fn or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)
        if session is None:
            kwargs = {"impersonate": impersonate, "timeout": 60}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            session = requests.Session(**kwargs)
        self.session = session
        self.sentinel = OpenAISentinelClient(
            session,
            user_agent=self.user_agent,
            proxy=self.proxy,
            use_browser_runtime=sentinel_runtime,
        )
        self.device_id = str(uuid.uuid4())

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise RuntimeError("任务已取消")

    def _common_headers(self, referer: str) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": OPENAI_AUTH,
            "referer": referer,
            "user-agent": self.user_agent,
        }

    def _follow_authorize_chain(self, location: str) -> None:
        current = str(location or "").strip()
        for _ in range(15):
            if not current:
                return
            self._check_cancelled()
            response = self.session.get(urljoin(OPENAI_AUTH, current), allow_redirects=False)
            current = str(response.headers.get("location") or "").strip()
        raise RuntimeError("OpenAI 授权重定向次数过多")

    def _initialize_signup(self, email: str) -> None:
        self.log("初始化 ChatGPT 协议注册会话...")
        response = self.session.get(CHATGPT_APP, allow_redirects=True)
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(f"ChatGPT 首页访问失败: {_response_error(response)}")
        csrf_response = self.session.get(f"{CHATGPT_APP}/api/auth/csrf")
        csrf_payload = _response_json(csrf_response)
        csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
        if getattr(csrf_response, "status_code", 0) != 200 or not csrf_token:
            raise RuntimeError(f"CSRF 获取失败: {_response_error(csrf_response, csrf_payload)}")

        query = urlencode(
            {
                "prompt": "login",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                "screen_hint": "login_or_signup",
                "login_hint": email,
            }
        )
        signin_response = self.session.post(
            f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
            data=urlencode(
                {
                    "callbackUrl": f"{CHATGPT_APP}/",
                    "csrfToken": csrf_token,
                    "json": "true",
                }
            ),
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_APP,
                "referer": f"{CHATGPT_APP}/",
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        signin_payload = _response_json(signin_response)
        location = str(
            signin_payload.get("url")
            or signin_response.headers.get("location")
            or ""
        ).strip()
        if getattr(signin_response, "status_code", 0) >= 400 or not location:
            raise RuntimeError(f"OpenAI 注册授权初始化失败: {_response_error(signin_response, signin_payload)}")
        self._follow_authorize_chain(location)
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass

    def _validate_otp(self, code: str) -> dict:
        response = self.session.post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            json={"code": code},
            headers=self._common_headers(f"{OPENAI_AUTH}/email-verification"),
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"邮箱验证码校验失败: {_response_error(response, payload)}")
        return payload

    def _session_payload(self) -> dict:
        response = self.session.get(f"{CHATGPT_APP}/api/auth/session")
        payload = _response_json(response)
        access_token = str(payload.get("accessToken") or "").strip()
        if getattr(response, "status_code", 0) != 200 or not access_token:
            raise RuntimeError(
                f"获取 ChatGPT session/accessToken 失败: {_response_error(response, payload)}"
            )
        return payload

    def _chatgpt_security_headers(self, access_token: str, target_path: str) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {access_token}",
            "oai-device-id": self.device_id,
            "oai-session-id": str(uuid.uuid4()),
            "oai-language": "en-US",
            "x-openai-target-path": target_path,
            "x-openai-target-route": target_path,
            "origin": CHATGPT_APP,
            "referer": f"{CHATGPT_APP}/",
            "user-agent": self.user_agent,
        }

    @staticmethod
    def _auth_url(value: str) -> str:
        target = str(value or "").strip()
        return urljoin(OPENAI_AUTH, target) if target else ""

    def _follow_security_url(self, value: str, *, referer: str = "") -> str:
        target = str(value or "").strip()
        if not target:
            return ""
        target = self._auth_url(target)
        response = self.session.get(
            target,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": referer or f"{CHATGPT_APP}/",
                "user-agent": self.user_agent,
            },
            allow_redirects=True,
        )
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(f"安全设置页面访问失败: {_response_error(response)}")
        final_url = str(getattr(response, "url", "") or "").strip()
        if not final_url:
            final_url = str(response.headers.get("location") or target).strip()
        return final_url

    def _start_security_reauth_protocol(
        self,
        *,
        email: str,
        params: dict,
        callback_url: str,
    ) -> str:
        csrf_response = self.session.get(f"{CHATGPT_APP}/api/auth/csrf")
        csrf_payload = _response_json(csrf_response)
        csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
        if getattr(csrf_response, "status_code", 0) != 200 or not csrf_token:
            raise RuntimeError(
                f"安全设置 re-auth 获取 CSRF token 失败: "
                f"{_response_error(csrf_response, csrf_payload)}"
            )

        query = {
            "login_hint": email,
            "ext-oai-did": self.device_id,
        }
        query.update(
            {
                str(key): str(value)
                for key, value in (params or {}).items()
                if value not in (None, "")
            }
        )
        response = self.session.post(
            f"{CHATGPT_APP}/api/auth/signin/openai?{urlencode(query)}",
            data=urlencode(
                {
                    "callbackUrl": callback_url,
                    "csrfToken": csrf_token,
                    "json": "true",
                }
            ),
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_APP,
                "referer": f"{CHATGPT_APP}/",
                "user-agent": self.user_agent,
            },
            allow_redirects=False,
        )
        payload = _response_json(response)
        auth_url = str(
            payload.get("url") or response.headers.get("location") or ""
        ).strip()
        if getattr(response, "status_code", 0) >= 400 or not auth_url:
            raise RuntimeError(
                f"安全设置 re-auth 启动失败: {_response_error(response, payload)}"
            )
        self.log("安全设置 re-auth 已启动")
        return self._follow_security_url(auth_url, referer=f"{CHATGPT_APP}/")

    def _resend_email_otp_protocol(self, *, referer: str) -> None:
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/email-otp/resend",
            json={},
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": OPENAI_AUTH,
                "referer": referer or f"{OPENAI_AUTH}/email-verification",
                "user-agent": self.user_agent,
                "x-access-flow-invocation-id": str(uuid.uuid4()),
            },
        )
        if getattr(response, "status_code", 0) == 429:
            raise RuntimeError("安全设置 re-auth 邮箱验证码重发过于频繁，请稍后再试")
        if getattr(response, "status_code", 0) >= 400:
            raise RuntimeError(
                f"安全设置 re-auth 邮箱验证码重发失败: {_response_error(response)}"
            )
        self.log("安全设置 re-auth 已通过 API 请求重发邮箱验证码")

    def _validate_email_otp_protocol(self, code: str, *, referer: str):
        headers = self._common_headers(referer or f"{OPENAI_AUTH}/email-verification")
        headers.update(self.sentinel.build_headers(self.device_id, "email_otp_validate"))
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/email-otp/validate",
            json={"code": str(code or "").strip()},
            headers=headers,
        )
        return response, _response_json(response)

    def _get_new_email_code_protocol(
        self,
        excluded_codes: set[str],
        *,
        deadline: float,
    ) -> str:
        if not callable(self.otp_callback):
            return ""
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            if getattr(self.otp_callback, "supports_timeout_override", False):
                code = self.otp_callback(timeout_override=remaining)
            else:
                code = self.otp_callback()
            code = str(code or "").strip()
            if not code:
                return ""
            if code not in excluded_codes:
                return code
            self.log(f"忽略注册阶段已使用的邮箱验证码: {code}，继续等待 re-auth 新验证码")
            time.sleep(0.5)
        return ""

    def _verify_password_reauth_protocol(self, password: str, *, referer: str) -> dict:
        response = self.session.post(
            f"{OPENAI_AUTH}/api/accounts/password/verify",
            json={"password": password},
            headers=self._common_headers(referer),
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(
                f"安全设置 re-auth 登录密码校验失败: {_response_error(response, payload)}"
            )
        return payload

    def _complete_security_reauth_protocol(
        self,
        *,
        start_url: str,
        password: str,
        secret: str,
        excluded_codes: set[str],
        expect_password_page: bool,
        timeout: int = 120,
    ) -> str:
        deadline = time.time() + timeout
        current_url = str(start_url or "").strip()
        email_resend_requested = False

        while time.time() < deadline:
            self._check_cancelled()
            path = urlparse(current_url).path.lower()
            if path == "/email-verification":
                if not callable(self.otp_callback):
                    raise RuntimeError("安全设置 re-auth 需要邮箱验证码，但未提供 otp_callback")
                if not email_resend_requested:
                    self._resend_email_otp_protocol(referer=current_url)
                    email_resend_requested = True
                code = self._get_new_email_code_protocol(
                    excluded_codes,
                    deadline=deadline,
                )
                if not code:
                    raise RuntimeError("安全设置 re-auth 未获取到新的邮箱验证码")
                response, payload = self._validate_email_otp_protocol(
                    code,
                    referer=current_url,
                )
                if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
                    excluded_codes.add(code)
                    self.log(
                        "安全设置 re-auth 邮箱验证码校验失败: "
                        f"{_response_error(response, payload)}，准备重新请求新验证码"
                    )
                    email_resend_requested = False
                    continue
                continue_url = _continue_url(payload)
                if not continue_url:
                    raise RuntimeError(
                        "安全设置 re-auth 邮箱验证码验证成功但未返回密码页面 URL"
                    )
                excluded_codes.add(code)
                current_url = self._follow_security_url(
                    continue_url,
                    referer=current_url,
                )
                self.log("安全设置 re-auth 邮箱验证码验证通过，已进入密码页面")
                continue

            if path == "/log-in/password":
                verified = self._verify_password_reauth_protocol(
                    password,
                    referer=current_url,
                )
                continue_url = _continue_url(verified)
                if not continue_url:
                    raise RuntimeError("安全设置 re-auth 密码校验成功但未返回后续页面 URL")
                current_url = self._follow_security_url(
                    continue_url,
                    referer=current_url,
                )
                continue

            if "/mfa-challenge/" in path:
                if not secret:
                    raise RuntimeError("安全设置 re-auth 需要 TOTP，但当前没有可用的 secret")
                factor_id = path.rstrip("/").rsplit("/", 1)[-1]
                issue_response = self.session.post(
                    f"{OPENAI_AUTH}/api/accounts/mfa/issue_challenge",
                    json={
                        "type": "totp",
                        "id": factor_id,
                        "force_fresh_challenge": False,
                    },
                    headers=self._common_headers(current_url),
                )
                issue_payload = _response_json(issue_response)
                if getattr(issue_response, "status_code", 0) >= 400 or issue_payload.get("error"):
                    raise RuntimeError(
                        f"安全设置 re-auth TOTP challenge 启动失败: "
                        f"{_response_error(issue_response, issue_payload)}"
                    )
                verify_response = self.session.post(
                    f"{OPENAI_AUTH}/api/accounts/mfa/verify",
                    json={
                        "type": "totp",
                        "id": factor_id,
                        "code": _generate_totp_code(secret),
                    },
                    headers=self._common_headers(current_url),
                )
                verify_payload = _response_json(verify_response)
                if getattr(verify_response, "status_code", 0) >= 400 or verify_payload.get("error"):
                    raise RuntimeError(
                        f"安全设置 re-auth TOTP 校验失败: "
                        f"{_response_error(verify_response, verify_payload)}"
                    )
                continue_url = _continue_url(verify_payload)
                if not continue_url:
                    raise RuntimeError("安全设置 re-auth TOTP 校验成功但未返回后续页面 URL")
                current_url = self._follow_security_url(
                    continue_url,
                    referer=current_url,
                )
                continue

            if path == "/reset-password/new-password":
                return current_url

            parsed = urlparse(current_url)
            if parsed.netloc.lower().endswith("chatgpt.com"):
                if expect_password_page:
                    raise RuntimeError("安全设置 re-auth 未进入设置密码页面")
                return current_url

            if "error=" in parsed.query.lower():
                raise RuntimeError(f"安全设置 re-auth 失败: {parsed.query[:300]}")
            raise RuntimeError(
                f"安全设置 re-auth 出现未支持的页面: {parsed.path or current_url}"
            )

        raise RuntimeError("安全设置 re-auth 超时")

    def _setup_totp_protocol(
        self,
        *,
        email: str,
        password: str,
        session_payload: dict,
        excluded_codes: set[str],
    ) -> dict:
        access_token = str(session_payload.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("TOTP 设置前缺少 ChatGPT accessToken")
        info_path = "/backend-api/accounts/mfa_info"
        info_response = self.session.get(
            f"{CHATGPT_APP}{info_path}",
            headers=self._chatgpt_security_headers(access_token, info_path),
        )
        info_payload = _response_json(info_response)
        if getattr(info_response, "status_code", 0) >= 400 or info_payload.get("error"):
            raise RuntimeError(
                f"读取 ChatGPT 2FA 状态失败: {_response_error(info_response, info_payload)}"
            )
        info_data = info_payload.get("data") if isinstance(info_payload.get("data"), dict) else info_payload
        if bool(info_data.get("mfa_enabled_v2")):
            self.log("2FA 已启用，跳过重复 enrollment")
            return {
                "totp_set": True,
                "totp_already_enabled": True,
                "totp_secret": "",
                "otpauth": "",
            }

        def enroll():
            path = "/backend-api/accounts/mfa/enroll"
            response = self.session.post(
                f"{CHATGPT_APP}{path}",
                json={"factor_type": "totp"},
                headers=self._chatgpt_security_headers(access_token, path),
            )
            return response, _response_json(response)

        enroll_response, enroll_payload = enroll()
        if getattr(enroll_response, "status_code", 0) >= 400 or enroll_payload.get("error"):
            self.log(
                "TOTP enrollment 需要 re-auth: "
                f"{_response_error(enroll_response, enroll_payload)}"
            )
            start_url = self._start_security_reauth_protocol(
                email=email,
                params={
                    "connection": "password",
                    "reauth": "password",
                    "max_age": "0",
                },
                callback_url=f"{CHATGPT_APP}/?security_setup=totp_continue",
            )
            self._complete_security_reauth_protocol(
                start_url=start_url,
                password=password,
                secret="",
                excluded_codes=excluded_codes,
                expect_password_page=False,
            )
            session_payload = self._session_payload()
            access_token = str(session_payload.get("accessToken") or "").strip()
            enroll_response, enroll_payload = enroll()

        enroll_data = enroll_payload.get("data") if isinstance(enroll_payload.get("data"), dict) else enroll_payload
        secret = str(enroll_data.get("secret") or "").replace(" ", "").upper()
        session_id = str(enroll_data.get("session_id") or "").strip()
        if not secret or not session_id:
            raise RuntimeError(
                f"TOTP enrollment 未返回 secret/session_id: "
                f"{_response_error(enroll_response, enroll_payload)}"
            )

        activate_path = "/backend-api/accounts/mfa/user/activate_enrollment"
        activate_response = self.session.post(
            f"{CHATGPT_APP}{activate_path}",
            json={
                "code": _generate_totp_code(secret),
                "factor_type": "totp",
                "session_id": session_id,
            },
            headers=self._chatgpt_security_headers(access_token, activate_path),
        )
        activate_payload = _response_json(activate_response)
        activate_data = (
            activate_payload.get("data")
            if isinstance(activate_payload.get("data"), dict)
            else activate_payload
        )
        if (
            getattr(activate_response, "status_code", 0) >= 400
            or activate_payload.get("error")
            or activate_data.get("success") is not True
        ):
            raise RuntimeError(
                f"TOTP activate 失败: {_response_error(activate_response, activate_payload)}"
            )
        self.log(f"TOTP enrollment/activate 成功，Base32 Secret: {secret}")
        return {
            "totp_set": True,
            "totp_already_enabled": False,
            "totp_secret": secret,
            "otpauth": _build_totp_otpauth(email, secret),
        }

    def _add_password_protocol(self, *, password: str, password_page_url: str) -> dict:
        results = []
        for path in ("/api/accounts/password/add", "/api/accounts/password/reset"):
            headers = self._common_headers(password_page_url or f"{OPENAI_AUTH}/reset-password/new-password")
            headers.update(self.sentinel.build_headers(self.device_id, "password_reset"))
            response = self.session.post(
                f"{OPENAI_AUTH}{path}",
                json={"password": password},
                headers=headers,
            )
            payload = _response_json(response)
            error = _response_error(response, payload)
            results.append(f"{path}->{getattr(response, 'status_code', 0)}")
            if getattr(response, "status_code", 0) < 400 and not payload.get("error"):
                continue_url = _continue_url(payload)
                if continue_url:
                    self._follow_security_url(continue_url, referer=password_page_url)
                return {
                    "password_set": True,
                    "password_path": path,
                }
            if "password_already_set" in error.lower():
                self.log(f"设置密码 API 返回已设置: {path}")
                return {
                    "password_set": True,
                    "password_path": f"already_set:{path}",
                }
            if path == "/api/accounts/password/add":
                self.log(f"设置密码 API 失败，尝试备用接口: {error[:180]}")
        raise RuntimeError(f"设置密码 API 失败: {'; '.join(results) or 'unknown'}")

    def _setup_password_protocol(
        self,
        *,
        email: str,
        password: str,
        secret: str,
        excluded_codes: set[str],
    ) -> dict:
        callback_url = f"{CHATGPT_APP}/?security_setup=password_done"
        start_url = self._start_security_reauth_protocol(
            email=email,
            params={
                "connection": "password",
                "reauth": "password",
                "post_login_add_password": "true",
                "prompt": "login",
                "max_age": "0",
            },
            callback_url=callback_url,
        )
        password_page_url = self._complete_security_reauth_protocol(
            start_url=start_url,
            password=password,
            secret=secret,
            excluded_codes=excluded_codes,
            expect_password_page=True,
        )
        return self._add_password_protocol(
            password=password,
            password_page_url=password_page_url,
        )

    def _setup_password_and_totp_protocol(
        self,
        *,
        email: str,
        password: str,
        excluded_codes: set[str],
    ) -> dict:
        session_payload = self._session_payload()
        self.log("安全设置前置认证完成")
        totp_result = self._setup_totp_protocol(
            email=email,
            password=password,
            session_payload=session_payload,
            excluded_codes=excluded_codes,
        )
        if totp_result.get("totp_already_enabled") and not totp_result.get("totp_secret"):
            # 注册阶段已经提交过密码；已有 TOTP 时无法推导出账户原有的 secret，
            # 因此保留注册密码，避免再次启动一个无法完成的 TOTP re-auth。
            self.log("2FA 已存在且未返回 secret，保留注册时已设置的密码")
            password_result = {
                "password_set": True,
                "password_path": "registration_existing",
            }
        else:
            password_result = self._setup_password_protocol(
                email=email,
                password=password,
                secret=str(totp_result.get("totp_secret") or ""),
                excluded_codes=excluded_codes,
            )
        if password_result.get("password_set"):
            self.log(f"设置的密码: {password}")
        return {**totp_result, **password_result}

    def _register_password(self, email: str, password: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account/password")
        headers.update(self.sentinel.build_headers(
            self.device_id,
            "username_password_create",
        ))
        response = self.session.post(
            OPENAI_API_ENDPOINTS["register"],
            json={"password": password, "username": email},
            headers=headers,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"设置 ChatGPT 密码失败: {_response_error(response, payload)}")
        return payload

    def _create_account(self, name: str, birthdate: str) -> dict:
        last_error = ""
        for attempt in range(3):
            self._check_cancelled()
            # Generate a fresh Sentinel proof for each retry.  Reusing a
            # rejected proof makes registration_disallowed retries ineffective.
            headers = self._common_headers(f"{OPENAI_AUTH}/about-you")
            headers.update(
                self.sentinel.build_headers(self.device_id, "oauth_create_account")
            )
            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                json={"name": name, "birthdate": birthdate},
                headers=headers,
            )
            payload = _response_json(response)
            if getattr(response, "status_code", 0) < 400 and not payload.get("error"):
                return payload
            last_error = _response_error(response, payload)
            if "registration_disallowed" not in last_error or attempt >= 2:
                break
            self.log(f"创建账号被临时拒绝，正在重试 ({attempt + 1}/3)...")
            time.sleep(2)
        raise RuntimeError(f"创建 ChatGPT 账号失败: {last_error}")

    def _session_result(self, email: str, password: str) -> dict:
        payload = self._session_payload()
        access_token = str(payload.get("accessToken") or "").strip()
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        claims = _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        account_id = str(
            auth_claims.get("chatgpt_account_id")
            or account.get("id")
            or ""
        )
        workspace_id = str(auth_claims.get("organization_id") or account_id)
        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = {}
        return {
            "email": email,
            "password": password,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "session_token": str(payload.get("sessionToken") or ""),
            "refresh_token": "",
            "id_token": "",
            "cookies": cookies,
            "profile": account,
            "expires_at": payload.get("expires") or "",
        }

    def run(self, *, email: str, password: str) -> dict:
        if not str(email or "").strip():
            raise RuntimeError("协议注册缺少邮箱")
        if not callable(self.otp_callback):
            raise RuntimeError("协议注册缺少 Outlook 验证码回调")
        self._check_cancelled()
        self.log(f"开始 ChatGPT 协议注册: {email}")
        registration_email_codes: set[str] = set()
        try:
            self._initialize_signup(email)
            self.log("等待 Outlook 验证码...")
            code = str(self.otp_callback() or "").strip()
            if not code:
                raise RuntimeError("未收到 Outlook 验证码")
            registration_email_codes.add(code)
            validation = self._validate_otp(code)
            self.log("邮箱验证码校验通过")
            continue_url = str(validation.get("continue_url") or "").strip()
            if continue_url:
                self.session.get(
                    urljoin(OPENAI_AUTH, continue_url),
                    headers={
                        "referer": f"{OPENAI_AUTH}/email-verification",
                        "user-agent": self.user_agent,
                    },
                    allow_redirects=True,
                )
            if "password" in continue_url.lower():
                password_result = self._register_password(email, password)
                self.log("ChatGPT 登录密码设置成功")
                password_continue_url = str(password_result.get("continue_url") or "").strip()
                if password_continue_url:
                    self.session.get(
                        urljoin(OPENAI_AUTH, password_continue_url),
                        headers={
                            "referer": f"{OPENAI_AUTH}/create-account/password",
                            "user-agent": self.user_agent,
                        },
                        allow_redirects=True,
                    )
            name, birthdate = _random_profile()
            created = self._create_account(name, birthdate)
            self.log("ChatGPT 账号资料创建成功")
            callback_url = str(created.get("continue_url") or "").strip()
            if callback_url:
                self.session.get(
                    urljoin(OPENAI_AUTH, callback_url),
                    headers={"user-agent": self.user_agent},
                    allow_redirects=True,
                )
            self.log("注册流程完成，开始设置 TOTP+密码")
            security_info = self._setup_password_and_totp_protocol(
                email=email,
                password=password,
                excluded_codes=registration_email_codes,
            )
            result = self._session_result(email, password)
            result.update(
                {
                    "password_set": bool(security_info.get("password_set")),
                    "password_path": security_info.get("password_path", ""),
                    "totp_set": bool(security_info.get("totp_set")),
                    "totp_secret": security_info.get("totp_secret", ""),
                    "otpauth": security_info.get("otpauth", ""),
                }
            )
            self.log("ChatGPT 协议注册完成并已获取 session")
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception:
                pass
            try:
                self.session.close()
            except Exception:
                pass
