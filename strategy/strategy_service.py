#!/usr/bin/env python3
"""
RustDesk Pro API 层平替服务（策略下发 + 细粒度 API 令牌 + Pro 形状只读资源接口）

三块能力，协议/形状均对齐开源客户端源码与官方 Pro API 参考脚本（res/*.py），
不使用 Pro 的任何代码：

1) 策略下发（Pro Strategy）
   客户端心跳 POST {api-server}/api/heartbeat（见客户端 src/hbbs_http/sync.rs）：
   本服务先把心跳原样转发给上游 lejianwen API，再把该设备被分配的策略注入响应
   {"strategy": {"config_options": {...}}, "modified_at": ts}，客户端收到即应用；
   取消分配时回送空值清除此前下发的键。
   管理接口（对齐 res/strategies.py）：GET/POST /api/strategies、
   GET/PUT/DELETE /api/strategies/{guid}、PUT /api/strategies/{guid}/status、
   POST /api/strategies/assign {strategy, peers[], users[], groups[]}。

2) 细粒度 API 令牌（Pro API Tokens）
   令牌携带权限名集合（Pro 词汇：users.view、devices.view、strategies.edit …，
   完整表见 res/admin-roles.py）。管理接口（仅主令牌可用）：
   POST /api/tokens {name, permissions[], expires_days?} → 令牌值仅返回一次；
   GET /api/tokens 列表（不含令牌值）；DELETE /api/tokens/{id} 吊销。
   校验规则：主令牌（STRATEGY_ADMIN_TOKEN）= 全权限；作用域令牌按权限名校验，
   *.edit 蕴含 *.view；令牌以 SHA-256 哈希落库，可吊销、可过期。

3) Pro 形状只读资源接口（数据来自 lejianwen 的 SQLite，只读）
   GET /api/devices（devices.view）、GET /api/users（users.view）、
   GET /api/user-groups（user_groups.view）、GET /api/device-groups（device_groups.view）、
   GET /api/audits/conns|files|logins（audits.view）。
   均为 {data: [...], total: N} 分页形状，对象带确定性 GUID（uuid5）。
   例外：/api/users 在 lejianwen 本有同名路由——请求若不带本服务的令牌，
   一律原样透传给上游，不影响既有功能。

部署：监听 127.0.0.1:21214，Nginx 把 /api/heartbeat、/api/strategies*、
/api/tokens、/api/devices、/api/users、/api/user-groups、/api/device-groups、
/api/audits* 路由到这里。仅 Python 标准库。
"""

import hashlib
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid as uuidlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("STRATEGY_UPSTREAM", "http://127.0.0.1:21114")
DB_PATH = os.environ.get("STRATEGY_DB", "/opt/rustdesk-strategy/strategy.db")
LEJIANWEN_DB = os.environ.get("LEJIANWEN_DB", "/opt/rustdesk-api/data/rustdeskapi.db")
ADMIN_TOKEN = os.environ.get("STRATEGY_ADMIN_TOKEN", "")
LISTEN_HOST = os.environ.get("STRATEGY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("STRATEGY_LISTEN_PORT", "21214"))

GUID_NS = uuidlib.UUID("6b1f2c3d-0000-4000-8000-525553544445")


def guid_for(kind, key):
    return str(uuidlib.uuid5(GUID_NS, f"{kind}:{key}"))


def iso_utc(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


# ---------------- 本服务数据库 ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategies (
            guid TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            config_options TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL,
            modified_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignments (
            strategy_guid TEXT NOT NULL,
            kind TEXT NOT NULL,
            target TEXT NOT NULL,
            PRIMARY KEY (strategy_guid, kind, target)
        );
        CREATE TABLE IF NOT EXISTS device_state (
            device_id TEXT PRIMARY KEY,
            applied_guid TEXT,
            applied_keys TEXT NOT NULL DEFAULT '[]',
            cleared_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL DEFAULT 0,
            last_used_at INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()


def lw_db():
    return sqlite3.connect(f"file:{LEJIANWEN_DB}?mode=ro", uri=True)


# ---------------- 策略 ----------------
def strategy_row_to_obj(row):
    return {
        "guid": row["guid"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "config_options": json.loads(row["config_options"] or "{}"),
        "created_at": row["created_at"],
        "modified_at": row["modified_at"],
    }


def list_strategies(conn):
    rows = conn.execute("SELECT * FROM strategies ORDER BY name").fetchall()
    return [strategy_row_to_obj(r) for r in rows]


def get_strategy(conn, guid):
    row = conn.execute("SELECT * FROM strategies WHERE guid=?", (guid,)).fetchone()
    return strategy_row_to_obj(row) if row else None


def find_assigned_strategy(conn, device_id):
    row = conn.execute(
        """
        SELECT s.* FROM strategies s
        JOIN assignments a ON a.strategy_guid = s.guid
        WHERE a.kind='peer' AND a.target=? AND s.enabled=1
        ORDER BY s.modified_at DESC LIMIT 1
        """,
        (device_id,),
    ).fetchone()
    return strategy_row_to_obj(row) if row else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _send(self, code, body_bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _proxy_upstream(self, method, raw):
        """把当前请求原样转发给上游 lejianwen 并透传其响应。"""
        try:
            req = urllib.request.Request(
                UPSTREAM + self.path,
                data=raw if method in ("POST", "PUT", "DELETE") else None,
                method=method,
                headers={
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "Authorization": self.headers.get("Authorization", ""),
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except Exception:
            self._send_json(502, {"error": "upstream unavailable"})

    # ---------- 鉴权 ----------
    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else ""

    def _is_master(self):
        return bool(ADMIN_TOKEN) and self._bearer() == ADMIN_TOKEN

    def _auth_perms(self):
        """返回权限集合；None 表示未通过任何令牌校验。"""
        tok = self._bearer()
        if not tok:
            return None
        if ADMIN_TOKEN and tok == ADMIN_TOKEN:
            return {"*"}
        conn = db()
        try:
            row = conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash=?",
                (hashlib.sha256(tok.encode()).hexdigest(),),
            ).fetchone()
            if not row or row["revoked"]:
                return None
            now = int(time.time())
            if row["expires_at"] and row["expires_at"] < now:
                return None
            if now - row["last_used_at"] > 60:
                conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
                conn.commit()
            return set(json.loads(row["permissions"] or "[]"))
        finally:
            conn.close()

    def _require(self, perm):
        perms = self._auth_perms()
        if perms is None:
            self._send_json(401, {"error": "unauthorized"})
            return False
        if "*" in perms or perm in perms:
            return True
        if perm.endswith(".view") and (perm[:-5] + ".edit") in perms:
            return True
        self._send_json(403, {"error": f"insufficient permission: {perm}"})
        return False

    # ---------- 心跳（策略注入） ----------
    def _handle_heartbeat(self, raw):
        upstream_body = b"{}"
        upstream_ctype = "application/json"
        try:
            req = urllib.request.Request(
                UPSTREAM + "/api/heartbeat",
                data=raw,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                upstream_body = r.read()
                upstream_ctype = r.headers.get("Content-Type", upstream_ctype)
        except Exception:
            upstream_body = b"{}"

        try:
            req_obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            req_obj = {}
        device_id = str(req_obj.get("id", ""))
        try:
            client_ts = int(req_obj.get("modified_at", 0))
        except Exception:
            client_ts = 0

        try:
            resp_obj = json.loads(upstream_body.decode("utf-8") or "{}")
            if not isinstance(resp_obj, dict):
                raise ValueError("not an object")
        except Exception:
            self._send(200, upstream_body, upstream_ctype)
            return

        if device_id:
            conn = db()
            try:
                strat = find_assigned_strategy(conn, device_id)
                state = conn.execute(
                    "SELECT * FROM device_state WHERE device_id=?", (device_id,)
                ).fetchone()
                applied_guid = state["applied_guid"] if state else None
                applied_keys = json.loads(state["applied_keys"] or "[]") if state else []
                cleared_at = state["cleared_at"] if state else 0

                if strat:
                    if strat["guid"] != applied_guid or strat["modified_at"] != client_ts:
                        resp_obj["strategy"] = {"config_options": strat["config_options"]}
                        resp_obj["modified_at"] = strat["modified_at"]
                        conn.execute(
                            "INSERT INTO device_state(device_id, applied_guid, applied_keys, cleared_at) "
                            "VALUES(?,?,?,0) ON CONFLICT(device_id) DO UPDATE SET "
                            "applied_guid=excluded.applied_guid, applied_keys=excluded.applied_keys, cleared_at=0",
                            (device_id, strat["guid"], json.dumps(sorted(strat["config_options"].keys()))),
                        )
                        conn.commit()
                else:
                    if applied_guid and cleared_at == 0:
                        clearing = {k: "" for k in applied_keys}
                        now = int(time.time())
                        resp_obj["strategy"] = {"config_options": clearing}
                        resp_obj["modified_at"] = now
                        conn.execute(
                            "UPDATE device_state SET applied_guid=NULL, applied_keys='[]', cleared_at=? WHERE device_id=?",
                            (now, device_id),
                        )
                        conn.commit()
            finally:
                conn.close()

        self._send_json(200, resp_obj)

    # ---------- 策略管理 ----------
    def _handle_strategies(self, method, path, raw):
        conn = db()
        try:
            parts = [p for p in path.split("/") if p]
            now = int(time.time())

            if method == "GET" and path == "/api/strategies":
                if not self._require("strategies.view"):
                    return
                self._send_json(200, list_strategies(conn))
                return

            if method == "POST" and path == "/api/strategies":
                if not self._require("strategies.edit"):
                    return
                data = json.loads(raw.decode("utf-8") or "{}")
                name = (data.get("name") or "").strip()
                opts = data.get("config_options") or {}
                if not name or not isinstance(opts, dict):
                    self._send_json(400, {"error": "name and config_options required"})
                    return
                guid = str(uuidlib.uuid4())
                try:
                    conn.execute(
                        "INSERT INTO strategies(guid,name,enabled,config_options,created_at,modified_at) VALUES(?,?,?,?,?,?)",
                        (guid, name, 1 if data.get("enabled", True) else 0, json.dumps(opts), now, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    self._send_json(409, {"error": "strategy name exists"})
                    return
                self._send_json(200, get_strategy(conn, guid))
                return

            if method == "POST" and path == "/api/strategies/assign":
                if not self._require("strategies.edit"):
                    return
                data = json.loads(raw.decode("utf-8") or "{}")
                sguid = data.get("strategy")
                if sguid:
                    row = conn.execute(
                        "SELECT guid FROM strategies WHERE guid=? OR name=?", (sguid, sguid)
                    ).fetchone()
                    if not row:
                        self._send_json(404, {"error": "strategy not found"})
                        return
                    sguid = row["guid"]
                for kind, key in (("peer", "peers"), ("user", "users"), ("group", "groups")):
                    for target in data.get(key) or []:
                        target = str(target)
                        conn.execute(
                            "DELETE FROM assignments WHERE kind=? AND target=?", (kind, target)
                        )
                        if sguid:
                            conn.execute(
                                "INSERT INTO assignments(strategy_guid,kind,target) VALUES(?,?,?)",
                                (sguid, kind, target),
                            )
                conn.commit()
                self._send_json(200, {"ok": True})
                return

            if len(parts) == 3 and parts[:2] == ["api", "strategies"]:
                guid = parts[2]
                obj = get_strategy(conn, guid)
                if not obj:
                    self._send_json(404, {"error": "strategy not found"})
                    return
                if method == "GET":
                    if not self._require("strategies.view"):
                        return
                    rows = conn.execute(
                        "SELECT kind,target FROM assignments WHERE strategy_guid=?", (guid,)
                    ).fetchall()
                    obj["assignments"] = [{"kind": r["kind"], "target": r["target"]} for r in rows]
                    self._send_json(200, obj)
                    return
                if method == "PUT":
                    if not self._require("strategies.edit"):
                        return
                    data = json.loads(raw.decode("utf-8") or "{}")
                    conn.execute(
                        "UPDATE strategies SET name=?, config_options=?, enabled=?, modified_at=? WHERE guid=?",
                        (
                            data.get("name", obj["name"]),
                            json.dumps(data.get("config_options", obj["config_options"])),
                            1 if data.get("enabled", obj["enabled"]) else 0,
                            now,
                            guid,
                        ),
                    )
                    conn.commit()
                    self._send_json(200, get_strategy(conn, guid))
                    return
                if method == "DELETE":
                    if not self._require("strategies.edit"):
                        return
                    conn.execute("DELETE FROM strategies WHERE guid=?", (guid,))
                    conn.execute("DELETE FROM assignments WHERE strategy_guid=?", (guid,))
                    conn.commit()
                    self._send_json(200, {"ok": True})
                    return

            if len(parts) == 4 and parts[:2] == ["api", "strategies"] and parts[3] == "status" and method == "PUT":
                if not self._require("strategies.edit"):
                    return
                guid = parts[2]
                if not get_strategy(conn, guid):
                    self._send_json(404, {"error": "strategy not found"})
                    return
                enabled = json.loads(raw.decode("utf-8") or "false")
                conn.execute(
                    "UPDATE strategies SET enabled=?, modified_at=? WHERE guid=?",
                    (1 if enabled else 0, now, guid),
                )
                conn.commit()
                self._send_json(200, {"ok": True})
                return

            self._send_json(404, {"error": "not found"})
        finally:
            conn.close()

    # ---------- 令牌管理（仅主令牌） ----------
    def _handle_tokens(self, method, path, raw):
        if not self._is_master():
            self._send_json(401, {"error": "unauthorized"})
            return
        conn = db()
        try:
            parts = [p for p in path.split("/") if p]
            now = int(time.time())
            if method == "GET" and path == "/api/tokens":
                rows = conn.execute("SELECT * FROM api_tokens ORDER BY id").fetchall()
                self._send_json(200, [
                    {
                        "id": r["id"], "name": r["name"],
                        "permissions": json.loads(r["permissions"] or "[]"),
                        "created_at": r["created_at"], "expires_at": r["expires_at"],
                        "last_used_at": r["last_used_at"], "revoked": bool(r["revoked"]),
                    } for r in rows
                ])
                return
            if method == "POST" and path == "/api/tokens":
                data = json.loads(raw.decode("utf-8") or "{}")
                name = (data.get("name") or "").strip()
                perms = data.get("permissions") or []
                if not name or not isinstance(perms, list):
                    self._send_json(400, {"error": "name and permissions required"})
                    return
                days = int(data.get("expires_days") or 0)
                expires = now + days * 86400 if days > 0 else 0
                value = "rdt_" + uuidlib.uuid4().hex + uuidlib.uuid4().hex[:8]
                cur = conn.execute(
                    "INSERT INTO api_tokens(name,token_hash,permissions,created_at,expires_at) VALUES(?,?,?,?,?)",
                    (name, hashlib.sha256(value.encode()).hexdigest(), json.dumps(perms), now, expires),
                )
                conn.commit()
                self._send_json(200, {
                    "id": cur.lastrowid, "name": name, "permissions": perms,
                    "token": value, "created_at": now, "expires_at": expires,
                    "note": "token value is shown only once",
                })
                return
            if method == "DELETE" and len(parts) == 3:
                conn.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (parts[2],))
                conn.commit()
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": "not found"})
        finally:
            conn.close()

    # ---------- Pro 形状只读资源 ----------
    def _paging(self):
        q = self._query()
        try:
            current = max(1, int((q.get("current") or ["1"])[0]))
        except Exception:
            current = 1
        try:
            size = min(200, max(1, int((q.get("pageSize") or ["50"])[0])))
        except Exception:
            size = 50
        return current, size

    def _lw(self):
        try:
            return lw_db()
        except Exception:
            return None

    def _devices(self):
        if not self._require("devices.view"):
            return
        conn = self._lw()
        if not conn:
            self._send_json(503, {"error": "upstream db unavailable"})
            return
        try:
            q = self._query()
            users = {r[0]: r[1] for r in conn.execute("SELECT id, username FROM users")}
            groups = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM groups")}
            rows = conn.execute("SELECT * FROM peers ORDER BY last_online_time DESC").fetchall()
            cols = [d[0] for d in conn.execute("SELECT * FROM peers LIMIT 0").description]
            items = []
            now = int(time.time())
            for row in rows:
                d = dict(zip(cols, row))
                if q.get("id") and not _like(str(d["id"]), q["id"][0]):
                    continue
                if q.get("device_name") and not _like(str(d["hostname"]), q["device_name"][0]):
                    continue
                if q.get("user_name") and not _like(str(d["username"]), q["user_name"][0]):
                    continue
                items.append({
                    "id": str(d["id"]),
                    "guid": guid_for("device", d["id"]),
                    "device_name": d["hostname"],
                    "hostname": d["hostname"],
                    "username": d["username"],
                    "user_name": users.get(d["user_id"], ""),
                    "os": d["os"],
                    "version": d["version"],
                    "uuid": d["uuid"],
                    "alias": d["alias"],
                    "group_id": d["group_id"],
                    "group_name": groups.get(d["group_id"], ""),
                    "last_online": iso_utc(d["last_online_time"]),
                    "online": (now - int(d["last_online_time"] or 0)) < 180,
                })
            current, size = self._paging()
            page = items[(current - 1) * size: current * size]
            self._send_json(200, {"data": page, "total": len(items)})
        finally:
            conn.close()

    def _users(self, raw):
        # lejianwen 本有 /api/users：不带本服务令牌的请求原样透传
        if self._auth_perms() is None:
            self._proxy_upstream("GET", raw)
            return
        if not self._require("users.view"):
            return
        conn = self._lw()
        if not conn:
            self._send_json(503, {"error": "upstream db unavailable"})
            return
        try:
            q = self._query()
            groups = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM groups")}
            items = []
            for r in conn.execute(
                "SELECT id, username, email, nickname, group_id, is_admin, status FROM users ORDER BY id"
            ):
                uid, username, email, nickname, gid, is_admin, status = r
                if q.get("name") and not _like(str(username), q["name"][0]):
                    continue
                items.append({
                    "guid": guid_for("user", username),
                    "name": username,
                    "email": email,
                    "nickname": nickname,
                    "display_name": nickname,
                    "is_admin": bool(is_admin),
                    "status": status,
                    "group_id": gid,
                    "group": groups.get(gid, ""),
                })
            current, size = self._paging()
            page = items[(current - 1) * size: current * size]
            self._send_json(200, {"data": page, "total": len(items)})
        finally:
            conn.close()

    def _groups(self, table, kind, perm, with_type):
        if not self._require(perm):
            return
        conn = self._lw()
        if not conn:
            self._send_json(503, {"error": "upstream db unavailable"})
            return
        try:
            q = self._query()
            items = []
            cols = "id, name" + (", type" if with_type else "")
            for r in conn.execute(f"SELECT {cols} FROM {table} ORDER BY id"):
                obj = {"guid": guid_for(kind, r[1]), "name": r[1]}
                if with_type:
                    obj["type"] = r[2]
                if q.get("name") and not _like(str(r[1]), q["name"][0]):
                    continue
                items.append(obj)
            current, size = self._paging()
            page = items[(current - 1) * size: current * size]
            self._send_json(200, {"data": page, "total": len(items)})
        finally:
            conn.close()

    def _audits(self, which):
        if not self._require("audits.view"):
            return
        conn = self._lw()
        if not conn:
            self._send_json(503, {"error": "upstream db unavailable"})
            return
        try:
            table = {"conns": "audit_conns", "files": "audit_files", "logins": "login_logs"}[which]
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            current, size = self._paging()
            cur = conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?",
                (size, (current - 1) * size),
            )
            cols = [d[0] for d in cur.description]
            data = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d.pop("password", None)
                data.append(d)
            self._send_json(200, {"data": data, "total": total})
        finally:
            conn.close()

    # ---------- 路由 ----------
    def do_POST(self):
        raw = self._read_body()
        path = self.path.split("?")[0]
        if path == "/api/heartbeat":
            self._handle_heartbeat(raw)
        elif path.startswith("/api/strategies"):
            self._handle_strategies("POST", path, raw)
        elif path == "/api/tokens":
            self._handle_tokens("POST", path, raw)
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self):
        raw = b""
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_strategies("GET", path, raw)
        elif path == "/api/tokens":
            self._handle_tokens("GET", path, raw)
        elif path == "/api/devices":
            self._devices()
        elif path == "/api/users":
            self._users(raw)
        elif path == "/api/user-groups":
            self._groups("groups", "user_group", "user_groups.view", True)
        elif path == "/api/device-groups":
            self._groups("device_groups", "device_group", "device_groups.view", False)
        elif path.startswith("/api/audits/"):
            which = path.rsplit("/", 1)[-1]
            if which in ("conns", "files", "logins"):
                self._audits(which)
            else:
                self._send_json(404, {"error": "not found"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        raw = self._read_body()
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_strategies("PUT", path, raw)
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_strategies("DELETE", path, b"")
        elif path.startswith("/api/tokens/"):
            self._handle_tokens("DELETE", path, b"")
        else:
            self._send_json(404, {"error": "not found"})


def _like(value, pattern):
    """SQL LIKE 语义（% 与 _ 通配），与官方脚本预包 % 的用法一致。"""
    import re
    regex = "^" + re.escape(pattern).replace("%", ".*").replace("_", ".") + "$"
    return re.match(regex, value, re.DOTALL) is not None


if __name__ == "__main__":
    init_db()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"pro-api layer on {LISTEN_HOST}:{LISTEN_PORT}, upstream={UPSTREAM}, db={DB_PATH}")
    srv.serve_forever()
