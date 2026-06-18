#!/usr/bin/env python3
"""从百度网盘下载分片并合并为完整 CSV"""

import subprocess
import os
import sys

REMOTE_DIR = "/apps/bypy/guba_crawl/000001/000001_enhanced.csv_chunks"
LOCAL_DIR = "/home/ubuntu/guba_crawler/EastMoney_Crawler/data"
OUTPUT_FILE = os.path.join(LOCAL_DIR, "000001_enhanced.csv")


def main():
    os.makedirs(LOCAL_DIR, exist_ok=True)

    # 1. 列出远程分片
    print("[1/3] 获取远程分片列表 ...")
    result = subprocess.run(
        ["bypy", "list", REMOTE_DIR],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"错误: {result.stderr}")
        sys.exit(1)

    chunks = []
    for line in result.stdout.split("\n"):
        if line.startswith("F "):
            parts = line.split()
            chunks.append(parts[1])

    if not chunks:
        print("没有找到分片文件")
        sys.exit(1)

    chunks.sort()
    print(f"  找到 {len(chunks)} 个分片")

    # 2. 下载所有分片
    print(f"\n[2/3] 下载分片到 {LOCAL_DIR} ...")
    for i, name in enumerate(chunks):
        remote_path = f"{REMOTE_DIR}/{name}"
        local_path = os.path.join(LOCAL_DIR, name)
        if os.path.exists(local_path):
            print(f"  [{i+1}/{len(chunks)}] {name} (已存在，跳过)")
            continue

        result = subprocess.run(
            ["bypy", "downfile", remote_path, local_path],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  [{i+1}/{len(chunks)}] {name} ✓")
        else:
            print(f"  [{i+1}/{len(chunks)}] {name} ✗ {result.stderr.strip()[:100]}")

    # 3. 合并分片
    print(f"\n[3/3] 合并分片 → {OUTPUT_FILE} ...")
    with open(OUTPUT_FILE, "wb") as f_out:
        for name in chunks:
            local_path = os.path.join(LOCAL_DIR, name)
            if os.path.exists(local_path):
                with open(local_path, "rb") as f_in:
                    f_out.write(f_in.read())
                os.remove(local_path)  # 合并后删除分片

    file_size_mb = os.path.getsize(OUTPUT_FILE) / 1024 / 1024
    print(f"  完成: {OUTPUT_FILE} ({file_size_mb:.1f} MB)")
    print(f"\n可以使用以下命令查看:")
    print(f"  head -5 {OUTPUT_FILE}")
    print(f"  wc -l {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
