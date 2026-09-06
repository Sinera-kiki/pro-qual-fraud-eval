"""高危模板人工复核后端 —— platform fastapi-only.

链路：从 runs/wXX/ 读 reps.json + batches.json（VLM 判定）→ 前端网格展示 →
人工勾选保留/剔除 → POST /submit 后端同进程调用 high_risk_media_bot.py
完成入库。

数据只写 PG（Pod 磁盘 redeploy 会丢），业务表两张：
  - review_decisions：每个 (run_tag, cluster_id) 的人工决定
  - submit_runs：入库任务的执行记录

runs/ 目录本身放在 Pod 磁盘（`/tmp/hrm_runs/`），入库前用户可通过 POST
/api/runs 上传本 run 的三份 CSV + embedding 补齐，或直接调 prepare 生成。
本轮先只支持"从已存在 runs/ 挑一个未入库的 run 展示"作为最小闭环。
"""
from __future__ import annotations

import json
import os
import re
import time
import hmac
import hashlib
from types import SimpleNamespace

import high_risk_media_bot as bot
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

# UploadFile / File / Form 用于上传端点
try:
    from fastapi import UploadFile, File, Form
except Exception:
    UploadFile = None  # type: ignore
    File = None  # type: ignore
    Form = None  # type: ignore

# 白名单校验：run tag 只允许 w + 数字（如 w35 / w36），阻挡任何路径穿越 / 命令注入
_TAG_RE = re.compile(r"^w\d{1,4}$")
# upload run tag = "upload-" + 短名（中文/字母/数字/横线/下划线均可）
# 短名部分用禁止列表防路径穿越：不允许空白/斜杠/反斜杠/点/控制符
_TAG_UPLOAD_BAD = re.compile(r"[\s/\\\.\x00-\x1f]")



def _safe_tag(tag: str) -> str:
    """把 tag 收窄到白名单：聚簇 wNN 或 upload-<safe短名>。避免其后拼进 argv / 路径。"""
    tag = (tag or "").strip()
    if _TAG_RE.fullmatch(tag):
        return tag
    if tag.startswith("upload-"):
        short = tag[len("upload-"):]
        if short and len(short) <= 24 and not _TAG_UPLOAD_BAD.search(short) and short not in (".", ".."):
            return tag
    raise HTTPException(status_code=400, detail=f"invalid run tag: {tag!r}")

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, StringConstraints


# ---------------------------------------------------------------------------
# properties / DB
# ---------------------------------------------------------------------------

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
        host=p["db.host"],
        port=int(p["db.port"]),
        dbname=p["db.database"],
        user=p["db.username"],
        password=p["db.password"],
        row_factory=dict_row,
    )


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------

def _parse_sso_user(decrypted_userinfo: Optional[str]) -> Optional[dict]:
    if not decrypted_userinfo:
        return None
    try:
        fixed = decrypted_userinfo.encode("latin-1").decode("utf-8")
        data = json.loads(fixed)
    except Exception:
        return None
    return {
        "userId":   data.get("userId") or data.get("id"),
        "username": data.get("username") or data.get("name") or data.get("displayName"),
        "email":    data.get("email") or data.get("workEmail"),
    }


def _require_user(decrypted_userinfo: Optional[str]) -> dict:
    user = _parse_sso_user(decrypted_userinfo)
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user


# ---------------------------------------------------------------------------
# runs/ 数据目录（Pod 内）
# ---------------------------------------------------------------------------

RUNS_ROOT = Path(os.environ.get("HRM_RUNS_ROOT", "/tmp/hrm_runs"))
BOT_PATH = os.environ.get("HRM_BOT_PATH", "/app/high_risk_media_bot.py")


def _list_run_tags() -> list[str]:
    """扫 runs 根目录：
       - 聚簇 run（wXX）：必须有 reps.json + batches.json
       - upload run：有 input_meta.json 即列出（VLM 未跑完的也算，前端好看到状态）
    """
    if not RUNS_ROOT.exists():
        return []
    out = []
    for p in sorted(RUNS_ROOT.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        if name.startswith("upload-"):
            if (p / "input_meta.json").exists():
                out.append(name)
        else:
            if (p / "reps.json").exists() and (p / "batches.json").exists():
                out.append(name)
    return out


def _run_dir(tag: str) -> Path:
    tag = _safe_tag(tag)
    d = RUNS_ROOT / tag
    # 二次校验：解析后仍在 RUNS_ROOT 下（防符号链接 / .. 兜底）
    d_resolved = d.resolve()
    root_resolved = RUNS_ROOT.resolve()
    if not str(d_resolved).startswith(str(root_resolved)):
        raise HTTPException(status_code=400, detail=f"tag escapes runs root")
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"run {tag} not found")
    return d


def _load_vlm_verdicts(state_dir: Path) -> dict[str, dict]:
    """从 batches.json 里读 VLM 判定 → {qualification_url: verdict}"""
    p = state_dir / "batches.json"
    if not p.exists():
        return {}
    batches = json.loads(p.read_text())
    out: dict[str, dict] = {}
    for b in batches:
        vlm = b.get("vlm_result") or []
        for i, row in enumerate(b.get("rows", [])):
            r = next((x for x in vlm if x.get("idx") == i + 1), None) or {}
            if r:
                out[row["qualification_url"]] = r
    return out


def _run_status(tag: str) -> dict:
    """判断 run 是否已入库 —— 看 report.csv 存在且有非空 sample_id。"""
    d = _run_dir(tag)
    report = d / "report.csv"
    if not report.exists():
        return {"submitted": False, "sample_count": 0}
    import csv
    count = 0
    with open(report, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("sample_id") and r["sample_id"] not in ("", "None"):
                count += 1
    return {"submitted": count > 0, "sample_count": count}


# ---------------------------------------------------------------------------
# Pydantic DTO
# ---------------------------------------------------------------------------

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RepItem(BaseModel):
    """代表图卡片"""
    cluster_id: str
    user_id: str
    qualification_url: str
    trade_first_name: Optional[str] = None
    trade_second_name: Optional[str] = None
    cluster_size: int = Field(0, alias="_cluster_size")
    pick_reason: str = Field("", alias="_pick_reason")
    ranked_urls: list[str] = Field(default_factory=list, alias="_ranked_urls")
    vlm_is_electronic: Optional[bool] = None
    vlm_confidence: Optional[str] = None
    vlm_reason: Optional[str] = None
    # 人工决定
    human_decision: str = "auto"   # "keep" / "drop" / "auto"
    default_decision: str = "keep"  # 展示时默认

    class Config:
        populate_by_name = True


class RunDetail(BaseModel):
    tag: str
    submitted: bool
    sample_count: int
    reps: list[RepItem]
    summary: dict


class RunListItem(BaseModel):
    tag: str
    submitted: bool
    sample_count: int
    total_reps: int
    vlm_status: Optional[str] = None  # pending / running / ready / failed
    creator_email: Optional[str] = None
    created_at: Optional[str] = None


class RunListOut(BaseModel):
    runs: list[RunListItem]


class DecisionIn(BaseModel):
    cluster_id: str
    decision: str  # "keep" / "drop"


class ReviewSaveIn(BaseModel):
    decisions: list[DecisionIn]


class SubmitOut(BaseModel):
    submitted: bool
    total: int
    ok: int
    duplicated: int
    failed: int
    sample_ids: list[int]
    report_rows: list[dict]
    log_tail: str


class WhoamiOut(BaseModel):
    userId: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="高危模板人工复核")

ALLOW_ORIGIN_REGEX = (
    r"^https://("
    r"[\w.-]*\.?example\.com"
    r"|picasso-private-\d+\.cos\.[\w-]+\.myqcloud\.com"
    r")$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
def _resume_orphan_vlm_runs():
    """启动时扫描 runs/upload-*：
    - vlm.status == pending：从没启动过（或 pod redeploy 前挂了），直接重跑
    - vlm.status == running 但 heartbeat 超时（>60s）：老 pod 死掉的僵尸，重跑
    - vlm.status == ready / failed：不管
    """
    if not RUNS_ROOT.exists():
        return
    try:
        now = datetime.utcnow()
        for p in sorted(RUNS_ROOT.iterdir()):
            if not (p.is_dir() and p.name.startswith("upload-")):
                continue
            if not (p / "input.csv").exists() or not (p / "input_meta.json").exists():
                continue
            status_file = p / "vlm.status"
            if not status_file.exists():
                continue
            try:
                status = status_file.read_text().strip()
            except Exception:
                continue
            should_resume = False
            if status == "pending":
                should_resume = True
            elif status == "running":
                hb = p / "vlm.heartbeat"
                if not hb.exists():
                    should_resume = True
                else:
                    try:
                        last = datetime.fromisoformat(hb.read_text().strip())
                        if (now - last).total_seconds() > 60:
                            should_resume = True
                    except Exception:
                        should_resume = True
            if should_resume:
                print(f"[startup] resuming orphan VLM run: {p.name} (status={status})")
                (p / "vlm.log").write_text(
                    (p / "vlm.log").read_text() + "\n---resume after redeploy---\n"
                    if (p / "vlm.log").exists() else "resume after redeploy\n"
                )
                _spawn_vlm_worker(p, p.name)
    except Exception as e:
        print(f"[startup] resume error: {type(e).__name__}: {e}")

    # 清理 DB 里目录已不存在的孤儿记录（不 fatal，出错不阻断启动）
    try:
        existing = set(_list_run_tags())
        with _get_db_conn() as conn:
            db_tags = {
                r["run_tag"] for r in conn.execute(
                    "SELECT DISTINCT run_tag FROM review_decisions"
                ).fetchall()
            } | {
                r["run_tag"] for r in conn.execute(
                    "SELECT DISTINCT run_tag FROM submit_runs"
                ).fetchall()
            }
            orphans = db_tags - existing
            for t in orphans:
                if not t.startswith("upload-"):
                    continue  # 只清 upload，聚簇 wNN 记录保留
                conn.execute("DELETE FROM review_decisions WHERE run_tag = %s", (t,))
                conn.execute("DELETE FROM submit_runs WHERE run_tag = %s", (t,))
                print(f"[startup] cleaned DB orphan: {t}")
            conn.commit()
    except Exception as e:
        print(f"[startup] db cleanup error: {type(e).__name__}: {e}")


@app.post("/api/runs/{tag}/retry")
def retry_vlm(
    tag: str,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """手动重跑 VLM。仅对 upload run 且状态非 running 时允许。"""
    _require_user(decrypted_userinfo)
    tag = _safe_tag(tag)
    if not tag.startswith("upload-"):
        raise HTTPException(status_code=403, detail="只能重跑 upload run")
    d = RUNS_ROOT / tag
    if not d.exists() or not (d / "input.csv").exists():
        raise HTTPException(status_code=404, detail=f"run {tag} 数据不完整，无法重跑")
    status_file = d / "vlm.status"
    if status_file.exists() and status_file.read_text().strip() == "running":
        # 心跳超过 60s 才允许强制重跑
        hb = d / "vlm.heartbeat"
        stale = True
        if hb.exists():
            try:
                last = datetime.fromisoformat(hb.read_text().strip())
                stale = (datetime.utcnow() - last).total_seconds() > 60
            except Exception:
                stale = True
        if not stale:
            raise HTTPException(status_code=409, detail="worker 正在跑，请等它完成再重试")
    (d / "vlm.status").write_text("pending")
    _spawn_vlm_worker(d, tag)
    return {"ok": True, "tag": tag}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ts": int(time.time())}


@app.get("/api")
def api_index() -> dict:
    return {
        "service": "qual-hrm-review",
        "runs_root": str(RUNS_ROOT),
        "bot_path": BOT_PATH,
        "endpoints": {
            "whoami":  "GET  /whoami",
            "list":    "GET  /api/runs",
            "detail":  "GET  /api/runs/{tag}",
            "save":    "POST /api/runs/{tag}/review",
            "submit":  "POST /api/runs/{tag}/submit",
        },
    }


# 同源前端托管：直接从后端 / 返回 index.html，避免跨域 cookie 丢失
from fastapi.responses import FileResponse

@app.get("/")
def index() -> FileResponse:
    return FileResponse("frontend/index.html", media_type="text/html; charset=utf-8")


@app.get("/api/downloads/source")
def download_source_bundle(
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> FileResponse:
    """下载脱敏源码包，仅允许已登录的公司用户访问。"""
    _require_user(decrypted_userinfo)
    bundle = Path("高危模版自动上传网站-源码包.zip")
    if not bundle.exists():
        raise HTTPException(status_code=404, detail="源码包暂不可用")
    return FileResponse(
        bundle,
        media_type="application/zip",
        filename="高危模版自动上传网站-源码包.zip",
    )


@app.get("/debug/headers")
def debug_headers(request: Request):
    """探针：看 guard 转发进来了哪些 header（重点看是否有 Cookie）"""
    return {
        "headers": {k: (v if k.lower() != "cookie" else v[:80] + "...") for k, v in request.headers.items()},
        "cookies": {k: v[:20] + "..." for k, v in request.cookies.items()},
    }


@app.get("/whoami", response_model=WhoamiOut)
def whoami(decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo")):
    return _require_user(decrypted_userinfo)


# ---------------------------------------------------------------------------
# 复核业务
# ---------------------------------------------------------------------------

@app.get("/api/runs", response_model=RunListOut)
def list_runs(decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo")):
    """列出所有 run（聚簇 + 上传），前端按 tag 前缀分 tab。"""
    _require_user(decrypted_userinfo)
    out = []
    for tag in _list_run_tags():
        d = RUNS_ROOT / tag
        reps_path = d / "reps.json"
        # upload run 在 VLM 完成前可能没有 reps.json
        total_reps = 0
        if reps_path.exists():
            total_reps = len(json.loads(reps_path.read_text()))
        st = _run_status(tag)
        # 读 upload run 特有元信息
        meta_path = d / "input_meta.json"
        vlm_status_path = d / "vlm.status"
        vlm_status: Optional[str] = "ready"  # 聚簇 run 默认 ready
        creator_email = None
        created_at = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                creator_email = meta.get("creator_email")
                created_at = meta.get("created_at")
            except Exception:
                pass
        if vlm_status_path.exists():
            try:
                vlm_status = vlm_status_path.read_text().strip() or "ready"
            except Exception:
                vlm_status = "ready"
        out.append({
            "tag": tag,
            "submitted": st["submitted"],
            "sample_count": st["sample_count"],
            "total_reps": total_reps,
            "vlm_status": vlm_status,
            "creator_email": creator_email,
            "created_at": created_at,
        })
    # 排序：upload run 优先按 created_at 降序（新的在前）；聚簇 wNN 按 tag 倒序（w35 在前）
    def _sort_key(r):
        is_upload = r["tag"].startswith("upload-")
        # is_upload 组内按 created_at 降序（None 排最后）
        # 聚簇组内按 tag 倒序
        if is_upload:
            return (0, -1 * (int(datetime.fromisoformat(r["created_at"]).timestamp()) if r["created_at"] else 0))
        return (1, r["tag"])  # w34, w35 → 反向排会导致 w35 在前
    out.sort(key=_sort_key)
    # 聚簇部分再翻转（tag 降序）
    upload_runs = [r for r in out if r["tag"].startswith("upload-")]
    cluster_runs = sorted([r for r in out if not r["tag"].startswith("upload-")], key=lambda r: r["tag"], reverse=True)
    return {"runs": upload_runs + cluster_runs}


@app.get("/api/runs/{tag}", response_model=RunDetail)
def run_detail(
    tag: str,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """返回该 run 全部代表图 + VLM 判定 + 已保存的人工决定。"""
    _require_user(decrypted_userinfo)
    d = _run_dir(tag)
    reps_path = d / "reps.json"
    if not reps_path.exists():
        # upload run 处于「VLM 未跑完 / 上一 pod 残留」状态：告诉前端明确原因
        status_file = d / "vlm.status"
        status_text = status_file.read_text().strip() if status_file.exists() else "unknown"
        raise HTTPException(
            status_code=409,
            detail=(
                f"批次 {tag} 尚未生成代表图（VLM 状态：{status_text}）。"
                f"如果是 redeploy 后残留，请调用 DELETE /api/runs/{tag} 清理后重建。"
            ),
        )
    reps_raw = json.loads(reps_path.read_text())
    verdicts = _load_vlm_verdicts(d)

    # 读已保存的人工决定
    saved: dict[str, str] = {}
    with _get_db_conn() as conn:
        rows = conn.execute(
            "SELECT cluster_id, decision FROM review_decisions"
            " WHERE run_tag = %s",
            (tag,),
        ).fetchall()
        for r in rows:
            saved[r["cluster_id"]] = r["decision"]

    reps: list[RepItem] = []
    kept = dropped_e = dropped_h = 0
    for r in reps_raw:
        url = r["qualification_url"]
        v = verdicts.get(url, {})
        is_e = v.get("is_electronic")
        # 默认跟 VLM：非电子版 keep / 电子版 drop
        default_dec = "drop" if is_e else "keep"
        human = saved.get(str(r["cluster_id"]), "auto")
        final = default_dec if human == "auto" else human
        if final == "keep":
            kept += 1
        elif is_e and final == "drop" and human == "auto":
            dropped_e += 1
        else:
            dropped_h += 1
        reps.append(RepItem(
            cluster_id=str(r["cluster_id"]),
            user_id=r["user_id"],
            qualification_url=url,
            trade_first_name=r.get("trade_first_name"),
            trade_second_name=r.get("trade_second_name"),
            _cluster_size=r.get("_cluster_size", 0),
            _pick_reason=r.get("_pick_reason", ""),
            _ranked_urls=r.get("_ranked_urls", []),
            vlm_is_electronic=is_e,
            vlm_confidence=v.get("confidence"),
            vlm_reason=v.get("reason"),
            human_decision=human,
            default_decision=default_dec,
        ))

    st = _run_status(tag)
    summary = {
        "total": len(reps),
        "will_keep": sum(1 for x in reps if (x.human_decision or x.default_decision) == "keep"
                          or (x.human_decision == "auto" and x.default_decision == "keep")),
        "will_drop": sum(1 for x in reps if (x.human_decision == "drop")
                          or (x.human_decision == "auto" and x.default_decision == "drop")),
        "vlm_electronic": sum(1 for x in reps if x.vlm_is_electronic),
        "human_overrides": sum(1 for x in reps if x.human_decision != "auto"),
    }
    return {
        "tag": tag,
        "submitted": st["submitted"],
        "sample_count": st["sample_count"],
        "reps": reps,
        "summary": summary,
    }


@app.delete("/api/runs/{tag}")
def delete_run(
    tag: str,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """清理 run：删目录 + 清数据库记录。仅允许 upload run，聚簇 wXX 禁删。"""
    user = _require_user(decrypted_userinfo)
    tag = _safe_tag(tag)
    if not tag.startswith("upload-"):
        raise HTTPException(status_code=403, detail=f"只能删 upload run，不能删聚簇 run {tag}")
    d = RUNS_ROOT / tag
    status = _run_status(tag)
    if status["submitted"]:
        raise HTTPException(status_code=409, detail="该批次已入库，禁止删除")
    vlm_status_path = d / "vlm.status"
    if vlm_status_path.exists() and vlm_status_path.read_text().strip() in ("pending", "running"):
        raise HTTPException(status_code=409, detail="该批次仍在识别中，完成或失败后才能删除")
    removed_dir = False
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        removed_dir = True
    # 清数据库
    with _get_db_conn() as conn:
        conn.execute("DELETE FROM review_decisions WHERE run_tag = %s", (tag,))
        conn.execute("DELETE FROM submit_runs WHERE run_tag = %s", (tag,))
        conn.commit()
    return {"ok": True, "tag": tag, "removed_dir": removed_dir, "operator": user["email"]}


@app.post("/api/runs/{tag}/review")
def save_review(
    tag: str,
    body: ReviewSaveIn,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """保存人工勾选。decision ∈ {keep, drop, auto}，auto 代表清除人工决定。"""
    user = _require_user(decrypted_userinfo)
    _run_dir(tag)  # 校验存在
    with _get_db_conn() as conn:
        for d in body.decisions:
            if d.decision == "auto":
                conn.execute(
                    "DELETE FROM review_decisions WHERE run_tag=%s AND cluster_id=%s",
                    (tag, d.cluster_id),
                )
            elif d.decision in ("keep", "drop"):
                conn.execute(
                    """
                    INSERT INTO review_decisions (run_tag, cluster_id, decision, reviewer, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (run_tag, cluster_id) DO UPDATE
                    SET decision=EXCLUDED.decision, reviewer=EXCLUDED.reviewer, updated_at=NOW()
                    """,
                    (tag, d.cluster_id, d.decision, user["email"] or user["userId"]),
                )
            else:
                raise HTTPException(status_code=400, detail=f"unknown decision: {d.decision}")
        conn.commit()
    return {"ok": True, "saved": len(body.decisions)}


@app.post("/api/runs/{tag}/submit", response_model=SubmitOut)
def submit_run(
    tag: str,
    request: Request,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """
    人工确认后触发真跑：把 review_decisions 里的 drop 决定合成"整簇跳过"标记
    → 覆盖写 batches.json.vlm_result → 调 high_risk_media_bot.py submit。

    幂等：run 已 submitted 直接返回结果。
    """
    user = _require_user(decrypted_userinfo)
    d = _run_dir(tag)

    st = _run_status(tag)
    if st["submitted"]:
        # 已入库直接回读 report.csv
        return _read_report(d, log_tail="(已入库，跳过实际提交)")

    # 读人工决定
    with _get_db_conn() as conn:
        rows = conn.execute(
            "SELECT cluster_id, decision FROM review_decisions WHERE run_tag=%s",
            (tag,),
        ).fetchall()
        decisions = {r["cluster_id"]: r["decision"] for r in rows}

    # 覆盖 batches.json 的 vlm_result：
    # 人工 keep → is_electronic=false（保留）
    # 人工 drop → is_electronic=true, confidence=high（整簇跳过）
    # 人工 auto → 保留原 VLM 判定
    batches_path = d / "batches.json"
    batches = json.loads(batches_path.read_text())
    for b in batches:
        rows_b = b.get("rows", [])
        vlm = b.get("vlm_result") or []
        vlm_by_idx = {v.get("idx"): v for v in vlm}
        for i, row in enumerate(rows_b):
            cid = str(row.get("cluster_id"))
            dec = decisions.get(cid, "auto")
            if dec == "auto":
                continue
            idx = i + 1
            v = vlm_by_idx.get(idx) or {"idx": idx}
            if dec == "keep":
                v["is_electronic"] = False
                v["confidence"] = "human"
                v["reason"] = f"人工复核保留（覆盖 VLM）by {user['email']}"
            else:  # drop
                v["is_electronic"] = True
                v["confidence"] = "high"
                v["reason"] = f"人工复核剔除 by {user['email']}"
            vlm_by_idx[idx] = v
        b["vlm_result"] = sorted(vlm_by_idx.values(), key=lambda x: x.get("idx", 0))

    # 写回（Pod 磁盘写入 batches.json 仅供本次 submit 读，不长期依赖）
    batches_path.write_text(json.dumps(batches, ensure_ascii=False, indent=2))

    # 同进程调用 bot：Cookie 只作为 Python 对象传入，不进入命令参数或进程环境。
    # 这同时避免命令注入污点链路及 Cookie 泄漏。
    safe_tag = _safe_tag(tag)
    week_tag = safe_tag.upper() if not safe_tag.startswith("upload-") else safe_tag
    submit_args = SimpleNamespace(
        state_dir=str(d.resolve()), week_tag=week_tag,
        dry_run=False, skip_vlm=False, strict_vlm=False,
        out=None, rollback_on_error=False,
    )
    caller_sso = {
        "cookie": request.headers.get("cookie", ""),
        "email": user.get("email", ""),
        "display_name": user.get("username", ""),
    }
    try:
        bot.cmd_submit(submit_args, sso_override=caller_sso)
        exit_code = 0
        log_tail = "同进程提交完成"
    except (Exception, SystemExit) as exc:
        exit_code = 1
        log_tail = f"submit 失败: {type(exc).__name__}: {exc}"

    # 记录本次任务
    with _get_db_conn() as conn:
        conn.execute(
            """INSERT INTO submit_runs (run_tag, operator, exit_code, log_tail, submitted_at)
                VALUES (%s, %s, %s, %s, NOW())""",
            (tag, user["email"] or user["userId"], exit_code, log_tail),
        )
        conn.commit()

    if exit_code != 0:
        raise HTTPException(status_code=500, detail=log_tail)

    result = _read_report(d, log_tail=log_tail)
    return result


# ---------------------------------------------------------------------------
# 上传入口（第二个 tab）
# ---------------------------------------------------------------------------

# 上传文件的临时区（Pod 磁盘，redeploy 会丢；只作 preview -> create 之间的临时存放）
UPLOAD_TMP = Path(os.environ.get("HRM_UPLOAD_TMP", "/tmp/hrm_uploads"))
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)

# input.csv 的自动列名候选
URL_COL_HINTS = ["qualification_url", "资质图URL", "url", "URL", "图片URL", "图片链接"]
USER_ID_HINTS = ["user_id", "uid", "账号id", "账号ID", "用户ID", "userId"]
TRADE_HINTS = ["trade_first_name", "一级行业", "行业", "一级类目", "trade"]

# 短名白名单
# 短名支持中文/字母/数字/横线/下划线，禁止路径分隔符与空白，长度 1~24
# 不用 fullmatch + 允许列表；转而用「禁止列表」——更宽容
_SHORTNAME_BAD = re.compile(r"[\s/\\\.\x00-\x1f]")


def _safe_shortname(s: str) -> str:
    s = (s or "").strip()
    if not s or len(s) > 24 or _SHORTNAME_BAD.search(s) or s in ("..", "."):
        raise HTTPException(
            status_code=400,
            detail=f"invalid short_name: {s!r} —— 1~24 字，不能含空白/斜杠/点",
        )
    return s


def _pick_col(headers: list[str], hints: list[str], override: Optional[str] = None) -> Optional[str]:
    if override:
        return override if override in headers else None
    for h in hints:
        if h in headers:
            return h
    return None


def _parse_shortcut_id(sheet_url: str) -> str:
    m = re.search(r"/sheet/([0-9a-f]{32})", sheet_url) or re.search(r"/doc/([0-9a-f]{32})", sheet_url)
    if not m:
        raise HTTPException(status_code=400, detail=f"无法从 URL 解析 shortcutId")
    return m.group(1)


def _rows_from_redoc(sheet_url: str) -> tuple[list[str], list[list[str]]]:
    """在线 REDoc 直读未接入 HTTP API，统一引导为导出上传。

    不再调用 Platform 容器不存在的 `hi` CLI，彻底移除 subprocess 依赖与安全告警。
    """
    raise HTTPException(
        status_code=501,
        detail="暂不支持 REDoc 在线表格直读，请导出 xlsx/csv 后上传（右上『下载』→ .xlsx）",
    )

def _rows_from_file(file_ref: str) -> tuple[list[str], list[list[str]]]:
    """从上传的临时文件读表；支持 .xlsx / .csv"""
    p = UPLOAD_TMP / file_ref
    if not p.exists():
        raise HTTPException(status_code=404, detail="file_ref 已过期，请重新上传")
    if p.suffix.lower() == ".csv":
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            import csv as _csv
            reader = list(_csv.reader(f))
        if len(reader) < 2:
            raise HTTPException(status_code=400, detail="csv 无有效数据")
        return [c.strip() for c in reader[0]], [[c.strip() for c in row] for row in reader[1:]]
    if p.suffix.lower() in (".xlsx", ".xls"):
        try:
            import openpyxl  # type: ignore
        except ImportError:
            raise HTTPException(status_code=500, detail="服务未装 openpyxl")
        wb = openpyxl.load_workbook(p, read_only=True)
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) >= 2:
                header = [str(c or "").strip() for c in rows[0]]
                body = [[str(c or "").strip() for c in row] for row in rows[1:]]
                return header, body
        raise HTTPException(status_code=400, detail="xlsx 无有效数据")
    raise HTTPException(status_code=400, detail=f"不支持的文件类型: {p.suffix}")


def _analyze_rows(
    header: list[str], body: list[list[str]], url_col_override: Optional[str],
) -> dict:
    """从原始表格提取预览信息"""
    url_col = _pick_col(header, URL_COL_HINTS, url_col_override)
    user_id_col = _pick_col(header, USER_ID_HINTS)
    trade_col = _pick_col(header, TRADE_HINTS)
    if not url_col:
        return {
            "rows_total": len(body),
            "rows_with_url": 0,
            "sample_urls": [],
            "detected_cols": {},
            "headers": header,
            "error": f"未找到 URL 列（尝试的候选: {URL_COL_HINTS}）",
        }
    ui = header.index(url_col)
    urls = []
    for row in body:
        if ui < len(row):
            u = (row[ui] or "").strip()
            if u.startswith("http"):
                urls.append(u)
    return {
        "rows_total": len(body),
        "rows_with_url": len(urls),
        "sample_urls": urls[:6],
        "detected_cols": {
            "url_col": url_col,
            "user_id_col": user_id_col,
            "trade_col": trade_col,
        },
        "headers": header,
    }


class UploadFileOut(BaseModel):
    file_ref: str
    preview: dict


@app.post("/api/uploads/file", response_model=UploadFileOut)
async def upload_file(
    file: UploadFile = File(...),
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """接收 xlsx / csv 上传，返回 file_ref（Pod 磁盘临时文件名）"""
    _require_user(decrypted_userinfo)
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".csv", ".xls")):
        raise HTTPException(status_code=400, detail="仅接受 .xlsx / .csv 文件")
    ext = Path(file.filename).suffix.lower()
    # 随机文件名防碰撞
    import secrets
    file_ref = f"{int(time.time())}-{secrets.token_hex(6)}{ext}"
    dst = UPLOAD_TMP / file_ref
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:  # 50 MB
        raise HTTPException(status_code=413, detail="文件过大（>50MB）")
    dst.write_bytes(content)
    # 立即出预览
    try:
        header, body = _rows_from_file(file_ref)
        preview = _analyze_rows(header, body, None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")
    return {"file_ref": file_ref, "preview": preview}


class UploadPreviewIn(BaseModel):
    source_type: str  # 'redoc' or 'file'
    sheet_url: Optional[str] = None
    file_ref: Optional[str] = None
    url_col: Optional[str] = None


@app.post("/api/uploads/preview")
def upload_preview(
    body: UploadPreviewIn,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """按用户选定的 url_col 重新分析预览。"""
    _require_user(decrypted_userinfo)
    if body.source_type == "redoc":
        if not body.sheet_url:
            raise HTTPException(status_code=400, detail="缺少 sheet_url")
        header, rows = _rows_from_redoc(body.sheet_url)
    elif body.source_type in ("file", "xlsx", "csv"):
        if not body.file_ref:
            raise HTTPException(status_code=400, detail="缺少 file_ref")
        header, rows = _rows_from_file(body.file_ref)
    else:
        raise HTTPException(status_code=400, detail=f"未知 source_type: {body.source_type}")
    return _analyze_rows(header, rows, body.url_col)


class CreateUploadIn(BaseModel):
    short_name: str
    source_type: str  # 'redoc' / 'xlsx' / 'csv'
    url_col: str
    sheet_url: Optional[str] = None
    file_ref: Optional[str] = None


class CreateUploadOut(BaseModel):
    tag: str
    total_urls: int


def _write_input_csv(
    header: list[str],
    body: list[list[str]],
    url_col: str,
    dst: Path,
) -> int:
    """把原始行转换成 input.csv（bot 侧 load_from_input_csv 消费的格式）"""
    ui = header.index(url_col)
    uii = header.index(_pick_col(header, USER_ID_HINTS) or "__none__") if _pick_col(header, USER_ID_HINTS) else -1
    tii = header.index(_pick_col(header, TRADE_HINTS) or "__none__") if _pick_col(header, TRADE_HINTS) else -1
    import csv as _csv
    count = 0
    with open(dst, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["qualification_url", "user_id", "trade_first_name"])
        for row in body:
            if ui >= len(row):
                continue
            u = (row[ui] or "").strip()
            if not u.startswith("http"):
                continue
            uid = row[uii].strip() if uii >= 0 and uii < len(row) else ""
            trade = row[tii].strip() if tii >= 0 and tii < len(row) else ""
            w.writerow([u, uid, trade])
            count += 1
    return count


# ---------------------------------------------------------------------------
# Hi 私聊播报（VLM 判定完成时通知创建人）
# 凭证走 Platform Studio 环境变量表单：APP_HI_APPID / APP_HI_APPSECRET / APP_HI_USER_TOKEN
# 环境变量缺失时静默跳过，功能降级
# ---------------------------------------------------------------------------

HI_TOKEN_API = "https://redcity-open.example.com/openapis/open/token/createAppAccessToken/v2"
HI_CALL_API = "https://redcity-open.example.com/openapis/open/api/call/v2"


def _get_hi_token() -> Optional[str]:
    app_id = os.environ.get("APP_HI_APPID")
    app_secret = os.environ.get("APP_HI_APPSECRET")
    if not app_id or not app_secret:
        return None
    try:
        import requests as _rq
        r = _rq.post(
            HI_TOKEN_API,
            json={"appId": app_id, "appSecret": app_secret},
            timeout=10,
        )
        d = r.json()
        if d.get("success"):
            return (d.get("data") or {}).get("appAccessToken")
    except Exception:
        pass
    return None


def _hi_dm(receiver_email: str, text: str) -> bool:
    """给 receiver_email 发一条 Hi 私聊文本消息；失败静默返 False。"""
    app_id = os.environ.get("APP_HI_APPID")
    user_token = os.environ.get("APP_HI_USER_TOKEN")
    if not (app_id and user_token and receiver_email):
        return False
    token = _get_hi_token()
    if not token:
        return False
    try:
        import requests as _rq
        import secrets as _sec
        biz = {
            "receiverContactId": receiver_email,
            "type": 1,           # TEXT（markdown=10 该应用无权限）
            "data": text[:2000],
            "operateCode": _sec.token_hex(8),
            "refId": "",
        }
        r = _rq.post(
            HI_CALL_API,
            json={
                "appId": app_id,
                "appAccessToken": token,
                "userAccessToken": user_token,
                "apiAlias": "im:message:sendMessageToPersonal:v1",
                "bizParams": json.dumps(biz, ensure_ascii=False),
            },
            timeout=10,
        )
        return bool(r.json().get("success"))
    except Exception:
        return False


def _spawn_vlm_worker(state_dir: Path, tag: str):
    """后台线程跑 prepare --from-input-csv --auto-vlm；用 Popen 便于流式 log 与 pid 记录"""
    def run():
        try:
            (state_dir / "vlm.status").write_text("running")
            (state_dir / "vlm.heartbeat").write_text(datetime.utcnow().isoformat())
            input_csv = state_dir / "input.csv"
            # 同进程后台线程调用，避免把用户上传标签/文件路径传给 subprocess。
            prepare_args = SimpleNamespace(
                from_cluster=False, from_input_csv=str(input_csv),
                from_online=None, from_xlsx=None, from_urls=None,
                annotated=None, cluster_full=None, embedding=None,
                url_col="资质图URL", input_prefix=tag,
                state_dir=str(state_dir), batch_size=10, auto_vlm=True,
            )
            (state_dir / "vlm.log").write_text("同进程 VLM worker started\n", encoding="utf-8")
            bot.cmd_prepare(prepare_args)
            (state_dir / "vlm.heartbeat").write_text(datetime.utcnow().isoformat())
            (state_dir / "vlm.status").write_text("ready")
            _notify_creator(state_dir, tag, ok=True)
        except (Exception, SystemExit) as e:
            try:
                (state_dir / "vlm.status").write_text("failed")
                (state_dir / "vlm.log").write_text(f"worker exception: {type(e).__name__}: {e}")
                _notify_creator(state_dir, tag, ok=False, extra=f"exception: {type(e).__name__}")
            except Exception:
                pass

    import threading
    t = threading.Thread(target=run, daemon=True, name=f"vlm-{tag}")
    t.start()


def _notify_creator(state_dir: Path, tag: str, ok: bool, extra: str = ""):
    """VLM 完成时给创建者发 Hi 私聊"""
    try:
        meta_path = state_dir / "input_meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text())
        email = meta.get("creator_email")
        if not email:
            return
        total = meta.get("total_urls", "?")
        # 统计 reps 数量
        reps_n = 0
        reps_path = state_dir / "reps.json"
        if reps_path.exists():
            try:
                reps_n = len(json.loads(reps_path.read_text()))
            except Exception:
                pass
        if ok:
            text = (
                f"【高危模板复核】上传批次 {tag} 已完成 VLM 判定\n"
                f"共 {reps_n} 张候选（原始 URL {total} 条），可去复核网页处理："
                f"\nhttps://app.example.com/s/qual-hrm-review-backend/"
            )
        else:
            text = f"【高危模板复核】上传批次 {tag} VLM 判定失败 ({extra})，请到网页查日志"
        _hi_dm(email, text)
    except Exception:
        pass


@app.post("/api/uploads", response_model=CreateUploadOut)
def create_upload(
    body: CreateUploadIn,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """创建一个上传批次 run，异步跑 VLM 判定。"""
    user = _require_user(decrypted_userinfo)
    short = _safe_shortname(body.short_name)
    tag = f"upload-{short}"
    d = RUNS_ROOT / tag
    if d.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"批次 {tag} 已存在。如果是残留想重建，"
                f"点批次左侧列表→旁边「删除」按钮清掉后再建；"
                f"或换个短名（比如加上时间/序号）。"
            ),
        )
    d.mkdir(parents=True, exist_ok=True)

    # 读原始数据
    if body.source_type == "redoc":
        if not body.sheet_url:
            raise HTTPException(status_code=400, detail="缺少 sheet_url")
        header, rows = _rows_from_redoc(body.sheet_url)
        source_ref = body.sheet_url
    elif body.source_type in ("xlsx", "csv", "file"):
        if not body.file_ref:
            raise HTTPException(status_code=400, detail="缺少 file_ref")
        header, rows = _rows_from_file(body.file_ref)
        source_ref = body.file_ref
    else:
        raise HTTPException(status_code=400, detail=f"未知 source_type: {body.source_type}")

    if body.url_col not in header:
        raise HTTPException(status_code=400, detail=f"表头无列 '{body.url_col}'")

    # 写 input.csv + input_meta.json
    total = _write_input_csv(header, rows, body.url_col, d / "input.csv")
    if total == 0:
        # 清掉空目录
        try:
            (d / "input.csv").unlink(missing_ok=True)
            d.rmdir()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="表中没有有效的 http URL")
    (d / "input_meta.json").write_text(json.dumps({
        "short_name": short,
        "source_type": body.source_type,
        "source_ref": source_ref,
        "url_col": body.url_col,
        "creator_email": user.get("email"),
        "creator_name": user.get("username"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total_urls": total,
    }, ensure_ascii=False, indent=2))
    (d / "vlm.status").write_text("pending")

    # 起后台线程跑 VLM，立即返回
    _spawn_vlm_worker(d, tag)

    return {"tag": tag, "total_urls": total}


# ---------------------------------------------------------------------------
# 调度平台 自动化上传入口
# ---------------------------------------------------------------------------

def _require_automation_upload_token(token: Optional[str]) -> None:
    """校验 调度平台 服务间调用凭证；凭证哈希持久化在 PostgreSQL。"""
    try:
        with _get_db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                ("automation_upload_token_sha256",),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="调度平台 上传鉴权配置暂不可用") from exc
    expected_hash = (row or {}).get("value")
    if not expected_hash:
        raise HTTPException(status_code=503, detail="调度平台 上传鉴权配置缺失")
    actual_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    if not token or not hmac.compare_digest(actual_hash, expected_hash):
        raise HTTPException(status_code=401, detail="invalid X-Push-Token")


@app.post("/api/push/uploads", response_model=CreateUploadOut)
async def automation_push_upload(
    file: UploadFile = File(...),
    short_name: str = Form(...),
    url_col: Optional[str] = Form(None),
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
):
    """调度平台 批量上传接口（multipart/form-data）。

    调度平台 提交一份 xlsx/csv 后，接口立即创建 upload run 并异步执行 VLM；
    完成后任务会自动出现在人工复核列表。接口只用服务间 Token 鉴权，
    文件和浏览器会话不会互相传递。
    """
    _require_automation_upload_token(x_push_token)
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="仅接受 .xlsx / .xls / .csv 文件")
    short = _safe_shortname(short_name)
    tag = f"upload-{short}"
    d = RUNS_ROOT / tag
    if d.exists():
        raise HTTPException(status_code=409, detail=f"批次 {tag} 已存在；请传入新的 short_name")

    ext = Path(file.filename).suffix.lower()
    import secrets
    file_ref = f"automation-{int(time.time())}-{secrets.token_hex(6)}{ext}"
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件过大（>50MB）")
    (UPLOAD_TMP / file_ref).write_bytes(content)

    try:
        header, rows = _rows_from_file(file_ref)
        selected_url_col = url_col or _pick_col(header, URL_COL_HINTS)
        if not selected_url_col or selected_url_col not in header:
            raise HTTPException(status_code=400, detail="未找到图片 URL 列；请通过 url_col 显式指定")
        d.mkdir(parents=True, exist_ok=False)
        total = _write_input_csv(header, rows, selected_url_col, d / "input.csv")
        if total == 0:
            raise HTTPException(status_code=400, detail="表中没有有效的 http URL")
        (d / "input_meta.json").write_text(json.dumps({
            "short_name": short,
            "source_type": "automation",
            "source_ref": file_ref,
            "url_col": selected_url_col,
            "creator_email": None,
            "creator_name": "调度平台 自动化工作流",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "total_urls": total,
        }, ensure_ascii=False, indent=2))
        (d / "vlm.status").write_text("pending")
        _spawn_vlm_worker(d, tag)
        return {"tag": tag, "total_urls": total}
    except HTTPException:
        if d.exists() and not (d / "input_meta.json").exists():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        raise
    except Exception as exc:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"调度平台 上传解析失败: {type(exc).__name__}: {exc}")


class NotifyClusterIn(BaseModel):
    tag: str
    to_email: str
    sample_count: int = 0
    kept: int = 0
    dropped: int = 0
    dedup: int = 0
    week: Optional[str] = None


@app.post("/api/notify/cluster-done")
def notify_cluster_done(
    body: NotifyClusterIn,
    x_hrm_token: Optional[str] = Header(None, alias="X-HRM-Token"),
):
    """cron 第 8d 步跑完后调用，播报聚簇 run 的复核提醒到指定邮箱。
    需要 APP_HRM_NOTIFY_TOKEN 匹配（放在 openclaw cron payload 里）。
    """
    expected = os.environ.get("APP_HRM_NOTIFY_TOKEN")
    if not expected or x_hrm_token != expected:
        raise HTTPException(status_code=401, detail="invalid notify token")
    text = (
        f"【高危模板复核】{body.week or body.tag} 已完成 VLM 判定\n"
        f"入库 {body.kept} 张 · 电子版剔除 {body.dropped} 张 · 跨周期重复 {body.dedup} 张\n"
        f"复核网页：https://app.example.com/s/qual-hrm-review-backend/"
    )
    ok = _hi_dm(body.to_email, text)
    return {"ok": ok}


class RegisterClusterRunIn(BaseModel):
    tag: str                      # 周期 tag，如 w36
    reps: list[dict]              # bot prepare 产出的 reps.json 内容
    batches: list[dict] = []      # batches.json 内容（含 vlm_result）
    week: Optional[str] = None    # 展示用，如 W36
    notify_email: Optional[str] = None  # 注册完成后私聊提醒谁


@app.post("/api/runs/register-cluster")
def register_cluster_run(
    body: RegisterClusterRunIn,
    x_hrm_token: Optional[str] = Header(None, alias="X-HRM-Token"),
):
    """openclaw 侧的周度 cron 跑完 `prepare --auto-vlm` 后调本接口，
    把代表图清单和 VLM 判定结果推进 pod，让复核网页能看到这一周待复核。

    设计原因：bot 跑在 openclaw 机器上，产物落它本地磁盘；platform pod 的
    RUNS_ROOT(/tmp/hrm_runs) 两边不共享，必须显式推送。

    幂等：同 tag 重复注册会覆盖 reps/batches，但**已 submit 的 run 拒绝覆盖**
    （避免把已入库的周期重置成待复核）。
    """
    expected = os.environ.get("APP_HRM_NOTIFY_TOKEN")
    if not expected or x_hrm_token != expected:
        raise HTTPException(status_code=401, detail="invalid token")

    tag = _safe_tag(body.tag)
    if tag.startswith("upload-"):
        raise HTTPException(status_code=400, detail="本接口只接收聚簇 run（wXX），上传批次走 /api/uploads")
    if not body.reps:
        raise HTTPException(status_code=400, detail="reps 为空，无可复核内容")

    d = RUNS_ROOT / tag
    # 已入库的周期不允许被覆盖回待复核。
    # 这里直接看 report.csv（不走 _run_status，它内部的 _run_dir 在目录还不存在时会 404）。
    if d.exists() and _run_status(tag)["submitted"]:
        raise HTTPException(
            status_code=409,
            detail=f"{tag} 已入库，拒绝覆盖。如需重跑请先在库里删除对应记录",
        )

    d.mkdir(parents=True, exist_ok=True)
    (d / "reps.json").write_text(
        json.dumps(body.reps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "batches.json").write_text(
        json.dumps(body.batches, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "vlm.status").write_text("ready", encoding="utf-8")
    meta = {
        "kind": "cluster",
        "week": body.week or tag.upper(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "registered_by": "cron",
        "rep_count": len(body.reps),
    }
    (d / "input_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 统计 VLM 判定分布，给播报用
    kept = dropped = 0
    for b in body.batches:
        v = (b or {}).get("vlm_result") or {}
        if v.get("is_electronic") and v.get("confidence") == "high":
            dropped += 1
        else:
            kept += 1

    notified = False
    if body.notify_email:
        text = (
            f"【高危模板复核】{body.week or tag.upper()} VLM 判定完成，待你确认\n"
            f"代表图 {len(body.reps)} 张：建议入库 {kept} 张 · VLM 判为纯电子版 {dropped} 张\n"
            f"去复核页勾选后点「确认入库」：\n"
            f"https://app.example.com/s/qual-hrm-review-backend/"
        )
        notified = _hi_dm(body.notify_email, text)

    return {
        "ok": True,
        "tag": tag,
        "rep_count": len(body.reps),
        "vlm_kept": kept,
        "vlm_dropped": dropped,
        "notified": notified,
    }


@app.get("/api/uploads/{tag}/status")
def upload_status(
    tag: str,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
):
    """查询 upload run 的 VLM 判定进度"""
    _require_user(decrypted_userinfo)
    tag = _safe_tag(tag)
    d = RUNS_ROOT / tag
    if not d.exists():
        raise HTTPException(status_code=404, detail="run not found")
    status_file = d / "vlm.status"
    status = status_file.read_text().strip() if status_file.exists() else "unknown"
    # 进度：ready 时 reps.json 已生成
    reps_path = d / "reps.json"
    total = 0
    if reps_path.exists():
        try:
            total = len(json.loads(reps_path.read_text()))
        except Exception:
            pass
    log_tail = ""
    log_path = d / "vlm.log"
    if log_path.exists():
        try:
            log_tail = log_path.read_text()[-2000:]
        except Exception:
            pass
    return {
        "tag": tag,
        "vlm_status": status,
        "total_reps": total,
        "log_tail": log_tail,
    }


# ---------------------------------------------------------------------------
# 报告帮助函数
# ---------------------------------------------------------------------------

def _read_report(d: Path, log_tail: str = "") -> dict:
    import csv
    report = d / "report.csv"
    if not report.exists():
        return {
            "submitted": False, "total": 0, "ok": 0, "duplicated": 0, "failed": 0,
            "sample_ids": [], "report_rows": [], "log_tail": log_tail,
        }
    rows = []
    with open(report, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    ok = [r for r in rows if r.get("step") == "done"]
    dup = [r for r in rows if r.get("error") and "重复" in (r.get("error") or "")]
    err = [r for r in rows if r.get("error") and r.get("step") != "done" and "重复" not in (r.get("error") or "")]
    sample_ids = []
    for r in ok:
        try:
            sid = r.get("sample_id")
            if sid and sid not in ("None", ""):
                sample_ids.append(int(sid))
        except (ValueError, TypeError):
            pass
    return {
        "submitted": True,
        "total": len(rows),
        "ok": len(ok),
        "duplicated": len(dup),
        "failed": len(err),
        "sample_ids": sample_ids,
        "report_rows": rows,
        "log_tail": log_tail,
    }
