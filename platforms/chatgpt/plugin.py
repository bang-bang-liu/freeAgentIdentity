"""ChatGPT / Codex CLI 平台插件"""
import secrets
from urllib.parse import parse_qs, urlparse

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register
from core.proxy_pool import proxy_pool


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def _security_setup_enabled(extra: dict) -> bool:
    """Read the post-registration 2FA+password switch safely.

    Older callers do not send the field, so the historical behavior remains
    enabled by default.  String values are accepted for API/legacy callers
    that serialize booleans as text.
    """
    raw = (extra or {}).get("enable_2fa_password", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def _bool_param(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _mask_email(value: str) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return "***"
    local, domain = text.split("@", 1)
    local_masked = "*" * len(local) if len(local) <= 2 else f"{local[:1]}***{local[-1:]}"
    domain_masked = f"{domain[:1]}***{domain[-1:]}" if len(domain) > 1 else "*"
    return f"{local_masked}@{domain_masked}"


def _extract_totp_secret(extra: dict) -> str:
    """Read the persisted TOTP secret without requiring one legacy key name."""

    candidates = [
        extra.get("totp_secret"),
        extra.get("totp"),
        extra.get("mfa_secret"),
        extra.get("two_factor"),
        extra.get("otpauth"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("secret") or value.get("totp_secret") or value.get("otpauth_uri")
        text = str(value or "").strip()
        if text.lower().startswith("otpauth://"):
            text = str(parse_qs(urlparse(text).query).get("secret", [""])[0] or "")
        text = text.replace(" ", "").strip()
        if text:
            return text.upper()
    return ""


def _redact_rebind_message(message: str, *secrets_to_hide: str) -> str:
    text = str(message or "")
    for secret in secrets_to_hide:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "[已脱敏]")
    return text


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []

    # Declarative capabilities
    capabilities = [
        "query_state",      # Query account state/quota
        "check_trial",      # Query Plus trial eligibility
        "upload_cpa",       # Upload to CPA system
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        runtime_extra = dict((self.config.extra if self.config else {}) or {})
        include_trial = _bool_param(runtime_extra.get("check_trial"), True)
        check_chain = _bool_param(runtime_extra.get("check_checkout_chain"), True)
        try:
            from platforms.chatgpt.subscription import (
                fetch_plus_trial_checkout_chain,
                fetch_subscription_status_details,
            )
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra
            a.account_id = getattr(account, "user_id", "") or extra.get("account_id", "")

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            a.region = region
            configured_proxy = self.config.proxy if self.config else None
            proxy_candidates: list[tuple[str | None, str, bool]] = []
            if configured_proxy:
                proxy_candidates.append((configured_proxy, "explicit_proxy", False))
            else:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, "project_proxy", True))
                proxy_candidates.append((None, "direct", False))

            last_error = ""
            last_network_path = "direct"
            for proxy, network_path, should_report in proxy_candidates:
                try:
                    try:
                        details = fetch_subscription_status_details(
                            a,
                            proxy=proxy,
                            include_trial=include_trial,
                        )
                    except TypeError as exc:
                        # Keep compatibility with protocol/test seams that
                        # still expose the original two-argument callable.
                        if "include_trial" not in str(exc):
                            raise
                        details = fetch_subscription_status_details(a, proxy=proxy)
                    if not include_trial:
                        details = {
                            key: value
                            for key, value in (details or {}).items()
                            if not str(key).startswith("plus_trial")
                        }
                    if should_report and proxy:
                        proxy_pool.report_success(proxy)
                    status = details.get("status")
                    # 把订阅状态同步映射成前端能用的 plan_state / chips
                    # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
                    if status == "plus":
                        plan_state = "subscribed"
                        chips = ["Plus"]
                    elif status == "team":
                        plan_state = "subscribed"
                        chips = ["Team"]
                    elif status == "free":
                        plan_state = "free"
                        chips = ["Free"]
                    elif status in ("expired", "invalid", "banned"):
                        plan_state = "expired"
                        chips = []
                    else:
                        plan_state = "unknown"
                        chips = []
                    previous_overview = extra.get("account_overview") if isinstance(extra, dict) else {}
                    if isinstance(previous_overview, dict):
                        previous_chips = {
                            str(chip or "").strip().lower()
                            for chip in previous_overview.get("chips") or []
                        }
                        gcash_known = (
                            previous_overview.get("gcash_eligible") is True
                            or previous_overview.get("gcash_qualified") is True
                            or previous_overview.get("gcash_available") is True
                            or "gcash" in previous_chips
                        )
                        if gcash_known:
                            chips.append("GCash")
                        if previous_overview.get("plus_trial_eligible") is True or "带plus试用" in previous_chips:
                            chips.append("带Plus试用")
                    overview = {
                        "plan": status,
                        "plan_name": status,
                        "plan_state": plan_state,
                        "chips": chips,
                        "check_source": details.get("source"),
                        "network_path": network_path,
                        "check_state": "invalid" if status in ("expired", "invalid", "banned") else "valid",
                    }
                    if "plus_trial_eligible" in details:
                        overview["plus_trial_eligible"] = details.get("plus_trial_eligible")
                    if details.get("plus_trial_check_state"):
                        overview["plus_trial_check_state"] = details.get("plus_trial_check_state")
                    if details.get("plus_trial_error"):
                        overview["plus_trial_error"] = details.get("plus_trial_error")
                    if details.get("plus_trial_eligible") is True:
                        overview["chips"].append("带Plus试用")
                    can_checkout = status not in ("expired", "invalid", "banned", None)
                    if can_checkout and check_chain:
                        # Checkout-chain detection is intentionally
                        # independent from the promotion-eligibility result.
                        # Use the promo payload only when eligibility is
                        # affirmative; a normal checkout still reveals
                        # whether the backend uses cs or oaics in other
                        # regions.
                        a.checkout_with_promo = details.get("plus_trial_eligible") is True
                        try:
                            checkout_details = fetch_plus_trial_checkout_chain(a, proxy=proxy)
                        except Exception as exc:
                            checkout_details = {
                                "plus_trial_checkout_state": "unavailable",
                                "plus_trial_checkout_error": f"checkout_check_error: {exc}",
                            }
                        if not isinstance(checkout_details, dict):
                            checkout_details = {
                                "plus_trial_checkout_state": "unavailable",
                                "plus_trial_checkout_error": "checkout check returned an invalid result",
                            }
                        overview["plus_trial_checkout_chain"] = checkout_details.get("plus_trial_checkout_chain")
                        overview["plus_trial_checkout_state"] = checkout_details.get(
                            "plus_trial_checkout_state",
                            "unavailable",
                        )
                        overview["plus_trial_checkout_error"] = checkout_details.get("plus_trial_checkout_error")
                    elif check_chain:
                        overview["plus_trial_checkout_chain"] = None
                        overview["plus_trial_checkout_state"] = "unavailable"
                        overview["plus_trial_checkout_error"] = "account status is not eligible for checkout detection"
                    if isinstance(details.get("usage"), dict):
                        overview["chatgpt_usage"] = details["usage"]
                    self._last_check_overview = overview
                    return status not in ("expired", "invalid", "banned", None)
                except Exception as exc:
                    last_error = str(exc)
                    last_network_path = network_path
                    if should_report and proxy:
                        proxy_pool.report_fail(proxy)
                    continue
        except Exception:
            last_error = "Unable to initialize ChatGPT state check"
            last_network_path = "direct"
        self._last_check_overview = {
            "check_state": "unavailable",
            "check_error": last_error or "State check did not reach ChatGPT",
            "network_path": last_network_path,
            "chips": ["检测失败"],
        }
        if include_trial:
            self._last_check_overview.update({
                "plus_trial_eligible": None,
                "plus_trial_check_state": "unavailable",
                "plus_trial_error": last_error or "State check did not reach ChatGPT",
            })
        if check_chain:
            self._last_check_overview.update({
                "plus_trial_checkout_chain": None,
                "plus_trial_checkout_state": "unavailable",
                "plus_trial_checkout_error": last_error or "State check did not reach ChatGPT",
            })
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
    ) -> RegistrationResult:
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": result.get("account_id", ""),
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "profile": result.get("profile", {}),
                "expires_at": result.get("expires_at", ""),
                "password_set": bool(result.get("password_set")),
                "password_path": result.get("password_path", ""),
                "totp_set": bool(result.get("totp_set")),
                "totp_secret": result.get("totp_secret", ""),
                "otpauth": result.get("otpauth", ""),
                "security_setup_ok": result.get("security_setup_ok", True),
                "security_setup_error": result.get("security_setup_error", ""),
                "token_verified": result.get("token_verified", False),
                "login_required": result.get("login_required", False),
                "credential_error_code": result.get("credential_error_code", ""),
                "credential_error": result.get("credential_error", ""),
                "account_overview": result.get("account_overview", {}),
            },
        )

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

            worker = ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
                cancel_check=ctx.platform.is_cancel_requested,
                setup_security=_security_setup_enabled(ctx.extra),
            )
            register_cancel = (ctx.extra or {}).get("_register_cancel_callback")
            if callable(register_cancel):
                register_cancel(worker.cancel)
                worker.register_cancel_callback = register_cancel
            return worker

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=600),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_protocol_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

            worker = ChatGPTProtocolRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
                setup_security=_security_setup_enabled(ctx.extra),
            )
            register_cancel = (ctx.extra or {}).get("_register_cancel_callback")
            if callable(register_cancel):
                register_cancel(worker.close)
                worker.register_cancel_callback = register_cancel
            return worker

        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                password=ctx.password or "",
            ),
            worker_builder=_build_protocol_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                # ChatGPT's current OTP emails use subjects such as
                # "Your temporary ChatGPT login code" and do not always
                # contain the literal "OpenAI".  The mailbox provider already
                # filters stale messages and extracts a six-digit code, so a
                # sender/brand keyword here only causes valid messages to be
                # discarded.
                keyword="",
                wait_message="等待 Outlook 验证码...",
                timeout=180,
            ),
        )

    def get_platform_actions(self) -> list:
        return [
            {"id": "query_state", "label": "查询账号状态/订阅", "params": [
                {
                    "key": "proxy",
                    "label": "查询代理",
                    "type": "proxy",
                    "options": [
                        "http://127.0.0.1:7897",
                        "http://127.0.0.1:7890",
                    ],
                    "placeholder": "http://proxy.example.com:8080",
                    "required": True,
                },
            ]},
            {"id": "check_trial", "label": "查询试用资格", "params": [
                {
                    "key": "country",
                    "label": "查询国家/地区",
                    "type": "country",
                    "options": ["PH", "JP", "GB", "US", "TH", "VN", "BR", "CA", "AU", "DE", "FR", "SG"],
                    "required": True,
                },
                {
                    "key": "proxy",
                    "label": "查询代理",
                    "type": "proxy",
                    "options": [
                        "http://127.0.0.1:7897",
                        "http://127.0.0.1:7890",
                    ],
                    "placeholder": "http://proxy.example.com:8080",
                    "required": True,
                },
                {
                    "key": "check_chain",
                    "label": "同时检测 cs/oaics Checkout 链路",
                    "type": "checkbox",
                    "required": False,
                },
            ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "rebind_email", "label": "换绑邮箱", "params": [
                {
                    "key": "new_email",
                    "label": "新邮箱",
                    "type": "text",
                    "placeholder": "new@example.com",
                    "required": True,
                },
                {
                    "key": "mail_api",
                    "label": "新邮箱收信 API",
                    "type": "text",
                    "placeholder": "https://mail-api.example.com/messages",
                    "required": True,
                },
                {
                    "key": "proxy",
                    "label": "换绑代理",
                    "type": "proxy",
                    "options": [
                        "http://127.0.0.1:7897",
                        "http://127.0.0.1:7890",
                    ],
                    "placeholder": "http://proxy.example.com:8080",
                    "required": True,
                },
            ]},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        # Keep tasks created by older frontends compatible with the query action ID.
        aliases = {
            "get_account_state": "query_state",
        }
        normalized_action = aliases.get(action_id, action_id)
        if normalized_action == "check_trial":
            return self._handle_check_trial(account, params)
        if normalized_action == "rebind_email":
            return self._handle_rebind_email(account, params)
        return super().execute_action(normalized_action, account, params)

    def _handle_rebind_email(self, account: Account, params: dict) -> dict:
        from rebind_core.pipeline import run_rebind_email

        params = dict(params or {})
        new_email = str(params.get("new_email") or "").strip()
        mail_api = str(params.get("mail_api") or "").strip()
        if not new_email:
            raise ValueError("换绑需要填写新邮箱")
        if not mail_api:
            raise ValueError("换绑需要填写新邮箱收信 API")

        extra = dict(account.extra or {})
        totp_secret = _extract_totp_secret(extra)
        if not totp_secret:
            raise ValueError("账号缺少 TOTP 密钥，无法执行换绑")
        password = str(account.password or "")
        if not password:
            raise ValueError("账号缺少密码，无法执行换绑")

        proxy = str(params.get("proxy") or "").strip() or "http://127.0.0.1:7897"
        try:
            mail_timeout = float(params.get("mail_timeout") or 120)
        except (TypeError, ValueError):
            mail_timeout = 120.0
        mail_timeout = min(600.0, max(15.0, mail_timeout))

        result = run_rebind_email(
            old_email=account.email,
            password=password,
            totp_secret=totp_secret,
            new_email=new_email,
            mail_api=mail_api,
            proxy=proxy,
            mail_timeout=mail_timeout,
            log_fn=self.log,
            cancel_check=self.is_cancel_requested,
            # The backend persists the new session directly into the account
            # graph. Never write a plaintext bundle into the workspace.
            write_artifacts=False,
        )
        public_data = {
            "message": (
                f"换绑成功：{_mask_email(account.email)} → {_mask_email(new_email)}"
                if result.ok
                else f"换绑失败：{result.code}"
            ),
            "old_email": _mask_email(account.email),
            "new_email": _mask_email(new_email),
            "access_token_updated": bool(result.access_token),
            "session_token_updated": bool(result.session_token),
        }
        if not result.ok:
            error = _redact_rebind_message(
                result.message,
                password,
                totp_secret,
                mail_api,
                proxy,
                account.email,
                new_email,
            )
            return {"ok": False, "data": public_data, "error": f"{result.code}: {error}"}

        credential_updates = {
            key: value
            for key, value in {
                "access_token": result.access_token,
                "session_token": result.session_token,
                "id_token": result.id_token,
                "refresh_token": result.refresh_token,
                "cookies": result.cookie_header,
                "auth_session_json": result.auth_session_json,
                "device_id": result.device_id,
            }.items()
            if value
        }
        public_data["_credential_updates"] = credential_updates
        public_data["_account_updates"] = {"email": new_email}
        return {"ok": True, "data": public_data}

    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Handle ChatGPT-specific actions."""
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        from .constants import OAUTH_CLIENT_ID
        a.client_id = extra.get("client_id", OAUTH_CLIENT_ID)
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id or ""
        a.account_id = account.user_id or ""

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"Unknown action: {action_id}")

    def _handle_check_trial(self, account: Account, params: dict) -> dict:
        from platforms.chatgpt.subscription import CHECKOUT_CURRENCY_BY_COUNTRY, check_plus_trial

        extra = account.extra or {}
        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.cookies = extra.get("cookies", "")
        a.id_token = extra.get("id_token", "")
        a.extra = extra
        a.account_id = account.user_id or extra.get("account_id", "")
        a.region = account.region or extra.get("region", "")
        proxy = str((params or {}).get("proxy") or "").strip() or None
        if not proxy:
            raise ValueError("查询试用需要填写代理")
        country = str((params or {}).get("country") or "PH").strip().upper()
        if country not in CHECKOUT_CURRENCY_BY_COUNTRY:
            raise ValueError("国家/地区代码不支持")
        check_chain = _bool_param((params or {}).get("check_chain"), False)
        details = check_plus_trial(
            a,
            proxy=proxy,
            country=country,
            check_chain=check_chain,
        )
        return {"ok": True, "data": details}

    # Override specific capability handlers
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Handle query_state capability for ChatGPT."""
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.access_token = extra.get("access_token") or account.token
        a.session_token = extra.get("session_token", "")
        a.cookies = extra.get("cookies", "")
        a.id_token = extra.get("id_token", "")
        a.extra = extra

        from core.proxy_pool import proxy_pool
        from platforms.chatgpt.switch import fetch_chatgpt_account_state

        # The proxy entered in the action dialog applies only to this query.
        # Keep the configured/project-pool behavior as a fallback when the
        # field is left blank, and never persist the manually entered value.
        manual_proxy = str((params or {}).get("proxy") or "").strip()
        check_chain = _bool_param(
            (params or {}).get("check_chain"),
            # Old callers did not send this switch; retain their historical
            # chain probe while the new UI sends an explicit false by default.
            True if "check_chain" not in (params or {}) else False,
        )
        # ``check_chain`` only controls the optional cs/oaics checkout probe.
        # The regular account-state query must still fetch promotion
        # eligibility; the UI always sends check_chain=false explicitly.
        include_trial = True
        configured_proxy = manual_proxy or (self.config.proxy if self.config else None)
        region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
        a.region = region
        a.account_id = account.user_id or extra.get("account_id", "")
        proxy_candidates: list[tuple[str | None, str, bool]] = []
        if configured_proxy:
            proxy_candidates.append((configured_proxy, "manual_proxy" if manual_proxy else "explicit_proxy", False))
        elif not manual_proxy:
            pooled_proxy = proxy_pool.get_next(region=region)
            if pooled_proxy:
                proxy_candidates.append((pooled_proxy, "project_proxy", True))
        # A manually supplied proxy is an explicit per-query choice. Do not
        # silently retry through the local direct connection if it fails.
        if not manual_proxy:
            proxy_candidates.append((None, "direct", False))

        data: dict = {}
        successful_proxy: str | None = None
        for proxy, network_path, should_report in proxy_candidates:
            candidate = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=a.session_token,
                cookies=a.cookies,
                proxy=proxy,
                include_trial=include_trial,
            )
            candidate["network_path"] = network_path
            if self._state_query_is_terminal(candidate):
                if should_report and proxy:
                    proxy_pool.report_success(proxy)
                data = candidate
                successful_proxy = proxy
                break
            if should_report and proxy:
                proxy_pool.report_fail(proxy)
            data = candidate
        if self._state_query_has_credential_error(data):
            # A rejected token or anonymous profile means this query could not
            # authenticate the account. Keep that separate from account validity.
            data.pop("valid", None)
            data["check_state"] = "credential_invalid"
            data["login_required"] = True
            profile_error = data.get("profile_error") or {}
            data["check_error"] = profile_error.get("error") or profile_error
        elif not self._state_query_is_terminal(data):
            # A timeout, rate limit, or connection failure says nothing about
            # account validity. A 403 can also be a WAF or network policy
            # response, so preserve the account lifecycle and make it retryable.
            data.pop("valid", None)
            data["check_state"] = "unavailable"
            data["check_error"] = data.get("profile_error") or "State check did not reach ChatGPT"
        elif data.get("valid") is False:
            data["check_state"] = "invalid"
        else:
            data["check_state"] = "valid"
            data["login_required"] = False

        if not include_trial:
            for key in ("plus_trial_eligible", "plus_trial_check_state", "plus_trial_error"):
                data.pop(key, None)

        if data.get("check_state") == "valid" and check_chain:
            from platforms.chatgpt.subscription import fetch_plus_trial_checkout_chain

            # A positive eligibility result gets the promotional payload; all
            # other valid accounts get a normal checkout request so chain
            # detection still works in regions where the campaign is absent.
            a.checkout_with_promo = data.get("plus_trial_eligible") is True
            try:
                checkout_details = fetch_plus_trial_checkout_chain(a, proxy=successful_proxy)
            except Exception as exc:
                checkout_details = {
                    "plus_trial_checkout_state": "unavailable",
                    "plus_trial_checkout_error": f"checkout_check_error: {exc}",
                }
            if not isinstance(checkout_details, dict):
                checkout_details = {
                    "plus_trial_checkout_state": "unavailable",
                    "plus_trial_checkout_error": "checkout check returned an invalid result",
                }
            data["plus_trial_checkout_chain"] = checkout_details.get("plus_trial_checkout_chain")
            data["plus_trial_checkout_state"] = checkout_details.get(
                "plus_trial_checkout_state",
                "unavailable",
            )
            data["plus_trial_checkout_error"] = checkout_details.get("plus_trial_checkout_error")
        elif check_chain:
            data["plus_trial_checkout_chain"] = None
            data["plus_trial_checkout_state"] = "unavailable"
            data["plus_trial_checkout_error"] = (
                data.get("plus_trial_error")
                or data.get("check_error")
                or "Plus trial eligibility is unavailable"
            )
        else:
            for key in (
                "plus_trial_checkout_chain",
                "plus_trial_checkout_state",
                "plus_trial_checkout_error",
            ):
                data.pop(key, None)

        return {"ok": True, "data": data}

    @staticmethod
    def _state_query_is_terminal(data: dict) -> bool:
        if data.get("valid") is True:
            return True
        return ChatGPTPlatform._state_query_has_credential_error(data)

    @staticmethod
    def _state_query_has_credential_error(data: dict) -> bool:
        profile_error = data.get("profile_error")
        if not isinstance(profile_error, dict):
            return False
        return (
            int(profile_error.get("status_code") or 0) == 401
            or profile_error.get("error_code") == "anonymous_profile"
        )

    def _handle_upload_cpa(self, account: Account, params: dict) -> dict:
        return self._execute_platform_action("upload_cpa", account, params)
