"""
Qual Annotation Platform - FastAPI backend
Architecture: react-fastapi-monorepo (single process, same origin)
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import asyncpg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────── DB connection pool ─────────────────────────────


DB_PROPS_FILE = "db" + ".properties"  # platform-injected at runtime (project root)
_DB_PROPS_CANDIDATES = [
    DB_PROPS_FILE,          # project root (Platform runtime, cwd = project root via --app-dir)
    "backend/" + DB_PROPS_FILE,  # fallback if cwd is elsewhere
]


def load_db_props(path: str = DB_PROPS_FILE) -> dict[str, str]:
    if os.environ.get("DB_HOST"):
        return {
            "db.host": os.environ.get("DB_HOST", "localhost"),
            "db.port": os.environ.get("DB_PORT", "5432"),
            "db.username": os.environ.get("DB_USER", "postgres"),
            "db.password": os.environ.get("DB_PASSWORD", "postgres"),
            "db.database": os.environ.get("DB_NAME", "qual_annotation"),
        }
    props: dict[str, str] = {}
    # Try candidates in order: same dir first, then parent (Platform platform injects at project root)
    candidates = [path] if path != DB_PROPS_FILE else _DB_PROPS_CANDIDATES
    for candidate in candidates:
        try:
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
            if props:
                print(f"[app] loaded DB props from {candidate}")
                return props
        except FileNotFoundError:
            continue
    return props


_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise HTTPException(503, "Database not available")
    return _pool


# ─────────────────────────── SSO helpers ────────────────────────────────────


def _parse_sso_user(request: Request) -> dict:
    raw = request.headers.get("decrypted-userinfo")
    if not raw:
        # Fallback for dev / standalone environment
        if os.environ.get("ENABLE_DEV_AUTH", "1") == "1":
            return {"user_id": "dev_annotator", "name": "Dev Annotator", "email": "annotator@example.com"}
        raise HTTPException(401, "No SSO header")
    fixed = raw.encode("latin-1").decode("utf-8")
    return json.loads(fixed)


def _require_user(request: Request) -> dict:
    return _parse_sso_user(request)


# ────────────────────────── Channel / review member helpers ──────────────────

VALID_CHANNELS = {"evaluation", "review"}


def _validate_channel(channel: str) -> str:
    if channel not in VALID_CHANNELS:
        raise HTTPException(400, f"无效的通道:{channel}(可选:evaluation / review)")
    return channel


async def _is_review_member(email: str) -> tuple[bool, bool]:
    """[v1.2 deprecated] 保留空实现防历史调用。"""
    return True, False


async def _require_review_member(request: Request, admin_only: bool = False) -> dict:
    """[v1.2 deprecated] 转接成普通 SSO 鉴权,不再看白名单。"""
    user = _require_user(request)
    user["is_review_admin"] = False
    return user


async def _require_channel_access(request: Request, channel: str) -> dict:
    """v1.2 起:两个通道均对任意 SSO 用户开放,仅按 channel 做数据隔离。"""
    _validate_channel(channel)
    user = _require_user(request)
    user["is_review_admin"] = False
    return user


async def _load_dataset_and_check(
    request: Request, dataset_id: str, need_conn=None,
) -> tuple[dict, dict]:
    """共用小工:根据 dataset_id 拿 channel 并校验访问权限。
    返回 (dataset_row_dict, user_dict)。dataset 不存在抛 404。
    need_conn: 已持有的连接;为 None 时从池自取。
    """
    async def _fetch(conn):
        return await conn.fetchrow(
            "SELECT id, name, channel FROM datasets WHERE id = $1", dataset_id,
        )

    if need_conn is not None:
        ds = await _fetch(need_conn)
    else:
        pool = await get_pool()
        async with pool.acquire() as conn:
            ds = await _fetch(conn)
    if not ds:
        raise HTTPException(404, "数据集不存在")
    user = await _require_channel_access(request, ds["channel"])
    return dict(ds), user



async def _seed_cluster_pool_if_review(conn, dataset_id: str, channel: str, cluster_ids: set[int]) -> None:
    """审核通道数据集上传后,把 cluster_pool 灌满(未认领状态,等审核员抢单)。
    评估通道不动。要求在同一个 conn/transaction 内调用。cluster_ids 会自动排序。
    """
    if channel != "review":
        return
    valid = sorted(c for c in cluster_ids if c != -1)
    if not valid:
        return
    await conn.executemany(
        """
        INSERT INTO cluster_pool (dataset_id, cluster_id)
        VALUES ($1, $2)
        ON CONFLICT (dataset_id, cluster_id) DO NOTHING
        """,
        [(dataset_id, cid) for cid in valid],
    )


# ─────────────────────────── App lifespan ───────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    props = load_db_props()
    if props:
        _pool = await asyncpg.create_pool(
            user=props["db.username"],
            password=props["db.password"],
            host=props["db.host"],
            port=int(props["db.port"]),
            database=props["db.database"],
            min_size=2,
            max_size=10,
        )
        print("[app] ✅ DB pool created")
    else:
        print("[app] ⚠️  DB props not found - disabled (dev mode)")
    yield
    if _pool:
        await _pool.close()
        print("[app] DB pool closed")


app = FastAPI(lifespan=lifespan)
api = app  # all routes go through /api prefix below


# ─────────────────────────── Health ─────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


# ─────────────────────────── Pydantic models ────────────────────────────────


class ClusterAnnotateRequest(BaseModel):
    cluster_id: int
    remark_first: str
    remark_second: str


# ─────────────────────────── API router ─────────────────────────────────────

from fastapi import APIRouter

api = APIRouter(prefix="/api")


@api.get("/health")
async def api_health():
    return {"status": "ok"}


@api.get("/whoami")
async def whoami(request: Request):
    user = _require_user(request)
    return {"user": user}


# ── Dataset APIs ─────────────────────────────────────────────────────────────

REQUIRED_COLUMNS = {"user_id", "qualification_url", "cluster_id"}


# ── 分片上传接口(解决大文件 413 问题)──────────────────────────────────

class ChunkUploadInit(BaseModel):
    filename: str
    channel: str = "evaluation"

class ChunkUploadPart(BaseModel):
    session_id: str
    chunk_index: int
    data: str          # base64 编码的小块内容

class ChunkUploadFinalize(BaseModel):
    session_id: str


@api.post("/datasets/upload/init")
async def upload_init(body: ChunkUploadInit, request: Request):
    """Step 1: 创建上传会话,返回 session_id。通道信息通过 filename 前缀 __ch=xxx__ 传递到 finalize。"""
    _validate_channel(body.channel)
    await _require_channel_access(request, body.channel)
    session_id = str(uuid.uuid4())
    tagged_filename = f"__ch={body.channel}__{body.filename}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO upload_sessions (id, filename) VALUES ($1, $2)",
            session_id, tagged_filename,
        )
    return {"session_id": session_id}


@api.post("/datasets/upload/chunk")
async def upload_chunk(body: ChunkUploadPart, request: Request):
    """Step 2: 上传单个分片(base64),可重试"""
    _require_user(request)
    import base64
    pool = await get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            "SELECT id, finalized FROM upload_sessions WHERE id = $1",
            body.session_id,
        )
        if not sess:
            raise HTTPException(404, "上传会话不存在")
        if sess["finalized"]:
            raise HTTPException(400, "上传会话已完成")
        # 用 ON CONFLICT DO UPDATE 实现分片幂等(可重试)
        await conn.execute(
            """
            INSERT INTO upload_chunks (session_id, chunk_index, data)
            VALUES ($1, $2, $3)
            ON CONFLICT (session_id, chunk_index) DO UPDATE SET data = EXCLUDED.data
            """,
            body.session_id, body.chunk_index, body.data,
        )
    return {"ok": True, "chunk_index": body.chunk_index}


@api.post("/datasets/upload/finalize")
async def upload_finalize(body: ChunkUploadFinalize, request: Request):
    """Step 3: 合并所有分片,写入数据库"""
    import base64
    _require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            "SELECT id, filename, finalized FROM upload_sessions WHERE id = $1",
            body.session_id,
        )
        if not sess:
            raise HTTPException(404, "上传会话不存在")
        if sess["finalized"]:
            raise HTTPException(400, "已经处理过,请勿重复提交")

        chunks = await conn.fetch(
            "SELECT data FROM upload_chunks WHERE session_id = $1 ORDER BY chunk_index",
            body.session_id,
        )
        if not chunks:
            raise HTTPException(400, "没有收到任何分片")

        # 拼合所有分片
        raw = b"".join(base64.b64decode(c["data"]) for c in chunks)
        content = raw.decode("utf-8-sig")

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise HTTPException(400, "CSV 文件为空或无法解析")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise HTTPException(400, f"CSV 缺少必要列:{', '.join(sorted(missing))}")

        rows = list(reader)
        if not rows:
            raise HTTPException(400, "CSV 文件没有数据行")

        items = []
        for i, row in enumerate(rows, start=2):
            try:
                cluster_id = int(row["cluster_id"])
            except (ValueError, KeyError):
                raise HTTPException(400, f"第 {i} 行 cluster_id 不是有效整数")
            items.append({
                "user_id": row.get("user_id", "").strip(),
                "qualification_url": row.get("qualification_url", "").strip(),
                "trade_first_name": row.get("trade_first_name", "").strip(),
                "trade_second_name": row.get("trade_second_name", "").strip(),
                "cluster_id": cluster_id,
            })

        cluster_ids = {item["cluster_id"] for item in items}
        dataset_id = str(uuid.uuid4())
        tagged = sess["filename"] or "upload.csv"
        # 解析通道前缀 __ch=xxx__
        channel = "evaluation"
        filename = tagged
        if tagged.startswith("__ch="):
            end = tagged.find("__", 5)
            if end > 5:
                channel = tagged[5:end]
                filename = tagged[end + 2:] or "upload.csv"
        _validate_channel(channel)

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO datasets (id, name, filename, total_rows, cluster_count, channel)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                dataset_id, filename, filename, len(items), len(cluster_ids), channel,
            )
            await conn.executemany(
                """
                INSERT INTO dataset_items
                    (dataset_id, user_id, qualification_url, trade_first_name, trade_second_name, cluster_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                [
                    (dataset_id, it["user_id"], it["qualification_url"],
                     it["trade_first_name"], it["trade_second_name"], it["cluster_id"])
                    for it in items
                ],
            )
            await _seed_cluster_pool_if_review(conn, dataset_id, channel, cluster_ids)
            # 标记会话已完成,防止重复提交
            await conn.execute(
                "UPDATE upload_sessions SET finalized = TRUE WHERE id = $1",
                body.session_id,
            )

    return {
        "id": dataset_id,
        "name": filename,
        "filename": filename,
        "total_rows": len(items),
        "cluster_count": len(cluster_ids),
    }


@api.post("/datasets/upload")
async def upload_dataset(
    request: Request,
    file: UploadFile = File(...),
    channel: str = Form("evaluation"),
):
    """按通道上传数据集。审核通道需在白名单内。"""
    _validate_channel(channel)
    await _require_channel_access(request, channel)
    content = (await file.read()).decode("utf-8-sig")  # handle BOM
    reader = csv.DictReader(io.StringIO(content))

    # Validate only required columns
    if not reader.fieldnames:
        raise HTTPException(400, "CSV 文件为空或无法解析")
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise HTTPException(400, f"CSV 缺少必要列:{', '.join(sorted(missing))}")

    rows = list(reader)
    if not rows:
        raise HTTPException(400, "CSV 文件没有数据行")

    # Parse rows - optional columns fall back to empty string
    items = []
    for i, row in enumerate(rows, start=2):
        try:
            cluster_id = int(row["cluster_id"])
        except (ValueError, KeyError):
            raise HTTPException(400, f"第 {i} 行 cluster_id 不是有效整数")
        items.append({
            "user_id": row.get("user_id", "").strip(),
            "qualification_url": row.get("qualification_url", "").strip(),
            "trade_first_name": row.get("trade_first_name", "").strip(),
            "trade_second_name": row.get("trade_second_name", "").strip(),
            "cluster_id": cluster_id,
        })

    cluster_ids = {item["cluster_id"] for item in items}
    dataset_id = str(uuid.uuid4())
    filename = file.filename or "upload.csv"
    name = filename

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO datasets (id, name, filename, total_rows, cluster_count, channel)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                dataset_id, name, filename, len(items), len(cluster_ids), channel,
            )
            await conn.executemany(
                """
                INSERT INTO dataset_items
                    (dataset_id, user_id, qualification_url, trade_first_name, trade_second_name, cluster_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                [
                    (dataset_id, it["user_id"], it["qualification_url"],
                     it["trade_first_name"], it["trade_second_name"], it["cluster_id"])
                    for it in items
                ],
            )
            await _seed_cluster_pool_if_review(conn, dataset_id, channel, cluster_ids)

    return {
        "id": dataset_id,
        "name": name,
        "filename": filename,
        "total_rows": len(items),
        "cluster_count": len(cluster_ids),
    }


@api.get("/datasets")
async def list_datasets(request: Request, channel: str = "evaluation"):
    """按通道列出数据集。审核通道需在白名单内。"""
    await _require_channel_access(request, channel)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, filename, total_rows, cluster_count, created_at, channel
            FROM datasets
            WHERE channel = $1
            ORDER BY created_at DESC
            """,
            channel,
        )
    return [dict(r) for r in rows]


@api.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str, request: Request):
    """按数据集所属通道校验权限后删除。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds = await conn.fetchrow("SELECT channel FROM datasets WHERE id = $1", dataset_id)
        if not ds:
            raise HTTPException(404, "数据集不存在")
        await _require_channel_access(request, ds["channel"])
        await conn.execute("DELETE FROM datasets WHERE id = $1", dataset_id)
    return {"ok": True}


# ── Cluster APIs ─────────────────────────────────────────────────────────────


@api.get("/datasets/{dataset_id}/search_uid")
async def search_uid_clusters(dataset_id: str, request: Request, q: str = ""):
    """模糊匹配 user_id,返回包含该 UID 的簇 id 列表。评估通道专用。"""
    if not q or len(q.strip()) < 2:
        return {"cluster_ids": [], "matched_count": 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            """
            SELECT DISTINCT cluster_id, COUNT(*) OVER () AS matched_count
            FROM dataset_items
            WHERE dataset_id = $1 AND cluster_id != -1 AND user_id ILIKE $2
            ORDER BY cluster_id
            LIMIT 500
            """,
            dataset_id, f"%{q.strip()}%",
        )
    return {
        "cluster_ids": [r["cluster_id"] for r in rows],
        "matched_count": int(rows[0]["matched_count"]) if rows else 0,
    }


@api.get("/datasets/{dataset_id}/clusters")
async def get_clusters(dataset_id: str, request: Request):
    """两通道都返回全部簇;审核通道的簇状态由前端结合 my_task/pool 展示。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            """
            WITH trade_mode AS (
                SELECT dataset_id, cluster_id, trade_first_name, trade_second_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY dataset_id, cluster_id
                           ORDER BY COUNT(*) DESC, trade_first_name, trade_second_name
                       ) AS rn
                FROM dataset_items
                WHERE dataset_id = $1 AND cluster_id != -1
                GROUP BY dataset_id, cluster_id, trade_first_name, trade_second_name
            )
            SELECT di.cluster_id, COUNT(*) AS count,
                   tm.trade_first_name,
                   tm.trade_second_name,
                   ca.remark_first, ca.remark_second
            FROM dataset_items di
            LEFT JOIN trade_mode tm
                ON tm.dataset_id = di.dataset_id AND tm.cluster_id = di.cluster_id AND tm.rn = 1
            LEFT JOIN cluster_annotations ca
                ON ca.dataset_id = di.dataset_id AND ca.cluster_id = di.cluster_id
            WHERE di.dataset_id = $1 AND di.cluster_id != -1
            GROUP BY di.cluster_id, tm.trade_first_name, tm.trade_second_name, ca.remark_first, ca.remark_second
            ORDER BY di.cluster_id
            """,
            dataset_id,
        )
    return [dict(r) for r in rows]


@api.get("/datasets/{dataset_id}/clusters/{cluster_id}/items")
async def get_cluster_items(
    dataset_id: str,
    cluster_id: int,
    request: Request,
    page: int = 1,
    page_size: int = 50,
):
    pool = await get_pool()
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        # 审核通道:只允许查看"我当前正在做(认领未完成)"的簇的图片;避免看到别人的
        if ds["channel"] == "review":
            allowed = await conn.fetchval(
                """
                SELECT 1 FROM cluster_pool
                WHERE dataset_id = $1 AND cluster_id = $2
                  AND assignee_email = $3 AND completed_at IS NULL
                """,
                dataset_id, cluster_id, user.get("email", ""),
            )
            if not allowed:
                raise HTTPException(403, "该簇不是你当前认领中的任务")

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM dataset_items WHERE dataset_id = $1 AND cluster_id = $2",
            dataset_id, cluster_id,
        )
        rows = await conn.fetch(
            """
            SELECT user_id, qualification_url, trade_first_name, trade_second_name, cluster_id
            FROM dataset_items
            WHERE dataset_id = $1 AND cluster_id = $2
            ORDER BY id
            LIMIT $3 OFFSET $4
            """,
            dataset_id, cluster_id, page_size, offset,
        )
    return {"total": total, "page": page, "page_size": page_size, "items": [dict(r) for r in rows]}


# ── Annotation APIs ──────────────────────────────────────────────────────────


@api.get("/datasets/{dataset_id}/cluster_annotations")
async def get_cluster_annotations(dataset_id: str, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            "SELECT cluster_id, remark_first, remark_second FROM cluster_annotations WHERE dataset_id = $1",
            dataset_id,
        )
    return {str(r["cluster_id"]): {"remark_first": r["remark_first"], "remark_second": r["remark_second"]} for r in rows}


@api.post("/datasets/{dataset_id}/cluster_annotations")
async def set_cluster_annotation(dataset_id: str, body: ClusterAnnotateRequest, request: Request):
    if not body.remark_first or not body.remark_second:
        raise HTTPException(400, "remark_first 和 remark_second 不能为空")
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        email = user.get("email", "")

        # v1.3：允许重新修改（避免手误），不再“标完即锁”
        if ds["channel"] == "review":
            # 审核通道：一旦该簇已有标注（不管是谁标的），开放给当前用户修改；
            # 否则必须是已认领 & 未完成的抢单任务
            existing = await conn.fetchrow(
                "SELECT annotator_email FROM cluster_annotations WHERE dataset_id = $1 AND cluster_id = $2",
                dataset_id, body.cluster_id,
            )
            if not existing:
                # 新标必须是自己认领中的簇
                claim = await conn.fetchrow(
                    """
                    SELECT 1 FROM cluster_pool
                    WHERE dataset_id = $1 AND cluster_id = $2
                      AND assignee_email = $3 AND completed_at IS NULL
                    """,
                    dataset_id, body.cluster_id, email,
                )
                if not claim:
                    raise HTTPException(403, "该簇不是你当前认领中的任务，不能保存标注")

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO cluster_annotations (dataset_id, cluster_id, remark_first, remark_second, annotator_email)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (dataset_id, cluster_id)
                DO UPDATE SET remark_first = EXCLUDED.remark_first,
                              remark_second = EXCLUDED.remark_second,
                              annotator_email = EXCLUDED.annotator_email,
                              annotated_at = NOW()
                """,
                dataset_id, body.cluster_id, body.remark_first, body.remark_second, email,
            )
            # 审核通道：按簇标注，保存簇标签即完成当前抢单簇
            if ds["channel"] == "review":
                await conn.execute(
                    """
                    UPDATE cluster_pool SET completed_at = COALESCE(completed_at, NOW())
                    WHERE dataset_id = $1 AND cluster_id = $2
                    """,
                    dataset_id, body.cluster_id,
                )
    return {"ok": True}


@api.delete("/datasets/{dataset_id}/cluster_annotations/{cluster_id}")
async def delete_cluster_annotation(dataset_id: str, cluster_id: int, request: Request):
    """v1.3 起重新开放清除标注（避免手误）。审核通道：要求是当前用户训标的才能清。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        email = user.get("email", "")
        if ds["channel"] == "review":
            existing = await conn.fetchrow(
                "SELECT annotator_email FROM cluster_annotations WHERE dataset_id = $1 AND cluster_id = $2",
                dataset_id, cluster_id,
            )
            if existing and existing["annotator_email"] and existing["annotator_email"] != email:
                raise HTTPException(403, "只能清除自己标注的簇")
        await conn.execute(
            "DELETE FROM cluster_annotations WHERE dataset_id = $1 AND cluster_id = $2",
            dataset_id, cluster_id,
        )
    return {"ok": True}


# ── Item label APIs (图片粒度剔除标记) ──────────────────────────────────────

@api.get("/datasets/{dataset_id}/clusters/{cluster_id}/item_labels")
async def get_item_labels(dataset_id: str, cluster_id: int, request: Request):
    """Return set of qualification_urls that have been labeled '\u5254\u9664' in this cluster."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            """
            SELECT il.qualification_url
            FROM item_labels il
            JOIN dataset_items di
              ON di.dataset_id = il.dataset_id AND di.qualification_url = il.qualification_url
            WHERE il.dataset_id = $1 AND di.cluster_id = $2
            """,
            dataset_id, cluster_id,
        )
    return {"labeled": [r["qualification_url"] for r in rows]}


class ItemLabelRequest(BaseModel):
    qualification_url: str


@api.post("/datasets/{dataset_id}/item_labels/toggle")
async def toggle_item_label(dataset_id: str, body: ItemLabelRequest, request: Request):
    """Toggle '\u5254\u9664' label for a single image. Returns new state."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        if ds["channel"] == "review":
            # 审核通道:图片所在簇必须是"我当前认领未完成"
            allowed = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM dataset_items di
                    JOIN cluster_pool cp
                      ON cp.dataset_id = di.dataset_id AND cp.cluster_id = di.cluster_id
                    WHERE di.dataset_id = $1 AND di.qualification_url = $2
                      AND cp.assignee_email = $3 AND cp.completed_at IS NULL
                )
                """,
                dataset_id, body.qualification_url, user.get("email", ""),
            )
            if not allowed:
                raise HTTPException(403, "该图片所在簇不是你当前认领中的任务")
        existing = await conn.fetchval(
            "SELECT 1 FROM item_labels WHERE dataset_id = $1 AND qualification_url = $2",
            dataset_id, body.qualification_url,
        )
        if existing:
            await conn.execute(
                "DELETE FROM item_labels WHERE dataset_id = $1 AND qualification_url = $2",
                dataset_id, body.qualification_url,
            )
            return {"ok": True, "labeled": False}
        else:
            await conn.execute(
                """
                INSERT INTO item_labels (dataset_id, qualification_url)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                dataset_id, body.qualification_url,
            )
            return {"ok": True, "labeled": True}


# ── v1.4/v1.5: 图片级 verdict 接口 ─────────────────────────────────────

class ImageVerdictItem(BaseModel):
    qualification_url: str
    verdict: str  # v1.5: 不违规 | 实锤造假·实拍图片p图 | ...


class ImageVerdictBatchRequest(BaseModel):
    cluster_id: int
    items: list[ImageVerdictItem]


@api.get("/datasets/{dataset_id}/clusters/{cluster_id}/image_reviews")
async def get_cluster_image_reviews(dataset_id: str, cluster_id: int, request: Request):
    """拿这个簇所有图片的 verdict 状态。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            """
            SELECT ir.qualification_url, ir.verdict, ir.reviewer_email, ir.updated_at
            FROM image_reviews ir
            JOIN dataset_items di
              ON di.dataset_id = ir.dataset_id AND di.qualification_url = ir.qualification_url
            WHERE ir.dataset_id = $1 AND di.cluster_id = $2
            """,
            dataset_id, cluster_id,
        )
    return {
        "reviews": {r["qualification_url"]: {
            "verdict": r["verdict"],
            "reviewer_email": r["reviewer_email"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        } for r in rows}
    }


@api.post("/datasets/{dataset_id}/image_reviews/upsert")
async def upsert_image_reviews(dataset_id: str, body: ImageVerdictBatchRequest, request: Request):
    """批量保存图片 verdict。审核通道：必须是自己认领的簇。
    保存后自动检查该簇是否全部图片有 verdict，是则打上 cluster_pool.completed_at 锁死。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        email = user.get("email", "")

        if ds["channel"] == "review":
            # 检查是自己认领中的簇（未 completed）；或已 completed 也允许修改（v1.3 放开）
            existing_pool = await conn.fetchrow(
                "SELECT assignee_email, completed_at FROM cluster_pool WHERE dataset_id = $1 AND cluster_id = $2",
                dataset_id, body.cluster_id,
            )
            if not existing_pool:
                raise HTTPException(404, "该簇不在任务池")
            if existing_pool["completed_at"] is None and existing_pool["assignee_email"] != email:
                raise HTTPException(403, "该簇不是你当前认领中的任务")

        # 校验 verdict 非空（v1.5：接受任意标签叶字符串）
        for it in body.items:
            if not it.verdict or not it.verdict.strip():
                raise HTTPException(400, "verdict 不能为空")

        # 校验 url 都属于该簇
        urls = [it.qualification_url for it in body.items]
        valid = await conn.fetch(
            "SELECT DISTINCT qualification_url FROM dataset_items WHERE dataset_id = $1 AND cluster_id = $2 AND qualification_url = ANY($3::text[])",
            dataset_id, body.cluster_id, urls,
        )
        valid_set = {r["qualification_url"] for r in valid}
        invalid = [u for u in urls if u not in valid_set]
        if invalid:
            raise HTTPException(400, f"以下 URL 不属于该簇：{len(invalid)} 条")

        async with conn.transaction():
            for it in body.items:
                await conn.execute(
                    """
                    INSERT INTO image_reviews (dataset_id, qualification_url, verdict, reviewer_email)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (dataset_id, qualification_url)
                    DO UPDATE SET verdict = EXCLUDED.verdict,
                                  reviewer_email = EXCLUDED.reviewer_email,
                                  updated_at = NOW()
                    """,
                    dataset_id, it.qualification_url, it.verdict, email,
                )

            # 检查该簇是否全部图片都有 verdict
            if ds["channel"] == "review":
                total = await conn.fetchval(
                    "SELECT COUNT(DISTINCT qualification_url) FROM dataset_items WHERE dataset_id = $1 AND cluster_id = $2",
                    dataset_id, body.cluster_id,
                ) or 0
                done = await conn.fetchval(
                    """
                    SELECT COUNT(DISTINCT ir.qualification_url)
                    FROM image_reviews ir
                    JOIN dataset_items di
                      ON di.dataset_id = ir.dataset_id AND di.qualification_url = ir.qualification_url
                    WHERE ir.dataset_id = $1 AND di.cluster_id = $2
                    """,
                    dataset_id, body.cluster_id,
                ) or 0
                if done >= total and total > 0:
                    await conn.execute(
                        """
                        UPDATE cluster_pool SET completed_at = COALESCE(completed_at, NOW())
                        WHERE dataset_id = $1 AND cluster_id = $2
                        """,
                        dataset_id, body.cluster_id,
                    )
    return {"ok": True, "saved": len(body.items)}


@api.get("/datasets/{dataset_id}/annotation_progress")
async def annotation_progress(dataset_id: str, request: Request):
    """两通道都返回全局进度。审核通道 v1.4 起：额外返回图片级 verdict 进度。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, _user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        total_clusters = await conn.fetchval(
            "SELECT COUNT(DISTINCT cluster_id) FROM dataset_items WHERE dataset_id = $1 AND cluster_id != -1",
            dataset_id,
        ) or 0
        annotated_clusters = await conn.fetchval(
            "SELECT COUNT(*) FROM cluster_annotations WHERE dataset_id = $1",
            dataset_id,
        ) or 0

        image_progress = None
        if ds["channel"] == "review":
            total_imgs = await conn.fetchval(
                "SELECT COUNT(DISTINCT qualification_url) FROM dataset_items WHERE dataset_id = $1 AND cluster_id != -1",
                dataset_id,
            ) or 0
            reviewed_imgs = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT ir.qualification_url)
                FROM image_reviews ir
                JOIN dataset_items di
                  ON di.dataset_id = ir.dataset_id AND di.qualification_url = ir.qualification_url
                WHERE ir.dataset_id = $1 AND di.cluster_id != -1
                """,
                dataset_id,
            ) or 0
            image_progress = {"total": total_imgs, "reviewed": reviewed_imgs}

    return {
        "total_clusters": total_clusters,
        "annotated_clusters": annotated_clusters,
        "pending_clusters": total_clusters - annotated_clusters,
        "image_progress": image_progress,
    }


@api.get("/datasets/{dataset_id}/export")
async def export_annotations(dataset_id: str, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, _user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        rows = await conn.fetch(
            """
            SELECT
                di.user_id,
                di.qualification_url,
                di.trade_first_name,
                di.trade_second_name,
                di.cluster_id,
                ca.remark_first,
                ca.remark_second,
                COALESCE(ca.annotator_email, '') AS annotator_email,
                COALESCE(ir.verdict, '') AS image_verdict,
                CASE
                    WHEN ir.verdict = '不违规' THEN '通过'
                    WHEN ir.verdict IS NOT NULL AND ir.verdict != '' THEN '违规'
                    WHEN il.qualification_url IS NOT NULL THEN '通过'
                    WHEN ca.remark_first = '不违规' THEN '通过'
                    ELSE '违规'
                END AS image_label
            FROM dataset_items di
            LEFT JOIN cluster_annotations ca
                ON ca.dataset_id = di.dataset_id AND ca.cluster_id = di.cluster_id
            LEFT JOIN item_labels il
                ON il.dataset_id = di.dataset_id AND il.qualification_url = di.qualification_url
            LEFT JOIN image_reviews ir
                ON ir.dataset_id = di.dataset_id AND ir.qualification_url = di.qualification_url
            WHERE di.dataset_id = $1
              AND (ca.remark_first IS NOT NULL OR ir.verdict IS NOT NULL OR il.qualification_url IS NOT NULL)
            ORDER BY di.cluster_id, di.id
            """,
            dataset_id,
        )

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["user_id", "qualification_url", "trade_first_name", "trade_second_name",
                    "cluster_id", "remark_first", "remark_second", "annotator_email",
                    "image_verdict", "image_label"],
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(dict(r))

    content = output.getvalue()
    safe_name = ds["name"].replace(".csv", "").replace(" ", "_")
    filename = f"{safe_name}_annotated.csv"
    from urllib.parse import quote
    encoded_filename = quote(filename, safe="")

    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


# ─────────────────────────── Mount routes ───────────────────────────────────



# ─────────────────────────── Review-channel claim APIs (v1.2) ────────────────
# v1.2 起:审核通道改为"抢单式"--不再由 admin 预分配,审核员点开始标注时
# 自动通过 cluster_pool 拿一个未认领的簇;标完即锁(cluster_annotations 已有的簇不允许改)。
# 并发正确性靠 PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 保证。


async def _get_assignee_current(conn, dataset_id: str, email: str) -> Optional[int]:
    """查审核员当前在此数据集有无进行中(认领未完成)的簇。"""
    return await conn.fetchval(
        """
        SELECT cluster_id FROM cluster_pool
        WHERE dataset_id = $1 AND assignee_email = $2 AND completed_at IS NULL
        LIMIT 1
        """,
        dataset_id, email,
    )


@api.get("/review/datasets/{dataset_id}/my_task")
async def review_my_task(dataset_id: str, request: Request):
    """查我当前在这个数据集里"进行中"的簇(用于刷新页面时恢复现场)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        if ds["channel"] != "review":
            raise HTTPException(400, "该接口仅审核通道可用")
        cur = await _get_assignee_current(conn, dataset_id, user.get("email", ""))
        # 顺便拿全局进度
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM cluster_pool WHERE dataset_id = $1", dataset_id,
        ) or 0
        done = await conn.fetchval(
            "SELECT COUNT(*) FROM cluster_pool WHERE dataset_id = $1 AND completed_at IS NOT NULL",
            dataset_id,
        ) or 0
        my_done = await conn.fetchval(
            """
            SELECT COUNT(*) FROM cluster_pool
            WHERE dataset_id = $1 AND assignee_email = $2 AND completed_at IS NOT NULL
            """,
            dataset_id, user.get("email", ""),
        ) or 0
    return {
        "current_cluster_id": cur,
        "total": total,
        "done": done,
        "my_done": my_done,
    }


@api.post("/review/datasets/{dataset_id}/claim_next")
async def review_claim_next(dataset_id: str, request: Request):
    """【核心】抢下一个未认领的簇。并发安全:SELECT FOR UPDATE SKIP LOCKED。
    - 若审核员当前已有一个进行中的簇,直接返回(不重复抢)
    - 若池已被抢空,返回 { "cluster_id": null, "reason": "no_more" }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        if ds["channel"] != "review":
            raise HTTPException(400, "该接口仅审核通道可用")
        email = user.get("email", "")

        # 1) 已有进行中的簇 → 直接返回
        cur = await _get_assignee_current(conn, dataset_id, email)
        if cur is not None:
            return {"cluster_id": cur, "resumed": True}

        # 2) 事务内 SKIP LOCKED 抢一个未认领 & 未完成的簇
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cluster_id FROM cluster_pool
                WHERE dataset_id = $1
                  AND assignee_email IS NULL
                  AND completed_at IS NULL
                ORDER BY cluster_id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                dataset_id,
            )
            if not row:
                return {"cluster_id": None, "reason": "no_more"}
            claimed_id = row["cluster_id"]
            await conn.execute(
                """
                UPDATE cluster_pool SET assignee_email = $1, claimed_at = NOW()
                WHERE dataset_id = $2 AND cluster_id = $3
                """,
                email, dataset_id, claimed_id,
            )
        return {"cluster_id": claimed_id, "resumed": False}


@api.post("/review/datasets/{dataset_id}/release")
async def review_release_current(dataset_id: str, request: Request):
    """主动释放当前认领但未完成的簇(用户点暂停/退出)。已 completed 的簇不动。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ds, user = await _load_dataset_and_check(request, dataset_id, need_conn=conn)
        if ds["channel"] != "review":
            raise HTTPException(400, "该接口仅审核通道可用")
        await conn.execute(
            """
            UPDATE cluster_pool
            SET assignee_email = NULL, claimed_at = NULL
            WHERE dataset_id = $1 AND assignee_email = $2 AND completed_at IS NULL
            """,
            dataset_id, user.get("email", ""),
        )
    return {"ok": True}


app.include_router(api)

# ── Automation integration router (X-API-Token auth, /api/v1/automation/*) ─────────────
from automation_router import build_automation_router
app.include_router(build_automation_router(get_pool))

# Serve built frontend
_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        index = _dist / "index.html"
        return FileResponse(str(index))
