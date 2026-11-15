# -*- coding: utf-8 -*-
"""
ComfyUI Output -> GitHub (miku-gallery) 近七天画廊 + 分类管理 (喜爱/保留/不喜欢) + 远程重命名双向同步脚本

核心功能：
1. 双向状态同步 (`gallery_state.json`)：
   - 拉取 GitHub 云端最新的 `gallery_state.json`（兼容迁移旧版 `likes.json`），汇总手机/平板/其他电脑上的修改。
   - **远程重命名同步**：网页端对任意图片重命名后，同步时自动在本地执行安全重命名（保留原扩展名、防重名冲突），同步更新缩略图与元数据缓存。
   - **分类变更与取消**：支持对每张图片在 `未分类 (none)`、`❤️ 喜爱 (like)`、`📌 保留 (keep)`、`👎 不喜欢 (dislike)` 之间自由切换与取消：
     * `喜爱`：本地移入 `output/喜爱/`，云端永久保留（不受 7 天过期限制）。
     * `保留`：自动新建并移入 `output/保留/`，云端永久保留（不受 7 天过期限制）。
     * `未分类`（含取消喜爱/取消保留）：移回 `output/` 根目录，按近 7 天窗口同步。
     * `不喜欢`：同步时在本地执行删除（优先移入 Windows 回收站，清理 `output/`），并从云端与索引中移除。
   - 也支持直接在本地电脑拖拽文件进/出 `喜爱/` 与 `保留/` 文件夹，同步时会自动识别并更新云端分类。
2. 元数据提取与缩略图：
   - 提取 ComfyUI 的提示词（含 DynamicPrompts / ImpactWildcard 展开）、Seed、大模型、LoRA、采样器、步数、CFG、分辨率等。
   - 生成轻量级 WebP 缩略图 (`thumbs/`) 供其他设备秒开预览。
3. 精确 Git 索引同步与分批上传：
   - 仅跟踪并上传「近 7 天根目录原图 + 喜爱文件夹原图 + 保留文件夹原图 + 缩略图 + 网页与索引」。
   - 本地超过 7 天的未分类旧图会自动从 GitHub 远端索引移除以瘦身，但【绝不删除本地硬盘上的任何未标记“不喜欢”的历史原图】。
"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from PIL import Image

# ================= 配置区域 =================
DEFAULT_DAYS = 7
BATCH_SIZE_MB = 80  # 每批次推送的最大体积 (MB)，避免代理或 HTTP 超时
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
THUMB_DIR_NAME = "thumbs"
FAV_DIR_NAME = "喜爱"
KEEP_DIR_NAME = "保留"
STATE_FILE_NAME = "gallery_state.json"
LEGACY_LIKES_FILE_NAME = "likes.json"
LOCAL_APPLIED_FILE_NAME = ".sync_applied.json"
THUMB_MAX_WIDTH = 560
THUMB_QUALITY = 80

OUTPUT_DIR = Path(__file__).resolve().parent
COMFY_ROOT = OUTPUT_DIR.parent
CUSTOM_NODES_DIR = COMFY_ROOT / "custom_nodes"

CATEGORY_DIRS = {
    "none": OUTPUT_DIR,
    "like": OUTPUT_DIR / FAV_DIR_NAME,
    "keep": OUTPUT_DIR / KEEP_DIR_NAME,
}
VALID_CATEGORIES = {"none", "like", "keep", "dislike"}
RESERVED_WIN_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 尝试加载 comfyui-dynamicprompts 以还原 DPCombinatorialGenerator 的真实提示词
dp_generator = None
if CUSTOM_NODES_DIR.exists():
    sys.path.insert(0, str(CUSTOM_NODES_DIR))
    try:
        from importlib import import_module
        dp_mod = import_module("comfyui-dynamicprompts.nodes.combinatorial")
        dp_generator = dp_mod.DPCombinatorialGenerator()
    except Exception:
        dp_generator = None


def log(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def detect_local_proxy() -> str:
    """自动检测环境变量或 Windows 注册表中的本地代理设置"""
    for env_key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if os.environ.get(env_key):
            return os.environ[env_key]
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enable:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server:
                    return server if "://" in server else f"http://{server}"
    except Exception:
        pass
    return ""


def run_git(args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.quotepath=false"] + args
    return subprocess.run(
        cmd,
        cwd=str(OUTPUT_DIR),
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def delete_local_file(path: Path) -> bool:
    """将本地文件删除（优先静默移入 Windows 回收站，失败时退回直接 unlink）"""
    if not path.exists():
        return True
    try:
        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        op = _SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = str(path.resolve()) + "\0\0"
        op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if res == 0 and not path.exists():
            return True
    except Exception:
        pass

    try:
        path.unlink(missing_ok=True)
        return not path.exists()
    except Exception as e:
        log(f"警告: 删除本地文件失败 {path}: {e}")
        return False


def sanitize_filename(new_name: str, orig_suffix: str) -> str:
    """清理非法字符、保留原扩展名并避免 Windows 保留名冲突"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (new_name or "").strip())
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return ""

    # 若用户输入了不同或缺少扩展名，保留图片原始后缀以防格式损坏
    lower_suf = orig_suffix.lower()
    if cleaned.lower().endswith(lower_suf):
        stem = cleaned[: -len(orig_suffix)]
    else:
        # 若末尾带了其他常见图片后缀，去掉后补回原后缀；否则直接补原后缀
        stem = re.sub(r"\.(png|jpe?g|webp)$", "", cleaned, flags=re.IGNORECASE)

    stem = stem.strip(" .")
    if not stem:
        return ""
    if stem.upper() in RESERVED_WIN_NAMES:
        stem = f"_{stem}"

    # 限制长度避免超过 Windows MAX_PATH
    max_stem_len = 160
    if len(stem) > max_stem_len:
        stem = stem[:max_stem_len].rstrip(" .")
    return f"{stem}{orig_suffix}"


def find_existing_anywhere(filename: str) -> Path | None:
    """检查同一文件名是否已存在于 output 根目录、喜爱或保留目录"""
    for folder in CATEGORY_DIRS.values():
        candidate = folder / filename
        if candidate.exists():
            return candidate
    return None


def make_unique_filename(desired_name: str, self_path: Path) -> str:
    """若目标文件名已被其他文件占用，自动生成不冲突的名称 (_1, _2...)"""
    p = Path(desired_name)
    stem, suffix = p.stem, p.suffix
    candidate = desired_name
    counter = 1
    while True:
        existing = find_existing_anywhere(candidate)
        if existing is None or existing.resolve() == self_path.resolve():
            return candidate
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1


def load_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_state_dict(raw: dict, fallback_likes: dict | None = None) -> dict[str, dict]:
    """统一解析 gallery_state.json，必要时兼容迁移旧版 likes.json"""
    items: dict[str, dict] = {}
    if raw and isinstance(raw.get("items"), dict):
        for name, entry in raw["items"].items():
            if not isinstance(entry, dict):
                continue
            cat = entry.get("category", "none")
            if cat not in VALID_CATEGORIES:
                cat = "none"
            rename_to = (entry.get("rename_to") or "").strip()
            ts = int(entry.get("updated_ts") or 0)
            items[name] = {
                "category": cat,
                "rename_to": rename_to,
                "updated_ts": ts,
            }
        return items

    if fallback_likes and isinstance(fallback_likes, dict):
        for name in fallback_likes.get("liked", []):
            items[name] = {"category": "like", "rename_to": "", "updated_ts": 0}
        for name in fallback_likes.get("unliked", []):
            items.setdefault(name, {"category": "none", "rename_to": "", "updated_ts": 0})
    return items


def fetch_remote_state() -> tuple[dict[str, dict], bool]:
    """拉取 origin/main 并读取云端 gallery_state.json（或旧版 likes.json），返回 (items, fetched_ok)"""
    fetch_res = run_git(["fetch", "origin", "main"], check=False)
    fetched_ok = fetch_res.returncode == 0
    if not fetched_ok:
        log("提示: 拉取远端状态失败（离线或网络问题），将仅基于本地状态继续。")

    res_state = run_git(["show", f"origin/main:{STATE_FILE_NAME}"], check=False)
    if res_state.returncode == 0 and res_state.stdout.strip():
        try:
            return parse_state_dict(json.loads(res_state.stdout)), fetched_ok
        except Exception:
            pass

    res_likes = run_git(["show", f"origin/main:{LEGACY_LIKES_FILE_NAME}"], check=False)
    if res_likes.returncode == 0 and res_likes.stdout.strip():
        try:
            return parse_state_dict({}, json.loads(res_likes.stdout)), fetched_ok
        except Exception:
            pass

    return {}, fetched_ok


def scan_disk_images() -> dict[str, tuple[Path, str]]:
    """扫描 output/、output/喜爱/、output/保留/ 中的所有图片文件"""
    disk_files: dict[str, tuple[Path, str]] = {}
    # 优先扫描分类子目录，再扫描根目录，避免同名重复覆盖分类目录
    for cat in ("like", "keep", "none"):
        folder = CATEGORY_DIRS[cat]
        if not folder.exists():
            continue
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in IMAGE_EXTS:
                continue
            if entry.name.startswith("clipspace-") or entry.name.startswith("ComfyUI_temp_"):
                continue
            if entry.name not in disk_files:
                disk_files[entry.name] = (entry, cat)
    return disk_files


def should_apply_remote_entry(
    name: str,
    r_entry: dict | None,
    prev_cat: str,
    applied_entry: dict | None,
) -> bool:
    """判断远端记录是否包含尚未在本地应用的新操作（重命名或分类更改/取消）"""
    if not r_entry:
        return False
    # 若该远端指令完全等于上次已本地应用过但可能尚未成功推送的指令，则不再重复应用
    if applied_entry and applied_entry == r_entry:
        return False
    if r_entry.get("rename_to"):
        return True
    if r_entry.get("category", "none") != prev_cat:
        return True
    return False


def sync_categories_and_renames(cache_map: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """
    核心双向同步：
    1. 合并云端 gallery_state.json 与本地目录实际状态
    2. 执行本地重命名（及缩略图/缓存迁移）
    3. 将「喜爱」移入 output/喜爱/，将「保留」移入 output/保留/，取消分类移回 output/
    4. 将「不喜欢」本地删除（移入回收站）
    返回 (最终 state_items, 更新后的 applied_map)
    """
    for cat in ("like", "keep"):
        CATEGORY_DIRS[cat].mkdir(parents=True, exist_ok=True)

    state_path = OUTPUT_DIR / STATE_FILE_NAME
    legacy_likes_path = OUTPUT_DIR / LEGACY_LIKES_FILE_NAME
    applied_path = OUTPUT_DIR / LOCAL_APPLIED_FILE_NAME
    snapshot_path = OUTPUT_DIR / "gallery_data.json"

    prev_snapshot_raw = load_json_safe(snapshot_path)
    prev_cat_map: dict[str, str] = {}
    for item in prev_snapshot_raw.get("images", []):
        if "name" in item:
            cat = item.get("category")
            if not cat:
                cat = "like" if item.get("liked") else "none"
            prev_cat_map[item["name"]] = cat

    local_state_items = parse_state_dict(
        load_json_safe(state_path),
        load_json_safe(legacy_likes_path),
    )
    remote_state_items, _ = fetch_remote_state()
    applied_map = load_json_safe(applied_path)
    disk_files = scan_disk_images()
    thumb_dir = OUTPUT_DIR / THUMB_DIR_NAME

    all_keys = set(remote_state_items) | set(local_state_items) | set(disk_files)
    final_items: dict[str, dict] = {}
    new_applied: dict[str, dict] = {}

    stats = {"renamed": 0, "to_like": 0, "to_keep": 0, "to_none": 0, "deleted": 0}

    for name in sorted(all_keys):
        r_entry = remote_state_items.get(name)
        l_entry = local_state_items.get(name)
        prev_cat = prev_cat_map.get(name, "none")

        if name not in disk_files:
            # 本地磁盘上已不存在（可能早已被删除或手动改名），记住已应用的远端记录以免反复处理
            if r_entry and applied_map.get(name) == r_entry:
                new_applied[name] = r_entry
            continue

        src_path, disk_cat = disk_files[name]

        # 选择应当应用的指令来源：远端网页更新 > 本地未推送状态 > 本地文件夹手动移动
        use_remote = should_apply_remote_entry(name, r_entry, prev_cat, applied_map.get(name))
        use_local_state = (not use_remote) and l_entry is not None and (
            bool(l_entry.get("rename_to")) or l_entry.get("category", "none") != prev_cat
        )

        if use_remote:
            chosen = r_entry
            target_cat = chosen.get("category", "none")
            desired_rename = chosen.get("rename_to", "")
            op_ts = chosen.get("updated_ts") or int(time.time())
            new_applied[name] = dict(r_entry)
        elif use_local_state:
            chosen = l_entry
            target_cat = chosen.get("category", "none")
            desired_rename = chosen.get("rename_to", "")
            op_ts = chosen.get("updated_ts") or int(time.time())
        else:
            # 云端无新改动时，若用户在本地直接把图片拖进了 喜爱/ 或 保留/ 或拖回根目录，则以本地目录为准
            target_cat = disk_cat
            desired_rename = ""
            op_ts = (l_entry or {}).get("updated_ts") or int(time.time())

        # 处理「不喜欢」：同步时本地删除
        if target_cat == "dislike":
            log(f"  [🗑️ 不喜欢删除] 正在删除本地文件: {src_path.relative_to(OUTPUT_DIR)}")
            if delete_local_file(src_path):
                stats["deleted"] += 1
                old_thumb = thumb_dir / f"{src_path.stem}.webp"
                if old_thumb.exists():
                    old_thumb.unlink(missing_ok=True)
                cache_map.pop(name, None)
            if r_entry:
                new_applied[name] = dict(r_entry)
            continue

        # 计算目标文件名（含远程重命名）
        target_name = src_path.name
        if desired_rename:
            sanitized = sanitize_filename(desired_rename, src_path.suffix)
            if sanitized and sanitized != src_path.name:
                target_name = make_unique_filename(sanitized, src_path)

        dst_dir = CATEGORY_DIRS.get(target_cat, OUTPUT_DIR)
        dst_path = dst_dir / target_name

        # 执行本地移动或重命名
        if src_path.resolve() != dst_path.resolve():
            # 若目标文件夹内已存在同名文件且不是自己，防止覆盖
            if dst_path.exists():
                if dst_path.stat().st_size == src_path.stat().st_size and int(dst_path.stat().st_mtime) == int(src_path.stat().st_mtime):
                    src_path.unlink()
                else:
                    target_name = make_unique_filename(target_name, src_path)
                    dst_path = dst_dir / target_name
                    shutil.move(str(src_path), str(dst_path))
            else:
                shutil.move(str(src_path), str(dst_path))

            if src_path.name != dst_path.name:
                stats["renamed"] += 1
                log(f"  [✏️ 重命名] {src_path.name} -> {dst_path.name}")
                # 同步重命名本地缩略图与缓存条目
                old_thumb = thumb_dir / f"{src_path.stem}.webp"
                new_thumb = thumb_dir / f"{dst_path.stem}.webp"
                if old_thumb.exists() and not new_thumb.exists():
                    try:
                        shutil.move(str(old_thumb), str(new_thumb))
                    except Exception:
                        pass
                if src_path.name in cache_map:
                    cached = dict(cache_map.pop(src_path.name))
                    cached["name"] = dst_path.name
                    cache_map[dst_path.name] = cached

            if target_cat != disk_cat:
                if target_cat == "like":
                    stats["to_like"] += 1
                    log(f"  [❤️ 喜爱] 已移入「{FAV_DIR_NAME}」文件夹: {dst_path.name}")
                elif target_cat == "keep":
                    stats["to_keep"] += 1
                    log(f"  [📌 保留] 已移入「{KEEP_DIR_NAME}」文件夹: {dst_path.name}")
                else:
                    stats["to_none"] += 1
                    log(f"  [📥 取消分类] 已移回 output 根目录: {dst_path.name}")

        # 只将明确分类为 like / keep 的条目写入 gallery_state.json，保持状态文件精简
        if target_cat in ("like", "keep"):
            final_items[dst_path.name] = {
                "category": target_cat,
                "rename_to": "",
                "updated_ts": op_ts,
            }

    if any(stats.values()):
        log(
            f"本地同步完成: 重命名 {stats['renamed']} 张, 移入「{FAV_DIR_NAME}」{stats['to_like']} 张, "
            f"移入「{KEEP_DIR_NAME}」{stats['to_keep']} 张, 移回根目录 {stats['to_none']} 张, 删除「不喜欢」{stats['deleted']} 张。"
        )

    # 写入最新本地 state 与应用记录
    state_payload = {
        "version": 2,
        "updated_ts": int(time.time()),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": dict(sorted(final_items.items())),
    }
    state_path.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    applied_path.write_text(json.dumps(new_applied, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return final_items, new_applied


def extract_comfy_metadata(
    img_path: Path,
    rel_path: str,
    category: str,
    cached_entry: dict | None = None,
) -> dict:
    """提取图片的尺寸、时间与 ComfyUI 节点元数据"""
    st = img_path.stat()
    mtime = int(st.st_mtime)
    fsize = st.st_size

    if cached_entry and cached_entry.get("mtime") == mtime and cached_entry.get("size") == fsize:
        entry = dict(cached_entry)
        entry["name"] = img_path.name
        entry["path"] = rel_path
        entry["category"] = category
        entry["liked"] = category == "like"
        return entry

    info = {
        "name": img_path.name,
        "path": rel_path,
        "category": category,
        "liked": category == "like",
        "mtime": mtime,
        "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
        "day": time.strftime("%Y-%m-%d", time.localtime(mtime)),
        "size": fsize,
        "width": 0,
        "height": 0,
        "model": "",
        "loras": [],
        "seed": None,
        "steps": None,
        "cfg": None,
        "sampler": "",
        "scheduler": "",
        "denoise": None,
        "positive": "",
        "positive_template": "",
        "negative": "",
        "upscale": "",
        "facedetailer": False,
        "has_workflow": False,
    }

    try:
        with Image.open(img_path) as im:
            info["width"], info["height"] = im.size
            info["has_workflow"] = ("workflow" in im.info) or ("prompt" in im.info)

            if "prompt" in im.info:
                p = json.loads(im.info["prompt"])

                def resolve_text(val, depth=0) -> str:
                    if depth > 6:
                        return ""
                    if isinstance(val, str):
                        return val.strip()
                    if isinstance(val, list) and len(val) >= 1:
                        node_id = str(val[0])
                        node = p.get(node_id, {})
                        ctype = node.get("class_type", "")
                        inp = node.get("inputs", {})
                        if ctype == "ImpactWildcardProcessor":
                            w_text = (inp.get("wildcard_text") or "").strip()
                            p_text = (inp.get("populated_text") or "").strip()
                            if w_text and w_text != p_text:
                                info["positive_template"] = w_text
                            return p_text or w_text
                        elif ctype == "DPCombinatorialGenerator":
                            raw_t = (inp.get("text") or "").strip()
                            idx = inp.get("index", 0)
                            info["positive_template"] = f"{raw_t}\n[Combinatorial Index: {idx}]"
                            if dp_generator and raw_t:
                                try:
                                    res_t, _ = dp_generator.get_prompt(raw_t, int(idx))
                                    if res_t:
                                        return res_t.strip()
                                except Exception:
                                    pass
                            return raw_t
                        else:
                            for k in ("populated_text", "text", "string", "prompt", "wildcard_text", "text_g", "text_l"):
                                if k in inp:
                                    res = resolve_text(inp[k], depth + 1)
                                    if res:
                                        return res
                    return ""

                for nid, node in p.items():
                    ctype = node.get("class_type", "")
                    inp = node.get("inputs", {})
                    if "CheckpointLoader" in ctype or "UNETLoader" in ctype:
                        ckpt = inp.get("ckpt_name") or inp.get("unet_name") or ""
                        if ckpt:
                            info["model"] = ckpt
                    elif "LoraLoader" in ctype:
                        lname = inp.get("lora_name", "")
                        s_mod = inp.get("strength_model", 1.0)
                        if lname:
                            info["loras"].append(f"{lname} ({s_mod})")
                    elif ctype == "LatentUpscaleBy":
                        m = inp.get("upscale_method", "")
                        s = inp.get("scale_by", "")
                        info["upscale"] = f"{m} {s}x".strip()
                    elif "FaceDetailer" in ctype:
                        info["facedetailer"] = True

                ksamplers = []
                for nid, node in p.items():
                    ctype = node.get("class_type", "")
                    if "KSampler" in ctype or "SamplerCustom" in ctype:
                        ksamplers.append((int(nid) if str(nid).isdigit() else 9999, node.get("inputs", {})))

                ksamplers.sort(key=lambda x: (0 if x[1].get("denoise", 1.0) == 1.0 else 1, x[0]))
                if ksamplers:
                    kinp = ksamplers[0][1]
                    info["seed"] = kinp.get("seed") if kinp.get("seed") is not None else kinp.get("noise_seed")
                    info["steps"] = kinp.get("steps")
                    info["cfg"] = kinp.get("cfg")
                    info["sampler"] = kinp.get("sampler_name", "")
                    info["scheduler"] = kinp.get("scheduler", "")
                    info["denoise"] = kinp.get("denoise")

                    pos_link = kinp.get("positive")
                    neg_link = kinp.get("negative")
                    if isinstance(pos_link, list) and str(pos_link[0]) in p:
                        pos_node = p[str(pos_link[0])]
                        info["positive"] = resolve_text(pos_node.get("inputs", {}).get("text", pos_link))
                    if isinstance(neg_link, list) and str(neg_link[0]) in p:
                        neg_node = p[str(neg_link[0])]
                        info["negative"] = resolve_text(neg_node.get("inputs", {}).get("text", neg_link))

            if info["seed"] is None and "Seed-" in img_path.name:
                m = re.search(r"Seed-(\d+)", img_path.name)
                if m:
                    info["seed"] = int(m.group(1))
    except Exception as e:
        log(f"警告: 读取图片元数据失败 {img_path.name}: {e}")

    return info


def ensure_thumbnail(img_path: Path, thumb_dir: Path, rel_fallback: str) -> str:
    """为原图生成轻量级 WebP 缩略图，返回相对路径"""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_name = f"{img_path.stem}.webp"
    thumb_path = thumb_dir / thumb_name
    rel_thumb = f"{THUMB_DIR_NAME}/{thumb_name}"

    try:
        if thumb_path.exists() and thumb_path.stat().st_mtime >= img_path.stat().st_mtime:
            return rel_thumb

        with Image.open(img_path) as im:
            w, h = im.size
            if w > THUMB_MAX_WIDTH:
                new_h = int(h * (THUMB_MAX_WIDTH / w))
                im_resized = im.resize((THUMB_MAX_WIDTH, new_h), Image.Resampling.LANCZOS)
            else:
                im_resized = im.copy()

            if im_resized.mode not in ("RGB", "RGBA"):
                im_resized = im_resized.convert("RGB")

            im_resized.save(thumb_path, "WEBP", quality=THUMB_QUALITY, method=4)
        return rel_thumb
    except Exception as e:
        log(f"警告: 生成缩略图失败 {img_path.name}: {e}")
        return rel_fallback


def get_ahead_behind() -> tuple[int, int]:
    rev = run_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], check=False)
    if rev.returncode != 0 or not rev.stdout.strip():
        return 0, 0
    parts = rev.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def fast_forward_if_no_ahead() -> None:
    """若本地没有未推送提交且云端有网页操作提交，先无损快进索引指针"""
    ahead, behind = get_ahead_behind()
    if ahead == 0 and behind > 0:
        log("检测到云端有新的网页端操作提交，正在快进对齐本地 Git 索引...")
        run_git(["reset", "--mixed", "origin/main"], check=True)


def merge_remote_if_diverged() -> None:
    """若本地有未推送提交且云端同时有网页操作提交，在本地提交完成后用 -s ours 无冲突合并指针"""
    ahead, behind = get_ahead_behind()
    if ahead > 0 and behind > 0:
        log("检测到本地与远端各有提交，正在无冲突合并历史指针...")
        run_git(["merge", "--no-edit", "-s", "ours", "origin/main"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="同步近七天 ComfyUI 生成图片、喜爱/保留/不喜欢分类与重命名到 GitHub 画廊")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS, help="保留并上传最近几天的图片（默认: 7）")
    parser.add_argument("--no-push", action="store_true", help="仅同步本地文件夹并生成画廊网页与元数据，不执行 Git 推送")
    args = parser.parse_args()

    # 1. 配置 Git 代理与基础项
    proxy = detect_local_proxy()
    if proxy:
        log(f"检测到本地代理: {proxy}")
        run_git(["config", "http.proxy", proxy], check=False)
        run_git(["config", "https.proxy", proxy], check=False)
    run_git(["config", "core.quotepath", "false"], check=False)
    run_git(["config", "http.postBuffer", "524288000"], check=False)
    if not run_git(["config", "--get", "user.name"], check=False).stdout.strip():
        run_git(["config", "user.name", "yumenggeng"], check=False)
    if not run_git(["config", "--get", "user.email"], check=False).stdout.strip():
        run_git(["config", "user.email", "2509902541@qq.com"], check=False)

    log(f"=== ComfyUI 云端画廊双向同步启动 (近 {args.days:g} 天 +「{FAV_DIR_NAME}」+「{KEEP_DIR_NAME}」+「不喜欢删除」+「重命名」) ===")
    log(f"工作目录: {OUTPUT_DIR}")

    # 2. 读取现有缓存供重命名和扫描复用
    cache_file = OUTPUT_DIR / "gallery_data.json"
    cache_map: dict[str, dict] = {}
    old_data = load_json_safe(cache_file)
    for item in old_data.get("images", []):
        if "name" in item:
            cache_map[item["name"]] = item

    # 3. 双向同步分类与远程重命名
    log("正在同步云端分类与重命名操作...")
    sync_categories_and_renames(cache_map)

    # 4. 扫描本地需要展示/上传的图片：
    #    (A) `output/喜爱/` 与 `output/保留/` 下的所有图片（永久保留）
    #    (B) `output/` 根目录下近 N 天的图片
    cutoff_ts = time.time() - (args.days * 86400)
    target_entries: list[tuple[Path, str, str]] = []  # (本地路径, 相对路径, category)
    seen_names: set[str] = set()
    skipped_old_count = 0
    counts = {"like": 0, "keep": 0, "none": 0}

    for cat, folder_name in (("like", FAV_DIR_NAME), ("keep", KEEP_DIR_NAME)):
        folder = CATEGORY_DIRS[cat]
        if folder.exists():
            for entry in folder.iterdir():
                if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
                    rel_p = f"{folder_name}/{entry.name}"
                    target_entries.append((entry, rel_p, cat))
                    seen_names.add(entry.name)
                    counts[cat] += 1

    for entry in OUTPUT_DIR.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in IMAGE_EXTS:
            continue
        if entry.name.startswith("clipspace-") or entry.name.startswith("ComfyUI_temp_"):
            continue
        if entry.name in seen_names:
            continue

        if entry.stat().st_mtime >= cutoff_ts:
            target_entries.append((entry, entry.name, "none"))
            seen_names.add(entry.name)
            counts["none"] += 1
        else:
            skipped_old_count += 1

    target_entries.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    log(
        f"扫描完成: 共收录 {len(target_entries)} 张图片 "
        f"(「{FAV_DIR_NAME}」{counts['like']} 张, 「{KEEP_DIR_NAME}」{counts['keep']} 张, "
        f"近 {args.days:g} 天未分类 {counts['none']} 张; 本地超期未分类旧图 {skipped_old_count} 张保留在硬盘不删)"
    )

    # 5. 提取元数据并生成 WebP 缩略图
    thumb_dir = OUTPUT_DIR / THUMB_DIR_NAME
    images_meta = []
    valid_thumb_names = set()

    log("正在提取 ComfyUI 生成信息并生成预览缩略图...")
    for idx, (img_path, rel_path, cat) in enumerate(target_entries, 1):
        cached = cache_map.get(img_path.name)
        meta = extract_comfy_metadata(img_path, rel_path, cat, cached)
        thumb_rel = ensure_thumbnail(img_path, thumb_dir, rel_path)
        meta["thumb"] = thumb_rel
        valid_thumb_names.add(Path(thumb_rel).name)
        images_meta.append(meta)
        if idx % 50 == 0 or idx == len(target_entries):
            log(f"  已处理 {idx}/{len(target_entries)} 张图片...")

    if thumb_dir.exists():
        for tfile in thumb_dir.iterdir():
            if tfile.is_file() and tfile.name not in valid_thumb_names:
                tfile.unlink(missing_ok=True)

    # 6. 写入 gallery_data.json 与 gallery_data.js（绝不在公共文件写入任何 Token）
    payload = {
        "updated_ts": int(time.time()),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days_window": args.days,
        "total_count": len(images_meta),
        "liked_count": counts["like"],
        "kept_count": counts["keep"],
        "total_size_mb": round(sum(x["size"] for x in images_meta) / (1024 * 1024), 2),
        "images": images_meta,
    }
    json_str = json.dumps(payload, ensure_ascii=False, indent=2)
    cache_file.write_text(json_str + "\n", encoding="utf-8")

    js_file = OUTPUT_DIR / "gallery_data.js"
    js_file.write_text(f"window.GALLERY_DATA = {json_str};\n", encoding="utf-8")
    log(f"已生成画廊索引数据: gallery_data.js (共 {len(images_meta)} 张图片, 原图总计 {payload['total_size_mb']} MB)")

    if args.no_push:
        log("已指定 --no-push，跳过 Git 上传步骤。")
        return

    # 7. 无冲突对齐分支指针并清理旧索引
    fast_forward_if_no_ahead()

    core_files = [
        ".gitignore",
        "index.html",
        "gallery_data.js",
        "gallery_data.json",
        STATE_FILE_NAME,
        "sync_gallery.py",
        "上传近7天图片到GitHub.bat",
    ]
    allowed_paths = {cf for cf in core_files if (OUTPUT_DIR / cf).exists()}
    for item in images_meta:
        allowed_paths.add(item["path"].replace("\\", "/"))
        if item.get("thumb"):
            allowed_paths.add(item["thumb"].replace("\\", "/"))

    ls_res = run_git(["ls-files"], check=True)
    tracked_files = [line.strip() for line in ls_res.stdout.splitlines() if line.strip()]

    to_remove_from_git = [f for f in tracked_files if f not in allowed_paths]
    if to_remove_from_git:
        log(f"正在从 Git 仓库索引中清理 {len(to_remove_from_git)} 个过期/已移动或已删除的文件索引...")
        chunk_size = 100
        for i in range(0, len(to_remove_from_git), chunk_size):
            chunk = to_remove_from_git[i : i + chunk_size]
            run_git(["rm", "--cached", "-f", "--ignore-unmatch", "--"] + chunk, check=False)

    stage_core = [f for f in core_files if (OUTPUT_DIR / f).exists()]
    if stage_core:
        run_git(["add", "-f", "--"] + stage_core, check=False)
    if thumb_dir.exists():
        run_git(["add", "-f", THUMB_DIR_NAME], check=False)

    diff_cached = run_git(["diff", "--cached", "--name-only"], check=False)
    if diff_cached.stdout.strip():
        msg = f"Update gallery UI, state & thumbs ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        log("正在提交画廊网页、分类状态与缩略图...")
        run_git(["commit", "-m", msg], check=False)

    merge_remote_if_diverged()

    unpushed = run_git(["rev-list", "--count", "origin/main..HEAD"], check=False)
    if unpushed.returncode != 0 or int(unpushed.stdout.strip() or "0") > 0:
        log("正在推送画廊页面与缩略图至 GitHub...")
        pres = run_git(["push", "-u", "origin", "main"], check=False, capture=False)
        if pres.returncode != 0:
            log("警告: 核心文件推送返回非零，将在后续重试...")

    # 8. 检测所有未跟踪或已修改的原图（注意：由于 .gitignore 含 *，必须直接比对 ls-files，不能用 git status）
    fresh_ls = run_git(["ls-files"], check=True)
    fresh_tracked = {line.strip() for line in fresh_ls.stdout.splitlines() if line.strip()}
    diff_work = run_git(["diff", "--name-only"], check=True)
    modified_tracked = {line.strip() for line in diff_work.stdout.splitlines() if line.strip()}

    pending_images = [
        item["path"].replace("\\", "/")
        for item in images_meta
        if item["path"].replace("\\", "/") not in fresh_tracked
        or item["path"].replace("\\", "/") in modified_tracked
    ]

    if not pending_images:
        log("所有近 7 天原图、喜爱及保留原图均已同步在 GitHub 仓库中，无需重复上传！")
    else:
        total_pending_mb = sum((OUTPUT_DIR / fn).stat().st_size for fn in pending_images) / (1024 * 1024)
        log(f"共有 {len(pending_images)} 张原图待上传/更新路径 (总计 {total_pending_mb:.1f} MB)...")

        batches: list[list[str]] = []
        cur_batch: list[str] = []
        cur_bytes = 0
        max_bytes = int(BATCH_SIZE_MB * 1024 * 1024)

        for fn in pending_images:
            fsize = (OUTPUT_DIR / fn).stat().st_size
            if cur_batch and (cur_bytes + fsize > max_bytes):
                batches.append(cur_batch)
                cur_batch = [fn]
                cur_bytes = fsize
            else:
                cur_batch.append(fn)
                cur_bytes += fsize
        if cur_batch:
            batches.append(cur_batch)

        for b_idx, batch in enumerate(batches, 1):
            b_mb = sum((OUTPUT_DIR / fn).stat().st_size for fn in batch) / (1024 * 1024)
            log(f"--- 正在处理第 {b_idx}/{len(batches)} 批原图 ({len(batch)} 张, {b_mb:.1f} MB) ---")
            run_git(["add", "-f", "--"] + batch, check=True)
            commit_msg = f"Upload gallery images batch {b_idx}/{len(batches)} ({len(batch)} imgs)"
            run_git(["commit", "-m", commit_msg], check=False)

            for attempt in range(1, 4):
                log(f"正在推送到 GitHub (第 {b_idx}/{len(batches)} 批, 尝试 {attempt}/3)...")
                pres = run_git(["push", "origin", "main"], check=False, capture=False)
                if pres.returncode == 0:
                    log(f"第 {b_idx}/{len(batches)} 批推送成功！")
                    break
                else:
                    log("警告: 推送中断，3 秒后重试...")
                    time.sleep(3)
            else:
                log(f"错误: 第 {b_idx} 批推送失败，请检查网络或代理 (127.0.0.1:7890) 后重新运行脚本（已上传的批次不会重复上传）。")
                sys.exit(1)

    log("====================================================")
    log("🎉 双向同步全部完成！")
    log(f"❤️ 喜爱文件夹: {CATEGORY_DIRS['like']}")
    log(f"📌 保留文件夹: {CATEGORY_DIRS['keep']}")
    log("👉 在线画廊地址 (手机/平板/其他电脑访问):")
    log("   https://yumenggeng.github.io/miku-gallery/")
    log("====================================================")


if __name__ == "__main__":
    main()
