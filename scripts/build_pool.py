#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CF IP Pool Builder
==================
从 Cloudflare 官方 IPv4 网段生成「纯 IP:端口」可用池，默认凑够 2 万条。

流程:
  1. 拉取官方 ips-v4
  2. 拆成 /24，每个 /24 随机取 N 个 IP，每个 IP 随机分配 1 个加密端口
  3. 并发 TCP 探测（连接超时 2 秒），连上即算可用
  4. 输出 ipv4.txt：只有 TCP 通的 IP:端口，凑够目标条数

不涉及 GeoIP、不读 colo、不产国家字段、不做 TLS 握手、不碰 IPv6。
输出就是纯净的 IP:端口。
"""

import argparse
import ipaddress
import json
import os
import random
import socket
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── 配置 ────────────────────────────────────────────────────────────────────

CF_IPV4_URL = "https://www.cloudflare.com/ips-v4"

CONNECT_TIMEOUT = 2.0        # TCP 连接超时（秒）
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Cloudflare 常用的 6 个 HTTPS(加密) 端口，每个 IP 随机分一个
ENCRYPTED_PORTS = [443, 8443, 2053, 2083, 2087, 2096]

# 推送相关键：可以写在 --config 里，也可以单独写在 gh_config.json
PUSH_KEYS = ("github_token", "github_username", "repo_name", "branch",
              "github_repo", "github_branch", "auto_push")

# gh_config.json 模板：Win / Linux / Termux 运行参数 + 推送设置，token 不落到仓库里
GH_CONFIG_TEMPLATE = json.dumps({
    "_comment": [
        "gh_config.json —— Windows / Linux / Termux 上跑的那份，已被 .gitignore 忽略，不会提交进仓库（token 就在这里，所以更不能外泄）。",
        "用法: python main.py            （main.py 会先检查这份配置，再跑，再按 auto_push 推送）",
        "单独跑: python scripts/build_pool.py --config gh_config.json",
        "命令行参数优先级高于此文件；重新生成模板: python main.py --init（已存在则不覆盖）",
        "your_ 开头的值会被当占位符拦下，记得换成真实值。",
        "token 也可用环境变量 GITHUB_TOKEN / GH_TOKEN 代替（优先级更高）。",
        "outdir 默认 out_local（不是仓库里的 output/），免得小样本覆盖 Actions 的生产产物。",
        "GitHub Actions 跑的那份是 config.json，和这个文件互不影响。",
        "每个键前面那行 _ 开头的都只是给人看的注释，加载时会被自动忽略。",
    ],
    "_run": [
        "Win / Linux / Termux 运行参数：小样本 + 单并发逐条探测，几秒跑完，适合改完逻辑先在自己机器上验一遍。",
        "凑不够 target 也不报错，给多少算多少；只把 TCP 探测通过的 IP:端口 写进 ipv4.txt。",
    ],
    "_sample": "每个 /24 随机取几个 IP（起始值）。不够 target 时自动按 sample_step 加码重采",
    "sample": 2,
    "_sample_step": "每轮补采样时，每个 /24 的取样数增加多少",
    "sample_step": 2,
    "_max_rounds": "最多补采样几轮。轮数越多越可能凑够 target，但耗时也线性上涨",
    "max_rounds": 3,
    "_target": "目标存活条数。默认 30，几秒就能跑完；凑不够也不报错，给多少算多少",
    "target": 30,
    "_workers": "并发线程数。固定 1 = 单线程逐条探测，结果最稳、也最慢",
    "workers": 1,
    "_attempts": "每个 IP 的 TCP 连接尝试次数。1 = 只探一次；网络抖动时可调大，让失败的 IP 多重试几轮",
    "attempts": 1,
    "_timeout": "TCP 连接超时（秒）。2 秒是「宁可略过也不干等」的甜点位",
    "timeout": 2.0,
    "_ports": [
        "加密端口池。每个 IP 只随机分其中一个，所以 6 个端口会大致均匀混在结果里。",
        "443 / 8443 / 2053 / 2083 / 2087 / 2096 是 CF 常用的 HTTPS 端口",
    ],
    "ports": list(ENCRYPTED_PORTS),
    "_outdir": "输出目录（相对当前工作目录）。默认 out_local，不写仓库里的 output/，免得覆盖 Actions 的生产产物",
    "outdir": "out_local",
    "_header": [
        "ipv4.txt 首行是否写 # 注释头（记录生成时间/行数）。",
        "下游解析器一般都会跳过 # 行；设 false 就是纯 IP:端口、一行不多",
    ],
    "header": True,

    "_sustain": [
        "可持续化运行四件套（详细说明见 README「可持续化运行」一节）:",
        "    merge       —— 增量累积：读已有 ipv4.txt 合并而非覆盖，池子只增不减",
        "    recheck     —— 存量复核：先把已有 IP:端口 重探一遍，不通的剔掉（配合 merge）",
        "    state_file  —— 断点续跑：记下探测过的 IP，下次跳过、只采新的",
        "    loop/interval —— 一条命令反复跑；Ctrl+C 会先把已探结果写盘再收尾",
    ],

    "_merge": [
        "增量累积。false = 每次跑都重新生成整个文件；true = 先读已有的 ipv4.txt 合并，池子只增不减。",
        "自己机器上默认开它，池子才会越跑越大；凑数时只补 target 减去存量的差额，不重复探已有的 IP。",
        "Actions 那份 config.json 是 false（每次全新覆盖）。",
    ],
    "merge": True,

    "_recheck": [
        "存量复核。开启后先把 ipv4.txt 里已有的 IP:端口 全部重探一遍，把不通的剔掉再补新的。",
        "配合 merge 用，池子才不会被死 IP 占坑。代价是每轮都要把整个存量池重探一遍，池子越大越慢。",
        "默认 false，想清理死 IP 时再开。",
    ],
    "recheck": False,

    "_state_file": [
        "断点续跑状态文件，记下哪些 IP 已经探测过了，下次跑直接跳过、只采新 IP。",
        "就在 outdir 里，随时可删掉重置。留空字符串 = 不启用。",
    ],
    "state_file": "pool_state.json",

    "_loop": [
        "循环次数。1 = 跑一次就退（默认）。大于 1 = 一条命令反复跑，配合 interval 当定时任务用。",
        "Ctrl+C 优雅收尾：先把已探到的结果写盘，再走推送。",
    ],
    "loop": 1,

    "_interval": "循环间隔秒数，仅 loop 大于 1 有意义。0 = 立刻接着跑下一轮",
    "interval": 0.0,

    "_schedule": [
      "定时任务设置（python main.py --schedule install 时用）。命令行显式给的参数优先于这里的值。",
      "为什么单独放一个对象里：顶层已经有个 interval，但那是「循环间隔秒数」（loop 大于 1 时用），",
      "和这里的「间隔分钟数」语义相反，混在一起会被误读。",
      "能表达的档位只有三类：1-59 分钟、整小时（最多 23）、整天数（最多 30）；别的会直接报错。",
      "用法: python main.py --schedule install / status / uninstall"
    ],
    "schedule": {
      "_interval": "间隔分钟数。360 = 每 6 小时（默认，跟 Actions 对齐）。可用的整档: 1-59、整小时最多 23、整天数最多 30",
      "interval": 360,
      "_at": [
        "首次运行时刻，HH:MM（也可只给分钟数，例: 30 = 00:30）。",
        "整天档里它就是具体几点跑；分钟档和小时档在 cron 上会被忽略（Windows 任务计划程序会认）。"
      ],
      "at": "00:00",
      "_name": "任务名。Windows 任务计划程序 / crontab 注释里显示的就是它",
      "name": "CF-IP-Pool",
      "_no_push": "定时跑的时候只生成 ipv4.txt、不推送。true = 不推；false = 按上面的 auto_push 推送",
      "no_push": False
    },
    "_push": [
        "推送设置（4 个键）。auto_push=true 表示跑完自动提交并推送，不用再手动加 --push；想临时关掉用 --no-push。",
        "token 去 https://github.com/settings/tokens 生成，权限要求见下面 _github_token。",
        "github_username + repo_name 拼成 owner/repo，照抄你仓库地址里那两段就行；branch 填 main。",
        "token 只拼进这一次 push 的临时 URL，不写进 .git/config，日志里也会打码。",
        "想先确认推送链路通不通（不真推）: python scripts/check_push.py",
    ],
    "_auto_push": [
        "跑完是否自动提交并推送 outdir。true = 自动推；false = 只生成不推。",
        "临时关掉: 加 --no-push，不用改这个文件。",
    ],
    "auto_push": True,
    "_github_token": [
        "GitHub Personal Access Token（PAT）。两种样子都认：",
        "    classic      —— ghp_ 开头，后面 36 位",
        "    fine-grained —— github_pat_ 开头，后面一串",
        "生成地址: https://github.com/settings/tokens",
        "权限: classic 勾 repo；fine-grained 只给这个仓库 Contents: Read and write",
        "不想写在这个文件里就留空，改用环境变量 GITHUB_TOKEN / GH_TOKEN（环境变量优先级更高）。",
        "只在 push 那一瞬间拼进临时 URL，不写 .git/config，日志里也会打码。",
    ],
    "github_token": "your_github_personal_access_token_here",
    "_github_username": [
        "GitHub 用户名或组织名 —— 仓库地址里 github.com/ 后面、/ 前面那一段。",
        "例: 仓库地址 github.com/RikkaSaiko1/cf-ip-pool，这里填 RikkaSaiko1",
    ],
    "github_username": "your_github_username",
    "_repo_name": [
        "仓库名 —— 仓库地址最后那一段，不带 owner、不带 .git。",
        "例: 仓库地址 github.com/RikkaSaiko1/cf-ip-pool，这里填 cf-ip-pool",
    ],
    "repo_name": "your_repo_name",
    "_branch": [
        "要推送到的分支名。例: main",
        "留空 = 当前分支（git branch --show-current）。",
    ],
    "branch": "main"
}, indent=2, ensure_ascii=False) + "\n"
DEFAULT_CONFIG = {
    "sample": 6,                     # 每个 /24 取几个 IP（起始值，不够会自动加）
    "ports": list(ENCRYPTED_PORTS),  # 加密端口池，每个 IP 随机取一个
    "workers": 1000,                 # 并发数
    "timeout": CONNECT_TIMEOUT,      # TCP 连接超时（秒）
    "attempts": 1,                   # 每个 IP 的 TCP 连接尝试次数（1 = 只探一次，失败不重试）
    "target": 20000,                 # 目标存活条数（2w）
    "sample_step": 2,                # 每轮补采样时每个 /24 增加几个
    "max_rounds": 6,                 # 最多补采样几轮
    "outdir": "output",              # 输出目录
    "header": True,                  # ipv4.txt 首行是否写 # 注释头
    "seed": None,                    # 随机种子

    # ── 可持续化运行（默认全关，行为与从前完全一致）──
    "merge": False,                  # 增量累积：读已有 ipv4.txt 合并而非覆盖，池子单调增长
    "recheck": False,                # 存量复核：先把已有 IP 重探一遍，死的剔掉
    "state_file": "pool_state.json", # 断点续跑状态文件（记录探测过的 IP）；空串 = 不启用
    "loop": 1,                       # 循环次数（1 = 跑一次就退）
    "interval": 0.0,                 # 循环间隔秒数（仅 loop > 1 有意义）

    # ── GitHub 推送（仅 --push 时用；token 只用于这一次 push，绝不落盘）──
    "github_token": None,            # PAT；留空则读环境变量 GITHUB_TOKEN / GH_TOKEN
    "github_username": None,         # GitHub 用户名；和 repo_name 拼成 owner/repo
    "repo_name": None,               # 仓库名称（不带 owner）；和 github_username 配合
    "branch": None,                  # 目标分支；留空则用当前分支
    "github_branch": None,           # 兼容旧键：目标分支；优先级低于 branch
    "auto_push": False,               # 跑完是否自动提交并推送（自己机器上用 gh_config.json 设 true；--no-push 可临时关掉）
}

CONFIG_TEMPLATE = """\
{
  "_comment": [
    "config.json —— GitHub Actions 跑的那份（生产口径），会被提交进仓库，所以不要放 token。",
    "在自己机器上（Win / Linux / Termux）跑，请用 main.py + gh_config.json，这份不用动。",
    "用法: python scripts/build_pool.py --config config.json   （workflow 里就是这么调的）",
    "命令行参数优先级高于此文件（--sample 会覆盖 sample）；留空或删除某行 = 用内置默认值",
    "每个键前面那行 _ 开头的都只是给人看的注释，加载时会被自动忽略。"
  ],

  "_sample": "每个 /24 随机取几个 IP（起始值）。不够 target 时自动按 sample_step 加码重采",
  "sample": 6,

  "_ports": [
    "加密端口池。每个 IP 只随机分其中一个，所以 6 个端口会大致均匀混在结果里。",
    "443 / 8443 / 2053 / 2083 / 2087 / 2096 是 CF 常用的 HTTPS 端口"
  ],
  "ports": [443, 8443, 2053, 2083, 2087, 2096],

  "_workers": "并发线程数。1000 下纯 TCP 探测很稳；再高本地句柄/端口可能吃紧",
  "workers": 1000,

  "_timeout": "TCP 连接超时（秒）。2 秒是「宁可略过也不干等」的甜点位",
  "timeout": 2.0,

  "_attempts": "每个 IP 的 TCP 连接尝试次数。1 = 只探一次；网络抖动时可调大，让失败的 IP 多重试几轮",
  "attempts": 1,

  "_target": "目标存活条数。默认 20000（2w）；网络差凑不够也不报错，给多少算多少",
  "target": 20000,

  "_sample_step": "每轮补采样时，每个 /24 的取样数增加多少",
  "sample_step": 2,

  "_max_rounds": "最多补采样几轮。轮数越多越可能凑够 target，但耗时也线性上涨",
  "max_rounds": 6,

  "_outdir": "输出目录（相对于当前工作目录）。Actions 的产物就是仓库里的 output/",
  "outdir": "output",

  "_header": [
    "ipv4.txt 首行是否写 # 注释头（记录生成时间/行数）。",
    "下游解析器一般都会跳过 # 行；设 false 就是纯 IP:端口、一行不多"
  ],
  "header": true,

  "_sustain": [
    "可持续化运行四件套（详细说明见 README「可持续化运行」一节）。",
    "Actions 这里保持全关：每次全新覆盖 output/，行为与从前完全一致。",
    "自己机器上要越跑越大 / 断点续跑 / 定时循环，请改 gh_config.json 的对应键。"
  ],

  "_merge": [
    "增量累积。false = 每次跑都重新生成整个文件（默认，Actions 用的就是这个）；",
    "true  = 先读已有的 ipv4.txt，把这次探到的存活 IP 合并进去，池子只增不减。",
    "想让自己机器上的池子越跑越大就开它；凑数时只补 target 减去存量的差额。"
  ],
  "merge": false,

  "_recheck": [
    "存量复核。开之前会先把 ipv4.txt 里已有的 IP:端口 全部重探一遍，",
    "把已经不通的剔掉，再把新探到的补进来。配合 merge 用，池子才不会被死 IP 占坑。",
    "代价是每轮都要把整个存量池重探一遍，池子越大越慢。"
  ],
  "recheck": false,

  "_state_file": [
    "断点续跑状态文件，记下哪些 IP 已经探测过了，",
    "下次跑直接跳过它们、只采新 IP，不会把时间反复花在同一批地址上。",
    "留空字符串 = 不启用。文件就在 outdir 里，可随时删掉重置。",
    "Actions 这里必须留空：merge=false 时旧存活 IP 不保留，再跳过已探 IP，",
    "状态文件被提交回仓库后会越积越大，直到所有 IP 都被跳过、产出 0 条。"
  ],
  "state_file": "",

  "_loop": [
    "循环次数。1 = 跑一次就退（默认）。",
    "大于 1 = 一条命令反复跑，配合 interval 就是自己机器上的定时任务。",
    "Ctrl+C 会优雅收尾：先把已探到的结果写盘，再走推送。"
  ],
  "loop": 1,

  "_interval": "循环间隔秒数。仅 loop 大于 1 有意义；0 = 立刻接着跑下一轮",
  "interval": 0.0,

  "_auto_push": [
    "固定 false。Actions 的提交推送由 workflow 最后一步负责（用 secrets.GITHUB_TOKEN），",
    "这里再开一遍会推两次。要自动推送请改 gh_config.json 的 auto_push。",
    "推送要用的 token / 用户名 / 仓库 / 分支全在 gh_config.json，本文件不碰。"
  ],
  "auto_push": false
}
"""


def _toml_parse(text):
    """
    极简 TOML 子集解析器，仅支持本脚本用到的标量与数组。
    不依赖 tomllib（Python 3.11+）或第三方库。
    """
    cfg = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().strip('"').strip("'")
        val = val.strip()

        # 去掉行内注释（仅当注释前是空白时）
        if "#" in val:
            head, _, tail = val.partition("#")
            # 避免把字符串里的 # 误当注释
            if not (head.count('"') % 2 or head.count("'") % 2):
                val = head.strip()

        if not val:
            continue

        # 布尔
        if val.lower() == "true":
            cfg[key] = True
            continue
        if val.lower() == "false":
            cfg[key] = False
            continue
        if val.lower() in ("null", "none"):
            cfg[key] = None
            continue

        # 数组  [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            body = val[1:-1].strip()
            items = []
            if body:
                for part in body.split(","):
                    p = part.strip().strip('"').strip("'")
                    if p:
                        # 元素本身能转 int 就转，便于 ports 这类数字数组直接用
                        try:
                            items.append(int(p))
                        except ValueError:
                            items.append(p)
            cfg[key] = items
            continue

        # 字符串
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            cfg[key] = val[1:-1]
            continue

        # 数字
        try:
            cfg[key] = int(val)
            continue
        except ValueError:
            pass
        try:
            cfg[key] = float(val)
            continue
        except ValueError:
            pass

        cfg[key] = val
    return cfg


def _load_py_config(path, keep_all=False):
    """
    执行 .py 配置文件，收集里面的配置项。

    支持两种写法（可混用）：
      1. 模块级直接写键：   sample = 2
      2. 一个 CONFIG 字典： CONFIG = {"sample": 2}
    只收 DEFAULT_CONFIG 里认识的键，_ 开头的（如 __name__）一律忽略。
    配置文件是用户自己的文件，这里直接 exec，不做沙箱。
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    ns = {"__name__": "__cf_ip_pool_config__", "__file__": path}
    exec(compile(text, path, "exec"), ns)
    cfg = {}
    block = ns.get("CONFIG")
    if isinstance(block, dict):
        cfg.update(block)
    for k, v in ns.items():
        if not k.startswith("_"):
            cfg[k] = v
    if keep_all:
        return cfg
    return {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}

def load_config(path):
    """读取配置文件（.json 用 json 模块，.toml 用内置解析器，.py 直接执行）。"""
    if not path or not os.path.exists(path):
        if path:
            log(f"      config not found: {path} (using defaults)")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith(".json"):
        cfg = json.loads(text.lstrip("\ufeff"))
    elif path.lower().endswith(".py"):
        cfg = _load_py_config(path)
    else:
        cfg = _toml_parse(text.lstrip("\ufeff"))
    # 去掉 JSON 模板里 _ 开头的说明键，再过滤未知键
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    unknown = [k for k in cfg if k not in DEFAULT_CONFIG]
    if unknown:
        log(f"      config ignored unknown keys: {', '.join(unknown)}")
    return {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}


def log(msg):
    print(msg, flush=True)


# ─── 1. 拉取官方网段 ─────────────────────────────────────────────────────────

def fetch_url(url, timeout=30):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_official_cidrs():
    """只拉 ips-v4。本工具只做 IPv4 纯 TCP 池，不碰 IPv6。"""
    log("[1/4] Fetching official CF IPv4 CIDRs ...")
    try:
        text = fetch_url(CF_IPV4_URL)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
    except Exception as e:
        log(f"      IPv4 fetch failed: {e}")
        lines = []
    log(f"      IPv4: {len(lines)} ranges")
    return lines


# ─── 2. 拆子网 + 采样 ────────────────────────────────────────────────────────

def host_ranges(cidrs):
    """
    把网段拆成 /24 粒度，返回可分配主机区间 (lo, hi) 闭区间列表（整数形式）。

    prefixlen < 24 的按 /24 切开（跳过 .0 网络地址和 .255 广播地址）；
    prefixlen >= 24 的（如 /25、/32）整段当一个区间，同样跳过首尾。
    """
    ranges = []
    for block in cidrs:
        try:
            net = ipaddress.IPv4Network(block, strict=False)
        except ValueError:
            continue
        base = int(net.network_address)
        size = 1 << (32 - net.prefixlen)
        if net.prefixlen < 24:
            blk = 1 << 8                       # /24 = 256
            n_blocks = size // blk
            for i in range(n_blocks):
                s = base + i * blk
                ranges.append((s + 1, s + blk - 2))
        elif size <= 2:                        # /31、/32 没有可跳过的首尾
            ranges.append((base, base + size - 1))
        else:
            ranges.append((base + 1, base + size - 2))
    return ranges


def total_hosts(ranges):
    return sum(hi - lo + 1 for lo, hi in ranges)


def sample_ips(ranges, k, exclude):
    """
    每个 /24 区间随机取 k 个 IP（整数），跳过 exclude 里已探测过的。
    返回打乱顺序的新 IP 列表。
    """
    out = []
    for lo, hi in ranges:
        n = hi - lo + 1
        kk = min(k, n)
        if kk <= 0:
            continue
        for off in random.sample(range(n), kk):
            ip = lo + off
            if ip in exclude:
                continue
            out.append(ip)
    random.shuffle(out)
    return out


# ─── 3. 并发 TCP 探测 ────────────────────────────────────────────────────────

_lock = threading.Lock()
_prog = {"done": 0, "alive": 0}
_errs = Counter()
_errs_lock = threading.Lock()


def _max_fds():
    """
    当前进程能同时打开的文件描述符上限；拿不到就返回 None。

    Windows 没有 ulimit 这套东西，也没有 resource 模块，所以直接返回 None，
    由调用方按「不限制」处理。
    """
    try:
        import resource  # noqa: F401  (仅 POSIX)
    except ImportError:
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return None
    # RLIM_INFINITY 在不同平台是 -1 或 2**64-1，都当成「没限制」
    if soft in (resource.RLIM_INFINITY, -1) or soft <= 0:
        return None
    return int(soft)


def cap_workers(workers, reserve=64):
    """
    把 workers 压到「不会把 fd 用光」的范围，返回 (调整后值, 提示或 None)。

    每个在飞的探测占 1 个 fd，加上 stdin/stdout/stderr，
    所以按 cap 的 80% 且留 reserve 个给系统，取更严的那个。
    限制很低（比如 256）时宁可降并发，也不要让一堆连接报
    Too many open files —— 那会被当成「IP 不通」，悄悄压低命中率。
    """
    cap = _max_fds()
    if cap is None:
        return workers, None
    limit = max(1, min(cap - reserve, int(cap * 0.8)))
    if workers <= limit:
        return workers, None
    return limit, (f"workers {workers} -> {limit} "
                   f"(fd limit {cap}, reserved {reserve} for the system)")


def _bump(done=0, alive=0):
    with _lock:
        _prog["done"] += done
        _prog["alive"] += alive


def _note_err(exc):
    with _errs_lock:
        _errs[type(exc).__name__] += 1


def probe_ip(ip, port, timeout, attempts=1):
    """
    纯 TCP 连接探测：连上即算可用。

    ip 可以是 int 或 str（内部统一用 int 做去重/排序，探测前转成点分十进制）。
    不做 TLS 握手、不读任何内容、不解析响应。timeout 秒连不上就当不可用。
    attempts = 最多尝试几次（1 = 只探一次）；任意一次连上即算可用。
    """
    host = ip if isinstance(ip, str) else str(ipaddress.IPv4Address(ip))
    ok = False
    last = None
    for i in range(max(1, int(attempts))):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                ok = True
                break
        except Exception as e:
            last = e
            if i + 1 < attempts:
                time.sleep(0.1)   # 重试前稍停一下，别瞬间打满
    if not ok and last is not None:
        # 每个 IP 只记一次失败原因，attempts 重试不会把计数放大
        _note_err(last)
    # done/alive 分开计数：alive 只统计真的连上的，进度日志才看得出命中率
    _bump(done=1, alive=1 if ok else 0)
    return (ip, port) if ok else None


def probe_pairs(pairs, workers, timeout, label="", attempts=1):
    log(f"      probing {len(pairs)} (IP,port) pairs | workers {workers} | "
        f"timeout {timeout}s x{max(1, int(attempts))} {label}")
    with _lock:
        _prog["done"] = 0
        _prog["alive"] = 0
    with _errs_lock:
        _errs.clear()

    out = []
    last = time.time()
    total = len(pairs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(probe_ip, ip, port, timeout, attempts) for ip, port in pairs]
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                out.append(r)
            now = time.time()
            if now - last >= 5:
                with _lock:
                    d, a = _prog["done"], _prog["alive"]
                log(f"        progress {d}/{total} ({d*100/max(1,total):.1f}%)  alive {a}")
                last = now

    # 失败原因分布：全是 0 存活时，靠这个一眼看出是系统性错误而不是真不通
    with _errs_lock:
        top = _errs.most_common(5)
    if top:
        log("        fail reasons: "
            + ", ".join(f"{k} {v}" for k, v in top))
    if not out and pairs:
        log("        ! 0 alive -- check network/DNS/firewall, not just CF reachability")

    return out


def build_pool(ranges, cfg, carry=None, state_ips=None):
    """
    采样 + 探测，直到凑够 target 条存活记录。

    每个 IP 只探测一次；端口从 cfg["ports"] 里随机取一个。
    一轮不够就按 sample_step 加大每 /24 取样数再采新 IP，最多 max_rounds 轮。

    carry     —— 已有的存活记录（merge 模式下从旧 ipv4.txt 读来的），直接算进结果，
                 采样时对应的 IP 会被排除，不会重复探测。
    state_ips —— 之前几轮已经探测过的 IP 整数集合（断点续跑），同样在采样时排除。
    """
    target = int(cfg["target"])
    ports = [int(p) for p in cfg["ports"]]
    if not ports:
        log("no ports configured, exit")
        sys.exit(1)
    workers = int(cfg["workers"])
    timeout = float(cfg["timeout"])
    attempts = max(1, int(cfg.get("attempts", 1)))
    step = max(1, int(cfg.get("sample_step", 2)))
    max_rounds = max(1, int(cfg.get("max_rounds", 6)))
    # 预估命中率，用来决定第一轮采多少（宁多勿少，减少往返轮次）；
    # 第二轮起改用上一轮的实测值
    hit_rate = 0.9

    capacity = total_hosts(ranges)

    # 存量直接进结果；这些 IP 不再采，省下的探测额度全部用来找新 IP
    carry = list(carry or [])
    state_ips = set(state_ips or ())
    tried = set(state_ips)
    for ip_s, _port in carry:
        try:
            tried.add(int(ipaddress.IPv4Address(ip_s)))
        except (ValueError, ipaddress.AddressValueError):
            pass
    alive = list(carry)
    seen_pairs = set(alive)
    carried = len(carry)
    k = max(1, int(cfg["sample"]))

    log(f"[2/4] Sampling from {len(ranges)} /24 blocks "
        f"({capacity} usable host addresses)")
    if carried:
        log(f"      carry-over: {carried} alive from the existing pool, "
            f"need {max(0, target - carried)} more")
    if state_ips:
        log(f"      resume: skipping {len(state_ips)} IPs probed in earlier runs")

    for rnd in range(1, max_rounds + 1):
        need = target - len(alive)
        if need <= 0:
            break

        want = int(need / hit_rate) + 500
        batch = sample_ips(ranges, k, tried)[:want]
        if not batch:
            log(f"      round {rnd}: no unsampled IPs left "
                f"(tried {len(tried)}/{capacity})")
            break
        tried.update(batch)

        log(f"[3/4] round {rnd}/{max_rounds}: k={k} per /24, "
            f"{len(batch)} new IPs (covered {len(tried)}/{capacity})")
        pairs = [(ip, random.choice(ports)) for ip in batch]
        got = probe_pairs(pairs, workers, timeout, f"(round {rnd})", attempts)
        for ip, port in got:
            if (ip, port) in seen_pairs:
                continue
            seen_pairs.add((ip, port))
            alive.append((ip, port))

        log(f"      round {rnd}: alive {len(got)} / {len(batch)} "
            f"({len(got)*100/max(1,len(batch)):.1f}%)  total {len(alive)}/{target}")
        if len(alive) >= target:
            break
        k += step
        # 用实测命中率替换预估，下一轮采得更准
        hit_rate = max(0.05, min(1.0, len(got) / max(1, len(batch))))

    # 实际探测的 IP 数（不含探测完就被丢掉的存活记录），用于总结里的准确统计
    stats = {
        "probed": len(tried),
        "alive": len(alive),
        "kept": min(len(alive), target),
        "carried": carried,
        "newly_probed": max(0, len(tried) - len(state_ips)),
        # 供断点续跑落盘用：这一轮实际碰过的 IP（含从状态里继承的）
        "_probed_ints": tried,
    }

    if len(alive) < target:
        log(f"      ! only reached {len(alive)}/{target} after "
            f"{max_rounds} rounds; using what we got")

    return alive[:target], stats



# ─── 4. 可持续化：存量池 / 断点续跑 / 存量复核 ─────────────────────────────────

def load_pool_pairs(path):
    """读已有的 ipv4.txt，返回 [(ip, port), ...]（跳过 # 注释头和空行）。

    解析失败/文件不存在都返回 []，绝不让一个坏文件把整个流程带崩。
    """
    if not path or not os.path.exists(path):
        return []
    out = []
    bad = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    bad += 1
                    continue
                ip_s, _, port_s = line.rpartition(":")
                try:
                    ipaddress.IPv4Address(ip_s.strip())
                    port = int(port_s.strip())
                except (ValueError, ipaddress.AddressValueError):
                    bad += 1
                    continue
                if not (0 < port < 65536):
                    bad += 1
                    continue
                out.append((ip_s.strip(), port))
    except OSError as exc:
        log(f"  ! 读不到 {path}: {exc}")
        return []
    if bad:
        log(f"  ! {path} 有 {bad} 行格式不对，已跳过")
    return out


def _state_path(outdir, cfg):
    """状态文件完整路径；没配 state_file 就返回 None。"""
    name = (cfg.get("state_file") or "").strip()
    if not name:
        return None
    return os.path.join(outdir, name)


def load_state(path):
    """读断点续跑状态：返回已经探测过的 IP 整数集合。"""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ips = data.get("probed", [])
        if not isinstance(ips, list):
            return set()
        return {int(x) for x in ips if str(x).lstrip("-").isdigit()}
    except (OSError, ValueError, TypeError):
        log(f"  ! {path} 读不出来，按空状态处理")
        return set()


def save_state(path, probed):
    """把探测过的 IP 整数集合写回状态文件（排序后写成紧凑 JSON）。"""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"probed": sorted(probed)}, f)
    except OSError as exc:
        log(f"  ! 状态写不进去 {path}: {exc}")


def recheck_pool(pairs, cfg):
    """存量复核：把已有的 IP:端口 全部重探一遍，只留下还通着的。

    端口分布照原样保留（一个 IP 原来配哪个端口就还探哪个），
    这样合并后的池子端口结构不会被打乱。
    """
    if not pairs:
        return pairs, 0
    workers = int(cfg["workers"])
    timeout = float(cfg["timeout"])
    attempts = max(1, int(cfg.get("attempts", 1)))
    log(f"[2b] 存量复核: 重探 {len(pairs)} 条已有记录 "
        f"(workers {workers}, timeout {timeout}s x{attempts}) ...")
    alive = set(probe_pairs(list(pairs), workers, timeout, "(存量复核)", attempts))
    kept = [(ip, port) for ip, port in pairs if (ip, port) in alive]
    dropped = len(pairs) - len(kept)
    log(f"      存量复核: {len(kept)} 通 / {dropped} 不通"
        + (f"（已剔除 {dropped} 条）" if dropped else ""))
    return kept, dropped

# ─── 5. 输出 ─────────────────────────────────────────────────────────────────

def _now_cn():
    """当前北京时间（UTC+8）。CI 与自己机器上都用它，保证时间戳一致。
    不依赖 tzdata，直接拿 UTC 加 8 小时，避免 Windows/精简容器没有时区库。"""
    return time.gmtime(time.time() + 8 * 3600)


def _stamp_cn():
    """北京时间字符串，例如 2026-10-16 14:30:05"""
    return time.strftime("%Y-%m-%d %H:%M:%S", _now_cn())


def _sort_key(r):
    """IPv4 按整数地址排序（不是字符串排序）。"""
    try:
        return int(ipaddress.ip_address(r["ip"]))
    except ValueError:
        return 0


def _write_list(path, rows, stamp, kind="", header=True):
    """写 IP:端口 列表文件。

    默认首行是注释头（以 # 开头），记录生成时间/行数。
    下游解析器都会跳过 # 开头的行，所以不影响使用，
    但打开文件就能看到「这批数据是什么时候出的」。
    header=False 时一行不多，纯 IP:端口。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if stamp and header:
            tag = f" {kind}" if kind else ""
            f.write(f"# Cloudflare IP Pool{tag} | {stamp} (UTC+8) | "
                    f"{len(rows)} IPs | IP:Port\n")
        for r in rows:
            f.write(f"{r['ip']}:{r['port']}\n")


def write_outputs(pairs, outdir, cfg):
    """
    只输出 ipv4.txt（纯 IP:端口，仅 TCP 通的）。

    不查 GeoIP、不读 colo、不产国家字段、不输出 IPv6 / 混合表 / CSV。
    meta.json 只是个小的元信息边车（时间、条数、端口分布），不是 IP 池文件。
    """
    log("[4/4] Writing outputs ...")
    os.makedirs(outdir, exist_ok=True)

    seen = set()
    rows = []
    for ip, port in pairs:
        key = (ip, port)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "ip": str(ipaddress.IPv4Address(ip)),
            "port": int(port),
            "ver": 4,
        })

    rows.sort(key=_sort_key)

    # 统一时间戳：文件头、meta.json、提交信息都用它（北京时间 UTC+8）
    stamp = _stamp_cn()

    v4_txt = os.path.join(outdir, "ipv4.txt")
    meta_json = os.path.join(outdir, "meta.json")

    # 唯一输出: IP:端口（仅 IPv4，仅 TCP 通）
    _write_list(v4_txt, rows, stamp, "IPv4", header=bool(cfg.get("header", True)))

    # 端口分布
    port_cnt = Counter(r["port"] for r in rows)

    with open(meta_json, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": stamp + " UTC+8",
            "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "total": len(rows),
            "unique_ips": len({r["ip"] for r in rows}),
            "ports": {str(k): v for k, v in port_cnt.most_common()},
            "probe": {
                "mode": "tcp",
                "workers": int(cfg["workers"]),
                "timeout": float(cfg["timeout"]),
                "attempts": int(cfg.get("attempts", 1)),
                "target": int(cfg["target"]),
            },
            "sustain": {
                "merge": bool(cfg.get("merge", False)),
                "recheck": bool(cfg.get("recheck", False)),
                "carried": int(cfg.get("_carried", 0)),
                "recheck_dropped": int(cfg.get("_recheck_dropped", 0)),
                "newly_probed": int(cfg.get("_newly_probed", 0)),
                "state_file": (cfg.get("state_file") or "") or None,
                "loop": int(cfg.get("loop", 1) or 1),
            },
        }, f, ensure_ascii=False, indent=2)

    log(f"      stamp: {stamp} UTC+8")
    log(f"      {v4_txt}    ({len(rows)} lines)")
    log(f"      {meta_json}")
    return rows


# ─── 5. GitHub 推送（自己机器上跑完直接推） ──────────────────────────────────

def _git(args):
    """跑一条 git 命令，返回 CompletedProcess（不抛异常，失败看 returncode）。"""
    import subprocess
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )

def _resolve_token(cfg):
    """
    取 GitHub token，优先级：环境变量 > 配置里的 github_token。
    环境变量优先，方便临时覆盖，也避免把 token 写进配置文件。
    """
    return (os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or (cfg or {}).get("github_token")
            or None)

def _origin_slug():
    """把 origin 的 URL 归一化成 owner/repo；拿不到就返回 None。"""
    r = _git(["remote", "get-url", "origin"])
    url = (r.stdout or "").strip()
    if not url:
        return None
    # 支持 https://github.com/owner/repo(.git) 和 git@github.com:owner/repo(.git)
    tail = url
    if tail.startswith("git@github.com:"):
        tail = tail[len("git@github.com:"):]
    elif "github.com/" in tail:
        tail = tail.split("github.com/", 1)[1]
    else:
        return None
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = tail.strip("/")
    parts = tail.split("/")
    if len(parts) < 2:
        return None
    return "/".join(parts[:2])

def _mask(url):
    """日志里给 URL 的 token 打码，避免密钥进日志。"""
    if "@" not in url:
        return url
    head, _, tail = url.partition("@")
    scheme, sep, _cred = head.partition("//")
    if not sep:
        return url
    return scheme + "//***@" + tail

PLACEHOLDER_PREFIX = "your_"

def _is_placeholder(val):
    """判断是不是还没替换的占位符（your_xxx）。"""
    return isinstance(val, str) and val.strip().lower().startswith(PLACEHOLDER_PREFIX)

def _check_push_config(cfg):
    """
    推送前检查配置，返回 (ok, 要提示的问题列表)。

    github_token 是占位符（或没填）本身不算错——可以走环境变量；
    但 github_username / repo_name / branch 是占位符时，推上去会指向一个假仓库，必须拦下。
    """
    cfg = cfg or {}
    problems = []
    for key in ("github_username", "repo_name", "branch"):
        if _is_placeholder(cfg.get(key)):
            problems.append(f'{key} 还是占位符 "{cfg.get(key)}"，请改成真实值')
    return (not problems), problems
def _resolve_repo(cfg):
    """
    解析推送目标 owner/repo，按优先级：

    1. github_username + repo_name  （推荐写法，两个都填才算）
    2. github_repo                 （兼容旧键，直接写 owner/repo）
    3. origin 的 URL               （都没填就自动取）
    """
    cfg = cfg or {}
    user = (cfg.get("github_username") or "").strip()
    name = (cfg.get("repo_name") or "").strip()
    if user and name:
        return f"{user}/{name}"
    repo = (cfg.get("github_repo") or "").strip()
    if repo:
        return repo
    return _origin_slug()

def _resolve_branch(cfg):
    """目标分支：branch > github_branch（旧键）> 当前分支。"""
    cfg = cfg or {}
    for key in ("branch", "github_branch"):
        val = (cfg.get(key) or "").strip()
        if val:
            return val
    cur = (_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout or "").strip()
    return "" if cur == "HEAD" else cur
GH_CONFIG_NAME = "gh_config.json"

def _gh_config_candidates(path, cfg_path=None):
    """
    gh_config.json 的查找顺序：命令行指定 > 当前目录 > 配置文件所在目录 > 脚本所在目录。
    """
    if path:
        return [path]
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(os.getcwd(), GH_CONFIG_NAME)]
    if cfg_path:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(cfg_path)), GH_CONFIG_NAME))
    cands.append(os.path.join(here, GH_CONFIG_NAME))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def load_gh_config(path=None, cfg_path=None):
    """
    读取 gh_config.json（自己机器上那份），取出其中与推送相关的键。

    这份文件同时也可以当运行配置用（--config gh_config.json），
    所以里面除了推送键还会有运行键；这里只挑推送键，其余由 load_config 负责。

    token 这类东西不该塞进仓库里，所以单独放一个被 .gitignore 忽略的文件::

        {"github_token": "ghp_xxx", "github_username": "me",
         "repo_name": "cf-ip-pool", "branch": "main"}

    文件不存在就返回空 dict（此时仍可走环境变量 / 本机 git 凭据）。
    """
    for p in _gh_config_candidates(path, cfg_path):
        if os.path.exists(p):
            break
    else:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.loads(f.read().lstrip("\ufeff"))
    except Exception as e:
        log(f"      ! {p} 读取失败: {e}（忽略，继续用其他来源）")
        return {}
    if not isinstance(raw, dict):
        log(f"      ! {p} 顶层不是对象，已忽略")
        return {}
    unknown = [k for k in raw if not k.startswith("_") and k not in DEFAULT_CONFIG]
    if unknown:
        log(f"      {p} ignored unknown keys: {', '.join(unknown)}")
    return {k: v for k, v in raw.items() if k in PUSH_KEYS}
def push_outputs(outdir, cfg=None):
    """
    把 outdir 提交并用 token 推送到 GitHub，只在真的有变化时提交。

    自己机器上跑完直接更新 GitHub 上的池子::

        python scripts/build_pool.py --config config.local.py --push

    认证优先级：环境变量 GITHUB_TOKEN / GH_TOKEN > 配置里的 github_token。
    都没有就退回本机 git 凭据（Windows 凭据管理器 / ~/.git-credentials）。
    token 只拼进这一次 push 的临时 URL，用完即弃，不写进 .git/config。
    """
    cfg = cfg or {}
    log(f"[push] {outdir} -> GitHub")

    ok, problems = _check_push_config(cfg)
    if not ok:
        for p in problems:
            log(f"  ! {p}")
        log("    hint: 占位符（your_xxx）不会被当真；填好真实值再重试")
        log("          只想要结果不推送，先别加 --push")
        return False

    r = _git(["rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0:
        log("  ! not inside a git working tree, skip")
        return False

    _git(["add", outdir])
    r = _git(["diff", "--staged", "--quiet"])
    if r.returncode == 0:
        log(f"  no changes in {outdir}, nothing to push")
        return False

    stamp = _stamp_cn()
    msg = f"chore: update CF IP pool {stamp} UTC+8"
    r = _git(["commit", "-m", msg])
    if r.returncode != 0:
        log(f"  ! commit failed: {(r.stderr or r.stdout).strip()}")
        log("    hint: 先设 git user.name / user.email")
        return False
    head = (r.stdout or "").strip().splitlines()
    if head:
        log(f"  {head[0]}")

    # 目标分支：branch / github_branch > 当前分支
    branch = _resolve_branch(cfg)
    if not branch:
        log("  ! cannot determine current branch")
        return False

    repo = _resolve_repo(cfg)
    token = _resolve_token(cfg)

    if token and repo:
        # token 只存在于这条命令的参数里，不写进 .git/config
        push_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        log(f"  pushing with token -> {repo} @ {branch}")
        r = _git(["push", push_url, f"HEAD:refs/heads/{branch}"])
    elif token and not repo:
        log('  ! 有 token 但拿不到仓库名：请在配置里写 github_username + repo_name')
        log('    （也可以直接写 github_repo = "owner/repo"）')
        return False
    else:
        log("  no token found; falling back to local git credentials")
        r = _git(["push", "origin", f"HEAD:refs/heads/{branch}"])

    if r.returncode != 0:
        raw = (r.stderr or r.stdout or "").strip()
        log(f"  ! push failed: {_mask(raw)}")
        low = raw.lower()
        if "authentication failed" in low or "401" in low or "invalid username or password" in low:
            log("    hint: token 无效或过期；fine-grained PAT 需要 Contents: Read and write")
        elif "403" in low or "denied" in low:
            log("    hint: token 权限不足（需要 Contents 写权限），或仓库不在 token 授权范围内")
        elif "not found" in low or "404" in low:
            log("    hint: 仓库名不对（github_username / repo_name），或 token 无权访问")
        elif "protected branch" in low or "rejected" in low:
            log("    hint: 目标分支受保护，或远端有新提交（先 git pull --rebase）")
        else:
            log("    hint: 检查 GITHUB_TOKEN / 配置里的 github_token、网络与仓库权限")
        return False

    log(f"  pushed to {repo or 'origin'} @ {branch}  ({msg})")
    return True

# ─── 主流程 ──────────────────────────────────────────────────────────────────

def _cli_overrides(args):
    """
    只收集「用户真的在命令行显式给了」的参数。
    办法是扫 sys.argv，避免用 None 做默认值时无法区分「没给」和「给了空」。
    """
    given = set()
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            given.add(name)
        elif i > 0 and argv[i - 1].startswith("--"):
            # 空格分隔的取值形式：--sample 3
            given.add(argv[i - 1][2:].split("=", 1)[0])
    return given


def main():
    """
    主流程：CLI 参数解析 → 配置合并 → 拉网段 → 采样探测 → 输出。
    """
    ap = argparse.ArgumentParser(
        description="Build Cloudflare IPv4 IP pool (plain IP:port, TCP only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="可用 --init-config 生成默认 config.json 模板")
    ap.add_argument("--config", type=str, default="config.json",
                    help="配置文件路径 (默认 config.json，不存在则用内置默认)")
    ap.add_argument("--init-config", action="store_true",
                    help="生成默认 config.json 模板后退出")
    ap.add_argument("--sample", type=int, default=None,
                    help="每个 /24 取几个 IP (默认 6，不够会自动加码)")
    ap.add_argument("--ports", type=str, default=None,
                    help="加密端口，逗号分隔 (默认 443,8443,2053,2083,2087,2096)")
    ap.add_argument("--workers", type=int, default=None, help="并发数 (默认 1000)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="TCP 连接超时秒数 (默认 2.0)")
    ap.add_argument("--target", type=int, default=None,
                    help="目标存活条数 (默认 20000)")
    ap.add_argument("--outdir", type=str, default=None, help="输出目录")
    ap.add_argument("--no-header", action="store_true",
                    help="ipv4.txt 首行不写 # 注释头，输出纯 IP:端口")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--merge", dest="merge", action="store_true", default=None,
                    help="增量累积：读已有 ipv4.txt 合并而非覆盖，池子只增不减")
    ap.add_argument("--no-merge", dest="merge", action="store_false", default=None,
                    help="关闭增量累积（每次重新生成整个文件）")
    ap.add_argument("--recheck", dest="recheck", action="store_true", default=None,
                    help="存量复核：先把已有 IP:端口 重探一遍，不通的剔掉")
    ap.add_argument("--no-recheck", dest="recheck", action="store_false", default=None,
                    help="关闭存量复核")
    ap.add_argument("--state-file", type=str, default=None,
                    help="断点续跑状态文件（默认 pool_state.json，空串=不启用）")
    ap.add_argument("--loop", type=int, default=None,
                    help="循环次数（1=跑一次就退，大于 1 配合 --interval 当定时任务用）")
    ap.add_argument("--interval", type=float, default=None,
                    help="循环间隔秒数（仅 --loop 大于 1 有意义）")
    ap.add_argument("--gh-config", type=str, default=None,
                    help="推送配置文件路径 (默认 gh_config.json，不存在则忽略)")
    ap.add_argument("--init-gh-config", action="store_true",
                    help="生成 gh_config.json 模板后退出")
    ap.add_argument("--push", action="store_true",
                    help=f"构建完成后把 outdir 提交并推送到 GitHub（token 读 gh_config.json / 环境变量）")
    ap.add_argument("--no-push", action="store_true",
                    help="本次不推送（覆盖配置里的 auto_push=true）")
    args = ap.parse_args()

    # --init-config 特殊模式：生成模板后立即退出
    if args.init_config:
        target = args.config or "config.json"
        if os.path.exists(target):
            log(f"{target} already exists, not overwritten")
        else:
            with open(target, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(CONFIG_TEMPLATE)
            log(f"wrote {target}")
        return

    # --init-gh-config 特殊模式：生成推送配置模板后立即退出
    if args.init_gh_config:
        target = args.gh_config or GH_CONFIG_NAME
        if os.path.exists(target):
            log(f"{target} already exists, not overwritten")
        else:
            with open(target, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(GH_CONFIG_TEMPLATE)
            log(f"wrote {target}")
            log(f"  (已被 .gitignore 忽略；把 your_ 开头的值换成真实值就能用)")
        return

    # 配置优先级：内置默认 < config.json < 命令行显式参数
    # 用 _cli_overrides 扫 argv 判断「用户到底给了哪些」，避免 None 歧义
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        cfg.update(load_config(args.config))

    # 推送配置单独一份（gh_config.json），只覆盖推送键，
    # 这样 token 可以只存在这一个被忽略的文件里
    gh_cfg = load_gh_config(args.gh_config, args.config)
    if gh_cfg:
        cfg.update(gh_cfg)
        log(f"      loaded push config from {args.gh_config or GH_CONFIG_NAME}")
    given = _cli_overrides(args)

    if "sample" in given:
        cfg["sample"] = args.sample
    if "ports" in given:
        cfg["ports"] = [p.strip() for p in (args.ports or "").split(",") if p.strip()]
    if "workers" in given:
        cfg["workers"] = args.workers
    if "timeout" in given:
        cfg["timeout"] = args.timeout
    if "target" in given:
        cfg["target"] = args.target
    if "outdir" in given:
        cfg["outdir"] = args.outdir
    if "no_header" in given:
        cfg["header"] = not args.no_header
    if "seed" in given:
        cfg["seed"] = args.seed
    if args.merge is not None:
        cfg["merge"] = bool(args.merge)
    if args.recheck is not None:
        cfg["recheck"] = bool(args.recheck)
    if args.state_file is not None:
        cfg["state_file"] = args.state_file
    if args.loop is not None:
        cfg["loop"] = max(1, int(args.loop))
    if args.interval is not None:
        cfg["interval"] = max(0.0, float(args.interval))

    if cfg["seed"] is not None:
        random.seed(cfg["seed"])

    # fd 上限保护：并发探得动才叫并发，超了会被 OS 拒连，
    # 报出来还长得很像「IP 不通」，所以这里提前压一压
    capped, note = cap_workers(int(cfg["workers"]))
    if note:
        cfg["workers"] = capped
        log(f"  ! {note}")

    t0 = time.time()
    log("=" * 60)
    log("CF IP Pool Builder  (IPv4 / TCP only / plain IP:Port)")
    if args.config and os.path.exists(args.config):
        log(f"config: {args.config}")
    log("=" * 60)
    log(f"sample={cfg['sample']}(+{cfg['sample_step']}/round, max {cfg['max_rounds']} rounds) "
        f"target={cfg['target']}")
    log(f"ports={cfg['ports']} workers={cfg['workers']} "
        f"timeout={cfg['timeout']}s x{cfg.get('attempts', 1)} outdir={cfg['outdir']}"
        + ("" if cfg.get("header", True) else " (no header)"))

    # 可持续化运行：一条命令反复跑，Ctrl+C 优雅收尾
    total_loop = max(1, int(cfg.get("loop", 1) or 1))
    interval = max(0.0, float(cfg.get("interval", 0.0) or 0.0))
    if total_loop > 1:
        log(f"sustain: loop x{total_loop}, interval {interval:g}s, "
            f"merge={bool(cfg.get('merge'))}, recheck={bool(cfg.get('recheck'))}, "
            f"state={cfg.get('state_file') or '(off)'}")

    pool_txt = os.path.join(cfg["outdir"], "ipv4.txt")
    st_path = _state_path(cfg["outdir"], cfg)
    probed_ips = load_state(st_path) if st_path else set()

    rows = []
    pairs = []
    stats = {"probed": 0, "alive": 0, "kept": 0, "carried": 0, "newly_probed": 0}
    interrupted = False

    try:
        for rnd_i in range(1, total_loop + 1):
            if total_loop > 1:
                log("-" * 60)
                log(f"[loop {rnd_i}/{total_loop}]  {_stamp_cn()} UTC+8")
                log("-" * 60)

            # 1. 拉取官方 CIDR（只 ips-v4）——每轮重拉，网段有变化能及时跟上
            cidrs = fetch_official_cidrs()
            if not cidrs:
                log("no CIDRs fetched, exit")
                sys.exit(1)

            # 2. 拆 /24 + 自适应采样 + 3. 并发 TCP 探测
            ranges = host_ranges(cidrs)
            if not ranges:
                log("no usable host ranges, exit")
                sys.exit(1)

            # 存量累积：先把旧池子读进来当底
            carry = []
            dropped = 0
            if cfg.get("merge"):
                carry = load_pool_pairs(pool_txt)
                if carry:
                    log(f"[1/4] merge: 读入已有池 {len(carry)} 条 ({pool_txt})")
                    if cfg.get("recheck"):
                        carry, dropped = recheck_pool(carry, cfg)
                cfg["_carried"] = len(carry)
                cfg["_recheck_dropped"] = dropped
            else:
                cfg["_carried"] = 0
                cfg["_recheck_dropped"] = 0

            pairs, stats = build_pool(ranges, cfg, carry=carry, state_ips=probed_ips)
            cfg["_newly_probed"] = stats.get("newly_probed", 0)

            # 4. 输出（只有 ipv4.txt）
            rows = write_outputs(pairs, cfg["outdir"], cfg)

            # 断点续跑：这轮探过的 IP 记下来，下一轮/下次启动直接跳过
            if st_path:
                probed_ips |= set(stats.get("_probed_ints", ()))
                save_state(st_path, probed_ips)
                log(f"      state: {len(probed_ips)} IPs 已探测过 -> {st_path}")

            if rnd_i < total_loop:
                if interval > 0:
                    log(f"      sleep {interval:g}s ... (Ctrl+C 收尾并推送)")
                    time.sleep(interval)
                else:
                    log("      next round immediately (Ctrl+C 收尾并推送)")
    except KeyboardInterrupt:
        interrupted = True
        log("")
        log("  ^C 收到中断，先把已探到的结果落盘再收尾")
        if rows:
            log(f"      保留本轮已写出的 {len(rows)} 条")

    if interrupted:
        log(f"  ! 中断于第 {min(total_loop, rnd_i)}/{total_loop} 轮，"
            f"输出与状态已保存，下次启动会接着跑")

    # 5. 跑完自动推送到 GitHub：--push 强制开，--no-push 强制关，否则看配置的 auto_push
    #   （在自己机器上跑，gh_config.json 里设 auto_push=true，就不用每次手打 --push；
    #     GitHub Actions 跑 config.json 时 auto_push=false，提交推送交给 workflow 第 6 步）
    if args.no_push:
        auto_push = False
        log(f"  tip: --no-push 已指定，跳过推送（配置里的 auto_push={cfg.get('auto_push')}）")
    else:
        auto_push = bool(args.push or cfg.get("auto_push"))
    if auto_push:
        push_outputs(cfg["outdir"], cfg)
    elif not args.no_push:
        log(f"  tip: 加 --push 或在配置里设 auto_push=true，可把 {cfg['outdir']}/ 提交并推送到 GitHub")
    log("=" * 60)
    log(f"finished in {time.time()-t0:.1f}s")
    carried = int(stats.get("carried", 0) or 0)
    fresh = int(stats.get("newly_probed", 0) or 0)
    if carried or fresh:
        log(f"  probed {stats['probed']} IPs (carry {carried} + new {fresh}) "
            f"-> alive {stats['alive']} -> kept {stats['kept']} (target {cfg['target']})")
    else:
        log(f"  probed {stats['probed']} IPs -> alive {stats['alive']} "
            f"-> kept {stats['kept']} (target {cfg['target']})")
    if interrupted:
        log(f"  ! 被中断，上面的数字是中断前已落盘的结果")
    if total_loop > 1 and not interrupted:
        log(f"  loop finished: {total_loop} round(s), "
            f"pool now {len(rows)} entries ({pool_txt})")
    if rows:
        port_cnt = Counter(r["port"] for r in rows)
        log(f"  ports: {', '.join(f'{k}:{v}' for k,v in port_cnt.most_common())}")
    log("=" * 60)


if __name__ == "__main__":
    main()
