# -*- coding: utf-8 -*-
"""相似图检测（单角色，手动触发）。

思路参考 `工具箱套件/图片相似比对` 模式三：pHash + 同一沙盒内两两比汉明距离。
本项目的差异：
  - 沙盒 = 角色（学院/角色），不是顶层目录；跨角色比没有意义
  - 加一道 CLIP 余弦复核（复用 ml/vectors.bin），挡掉「大片留白 + 小图」的误报
  - 过滤词（默认「差分」）：一对图**两侧都命中**任一过滤词就跳过
    （差分本就是同一张底图换表情，互比是无意义的噪音；差分 vs 原图仍然会检出）

产物：
  ml/phash.json   { rel: [hash, mtime, size] }   哈希缓存，增量只算新的
  ml/dupes.json   { char: {built_at, th, cos, groups:[{id, files, pairs}]} }
  ml/dup_skip.json { char: [group_id, ...] }      人工忽略过的组

用法：
    python dupes.py --char "格黑娜/日奈"
    python dupes.py --char "格黑娜/日奈" --th 8 --cos 0.85
    python dupes.py --char "格黑娜/日奈" --skip-words "差分,线稿"   # 空串 = 不过滤
    python dupes.py --list                 # 列出各角色已算情况
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

BASE = Path(os.environ.get("EMOJI_BASE", str(Path(__file__).resolve().parent.parent)))


def _cfg_all():
    """config.json（默认值）+ config.local.json（本机覆盖）合并。"""
    cfg = {}
    for name in ("config.json", "config.local.json"):
        try:
            cfg.update(json.loads((BASE / name).read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _data_dir():
    """运行数据目录：EMOJI_DATA > config 的 data_dir > 项目目录（和 app.py 同规则）。"""
    raw = os.environ.get("EMOJI_DATA") or str(_cfg_all().get("data_dir") or "").strip()
    if not raw:
        return BASE
    p = Path(raw)
    if not p.is_absolute():
        p = BASE / p
    return p


def _cfg_root():
    return str(_cfg_all().get("root") or "").strip()


DATA = _data_dir()
_root_raw = os.environ.get("EMOJI_ROOT") or _cfg_root()
if not _root_raw:
    raise SystemExit("没有配置库路径：在 config.local.json 里设 root，或设环境变量 EMOJI_ROOT")
ROOT = Path(_root_raw)
ML = DATA / "ml"
PHASH_JSON = ML / "phash.json"
DUPES_JSON = ML / "dupes.json"
SKIP_JSON = ML / "dup_skip.json"
INDEX = ML / "index.txt"
VECTORS = ML / "vectors.bin"
DIM = 128

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}


# ---------------------------------------------------------------- pHash
def _dct_mat(n=32):
    k = np.arange(n)[:, None]
    x = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] *= 1 / np.sqrt(2.0)
    return m


DCT = _dct_mat()


def phash(full):
    """64 bit 感知哈希。GIF 只取第 0 帧（取中间帧要解码整段，慢 80 倍）。"""
    try:
        with Image.open(full) as im:
            if im.format == "JPEG":
                im.draft("L", (64, 64))      # 降采样解码，快 4 倍
            a = np.asarray(im.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float32)
    except Exception:
        return None
    d = DCT @ a @ DCT.T
    low = d[:8, :8].reshape(-1)
    bits = low > np.median(low[1:])
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def popcount(x):
    x = x.astype(np.uint64)
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return (x * np.uint64(0x0101010101010101)) >> np.uint64(56)


# ---------------------------------------------------------------- 数据
def char_key(folder):
    parts = (folder or "").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else (folder or "")


def load_files(char=None):
    con = sqlite3.connect("file:%s?mode=ro" % (DATA / "library.db").as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT rel, folder, name, size, mtime FROM files WHERE status='normal'").fetchall()
    out = []
    for r in rows:
        k = char_key(r["folder"])
        if char and k != char:
            continue
        if os.path.splitext(r["name"])[1].lower() not in IMG_EXTS:
            continue
        out.append({"rel": r["rel"], "char": k, "name": r["name"], "size": r["size"], "mtime": r["mtime"]})
    return out


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p, data):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def load_clip():
    """返回 (rows_index, 向量矩阵) —— 量化索引与 CLIP 索引行号对齐。"""
    if not (INDEX.exists() and VECTORS.exists()):
        return None, None
    rows = INDEX.read_text(encoding="utf-8").splitlines()
    arr = np.fromfile(VECTORS, dtype=np.float16)
    if arr.size != len(rows) * DIM:
        return None, None
    return {r: i for i, r in enumerate(rows)}, arr.reshape(-1, DIM).astype(np.float32)


def file_md5(full):
    h = hashlib.md5()
    try:
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def group_id(rels):
    h = hashlib.blake2b("|".join(sorted(rels)).encode("utf-8"), digest_size=8).hexdigest()
    return h


# ---------------------------------------------------------------- 检测
def split_words(raw):
    """把 "差分，线稿 线稿" 这类输入切成干净的词表（兼容中英文分隔符）。"""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,，、;；/\s]+", str(raw or ""))
    out = []
    for w in parts:
        w = w.strip().lower()
        if w and w not in out:
            out.append(w)
    return out


def hits(rel, words):
    s = (rel or "").lower()
    return any(w in s for w in words)


def file_sig(files, th, cos_th, words, skip_mode="pair"):
    """签名 = 文件集合 + 检测参数。只有它变了才值得重跑（静默检测用它跳过）。"""
    if not files:
        return {"n": 0, "mt": 0, "th": th, "cos": cos_th, "w": "|".join(words), "m": skip_mode}
    return {
        "n": len(files),
        "mt": int(max(f["mtime"] for f in files)),
        "th": th, "cos": cos_th, "w": "|".join(words), "m": skip_mode,
    }


def scan_char(char, th=8, cos_th=0.85, workers=8, skip_words=("差分",), skip_mode="pair",
              files=None, cache=None, clip=None, save_cache=True, quiet=False):
    """扫一个角色。

    files / cache / clip 可由批量入口预先加载后传进来，省掉反复读库、读 JSON、读向量；
    save_cache=False 时哈希缓存不落盘（批量跑完统一写，避免每个角色都写几 MB）。

    skip_mode:
      file —— 命中过滤词的文件不参与检测（结果里彻底看不到它们）
      pair —— 只有一对图两侧都命中才跳过互比（「差分 vs 原图」这种真重复仍会报）
    """
    files = list(files) if files is not None else [f for f in load_files(char)]
    words = [w.lower() for w in (skip_words or []) if w]
    skip_mode = "file" if skip_mode == "file" else "pair"
    excluded = 0
    if words and skip_mode == "file":
        kept = [f for f in files if not hits(f["rel"], words)]
        excluded = len(files) - len(kept)
        files = kept
    sig = file_sig(files, th, cos_th, words, skip_mode)

    def empty():
        return {"char": char, "groups": [], "built_at": time.strftime("%Y-%m-%d %H:%M"),
                "th": th, "cos": cos_th, "files": len(files), "skip_words": words,
                "skip_mode": skip_mode, "excluded": excluded, "sig": sig}

    if len(files) < 2:
        return empty()

    cache = cache if cache is not None else load_json(PHASH_JSON, {})
    need = [f for f in files if not (cache.get(f["rel"]) and cache[f["rel"]][1] == f["mtime"])]
    t0 = time.time()
    if need:
        def one(f):
            return f["rel"], phash(str(ROOT / f["rel"])), f["mtime"], f["size"]

        with ThreadPoolExecutor(workers) as ex:
            for rel, h, mt, sz in ex.map(one, need):
                if h is not None:
                    cache[rel] = [h, mt, sz]
        if save_cache:
            save_json(PHASH_JSON, cache)
        if not quiet:
            print("  新增/更新哈希 %d 张，用时 %.1f 秒" % (len(need), time.time() - t0), flush=True)

    items = [(f["rel"], cache[f["rel"]][0]) for f in files if f["rel"] in cache]
    if len(items) < 2:
        return empty()

    row_of, Z = clip if clip else load_clip()
    rels = [r for r, _ in items]
    arr = np.array([h for _, h in items], dtype=np.uint64)
    if row_of is not None:
        keep = [i for i, r in enumerate(rels) if r in row_of]
        V = np.zeros((len(rels), DIM), dtype=np.float32)
        for i in keep:
            V[i] = Z[row_of[rels[i]]]
        has_vec = np.zeros(len(rels), dtype=bool)
        has_vec[keep] = True
    else:
        V = np.zeros((len(rels), DIM), dtype=np.float32)
        has_vec = np.zeros(len(rels), dtype=bool)

    n = len(rels)
    idx = np.arange(n)
    pairs = []
    step = 1024
    for s in range(0, n, step):
        e = min(s + step, n)
        d = popcount(arr[s:e, None] ^ arr[None, :]).astype(np.int32)
        hit = (d <= th) & (idx[None, :] > idx[s:e, None])
        ri, ci = np.nonzero(hit)
        if not ri.size:
            continue
        cos = V[s:e] @ V.T
        for a, b in zip(ri, ci):
            i, j = int(a + s), int(b)
            if words and hits(rels[i], words) and hits(rels[j], words):
                continue
            c = float(cos[a, b]) if (has_vec[i] and has_vec[j]) else None
            if c is not None and c < cos_th:
                continue
            pairs.append((i, j, int(d[a, b]), c))
    print("  阈值 %d / CLIP≥%.2f：命中 %d 对（候选阶段 %d 张）" % (th, cos_th, len(pairs), n), flush=True)

    # 精确重复：同大小才可能字节相同，只对这些算 md5（很快）
    pos = {r: i for i, r in enumerate(rels)}
    by_size = {}
    for f in files:
        if f["rel"] in pos:
            by_size.setdefault(f["size"], []).append(f["rel"])
    md5_pairs = []
    for sz, group in by_size.items():
        if len(group) < 2:
            continue
        by_hash = {}
        for r in group:
            h = file_md5(str(ROOT / r))
            if h:
                by_hash.setdefault(h, []).append(r)
        for same in by_hash.values():
            for k in range(1, len(same)):
                i, j = pos[same[0]], pos[same[k]]
                # md5 连起来的也守同一条规则，否则「完全相同的两张差分图」照样成组
                if skip_mode == "pair" and words and hits(rels[i], words) and hits(rels[j], words):
                    continue
                md5_pairs.append((i, j))
    if md5_pairs:
        print("  完全相同的对：%d" % len(md5_pairs), flush=True)

    # 并查集分组
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _d, _c in pairs:
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b
    exact_set = set()
    for i, j in md5_pairs:
        exact_set.add((min(i, j), max(i, j)))
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b
    comp = {}
    for i in range(n):
        comp.setdefault(find(i), []).append(i)

    groups = []
    for root, members in comp.items():
        if len(members) < 2:
            continue
        member_set = set(members)
        sub = [(i, j, d, c) for (i, j, d, c) in pairs if i in member_set]
        g_rels = [rels[i] for i in members]
        exact = any((min(i, j), max(i, j)) in exact_set for i, j in [(x, y) for x, y, _d, _c in sub]) or any(
            (min(i, j), max(i, j)) in exact_set for i, j in md5_pairs if i in member_set)
        groups.append({
            "id": group_id(g_rels),
            "exact": bool(exact),
            "files": g_rels,
            "pairs": [{"a": rels[i], "b": rels[j], "d": d, "cos": (round(c, 3) if c is not None else None)} for i, j, d, c in sub],
            "min_d": (min(p[2] for p in sub) if sub else 0),
            "max_cos": max((p[3] for p in sub if p[3] is not None), default=None),
        })
    groups.sort(key=lambda g: (not g["exact"], -len(g["files"]), g["min_d"]))

    return {
        "char": char, "built_at": time.strftime("%Y-%m-%d %H:%M"), "th": th, "cos": cos_th,
        "files": len(files), "skip_words": words, "skip_mode": skip_mode,
        "excluded": excluded, "groups": groups, "sig": sig,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="")
    ap.add_argument("--th", type=int, default=8)
    ap.add_argument("--cos", type=float, default=0.85)
    ap.add_argument("--skip-words", default="差分", help="逗号分隔；命中过滤词的文件怎么处理见 --skip-mode")
    ap.add_argument("--skip-mode", default="pair", choices=["pair", "file"],
                    help="pair=两侧都命中才跳过互比；file=命中即不参与检测")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("DUPE_WORKERS", "8")))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    ML.mkdir(exist_ok=True)
    if args.list:
        data = load_json(DUPES_JSON, {})
        for k, v in data.items():
            print("%-24s %4d 组  %s" % (k, len(v.get("groups") or []), v.get("built_at", "")))
        return 0

    if not args.char:
        print("需要 --char（或 --list）")
        return 2

    res = scan_char(args.char, th=args.th, cos_th=args.cos, workers=args.workers,
                    skip_words=split_words(args.skip_words), skip_mode=args.skip_mode)
    data = load_json(DUPES_JSON, {})
    data[args.char] = res
    save_json(DUPES_JSON, data)
    print("完成：%s，%d 张图（排除 %d），%d 组（过滤词：%s / %s）" % (
        args.char, res.get("files", 0), res.get("excluded", 0), len(res["groups"]),
        "、".join(res.get("skip_words") or []) or "无", args.skip_mode), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
