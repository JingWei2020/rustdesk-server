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


# ================= WebAuthn（YubiKey 等安全密钥免密码登录） =================
# 在口令+TOTP 之外增加 FIDO2/WebAuthn 登录：注册必须在已通过网关验证的会话里
# 完成（因此不校验 attestation 证书链，只解析 authData 取凭据公钥）；
# 登录断言则严格验签（cryptography 库，DC-02 随 certbot 已装 python3-cryptography）。
# 口令+TOTP 保留为兜底与恢复路径；可注册多把密钥（常用 + 备份）。
WA_RP_ID = os.environ.get("GATE_RP_ID", "rd.ljwc.eu.cc")
WA_ORIGIN = os.environ.get("GATE_ORIGIN", "https://rd.ljwc.eu.cc")
# 安全密钥登录成功后直达后台（SSO）：网关用管理员在 /gate/security 里预存的
# lejianwen 账号密码（存 state.json，root-only 600）向本地 API 换 token，
# 再由 /gate/sso 页面把 token 写入控制台 SPA 的 localStorage 后跳入后台。
SSO_API_BASE = os.environ.get("GATE_SSO_API_BASE", "http://127.0.0.1:21114")


def _lejianwen_login(username: str, password: str):
    """向 lejianwen 管理端登录口 /api/admin/login 换 token（控制台 SPA 同款），
    成功返回 data dict，失败抛 ValueError。
    注意不要用 /api/login：那是客户端登录口，成功返回的是顶层 access_token
    且形状不同——曾因此把正确密码的成功响应误判为失败。"""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        SSO_API_BASE + "/api/admin/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise ValueError(f"lejianwen login http {e.code}")
    data = out.get("data") or {}
    if data.get("token"):
        return data
    if out.get("access_token"):
        return {"token": out["access_token"], "username": username}
    msg = out.get("error") or out.get("message") or out.get("msg")
    if msg:
        raise ValueError(str(msg))
    raise ValueError(f"lejianwen login: unexpected response code={out.get('code')}")


def _sso_configured(st) -> bool:
    return bool(st.get("sso_user") and st.get("sso_pass"))
WA_USER_HANDLE = b"jingwei-rustdesk-gate"
WA_CHALLENGES = {}  # challenge_b64u -> (purpose, expire_ts)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _cbor_read(data, off):
    """极简 CBOR 解码（WebAuthn 用到的主类型 0-5 与简单值），返回 (对象, 新偏移)。"""
    b0 = data[off]
    off += 1
    major, ai = b0 >> 5, b0 & 0x1F
    if ai < 24:
        n = ai
    elif ai == 24:
        n = data[off]
        off += 1
    elif ai == 25:
        n = struct.unpack(">H", data[off:off + 2])[0]
        off += 2
    elif ai == 26:
        n = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
    elif ai == 27:
        n = struct.unpack(">Q", data[off:off + 8])[0]
        off += 8
    else:
        raise ValueError("cbor: unsupported additional info")
    if major == 0:
        return n, off
    if major == 1:
        return -1 - n, off
    if major == 2:
        return data[off:off + n], off + n
    if major == 3:
        return data[off:off + n].decode("utf-8"), off + n
    if major == 4:
        arr = []
        for _ in range(n):
            v, off = _cbor_read(data, off)
            arr.append(v)
        return arr, off
    if major == 5:
        m = {}
        for _ in range(n):
            k, off = _cbor_read(data, off)
            v, off = _cbor_read(data, off)
            m[k] = v
        return m, off
    if major == 7:
        if ai == 20:
            return False, off
        if ai == 21:
            return True, off
        if ai == 22:
            return None, off
        raise ValueError("cbor: unsupported simple value")
    raise ValueError("cbor: bad major type")


def _parse_auth_data(auth: bytes, with_attested: bool):
    if len(auth) < 37:
        raise ValueError("authData too short")
    out = {
        "rp_id_hash": auth[:32],
        "flags": auth[32],
        "sign_count": struct.unpack(">I", auth[33:37])[0],
    }
    if with_attested:
        off = 37 + 16  # 跳过 AAGUID
        clen = struct.unpack(">H", auth[off:off + 2])[0]
        off += 2
        out["cred_id"] = auth[off:off + clen]
        off += clen
        cose, _ = _cbor_read(auth, off)
        out["cose"] = cose
    return out


def _numbers_to_pubkey(numbers):
    # cryptography 2.8（DC-02 系统版）要求传 backend，新版又移除了该参数，两边都兼容
    try:
        return numbers.public_key()
    except TypeError:
        from cryptography.hazmat.backends import default_backend
        return numbers.public_key(default_backend())


def _load_pem_key(pem: str):
    from cryptography.hazmat.primitives import serialization
    try:
        return serialization.load_pem_public_key(pem.encode())
    except TypeError:
        from cryptography.hazmat.backends import default_backend
        return serialization.load_pem_public_key(pem.encode(), backend=default_backend())


def _cose_to_pem(cose) -> str:
    from cryptography.hazmat.primitives import serialization
    kty = cose.get(1)
    if kty == 2:  # EC2
        from cryptography.hazmat.primitives.asymmetric import ec
        x = int.from_bytes(cose[-2], "big")
        y = int.from_bytes(cose[-3], "big")
        key = _numbers_to_pubkey(ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()))
    elif kty == 1:  # OKP (Ed25519)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(cose[-2])
    elif kty == 3:  # RSA
        from cryptography.hazmat.primitives.asymmetric import rsa
        n = int.from_bytes(cose[-1], "big")
        e = int.from_bytes(cose[-2], "big")
        key = _numbers_to_pubkey(rsa.RSAPublicNumbers(e, n))
    else:
        raise ValueError("unsupported COSE kty")
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def _verify_assertion(pem: str, alg: int, signature: bytes, data: bytes):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.exceptions import InvalidSignature
    key = _load_pem_key(pem)
    try:
        if alg == -8:  # EdDSA
            key.verify(signature, data)
        elif alg == -257:  # RS256
            key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        else:  # ES256 (-7) 及其它 EC 算法按 ES256 处理
            key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("signature verification failed")


def _wa_issue_challenge(purpose: str) -> str:
    chal = b64u(secrets.token_bytes(32))
    now = time.time()
    for k in [k for k, (_, exp) in WA_CHALLENGES.items() if exp < now]:
        WA_CHALLENGES.pop(k, None)
    WA_CHALLENGES[chal] = (purpose, now + 300)
    return chal


def _wa_consume_challenge(chal: str, purpose: str):
    item = WA_CHALLENGES.pop(chal, None)
    if not item or item[0] != purpose or item[1] < time.time():
        raise ValueError("challenge invalid or expired")


def _wa_check_client_data(raw: bytes, expect_type: str, purpose: str):
    cd = json.loads(raw.decode("utf-8"))
    if cd.get("type") != expect_type:
        raise ValueError("clientData type mismatch")
    if cd.get("origin") != WA_ORIGIN:
        raise ValueError("origin mismatch")
    _wa_consume_challenge(cd.get("challenge", ""), purpose)
    return cd


def wa_credentials(st):
    return st.get("webauthn") or []


WA_JS = """
<script>
function b64uToBuf(s){s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';var bin=atob(s);var b=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)b[i]=bin.charCodeAt(i);return b.buffer;}
function bufToB64u(buf){var b=new Uint8Array(buf);var s='';for(var i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
async function waPost(url, body){var r=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json();}
var waLoginPending=false;
var waStash=null, waStashAt=0;
async function waPrefetch(){
  try{
    var o=await waPost('/gate/webauthn/login/options');
    if(!o.error){ waStash=o; waStashAt=Date.now(); }
  }catch(e){}
}
async function waLoginStart(errEl){
  if(waLoginPending)return;
  waLoginPending=true;
  var btn=document.getElementById('wa-login-btn');
  if(btn){btn.disabled=true;btn.textContent='正在等待系统验证弹窗…请在弹窗中选通行密钥并确认';}
  try{
    var raw;
    if(waStash && Date.now()-waStashAt < 240000){ raw=waStash; waStash=null; waPrefetch(); }
    else { raw=await waPost('/gate/webauthn/login/options'); }
    if(raw.error)throw new Error(raw.error);
    var o={challenge:b64uToBuf(raw.challenge), rpId:raw.rpId, timeout:120000,
           userVerification:raw.userVerification||'preferred',
           allowCredentials:(raw.allowCredentials||[]).map(function(c){return {type:'public-key', id:b64uToBuf(c.id)};})};
    var cred=await navigator.credentials.get({publicKey:o});
    var j=await waPost('/gate/webauthn/login/verify',{id:cred.id,rawId:bufToB64u(cred.rawId),response:{authenticatorData:bufToB64u(cred.response.authenticatorData),clientDataJSON:bufToB64u(cred.response.clientDataJSON),signature:bufToB64u(cred.response.signature)}});
    if(!j.ok)throw new Error(j.error||'验证失败');
    location.href=j.redirect||'/_admin/';
  }catch(e){ waLoginPending=false; if(btn){btn.disabled=false;btn.textContent='用安全密钥登录（免口令）';} if(errEl)errEl.textContent='安全密钥登录失败：'+e.message; else alert('安全密钥登录失败：'+e.message); }
}
async function waRegisterStart(){
  var name=(document.getElementById('wa-name').value||'YubiKey').trim();
  var errEl=document.getElementById('wa-err');
  try{
    var o=await waPost('/gate/webauthn/register/options');
    if(o.error)throw new Error(o.error);
    o.challenge=b64uToBuf(o.challenge); o.user.id=b64uToBuf(o.user.id);
    (o.excludeCredentials||[]).forEach(function(c){c.id=b64uToBuf(c.id);});
    var cred=await navigator.credentials.create({publicKey:o});
    var j=await waPost('/gate/webauthn/register/verify',{id:cred.id,rawId:bufToB64u(cred.rawId),name:name,response:{attestationObject:bufToB64u(cred.response.attestationObject),clientDataJSON:bufToB64u(cred.response.clientDataJSON)}});
    if(!j.ok)throw new Error(j.error||'注册失败');
    location.reload();
  }catch(e){ if(errEl)errEl.textContent='添加失败：'+e.message; }
}
if(document.getElementById('wa-login-btn')){ waPrefetch(); setInterval(function(){ if(!waLoginPending) waPrefetch(); }, 120000); }
</script>"""


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
        wa = ""
        if wa_credentials(load_state()):
            wa = f"""
        <button type="button" id="wa-login-btn" onclick="waLoginStart(document.getElementById('wa-err'))">用安全密钥登录（免口令）</button>
        <div class="err" id="wa-err"></div>
        <p>或使用口令 + 动态码登录：</p>{WA_JS}"""
        return page("控制台双因素验证", f"""
        <h1>RustDesk 控制台 · 双因素验证</h1>
        <p>安全密钥登录通过后将直达控制台；口令 + 动态码通过后，还需再完成控制台自身的账号登录。</p>{e}{wa}
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

    # ---------- WebAuthn（安全密钥免密码登录） ----------
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _send_json(self, code, obj, cookie=None):
        extra = {}
        if cookie:
            extra["Set-Cookie"] = (
                f"{COOKIE_NAME}={cookie}; Path=/; Max-Age={COOKIE_TTL}; "
                "HttpOnly; Secure; SameSite=Lax"
            )
        self._send(code, json.dumps(obj), "application/json", extra=extra)

    def _wa_err(self, msg, code=400):
        self._send_json(code, {"ok": False, "error": msg})

    def _wa_register_options(self):
        if not self._gate_authed():
            return self._wa_err("not authenticated", 401)
        st = load_state()
        opts = {
            "challenge": _wa_issue_challenge("register"),
            "rp": {"id": WA_RP_ID, "name": "RustDesk Console"},
            "user": {
                "id": b64u(WA_USER_HANDLE),
                "name": "jingwei",
                "displayName": "JingWei",
            },
            "pubKeyCredParams": [
                {"type": "public-key", "alg": -7},
                {"type": "public-key", "alg": -8},
                {"type": "public-key", "alg": -257},
            ],
            "authenticatorSelection": {
                # 不限制 authenticatorAttachment：手机本机通行密钥与
                # USB/NFC 安全密钥（YubiKey）都可注册，登录时任选其一
                "residentKey": "preferred",
                "userVerification": "preferred",
            },
            "attestation": "none",
            "excludeCredentials": [
                {"type": "public-key", "id": c["cred_id"]} for c in wa_credentials(st)
            ],
            "timeout": 60000,
        }
        self._send_json(200, opts)

    def _wa_register_verify(self):
        if not self._gate_authed():
            return self._wa_err("not authenticated", 401)
        try:
            body = self._json_body()
            resp = body["response"]
            _wa_check_client_data(b64u_dec(resp["clientDataJSON"]), "webauthn.create", "register")
            att, _ = _cbor_read(b64u_dec(resp["attestationObject"]), 0)
            auth = _parse_auth_data(att["authData"], True)
            if auth["rp_id_hash"] != hashlib.sha256(WA_RP_ID.encode()).digest():
                raise ValueError("rpIdHash mismatch")
            if not (auth["flags"] & 0x01):
                raise ValueError("user presence flag missing")
            cose = auth["cose"]
            entry = {
                "cred_id": b64u(auth["cred_id"]),
                "pem": _cose_to_pem(cose),
                "alg": cose.get(3, -7),
                "sign_count": auth["sign_count"],
                "name": (body.get("name") or "YubiKey")[:40],
                "created": int(time.time()),
            }
            st = load_state()
            creds = [c for c in wa_credentials(st) if c["cred_id"] != entry["cred_id"]]
            creds.append(entry)
            st["webauthn"] = creds
            save_state(st)
            self._send_json(200, {"ok": True})
        except Exception as e:
            self._wa_err(str(e))

    def _wa_login_options(self):
        st = load_state()
        creds = wa_credentials(st)
        if not creds:
            return self._wa_err("no security key registered")
        self._send_json(200, {
            "challenge": _wa_issue_challenge("login"),
            "rpId": WA_RP_ID,
            "allowCredentials": [
                {"type": "public-key", "id": c["cred_id"]} for c in creds
            ],
            "userVerification": "preferred",
            "timeout": 60000,
        })

    def _wa_login_verify(self):
        ip = self._ip()
        if rate_limited(ip):
            return self._wa_err("too many attempts", 429)
        try:
            body = self._json_body()
            resp = body["response"]
            st = load_state()
            cred = None
            for c in wa_credentials(st):
                if c["cred_id"] in (body.get("id"), body.get("rawId")):
                    cred = c
                    break
            if cred is None:
                raise ValueError("unknown credential")
            auth = _parse_auth_data(b64u_dec(resp["authenticatorData"]), False)
            if auth["rp_id_hash"] != hashlib.sha256(WA_RP_ID.encode()).digest():
                raise ValueError("rpIdHash mismatch")
            if not (auth["flags"] & 0x01):
                raise ValueError("user presence flag missing")
            cd_raw = b64u_dec(resp["clientDataJSON"])
            _wa_check_client_data(cd_raw, "webauthn.get", "login")
            signed = b64u_dec(resp["authenticatorData"]) + hashlib.sha256(cd_raw).digest()
            _verify_assertion(cred["pem"], cred.get("alg", -7), b64u_dec(resp["signature"]), signed)
            sc = auth["sign_count"]
            if sc and cred.get("sign_count") and sc <= cred["sign_count"]:
                raise ValueError("sign count regression")
            if sc:
                cred["sign_count"] = sc
                save_state(st)
            clear_fails(ip)
            out = {"ok": True}
            if _sso_configured(st):
                out["redirect"] = "/gate/sso"
            self._send_json(200, out, cookie=make_cookie(st))
        except Exception as e:
            record_fail(ip)
            self._wa_err(str(e))

    def _sso_page(self):
        st = load_state()
        if not _sso_configured(st):
            self._redirect("/_admin/")
            return
        try:
            data = _lejianwen_login(st["sso_user"], st["sso_pass"])
        except Exception:
            self._redirect("/_admin/")
            return
        token = json.dumps(data["token"])
        uname = json.dumps(data.get("username") or st["sso_user"])
        body = f"""<script>
        localStorage.setItem('access_token', {token});
        localStorage.setItem('wc-option:local:access_token', {token});
        localStorage.setItem('user_info', JSON.stringify({{name: {uname}}}));
        location.replace('/_admin/');
        </script>"""
        self._send(200, body)

    def _sso_config(self):
        if not self._gate_authed():
            self._redirect("/gate/login")
            return
        form = self._form()
        st = load_state()
        if form.get("clear", "") == "1":
            st.pop("sso_user", None)
            st.pop("sso_pass", None)
            save_state(st)
            self._redirect("/gate/security")
            return
        user = form.get("sso_user", "").strip()
        pw = form.get("sso_pass", "")
        try:
            _lejianwen_login(user, pw)
        except Exception as e:
            self._send(400, self._security_page(err=f"控制台账号验证失败：{e}"))
            return
        st["sso_user"] = user
        st["sso_pass"] = pw
        save_state(st)
        self._redirect("/gate/security")

    def _security_page(self, err=""):
        st = load_state()
        rows = ""
        for c in wa_credentials(st):
            from datetime import datetime
            when = datetime.fromtimestamp(c.get("created", 0)).strftime("%Y-%m-%d %H:%M")
            rows += (
                f'<div class="secret">{c.get("name", "YubiKey")} · 添加于 {when}'
                f'<form method="post" action="/gate/webauthn/delete" '
                f'onsubmit="return confirm(\'删除这把安全密钥？\')">'
                f'<input type="hidden" name="cred_id" value="{c["cred_id"]}">'
                f'<button type="submit">删除</button></form></div>'
            )
        if not rows:
            rows = "<p>还没有安全密钥。添加一把后，登录页即可免口令一键登录。</p>"
        e = f'<div class="err">{err}</div>' if err else ""
        if _sso_configured(st):
            sso = f"""
        <p>已开启：安全密钥验证通过后自动登录控制台（账号 {st.get('sso_user')}），
        不再输控制台账号密码。</p>
        <form method="post" action="/gate/sso/config">
          <input type="hidden" name="clear" value="1">
          <button type="submit">关闭免登录直达</button>
        </form>"""
        else:
            sso = """
        <p>未开启。开启后：安全密钥验证通过即自动登录控制台，不再输控制台账号密码。
        需在此填一次控制台管理员账号密码（网关会先向控制台验证，正确才保存；
        密码明文存服务器 root 专用状态文件，与动态码密钥同级保护）。</p>
        <form method="post" action="/gate/sso/config">
          <label>控制台用户名</label>
          <input type="text" name="sso_user" value="admin" autocomplete="username">
          <label>控制台密码</label>
          <input type="password" name="sso_pass" autocomplete="current-password">
          <button type="submit">验证并开启免登录直达</button>
        </form>"""
        body = f"""
        <h1>安全密钥（免密码登录）</h1>{e}
        <p>已添加的安全密钥：</p>{rows}
        <label>给新密钥起个名（如 YubiKey 5 NFC）</label>
        <input type="text" id="wa-name" value="YubiKey">
        <button type="button" onclick="waRegisterStart()">添加安全密钥</button>
        <div class="err" id="wa-err"></div>
        <p>建议两把 YubiKey 都加上（一把常用、一把备份）。口令 + 动态码的
        登录方式会保留作兜底。添加时浏览器会要求触摸密钥。</p>
        <h1>控制台免登录直达</h1>{sso}
        <p><a href="/_admin/">返回控制台</a> · <a href="/gate/client">定制客户端</a></p>
        {WA_JS}"""
        return page("安全密钥", body)

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
        elif path == "/gate/security":
            if self._gate_authed():
                self._send(200, self._security_page())
            else:
                self._redirect("/gate/login")
        elif path == "/gate/sso":
            if self._gate_authed():
                self._sso_page()
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
        elif path == "/gate/sso/config":
            self._sso_config()
        elif path == "/gate/webauthn/register/options":
            self._wa_register_options()
        elif path == "/gate/webauthn/register/verify":
            self._wa_register_verify()
        elif path == "/gate/webauthn/login/options":
            self._wa_login_options()
        elif path == "/gate/webauthn/login/verify":
            self._wa_login_verify()
        elif path == "/gate/webauthn/delete":
            if not self._gate_authed():
                self._redirect("/gate/login")
            else:
                data = self._form()
                st = load_state()
                st["webauthn"] = [
                    c for c in wa_credentials(st)
                    if c["cred_id"] != data.get("cred_id", "")
                ]
                save_state(st)
                self._redirect("/gate/security")
        else:
            self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"admin gate on {LISTEN_HOST}:{LISTEN_PORT}, state={STATE_DIR}")
    srv.serve_forever()
