# Cloudflare IP Pool

自动生成 Cloudflare 可用 IPv4 池，每 6 小时刷新。**纯 `IP:端口`，无国家/机房标注。**

GitHub 构建--[ipv4.txt](https://raw.githubusercontent.com/RIKKASAIKOq/cf-ip-pool/refs/heads/main/output/ipv4.txt)
```
https://raw.githubusercontent.com/RIKKASAIKOq/cf-ip-pool/refs/heads/main/output/ipv4.txt
```



## 产出

| 文件 | 说明 |
|---|---|
| `output/ipv4.txt` | 唯一输出，格式 `IP:端口`，只收 TCP 探测通过的 IP |
| `output/meta.json` | 元信息：生成时间、条数、端口分布、探测参数 |

第 1 行是 `#` 注释头（时间 / 条数），下游解析器一般会跳过：

```
# Cloudflare IP Pool IPv4 | 2026-10-17 02:30:05 (UTC+8) | 20000 IPs | IP:Port
104.23.207.194:443
```

探测是**纯 TCP 连通性**：不做 TLS 握手、不读 `cdn-cgi/trace`。连上即收，2 秒为限。

## 怎么跑

一条命令走完：预检推送配置 → 生成 IP 池 → 按需推送。

| 平台 | 命令 |
|---|---|
| Windows | `run.bat` 或 `run.ps1` |
| Linux / macOS | `./run`（无执行权限时 `sh run`） |
| Termux / Android | `./run` 或 `./run termux` |
| 任意（有 Python 就行） | `python main.py` |

平台会自动检测，检测不到就显式写，例如 `./run win`。

启动脚本认两个裸命令，其余参数原样透传：

| 命令 | 作用 |
|---|---|
| `run` / `run.bat` / `run.ps1` | 跑构建（预检 → 生成 → 推送） |
| `... check` | 只自检推送链路通不通，不构建 |
| `... init` | 生成 `gh_config.json` 模板后退出 |
| `... schedule install` | 装系统定时任务，之后到点自己跑 |
| `... schedule status` | 看装没装、跑的什么命令 |
| `... schedule uninstall` | 卸掉定时任务 |
| `... --no-push` | 这次不推送，只本地生成 |
| `... --target 30` | 临时改目标条数，其余参数同理 |

> GitHub Actions **不走 main.py**，直接跑 `scripts/build_pool.py --config config.json`。

## 配置

两份 JSON，分工固定，别混用：

| 文件 | 提交 | 给谁用 |
|---|---|---|
| `config.json` | ✅ | **GitHub Actions 定时任务**：`target=20000`、`workers=1000` 生产口径，不放 token |
| `gh_config.json` | ❌（gitignore） | **自己机器上跑**：小样本 + 推送设置（token 在这里），跑完自动推 |

自己机器上跑一律用 `gh_config.json`，**不要动 `config.json`**。

**JSON 没有注释语法**，所以两份配置里 `_` 开头的键就是注释，加载时自动跳过，可随便删改。

| 键 | 默认 | 说明 |
|---|---|---|
| `sample` | 6 | 每个 /24 随机取几个 IP，凑不够 `target` 时按 `sample_step` 加码重采 |
| `ports` | `[443,8443,2053,2083,2087,2096]` | 端口池，**每个 IP 随机分一个** |
| `workers` | 1000 | 并发线程数，超过 fd 上限会自动下调 |
| `timeout` | 2.0 | TCP 连接超时（秒） |
| `attempts` | 1 | 每个 IP 的 TCP 尝试次数，网络抖动可调大 |
| `target` | 20000 | 目标存活条数，凑不够不报错 |
| `sample_step` | 2 | 每轮补采样时每 /24 增加几个 |
| `max_rounds` | 6 | 最多补采样几轮 |
| `outdir` | `output` | 输出目录 |
| `header` | `true` | `ipv4.txt` 首行是否写 `#` 注释头 |

可持续化四件套（默认全关，详见下一节）：

| 键 | 默认 | 说明 |
|---|---|---|
| `merge` | `false` | 增量累积：读已有 `ipv4.txt` 合并而非覆盖，池子只增不减 |
| `recheck` | `false` | 存量复核：先把已有 IP:端口 重探一遍，不通的剔掉再补新的 |
| `state_file` | `pool_state.json` | 断点续跑：记下探测过的 IP，下次跳过只采新的；留空 = 不启用 |
| `loop` | `1` | 循环次数，大于 1 = 一条命令反复跑 |
| `interval` | `0.0` | 循环间隔秒数，仅 `loop` 大于 1 有意义 |

推送键（只放在 `gh_config.json`）：

| 键 | 说明 |
|---|---|
| `auto_push` | 跑完自动提交并推送，`--no-push` 可临时关掉 |
| `github_token` | GitHub PAT，`ghp_` 或 `github_pat_` 开头；留空则读环境变量 `GITHUB_TOKEN` / `GH_TOKEN` |
| `github_username` | 仓库地址 `github.com/` 后面那一段，例 `github.com/RikkaSaiko1/cf-ip-pool` → `RikkaSaiko1` |
| `repo_name` | 仓库地址最后一段，例 → `cf-ip-pool` |
| `branch` | 推送目标分支，例 `main`；留空 = 当前分支 |

token 在 <https://github.com/settings/tokens> 生成：classic 勾 `repo`，fine-grained 给本仓库 **Contents: Read and write**。
token 只拼进本次 push 的临时 URL，不写 `.git/config`，日志里自动打码。`your_` 开头的值会被当占位符拦下。

### `gh_config.json` 样例

每个键前面那行 `_` 开头的就是注释：

```json
{
  "_comment": ["自己机器上跑的那份，已被 .gitignore 忽略，不会进仓库（token 就在这里）"],
  "_run": ["小样本 + 单并发逐条探测，几秒跑完"],

  "_sample": "每个 /24 随机取几个 IP，凑不够 target 时自动加码重采",
  "sample": 2,
  "_target": "目标存活条数，凑不够不报错",
  "target": 30,
  "_workers": "固定 1 = 单线程逐条探测，结果最稳、也最慢",
  "workers": 1,
  "_attempts": "每个 IP 的 TCP 尝试次数，1 = 只探一次",
  "attempts": 1,
  "_timeout": "TCP 连接超时（秒）",
  "timeout": 2.0,
  "_ports": ["端口池，每个 IP 随机分一个"],
  "ports": [443, 8443, 2053, 2083, 2087, 2096],
  "_outdir": "默认 out_local，不写仓库的 output/，免得覆盖 Actions 产物",
  "outdir": "out_local",
  "_header": ["ipv4.txt 首行是否写 # 注释头"],
  "header": true,

  "_sustain": ["可持续化四件套。自己机器上默认开 merge：池子越跑越大"],
  "_merge": ["读已有 ipv4.txt 合并而非覆盖，只补 target 减去存量的差额"],
  "merge": true,
  "_recheck": ["先把已有 IP:端口 重探一遍，不通的剔掉；默认 false，池子越大越慢"],
  "recheck": false,
  "_state_file": ["记下探测过的 IP，下次跳过只采新的；在 outdir 里，可随时删"],
  "state_file": "pool_state.json",
  "_loop": ["循环次数。1 = 跑一次就退"],
  "loop": 1,
  "_interval": ["循环间隔秒数，仅 loop 大于 1 有意义"],
  "interval": 0.0,

  "_push": ["推送设置。token 去 https://github.com/settings/tokens 生成"],
  "_auto_push": ["跑完自动提交并推送；临时关掉用 --no-push"],
  "auto_push": true,
  "_github_token": [
    "GitHub PAT。ghp_ 开头（classic）或 github_pat_ 开头（fine-grained）都认",
    "生成地址: https://github.com/settings/tokens",
    "权限: classic 勾 repo；fine-grained 给本仓库 Contents: Read and write",
    "留空则改用环境变量 GITHUB_TOKEN / GH_TOKEN（优先级更高）"
  ],
  "github_token": "your_github_personal_access_token_here",
  "_github_username": ["仓库地址里 github.com/ 后面那一段。例: github.com/RikkaSaiko1/cf-ip-pool → RikkaSaiko1"],
  "github_username": "your_github_username",
  "_repo_name": ["仓库地址最后一段，不带 .git。例: → cf-ip-pool"],
  "repo_name": "your_repo_name",
  "_branch": ["推送目标分支。例: main；留空 = 当前分支"],
  "branch": "main"
}
```

自己机器上默认就是小口径：**1 WIP + 1 次 TCP + 2 秒超时 + 6 个随机端口**，
`sample=2`、`target=30`，几秒跑完，结果格式和 Actions 产出完全一致。

## 定时任务

想让仓库自己每隔几小时刷一次，不用挂进程、也不用记着敲命令 —— 装成系统定时任务就行。
它到点执行的就是 `python main.py -y`，整套流程（预检推送配置 → 生成 ipv4.txt → 按 `auto_push` 推送）和手跑一模一样。

```
run.bat schedule install          # Windows
./run schedule install            # Linux / macOS / Termux
python main.py --schedule status  # 看装没装、跑的什么命令
python main.py --schedule uninstall
```

默认每 6 小时一次，跟 Actions 的刷新节奏对齐。可改：

| 参数 | 作用 |
|---|---|
| `--interval N` | 间隔分钟数，默认 `360`。支持 1-59 分钟、整小时（最多 23 小时）、整天数（最多 30 天） |
| `--at HH:MM` | 首次运行时刻，默认 `00:00` |
| `--no-push` | 定时跑的时候只生成，不推送 |
| `--name 名字` | 任务名，默认 `CF-IP-Pool`；想装多个就起不同名字 |
| `--dry-run` | 只打印会做什么，不动系统 |

`install` / `run` 后面多给的参数会原样进定时命令，例如
`run.bat schedule install --target 30 --no-push`。

这些也能写进 `gh_config.json`，就不用每次敲参数：

```json
"schedule": {
    "interval": 360,
    "at": "00:00",
    "name": "CF-IP-Pool",
    "no_push": false
}
```

| 键 | 作用 | 说明 |
|---|---|---|
| `interval` | 间隔分钟数 | 默认 `360`（每 6 小时，跟 Actions 对齐）。可用档位同 `--interval`：1-59 分钟、整小时（≤23）、整天数（≤30） |
| `at` | 首次运行时刻 | `HH:MM`，或只给分钟数（`30` = 00:30）。整天档里就是具体几点跑 |
| `name` | 任务名 | 默认 `CF-IP-Pool` |
| `no_push` | 定时跑时是否推送 | `true` = 只生成 `ipv4.txt` 不推送；`false` = 按 `auto_push` 推送 |

**命令行优先于配置文件**，所以想临时换个时间，加 `--interval 720 --at 06:30` 就覆盖掉配置文件里的值；不敲就按配置文件来。
注意 `gh_config.json` 顶层还有个 `interval`（那是循环模式的秒数），跟这里的「间隔分钟数」不是一回事，所以定时设置单独放在 `schedule` 对象里。

拿不准先 `--dry-run`，或者用 `schedule run` 手动跑一次那条命令看看结果：

```
python main.py --schedule run --target 2 --sample 1
```

各平台装的是什么：

| 平台 | 机制 | 注意 |
|---|---|---|
| Windows | 任务计划程序 `schtasks` | 普通用户就能装；上次运行结果在「任务计划程序」里看 |
| Linux | `crontab` | 装包 `sudo apt install cron` 并确认服务在跑 |
| macOS | `crontab` | 要给终端「完全磁盘访问权限」，否则可能不执行 |
| Termux / Android | `crontab` | `pkg install cronie` + `crond`，再 `termux-wake-lock` 防被杀 |

Linux / macOS / Termux 上只会动自己标记出来的那一段（`# >>> cf-ip-pool schedule >>>` 到
`# <<< cf-ip-pool schedule <<<`），你原有的 crontab 一行不碰。输出追加写到 `logs/schedule.log`（已被 gitignore）。

## 可持续化运行

默认行为一点没变：每次跑都重新生成整个文件，跑完就退。想换种跑法，打开下面四个键。

| 想要 | 打开 | 效果 |
|---|---|---|
| 池子越跑越大 | `merge` | 读已有 `ipv4.txt` 当底，只补 `target` 减去存量的差额，已有的 IP 不重复探 |
| 不把时间花在同一批地址上 | `state_file` | 探测过的 IP 记进 `pool_state.json`，下次直接跳过、只采新 IP |
| 池子里不留死 IP | `recheck` | 先把存量 IP:端口 重探一遍，不通的剔掉再补新的（配合 `merge`） |
| 一条命令挂那儿定时跑 | `loop` + `interval` | 反复跑，`interval` 秒一轮；`Ctrl+C` 会先把已探结果写盘再收尾 |

组合起来最实用的两种：

- **只增不减**：`merge: true` + `state_file` 留默认。每跑一次池子就大一点，探测过的地址不再碰。
- **又大又干净**：`merge: true` + `recheck: true`。顺带清掉失效 IP，代价是每轮都要把整个存量池重探一遍，池子越大越慢。

`state_file` 就在 `outdir` 里，删掉它 = 重置断点。`meta.json` 里多一个 `sustain` 段，记录这一轮用了哪几项、带进来多少存量、剔掉多少。

命令行可临时拧，不用改文件：`--merge` / `--no-merge` / `--recheck` / `--no-recheck` / `--state-file` / `--loop` / `--interval`。

## 推送链路自检

不确定 token / 仓库 / 分支填对没，先跑自检。它**不构建、不改仓库**，
按顺序查配置 → token → git 环境 → 网络 → 认证权限，退出码 0 = 通，1 = 有关键项失败。

```
run.bat check          # Windows
./run check            # Linux / macOS / Termux
```

只在真的有变化时才提交（没变化直接跳过），commit message 用北京时间。
报错会按原因给提示：token 失效 / 权限不足 / 仓库名不对 / 分支保护。

## 文件布局

```
main.py                Win / Linux / Termux 一键入口（推荐）
run / run.bat / run.ps1   启动脚本：手机 / Windows / Linux / macOS
config.json            Actions 生产配置（进仓库，不放 token）
gh_config.json         自己机器上一站配置（gitignore，token 在这里）
scripts/
  build_pool.py        核心：采样 + TCP 探测 + 写 ipv4.txt / meta.json
  check_push.py        推送链路自检
  schedule.py          定时任务：install / status / uninstall / run
output/
  ipv4.txt             Actions 产物：IP:端口 清单
  meta.json            构建元数据
out_local/            自己机器上的产物 + pool_state.json（断点续跑状态），被 gitignore
logs/                 定时任务跑的时候留下的日志，被 gitignore
.github/workflows/build.yml   Actions 定时任务（每 6 小时 + 手动触发）
```

## 原理

```
① 拉官方 ips-v4
② 拆 /24，每段随机取 N 个 IP（不够就逐轮加码）
③ 每个 IP 随机分配 1 个加密端口
④ 并发 TCP 探测，超时 2 秒（线程数按 fd 上限自动收敛）
⑤ 只把 TCP 通的 IP:端口 写进 ipv4.txt，凑满 target 即停
```

## 手动触发

Actions 页面 → Build CF IP Pool → Run workflow，可自定义 `sample` / `ports` / `workers` /
`target` / `timeout` / `no_header`，留空则用 `config.json` 的值。

## 直链

```
https://raw.githubusercontent.com/<用户名>/<仓库名>/main/output/ipv4.txt
```
