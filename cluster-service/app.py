"""资质图聚簇自助网站后端 —— platform fastapi-only.

架构：本后端只做「收单 + 交付」，不跑聚簇。
  浏览器用户 ──SSO──▶ 本后端（任务入 PG 队列）
  本机跑批(cron 轮询) ──共享token──▶ 领任务 → 取数聚簇 → 产物传CDN → 回写

API 分三组：
  用户侧（SSO 必须）：
    GET  /api/tasks                自己的任务列表
    GET  /api/tasks/{id}           任务详情 + 事件流
    POST /api/tasks/preview        上传表格 → ID 列识别预览（不入队）
    POST /api/tasks                确认提交 → 入队
  跑批侧（共享 token，无 SSO——跑批不是浏览器）：
    POST /api/runner/claim         领任务（pending → running）
    POST /api/runner/{id}/event    上报进度事件
    POST /api/runner/{id}/result   回写结果（状态/统计/产物链接）
  白名单：/api/tasks 仅治理组成员可提交（提交时校验 email）。

ID 识别预览是「无状态」的：表格内容随 preview 请求传上来，识别结果
返回给前端确认；提交时表格内容也一并带上，后端存 PG Large Object 供跑批取。
（跑批不直连 PG，通过 /api/runner/{id}/claim 的返回体拿表格内容。）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import date, timedelta
from typing import Optional

import psycopg
from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

# ── SSO（模板内置，勿改逻辑）─────────────────────────────────────────


def _parse_sso_user(decrypted_userinfo: Optional[str]) -> Optional[dict]:
    if not decrypted_userinfo:
        return None
    try:
        fixed = decrypted_userinfo.encode("latin-1").decode("utf-8")
        return json.loads(fixed)
    except Exception:
        return None


def _require_user(decrypted_userinfo: Optional[str]) -> dict:
    user = _parse_sso_user(decrypted_userinfo)
    if not user or not user.get("email"):
        raise HTTPException(status_code=401, detail="no sso header")
    return user


def _load_props(path: str) -> dict[str, str]:
    props: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return props


def _get_db_conn() -> psycopg.Connection:
    p = _load_props("db.properties")
    if not p.get("db.host"):
        raise HTTPException(status_code=503, detail="db.properties 未配置")
    return psycopg.connect(
        host=p["db.host"], port=int(p["db.port"]), dbname=p["db.database"],
        user=p["db.username"], password=p["db.password"], row_factory=dict_row,
    )


app = FastAPI(title="资质图聚簇自助网站")
# 前端从同源 / 返回，无跨域调用；CORS 白名单仅放行同源
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.example.com",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Decrypted-Userinfo"],
)


# ── 白名单：治理组成员可提交 ─────────────────────────────────────────
# 环境变量 APP_QC_ALLOWED_USERS（逗号分隔邮箱，Studio 表单配置）优先；
# 未配置时默认仅工具所有者可用。所有者邮箱永远并入名单，防止配置时把自己锁外面。
_OWNER_EMAIL = "owner@example.com"


def _load_allowed_users() -> set:
    raw = os.environ.get("APP_QC_ALLOWED_USERS", "")
    users = {e.strip().lower() for e in raw.split(",") if e.strip()}
    users.add(_OWNER_EMAIL)
    return users


def _require_member(user: dict) -> str:
    email = str(user.get("email", "")).strip().lower()
    if email not in _load_allowed_users():
        raise HTTPException(status_code=403,
                            detail="暂未开放使用权限，请联系治理组开通")
    return email


# ── 跑批共享 token ──────────────────────────────────────────────────
# 环境变量 APP_QC_RUNNER_TOKEN 优先（Studio 表单），否则读随包 runner.properties。


def _load_runner_secret() -> str:
    tok = os.environ.get("APP_QC_RUNNER_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open("runner.properties") as f:
            for line in f:
                line = line.strip()
                if line.startswith("token="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def _require_runner_token(x_qc_runner_token: Optional[str]) -> None:
    expected = _load_runner_secret()
    if not expected or not x_qc_runner_token or x_qc_runner_token != expected:
        raise HTTPException(status_code=401, detail="bad runner token")


# ── 校验 ────────────────────────────────────────────────────────────
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_date(s: str, name: str) -> date:
    if not _DATE_RE.fullmatch(s or ""):
        raise HTTPException(status_code=400, detail=f"{name} 须为 yyyy-MM-dd")
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} 非法日期")
    return d


STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED = \
    "pending", "running", "done", "failed"
MODE_POOL_MATCH, MODE_SELF_CLUSTER = "pool_match", "self_cluster"
CLUSTER_MODES = {MODE_POOL_MATCH, MODE_SELF_CLUSTER}


# ── 模型 ────────────────────────────────────────────────────────────

class TaskOut(BaseModel):
    id: int
    title: str
    status: str
    id_type: str
    id_column: Optional[str] = None
    n_ids: Optional[int] = None
    cluster_mode: str = MODE_POOL_MATCH
    pool_start: Optional[str] = None
    pool_end: Optional[str] = None
    detail: Optional[str] = None
    result_csv_url: Optional[str] = None
    flat_page_url: Optional[str] = None
    n_accounts: Optional[int] = None
    n_clustered: Optional[int] = None
    n_unclustered: Optional[int] = None
    n_no_emb: Optional[int] = None
    created_by: str
    created_by_name: Optional[str] = None
    created_at: str


class TaskListOut(BaseModel):
    tasks: list[TaskOut]


class TaskDetailOut(TaskOut):
    events: list[dict]


# ── 用户侧 API ──────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "qual-cluster-service"}


@app.get("/api/meta")
def app_meta(decrypted_userinfo: Optional[str] = Header(None)) -> dict:
    """返回前端初始化信息；由服务端计算日期，避免浏览器时区偏移。"""
    user = _require_user(decrypted_userinfo)
    end = date.today() - timedelta(days=1)
    return {
        "name": "资质图聚簇自助网站",
        "user_name": user.get("username") or user.get("name") or user.get("email"),
        "default_pool_start": str(end - timedelta(days=6)),
        "default_pool_end": str(end),
        "max_file_mb": 8,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse("frontend/index.html", media_type="text/html; charset=utf-8")


@app.get("/xlsx.full.min.js")
def xlsx_lib() -> FileResponse:
    # 前端 xlsx 解析库本地化：外网 CDN 在办公网内不可靠，同源下发
    return FileResponse("frontend/xlsx.full.min.js",
                        media_type="application/javascript; charset=utf-8")


def _row_to_task(r: dict) -> dict:
    detail = r["detail"] or None
    return {
        "id": r["id"], "title": r["title"], "status": r["status"],
        "id_type": r["id_type"], "id_column": r["id_column"],
        "n_ids": r["n_ids"], "cluster_mode": r.get("cluster_mode") or MODE_POOL_MATCH,
        "pool_start": str(r["pool_start"]) if r["pool_start"] else None, "pool_end": str(r["pool_end"]) if r["pool_end"] else None,
        "detail": detail, "result_csv_url": r["result_csv_url"],
        "flat_page_url": r["flat_page_url"], "n_accounts": r["n_accounts"],
        "n_clustered": r["n_clustered"], "n_unclustered": r["n_unclustered"],
        "n_no_emb": r["n_no_emb"], "created_by": r["created_by"],
        "created_by_name": r["created_by_name"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else "",
    }


@app.get("/api/tasks", response_model=TaskListOut)
def list_tasks(decrypted_userinfo: Optional[str] = Header(None)):
    user = _require_user(decrypted_userinfo)
    with _get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM qc_tasks WHERE created_by = %s "
            "ORDER BY created_at DESC LIMIT 100",
            (user["email"],),
        ).fetchall()
    return {"tasks": [_row_to_task(r) for r in rows]}


@app.get("/api/tasks/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: int, decrypted_userinfo: Optional[str] = Header(None)):
    user = _require_user(decrypted_userinfo)
    with _get_db_conn() as conn:
        r = conn.execute("SELECT * FROM qc_tasks WHERE id = %s",
                         (task_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="任务不存在")
        if r["created_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="只能查看自己的任务")
        evs = conn.execute(
            "SELECT stage, message, created_at FROM qc_events "
            "WHERE task_id = %s ORDER BY id ASC LIMIT 200",
            (task_id,),
        ).fetchall()
    out = _row_to_task(r)
    out["events"] = [{"stage": e["stage"], "message": e["message"],
                      "ts": e["created_at"].isoformat() if e["created_at"] else ""}
                     for e in evs]
    return out


class PreviewReq(BaseModel):
    filename: str
    header: list[str]
    rows: list[list] = Field(max_length=300)


@app.post("/api/tasks/preview")
def preview_ids(req: PreviewReq, decrypted_userinfo: Optional[str] = Header(None)):
    """前端把表格解析后的 header+前若干行发来，后端跑 ID 列识别。

    真正的全量识别在跑批侧做；这里给前端「确认页」用的初判。
    """
    _require_user(decrypted_userinfo)
    # 轻量实现：与跑批侧 id_mapper 同一套规则的最小副本
    RE_OID = re.compile(r"^[0-9a-f]{24}$")
    RE_NUM = re.compile(r"^\d{6,}$")
    HINTS = [("uid", ("user_id", "userid", "uid", "账户id", "账号id", "用户id")),
             ("audit_order", ("history_id", "task_id", "版本id", "任务id", "订单号", "物料id"))]

    best = None
    for i, col in enumerate(req.header):
        lname = col.lower()
        vals = [str(r[i]).strip() for r in req.rows if i < len(r) and str(r[i]).strip()]
        hinted = next((t for t, kws in HINTS if any(k in lname for k in kws)), None)
        votes = {}
        for v in vals:
            t = "uid" if RE_OID.match(v.lower()) else ("audit_order" if RE_NUM.match(v) else "unknown")
            votes[t] = votes.get(t, 0) + 1
        top, n = max(votes.items(), key=lambda kv: kv[1]) if votes else ("unknown", 0)
        ratio = n / len(vals) if vals else 0
        if hinted == top and ratio >= 0.9:
            cand = (col, hinted, ratio)
            if best is None or (hinted != "uid" and best[1] == "uid"):
                best = cand
            elif hinted == "uid":
                best = cand
    if not best:
        return {"ok": False, "message": "未识别到支持的 ID 列（支持 user_id / 机审订单号）"}
    return {"ok": True, "id_column": best[0], "id_type": best[1],
            "confidence": round(best[2], 2), "n_sample": len(req.rows)}


@app.post("/api/tasks", status_code=201)
def create_task(
    title: str = Form(...),
    id_type: str = Form(...),
    id_column: str = Form(...),
    cluster_mode: str = Form(MODE_POOL_MATCH),
    pool_start: Optional[str] = Form(None),
    pool_end: Optional[str] = Form(None),
    source_kind: str = Form(...),          # online / offline
    source_ref: str = Form(...),           # 在线表链接 / 文件名
    n_ids: Optional[int] = Form(None),
    file: UploadFile = File(...),           # xlsx/csv 全量内容
    decrypted_userinfo: Optional[str] = Header(None),
):
    user = _require_user(decrypted_userinfo)
    _require_member(user)
    if id_type not in ("uid", "audit_order"):
        raise HTTPException(status_code=400,
                            detail="id_type 仅支持 uid / audit_order（主体类待接入）")
    if cluster_mode not in CLUSTER_MODES:
        raise HTTPException(status_code=400, detail="不支持的聚簇模式")
    # 账户自聚簇只比较本次上传账户，不依赖入驻时间窗，存 NULL；
    # 历史图池匹配需要时间窗圈定池子范围，必须给完整时间窗。
    pool_start = (pool_start or "").strip() or None
    pool_end = (pool_end or "").strip() or None
    if cluster_mode == MODE_POOL_MATCH:
        if not pool_start or not pool_end:
            raise HTTPException(status_code=400, detail="历史图池匹配须选择完整入驻时间窗")
        _check_date(pool_start, "pool_start")
        _check_date(pool_end, "pool_end")
        if pool_start > pool_end:
            raise HTTPException(status_code=400, detail="时间窗起点须早于终点")
    else:
        pool_start = pool_end = None

    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="请填写任务名称")
    if len(clean_title) > 80:
        raise HTTPException(status_code=400, detail="任务名称不能超过 80 个字符")
    filename = (file.filename or source_ref or "").strip()
    suffix = os.path.splitext(filename.lower())[1]
    if suffix not in {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}:
        raise HTTPException(status_code=400,
                            detail="仅支持 xlsx / xlsm / xls / csv / tsv 文件")

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="表格内容为空")
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="表格超过 8MB，请精简后重试")

    with _get_db_conn() as conn:
        # 表格内容落 PG Large Object（Pod 磁盘重启即丢，禁本地盘）。
        # psycopg3 没有 psycopg2 的 conn.lobject()，必须走服务端函数：
        # lo_from_bytea(0, data) 一次性建对象并写入，返回 oid。
        row = conn.execute("SELECT lo_from_bytea(0, %s) AS oid",
                           (content,)).fetchone()
        oid = int(row["oid"])
        r = conn.execute(
            "INSERT INTO qc_tasks (created_by, created_by_name, title, id_type, "
            "id_column, source_kind, source_ref, n_ids, cluster_mode, status, pool_start, pool_end, content_oid) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user["email"], user.get("username"), clean_title, id_type, id_column,
             source_kind, filename, n_ids, cluster_mode, STATUS_PENDING, pool_start, pool_end, oid),
        ).fetchone()
        initial_message = (
            "任务已提交，等待支持账户自聚簇的跑批领取"
            if cluster_mode == MODE_SELF_CLUSTER else "任务已提交，等待跑批领取"
        )
        conn.execute(
            "INSERT INTO qc_events (task_id, stage, message) VALUES (%s,'ingest',%s)",
            (r["id"], initial_message),
        )
        conn.commit()
    return {"id": r["id"], "status": STATUS_PENDING}


@app.post("/api/tasks/{task_id}/retry", status_code=201)
def retry_task(task_id: int, decrypted_userinfo: Optional[str] = Header(None)):
    """复制失败任务及原始文件重新入队，供数据分区补齐后重跑。"""
    user = _require_user(decrypted_userinfo)
    _require_member(user)
    with _get_db_conn() as conn:
        old = conn.execute("SELECT * FROM qc_tasks WHERE id = %s", (task_id,)).fetchone()
        if not old:
            raise HTTPException(status_code=404, detail="任务不存在")
        if old["created_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="只能重试自己的任务")
        if old["status"] != STATUS_FAILED:
            raise HTTPException(status_code=400, detail="仅失败任务可重试")
        if not old.get("content_oid"):
            raise HTTPException(status_code=400, detail="原始文件已丢失，请重新上传")
        data = conn.execute("SELECT lo_get(%s) AS data", (int(old["content_oid"]),)).fetchone()
        if not data or not data["data"]:
            raise HTTPException(status_code=400, detail="原始文件已丢失，请重新上传")
        new_oid = int(conn.execute("SELECT lo_from_bytea(0, %s) AS oid",
                                   (bytes(data["data"]),)).fetchone()["oid"])
        title = re.sub(r"（重试(?:\d+)?）$", "", old["title"]) + "（重试）"
        row = conn.execute(
            "INSERT INTO qc_tasks (created_by, created_by_name, title, id_type, id_column, "
            "source_kind, source_ref, n_ids, cluster_mode, status, pool_start, pool_end, content_oid) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (old["created_by"], old["created_by_name"], title, old["id_type"],
             old["id_column"], old["source_kind"], old["source_ref"], old["n_ids"],
             old.get("cluster_mode") or MODE_POOL_MATCH, STATUS_PENDING,
             old["pool_start"], old["pool_end"], new_oid),
        ).fetchone()
        conn.execute(
            "INSERT INTO qc_events (task_id, stage, message) VALUES (%s,'ingest',%s)",
            (row["id"], f"由任务 #{task_id} 重新提交，等待跑批领取"),
        )
        conn.commit()
    return {"id": row["id"], "status": STATUS_PENDING}


@app.post("/api/tasks/{task_id}/delete")
def delete_task(task_id: int, decrypted_userinfo: Optional[str] = Header(None)):
    """删除自己的非运行中任务，并清理事件及 PG Large Object 原文件。"""
    user = _require_user(decrypted_userinfo)
    with _get_db_conn() as conn:
        with conn.transaction():
            task = conn.execute(
                "SELECT id, created_by, status, content_oid, csv_oid, page_oid FROM qc_tasks "
                "WHERE id = %s FOR UPDATE", (task_id,),
            ).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="任务不存在或已删除")
            if task["created_by"] != user["email"]:
                raise HTTPException(status_code=403, detail="只能删除自己的任务")
            if task["status"] == STATUS_RUNNING:
                raise HTTPException(status_code=409,
                                    detail="任务正在运行，完成或失败后才能删除")
            for oid_key in ("content_oid", "csv_oid", "page_oid"):
                if task.get(oid_key):
                    conn.execute("SELECT lo_unlink(%s)", (int(task[oid_key]),))
            # qc_events 通过 ON DELETE CASCADE 自动清理。
            conn.execute("DELETE FROM qc_tasks WHERE id = %s", (task_id,))
    return {"ok": True, "id": task_id}


# ── 跑批侧 API（共享 token）─────────────────────────────────────────

@app.post("/api/runner/claim")
def claim_task(x_qc_runner_token: Optional[str] = Header(None),
               x_qc_runner_modes: Optional[str] = Header(None)):
    """领取一个最早的 pending 任务：pending → running。

    表格内容从 PG Large Object 读出，随响应返回给跑批。
    """
    _require_runner_token(x_qc_runner_token)
    try:
        return _claim_task_inner(x_qc_runner_modes)
    except HTTPException:
        raise
    except Exception as e:
        # 平台把未捕获异常压成纯文本 500，跑批侧完全看不到原因；
        # 这里显式带回异常类型与消息，便于定位。
        import traceback
        return JSONResponse(status_code=500, content={
            "ok": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "where": traceback.format_exc().strip().splitlines()[-3:],
        })


def _claim_task_inner(raw_modes: Optional[str] = None):
    """按跑批声明的能力领取任务。

    未升级的老跑批没有 X-Qc-Runner-Modes header，只能领取历史图池匹配，
    从而避免把自聚簇任务误按旧逻辑执行。
    """
    supported_modes = {MODE_POOL_MATCH}
    if raw_modes:
        supported_modes = {
            mode.strip() for mode in raw_modes.split(",")
            if mode.strip() in CLUSTER_MODES
        } or {MODE_POOL_MATCH}
    with _get_db_conn() as conn:
        # 兜底：跑批中断（容器重启/崩溃）会让任务卡在 running——
        # 领取前把超过 2 小时未回写的置失败，避免永久卡死。
        # 单独提交：这条失败不能污染后续领取事务（否则整个 claim 500）。
        try:
            conn.execute(
                "UPDATE qc_tasks SET status = %s, "
                "detail = '跑批超时未回写（>2小时），已自动置失败' "
                "WHERE status = %s "
                "AND started_at < NOW() - make_interval(hours => %s)",
                (STATUS_FAILED, STATUS_RUNNING, 2),
            )
            conn.commit()
        except Exception:
            conn.rollback()

        with conn.transaction():
            r = conn.execute(
                "SELECT * FROM qc_tasks WHERE status = %s AND cluster_mode = ANY(%s) "
                "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
                (STATUS_PENDING, list(supported_modes)),
            ).fetchone()
            if not r:
                return JSONResponse({"ok": False, "message": "队列空"})

            # 先读表格内容，再翻状态：读失败就直接置败并给出明确原因，
            # 不能静默返回空表格——跑批拿着空数据只会报个莫名其妙的错。
            # psycopg3 无 conn.lobject()，用服务端 lo_get(oid) 取 bytea。
            content = b""
            read_err = None
            oid = r.get("content_oid")
            if oid:
                try:
                    oid = int(oid)
                    row = conn.execute("SELECT lo_get(%s) AS data", (oid,)).fetchone()
                    content = bytes(row["data"]) if row and row["data"] is not None else b""
                    if not content:
                        read_err = "表格内容为空"
                except Exception as e:
                    read_err = f"表格内容读取失败：{type(e).__name__}"
            else:
                read_err = "任务缺少表格内容引用"

            if read_err:
                conn.execute(
                    "UPDATE qc_tasks SET status = %s, detail = %s, finished_at = NOW() "
                    "WHERE id = %s", (STATUS_FAILED, read_err, r["id"]),
                )
                conn.execute(
                    "INSERT INTO qc_events (task_id, stage, message) "
                    "VALUES (%s,'runner',%s)", (r["id"], read_err),
                )
                # 事务上下文退出时自动提交，psycopg3 禁止在此显式 commit
                return JSONResponse({"ok": False, "message": read_err})

            conn.execute(
                "UPDATE qc_tasks SET status = %s, started_at = NOW() WHERE id = %s",
                (STATUS_RUNNING, r["id"]),
            )
            conn.execute(
                "INSERT INTO qc_events (task_id, stage, message) "
                "VALUES (%s,'runner','任务已被跑批领取')", (r["id"],),
            )
        return {
            "ok": True,
            "task": {
                "id": r["id"], "title": r["title"], "id_type": r["id_type"],
                "id_column": r["id_column"], "source_kind": r["source_kind"],
                "source_ref": r["source_ref"],
                "cluster_mode": r.get("cluster_mode") or MODE_POOL_MATCH,
                "pool_start": str(r["pool_start"]) if r["pool_start"] else None, "pool_end": str(r["pool_end"]) if r["pool_end"] else None,
                "created_by": r["created_by"],
            },
            "table_content_b64": base64.b64encode(content).decode(),
        }


class EventReq(BaseModel):
    stage: str
    message: str


@app.post("/api/runner/{task_id}/requeue")
def requeue_task(task_id: int, x_qc_runner_token: Optional[str] = Header(None)):
    """把卡在 running 的任务放回 pending，供立即重跑。

    正常兜底要等 2 小时才自动置败；跑批中途被杀（容器重启、手工中断）时
    用这个立刻回队，不必等兜底。
    """
    _require_runner_token(x_qc_runner_token)
    with _get_db_conn() as conn:
        r = conn.execute("SELECT status, content_oid FROM qc_tasks WHERE id = %s",
                         (task_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="任务不存在")
        if r["status"] not in (STATUS_RUNNING, STATUS_FAILED):
            raise HTTPException(status_code=400,
                                detail=f"仅 running/failed 可回队，当前 {r['status']}")
        if not r["content_oid"]:
            raise HTTPException(status_code=400, detail="任务表格内容引用已丢失，无法重跑")
        conn.execute(
            "UPDATE qc_tasks SET status = %s, started_at = NULL, finished_at = NULL, "
            "detail = NULL WHERE id = %s", (STATUS_PENDING, task_id),
        )
        conn.execute(
            "INSERT INTO qc_events (task_id, stage, message) "
            "VALUES (%s,'runner','任务已放回队列，等待重新领取')", (task_id,),
        )
        conn.commit()
    return {"ok": True, "id": task_id, "status": STATUS_PENDING}


@app.post("/api/runner/diag")
def runner_diag(x_qc_runner_token: Optional[str] = Header(None)):
    """诊断：逐步跑 claim 里的关键 SQL，把真实异常文本带回来。

    平台把未捕获异常统一成纯文本 500，日志又拿不到，
    只能靠这个接口定位。token 保护，不外泄给普通用户。
    """
    _require_runner_token(x_qc_runner_token)
    steps = []

    def step(name, sql, params=()):
        """每条单独连接执行，避免一条失败污染后续判断。"""
        try:
            with _get_db_conn() as c:
                cur = c.execute(sql, params)
                res = cur.fetchone() if cur.description else f"rowcount={cur.rowcount}"
                c.rollback()
            steps.append({"step": name, "ok": True, "result": str(res)[:200]})
        except Exception as e:
            steps.append({"step": name, "ok": False,
                          "error": f"{type(e).__name__}: {str(e)[:300]}"})

    step("select_pending",
         "SELECT id, content_oid FROM qc_tasks WHERE status = %s ORDER BY created_at ASC LIMIT 1",
         (STATUS_PENDING,))
    step("reaper_interval_literal",
         "UPDATE qc_tasks SET status = status "
         "WHERE status = %s AND started_at < NOW() - INTERVAL '2 hours'",
         (STATUS_RUNNING,))
    step("reaper_make_interval",
         "UPDATE qc_tasks SET status = status "
         "WHERE status = %s AND started_at < NOW() - make_interval(hours => %s)",
         (STATUS_RUNNING, 2))
    step("for_update_skip_locked",
         "SELECT id FROM qc_tasks WHERE status = %s "
         "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
         (STATUS_PENDING,))
    try:
        with _get_db_conn() as c:
            r = c.execute(
                "SELECT content_oid FROM qc_tasks WHERE status = %s "
                "ORDER BY created_at ASC LIMIT 1", (STATUS_PENDING,)).fetchone()
        if r and r["content_oid"]:
            oid = int(r["content_oid"])
            step(f"lo_get({oid})", "SELECT length(lo_get(%s)) AS n", (oid,))
    except Exception as e:
        steps.append({"step": "lo_probe", "ok": False, "error": str(e)[:200]})
    return {"steps": steps}


@app.post("/api/runner/{task_id}/event")
def runner_event(task_id: int, req: EventReq,
                 x_qc_runner_token: Optional[str] = Header(None)):
    _require_runner_token(x_qc_runner_token)
    with _get_db_conn() as conn:
        conn.execute(
            "INSERT INTO qc_events (task_id, stage, message) VALUES (%s,%s,%s)",
            (task_id, req.stage[:32], req.message[:500]),
        )
        conn.commit()
    return {"ok": True}


class ResultReq(BaseModel):
    status: str                      # done / failed
    detail: Optional[str] = None
    result_csv_url: Optional[str] = None
    flat_page_url: Optional[str] = None
    n_accounts: Optional[int] = None
    n_clustered: Optional[int] = None
    n_unclustered: Optional[int] = None
    n_no_emb: Optional[int] = None


@app.post("/api/runner/{task_id}/result")
def runner_result(task_id: int, req: ResultReq,
                  x_qc_runner_token: Optional[str] = Header(None)):
    _require_runner_token(x_qc_runner_token)
    if req.status not in (STATUS_DONE, STATUS_FAILED):
        raise HTTPException(status_code=400, detail="status 须为 done/failed")
    with _get_db_conn() as conn:
        cur = conn.execute(
            "UPDATE qc_tasks SET status=%s, detail=%s, "
            "result_csv_url=COALESCE(%s, result_csv_url), "
            "flat_page_url=COALESCE(%s, flat_page_url), "
            "n_accounts=%s, n_clustered=%s, n_unclustered=%s, "
            "n_no_emb=%s, finished_at=NOW() WHERE id=%s AND status=%s",
            (req.status, req.detail, req.result_csv_url, req.flat_page_url,
             req.n_accounts, req.n_clustered, req.n_unclustered, req.n_no_emb,
             task_id, STATUS_RUNNING),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务不在 running 态或不存在")
        conn.execute(
            "INSERT INTO qc_events (task_id, stage, message) VALUES (%s,'runner',%s)",
            (task_id, f"任务结束：{req.status}" + (f" — {req.detail}" if req.detail else "")),
        )
        conn.commit()
    return {"ok": True}


class ArtifactReq(BaseModel):
    kind: str                      # csv / page
    filename: str
    content_b64: str


@app.post("/api/runner/{task_id}/artifact")
def runner_artifact(task_id: int, req: ArtifactReq,
                    x_qc_runner_token: Optional[str] = Header(None)):
    """跑批上传产物（结果表 / 聚簇网页），存 PG Large Object，返回同源下载链接。"""
    _require_runner_token(x_qc_runner_token)
    if req.kind not in ("csv", "page"):
        raise HTTPException(status_code=400, detail="kind 仅支持 csv / page")
    try:
        content = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 不是合法 base64")
    if not content:
        raise HTTPException(status_code=400, detail="产物内容为空")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="产物超过 20MB，请精简")
    col_oid = "csv_oid" if req.kind == "csv" else "page_oid"
    col_url = "result_csv_url" if req.kind == "csv" else "flat_page_url"
    url = f"api/tasks/{task_id}/file?kind={req.kind}"
    with _get_db_conn() as conn:
        with conn.transaction():
            r = conn.execute(
                f"SELECT id, {col_oid} AS old_oid FROM qc_tasks "
                "WHERE id = %s FOR UPDATE", (task_id,),
            ).fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="任务不存在")
            oid = int(conn.execute("SELECT lo_from_bytea(0, %s) AS oid",
                                   (content,)).fetchone()["oid"])
            if r["old_oid"]:
                conn.execute("SELECT lo_unlink(%s)", (int(r["old_oid"]),))
            conn.execute(
                f"UPDATE qc_tasks SET {col_url} = %s, {col_oid} = %s WHERE id = %s",
                (url, oid, task_id),
            )
    return {"ok": True, "url": url}


@app.get("/api/tasks/{task_id}/file")
def task_file(task_id: int, kind: str,
              decrypted_userinfo: Optional[str] = Header(None)):
    """任务产物下载（仅本人）：kind=csv 结果表 / kind=page 聚簇网页。"""
    user = _require_user(decrypted_userinfo)
    if kind not in ("csv", "page"):
        raise HTTPException(status_code=400, detail="kind 仅支持 csv / page")
    col_oid = "csv_oid" if kind == "csv" else "page_oid"
    with _get_db_conn() as conn:
        r = conn.execute(
            f"SELECT created_by, {col_oid} AS oid FROM qc_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="任务不存在")
    if r["created_by"] != user["email"]:
        raise HTTPException(status_code=403, detail="只能下载自己任务的产物")
    if not r["oid"]:
        raise HTTPException(status_code=404, detail="产物尚未生成")
    with _get_db_conn() as conn:
        row = conn.execute("SELECT lo_get(%s) AS data", (int(r["oid"]),)).fetchone()
    if not row or not row["data"]:
        raise HTTPException(status_code=404, detail="产物内容已丢失")
    media = "text/csv; charset=utf-8" if kind == "csv" else "text/html; charset=utf-8"
    disp = "inline" if kind == "page" else "attachment"
    fname = f"task_{task_id}.{'csv' if kind == 'csv' else 'html'}"
    return Response(content=bytes(row["data"]), media_type=media,
                    headers={"Content-Disposition": f'{disp}; filename="{fname}"'})
