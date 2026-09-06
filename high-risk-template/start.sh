#!/usr/bin/env bash
# start.sh — 由 Guard 拉起业务主进程；末行必须 exec
set -eo pipefail
cd "$(dirname "$0")"

# UTF-8 locale：upload run 目录名带中文，Python 需要正确 encoding 才能 open()
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# runs/ 和 bot 脚本随源码打包；Pod 磁盘可写时把 runs 从源码同步到 /tmp/hrm_runs（可写副本）
# 用 rsync -a 让每次 redeploy 都能带上镜像里的 w34/w35 等只读历史，但 upload run（用户新建的）
# 一旦存在就不覆盖（-n 不覆盖，靠 rsync 的 --ignore-existing）
export HRM_BOT_PATH="$(pwd)/high_risk_media_bot.py"
if [ -z "${HRM_RUNS_ROOT:-}" ]; then
  mkdir -p /tmp/hrm_runs
  if [ -d "runs" ]; then
    # -a 递归；--ignore-existing 已存在的不覆盖（保留 upload run）
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --ignore-existing runs/ /tmp/hrm_runs/ 2>/dev/null || true
    else
      cp -rn runs/. /tmp/hrm_runs/ 2>/dev/null || true
    fi
  fi
  export HRM_RUNS_ROOT=/tmp/hrm_runs
fi

# Pod 镜像预置 /opt/venv/bin 在 PATH 最前，python3 直接解析到 /opt/venv/bin/python3。
# 禁止在工程内建 .venv（verify_no_venv_creation.sh 会卡；详见 subapp-spec §4.1）。
export APP_PORT="${APP_PORT:-3000}"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${APP_PORT}" 2>&1
