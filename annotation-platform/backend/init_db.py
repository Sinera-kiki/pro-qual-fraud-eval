"""
DB initializer — idempotent, runs in install.sh.
Creates tables for the qual-annotation-platform schema.
"""
import asyncio
import asyncpg
import sys


DB_PROPS_FILE = "db" + ".properties"  # platform-injected at runtime


def load_db_props(path=DB_PROPS_FILE):
    if os.environ.get("DB_HOST"):
        return {
            "db.host": os.environ.get("DB_HOST", "localhost"),
            "db.port": os.environ.get("DB_PORT", "5432"),
            "db.username": os.environ.get("DB_USER", "postgres"),
            "db.password": os.environ.get("DB_PASSWORD", "postgres"),
            "db.database": os.environ.get("DB_NAME", "qual_annotation"),
        }
    props = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"[init_db] DB props file not found at {path!r} — skipping (dev mode)")
        sys.exit(0)
    return props


DDL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 数据集表
CREATE TABLE IF NOT EXISTS datasets (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          TEXT        NOT NULL,
    filename      TEXT        NOT NULL,
    total_rows    INT         NOT NULL DEFAULT 0,
    cluster_count INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 数据行表（每行对应 CSV 一条记录）
CREATE TABLE IF NOT EXISTS dataset_items (
    id                BIGSERIAL   PRIMARY KEY,
    dataset_id        UUID        NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    user_id           TEXT        NOT NULL,
    qualification_url TEXT        NOT NULL,
    trade_first_name  TEXT        NOT NULL,
    trade_second_name TEXT        NOT NULL,
    cluster_id        INT         NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dataset_items_dataset
    ON dataset_items (dataset_id);

CREATE INDEX IF NOT EXISTS idx_dataset_items_dataset_cluster
    ON dataset_items (dataset_id, cluster_id);

-- 簇标注表（每个簇一条标注记录）
CREATE TABLE IF NOT EXISTS cluster_annotations (
    id            BIGSERIAL   PRIMARY KEY,
    dataset_id    UUID        NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    cluster_id    INT         NOT NULL,
    remark_first  TEXT        NOT NULL,
    remark_second TEXT        NOT NULL,
    annotated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dataset_id, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_annotations_dataset
    ON cluster_annotations (dataset_id);

-- 图片粒度标记表（剔除/不违规）
CREATE TABLE IF NOT EXISTS item_labels (
    id                BIGSERIAL   PRIMARY KEY,
    dataset_id        UUID        NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    qualification_url TEXT        NOT NULL,
    labeled_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dataset_id, qualification_url)
);

CREATE INDEX IF NOT EXISTS idx_item_labels_dataset
    ON item_labels (dataset_id);

CREATE INDEX IF NOT EXISTS idx_item_labels_dataset_url
    ON item_labels (dataset_id, qualification_url);

CREATE TABLE IF NOT EXISTS upload_sessions (
    id          UUID        PRIMARY KEY,
    filename    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized   BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS upload_chunks (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
    chunk_index INT         NOT NULL,
    data        TEXT        NOT NULL,
    UNIQUE (session_id, chunk_index)
);

-- 调度平台集成用：API Token 表
CREATE TABLE IF NOT EXISTS api_tokens (
    id          BIGSERIAL   PRIMARY KEY,
    token       TEXT        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    created_by  TEXT        NOT NULL DEFAULT 'system',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- 首次部署时植入一个默认 token 供调度平台立刻可用（幂等）
INSERT INTO api_tokens (token, name, created_by)
VALUES ('automation-default-token-please-rotate-in-prod', '调度平台默认接入 token（请在生产环境轮换）', 'system')
ON CONFLICT (token) DO NOTHING;

-- ─── v1.1: 双通道 (评估 evaluation / 审核 review) ───────────────────────────

-- 数据集加 channel 字段：evaluation | review；老数据默认 evaluation（向后兼容）
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'evaluation';
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'datasets_channel_check'
    ) THEN
        ALTER TABLE datasets ADD CONSTRAINT datasets_channel_check
            CHECK (channel IN ('evaluation', 'review'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_datasets_channel ON datasets (channel);

-- 标注记录保留是谁标的，供审核通道结果导出用；老数据留 NULL
ALTER TABLE cluster_annotations ADD COLUMN IF NOT EXISTS annotator_email TEXT;

-- ─── v1.2: 审核通道改为抢单制（cluster_pool）──────────────────────
-- 以前用 review_members（白名单）+ assignments（批量预分）。
-- v1.2 改为：任何 SSO 用户可入审核通道；抢单式自动分配；簇标完即锁（不可改）。
-- 旧表直接 drop，让 v1.1 测试数据一并清掌。
DROP TABLE IF EXISTS assignments;
DROP TABLE IF EXISTS review_members;

-- 簇任务池：审核通道专用。上传审核数据集时自动插满（每个簇一行）。
CREATE TABLE IF NOT EXISTS cluster_pool (
    dataset_id     UUID        NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    cluster_id     INT         NOT NULL,
    assignee_email TEXT,                    -- NULL = 未被认领
    claimed_at     TIMESTAMPTZ,             -- 认领时间
    completed_at   TIMESTAMPTZ,             -- 标注完成时间（!= NULL 即锁死，不可改）
    PRIMARY KEY (dataset_id, cluster_id)
);
-- 抢单时用到的共用索引（未认领 & 未完成）
CREATE INDEX IF NOT EXISTS idx_cluster_pool_available
    ON cluster_pool (dataset_id, cluster_id) WHERE assignee_email IS NULL AND completed_at IS NULL;
-- 查“我当前任务”用的索引
CREATE INDEX IF NOT EXISTS idx_cluster_pool_assignee
    ON cluster_pool (dataset_id, assignee_email) WHERE completed_at IS NULL;

-- ─── v1.4: 图片粒度审核判定（违规 / 不违规） ────────────────────
-- v1.5: verdict 存 label 叶字符串（不违规 / 实拍图片p图 / 资质模糊 ...），不再只存 violate/ok
CREATE TABLE IF NOT EXISTS image_reviews (
    dataset_id        UUID        NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    qualification_url TEXT        NOT NULL,
    verdict           TEXT        NOT NULL,
    reviewer_email    TEXT        NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dataset_id, qualification_url)
);
-- 历史 CHECK 约束只允许 violate/ok，v1.5 需接受任意标签字符串，需 drop 旧约束
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'image_reviews_verdict_check'
    ) THEN
        ALTER TABLE image_reviews DROP CONSTRAINT image_reviews_verdict_check;
    END IF;
END $$;

-- v1.5 迁移：旧值 violate/ok → 新标签（幂等，只迁移一次）
UPDATE image_reviews SET verdict = '不违规' WHERE verdict = 'ok';
UPDATE image_reviews SET verdict = '实锤造假·实拍图片p图' WHERE verdict = 'violate';
CREATE INDEX IF NOT EXISTS idx_image_reviews_dataset
    ON image_reviews (dataset_id);

-- v1.6: 审核通道回归纯图片审核，移除旧账户假数据表
DROP TABLE IF EXISTS account_info;
"""


async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DB_PROPS_FILE
    props = load_db_props(path)
    conn = await asyncpg.connect(
        user=props["db.username"],
        password=props["db.password"],
        host=props["db.host"],
        port=int(props["db.port"]),
        database=props["db.database"],
    )
    try:
        await conn.execute(DDL)
        print("[init_db] ✅ DB schema initialized")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
