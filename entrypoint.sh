#!/bin/sh
# 本 fork 自定义入口：把容器变成「跑完即退」的电子书转有声书任务。
#
# 设计原则：
#   * 不修改上游任何代码 —— 只替换 entrypoint。上游源码原样保留，
#     以后 `git pull` 上游不会冲突。
#   * 容器内不跑 WebUI、不跑常驻服务。启动 -> 转换 -> 退出，
#     配 `docker run --rm` 时容器自动删除。
#
# 用法（在 128 上）：
#   docker run --rm \
#     -v /path/book.epub:/in/book.epub \
#     -v /data/stacks/audiobookshelf/audiobooks:/audiobooks \
#     -e OPENAI_API_KEY=... -e OPENAI_BASE_URL=... \
#     ghcr.io/houoop/epub_to_audiobook:latest /in/book.epub [音色] [并发]
set -e

echo "[entrypoint] 电子书 → 有声书"

: "${E2A_VOICE:=茉莉}"
: "${E2A_CONCURRENCY:=4}"
: "${E2A_STATE_DIR:=/state}"
export E2A_STATE_DIR

if [ $# -eq 0 ]; then
    echo "[entrypoint] 没有参数。" >&2
    echo "用法: docker run --rm -v <book.epub>:/in/book.epub \\" >&2
    echo "          -v <输出目录>:/audiobooks <image> [音色] [并发]" >&2
    exit 2
fi

# 保留上游的逃生舱：传 python3/-c 等自定义命令时直接执行（便于调试排查）
case "$1" in
    python3|python|/bin/sh|sh|/bin/bash|bash)
        echo "[entrypoint] 执行自定义命令: $@"
        exec "$@"
        ;;
esac

BOOK="$1"
VOICE="${2:-$E2A_VOICE}"
CONCURRENCY="${3:-$E2A_CONCURRENCY}"

if [ ! -f "$BOOK" ]; then
    echo "[entrypoint] 找不到书籍文件: $BOOK" >&2
    exit 1
fi

echo "[entrypoint] 书籍: $BOOK"
echo "[entrypoint] 音色: $VOICE"
echo "[entrypoint] 并发: $CONCURRENCY"

# --out 留空 => convert.py 按 EPUB 元数据自动生成「书名 -- 作者」目录
exec python3 /app_src/convert.py \
    --book "$BOOK" \
    --voice "$VOICE" \
    --concurrency "$CONCURRENCY"
