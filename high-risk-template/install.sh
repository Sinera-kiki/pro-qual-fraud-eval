#!/usr/bin/env bash
# install.sh - Pod 解压后跑一次
# 源自 guard-transform 模板；已按 automation 后端场景微调（去掉 npm / 前端静态托管分支）。
# shebang / set -eo pipefail / cd 都是硬约束，请勿手改。
set -eo pipefail
cd "$(dirname "$0")"

# monorepo 后端子目录（空字符串表示单仓，pip/npm 直接在顶层执行）
BACKEND_DIR=""

echo "[install] step: start (backend_dir='${BACKEND_DIR}')"

# 切到 backend 目录的 helper：单仓时是 no-op
_cd_backend() {
  if [ -n "$BACKEND_DIR" ] && [ -d "$BACKEND_DIR" ]; then
    cd "$BACKEND_DIR"
  fi
}

if [ "1" = "1" ]; then
  (
    _cd_backend
    if [ -f requirements.txt ]; then
      # ★ Pod 镜像（Dockerfile 里）已预创建 /opt/venv 并把 /opt/venv/bin 拍到 PATH 最前；
      #   pip / python 都解析到 /opt/venv/bin。直接用 ambient `python3 -m pip` 即可。
      # 历史坑：在 venv 内部再跑 `python3 -m venv .venv` 会触发：
      #   1) bookworm 的 python3-venv 单包不带 ensurepip 的 pip wheel（pip 在 python3-pip 包里），
      #      新建 .venv 没有 pip → `. .venv/bin/activate` 后 PATH 上 `pip: command not found`
      #   2) pip 装出来的 console_script shebang 写死创建时 venv 绝对路径，guard-rust
      #      启动期 fs::rename 工程目录后 execve ENOENT
      # 镜像源不写死在脚本里；如需走内部 mirror，在 Pod env 设 PIP_INDEX_URL / PIP_TRUSTED_HOST。
      # 2026-09-03: platform precheck 要求 install.sh 显式指定内网镜像（pypi.example.com），
      # Pod 内实际同时也有 PIP_INDEX_URL / PIP_TRUSTED_HOST 环境兜底，双保险。
      echo "[install] step: pip install (internal mirror) in $(pwd)"
      python3 -m pip install --no-cache-dir \
        -i http://pypi.example.com/simple/ \
        --trusted-host pypi.example.com \
        -r requirements.txt 2>&1
    fi
  )
fi

# ⛔ 本模板是纯 Python 后端：不做任何 npm / 前端托管相关安装。
#    前端是独立的 Platform 静态作品，由 platform-publish-automation-frontend skill 单独发布。

if [ "1" = "1" ]; then
  (
    _cd_backend
    echo "[install] step: db init (DDL + DML) in $(pwd)"
    if [ -f app/init_db.py ]; then
      python3 -m app.init_db 2>&1
    elif [ -f init_db.py ]; then
      python3 init_db.py 2>&1
    elif [ -f dist/init_db.js ]; then
      node dist/init_db.js 2>&1
    elif [ -f init_db.js ]; then
      node init_db.js 2>&1
    fi

    if [ -f app/seed_db.py ]; then
      echo "[install] step: db seed"
      python3 -m app.seed_db 2>&1
    elif [ -f dist/seed_db.js ]; then
      echo "[install] step: db seed"
      node dist/seed_db.js 2>&1
    fi
  )
fi

echo "[install] done"
