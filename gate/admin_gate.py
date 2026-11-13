#!/usr/bin/env python3
"""
RustDesk Web 控制台双因素（TOTP）网关 —— 开源平替 Pro 控制台 2FA。

形态：Nginx auth_request 的后端。保护 lejianwen Web Admin 的两个面：
  - /_admin/（管理后台 SPA）
  - /api/admin/（管理后台 API）
客户端个人 API（/api/login、/api/heartbeat 等）不在保护范围。

流程：
  - 未注册状态：/auth 一律放行（204），控制台行为与现状一致，不会把管理员锁在外面；
  - 管理员通过一次性注册链接（setup token 存服务器 root-only 文件）打开 /gate/enroll：
    自行设置网关口令、把页面显示的 TOTP 密钥录入验证器 App（如 YubiKey Authenticator）、
    输入第一枚动态码确认 → 注册完成，setup token 立即作废，网关转入强制模式；
  - 强制模式：/auth 仅当请求带有效签名 Cookie（rdgate，12 小时）时放行，
    否则 401，由 Nginx 跳转到 /gate/login；登录 = 网关口令 + TOTP 动态码。

仅 Python 标准库。状态存 JSON 文件（600 权限）：口令 scrypt 哈希、TOTP 密钥、Cookie 签名密钥。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = os.environ.get("GATE_STATE_DIR", "/opt/rustdesk-gate")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SETUP_TOKEN_FILE = os.path.join(STATE_DIR, "setup-token")
LISTEN_HOST = os.environ.get("GATE_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("GATE_LISTEN_PORT", "21215"))
COOKIE_NAME = "rdgate"
COOKIE_TTL = 12 * 3600

# 定制客户端页展示的服务器参数（公开信息：公钥本身就是给客户端用的）
CLIENT_PARAMS = {
    "id_server": os.environ.get("CLIENT_ID_SERVER", "23.95.243.27:21116"),
    "relay_server": os.environ.get("CLIENT_RELAY_SERVER", "23.95.243.27:21117"),
    "api_server": os.environ.get("CLIENT_API_SERVER", "https://rd.ljwc.eu.cc"),
    "key": os.environ.get("CLIENT_KEY", "AgY96DpUBIH6MvNSM56itLNqCHCZ7FWXjrWjqINCqvo="),
}
CLIENT_PACKAGE = os.environ.get(
    "CLIENT_PACKAGE", "/opt/rustdesk-gate/downloads/RustDesk-1.5.3-JingWei.exe"
)

_fails = {}  # ip -> [timestamps]


# ---------------- 状态 ----------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"enrolled": False}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def server_secret(st):
    if "cookie_secret" not in st:
        st["cookie_secret"] = secrets.token_hex(32)
        save_state(st)
    return bytes.fromhex(st["cookie_secret"])


# ---------------- TOTP (RFC 6238) ----------------
def totp_at(secret_b32, t):
    key = base64.b32decode(secret_b32, casefold=True)
    counter = int(t // 30)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def totp_verify(secret_b32, code):
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return False
    now = time.time()
    return any(
        hmac.compare_digest(totp_at(secret_b32, now + d), code) for d in (-30, 0, 30)
    )


# ---------------- 口令 ----------------
def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex(), h.hex()


def verify_password(st, pw):
    try:
        salt = bytes.fromhex(st["pw_salt"])
        expect = bytes.fromhex(st["pw_hash"])
    except Exception:
        return False
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(h, expect)


# ---------------- Cookie ----------------
def make_cookie(st):
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + COOKIE_TTL}).encode()
    ).decode()
    sig = hmac.new(server_secret(st), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_cookie(st, cookie_header):
    if not cookie_header:
        return False
    value = None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(COOKIE_NAME + "="):
            value = part[len(COOKIE_NAME) + 1:]
            break
    if not value or "." not in value:
        return False
    payload, sig = value.rsplit(".", 1)
    expect = hmac.new(server_secret(st), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return int(data["exp"]) > int(time.time())
    except Exception:
        return False


# ---------------- 限速 ----------------
def rate_limited(ip):
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = lst
    return len(lst) >= 5


def record_fail(ip):
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip):
    _fails.pop(ip, None)


# ---------------- 页面 ----------------
def page(title, body):
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1e293b;padding:32px 28px;border-radius:12px;width:min(92vw,380px);box-shadow:0 10px 30px rgba(0,0,0,.4)}}
h1{{font-size:19px;margin:0 0 6px}} p{{color:#94a3b8;font-size:13px;line-height:1.6}}
label{{display:block;font-size:13px;margin:14px 0 6px;color:#cbd5e1}}
input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:15px}}
button{{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:15px;cursor:pointer}}
.err{{background:#7f1d1d;color:#fecaca;padding:8px 10px;border-radius:8px;font-size:13px;margin-top:12px}}
.secret{{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0f172a;border:1px dashed #475569;border-radius:8px;padding:10px;word-break:break-all;font-size:14px;color:#fbbf24}}
code{{color:#93c5fd;word-break:break-all;font-size:12px}}
</style></head><body><div class="card">{body}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, to, cookie=None):
        extra = {"Location": to}
        if cookie:
            extra["Set-Cookie"] = (
                f"{COOKIE_NAME}={cookie}; Path=/; Max-Age={COOKIE_TTL}; "
                "HttpOnly; Secure; SameSite=Lax"
            )
        self._send(302, "", extra=extra)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        out = {}
        for pair in raw.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                from urllib.parse import unquote_plus
                out[unquote_plus(k)] = unquote_plus(v)
        return out

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    # ---------- /auth ----------
    def _auth(self):
        st = load_state()
        if not st.get("enrolled"):
            self._send(204, "", "text/plain")
            return
        if check_cookie(st, self.headers.get("Cookie", "")):
            self._send(204, "", "text/plain")
        else:
            self._send(401, "", "text/plain")

    # ---------- 登录 ----------
    def _login_page(self, err=""):
        e = f'<div class="err">{err}</div>' if err else ""
        return page("控制台双因素验证", f"""
        <h1>RustDesk 控制台 · 双因素验证</h1>
        <p>通过本网关后，还需再完成控制台自身的账号登录。</p>{e}
        <form method="post" action="/gate/login">
          <label>网关口令</label>
          <input type="password" name="password" autocomplete="current-password" required>
          <label>验证器动态码（6 位）</label>
          <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" required>
          <button type="submit">验证并进入控制台</button>
        </form>""")

    def _login(self):
        st = load_state()
        if not st.get("enrolled"):
            self._redirect("/gate/enroll")
            return
        ip = self._ip()
        if rate_limited(ip):
            self._send(429, self._login_page("尝试过于频繁，请 5 分钟后再试。"))
            return
        form = self._form()
        if verify_password(st, form.get("password", "")) and totp_verify(
            st["totp_secret"], form.get("code", "")
        ):
            clear_fails(ip)
            self._redirect("/_admin/", cookie=make_cookie(st))
        else:
            record_fail(ip)
            self._send(401, self._login_page("口令或动态码不正确。"))

    # ---------- 注册 ----------
    def _setup_token_ok(self, token):
        try:
            with open(SETUP_TOKEN_FILE) as f:
                expect = f.read().strip()
        except Exception:
            return False
        return bool(token) and hmac.compare_digest(token, expect)

    def _enroll_page(self, token, err=""):
        st = load_state()
        if st.get("enrolled"):
            return page("已完成注册", "<h1>双因素验证已启用</h1><p>本控制台已完成注册，无需重复操作。</p>")
        if not self._setup_token_ok(token):
            return page("链接无效", "<h1>注册链接无效或已过期</h1><p>请使用管理员提供的一次性注册链接。</p>")
        if "pending_secret" not in st:
            st["pending_secret"] = base64.b32encode(secrets.token_bytes(20)).decode()
            save_state(st)
        secret = st["pending_secret"]
        grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
        uri = f"otpauth://totp/RustDesk%20Console:admin?secret={secret}&issuer=RustDesk%20Console"
        e = f'<div class="err">{err}</div>' if err else ""
        return page("启用双因素验证", f"""
        <h1>启用控制台双因素验证</h1>
        <p>1) 设置一个网关口令（与控制台账号密码相互独立）。</p>
        <p>2) 把下面的密钥录入你的验证器 App（YubiKey Authenticator、Google Authenticator 等均可，选择“输入设置密钥”）：</p>
        <div class="secret">{grouped}</div>
        <p>或使用 otpauth 链接：<br><code>{uri}</code></p>
        <p>3) 输入验证器显示的第一枚 6 位动态码，确认启用。启用后本链接立即作废。</p>{e}
        <form method="post" action="/gate/enroll">
          <input type="hidden" name="setup" value="{token}">
          <label>设置网关口令</label>
          <input type="password" name="password" autocomplete="new-password" required>
          <label>再次输入口令</label>
          <input type="password" name="password2" autocomplete="new-password" required>
          <label>验证器动态码（6 位）</label>
          <input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" required>
          <button type="submit">启用双因素验证</button>
        </form>""")

    def _enroll(self):
        form = self._form()
        token = form.get("setup", "")
        st = load_state()
        if st.get("enrolled") or not self._setup_token_ok(token):
            self._send(403, self._enroll_page(""))
            return
        secret = st.get("pending_secret", "")
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        if len(pw) < 8 or pw != pw2:
            self._send(400, self._enroll_page(token, "口令至少 8 位，且两次输入需一致。"))
            return
        if not secret or not totp_verify(secret, form.get("code", "")):
            self._send(400, self._enroll_page(token, "动态码不正确，请确认验证器时间准确后重试。"))
            return
        salt, h = hash_password(pw)
        st.update({
            "enrolled": True,
            "pw_salt": salt,
            "pw_hash": h,
            "totp_secret": secret,
        })
        st.pop("pending_secret", None)
        save_state(st)
        try:
            os.remove(SETUP_TOKEN_FILE)
        except Exception:
            pass
        self._redirect("/_admin/", cookie=make_cookie(st))

    # ---------- 定制客户端页（一键生成定制客户端的开源平替） ----------
    def _gate_authed(self):
        st = load_state()
        return bool(st.get("enrolled")) and check_cookie(st, self.headers.get("Cookie", ""))

    def _client_page(self):
        p = CLIENT_PARAMS
        magic = f"rustdesk-host={p['id_server'].split(':')[0]},key={p['key']},.exe"
        body = f"""
        <h1>定制客户端</h1>
        <p>预置好本服务器参数的客户端安装包，装完即连自建服务器，无需再改设置。</p>
        <label>ID 服务器</label><div class="secret">{p['id_server']}</div>
        <label>中继服务器</label><div class="secret">{p['relay_server']}</div>
        <label>API 服务器</label><div class="secret">{p['api_server']}</div>
        <label>公钥 Key</label><div class="secret">{p['key']}</div>
        <form method="get" action="/gate/client/download">
          <button type="submit">下载定制客户端（Windows，1.5.3）</button>
        </form>
        <p>也可以用官方安装包自助定制：把官方安装器改名为
        <code>{magic}</code> 再运行，可自动带入 ID 服务器与公钥
        （中继与 API 仍需在设置里补一次）。</p>
        <p>重新打包说明与脚本在代码仓库 client-package/ 目录（Windows 上运行
        build 即可按当前参数重新生成安装包）。</p>"""
        return page("定制客户端", body)

    def _client_download(self):
        try:
            size = os.path.getsize(CLIENT_PACKAGE)
        except Exception:
            self._send(404, "package not found", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header(
            "Content-Disposition",
            'attachment; filename="RustDesk-1.5.3-JingWei.exe"',
        )
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(CLIENT_PACKAGE, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/auth":
            self._auth()
        elif path == "/gate/client":
            if self._gate_authed():
                self._send(200, self._client_page())
            else:
                self._redirect("/gate/login")
        elif path == "/gate/client/download":
            if self._gate_authed():
                self._client_download()
            else:
                self._redirect("/gate/login")
        elif path == "/gate/login":
            st = load_state()
            if st.get("enrolled"):
                self._send(200, self._login_page())
            else:
                self._send(200, page("尚未启用", "<h1>双因素验证尚未启用</h1><p>请管理员先通过一次性注册链接完成启用。</p>"))
        elif path == "/gate/enroll":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            self._send(200, self._enroll_page(q.get("setup", [""])[0]))
        elif path == "/gate/logout":
            self._send(302, "", extra={
                "Location": "/gate/login",
                "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
            })
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/gate/login":
            self._login()
        elif path == "/gate/enroll":
            self._enroll()
        else:
            self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"admin gate on {LISTEN_HOST}:{LISTEN_PORT}, state={STATE_DIR}")
    srv.serve_forever()
