# -*- coding: utf-8 -*-
"""离线特征索引：给每个文件算一个 CLIP 向量，供网页端做「角色候选」。

线上（app.py）不加载模型、不装 numpy，只按行号读 ml/vectors.bin 里的 float16，
再用纯 Python 点积算余弦。

本脚本可中断续跑：每处理完一批就把原始向量追加到 ml/_raw.*，
下次启动自动跳过已经算过的文件。全部跑完后才拟合白化、写正式索引。

用法：
    python embed.py                 # 增量/续跑
    python embed.py --rebuild       # 丢弃进度与旧索引，全量重算
    python embed.py --workers 6     # 并发进程数（默认 4；沙箱内需用 1）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

BASE = Path(r"F:\BA\表情包\.emoji-tool")
ROOT = Path(r"F:\BA\表情包")
ML = BASE / "ml"
MODEL = Path(os.environ.get("EMBED_MODEL", r"C:\Users\daoshiyige\WorkBuddy\emoji-tool\_diag\models\vision_model.onnx"))
SIZE = 224
RAW_DIM = 512
DIM = 128
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)

_SESS = None


def _init(model_path):
    global _SESS
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    _SESS = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])


def _encode(chunk):
    """chunk: [(rel, full_path)] -> [(rel, ndarray(512))]"""
    out, batch, rels = [], [], []
    for rel, full in chunk:
        try:
            with Image.open(full) as im:
                try:
                    im.seek(0)
                except Exception:
                    pass
                im = im.convert("RGB")
                w, h = im.size
                if w < 16 or h < 16:
                    raise ValueError("too small")
                s = SIZE / float(min(w, h))
                nw, nh = max(SIZE, int(round(w * s))), max(SIZE, int(round(h * s)))
                im = im.resize((nw, nh), Image.BILINEAR)
                l, t = (nw - SIZE) // 2, (nh - SIZE) // 2
                im = im.crop((l, t, l + SIZE, t + SIZE))
                a = (np.asarray(im, dtype=np.float32) / 255.0 - MEAN) / STD
        except Exception:
            continue
        batch.append(a)
        rels.append(rel)
        if len(batch) >= 12:
            arr = np.stack(batch).transpose(0, 3, 1, 2)
            v = _SESS.run(None, {_SESS.get_inputs()[0].name: arr})[0]
            out.extend(zip(rels, np.asarray(v, np.float32)))
            batch, rels = [], []
    if batch:
        arr = np.stack(batch).transpose(0, 3, 1, 2)
        v = _SESS.run(None, {_SESS.get_inputs()[0].name: arr})[0]
        out.extend(zip(rels, np.asarray(v, np.float32)))
    return out


def load_rels():
    con = sqlite3.connect("file:%s?mode=ro" % (BASE / "library.db"), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT rel, characters FROM files WHERE status='normal' AND folder <> '未处理' ORDER BY rel"
    ).fetchall()
    return [(r["rel"], (r["characters"] or "").split(",")[0].strip()) for r in rows]


def fit_whiten(X, dim=DIM):
    mu = X.mean(0)
    Xc = X - mu
    cov = Xc.T @ Xc / len(Xc)
    w, V = np.linalg.eigh(cov)
    order = np.argsort(-w)[:dim]
    return mu.astype(np.float32), V[:, order].astype(np.float32), (1.0 / np.sqrt(w[order] + 1e-6)).astype(np.float32)


def apply_whiten(X, mu, V, inv):
    Z = ((X - mu) @ V) * inv
    Z /= (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-8)
    return Z.astype(np.float32)


def load_raw():
    tp, bp = ML / "_raw.txt", ML / "_raw.bin"
    if not (tp.exists() and bp.exists()):
        return [], None
    rels = tp.read_text(encoding="utf-8").splitlines()
    arr = np.fromfile(bp, dtype=np.float16)
    if arr.size != len(rels) * RAW_DIM:
        print("!! 临时文件长度不符，忽略进度")
        return [], None
    return rels, arr.reshape(-1, RAW_DIM)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ML.mkdir(exist_ok=True)
    if args.rebuild:
        for n in ("_raw.txt", "_raw.bin", "index.txt", "vectors.bin", "proto.json", "whiten.bin"):
            p = ML / n
            if p.exists():
                p.unlink()
        print("已清空旧进度与索引")

    rels = load_rels()
    if args.limit:
        rels = rels[: args.limit]
    cls_of = dict(rels)
    all_rel = [r for r, _ in rels]

    done_rel, raw = load_raw()
    done_set = set(done_rel)
    todo = [(r, str(ROOT / r)) for r in all_rel if r not in done_set]
    print("库里 %d 个文件，已算 %d，本次需要 %d" % (len(all_rel), len(done_rel), len(todo)))

    if todo:
        t0 = time.time()
        chunks = [todo[i:i + 24] for i in range(0, len(todo), 24)]
        done = 0
        tp, bp = ML / "_raw.txt", ML / "_raw.bin"
        with open(tp, "a", encoding="utf-8") as ftxt, open(bp, "ab") as fbin:

            def consume(parts):
                nonlocal done
                for part in parts:
                    if not part:
                        continue
                    for r, _v in part:
                        ftxt.write(r + "\n")
                    np.stack([v for _, v in part]).astype(np.float16).tofile(fbin)
                    ftxt.flush()
                    fbin.flush()
                    prev, done = done, done + len(part)
                    if done // 960 != prev // 960:
                        el = time.time() - t0
                        print("  %d/%d  %.0fs  预计还要 %.0f 分钟" % (done, len(todo), el, el / done * (len(todo) - done) / 60), flush=True)

            if args.workers <= 1:
                _init(str(MODEL))
                consume(map(_encode, chunks))
            else:
                with Pool(args.workers, initializer=_init, initargs=(str(MODEL),)) as pool:
                    consume(pool.imap_unordered(_encode, chunks))
        print("特征提取完成，用时 %.1f 分钟" % ((time.time() - t0) / 60))

    done_rel, raw = load_raw()
    size_mb = (ML / "_raw.bin").stat().st_size / 1048576 if (ML / "_raw.bin").exists() else 0
    print("临时向量 %d 条（%.0f MB）" % (len(done_rel), size_mb))
    if not done_rel:
        print("没有任何特征")
        return 1

    X = raw.astype(np.float32)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-8)

    whiten_path = ML / "whiten.bin"
    if whiten_path.exists() and not args.rebuild:
        w = np.fromfile(whiten_path, dtype=np.float32)
        mu, V, inv = w[:RAW_DIM], w[RAW_DIM:RAW_DIM + RAW_DIM * DIM].reshape(RAW_DIM, DIM), w[RAW_DIM + RAW_DIM * DIM:]
        print("复用已有白化参数")
    else:
        print("拟合白化参数（%d 条样本）" % len(X))
        mu, V, inv = fit_whiten(X)
        np.concatenate([mu, V.reshape(-1), inv]).astype(np.float32).tofile(whiten_path)

    Z = apply_whiten(X, mu, V, inv)
    (ML / "index.txt").write_text("\n".join(done_rel) + "\n", encoding="utf-8")
    Z.astype(np.float16).tofile(ML / "vectors.bin")
    print("已写入 %d 行 × %d 维（%.1f MB）" % (len(done_rel), DIM, (ML / "vectors.bin").stat().st_size / 1048576))

    labels = np.array([cls_of.get(r, "") for r in done_rel])
    keep = labels != ""
    classes, centroids, counts = [], [], []
    for c in sorted(set(labels[keep].tolist())):
        m = labels == c
        if m.sum() < 3:
            continue
        v = Z[m].mean(0)
        v /= (np.linalg.norm(v) + 1e-8)
        classes.append(c)
        centroids.append([round(float(x), 5) for x in v])
        counts.append(int(m.sum()))
    (ML / "proto.json").write_text(json.dumps({
        "dim": DIM, "classes": classes, "centroids": centroids, "counts": counts,
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False), encoding="utf-8")
    print("角色质心 %d 个，覆盖 %d 张图" % (len(classes), int(sum(counts))))

    for n in ("_raw.txt", "_raw.bin"):
        try:
            (ML / n).unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
