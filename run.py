#!/usr/bin/env python3
"""启动一次性转换容器，等它跑完，容器自动删除，最后触发 ABS 扫描。

配套 ghcr.io/houoop/epub_to_audiobook 镜像使用：容器不常驻、不跑 WebUI，
启动 -> 转换 -> 退出（`--rm` 自动删除）。

用法（在 128 上）：
  python3 /opt/epub2audio/run.py /root/books/三体.epub
  python3 /opt/epub2audio/run.py /root/books/三体.epub 白桦
  python3 /opt/epub2audio/run.py /root/books/三体.epub --concurrency 6

默认后台运行（`-d`），脚本只负责启动 + 报告；加 --wait 才阻塞等待完成。
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

IMAGE = "ghcr.io/houoop/epub_to_audiobook:latest"
TTS_CONTAINER = "mimo-tts-conv"          # 协议适配器（常驻）
ABS_DIR = "/data/stacks/audiobookshelf/audiobooks"
STATE_DIR = "/data/stacks/epub-to-audiobook/state"
VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]
DEFAULT_VOICE = "茉莉"
ABS_PORT = 13378


def sh(cmd, timeout=300):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return re.sub(r"bash: warning: setlocale[^\n]*\n?", "", out).strip()


def shq(args):
    """参数列表 -> 安全 shell 命令。

    必须加引号：输出目录名形如 `书名 -- 作者` **含空格**，直接 join 会被
    shell 拆成多个参数，导致进程秒退（表现为「无日志、0 章完成」）。
    """
    return " ".join(shlex.quote(a) for a in args)


def container_ip(name):
    return sh("docker inspect %s --format '{{range .NetworkSettings.Networks}}"
              "{{.IPAddress}}{{end}}'" % name).strip()


def abs_scan():
    """触发 ABS 库扫描。

    ABS 容器内没有 python3/sqlite3，所以 token 从宿主机读 sqlite、API 走宿主机。
    """
    db = "/data/stacks/audiobookshelf/config/absdatabase.sqlite"
    if not os.path.exists(db):
        return "  找不到 ABS 数据库，跳过扫描"
    tok = sh("python3 -c %s" % shq([
        "import sqlite3\n"
        "d=sqlite3.connect(%r)\n"
        "print(d.execute('select token from users limit 1').fetchone()[0])\n" % db
    ]), timeout=60)
    if not tok:
        return "  读不到 ABS token，跳过扫描"

    libs = sh("curl -s -m 15 'http://127.0.0.1:%d/api/libraries?token=%s'"
              % (ABS_PORT, tok), timeout=60)
    pairs = re.findall(r'"id":"([0-9a-f-]{36})","name":"([^"]+)"', libs)
    if not pairs:
        return "  取不到书库列表，跳过扫描"

    out = []
    for lib_id, name in pairs:
        r = sh("curl -s -m 20 -X POST -H 'Content-Type: application/json' -d '{}' "
               "'http://127.0.0.1:%d/api/libraries/%s/scan?token=%s'"
               % (ABS_PORT, lib_id, tok), timeout=60)
        out.append("  已触发扫描: %s %s" % (name, r[:60]))
    return "\n".join(out)


def read_state():
    hb = os.path.join(STATE_DIR, "heartbeat.json")
    if not os.path.exists(hb):
        return {}
    try:
        with open(hb, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="一次性容器：转有声书 → 自动清理")
    ap.add_argument("epub", help="EPUB 路径（128 上的路径）")
    ap.add_argument("voice", nargs="?", default=DEFAULT_VOICE)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--wait", action="store_true", help="阻塞等待转换完成")
    ap.add_argument("--timeout", type=int, default=10800, help="--wait 的最长等待秒数")
    ap.add_argument("--name", default="", help="容器名（默认自动生成）")
    ap.add_argument("--refresh", action="store_true", help="强制拉取最新镜像")
    a = ap.parse_args()

    epub = os.path.abspath(a.epub)
    if not os.path.exists(epub):
        sys.exit("找不到文件: %s" % epub)
    if a.voice not in VOICES:
        sys.exit("音色必须是: %s" % ", ".join(VOICES))

    # 适配器必须在跑（转换要经它转成小米 chat 形态）
    if TTS_CONTAINER not in sh("docker ps --format '{{.Names}}'"):
        sys.exit("适配器 %s 没在运行：cd /data/stacks/epub-to-audiobook && docker compose up -d"
                 % TTS_CONTAINER)
    adapter_ip = container_ip(TTS_CONTAINER)
    if not adapter_ip:
        sys.exit("取不到适配器 IP")

    key = sh("grep -E '^MIMO_KEY=' /data/stacks/epub-to-audiobook/.env | cut -d= -f2-")
    if not key:
        sys.exit("读不到 MIMO_KEY（检查 /data/stacks/epub-to-audiobook/.env）")

    if a.refresh:
        print("  拉取最新镜像…")
        print("  " + sh("docker pull %s 2>&1 | tail -2" % IMAGE, timeout=900).replace("\n", "\n  "))

    os.makedirs(STATE_DIR, exist_ok=True)
    # 清掉旧心跳，避免误判为「已完成」
    for f in ("heartbeat.json", "convert_progress.log"):
        try:
            os.remove(os.path.join(STATE_DIR, f))
        except FileNotFoundError:
            pass

    cname = a.name or "e2a-%s" % time.strftime("%m%d%H%M%S")
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", cname,
        "--network", "epub-to-audiobook_default",
        "-v", "%s:/in/book.epub:ro" % epub,
        "-v", "%s:/audiobooks" % ABS_DIR,
        "-v", "%s:/state" % STATE_DIR,
        "-e", "OPENAI_API_KEY=%s" % key,
        "-e", "OPENAI_BASE_URL=http://%s:8080/v1" % adapter_ip,
        "-e", "E2A_STATE_DIR=/state",
        "-e", "E2A_VOICE=%s" % a.voice,
        "-e", "E2A_CONCURRENCY=%d" % a.concurrency,
        IMAGE,
        "/in/book.epub",
    ]
    out = sh(shq(cmd), timeout=300)
    if not out or len(out) > 70 or " " in out.strip():
        # docker run -d 成功时只回一行完整容器 ID
        if not re.fullmatch(r"[0-9a-f]{64}", out.strip()):
            sys.exit("启动失败: %s" % out[:400])

    print("=" * 64)
    print("  已启动转换容器: %s" % cname)
    print("  书籍: %s" % epub)
    print("  音色: %s   并发: %d" % (a.voice, a.concurrency))
    print("=" * 64)
    print()
    print("  查看进度:")
    print("    docker exec %s tail -20 /state/convert_progress.log" % cname)
    print("    cat %s/heartbeat.json" % STATE_DIR)
    print()

    if not a.wait:
        print("  容器在后台运行，跑完自动删除。")
        return

    print("  等待转换完成…")
    t0 = time.time()
    last = -1
    while time.time() - t0 < a.timeout:
        st = sh("docker ps --filter name=^%s$ --format '{{.Names}}'" % cname)
        hb = read_state()
        n = len(hb.get("failed") or []) + (hb.get("done") or 0)
        if n != last:
            el = int(time.time() - t0)
            print("  [%5ds] %s/%s 章  %s" % (el, hb.get("done", 0),
                  hb.get("total", "?"), hb.get("state", "")))
            sys.stdout.flush()
            last = n
        if not st:
            break   # 容器已退出（--rm 已删除）
        time.sleep(20)

    hb = read_state()
    print()
    print("  " + "=" * 60)
    print("  状态: %s | 成功 %s | 失败 %s" % (
        hb.get("state", "?"), hb.get("done", "?"), len(hb.get("failed") or [])))
    print("  输出: %s" % hb.get("out", "(未知)"))
    print("  " + "=" * 60)

    if hb.get("done"):
        print()
        print("  触发 Audiobookshelf 扫描…")
        print(abs_scan())


if __name__ == "__main__":
    main()
