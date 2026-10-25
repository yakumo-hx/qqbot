# -*- coding: utf-8 -*-
"""从 LLBot 官方 GitHub Releases 下载 Windows x64 安装包并记录哈希。

只允许官方仓库：https://github.com/LLOneBot/LuckyLilliaBot
输出：
  <out>/LuckyLillia-Desktop-win-x64.zip
  <out>/LuckyLillia-CLI-win-x64.zip
  <out>/release_v8.3.2.json        （release 元数据，含资产 URL/digest）
  <out>/download_hashes.json      （本地实测大小/SHA256/下载时间）

用法：python fetch_llbot.py --out E:\\QQbot-trials\\download [--tag v8.3.2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
API = "https://api.github.com/repos/LLOneBot/LuckyLilliaBot/releases/tags/{tag}"
ASSETS = [
    "LuckyLillia-Desktop-win-x64.zip",
    "LuckyLillia-CLI-win-x64.zip",
]


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "llbot-trial/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def download(url: str, path: str, expected_size: int = 0) -> dict:
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "llbot-trial/1.0"})
    sha = hashlib.sha256()
    got = 0
    last = 0
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            got += len(chunk)
            if total and got - last > 20 * 1024 * 1024:
                last = got
                pct = got * 100.0 / total
                print(f"  {os.path.basename(path)}: {got/1048576:.1f}/{total/1048576:.1f} MB ({pct:.0f}%)",
                      flush=True)
    return {
        "bytes": got, "sha256": sha.hexdigest(),
        "elapsed_s": round(time.time() - t0, 1),
        "expected_size": expected_size,
        "size_matches": (got == expected_size) if expected_size else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="v8.3.2")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    meta = get_json(API.format(tag=a.tag))
    with open(os.path.join(a.out, f"release_{a.tag}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"release {meta['tag_name']} published {meta['published_at']}; "
          f"{len(meta.get('assets', []))} assets", flush=True)
    by_name = {x["name"]: x for x in meta.get("assets", [])}
    results = {"tag": meta["tag_name"], "release_url": meta["html_url"],
               "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
               "assets": {}}
    for name in ASSETS:
        info = by_name.get(name)
        if not info:
            print(f"MISSING ASSET {name}", flush=True)
            return 3
        path = os.path.join(a.out, name)
        print(f"downloading {name} ({info['size']} bytes) from {info['browser_download_url']}", flush=True)
        res = download(info["browser_download_url"], path, int(info.get("size") or 0))
        res["url"] = info["browser_download_url"]
        res["api_digest"] = info.get("digest")
        res["download_path"] = path
        if res["api_digest"]:
            res["digest_matches"] = (res["api_digest"] == "sha256:" + res["sha256"])
        results["assets"][name] = res
        print(f"  done {res['bytes']} bytes sha256={res['sha256']} "
              f"digest_match={res.get('digest_matches')}", flush=True)
    with open(os.path.join(a.out, "download_hashes.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("OK", json.dumps({k: v["sha256"] for k, v in results["assets"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
