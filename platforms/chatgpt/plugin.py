"""ChatGPT / Codex CLI 平台插件"""
import secrets
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
        "upload_cpa",       # Upload to CPA system
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
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
                    details = fetch_subscription_status_details(a, proxy=proxy)
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
                    if can_checkout:
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
                    else:
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
            # Explicitly clear enrichment fields so a failed refresh cannot
            # leave a stale promotion/checkout chain visible in the UI.
            "plus_trial_eligible": None,
            "plus_trial_check_state": "unavailable",
            "plus_trial_checkout_chain": None,
            "plus_trial_checkout_state": "unavailable",
            "plus_trial_checkout_error": last_error or "State check did not reach ChatGPT",
        }
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
            },
        )

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

            return ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                backend_config=(ctx.extra or {}).get("_reuse_backend_config"),
            )

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

            return ChatGPTProtocolRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
            )

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
                    "placeholder": "http://user:pass@host:port",
                    "required": True,
                },
            ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        # Keep tasks created by older frontends compatible with the query action ID.
        aliases = {
            "get_account_state": "query_state",
        }
        return super().execute_action(aliases.get(action_id, action_id), account, params)

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
            # A 401 response proves the saved bearer credential is no longer
            # usable. The account itself can still be active through a newer
            # browser session, so do not turn it into an account-invalid flag.
            data.pop("valid", None)
            data["check_state"] = "credential_invalid"
            data["check_error"] = data.get("profile_error") or "ChatGPT rejected the saved access token"
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

        if data.get("check_state") == "valid":
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
        else:
            data["plus_trial_checkout_chain"] = None
            data["plus_trial_checkout_state"] = "unavailable"
            data["plus_trial_checkout_error"] = (
                data.get("plus_trial_error")
                or data.get("check_error")
                or "Plus trial eligibility is unavailable"
            )

        return {"ok": True, "data": data}

    @staticmethod
    def _state_query_is_terminal(data: dict) -> bool:
        if data.get("valid") is True:
            return True
        profile_error = data.get("profile_error")
        if isinstance(profile_error, dict):
            return int(profile_error.get("status_code") or 0) == 401
        return False

    @staticmethod
    def _state_query_has_credential_error(data: dict) -> bool:
        profile_error = data.get("profile_error")
        if not isinstance(profile_error, dict):
            return False
        return int(profile_error.get("status_code") or 0) == 401

    def _handle_upload_cpa(self, account: Account, params: dict) -> dict:
        return self._execute_platform_action("upload_cpa", account, params)
