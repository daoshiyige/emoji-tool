# -*- coding: utf-8 -*-
"""全库静默检测：按角色逐个跑相似图检测，只补「有变化」的角色。

设计要点（对齐「静默 · 不影响使用」）：
  - 进程压到 IDLE 优先级，线程数默认 3，角色之间让出 CPU（--gap）
  - 复用 ml/phash.json 的 mtime 缓存与 ml/vectors.bin，只算新图
  - 每个角色带签名（文件数 + 最新 mtime + 阈值/相似度/过滤词），签名没变直接跳过
  - 进度写 ml/dupe_batch.json（网页轮询显示）；出现 ml/dupe_batch.stop 就收尾退出
  - 哈希缓存 / dupes.json 每若干个角色或 45 秒才落盘一次，避免反复写十几 MB

用法：
    python dupes_batch.py                  # 增量：只跑新增/改过的角色
    python dupes_batch.py --all            # 全部重跑
    python dupes_batch.py --limit 20       # 只跑前 20 个（试水）
    python dupes_batch.py --char "格黑娜/日奈"
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dupes  # noqa: E402

ML = dupes.ML
STATE = ML / "dupe_batch.json"
STOP = ML / "dupe_batch.stop"


def low_priority():
    """Windows 下把本进程降到空闲优先级，别的程序先用 CPU。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000040)  # IDLE_PRIORITY_CLASS
        return True
    except Exception:
        return False


def write_state(st):
    st["updated"] = time.time()
    dupes.save_json(STATE, st)


def start_heartbeat():
    """大角色一次要跑几分钟。定时 touch 状态文件，让网页知道「还活着」。"""
    def beat():
        while True:
            time.sleep(20)
            try:
                if STATE.exists():
                    os.utime(STATE, None)
            except Exception:
                pass

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return t


def load_cfg():
    try:
        cfg = json.loads((dupes.BASE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    th = int(cfg.get("dupe_threshold") or 8)
    cos = float(cfg.get("dupe_cos") or 0.85)
    words = dupes.split_words(cfg.get("dupe_skip_words", "差分"))
    mode = cfg.get("dupe_skip_mode", "file")
    return th, cos, words, ("file" if mode == "file" else "pair")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="")
    ap.add_argument("--all", action="store_true", help="忽略签名，全部重跑")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--th", type=int, default=None)
    ap.add_argument("--cos", type=float, default=None)
    ap.add_argument("--skip-words", default=None)
    ap.add_argument("--skip-mode", default=None, choices=["pair", "file"])
    ap.add_argument("--workers", type=int, default=int(os.environ.get("DUPE_WORKERS", "3")))
    ap.add_argument("--gap", type=float, default=float(os.environ.get("DUPE_GAP", "0.6")))
    args = ap.parse_args()

    d_th, d_cos, d_words, d_mode = load_cfg()
    th = args.th if args.th is not None else d_th
    cos = args.cos if args.cos is not None else d_cos
    words = dupes.split_words(args.skip_words) if args.skip_words is not None else d_words
    mode = args.skip_mode or d_mode

    ML.mkdir(exist_ok=True)
    if STOP.exists():
        STOP.unlink()
    low = low_priority()

    st = {"running": True, "pid": os.getpid(), "phase": "prepare", "char": "",
          "done": 0, "total": 0, "skipped": 0, "groups": 0, "files": 0,
          "started": time.time(), "updated": time.time(), "low_priority": low,
          "th": th, "cos": cos, "words": words, "mode": mode, "stopped": False, "error": ""}
    write_state(st)
    start_heartbeat()

    try:
        allf = dupes.load_files()
        groups = {}
        for f in allf:
            groups.setdefault(f["char"], []).append(f)

        data = dupes.load_json(dupes.DUPES_JSON, {})
        cache = dupes.load_json(dupes.PHASH_JSON, {})
        clip = dupes.load_clip()

        chars = [args.char] if args.char else sorted(groups, key=lambda c: len(groups[c]))
        todo, skipped = [], 0
        for c in chars:
            files = groups.get(c) or []
            if len(files) < 2:
                skipped += 1
                continue
            sig = dupes.file_sig(files, th, cos, words, mode)
            prev = data.get(c) or {}
            if not args.all and prev.get("built_at") and prev.get("sig") == sig:
                skipped += 1
                continue
            todo.append(c)
        if args.limit:
            todo = todo[:args.limit]

        st.update({"phase": "scan", "total": len(todo), "skipped": skipped})
        write_state(st)
        print("待检测 %d 个角色（跳过 %d 个无变化）" % (len(todo), skipped), flush=True)

        t0 = time.time()
        last_flush = time.time()
        done = 0
        total_groups = 0
        total_files = 0
        stopped = False

        for i, c in enumerate(todo):
            if STOP.exists():
                stopped = True
                break
            st["char"] = c
            st["done"] = i
            write_state(st)

            try:
                res = dupes.scan_char(c, th=th, cos_th=cos, workers=args.workers, skip_words=words, skip_mode=mode,
                                      files=groups.get(c), cache=cache, clip=clip,
                                      save_cache=False, quiet=True)
            except Exception as e:  # noqa: BLE001 单个角色炸了不影响整批
                print("[跳过] %s: %s" % (c, e), flush=True)
                st["error"] = "%s: %s" % (c, e)
                continue

            data[c] = res
            total_groups += len(res.get("groups") or [])
            total_files += res.get("files") or 0
            done += 1
            st.update({"done": done, "groups": total_groups, "files": total_files})
            print("[%d/%d] %s → %d 张 / %d 组" % (done, len(todo), c, res.get("files") or 0, len(res.get("groups") or [])), flush=True)

            if time.time() - last_flush > 45 or done % 8 == 0:
                dupes.save_json(dupes.DUPES_JSON, data)
                dupes.save_json(dupes.PHASH_JSON, cache)
                last_flush = time.time()
            if args.gap:
                time.sleep(args.gap)

        dupes.save_json(dupes.DUPES_JSON, data)
        dupes.save_json(dupes.PHASH_JSON, cache)

        st.update({
            "running": False, "phase": "stopped" if stopped else "done",
            "done": done, "stopped": stopped, "char": "",
            "elapsed": round(time.time() - t0, 1),
        })
        write_state(st)
        print("完成：%d 个角色，%d 组，用时 %.1f 分钟%s" % (
            done, total_groups, (time.time() - t0) / 60.0, "（已停止）" if stopped else ""), flush=True)
        return 0
    except Exception as e:  # noqa: BLE001
        st.update({"running": False, "phase": "error", "error": str(e)})
        write_state(st)
        raise


if __name__ == "__main__":
    sys.exit(main())
