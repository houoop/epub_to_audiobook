#!/usr/bin/env python3
"""电子书 → 有声书（逐章 / 断点续传 / 失败重试 / 章内并发）。

这是本 fork 相对上游唯一的新增可执行文件，上游代码一行未改。
上游的 `main_ui.py`（WebUI）和 `main.py` 保持原样，本脚本独立于它们工作。

为什么不用上游的 `AudiobookGenerator.run()`：
  1. 它把整本书交给 multiprocessing.Pool 一次性处理，没有断点续传——
     中断一次就要重头再来（长书几小时的成果全丢）。
  2. 它无条件用 `self.config.log` 当 Pool initializer 的日志级别，而
     `GeneralConfig(None)` 会把该字段留成 None，于是每个 worker 初始化即抛
     `TypeError: Level not an integer or a valid string: None`；Pool 遇到
     worker 初始化失败会不停补拉新 worker，形成**无限崩溃循环**——实测
     3 分钟 2.4 万个 worker、38 万行日志、持续吃满一个 CPU 核。
  本脚本直接调底层函数（parser / tts_provider / split_text / merge_audio_segments），
  既天然绕开该缺陷，又拿到了上游没有的续传 + 章内并发能力。

用法（容器内）：
  python3 /app_src/convert.py --book /in/book.epub [--out "/audiobooks/书名"] [音色]
"""
import argparse
import glob
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from xml.etree import ElementTree as ET

sys.path.insert(0, "/app_src")

from audiobook_generator.book_parsers.base_book_parser import get_book_parser
from audiobook_generator.config.general_config import GeneralConfig
from audiobook_generator.tts_providers.base_tts_provider import get_tts_provider
from audiobook_generator.utils.utils import split_text, set_audio_tags, merge_audio_segments
from audiobook_generator.core.audio_tags import AudioTags
from openai import OpenAI

DEFAULT_MODEL = "mimo-v2.5-tts"
DEFAULT_VOICE = "茉莉"
DEFAULT_LANG = "zh-CN"
DEFAULT_RETRIES = 3
RETRY_DELAYS = [30, 60, 120, 300]
CHUNK_CHARS = 1800

# 运行期状态（进度 / 心跳）放这里，宿主机可挂载以观察进度
STATE_DIR = os.environ.get("E2A_STATE_DIR", "/state")
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat.json")

log = logging.getLogger("e2a")


# ---------------------------------------------------------------- 元数据

def read_book_meta(path):
    """从 EPUB 的 OPF metadata 读 (书名, 作者)。纯标准库，不依赖 ebooklib。"""
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("META-INF/container.xml"))
            node = root.find(".//{*}rootfile")
            opf = z.read(node.get("full-path"))
        root = ET.fromstring(opf)

        def first(tag):
            el = root.find(".//dc:" + tag, ns) or root.find(
                ".//{http://purl.org/dc/elements/1.1/}" + tag)
            return (el.text or "").strip() if el is not None else ""

        title, author = first("title"), first("creator")
    except Exception as e:
        log.warning(f"读取 EPUB 元数据失败（将用文件名兜底）: {type(e).__name__}: {e}")
        title, author = os.path.splitext(os.path.basename(path))[0], ""

    def clean(s):
        s = re.sub(r'[\\/:*?"<>|]', "", s)
        return re.sub(r"\s+", " ", s).strip()

    title, author = clean(title), clean(author)
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    return title, author


# ---------------------------------------------------------------- 基础设施

def parse_args():
    p = argparse.ArgumentParser(description="逐章转有声书（可续传/并发）")
    p.add_argument("--book", required=True, help="EPUB 路径")
    p.add_argument("--out", default="", help="输出目录；留空则按书名自动生成")
    p.add_argument("--outdir", default="/audiobooks", help="输出根目录")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=-1)
    p.add_argument("--skip", default="", help="跳过的章节，如 4,5")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--lang", default=DEFAULT_LANG)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--concurrency", type=int, default=4, help="章内并发分片数")
    p.add_argument("--min-size-kb", type=int, default=10,
                   help="MP3 小于此体积视为残缺，会被清理重转")
    p.add_argument("--remove-refs", action="store_true", default=True)
    p.add_argument("--keep-refs", dest="remove_refs", action="store_false")
    p.add_argument("--log", default=os.path.join(STATE_DIR, "convert_progress.log"))
    return p.parse_args()


def setup_logging(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]


def write_heartbeat(**kw):
    kw["ts"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HEARTBEAT, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
    except Exception:
        pass


def build_config(a, idx):
    c = GeneralConfig(None)
    c.input_file = a.book
    c.output_folder = a.out
    c.tts = "openai"
    c.model_name = a.model
    c.voice_name = a.voice
    c.language = a.lang
    c.output_format = "mp3"
    c.no_prompt = True
    c.log = "INFO"          # 关键：绝不可留空（见文件头注释的崩溃循环）
    c.title_mode = "auto"
    c.newline_mode = "double"
    c.chapter_start = idx
    c.chapter_end = idx
    c.remove_endnotes = False
    c.remove_reference_numbers = a.remove_refs
    c.preview = False
    c.output_text = False
    c.search_and_replace_file = ""
    c.worker_count = 1
    c.speed = 1.0
    c.instructions = None   # WebUI 默认会塞文本，非 gpt-4o-mini-tts 会直接报错
    return c


def chapter_output(a, idx, min_size):
    for f in sorted(glob.glob(os.path.join(a.out, f"{idx:04d}_*.mp3"))):
        if os.path.getsize(f) >= min_size:
            return f
    return None


def cleanup_partial(a, idx, min_size):
    for f in glob.glob(os.path.join(a.out, f"{idx:04d}_*.mp3")):
        if os.path.getsize(f) < min_size:
            os.remove(f)
            return os.path.basename(f)
    return None


def get_titles_and_texts(a):
    c = build_config(a, 1)
    c.chapter_end = -1
    parser = get_book_parser(c)
    chs = parser.get_chapters(get_tts_provider(c).get_break_string())
    return [(t, txt) for t, txt in chs if txt.strip()], parser


def synth_chunk(client, model, voice, chunk, idx, i, n):
    last = None
    for attempt in range(1, 4):
        try:
            resp = client.audio.speech.create(model=model, voice=voice, input=chunk,
                                              response_format="mp3")
            return i, resp.content, None
        except Exception as e:
            last = e
            log.warning(f"      [{idx}] 分片 {i}/{n} 第 {attempt} 次失败: "
                        f"{type(e).__name__}: {str(e)[:100]}")
            time.sleep(min(30 * attempt, 90))
    return i, None, last


def convert_chapter_fast(a, idx, title, text, parser):
    """章内并发合成，全部成功才落盘。"""
    from audiobook_generator.utils.filename_sanitizer import make_safe_filename

    safe = make_safe_filename(title=title, idx=idx, output_dir=a.out, ext=".mp3",
                              collision_check=False)
    out_file = os.path.join(a.out, safe)

    chunks = split_text(text, CHUNK_CHARS, a.lang)
    n = len(chunks)
    log.info(f"    ↳ {n} 个分片，并发 {a.concurrency} 合成…")

    client = OpenAI(max_retries=4)   # 读 OPENAI_API_KEY / OPENAI_BASE_URL
    results = {}
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(synth_chunk, client, a.model, a.voice, ch, idx, i, n): i
                for i, ch in enumerate(chunks, 1)}
        completed = 0
        for fut in as_completed(futs):
            i, content, err = fut.result()
            completed += 1
            if content is None:
                log.error(f"      [{idx}] 分片 {i}/{n} 最终失败: {err}")
                write_heartbeat(state="converting", current=idx, title=title,
                                chunk=f"{completed}/{n}", chunk_failed=i)
                raise RuntimeError(f"chapter {idx} chunk {i} failed: {err}")
            results[i] = content
            if completed % 3 == 0 or completed == n:
                log.info(f"      [{idx}] 分片进度 {completed}/{n}")
                write_heartbeat(state="converting", current=idx, title=title,
                                chunk=f"{completed}/{n}")

    segs = [io.BytesIO(results[i]) for i in range(1, n + 1)]
    ids = [f"chapter-{idx}_{title}_chunk_{i}_of_{n}" for i in range(1, n + 1)]
    merge_audio_segments(segs, out_file, "mp3", ids, False)
    tags = AudioTags(title, parser.get_book_author(), parser.get_book_title(), idx)
    set_audio_tags(out_file, tags)
    return out_file


def main():
    a = parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)

    # 输出目录：未指定则按 EPUB 元数据自动生成「书名 -- 作者」
    if not a.out:
        title, author = read_book_meta(a.book)
        dirname = f"{title} -- {author}" if author else title
        a.out = os.path.join(a.outdir.rstrip("/"), dirname)
    setup_logging(a.log)
    os.makedirs(a.out, exist_ok=True)
    min_size = a.min_size_kb * 1024

    if not os.path.exists(a.book):
        log.error(f"找不到书籍: {a.book}")
        sys.exit(1)

    log.info("=" * 66)
    log.info(f"书籍: {os.path.basename(a.book)} → {a.out}")
    chapters, parser = get_titles_and_texts(a)
    total = len(chapters)
    end = total if a.end == -1 else a.end
    skip = {int(x) for x in a.skip.split(",") if x.strip().isdigit()}

    log.info(f"开始: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info(f"总章数: {total} | 范围: {a.start}-{end} | 跳过: {sorted(skip) or '无'}")
    log.info(f"音色: {a.voice} | 模型: {a.model} | 并发: {a.concurrency}")
    log.info("=" * 66)
    write_heartbeat(state="running", total=total, current=0, out=a.out)

    done = skipped = 0
    failed = []

    for idx in range(a.start, end + 1):
        title, text = chapters[idx - 1] if idx <= total else (f"章节{idx}", "")

        if idx in skip:
            log.info(f"[{idx:02d}/{total}] 按设置跳过：{title[:40]}")
            skipped += 1
            continue

        removed = cleanup_partial(a, idx, min_size)
        if removed:
            log.info(f"[{idx:02d}/{total}] 清理残缺文件 {removed}")

        existing = chapter_output(a, idx, min_size)
        if existing:
            log.info(f"[{idx:02d}/{total}] ✔ 已完成，跳过"
                     f"（{os.path.getsize(existing)/1024/1024:.1f}MB）")
            done += 1
            continue

        t0 = time.time()
        ok = False
        for attempt in range(1, a.retries + 1):
            try:
                log.info(f"[{idx:02d}/{total}] ▶ 转换中（尝试 {attempt}/{a.retries}，"
                         f"{len(text)} 字符）：{title[:40]}")
                write_heartbeat(state="converting", total=total, current=idx,
                                title=title, attempt=attempt, out=a.out)
                convert_chapter_fast(a, idx, title, text, parser)
                f = chapter_output(a, idx, min_size)
                if f:
                    el = time.time() - t0
                    log.info(f"[{idx:02d}/{total}] ✅ 完成 {el/60:.1f} 分钟 "
                             f"({os.path.getsize(f)/1024/1024:.1f}MB)")
                    ok = True
                    break
                log.warning(f"[{idx:02d}/{total}] 未产出有效文件")
            except Exception as e:
                log.error(f"[{idx:02d}/{total}] 第 {attempt} 次失败: "
                          f"{type(e).__name__}: {str(e)[:200]}")
            if attempt < a.retries:
                d = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                log.info(f"[{idx:02d}/{total}] 等待 {d}s 后重试…")
                write_heartbeat(state="retry_wait", total=total, current=idx, delay=d)
                time.sleep(d)

        if ok:
            done += 1
        else:
            log.error(f"[{idx:02d}/{total}] ❌ 重试 {a.retries} 次仍失败，继续下一章")
            failed.append(idx)
            write_heartbeat(state="chapter_failed", total=total, current=idx)

    log.info("=" * 66)
    log.info(f"结束: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info(f"成功: {done} | 跳过: {skipped} | 失败: {len(failed)}")
    if failed:
        log.info(f"失败章节: {failed} —— 重跑本脚本会自动重试")
    log.info("=" * 66)
    write_heartbeat(state="finished", total=total, current=end,
                    done=done, skipped=skipped, failed=failed, out=a.out)

    # 有章节失败时用非零退出码，让调用方（和 docker run --rm）能判定失败
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
