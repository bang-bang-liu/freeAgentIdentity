from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .change_email import ChangeEmailClient, ChangeEmailError, password_reauth
from .mail_inbox import wait_code
from .mfa_login import LoginSession, MfaLoginError, login_with_password_and_totp
from .paths import ROOT
from .session_export import build_login_bundle, write_login_bundle


class RebindError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class RebindResult:
    ok: bool
    code: str = "OK"
    message: str = ""
    old_email: str = ""
    new_email: str = ""
    bundle_path: str = ""
    access_token_masked: str = ""
    session_email: str = ""
    access_token: str = ""
    session_token: str = ""
    id_token: str = ""
    refresh_token: str = ""
    cookie_header: str = ""
    auth_session_json: str = ""
    device_id: str = ""
    account_id: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    run_dir: str = ""


def _mask(value: str, head: int = 12, tail: int = 6) -> str:
    text = str(value or "")
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _mask_email(value: str) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return _mask(text, 2, 2)
    local, domain = text.split("@", 1)
    if len(local) <= 2:
        local_masked = "*" * len(local)
    else:
        local_masked = f"{local[:1]}***{local[-1:]}"
    return f"{local_masked}@{domain[:1]}***{domain[-1:] if len(domain) > 1 else '*'}"


def _log(
    trace: list[dict[str, Any]],
    step: str,
    *,
    log_fn: Callable[[str], None] | None = None,
    **payload: Any,
) -> None:
    item = {"time": datetime.now().isoformat(timespec="seconds"), "step": step, **payload}
    # scrub
    for k in list(item.keys()):
        lk = k.lower()
        if any(x in lk for x in ("password", "totp", "secret", "cookie", "token")) and k not in {
            "access_token_masked",
            "has_at",
            "has_session",
        }:
            if isinstance(item[k], str) and len(item[k]) > 12:
                item[k] = _mask(item[k])
    trace.append(item)
    message = f"[{step}] " + ", ".join(
        f"{k}={v}" for k, v in item.items() if k not in {"time", "step"}
    )
    if callable(log_fn):
        log_fn(message)
    else:
        print(message)


def run_rebind_email(
    *,
    old_email: str,
    password: str,
    totp_secret: str,
    new_email: str,
    mail_api: str,
    proxy: str | None = None,
    out_dir: str | Path | None = None,
    mail_timeout: float = 120.0,
    log_fn: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    write_artifacts: bool = True,
) -> RebindResult:
    old_email = (old_email or "").strip()
    new_email = (new_email or "").strip()
    password = (password or "").strip()
    totp_secret = (totp_secret or "").strip()
    mail_api = (mail_api or "").strip()
    trace: list[dict[str, Any]] = []
    run_dir = ROOT / "outputs" / "rebind_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    if write_artifacts:
        run_dir.mkdir(parents=True, exist_ok=True)

    sensitive_values = [value for value in (password, totp_secret, mail_api, proxy) if value]

    def _redact(text: str) -> str:
        result = str(text or "")
        for email in (old_email, new_email):
            if email:
                result = result.replace(email, _mask_email(email))
        for value in sensitive_values:
            result = result.replace(value, "[已脱敏]")
        return result

    def _emit(message: str) -> None:
        if callable(log_fn):
            log_fn(_redact(message))
        else:
            print(_redact(message))

    def _check_cancelled() -> None:
        if callable(cancel_check) and cancel_check():
            raise RebindError("CANCELLED", "任务已取消")

    def _stage(message: str) -> None:
        _emit(message)

    def _fail(code: str, message: str) -> RebindResult:
        message = _redact(message)
        _log(trace, "failed", log_fn=_emit, code=code, message=message)
        if write_artifacts:
            (run_dir / "trace.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return RebindResult(
            ok=False,
            code=code,
            message=message,
            old_email=old_email,
            new_email=new_email,
            trace=trace,
            run_dir=str(run_dir) if write_artifacts else "",
        )

    try:
        _check_cancelled()
        _stage("[1/6] 旧邮箱账密+TOTP 登录 ...")
        login1 = login_with_password_and_totp(
            old_email, password, totp_secret, proxy=proxy
        )
        _log(
            trace,
            "login_old",
            log_fn=_emit,
            email=_mask_email(old_email),
            account_id=login1.account_id,
            at=_mask(login1.access_token),
            factor_id=login1.factor_id,
        )

        _check_cancelled()
        _stage("[2/6] 检查 change_email eligibility ...")
        client = ChangeEmailClient(login=login1)
        try:
            elig = client.eligibility()
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "eligibility", log_fn=_emit, **{k: elig.get(k) for k in ("eligible", "eligibility_type")})

        _check_cancelled()
        _stage("[3/6] begin 发送新邮箱验证码 ...")
        issued_after = time.time()
        try:
            begin_resp = client.begin(new_email)
        except ChangeEmailError as exc:
            if exc.code == "REAUTH_FAILED":
                _log(trace, "begin_need_reauth", log_fn=_emit, message=exc.message)
                _stage("begin 要求 reauth，执行 password+MFA 再试 ...")
                try:
                    password_reauth(login1)
                    # refresh client tokens
                    client = ChangeEmailClient(login=login1, session_id=str(uuid.uuid4()))
                    begin_resp = client.begin(new_email)
                except Exception as exc2:
                    return _fail("REAUTH_FAILED", str(exc2))
            else:
                return _fail(exc.code, exc.message)
        _log(trace, "begin", log_fn=_emit, new_email=_mask_email(new_email), resp_keys=list(begin_resp.keys())[:10] if isinstance(begin_resp, dict) else [])

        _check_cancelled()
        _stage("[4/6] 等待新邮箱验证码 ...")
        try:
            code = wait_code(
                mail_api,
                issued_after=issued_after - 5,
                timeout=mail_timeout,
                cancel_check=cancel_check,
            )
        except TimeoutError as exc:
            return _fail("MAIL_TIMEOUT", str(exc))
        _log(trace, "mail_code", log_fn=_emit, code_tail=code[-2:])

        _check_cancelled()
        _stage("[5/6] verify 换绑 ...")
        try:
            verify_resp = client.verify(new_email, code)
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "verify", log_fn=_emit, resp_keys=list(verify_resp.keys())[:10] if isinstance(verify_resp, dict) else [])

        _check_cancelled()
        _stage("[6/6] 新邮箱账密+TOTP 重登并导出 ...")
        # 主动重建登录会话
        try:
            login2 = login_with_password_and_totp(
                new_email, password, totp_secret, proxy=proxy
            )
        except MfaLoginError as exc:
            return _fail(exc.code if exc.code in {"LOGIN_FAILED", "MFA_FAILED"} else "RELOGIN_FAILED", exc.message)

        bundle = build_login_bundle(login2, rebind_email=new_email)
        session_email = str(bundle.get("email") or "")
        if session_email and session_email.lower() != new_email.lower():
            return _fail(
                "RELOGIN_FAILED",
                f"重登后 email 不匹配: got={session_email} expected={new_email}",
            )
        bundle_path = ""
        if write_artifacts:
            paths = write_login_bundle(bundle, out_dir=out_dir)
            bundle_path = str(paths["bundle"])
        _log(
            trace,
            "export",
            log_fn=_emit,
            bundle=bundle_path or "disabled",
            email=_mask_email(session_email),
            at=_mask(login2.access_token),
        )
        if write_artifacts:
            (run_dir / "trace.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (run_dir / "login_bundle.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return RebindResult(
            ok=True,
            code="OK",
            message="rebind success",
            old_email=old_email,
            new_email=new_email,
            bundle_path=bundle_path,
            access_token_masked=_mask(login2.access_token),
            session_email=session_email,
            access_token=str(login2.access_token or ""),
            session_token=str(login2.session_token or ""),
            id_token=str(login2.result.id_token or ""),
            refresh_token=str(login2.result.refresh_token or ""),
            cookie_header=str(login2.cookie_header or ""),
            auth_session_json=str(login2.result.auth_session_json or ""),
            device_id=str(login2.device_id or ""),
            account_id=str(login2.account_id or ""),
            trace=trace,
            run_dir=str(run_dir) if write_artifacts else "",
        )
    except MfaLoginError as exc:
        return _fail(exc.code, exc.message)
    except ChangeEmailError as exc:
        return _fail(exc.code, exc.message)
    except RebindError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:
        return _fail("EXPORT_FAILED", str(exc))
