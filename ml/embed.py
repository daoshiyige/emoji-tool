# -*- coding: utf-8 -*-
"""离线特征索引：给每个文件算一个 CLIP 向量，供网页端做「角色候选」。

线上（app.py）不加载模型、不装 numpy，只按行号读 ml/vectors.bin 里的 float16，
再用纯 Python 点积算余弦。

索引分两层：
  ml/vectors.bin  已白化的 128 维向量（float16，行号 = index.txt 行号）
  ml/index.txt    每行一个 rel
  ml/whiten.bin   白化参数（首跑拟合，之后固定复用）

增量：只给「索引里还没有的行」算特征，算完追加到 index.txt / vectors.bin，
不需要保留 512 维原始向量，也不用重算全库。

用法：
    python embed.py                 # 增量（新增图片后跑这个，几秒到几分钟）
    python embed.py --rebuild       # 全量重建（换模型时）
    python embed.py --workers 6     # 并发进程数（默认 4；沙箱内需用 1）
    python embed.py --status        # 只打印状态，不算
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

BASE = Path(os.environ.get("EMOJI_BASE", str(Path(__file__).resolve().parent.parent)))
def _cfg_root():
    try:
        import json as _json
        cfg = _json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        return str(cfg.get("root") or "").strip()
    except Exception:
        return ""


ROOT = Path(os.environ.get("EMOJI_ROOT") or _cfg_root() or str(BASE.parent))
ML = BASE / "ml"
MODEL = Path(os.environ.get("EMBED_MODEL", str(ML / "models" / "vision_model.onnx")))
SIZE = 224
RAW_DIM = 512
DIM = 128
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
FIT_SAMPLE = 3000          # 首次拟合白化用的样本上限

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


IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}


def load_rels():
    """库里 normal 的文件 + 「未处理」目录里还没入库的文件。

    未处理的图在归位前根本不在 files 表里（/api/inbox 是直接读盘的），
    所以必须单独补进来，否则它们永远没有向量、也永远没有候选。
    """
    con = sqlite3.connect("file:%s?mode=ro" % (BASE / "library.db"), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT rel, characters FROM files WHERE status='normal' ORDER BY rel").fetchall()
    out = [(r["rel"], (r["characters"] or "").split(",")[0].strip()) for r in rows]

    inbox = ROOT / os.environ.get("EMOJI_INBOX", "未处理")
    if inbox.is_dir():
        for fn in sorted(os.listdir(inbox)):
            full = inbox / fn
            if full.is_file() and full.suffix.lower() in IMG_EXTS:
                rel = full.relative_to(ROOT).as_posix()
                out.append((rel, ""))
    return out


def fit_whiten(X, dim=DIM):
    mu = X.mean(0)
    Xc = X - mu
    cov = Xc.T @ Xc / len(Xc)
    w, V = np.linalg.eigh(cov)
    order = np.argsort(-w)[:dim]
    return mu.astype(np.float32), V[:, order].astype(np.float32), (1.0 / np.sqrt(w[order] + 1e-6)).astype(np.float32)


def apply_whiten(X, mu, V, inv):
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-8)
    Z = ((X - mu) @ V) * inv
    Z /= (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-8)
    return Z.astype(np.float32)


def load_whiten():
    p = ML / "whiten.bin"
    if not p.exists():
        return None
    w = np.fromfile(p, dtype=np.float32)
    if w.size != RAW_DIM + RAW_DIM * DIM + DIM:
        return None
    return w[:RAW_DIM], w[RAW_DIM:RAW_DIM + RAW_DIM * DIM].reshape(RAW_DIM, DIM), w[RAW_DIM + RAW_DIM * DIM:]


def load_index():
    ip, vp = ML / "index.txt", ML / "vectors.bin"
    if not (ip.exists() and vp.exists()):
        return [], None
    rels = ip.read_text(encoding="utf-8").splitlines()
    arr = np.fromfile(vp, dtype=np.float16)
    if arr.size != len(rels) * DIM:
        print("!! 索引行数不一致（%d vs %d），忽略旧索引" % (arr.size // DIM, len(rels)))
        return [], None
    return rels, arr.reshape(-1, DIM).astype(np.float32)


def encode_all(todo):
    """编码 todo 里的文件，返回 (rels, raw float16 矩阵)。中断可续（写 _new.*）。"""
    tp, bp = ML / "_new.txt", ML / "_new.bin"
    got_rel = []
    if tp.exists() and bp.exists():
        got_rel = tp.read_text(encoding="utf-8").splitlines()
        if np.fromfile(bp, dtype=np.float16).size != len(got_rel) * RAW_DIM:
            print("!! 临时文件长度不符，丢弃")
            got_rel = []
    done = set(got_rel)
    todo = [(r, p) for r, p in todo if r not in done]
    if not todo:
        return got_rel, np.fromfile(bp, dtype=np.float16).reshape(-1, RAW_DIM)

    t0 = time.time()
    chunks = [todo[i:i + 24] for i in range(0, len(todo), 24)]
    total, done_n = len(todo), 0

    def consume(parts):
        nonlocal done_n
        with open(tp, "a", encoding="utf-8") as ftxt, open(bp, "ab") as fbin:
            for part in parts:
                if not part:
                    continue
                for r, _v in part:
                    ftxt.write(r + "\n")
                np.stack([v for _, v in part]).astype(np.float16).tofile(fbin)
                ftxt.flush()
                fbin.flush()
                prev, done_n = done_n, done_n + len(part)
                if done_n // 480 != prev // 480:
                    el = time.time() - t0
                    left = el / done_n * (total - done_n) / 60
                    print("  %d/%d  %.0fs  预计还要 %.1f 分钟" % (done_n, total, el, left), flush=True)

    workers = int(os.environ.get("EMBED_WORKERS", "4"))
    if workers <= 1:
        _init(str(MODEL))
        consume(map(_encode, chunks))
    else:
        with Pool(workers, initializer=_init, initargs=(str(MODEL),)) as pool:
            consume(pool.imap_unordered(_encode, chunks))
    print("特征提取完成，用时 %.1f 分钟" % ((time.time() - t0) / 60,))

    got_rel = tp.read_text(encoding="utf-8").splitlines()
    got_raw = np.fromfile(bp, dtype=np.float16).reshape(-1, RAW_DIM)
    return got_rel, got_raw


def write_proto(Z, rels, cls_of):
    labels = np.array([cls_of.get(r, "") for r in rels])
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
    return len(classes), int(sum(counts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.workers:
        os.environ["EMBED_WORKERS"] = str(args.workers)

    ML.mkdir(exist_ok=True)
    if args.rebuild:
        for n in ("index.txt", "vectors.bin", "proto.json", "whiten.bin", "_new.txt", "_new.bin", "raw.txt", "raw.bin"):
            p = ML / n
            if p.exists():
                p.unlink()
        print("已清空旧索引与白化参数")

    rels = load_rels()
    if args.limit:
        rels = rels[: args.limit]
    cls_of = dict(rels)
    all_rel = [r for r, _ in rels]

    idx_rel, Z_old = load_index()
    if idx_rel and Z_old is not None:
        keep = np.array([(ROOT / r).exists() for r in idx_rel])
        if not keep.all():
            idx_rel = [r for r, k in zip(idx_rel, keep) if k]
            Z_old = Z_old[keep]
    have = set(idx_rel)
    todo = [(r, str(ROOT / r)) for r in all_rel if r not in have]

    if args.status:
        print(json.dumps({
            "db": len(all_rel), "indexed": len(idx_rel), "missing": len(todo),
            "proto": (ML / "proto.json").exists(),
        }, ensure_ascii=False))
        return 0

    print("库里 %d 个文件，已索引 %d，本次需要 %d" % (len(all_rel), len(idx_rel), len(todo)))

    wh = load_whiten()
    if wh is None:
        # 首次：必须全量算 raw 才能拟合白化（只用新图拟合会让空间偏移）
        print("没有白化参数，全量计算并拟合")
        got_rel, got_raw = encode_all([(r, str(ROOT / r)) for r in all_rel])
        X = got_raw.astype(np.float32)
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-8)
        sample = Xn if len(Xn) <= FIT_SAMPLE else Xn[np.random.default_rng(0).choice(len(Xn), FIT_SAMPLE, replace=False)]
        mu, V, inv = fit_whiten(sample)
        np.concatenate([mu, V.reshape(-1), inv]).astype(np.float32).tofile(ML / "whiten.bin")
        Z = apply_whiten(X, mu, V, inv)
        out_rel = got_rel
    else:
        mu, V, inv = wh
        if todo:
            new_rel, new_raw = encode_all(todo)
            new_Z = apply_whiten(new_raw.astype(np.float32), mu, V, inv) if new_rel else None
        else:
            new_rel, new_Z = [], None
            print("索引已是最新，只刷新质心")
        parts = [p for p in (Z_old, new_Z) if p is not None]
        if not parts:
            print("没有任何向量可写")
            return 1
        Z = np.concatenate(parts, 0)
        out_rel = idx_rel + new_rel
        if not todo:
            Z, out_rel = Z_old, idx_rel

    (ML / "index.txt").write_text("\n".join(out_rel) + "\n", encoding="utf-8")
    Z.astype(np.float16).tofile(ML / "vectors.bin")
    print("已写入 %d 行 × %d 维（%.1f MB）" % (len(out_rel), DIM, (ML / "vectors.bin").stat().st_size / 1048576))

    n, c = write_proto(Z, out_rel, cls_of)
    print("角色质心 %d 个，覆盖 %d 张图" % (n, c))

    for n_ in ("_new.txt", "_new.bin", "raw.txt", "raw.bin"):
        p = ML / n_
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
