# -*- coding: utf-8 -*-
"""把「运行数据」从工程目录搬到数据目录，把「废纸篓」搬到库根 .trash。

分离后的三层：
    工程目录（本文件所在）     app.py / static / templates / ml/*.py / *.md / 两个 *_extra.json
    数据目录（data_dir）       library.db / cache / ml 下的模型与索引 / 日志
    库目录（root）             图片本体 + .trash

默认只打印清单，不动任何文件；确认后加 --apply 才执行。
目标位置已存在的东西一律跳过（不覆盖、不合并），所以脚本可以反复跑。

用法：
    python tools/migrate_data.py                # 只看清单
    python tools/migrate_data.py --apply        # 真的搬
    python tools/migrate_data.py --apply --skip-trash
"""
import argparse
import json
import os
import shutil
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# 数据目录里的内容（相对工程目录 → 相对数据目录）
DATA_ITEMS = [
    "library.db", "library.db-wal", "library.db-shm",
    "cache",
    "moves.log",
    "server.out.log", "server.err.log",
]

# 用 glob 匹配的（数据库历史备份，属于数据不属于代码）
DATA_GLOBS = ["library.db.bak_*"]

# ml/ 下属于「数据」的部分；ml/*.py 是代码，留在工程里
ML_ITEMS = [
    "models",
    "vectors.bin", "index.txt", "whiten.bin", "proto.json",
    "phash.json", "dupes.json", "dup_skip.json",
    "dupe_batch.json", "dupe_batch.stop",
    "embed.log", "dupes.log", "dupes_batch.log",
    "_raw.bin", "_raw.txt",
]


def merged_cfg():
    cfg = {}
    for name in ("config.json", "config.local.json"):
        try:
            cfg.update(json.loads((BASE / name).read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def abs_path(raw, base):
    txt = str(raw or "").strip()
    if not txt:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(txt)))
    return (p if p.is_absolute() else base / p).resolve()


def measure(p):
    """返回 (文件数, 字节数)。"""
    if p.is_file():
        try:
            return 1, p.stat().st_size
        except OSError:
            return 1, 0
    n = total = 0
    for dirpath, _dirnames, filenames in os.walk(p):
        for fn in filenames:
            n += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return n, total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def build_plan(cfg, skip_trash=False):
    data_dir = abs_path(cfg.get("data_dir"), BASE)
    root = abs_path(cfg.get("root"), BASE)
    if data_dir is None:
        raise SystemExit("config.local.json 里没有 data_dir，先填上再跑。")
    if root is None:
        raise SystemExit("配置里没有 root。")

    trash_dir = abs_path(cfg.get("trash_dir"), root) or (root / ".trash")

    plan = []
    for rel in DATA_ITEMS:
        plan.append({"src": BASE / rel, "dst": data_dir / rel,
                     "label": "数据", "what": rel})
    for pat in DATA_GLOBS:
        for p in sorted(BASE.glob(pat)):
            plan.append({"src": p, "dst": data_dir / p.name,
                         "label": "数据", "what": p.name})
    for rel in ML_ITEMS:
        plan.append({"src": BASE / "ml" / rel, "dst": data_dir / "ml" / rel,
                     "label": "ML", "what": "ml/" + rel})
    if not skip_trash:
        plan.append({"src": BASE / "trash", "dst": trash_dir,
                     "label": "废纸篓", "what": "trash → .trash"})
    return data_dir, root, trash_dir, plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行（默认只打印）")
    ap.add_argument("--skip-trash", action="store_true", help="不动废纸篓")
    args = ap.parse_args()

    cfg = merged_cfg()
    data_dir, root, trash_dir, plan = build_plan(cfg, args.skip_trash)

    print("工程目录 : %s" % BASE)
    print("数据目录 : %s%s" % (data_dir, "" if data_dir.exists() else "  （还不存在，会新建）"))
    print("库目录   : %s" % root)
    print("废纸篓   : %s" % trash_dir)
    print("-" * 72)

    todo, skip, total_n, total_b = [], [], 0, 0
    for it in plan:
        src, dst = it["src"], it["dst"]
        if not src.exists():
            continue
        n, b = measure(src)
        it["n"], it["b"] = n, b
        if dst.exists():
            it["why"] = "目标已存在"
            skip.append(it)
            continue
        total_n += n
        total_b += b
        todo.append(it)

    print("%-10s %-34s %6s %10s" % ("类别", "项目", "文件", "体积"))
    for it in todo:
        print("%-10s %-34s %6d %10s" % (it["label"], it["what"], it["n"], human(it["b"])))
    if skip:
        print("-" * 72)
        print("跳过（目标已有同名项，不覆盖）：")
        for it in skip:
            print("  %-34s %s" % (it["what"], it["dst"]))
    print("-" * 72)
    print("合计：%d 个文件 / %s，%d 项待搬" % (total_n, human(total_b), len(todo)))

    if not args.apply:
        print()
        print("这是 dry-run，什么都没动。确认无误后执行：")
        print("    python tools/migrate_data.py --apply")
        return 0

    if not todo:
        print("没有需要搬的东西。")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "base": str(BASE),
                "data_dir": str(data_dir), "items": []}
    ok = fail = 0
    for it in todo:
        src, dst = it["src"], it["dst"]
        print("搬运 %-34s → %s" % (it["what"], dst), flush=True)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except Exception as exc:  # noqa: BLE001
            print("   失败：%s" % exc, flush=True)
            manifest["items"].append({"what": it["what"], "src": str(src),
                                      "dst": str(dst), "ok": False, "error": str(exc)})
            fail += 1
            continue
        good = dst.exists()
        n2, b2 = measure(dst) if good else (0, 0)
        same = good and n2 == it["n"] and b2 == it["b"]
        if not same:
            print("   校验不一致：期望 %d 个文件/%s，实际 %d 个/%s"
                  % (it["n"], human(it["b"]), n2, human(b2)), flush=True)
            fail += 1
        else:
            ok += 1
        manifest["items"].append({"what": it["what"], "src": str(src), "dst": str(dst),
                                  "files": it["n"], "bytes": it["b"], "ok": same})

    man_path = data_dir / ("migrate_manifest_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("清单已写入 %s" % man_path)
    except OSError as exc:
        print("清单写入失败：%s" % exc)

    print("-" * 72)
    print("完成：成功 %d 项，失败 %d 项" % (ok, fail))
    print("接下来：重启服务（启动网页.bat），然后进任意角色确认图片和废纸篓都在。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
