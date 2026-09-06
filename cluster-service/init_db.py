"""DDL — 聚簇自助服务后端。

两张表：
  qc_tasks        任务主表（状态机 + 产物链接）
  qc_events       任务事件流（进度日志，前端轮询展示）

install.sh 在 TPL_NEEDS_DB_INIT=1 时自动跑，全部幂等。
"""
from __future__ import annotations

import psycopg


def _load_props(path: str) -> dict[str, str]:
    props: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    return props


DDL = [
    """
    CREATE TABLE IF NOT EXISTS qc_tasks (
        id              BIGSERIAL PRIMARY KEY,
        created_by      TEXT NOT NULL,             -- 提交人 SSO email
        created_by_name TEXT,
        title           TEXT NOT NULL,             -- 任务名（用户起）
        id_type         TEXT NOT NULL,             -- uid / audit_order
        id_column       TEXT,                      -- 识别到的 ID 列名
        source_kind     TEXT NOT NULL,             -- online / offline
        source_ref      TEXT NOT NULL,             -- 在线表链接 / 原文件名
        n_ids           INTEGER,                   -- 清单 ID 数
        cluster_mode    TEXT NOT NULL DEFAULT 'pool_match',
                        -- pool_match: 匹配历史图池；self_cluster: 上传账户彼此聚簇
        status          TEXT NOT NULL DEFAULT 'pending',
                        -- pending → running → done / failed
        pool_start      DATE NOT NULL,            -- 池时间窗
        pool_end        DATE NOT NULL,
        detail          TEXT,                      -- 失败原因 / 备注
        content_oid     BIGINT,                    -- 表格内容的 PG Large Object oid
        result_csv_url  TEXT,                      -- 账户级结果表（CDN）
        flat_page_url   TEXT,                      -- 平摊网页（CDN）
        n_accounts      INTEGER,                   -- 结果统计
        n_clustered     INTEGER,
        n_unclustered   INTEGER,
        n_no_emb        INTEGER,
        started_at      TIMESTAMPTZ,
        finished_at     TIMESTAMPTZ,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qc_tasks_status ON qc_tasks (status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qc_tasks_owner ON qc_tasks (created_by, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS qc_events (
        id          BIGSERIAL PRIMARY KEY,
        task_id     BIGINT NOT NULL REFERENCES qc_tasks(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL,                -- ingest/map/fetch/cluster/build/notify
        message     TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qc_events_task ON qc_events (task_id, id)
    """,
    # 增量补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。
    # content_oid 从 detail 里拆出来——一个字段兼作内容引用和失败原因，
    # 任务失败时错误信息会把 lo:oid 冲掉，导致无法重跑。
    """
    ALTER TABLE qc_tasks ADD COLUMN IF NOT EXISTS content_oid BIGINT
    """,
    # 账户自聚簇任务无时间窗，放开非空约束（重复执行无副作用）
    """
    ALTER TABLE qc_tasks ALTER COLUMN pool_start DROP NOT NULL
    """,
    """
    ALTER TABLE qc_tasks ALTER COLUMN pool_end DROP NOT NULL
    """,
    # 产物（结果表 / 聚簇网页）存 PG Large Object，同源下载
    """
    ALTER TABLE qc_tasks ADD COLUMN IF NOT EXISTS csv_oid BIGINT
    """,
    """
    ALTER TABLE qc_tasks ADD COLUMN IF NOT EXISTS page_oid BIGINT
    """,
    """
    ALTER TABLE qc_tasks ADD COLUMN IF NOT EXISTS cluster_mode TEXT NOT NULL DEFAULT 'pool_match'
    """,
    # 存量数据搬迁：把 detail 里的 lo:oid 迁到新列
    """
    UPDATE qc_tasks SET content_oid = CAST(SUBSTRING(detail FROM 4) AS BIGINT)
    WHERE content_oid IS NULL AND detail LIKE 'lo:%'
      AND SUBSTRING(detail FROM 4) ~ '^[0-9]+$'
    """,
    # 跑批侧用共享 token 领任务，防误领：领取即把 pending → running 并打时间戳
]


def main() -> None:
    p = _load_props("db.properties")
    if not p.get("db.host"):
        print("db.properties 未配置，跳过 init")
        return
    with psycopg.connect(
        host=p["db.host"], port=int(p["db.port"]), dbname=p["db.database"],
        user=p["db.username"], password=p["db.password"],
    ) as conn:
        with conn.cursor() as cur:
            for ddl in DDL:
                cur.execute(ddl)
        conn.commit()
    print("qc_tables ready")


if __name__ == "__main__":
    main()
