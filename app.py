# -*- coding: utf-8 -*-
"""
-80℃ 冰箱菌种管理台 · 云端后端
================================
- 数据模型与前端 JSON 结构完全一致（racks -> boxes -> wells）
- 多实验室隔离：每个实验室一个独立 SQLite 文件（data/labs/<lab_id>/freezer.db），
  互不可见；实验室名 + 密码（PBKDF2-SHA256 加盐哈希）准入，令牌或 IP 绑定鉴权
- SQLite 单实验室库存取，rev 递增号实现乐观并发控制
- 每次保存自动落一份快照到该实验室 backups/，最多保留 200 份
- GET  /api/session        查询当前会话（令牌/IP 自动加入）
- GET  /api/labs           列出所有实验室名（公开，供加入时选择）
- POST /api/labs/create    创建实验室 {name, password} -> {token, lab}
- POST /api/labs/join      加入实验室 {name, password} -> {token, lab}
- POST /api/labs/claim     认领旧版单库数据 {id, name, password}
- POST /api/labs/leave     离开（解除本 IP 绑定，令牌失效）
- GET  /api/labs/bracelet-key  查询本实验室手环进门码（无则返回 null）
- POST /api/labs/bracelet-key  生成/轮换进门码 {rotate?}（烧进 NFC 手环，碰环免密进入）
- POST /api/labs/bracelet-enter 凭进门码进入 {bkey} -> {token, lab}（公开接口）
- GET  /api/db             读取当前实验室最新数据 {rev, db}
- PUT  /api/db             保存数据（带版本校验，冲突返回 409 + 服务器最新数据）
- POST /api/feedback       提交问题反馈 {issue 必填, idea?, user?}（X-Lab-Token 鉴权）
- GET  /api/feedback/mine  本实验室未读的工程师回复（进入页面弹窗提醒用）
- POST /api/feedback/read  标记回复已读 {ids}
- POST /api/engineer/login 工程师界面登录 {pw} -> {token}（密码来自 ENGINEER_PASS 环境变量）
- GET  /api/engineer/feedback    全部反馈列表（X-Eng-Token）
- POST /api/engineer/feedback/reply  回复反馈 {id, reply}（用户下次进入弹窗收到）
- POST /api/engineer/feedback/close  复选框批量标记已处理 {ids}
- GET/POST /api/engineer/email       读取/保存提醒邮箱（SMTP 凭据走环境变量，一天最多一封提醒）
- GET  /api/health    健康检查
- 其余路径托管 static/ 下的前端页面

启动：uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import datetime
import smtplib
import xml.etree.ElementTree as ET
import zipfile
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import openpyxl
except ImportError:  # pragma: no cover - openpyxl 是部署必备依赖
    openpyxl = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FREEZER_DATA", BASE_DIR / "data"))
REGISTRY_PATH = DATA_DIR / "registry.db"
LEGACY_DB_PATH = DATA_DIR / "freezer.db"
LEGACY_BACKUP_DIR = DATA_DIR / "backups"
LABS_DIR = DATA_DIR / "labs"
BACKUP_KEEP = 200
PBKDF2_ROUNDS = 150_000

_lock = threading.Lock()

app = FastAPI(title="-80C Freezer Inventory", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- registry
def _reg() -> sqlite3.Connection:
    """实验室注册表（库名/密码哈希、IP 绑定、登录令牌）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(REGISTRY_PATH, check_same_thread=False)
    c.execute(
        "CREATE TABLE IF NOT EXISTS labs ("
        "id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, "
        "salt TEXT, pw_hash TEXT, created_at REAL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS ip_binds ("
        "ip TEXT PRIMARY KEY, lab_id TEXT NOT NULL, updated_at REAL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS tokens ("
        "token TEXT PRIMARY KEY, lab_id TEXT NOT NULL, created_at REAL)"
    )
    # 手环进门码：每实验室一个随机码，烧进 NFC 手环网址（?bk=xxx），
    # 碰环即可换发正式登录令牌、免输实验室密码；轮换后旧码立即作废（挂失用）
    c.execute(
        "CREATE TABLE IF NOT EXISTS bracelet_keys ("
        "lab_id TEXT PRIMARY KEY, bkey TEXT UNIQUE NOT NULL, created_at REAL)"
    )
    # 取菌搜索历史：按「客户端 IP + 实验室」隔离，同 IP 同实验室共享、不同 IP 各自独立；
    # 同词 upsert 更新时间，超过上限自动淘汰最旧条目。
    c.execute(
        "CREATE TABLE IF NOT EXISTS search_history ("
        "ip TEXT NOT NULL, lab_id TEXT NOT NULL, query TEXT NOT NULL, created_at REAL NOT NULL, "
        "PRIMARY KEY (ip, lab_id, query))"
    )
    # 问题反馈：跨实验室全局（工程师统一查看），实验室解散不影响已提交记录；
    # status: open 待处理 / replied 已回复 / closed 已标记处理；user_read: 回复后用户是否已读
    c.execute(
        "CREATE TABLE IF NOT EXISTS feedback ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "lab_id TEXT, lab_name TEXT, user TEXT, "
        "issue TEXT NOT NULL, idea TEXT DEFAULT '', created_at TEXT, "
        "status TEXT DEFAULT 'open', reply TEXT DEFAULT '', replied_at TEXT, "
        "user_read INTEGER DEFAULT 0)"
    )
    # 主库键值对（工程师提醒邮箱、上次发信日期等全局配置）
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _issue_token(lab_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock, _reg() as c:
        c.execute(
            "INSERT INTO tokens(token, lab_id, created_at) VALUES(?,?,?)",
            (token, lab_id, time.time()),
        )
    return token


def _bind_ip(ip: str, lab_id: str) -> None:
    with _lock, _reg() as c:
        c.execute(
            "INSERT INTO ip_binds(ip, lab_id, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET lab_id=excluded.lab_id, updated_at=excluded.updated_at",
            (ip, lab_id, time.time()),
        )


def _auth(request: Request) -> Optional[dict]:
    """鉴权：先认自定义头令牌，再认 IP 绑定。返回 {id,name} 或 None。

    注意：不能用标准 Authorization 头——ModelScope/阿里云平台网关会占用该头
    （带 Bearer 的请求在网关层直接 403「不支持通过 SDK Token 直接访问」），
    故令牌统一走 X-Lab-Token 自定义请求头。
    """
    token = (request.headers.get("x-lab-token", "") or "").strip()
    if token:
        with _reg() as c:
            row = c.execute(
                "SELECT l.id, l.name FROM tokens t JOIN labs l ON l.id = t.lab_id "
                "WHERE t.token = ?",
                (token,),
            ).fetchone()
        if row:
            return {"id": row[0], "name": row[1], "token": token}
    ip = _client_ip(request)
    with _reg() as c:
        row = c.execute(
            "SELECT l.id, l.name FROM ip_binds b JOIN labs l ON l.id = b.lab_id "
            "WHERE b.ip = ?",
            (ip,),
        ).fetchone()
    if row:
        return {"id": row[0], "name": row[1], "token": ""}
    return None


def _require_lab(request: Request) -> dict:
    lab = _auth(request)
    if lab is None:
        raise HTTPException(status_code=401, detail="请先创建或加入实验室")
    return lab


# ---------------------------------------------------------------- per-lab storage
def _lab_dir(lab_id: str) -> Path:
    return LABS_DIR / lab_id


def _connect_lab(lab_id: str) -> sqlite3.Connection:
    d = _lab_dir(lab_id)
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / "freezer.db", check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _load_doc(lab_id: str) -> Optional[dict]:
    db_path = _lab_dir(lab_id) / "freezer.db"
    if not db_path.exists():
        return None
    with _lock, _connect_lab(lab_id) as c:
        row = c.execute("SELECT v FROM kv WHERE k='doc'").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _save_doc(lab_id: str, doc: dict) -> None:
    with _lock, _connect_lab(lab_id) as c:
        c.execute(
            "INSERT INTO kv(k, v) VALUES('doc', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (json.dumps(doc, ensure_ascii=False),),
        )


def _write_backup(lab_id: str, doc: dict) -> None:
    """每次成功保存后落一份快照，防止误操作/覆盖导致数据丢失。"""
    try:
        bdir = _lab_dir(lab_id) / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = bdir / f"freezer-{stamp}-r{doc['rev']}.json"
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        olds = sorted(bdir.glob("freezer-*.json"))
        for stale in olds[:-BACKUP_KEEP]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:
        pass  # 备份失败不影响主流程


# ---------------------------------------------------------------- legacy migration
def _migrate_legacy() -> None:
    """旧版单库 freezer.db 迁移为待认领实验室「默认实验室」（首次访问者设置密码）。"""
    with _lock:
        with _reg() as c:
            n = c.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
        if n or not LEGACY_DB_PATH.exists():
            return
        lab_id = "legacy"
        d = _lab_dir(lab_id)
        d.mkdir(parents=True, exist_ok=True)
        shutil.move(str(LEGACY_DB_PATH), str(d / "freezer.db"))
        if LEGACY_BACKUP_DIR.exists():
            try:
                shutil.move(str(LEGACY_BACKUP_DIR), str(d / "backups"))
            except OSError:
                pass
        with _reg() as c:
            c.execute(
                "INSERT INTO labs(id, name, salt, pw_hash, created_at) VALUES(?,?,?,?,?)",
                (lab_id, "默认实验室", None, None, time.time()),
            )


_migrate_legacy()


# ---------------------------------------------------------------- models
class SaveBody(BaseModel):
    rev: Optional[int] = None
    force: bool = False
    db: dict


class LabBody(BaseModel):
    name: str = ""
    password: str = ""


class ClaimBody(BaseModel):
    id: str
    name: str = ""
    password: str = ""


class BraceletBody(BaseModel):
    bkey: str = ""
    rotate: bool = False


class SearchHistoryBody(BaseModel):
    query: str = ""


class FeedbackBody(BaseModel):
    issue: str = ""
    idea: str = ""
    user: str = ""


class FeedbackReadBody(BaseModel):
    ids: list = []


class EngineerLoginBody(BaseModel):
    pw: str = ""


class EngineerReplyBody(BaseModel):
    id: int
    reply: str = ""


class EngineerCloseBody(BaseModel):
    ids: list = []


class EngineerEmailBody(BaseModel):
    email: str = ""


# ---------------------------------------------------------------- lab routes
@app.get("/api/health")
def health() -> dict:
    with _reg() as c:
        n = c.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
    return {"ok": True, "labs": n}


@app.get("/api/session")
def get_session(request: Request) -> dict:
    """当前浏览器/IP 是否已属于某个实验室；未登录时顺带返回待认领的旧库。"""
    lab = _auth(request)
    if lab:
        return {"lab": {"id": lab["id"], "name": lab["name"]}}
    with _reg() as c:
        row = c.execute(
            "SELECT id, name FROM labs WHERE pw_hash IS NULL"
        ).fetchone()
    claim = {"id": row[0], "name": row[1]} if row else None
    return {"lab": None, "claim": claim}


@app.get("/api/labs")
def list_labs() -> dict:
    """公开列出实验室名（加入时让用户点选）；不泄露任何其他信息。"""
    with _reg() as c:
        rows = c.execute(
            "SELECT name FROM labs WHERE pw_hash IS NOT NULL ORDER BY created_at"
        ).fetchall()
    return {"labs": [r[0] for r in rows]}


def _admit(lab_row, request: Request) -> dict:
    ip = _client_ip(request)
    _bind_ip(ip, lab_row[0])
    token = _issue_token(lab_row[0])
    return {"token": token, "lab": {"id": lab_row[0], "name": lab_row[1]}}


@app.post("/api/labs/create")
def create_lab(body: LabBody, request: Request) -> Any:
    name = (body.name or "").strip()
    pw = body.password or ""
    if not name:
        raise HTTPException(status_code=400, detail="请填写实验室名称")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="实验室名称最长 40 个字")
    if len(pw) < 4:
        raise HTTPException(status_code=400, detail="密码至少 4 位")
    lab_id = "lab_" + secrets.token_hex(6)
    salt = secrets.token_hex(16)
    try:
        with _lock, _reg() as c:
            exists = c.execute("SELECT 1 FROM labs WHERE name = ?", (name,)).fetchone()
            if exists:
                raise HTTPException(status_code=409, detail="实验室名称已存在，请换一个或直接加入")
            c.execute(
                "INSERT INTO labs(id, name, salt, pw_hash, created_at) VALUES(?,?,?,?,?)",
                (lab_id, name, salt, _hash_pw(pw, salt), time.time()),
            )
    except HTTPException:
        raise
    # 预建实验室目录（空库；前端首次 GET 404 后会写入种子数据）
    _lab_dir(lab_id).mkdir(parents=True, exist_ok=True)
    with _reg() as c:
        row = c.execute("SELECT id, name FROM labs WHERE id = ?", (lab_id,)).fetchone()
    return _admit(row, request)


@app.post("/api/labs/join")
def join_lab(body: LabBody, request: Request) -> Any:
    name = (body.name or "").strip()
    pw = body.password or ""
    if not name or not pw:
        raise HTTPException(status_code=400, detail="请填写实验室名称和密码")
    with _reg() as c:
        row = c.execute(
            "SELECT id, name, salt, pw_hash FROM labs WHERE name = ?", (name,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="没有这个实验室，请检查名称（可点列表查看现有实验室）")
    lab_id, lab_name, salt, pw_hash = row
    if pw_hash is None:
        raise HTTPException(status_code=403, detail="该实验室的数据尚未被认领，请先设置密码")
    if not secrets.compare_digest(pw_hash, _hash_pw(pw, salt)):
        raise HTTPException(status_code=403, detail="密码不正确")
    return _admit((lab_id, lab_name), request)


@app.post("/api/labs/claim")
def claim_lab(body: ClaimBody, request: Request) -> Any:
    """认领旧版迁移过来的无密码实验室：只能认领一次。"""
    name = (body.name or "").strip() or "默认实验室"
    pw = body.password or ""
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="实验室名称最长 40 个字")
    if len(pw) < 4:
        raise HTTPException(status_code=400, detail="密码至少 4 位")
    salt = secrets.token_hex(16)
    with _lock, _reg() as c:
        row = c.execute(
            "SELECT id, name FROM labs WHERE id = ? AND pw_hash IS NULL", (body.id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="没有待认领的实验室（可能已被认领）")
        dup = c.execute("SELECT 1 FROM labs WHERE name = ? AND id <> ?", (name, body.id)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="实验室名称已存在，请换一个")
        c.execute(
            "UPDATE labs SET name = ?, salt = ?, pw_hash = ? WHERE id = ?",
            (name, salt, _hash_pw(pw, salt), body.id),
        )
    return _admit((body.id, name), request)


@app.post("/api/labs/leave")
def leave_lab(request: Request) -> dict:
    """解除当前 IP 的自动加入并作废本次令牌（切换实验室用）。"""
    lab = _auth(request)
    ip = _client_ip(request)
    with _lock, _reg() as c:
        c.execute("DELETE FROM ip_binds WHERE ip = ?", (ip,))
        if lab and lab.get("token"):
            c.execute("DELETE FROM tokens WHERE token = ?", (lab["token"],))
    return {"ok": True}


@app.get("/api/labs/bracelet-key")
def get_bracelet_key(request: Request) -> dict:
    """查询本实验室当前的手环进门码（未生成时 bkey 为 null）。"""
    lab = _require_lab(request)
    with _reg() as c:
        row = c.execute(
            "SELECT bkey, created_at FROM bracelet_keys WHERE lab_id = ?",
            (lab["id"],),
        ).fetchone()
    if not row:
        return {"bkey": None, "created_at": None}
    return {"bkey": row[0], "created_at": row[1]}


@app.post("/api/labs/bracelet-key")
def upsert_bracelet_key(body: BraceletBody, request: Request) -> dict:
    """生成进门码；rotate=true 时强制换码（旧码立刻失效，用于手环挂失/人员离开）。"""
    lab = _require_lab(request)
    with _lock, _reg() as c:
        row = c.execute(
            "SELECT bkey FROM bracelet_keys WHERE lab_id = ?", (lab["id"],)
        ).fetchone()
        if row and not body.rotate:
            return {"bkey": row[0], "created": False}
        bkey = secrets.token_urlsafe(18)  # ~144bit 熵，无法枚举；URL 安全字符
        c.execute(
            "INSERT INTO bracelet_keys(lab_id, bkey, created_at) VALUES(?,?,?) "
            "ON CONFLICT(lab_id) DO UPDATE SET bkey=excluded.bkey, created_at=excluded.created_at",
            (lab["id"], bkey, time.time()),
        )
    return {"bkey": bkey, "created": True}


@app.post("/api/labs/bracelet-enter")
def bracelet_enter(body: BraceletBody, request: Request) -> Any:
    """公开接口：凭手环网址里的进门码换发正式登录令牌（并绑定当前 IP）。

    进门码只解决「碰环免密进入」，不是实验室密码：解散实验室等敏感操作仍需密码。
    """
    code = (body.bkey or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少进门码")
    with _reg() as c:
        row = c.execute(
            "SELECT l.id, l.name FROM bracelet_keys bk "
            "JOIN labs l ON l.id = bk.lab_id WHERE bk.bkey = ?",
            (code,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="进门码无效或已挂失，请联系管理员重新烧录手环")
    return _admit(row, request)


@app.post("/api/labs/dissolve")
def dissolve_lab(body: LabBody, request: Request) -> dict:
    """解散当前实验室（需再次输入密码）：删除注册表记录、全部令牌/IP 绑定与数据目录。"""
    lab = _require_lab(request)
    pw = body.password or ""
    with _reg() as c:
        row = c.execute("SELECT salt, pw_hash FROM labs WHERE id = ?", (lab["id"],)).fetchone()
    if not row or not row[1]:
        raise HTTPException(status_code=404, detail="实验室不存在")
    salt, pw_hash = row
    if not secrets.compare_digest(pw_hash, _hash_pw(pw, salt)):
        raise HTTPException(status_code=403, detail="密码不正确")
    with _lock, _reg() as c:
        c.execute("DELETE FROM tokens WHERE lab_id = ?", (lab["id"],))
        c.execute("DELETE FROM ip_binds WHERE lab_id = ?", (lab["id"],))
        c.execute("DELETE FROM bracelet_keys WHERE lab_id = ?", (lab["id"],))
        c.execute("DELETE FROM labs WHERE id = ?", (lab["id"],))
    shutil.rmtree(_lab_dir(lab["id"]), ignore_errors=True)
    return {"ok": True}


# ---------------------------------------------------------------- data routes
@app.get("/api/db")
def get_db(request: Request) -> dict:
    lab = _require_lab(request)
    doc = _load_doc(lab["id"])
    if doc is None:
        # 尚未初始化：前端收到 404 后会用种子数据 POST/PUT 上来
        raise HTTPException(status_code=404, detail="数据库为空，等待前端初始化")
    return doc


@app.put("/api/db")
def put_db(request: Request, body: SaveBody = Body(...)) -> Any:
    lab = _require_lab(request)
    lab_id = lab["id"]
    cur = _load_doc(lab_id)
    cur_rev = cur["rev"] if cur else 0

    # 乐观并发校验：rev 不一致且未强制覆盖 -> 409 + 服务器当前数据
    if cur is not None and body.rev != cur_rev and not body.force:
        return JSONResponse(
            status_code=409,
            content={
                "rev": cur_rev,
                "db": cur["db"],
                "msg": "云端数据已被他人更新，请选择覆盖或加载最新版本",
            },
        )

    next_doc = {"rev": cur_rev + 1, "db": body.db}
    _save_doc(lab_id, next_doc)
    _write_backup(lab_id, next_doc)
    return {"rev": next_doc["rev"]}


# ---------------------------------------------------------------- excel 多 sheet 解析
# 表头别名（中英文），与前端 BULK_ALIASES 保持一致
_EXCEL_HEADERS = {
    "pos": ["孔位", "位置", "position", "pos", "well", "坐标"],
    "strain": ["菌种", "菌株", "strain"],
    "plasmid": ["质粒", "plasmid"],
    "keeper": ["保存人", "操作人", "keeper", "operator"],
    "date": ["日期", "保存日期", "存入日期", "date", "stored date"],
    "color": ["类型", "保菌管类型", "管型", "管盖颜色", "颜色", "type", "tube type", "color", "cap color"],
    "note": ["备注", "说明", "note", "remark"],
}


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            return str(int(v))
        return str(v)
    s = str(v).strip()
    if s == "空":
        return ""  # 质粒"空"=无质粒
    return s


def _date_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if s in ("", "？", "?"):
        return ""
    if re.fullmatch(r"\d{3,4}", s):  # 纯年份 -> YYYY-01-01
        return s + "-01-01"
    return s


def _parse_col_label(s: str) -> Optional[int]:
    """A->0, B->1, ..., Z->25, AA->26 ..."""
    s = s.strip().upper()
    if not s or not s.isalpha():
        return None
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_label_name(idx: int) -> str:
    """0->A, 25->Z, 26->AA（_parse_col_label 的逆函数）。"""
    s = ""
    idx = int(idx)
    while idx >= 0:
        s = chr(65 + idx % 26) + s
        idx = idx // 26 - 1
    return s


def _parse_position(s: str):
    s = str(s or "").strip().upper()
    m = re.match(r"^([A-Z]+)0*(\d+)$", s)
    if not m:
        return None
    col = _parse_col_label(m[1])
    row = int(m[2]) - 1
    if col is None or row < 0:
        return None
    return row, col


def _parse_sheet_rows(title: str, rows: list) -> dict:
    """通用：按表头别名识别列，产出 {name, rows, cols, wells}。
    rows 元素为一行单元格（None/str/int/float/datetime），openpyxl 与内置解析器共用。"""
    if not rows:
        return {"name": title, "rows": 0, "cols": 0, "wells": []}

    # 识别表头：第一行是否包含已知列名
    first = [str(c or "").strip() for c in rows[0]]
    col_idx = {}
    has_header = False
    for key, aliases in _EXCEL_HEADERS.items():
        for i, h in enumerate(first):
            if h.lower() in [a.lower() for a in aliases]:
                col_idx[key] = i
                has_header = True
                break
    if not has_header or "pos" not in col_idx:
        col_idx = {"pos": 0, "strain": 1, "plasmid": 2, "keeper": 3, "date": 4, "color": 5, "note": 6}
        data_rows = rows
    else:
        data_rows = rows[1:]

    wells = []
    max_row = 0
    max_col = 0
    for r in data_rows:
        if not r:
            continue
        pos_raw = r[col_idx["pos"]] if col_idx["pos"] < len(r) else None
        if pos_raw is None:
            continue
        parsed = _parse_position(str(pos_raw))
        if parsed is None:
            continue
        row_idx, col_idx_pos = parsed
        strain = _cell_str(r[col_idx["strain"]] if col_idx["strain"] < len(r) else None)
        plasmid = _cell_str(r[col_idx["plasmid"]] if col_idx["plasmid"] < len(r) else None)
        # 菌名+质粒都空则跳过
        if not strain and not plasmid:
            continue
        keeper = _cell_str(r[col_idx["keeper"]] if col_idx["keeper"] < len(r) else None)
        date = _date_str(r[col_idx["date"]] if col_idx["date"] < len(r) else None)
        color = _cell_str(r[col_idx["color"]] if col_idx["color"] < len(r) else None)
        note = _cell_str(r[col_idx["note"]] if col_idx["note"] < len(r) else None)
        wells.append({
            "pos": _col_label_name(col_idx_pos) + str(row_idx + 1),
            "strain": strain,
            "plasmid": plasmid,
            "keeper": keeper,
            "date": date,
            "color": color,
            "note": note,
        })
        max_row = max(max_row, row_idx + 1)
        max_col = max(max_col, col_idx_pos + 1)

    return {"name": title, "rows": max_row, "cols": max_col, "wells": wells}


def _parse_sheet(ws) -> dict:
    """openpyxl 工作表适配层。"""
    return _parse_sheet_rows(ws.title, list(ws.iter_rows(values_only=True)))


# ---------------------------------------------------------------- 无 openpyxl 时的内置 xlsx 解析器
_XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# Excel 内置日期/时间格式 numFmtId
_BUILTIN_DATE_FMTS = {
    14, 15, 16, 17, 18, 19, 20, 21, 22,
    27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
    45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58,
}


def _xlsx_serial_to_dt(serial: float, date1904: bool) -> datetime.datetime:
    """Excel 日期序列号 -> datetime（Windows 1900 体系以 1899-12-30 为零点）。"""
    base = datetime.datetime(1904, 1, 1) if date1904 else datetime.datetime(1899, 12, 30)
    return base + datetime.timedelta(days=float(serial))


def _xlsx_is_date_fmt(code: str) -> bool:
    """判断自定义 numFmt 的格式码是否表示日期/时间（去掉颜色/区域/字面量后看日期字母）。"""
    s = re.sub(r"\[[^\]]*\]", "", code or "")   # [Red]、[$-409] 等
    s = re.sub(r'"[^"]*"', "", s)               # 引号包裹的字面文本
    s = re.sub(r"\\.", "", s).lower()           # 反斜杠转义字符
    return bool(re.search(r"[ymdhs]", s))


def _xlsx_stdlib(raw: bytes) -> list:
    """纯标准库解析 .xlsx（zip+XML）。返回 [(sheet名, 行列表)]。
    行为尽量贴近 openpyxl iter_rows(values_only=True)：空单元格补 None、
    共享/内联字符串还原、日期序列号转 datetime、整数去 .0。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())

        # 1) 共享字符串表（含富文本拼接）
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{{{_XLSX_MAIN}}}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_XLSX_MAIN}}}t")))

        # 2) 样式表：找出"日期型"单元格样式序号
        date_xfs: set[int] = set()
        custom_date_ids: set[int] = set()
        if "xl/styles.xml" in names:
            st = ET.fromstring(zf.read("xl/styles.xml"))
            fmts = st.find(f"{{{_XLSX_MAIN}}}numFmts")
            if fmts is not None:
                for nf in fmts.findall(f"{{{_XLSX_MAIN}}}numFmt"):
                    fid = int(nf.get("numFmtId", 0))
                    if _xlsx_is_date_fmt(nf.get("formatCode", "")):
                        custom_date_ids.add(fid)
            xfs = st.find(f"{{{_XLSX_MAIN}}}cellXfs")
            if xfs is not None:
                for i, xf in enumerate(xfs.findall(f"{{{_XLSX_MAIN}}}xf")):
                    fid = int(xf.get("numFmtId", 0))
                    if fid in _BUILTIN_DATE_FMTS or fid in custom_date_ids:
                        date_xfs.add(i)

        # 3) 工作簿：日期系统 + sheet 顺序/名称/r:id -> 文件路径
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        wb_pr = wb.find(f"{{{_XLSX_MAIN}}}workbookPr")
        date1904 = wb_pr is not None and wb_pr.get("date1904") in ("1", "true")
        rid_to_target: dict[str, str] = {}
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            rid_to_target[rel.get("Id", "")] = rel.get("Target", "")
        sheets_meta = []
        sheets_el = wb.find(f"{{{_XLSX_MAIN}}}sheets")
        if sheets_el is not None:
            for sh in sheets_el.findall(f"{{{_XLSX_MAIN}}}sheet"):
                rid = sh.get(f"{{{_XLSX_REL}}}id", "")
                target = rid_to_target.get(rid, "")
                path = target.lstrip("/") if target.startswith("/") else "xl/" + target
                sheets_meta.append((sh.get("name", ""), path))

        # 4) 逐 sheet 读行
        out = []
        for name, path in sheets_meta:
            if path not in names:
                continue
            root = ET.fromstring(zf.read(path))
            sd = root.find(f"{{{_XLSX_MAIN}}}sheetData")
            row_map: dict[int, list] = {}
            max_row = -1
            fallback_r = 0
            for row_el in (sd.findall(f"{{{_XLSX_MAIN}}}row") if sd is not None else []):
                try:
                    rnum = int(row_el.get("r", "")) - 1
                except ValueError:
                    rnum = fallback_r
                fallback_r += 1
                vals: dict[int, Any] = {}
                max_col = -1
                for c in row_el.findall(f"{{{_XLSX_MAIN}}}c"):
                    cm = re.match(r"^([A-Z]+)", c.get("r", "").upper())
                    ci = _parse_col_label(cm.group(1)) if cm else None
                    if ci is None:
                        continue
                    ctype = c.get("t")
                    v_el = c.find(f"{{{_XLSX_MAIN}}}v")
                    val: Any = None
                    if ctype == "s":  # 共享字符串
                        if v_el is not None and v_el.text is not None:
                            idx = int(v_el.text)
                            val = shared[idx] if 0 <= idx < len(shared) else ""
                    elif ctype == "inlineStr":
                        is_el = c.find(f"{{{_XLSX_MAIN}}}is")
                        if is_el is not None:
                            val = "".join(t.text or "" for t in is_el.iter(f"{{{_XLSX_MAIN}}}t"))
                    elif ctype == "b":
                        val = (v_el is not None and v_el.text == "1")
                    elif ctype in ("str", "e"):
                        val = v_el.text if v_el is not None else None
                    elif v_el is not None and v_el.text is not None:
                        txt = v_el.text
                        try:
                            num = float(txt)
                            sidx = int(c.get("s", -1))
                            if sidx in date_xfs:
                                val = _xlsx_serial_to_dt(num, date1904)
                            elif num.is_integer():
                                val = int(num)
                            else:
                                val = num
                        except ValueError:
                            val = txt
                    vals[ci] = val
                    max_col = max(max_col, ci)
                row_map[rnum] = [vals.get(i) for i in range(max_col + 1)]
                max_row = max(max_row, rnum)
            rows = [row_map.get(i, []) for i in range(max_row + 1)]
            out.append((name, rows))
        return out


@app.post("/api/parse-excel")
def parse_excel(body: dict = Body(...)) -> Any:
    """解析上传的 Excel（.xlsx），每个 sheet 作为一个盒子，自动识别行列数。
    入参：{data: base64字符串, name: 文件名}
    返回：{sheets: [{name, rows, cols, wells:[{pos,strain,plasmid,keeper,date,color,note}]}]}
    openpyxl 可用时优先使用；不可用时自动降级为内置标准库（zipfile+XML）解析器。"""
    data = body.get("data")
    if not data:
        raise HTTPException(status_code=400, detail="缺少文件内容")
    try:
        raw = base64.b64decode(data)
    except Exception:
        raise HTTPException(status_code=400, detail="文件编码错误")
    try:
        if openpyxl is not None:
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            sheets = [_parse_sheet(ws) for ws in wb.worksheets]
        else:
            # 部署环境缺少 openpyxl 时的兜底：.xlsx 本质是 zip+XML，标准库即可解析
            sheets = [_parse_sheet_rows(title, rows) for title, rows in _xlsx_stdlib(raw)]
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail="无法解析 Excel 文件（仅支持标准 .xlsx 格式，旧版 .xls 请先另存为 .xlsx）：" + str(e),
        )
    except Exception as e:  # noqa: BLE001 - 给前端可读错误，不抛 500
        raise HTTPException(status_code=400, detail="无法解析 Excel 文件：" + str(e))
    return {"sheets": sheets}


# ---------------------------------------------------------------- search history（按 IP + 实验室隔离）
_SEARCH_HISTORY_LIMIT = 15


@app.get("/api/search-history")
def get_search_history(request: Request) -> dict:
    """取当前「客户端 IP + 实验室」的搜索历史（最近 15 条，按时间倒序）。"""
    lab = _require_lab(request)
    ip = _client_ip(request)
    with _reg() as c:
        rows = c.execute(
            "SELECT query FROM search_history "
            "WHERE ip = ? AND lab_id = ? ORDER BY created_at DESC LIMIT ?",
            (ip, lab["id"], _SEARCH_HISTORY_LIMIT),
        ).fetchall()
    return {"queries": [r[0] for r in rows]}


@app.post("/api/search-history")
def add_search_history(body: SearchHistoryBody, request: Request) -> dict:
    """写入一条搜索词（空串忽略）；同词 upsert 更新时间，超上限淘汰最旧。"""
    lab = _require_lab(request)
    ip = _client_ip(request)
    q = (body.query or "").strip()
    if not q:
        return get_search_history(request)
    now = time.time()
    with _lock, _reg() as c:
        c.execute(
            "INSERT INTO search_history(ip, lab_id, query, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(ip, lab_id, query) DO UPDATE SET created_at=excluded.created_at",
            (ip, lab["id"], q, now),
        )
        # 超过上限则删除最旧的条目（保留最新 N 条）
        c.execute(
            "DELETE FROM search_history WHERE ip = ? AND lab_id = ? AND query NOT IN ("
            "SELECT query FROM search_history WHERE ip = ? AND lab_id = ? "
            "ORDER BY created_at DESC LIMIT ?)",
            (ip, lab["id"], ip, lab["id"], _SEARCH_HISTORY_LIMIT),
        )
    return get_search_history(request)


@app.delete("/api/search-history")
def clear_search_history(request: Request) -> dict:
    """清空当前「客户端 IP + 实验室」的搜索历史。"""
    lab = _require_lab(request)
    ip = _client_ip(request)
    with _lock, _reg() as c:
        c.execute(
            "DELETE FROM search_history WHERE ip = ? AND lab_id = ?",
            (ip, lab["id"]),
        )
    return {"queries": []}


# ---------------------------------------------------------------- feedback / engineer
# 问题反馈系统：实验室用户在设置里提交「遇到的问题/想法」；
# 工程师凭密码进入工程师界面查看全部反馈、填写提醒邮箱并逐条回复。
# 回复后反馈置为 replied，该实验室用户下次进入页面时弹窗收到回复。
# 邮件提醒：存在未处理反馈时每个自然日最多发一封汇总（QQ 邮箱 SMTP，凭据走环境变量）。
_ENGINEER_SESSION_TTL = 8 * 3600.0
_engineer_tokens: dict = {}  # token -> 过期时间戳（单实例内存态，重启后需重新登录）


def _engineer_pass() -> str:
    return (os.environ.get("ENGINEER_PASS", "") or "").strip() or "daylab2026"


def _smtp_conf() -> Optional[dict]:
    user = (os.environ.get("SMTP_USER", "") or "").strip()
    pwd = (os.environ.get("SMTP_PASS", "") or "").strip()
    if not user or not pwd:
        return None
    try:
        port = int((os.environ.get("SMTP_PORT", "") or "").strip() or "465")
    except ValueError:
        port = 465
    return {
        "host": (os.environ.get("SMTP_HOST", "") or "").strip() or "smtp.qq.com",
        "port": port,
        "user": user,
        "pwd": pwd,
        "frm": (os.environ.get("SMTP_FROM", "") or "").strip() or user,
    }


def _kv_get(c: sqlite3.Connection, key: str) -> Optional[str]:
    row = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


def _kv_set(c: sqlite3.Connection, key: str, val: str) -> None:
    c.execute(
        "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, val),
    )


def _require_engineer(request: Request) -> None:
    tok = (request.headers.get("x-eng-token", "") or "").strip()
    exp = _engineer_tokens.get(tok)
    if not tok or exp is None or exp < time.time():
        raise HTTPException(status_code=401, detail="工程师登录已过期，请重新输入密码")
    _engineer_tokens[tok] = time.time() + _ENGINEER_SESSION_TTL  # 滑动续期


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _maybe_send_feedback_mail() -> None:
    """存在未处理反馈且今天还没发过提醒时，给工程师邮箱发一封汇总。
    任何失败只写日志，绝不影响反馈提交本身。"""
    try:
        smtp = _smtp_conf()
        if not smtp:
            return
        today = datetime.date.today().isoformat()
        with _reg() as c:
            if _kv_get(c, "feedback_mail_last_date") == today:
                return
            to = (_kv_get(c, "engineer_email") or "").strip()
            pending = c.execute(
                "SELECT COUNT(*) FROM feedback WHERE status = 'open'"
            ).fetchone()[0]
            if not to or pending <= 0:
                return
            rows = c.execute(
                "SELECT lab_name, user, issue, created_at FROM feedback "
                "WHERE status = 'open' ORDER BY id DESC LIMIT 20"
            ).fetchall()
            _kv_set(c, "feedback_mail_last_date", today)
        lines = [f"- [{r[0]}] {r[1] or '匿名'}（{r[3]}）：{r[2]}" for r in rows]
        text = (
            "实验室菌种管理台有新的问题反馈，当前待处理 {n} 条：\n\n{body}\n\n"
            "打开管理台 → 设置 → 工程师界面，可查看全部反馈并回复。"
        ).format(n=pending, body="\n".join(lines))
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header(f"【菌种管理台】有 {pending} 条待处理反馈", "utf-8")
        msg["From"] = smtp["frm"]
        msg["To"] = to
        if smtp["port"] == 465:
            server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=15)
        else:
            server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=15)
            server.starttls()
        try:
            server.login(smtp["user"], smtp["pwd"])
            server.sendmail(smtp["frm"], [to], msg.as_string())
        finally:
            server.quit()
        print("[feedback] 提醒邮件已发送至", to, flush=True)
    except Exception as exc:  # noqa: BLE001 - 邮件失败绝不能阻断反馈流程
        print("[feedback] 发送提醒邮件失败（不影响反馈提交）:", exc, flush=True)


@app.post("/api/feedback")
def submit_feedback(body: FeedbackBody, request: Request) -> dict:
    """提交问题反馈：issue 必填，idea 选填；提交人取本机操作人名。"""
    lab = _require_lab(request)
    issue = body.issue.strip()
    if not issue:
        raise HTTPException(status_code=400, detail="请填写遇到的问题")
    if len(issue) > 2000 or len(body.idea) > 2000:
        raise HTTPException(status_code=400, detail="内容过长，请精简后提交")
    with _lock, _reg() as c:
        c.execute(
            "INSERT INTO feedback(lab_id, lab_name, user, issue, idea, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (lab["id"], lab["name"], body.user.strip()[:40], issue, body.idea.strip(), _now_str()),
        )
    _maybe_send_feedback_mail()
    return {"ok": True}


@app.get("/api/feedback/mine")
def my_feedback_replies(request: Request) -> dict:
    """本实验室收到但还没点开看过的工程师回复（进入页面时弹窗提醒用）。

    注意条件是 user_read=0 且有回复内容，不限定 status='replied'——
    否则工程师回复后若立即标记「已处理」（closed），用户将永远收不到该回复。
    """
    lab = _require_lab(request)
    with _reg() as c:
        rows = c.execute(
            "SELECT id, reply, replied_at, issue, created_at FROM feedback "
            "WHERE lab_id = ? AND user_read = 0 AND reply != '' "
            "ORDER BY replied_at DESC LIMIT 20",
            (lab["id"],),
        ).fetchall()
    return {
        "replies": [
            {"id": r[0], "reply": r[1], "repliedAt": r[2], "issue": r[3], "createdAt": r[4]}
            for r in rows
        ]
    }


@app.post("/api/feedback/read")
def mark_feedback_read(body: FeedbackReadBody, request: Request) -> dict:
    """用户看完回复弹窗后标记已读，之后不再弹。"""
    lab = _require_lab(request)
    if body.ids:
        with _lock, _reg() as c:
            c.executemany(
                "UPDATE feedback SET user_read = 1 WHERE id = ? AND lab_id = ?",
                [(i, lab["id"]) for i in body.ids],
            )
    return {"ok": True}


@app.post("/api/engineer/login")
def engineer_login(body: EngineerLoginBody) -> dict:
    """工程师界面登录：密码比对通过后发内存态 token（8 小时滑动续期）。"""
    pw = body.pw.strip()
    if not pw or not secrets.compare_digest(pw, _engineer_pass()):
        raise HTTPException(status_code=403, detail="密码错误")
    now = time.time()
    for k in [k for k, v in _engineer_tokens.items() if v < now]:
        _engineer_tokens.pop(k, None)
    tok = secrets.token_urlsafe(24)
    _engineer_tokens[tok] = now + _ENGINEER_SESSION_TTL
    return {"token": tok}


@app.get("/api/engineer/feedback")
def engineer_list_feedback(request: Request) -> dict:
    """全部反馈（待处理排前），供工程师界面罗列。"""
    _require_engineer(request)
    with _reg() as c:
        rows = c.execute(
            "SELECT id, lab_name, user, issue, idea, created_at, status, reply, replied_at "
            "FROM feedback ORDER BY (status = 'open') DESC, id DESC LIMIT 500"
        ).fetchall()
        pending = c.execute(
            "SELECT COUNT(*) FROM feedback WHERE status = 'open'"
        ).fetchone()[0]
    return {
        "pending": pending,
        "items": [
            {
                "id": r[0], "labName": r[1], "user": r[2], "issue": r[3], "idea": r[4],
                "createdAt": r[5], "status": r[6], "reply": r[7], "repliedAt": r[8],
            }
            for r in rows
        ],
    }


@app.post("/api/engineer/feedback/reply")
def engineer_reply_feedback(body: EngineerReplyBody, request: Request) -> dict:
    """回复一条反馈：置 replied 并标记用户未读（该实验室用户下次进入时弹窗收到）。"""
    _require_engineer(request)
    reply = body.reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="回复内容不能为空")
    with _lock, _reg() as c:
        cur = c.execute(
            "UPDATE feedback SET status = 'replied', reply = ?, replied_at = ?, user_read = 0 "
            "WHERE id = ?",
            (reply, _now_str(), body.id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="该反馈不存在")
    return {"ok": True}


@app.post("/api/engineer/feedback/close")
def engineer_close_feedback(body: EngineerCloseBody, request: Request) -> dict:
    """复选框批量「标记已处理」（不发用户通知）。"""
    _require_engineer(request)
    if body.ids:
        with _lock, _reg() as c:
            c.executemany(
                "UPDATE feedback SET status = 'closed' WHERE id = ? AND status != 'closed'",
                [(i,) for i in body.ids],
            )
    return {"ok": True}


@app.get("/api/engineer/email")
def engineer_get_email(request: Request) -> dict:
    _require_engineer(request)
    with _reg() as c:
        email = _kv_get(c, "engineer_email") or ""
    return {"email": email, "smtpConfigured": bool(_smtp_conf())}


@app.post("/api/engineer/email")
def engineer_set_email(body: EngineerEmailBody, request: Request) -> dict:
    """保存/清除提醒邮箱；SMTP 凭据来自环境变量，未配置时仅保存不发信。"""
    _require_engineer(request)
    email = body.email.strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    with _lock, _reg() as c:
        _kv_set(c, "engineer_email", email)
    return {"ok": True, "email": email, "smtpConfigured": bool(_smtp_conf())}


# ---------------------------------------------------------------- static
# 注意：挂在 "/" 之前已注册的 /api 路由优先生效
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")

# ---------------------------------------------------------------- entrypoint
# 同时兼容三种云端启动方式：
#   1) python app.py            （平台默认入口）
#   2) uvicorn app:app          （start.sh / start.bat）
# 端口优先取平台注入的 PORT 环境变量，未设置时本地默认 8000
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
