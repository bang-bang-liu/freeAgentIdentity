"""按缺失项幂等补齐密码和 TOTP。

设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 7.1 / 7.2 / 10.3。

语义是"确保账户具备密码和 TOTP"，不是"每次刷新都重置"：

- 已有密码：不重置。
- 已有 TOTP：不重新 enrollment，不替换密钥。
- 只缺一项：只补那一项。
- 两项都缺：先补密码，再补 TOTP（后者的 re-auth 可以用刚确认成功的新密码）。

TOTP 分支直接复用 ``registration_core.mfa_totp_protocol.enable_totp_for_token``，
它本身已经幂等：先查 ``mfa_info()``，已启用就返回 ``already_enabled`` 且不
enroll（``mfa_totp_protocol.py:339-356``）。

密码分支通过 ``password_setter`` 注入。现有的 ``_add_password_protocol`` 与
ChatGPT 注册类耦合（``self.sentinel`` / ``self._follow_security_url``），把它
抽成独立函数属于 Phase 2 的接线工作，这里只固定契约和判定顺序。
"""
from __future__ import annotations

from typing import Any, Callable

from platforms.chatgpt.login_types import (
    ChatGPTLoginResult,
    LoginErrorCode,
    SecuritySetupResult,
    SecuritySetupStatus,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def generate_password(length: int = 20) -> str:
    """生成满足平台策略的随机密码（大小写+数字+符号，16-24 位）。

    只在内存中流转，服务端确认成功后才写入账户安全存储（设计 7.1）。
    """
    import secrets
    import string

    size = min(max(int(length or 20), 16), 24)
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(size))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*-_=+" for c in candidate)
        ):
            return candidate


def plan_security_setup(*, has_password: bool, has_totp: bool) -> str:
    """返回执行计划标识，供预检展示（设计 9.2 的 ``security_setup_plan``）。"""
    if has_password and has_totp:
        return "skip"
    if has_password:
        return "add_totp"
    if has_totp:
        return "add_password"
    return "add_password_then_totp"


def ensure_security(
    *,
    email: str,
    session: ChatGPTLoginResult,
    has_password: bool,
    has_totp: bool,
    proxy: str = "",
    password_setter: Callable[..., dict] | None = None,
    totp_enabler: Callable[..., dict] | None = None,
    log: Callable[[str], None] | None = None,
) -> SecuritySetupResult:
    """只补齐缺失的安全项，返回结构化结果。

    ``password_setter(email=..., password=..., session=...) -> dict``
        约定返回 ``{"password_set": bool}``；抛异常视为失败。
    ``totp_enabler(access_token=..., cookie_header=..., device_id=..., proxy=...) -> dict``
        默认用 ``enable_totp_for_token``，返回含 ``secret`` / ``ok`` / ``error``。

    绝不覆盖已存在的密码或 TOTP：``has_password`` / ``has_totp`` 为真时对应分支
    直接跳过，连接口都不调。
    """
    emit = log or (lambda _message: None)
    result = SecuritySetupResult()

    if has_password and has_totp:
        result.password_set = True
        result.totp_set = True
        result.security_setup_status = SecuritySetupStatus.NOT_NEEDED
        emit("安全项均已存在，跳过设置")
        return result

    failures: list[str] = []
    completed = 0
    requested = 0

    # ---- 密码：只在缺失时补
    if has_password:
        result.password_set = True
        emit("已有密码，不重置")
    else:
        requested += 1
        setter = password_setter
        if setter is None:
            result.error_code = LoginErrorCode.SECURITY_SETUP_FAILED
            result.error_message = "未提供密码设置通道"
            failures.append("password")
            emit("跳过密码补设：未提供设置通道")
        else:
            new_password = generate_password()
            try:
                setter_result = setter(
                    email=email, password=new_password, session=session
                )
            except Exception as exc:
                failures.append("password")
                result.error_code = LoginErrorCode.SECURITY_SETUP_FAILED
                result.error_message = f"密码设置失败: {type(exc).__name__}"
                emit("密码补设失败")
            else:
                if isinstance(setter_result, dict) and setter_result.get("password_set"):
                    result.password_set = True
                    result.password_created = True
                    # 调用方负责落库；落库成功后回填 password_persisted。
                    result.new_password = new_password
                    completed += 1
                    emit("password=created")
                else:
                    failures.append("password")
                    result.error_code = LoginErrorCode.SECURITY_SETUP_FAILED
                    result.error_message = "密码设置未被服务端确认"
                    emit("密码补设未被确认")

    # ---- TOTP：只在缺失时补
    if has_totp:
        result.totp_set = True
        emit("已有 TOTP，不重新绑定")
    else:
        requested += 1
        enabler = totp_enabler
        if enabler is None:
            from registration_core.mfa_totp_protocol import enable_totp_for_token

            enabler = enable_totp_for_token
        try:
            totp_result = enabler(
                access_token=session.access_token,
                cookie_header=session.cookie_header,
                device_id=session.device_id,
                proxy=proxy or "",
            )
        except Exception as exc:
            failures.append("totp")
            result.error_code = result.error_code or LoginErrorCode.SECURITY_SETUP_FAILED
            result.error_message = result.error_message or f"TOTP 设置失败: {type(exc).__name__}"
            emit("TOTP 补设失败")
        else:
            payload = totp_result if isinstance(totp_result, dict) else {}
            secret = _text(payload.get("secret"))
            already = _text(payload.get("error")) == "already_enabled"
            if already:
                # 服务端说已启用但本地没存密钥：不算新建，也不能伪造 secret。
                result.totp_set = True
                emit("服务端报告 TOTP 已启用，本地无密钥可保存")
            elif payload.get("ok") and secret:
                # 只有 activate 成功才有 secret，未激活的 secret 不会走到这里。
                result.totp_set = True
                result.totp_created = True
                result.totp_secret = secret
                # MfaProtocolResult 里的字段名是 otpauth_uri（mfa_totp_protocol.py:76）。
                result.otpauth = _text(
                    payload.get("otpauth_uri") or payload.get("otpauth")
                )
                completed += 1
                emit("totp=created")
            else:
                failures.append("totp")
                result.error_code = result.error_code or LoginErrorCode.SECURITY_SETUP_FAILED
                result.error_message = (
                    result.error_message or "TOTP 激活未成功，未保存密钥"
                )
                emit("TOTP 未激活，不保存密钥")

    if not failures:
        result.security_setup_status = SecuritySetupStatus.COMPLETED
    elif completed > 0:
        result.security_setup_status = SecuritySetupStatus.PARTIAL
    else:
        result.security_setup_status = SecuritySetupStatus.FAILED
    emit(f"安全设置结果: {result.security_setup_status} ({completed}/{requested})")
    return result


def take_new_password(result: SecuritySetupResult) -> str:
    """取出本次新建的密码明文并从结果对象上清空（只能取一次）。

    密码只在内存中流转，调用方拿到后应立即在同一事务里落库，然后按落库结果
    回填 ``password_persisted``（设计 10.3）。
    """
    password = _text(result.new_password)
    result.new_password = ""
    return password
