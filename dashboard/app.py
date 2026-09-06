"""专业号资质造假浓度评估工作流管理页面 (MVP)

功能：
  - 展示当前周和历史周任务状态、阶段进度
  - 各阶段执行按钮（手动触发，MVP 用占位/mock 实现，方便先看流程）
  - 产物列表：上传/下载
  - 快速跳转标注平台
  - 一键发送 Hi 群提示消息（MVP 只打日志到阶段流水，不真的连 Hi）
  - 违规浓度计算：读入标注结果 CSV，过滤 is_sample_user=True，按 sample_group 计算浓度

限制说明：
  - 平台注入 db.properties 才能真正持久化，本地 dev 缺 db 时会退化为“只读演示模式”
  - 真实的“取数/聚簇/上传标注平台/发送 Hi/写结果表”接口尚未接入，均标注 [TODO 接生产]
  - 定时任务：目前用 asyncio 后台任务实现周一 08:00 / 周三 08:00 触发；生产建议改到平台调度
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import traceback
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel


# ── 常量 ─────────────────────────────────────────────────────────────────────
CN_TZ = ZoneInfo("Asia/Shanghai")

ANNOTATION_PLATFORM_URL = "http://localhost:3001"
RESULT_STORE_URL = (
    os.environ.get("RESULT_STORE_URL", "https://docs.example.com/sheet/sample_result_id")
)
HI_GROUP_NAME = "资质造假评估"
HI_GROUP_CHAT_ID = "CHAT_SAMPLE_ID"

# 允许 openclaw 侧本地脚本推送数据的 token。生产环境请通过 db.properties 之外的
# 安全渠道（Platform Studio 的环境变量、K8s Secret 等）注入。这里读环境变量优先。
PUSH_TOKEN = os.environ.get("WORKFLOW_PUSH_TOKEN") or "pqr-mvp-2026-push-token-change-me"

# 阶段定义（顺序 = 展示顺序）
STAGES: list[tuple[str, str]] = [
    ("fetch_full",       "取数：全量账户 embedding"),
    ("cluster_full",     "聚簇：全量账户"),
    ("fetch_sample",     "抽样：全行业 + 金融 + 房地产"),
    ("match_expand",     "匹配：抽样命中并扩展同簇账号"),
    ("upload_annotate",  "上传：同步至标注平台"),
    ("notify_hi",        "通知：Hi 群 " + HI_GROUP_NAME + " 提醒标注"),
    ("wait_annotation",  "标注：等待人工标注（截止周二晚）"),
    ("calc_metric",      "评估：账户违规浓度计算"),
    ("write_result",     "维护：写入结果留存表"),
]

STAGE_TITLES = {code: title for code, title in STAGES}
STAGE_CODES = [code for code, _ in STAGES]

VIOLATION_LABELS = {"实锤造假", "疑似造假", "资质挂靠"}


# ── db.properties 加载 ───────────────────────────────────────────────────────
def _load_db_properties(path: str = "db.properties") -> dict[str, str]:
    # Check env vars first
    if os.environ.get("DB_HOST"):
        return {
            "db.host": os.environ.get("DB_HOST", "localhost"),
            "db.port": os.environ.get("DB_PORT", "5432"),
            "db.username": os.environ.get("DB_USER", "postgres"),
            "db.password": os.environ.get("DB_PASSWORD", "postgres"),
            "db.database": os.environ.get("DB_NAME", "qual_dashboard"),
        }
    p = Path(__file__).resolve().parent / path
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


DB_PROPS = _load_db_properties()

# 有 db.properties 才启用 PG，否则只读演示
try:
    import psycopg  # type: ignore
    from psycopg.rows import dict_row  # type: ignore
except Exception:
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore


def _db_available() -> bool:
    return bool(DB_PROPS) and psycopg is not None


def _conn():
    """获取一个短连接（同步 psycopg3），业务侧用完 close。"""
    if not _db_available():
        raise HTTPException(503, "db 未配置或 psycopg 未安装：当前为只读演示模式，无法写入")
    return psycopg.connect(  # type: ignore
        host=DB_PROPS["db.host"],
        port=int(DB_PROPS["db.port"]),
        user=DB_PROPS["db.username"],
        password=DB_PROPS["db.password"],
        dbname=DB_PROPS["db.database"],
        row_factory=dict_row,  # type: ignore
        autocommit=False,
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS weekly_run (
  id SERIAL PRIMARY KEY,
  week_start DATE NOT NULL UNIQUE,       -- 上周一
  week_end   DATE NOT NULL,              -- 上周日
  status TEXT NOT NULL DEFAULT 'created',
  current_stage TEXT,
  automation_instance_url TEXT,                -- 调度平台任务实例链接（可空）
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE weekly_run ADD COLUMN IF NOT EXISTS automation_instance_url TEXT;
ALTER TABLE weekly_run ADD COLUMN IF NOT EXISTS report_shortcut_id TEXT;
ALTER TABLE weekly_run ADD COLUMN IF NOT EXISTS report_url TEXT;
ALTER TABLE weekly_run ADD COLUMN IF NOT EXISTS is_baseline BOOLEAN DEFAULT FALSE;   -- 为 true 时不在看板周期切换菜单里显示
ALTER TABLE weekly_run ADD COLUMN IF NOT EXISTS baseline_label TEXT;                -- 基线的展示名（例如 "0701-0714"），仅本字段需要时使用
CREATE INDEX IF NOT EXISTS idx_weekly_run_start ON weekly_run(week_start DESC);

CREATE TABLE IF NOT EXISTS stage_log (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  stage_code TEXT NOT NULL,
  status TEXT NOT NULL,                  -- pending / running / done / failed / skipped
  message TEXT,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stage_log_run ON stage_log(run_id);

CREATE TABLE IF NOT EXISTS weekly_artifact (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  stage_code TEXT NOT NULL,
  filename TEXT NOT NULL,
  content_type TEXT,
  content BYTEA NOT NULL,                -- 直接存二进制，避免 PG Large Object API 兼容问题
  size BIGINT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_artifact_run ON weekly_artifact(run_id);

CREATE TABLE IF NOT EXISTS weekly_metric (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  sample_group TEXT NOT NULL,            -- 整体样本 / 金融行业样本 / 房地产行业样本
  annotated_sample_users INTEGER,
  violated_sample_users INTEGER,
  concentration NUMERIC(10,4),
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_metric_run ON weekly_metric(run_id);

-- 一级行业基准数据（全量入驻/聚簇/实锤造假 UID）
-- 属于 run 的快照：避免后续入驻数据变动影响历史报告
CREATE TABLE IF NOT EXISTS weekly_industry_baseline (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  period TEXT,                            -- 入驻/聚簇数据的时间窗口，例如 "0701-0714"
  industry TEXT NOT NULL,                 -- 一级行业名
  registered_uid INTEGER,                 -- 专业号入驻 UID 数
  clustered_uid INTEGER,                  -- 参与聚簇的 UID 数
  suspected_fake_uid INTEGER,             -- 疑似造假 UID 数（聚簇算法判定的可疑簇内账户数，未经人工确认）
  annotated_uid INTEGER,                  -- 已进入人工标注池的账户数（抽样 + 同簇扩展的并集）
  confirmed_fake_uid INTEGER,             -- 实锤造假 UID 数（标注池里被人工实锤的账户数，用于算算法准确率）
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE weekly_industry_baseline ADD COLUMN IF NOT EXISTS suspected_fake_uid INTEGER;
ALTER TABLE weekly_industry_baseline ADD COLUMN IF NOT EXISTS annotated_uid INTEGER;
COMMENT ON COLUMN weekly_industry_baseline.confirmed_fake_uid IS '实锤造假 UID 数：标注池里被人工确认为实锤的账户数（分子），新定义：与 annotated_uid 分母搭配使用算算法准确率；旧定义（全量人工实锤）暂不使用';
CREATE INDEX IF NOT EXISTS idx_weekly_industry_baseline_run ON weekly_industry_baseline(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_industry_baseline_run_industry
  ON weekly_industry_baseline(run_id, industry);

-- 抽样行业分布（本周抽样到的各行业账号数）
CREATE TABLE IF NOT EXISTS weekly_sample_distribution (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  industry TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_sample_distribution_run ON weekly_sample_distribution(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_sample_distribution_run_industry
  ON weekly_sample_distribution(run_id, industry);

-- 违规账户行业分布（本周实际判定违规的账户按行业）
CREATE TABLE IF NOT EXISTS weekly_violation_distribution (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  industry TEXT NOT NULL,
  violated_count INTEGER NOT NULL DEFAULT 0,  -- 本周判定违规的账户数
  detail JSONB,                                -- 如需区分 实锤/疑似/挂靠 可靠这里
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_violation_distribution_run ON weekly_violation_distribution(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_violation_distribution_run_industry
  ON weekly_violation_distribution(run_id, industry);

-- 二级行业抽样分布
CREATE TABLE IF NOT EXISTS weekly_sample_distribution_l2 (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  first_industry TEXT NOT NULL,          -- 所属一级行业
  second_industry TEXT NOT NULL,         -- 二级行业
  count INTEGER NOT NULL DEFAULT 0,
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_sample_distribution_l2_run ON weekly_sample_distribution_l2(run_id);
CREATE INDEX IF NOT EXISTS idx_weekly_sample_distribution_l2_first ON weekly_sample_distribution_l2(run_id, first_industry);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_sample_distribution_l2_run_industry
  ON weekly_sample_distribution_l2(run_id, first_industry, second_industry);

-- 二级行业违规分布
CREATE TABLE IF NOT EXISTS weekly_violation_distribution_l2 (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  first_industry TEXT NOT NULL,
  second_industry TEXT NOT NULL,
  violated_count INTEGER NOT NULL DEFAULT 0,
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_violation_distribution_l2_run ON weekly_violation_distribution_l2(run_id);
CREATE INDEX IF NOT EXISTS idx_weekly_violation_distribution_l2_first ON weekly_violation_distribution_l2(run_id, first_industry);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_violation_distribution_l2_run_industry
  ON weekly_violation_distribution_l2(run_id, first_industry, second_industry);

-- 二级行业入驻基准（可选，供未来“二级代表性”分析使用）
CREATE TABLE IF NOT EXISTS weekly_industry_baseline_l2 (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  period TEXT,
  first_industry TEXT NOT NULL,
  second_industry TEXT NOT NULL,
  registered_uid INTEGER,
  clustered_uid INTEGER,
  suspected_fake_uid INTEGER,
  annotated_uid INTEGER,
  confirmed_fake_uid INTEGER,
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE weekly_industry_baseline_l2 ADD COLUMN IF NOT EXISTS annotated_uid INTEGER;
ALTER TABLE weekly_industry_baseline_l2 ADD COLUMN IF NOT EXISTS confirmed_fake_uid INTEGER;
CREATE INDEX IF NOT EXISTS idx_weekly_industry_baseline_l2_run ON weekly_industry_baseline_l2(run_id);
CREATE INDEX IF NOT EXISTS idx_weekly_industry_baseline_l2_first ON weekly_industry_baseline_l2(run_id, first_industry);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_industry_baseline_l2_run_industry
  ON weekly_industry_baseline_l2(run_id, first_industry, second_industry);

-- 实锤造假来源分布（专业号入驻 / 号店入驻 / 号广入驻）
CREATE TABLE IF NOT EXISTS weekly_violation_source (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  industry TEXT NOT NULL,                 -- 一级行业名，'大盘整体' 表示全行业汇总
  professional_count INTEGER DEFAULT 0,   -- 专业号入驻中被实锤的账户数
  shop_count INTEGER DEFAULT 0,           -- 号店入驻中被实锤的账户数
  ad_count INTEGER DEFAULT 0,             -- 号广入驻中被实锤的账户数
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_violation_source_run ON weekly_violation_source(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_violation_source_run_industry
  ON weekly_violation_source(run_id, industry);

-- 入驻量级来源分布（专业号 / 号店 / 号广）
CREATE TABLE IF NOT EXISTS weekly_registered_source (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  industry TEXT NOT NULL,                 -- 一级行业名，'大盘整体' 表示全行业汇总
  professional_count INTEGER DEFAULT 0,   -- 专业号入驻账户数
  shop_count INTEGER DEFAULT 0,           -- 号店入驻账户数
  ad_count INTEGER DEFAULT 0,             -- 号广入驻账户数
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_registered_source_run ON weekly_registered_source(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_registered_source_run_industry
  ON weekly_registered_source(run_id, industry);

-- 认证方式分布（对公打款 / 人脸识别 等，按行业）
CREATE TABLE IF NOT EXISTS weekly_cert_method (
  id SERIAL PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES weekly_run(id) ON DELETE CASCADE,
  industry TEXT NOT NULL,                 -- 一级行业名
  method TEXT NOT NULL,                   -- 认证方式：对公打款 / 人脸识别 等
  registered_count INTEGER DEFAULT 0,     -- 该认证方式的全量入驻账户数
  violated_count INTEGER DEFAULT 0,       -- 该认证方式的实锤造假账户数（抽样标注口径）
  detail JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_weekly_cert_method_run ON weekly_cert_method(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_weekly_cert_method_run_industry_method
  ON weekly_cert_method(run_id, industry, method);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='weekly_artifact' AND column_name='oid')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='weekly_artifact' AND column_name='content') THEN
    ALTER TABLE weekly_artifact ADD COLUMN content BYTEA;
    ALTER TABLE weekly_artifact ALTER COLUMN oid DROP NOT NULL;
  END IF;
END $$;
"""


def init_db_if_available() -> None:
    if not _db_available():
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        c.commit()


# ── SSO ──────────────────────────────────────────────────────────────────────
def _parse_sso_user(decrypted_userinfo: Optional[str]) -> Optional[dict]:
    if not decrypted_userinfo:
        return None
    try:
        fixed = decrypted_userinfo.encode("latin-1").decode("utf-8")
        data = json.loads(fixed)
    except Exception:
        return None
    return {
        "email": data.get("email") or data.get("workEmail"),
        "name": data.get("name") or data.get("displayName"),
        "userId": data.get("userId") or data.get("id"),
    }


def _require_user(decrypted_userinfo: Optional[str]) -> dict:
    user = _parse_sso_user(decrypted_userinfo)
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user


# ── 时间工具 ─────────────────────────────────────────────────────────────────
def _now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _last_week_range(today: Optional[date] = None) -> tuple[date, date]:
    """返回上一自然周（周一~周日）。"""
    d = today or _now_cn().date()
    # 本周一
    this_monday = d - timedelta(days=d.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


# ── weekly_run / stage_log 数据访问 ─────────────────────────────────────────
def _ensure_run(week_start: date, week_end: date) -> dict:
    """幂等地拿到某周的 run，不存在则新建 + 初始化所有 stage_log=pending。"""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM weekly_run WHERE week_start = %s", (week_start,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            "INSERT INTO weekly_run (week_start, week_end, status, current_stage) "
            "VALUES (%s, %s, 'created', %s) RETURNING *",
            (week_start, week_end, STAGE_CODES[0]),
        )
        run = cur.fetchone()
        # 初始化 stage_log
        for code, _ in STAGES:
            cur.execute(
                "INSERT INTO stage_log (run_id, stage_code, status) VALUES (%s, %s, 'pending')",
                (run["id"], code),
            )
        c.commit()
        return run


def _update_stage(run_id: int, stage_code: str, *, status: str, message: str = "") -> None:
    now = datetime.now(timezone.utc)
    with _conn() as c, c.cursor() as cur:
        # 找该 run 该 stage 最后一条 log
        cur.execute(
            "SELECT id FROM stage_log WHERE run_id=%s AND stage_code=%s ORDER BY id DESC LIMIT 1",
            (run_id, stage_code),
        )
        row = cur.fetchone()
        if row:
            log_id = row["id"]
            if status == "running":
                cur.execute(
                    "UPDATE stage_log SET status=%s, message=%s, started_at=%s WHERE id=%s",
                    (status, message, now, log_id),
                )
            elif status in ("done", "failed", "skipped"):
                cur.execute(
                    "UPDATE stage_log SET status=%s, message=%s, finished_at=%s WHERE id=%s",
                    (status, message, now, log_id),
                )
            else:
                cur.execute(
                    "UPDATE stage_log SET status=%s, message=%s WHERE id=%s",
                    (status, message, log_id),
                )
        else:
            cur.execute(
                "INSERT INTO stage_log (run_id, stage_code, status, message) VALUES (%s, %s, %s, %s)",
                (run_id, stage_code, status, message),
            )
        # 同步 run 状态
        if status == "running":
            cur.execute(
                "UPDATE weekly_run SET current_stage=%s, status='running', updated_at=NOW() WHERE id=%s",
                (stage_code, run_id),
            )
        elif status == "failed":
            cur.execute(
                "UPDATE weekly_run SET current_stage=%s, status='failed', updated_at=NOW() WHERE id=%s",
                (stage_code, run_id),
            )
        elif status == "done" and stage_code == STAGE_CODES[-1]:
            cur.execute(
                "UPDATE weekly_run SET current_stage=%s, status='done', updated_at=NOW() WHERE id=%s",
                (stage_code, run_id),
            )
        else:
            cur.execute("UPDATE weekly_run SET updated_at=NOW() WHERE id=%s", (run_id,))
        c.commit()


def _get_run_detail(run_id: int) -> dict:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM weekly_run WHERE id=%s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "run not found")
        cur.execute(
            "SELECT stage_code, status, message, started_at, finished_at "
            "FROM stage_log WHERE run_id=%s ORDER BY id",
            (run_id,),
        )
        logs = cur.fetchall()
        cur.execute(
            "SELECT id, stage_code, filename, content_type, size, created_at "
            "FROM weekly_artifact WHERE run_id=%s ORDER BY id DESC",
            (run_id,),
        )
        arts = cur.fetchall()
        cur.execute(
            "SELECT sample_group, annotated_sample_users, violated_sample_users, "
            "concentration, detail, created_at FROM weekly_metric WHERE run_id=%s ORDER BY id",
            (run_id,),
        )
        metrics = cur.fetchall()

        cur.execute(
            "SELECT industry, registered_uid, clustered_uid, suspected_fake_uid, annotated_uid, confirmed_fake_uid, period, detail "
            "FROM weekly_industry_baseline WHERE run_id=%s ORDER BY registered_uid DESC NULLS LAST, id",
            (run_id,),
        )
        baseline = cur.fetchall()

        cur.execute(
            "SELECT industry, count, detail "
            "FROM weekly_sample_distribution WHERE run_id=%s ORDER BY count DESC, id",
            (run_id,),
        )
        sample_dist = cur.fetchall()

        cur.execute(
            "SELECT industry, violated_count, detail "
            "FROM weekly_violation_distribution WHERE run_id=%s ORDER BY violated_count DESC, id",
            (run_id,),
        )
        violation_dist = cur.fetchall()

        # 二级行业
        cur.execute(
            "SELECT first_industry, second_industry, count, detail "
            "FROM weekly_sample_distribution_l2 WHERE run_id=%s ORDER BY first_industry, count DESC, id",
            (run_id,),
        )
        sample_dist_l2 = cur.fetchall()

        cur.execute(
            "SELECT first_industry, second_industry, violated_count, detail "
            "FROM weekly_violation_distribution_l2 WHERE run_id=%s ORDER BY first_industry, violated_count DESC, id",
            (run_id,),
        )
        violation_dist_l2 = cur.fetchall()

        cur.execute(
            "SELECT first_industry, second_industry, registered_uid, clustered_uid, suspected_fake_uid, annotated_uid, confirmed_fake_uid, period, detail "
            "FROM weekly_industry_baseline_l2 WHERE run_id=%s ORDER BY first_industry, registered_uid DESC NULLS LAST, id",
            (run_id,),
        )
        baseline_l2 = cur.fetchall()

        cur.execute(
            "SELECT industry, professional_count, shop_count, ad_count, detail "
            "FROM weekly_violation_source WHERE run_id=%s ORDER BY "
            "CASE WHEN industry='大盘整体' THEN 0 ELSE 1 END, professional_count+shop_count+ad_count DESC, id",
            (run_id,),
        )
        violation_source = cur.fetchall()

        cur.execute(
            "SELECT industry, professional_count, shop_count, ad_count, detail "
            "FROM weekly_registered_source WHERE run_id=%s ORDER BY "
            "CASE WHEN industry='大盘整体' THEN 0 ELSE 1 END, id",
            (run_id,),
        )
        registered_source = cur.fetchall()

        cur.execute(
            "SELECT industry, method, registered_count, violated_count, detail "
            "FROM weekly_cert_method WHERE run_id=%s ORDER BY id",
            (run_id,),
        )
        cert_method = cur.fetchall()

    # 阶段按 STAGES 顺序补齐
    log_map = {l["stage_code"]: l for l in logs}
    stages = []
    for code, title in STAGES:
        l = log_map.get(code) or {"stage_code": code, "status": "pending", "message": "", "started_at": None, "finished_at": None}
        stages.append({
            "code": code,
            "title": title,
            "status": l["status"],
            "message": l.get("message") or "",
            "started_at": (l.get("started_at").isoformat() if l.get("started_at") else None),
            "finished_at": (l.get("finished_at").isoformat() if l.get("finished_at") else None),
        })
    return {
        "run": {
            "id": run["id"],
            "week_start": run["week_start"].isoformat(),
            "week_end": run["week_end"].isoformat(),
            "status": run["status"],
            "current_stage": run["current_stage"],
            "current_stage_title": STAGE_TITLES.get(run["current_stage"], ""),
            "automation_instance_url": run.get("automation_instance_url"),
            "report_shortcut_id": run.get("report_shortcut_id"),
            "report_url": run.get("report_url"),
            "is_baseline": bool(run.get("is_baseline")),
            "baseline_label": run.get("baseline_label"),
            "created_at": run["created_at"].isoformat(),
            "updated_at": run["updated_at"].isoformat(),
        },
        "stages": stages,
        "artifacts": [
            {
                "id": a["id"],
                "stage_code": a["stage_code"],
                "stage_title": STAGE_TITLES.get(a["stage_code"], a["stage_code"]),
                "filename": a["filename"],
                "content_type": a["content_type"],
                "size": a["size"],
                "created_at": a["created_at"].isoformat(),
            }
            for a in arts
        ],
        "metrics": [
            {
                "sample_group": m["sample_group"],
                "annotated_sample_users": m["annotated_sample_users"],
                "violated_sample_users": m["violated_sample_users"],
                "concentration": (float(m["concentration"]) if m["concentration"] is not None else None),
                "detail": m["detail"],
                "created_at": m["created_at"].isoformat(),
            }
            for m in metrics
        ],
        "industry_baseline": [
            {
                "industry": b["industry"],
                "registered_uid": b["registered_uid"],
                "clustered_uid": b["clustered_uid"],
                "suspected_fake_uid": b.get("suspected_fake_uid"),
                "annotated_uid": b.get("annotated_uid"),
                "confirmed_fake_uid": b["confirmed_fake_uid"],
                "period": b["period"],
                "detail": b["detail"],
            }
            for b in baseline
        ],
        "sample_distribution": [
            {"industry": s["industry"], "count": s["count"], "detail": s["detail"]}
            for s in sample_dist
        ],
        "violation_distribution": [
            {"industry": v["industry"], "violated_count": v["violated_count"], "detail": v["detail"]}
            for v in violation_dist
        ],
        "sample_distribution_l2": [
            {
                "first_industry": s["first_industry"],
                "second_industry": s["second_industry"],
                "count": s["count"],
                "detail": s["detail"],
            }
            for s in sample_dist_l2
        ],
        "violation_distribution_l2": [
            {
                "first_industry": v["first_industry"],
                "second_industry": v["second_industry"],
                "violated_count": v["violated_count"],
                "detail": v["detail"],
            }
            for v in violation_dist_l2
        ],
        "industry_baseline_l2": [
            {
                "first_industry": b["first_industry"],
                "second_industry": b["second_industry"],
                "registered_uid": b["registered_uid"],
                "clustered_uid": b["clustered_uid"],
                "suspected_fake_uid": b.get("suspected_fake_uid"),
                "annotated_uid": b.get("annotated_uid"),
                "confirmed_fake_uid": b.get("confirmed_fake_uid"),
                "period": b["period"],
                "detail": b["detail"],
            }
            for b in baseline_l2
        ],
        "violation_source": [
            {
                "industry": v["industry"],
                "professional_count": v["professional_count"],
                "shop_count": v["shop_count"],
                "ad_count": v["ad_count"],
                "detail": v["detail"],
            }
            for v in violation_source
        ],
        "registered_source": [
            {
                "industry": v["industry"],
                "professional_count": v["professional_count"],
                "shop_count": v["shop_count"],
                "ad_count": v["ad_count"],
                "detail": v["detail"],
            }
            for v in registered_source
        ],
        "cert_method": [
            {
                "industry": v["industry"],
                "method": v["method"],
                "registered_count": v["registered_count"],
                "violated_count": v["violated_count"],
                "detail": v["detail"],
            }
            for v in cert_method
        ],
    }


def _list_runs(limit: int = 12, include_baseline: bool = False) -> list[dict]:
    if not _db_available():
        return []
    with _conn() as c, c.cursor() as cur:
        if include_baseline:
            cur.execute(
                "SELECT id, week_start, week_end, status, current_stage, updated_at, is_baseline, baseline_label "
                "FROM weekly_run ORDER BY week_start DESC LIMIT %s",
                (limit,),
            )
        else:
            cur.execute(
                "SELECT id, week_start, week_end, status, current_stage, updated_at, is_baseline, baseline_label "
                "FROM weekly_run WHERE COALESCE(is_baseline, FALSE) = FALSE "
                "ORDER BY week_start DESC LIMIT %s",
                (limit,),
            )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "week_start": r["week_start"].isoformat(),
            "week_end": r["week_end"].isoformat(),
            "status": r["status"],
            "current_stage": r["current_stage"],
            "current_stage_title": STAGE_TITLES.get(r["current_stage"], ""),
            "updated_at": r["updated_at"].isoformat(),
            "is_baseline": bool(r.get("is_baseline")),
            "baseline_label": r.get("baseline_label"),
        }
        for r in rows
    ]


# ── 产物：直接存二进制 (BYTEA) ──────────────────────────────────────────────
def _save_artifact(run_id: int, stage_code: str, filename: str, content_type: str, blob: bytes) -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO weekly_artifact (run_id, stage_code, filename, content_type, content, size) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (run_id, stage_code, filename, content_type, blob, len(blob)),
        )
        aid = cur.fetchone()["id"]
        c.commit()
        return aid


def _read_artifact(artifact_id: int) -> tuple[dict, bytes]:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM weekly_artifact WHERE id=%s", (artifact_id,))
        a = cur.fetchone()
        if not a:
            raise HTTPException(404, "artifact not found")
        content = a.get("content")
        blob = bytes(content) if content is not None else b""
        return a, blob


# ── 违规浓度计算 ────────────────────────────────────────────────────────────
def _parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "t", "yes", "y", "是"}


def _calc_metric_from_csv(blob: bytes) -> list[dict]:
    """从标注结果 CSV 计算违规浓度。

    输入 CSV 建议包含字段：
      user_id, sample_groups, is_sample_user, annotation_result

    规则：
      1. 只保留 is_sample_user = True 的账号（剔除同簇扩展账号）
      2. 一个账号可能同时属于多个 sample_groups（用 "|" 或 "," 分隔），
         每个所属样本组分别记一次
      3. 违规判定：annotation_result 命中 {实锤造假, 疑似造假, 资质挂靠}
      4. 若同一 (sample_group, user_id) 有多条记录，先按 user_id 去重（违规优先）
    """
    text = blob.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []

    def _find(*names: str) -> Optional[str]:
        lower = {n.lower(): n for n in fieldnames}
        for n in names:
            if n in fieldnames:
                return n
            if n.lower() in lower:
                return lower[n.lower()]
        return None

    col_user = _find("user_id", "userId")
    col_flag = _find("is_sample_user", "isSampleUser")
    col_groups = _find("sample_groups", "sampleGroups", "sample_group", "sampleGroup")
    col_result = _find("annotation_result", "label", "annotationResult", "标注结果")
    if not (col_user and col_flag and col_groups and col_result):
        raise HTTPException(
            400,
            f"CSV 缺少必要字段，需包含 user_id / is_sample_user / sample_groups / annotation_result，实际字段：{fieldnames}",
        )

    # 收集 {sample_group: {user_id: is_violated}}
    grouped: dict[str, dict[str, bool]] = {}
    for row in reader:
        if not _parse_bool(row.get(col_flag)):
            continue  # 剔除同簇扩展账号
        uid = str(row.get(col_user) or "").strip()
        if not uid:
            continue
        groups_raw = str(row.get(col_groups) or "").strip()
        if not groups_raw:
            continue
        groups = [g.strip() for g in re.split(r"[|,;/]", groups_raw) if g.strip()]
        label = str(row.get(col_result) or "").strip()
        is_violated = label in VIOLATION_LABELS
        for g in groups:
            g_map = grouped.setdefault(g, {})
            # 违规优先：一旦违规就保持违规
            if uid in g_map:
                g_map[uid] = g_map[uid] or is_violated
            else:
                g_map[uid] = is_violated

    metrics = []
    for group, uid_map in sorted(grouped.items()):
        annotated = len(uid_map)
        violated = sum(1 for v in uid_map.values() if v)
        concentration = (violated / annotated) if annotated else 0.0
        metrics.append({
            "sample_group": group,
            "annotated_sample_users": annotated,
            "violated_sample_users": violated,
            "concentration": round(concentration, 4),
            "detail": {
                "violation_labels": sorted(VIOLATION_LABELS),
                "note": "分子分母均只统计 is_sample_user=True 的原始抽样账号",
            },
        })
    return metrics


# ── FastAPI ─────────────────────────────────────────────────────────────────
app = FastAPI(title="专业号资质造假浓度评估工作流")


@app.on_event("startup")
def _startup() -> None:
    try:
        init_db_if_available()
    except Exception as e:
        # 首启若 DB 尚未就绪不阻塞进程
        print(f"[startup] init_db skipped: {e}")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": _db_available()}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.get("/report/{run_id}", response_class=HTMLResponse)
def report_page(run_id: int) -> HTMLResponse:
    """可视化报告页面。HTML 内嵌 ECharts CDN，前端自己拉 /api/runs/{id} + /api/metrics_history。"""
    html = REPORT_HTML.replace("__RUN_ID__", str(run_id))
    return HTMLResponse(html)


@app.get("/report", response_class=HTMLResponse)
def report_index() -> HTMLResponse:
    """不带 run_id 时，默认看当前周（last week），跳过 baseline run。"""
    ws, _ = _last_week_range()
    if _db_available():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT id FROM weekly_run WHERE week_start=%s AND COALESCE(is_baseline, FALSE)=FALSE",
                    (ws,),
                )
                row = cur.fetchone()
                if row:
                    return HTMLResponse(REPORT_HTML.replace("__RUN_ID__", str(row["id"])))
        except Exception:
            pass
    # 回退：取最新一周（非基线）
    if _db_available():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT id FROM weekly_run WHERE COALESCE(is_baseline, FALSE)=FALSE "
                    "ORDER BY week_start DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return HTMLResponse(REPORT_HTML.replace("__RUN_ID__", str(row["id"])))
        except Exception:
            pass
    return HTMLResponse("<h3>暂无可视化报告</h3>", status_code=404)


@app.get("/whoami")
def whoami(
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> JSONResponse:
    user = _require_user(decrypted_userinfo)
    return JSONResponse({"email": user["email"], "name": user["name"], "userId": user["userId"]})


@app.get("/api/meta")
def meta(
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    user = _parse_sso_user(decrypted_userinfo) or {"email": None, "name": None, "userId": None}
    ws, we = _last_week_range()
    return {
        "user": user,
        "db_available": _db_available(),
        "annotation_platform_url": ANNOTATION_PLATFORM_URL,
        "result_store_url": RESULT_STORE_URL,
        "hi_group_name": HI_GROUP_NAME,
        "next_week_start": ws.isoformat(),
        "next_week_end": we.isoformat(),
        "stage_definitions": [{"code": c, "title": t} for c, t in STAGES],
        "violation_labels": sorted(VIOLATION_LABELS),
        "schedule": {
            "auto_start_cn": "每周一 08:00 (Asia/Shanghai)",
            "annotation_deadline_cn": "每周二 23:59 (Asia/Shanghai)",
            "auto_calc_cn": "每周三 08:00 (Asia/Shanghai)",
        },
    }


@app.get("/api/metrics_history")
def metrics_history(limit: int = 12) -> dict:
    """返回最近 N 周的大盘/房地产/金融三大分组浓度（按 week_start 升序）。供前端浓度趋势图使用。
    基线 run（is_baseline=TRUE）也会包含在内，但使用 baseline_label 作为 x 轴标签。"""
    if not _db_available():
        return {"weeks": []}
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT r.id AS run_id, r.week_start, r.week_end, r.is_baseline, r.baseline_label, "
            "m.sample_group, m.concentration "
            "FROM weekly_run r "
            "JOIN weekly_metric m ON m.run_id = r.id "
            "WHERE m.concentration IS NOT NULL "
            "ORDER BY r.week_start ASC "
        )
        rows = cur.fetchall()
    grouped: dict = {}
    for r in rows:
        wk = r["week_start"].isoformat()
        if wk not in grouped:
            grouped[wk] = {
                "run_id": r["run_id"],
                "week_start": wk,
                "week_end": r["week_end"].isoformat(),
                "is_baseline": bool(r["is_baseline"]),
                "baseline_label": r["baseline_label"],
                "overall": None,
                "realestate": None,
                "finance": None,
            }
        conc = float(r["concentration"])
        g = (r["sample_group"] or "").replace("行业样本", "").replace("样本", "").strip()
        if g in ("整体", "全行业", "大盘"):
            grouped[wk]["overall"] = conc
        elif g == "房地产":
            grouped[wk]["realestate"] = conc
        elif g == "金融":
            grouped[wk]["finance"] = conc
    weeks = sorted(grouped.values(), key=lambda x: x["week_start"])
    if limit and len(weeks) > limit:
        weeks = weeks[-limit:]
    return {"weeks": weeks}


@app.get("/api/runs")
def list_runs_api(
    limit: int = 12,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    _ = _parse_sso_user(decrypted_userinfo)
    return {"runs": _list_runs(limit=limit)}


@app.get("/api/runs/current")
def get_current_run(
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    _ = _parse_sso_user(decrypted_userinfo)
    if not _db_available():
        raise HTTPException(503, "db 未配置，无法读取任务")
    ws, we = _last_week_range()
    run = _ensure_run(ws, we)
    return _get_run_detail(run["id"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    return _get_run_detail(run_id)


class TriggerReq(BaseModel):
    run_id: Optional[int] = None


@app.post("/api/runs/start")
def start_run(
    req: TriggerReq | None = None,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    user = _require_user(decrypted_userinfo)
    ws, we = _last_week_range()
    run = _ensure_run(ws, we)
    _update_stage(run["id"], STAGE_CODES[0], status="pending", message=f"由 {user['name']} 手动启动")
    return _get_run_detail(run["id"])




@app.post("/api/runs/{run_id}/stages/{stage_code}/mark")
def mark_stage(
    run_id: int,
    stage_code: str,
    status: str,
    message: str = "",
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    _require_user(decrypted_userinfo)
    if stage_code not in STAGE_TITLES:
        raise HTTPException(400, f"未知阶段 {stage_code}")
    if status not in {"pending", "running", "done", "failed", "skipped"}:
        raise HTTPException(400, "非法 status")
    _update_stage(run_id, stage_code, status=status, message=message)
    return _get_run_detail(run_id)




# ── openclaw 侧本地脚本推送数据用（走 push token） ────────────────────────
def _require_push_token(x_push_token: Optional[str]) -> None:
    if not x_push_token or x_push_token != PUSH_TOKEN:
        raise HTTPException(401, "invalid push token")


class StageMarkReq(BaseModel):
    status: str
    message: str = ""


class EnsureRunReq(BaseModel):
    week_start: Optional[str] = None       # ISO 日期字符串，例如 "2026-07-13"
    week_end: Optional[str] = None
    is_baseline: Optional[bool] = None      # 为 true 则该 run 不在看板周期切换菜单里显示
    baseline_label: Optional[str] = None    # 基线 run 在图表 x 轴上的展示名（例如 "0701-0714"）


@app.post("/api/push/runs/ensure")
def push_ensure_run(
    req: Optional[EnsureRunReq] = None,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """幂等地确保某周存在 run。传入 week_start/week_end 则使用指定周期（支持 seed 历史基线）；否则默认上一自然周。
    基线 run：传入 is_baseline=true 时，该 run 不会出现在看板周期切换菜单里，仅供趋势图使用。"""
    _require_push_token(x_push_token)
    if req and req.week_start and req.week_end:
        ws = date.fromisoformat(req.week_start)
        we = date.fromisoformat(req.week_end)
    else:
        ws, we = _last_week_range()
    run = _ensure_run(ws, we)
    # 回写基线字段（若传入）
    if req and (req.is_baseline is not None or req.baseline_label is not None):
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE weekly_run SET "
                "is_baseline = COALESCE(%s, is_baseline), "
                "baseline_label = COALESCE(%s, baseline_label), "
                "updated_at = NOW() WHERE id = %s",
                (req.is_baseline, req.baseline_label, run["id"]),
            )
            c.commit()
    return _get_run_detail(run["id"])


@app.post("/api/push/runs/{run_id}/stages/{stage_code}/mark")
def push_mark_stage(
    run_id: int,
    stage_code: str,
    req: StageMarkReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    _require_push_token(x_push_token)
    if stage_code not in STAGE_TITLES:
        raise HTTPException(400, f"未知阶段 {stage_code}")
    if req.status not in {"pending", "running", "done", "failed", "skipped"}:
        raise HTTPException(400, "非法 status")
    _update_stage(run_id, stage_code, status=req.status, message=req.message)
    return _get_run_detail(run_id)


@app.post("/api/push/runs/{run_id}/artifacts")
async def push_upload_artifact(
    run_id: int,
    stage_code: str,
    file: UploadFile = File(...),
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    _require_push_token(x_push_token)
    if stage_code not in STAGE_TITLES:
        raise HTTPException(400, f"未知阶段 {stage_code}")
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "文件为空")
    if len(blob) > 200 * 1024 * 1024:
        raise HTTPException(413, "单文件不支持 > 200MB")
    aid = _save_artifact(
        run_id, stage_code, file.filename or "unnamed",
        file.content_type or "application/octet-stream", blob,
    )
    # 不覆盖已有的 stage message（避免把业务方写进去的 “N 行/M 个” 结构化信息盖掉）
    # 仅当前 stage 无业务消息时，才写入默认“产物已推送”提示
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT message FROM stage_log WHERE run_id=%s AND stage_code=%s", (run_id, stage_code))
        row = cur.fetchone()
    existing = (row and (row.get("message") or "").strip()) if row else ""
    if not existing:
        _update_stage(run_id, stage_code, status="done",
                      message=f"[push] 产物已推送：{file.filename} ({len(blob)} bytes)")
    else:
        # 保留业务消息，仅把状态推到 done
        _update_stage(run_id, stage_code, status="done", message=existing)
    return {"artifact_id": aid, **_get_run_detail(run_id)}


class MetricsPushReq(BaseModel):
    metrics: list[dict]
    note: str = ""


class DibpUpdate(BaseModel):
    automation_instance_url: str


@app.post("/api/push/runs/{run_id}/metrics")
def push_metrics(
    run_id: int,
    req: MetricsPushReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_metric WHERE run_id=%s", (run_id,))
        for m in req.metrics:
            cur.execute(
                "INSERT INTO weekly_metric (run_id, sample_group, annotated_sample_users, "
                "violated_sample_users, concentration, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    m.get("sample_group"),
                    m.get("annotated_sample_users"),
                    m.get("violated_sample_users"),
                    m.get("concentration"),
                    json.dumps(m.get("detail") or {}, ensure_ascii=False),
                ),
            )
        c.commit()
    _update_stage(run_id, "calc_metric", status="done",
                  message=req.note or f"[push] 违规浓度已写入，共 {len(req.metrics)} 个样本组")
    return _get_run_detail(run_id)


@app.post("/api/push/runs/{run_id}/clear_all")
def push_clear_all(
    run_id: int,
    confirm: str = "",
    x_push_token: str = Header(None, alias="X-Push-Token"),
):
    """强制重置本 run。需传 confirm="yes-reset-all-data" 确认，避免误操作。
    删除：weekly_artifact / weekly_metric / weekly_industry_baseline / 一二级分布表 全部，并回滚所有 stage 为 pending。"""
    _require_push_token(x_push_token)
    if confirm != "yes-reset-all-data":
        raise HTTPException(400, "clear_all 需顯式确认，请传 confirm=yes-reset-all-data（该操作会清除本 run 的所有业务数据、不可恢复）")
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_artifact WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_metric WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_industry_baseline WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_sample_distribution WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_violation_distribution WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_industry_baseline_l2 WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_sample_distribution_l2 WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM weekly_violation_distribution_l2 WHERE run_id=%s", (run_id,))
        cur.execute("UPDATE stage_log SET status='pending', message=NULL, started_at=NULL, finished_at=NULL WHERE run_id=%s", (run_id,))
        cur.execute("UPDATE weekly_run SET status='created', current_stage='fetch_full', automation_instance_url=NULL WHERE id=%s", (run_id,))
        c.commit()
    return _get_run_detail(run_id)


@app.post("/api/push/runs/{run_id}/metrics/clear")
def push_clear_metrics(
    run_id: int,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """只清本 run 的浓度数据（weekly_metric），不动 stage 状态、不删产物。供重新写入之前使用。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_metric WHERE run_id=%s", (run_id,))
        deleted = cur.rowcount
        c.commit()
    return {"deleted": deleted, **_get_run_detail(run_id)}

@app.post("/api/push/runs/{run_id}/artifacts/clear")
def push_clear_artifacts(
    run_id: int,
    stage_code: Optional[str] = None,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """openclaw 侧重跑前清理旧产物。stage_code 为空时清理该 run 全部产物。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        if stage_code:
            cur.execute("DELETE FROM weekly_artifact WHERE run_id=%s AND stage_code=%s", (run_id, stage_code))
            msg = f"[push] 已清理旧产物：stage={stage_code}"
        else:
            cur.execute("DELETE FROM weekly_artifact WHERE run_id=%s", (run_id,))
            msg = "[push] 已清理该 run 全部旧产物"
        deleted = cur.rowcount
        c.commit()
    if stage_code and stage_code in STAGE_TITLES:
        _update_stage(run_id, stage_code, status="pending", message=msg)
    return {"deleted": deleted, **_get_run_detail(run_id)}




class ReportUpdate(BaseModel):
    report_shortcut_id: str
    report_url: Optional[str] = None


@app.post("/api/push/runs/{run_id}/report")
def push_report_link(
    run_id: int,
    req: ReportUpdate,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """回填本 run 的周度报告 RedDoc 地址（子文档 shortcutId + url）。同一 run 重复推会覆盖。
    看板“评估报告”按钮会优先跳转到当周报告，没回填才回到目录页。
    """
    _require_push_token(x_push_token)
    url = req.report_url or f"https://docs.example.com/doc/{req.report_shortcut_id}"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE weekly_run SET report_shortcut_id=%s, report_url=%s, updated_at=NOW() WHERE id=%s",
            (req.report_shortcut_id, url, run_id),
        )
        c.commit()
    return _get_run_detail(run_id)


@app.post("/api/push/runs/{run_id}/automation")
def push_set_automation_url(
    run_id: int,
    req: DibpUpdate,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE weekly_run SET automation_instance_url=%s, updated_at=NOW() WHERE id=%s",
            (req.automation_instance_url, run_id),
        )
        c.commit()
    return _get_run_detail(run_id)


# ── 行业维度 push 接口（供数据推送可靠的数字，供报告生成器使用） ────────

class IndustryItem(BaseModel):
    industry: str
    registered_uid: Optional[int] = None
    clustered_uid: Optional[int] = None
    suspected_fake_uid: Optional[int] = None    # 聚簇算法自动判定的“多账号共用同一资质图”疑似账户数
    annotated_uid: Optional[int] = None         # 已进入人工标注池的账户数（抽样 + 同簇扩展并集）
    confirmed_fake_uid: Optional[int] = None    # 标注池里被人工实锤的账户数（与 annotated_uid 搭配得算法准确率）
    detail: Optional[dict] = None


class IndustryBaselineReq(BaseModel):
    period: Optional[str] = None          # 例如 "0701-0714"
    items: list[IndustryItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/industry_baseline")
def push_industry_baseline(
    run_id: int,
    req: IndustryBaselineReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本 run 的行业入驻基准数据（全量入驻/聚簇/实锤造假）。
    写入为快照（高时同一行业会覆盖不重复）。供违规占比、抽样代表性分析使用。
    """
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_industry_baseline WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_industry_baseline "
                "(run_id, period, industry, registered_uid, clustered_uid, suspected_fake_uid, annotated_uid, confirmed_fake_uid, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id, req.period, it.industry,
                    it.registered_uid, it.clustered_uid,
                    it.suspected_fake_uid, it.annotated_uid, it.confirmed_fake_uid,
                    json.dumps(it.detail or {}, ensure_ascii=False),
                ),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


class SampleCountItem(BaseModel):
    industry: str
    count: int
    detail: Optional[dict] = None


class SampleDistributionReq(BaseModel):
    items: list[SampleCountItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/sample_distribution")
def push_sample_distribution(
    run_id: int,
    req: SampleDistributionReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周抽样的各行业账户数。写入为快照（同一 run 下高时行业同名覆盖）。
    用于“抽样占比 vs 入驻占比”的代表性检验。
    """
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_sample_distribution WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_sample_distribution (run_id, industry, count, detail) VALUES (%s, %s, %s, %s)",
                (run_id, it.industry, it.count, json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


class ViolationCountItem(BaseModel):
    industry: str
    violated_count: int
    detail: Optional[dict] = None            # 可选：{"real":88, "suspect":3, "proxy":0}


class ViolationDistributionReq(BaseModel):
    items: list[ViolationCountItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/violation_distribution")
def push_violation_distribution(
    run_id: int,
    req: ViolationDistributionReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周实际判定违规账户的行业分布。detail 可可选地写 实锤/疑似/挂靠 拆分。
    用于“违规行业构成 vs 抽样占比”的超额贡献分析。
    """
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_violation_distribution WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_violation_distribution (run_id, industry, violated_count, detail) VALUES (%s, %s, %s, %s)",
                (run_id, it.industry, it.violated_count, json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


# ── 二级行业维度 push 接口 ───────────────────────────────────

class SampleCountItemL2(BaseModel):
    first_industry: str
    second_industry: str
    count: int
    detail: Optional[dict] = None


class SampleDistributionL2Req(BaseModel):
    items: list[SampleCountItemL2]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/sample_distribution_l2")
def push_sample_distribution_l2(
    run_id: int,
    req: SampleDistributionL2Req,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周抽样的二级行业分布。快照式写入（同一 run 重复推会覆盖）。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_sample_distribution_l2 WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_sample_distribution_l2 (run_id, first_industry, second_industry, count, detail) VALUES (%s, %s, %s, %s, %s)",
                (run_id, it.first_industry, it.second_industry, it.count,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


class ViolationCountItemL2(BaseModel):
    first_industry: str
    second_industry: str
    violated_count: int
    detail: Optional[dict] = None


class ViolationDistributionL2Req(BaseModel):
    items: list[ViolationCountItemL2]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/violation_distribution_l2")
def push_violation_distribution_l2(
    run_id: int,
    req: ViolationDistributionL2Req,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周违规账户的二级行业分布。快照式写入。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_violation_distribution_l2 WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_violation_distribution_l2 (run_id, first_industry, second_industry, violated_count, detail) VALUES (%s, %s, %s, %s, %s)",
                (run_id, it.first_industry, it.second_industry, it.violated_count,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


class IndustryItemL2(BaseModel):
    first_industry: str
    second_industry: str
    registered_uid: Optional[int] = None
    clustered_uid: Optional[int] = None
    suspected_fake_uid: Optional[int] = None
    annotated_uid: Optional[int] = None
    confirmed_fake_uid: Optional[int] = None
    detail: Optional[dict] = None


class IndustryBaselineL2Req(BaseModel):
    period: Optional[str] = None
    items: list[IndustryItemL2]
    note: Optional[str] = None


# ── 实锤造假来源分布 push 接口 ─────────────────────────────

class ViolationSourceItem(BaseModel):
    industry: str                          # 一级行业名，或 "大盘整体"
    professional_count: int = 0            # 专业号入驻中被实锤的账户数
    shop_count: int = 0                    # 号店入驻中被实锤的账户数
    ad_count: int = 0                      # 号广入驻中被实锤的账户数
    detail: Optional[dict] = None


class ViolationSourceReq(BaseModel):
    items: list[ViolationSourceItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/violation_source")
def push_violation_source(
    run_id: int,
    req: ViolationSourceReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周实锤造假账户的来源分布（专业号入驻 / 号店入驻 / 号广入驻）。快照式写入。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_violation_source WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_violation_source "
                "(run_id, industry, professional_count, shop_count, ad_count, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (run_id, it.industry, it.professional_count, it.shop_count, it.ad_count,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


# ── 入驻量级来源分布 push 接口 ─────────────────────

class RegisteredSourceItem(BaseModel):
    industry: str                          # 一级行业名，或 "大盘整体"
    professional_count: int = 0            # 专业号入驻账户数
    shop_count: int = 0                    # 号店入驻账户数
    ad_count: int = 0                      # 号广入驻账户数
    detail: Optional[dict] = None


class RegisteredSourceReq(BaseModel):
    items: list[RegisteredSourceItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/registered_source")
def push_registered_source(
    run_id: int,
    req: RegisteredSourceReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周入驻量的来源分布（专业号 / 号店 / 号广）。快照式写入，同 run 重复推 = 覆盖。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_registered_source WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_registered_source "
                "(run_id, industry, professional_count, shop_count, ad_count, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (run_id, it.industry, it.professional_count, it.shop_count, it.ad_count,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


# ── 认证方式分布 push 接口 ─────────────────────

class CertMethodItem(BaseModel):
    industry: str                          # 一级行业名，如 "商务服务"
    method: str                            # 认证方式，如 "对公打款" / "人脸识别"
    registered_count: int = 0              # 该认证方式的全量入驻账户数
    violated_count: int = 0                # 该认证方式的实锤造假账户数（抽样标注口径）
    detail: Optional[dict] = None


class CertMethodReq(BaseModel):
    items: list[CertMethodItem]
    note: Optional[str] = None


@app.post("/api/push/runs/{run_id}/cert_method")
def push_cert_method(
    run_id: int,
    req: CertMethodReq,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送按行业×认证方式的入驻/实锤造假分布（对公打款 / 人脸识别等）。快照式写入，同 run 重复推 = 覆盖。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_cert_method WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_cert_method "
                "(run_id, industry, method, registered_count, violated_count, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (run_id, it.industry, it.method, it.registered_count, it.violated_count,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


@app.post("/api/push/runs/{run_id}/industry_baseline_l2")
def push_industry_baseline_l2(
    run_id: int,
    req: IndustryBaselineL2Req,
    x_push_token: Optional[str] = Header(None, alias="X-Push-Token"),
) -> dict:
    """推送本周二级行业的入驻基准数据（可选）。快照式写入。"""
    _require_push_token(x_push_token)
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_industry_baseline_l2 WHERE run_id=%s", (run_id,))
        for it in req.items:
            cur.execute(
                "INSERT INTO weekly_industry_baseline_l2 "
                "(run_id, period, first_industry, second_industry, registered_uid, clustered_uid, suspected_fake_uid, annotated_uid, confirmed_fake_uid, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, req.period, it.first_industry, it.second_industry,
                 it.registered_uid, it.clustered_uid, it.suspected_fake_uid,
                 it.annotated_uid, it.confirmed_fake_uid,
                 json.dumps(it.detail or {}, ensure_ascii=False)),
            )
        c.commit()
    return {"count": len(req.items), **_get_run_detail(run_id)}


@app.post("/api/runs/{run_id}/artifacts/upload")
async def upload_artifact(
    run_id: int,
    stage_code: str,
    file: UploadFile = File(...),
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    _require_user(decrypted_userinfo)
    if stage_code not in STAGE_TITLES:
        raise HTTPException(400, f"未知阶段 {stage_code}")
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "文件为空")
    if len(blob) > 200 * 1024 * 1024:
        raise HTTPException(413, "单文件不支持 > 200MB")
    aid = _save_artifact(
        run_id, stage_code, file.filename or "unnamed", file.content_type or "application/octet-stream", blob
    )
    # 不覆盖已有的业务 message（与 push_upload_artifact 一致的策略）
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT message FROM stage_log WHERE run_id=%s AND stage_code=%s", (run_id, stage_code))
        row = cur.fetchone()
    existing = (row and (row.get("message") or "").strip()) if row else ""
    if not existing or existing.startswith("[push] 产物已推送") or existing.startswith("已上传产物："):
        _update_stage(run_id, stage_code, status="done", message=f"已上传产物：{file.filename} ({len(blob)} bytes)")
    else:
        _update_stage(run_id, stage_code, status="done", message=existing)
    return {"artifact_id": aid, **_get_run_detail(run_id)}


@app.get("/api/artifacts/{artifact_id}/stats")
def artifact_stats(artifact_id: int) -> dict:
    """简易统计：仅支持 CSV，返回行数 + 可选列去重个数。
    查询参数无（预置常用列）、前端自己选字段。
    """
    a, blob = _read_artifact(artifact_id)
    filename = (a.get("filename") or "").lower()
    content_type = (a.get("content_type") or "").lower()
    if not (filename.endswith(".csv") or "csv" in content_type):
        return {"artifact_id": artifact_id, "filename": a.get("filename"), "csv": False}
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:
        return {"artifact_id": artifact_id, "filename": a.get("filename"), "csv": True, "error": "decode fail"}
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    rows = 0
    seen: dict[str, set[str]] = {}
    # 预定义常用列（存在就统计，不存在自动跳过）
    target_cols = [c for c in ("user_id", "cluster_id", "cluster", "group_id", "sample_group") if c in fieldnames]
    for c in target_cols:
        seen[c] = set()
    for row in reader:
        rows += 1
        for c in target_cols:
            v = (row.get(c) or "").strip()
            if v:
                seen[c].add(v)
    return {
        "artifact_id": artifact_id,
        "filename": a.get("filename"),
        "csv": True,
        "rows": rows,
        "columns": fieldnames,
        "unique": {c: len(seen[c]) for c in target_cols},
    }


@app.delete("/api/artifacts/{artifact_id}")
def delete_artifact(
    artifact_id: int,
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    _require_user(decrypted_userinfo)
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT run_id FROM weekly_artifact WHERE id=%s", (artifact_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "artifact not found")
        run_id = row["run_id"]
        cur.execute("DELETE FROM weekly_artifact WHERE id=%s", (artifact_id,))
        c.commit()
    return _get_run_detail(run_id)


@app.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: int) -> Response:
    a, blob = _read_artifact(artifact_id)
    raw_name = a["filename"] or f"artifact-{artifact_id}"
    # RFC 5987: filename* 需要 URL-encode，直接把中文写进 header 会触发 500
    encoded = quote(raw_name, safe="")
    # 同时给一个 ASCII fallback，避免部分老浏览器/中间件解不了 filename*
    ascii_fallback = raw_name.encode("ascii", errors="ignore").decode("ascii") or f"artifact-{artifact_id}"
    disp = f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
    return Response(
        content=blob,
        media_type=a["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": disp},
    )


@app.post("/api/runs/{run_id}/calc_metric")
async def calc_metric_api(
    run_id: int,
    file: UploadFile = File(...),
    decrypted_userinfo: Optional[str] = Header(None, alias="Decrypted-Userinfo"),
) -> dict:
    """上传标注结果 CSV，直接算浓度并写库。文件也会作为产物落库。"""
    _require_user(decrypted_userinfo)
    blob = await file.read()
    if not blob:
        raise HTTPException(400, "文件为空")
    metrics = _calc_metric_from_csv(blob)
    # 先落原始文件
    _save_artifact(run_id, "calc_metric", file.filename or "annotation.csv", file.content_type or "text/csv", blob)
    # 写指标
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM weekly_metric WHERE run_id=%s", (run_id,))
        for m in metrics:
            cur.execute(
                "INSERT INTO weekly_metric (run_id, sample_group, annotated_sample_users, "
                "violated_sample_users, concentration, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    m["sample_group"],
                    m["annotated_sample_users"],
                    m["violated_sample_users"],
                    m["concentration"],
                    json.dumps(m["detail"], ensure_ascii=False),
                ),
            )
        c.commit()
    _update_stage(run_id, "calc_metric", status="done", message=f"评估完成，共 {len(metrics)} 个样本组")
    return _get_run_detail(run_id)


# ── 简易调度：周一 08:00 建 run，周三 08:00 提示评估 ────────────────────────
async def _scheduler_loop() -> None:
    """MVP 调度：只做“到点建 run / 到点写提示阶段日志”。真实生产建议改用平台调度。"""
    while True:
        try:
            now = _now_cn()
            # 计算下一次触发时间：下一个整点 5s 后检查
            await asyncio.sleep(30)
            if not _db_available():
                continue
            # 每周一 08:00 建 run
            if now.weekday() == 0 and now.time() >= dtime(8, 0) and now.time() < dtime(8, 5):
                ws, we = _last_week_range(now.date())
                run = _ensure_run(ws, we)
                _update_stage(
                    run["id"], STAGE_CODES[0], status="pending",
                    message="[定时调度] 周一 08:00 自动创建本周任务，等待各阶段接入生产任务后自动执行",
                )
            # 每周三 08:00 提示评估（但不要覆盖已 done 的状态）
            if now.weekday() == 2 and now.time() >= dtime(8, 0) and now.time() < dtime(8, 5):
                ws, we = _last_week_range(now.date() - timedelta(days=2))  # 依然指向同一周
                with _conn() as c, c.cursor() as cur:
                    # 只在 calc_metric 还没 done 时才写提示
                    cur.execute(
                        "SELECT id, status FROM stage_log WHERE run_id="
                        "(SELECT id FROM weekly_run WHERE week_start=%s) AND stage_code='calc_metric' "
                        "ORDER BY id DESC LIMIT 1",
                        (ws,),
                    )
                    row = cur.fetchone()
                if row and row.get("status") not in ("done", "running"):
                    _update_stage(
                        row["id"], "calc_metric", status="pending",
                        message="[定时调度] 周三 08:00 提示：可上传标注结果 CSV 进行违规浓度计算",
                    )
        except Exception:
            traceback.print_exc()


@app.on_event("startup")
async def _launch_scheduler() -> None:
    # 独立后台任务，出错自恢复
    asyncio.create_task(_scheduler_loop())


# ── 前端 HTML（单文件内嵌，SSO 后直接渲染） ─────────────────────────────────
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>资质造假评估</title>
<style>
:root {
  --bg: #ffffff;
  --border: #f0f1f3;
  --border-strong: #e5e7eb;
  --text-1: #0f172a;
  --text-2: #475569;
  --text-3: #94a3b8;
  --blue: #2563eb;
  --blue-soft: #eff6ff;
  --green: #10b981;
  --green-soft: #ecfdf5;
  --red: #ef4444;
  --red-soft: #fef2f2;
  --gray: #cbd5e1;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "PingFang SC", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text-1);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
a { text-decoration: none; color: inherit; }

/* ── Header ── */
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 20px 40px;
  border-bottom: 1px solid var(--border);
}
.header-left { display: flex; align-items: center; gap: 12px; }
.brand-logo {
  width: 28px; height: 28px; border-radius: 8px;
  background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 700; font-size: 13px;
  box-shadow: 0 2px 8px rgba(37,99,235,0.2);
}
.header-left h1 { font-size: 15px; font-weight: 600; letter-spacing: 0.2px; }
.week-badge {
  color: var(--text-3); font-size: 13px; font-variant-numeric: tabular-nums;
  margin-left: 4px;
}

/* 周期切换入口 */
.week-switcher { position: relative; margin-left: 6px; }
.week-badge-btn {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid transparent; background: transparent;
  padding: 4px 10px; border-radius: 6px;
  color: var(--text-3); font-size: 13px; font-variant-numeric: tabular-nums;
  cursor: pointer; transition: all .15s;
  font-family: inherit;
}
.week-badge-btn:hover { background: #f8fafc; color: var(--text-1); border-color: var(--border); }
.week-badge-btn .chevron { opacity: 0.6; transition: transform .15s; }
.week-switcher.open .week-badge-btn { background: #f8fafc; color: var(--text-1); border-color: var(--border); }
.week-switcher.open .week-badge-btn .chevron { transform: rotate(180deg); }
.week-menu {
  position: absolute; left: 0; top: calc(100% + 6px);
  background: #fff; border: 1px solid var(--border);
  border-radius: 10px; min-width: 240px; z-index: 200; padding: 6px 0;
  box-shadow: 0 12px 28px -8px rgba(15,23,42,0.12);
  display: none;
  max-height: 380px; overflow-y: auto;
}
.week-switcher.open .week-menu { display: block; }
.week-menu-item {
  padding: 9px 14px; font-size: 13px; color: var(--text-1);
  cursor: pointer; display: flex; align-items: center; justify-content: space-between;
  gap: 12px; transition: background .15s;
  font-variant-numeric: tabular-nums;
}
.week-menu-item:hover { background: #f8fafc; }
.week-menu-item.active { background: var(--blue-soft); color: var(--blue); font-weight: 500; }
.week-menu-item .wm-status {
  font-size: 11px; padding: 1px 6px; border-radius: 3px;
  background: #f3f4f6; color: var(--text-3);
}
.week-menu-item .wm-status.done { background: var(--green-soft); color: var(--green); }
.week-menu-item .wm-status.running { background: var(--blue-soft); color: var(--blue); }
.week-menu-item .wm-status.failed { background: var(--red-soft); color: var(--red); }
.week-menu-empty { padding: 14px; color: var(--text-3); font-size: 12px; text-align: center; }

/* 整体状态 */
.overall-status {
  display: inline-flex; align-items: center; gap: 6px;
  margin-left: 4px; padding: 4px 10px;
  border-radius: 6px; background: transparent;
  font-size: 12px; color: var(--text-2);
  font-variant-numeric: tabular-nums;
  transition: background .15s;
}
.overall-status .os-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-3); flex-shrink: 0;
}
.overall-status.running .os-dot {
  background: var(--blue);
  animation: pulse 2s infinite;
}
.overall-status.done .os-dot { background: var(--green); }
.overall-status.failed .os-dot { background: var(--red); }
.overall-status.pending .os-dot { background: var(--text-3); }
.header-right { display: flex; align-items: center; gap: 2px; }
.nav-link {
  color: var(--text-2); font-size: 13px;
  padding: 6px 10px; border-radius: 6px;
  display: inline-flex; align-items: center; gap: 6px;
  transition: all .15s;
}
.nav-link:hover { color: var(--text-1); background: #f8fafc; }
.nav-link.primary { color: var(--blue); font-weight: 500; }
.nav-icon {
  width: 14px; height: 14px; display: inline-block;
  opacity: 0.7;
}

.icon-btn {
  border: none; background: transparent; cursor: pointer;
  padding: 6px 8px; border-radius: 6px; color: var(--text-2);
  transition: all .15s; font-size: 15px; margin-left: 4px;
}
.icon-btn:hover { background: #f8fafc; color: var(--text-1); }

/* ── Dropdown ── */
.dropdown { position: relative; }
.dropdown-menu {
  position: absolute; right: 0; top: calc(100% + 6px);
  background: #fff; border: 1px solid var(--border);
  border-radius: 10px; width: 240px; z-index: 200; padding: 6px 0;
  box-shadow: 0 12px 28px -8px rgba(15,23,42,0.12);
  display: none;
}
.dropdown-item {
  padding: 9px 16px; font-size: 13px; color: var(--text-1);
  cursor: pointer; display: flex; align-items: center; gap: 10px;
  transition: background .15s;
}
.dropdown-item:hover { background: #f8fafc; }
.dropdown-item.warn { color: var(--red); }
.dropdown-title {
  padding: 10px 16px 4px; font-size: 11px; color: var(--text-3);
  text-transform: uppercase; font-weight: 600; letter-spacing: 0.6px;
}
.dropdown-divider { height: 1px; background: var(--border); margin: 6px 0; }
.full-select {
  width: calc(100% - 32px); margin: 4px 16px; padding: 6px 10px;
  border-radius: 6px; border: 1px solid var(--border);
  font-size: 12px; outline: none; background: #fff;
}
.full-select:focus { border-color: var(--blue); }

/* ── Layout ── */
.main {
  max-width: 1200px; margin: 0 auto; padding: 32px 40px 80px;
}
.main-layout {
  display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  gap: 56px; align-items: start;
}
@media (max-width: 900px) { .main-layout { grid-template-columns: 1fr; gap: 40px; } }

.section-title {
  font-size: 12px; font-weight: 600; color: var(--text-3);
  text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 18px;
  display: flex; align-items: center; justify-content: space-between;
}
.section-title .refresh-btn {
  color: var(--text-3); cursor: pointer; padding: 2px 6px;
  border-radius: 4px; transition: all .15s; font-size: 13px;
}
.section-title .refresh-btn:hover { color: var(--text-1); background: #f8fafc; }
.section + .section { margin-top: 40px; }

/* ── Timeline ── */
.timeline { list-style: none; }
.stage-item {
  position: relative; padding-left: 24px; padding-bottom: 20px;
}
.stage-item:last-child { padding-bottom: 0; }
.stage-item::before {
  content: ''; position: absolute; left: 5px; top: 18px; bottom: -4px;
  width: 1.5px; background: var(--border);
}
.stage-item:last-child::before { display: none; }

.stage-icon {
  position: absolute; left: 0; top: 4px;
  width: 12px; height: 12px; border-radius: 50%;
  background: #fff; border: 2px solid var(--gray); z-index: 2;
}
.stage-icon.running {
  border-color: var(--blue); background: var(--blue);
  animation: pulse 2s infinite;
}
.stage-icon.done { border-color: var(--green); background: var(--green); }
.stage-icon.failed { border-color: var(--red); background: var(--red); }
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(37,99,235,0.4); }
  50% { box-shadow: 0 0 0 6px rgba(37,99,235,0); }
}

.stage-header {
  display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
}
.stage-title { font-weight: 500; font-size: 14px; color: var(--text-1); }
.stage-time {
  font-size: 12px; color: var(--text-3);
  font-variant-numeric: tabular-nums; flex-shrink: 0;
}
.stage-log {
  font-size: 12px; color: var(--text-3);
  margin-top: 3px; line-height: 1.5;
}
.stage-actions { display: flex; gap: 8px; margin-top: 10px; }

.mark-btn {
  border: 1px solid var(--border); background: #fff;
  padding: 3px 10px; border-radius: 5px; font-size: 12px;
  color: var(--text-2); cursor: pointer; transition: all .15s;
}
.mark-btn:hover { border-color: var(--gray); color: var(--text-1); }

/* ── Metrics Table ── */
.metric-table { width: 100%; border-collapse: collapse; }
.metric-table th, .metric-table td {
  padding: 12px 8px; text-align: left; font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.metric-table th {
  font-weight: 500; color: var(--text-3); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.5px;
  padding-bottom: 8px;
}
.metric-table th:first-child, .metric-table td:first-child { padding-left: 0; }
.metric-table th:last-child, .metric-table td:last-child { padding-right: 0; }
.metric-table tr:last-child td { border-bottom: none; }

.metric-cell {
  display: flex; flex-direction: column; gap: 3px;
}
.metric-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px; font-weight: 500; color: var(--text-1);
  font-variant-numeric: tabular-nums;
  line-height: 1.3;
  letter-spacing: -0.2px;
}
.metric-delta {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px; font-variant-numeric: tabular-nums;
  display: inline-flex; align-items: center; gap: 2px;
  line-height: 1.2;
  letter-spacing: -0.2px;
}
.metric-delta.up { color: var(--red); }
.metric-delta.down { color: var(--green); }
.metric-delta.flat { color: var(--text-3); }
.metric-value.empty {
  color: var(--text-3); font-weight: 400;
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "PingFang SC", sans-serif;
}

.report-link {
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--blue); font-size: 12px; font-weight: 500;
  padding: 3px 8px; border-radius: 5px;
  text-transform: none; letter-spacing: normal;
  transition: background .15s;
  text-decoration: none;
}
.report-link:hover { background: var(--blue-soft); }
.report-link .nav-icon { width: 12px; height: 12px; opacity: 0.9; }

.week-cell {
  font-size: 12px; color: var(--text-2);
  font-variant-numeric: tabular-nums;
}

/* ── Artifacts ── */
.artifact-group {
  border-bottom: 1px solid var(--border);
}
.artifact-group:last-child { border-bottom: none; }
.artifact-group-header {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 0; cursor: pointer;
  transition: all .15s;
}
.artifact-group-header:hover .artifact-group-title { color: var(--text-1); }
.artifact-group-toggle {
  color: var(--text-3); font-size: 10px;
  transition: transform 0.2s;
  display: inline-block;
}
.artifact-group.expanded .artifact-group-toggle {
  transform: rotate(90deg);
}
.artifact-group-title {
  font-size: 13px; color: var(--text-2); font-weight: 500;
  transition: color .15s;
}
.artifact-group-count {
  font-size: 11px; color: var(--text-3);
  background: #f8fafc; padding: 1px 6px; border-radius: 8px;
}
.artifact-group-list {
  display: none; padding-bottom: 8px;
}
.artifact-group.expanded .artifact-group-list {
  display: block;
}
.artifact-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0 8px 16px;
}
.artifact-info { min-width: 0; }
.artifact-info .fname {
  font-size: 13px; color: var(--text-1); margin-bottom: 2px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 320px;
}
.artifact-info .fmeta {
  font-size: 11px; color: var(--text-3);
}
.artifact-actions {
  display: flex; align-items: center; gap: 4px;
  flex-shrink: 0;
}
.artifact-btn {
  color: var(--blue); font-size: 12px; font-weight: 500;
  padding: 4px 8px; border-radius: 5px; transition: background .15s;
  cursor: pointer;
}
.artifact-btn:hover { background: var(--blue-soft); }
.artifact-btn.delete { color: var(--text-3); }
.artifact-btn.delete:hover { color: var(--red); background: var(--red-soft); }

/* ── Empty State ── */
.dim { color: var(--text-3); padding: 20px 0; font-size: 13px; }
.empty { color: var(--text-3); font-size: 13px; padding: 12px 0; }
</style>
</head>
<body>

<header class="header">
  <div class="header-left">
    <div class="brand-logo">Q</div>
    <h1>资质造假评估</h1>
    <div class="week-switcher" id="week-switcher">
      <button class="week-badge-btn" id="week-badge-btn" type="button" onclick="toggleWeekMenu(event)">
        <span id="week-badge">--</span>
        <svg class="chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      <div class="week-menu" id="week-menu"></div>
    </div>
    <div class="overall-status" id="overall-status" title="本周整体状态">
      <span class="os-dot"></span>
      <span class="os-text">--</span>
    </div>
  </div>
  <div class="header-right">
    <a id="automation-link" href="#" class="nav-link primary" style="display:none;" target="_blank">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3h7v7M10 14L21 3M5 21h14a2 2 0 0 0 2-2v-6"/></svg>
      任务
    </a>
    <a href="__ANNOTATION_URL__" class="nav-link" target="_blank">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="m18.5 2.5 3 3L12 15l-4 1 1-4z"/></svg>
      标注平台
    </a>
    <a href="__RESULT_URL__" class="nav-link" target="_blank">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
      结果留存
    </a>

    <div class="dropdown">
      <button class="icon-btn" onclick="toggleGear(event)" title="管理操作">
        <svg style="width:16px;height:16px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </button>
      <div class="dropdown-menu" id="gear-menu">
        <div class="dropdown-item warn" id="btn-start">⚡ 初始化本周任务</div>
        <div class="dropdown-item" onclick="toggleFallbackMarks(event)">🛠 显示状态标记按钮</div>

        <div class="dropdown-divider"></div>
        <div class="dropdown-title">上传产物</div>
        <select id="upload-stage" class="full-select"></select>
        <label class="dropdown-item">📁 选择文件<input type="file" id="upload-input" hidden></label>

        <div class="dropdown-divider"></div>
        <div class="dropdown-title">上传评估 CSV</div>
        <label class="dropdown-item">📊 选择 CSV<input type="file" id="metric-input" accept=".csv" hidden></label>
      </div>
    </div>
  </div>
</header>

<main class="main">
  <div class="main-layout">

    <!-- 左侧：时间轴 -->
    <div class="left-col">
      <div class="section">
        <div class="section-title">
          <span>资质造假浓度评估工作流</span>
          <span class="refresh-btn" id="btn-refresh" title="刷新">↻</span>
        </div>
        <ul class="timeline" id="stages">
          <div class="dim">加载中...</div>
        </ul>
      </div>
    </div>

    <!-- 右侧：违规浓度 + 产物 -->
    <div class="right-col">
      <div class="section">
        <div class="section-title">
          <span>违规浓度</span>
          <a class="report-link" id="report-link" href="/report" target="_blank" rel="noopener" title="打开本周可视化评估视图">
            <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/></svg>
            评估视图
          </a>
        </div>
        <table class="metric-table" id="metric-table">
          <thead>
            <tr>
              <th>评估周期</th>
              <th>大盘整体</th>
              <th>房地产</th>
              <th>金融行业</th>
            </tr>
          </thead>
          <tbody id="metric-body">
            <tr><td colspan="4" class="empty">暂无数据</td></tr>
          </tbody>
        </table>
      </div>

      <div class="section">
        <div class="section-title">
          <span>文件产物</span>
        </div>
        <div id="artifact-list">
          <div class="empty">暂无产物</div>
        </div>
      </div>
    </div>
  </div>

</main>

<script>
const state = { meta:null, currentRun:null, allRuns:[], showMarks:false, expandedGroups:{}, artifactStats:{} };

// 周期切换菜单
function toggleWeekMenu(e) {
  if (e) e.stopPropagation();
  document.getElementById('week-switcher').classList.toggle('open');
}
document.addEventListener('click', () => {
  const sw = document.getElementById('week-switcher');
  if (sw) sw.classList.remove('open');
});
document.addEventListener('DOMContentLoaded', () => {
  const menu = document.getElementById('week-menu');
  if (menu) menu.addEventListener('click', e => e.stopPropagation());
});

// stats 同步获取（依赖缓存）；如果未缓存，发请求并在回来后重新渲染
function getArtifactStatsSync(artifactId) {
  const key = String(artifactId);
  if (state.artifactStats[key] !== undefined) return state.artifactStats[key];
  state.artifactStats[key] = null; // 占位，避免重复拉
  fetch(`/api/artifacts/${artifactId}/stats`).then(r => r.json()).then(data => {
    state.artifactStats[key] = data;
    if (state.currentRun) renderCurrent(); // 拿到后重渲
  }).catch(() => { state.artifactStats[key] = null; });
  return null;
}

function toggleGear(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById('gear-menu');
  menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
}
document.addEventListener('click', () => {
  const menu = document.getElementById('gear-menu');
  if (menu) menu.style.display = 'none';
});
document.getElementById('gear-menu').addEventListener('click', e => e.stopPropagation());

function toggleFallbackMarks(e) {
  if(e) e.preventDefault();
  state.showMarks = !state.showMarks;
  renderCurrent();
  toggleGear();
}

// 阶段标题精简映射（前端展示层）
const STAGE_TITLE_MAP = {
  fetch_full: '取数',
  cluster_full: '聚簇',
  fetch_sample: '抽样',
  match_expand: '匹配扩展',
  upload_annotate: '上传标注',
  notify_hi: '提醒通知',
  wait_annotation: '等待标注',
  calc_metric: '浓度评估',
  write_result: '结果留存',
};

// 阶段日志精简：按阶段类型定制展示规则
// 优先从 stage message 里提取业务数字；抽不到时回退到从 产物文件名/大小维度展示
function extractStageLog(code, msg, artifacts, status) {
  const stageArts = (artifacts || []).filter(a => a.stage_code === code);

  // 先尝试从业务消息提取；失败后从 artifacts 兄弟字段兼向下文展示
  const businessSummary = tryBusinessSummary(code, msg, status);
  if (businessSummary) return businessSummary;

  // 开头就是 [push] 产物已推送 / 当前无有信息时，回退到产物维度
  return artifactSummary(code, stageArts);
}

function tryBusinessSummary(code, msg, status) {
  if (!msg) return '';
  msg = String(msg);
  // 如果是默认 push 提示，直接返回空，进入产物兼往下展示
  if (/^\[push\]\s*产物已推送：/.test(msg)) return '';

  const pickNum = (re) => { const m = msg.match(re); return m ? m[1].replace(/,/g,'') : null; };
  const fmtNum = (n) => n === null ? null : Number(n).toLocaleString('en-US');

  switch (code) {
    case 'fetch_full': {
      // 共提取 24,389 行，覆盖 14,215 个账户
      const rows = pickNum(/(\d[\d,]*)\s*行/);
      const accounts = pickNum(/覆盖\s*(\d[\d,]*)\s*个\s*账户/) || pickNum(/去重[^\d]*(\d[\d,]*)\s*个/) || pickNum(/(\d[\d,]*)\s*个\s*账户/);
      const parts = [];
      if (rows) parts.push(`${fmtNum(rows)} 行`);
      if (accounts) parts.push(`${fmtNum(accounts)} 个账户`);
      return parts.join(' · ');
    }
    case 'cluster_full': {
      // 共聚簇 24,389 行，形成 2,430 个簇，其中可疑簇 1,923 个，未成簇 15,082 行
      const rows = pickNum(/(\d[\d,]*)\s*行/);
      const clusters = pickNum(/(\d[\d,]*)\s*(?:个\s*)?簇/);
      const suspect = pickNum(/可疑簇\s*(\d[\d,]*)/) || pickNum(/可疑[^\d]*(\d[\d,]*)\s*个/);
      const parts = [];
      if (rows) parts.push(`${fmtNum(rows)} 行`);
      if (clusters) parts.push(`${fmtNum(clusters)} 个簇`);
      if (suspect) parts.push(`可疑 ${fmtNum(suspect)} 个簇`);
      return parts.join(' · ');
    }
    case 'fetch_sample': {
      // 全行业抽样 1,034 个账户，金融行业 24 个，房地产 502 个，去重后共 1,517 个账户
      const parts = [];
      const all = pickNum(/全行业[^\d]*(\d[\d,]*)/);
      const fin = pickNum(/金融[^\d]*(\d[\d,]*)/);
      const re  = pickNum(/房(?:地产)?[^\d]*(\d[\d,]*)/);
      const dedup = pickNum(/去重[^\d]*(?:共\s*)?(\d[\d,]*)/) || pickNum(/合计[^\d]*(\d[\d,]*)/);
      if (all) parts.push(`全行业 ${fmtNum(all)}`);
      if (fin) parts.push(`金融 ${fmtNum(fin)}`);
      if (re)  parts.push(`房地产 ${fmtNum(re)}`);
      if (dedup) parts.push(`去重 ${fmtNum(dedup)}`);
      return parts.join(' · ');
    }
    case 'match_expand': {
      // 共匹配上 780 个抽样账户，命中 689 个簇。
      // 抽样账户 1,517 个，其中 23 个因无资质图数据剔除，有效抽样账户 1,494 个（浓度计算分母）…
      // 匹配到簇的账户带出同簇账户 2,315 个，待标注账户合计 3,095 个
      const valid = pickNum(/有效抽样账户\s*(\d[\d,]*)/);
      const added = pickNum(/同簇账户\s*(\d[\d,]*)/) || pickNum(/新增\s*(?:同簇账户)?\s*(\d[\d,]*)/);
      const parts = [];
      if (valid) parts.push(`有效抽样账户 ${fmtNum(valid)} 个`);
      if (added) parts.push(`新增同簇账户 ${fmtNum(added)} 个`);
      return parts.join(' · ');
    }
    case 'upload_annotate': {
      // 已上传标注平台，数据集 xxx.csv，共 5,511 行 / 689 个簇 / 3,095 个账户
      const rows = pickNum(/(\d[\d,]*)\s*行/);
      const clusters = pickNum(/(\d[\d,]*)\s*个?\s*簇/);
      const accounts = pickNum(/(\d[\d,]*)\s*个\s*账户/);
      const parts = [];
      if (rows) parts.push(`${fmtNum(rows)} 行`);
      if (clusters) parts.push(`${fmtNum(clusters)} 个簇`);
      if (accounts) parts.push(`${fmtNum(accounts)} 个账户`);
      return parts.join(' · ');
    }
    case 'notify_hi': {
      // 只留第一句（遇到句号/逗号/换行就截）
      const first = msg.split(/[。，,、\n\r]/)[0].trim();
      return first.length > 40 ? first.slice(0, 40) + '…' : first;
    }
    case 'wait_annotation': {
      return '等待人工标注中';
    }
    case 'calc_metric': {
      // done 时优先展示固定文案，即使上游不推 message
      if (status === 'done') return '违规浓度计算完成';
      if (/^\s*$/.test(msg)) return '';
      const groups = pickNum(/(\d[\d,]*)\s*个?\s*样本组/);
      if (groups) return `完成 ${fmtNum(groups)} 个样本组浓度计算`;
      return '违规浓度计算完成';
    }
    case 'write_result': {
      if (status === 'done') return '已回填结果留存表';
      if (/^\s*$/.test(msg)) return '';
      return '已回填结果留存表';
    }
    default: {
      const firstLine = msg.split(/[\n\r。]/)[0].trim();
      return firstLine.length > 40 ? firstLine.slice(0, 40) + '…' : firstLine;
    }
  }
}

// 从阶段已上传的文件名列表启发式地展示（先看文件名里有无 sample/cluster/expanded 等关键字）
function artifactSummary(code, arts) {
  if (!arts || !arts.length) {
    if (code === 'wait_annotation') return '等待人工标注中';
    return '';
  }
  const names = arts.map(a => a.filename).join(' , ');
  const bySize = arts.reduce((sum, a) => sum + (a.size || 0), 0);

  switch (code) {
    case 'fetch_full': {
      const a = arts[0];
      return `全量取数完成 · ${a.filename}`;
    }
    case 'cluster_full': {
      const cluster = arts.find(a => /cluster/i.test(a.filename) && !/suspect/i.test(a.filename));
      const suspect = arts.find(a => /suspect/i.test(a.filename));
      const parts = [];
      // 异步拉 stats 拿簇数（cluster_id 去重）——有缓存就直接取
      if (cluster) {
        const st = getArtifactStatsSync(cluster.id);
        if (st && st.unique) {
          const clusters = st.unique.cluster_id || st.unique.cluster || st.unique.group_id;
          if (st.rows) parts.push(`${st.rows.toLocaleString('en-US')} 行`);
          if (clusters) parts.push(`${clusters.toLocaleString('en-US')} 簇`);
        } else {
          parts.push('聚簇完成');
        }
      } else {
        parts.push('聚簇完成');
      }
      if (suspect) parts.push('含可疑簇');
      return parts.join(' · ');
    }
    case 'fetch_sample': {
      const groups = [];
      if (arts.find(a => /overall|allindustry|全行业/i.test(a.filename))) groups.push('全行业');
      if (arts.find(a => /finance|金融/i.test(a.filename))) groups.push('金融');
      if (arts.find(a => /realestate|房地产/i.test(a.filename))) groups.push('房地产');
      return groups.length ? `已抽样：${groups.join(' / ')}` : `已抽样 · ${arts.length} 个产物`;
    }
    case 'match_expand': {
      const expanded = arts.find(a => /expanded/i.test(a.filename));
      const matched = arts.find(a => /matched/i.test(a.filename));
      const parts = [];
      if (matched) parts.push('已命中');
      if (expanded) parts.push('已拓展同簇');
      return parts.join(' · ') || '匹配拓展完成';
    }
    case 'upload_annotate': {
      return `已上传 · ${arts[0].filename}`;
    }
    default:
      return arts.length > 1 ? `${arts.length} 个产物` : arts[0].filename;
  }
}

function defaultShort(msg) {
  const firstLine = String(msg||'').split(/[\n\r。]/)[0].trim();
  return firstLine.length > 40 ? firstLine.slice(0, 40) + '…' : firstLine;
}

function stageDotClass(status){
  if(status==='done') return 'done';
  if(status==='running') return 'running';
  if(status==='failed') return 'failed';
  return '';
}
const statusTextMap = {done:'完成', running:'进行中', failed:'失败', pending:'待执行', skipped:'跳过', created:'已创建'};

async function fetchJSON(url, opts){
  const resp = await fetch(url, opts);
  if(!resp.ok){ const txt = await resp.text(); throw new Error(`${resp.status}: ${txt}`); }
  return resp.json();
}
function fmtTime(iso){ return iso ? iso.replace('T',' ').slice(5,16) : ''; }

async function loadMeta(){
  state.meta = await fetchJSON('/api/meta');
  document.getElementById('week-badge').textContent = `${state.meta.next_week_start.slice(5)} ~ ${state.meta.next_week_end.slice(5)}`;
  const sel = document.getElementById('upload-stage');
  sel.innerHTML = state.meta.stage_definitions.map(s=>`<option value="${s.code}">${STAGE_TITLE_MAP[s.code] || s.title}</option>`).join('');
}

async function loadCurrent(){
  try{ state.currentRun = await fetchJSON('/api/runs/current'); renderCurrent(); }
  catch(e){
    document.getElementById('week-badge').textContent = '未初始化';
    document.getElementById('stages').innerHTML = '<div class="dim">暂无本周任务</div>';
  }
}

// 加载最近几周的浓度数据，做环比
async function loadRunsAndMetrics(){
  const {runs} = await fetchJSON('/api/runs?limit=12');
  state.allRuns = runs;

  // 顶部周期切换菜单
  renderWeekMenu();

  await loadMetricsHistory();
}

function renderWeekMenu() {
  const menu = document.getElementById('week-menu');
  if (!menu) return;
  const curId = state.currentRun?.run?.id;
  if (!state.allRuns.length) {
    menu.innerHTML = '<div class="week-menu-empty">尚无历史周期</div>';
    return;
  }
  menu.innerHTML = state.allRuns.map(r => {
    const label = `${r.week_start.slice(5)} ~ ${r.week_end.slice(5)}`;
    const statusCls = stageDotClass(r.status);
    const statusLabel = statusTextMap[r.status] || r.status;
    const active = curId === r.id ? ' active' : '';
    return `<div class="week-menu-item${active}" data-run="${r.id}">
      <span>${label}</span>
      <span class="wm-status ${statusCls}">${statusLabel}</span>
    </div>`;
  }).join('');
  menu.querySelectorAll('.week-menu-item').forEach(el => {
    el.addEventListener('click', async () => {
      const runId = parseInt(el.dataset.run, 10);
      document.getElementById('week-switcher').classList.remove('open');
      if (runId === curId) return;
      await switchToRun(runId);
    });
  });
}

async function switchToRun(runId) {
  if (!runId) return;
  try {
    state.currentRun = await fetchJSON(`/api/runs/${runId}`);
    state.expandedGroups = {}; // 重置产物展开状态
    renderCurrent();
    renderWeekMenu();
  } catch (e) { alert('切换失败：' + e.message); }
}

// 拉历史浓度（供 renderCurrentMetrics 找上周环比）——直接用 /api/metrics_history，它会含基线 run
async function loadMetricsHistory() {
  const j = await fetchJSON('/api/metrics_history?limit=20').catch(() => ({ weeks: [] }));
  // 已按 week_start 升序
  state.metricsHistory = (j.weeks || []).map(w => ({
    run_id: w.run_id,
    week_start: w.week_start,
    overall: w.overall,
    realestate: w.realestate,
    finance: w.finance,
  }));
  renderCurrentMetrics();
}

// 仅展示当前 run 的一行，带环比上周箭头
function renderCurrentMetrics() {
  const tb = document.getElementById('metric-body');
  if (!tb) return;
  const d = state.currentRun;
  if (!d || !d.metrics || !d.metrics.length) {
    tb.innerHTML = '<tr><td colspan="4" class="empty">当前周无浓度数据</td></tr>';
    return;
  }

  const map = {};
  d.metrics.forEach(m => {
    const key = m.sample_group.replace('行业样本','').replace('样本','').trim();
    map[key] = m.concentration;
  });
  const cur = {
    overall: map['整体'] ?? map['全行业'] ?? map['大盘'] ?? null,
    realestate: map['房地产'] ?? null,
    finance: map['金融'] ?? null,
  };

  // 上周：从 history 里找当前 run 前一个
  const history = state.metricsHistory || [];
  const curIdx = history.findIndex(h => h.run_id === d.run.id);
  const prev = curIdx > 0 ? history[curIdx - 1]
                          : (curIdx === -1 && history.length ? history[history.length - 1] : null);

  const week = `${d.run.week_start.slice(5)} ~ ${d.run.week_end.slice(5)}`;
  tb.innerHTML = `
    <tr>
      <td><span class="week-cell">${week}</span></td>
      <td>${renderMetricCell(cur.overall, prev?.overall)}</td>
      <td>${renderMetricCell(cur.realestate, prev?.realestate)}</td>
      <td>${renderMetricCell(cur.finance, prev?.finance)}</td>
    </tr>
  `;
}

function renderMetricCell(cur, prev) {
  if (cur === null || cur === undefined) {
    return '<span class="metric-value empty">—</span>';
  }
  const curPct = (cur * 100).toFixed(2) + '%';
  let deltaHtml = '';
  if (prev !== null && prev !== undefined) {
    const diff = (cur - prev) * 100; // 百分点变化
    if (Math.abs(diff) < 0.005) {
      deltaHtml = `<span class="metric-delta flat">— 持平</span>`;
    } else if (diff > 0) {
      deltaHtml = `<span class="metric-delta up">↑ ${diff.toFixed(2)}pp</span>`;
    } else {
      deltaHtml = `<span class="metric-delta down">↓ ${Math.abs(diff).toFixed(2)}pp</span>`;
    }
  } else {
    deltaHtml = `<span class="metric-delta flat">首周基线</span>`;
  }
  return `<div class="metric-cell"><span class="metric-value">${curPct}</span>${deltaHtml}</div>`;
}

function renderOverallStatus(stages) {
  const el = document.getElementById('overall-status');
  if (!el) return;
  const total = stages.length;
  const cnt = {done:0, running:0, failed:0, pending:0, skipped:0, created:0};
  stages.forEach(s => { cnt[s.status] = (cnt[s.status]||0) + 1; });
  const done = cnt.done + cnt.skipped;
  let cls = 'pending', text = `未开始 · 0/${total}`;
  if (cnt.failed > 0) {
    cls = 'failed';
    text = `存在失败 · ${done}/${total}`;
  } else if (cnt.running > 0) {
    cls = 'running';
    text = `进行中 · ${done}/${total}`;
  } else if (done === total) {
    cls = 'done';
    text = `已完成 · ${total}/${total}`;
  } else if (done > 0) {
    cls = 'pending';
    text = `待推进 · ${done}/${total}`;
  }
  el.className = 'overall-status ' + cls;
  el.querySelector('.os-text').textContent = text;
}

function renderCurrent(){
  const d = state.currentRun; if(!d) return;
  const run = d.run;
  document.getElementById('week-badge').textContent = `${run.week_start.slice(5)} ~ ${run.week_end.slice(5)}`;

  const automationA = document.getElementById('automation-link');
  if(run.automation_instance_url){ automationA.href = run.automation_instance_url; automationA.style.display = 'inline-flex'; }
  else { automationA.style.display = 'none'; }

  // 评估视图链接：直接指向当前 run 的可视化页面
  const reportA = document.getElementById('report-link');
  if (reportA) {
    reportA.href = `/report/${run.id}`;
    reportA.title = `查看 ${run.week_start.slice(5)} – ${run.week_end.slice(5)} 评估视图`;
  }

  // 整体状态（顶部小圆点）
  renderOverallStatus(d.stages);

  // Timeline
  const ul = document.getElementById('stages');
  ul.innerHTML = d.stages.map((s)=>{
    const shortTitle = STAGE_TITLE_MAP[s.code] || s.title;
    const shortLog = extractStageLog(s.code, s.message, d.artifacts, s.status);
    return `
    <li class="stage-item">
      <div class="stage-icon ${stageDotClass(s.status)}"></div>
      <div class="stage-header">
        <span class="stage-title">${shortTitle}</span>
        <span class="stage-time">${s.finished_at ? fmtTime(s.finished_at) : (s.started_at ? fmtTime(s.started_at) : '')}</span>
      </div>
      ${shortLog ? `<div class="stage-log">${escapeHtml(shortLog)}</div>` : ''}
      ${state.showMarks ? `
        <div class="stage-actions">
          <button class="mark-btn" onclick="mark('${s.code}','done')">强制成功</button>
          <button class="mark-btn" onclick="mark('${s.code}','failed')">强制失败</button>
        </div>` : ''}
    </li>`;
  }).join('');

  // Artifacts: 从当前 run 取
  renderArtifacts(d.artifacts || []);

  // 当周浓度（单行表）：切周时同步刷新，上周环比从 state.metricsHistory 找
  renderCurrentMetrics();
}

function renderArtifacts(artifacts) {
  const list = document.getElementById('artifact-list');
  if (!artifacts.length) {
    list.innerHTML = '<div class="empty">暂无产物</div>';
    return;
  }

  // 按 stage_code 分组，排除 write_result 和 match_expand
  const groups = {};
  artifacts.filter(a => a.stage_code !== 'write_result' && a.stage_code !== 'match_expand').forEach(a => {
    if (!groups[a.stage_code]) {
      groups[a.stage_code] = { stage_code: a.stage_code, stage_title: a.stage_title, items: [] };
    }
    groups[a.stage_code].items.push(a);
  });

  list.innerHTML = Object.values(groups).map(g => {
    const shortTitle = STAGE_TITLE_MAP[g.stage_code] || g.stage_title;
    const isExpanded = state.expandedGroups[g.stage_code] ?? (g.items.length === 1);
    return `
      <div class="artifact-group ${isExpanded ? 'expanded' : ''}" data-stage="${g.stage_code}">
        <div class="artifact-group-header" onclick="toggleArtifactGroup('${g.stage_code}')">
          <span class="artifact-group-toggle">▶</span>
          <span class="artifact-group-title">${shortTitle}</span>
          <span class="artifact-group-count">${g.items.length}</span>
        </div>
        <div class="artifact-group-list">
          ${g.items.map(a => `
            <div class="artifact-item">
              <div class="artifact-info">
                <div class="fname" title="${escapeHtml(a.filename)}">${escapeHtml(a.filename)}</div>
                <div class="fmeta">${formatSize(a.size)} · ${fmtTime(a.created_at)}</div>
              </div>
              <div class="artifact-actions">
                <a class="artifact-btn" href="/api/artifacts/${a.id}/download" target="_blank">下载</a>
                <span class="artifact-btn delete" onclick="deleteArtifact(${a.id}, '${escapeHtml(a.filename)}')" title="删除">删除</span>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }).join('');
}

window.toggleArtifactGroup = (stage) => {
  const el = document.querySelector(`.artifact-group[data-stage="${stage}"]`);
  if (!el) return;
  const nowExpanded = !el.classList.contains('expanded');
  el.classList.toggle('expanded');
  state.expandedGroups[stage] = nowExpanded;
};

window.deleteArtifact = async (id, name) => {
  if (!confirm(`确认删除文件「${name}」？此操作不可恢复。`)) return;
  try {
    state.currentRun = await fetchJSON(`/api/artifacts/${id}`, { method: 'DELETE' });
    renderCurrent();
  } catch (e) { alert('删除失败：' + e.message); }
};

function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function formatSize(n){ if(!n) return '-'; if(n<1024) return n+' B'; if(n<1024*1024) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(2)+' MB'; }

window.mark = async (code, st)=>{
  try{
    const qs = new URLSearchParams({status:st, message: `手动覆盖: ${st}`}).toString();
    state.currentRun = await fetchJSON(`/api/runs/${state.currentRun.run.id}/stages/${code}/mark?${qs}`, {method:'POST'});
    renderCurrent();
  }catch(e){ alert('操作失败：'+e.message); }
};

document.getElementById('btn-start').addEventListener('click', async ()=>{
  toggleGear();
  try{ state.currentRun = await fetchJSON('/api/runs/start', {method:'POST'}); renderCurrent(); await loadRunsAndMetrics(); }
  catch(e){ alert('初始化失败：'+e.message); }
});
document.getElementById('btn-refresh').addEventListener('click', async ()=>{
  try { state.currentRun = await fetchJSON(`/api/runs/${state.currentRun.run.id}`); }
  catch(e) { state.currentRun = await fetchJSON('/api/runs/current').catch(()=>null); }
  renderCurrent(); await loadRunsAndMetrics();
});

document.getElementById('upload-input').addEventListener('change', async (e)=>{
  const file = e.target.files[0]; if(!file) return; e.target.value=''; toggleGear();
  const runId = state.currentRun?.run?.id; if(!runId) return alert('没有进行中的任务');
  const stage = document.getElementById('upload-stage').value;
  const fd = new FormData(); fd.append('file', file);
  try{ state.currentRun = await fetchJSON(`/api/runs/${runId}/artifacts/upload?stage_code=${encodeURIComponent(stage)}`, {method:'POST', body:fd}); renderCurrent(); }
  catch(err){ alert('上传失败：'+err.message); }
});

document.getElementById('metric-input').addEventListener('change', async (e)=>{
  const file = e.target.files[0]; if(!file) return; e.target.value=''; toggleGear();
  const runId = state.currentRun?.run?.id; if(!runId) return alert('没有进行中的任务');
  const fd = new FormData(); fd.append('file', file);
  try{
    state.currentRun = await fetchJSON(`/api/runs/${runId}/calc_metric`, {method:'POST', body:fd});
    renderCurrent(); await loadRunsAndMetrics();
  } catch(err){ alert('评估失败：'+err.message); }
});

(async ()=>{
  await loadMeta();
  state.currentRun = await fetchJSON('/api/runs/current').catch(()=>null);
  renderCurrent();
  await loadRunsAndMetrics();
})();
</script>
</body>
</html>

"""
INDEX_HTML = INDEX_HTML.replace("__ANNOTATION_URL__", ANNOTATION_PLATFORM_URL).replace(
    "__RESULT_URL__", RESULT_STORE_URL
).replace("__HI_GROUP__", HI_GROUP_NAME)

# ── REPORT_HTML AUTOGEN START ──
REPORT_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>资质造假评估视图</title>
<script src="https://cdn.staticfile.net/echarts/5.5.0/echarts.min.js"></script>
<style>
:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --border: #e2e8f0;
  --border-soft: #f1f5f9;
  --text-1: #1e293b;
  --text-2: #64748b;
  --text-3: #94a3b8;
  --blue: #3b82f6;
  --blue-soft: #eff6ff;
  --blue-light: #dbeafe;
  --green: #10b981;
  --green-soft: #ecfdf5;
  --red: #f43f5e;
  --red-soft: #fff1f2;
  --orange: #f59e0b;
  --orange-soft: #fffbeb;
  --purple: #8b5cf6;
  --purple-soft: #f5f3ff;
  --cyan: #06b6d4;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.06), 0 2px 4px -2px rgba(0,0,0,0.04);
  --shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.07), 0 4px 6px -4px rgba(0,0,0,0.04);
  --radius: 12px;
  --radius-sm: 8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Noto Sans SC", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text-1);
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

/* ── Header ── */
.header {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 32px;
  background: rgba(255,255,255,0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
}
.brand-logo {
  width: 28px; height: 28px; border-radius: 7px;
  background: var(--text-1);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 600; font-size: 13px;
  flex-shrink: 0;
}
.header h1 { font-size: 15px; font-weight: 600; color: var(--text-1); letter-spacing: -0.01em; }
.header .week {
  color: var(--text-3); font-size: 12px; font-variant-numeric: tabular-nums;
  padding-left: 14px; border-left: 1px solid var(--border);
  font-weight: 500;
}
.header .spacer { flex: 1; }
.header a {
  color: var(--text-2); font-size: 13px; text-decoration: none; font-weight: 500;
  padding: 5px 12px; border-radius: var(--radius-sm); transition: all .2s;
  border: 1px solid transparent;
}
.header a:hover { background: var(--border-soft); color: var(--text-1); border-color: var(--border); }

/* ─ Main ── */
.main { max-width: 1200px; margin: 0 auto; padding: 24px 32px 80px; }

/* ── KPI Cards ── */
.kpi-row {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
  margin-bottom: 24px;
}
.kpi-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px 28px;
  box-shadow: var(--shadow-sm);
  transition: all .25s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
  overflow: hidden;
}
.kpi-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: var(--blue); opacity: 0;
  transition: opacity .25s;
}
.kpi-card:nth-child(2)::before { background: var(--red); }
.kpi-card:nth-child(3)::before { background: var(--orange); }
.kpi-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
  border-color: var(--border);
}
.kpi-card:hover::before { opacity: 1; }
.kpi-label {
  font-size: 11px; color: var(--text-3); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 14px;
}
.kpi-value {
  font-family: "SF Mono", SFMono-Regular, ui-monospace, Menlo, Monaco, Consolas, monospace;
  font-size: 32px; font-weight: 700; color: var(--text-1);
  font-variant-numeric: tabular-nums; line-height: 1;
  letter-spacing: -1px;
}
.kpi-value.empty { color: var(--text-3); font-weight: 400; font-size: 18px; letter-spacing: 0; }
.kpi-delta {
  margin-top: 14px; font-size: 12px; font-weight: 600;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 10px; border-radius: 20px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.01em;
}
.kpi-delta.up { color: var(--red); background: var(--red-soft); }
.kpi-delta.down { color: var(--green); background: var(--green-soft); }
.kpi-delta.flat { color: var(--text-3); background: var(--border-soft); }
.kpi-delta.first { color: var(--text-3); background: var(--border-soft); font-weight: 500; }

/* ── Section Cards ── */
.section {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 24px 28px;
  margin-bottom: 20px;
  box-shadow: var(--shadow-sm);
}
.section-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 20px;
}
.section-title {
  font-size: 14px; font-weight: 600; color: var(--text-1);
  letter-spacing: -0.01em;
}
.section-sub { font-size: 11px; color: var(--text-3); font-weight: 400; margin-top: 2px; }
.two-col {
  display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
}
@media (max-width: 960px) { .two-col { grid-template-columns: 1fr; } }

/* ── Chart wrappers ── */
.chart {
  width: 100%; height: 320px;
}
.chart.small { height: 280px; }
.chart.tall { height: 400px; }
.chart-empty {
  height: 320px; display: flex; align-items: center; justify-content: center;
  color: var(--text-3); font-size: 13px; background: var(--bg); border-radius: var(--radius-sm);
  flex-direction: column; gap: 6px;
}

/* ── Loading & error ── */
.loading, .error {
  padding: 80px 20px; text-align: center; color: var(--text-3); font-size: 14px;
}
.error { color: var(--red); }

/* ── Drilldown badge ── */
.drilldown-hint {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; color: var(--blue); font-weight: 500;
  padding: 2px 8px; background: var(--blue-soft); border-radius: 12px;
  margin-left: 8px;
}

/* ── L2 drawer ── */
.drawer-mask {
  position: fixed; inset: 0; background: rgba(15,23,42,0.35);
  backdrop-filter: blur(4px);
  z-index: 300; display: none;
}
.drawer-mask.open { display: block; }
.drawer {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: min(600px, 100vw);
  background: #fff; z-index: 301;
  box-shadow: var(--shadow-lg);
  transform: translateX(100%); transition: transform .3s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex; flex-direction: column;
}
.drawer.open { transform: translateX(0); }
.drawer-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 20px 24px; border-bottom: 1px solid var(--border);
}
.drawer-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.drawer-sub { font-size: 12px; color: var(--text-3); margin-top: 4px; }
.drawer-close {
  border: none; background: transparent; cursor: pointer;
  font-size: 18px; color: var(--text-3);
  width: 32px; height: 32px; border-radius: var(--radius-sm);
  display: flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.drawer-close:hover { background: var(--border-soft); color: var(--text-1); }
.drawer-body { flex: 1; padding: 20px 24px; overflow-y: auto; }

/* ── Empty overlay ── */
.data-source {
  font-size: 11px; color: var(--text-3);
  text-align: right; padding: 4px 0;
  font-variant-numeric: tabular-nums;
}

/* ── Sub-chart labels ── */
.sub-chart-label {
  font-size: 11px; color: var(--text-3); font-weight: 500;
  margin-bottom: 10px; letter-spacing: 0.02em;
}

/* ─ Mini cards in registered compare ── */
.mini-card {
  flex: 1; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 14px 16px;
}
.mini-card-label { font-size: 11px; color: var(--text-3); margin-bottom: 6px; font-weight: 500; }
.mini-card-value {
  font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums;
  color: var(--text-1); letter-spacing: -0.5px;
}
</style>
</head>
<body>

<header class="header">
  <div class="brand-logo">Q</div>
  <h1>资质造假评估视图</h1>
  <span class="week" id="week-label">加载中…</span>
  <span class="spacer"></span>
  <a href="/" target="_blank" rel="noopener">← 返回看板</a>
</header>

<main class="main">
  <div id="content"><div class="loading">数据加载中…</div></div>
</main>

<!-- L2 Drilldown Drawer -->
<div class="drawer-mask" id="drawer-mask" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <div class="drawer-head">
    <div>
      <div class="drawer-title" id="drawer-title">二级行业分布</div>
      <div class="drawer-sub" id="drawer-sub"></div>
    </div>
    <button class="drawer-close" onclick="closeDrawer()">✕</button>
  </div>
  <div class="drawer-body">
    <div id="drawer-content"></div>
    <div id="drawer-chart" class="chart tall"></div>
  </div>
</div>

<script>
const RUN_ID = __RUN_ID__;
const state = { detail: null, history: null, charts: {}, drawerChart: null };

// ── 统一配色 ──
const COLORS = {
  blue:    '#3b82f6',
  rose:    '#f43f5e',
  amber:   '#f59e0b',
  emerald: '#10b981',
  violet:  '#8b5cf6',
  cyan:    '#06b6d4',
  orange:  '#f97316',
  indigo:  '#6366f1',
  slate:   '#94a3b8',
  slate2:  '#cbd5e1',
};
const PIE_PALETTE = [
  '#3b82f6', '#f43f5e', '#f59e0b', '#10b981',
  '#8b5cf6', '#06b6d4', '#f97316', '#6366f1',
  '#ec4899', '#14b8a6', '#84cc16', '#94a3b8',
];

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

function pct(a, b) { return b ? (a / b * 100) : 0; }

// ── 主流程 ─────────────
(async function main() {
  try {
    const [detail, history] = await Promise.all([
      fetchJSON(`/api/runs/${RUN_ID}`),
      fetchJSON('/api/metrics_history?limit=20').catch(() => ({ weeks: [] })),
    ]);
    state.detail = detail;
    state.history = history;
    render();
  } catch (e) {
    document.getElementById('content').innerHTML = `<div class="error">加载失败：${e.message}</div>`;
  }
})();

function render() {
  const d = state.detail;
  const run = d.run;
  document.getElementById('week-label').textContent = `${run.week_start} ~ ${run.week_end}`;

  const html = `
    ${renderKPISection()}
    ${renderTrendSection()}
    <div class="two-col">
      ${renderL1PieSection()}
      ${renderL2BarSection()}
    </div>
    ${renderConcentrationCompareSection()}
    ${renderRegisteredCompareSection()}
    ${renderViolationSourceSection()}
    ${renderSuspectedSection()}
    ${renderConfidenceSection()}
  `;
  document.getElementById('content').innerHTML = html;

  setTimeout(() => {
    renderTrendChart();
    renderConcentrationCompareChart();
    renderL1PieChart();
    renderL2BarChart();
    renderRegisteredCompareChart();
    renderViolationSourceChart();
    renderSuspectedChart();
    renderConfidenceChart();
  }, 30);
}

// ── KPI 卡片 ─────────────
function renderKPISection() {
  const metrics = {};
  (state.detail.metrics || []).forEach(m => {
    const k = m.sample_group.replace('行业样本', '').replace('样本', '').trim();
    metrics[k] = m.concentration;
  });

  const cur = {
    overall: metrics['整体'] ?? metrics['全行业'] ?? metrics['大盘'] ?? null,
    realestate: metrics['房地产'] ?? null,
    finance: metrics['金融'] ?? null,
  };

  const weeks = state.history.weeks || [];
  const curIdx = weeks.findIndex(w => w.run_id === state.detail.run.id);
  const prev = curIdx > 0 ? weeks[curIdx - 1] : (curIdx === -1 && weeks.length ? weeks[weeks.length - 1] : null);

  return `
    <div class="kpi-row">
      ${renderKPI('大盘整体', cur.overall, prev?.overall)}
      ${renderKPI('房地产', cur.realestate, prev?.realestate)}
      ${renderKPI('金融', cur.finance, prev?.finance)}
    </div>
  `;
}

function renderKPI(label, cur, prev) {
  const valueHtml = cur === null || cur === undefined
    ? `<div class="kpi-value empty">暂无数据</div>`
    : `<div class="kpi-value">${(cur * 100).toFixed(2)}%</div>`;

  let deltaHtml = '';
  if (cur !== null && cur !== undefined) {
    if (prev === null || prev === undefined) {
      deltaHtml = `<div class="kpi-delta first">首次数据</div>`;
    } else {
      const diff = (cur - prev) * 100;
      if (Math.abs(diff) < 0.005) {
        deltaHtml = `<div class="kpi-delta flat">— 持平</div>`;
      } else if (diff > 0) {
        deltaHtml = `<div class="kpi-delta up">↑ ${diff.toFixed(2)}pp</div>`;
      } else {
        deltaHtml = `<div class="kpi-delta down">↓ ${Math.abs(diff).toFixed(2)}pp</div>`;
      }
    }
  }

  return `
    <div class="kpi-card">
      <div class="kpi-label">${label}违规浓度</div>
      ${valueHtml}
      ${deltaHtml}
    </div>
  `;
}

// ── 各行业浓度环比 ─────────────
function renderConcentrationCompareSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">各行业违规浓度 · 环比上周</div>
          <div class="section-sub" id="concentration-compare-sub">处理中…</div>
        </div>
      </div>
      <div class="chart tall" id="chart-concentration-compare"></div>
    </div>
  `;
}

function renderConcentrationCompareChart() {
  const el = document.getElementById('chart-concentration-compare');
  if (!el) return;

  const cmpTitleEl = document.getElementById('concentration-compare-sub');
  const curSample = {};
  (state.detail.sample_distribution || []).forEach(s => { curSample[s.industry] = s.count; });
  const curViol = {};
  (state.detail.violation_distribution || []).forEach(v => { curViol[v.industry] = v.violated_count; });

  const weeks = state.history.weeks || [];
  const curIdx = weeks.findIndex(w => w.run_id === state.detail.run.id);
  const prevWeek = curIdx > 0 ? weeks[curIdx - 1]
                              : (curIdx === -1 && weeks.length ? weeks[weeks.length - 1] : null);

  if (!prevWeek) {
    el.innerHTML = '<div class="chart-empty">未找到上周数据，无法计算环比</div>';
    cmpTitleEl.textContent = '无上周参照';
    return;
  }

  fetch(`/api/runs/${prevWeek.run_id}`).then(r => r.json()).then(prev => {
    const prevSample = {};
    (prev.sample_distribution || []).forEach(s => { prevSample[s.industry] = s.count; });
    const prevViol = {};
    (prev.violation_distribution || []).forEach(v => { prevViol[v.industry] = v.violated_count; });

    const prevLabel = prevWeek.is_baseline && prevWeek.baseline_label
      ? prevWeek.baseline_label
      : `${(prevWeek.week_start||'').replace(/-/g,'').slice(4)}-${(prevWeek.week_end||'').replace(/-/g,'').slice(4)}`;
    cmpTitleEl.textContent = `本周 vs 上周 ${prevLabel} · 按变化幅度降序`;

    const allIndustries = new Set([...Object.keys(curSample), ...Object.keys(prevSample)]);
    const items = [...allIndustries]
      .map(ind => {
        const cs = curSample[ind] || 0;
        const cv = curViol[ind] || 0;
        const ps = prevSample[ind] || 0;
        const pv = prevViol[ind] || 0;
        const curConc = cs ? cv / cs * 100 : null;
        const prevConc = ps ? pv / ps * 100 : null;
        return {
          industry: ind,
          curConc, prevConc,
          cs, cv, ps, pv,
          delta: (curConc !== null && prevConc !== null) ? (curConc - prevConc) : null,
        };
      })
      .filter(i => i.curConc !== null && i.prevConc !== null && Math.abs(i.delta) >= 0.01)
      .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));

    if (!items.length) {
      el.innerHTML = '<div class="chart-empty">本周与上周无共同行业，无法环比</div>';
      return;
    }

    el.style.height = Math.max(300, items.length * 34 + 60) + 'px';

    const chart = echarts.init(el);
    chart.setOption({
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
        backgroundColor: '#fff',
        borderColor: '#e2e8f0',
        borderWidth: 1,
        padding: [10, 14],
        textStyle: { color: '#1e293b', fontSize: 12 },
        formatter: params => {
          const it = items[params[0].dataIndex];
          const sign = it.delta > 0 ? '+' : (it.delta < 0 ? '' : '±');
          const deltaColor = it.delta > 0 ? '#f43f5e' : (it.delta < 0 ? '#10b981' : '#94a3b8');
          return `${it.industry}<br/>` +
            `<span style="color:#64748b;">本周</span> <b>${it.curConc.toFixed(2)}%</b> <span style="color:#94a3b8;">(${it.cv}/${it.cs})</span><br/>` +
            `<span style="color:#64748b;">上周</span> ${it.prevConc.toFixed(2)}% <span style="color:#94a3b8;">(${it.pv}/${it.ps})</span><br/>` +
            `<b style="color:${deltaColor};">环比 ${sign}${it.delta.toFixed(2)}pp</b>`;
        },
      },
      grid: { top: 16, right: 140, bottom: 24, left: 96 },
      xAxis: {
        type: 'value',
        axisLabel: { formatter: '{value}pp', color: '#94a3b8', fontSize: 11 },
        splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
        axisLine: { show: false },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category', inverse: true,
        data: items.map(i => i.industry),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#475569', fontSize: 12 },
      },
      series: [{
        type: 'bar',
        data: items.map(i => ({
          value: +i.delta.toFixed(2),
          itemStyle: {
            color: i.delta > 0.01 ? '#fda4af' : (i.delta < -0.01 ? '#6ee7b7' : '#cbd5e1'),
            borderRadius: 3,
          },
        })),
        label: {
          show: true, position: 'right', color: '#64748b', fontSize: 11,
          formatter: p => {
            const it = items[p.dataIndex];
            const sign = it.delta > 0 ? '+' : '';
            return `${sign}${it.delta.toFixed(2)}pp`;
          },
        },
        barMaxWidth: 14,
      }],
    });

    window.addEventListener('resize', () => chart.resize());
    state.charts.concentrationCompare = chart;
  }).catch(e => {
    el.innerHTML = `<div class="chart-empty">拉取上周数据失败：${e.message}</div>`;
  });
}

// ─ 浓度趋势折线图 ─────────────
function renderTrendSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div class="section-title">违规浓度趋势</div>
        <div class="section-sub">近 ${(state.history.weeks || []).length} 周</div>
      </div>
      <div class="chart" id="chart-trend"></div>
    </div>
  `;
}

function renderTrendChart() {
  // 趋势图去掉 baseline 点（2026-08-20 确认），从首个非-baseline 周开始
  const weeks = (state.history.weeks || []).filter(w => !w.is_baseline);
  const el = document.getElementById('chart-trend');
  if (!el || !weeks.length) return;

  const chart = echarts.init(el);
  const xData = weeks.map(w => {
    if (w.is_baseline && w.baseline_label) return w.baseline_label;
    const start = (w.week_start || '').replace(/-/g, '').slice(4);
    const end   = (w.week_end   || '').replace(/-/g, '').slice(4);
    return start && end ? `${start}-${end}` : (w.week_start || '').slice(5);
  });
  // 逐点选择标签位置：同一 x 上三条线的值排序，最高放 top，最低放 bottom，中间也放 top 但 distance 拉大
  const seriesKeys = [
    { name: '大盘整体', key: 'overall',    color: '#3b82f6' },
    { name: '房地产',     key: 'realestate', color: '#f43f5e' },
    { name: '金融',         key: 'finance',    color: '#f59e0b' },
  ];
  // 先建一个矩阵：rows[x][seriesIdx] = value；然后对每一列排序，得到每个点在同 x 上的位次
  const rows = weeks.map(w => seriesKeys.map(s => w[s.key] === null || w[s.key] === undefined ? null : +(w[s.key] * 100).toFixed(2)));
  // labelPos[seriesIdx][xIdx] = { position, distance }
  const labelPos = seriesKeys.map(() => []);
  rows.forEach((rowVals, xIdx) => {
    const indexed = rowVals.map((v, i) => ({ v, i })).filter(o => o.v !== null);
    if (!indexed.length) return;
    // 按 v 从高到低排序
    indexed.sort((a, b) => b.v - a.v);
    indexed.forEach((o, rank) => {
      let position = 'top', distance = 8;
      if (indexed.length === 1) { position = 'top'; distance = 8; }
      else if (rank === 0) { position = 'top'; distance = 10; }
      else if (rank === indexed.length - 1) {
        // 最低位：默认 bottom；但贴着 0 轴的点（≤1%）下方没空间，翻到 top
        position = o.v <= 1 ? 'top' : 'bottom';
        distance = 6;
      }
      else {
        // 中间位：放到离相邻点更远的那一侧（避开挨得近的邻线）
        const gapAbove = indexed[rank - 1].v - o.v;
        const gapBelow = o.v - indexed[rank + 1].v;
        if (gapAbove >= gapBelow) { position = 'top'; distance = 8; }
        else { position = 'bottom'; distance = 8; }
      }
      labelPos[o.i][xIdx] = { position, distance };
    });
  });

  const buildSeries = (seriesIdx, name, key, color) => ({
    name, type: 'line', smooth: true,
    symbol: 'circle', symbolSize: 6,
    showSymbol: true,
    emphasis: { symbolSize: 9 },
    data: weeks.map((w, xIdx) => {
      const v = w[key] === null || w[key] === undefined ? null : +(w[key] * 100).toFixed(2);
      if (v === null) return null;
      const lp = labelPos[seriesIdx][xIdx] || { position: 'top', distance: 6 };
      return {
        value: v,
        label: {
          show: true,
          position: lp.position,
          distance: lp.distance,
          color,
          fontSize: 11,
          fontWeight: 600,
          formatter: `${v}%`,
        },
      };
    }),
    itemStyle: { color, borderWidth: 2, borderColor: '#fff' },
    lineStyle: { width: 2, cap: 'round' },
    connectNulls: true,
  });

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#fff',
      borderColor: '#e2e8f0',
      borderWidth: 1,
      padding: [10, 14],
      textStyle: { color: '#1e293b', fontSize: 12 },
      valueFormatter: v => v === null ? '—' : `${v}%`,
    },
    legend: {
      top: 0, right: 0,
      textStyle: { fontSize: 12, color: '#64748b' },
      itemWidth: 16, itemHeight: 3, itemGap: 20,
    },
    grid: { top: 56, right: 20, bottom: 58, left: 42 },
    xAxis: {
      type: 'category', data: xData,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#94a3b8', fontSize: 11, margin: 18, interval: 0 },
    },
    yAxis: {
      type: 'value', min: 0, max: 100,
      axisLabel: { formatter: '{value}%', color: '#94a3b8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9' } },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: seriesKeys.map((s, i) => buildSeries(i, s.name, s.key, s.color)),
  });

  window.addEventListener('resize', () => chart.resize());
  state.charts.trend = chart;
}

// ── 大盘一级行业违规占比 ─────────────
function renderL1PieSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">一级行业违规构成
            <span class="drilldown-hint">点击下钻</span>
          </div>
        </div>
      </div>
      <div class="chart" id="chart-l1-pie"></div>
    </div>
  `;
}

function renderL1PieChart() {
  const el = document.getElementById('chart-l1-pie');
  if (!el) return;

  const items = (state.detail.violation_distribution || [])
    .filter(v => v.violated_count > 0)
    .map(v => ({ name: v.industry, value: v.violated_count }));

  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">暂无违规行业分布数据</div>';
    return;
  }

  const chart = echarts.init(el);
  chart.setOption({
    tooltip: {
      trigger: 'item',
      backgroundColor: '#fff',
      borderColor: '#e2e8f0',
      borderWidth: 1,
      padding: [10, 14],
      textStyle: { color: '#1e293b', fontSize: 12 },
      formatter: p => `${p.name}<br/><span style="color:#64748b;">违规账户</span> ${p.value} · <b>${p.percent}%</b>`,
    },
    legend: {
      type: 'scroll', bottom: 0,
      textStyle: { fontSize: 11, color: '#64748b' },
      itemWidth: 10, itemHeight: 10, itemGap: 14,
    },
    series: [{
      type: 'pie',
      radius: ['50%', '72%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      padAngle: 2,
      itemStyle: { borderRadius: 5, borderColor: '#fff', borderWidth: 2 },
      label: {
        show: true,
        formatter: p => p.percent >= 5 ? `{name|${p.name}}\n{val|${p.percent}%}` : '',
        rich: {
          name: { fontSize: 11, color: '#475569', lineHeight: 18 },
          val:  { fontSize: 10, color: '#94a3b8', fontWeight: 600 },
        },
      },
      labelLine: { length: 10, length2: 8, lineStyle: { color: '#e2e8f0' } },
      emphasis: {
        scaleSize: 4,
        itemStyle: { shadowBlur: 12, shadowColor: 'rgba(0,0,0,0.1)' },
      },
      data: items.map((d, i) => ({ ...d, itemStyle: { color: PIE_PALETTE[i % PIE_PALETTE.length] } })),
    }],
  });

  chart.on('click', p => openL2Drawer(p.name));
  window.addEventListener('resize', () => chart.resize());
  state.charts.l1pie = chart;
}

// ── 房地产二级行业违规占比 ────────────
function renderL2BarSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div class="section-title">房地产二级行业违规分布</div>
      </div>
      <div class="chart" id="chart-realestate-l2"></div>
    </div>
  `;
}

function renderL2BarChart() {
  const el = document.getElementById('chart-realestate-l2');
  if (!el) return;

  const items = (state.detail.violation_distribution_l2 || [])
    .filter(v => v.first_industry === '房地产' && v.violated_count > 0)
    .sort((a, b) => b.violated_count - a.violated_count);

  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">暂无房地产二级行业数据<br/><span style="font-size:11px;color:#94a3b8;">等待数据推送</span></div>';
    return;
  }

  const maxVal = Math.max(...items.map(i => i.violated_count));

  const chart = echarts.init(el);
  chart.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
      backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
      padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
    },
    grid: { top: 16, right: 48, bottom: 20, left: 90 },
    xAxis: {
      type: 'value',
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#94a3b8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
    },
    yAxis: {
      type: 'category', inverse: true,
      data: items.map(i => i.second_industry),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#475569', fontSize: 12 },
    },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: i.violated_count,
        itemStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
            colorStops: [
              { offset: 0, color: '#3b82f6' },
              { offset: 1, color: '#60a5fa' },
            ],
          },
          borderRadius: [0, 3, 3, 0],
        },
      })),
      label: {
        show: true, position: 'right', color: '#94a3b8', fontSize: 11,
        formatter: '{c}',
      },
      barMaxWidth: 14,
    }],
  });

  window.addEventListener('resize', () => chart.resize());
  state.charts.realestateL2 = chart;
}

// ── 二级行业下钻抽屉 ─────────────
function openL2Drawer(firstIndustry) {
  document.getElementById('drawer-content').style.display = 'none';
  document.getElementById('drawer-content').innerHTML = '';
  document.getElementById('drawer-chart').style.display = 'block';
  const items = (state.detail.violation_distribution_l2 || [])
    .filter(v => v.first_industry === firstIndustry && v.violated_count > 0)
    .sort((a, b) => b.violated_count - a.violated_count);

  document.getElementById('drawer-title').textContent = `${firstIndustry} · 二级行业违规分布`;
  document.getElementById('drawer-sub').textContent = items.length
    ? `共 ${items.length} 个二级行业`
    : '暂无二级行业数据';

  document.getElementById('drawer-mask').classList.add('open');
  document.getElementById('drawer').classList.add('open');

  setTimeout(() => {
    const el = document.getElementById('drawer-chart');
    if (!items.length) {
      el.innerHTML = '<div class="chart-empty">暂无二级行业数据</div>';
      return;
    }
    if (state.drawerChart) { state.drawerChart.dispose(); state.drawerChart = null; }
    state.drawerChart = echarts.init(el);
    state.drawerChart.setOption({
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
        backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
        padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
      },
      grid: { top: 16, right: 48, bottom: 20, left: 100 },
      xAxis: {
        type: 'value',
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: '#94a3b8', fontSize: 11 },
        splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
      },
      yAxis: {
        type: 'category', inverse: true,
        data: items.map(i => i.second_industry),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: '#475569', fontSize: 12 },
      },
      series: [{
        type: 'bar',
        data: items.map(i => ({
          value: i.violated_count,
          itemStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
              colorStops: [
                { offset: 0, color: '#3b82f6' },
                { offset: 1, color: '#60a5fa' },
              ],
            },
            borderRadius: [0, 3, 3, 0],
          },
        })),
        label: { show: true, position: 'right', color: '#94a3b8', fontSize: 11, formatter: '{c}' },
        barMaxWidth: 14,
      }],
    });
  }, 50);
}

window.closeDrawer = function() {
  document.getElementById('drawer-mask').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
};

// ── 认证方式分布块（行业下钻抽屉内，有数据才渲染） ─────
function renderCertMethodBlock(industry) {
  const cert = (state.detail.cert_method || []).filter(c => c.industry === industry);
  if (!cert.length) return '';

  const METHOD_COLORS = ['#6366f1', '#06b6d4', '#8b5cf6', '#f97316'];
  const regTotal = cert.reduce((s, c) => s + (c.registered_count || 0), 0);
  const vioTotal = cert.reduce((s, c) => s + (c.violated_count || 0), 0);

  const segBar = items => {
    const segs = items.filter(s => s.v > 0);
    if (!segs.length) return '<div style="height:12px;"></div>';
    return `<div style="display:flex;height:12px;border-radius:6px;overflow:hidden;background:#f1f5f9;">${segs.map(s => `<div title="${s.name} ${s.v.toLocaleString()} (${s.pct.toFixed(1)}%)" style="background:${s.color};width:${s.pct}%;"></div>`).join('')}</div>`;
  };

  const regSegs = cert.map((c, i) => ({ name: c.method, v: c.registered_count || 0, color: METHOD_COLORS[i % METHOD_COLORS.length], pct: regTotal ? (c.registered_count || 0) / regTotal * 100 : 0 }));
  const vioSegs = cert.map((c, i) => ({ name: c.method, v: c.violated_count || 0, color: METHOD_COLORS[i % METHOD_COLORS.length], pct: vioTotal ? (c.violated_count || 0) / vioTotal * 100 : 0 }));

  const rowHtml = (c, i) => {
    const color = METHOD_COLORS[i % METHOD_COLORS.length];
    const rv = c.registered_count || 0;
    const vv = c.violated_count || 0;
    const pctR = regTotal ? rv / regTotal * 100 : 0;
    const pctV = vioTotal ? vv / vioTotal * 100 : 0;
    let liftHtml;
    if (!rv || !vioTotal) {
      liftHtml = '<span style="color:#cbd5e1;">—</span>';
    } else {
      const lift = pctV / pctR;
      const lc = lift >= 1.3 ? '#f43f5e' : (lift <= 0.7 ? '#10b981' : '#64748b');
      liftHtml = `<span style="font-weight:700;color:${lc};">×${lift.toFixed(2)}</span>`;
    }
    return `
      <tr>
        <td style="padding:10px 0;"><span style="display:inline-flex;align-items:center;gap:8px;"><span style="width:8px;height:8px;border-radius:2px;background:${color};"></span><span style="font-size:13px;color:#334155;">${c.method}</span></span></td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:600;">${rv.toLocaleString()}</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">${pctR.toFixed(1)}%</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#334155;">${vv.toLocaleString()}</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">${pctV.toFixed(1)}%</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;">${liftHtml}</td>
      </tr>
    `;
  };

  return `
    <div style="margin-top:28px;padding-top:20px;border-top:1px solid #f1f5f9;"></div>
    <div class="sub-chart-label">认证方式分布 · 全量入驻占比</div>
    ${segBar(regSegs)}
    <div style="display:flex;gap:16px;margin-top:8px;margin-bottom:16px;flex-wrap:wrap;">
      ${regSegs.filter(s=>s.v>0).map(s => `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#64748b;"><span style="width:8px;height:8px;border-radius:2px;background:${s.color};"></span>${s.name} <span style="color:#1e293b;font-weight:600;">${s.v.toLocaleString()}</span> <span style="color:#94a3b8;">(${s.pct.toFixed(1)}%)</span></span>`).join('')}
    </div>
    ${vioTotal ? `
    <div class="sub-chart-label">实锤造假 · 认证方式占比（标注口径，以数据推送为准）</div>
    ${segBar(vioSegs)}
    <div style="display:flex;gap:16px;margin-top:8px;margin-bottom:16px;flex-wrap:wrap;">
      ${vioSegs.filter(s=>s.v>0).map(s => `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#64748b;"><span style="width:8px;height:8px;border-radius:2px;background:${s.color};"></span>${s.name} <span style="color:#1e293b;font-weight:600;">${s.v.toLocaleString()}</span> <span style="color:#94a3b8;">(${s.pct.toFixed(1)}%)</span></span>`).join('')}
    </div>` : ''}
    <div class="sub-chart-label" style="margin-bottom:4px;">认证方式明细 · 占比偏移 = 造假占比 / 入驻占比</div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="border-bottom:1px solid #e2e8f0;">
          <th style="text-align:left;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">认证方式</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">全量入驻</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">入驻占比</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">实锤造假</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">造假占比</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">占比偏移</th>
        </tr>
      </thead>
      <tbody>
        ${cert.map((c, i) => rowHtml(c, i)).join('')}
        <tr style="border-top:1px solid #e2e8f0;">
          <td style="padding:10px 0;font-size:13px;font-weight:600;color:#1e293b;">合计</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:700;">${regTotal.toLocaleString()}</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">100%</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:700;">${vioTotal.toLocaleString()}</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">100%</td>
          <td style="padding:10px 0;"></td>
        </tr>
      </tbody>
    </table>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;line-height:1.6;">
      注：实锤造假来自抽样标注结果，仅比较两组构成占比的相对偏移。
    </div>
  `;
}

// ── 行业入驻/造假来源下钻抽屉（入驻量级图点击） ─────
function openIndustrySourceDrawer(industry) {
  const chartEl = document.getElementById('drawer-chart');
  const contentEl = document.getElementById('drawer-content');
  if (!contentEl) return;

  const rs = (state.detail.registered_source || []).find(r => r.industry === industry);
  const vs = (state.detail.violation_source || []).find(r => r.industry === industry);

  const reg = rs ? { p: rs.professional_count || 0, s: rs.shop_count || 0, a: rs.ad_count || 0 } : null;
  const vio = vs ? { p: vs.professional_count || 0, s: vs.shop_count || 0, a: vs.ad_count || 0 } : null;
  const regTotal = reg ? reg.p + reg.s + reg.a : 0;
  const vioTotal = vio ? vio.p + vio.s + vio.a : 0;

  document.getElementById('drawer-title').textContent = `${industry} · 入驻来源 vs 造假来源`;

  // 两个实锤口径分开标：抽样标注（浓度口径） / 标注池（含同簇扩展，violation_source 总数与 confirmed_fake_uid 一致）
  const vdRow = (state.detail.violation_distribution || []).find(v => v.industry === industry);
  const sampleRow = (state.detail.sample_distribution || []).find(s => s.industry === industry);
  const sampleCount = sampleRow ? sampleRow.count : null;
  const sampleViolated = vdRow ? vdRow.violated_count : null;

  let subText = '';
  if (regTotal) {
    subText = `入驻 ${regTotal.toLocaleString()}（全量）`;
    if (sampleCount != null && sampleViolated != null) {
      subText += ` · 抽样 ${sampleCount.toLocaleString()} 实锤 ${sampleViolated.toLocaleString()}`;
    }
    if (vioTotal) {
      subText += ` · 标注池实锤 ${vioTotal.toLocaleString()}（含同簇扩展）`;
    }
  } else {
    subText = '暂无入驻来源数据';
  }
  document.getElementById('drawer-sub').textContent = subText;

  document.getElementById('drawer-mask').classList.add('open');
  document.getElementById('drawer').classList.add('open');
  if (state.drawerChart) { state.drawerChart.dispose(); state.drawerChart = null; }
  chartEl.style.display = 'none';
  contentEl.style.display = 'block';

  const chans = [
    { name: '专业号入驻', color: '#3b82f6' },
    { name: '号店入驻',   color: '#f59e0b' },
    { name: '号广入驻',   color: '#10b981' },
  ];
  const keys = ['p', 's', 'a'];

  if (!reg) {
    contentEl.innerHTML = `<div class="chart-empty">暂无 ${industry} 的入驻来源数据<br/><span style="font-size:11px;color:#94a3b8;">需数据推送 registered_source</span></div>`;
    return;
  }

  const rowHtml = (name, color, rv, vv) => {
    const pctR = regTotal ? (rv / regTotal * 100) : 0;
    const pctV = vioTotal ? (vv / vioTotal * 100) : 0;
    // 占比偏移 = 造假来源占比 / 入驻占比，>1 表示该渠道造假占比高于其入驻占比（风险偏高）
    let liftHtml;
    if (!rv || !vioTotal) {
      liftHtml = '<span style="color:#cbd5e1;">—</span>';
    } else {
      const lift = pctV / pctR;
      const color = lift >= 1.3 ? '#f43f5e' : (lift <= 0.7 ? '#10b981' : '#64748b');
      liftHtml = `<span style="font-weight:700;color:${color};">×${lift.toFixed(2)}</span>`;
    }
    return `
      <tr>
        <td style="padding:10px 0;"><span style="display:inline-flex;align-items:center;gap:8px;"><span style="width:8px;height:8px;border-radius:2px;background:${color};"></span><span style="font-size:13px;color:#334155;">${name}</span></span></td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:600;">${rv.toLocaleString()}</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">${pctR.toFixed(1)}%</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#334155;">${vv.toLocaleString()}</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">${pctV.toFixed(1)}%</td>
        <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;">${liftHtml}</td>
      </tr>
    `;
  };

  // 占比条（入驻 vs 造假双条对照）
  const segBar = (vals, total) => {
    const segs = chans.map((c, i) => ({ ...c, v: vals[i], pct: total ? vals[i] / total * 100 : 0 })).filter(s => s.v > 0);
    if (!segs.length) return '<div style="height:12px;"></div>';
    return `<div style="display:flex;height:12px;border-radius:6px;overflow:hidden;background:#f1f5f9;">${segs.map(s => `<div title="${s.name} ${s.v.toLocaleString()} (${s.pct.toFixed(1)}%)" style="background:${s.color};width:${s.pct}%;"></div>`).join('')}</div>`;
  };

  contentEl.innerHTML = `
    <div class="sub-chart-label">入驻来源占比</div>
    ${segBar([reg.p, reg.s, reg.a], regTotal)}
    <div style="display:flex;gap:16px;margin-top:8px;margin-bottom:20px;flex-wrap:wrap;">
      ${chans.map((c, i) => { const v = [reg.p, reg.s, reg.a][i]; return `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#64748b;"><span style="width:8px;height:8px;border-radius:2px;background:${c.color};"></span>${c.name} <span style="color:#1e293b;font-weight:600;">${v.toLocaleString()}</span></span>`; }).join('')}
    </div>
    ${vioTotal ? `
    <div class="sub-chart-label">实锤造假来源占比 · 标注池口径（抽样+同簇扩展，非浓度口径）</div>
    ${segBar([vio.p, vio.s, vio.a], vioTotal)}
    <div style="display:flex;gap:16px;margin-top:8px;margin-bottom:20px;flex-wrap:wrap;">
      ${chans.map((c, i) => { const v = [vio.p, vio.s, vio.a][i]; const share = vioTotal ? v / vioTotal * 100 : 0; return `<span style="display:inline-flex;align-items:center;gap:6px;font-size:11px;color:#64748b;"><span style="width:8px;height:8px;border-radius:2px;background:${c.color};"></span>${c.name} <span style="color:#1e293b;font-weight:600;">${v.toLocaleString()}</span> <span style="color:#94a3b8;">(${share.toFixed(1)}%)</span></span>`; }).join('')}
    </div>` : ''}
    ${renderCertMethodBlock(industry)}
    <div class="sub-chart-label" style="margin-bottom:4px;">渠道明细 · 占比偏移 = 造假占比 / 入驻占比，×>1 表示该渠道造假占比高于其入驻占比</div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="border-bottom:1px solid #e2e8f0;">
          <th style="text-align:left;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">渠道</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">入驻</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">入驻占比</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">实锤造假</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">造假占比</th>
          <th style="text-align:right;font-size:11px;color:#94a3b8;font-weight:600;padding:6px 0;">占比偏移</th>
        </tr>
      </thead>
      <tbody>
        ${chans.map((c, i) => rowHtml(c.name, c.color, [reg.p, reg.s, reg.a][i], vio ? [vio.p, vio.s, vio.a][i] : 0)).join('')}
        <tr style="border-top:1px solid #e2e8f0;">
          <td style="padding:10px 0;font-size:13px;font-weight:600;color:#1e293b;">合计</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:700;">${regTotal.toLocaleString()}</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">100%</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#1e293b;font-weight:700;">${vioTotal.toLocaleString()}</td>
          <td style="padding:10px 0;text-align:right;font-variant-numeric:tabular-nums;color:#94a3b8;">100%</td>
          <td style="padding:10px 0;"></td>
        </tr>
      </tbody>
    </table>
    <div style="margin-top:12px;font-size:11px;color:#94a3b8;line-height:1.6;">
      注：渠道拆分基于标注池实锤结果（抽样+同簇扩展，与 industry_baseline 的 confirmed_fake_uid 同口径），因含同簇扩展账户，绝对量高于抽样实锤数；仅比较两组构成占比的相对偏移，不代表浓度。
    </div>
  `;
}

// ── 入驻量级对比 ─────────────
function renderRegisteredCompareSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">各行业入驻量级 · 本周 vs 上周</div>
          <div class="section-sub" id="registered-compare-sub">处理中…</div>
        </div>
      </div>
      <div id="registered-overall" style="display:flex;gap:12px;margin-bottom:12px;"></div>
      <div id="registered-source-row" style="margin-bottom:18px;"></div>
      <div class="chart tall" id="chart-registered-compare"></div>
    </div>
  `;
}

function renderRegisteredCompareChart() {
  const el = document.getElementById('chart-registered-compare');
  const overallEl = document.getElementById('registered-overall');
  const subEl = document.getElementById('registered-compare-sub');
  if (!el) return;

  // ── 入驻来源构成行（不依赖上周数据，先渲染） ──
  const srcRowEl = document.getElementById('registered-source-row');
  if (srcRowEl) {
    const rs = (state.detail.registered_source || []).find(r => r.industry === '大盘整体');
    const total = rs ? ((rs.professional_count || 0) + (rs.shop_count || 0) + (rs.ad_count || 0)) : 0;
    if (rs && total > 0) {
      const chans = [
        { name: '专业号入驻', v: rs.professional_count || 0, color: '#3b82f6' },
        { name: '号店入驻', v: rs.shop_count || 0, color: '#f59e0b' },
        { name: '号广入驻', v: rs.ad_count || 0, color: '#10b981' },
      ];
      srcRowEl.innerHTML = `
        <div class="sub-chart-label">入驻来源构成 · 总计 ${total.toLocaleString()}</div>
        <div style="display:flex;gap:12px;">
          ${chans.map(c => `
            <div style="flex:1;display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 14px;">
              <span style="width:8px;height:8px;border-radius:2px;background:${c.color};flex-shrink:0;"></span>
              <span style="font-size:12px;color:var(--text-2);">${c.name}</span>
              <span style="margin-left:auto;font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text-1);">${(c.v / total * 100).toFixed(1)}%</span>
              <span style="font-size:11px;color:var(--text-3);font-variant-numeric:tabular-nums;">${c.v.toLocaleString()}</span>
            </div>
          `).join('')}
        </div>
      `;
    } else {
      srcRowEl.innerHTML = `
        <div class="sub-chart-label">入驻来源构成</div>
        <div style="background:var(--bg);border:1px dashed var(--border);border-radius:var(--radius-sm);padding:10px 14px;font-size:12px;color:var(--text-3);">
          暂无数据（需数据推送 registered_source）
        </div>
      `;
    }
  }

  const curBaseline = {};
  (state.detail.industry_baseline || []).forEach(b => { curBaseline[b.industry] = b.registered_uid || 0; });

  const weeks = state.history.weeks || [];
  const curIdx = weeks.findIndex(w => w.run_id === state.detail.run.id);
  const prevWeek = curIdx > 0 ? weeks[curIdx - 1]
                              : (curIdx === -1 && weeks.length ? weeks[weeks.length - 1] : null);

  if (!prevWeek) {
    el.innerHTML = '<div class="chart-empty">未找到上周数据</div>';
    subEl.textContent = '无上周参照';
    return;
  }

  fetch(`/api/runs/${prevWeek.run_id}`).then(r => r.json()).then(prev => {
    const prevBaseline = {};
    (prev.industry_baseline || []).forEach(b => { prevBaseline[b.industry] = b.registered_uid || 0; });

    const prevLabel = prevWeek.is_baseline && prevWeek.baseline_label
      ? prevWeek.baseline_label
      : `${(prevWeek.week_start||'').replace(/-/g,'').slice(4)}-${(prevWeek.week_end||'').replace(/-/g,'').slice(4)}`;
    subEl.textContent = `按本周入驻量降序 · 点击行业查看来源下钻`;

    const allIndustries = new Set([...Object.keys(curBaseline), ...Object.keys(prevBaseline)]);
    const items = [...allIndustries]
      .map(ind => ({ industry: ind, cur: curBaseline[ind] || 0, prev: prevBaseline[ind] || 0 }))
      .filter(i => i.cur > 0 || i.prev > 0)
      .sort((a, b) => b.cur - a.cur);

    if (!items.length) {
      el.innerHTML = '<div class="chart-empty">暂无数据</div>';
      return;
    }

    const curTotal = items.reduce((s, i) => s + i.cur, 0);
    const prevTotal = items.reduce((s, i) => s + i.prev, 0);
    const diff = curTotal - prevTotal;
    const diffPct = prevTotal ? (diff / prevTotal * 100) : null;
    const sign = diff > 0 ? '+' : '';
    const trendColor = diff > 0 ? '#f43f5e' : (diff < 0 ? '#10b981' : '#94a3b8');
    const arrow = diff > 0 ? '↑' : (diff < 0 ? '↓' : '—');
    overallEl.innerHTML = `
      <div class="mini-card" style="border-left:3px solid #3b82f6;">
        <div class="mini-card-label">本周整体入驻</div>
        <div class="mini-card-value">${curTotal.toLocaleString()}</div>
      </div>
      <div class="mini-card">
        <div class="mini-card-label">上周整体入驻</div>
        <div class="mini-card-value" style="color:var(--text-2);font-weight:500;">${prevTotal.toLocaleString()}</div>
      </div>
      <div class="mini-card">
        <div class="mini-card-label">环比变化</div>
        <div class="mini-card-value" style="color:${trendColor};font-size:20px;">${arrow} ${sign}${diff.toLocaleString()}${diffPct !== null ? ` (${sign}${diffPct.toFixed(1)}%)` : ''}</div>
      </div>
    `;

    el.style.height = Math.max(340, items.length * 36 + 60) + 'px';

    // 以本周为主的堆叠：
    //   上升行 (cur > prev): base=prev, extra=cur-prev(绿)，总长=cur
    //   下降行 (cur < prev): base=cur(蓝), extra=0，总长=cur；额外用 markPoint 在 prev 位置标一条浅红短线 + “-XXX”标签提示上周基线
    const baseData = items.map(i => Math.min(i.cur, i.prev));
    const upData = items.map(i => Math.max(0, i.cur - i.prev));

    const prevMarkPoints = items.map((i, idx) => {
      if (i.prev <= i.cur) return null;
      return {
        coord: [i.prev, idx],
        symbol: 'rect',
        symbolSize: [2, 14],
        itemStyle: { color: '#fda4af' },
        label: {
          show: true, position: 'right', distance: 4,
          color: '#f43f5e', fontSize: 10, fontWeight: 600,
          formatter: `-${(i.prev - i.cur).toLocaleString()}`,
        },
      };
    }).filter(Boolean);

    const chart = echarts.init(el);
    chart.setOption({
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
        backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
        padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
        formatter: params => {
          const it = items[params[0].dataIndex];
          const d = it.cur - it.prev;
          const sign = d > 0 ? '+' : '';
          const changePct = it.prev ? (d / it.prev * 100).toFixed(1) : '—';
          const color = d > 0 ? '#10b981' : (d < 0 ? '#f43f5e' : '#94a3b8');
          return `${it.industry}<br/>` +
            `<span style="color:#64748b;">本周</span> <b>${it.cur.toLocaleString()}</b><br/>` +
            `<span style="color:#64748b;">上周</span> ${it.prev.toLocaleString()}<br/>` +
            `<b style="color:${color};">${sign}${d.toLocaleString()}（${sign}${changePct}%）</b>`;
        },
      },
      legend: {
        top: 0, right: 0,
        textStyle: { fontSize: 11, color: '#64748b' },
        itemWidth: 12, itemHeight: 10, itemGap: 16,
        data: ['上周基线', '本周新增'],
      },
      grid: { top: 36, right: 100, bottom: 24, left: 96 },
      xAxis: {
        type: 'value',
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: '#94a3b8', fontSize: 11 },
        splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
      },
      yAxis: {
        type: 'category', inverse: true,
        data: items.map(i => i.industry),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: '#475569', fontSize: 12 },
      },
      series: [
        {
          name: '上周基线', type: 'bar', stack: 'waterfall',
          data: baseData,
          itemStyle: { color: '#e2e8f0' },
          barMaxWidth: 14,
          label: {
            show: true, position: 'insideRight', color: '#94a3b8', fontSize: 10,
            formatter: ({ dataIndex }) => {
              const it = items[dataIndex];
              // 下降行时内部不显示（避免与右侧 -XXX 重复）；上升行内部显示上周基线值
              return it.cur > it.prev ? it.prev.toLocaleString() : '';
            },
          },
        },
        {
          name: '本周新增', type: 'bar', stack: 'waterfall',
          data: upData,
          itemStyle: { color: '#6ee7b7', borderRadius: [0, 3, 3, 0] },
          barMaxWidth: 14,
          label: {
            show: true, position: 'right', color: '#475569', fontSize: 11, fontWeight: 600,
            formatter: ({ dataIndex }) => {
              const it = items[dataIndex];
              const d = it.cur - it.prev;
              if (d > 0) return `{cur|${it.cur.toLocaleString()}} {up|+${d.toLocaleString()}}`;
              // 下降行右侧显示本周值 + -XXX
              if (d < 0) return `{cur|${it.cur.toLocaleString()}} {down|${d.toLocaleString()}}`;
              return `{cur|${it.cur.toLocaleString()}}`;
            },
            rich: {
              cur:  { color: '#475569', fontSize: 11, fontWeight: 600 },
              up:   { color: '#10b981', fontSize: 10, fontWeight: 600, padding: [0, 0, 0, 4] },
              down: { color: '#f43f5e', fontSize: 10, fontWeight: 600, padding: [0, 0, 0, 4] },
            },
          },
        },
      ],
    });

    window.addEventListener('resize', () => chart.resize());
    chart.on('click', params => {
      if (params.componentType === 'series' && params.name) openIndustrySourceDrawer(params.name);
    });
    state.charts.registeredCompare = chart;
  }).catch(e => {
    el.innerHTML = `<div class="chart-empty">拉取上周数据失败：${e.message}</div>`;
  });
}

// ── 实锤造假来源比例 ─────────────
function renderViolationSourceSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">造假来源渠道</div>
          <div class="section-sub">大盘整体各渠道占比 · 各行业渠道分布</div>
        </div>
      </div>
      <div id="source-overall-cards" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px;"></div>
      <div id="source-overall-stacked" style="margin-bottom:22px;"></div>
      <div class="sub-chart-label">各行业渠道分布（按总量降序）</div>
      <div class="chart" id="chart-violation-source-bar"></div>
    </div>
  `;
}

function renderViolationSourceChart() {
  const cardsEl = document.getElementById('source-overall-cards');
  const stackedEl = document.getElementById('source-overall-stacked');
  const barEl = document.getElementById('chart-violation-source-bar');
  if (!cardsEl || !barEl) return;

  const rawItems = (state.detail.violation_source || [])
    .map(v => ({
      industry: v.industry,
      professional: v.professional_count || 0,
      shop: v.shop_count || 0,
      ad: v.ad_count || 0,
      total: (v.professional_count || 0) + (v.shop_count || 0) + (v.ad_count || 0),
    }))
    .filter(i => i.total > 0);

  if (!rawItems.length) {
    cardsEl.innerHTML = '<div class="chart-empty" style="grid-column:1/-1;height:120px;">暂无数据<br/><span style="font-size:11px;color:#94a3b8;">需数据推送 violation_source</span></div>';
    stackedEl.innerHTML = '';
    barEl.innerHTML = '<div class="chart-empty">暂无数据</div>';
    return;
  }

  const overall = rawItems.find(i => i.industry === '大盘整体')
    || rawItems.reduce((acc, i) => {
      acc.professional += i.professional;
      acc.shop += i.shop;
      acc.ad += i.ad;
      acc.total += i.total;
      return acc;
    }, { industry: '大盘整体', professional: 0, shop: 0, ad: 0, total: 0 });

  // ── 顶部 3 张 mini 卡（专业号/号店/号广，含占比） ──
  const channels = [
    { name: '专业号入驻', value: overall.professional, color: '#3b82f6' },
    { name: '号店入驻', value: overall.shop, color: '#f59e0b' },
    { name: '号广入驻', value: overall.ad, color: '#10b981' },
  ];
  cardsEl.innerHTML = channels.map(c => {
    const pctVal = overall.total ? (c.value / overall.total * 100) : 0;
    return `
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 16px;position:relative;overflow:hidden;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <span style="width:8px;height:8px;border-radius:2px;background:${c.color};"></span>
          <span style="font-size:11px;color:var(--text-3);font-weight:500;letter-spacing:0.02em;">${c.name}</span>
        </div>
        <div style="display:flex;align-items:baseline;gap:8px;">
          <div style="font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text-1);letter-spacing:-0.5px;">${pctVal.toFixed(1)}%</div>
          <div style="font-size:12px;color:var(--text-3);font-variant-numeric:tabular-nums;">${c.value.toLocaleString()} 个</div>
        </div>
      </div>
    `;
  }).join('');

  // ── 大盘整体：单条水平堆叠占比条（100% 归一化，直观展示比例） ──
  const totalV = overall.total || 1;
  const segs = channels.map(c => ({
    ...c,
    pct: c.value / totalV * 100,
  })).filter(s => s.value > 0);

  stackedEl.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
      <span style="font-size:11px;color:var(--text-3);font-weight:500;">大盘整体占比构成</span>
      <span style="font-size:11px;color:var(--text-3);font-variant-numeric:tabular-nums;">总计 ${overall.total.toLocaleString()}</span>
    </div>
    <div style="display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--border-soft);">
      ${segs.map(s => `<div title="${s.name} ${s.value} (${s.pct.toFixed(1)}%)" style="background:${s.color};width:${s.pct}%;transition:opacity 0.2s;" onmouseover="this.style.opacity=0.75" onmouseout="this.style.opacity=1"></div>`).join('')}
    </div>
    <div style="display:flex;gap:16px;margin-top:8px;flex-wrap:wrap;">
      ${segs.map(s => `
        <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-2);">
          <span style="width:8px;height:8px;border-radius:2px;background:${s.color};"></span>
          <span>${s.name}</span>
          <span style="color:var(--text-3);font-variant-numeric:tabular-nums;">${s.pct.toFixed(1)}%</span>
        </div>
      `).join('')}
    </div>
  `;

  // ── 行业堆叠柱状图（铺满宽度） ──
  const industryItems = rawItems
    .filter(i => i.industry !== '大盘整体')
    .sort((a, b) => b.total - a.total);

  if (!industryItems.length) {
    barEl.innerHTML = '<div class="chart-empty">暂无行业分布数据</div>';
    return;
  }

  barEl.style.height = Math.max(300, industryItems.length * 34 + 60) + 'px';

  const barChart = echarts.init(barEl);
  barChart.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
      backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
      padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
      formatter: params => {
        const it = industryItems[params[0].dataIndex];
        let html = `${it.industry} <span style="color:#94a3b8;">(总计 ${it.total})</span><br/>`;
        params.forEach(p => {
          const pct = it.total ? (p.value / it.total * 100).toFixed(1) : '0.0';
          html += `${p.marker} ${p.seriesName}: ${p.value} <span style="color:#94a3b8;">(${pct}%)</span><br/>`;
        });
        return html;
      },
    },
    legend: {
      top: 0, right: 0,
      textStyle: { fontSize: 11, color: '#64748b' },
      itemWidth: 10, itemHeight: 10, itemGap: 16,
    },
    grid: { top: 32, right: 40, bottom: 20, left: 96 },
    xAxis: {
      type: 'value',
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#94a3b8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
    },
    yAxis: {
      type: 'category', inverse: true,
      data: industryItems.map(i => i.industry),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#475569', fontSize: 12 },
    },
    series: [
      {
        name: '专业号入驻', type: 'bar', stack: 'source',
        data: industryItems.map(i => i.professional),
        itemStyle: { color: '#3b82f6' },
        barMaxWidth: 14,
      },
      {
        name: '号店入驻', type: 'bar', stack: 'source',
        data: industryItems.map(i => i.shop),
        itemStyle: { color: '#f59e0b' },
        barMaxWidth: 14,
      },
      {
        name: '号广入驻', type: 'bar', stack: 'source',
        data: industryItems.map(i => i.ad),
        itemStyle: { color: '#10b981', borderRadius: [0, 3, 3, 0] },
        barMaxWidth: 14,
      },
    ],
  });
  window.addEventListener('resize', () => barChart.resize());
}

// ── 算法准确率 ─────────────
function renderSuspectedSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">算法准确率</div>
          <div class="section-sub">人工确认实锤 / 进入标注池</div>
        </div>
      </div>
      <div class="chart tall" id="chart-accuracy"></div>
    </div>
  `;
}

function renderSuspectedChart() {
  const el = document.getElementById('chart-accuracy');
  if (!el) return;

  const items = (state.detail.industry_baseline || [])
    .map(b => ({
      industry: b.industry,
      annotated: b.annotated_uid || 0,
      confirmed: b.confirmed_fake_uid || 0,
      rate: pct(b.confirmed_fake_uid || 0, b.annotated_uid || 0),
    }))
    .filter(i => i.annotated > 0)
    .sort((a, b) => b.rate - a.rate);

  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">暂无算法准确率数据<br/><span style="font-size:11px;color:#94a3b8;">需数据推送 annotated_uid + confirmed_fake_uid</span></div>';
    return;
  }

  const chart = echarts.init(el);
  chart.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
      backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
      padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
      formatter: params => {
        const it = items[params[0].dataIndex];
        return `${it.industry}<br/>` +
          `<span style="color:#64748b;">标注池</span> ${it.annotated.toLocaleString()}<br/>` +
          `<span style="color:#64748b;">实锤</span> ${it.confirmed.toLocaleString()}<br/>` +
          `<b style="color:#3b82f6;">准确率 ${it.rate.toFixed(2)}%</b>`;
      },
    },
    grid: { top: 16, right: 56, bottom: 20, left: 100 },
    xAxis: {
      type: 'value', axisLabel: { formatter: '{value}%', color: '#94a3b8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
      axisLine: { show: false }, axisTick: { show: false },
      max: 100,
    },
    yAxis: {
      type: 'category', inverse: true,
      data: items.map(i => i.industry),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#475569', fontSize: 12 },
    },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: +i.rate.toFixed(2),
        itemStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
            colorStops: [
              { offset: 0, color: i.rate >= 50 ? '#10b981' : i.rate >= 20 ? '#3b82f6' : '#94a3b8' },
              { offset: 1, color: i.rate >= 50 ? '#6ee7b7' : i.rate >= 20 ? '#60a5fa' : '#cbd5e1' },
            ],
          },
          borderRadius: [0, 3, 3, 0],
        },
      })),
      label: { show: true, position: 'right', color: '#94a3b8', fontSize: 11, formatter: '{c}%' },
      barMaxWidth: 14,
    }],
  });

  window.addEventListener('resize', () => chart.resize());
  state.charts.accuracy = chart;
}

// ── 抽样置信对比 ─────────────
function renderConfidenceSection() {
  return `
    <div class="section">
      <div class="section-head">
        <div>
          <div class="section-title">抽样置信对比</div>
          <div class="section-sub">抽样占比 vs 入驻占比，偏差越小代表性越好</div>
        </div>
      </div>
      <div class="chart tall" id="chart-confidence"></div>
    </div>
  `;
}

function renderConfidenceChart() {
  const el = document.getElementById('chart-confidence');
  if (!el) return;

  const sample = {};
  (state.detail.sample_distribution || []).forEach(s => { sample[s.industry] = s.count; });
  const baseline = {};
  (state.detail.industry_baseline || []).forEach(b => { baseline[b.industry] = b.registered_uid || 0; });

  const allIndustries = new Set([...Object.keys(sample), ...Object.keys(baseline)]);
  const sampleTotal = Object.values(sample).reduce((s, v) => s + v, 0);
  const baselineTotal = Object.values(baseline).reduce((s, v) => s + v, 0);

  const items = [...allIndustries]
    .map(ind => ({
      industry: ind,
      samplePct: sampleTotal ? sample[ind] / sampleTotal * 100 : 0,
      baselinePct: baselineTotal ? baseline[ind] / baselineTotal * 100 : 0,
    }))
    .filter(i => i.samplePct > 0 || i.baselinePct > 0)
    .sort((a, b) => b.baselinePct - a.baselinePct);

  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">暂无数据</div>';
    return;
  }

  // 改用水平条形图，避免 x 轴标签拥挤
  el.style.height = Math.max(300, items.length * 34 + 60) + 'px';

  const chart = echarts.init(el);
  chart.setOption({
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(0,0,0,0.03)' } },
      backgroundColor: '#fff', borderColor: '#e2e8f0', borderWidth: 1,
      padding: [10, 14], textStyle: { color: '#1e293b', fontSize: 12 },
      formatter: params => {
        const it = items[params[0].dataIndex];
        const delta = it.samplePct - it.baselinePct;
        const sign = delta > 0 ? '+' : '';
        const deltaColor = Math.abs(delta) < 1 ? '#94a3b8' : (delta > 0 ? '#3b82f6' : '#f43f5e');
        return `${it.industry}<br/>` +
          `<span style="color:#64748b;">抽样</span> ${it.samplePct.toFixed(2)}%<br/>` +
          `<span style="color:#64748b;">入驻</span> ${it.baselinePct.toFixed(2)}%<br/>` +
          `<b style="color:${deltaColor};">偏差 ${sign}${delta.toFixed(2)}pp</b>`;
      },
    },
    legend: {
      top: 0, right: 0,
      textStyle: { fontSize: 11, color: '#64748b' },
      itemWidth: 10, itemHeight: 10, itemGap: 16,
    },
    grid: { top: 32, right: 52, bottom: 20, left: 96 },
    xAxis: {
      type: 'value', axisLabel: { formatter: '{value}%', color: '#94a3b8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9', type: 'dashed' } },
      axisLine: { show: false }, axisTick: { show: false },
    },
    yAxis: {
      type: 'category', inverse: true,
      data: items.map(i => i.industry),
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: '#475569', fontSize: 12 },
    },
    series: [
      {
        name: '抽样占比', type: 'bar',
        data: items.map(i => +i.samplePct.toFixed(2)),
        itemStyle: { color: '#3b82f6', borderRadius: [0, 3, 3, 0] },
        barMaxWidth: 10,
        barGap: '30%',
      },
      {
        name: '入驻占比', type: 'bar',
        data: items.map(i => +i.baselinePct.toFixed(2)),
        itemStyle: { color: '#cbd5e1', borderRadius: [0, 3, 3, 0] },
        barMaxWidth: 10,
      },
    ],
  });

  window.addEventListener('resize', () => chart.resize());
  state.charts.confidence = chart;
}
</script>
</body>
</html>

"""
# ── REPORT_HTML AUTOGEN END ──
