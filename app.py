# -*- coding: utf-8 -*-
"""
-80℃ 冰箱菌种管理台 · 云端后端
================================
- 数据模型与前端 JSON 结构完全一致（racks -> boxes -> wells）
- 双存储后端：默认 SQLite 单文件；设置环境变量 DATABASE_URL 时使用 Postgres（云端部署用）
- 整库存取，rev 递增号实现乐观并发控制
- 每次保存自动落一份快照（SQLite 模式写 backups/ 目录，Postgres 模式写 backups 表），最多保留 200 份
- GET  /api/db        读取最新数据 {rev, db}
- PUT  /api/db        保存数据（带版本校验，冲突返回 409 + 服务器最新数据）
- GET  /api/health    健康检查
- 其余路径托管 static/ 下的前端页面

启动：uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FREEZER_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "freezer.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 200

# 云端部署（Render/Railway 等）时设置 DATABASE_URL 指向 Postgres（如 Neon 免费库）
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_lock = threading.Lock()

app = FastAPI(title="-80C Freezer Inventory", version="1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- storage
def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _pg():
    """Postgres 连接（psycopg3）。仅在设置了 DATABASE_URL 时使用。"""
    import psycopg  # 延迟导入，本地 SQLite 模式无需安装

    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backups ("
        "id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), "
        "rev INTEGER, doc TEXT)"
    )
    return conn


def _load_doc() -> Optional[dict]:
    if DATABASE_URL:
        with _pg() as c:
            row = c.execute("SELECT v FROM kv WHERE k='doc'").fetchone()
    else:
        with _lock:
            with _connect() as c:
                row = c.execute("SELECT v FROM kv WHERE k='doc'").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _save_doc(doc: dict) -> None:
    payload = json.dumps(doc, ensure_ascii=False)
    if DATABASE_URL:
        with _pg() as c:
            c.execute(
                "INSERT INTO kv(k, v) VALUES('doc', %s) "
                "ON CONFLICT(k) DO UPDATE SET v = EXCLUDED.v",
                (payload,),
            )
    else:
        with _lock:
            with _connect() as c:
                c.execute(
                    "INSERT INTO kv(k, v) VALUES('doc', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (payload,),
                )


def _write_backup(doc: dict) -> None:
    """每次成功保存后落一份快照，防止误操作/覆盖导致数据丢失。"""
    try:
        if DATABASE_URL:
            # Postgres 模式：快照写入数据库表，随库永久保存
            with _pg() as c:
                c.execute(
                    "INSERT INTO backups(rev, doc) VALUES(%s, %s)",
                    (doc.get("rev", 0), json.dumps(doc, ensure_ascii=False)),
                )
                c.execute(
                    "DELETE FROM backups WHERE id NOT IN "
                    "(SELECT id FROM backups ORDER BY id DESC LIMIT %s)",
                    (BACKUP_KEEP,),
                )
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = BACKUP_DIR / f"freezer-{stamp}-r{doc['rev']}.json"
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        olds = sorted(BACKUP_DIR.glob("freezer-*.json"))
        for stale in olds[:-BACKUP_KEEP]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:
        pass  # 备份失败不影响主流程


# ---------------------------------------------------------------- models
class SaveBody(BaseModel):
    rev: Optional[int] = None
    force: bool = False
    db: dict


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "rev": (_load_doc() or {}).get("rev", 0)}


@app.get("/api/db")
def get_db() -> dict:
    doc = _load_doc()
    if doc is None:
        # 尚未初始化：前端收到 404 后会用种子数据 POST/PUT 上来
        raise HTTPException(status_code=404, detail="数据库为空，等待前端初始化")
    return doc


@app.put("/api/db")
def put_db(body: SaveBody = Body(...)) -> Any:
    cur = _load_doc()
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
    _save_doc(next_doc)
    _write_backup(next_doc)
    return {"rev": next_doc["rev"]}


# ---------------------------------------------------------------- static
# 注意：挂在 "/" 之前已注册的 /api 路由优先生效
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
