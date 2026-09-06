#!/usr/bin/env bash
# 资质造假评估工作流 · 周度磁盘清理（2026-08-28 固化）
# 目标：把周度产物累积释放的磁盘定期回收，避免 40G 盘撞满
# 覆盖：
#   1) img_disk_cache 里 >14 天的老图片缓存（当周聚簇不受影响）
#   2) tmp 下的一次性图片缓存目录（bl_imgcache / w*_imgcache）
#   3) 周目录里的 full_account_embedding.csv（有 slim 版可替代，每份 400M+）
#   4) daily 目录只保留最近 3 天
#   5) __pycache__ 与 *.pyc 老垃圾
set -u
cd ${PROJECT_ROOT} || exit 1

echo "=== 周度磁盘清理 $(date '+%F %T') ==="
before=$(df -h ${HOME} | awk 'NR==2 {print $3"/"$2" ("$5")"}')
echo "清理前：$before"

# 1) 剪 14 天前的图片缓存文件（保留目录结构与近期新增）
if [ -d img_disk_cache ]; then
    find img_disk_cache -type f -mtime +14 -delete 2>/dev/null
    echo "  ✓ img_disk_cache 剪枝完成（>14 天）当前 $(du -sh img_disk_cache | cut -f1)"
fi

# 2) tmp 下的一次性图片缓存
for d in ${WORKSPACE_ROOT}/tmp/bl_imgcache \
         ${WORKSPACE_ROOT}/tmp/w*_imgcache; do
    [ -d "$d" ] && rm -rf "$d" && echo "  ✓ 删除 $d"
done

# 3) 周目录 full embedding 大文件（有 slim 版存在时才删）
for w in w*; do
    [ -d "$w" ] || continue
    if [ -f "$w/full_account_embedding_slim.csv" ] && [ -f "$w/full_account_embedding.csv" ]; then
        rm -f "$w/full_account_embedding.csv"
        echo "  ✓ 删除 $w/full_account_embedding.csv"
    fi
done

# 4) daily 目录只保留最近 3 天（按目录名字符序，格式 yyyymmdd）
if [ -d daily ]; then
    cd daily || exit 1
    kept=0; deleted=0
    for d in $(ls -d 2026* 2>/dev/null | sort); do
        kept=$((kept+1))
    done
    to_delete=$(( kept > 3 ? kept - 3 : 0 ))
    if [ "$to_delete" -gt 0 ]; then
        for d in $(ls -d 2026* 2>/dev/null | sort | head -n "$to_delete"); do
            rm -rf "$d"
            deleted=$((deleted+1))
            echo "  ✓ 删除 daily/$d"
        done
    fi
    cd ..
fi

# 5) __pycache__
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -type f -name '*.pyc' -delete 2>/dev/null

after=$(df -h ${HOME} | awk 'NR==2 {print $3"/"$2" ("$5")"}')
echo "清理后：$after"
echo "=== 完成 $(date '+%F %T') ==="
