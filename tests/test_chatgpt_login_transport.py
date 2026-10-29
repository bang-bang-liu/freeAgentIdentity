"""挑战、重放、重定向和终止行为均使用本地响应，不访问外部服务。"""
from http.cookiejar import Cookie, CookieJar
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode
from registration_core.auth_errors import AuthFlowError
from registration_core.cloudflare import solver
from registration_core.http_client import browser_profiles_for_flow
from registration_core.login_transport import LoginChallengeSession, is_cloudflare_challenge, resolve_node_binary, rotate_proxy_session


class Response:
    def __init__(self, status=200, text="", *, url="https://auth.openai.com/log-in/password", headers=None):
        self.status_code, self.text, self.url = status, text, url
        self.content = text.encode()
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def json(self):
        return json.loads(self.text)


def challenge(**kwargs):
    return Response(403, '<html><script>window._cf_chl_opt={};</script></html>',
                    headers={"cf-mitigated": "challenge", "content-type": "text/html"}, **kwargs)


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = SimpleNamespace(jar=CookieJar())
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def wrap(session, **kwargs):
    return LoginChallengeSession(session, browser_profiles_for_flow()[0],
                                 log=kwargs.pop("log", lambda _msg: None),
                                 check_budget=kwargs.pop("check_budget", lambda: None),
                                 deadline=kwargs.pop("deadline", time.monotonic() + 30),
                                 retry_delay=0, **kwargs)


def test_redirect_challenge_replays_only_current_hop_with_same_session():
    initial = "https://auth.openai.com/oauth/authorize?state=private"
    landing = "https://auth.openai.com/log-in/password"
    raw = Session([Response(302, url=initial, headers={"location": landing}), challenge(), Response()])
    solved = []

    def complete(session, **kwargs):
        solved.append((session, kwargs))
        return {"ok": True}

    response = wrap(raw, solver=complete).get(initial, headers={"Referer": "https://chatgpt.com/"})
    assert response.status_code == 200
    assert [call[1] for call in raw.calls] == [initial, landing, landing]
    assert solved[0][0] is raw
    assert solved[0][1]["browser_identity"]["userAgent"] == solved[0][1]["user_agent"]
    assert all(call[2]["allow_redirects"] is False for call in raw.calls)


def test_post_replay_preserves_body_and_does_not_reconfigure_proxy():
    raw = Session([challenge(), Response(text='{"ok":true}')])
    client = wrap(raw, solver=lambda *_args, **_kwargs: {"ok": True})
    client.post("https://chatgpt.com/api/auth/signin/openai", data={"csrfToken": "private"}, headers={"Origin": "https://chatgpt.com"})
    assert [call[:2] for call in raw.calls] == [("POST", "https://chatgpt.com/api/auth/signin/openai")] * 2
    assert all(call[2]["data"] == {"csrfToken": "private"} for call in raw.calls)
    assert raw.calls[0][2]["headers"] == raw.calls[1][2]["headers"]
    assert 0 < raw.calls[1][2]["timeout"] <= raw.calls[0][2]["timeout"] <= 30
    assert not raw.closed


def test_auth_flow_request_id_changes_between_posts_but_survives_challenge_replay():
    raw = Session([challenge(), Response(), Response()])
    client = wrap(raw, solver=lambda *_args, **_kwargs: {"ok": True})
    url = "https://auth.openai.com/api/accounts/password/verify"
    for _ in range(2):
        client.post(url, json={"password": "private"}, headers={"x-access-flow-invocation-id": "old"})
    invocation_ids = [call[2]["headers"]["x-access-flow-invocation-id"] for call in raw.calls]
    assert invocation_ids[0] == invocation_ids[1] and invocation_ids[1] != invocation_ids[2]
    assert "old" not in invocation_ids


@pytest.mark.parametrize("status,body", [(409, '{"error":{"code":"invalid_state"}}'), (403, '{"error":{"code":"account_deactivated"}}')])
def test_business_errors_do_not_trigger_solver_or_retry(status, body):
    response = Response(status, body, headers={"content-type": "application/json"})
    raw = Session([response])
    assert wrap(raw, solver=lambda *_args, **_kwargs: pytest.fail("business error treated as challenge")).post("https://auth.openai.com/api/accounts/password/verify") is response
    assert len(raw.calls) == 1 and not is_cloudflare_challenge(response)


def test_solver_failure_retries_are_bounded_and_logs_do_not_expose_exception():
    raw = Session([challenge() for _ in range(4)])
    logs, solves = [], []

    def fail(*_args, **_kwargs):
        solves.append(True)
        raise RuntimeError("private-token")

    with pytest.raises(AuthFlowError) as exc:
        wrap(raw, solver=fail, log=logs.append).get("https://auth.openai.com/log-in")
    assert exc.value.error_code == "cloudflare_challenge"
    assert len(raw.calls) == 4 and len(solves) == 1
    assert "private-token" not in " ".join(logs) + str(exc.value)


def test_cancellation_during_solver_stops_without_replay():
    raw = Session([challenge()])
    cancelled = False

    def check():
        if cancelled:
            raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

    def cancel(*_args, **kwargs):
        nonlocal cancelled
        cancelled = True
        kwargs["check_budget"]()

    with pytest.raises(ChatGPTLoginError) as exc:
        wrap(raw, solver=cancel, check_budget=check).get("https://auth.openai.com/log-in")
    assert exc.value.code == LoginErrorCode.CANCELLED and len(raw.calls) == 1


def test_closed_or_expired_session_cannot_send_more_requests():
    raw = Session([])
    client = wrap(raw)
    client.close()
    with pytest.raises(TimeoutError):
        client.get("https://chatgpt.com/")
    with pytest.raises(TimeoutError):
        wrap(raw, deadline=time.monotonic() - 1).get("https://chatgpt.com/")
    assert not raw.calls and raw.closed


def add_cookie(raw, value, *, domain="auth.openai.com", path="/", expires=None):
    raw.cookies.jar.set_cookie(Cookie(0, "cf_clearance", value, None, False,
                                     domain, True, domain.startswith("."), path, True, True,
                                     expires, False, None, None, {}))


def test_clearance_requires_matching_domain_path_and_fresh_value():
    raw = Session([])
    add_cookie(raw, "wrong-domain", domain="chatgpt.com")
    add_cookie(raw, "wrong-path", path="/other")
    add_cookie(raw, "expired", path="/log-in", expires=time.time() - 10)
    add_cookie(raw, "usable")
    before = solver.clearance_values(raw, "https://auth.openai.com/log-in/password")
    assert before == {"usable"}
    add_cookie(raw, "fresh")
    assert solver.clearance_values(raw, "https://auth.openai.com/log-in/password") - before == {"fresh"}


def test_proxy_rotation_changes_only_supported_session_fields():
    assert rotate_proxy_session("http://127.0.0.1:7897") is None
    proxy = "http://user-sid-old-t-20:password@proxy.example:8080"
    rotated = rotate_proxy_session(proxy)
    assert rotated != proxy and rotated.startswith("http://user-sid-") and rotated.endswith("-t-20:password@proxy.example:8080")


def test_node_runtime_uses_real_pipes_and_cleans_up_on_cancel(monkeypatch):
    # 真正执行父挑战脚本，覆盖 Windows 无法 select 管道及临时文件重开问题。
    tracked = []
    original = solver.start_runtime
    started = time.monotonic()

    def start(*args, **kwargs):
        process, path = original(*args, **kwargs)
        tracked.append((process, path))
        return process, path

    def check():
        if tracked and time.monotonic() - started > 0.8:
            raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

    monkeypatch.setattr(solver, "start_runtime", start)
    with pytest.raises(ChatGPTLoginError):
        solver.solve_challenge(Session([]), challenge_url="https://auth.openai.com/log-in",
                               challenge_html="<script>window._cf_chl_opt={};</script>",
                               challenge_size=50, user_agent="test", node_command=resolve_node_binary(),
                               timeout_seconds=5, check_budget=check)
    assert tracked and all(process.poll() is not None and not path.exists() for process, path in tracked)


def test_node_runtime_completes_local_challenge_and_verifies_new_clearance():
    class ChallengeSession(Session):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if "/cdn-cgi/" in url:
                add_cookie(self, "new-clearance")
            return Response(text="ok", url=url)

    raw = ChallengeSession([])
    html = '<script>window._cf_chl_opt={};fetch("https://auth.openai.com/cdn-cgi/challenge-platform/solve",{method:"POST",body:"proof"});</script>'
    result = solver.solve_challenge(raw, challenge_url="https://auth.openai.com/log-in",
                                    challenge_html=html, challenge_size=len(html), user_agent="test",
                                    node_command=resolve_node_binary(), timeout_seconds=5)
    assert result["ok"] and result["clearance"]
    assert [call[0] for call in raw.calls] == ["POST", "GET"]
