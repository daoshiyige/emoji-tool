# -*- coding: utf-8 -*-
r"""表情包本地整理工具 (Demo)
基于 Flask 的本地网页：扫描、打标、筛选、预览、重命名、废纸篓。
运行:  .emoji-tool\venv\Scripts\python .emoji-tool\app.py
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image
try:
    from pypinyin import Style, lazy_pinyin
    _HAS_PINYIN = True
except Exception:  # 未安装 pypinyin 时降级为不支持拼音
    _HAS_PINYIN = False

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "root": r"F:\BA\表情包",
    "exclude_dirs": ["杂图", "未处理"],
    "exclude_segments": ["语音"],
    "image_exts": [".jpg", ".jpeg", ".png", ".gif", ".webp"],
    "special_folders": ["差分", "铸币大头", "圣诞特供"],
    "pair_characters": {
        "桃绿": ["桃", "绿", "桃绿"],
        "光望": ["光", "望", "光望"],
    },
    "multi_academy_characters": ["杂鱼酱"],
    "artist_min_files": 3,
    "artist_threshold_ratio": 0.1,
    "artist_threshold_min": 3,
    "artist_threshold_mid1": 3.5,
    "artist_threshold_mid": 4,
    "artist_threshold_mid2": 4.5,
    "artist_threshold_split1": 300,
    "artist_threshold_split2": 500,
    "artist_threshold_split3": 600,
    "artist_weight_gif": 1.5,
    "artist_weight_gif_large": 2.0,
    "artist_weight_gif_large_mb": 5,
    "artist_folder_weight_cap": 8,
    "artist_folder_weight_ratio": 0.4,
    "port": 5178,
    "host": "127.0.0.1",
    # 差分内 N（X） 中 X 属于“类型”而非画师的名字（事件/画风/来源等）
    "diff_type_denylist": ["未知", "古书馆", "AI", "AI差分", "官方", "官方初始立绘",
                           "初始立绘", "普通", "寝癖", "水着", "制服", "原版", "野营"],
}

CONFIG = dict(DEFAULT_CONFIG)
CONFIG_PATH = BASE_DIR / "config.json"
if CONFIG_PATH.exists():
    try:
        CONFIG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        print("config.json 解析失败，使用默认配置:", exc)

ROOT = Path(CONFIG["root"]).resolve()
DB_PATH = BASE_DIR / "library.db"
THUMB_DIR = BASE_DIR / "cache" / "thumbs"
TRASH_DIR = BASE_DIR / "trash"

_CONFIG_MTIME = None


def reload_config():
    """config.json 被修改后自动热加载（按文件 mtime 判断），并清空相关缓存。"""
    global _CONFIG_MTIME
    try:
        m = CONFIG_PATH.stat().st_mtime
    except OSError:
        return
    if _CONFIG_MTIME == m:
        return
    _CONFIG_MTIME = m
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("config.json 解析失败，沿用当前配置:", exc)
        return
    CONFIG.clear()
    CONFIG.update(DEFAULT_CONFIG)
    CONFIG.update(data)
    invalidate_tags()

TRASH_EXPIRE_DAYS = int(os.environ.get("EMOJI_TRASH_EXPIRE_DAYS", "30"))

IMG_EXTS = set(CONFIG["image_exts"])

INBOX_NAME = "未处理"

MOVE_LOG = BASE_DIR / "moves.log"


def _log_move(action, old_rel, new_rel=""):
    """记录自动整理产生的文件移动（时间、动作、源路径、目标路径），供导出核对。"""
    try:
        with open(MOVE_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), action, old_rel, new_rel))
    except OSError:
        pass

INBOX_DIR = ROOT / INBOX_NAME
SPECIAL_FOLDERS = set(CONFIG["special_folders"])
# 池根目录下自动归位的特殊类型：文件标上这些类型后，自动移到 <角色池>/<特殊类型>/。
# 差分 优先并入该画师已有的 差分/N（画师） 目录，没有就落 差分/ 根目录（编号留给人工细分）。
AUTO_SPECIAL_SEGS = ("铸币大头", "差分", "圣诞特供")


def auto_special_seg(specials):
    """标签含可自动归位的特殊类型 → 返回第一个命中的类型名；否则返回空串。"""
    if isinstance(specials, str):
        specials = [x for x in specials.split("|") if x]
    for seg in AUTO_SPECIAL_SEGS:
        if seg in (specials or ()):
            return seg
    return ""


def _diff_target_dir(pool_dir, artist):
    """差分 的目标目录：优先该画师已有的 差分/N（画师） 目录，否则 差分/ 根目录。

    N（…） 里的名字既可能是画师（花かざり、-Velstadt-…）也可能是类型（水着、古书馆…），
    只有能在别名表里对上该文件画师的才算命中；命中多个编号时取编号最小的。
    """
    bare = pool_dir / "差分"
    if not artist:
        return bare
    aliases = {norm(n) for n in alias_names_for(artist)}
    try:
        entries = sorted(bare.iterdir()) if bare.is_dir() else []
    except OSError:
        entries = []
    hits = []
    for d in entries:
        if not d.is_dir():
            continue
        m = DIFF_SUB_RE.match(d.name)
        if not m:
            continue
        inner = m.group(2).strip().strip(" -")
        if inner and inner not in TYPE_DENYLIST and norm(inner) in aliases:
            hits.append((int(m.group(1)), d))
    if hits:
        return sorted(hits, key=lambda x: x[0])[0][1]
    return bare


def auto_special_dir(pool_dir, specials, artist=""):
    """可自动归位特殊类型的目标目录；标签里没有可归位的类型时返回 None（目录由调用方创建）。"""
    seg = auto_special_seg(specials)
    if not seg:
        return None
    if seg == "差分":
        return _diff_target_dir(pool_dir, artist)
    return pool_dir / seg


PAIR_CHARS = CONFIG["pair_characters"]
TYPE_DENYLIST = set(CONFIG["diff_type_denylist"])
SCAN_LIMIT = int(os.environ.get("EMOJI_SCAN_LIMIT", "0")) or None

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))

# ---------------------------------------------------------------- 数据库
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=()):
    with contextlib.closing(_connect()) as db:
        return db.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with contextlib.closing(_connect()) as db:
        return db.execute(sql, params).fetchone()


def execute(sql, params=()):
    with contextlib.closing(_connect()) as db:
        db.execute(sql, params)
        db.commit()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files(
              id INTEGER PRIMARY KEY,
              rel TEXT UNIQUE,
              name TEXT,
              folder TEXT,
              academy TEXT DEFAULT '',
              characters TEXT DEFAULT '',
              specials TEXT DEFAULT '',
              artist TEXT DEFAULT '',
              artist_raw TEXT DEFAULT '',
              size INTEGER DEFAULT 0,
              mtime REAL DEFAULT 0,
              ctime REAL DEFAULT 0,
              trashed_at REAL DEFAULT 0,
              status TEXT DEFAULT 'normal'
            );
            CREATE INDEX IF NOT EXISTS idx_files_academy ON files(academy);
            CREATE INDEX IF NOT EXISTS idx_files_artist ON files(artist);
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            """
        )
        db.commit()
        cols = {r[1] for r in db.execute("PRAGMA table_info(files)")}
        if "ctime" not in cols:
            db.execute("ALTER TABLE files ADD COLUMN ctime REAL DEFAULT 0")
        if "trashed_at" not in cols:
            db.execute("ALTER TABLE files ADD COLUMN trashed_at REAL DEFAULT 0")
        db.execute(
            "UPDATE files SET ctime = mtime WHERE ctime IS NULL OR ctime = 0"
        )
        db.execute(
            "UPDATE files SET trashed_at = ? WHERE status='trashed' AND (trashed_at IS NULL OR trashed_at = 0)",
            (time.time(),),
        )
        db.commit()


# ---------------------------------------------------------------- 名称归一化 / 画师别名
_NFKC_CACHE = {}


def norm(s):
    """归一化用于匹配：NFKC、小写、去空格与下划线。"""
    if s is None:
        return ""
    n = unicodedata.normalize("NFKC", str(s)).strip().lower()
    return re.sub(r"[\s_]+", "", n)


def is_ascii(s):
    return all(ord(c) < 128 for c in s)

_py_cache = {}


def _has_cjk(s):
    """是否含中日韩汉字（含扩展 A 区生僻字，如 䌷 U+4337）。"""
    for ch in s:
        o = ord(ch)
        if 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF:
            return True
        if 0x20000 <= o <= 0x3134F:
            return True
    return False


def tag_pinyin(name):
    """返回 (全拼, 首字母缩写)；名字不含中文则返回空串。"""
    if name in _py_cache:
        return _py_cache[name]
    if not name or not _HAS_PINYIN:
        return ("", "")
    if not _has_cjk(name):
        _py_cache[name] = ("", "")
        return ("", "")
    full = "".join(lazy_pinyin(name, errors="ignore")).lower()
    ab = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore")).lower()
    _py_cache[name] = (full, ab)
    return (full, ab)


def split_aliases(cell):
    out = []
    for part in re.split(r"[、，,／/]", cell):
        part = part.strip()
        part = re.sub(r"（@后缀已删）$", "", part).strip()
        if part and not re.fullmatch(r":?-{3,}:?", part):
            out.append(part)
    return out


_ALIAS_HEADER_CELLS = {"画师", "画师名", "主画师名", "主名", "主画师", "别名", "别名/其他写法", "其他写法", "其他"}


def parse_readme_aliases():
    """从根 README.md 的画师别名表读取分组。"""
    groups = []
    readme = ROOT / "README.md"
    if not readme.exists():
        return groups
    for line in readme.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        if any(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        if cells[0] in _ALIAS_HEADER_CELLS:
            continue
        names = []
        for idx, c in enumerate(cells):
            if idx == 0:
                names += split_aliases(c)
            else:
                for part in split_aliases(c):
                    if part and part not in _ALIAS_HEADER_CELLS:
                        names.append(part)
        if names:
            groups.append(names)
    return groups


def pick_primary(names):
    ascii_names = [n for n in names if is_ascii(n) and any(c.isalnum() for c in n)]
    if ascii_names:
        return max(ascii_names, key=len)
    # 无英文名时以主名为准（README 别名表第一格 / 文件夹名），不取最长别名
    return names[0]


# 文件夹名里的画师：系列N（X）/ 差分（X）/ 圣诞特供（X）/ 铸币大头（X）
FOLDER_ARTIST_RE = re.compile(r"^(系列\d+|差分|圣诞特供|铸币大头)（(.+)）$")
DIFF_SUB_RE = re.compile(r"^(\d+)（(.+)）$")

_artist_groups_cache = None
_artist_known_norms = None
_artist_primary_map = {}


def collect_folder_artist_names():
    """扫描目录名，收集出现在画师文件夹位置上的名字（不含 denylist 类型名）。"""
    names = set()
    for dirpath, dirnames, _ in os.walk(ROOT):
        parts = os.path.relpath(dirpath, ROOT).split(os.sep)
        if parts[0] != ".":
            if any(p in CONFIG["exclude_segments"] or p.startswith(".") for p in parts):
                dirnames[:] = []
                continue
            if parts[0] in CONFIG["exclude_dirs"]:
                dirnames[:] = []
                continue
        for d in list(dirnames):
            m = FOLDER_ARTIST_RE.match(d)
            if m:
                inner = m.group(2).strip().strip(" -")
                kind = m.group(1)
                if inner and inner != "未知":
                    if kind.startswith("系列") or (inner not in TYPE_DENYLIST):
                        names.add(inner)
            elif DIFF_SUB_RE.match(d):
                inner = d[d.index("（") + 1:d.rindex("）")].strip().strip(" -")
                if inner and inner != "未知" and inner not in TYPE_DENYLIST:
                    names.add(inner)
    return names


EXTRA_TAGS_FILE = BASE_DIR / "tags_extra.json"
EXTRA_ALIASES_FILE = BASE_DIR / "aliases_extra.json"


def _load_json_file(p, default):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json_file(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_extra_tags():
    """网页新建的标签：{characters: {名: 学院}, academies: [...], artists: [...]}"""
    return _load_json_file(EXTRA_TAGS_FILE, {"characters": {}, "academies": [], "artists": []})


def get_extra_aliases():
    """网页添加的画师别名分组：{groups: [[主名, 别名...], ...]}"""
    return _load_json_file(EXTRA_ALIASES_FILE, {"groups": []})


def _reset_alias_caches():
    global _artist_groups_cache, _artist_known_norms, _artist_primary_map, _alias_cache
    _artist_groups_cache = None
    _artist_known_norms = None
    _artist_primary_map = {}
    _alias_cache = {}


_alias_sig = None
_alias_checked_at = 0.0
# 别名表的 mtime 检查节流：README/别名表改动最多 1 秒后生效，
# 但避免一次全库扫描里对每个文件都 stat 一遍文件系统（9.6 万次 → 每秒 1 次）
_ALIAS_CHECK_INTERVAL = 1.0


# 别名表变动后的画师名重算（见 snapshot_primary_map / retag_stored_artists）
_retag_event = threading.Event()
_retag_prev = {}


def _file_signature(paths):
    """给定文件的指纹（路径、mtime_ns、大小）。"""
    sig = []
    for p in paths:
        try:
            if p.exists():
                st = p.stat()
                sig.append((p.as_posix(), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(sig)


def _alias_signature():
    """画师别名来源（根 README.md + aliases_extra.json）的指纹。"""
    return _file_signature((ROOT / "README.md", EXTRA_ALIASES_FILE))


def _scan_fingerprint():
    """影响打标结果的全部输入（别名表 + config.json + 别名补充）：
    只要它没变，尺寸与 mtime 都没变的文件就不必重新打标。"""
    return _file_signature((ROOT / "README.md", CONFIG_PATH, EXTRA_ALIASES_FILE))


def snapshot_primary_map():
    """当前别名表下 {artist_raw: 主名} 的快照。

    别名改动前先拍一张：改动后靠它认领「旧值本来就是我们派生的主名」那些行，
    否则删除别名时那些行会被当成手工填写的标签而跳过。

    若这次调用本身才发现别名表被改过（例如手工编辑了 aliases_extra.json），
    当前缓存已经换成新表，只有 _retag_prev 还是产出库里现有值的那张旧快照。
    """
    if _ensure_aliases_fresh():
        return dict(_retag_prev)
    rows = list(query("SELECT DISTINCT artist_raw FROM files WHERE artist_raw <> ''")
                )
    return dict((r["artist_raw"], artist_primary(r["artist_raw"])) for r in rows if r["artist_raw"])


def retag_stored_artists(prev_primaries=None, detail=None):
    """按当前别名表重算库里 files.artist（只动「本来就是我们按 artist_raw 派生的」行）。

    加/删画师别名后立刻调用：否则库里存的还是旧画师名，左侧画师列表、筛选、
    文件标签面板都要等下一次全库扫描才更新。返回实际改动的行数。

    detail 非 None 时把真正改到的 [artist_raw, 旧名, 新名] 追加进去：前端拿它就地
    改本地列表里对得上的行，不用整页重拉，滚动位置和选中状态都不动。
    """
    todo = []
    for r in query("SELECT DISTINCT artist_raw, artist FROM files WHERE artist_raw <> ''"):
        raw = r["artist_raw"] or ""
        cur = r["artist"] or ""
        if not raw:
            continue
        prim = artist_primary(raw)
        if not prim or prim == cur:
            continue
        if cur:
            known = set(norm(n) for n in alias_names_for(raw))
            if norm(cur) not in known and (prev_primaries or {}).get(raw) != cur:
                continue  # 旧值既不属于当前别名组、也不是改动前的主名 → 当手工标签，不动它
        todo.append((prim, cur, raw))
    if not todo:
        return 0
    changed = 0
    with contextlib.closing(_connect()) as db:
        for prim, cur, raw in todo:
            cur2 = db.execute("UPDATE files SET artist=? WHERE artist_raw=? AND artist=?",
                              (prim, raw, cur))
            n = cur2.rowcount or 0
            changed += n
            if n and detail is not None:
                detail.append([raw, cur, prim])
        db.commit()
    return changed


def _ensure_aliases_fresh(force=False):
    """README.md / aliases_extra.json 变动时自动重置别名缓存，无需重启。

    返回是否刚刚刷新过：调用方据此判断「当前缓存是否已经换成新别名表」。
    """
    global _alias_sig, _alias_checked_at
    now = time.monotonic()
    if not force and now - _alias_checked_at < _ALIAS_CHECK_INTERVAL:
        return False
    _alias_checked_at = now
    sig = _alias_signature()
    if sig != _alias_sig:
        _alias_sig = sig
        # 别名表/README 变了：先按旧表拍一张主名快照（供重算认领旧值），再清缓存
        try:
            snap = snapshot_primary_map()
        except Exception:
            snap = {}
        _retag_prev.clear()
        _retag_prev.update(snap)
        _reset_alias_caches()
        _retag_event.set()
        return True
    return False


def artist_groups():
    global _artist_groups_cache
    _ensure_aliases_fresh()
    if _artist_groups_cache is None:
        groups = parse_readme_aliases()
        seen = set()
        for g in groups:
            for n in g:
                seen.add(norm(n))
        for name in sorted(collect_folder_artist_names()):
            if norm(name) not in seen:
                groups.append([name])
                seen.add(norm(name))
        # 网页添加的画师别名分组：与已有分组按名称合并
        for g in get_extra_aliases().get("groups", []):
            g2 = []
            for n in g:
                if n and n not in g2:
                    g2.append(n)
            if not g2:
                continue
            target = None
            for base in groups:
                bn = {norm(x) for x in base}
                if any(norm(n) in bn for n in g2):
                    target = base
                    break
            if target is not None:
                tn = {norm(x) for x in target}
                for n in g2:
                    if norm(n) not in tn:
                        target.append(n)
            else:
                groups.append(g2)
        _artist_groups_cache = groups
    return _artist_groups_cache


def artist_known_norms():
    global _artist_known_norms
    _ensure_aliases_fresh()
    if _artist_known_norms is None:
        s = set()
        for g in artist_groups():
            for n in g:
                s.add(norm(n))
        _artist_known_norms = s
    return _artist_known_norms


def artist_primary(name):
    if not name:
        return ""
    _ensure_aliases_fresh()
    key = norm(name)
    m = _artist_primary_map
    if key in m:
        return m[key]
    if not m:
        # 别名表刚变动过：一次性把「每个写法 → 主名」全算好，避免对每个名字线性扫一遍分组。
        # 加/删别名后要重算全库 artist 列，逐个名字扫分组会慢到几秒。
        for g in artist_groups():
            prim = pick_primary(g)
            for n in g:
                m.setdefault(norm(n), prim)
        if key in m:
            return m[key]
    m[key] = name          # 不在任何别名分组里 → 主名就是它自己
    return name


_alias_cache = {}


def alias_names_for(name):
    """返回该画师及其全部别名的原始写法（用于判断文件名是否包含画师名）。"""
    _ensure_aliases_fresh()
    key = norm(name)
    if key in _alias_cache:
        return _alias_cache[key]
    result = [name]
    for g in artist_groups():
        if any(norm(n) == key for n in g):
            result = g
            break
    _alias_cache[key] = result
    return result


# ---------------------------------------------------------------- 文件名画师识别
def candidates_from_filename(stem):
    cands = []
    for m in re.finditer(r"(?:^|[_@＠＿])([^_@＠＿.\-()（）\s]+)", stem):
        c = m.group(1).strip()
        if c and c not in cands:
            cands.append(c)
    # 首个连字符前的部分（处理 Dari Uri-1111 / Moricho-1111-01 / AYA-cis-1111 等）
    segs = stem.split("-")
    for i in range(1, len(segs)):
        prefix = "-".join(segs[:i]).strip()
        if prefix and prefix not in cands:
            cands.append(prefix)
    # 按分隔符切出的所有 token
    for tok in re.split(r"[-_\s]+", stem):
        tok = tok.strip()
        if len(tok) >= 2 and tok not in cands:
            cands.append(tok)
    return cands


# 下载器在「画师名-作品ID」之后常见的来源/类型标注，不属于画师名
TAIL_WORDS = {
    "fanbox", "pixiv", "pixivfanbox", "twitter", "x", "patreon", "fantia",
    "skeb", "poipiku", "danbooru", "konachan", "gelbooru", "yandere", "sankaku",
    "weibo", "bilibili", "lofter", "tumblr", "bluesky", "bsky", "telegram", "plurk",
    "pawoo", "nijie", "booth", "kofi", "gumroad", "instagram", "facebook", "youtube",
    "niconico", "misskey", "artstation", "deviantart", "afdian", "note", "r18",
    "sample", "preview", "fanart", "artwork", "illust", "set", "part",
}
TAIL_SEG_RE = re.compile(r"^(?:\d+(?:_p\d+)?(?:[（(]\d+[)）])?|\d+_\d+|_p\d+)$")


def is_tail_seg(tok):
    """作品 ID 之后的片段是否为「序号 / 页码 / 去重括号 / 平台来源标注」，而非画师名的一部分。"""
    t = (tok or "").strip()
    if not t:
        return False
    if TAIL_SEG_RE.match(t):
        return True
    if t.lower() in TAIL_WORDS:
        return True
    return bool(re.fullmatch(r"[A-Za-z]{1,12}", t))



def detect_artist_from_filename(name):
    stem = os.path.splitext(name)[0]
    # 强信号：_@画师 / @画师（pixiv 式署名），无需预先知道该画师
    for m in re.finditer(r"(?:^|_)[@＠]([^@＠.\-()（）\s]+)", stem):
        c = m.group(1).strip(" _　")
        # 结尾的 _1 / _12 若是下载去重序号则去掉；仅当去掉后能在别名表里找到该画师时才处理，
        # 避免误伤 Akichan_205 / bob_tong_95 这类名字本身带数字的画师（40_@PanTo_PT_1 -> PanTo_PT）
        if "_" in c:
            base = re.sub(r"_\d{1,3}$", "", c).strip(" _　")
            if base and base != c and norm(base) in artist_known_norms():
                c = base
        if len(norm(c)) >= 2:
            return c
    # 未知画师：pixiv 下载格式「画师名-作品ID(_p页/-序号/日期-序号…)」，作品 ID 为 ≥5 位数字。
    # 从最左侧的分隔点开始匹配：作品 ID 之后只允许出现编号/页码/日期等纯数字段，
    # 避免「画师-作品ID-日期-序号」被贪婪地整段当成画师名（如 rocktide_-1869…-20241218-01）。
    # 注意：下划线可能是画师名的一部分（rocktide_ / dit_dit_），只去掉首尾空格与连字符。
    # 优先于已知名单弱信号，避免「あ@-pid_p0」被拆成别名「あ」
    id_seg = re.compile(r"^\d{5,}(?:_p\d+)?$")
    segs = stem.split("-")
    for i in range(1, len(segs)):
        rest = segs[i:]
        if not id_seg.match(rest[0]):
            continue
        if not all(is_tail_seg(t) for t in rest[1:]):
            continue
        cand = "-".join(segs[:i]).strip(" 　-")
        if not cand:
            continue
        # 「画师名@简介-作品ID」：@ 后有文字时取 @ 前（TY楓@お仕事募集中 -> TY楓）；
        # 特例：@ 结尾（如 あ@）表示画师名本身以 @ 结尾，保留整段。
        at = cand.find("@") if "@" in cand else cand.find("＠")
        if at >= 0 and at + 1 < len(cand) and cand[at + 1] not in ("-", " ", "　"):
            cand = cand[:at].strip(" 　-")
        if (len(norm(cand)) >= 2 and not cand.isdigit()
                and any(ch.isalnum() for ch in cand)
                and cand not in ("未知", "杂烩", "AI") and cand not in SPECIAL_FOLDERS):
            return cand
    # 弱信号：仅在已知画师名单中匹配（避免误判 pixiv id / 普通单词）；支持单字画师如 特
    known = artist_known_norms()
    for c in candidates_from_filename(stem):
        if norm(c) in known:
            return c
    return ""


# ---------------------------------------------------------------- 路径打标
def tag_for_rel(rel):
    parts = rel.split("/")
    academy = parts[0] if len(parts) > 1 else ""
    characters = []
    specials = []
    artist_raw = ""
    rest = []

    if len(parts) > 1:
        char = parts[1]
        pair = PAIR_CHARS.get(char)
        if pair and len(parts) > 2 and parts[2] in pair:
            # 组合子文件夹只打成员标签：光望/光 -> 光；光望/光望 -> 光望
            characters.append(parts[2])
            rest = parts[3:-1]
        else:
            characters.append(char)
            rest = parts[2:-1]

    for seg in rest:
        m = FOLDER_ARTIST_RE.match(seg)
        if m:
            kind, inner = m.group(1), m.group(2).strip().strip(" -")
            if kind.startswith("系列"):
                if inner and inner != "未知":
                    artist_raw = inner
            elif kind in SPECIAL_FOLDERS:
                specials.append(kind)
                if inner in ("AI", "AI差分"):
                    specials.append("AI")
                elif inner and inner != "未知" and inner not in TYPE_DENYLIST:
                    artist_raw = inner
            continue
        if seg in SPECIAL_FOLDERS:
            specials.append(seg)
            continue
        m2 = DIFF_SUB_RE.match(seg)
        if m2:
            inner = m2.group(2).strip().strip(" -")
            if inner == "AI" or inner == "AI差分":
                specials.append("AI")
            elif inner and inner != "未知" and inner not in TYPE_DENYLIST:
                artist_raw = inner
            continue
        if seg.startswith("差分"):
            specials.append("差分")
    if not specials and any(s.startswith("差分") or s == "差分" for s in parts[1:-1]):
        specials.append("差分")
    if any(p in ("AI", "AI差分") for p in parts[1:-1]) and "AI" not in specials:
        specials.append("AI")

    if not artist_raw:
        artist_raw = detect_artist_from_filename(parts[-1])
    return {
        "academy": academy,
        "characters": characters,
        "specials": specials,
        "artist_raw": artist_raw,
        "artist": artist_primary(artist_raw) if artist_raw else "",
    }


# ---------------------------------------------------------------- 扫描
def iter_files():
    """单遍 os.scandir 遍历图片：目录项与 stat 信息一次取出（Windows 下 DirEntry.stat 复用目录枚举
    的结果，比 os.walk + 每个文件一次 os.stat 省掉一半文件系统调用）。产出 (rel, full, size, mtime, ctime)。"""
    exclude_dirs = set(CONFIG["exclude_dirs"])
    exclude_segs = set(CONFIG["exclude_segments"])
    root_str = str(ROOT)
    root_prefix = root_str + os.sep
    stack = [root_str]
    count = 0
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                nm = e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if nm.startswith(".") or nm in exclude_dirs or nm in exclude_segs:
                            continue
                        stack.append(e.path)
                        continue
                    if os.path.splitext(nm)[1].lower() not in IMG_EXTS:
                        continue
                    st = e.stat()
                except OSError:
                    continue
                rel = (e.path[len(root_prefix):] if e.path.startswith(root_prefix)
                       else os.path.relpath(e.path, ROOT)).replace(os.sep, "/")
                count += 1
                if SCAN_LIMIT and count > SCAN_LIMIT:
                    return
                yield rel, e.path, st.st_size, st.st_mtime, st.st_ctime


SCAN_UPSERT_SQL = """
            INSERT INTO files(rel, name, folder, academy, characters, specials,
                              artist, artist_raw, size, mtime, ctime, status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'normal')
            ON CONFLICT(rel) DO UPDATE SET
              name=excluded.name, folder=excluded.folder, academy=excluded.academy,
              characters=excluded.characters, specials=excluded.specials,
              artist=excluded.artist, artist_raw=excluded.artist_raw,
              size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, status='normal'
            """


def scan_disk():
    # phase: count(数文件) -> scan(读目录/打标) -> write(写库) -> done
    stats = {"total": 0, "inserted": 0, "updated": 0, "elapsed": 0.0,
             "phase": "count", "done": 0, "est_total": 0, "write_total": 0,
             "skipped": 0, "missing": 0}
    SCAN_STATE["progress"] = stats  # 同一个 dict，/api/scan/status 实时反映进度
    t0 = time.time()
    stats["est_total"] = query_one("SELECT COUNT(*) c FROM files WHERE status='normal'")["c"]
    stats["phase"] = "scan"
    # 一次性取回库内快照：用 size+mtime 判断“没变过”的文件，直接跳过打标与写库。
    # 只有别名表/config.json 变动过（fingerprint 不同）才整库重打标，保证标签不会变陈旧。
    db_state = {}
    with contextlib.closing(sqlite3.connect(DB_PATH)) as db:
        for rel, size, mtime, status in db.execute("SELECT rel, size, mtime, status FROM files"):
            db_state[rel] = (size, mtime, status)
    fp_now = repr(_scan_fingerprint())
    row_fp = query_one("SELECT v FROM meta WHERE k='scan_fingerprint'")
    retag_all = (row_fp is None) or (row_fp["v"] != fp_now)
    rows = []
    seen = set()
    t_loop = time.time()
    for rel, full, size, mtime, ctime in iter_files():
        stats["total"] += 1
        seen.add(rel)
        prev = db_state.get(rel)
        if (not retag_all and prev is not None and prev[2] == "normal"
                and prev[0] == size and prev[1] == mtime):
            stats["skipped"] += 1
            continue
        tag = tag_for_rel(rel)
        rows.append((rel, os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"),
                     tag["academy"], "|".join(tag["characters"]), "|".join(tag["specials"]),
                     tag["artist"], tag["artist_raw"], size, mtime, ctime))
        if stats["total"] % 500 == 0:
            stats["done"] = stats["total"]
    t_loop = time.time() - t_loop
    stats["done"] = stats["total"]
    stats["write_total"] = len(rows)
    stats["phase"] = "write"
    stats["done"] = 0
    gone = [rel for rel, v in db_state.items() if v[2] == "normal" and rel not in seen]
    t_db = time.time()
    with contextlib.closing(_connect()) as db:
        chunk = 2000
        for i in range(0, len(rows), chunk):
            db.executemany(SCAN_UPSERT_SQL, rows[i:i + chunk])
            stats["done"] = min(len(rows), i + chunk)
        for i in range(0, len(gone), chunk):
            db.executemany("UPDATE files SET status='missing' WHERE rel=?",
                           [(r,) for r in gone[i:i + chunk]])
        stats["inserted"] = len(rows)
        stats["missing"] = len(gone)
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_scan', ?)", (str(time.time()),))
        db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('scan_fingerprint', ?)", (fp_now,))
        db.commit()
    t_db = time.time() - t_db
    stats["phase"] = "done"
    stats["elapsed"] = round(time.time() - t0, 1)
    print("[scan] 文件 %d（未变 %d / 改写 %d / 标记缺失 %d）遍历+打标 %.1fs 写库 %.1fs 合计 %.1fs%s"
          % (stats["total"], stats["skipped"], len(rows), len(gone), t_loop, t_db, stats["elapsed"],
             "（别名表或配置变动，整库重打标）" if retag_all else ""), flush=True)
    return stats


SCAN_STATE = {"running": False, "progress": None}


def run_scan_async():
    def worker():
        try:
            SCAN_STATE["running"] = True
            SCAN_STATE["progress"] = {"phase": "count", "done": 0, "est_total": 0, "total": 0}
            stats = scan_disk()
            SCAN_STATE["progress"] = stats
            t_move = time.time()
            stats["phase"] = "move"
            stats["done"] = 0
            stats["move_total"] = 0
            stats["moved"] = auto_move_in_pool_loose(progress=stats)
            print("[scan] 自动归位 %d 个文件，用时 %.1fs" % (stats.get("moved", 0), time.time() - t_move),
                  flush=True)
            # 扫描/归位改过库：标签统计要跟着更新，否则侧栏计数一直停在扫描前
            if stats["inserted"] or stats["missing"] or stats.get("moved"):
                t_tag = time.time()
                invalidate_tags(sync=True)
                print("[scan] 标签统计已重算，用时 %.1fs" % (time.time() - t_tag), flush=True)
            stats["phase"] = "done"
            stats["elapsed"] = round(stats["elapsed"] + (time.time() - t_move), 1)
        except Exception as exc:  # noqa: BLE001
            SCAN_STATE["progress"] = {"error": str(exc)}
        finally:
            SCAN_STATE["running"] = False

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- 路径安全
def safe_rel(rel):
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel or any(p in ("..", ".") or p == "" for p in rel.split("/")):
        abort(400, "非法路径")
    full = (ROOT / rel).resolve()
    if not str(full).startswith(str(ROOT)):
        abort(400, "非法路径")
    return rel


# ---------------------------------------------------------------- 标签聚合
def _parse_csv(s):
    return [x for x in (s or "").split("|") if x]


def _empty_tag_raw():
    return {"academies": {}, "characters": {}, "artists": {}, "specials": {}, "folders": {},
            "exts": {}, "untagged": 0, "char_academy_counts": {}}


def _row_field(row, key):
    try:
        return row[key] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _bump(counter, key, sign):
    val = counter.get(key, 0) + sign
    if val > 0:
        counter[key] = val
    else:
        counter.pop(key, None)


def accumulate_tag_row(raw, row, sign=1):
    """把一行文件记录计入(sign=1)/移出(sign=-1)原始计数缓存。改标签时用它做增量更新。"""
    status = _row_field(row, "status")
    if status and status != "normal":
        return  # 只有 normal 状态才计入统计（废纸篓/丢失文件不参与）
    academy = _row_field(row, "academy")
    chars = [c for c in _row_field(row, "characters").split("|") if c]
    specs = [s for s in _row_field(row, "specials").split("|") if s]
    artist = _row_field(row, "artist")
    folder = _row_field(row, "folder")
    name = _row_field(row, "name")
    ac = academy or "无学院"
    _bump(raw["academies"], ac, sign)
    if not academy and not chars and not specs and not artist:
        raw["untagged"] += sign
    for c in chars:
        _bump(raw["characters"], c, sign)
        cac = raw["char_academy_counts"].get(c)
        if cac is None:
            cac = raw["char_academy_counts"][c] = {}
        _bump(cac, ac, sign)
        if not cac:
            raw["char_academy_counts"].pop(c, None)
    for s in specs:
        _bump(raw["specials"], s, sign)
    if artist:
        _bump(raw["artists"], artist, sign)
    if folder:
        _bump(raw["folders"], folder, sign)
    if "." in name:
        _bump(raw["exts"], name.rsplit(".", 1)[1].lower(), sign)


def build_tag_raw():
    """扫库统计原始计数（约 0.5s）：只在库里文件本身变动后由后台线程重建。"""
    raw = _empty_tag_raw()
    for r in query("SELECT academy, characters, specials, artist, folder, name FROM files "
                   "WHERE status='normal' AND folder <> ?", (INBOX_NAME,)):
        accumulate_tag_row(raw, r, 1)
    return raw


def finalize_tag_stats(raw):
    """把原始计数整理成前端用的标签视图（约 50ms）：每次打标后重建也很快。"""
    stats = {"academies": dict(raw["academies"]), "characters": dict(raw["characters"]),
             "artists": dict(raw["artists"]), "specials": dict(raw["specials"]),
             "untagged": raw["untagged"], "relations": {}, "char_academy": {},
             "char_academy_counts": {}, "artist_aliases": {}, "artist_alias_map": {},
             "extra_aliases": {}, "folders": dict(raw["folders"]), "exts": dict(raw["exts"]),
             "char_qualified": {}, "char_qualified_base": {}}
    for c, cac in raw["char_academy_counts"].items():
        stats["char_academy_counts"][c] = dict(cac)
        stats["char_academy"][c] = set(cac)
        for a in cac:
            stats["relations"].setdefault(a, set()).add(c)

    for g in artist_groups():
        prim = pick_primary(g)
        others = [n for n in g if n != prim]
        if others:
            stats["artist_aliases"][prim] = others

    # 网页新建的标签合并进来（可能还没有文件，计数为 0）
    extra = get_extra_tags()
    for ac in extra.get("academies", []):
        stats["academies"].setdefault(ac, stats["academies"].get(ac, 0))
    for ch, ac in extra.get("characters", {}).items():
        if ch not in stats["characters"]:
            stats["characters"][ch] = 0
        if ac:
            stats["academies"].setdefault(ac, stats["academies"].get(ac, 0))
            stats["relations"].setdefault(ac, set()).add(ch)
            stats["char_academy"].setdefault(ch, set()).add(ac)
            stats["char_academy_counts"].setdefault(ch, {}).setdefault(ac, 0)
    for ar in extra.get("artists", []):
        stats["artists"].setdefault(ar, stats["artists"].get(ar, 0))

    # 除明确允许多学院的角色（如杂鱼酱）外，其余角色只保留文件数最多的学院（多数票），
    # 避免个别误归位文件把角色变成「多学院」、导致标签显示成「学院+角色」。
    multi_ok = set(CONFIG.get("multi_academy_characters", ["杂鱼酱"]))
    # 非杂鱼酱角色：一律以新建角色时设定的学院为准（tags_extra.json）。
    # 即使个别文件被误归到其他学院，也只认设定学院，避免出现「学院+角色」前缀。
    extra_ch_acad = extra.get("characters", {})
    for c in list(stats["characters"]):
        if c in multi_ok:
            continue
        cfg_ac = extra_ch_acad.get(c)
        if not cfg_ac:
            continue
        old_acs = set(stats["char_academy"].get(c, ()))
        if old_acs == {cfg_ac}:
            continue
        for a in old_acs:
            if a != cfg_ac:
                stats["relations"].get(a, set()).discard(c)
        stats["char_academy"][c] = {cfg_ac}
        stats["char_academy_counts"][c] = {cfg_ac: stats["characters"][c]}
    for c, acs in list(stats["char_academy"].items()):
        if c in multi_ok:
            continue
        cac = stats["char_academy_counts"].get(c, {})
        if len(cac) <= 1:
            continue
        ranked = sorted(((cnt, a) for a, cnt in cac.items()), reverse=True)
        if len(ranked) >= 2 and ranked[0][0] == ranked[1][0]:
            continue  # 并列最多：保持多学院展示，避免强行二选一
        keep = ranked[0][1]
        for a in list(acs):
            if a != keep:
                stats["relations"].get(a, set()).discard(c)
                cac.pop(a, None)
        stats["char_academy"][c] = {keep}

    # 多学院角色（如杂鱼酱）：展示与筛选时在角色名前加学院名，如 千年杂鱼酱
    amb = [c for c, acs in stats["char_academy"].items() if len(acs) > 1]
    if amb:
        qual = {}
        qual_base = {}
        new_chars, new_char_academy, new_char_academy_counts = {}, {}, {}
        for c in amb:
            for a in stats["char_academy"][c]:
                q = a + c
                qual[q] = {"academy": a, "base": c}
                qual_base.setdefault(c, {})[a] = q
                n = stats["char_academy_counts"].get(c, {}).get(a, 0)
                new_chars[q] = n
                new_char_academy[q] = {a}
                new_char_academy_counts[q] = {a: n}
        for c in amb:
            stats["characters"].pop(c, None)
            stats["char_academy"].pop(c, None)
            stats["char_academy_counts"].pop(c, None)
        stats["characters"].update(new_chars)
        stats["char_academy"].update(new_char_academy)
        stats["char_academy_counts"].update(new_char_academy_counts)
        amb_set = set(amb)
        for ac, cs in stats["relations"].items():
            stats["relations"][ac] = {ac + c if c in amb_set else c for c in cs}
        stats["char_qualified"] = qual
        stats["char_qualified_base"] = qual_base

    for prim, others in stats["artist_aliases"].items():
        for o in others:
            stats["artist_alias_map"][o] = prim
    for g in get_extra_aliases().get("groups", []):
        prim = pick_primary(g)
        others = [n for n in g if n != prim]
        if others:
            stats["extra_aliases"][prim] = others

    pinyin = {}
    for name in list(stats["academies"]) + list(stats["characters"]) + list(stats["specials"]) + list(stats["artists"]):
        full, ab = tag_pinyin(name)
        if full:
            pinyin[name] = {"py": full, "ab": ab}
    stats["pinyin"] = pinyin

    for k in ("relations", "char_academy"):
        stats[k] = {kk: sorted(vv) for kk, vv in stats[k].items()}
    return stats

_TAG_CACHE = {"t": 0.0, "data": None}
# 原始计数（全库扫描结果）单独缓存：打标/移动等操作只在它上面做增量，不再重扫全库
_TAG_RAW = {"data": None, "dirty": True}
_TAG_STATS_TTL = 30.0
# /api/facets 结果缓存：筛选条件 -> 各标签计数（打标/扫描等改动后清空）
_FACET_CACHE = {}
_FACET_CACHE_MAX = 40
_raw_rebuild_event = threading.Event()


def _raw_rebuild_worker():
    """后台重建原始计数：合并短时间内的多次失效，避免连续打标时反复重算。"""
    while True:
        _raw_rebuild_event.wait()
        _raw_rebuild_event.clear()
        while True:
            time.sleep(0.25)
            if not _raw_rebuild_event.is_set():
                break
            _raw_rebuild_event.clear()
        try:
            _TAG_RAW["data"] = build_tag_raw()
            _TAG_RAW["dirty"] = False
            _TAG_CACHE["data"] = None
        except Exception as exc:  # noqa: BLE001
            print("标签计数后台重建失败:", exc)


threading.Thread(target=_raw_rebuild_worker, daemon=True).start()


def get_tag_stats(force=False):
    reload_config()
    now = time.time()
    if force or _TAG_CACHE["data"] is None or now - _TAG_CACHE["t"] > _TAG_STATS_TTL:
        raw = _TAG_RAW["data"]
        if raw is None or force:
            raw = build_tag_raw()
            _TAG_RAW["data"] = raw
            _TAG_RAW["dirty"] = False
        _TAG_CACHE["t"] = now
        _TAG_CACHE["data"] = finalize_tag_stats(raw)
    return _TAG_CACHE["data"]


def invalidate_tags(delta=None, sync=False):
    """标签/位置变动后失效缓存。

    delta=[(旧行, 新行), …]：直接增量改计数（打标这种高频路径用，立刻准确）；
    否则只标记原始计数待重建，交给后台线程刷新，请求不再等整表重算。
    sync=True：在这里就把原始计数重建好（扫描结束、加/删画师别名后用——这些操作前端
    都会紧接着拉 /api/tags，交给后台线程（0.25s 合并 + 约 1s 重建）来不及，界面会停在
    改动前）。
    """
    global _artist_groups_cache, _artist_known_norms
    _TAG_CACHE["data"] = None
    _FACET_CACHE.clear()
    raw = _TAG_RAW["data"]
    if raw is None:
        return
    if delta:
        try:
            for old, new in delta:
                if old is not None:
                    accumulate_tag_row(raw, old, -1)
                if new is not None:
                    accumulate_tag_row(raw, new, 1)
            _TAG_RAW["dirty"] = False
            # 打标不动目录结构：保留画师名单缓存（重扫目录约 0.3s，是打标变慢的主因）
            return
        except Exception:  # noqa: BLE001
            pass
    _artist_groups_cache = None
    _artist_known_norms = None
    _TAG_RAW["dirty"] = True
    _raw_rebuild_event.set()
    if sync:
        _raw_rebuild_event.clear()   # 马上就地重建，不必再让后台线程跑一遍
        try:
            _TAG_RAW["data"] = build_tag_raw()
            _TAG_RAW["dirty"] = False
        except Exception as exc:  # noqa: BLE001
            print("标签统计同步重建失败:", exc)


def _artist_retag_worker():
    """别名表被手工改动（网页加别名走同步重算）时，后台把库里的 artist 列重算一遍，
    省得非要先跑一次全库扫描才能看到新画师名。"""
    while True:
        _retag_event.wait()
        _retag_event.clear()
        while True:                      # 合并短时间内的多次改动
            time.sleep(0.3)
            if not _retag_event.is_set():
                break
            _retag_event.clear()
        try:
            if retag_stored_artists(dict(_retag_prev) or None):
                invalidate_tags(sync=True)
        except Exception as exc:  # noqa: BLE001
            print("画师名后台重算失败:", exc)


threading.Thread(target=_artist_retag_worker, daemon=True).start()



# ---------------------------------------------------------------- 筛选
def build_file_filter(ignore=None):
    """把 /api/files 的查询参数拼成 SQL WHERE。ignore 里的维度跳过不拼
    （/api/facets 统计某一组时要忽略这一组自己的选择）。"""
    args = request.args
    skip = ignore or ()
    academies = [] if "academy" in skip else args.getlist("academy")
    characters = [] if "character" in skip else args.getlist("character")
    artists = [] if "artist" in skip else args.getlist("artist")
    specials = [] if "special" in skip else args.getlist("special")
    untagged = args.get("untagged") == "1"
    no_char = args.get("no_char") == "1"
    q = (args.get("q") or "").strip()
    name_q = (args.get("name") or "").strip()
    folders = args.getlist("folder")
    exts = args.getlist("ext")

    note = ""
    if academies and characters:
        rels = get_tag_stats()
        acad_chars = set()
        for a in academies:
            acad_chars |= set(rels["relations"].get(a, []))
        if not any(c in acad_chars for c in characters):
            note = "学院【%s】与角色【%s】不匹配，强制搜索无结果" % (
                "、".join(academies), "、".join(characters))

    wheres = ["status='normal'", "folder <> ?"]
    params = [INBOX_NAME]
    if academies:
        wheres.append("academy IN (%s)" % ",".join("?" * len(academies)))
        params += academies
    if characters:
        ts_ = get_tag_stats()
        conds = []
        for c in characters:
            qd = ts_.get("char_qualified", {}).get(c)
            if qd:
                conds.append("(academy=? AND instr('|'||characters||'|', ?) > 0)")
                params += [qd["academy"], "|" + qd["base"] + "|"]
            else:
                conds.append("instr('|'||characters||'|', ?) > 0")
                params += ["|" + c + "|"]
        wheres.append("(" + " OR ".join(conds) + ")")
    if artists:
        wheres.append("artist IN (%s)" % ",".join("?" * len(artists)))
        params += artists
    if specials:
        conds = " OR ".join(["instr('|'||specials||'|', ?) > 0"] * len(specials))
        wheres.append("(" + conds + ")")
        params += ["|" + s + "|" for s in specials]
    if untagged:
        wheres.append("academy='' AND characters='' AND specials='' AND artist=''")
    if no_char:
        wheres.append("characters=''")
    if name_q:
        wheres.append("name LIKE ?")
        params.append("%" + name_q + "%")
    if folders:
        conds = []
        for f in folders:
            conds.append("(folder = ? OR folder LIKE ?)")
            params += [f, f + "/%"]
        wheres.append("(" + " OR ".join(conds) + ")")
    if exts:
        conds = " OR ".join(["instr(name, '.') > 0 AND lower(substr(name, instr(name, '.') + 1)) = ?"] * len(exts))
        wheres.append("(" + conds + ")")
        params += [e.lower() for e in exts]
    if q:
        like = "%" + q + "%"
        ql = q.lower()
        nq = norm(q)
        conds = ["name LIKE ?", "artist LIKE ?", "characters LIKE ?"]
        params += [like, like, like]
        extra = []
        for g in artist_groups():
            if nq and any(nq in norm(n) for n in g):
                prim = pick_primary(g)
                if prim not in extra:
                    extra.append(prim)
        if extra:
            conds.append("artist IN (%s)" % ",".join("?" * len(extra)))
            params += extra
        # 拼音 / 拼音缩写命中：追加对应学院、角色、画师、特殊类型过滤
        ts = get_tag_stats()
        ac_hit, ch_hit, ar_hit, sp_hit = [], [], [], []
        for name, pin in ts.get("pinyin", {}).items():
            if pin["ab"].startswith(ql) or (len(ql) >= 2 and (ql in pin["py"] or ql in pin["ab"])):
                if name in ts["academies"]:
                    ac_hit.append(name)
                elif name in ts["characters"]:
                    ch_hit.append(name)
                elif name in ts["artists"]:
                    ar_hit.append(name)
                elif name in ts["specials"]:
                    sp_hit.append(name)
        # NFKC 归一化命中：数学粗体等 Unicode 变体（如 𝗞𝗶𝗿𝗼𝘀𝗵𝗶 → kiroshi）用普通写法也能搜到
        if nq and len(nq) >= 2:
            cand = []
            for key, bucket in (("artists", ar_hit), ("academies", ac_hit),
                                ("characters", ch_hit), ("specials", sp_hit)):
                for nm, cnt in ts.get(key, {}).items():
                    if nm == unicodedata.normalize("NFKC", nm):
                        continue          # 纯 ASCII/已是标准形：LIKE 已覆盖，不必重复
                    if nq in norm(nm) and nm not in bucket:
                        cand.append((cnt or 0, nm, bucket))
            cand.sort(key=lambda x: -x[0])
            for _cnt, nm, bucket in cand[:200]:
                bucket.append(nm)
        if ac_hit:
            conds.append("academy IN (%s)" % ",".join("?" * len(ac_hit)))
            params += ac_hit
        if ch_hit:
            ch_conds = []
            for c in ch_hit:
                qd = ts.get("char_qualified", {}).get(c)
                if qd:
                    ch_conds.append("(academy=? AND instr('|'||characters||'|', ?) > 0)")
                    params += [qd["academy"], "|" + qd["base"] + "|"]
                else:
                    ch_conds.append("instr('|'||characters||'|', ?) > 0")
                    params += ["|" + c + "|"]
            conds.append("(" + " OR ".join(ch_conds) + ")")
        if ar_hit:
            conds.append("artist IN (%s)" % ",".join("?" * len(ar_hit)))
            params += ar_hit
        if sp_hit:
            conds.append("(" + " OR ".join(["instr('|'||specials||'|', ?) > 0"] * len(sp_hit)) + ")")
            params += ["|" + s + "|" for s in sp_hit]
        wheres.append("(" + " OR ".join(conds) + ")")
    return " AND ".join(wheres), params, note


def _qualify_chars(academy, chars):
    """文件展示用：多学院角色（如杂鱼酱）在角色名前加学院名，如 千年杂鱼酱"""
    if not chars:
        return chars
    base = get_tag_stats().get("char_qualified_base", {})
    return [base.get(c, {}).get(academy or "", c) for c in chars]


def row_to_dict(r):
    return {
        "id": r["id"],
        "rel": r["rel"],
        "name": r["name"],
        "folder": r["folder"],
        "academy": r["academy"],
        "characters": _qualify_chars(r["academy"], _parse_csv(r["characters"])),
        "specials": _parse_csv(r["specials"]),
        "artist": r["artist"],
        "artist_raw": r["artist_raw"],
        "size": r["size"],
        "mtime": r["mtime"],
        "ctime": r["ctime"],
        "trashed_at": r["trashed_at"],
    }


# ---------------------------------------------------------------- 缩略图
_THUMB_CONCURRENCY = 4
_THUMB_SEM = threading.Semaphore(_THUMB_CONCURRENCY)


def make_thumb(rel):
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    out = THUMB_DIR / (h + ".webp")
    src = ROOT / rel
    if not src.exists():
        src = TRASH_DIR / rel
    if not src.exists():
        return None
    try:
        src_m = src.stat().st_mtime
    except OSError:
        src_m = 0
    if out.exists() and out.stat().st_mtime >= src_m:
        return out
    # 解码限流：网格一次会并发要好几十上百张缩略图，无上限地同时解码会把磁盘打满，
    # 与正在进行的全库扫描抢 I/O（“启动后第一次整理特别慢”的主因之一）。
    with _THUMB_SEM:
        if out.exists() and out.stat().st_mtime >= src_m:
            return out
        try:
            with Image.open(src) as im:
                try:
                    im.seek(0)
                except Exception:
                    pass
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                im.thumbnail((280, 280), Image.LANCZOS)
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                im.save(out, "WEBP", quality=82)
            return out
        except Exception:
            return None


def delete_thumb(rel):
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    p = THUMB_DIR / (h + ".webp")
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def send_to_recyclebin(path):
    """把文件移入操作系统回收站；失败时退回普通删除。"""
    try:
        from send2trash import send2trash
        send2trash(str(path))
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_SILENT = 0x4
        FOF_NOCONFIRMATION = 0x10
        FOF_NOERRORUI = 0x400
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = str(path) + "\0\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if ret == 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(str(path))
        return True
    except OSError:
        return False


def mark_trashed(rel, dst_path):
    """把文件标记为已删除；若库中无记录（如「未处理」文件）则补一条废纸篓记录。"""
    if query_one("SELECT 1 FROM files WHERE rel=?", (rel,)):
        execute("UPDATE files SET status='trashed', trashed_at=? WHERE rel=?", (time.time(), rel))
        return
    try:
        st = os.stat(dst_path)
        size, mtime, ctime = st.st_size, st.st_mtime, st.st_ctime
    except OSError:
        size = mtime = ctime = 0
    execute(
        "INSERT INTO files(rel,name,folder,academy,characters,specials,artist,artist_raw,size,mtime,ctime,status,trashed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'trashed',?)",
        (rel, dst_path.name, os.path.dirname(rel).replace("\\", "/"),
         "", "", "", "", "", size, mtime, ctime, time.time()),
    )


def _trash_overwritten(dst):
    """覆盖前把目标位置已有的同名文件移入废纸篓（保留记录，可在废纸篓中还原）。
    废纸篓记录使用独立 rel（原名加序号），避免与新文件同名导致记录被覆盖。"""
    dst = Path(dst)
    try:
        orig_rel = dst.relative_to(ROOT).as_posix()
    except ValueError:
        orig_rel = dst.name
    dst_trash = TRASH_DIR / orig_rel
    dst_trash.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        cand = dst_trash if n == 0 else dst_trash.with_name("%s_%d%s" % (dst_trash.stem, n, dst_trash.suffix))
        if not cand.exists() and not query_one(
                "SELECT 1 FROM files WHERE rel=?", (cand.relative_to(TRASH_DIR).as_posix(),)):
            dst_trash = cand
            break
        n += 1
    dst.rename(dst_trash)
    trash_rel = dst_trash.relative_to(TRASH_DIR).as_posix()
    if query_one("SELECT 1 FROM files WHERE rel=?", (orig_rel,)):
        execute(
            "UPDATE files SET rel=?, name=?, folder=?, status='trashed', trashed_at=? WHERE rel=?",
            (trash_rel, dst_trash.name, dst_trash.parent.relative_to(TRASH_DIR).as_posix(),
             time.time(), orig_rel),
        )
    else:
        mark_trashed(trash_rel, dst_trash)
    delete_thumb(orig_rel)


def _move_file(src, dst, copy=False):
    """移动/复制 src 到 dst；若 dst 已有同名文件，先将其移入废纸篓再覆盖。
    源与目标为同一文件时不做任何操作。"""
    src = Path(src)
    dst = Path(dst)
    if src.resolve() == dst.resolve():
        return
    if dst.exists():
        _trash_overwritten(dst)
    if copy:
        shutil.copy2(str(src), str(dst))
    else:
        src.rename(dst)


def purge_expired_trash():
    """把超过 30 天的废纸篓文件移入系统回收站并从库中删除记录。"""
    cutoff = time.time() - TRASH_EXPIRE_DAYS * 86400
    rows = query(
        "SELECT rel FROM files WHERE status='trashed' AND trashed_at > 0 AND trashed_at < ?",
        (cutoff,),
    )
    removed = 0
    for r in rows:
        p = TRASH_DIR / r["rel"]
        if p.exists() and not send_to_recyclebin(p):
            continue
        execute("DELETE FROM files WHERE rel=? AND status='trashed'", (r["rel"],))
        removed += 1
    if removed:
        invalidate_tags()
    return removed


# ---------------------------------------------------------------- Flask 路由
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    reload_config()
    return jsonify({
        "root": str(ROOT),
        "port": CONFIG["port"],
        "artist_min_files": CONFIG["artist_min_files"],
        "artist_threshold_ratio": CONFIG["artist_threshold_ratio"],
        "artist_threshold_min": CONFIG.get("artist_threshold_min", 3),
        "artist_threshold_mid1": CONFIG.get("artist_threshold_mid1", 3.5),
        "artist_threshold_mid": CONFIG.get("artist_threshold_mid", 4),
        "artist_threshold_mid2": CONFIG.get("artist_threshold_mid2", 4.5),
        "artist_threshold_split1": CONFIG.get("artist_threshold_split1", 300),
        "artist_threshold_split2": CONFIG.get("artist_threshold_split2", 500),
        "artist_threshold_split3": CONFIG.get("artist_threshold_split3", 600),
        "artist_weight_gif": CONFIG.get("artist_weight_gif", 1.5),
        "artist_weight_gif_large": CONFIG.get("artist_weight_gif_large", 2.0),
        "artist_weight_gif_large_mb": CONFIG.get("artist_weight_gif_large_mb", 5),
        "artist_folder_weight_cap": CONFIG.get("artist_folder_weight_cap", 8),
        "artist_folder_weight_ratio": CONFIG.get("artist_folder_weight_ratio", 0.4),
        "pair_characters": PAIR_CHARS,
        "exclude_dirs": CONFIG["exclude_dirs"],
        "exclude_segments": CONFIG["exclude_segments"],
    })


@app.post("/api/scan")
def api_scan():
    if SCAN_STATE["running"]:
        return jsonify({"ok": False, "error": "扫描正在进行中"}), 409
    run_scan_async()
    return jsonify({"ok": True})


@app.get("/api/scan/status")
def api_scan_status():
    return jsonify({"running": SCAN_STATE["running"], "progress": SCAN_STATE["progress"]})


@app.get("/api/tags")
def api_tags():
    return jsonify(get_tag_stats())


FACET_GROUPS = (("academy", "academies"), ("character", "characters"),
                ("artist", "artists"), ("special", "specials"),
                ("folder", "folders"), ("ext", "exts"))


def _facet_counts(where, params):
    """按 where 统计各学院/角色/画师/特殊类型还剩多少文件（还有总数、未标记数）。

    刻意复用 accumulate_tag_row + finalize_tag_stats：多学院角色加学院前缀、
    「无学院」归类、网页新建的标签等口径要和 /api/tags 完全一致，前端才能直接换用。
    """
    raw = _empty_tag_raw()
    total = 0
    for r in query("SELECT status, academy, characters, specials, artist, folder, name "
                   "FROM files WHERE " + where, params):
        accumulate_tag_row(raw, r, 1)
        total += 1
    stats = finalize_tag_stats(raw)
    return {"total": total, "untagged": raw["untagged"],
            "academies": stats["academies"], "characters": stats["characters"],
            "artists": stats["artists"], "specials": stats["specials"],
            "folders": stats["folders"], "exts": stats["exts"],
            "artist_aliases": stats["artist_aliases"],
            "char_qualified": stats["char_qualified"],
            "char_qualified_base": stats["char_qualified_base"]}


def _facet_cached(where, params):
    """_facet_counts 带缓存：筛选来回切换时不用重复统计。"""
    key = (where, tuple(params))
    hit = _FACET_CACHE.get(key)
    if hit is not None:
        return hit
    out = _facet_counts(where, params)
    if len(_FACET_CACHE) >= _FACET_CACHE_MAX:
        _FACET_CACHE.clear()
    _FACET_CACHE[key] = out
    return out


@app.get("/api/facets")
def api_facets():
    """当前筛选条件下各标签还剩多少文件：侧栏标签计数和「全部文件」跟着筛选变。

    已选的那一组单独去掉自己这一维再统计（多选是 OR，带着自己算会让同组别的标签全变 0）。
    """
    where, params, _note = build_file_filter()
    out = dict(_facet_cached(where, params))
    for arg, key in FACET_GROUPS:
        if request.args.getlist(arg):
            w2, p2, _ = build_file_filter(ignore={arg})
            out[key] = _facet_cached(w2, p2)[key]
    return jsonify(out)


ML_DIR = BASE_DIR / "ml"
_ML = {"key": None, "dim": 0, "classes": [], "centroids": [], "rows": None, "fh": None}
_ML_LOCK = threading.Lock()


def _ml_load():
    """懒加载 ml/ 下的特征索引；文件更新后自动重载（按 mtime）。没有索引返回 None。"""
    try:
        ip = ML_DIR / "index.txt"
        vp = ML_DIR / "vectors.bin"
        jp = ML_DIR / "proto.json"
        if not (ip.exists() and vp.exists() and jp.exists()):
            return None
        key = (ip.stat().st_mtime_ns, vp.stat().st_mtime_ns, jp.stat().st_mtime_ns)
        with _ML_LOCK:
            if _ML["key"] == key:
                return _ML
            raw = json.loads(jp.read_text(encoding="utf-8"))
            rows = ip.read_text(encoding="utf-8").splitlines()
            if _ML.get("fh"):
                try:
                    _ML["fh"].close()
                except Exception:
                    pass
            _ML.update({
                "key": key,
                "dim": int(raw.get("dim") or 128),
                "classes": raw.get("classes") or [],
                "centroids": raw.get("centroids") or [],
                "rows": {r: i for i, r in enumerate(rows)},
                "fh": open(str(vp), "rb"),
            })
            return _ML
    except Exception:
        return None


def _ml_suggest(rel, n=3):
    """返回 [{character, score}]。纯 Python 点积（210 类 × 128 维，约 3ms）。"""
    m = _ml_load()
    if not m:
        return []
    row = (m["rows"] or {}).get(rel)
    if row is None:
        return []
    d = m["dim"]
    try:
        with _ML_LOCK:
            m["fh"].seek(row * d * 2)
            buf = m["fh"].read(d * 2)
        q = struct.unpack("<%de" % d, buf)
    except Exception:
        return []
    out = []
    for name, c in zip(m["classes"], m["centroids"]):
        s = 0.0
        for a, b in zip(q, c):
            s += a * b
        out.append((s, name))
    out.sort(reverse=True)
    return [{"character": nm, "score": round(sc, 3)} for sc, nm in out[:n]]


@app.get("/api/suggest")
def api_suggest():
    """角色候选：只给排序建议，不写库；由前端点选后走 /api/tags。"""
    rel = safe_rel(request.args.get("rel", ""))
    try:
        n = int(request.args.get("n", 3))
    except ValueError:
        n = 3
    items = _ml_suggest(rel, max(1, min(n, 8)))
    # 分数和准确率强相关（实测 top 分 ≥0.35 时 top-1 精确率约 86%、top-3 约 94%），
    # 低于阈值的干脆不显示，免得给人不可信的候选。
    try:
        th = float(CONFIG.get("ml_suggest_min_score", 0.35))
    except (TypeError, ValueError):
        th = 0.35
    if not items or items[0]["score"] < th:
        items = []
    return jsonify({"ok": True, "items": items})


@app.post("/api/new_tag")
def api_new_tag():
    data = request.get_json(force=True)
    kind = (data.get("type") or "").strip()
    name = (data.get("name") or "").strip()
    if kind not in ("character", "artist"):
        return jsonify({"ok": False, "error": "未知标签类型"}), 400
    if not name:
        return jsonify({"ok": False, "error": "标签名不能为空"}), 400
    extra = get_extra_tags()
    if kind == "character":
        academy = (data.get("academy") or "").strip()
        if not academy:
            return jsonify({"ok": False, "error": "新建角色必须选择学院或输入新学院"}), 400
        extra.setdefault("characters", {})[name] = academy
        if academy not in extra.setdefault("academies", []):
            extra["academies"].append(academy)
    else:
        if name not in extra.setdefault("artists", []):
            extra["artists"].append(name)
    _save_json_file(EXTRA_TAGS_FILE, extra)
    _reset_alias_caches()
    _ensure_aliases_fresh(force=True)
    invalidate_tags()
    return jsonify({"ok": True})


@app.post("/api/artist_alias")
def api_artist_alias():
    data = request.get_json(force=True)
    if data.get("remove"):
        rm = (data.get("remove") or "").strip()
        extra = get_extra_aliases()
        groups = []
        changed = False
        retag = []
        for g in extra.get("groups", []):
            g2 = [n for n in g if n != rm]
            if len(g2) != len(g):
                changed = True
            if len(g2) >= 2:      # 只剩一个名字的分组不再有别名含义，顺手清掉
                groups.append(g2)
        if changed:
            extra["groups"] = groups
            prev = snapshot_primary_map()
            _save_json_file(EXTRA_ALIASES_FILE, extra)
            _reset_alias_caches()
            _ensure_aliases_fresh(force=True)
            retag_stored_artists(prev, retag)
            _retag_event.clear()
            invalidate_tags(sync=True)
        return jsonify({"ok": True, "retag": retag})
    names = [str(n).strip() for n in (data.get("names") or []) if str(n).strip()]
    names = list(dict.fromkeys(names))
    if len(names) < 2:
        return jsonify({"ok": False, "error": "至少需要两个画师名来建立关联"}), 400
    extra = get_extra_aliases()
    extra.setdefault("groups", [])
    target = None
    for g in extra["groups"]:
        gn = {norm(n) for n in g}
        if any(norm(n) in gn for n in names):
            target = g
            break
    if target is None:
        target = []
        extra["groups"].append(target)
    tn = {norm(n) for n in target}
    for n in names:
        if norm(n) not in tn:
            target.append(n)
            tn.add(norm(n))
    prev = snapshot_primary_map()
    _save_json_file(EXTRA_ALIASES_FILE, extra)
    _reset_alias_caches()
    _ensure_aliases_fresh(force=True)
    retag = []
    retag_stored_artists(prev, retag)
    _retag_event.clear()
    invalidate_tags(sync=True)
    return jsonify({"ok": True, "retag": retag})


@app.get("/api/files")
def api_files():
    where, params, note = build_file_filter()
    sort = request.args.get("sort", "default")
    dirn = (request.args.get("dir", "asc") or "asc").lower()
    limit = min(int(request.args.get("limit", 120)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    order_map = {
        "name": "name COLLATE NOCASE",
        "type": "lower(substr(name, instr(name, '.') + 1))",
        "size": "size",
        "ctime": "ctime",
        "mtime": "mtime",
    }
    if sort == "default":
        order = "academy, folder, name"
    elif sort in order_map:
        d = "DESC" if dirn == "desc" else "ASC"
        order = "%s %s, id" % (order_map[sort], d)
    else:
        order = "academy, folder, name"
    total = query_one("SELECT COUNT(*) c FROM files WHERE " + where, params)["c"]
    rows = query(
        "SELECT * FROM files WHERE %s ORDER BY %s LIMIT ? OFFSET ?" % (where, order),
        params + [limit, offset],
    )
    return jsonify({"total": total, "files": [row_to_dict(r) for r in rows], "note": note})


@app.get("/api/thumb")
def api_thumb():
    rel = safe_rel(request.args.get("rel", ""))
    p = make_thumb(rel)
    if not p:
        abort(404)
    resp = send_file(p, mimetype="image/webp")
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


@app.get("/api/file")
def api_file():
    rel = safe_rel(request.args.get("rel", ""))
    full = ROOT / rel
    if not full.exists():
        abort(404)
    return send_file(str(full))


@app.post("/api/rename")
def api_rename():
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    new_name = (data.get("name") or "").strip()
    if not new_name or new_name in (".", "..") or any(ch in new_name for ch in "/\\:*?\"<>|"):
        return jsonify({"ok": False, "error": "文件名不合法"}), 400
    src = ROOT / rel
    if not src.exists():
        return jsonify({"ok": False, "error": "源文件不存在"}), 404
    dst = src.parent / new_name
    if dst.exists():
        return jsonify({"ok": False, "error": "目标文件已存在"}), 409
    old_rel = rel
    src.rename(dst)
    new_rel = (dst.relative_to(ROOT)).as_posix()
    delete_thumb(old_rel)
    execute(
        "UPDATE files SET rel=?, name=?, folder=? WHERE rel=?",
        (new_rel, new_name, dst.parent.relative_to(ROOT).as_posix(), old_rel),
    )
    return jsonify({"ok": True, "rel": new_rel, "name": new_name})


@app.post("/api/trash")
def api_trash():
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    src = ROOT / rel
    if not src.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    dst = TRASH_DIR / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stem = dst.stem + "_" + str(int(time.time()))
        dst = dst.with_name(stem + dst.suffix)
    src.rename(dst)
    mark_trashed(rel, dst)
    invalidate_tags()
    return jsonify({"ok": True, "trash_rel": dst.relative_to(TRASH_DIR).as_posix()})


def _reveal_in_explorer(path, select=True):
    """在系统文件管理器里打开/定位路径（Windows 用资源管理器 /select 选中文件）。"""
    p = str(path)
    if os.name == "nt":
        # 传成单个参数，含空格的路径由 Python 自动加引号，explorer 能正确解析
        subprocess.Popen(["explorer", "/select," + p] if select else ["explorer", p])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", p] if select else ["open", p])
    else:
        subprocess.Popen(["xdg-open", p if not select else str(Path(p).parent)])


@app.post("/api/reveal")
def api_reveal():
    """打开文件所在的文件夹并选中它；文件已进废纸篓就定位废纸篓里的那份。"""
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    root = ROOT.resolve()
    live = (ROOT / rel).resolve()
    if root != live and root not in live.parents:
        return jsonify({"ok": False, "error": "路径不在库内"}), 400
    try:
        if live.exists():
            _reveal_in_explorer(live, select=True)
            return jsonify({"ok": True, "mode": "select"})
        # 已删除的文件：库里的 rel 仍是原路径，实际文件在废纸篓目录下
        trashed = (TRASH_DIR / rel).resolve()
        if trashed.exists():
            _reveal_in_explorer(trashed, select=True)
            return jsonify({"ok": True, "mode": "trash"})
        for fallback in (live.parent, trashed.parent):
            if fallback.exists():
                _reveal_in_explorer(fallback, select=False)
                return jsonify({"ok": True, "mode": "folder"})
    except OSError as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": "无法打开资源管理器：%s" % exc}), 500
    return jsonify({"ok": False, "error": "文件和所在文件夹都不存在"}), 404


@app.get("/api/trash")
def api_trash_list():
    purge_expired_trash()
    rows = query("SELECT * FROM files WHERE status='trashed' ORDER BY id DESC LIMIT 500")
    return jsonify({"files": [row_to_dict(r) for r in rows]})


@app.post("/api/trash/clear")
def api_trash_clear():
    rows = query("SELECT rel FROM files WHERE status='trashed'")
    cleared = 0
    for r in rows:
        p = TRASH_DIR / r["rel"]
        if p.exists() and not send_to_recyclebin(p):
            continue
        execute("DELETE FROM files WHERE rel=? AND status='trashed'", (r["rel"],))
        cleared += 1
    if cleared:
        invalidate_tags()
    return jsonify({"ok": True, "cleared": cleared})


@app.post("/api/restore_batch")
def api_restore_batch():
    data = request.get_json(force=True) or {}
    rels = [safe_rel(r) for r in (data.get("rels") or [])]
    restored = failed = 0
    for rel in rels:
        src = TRASH_DIR / rel
        dst = ROOT / rel
        if not src.exists() or dst.exists():
            failed += 1
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        execute("UPDATE files SET status='normal' WHERE rel=?", (rel,))
        restored += 1
    if restored:
        invalidate_tags()
    return jsonify({"ok": True, "restored": restored, "failed": failed})


@app.post("/api/restore")
def api_restore():
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    src = TRASH_DIR / rel
    dst = ROOT / rel
    if not src.exists():
        return jsonify({"ok": False, "error": "废纸篓中不存在该文件"}), 404
    if dst.exists():
        return jsonify({"ok": False, "error": "原位置已存在同名文件"}), 409
    src.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    execute("UPDATE files SET status='normal' WHERE rel=?", (rel,))
    invalidate_tags()
    return jsonify({"ok": True})


def auto_move_out(rel):
    """若文件位于画师文件夹内、但画师标签与文件夹画师不符（含已删除画师标签），立即移回池根目录。
    返回新 rel；未移动返回 None。"""
    row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
    if not row or row["status"] != "normal":
        return None
    pool, artist_seg = artist_folder_of(row["folder"])
    if not artist_seg:
        return None
    m = FOLDER_ARTIST_RE.match(artist_seg)
    inner = (m.group(2).strip().strip(" -") if m else "") or ""
    folder_prim = artist_primary(inner) if inner else ""
    if not folder_prim or folder_prim in ("未知", "杂烩"):
        return None
    f_prim = artist_primary(row["artist"]) if row["artist"] else ""
    if f_prim == folder_prim:
        return None
    src = ROOT / row["rel"]
    if not src.exists():
        return None
    dst = ROOT / pool / row["name"]
    _move_file(src, dst)
    new_rel = dst.relative_to(ROOT).as_posix()
    execute(
        "UPDATE files SET rel=?, name=?, folder=? WHERE rel=?",
        (new_rel, dst.name, pool, row["rel"]),
    )
    delete_thumb(row["rel"])
    _log_move("move_out", row["rel"], new_rel)
    return new_rel



def auto_move_in(rel):
    """若文件位于角色（池）根目录：带可归位特殊类型（铸币大头/差分/圣诞特供）的移入对应特殊目录；
    否则带画师标签、且该画师在池内已有画师文件夹的移入该文件夹。
    返回新 rel；未移动返回 None。"""
    row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
    if not row or row["status"] != "normal":
        return None
    pool, artist_seg = artist_folder_of(row["folder"])
    if not pool or artist_seg:
        return None
    tgt_dir = auto_special_dir(ROOT / pool, row["specials"], row["artist"])
    if tgt_dir:
        src = ROOT / row["rel"]
        if not src.exists():
            return None
        tgt_dir.mkdir(parents=True, exist_ok=True)
        dst = tgt_dir / row["name"]
        _move_file(src, dst)
        new_rel = dst.relative_to(ROOT).as_posix()
        tag = tag_for_rel(new_rel)
        merged_specials = list(dict.fromkeys(
            tag["specials"] + [x for x in (row["specials"] or "").split("|") if x]))
        merged_artist_raw = tag["artist_raw"] or row["artist_raw"]
        merged_artist = tag["artist"] or row["artist"]
        execute(
            "UPDATE files SET rel=?, name=?, folder=?, academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
            (new_rel, dst.name, dst.parent.relative_to(ROOT).as_posix(),
             tag["academy"], "|".join(tag["characters"]), "|".join(merged_specials),
             merged_artist, merged_artist_raw, row["rel"]),
        )
        delete_thumb(row["rel"])
        _log_move("move_in", row["rel"], new_rel)
        return new_rel
    if row["specials"]:
        return None
    f_prim = artist_primary(row["artist"]) if row["artist"] else ""
    if not f_prim or f_prim in ("未知", "杂烩"):
        return None
    tgt = find_artist_folder(ROOT / pool, f_prim)
    if not tgt:
        return None
    src = ROOT / row["rel"]
    if not src.exists():
        return None
    dst = tgt / row["name"]
    _move_file(src, dst)
    new_rel = dst.relative_to(ROOT).as_posix()
    tag = tag_for_rel(new_rel)
    execute(
        "UPDATE files SET rel=?, name=?, folder=?, academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
        (new_rel, dst.name, dst.parent.relative_to(ROOT).as_posix(),
         tag["academy"], "|".join(tag["characters"]), "|".join(tag["specials"]),
         tag["artist"], tag["artist_raw"], row["rel"]),
    )
    delete_thumb(row["rel"])
    _log_move("move_in", row["rel"], new_rel)
    return new_rel


def artist_folder_index():
    """一次遍历目录得到 {父目录相对路径: {norm(画师名): 文件夹路径}}。

    替代对同一角色目录反复 iterdir：扫描时 4119 个唯一（角色池, 画师）组合
    逐个列目录要 8s 以上，建一次索引只要不到 1s。
    """
    idx = {}
    exclude_dirs = set(CONFIG["exclude_dirs"])
    exclude_segs = set(CONFIG["exclude_segments"])
    for dirpath, dirnames, _files in os.walk(ROOT):
        parts = os.path.relpath(dirpath, ROOT).split(os.sep)
        if parts[0] != ".":
            if any(p in exclude_segs or p.startswith(".") for p in parts):
                dirnames[:] = []
                continue
            if parts[0] in exclude_dirs or parts[0] == INBOX_NAME:
                dirnames[:] = []
                continue
        dirnames[:] = [d for d in dirnames
                       if not (d in exclude_dirs or d in exclude_segs
                               or d.startswith(".") or d == "语音" or d == INBOX_NAME)]
        for d in dirnames:
            m = FOLDER_ARTIST_RE.match(d)
            if not (m and m.group(1).startswith("系列")):
                continue
            inner = m.group(2).strip().strip(" -")
            if not inner:
                continue
            parent = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
            idx.setdefault(parent, {})[norm(inner)] = Path(dirpath) / d
    return idx


def _dir_has_files(path):
    """递归判断目录里（含所有子目录）是否存在文件，找到一个就返回 True。"""
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_file():
                            return True
                        if e.is_dir():
                            stack.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def artist_folder_disk_map():
    """{角色池相对路径: {画师文件夹名: 里面有没有文件}}。

    注意“有没有文件”是**递归**判断的：像 系列6（喜欢睡觉的瓜瓜）/1.0/xxx.png 这种
    把图放在子目录里的文件夹不算空，否则会被误当成空文件夹清理掉。
    用来发现磁盘上存在、但数据库里没记录的画师文件夹——典型是被清空后留下的旧文件夹，
    它会让同一画师被重复建一个新文件夹。
    """
    out = {}
    exclude_dirs = set(CONFIG["exclude_dirs"])
    exclude_segs = set(CONFIG["exclude_segments"])
    for dirpath, dirnames, _files in os.walk(ROOT):
        parts = os.path.relpath(dirpath, ROOT).split(os.sep)
        if parts[0] != ".":
            if any(p in exclude_segs or p.startswith(".") for p in parts):
                dirnames[:] = []
                continue
            if parts[0] in exclude_dirs or parts[0] == INBOX_NAME:
                dirnames[:] = []
                continue
        keep, hits = [], []
        for d in dirnames:
            if d in exclude_dirs or d in exclude_segs or d.startswith(".") or d == "语音" or d == INBOX_NAME:
                continue
            mm = FOLDER_ARTIST_RE.match(d)
            if mm and mm.group(1).startswith("系列"):
                hits.append(d)
                continue          # 不再深入画师文件夹内部
            keep.append(d)
        dirnames[:] = keep
        if not hits:
            continue
        parent = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        bucket = out.setdefault(parent, {})
        for d in hits:
            bucket[d] = _dir_has_files(os.path.join(dirpath, d))
    return out


def pick_target_seg(artist, candidates):
    """同一画师有多个候选文件夹（别名写法不同 / 有被清空的旧文件夹）时选并入目标。

    优先级：有文件的 > 文件夹名就是画师主名的 > 系列序号小的。"""
    if not candidates:
        return ""
    an = norm(artist)

    def key(item):
        seg, has_files = item
        mm = FOLDER_ARTIST_RE.match(seg)
        inner = (mm.group(2).strip().strip(" -") if mm else "") or ""
        return (0 if has_files else 1, 0 if norm(inner) == an else 1, series_number(seg))
    return sorted(candidates, key=key)[0][0]


def auto_move_in_pool_loose(progress=None):
    """扫描/导入后自动归位：池根目录中带画师标签（且池内已有该画师文件夹）、或带可自动归位
    特殊类型（铸币大头/差分/圣诞特供）的文件，自动移入对应子目录。

    progress: 可选进度字典，写入 done/move_total 供前端进度条显示。
    """
    moved = 0
    like = " OR ".join(["artist <> ''"] + ["specials LIKE ?" for _ in AUTO_SPECIAL_SEGS])
    params = tuple("%" + x + "%" for x in AUTO_SPECIAL_SEGS)
    rows = query("SELECT * FROM files WHERE status='normal' AND (%s)" % like, params)
    idx = artist_folder_index()
    alias_cache = {}
    todo = []
    for r in rows:
        pool, artist_seg = artist_folder_of(r["folder"])
        if not pool or artist_seg:
            continue
        if auto_special_seg(r["specials"]):
            todo.append(r["rel"])
            continue
        prim = artist_primary(r["artist"])
        if not prim or prim in ("未知", "杂烩"):
            continue
        sub = idx.get(pool)
        if not sub:
            continue
        aliases = alias_cache.get(prim)
        if aliases is None:
            aliases = [norm(n) for n in alias_names_for(prim)]
            alias_cache[prim] = aliases
        if any(a in sub for a in aliases):
            todo.append(r["rel"])
    if progress is not None:
        progress["move_total"] = len(todo)
        progress["done"] = 0
    for i, rel in enumerate(todo, 1):
        if auto_move_in(rel):
            moved += 1
        if progress is not None and (i % 20 == 0 or i == len(todo)):
            progress["done"] = i
    return moved


def _pool_of_char(acad, folder_char):
    """计算角色文件夹路径：组合角色（桃/绿/光/望）进 学院/组合/成员。"""
    for pair, members in PAIR_CHARS.items():
        if folder_char in members:
            return "%s/%s/%s" % (acad, pair, folder_char)
    return "%s/%s" % (acad, folder_char)


def role_place_after_tag(row, chars):
    """角色标签变更后自动归位：已入库文件（非未处理/特殊目录）按角色标签移动/复制到对应角色文件夹。
    多角色时第一个文件移动、其余复制；画师子文件夹优先。返回 (最后新 rel 或 None, 移动数量)。"""
    if not chars:
        return None, 0
    pool, artist_seg = artist_folder_of(row["folder"])
    if not pool or pool == INBOX_NAME:
        return None, 0
    ts = get_tag_stats()
    acad_map = {}
    for ch in chars:
        qd = ts.get("char_qualified", {}).get(ch)
        if qd:
            acad_map[ch] = (qd["academy"], qd["base"])
            continue
        acads = ts["char_academy"].get(ch, [])
        if len(acads) == 1:
            acad_map[ch] = (acads[0], ch)
            continue
        if not acads:
            return None, 0
        cnts = ts.get("char_academy_counts", {}).get(ch, {})
        ranked = sorted(((cnts.get(a, 0), a) for a in acads), reverse=True)
        if len(ranked) >= 2 and ranked[0][0] == ranked[1][0]:
            return None, 0
        acad_map[ch] = (ranked[0][1], ch)
    if len(chars) == 1:
        acd, fch = acad_map[chars[0]]
        if _pool_of_char(acd, fch) == pool:
            return None, 0  # 已在目标角色池
    full = ROOT / row["rel"]
    if not full.exists():
        return None, 0
    specials = [x for x in (row["specials"] or "").split("|") if x]
    eff_artist = row["artist"] or tag_for_rel(row["rel"])["artist"]

    # 每个角色对应的目标文件路径
    plans = []
    for ch in chars:
        acd, fch = acad_map[ch]
        tpool = _pool_of_char(acd, fch)
        char_dir = ROOT / tpool
        char_dir.mkdir(parents=True, exist_ok=True)
        target_dir = char_dir
        sdir = auto_special_dir(char_dir, specials, eff_artist)
        if sdir:
            target_dir = sdir
            target_dir.mkdir(parents=True, exist_ok=True)
        elif not specials and eff_artist:
            tgt = find_artist_folder(char_dir, eff_artist)
            if tgt:
                target_dir = tgt
        plans.append((acd, fch, target_dir / full.name))
    # 原文件已位于某个角色的目标位置：保留原文件代表该角色，其余角色复制
    kept_idx = next((i for i, (_, _, dst) in enumerate(plans)
                     if dst.resolve() == full.resolve()), None)
    if kept_idx is None:
        with contextlib.closing(_connect()) as db:
            db.execute("DELETE FROM files WHERE rel=?", (row["rel"],))
            db.commit()
    moved = 0
    last_rel = None
    for i, (acd, fch, dst) in enumerate(plans):
        if i == kept_idx:
            tag0 = tag_for_rel(row["rel"])
            merged_specials = list(dict.fromkeys(tag0["specials"] + specials))
            merged_artist = eff_artist or tag0["artist"]
            execute(
                "UPDATE files SET academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
                (acd, fch, "|".join(merged_specials), merged_artist, merged_artist, row["rel"]),
            )
            delete_thumb(row["rel"])
            moved += 1
            last_rel = row["rel"]
            continue
        if i == len(plans) - 1 and kept_idx is None:
            _move_file(full, dst)
        else:
            _move_file(full, dst, copy=True)
        new_rel = dst.relative_to(ROOT).as_posix()
        tag = tag_for_rel(new_rel)
        merged_specials = list(dict.fromkeys(tag["specials"] + specials))
        merged_artist = eff_artist or tag["artist"]
        try:
            st = os.stat(dst)
        except OSError:
            st = None
        with contextlib.closing(_connect()) as db:
            db.execute(
                """
                INSERT INTO files(rel, name, folder, academy, characters, specials,
                                  artist, artist_raw, size, mtime, ctime, status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'normal')
                ON CONFLICT(rel) DO UPDATE SET
                  name=excluded.name, folder=excluded.folder, academy=excluded.academy,
                  characters=excluded.characters, specials=excluded.specials,
                  artist=excluded.artist, artist_raw=excluded.artist_raw,
                  size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, status='normal'
                """,
                (new_rel, dst.name, dst.parent.relative_to(ROOT).as_posix(), acd,
                 fch, "|".join(merged_specials), merged_artist, merged_artist,
                 st.st_size if st else 0, st.st_mtime if st else 0, st.st_ctime if st else 0),
            )
            db.commit()
        delete_thumb(row["rel"])
        moved += 1
        last_rel = new_rel
    return last_rel, moved


def reposition_after_tag(rel):
    """打标后自动归位：标签与所在画师文件夹不符 → 移出；在池根目录且画师有对应文件夹 → 移入。
    返回 (新 rel, 方向)；未移动返回 (None, None)。"""
    new_rel = auto_move_out(rel)
    if new_rel:
        inner = auto_move_in(new_rel)
        if inner:
            return inner, "in"
        return new_rel, "out"
    moved_in = auto_move_in(rel)
    if moved_in:
        return moved_in, "in"
    return None, None


@app.post("/api/tags")
def api_set_tags():
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
    if not row:
        return jsonify({"ok": False, "error": "记录不存在，请先扫描"}), 404
    chars = [str(c).strip() for c in (data.get("characters") or []) if str(c).strip()]
    chars = list(dict.fromkeys(chars))
    specials = [str(s).strip() for s in (data.get("specials") or []) if str(s).strip()]
    specials = list(dict.fromkeys(specials))
    artist = (data.get("artist") or "").strip()
    # 全部文件页侧栏打标：只保存标签、暂不归位；取消选中后由 /api/reposition 归位
    no_move = bool(data.get("no_move"))

    acad = row["academy"]
    if "academy" in data:
        acad = (data.get("academy") or "").strip()
    else:
        rels = get_tag_stats()
        norm, hints = [], set()
        for c in chars:
            qd = rels.get("char_qualified", {}).get(c)
            if qd:
                norm.append(qd["base"])
                hints.add(qd["academy"])
            else:
                norm.append(c)
        chars = list(dict.fromkeys(norm))
        if chars and not acad:
            if len(hints) == 1:
                acad = next(iter(hints))
            else:
                acads = set()
                for c in chars:
                    acads |= set(rels["char_academy"].get(c, []))
                if len(acads) == 1:
                    acad = next(iter(acads))
        elif chars:
            if len(hints) == 1:
                acad = next(iter(hints))
            else:
                valid = set()
                for c in chars:
                    valid |= set(rels["char_academy"].get(c, []))
                if valid and acad not in valid:
                    acad = next(iter(valid)) if len(valid) == 1 else ""

    old_chars = [x for x in (row["characters"] or "").split("|") if x]
    row_before = row
    execute(
        "UPDATE files SET academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
        (acad, "|".join(chars), "|".join(specials), artist, artist, rel),
    )
    row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
    invalidate_tags(delta=[(row_before, row)])
    moved = move_dir = None
    placed_role = 0
    if row and not no_move:
        new_chars = [x for x in (row["characters"] or "").split("|") if x]
        if set(old_chars) != set(new_chars):
            r_moved, placed_role = role_place_after_tag(row, new_chars)
            if r_moved:
                moved, move_dir = r_moved, "in"
        if not moved:
            moved, move_dir = reposition_after_tag(rel)
    if moved:
        row = query_one("SELECT * FROM files WHERE rel=?", (moved,))
    return jsonify({"ok": True, "file": row_to_dict(row), "moved": moved, "move_dir": move_dir, "placed_role": placed_role})


@app.post("/api/reposition")
def api_reposition():
    """取消选中后自动归位：按文件当前标签执行角色归位与画师文件夹移入/移出。"""
    data = request.get_json(force=True)
    rel = safe_rel(data.get("rel", ""))
    row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
    if not row or row["status"] != "normal":
        return jsonify({"ok": True, "file": None, "moved": None, "move_dir": None, "placed_role": 0})
    final_rel, moved, move_dir, placed_role = _finalize_placement(row, rel)
    row = query_one("SELECT * FROM files WHERE rel=?", (final_rel,))
    invalidate_tags()
    return jsonify({
        "ok": True,
        "file": row_to_dict(row) if row else None,
        "moved": moved,
        "move_dir": move_dir,
        "placed_role": placed_role,
    })


def _finalize_placement(row, rel):
    """对单个记录执行「角色归位 + 画师文件夹进出」，与 /api/reposition 单文件逻辑一致。
    返回 (最终 rel, 原 rel 或 None, 移动方向, 角色复制份数)。"""
    if not row or row["status"] != "normal":
        return rel, None, None, 0
    chars = [x for x in (row["characters"] or "").split("|") if x]
    moved = move_dir = None
    placed_role = 0
    if chars:
        r_moved, placed_role = role_place_after_tag(row, chars)
        if r_moved:
            moved, move_dir = r_moved, "in"
    if not moved:
        moved, move_dir = reposition_after_tag(rel)
    return (moved or rel), moved, move_dir, placed_role


@app.post("/api/reposition_batch")
def api_reposition_batch():
    """取消选中后批量自动归位：一次请求处理多个文件（减少往返、避免多次全量刷新）。"""
    data = request.get_json(force=True)
    rels = [safe_rel(r) for r in (data.get("rels") or []) if r]
    results = []
    for rel in rels:
        row = query_one("SELECT * FROM files WHERE rel=?", (rel,))
        if not row or row["status"] != "normal":
            results.append({"rel": rel, "file": None, "moved": None, "move_dir": None, "placed_role": 0})
            continue
        final_rel, moved, move_dir, placed_role = _finalize_placement(row, rel)
        row = query_one("SELECT * FROM files WHERE rel=?", (final_rel,))
        results.append({
            "rel": rel,
            "file": row_to_dict(row) if row else None,
            "moved": moved,
            "move_dir": move_dir,
            "placed_role": placed_role,
        })
    invalidate_tags()
    return jsonify({"ok": True, "results": results})


@app.get("/api/new")
def api_new_files():
    disk = set()
    for rel, full, size, mtime, ctime in iter_files():
        disk.add(rel)
    known = {r["rel"] for r in query("SELECT rel FROM files WHERE status IN ('normal','missing')")}
    new_rels = sorted(disk - known)
    auto, manual = [], []
    for rel in new_rels:
        tag = tag_for_rel(rel)
        item = {"rel": rel, "name": os.path.basename(rel), "tag": tag}
        (auto if tag["characters"] else manual).append(item)
    missing = sorted(known - disk)
    return jsonify({"auto": auto[:500], "manual": manual[:500], "missing": missing[:500]})


@app.post("/api/import_new")
def api_import_new():
    data = request.get_json(force=True)
    rels = [safe_rel(r) for r in (data.get("rels") or [])]
    inserted = 0
    rows = []
    for rel in rels:
        full = ROOT / rel
        if not full.exists():
            continue
        tag = tag_for_rel(rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        rows.append((rel, os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"),
                     tag["academy"], "|".join(tag["characters"]), "|".join(tag["specials"]),
                     tag["artist"], tag["artist_raw"], st.st_size, st.st_mtime, st.st_ctime))
    if rows:
        with contextlib.closing(_connect()) as db:
            db.executemany(
                """
                INSERT OR IGNORE INTO files(rel, name, folder, academy, characters, specials,
                                            artist, artist_raw, size, mtime, ctime, status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'normal')
                """,
                rows,
            )
            db.commit()
        inserted = len(rows)
    if inserted:
        auto_move_in_pool_loose()
    invalidate_tags()
    return jsonify({"ok": True, "inserted": inserted})


# ---------------------------------------------------------------- 画师后缀（未处理打画师标后写入文件名）
def artist_suffix_name(name, artist):
    """文件名缺画师标记时补 _@主名 后缀；已含该画师主名/任一别名则原样返回。"""
    if not artist:
        return name
    prim = artist_primary(artist)
    if not prim:
        return name
    stem, ext = os.path.splitext(name)
    nstem = norm(stem)
    if any(a and norm(a) in nstem for a in alias_names_for(prim)):
        return name
    # 移除末尾已有的 _@画师 / @画师 标记再补新主名，避免叠加
    stem2 = re.sub(r"(?:^|_)[@＠][^@＠.]+$", "", stem).rstrip(" _　")
    return "%s_@%s%s" % (stem2, prim, ext)


# ---------------------------------------------------------------- 未处理收件箱 / 上传 / 归位
@app.get("/api/inbox")
def api_inbox():
    items = []
    if INBOX_DIR.exists():
        for fn in sorted(os.listdir(INBOX_DIR)):
            full = INBOX_DIR / fn
            if full.is_file() and os.path.splitext(fn)[1].lower() in IMG_EXTS:
                rel = full.relative_to(ROOT).as_posix()
                artist = detect_artist_from_filename(fn)
                try:
                    st = os.stat(full)
                    size, mtime, ctime = st.st_size, st.st_mtime, st.st_ctime
                except OSError:
                    size = mtime = ctime = 0
                items.append({
                    "rel": rel, "name": fn, "folder": INBOX_NAME,
                    "artist": artist_primary(artist) if artist else "",
                    "artist_raw": artist, "size": size, "mtime": mtime, "ctime": ctime,
                })
    return jsonify({"files": items})


@app.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "未选择文件"}), 400
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = os.path.basename(f.filename or "").strip()
        if not name:
            continue
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        dst = INBOX_DIR / name
        if dst.exists():
            _trash_overwritten(dst)
        f.save(dst)
        saved.append({"rel": dst.relative_to(ROOT).as_posix(), "name": dst.name})
    invalidate_tags()
    return jsonify({"ok": True, "saved": saved})


@app.post("/api/place")
def api_place():
    data = request.get_json(force=True)
    rels_in = data.get("rels") or ([data.get("rel")] if data.get("rel") else [])
    rels = [safe_rel(r) for r in rels_in if r]
    if not rels:
        return jsonify({"ok": False, "error": "未指定文件"}), 400
    chars = [str(x).strip() for x in (data.get("characters") or []) if str(x).strip()]
    chars = list(dict.fromkeys(chars))
    if not chars:
        return jsonify({"ok": False, "error": "请至少选择一个角色"}), 400
    artist = (data.get("artist") or "").strip()
    specials = [str(s).strip() for s in (data.get("specials") or []) if str(s).strip()]
    specials = list(dict.fromkeys(specials))

    ts = get_tag_stats()
    acad_map = {}
    for ch in chars:
        qd = ts.get("char_qualified", {}).get(ch)
        if qd:
            acad_map[ch] = (qd["academy"], qd["base"])
            continue
        acads = ts["char_academy"].get(ch, [])
        if len(acads) == 1:
            acad_map[ch] = (acads[0], ch)
            continue
        if not acads:
            return jsonify({"ok": False, "error": "角色「%s」未关联学院，无法自动归位" % ch}), 400
        # 关联多个学院：取该角色文件数最多的学院（多数票），并列则无法自动归位
        cnts = ts.get("char_academy_counts", {}).get(ch, {})
        ranked = sorted(((cnts.get(a, 0), a) for a in acads), reverse=True)
        if len(ranked) >= 2 and ranked[0][0] == ranked[1][0]:
            return jsonify({"ok": False, "error": "角色「%s」关联多个学院且数量并列（%s），无法自动归位" % (ch, "、".join(acads))}), 400
        acad_map[ch] = (ranked[0][1], ch)

    placed = []
    skipped = []
    for rel0 in rels:
        full = ROOT / rel0
        if not full.exists() or not rel0.split("/")[0] == INBOX_NAME:
            skipped.append(rel0)
            continue
        eff_artist = artist or tag_for_rel(rel0)["artist"]
        rel = rel0
        # 画师打标后文件名缺该画师 → 先补 _@主名 后缀，再随角色归位
        if eff_artist:
            new_name = artist_suffix_name(full.name, eff_artist)
            if new_name != full.name:
                new_full = full.parent / new_name
                if new_full.exists():
                    _trash_overwritten(new_full)
                full.rename(new_full)
                rel = new_full.relative_to(ROOT).as_posix()
                full = new_full
                delete_thumb(rel0)
        for i, ch in enumerate(chars):
            acad, folder_char = acad_map[ch]
            pair_dir = ""
            for pair, members in PAIR_CHARS.items():
                if folder_char in members:
                    pair_dir = pair
                    break
            if pair_dir:
                char_dir = ROOT / acad / pair_dir / folder_char
            else:
                char_dir = ROOT / acad / folder_char
            char_dir.mkdir(parents=True, exist_ok=True)
            target_dir = char_dir
            sdir = auto_special_dir(char_dir, specials, eff_artist)
            if sdir:
                target_dir = sdir
                target_dir.mkdir(parents=True, exist_ok=True)
            elif not specials and eff_artist:
                tgt = find_artist_folder(char_dir, eff_artist)
                if tgt:
                    target_dir = tgt
            dst = target_dir / full.name
            if i == len(chars) - 1:
                _move_file(full, dst)
            else:
                _move_file(full, dst, copy=True)
            new_rel = dst.relative_to(ROOT).as_posix()
            tag = tag_for_rel(new_rel)
            merged_specials = list(dict.fromkeys(tag["specials"] + specials))
            merged_artist = artist or tag["artist"]
            try:
                st = os.stat(dst)
            except OSError:
                st = None
            with contextlib.closing(_connect()) as db:
                db.execute("DELETE FROM files WHERE rel=?", (rel0,))
                db.execute(
                    """
                    INSERT INTO files(rel, name, folder, academy, characters, specials,
                                      artist, artist_raw, size, mtime, ctime, status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,'normal')
                    ON CONFLICT(rel) DO UPDATE SET
                      name=excluded.name, folder=excluded.folder, academy=excluded.academy,
                      characters=excluded.characters, specials=excluded.specials,
                      artist=excluded.artist, artist_raw=excluded.artist_raw,
                      size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, status='normal'
                    """,
                    (new_rel, dst.name, dst.parent.relative_to(ROOT).as_posix(), acad,
                     folder_char, "|".join(merged_specials), merged_artist, merged_artist,
                     st.st_size if st else 0, st.st_mtime if st else 0, st.st_ctime if st else 0),
                )
                db.commit()
            delete_thumb(rel)
            placed.append(new_rel)
    invalidate_tags()
    return jsonify({"ok": True, "placed": placed, "skipped": skipped})


@app.post("/api/inbox_artist")
def api_inbox_artist():
    """未处理：仅打画师标（未选角色）时，把画师主名写入文件名后缀 _@主名。
    文件名已含该画师主名/别名则跳过；只重命名、不移出未处理。"""
    data = request.get_json(force=True)
    rels = [safe_rel(r) for r in (data.get("rels") or []) if r]
    artist = (data.get("artist") or "").strip()
    prim = artist_primary(artist) if artist else ""
    renamed = []
    failed = []
    for rel in rels:
        full = ROOT / rel
        if not full.exists() or not rel.split("/")[0] == INBOX_NAME:
            failed.append({"rel": rel, "error": "文件不存在或不在未处理"})
            continue
        if not prim:
            failed.append({"rel": rel, "error": "画师为空"})
            continue
        new_name = artist_suffix_name(full.name, prim)
        if new_name == full.name:
            continue  # 文件名已含该画师，无需追加
        dst = full.parent / new_name
        try:
            if dst.exists():
                _trash_overwritten(dst)
            full.rename(dst)
            delete_thumb(rel)
        except OSError as e:
            failed.append({"rel": rel, "error": str(e)})
            continue
        renamed.append({"old_rel": rel, "rel": dst.relative_to(ROOT).as_posix(), "name": dst.name})
    return jsonify({"ok": True, "renamed": renamed, "failed": failed})


def _norm_chars_and_hints(chars, ts):
    """把可能带学院前缀的多学院角色名归一到基础名。返回 (去重基础名列表, 学院提示集合)。"""
    norm, hints = [], set()
    for c in (chars or []):
        c = str(c).strip()
        if not c:
            continue
        qd = ts.get("char_qualified", {}).get(c)
        if qd:
            norm.append(qd["base"])
            hints.add(qd["academy"])
        else:
            norm.append(c)
    return list(dict.fromkeys(norm)), hints


def _resolve_acad_for(row, chars, hints, ts):
    """按角色/学院推断 academy（与 api_set_tags 逻辑一致：角色变更后若原学院不再匹配则更新/清空）。"""
    acad = row["academy"] or ""
    if not chars:
        return acad
    if len(hints) == 1:
        return next(iter(hints))
    valid = set()
    for ch in chars:
        valid |= set(ts.get("char_academy", {}).get(ch, []))
    if valid and acad not in valid:
        return next(iter(valid)) if len(valid) == 1 else ""
    return acad


@app.post("/api/tags_batch")
def api_tags_batch():
    data = request.get_json(force=True)
    rels = [safe_rel(r) for r in (data.get("rels") or []) if r]
    rows_in = data.get("rows")
    no_move = bool(data.get("no_move"))
    ts = get_tag_stats()
    updated = 0
    targets = []
    before_rows = {}
    with contextlib.closing(_connect()) as db:
        if isinstance(rows_in, list) and rows_in:
            # 全量设置模式（多选侧栏批量打标）：每行 characters/specials/artist 即最终值
            for item in rows_in:
                if not isinstance(item, dict):
                    continue
                rel = safe_rel(str(item.get("rel") or "").strip())
                if not rel or rel.split("/")[0] == INBOX_NAME:
                    continue
                row = db.execute("SELECT * FROM files WHERE rel=?", (rel,)).fetchone()
                if not row or row["status"] != "normal":
                    continue
                before_rows[rel] = row
                chars, hints = _norm_chars_and_hints(item.get("characters") or [], ts)
                specials = [str(x).strip() for x in (item.get("specials") or []) if str(x).strip()]
                specials = list(dict.fromkeys(specials))
                artist = (item.get("artist") or "").strip()
                if "academy" in item:
                    acad = (item.get("academy") or "").strip()
                else:
                    acad = _resolve_acad_for(row, chars, hints, ts)
                db.execute(
                    "UPDATE files SET academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
                    (acad, "|".join(chars), "|".join(specials), artist, artist, rel),
                )
                updated += 1
                targets.append(rel)
        else:
            chars, hints = _norm_chars_and_hints(data.get("characters") or [], ts)
            specials = [str(x).strip() for x in (data.get("specials") or []) if str(x).strip()]
            specials = list(dict.fromkeys(specials))
            artist = (data.get("artist") or "").strip()
            for rel in rels:
                if rel.split("/")[0] == INBOX_NAME:
                    continue
                row = db.execute("SELECT * FROM files WHERE rel=?", (rel,)).fetchone()
                if not row or row["status"] != "normal":
                    continue
                before_rows[rel] = row
                cur_chars = _parse_csv(row["characters"])
                cur_specials = _parse_csv(row["specials"])
                new_chars = list(dict.fromkeys(cur_chars + chars))
                new_specials = list(dict.fromkeys(cur_specials + specials))
                new_artist = artist or row["artist"]
                acad = _resolve_acad_for(row, new_chars, hints, ts)
                db.execute(
                    "UPDATE files SET academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
                    (acad, "|".join(new_chars), "|".join(new_specials), new_artist, new_artist, rel),
                )
                updated += 1
                targets.append(rel)
        db.commit()
    new_rows = {}
    if targets:
        ph = ",".join("?" * len(targets))
        for r in query("SELECT * FROM files WHERE rel IN (%s)" % ph, targets):
            new_rows[r["rel"]] = r
    invalidate_tags(delta=[(before_rows.get(rel), new_rows.get(rel)) for rel in targets])
    moved = 0
    if not no_move:
        for rel in targets:
            row = new_rows.get(rel)
            if row and _finalize_placement(row, rel)[1]:
                moved += 1
    files = []
    if no_move and targets:
        files = [row_to_dict(new_rows[rel]) for rel in targets if rel in new_rows]
    return jsonify({"ok": True, "updated": updated, "moved": moved, "files": files})


@app.post("/api/trash_batch")
def api_trash_batch():
    data = request.get_json(force=True)
    rels = [safe_rel(r) for r in (data.get("rels") or [])]
    moved = 0
    for rel in rels:
        src = ROOT / rel
        if not src.exists():
            continue
        dst = TRASH_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst = dst.with_name(dst.stem + "_" + str(int(time.time())) + dst.suffix)
        src.rename(dst)
        mark_trashed(rel, dst)
        delete_thumb(rel)
        moved += 1
    invalidate_tags()
    return jsonify({"ok": True, "moved": moved})


# ---------------------------------------------------------------- 整理预览（dry-run）
def series_number(name):
    m = re.match(r"系列(\d+)", name)
    return int(m.group(1)) if m else 0


def _file_weight(r):
    """gif 按大小加权（>10MB ×2，≤10MB ×1.5），其余计 1。"""
    if os.path.splitext(r["name"])[1].lower() != ".gif":
        return 1.0
    mb = float(CONFIG.get("artist_weight_gif_large_mb", 5))
    if r["size"] > mb * 1024 * 1024:
        return float(CONFIG.get("artist_weight_gif_large", 2.0))
    return float(CONFIG.get("artist_weight_gif", 1.5))


def artist_folder_of(folder):
    """返回 (pool, artist_seg)。artist_seg 为 系列N（画师） 段，无则 None。
    含特殊类型段（差分/铸币大头/圣诞特供）的路径返回 (None, None)，不参与整理。"""
    segs = folder.split("/")
    for i in range(len(segs) - 1, -1, -1):
        m = FOLDER_ARTIST_RE.match(segs[i])
        if m and m.group(1).startswith("系列"):
            pool = "/".join(segs[:i])
            if any(_is_special_seg(s) for s in pool.split("/")):
                return None, None
            return pool, segs[i]
    if any(_is_special_seg(s) for s in segs):
        return None, None
    return folder, None


def find_artist_folder(char_dir, artist):
    """在角色目录下查找该画师已有的 系列N（画师） 文件夹；没有则返回 None。"""
    if not artist:
        return None
    aliases = {norm(n) for n in alias_names_for(artist)}
    try:
        entries = sorted(char_dir.iterdir()) if char_dir.is_dir() else []
    except OSError:
        return None
    for d in entries:
        if not d.is_dir():
            continue
        m = FOLDER_ARTIST_RE.match(d.name)
        if m and m.group(1).startswith("系列"):
            inner = m.group(2).strip().strip(" -")
            if inner and norm(inner) in aliases:
                return d
    return None


def _renumber_apply(base, pool, pending):
    stamp = int(time.time())
    tmp_pairs = []
    try:
        for k, (old_name, _new_name) in enumerate(pending):
            tmp = base / ("__renumber_%d_%d__" % (k, stamp))
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            (base / old_name).rename(tmp)
            tmp_pairs.append((tmp, old_name, _new_name))
        for tmp, old_name, new_name in tmp_pairs:
            tmp.rename(base / new_name)
            _renumber_rows(pool, old_name, new_name)
    except OSError:
        # 重排失败时尽力把临时目录改回原名，不影响其它整理结果
        for tmp, old_name, _new_name in tmp_pairs:
            if tmp.exists() and not (base / old_name).exists():
                try:
                    tmp.rename(base / old_name)
                except OSError:
                    pass
        raise
    return len(pending)


def renumber_pool(pool):
    """把角色池中的 系列N（画师） 文件夹按现有顺序重排为 系列1、系列2…

    文件夹被占用（正在浏览/生成缩略图）时改名会失败，这里最多重试 3 次，
    每次重试都重新读一遍目录，避免上次失败留下的半成品影响判断。
    """
    base = ROOT / pool
    if not base.is_dir():
        return 0
    last_exc = None
    for attempt in range(3):
        items = []
        for d in os.listdir(base):
            m = FOLDER_ARTIST_RE.match(d)
            if m and m.group(1).startswith("系列"):
                items.append((series_number(d), d, m.group(2)))
        items.sort(key=lambda x: x[0])
        pending = []
        for i, (_, old_name, inner) in enumerate(items, start=1):
            new_name = "系列%d（%s）" % (i, inner)
            if new_name != old_name:
                pending.append((old_name, new_name))
        if not pending:
            return 0
        try:
            return _renumber_apply(base, pool, pending)
        except OSError as exc:
            last_exc = exc
            time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return 0


def _renumber_rows(pool, old_name, new_name):
    """系列N 文件夹改名后，更新其下所有文件的 DB 记录（rel/folder）并失效缩略图。"""
    old_pre = pool + "/" + old_name
    new_pre = pool + "/" + new_name
    rows = query("SELECT id, rel, folder FROM files WHERE rel LIKE ? OR folder LIKE ?",
                 (old_pre + "%", old_pre + "%"))
    n = 0
    for r in rows:
        new_rel = new_pre + r["rel"][len(old_pre):] if r["rel"].startswith(old_pre) else r["rel"]
        new_folder = new_pre + r["folder"][len(old_pre):] if r["folder"].startswith(old_pre) else r["folder"]
        execute("UPDATE files SET rel=?, folder=? WHERE id=?", (new_rel, new_folder, r["id"]))
        delete_thumb(r["rel"])
        _log_move("renumber", r["rel"], new_rel)
        n += 1
    return n


def pair_info(pool):
    """若 pool 是 桃绿/光望 的子池，返回 (pair, member)；否则 (None, None)。"""
    segs = pool.split("/")
    if len(segs) >= 3 and segs[-2] in PAIR_CHARS and segs[-1] in PAIR_CHARS[segs[-2]]:
        return segs[-2], segs[-1]
    return None, None


def _is_special_seg(seg):
    """差分/铸币大头/圣诞特供 目录（含 差分（X） 形式）。"""
    if seg in SPECIAL_FOLDERS:
        return True
    m = FOLDER_ARTIST_RE.match(seg)
    return bool(m and m.group(1) in SPECIAL_FOLDERS)


def artist_threshold_for(total_w):
    """加权总数 <300 → 3；300–500 → 3.5；500–600 → 4；≥600 → 4.5。"""
    s1 = float(CONFIG.get("artist_threshold_split1", 300))
    s2 = float(CONFIG.get("artist_threshold_split2", 500))
    s3 = float(CONFIG.get("artist_threshold_split3", 600))
    if total_w >= s3:
        return float(CONFIG.get("artist_threshold_mid2", 4.5))
    if total_w >= s2:
        return float(CONFIG.get("artist_threshold_mid", 4))
    if total_w >= s1:
        return float(CONFIG.get("artist_threshold_mid1", 3.5))
    return float(CONFIG.get("artist_threshold_min", 3))


def _weighted_rows(rows):
    total = 0.0
    per = {}
    for r in rows:
        w = _file_weight(r)
        total += w
        if r["artist"]:
            k = artist_primary(r["artist"])
            per[k] = per.get(k, 0) + w
    return total, per


def organize_preview(scope=None):
    """按第 5/6 条规则预览画师文件夹的创建/解散/重命名。
    scope: 可选池路径前缀，仅统计该范围内的池。"""
    reload_config()
    rows = query("SELECT * FROM files WHERE status='normal'")
    pools = {}  # pool -> {"direct": [rows], "folders": {seg: [rows]}}
    for r in rows:
        pool, artist_seg = artist_folder_of(r["folder"])
        if not pool:
            continue  # 差分/铸币大头/圣诞特供 等特殊目录不参与
        if auto_special_seg(r["specials"]):
            continue  # 铸币大头/差分/圣诞特供 自动归位到特殊子目录，不计入画师文件夹整理
        if scope and not pool.startswith(scope):
            continue
        p = pools.setdefault(pool, {"direct": [], "folders": {}})
        if artist_seg:
            p["folders"].setdefault(artist_seg, []).append(r)
        else:
            p["direct"].append(r)

    suggestions = {"create": [], "remove": [], "rename": []}
    disk_folders = artist_folder_disk_map()
    used_segs = set()

    def pool_own(pool):
        p = pools.get(pool, {"direct": [], "folders": {}})
        per = {}
        total = 0.0
        cap = float(CONFIG.get("artist_folder_weight_cap", 8))
        ratio = float(CONFIG.get("artist_folder_weight_ratio", 0.4))
        # 零散文件：按实际权重计入总数，并累计画师权重（用于创建/解散判定）
        for r in p["direct"]:
            w = _file_weight(r)
            total += w
            if r["artist"]:
                a = artist_primary(r["artist"])
                per[a] = per.get(a, 0) + w
        # 画师文件夹：按 0.4×文件夹权重计入总数，最多计入 cap（默认 10）
        for frows in p["folders"].values():
            fw, fper = _weighted_rows(frows)
            total += min(fw * ratio, cap)
            for a, w in fper.items():
                per[a] = per.get(a, 0) + w
        return total, per

    def effective(pool):
        """成员池（桃/绿/光/望）= 自身 + 组合池（桃绿/光望）。"""
        own = pool_own(pool)
        pair, member = pair_info(pool)
        if pair and member != pair:
            comb = "/".join(pool.split("/")[:2] + [pair])
            cw, cper = pool_own(comb)
            per = dict(own[1])
            for k, v in cper.items():
                per[k] = per.get(k, 0) + v
            return own[0] + cw, per
        return own

    def label_of(pool):
        pair, member = pair_info(pool)
        if pair:
            return member
        return pool.split("/")[-1] if pool.count("/") else pool

    for pool in sorted(pools):
        p = pools[pool]
        label = label_of(pool)
        total_w, per_w = effective(pool)
        threshold = artist_threshold_for(total_w)
        direct_w = _weighted_rows(p["direct"])[1]

        # 现有画师文件夹（按主名聚合）
        existing = {}
        for seg, frows in p["folders"].items():
            m = FOLDER_ARTIST_RE.match(seg)
            inner = (m.group(2).strip().strip(" -") if m else "") or ""
            prim = artist_primary(inner) if inner else ""
            existing.setdefault(prim, []).append((seg, frows))

        # 磁盘上存在、但数据库里没有记录的画师文件夹（文件已被移走/记录为缺失，或是空文件夹）：
        # 并入时要认得出来，空文件夹还要提示清理，否则同一画师会被重复建一个新文件夹
        disk_extra = {}
        empty_segs = []
        for seg, has_files in (disk_folders.get(pool) or {}).items():
            if seg in p["folders"]:
                continue
            mm = FOLDER_ARTIST_RE.match(seg)
            inner = (mm.group(2).strip().strip(" -") if mm else "") or ""
            prim = artist_primary(inner) if inner else ""
            if not prim:
                continue
            disk_extra.setdefault(prim, []).append((seg, has_files))
            if not has_files:
                empty_segs.append((seg, prim))

        # 解散：文件夹内文件权重低于阈值的解散；未知/杂烩 低于阈值时解散但不改名
        # 桃/绿/光/望 成员池：按「自身 + 组合池」的画师有效权重判定（绿=绿+桃绿）
        # 桃绿/光望 组合池：与该画师在任一成员池的累计加权都低于阈值才解散
        pair, member = pair_info(pool)
        member_pool = bool(pair and member != pair)
        comb_pool = bool(pair and member == pair)
        member_pers = {}
        if comb_pool:
            for m in PAIR_CHARS[pair]:
                if m == pair:
                    continue
                mp = "/".join(pool.split("/")[:-1] + [m])
                member_pers[m] = pool_own(mp)[1]
        dissolving = set()
        for prim, segs in sorted(existing.items(), key=lambda kv: kv[0]):
            for seg, frows in segs:
                folder_w = _weighted_rows(frows)[0]
                if member_pool:
                    check_w = max(folder_w, per_w.get(prim, 0))
                elif comb_pool:
                    comb_w = per_w.get(prim, 0)
                    check_w = folder_w
                    for mper in member_pers.values():
                        check_w = max(check_w, comb_w + mper.get(prim, 0))
                else:
                    check_w = folder_w
                if check_w >= threshold:
                    continue
                dissolving.add(seg)
                no_rename = prim in ("未知", "杂烩")
                rename_n = 0
                if not no_rename:
                    aliases = alias_names_for(prim)
                    for r in frows:
                        stem = os.path.splitext(r["name"])[0]
                        if not any(norm(a) and norm(a) in norm(stem) for a in aliases):
                            rename_n += 1
                suggestions["remove"].append({
                    "pool": pool, "character": label, "folder": seg,
                    "artist": prim, "count": len(frows),
                    "weight": round(folder_w, 1),
                    "reason": "杂项/未知，按阈值解散" if no_rename else "数量不足",
                    "no_rename": no_rename, "rename_n": rename_n,
                    "threshold": threshold,
                    "id": json.dumps(["remove", pool, seg], ensure_ascii=False),
                })

        # 创建：直接放在池根目录、且有效权重达标的画师
        for artist, w in sorted(direct_w.items(), key=lambda kv: -kv[1]):
            if per_w.get(artist, 0) < threshold:
                continue
            candidates = [(seg, bool(frows)) for seg, frows in existing.get(artist, [])]
            candidates += disk_extra.get(artist, [])
            target_seg = pick_target_seg(artist, candidates)
            if target_seg:
                used_segs.add(target_seg)
            exists = bool(candidates)
            files = [r for r in p["direct"] if artist_primary(r["artist"]) == artist]
            aliases = alias_names_for(artist)
            rename_n = sum(
                1 for r in files
                if not any(norm(a) and norm(a) in norm(os.path.splitext(r["name"])[0]) for a in aliases)
            )
            suggestions["create"].append({
                "pool": pool, "character": label, "artist": artist,
                "folder": target_seg, "existing": exists,
                "count": len(files), "weight": round(per_w.get(artist, 0), 1),
                "threshold": threshold, "rename_n": rename_n,
                "id": json.dumps(["create", pool, artist], ensure_ascii=False),
            })

        # 空画师文件夹（没有文件、数据库里也没记录）：提示清理，
        # 避免下次又给同一画师建一个新文件夹；被选作并入目标的跳过
        for seg, prim in empty_segs:
            if seg in used_segs:
                continue
            suggestions["remove"].append({
                "pool": pool, "character": label, "folder": seg,
                "artist": prim, "count": 0, "weight": 0.0,
                "reason": "空文件夹（没有文件）", "no_rename": True, "rename_n": 0,
                "threshold": threshold, "empty": True,
                "id": json.dumps(["remove", pool, seg], ensure_ascii=False),
            })

    return {
        "suggestions": suggestions,
        "config": {
            "artist_threshold_min": float(CONFIG.get("artist_threshold_min", 3)),
            "artist_threshold_mid1": float(CONFIG.get("artist_threshold_mid1", 3.5)),
            "artist_threshold_mid": float(CONFIG.get("artist_threshold_mid", 4)),
            "artist_threshold_mid2": float(CONFIG.get("artist_threshold_mid2", 4.5)),
            "artist_threshold_split1": float(CONFIG.get("artist_threshold_split1", 300)),
            "artist_threshold_split2": float(CONFIG.get("artist_threshold_split2", 500)),
            "artist_threshold_split3": float(CONFIG.get("artist_threshold_split3", 600)),
            "artist_weight_gif": CONFIG.get("artist_weight_gif", 1.5),
            "artist_weight_gif_large": CONFIG.get("artist_weight_gif_large", 2.0),
            "artist_weight_gif_large_mb": CONFIG.get("artist_weight_gif_large_mb", 5),
            "artist_folder_weight_cap": CONFIG.get("artist_folder_weight_cap", 8),
            "artist_folder_weight_ratio": CONFIG.get("artist_folder_weight_ratio", 0.4),
        },
    }


@app.get("/api/organize")
def api_organize():
    scope = request.args.get("scope") or None
    return jsonify(organize_preview(scope=scope))


def organize_apply(scope=None, ids=None, items=None, progress_cb=None):
    """执行整理：创建/并入画师文件夹、解散画师文件夹、按画师名补文件名。
    items: 前端选中的建议项（含 type/pool/artist/folder 等），优先级最高，不再重算整库预览；
    ids: 可选，仅执行指定 id 的建议（需重算预览）；均缺省 = 执行全部建议。"""
    stats = {"create": 0, "move_in": 0, "remove": 0, "dissolve_out": 0, "rename": 0}

    def selected(item):
        return ids is None or item["id"] in ids

    if items is None:
        preview = organize_preview(scope=scope)
        s = preview["suggestions"]
        items = ([dict(it, type="create") for it in s["create"]]
                 + [dict(it, type="remove") for it in s["remove"]])
        items = [it for it in items if selected(it)]

    def update_row_after_move(old_rel, dst, action="organize"):
        new_rel = dst.relative_to(ROOT).as_posix()
        tag = tag_for_rel(new_rel)
        execute(
            "UPDATE files SET rel=?, name=?, folder=?, academy=?, characters=?, specials=?, artist=?, artist_raw=? WHERE rel=?",
            (new_rel, dst.name, dst.parent.relative_to(ROOT).as_posix(),
             tag["academy"], "|".join(tag["characters"]), "|".join(tag["specials"]),
             tag["artist"], tag["artist_raw"], old_rel),
        )
        delete_thumb(old_rel)
        _log_move(action, old_rel, new_rel)

    def create_folder(pool, artist, files, target_seg=""):
        base = ROOT / pool
        if not target_seg:
            nums = [series_number(d) for d in os.listdir(base)]
            n = (max(nums) if nums else 0) + 1
            target_seg = "系列%d（%s）" % (n, artist)
        tdir = base / target_seg
        tdir.mkdir(parents=True, exist_ok=True)
        aliases = alias_names_for(artist)
        moved = 0
        for r in files:
            src = ROOT / r["rel"]
            if not src.exists():
                continue
            name = r["name"]
            stem = os.path.splitext(name)[0]
            if not any(norm(a) and norm(a) in norm(stem) for a in aliases):
                name = artist + "-" + name
                stats["rename"] += 1
            dst = tdir / name
            _move_file(src, dst)
            update_row_after_move(r["rel"], dst, "create_folder")
            moved += 1
        return target_seg, moved

    def dissolve_folder(pool, seg):
        base = ROOT / pool
        fdir = base / seg
        if not fdir.exists():
            return 0
        m = FOLDER_ARTIST_RE.match(seg)
        inner = (m.group(2).strip().strip(" -") if m else "") or ""
        prim = artist_primary(inner) if inner else ""
        no_rename = not prim or prim in ("未知", "杂烩")
        aliases = alias_names_for(prim) if not no_rename else []
        rows_here = query(
            "SELECT * FROM files WHERE status='normal' AND folder LIKE ?",
            (pool + "/" + seg + "%",),
        )
        moved = 0
        for r in rows_here:
            src = ROOT / r["rel"]
            if not src.exists():
                continue
            name = r["name"]
            if not no_rename:
                stem = os.path.splitext(name)[0]
                if not any(norm(a) and norm(a) in norm(stem) for a in aliases):
                    name = prim + "-" + name
                    stats["rename"] += 1
            dst = base / name
            _move_file(src, dst)
            update_row_after_move(r["rel"], dst, "dissolve_folder")
            moved += 1
        # 只有在确实没有任何文件残留时才删目录：数据库里没登记的散图不会被连带删除
        if _dir_has_files(fdir):
            _log_move("dissolve_keep", pool + "/" + seg, "目录内仍有未登记文件，保留")
        else:
            shutil.rmtree(fdir, ignore_errors=True)
        return moved

    affected_pools = set()
    for i, item in enumerate(items):
        if progress_cb:
            progress_cb(i + 1, len(items))
        if item.get("type") == "remove":
            moved = dissolve_folder(item["pool"], item["folder"].rsplit("/", 1)[-1] if "/" in item["folder"] else item["folder"])
            if moved:
                stats["remove"] += 1
                stats["dissolve_out"] += moved
            affected_pools.add(item["pool"])
        else:
            p = pools_direct(item["pool"], item["artist"])
            if not p:
                continue
            _, moved = create_folder(item["pool"], item["artist"], p, item.get("folder") or "")
            stats["create"] += 1
            stats["move_in"] += moved
            affected_pools.add(item["pool"])
    stats["renumber"] = 0
    failed_pools = []
    for pool in affected_pools:
        try:
            stats["renumber"] += renumber_pool(pool)
        except OSError as exc:
            # 单个角色池重排失败（文件夹被占用等）不能影响其它池的处理
            failed_pools.append(pool)
            _log_move("renumber_failed", pool, str(exc))
    stats["renumber_failed"] = len(failed_pools)
    stats["renumber_failed_pools"] = failed_pools[:20]
    invalidate_tags()
    return {"ok": True, "stats": stats}


def pools_direct(pool, artist):
    """取某池中直接放置的、属于该画师（主名）的文件。"""
    rows = query(
        "SELECT * FROM files WHERE status='normal' AND folder=?",
        (pool,),
    )
    return [r for r in rows if r["artist"] and artist_primary(r["artist"]) == artist
            and not auto_special_seg(r["specials"])]


ORGANIZE_STATE = {"running": False, "progress": None, "result": None, "error": None}


def _organize_worker(scope, ids, items):
    try:
        total = len(items) if items is not None else 0
        def cb(done, n):
            ORGANIZE_STATE["progress"] = {"stage": "整理中", "done": done, "total": n}
        ORGANIZE_STATE["progress"] = {"stage": "整理中", "done": 0, "total": total}
        result = organize_apply(scope=scope, ids=ids, items=items, progress_cb=cb)
        ORGANIZE_STATE["result"] = result
        ORGANIZE_STATE["progress"] = {"stage": "完成", "done": total, "total": total}
    except Exception as exc:  # noqa: BLE001
        ORGANIZE_STATE["error"] = str(exc)
        ORGANIZE_STATE["progress"] = {"stage": "出错", "done": 0, "total": 0}
    finally:
        ORGANIZE_STATE["running"] = False


@app.post("/api/organize/apply")
def api_organize_apply():
    if ORGANIZE_STATE["running"]:
        return jsonify({"ok": False, "error": "整理正在执行中，请稍候"}), 409
    data = request.get_json(force=True) or {}
    scope = (data.get("scope") or "").strip() or None
    ids = data.get("ids")
    if ids is not None and not isinstance(ids, (list, tuple, set)):
        ids = [ids]
    items = data.get("items")
    if items is not None and not isinstance(items, list):
        items = [items]
    total = len(items) if items else (len(ids) if ids else 0)
    ORGANIZE_STATE.update(running=True, progress={"stage": "准备", "done": 0, "total": total},
                          result=None, error=None)
    threading.Thread(target=_organize_worker, args=(scope, ids, items), daemon=True).start()
    return jsonify({"ok": True, "running": True})


@app.get("/api/organize/apply/status")
def api_organize_status():
    return jsonify({
        "running": ORGANIZE_STATE["running"],
        "progress": ORGANIZE_STATE["progress"],
        "result": ORGANIZE_STATE["result"],
        "error": ORGANIZE_STATE["error"],
    })


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(500)
def api_error(e):
    return jsonify({"ok": False, "error": getattr(e, "description", str(e))}), getattr(e, "code", 500) or 500


# ---------------------------------------------------------------- 缩略图预热
def warmup_thumbs(n=240):
    try:
        rows = query(
            "SELECT rel FROM files WHERE status='normal' ORDER BY academy, folder, name LIMIT ?",
            (n,),
        )
        for i, r in enumerate(rows):
            # 扫描在跑就先让路：预热只是提前缓存，没必要跟全库扫描抢磁盘
            while SCAN_STATE["running"]:
                time.sleep(0.5)
            make_thumb(r["rel"])
            if i and i % 80 == 0:
                print("缩略图预热 %d/%d…" % (i, len(rows)), flush=True)
        print("缩略图预热完成")
    except Exception as exc:  # noqa: BLE001
        print("缩略图预热失败:", exc)


# ---------------------------------------------------------------- 入口
def main():
    init_db()
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        removed = purge_expired_trash()
        if removed:
            print("废纸篓自动清理: %d 个超过 %d 天的文件已移入系统回收站" % (removed, TRASH_EXPIRE_DAYS))
    except Exception as exc:  # noqa: BLE001
        print("废纸篓自动清理失败:", exc)
    count = query_one("SELECT COUNT(*) c FROM files")["c"]
    if count == 0:
        print("首次运行，后台开始全量扫描，可稍后刷新页面…")
        run_scan_async()
    else:
        threading.Thread(target=warmup_thumbs, daemon=True).start()
    url = "http://%s:%s" % (CONFIG["host"], CONFIG["port"])
    print("=" * 56)
    print("表情包整理 Demo 已启动: %s" % url)
    print("关闭本窗口即停止服务。Ctrl+C 退出。")
    print("=" * 56)
    if "--open" in os.sys.argv:
        webbrowser.open(url)
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True)


if __name__ == "__main__":
    main()
