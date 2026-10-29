# 登录挑战运行时

改编自 [toSub2 v1.7.1](https://github.com/poxiao33/toSub2/tree/8548397e89bf80e508eda64a87e0d556d43abc84/src/cloudflare-ctf)。
许可见项目根目录 `THIRD_PARTY_NOTICES.md`。

在本目录执行 `npm ci --omit=dev`。本地启动脚本和 Docker 构建会安装依赖。
需要 Node.js 20 或更新版本。优先使用项目自带的 Node，其次使用 Playwright/Patchright 的驱动 Node 和系统 Node。
可通过 `CHATGPT_LOGIN_NODE_PATH` 指定路径。

只有 ChatGPT 重新登录会启用该处理链。挑战、验证和原请求重放共用原 HTTP Session，
挑战期间复用 Cookie 和出口。适配保留 v1.7.1 对 clearance 域名、路径、过期时间及新旧值的检查，
增加 Windows 管道读取、临时文件清理、子进程隐藏、任务取消和总超时。

支持的挑战会尝试执行一次，随后最多在原代理重试三次。仍受拦截时，可轮换 sid 的代理重新开启完整登录流程，
固定代理返回明确风控错误。挑战算法发生变化时仍可能失败。

登录顺序为：首页 → providers → CSRF（检查 Cookie）→ signin/openai → OAuth 实际落地页。
密码页、邮箱验证页和已有会话分别继续对应步骤；仅在邮箱输入页提交 authorize/continue。
该接口返回 invalid_state 时只重建一次，重新判断落地页并更新邮箱基线。邮箱验证页已发码时不自动 resend。
密码和 MFA 验证使用 password_verify Sentinel，邮箱验证使用 email_otp_validate；回调只消费一次。
auth API 的 POST 每次生成新的 access-flow invocation ID，挑战重放保留原 ID。
最终 Bearer Token 校验仍新建独立会话，并复用登录指纹及挑战处理；不导入登录 Cookie。
