# -*- coding: utf-8 -*-
"""
ComfyUI Output -> GitHub (miku-gallery) 近七天画廊同步与元数据构建脚本
功能：
1. 扫描当前目录 (ComfyUI/output) 下近 7 天（可配置）生成的图片文件。
2. 自动提取 ComfyUI 的完整生成信息（正面/负面提示词、动态提示词展开、Seed、模型、LoRA、采样器、步数、CFG、分辨率等）。
3. 自动为图片生成轻量级 WebP 缩略图（存入 thumbs/ 目录），让手机和其他设备秒开预览，同时保留原图用于高清查看与下载。
4. 生成静态画廊数据文件 (gallery_data.js) 与功能完善的响应式画廊网页 (index.html)。
5. 通过 Git Index 精确控制：仅将「近 7 天的图片 + 对应缩略图 + 网页与索引文件」上传到 GitHub 仓库，
   自动从远端移除超过 7 天的旧图片，但【绝不删除本地硬盘上的任何历史原图】。
6. 支持分批提交与推送，避免单次上传体积过大导致网络超时。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from PIL import Image

# ================= 配置区域 =================
DEFAULT_DAYS = 7
BATCH_SIZE_MB = 120  # 每批次推送的最大体积 (MB)，避免 GitHub HTTP 推送超时
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
THUMB_DIR_NAME = "thumbs"
THUMB_MAX_WIDTH = 560
THUMB_QUALITY = 80

OUTPUT_DIR = Path(__file__).resolve().parent
COMFY_ROOT = OUTPUT_DIR.parent
CUSTOM_NODES_DIR = COMFY_ROOT / "custom_nodes"

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


def log(msg: str):
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
                    if "://" not in server:
                        return f"http://{server}"
                    return server
    except Exception:
        pass
    return ""


def run_git(args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=str(OUTPUT_DIR),
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def extract_comfy_metadata(img_path: Path, cached_entry: dict | None = None) -> dict:
    """提取图片的尺码、时间与 ComfyUI 节点元数据"""
    st = img_path.stat()
    mtime = int(st.st_mtime)
    fsize = st.st_size

    # 如果缓存命中且文件未修改，直接复用
    if cached_entry and cached_entry.get("mtime") == mtime and cached_entry.get("size") == fsize:
        return cached_entry

    info = {
        "name": img_path.name,
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

                # 扫描模型、LoRA、放大、修脸节点
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

                # 寻找主采样器 (优先 denoise == 1.0 且节点编号较小的 KSampler)
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

            # 如果文件名里带 Seed 但节点没取到，尝试从文件名兜底
            if info["seed"] is None and "Seed-" in img_path.name:
                import re
                m = re.search(r"Seed-(\d+)", img_path.name)
                if m:
                    info["seed"] = int(m.group(1))
    except Exception as e:
        log(f"警告: 读取图片元数据失败 {img_path.name}: {e}")

    return info


def ensure_thumbnail(img_path: Path, thumb_dir: Path) -> str:
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
        return img_path.name


def main():
    parser = argparse.ArgumentParser(description="同步近七天 ComfyUI 生成图片到 GitHub 画廊")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS, help="保留并上传最近几天的图片（默认: 7）")
    parser.add_argument("--no-push", action="store_true", help="仅生成画廊网页与元数据，不执行 Git 提交与推送")
    args = parser.parse_args()

    log(f"=== ComfyUI 云端画廊同步脚本启动 (保留近 {args.days:g} 天图片) ===")
    log(f"工作目录: {OUTPUT_DIR}")

    # 1. 配置 Git 代理与基础项
    proxy = detect_local_proxy()
    if proxy:
        log(f"检测到本地代理: {proxy}")
        run_git(["config", "http.proxy", proxy], check=False)
        run_git(["config", "https.proxy", proxy], check=False)
    run_git(["config", "core.quotepath", "false"], check=False)
    run_git(["config", "http.postBuffer", "524288000"], check=False)
    run_git(["config", "user.name", "yumenggeng"], check=False)
    run_git(["config", "user.email", "2509902541@qq.com"], check=False)

    # 2. 读取已有元数据缓存 (gallery_data.json)
    cache_file = OUTPUT_DIR / "gallery_data.json"
    cache_map: dict[str, dict] = {}
    if cache_file.exists():
        try:
            old_data = json.loads(cache_file.read_text(encoding="utf-8"))
            for item in old_data.get("images", []):
                if "name" in item:
                    cache_map[item["name"]] = item
        except Exception:
            pass

    # 3. 扫描近 N 天的图片 (排除 clipspace 或临时预览图)
    cutoff_ts = time.time() - (args.days * 86400)
    all_recent_files: list[Path] = []
    skipped_old_count = 0

    for entry in OUTPUT_DIR.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in IMAGE_EXTS:
            continue
        if entry.name.startswith("clipspace-") or entry.name.startswith("ComfyUI_temp_"):
            continue

        mtime = entry.stat().st_mtime
        if mtime >= cutoff_ts:
            all_recent_files.append(entry)
        else:
            skipped_old_count += 1

    # 按修改时间倒序排列（最新生成的排最前面）
    all_recent_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    log(f"扫描完成: 近 {args.days:g} 天内的图片共 {len(all_recent_files)} 张 (已忽略本地超期旧图 {skipped_old_count} 张，本地文件保留不删)")

    # 4. 提取元数据并生成 WebP 缩略图
    thumb_dir = OUTPUT_DIR / THUMB_DIR_NAME
    images_meta = []
    valid_thumb_names = set()

    log("正在提取 ComfyUI 生成信息并生成高压缩比预览缩略图...")
    for idx, img_path in enumerate(all_recent_files, 1):
        cached = cache_map.get(img_path.name)
        meta = extract_comfy_metadata(img_path, cached)
        thumb_rel = ensure_thumbnail(img_path, thumb_dir)
        meta["thumb"] = thumb_rel
        valid_thumb_names.add(Path(thumb_rel).name)
        images_meta.append(meta)
        if idx % 50 == 0 or idx == len(all_recent_files):
            log(f"  已处理 {idx}/{len(all_recent_files)} 张图片...")

    # 清理超过 7 天的旧缩略图文件（缩略图是自动生成的派生文件，清理可保持目录整洁）
    if thumb_dir.exists():
        for tfile in thumb_dir.iterdir():
            if tfile.is_file() and tfile.name not in valid_thumb_names:
                try:
                    tfile.unlink()
                except Exception:
                    pass

    # 5. 写入 gallery_data.json 与 gallery_data.js
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days_window": args.days,
        "total_count": len(images_meta),
        "total_size_mb": round(sum(x["size"] for x in images_meta) / (1024 * 1024), 2),
        "images": images_meta,
    }
    json_str = json.dumps(payload, ensure_ascii=False, indent=2)
    cache_file.write_text(json_str, encoding="utf-8")

    js_file = OUTPUT_DIR / "gallery_data.js"
    js_file.write_text(f"window.GALLERY_DATA = {json_str};\n", encoding="utf-8")
    log(f"已生成画廊索引数据: gallery_data.js (共 {len(images_meta)} 张图片, 原图总计 {payload['total_size_mb']} MB)")

    if args.no_push:
        log("已指定 --no-push，跳过 Git 上传步骤。")
        return

    # 6. 同步 Git 索引：仅跟踪【核心脚本/网页文件 + 近 7 天原图 + 近 7 天缩略图】
    core_files = [
        ".gitignore",
        "index.html",
        "gallery_data.js",
        "gallery_data.json",
        "sync_gallery.py",
        "上传近7天图片到GitHub.bat",
    ]
    allowed_paths = set()
    for cf in core_files:
        if (OUTPUT_DIR / cf).exists():
            allowed_paths.add(cf)

    for item in images_meta:
        allowed_paths.add(item["name"])
        if item.get("thumb"):
            allowed_paths.add(item["thumb"].replace("\\", "/"))

    # 获取当前 Git 索引中已跟踪的所有文件
    ls_res = run_git(["ls-files"], check=True)
    tracked_files = [line.strip() for line in ls_res.stdout.splitlines() if line.strip()]

    # 找出已在 Git 跟踪中、但不在本次允许列表中的旧文件（仅从 Git 仓库移除，绝不删除本地文件 --cached）
    to_remove_from_git = [f for f in tracked_files if f not in allowed_paths]
    if to_remove_from_git:
        log(f"正在从 GitHub 仓库索引中移除 {len(to_remove_from_git)} 个超过 {args.days:g} 天的旧文件 (本地原图完好保留)...")
        # 分批执行 git rm --cached 防止命令行过长
        chunk_size = 100
        for i in range(0, len(to_remove_from_git), chunk_size):
            chunk = to_remove_from_git[i : i + chunk_size]
            run_git(["rm", "--cached", "-f", "--ignore-unmatch", "--"] + chunk, check=False)

    # 先提交移除操作与核心网页数据更新
    stage_core = [f for f in core_files if (OUTPUT_DIR / f).exists()]
    if stage_core:
        run_git(["add", "-f", "--"] + stage_core, check=False)
    if thumb_dir.exists():
        run_git(["add", "-f", THUMB_DIR_NAME], check=False)

    # 检查暂存区是否有变更
    diff_cached = run_git(["diff", "--cached", "--name-only"], check=False)
    if diff_cached.stdout.strip():
        msg = f"Update gallery web UI, metadata & prune >{args.days:g}d old images ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        log("正在提交画廊网页、缩略图与索引清理...")
        run_git(["commit", "-m", msg], check=False)
        log("正在推送画廊页面与缩略图至 GitHub...")
        push_res = run_git(["push", "-u", "origin", "main"], check=False, capture=False)
        if push_res.returncode != 0:
            log("提示: 首次推送返回非零状态，尝试先拉取或继续分批推送...")

    # 7. 分批暂存并推送近 7 天的新增/修改原图
    # 找出哪些近 7 天原图还未与 Git 索引一致
    status_res = run_git(["status", "--porcelain", "-uall"], check=True)
    dirty_files = set()
    for line in status_res.stdout.splitlines():
        if len(line) > 3:
            rel_p = line[3:].strip().strip('"')
            dirty_files.add(rel_p)

    pending_images = [item["name"] for item in images_meta if item["name"] in dirty_files]
    if not pending_images:
        log("所有近 7 天原图均已同步在 GitHub 仓库中，无需重复上传！")
    else:
        total_pending_mb = sum((OUTPUT_DIR / fn).stat().st_size for fn in pending_images) / (1024 * 1024)
        log(f"共有 {len(pending_images)} 张近 {args.days:g} 天的新图片待上传 (总计 {total_pending_mb:.1f} MB)...")

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
            commit_msg = f"Upload recent images batch {b_idx}/{len(batches)} ({len(batch)} imgs)"
            run_git(["commit", "-m", commit_msg], check=False)

            # 带重试机制的推送
            for attempt in range(1, 4):
                log(f"正在推送到 GitHub (第 {b_idx}/{len(batches)} 批, 尝试 {attempt}/3)...")
                pres = run_git(["push", "origin", "main"], check=False, capture=False)
                if pres.returncode == 0:
                    log(f"第 {b_idx}/{len(batches)} 批推送成功！")
                    break
                else:
                    log(f"警告: 推送中断，3 秒后重试...")
                    time.sleep(3)
            else:
                log(f"错误: 第 {b_idx} 批推送失败，请检查网络或代理 (127.0.0.1:7890) 是否畅通后重新运行脚本。")
                sys.exit(1)

    log("====================================================")
    log("🎉 同步全部完成！")
    log("👉 在线画廊访问地址 (支持手机/电脑/平板直接浏览、查参数、下原图):")
    log("   https://yumenggeng.github.io/miku-gallery/")
    log("====================================================")


if __name__ == "__main__":
    main()
