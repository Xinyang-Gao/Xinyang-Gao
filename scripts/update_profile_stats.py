#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚合 GitHub 账号数据，并把结果写回 README.md 的指定区块。

用法::

    python scripts/update_profile_stats.py --user Xinyang-Gao
    python scripts/update_profile_stats.py --user Xinyang-Gao --include-forks

README 中需要成对出现的区块标记::

    <!-- STATS_SUMMARY:START --> ... <!-- STATS_SUMMARY:END -->
    <!-- LANG_STATS:START -->   ... <!-- LANG_STATS:END -->
    <!-- PINNED_REPOS:START --> ... <!-- PINNED_REPOS:END -->   # 需要 GITHUB_TOKEN

环境变量 ``GITHUB_TOKEN`` 可选：提供后可获得 5000/小时 的配额，并用于拉取 Pinned 仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
UA = "xinyang-profile-readme-updater"

ACCENT = "b45b63"  # 与个人网站保持一致的强调色

# GitHub / linguist 常见语言配色，用于生成徽章
LANG_COLORS = {
    "Python": "3776AB",
    "TypeScript": "3178C6",
    "JavaScript": "F7DF1E",
    "HTML": "E34F26",
    "CSS": "663399",
    "SCSS": "C6538C",
    "Java": "B07219",
    "Kotlin": "7F52FF",
    "Vue": "41B883",
    "Shell": "89E051",
    "Dockerfile": "2496ED",
    "C": "555555",
    "C++": "F34B7D",
    "C#": "178600",
    "Go": "00ADD8",
    "Rust": "DEA584",
    "PHP": "4F5D95",
    "Ruby": "CC342D",
    "Swift": "F05138",
    "Lua": "000080",
    "Markdown": "083FA1",
    "JSON": "8BC9FF",
    "YAML": "CB171E",
    "Visual Basic .NET": "945DB7",
    "Batchfile": "C1F12E",
    "Makefile": "427819",
    "Nix": "7E7EFF",
}

# 只有确定存在的 simple-icons slug 才加 logo，避免出现裂图
LANG_LOGOS = {
    "Python": ("python", "white"),
    "TypeScript": ("typescript", "white"),
    "JavaScript": ("javascript", "black"),
    "HTML": ("html5", "white"),
    "CSS": ("css3", "white"),
    "Java": ("openjdk", "white"),
    "Kotlin": ("kotlin", "white"),
    "Vue": ("vuedotjs", "white"),
    "Shell": ("gnubash", "white"),
    "Dockerfile": ("docker", "white"),
    "Go": ("go", "white"),
    "Rust": ("rust", "white"),
    "Swift": ("swift", "white"),
    "Ruby": ("ruby", "white"),
    "PHP": ("php", "white"),
    "Lua": ("lua", "white"),
    "Markdown": ("markdown", "white"),
    "JSON": ("javascript", "white"),
    "Nix": ("nixos", "white"),
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(url: str, token: str | None = None, tries: int = 4) -> dict | list:
    """请求 GitHub REST API，遇到速率限制时自动等待重试。"""
    for attempt in range(tries):
        req = urllib.request.Request(
            url,
            headers={
                "Accept": ACCEPT,
                "User-Agent": UA,
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if attempt == tries - 1:
                raise RuntimeError(f"GET {url} 失败：HTTP {err.code} {err.reason}") from err
            if err.code in (403, 429):
                wait = rate_limit_wait_seconds(err.headers, attempt)
                print(f"  ! 速率限制触发，等待 {wait}s 后重试", flush=True)
                time.sleep(wait)
            elif 500 <= err.code < 600:
                print(f"  ! 服务端错误 {err.code}，{5 * (attempt + 1)}s 后重试", flush=True)
                time.sleep(5 * (attempt + 1))
            else:
                raise
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt == tries - 1:
                raise
            print(f"  ! 网络异常（{err}），{5 * (attempt + 1)}s 后重试", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def rate_limit_wait_seconds(headers, attempt: int) -> int:
    reset = headers.get("X-RateLimit-Reset")
    if reset and str(reset).isdigit():
        delta = int(reset) - int(time.time())
        if delta > 0:
            return min(delta + 2, 300)
    return min(15 * (attempt + 1), 300)


def paginate(path: str, token: str | None = None) -> list:
    """按 Link 头翻页，直到拿完所有结果。"""
    items: list = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        data = http_json(f"{API}{path}{sep}per_page=100&page={page}", token)
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def fmt_percent(value: float) -> str:
    return f"{value:.1f}%"


def bar(percent: float, width: int = 20) -> str:
    """用方块字符画进度条，GitHub 渲染无依赖。"""
    filled = int(round(percent / 100 * width))
    filled = max(0, min(width, filled))
    return "▓" * filled + "░" * (width - filled)


def badge(label: str, message: str, color: str, logo: str | None = None,
          logo_color: str | None = None) -> str:
    def esc(text: str) -> str:
        return urllib.parse.quote(str(text), safe="")

    url = f"https://img.shields.io/badge/{esc(label)}-{esc(message)}-{color}"
    opts = ["style=flat-square"]
    if logo:
        opts.append(f"logo={logo}")
        if logo_color:
            opts.append(f"logoColor={logo_color}")
    return f"{url}?{'&'.join(opts)}"


# --------------------------------------------------------------------------- #
# 数据收集
# --------------------------------------------------------------------------- #
def collect(user: str, token: str | None, include_forks: bool) -> dict:
    print(f"→ 读取用户 @{user} 信息", flush=True)
    profile = http_json(f"{API}/users/{user}", token)

    print("→ 读取仓库列表", flush=True)
    repos = paginate(f"/users/{user}/repos", token)

    targets = repos if include_forks else [r for r in repos if not r.get("fork")]
    print(f"  共 {len(repos)} 个公开仓库，其中 {len(targets)} 个参与统计", flush=True)

    print("→ 汇总各仓库语言字节数", flush=True)
    lang_bytes: dict[str, int] = {}
    lang_repos: dict[str, set[str]] = {}
    total_bytes = 0
    for idx, repo in enumerate(targets, 1):
        name = repo["name"]
        try:
            langs = http_json(f"{API}/repos/{user}/{name}/languages", token)
        except RuntimeError as err:
            print(f"  ! 跳过 {name}：{err}", flush=True)
            continue
        if not langs:
            continue
        for language, value in langs.items():
            lang_bytes[language] = lang_bytes.get(language, 0) + value
            lang_repos.setdefault(language, set()).add(name)
            total_bytes += value
        if idx % 10 == 0:
            print(f"  · 已处理 {idx}/{len(targets)}", flush=True)
        time.sleep(0.05)

    commits = None
    try:
        search = http_json(
            f"{API}/search/commits?q={urllib.parse.quote('author:' + user)}&per_page=1",
            token,
        )
        commits = int(search.get("total_count", 0))
    except RuntimeError as err:
        print(f"  ! 提交数获取失败：{err}", flush=True)

    return {
        "profile": profile,
        "repos": repos,
        "targets": targets,
        "lang_bytes": lang_bytes,
        "lang_repos": lang_repos,
        "total_bytes": total_bytes,
        "commits": commits,
    }


def fetch_pinned(user: str, token: str) -> list[dict]:
    """读取 Pin 到主页的仓库（GraphQL，需要 token）。"""
    query = """
    query Pinned($login: String!) {
      user(login: $login) {
        pinnedItems(first: 6, types: [REPOSITORY]) {
          nodes {
            ... on Repository {
              name
              description
              url
              stargazerCount
              forkCount
              primaryLanguage { name }
            }
          }
        }
      }
    }
    """
    try:
        payload = json.dumps({"query": query, "variables": {"login": user}}).encode("utf-8")
        req = urllib.request.Request(
            f"{API}/graphql",
            data=payload,
            method="POST",
            headers={
                "Accept": ACCEPT,
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as err:  # noqa: BLE001
        print(f"  ! GraphQL 请求失败：{err}", flush=True)
        return []

    if data.get("errors"):
        print(f"  ! GraphQL 返回错误：{data['errors'][0].get('message')}", flush=True)
        return []
    return (data.get("data", {}).get("user") or {}).get("pinnedItems", {}).get("nodes", [])


# --------------------------------------------------------------------------- #
# 区块渲染
# --------------------------------------------------------------------------- #
def render_stats_summary(data: dict, updated_at: str) -> str:
    profile = data["profile"]
    repos = data["repos"]
    targets = data["targets"]

    forks = sum(1 for r in repos if r.get("fork"))
    stars = sum(int(r.get("stargazers_count", 0)) for r in targets)
    # 自有仓库中被 fork 的次数
    forks_received = sum(int(r.get("forks_count", 0)) for r in targets)
    commits = data["commits"]
    joined = profile.get("created_at", "")[:10]

    items = [
        ("公开仓库", len(repos), ACCENT, "github"),
        ("自有仓库", len(targets), ACCENT, None),
    ]
    if forks:
        items.append(("Fork 仓库", forks, "6d6d66", None))
    if stars:
        items.append(("Stars", stars, "b8860b", None))
    if forks_received:
        items.append(("Forked by", forks_received, "6d6d66", None))
    if commits is not None:
        items.append(("公开提交", commits, ACCENT, None))
    items.append(("Followers", profile.get("followers", 0), ACCENT, None))

    lines = ["<p>"]
    for label, value, color, logo in items:
        lines.append(f'  <img src="{badge(label, value, color, logo)}" alt="{label}" />')
    lines += [
        "</p>",
        "",
        f"> **{joined}** 加入 GitHub · 关注者 **{profile.get('followers', 0)}**"
        f" · 关注中 **{profile.get('following', 0)}**"
        + (f" · 📮 Gists **{profile.get('public_gists', 0)}**" if profile.get("public_gists") else ""),
        ">",
        "",
        f"<sub>数据同步于 {updated_at}（UTC+8）</sub>",
    ]
    return "\n".join(lines)


def render_lang_stats(data: dict, updated_at: str) -> str:
    lang_bytes = data["lang_bytes"]
    lang_repos = data["lang_repos"]
    total = data["total_bytes"]
    targets = data["targets"]

    if not lang_bytes:
        return "_暂时没有可统计的语言数据。_"

    ordered = sorted(lang_bytes.items(), key=lambda kv: kv[1], reverse=True)

    lines = [
        f"**全部自有仓库代码总量：`{human_size(total)}`**"
        f"（{total:,} 字节） · 覆盖 **{len(ordered)}** 种语言"
        f" · 统计范围：**{len(targets)}** 个非 Fork 仓库",
        "",
        "| 语言 | 占比 | 代码量 | 涉及仓库 |",
        "| :--- | :--- | ---: | ---: |",
    ]

    for language, value in ordered:
        percent = value / total * 100 if total else 0.0
        lines.append(
            f"| **{language}** | `{bar(percent)}` {fmt_percent(percent)}"
            f" | {human_size(value)} | {len(lang_repos.get(language, ()))} |"
        )

    lines += [
        "",
        "<p>",
    ]
    for language, value in ordered[:8]:
        percent = value / total * 100 if total else 0.0
        color = LANG_COLORS.get(language, "6d6d66")
        logo, logo_color = LANG_LOGOS.get(language, (None, None))
        lines.append(
            f'  <img src="{badge(language, fmt_percent(percent), color, logo, logo_color)}"'
            f' alt="{language} {fmt_percent(percent)}" />'
        )
    lines += ["</p>", "", f"<sub>最后更新：{updated_at}（UTC+8）</sub>"]
    return "\n".join(lines)


def render_pinned(nodes: list[dict]) -> str:
    if not nodes:
        return None
    lines = []
    for node in nodes:
        language = (node.get("primaryLanguage") or {}).get("name")
        stars = node.get("stargazerCount", 0)
        meta = " · ".join(
            p for p in (language, f"★ {stars}" if stars else None) if p
        )
        description = (node.get("description") or "").strip().replace("\n", " ")
        lines.append(
            f"- 🔗 [{node['name']}]({node['url']})"
            + (f" — {description}" if description else "")
            + (f" <sub>({meta})</sub>" if meta else "")
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 写回 README
# --------------------------------------------------------------------------- #
def replace_block(text: str, name: str, body: str | None) -> str:
    """替换 <!-- NAME:START --> 与 <!-- NAME:END --> 之间的内容。"""
    if body is None:
        return text
    pattern = re.compile(
        rf"(?s)(<!--\s*{name}:START\s*-->)(.*?)(<!--\s*{name}:END\s*-->)"
    )
    match = pattern.search(text)
    if not match:
        print(f"  ! README 中未找到 {name} 区块，已跳过", flush=True)
        return text
    return f"{match.group(1)}\n{body}\n{match.group(3)}".join(
        [text[: match.start()], text[match.end():]]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="更新 README 中的 GitHub 数据区块")
    parser.add_argument("--user", default="Xinyang-Gao", help="GitHub 用户名")
    parser.add_argument("--readme", default="README.md", help="README 文件路径")
    parser.add_argument("--include-forks", action="store_true", help="统计时也包含 Fork 仓库")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub Token")
    args = parser.parse_args()

    token = (args.token or "").strip() or None
    print(f"认证方式：{'Token' if token else '匿名（60 次/小时配额）'}", flush=True)

    data = collect(args.user, token, args.include_forks)
    updated_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp(), tz=timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M")

    pinned = fetch_pinned(args.user, token) if token else []
    if token and not pinned:
        print("  · 没有 Pin 任何仓库，保留原有内容", flush=True)

    with open(args.readme, "r", encoding="utf-8", newline="\n") as fh:
        original = fh.read()

    text = replace_block(original, "STATS_SUMMARY", render_stats_summary(data, updated_at))
    text = replace_block(text, "LANG_STATS", render_lang_stats(data, updated_at))
    text = replace_block(text, "PINNED_REPOS", render_pinned(pinned))

    if text == original:
        print("→ README 内容无变化", flush=True)
        return 0

    with open(args.readme, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("→ README 已更新", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
