"""DDL — 高危模板人工复核后端。

install.sh 在 TPL_NEEDS_DB_INIT=1 时自动跑一次。
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
    CREATE TABLE IF NOT EXISTS review_decisions (
        id          BIGSERIAL PRIMARY KEY,
        run_tag     TEXT NOT NULL,
        cluster_id  TEXT NOT NULL,
        decision    TEXT NOT NULL,        -- keep / drop
        reviewer    TEXT,                  -- SSO email
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (run_tag, cluster_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_review_decisions_run
        ON review_decisions (run_tag)
    """,
    """
    CREATE TABLE IF NOT EXISTS submit_runs (
        id            BIGSERIAL PRIMARY KEY,
        run_tag       TEXT NOT NULL,
        operator      TEXT,
        exit_code     INTEGER,
        log_tail      TEXT,
        submitted_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_submit_runs_tag
        ON submit_runs (run_tag, submitted_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    INSERT INTO app_settings (key, value)
    VALUES (
        'automation_upload_token_sha256',
        '47f7e2f094d04347a5f17107c7f0a4488a4634e67f2e5813b3db28fe7cfb6392'
    )
    ON CONFLICT (key) DO NOTHING
    """,
]


def main() -> None:
    p = _load_props("db.properties")
    if not p.get("db.host"):
        print("db.properties 未配置，跳过 init")
        return
    with psycopg.connect(
        host=p["db.host"], port=int(p["db.port"]),
        dbname=p["db.database"], user=p["db.username"], password=p["db.password"],
    ) as conn:
        for sql in DDL:
            conn.execute(sql)
        conn.commit()
    print(f"init_db 完成: {p['db.host']}:{p['db.port']}/{p['db.database']}")


if __name__ == "__main__":
    main()
