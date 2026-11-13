#!/usr/bin/env python3
"""
RustDesk 策略下发服务（开源平替 Pro Strategy）

协议依据（均来自开源客户端源码与官方 Pro API 参考脚本，非 Pro 代码）：
- 客户端心跳：POST {api-server}/api/heartbeat
  body: {"id","uuid","ver","conns"?,"modified_at"}（modified_at 为客户端已存的策略时间戳）
  响应 JSON 可含 "strategy": {"config_options": {k: v}} 与顶层 "modified_at"，
  客户端收到后把 config_options 合并进本机配置（客户端内置 BLOCKED 名单，服务器/密钥类选项无法被覆盖）。
  见 rustdesk 客户端 src/hbbs_http/sync.rs。
- 管理 API（对齐官方 res/strategies.py 的调用形状）：
  GET    /api/strategies                列表（数组）
  POST   /api/strategies                新建 {name, config_options, enabled?}
  GET    /api/strategies/{guid}         单个
  PUT    /api/strategies/{guid}         更新 {name?, config_options?, enabled?}
  DELETE /api/strategies/{guid}         删除
  PUT    /api/strategies/{guid}/status  body 为 JSON true/false 启停
  POST   /api/strategies/assign         {strategy: guid|null, peers: [设备ID], users: [...], groups: [...]}
  管理接口需 Authorization: Bearer <STRATEGY_ADMIN_TOKEN>。

部署形态：本服务监听 127.0.0.1:21214，Nginx 把 /api/heartbeat 与 /api/strategies*
转到这里；/api/heartbeat 先原样转发给上游 lejianwen API（127.0.0.1:21114），
再把该设备应得的策略注入响应 JSON，其余响应原样透传。

仅使用 Python 标准库。存储：SQLite。
"""

import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("STRATEGY_UPSTREAM", "http://127.0.0.1:21114")
DB_PATH = os.environ.get("STRATEGY_DB", "/opt/rustdesk-strategy/strategy.db")
ADMIN_TOKEN = os.environ.get("STRATEGY_ADMIN_TOKEN", "")
LISTEN_HOST = os.environ.get("STRATEGY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("STRATEGY_LISTEN_PORT", "21214"))


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
            kind TEXT NOT NULL,          -- peer | user | group
            target TEXT NOT NULL,        -- 设备ID / 用户名 / 组名
            PRIMARY KEY (strategy_guid, kind, target)
        );
        CREATE TABLE IF NOT EXISTS device_state (
            device_id TEXT PRIMARY KEY,
            applied_guid TEXT,
            applied_keys TEXT NOT NULL DEFAULT '[]',
            cleared_at INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()


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
    """返回该设备当前应生效的已启用策略对象（按 peer 直接分配优先）。"""
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

    def log_message(self, fmt, *args):  # 静默，避免日志刷屏
        pass

    # ---------- 基础工具 ----------
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

    def _admin_ok(self):
        if not ADMIN_TOKEN:
            return False
        return self.headers.get("Authorization", "") == f"Bearer {ADMIN_TOKEN}"

    # ---------- 心跳：转发 + 注入 ----------
    def _handle_heartbeat(self, raw):
        # 1) 原样转发给上游 API 服务（lejianwen），保住其设备在线状态等处理
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
            # 上游不可达时不阻断心跳：返回空对象，策略仍可下发
            upstream_body = b"{}"

        # 2) 解析客户端请求，找设备与策略时间戳
        try:
            req_obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            req_obj = {}
        device_id = str(req_obj.get("id", ""))
        client_ts = req_obj.get("modified_at", 0)
        try:
            client_ts = int(client_ts)
        except Exception:
            client_ts = 0

        try:
            resp_obj = json.loads(upstream_body.decode("utf-8") or "{}")
            if not isinstance(resp_obj, dict):
                raise ValueError("not an object")
        except Exception:
            # 上游响应不是 JSON 对象时原样透传，不注入
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
                    # 无策略但此前下发过：回空值清除此前下发的键（一次性）
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

    # ---------- 管理 API ----------
    def _handle_admin(self, method, path, raw):
        if not self._admin_ok():
            self._send_json(401, {"error": "unauthorized"})
            return
        conn = db()
        try:
            parts = [p for p in path.split("/") if p]  # ['api','strategies',...]
            now = int(time.time())

            if method == "GET" and path == "/api/strategies":
                self._send_json(200, list_strategies(conn))
                return

            if method == "POST" and path == "/api/strategies":
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
                data = json.loads(raw.decode("utf-8") or "{}")
                sguid = data.get("strategy")  # None 表示取消分配
                if sguid:
                    # 兼容按名称分配
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
                        # 先清掉该目标在其它策略上的同类分配（一目标一策略）
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
                    rows = conn.execute(
                        "SELECT kind,target FROM assignments WHERE strategy_guid=?", (guid,)
                    ).fetchall()
                    obj["assignments"] = [
                        {"kind": r["kind"], "target": r["target"]} for r in rows
                    ]
                    self._send_json(200, obj)
                    return
                if method == "PUT":
                    data = json.loads(raw.decode("utf-8") or "{}")
                    name = data.get("name", obj["name"])
                    opts = data.get("config_options", obj["config_options"])
                    enabled = data.get("enabled", obj["enabled"])
                    conn.execute(
                        "UPDATE strategies SET name=?, config_options=?, enabled=?, modified_at=? WHERE guid=?",
                        (name, json.dumps(opts), 1 if enabled else 0, now, guid),
                    )
                    conn.commit()
                    self._send_json(200, get_strategy(conn, guid))
                    return
                if method == "DELETE":
                    conn.execute("DELETE FROM strategies WHERE guid=?", (guid,))
                    conn.execute("DELETE FROM assignments WHERE strategy_guid=?", (guid,))
                    conn.commit()
                    self._send_json(200, {"ok": True})
                    return

            if len(parts) == 4 and parts[:2] == ["api", "strategies"] and parts[3] == "status" and method == "PUT":
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

    # ---------- 路由 ----------
    def do_POST(self):
        raw = self._read_body()
        path = self.path.split("?")[0]
        if path == "/api/heartbeat":
            self._handle_heartbeat(raw)
        elif path.startswith("/api/strategies"):
            self._handle_admin("POST", path, raw)
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_admin("GET", path, b"")
        else:
            self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        raw = self._read_body()
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_admin("PUT", path, raw)
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/strategies"):
            self._handle_admin("DELETE", path, b"")
        else:
            self._send_json(404, {"error": "not found"})


if __name__ == "__main__":
    init_db()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"strategy service on {LISTEN_HOST}:{LISTEN_PORT}, upstream={UPSTREAM}, db={DB_PATH}")
    srv.serve_forever()
