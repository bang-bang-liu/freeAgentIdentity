export function loginMethodLabel(method: string) {
  return ({
    password: '密码', password_totp: '密码 + TOTP',
    email_otp: '邮箱验证码', email_otp_totp: '邮箱验证码 + TOTP',
    manual_required: '手动邮箱验证码', existing_session: '现有认证会话', blocked: '无法登录',
  } as Record<string, string>)[method] || method || '待确认'
}
