"""Structured auth failures without response bodies or credentials."""
from __future__ import annotations

import re


def _identifier(value: object) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", text) else ""


class AuthFlowError(RuntimeError):
    def __init__(self, stage: str, *, status_code: int, error_code: str = "", request_id: str = ""):
        self.status_code = status_code
        self.error_code = _identifier(error_code)
        self.request_id = _identifier(request_id)
        detail = f"{stage}失败：HTTP {status_code}"
        if self.error_code:
            detail += f" ({self.error_code})"
        if self.request_id:
            detail += f" req_id={self.request_id}"
        super().__init__(detail)

    @classmethod
    def from_response(cls, stage: str, response) -> AuthFlowError:
        error_code = ""
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                error_code = error.get("code") or ""
        except Exception:
            pass
        headers = response.headers
        body = str(response.text or "")[:4096].lower()
        if (str(headers.get("cf-mitigated", "")).lower() == "challenge"
                or "<title>just a moment" in body or "cf-chl-" in body):
            error_code = "cloudflare_challenge"
        return cls(
            stage,
            status_code=response.status_code,
            error_code=error_code,
            request_id=headers.get("x-request-id", ""),
        )
