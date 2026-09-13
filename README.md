# git-ai-manager

管理 git-ai 提交归属数据的小工具：把分散的 PowerShell 脚本、手动 `git push refs/notes/ai`、
历史 note 修复和日志查看，合并成一个跨平台（Windows / macOS）的命令行 + 图形界面程序。

```
git-ai-manager.py        命令行核心
git-ai-manager-gui.py    图形界面（tkinter）
manager.example.json     配置模板
manager.json             当前生效的配置（本机生成，已被 .gitignore 忽略）
legacy-powershell/       旧的 PowerShell 脚本，留作兜底/参考
manager-logs/            运行日志（JSONL，自动生成）
```

## 环境要求

- Python 3.10+（界面需要 tkinter，官方安装包自带；Homebrew 用 `brew install python-tk`）
- 已安装 git-ai（本工具会在 PATH、`$GIT_AI_EXE`、`<本目录>/bin/git-ai[.exe]` 里找它）
- 只用标准库，不需要 pip 安装任何东西

## 快速开始

```bash
# 界面（推荐）
python git-ai-manager-gui.py          # Windows
python3 git-ai-manager-gui.py         # macOS

# 命令行
python git-ai-manager.py init                     # 建立目录和初始配置
python git-ai-manager.py config add-root D:/Projects
python git-ai-manager.py status                   # 看看各仓库现状
python git-ai-manager.py install                  # 一键装钩子
python git-ai-manager.py sync-notes --dry-run     # 先预览再同步
```

界面里对应的操作：**设置**（目录/深度/工具模型）→ **仓库**（扫描 → 一键安装钩子）→
**同步**（选项目 → 同步）→ **记录**（**先选项目再加载**，避免一次拉全部仓库）→ **日志**。

### 关于「记录」页

下拉框先选项目，选中即加载，只查那一个仓库，默认最多 20 条；想在单个仓库里多看几条，
把右上角的数字调大即可。第一项 `（全部仓库 - 较慢）` 才会遍历所有仓库（28 个仓库时约
500+ 条记录，会明显变慢）。点击「刷新列表」会重新扫描仓库。

## 它是怎么工作的

```
git commit
  └─ pre-commit  : 对暂存文件执行 git-ai checkpoint mock_ai，等 daemon 落盘
  └─ post-commit : 写入 commit_sha → 仓库路径 映射；并把「上一条提交」的 note 归一化
git push          （只推分支，不推 notes）
python git-ai-manager.py sync-notes
  └─ 先归一化本地 note，再 fetch / merge -s ours / push refs/notes/ai
```

归一化 = 把 `"tool": "mock_ai"` 改成配置里的工具名/模型名，把本地用户自己产生的
human note 补成真实 session；**别人的 human note 一律不动**。

### 提交之后，note 多久变成配置的工具/模型

git-ai 的 daemon **只在 git 进程退出之后**才发布这条提交的 note（实测约 0.6 秒），所以提交钩子里
永远看不到自己这条 note。做法分三层：

1. **post-commit 立刻归一化上一条**：那时它必定已经落盘，零等待；
2. **同时甩一个后台助手**（`hook normalize-later`）去等当前这条 note，一出现就改写 ——
   提交本身完全不等待，实测提交后 **约 1~2 秒** note 就变成配置的 tool/model；
3. **兜底**：机器关机 / 助手没跑成也不怕 —— `sync-notes` 推送前会再补一次，`normalize` 可以批量修历史。

后台助手最多等多久由 `defaults.noteWaitSeconds` 控制（默认 30 秒）。它只是一个普通进程，
写完一条日志就退出；用 `logs` 命令能看到成对的 `post-commit` → `notes-updated` 记录。

## 配置（manager.json）

```jsonc
{
  "projectsRoots": ["D:/Projects"],     // 源码目录，可多个
  "defaults": {
    "tool": "codex",                    // 默认工具名
    "model": "unknown",                 // 默认模型名
    "repoDepth": 4,                     // 在源码目录下最多往下找几层仓库
    "convertLocalHuman": true,          // 是否把本地 human note 转成 session
    "normalizeOnCommit": true,          // 提交后自动归一化上一条
    "syncNotes": true,
    "remote": "origin"
  },
  "repos": {                            // 单个仓库覆盖默认工具/模型
    "D:/Projects/SomeMacRepo": { "tool": "claude", "model": "claude-opus-4-8" }
  }
}
```

`repoDepth` 默认 4：有的项目 `.git` 在第一层，有的在更深的目录（例如
`D:\Projects\everything-claude-code\everything-claude-code`），扫描会自动跳过
`node_modules / target / build / dist / .venv / Pods / .gradle` 等目录，
并且遇到仓库就不再往里钻。

## 路径约定

代码里没有写死任何目录，全部运行时解析：

| 项 | 解析顺序 |
|---|---|
| git-ai 家目录（配置、日志、映射） | `$GIT_AI_HOME` → 本脚本所在目录（打包后 = exe 所在目录） |
| git-ai 可执行文件 | `$GIT_AI_EXE` → PATH → `<工具目录>/bin/` → `~/.git-ai/bin/` → 其它常见位置（见下） |
| 钩子使用的 Python | 仅源码方式：`--python` → `$GIT_AI_PYTHON` → 本目录解释器 → 当前解释器 → PATH |
| 项目根目录 | `--root` → `$GIT_AI_PROJECT_ROOTS` → `manager.json` |
| 工具/模型 | `repos[仓库]` → `defaults` |

### git-ai 到底在哪里找

`git-ai` 不是本工具自带的，所以要能在别人机器上找到它。查找顺序（`find_git_ai_with_origin()`）：

```
1. 环境变量 GIT_AI_EXE
2. PATH
3. <工具目录>/bin/git-ai[.exe]        ← 便携包：把 git-ai.exe 放这里，随包一起发
4. ~/.git-ai/bin/git-ai[.exe]         ← git-ai 默认安装位置
5. ~/.local/bin、~/.cargo/bin、~/bin
6. Windows: %LOCALAPPDATA%\{git-ai,Programs/git-ai,git-ai/bin}
            %APPDATA%\{...}、%ProgramFiles%\{git-ai,Programs/git-ai,git-ai/bin}
   macOS/Linux: /usr/local/bin、/opt/homebrew/bin、/usr/bin、/opt/local/bin
```

第 4 条是关键兜底：**就算 PATH 里没有、工具也不在 `~/.git-ai` 下**（现在工具在 `D:\Projects\git-ai-manager`），
只要 git-ai 装在默认位置就能找到 —— 从资源管理器双击启动、Explorer 的 PATH 还是旧的这种情况也不会翻车。

「设置」页有 **自动检测** 按钮：找到会显示**来源**（PATH / ~/.git-ai/bin / …）和版本；找不到会把上面每一处
都列出来，并给出 4 种解决办法。`doctor` 命令里也会打印来源。

### 让 hook 的 Python 固定在本目录（推荐）

钩子里写的是解释器的绝对路径，如果它指向系统 Python，换机器或系统 Python 升级后就可能失效。
所以在工具目录里放一份解释器，会被**自动优先使用**，按以下顺序查找：

```
.venv/Scripts/python.exe   .venv/bin/python3   .venv/bin/python
venv/...                   python/python.exe   python/bin/python3
runtime/python.exe         runtime/bin/python3 ...
```

界面上：**设置 → hook 使用的 Python → 「建 .venv」**，一键在本目录创建虚拟环境并把
钩子指向它（字段保持为空 = 自动探测，换目录也能用）。命令行等价于：

```bash
python -m venv .venv          # 之后 install 就会用 .venv 里的解释器
```

说明两点：

- 虚拟环境仍然是**引用**创建它的那个 Python 安装，如果那个安装被卸载，本工具会自动
  探测失败并回退到其他解释器（不会把钩子写坏）。
- 想要真正便携，把 Windows embeddable 包解压到 `python\` 目录（或 macOS 上放一份
  `python/bin/python3`），本工具同样会自动采用；只跑钩子不需要 tkinter。

想把配置和日志放回 `~/.git-ai`（源码留在这个文件夹）就设一个环境变量：

```
GIT_AI_HOME=%USERPROFILE%\.git-ai        # Windows
export GIT_AI_HOME=~/.git-ai             # macOS
```

## 常用命令

```bash
python git-ai-manager.py status [--json] [--depth N]     # 各仓库概览
python git-ai-manager.py doctor --repo <路径>            # 诊断「为什么没算成 AI」
python git-ai-manager.py records --limit 20              # AI 提交记录
python git-ai-manager.py logs --tail 50                  # 工具运行日志
python git-ai-manager.py install [--dry-run]             # 安装钩子
python git-ai-manager.py uninstall [--restore-legacy]    # 卸载（保留第三方钩子内容）
python git-ai-manager.py sync-notes [--dry-run] [--no-normalize] [--no-push]
python git-ai-manager.py refresh-notes --repo <路径> --apply
python git-ai-manager.py normalize --apply [--include-human]
python git-ai-manager.py convert-human --apply [--email X --subject-pattern REGEX]
python git-ai-manager.py repair-empty --repo <路径> --scan --apply   # 重建无行归属的 note
python git-ai-manager.py repair-empty --repo <路径> --scan-missing --count-only            # 历史缺多少条
python git-ai-manager.py repair-empty --repo <路径> --scan-missing [--since DATE] [--branch REV] [--limit N] --apply
                                                          # 给「装 git-ai 之前」的提交补记录
```

批量操作默认 **dry-run**，确认没问题再加 `--apply`。
`sync-notes --dry-run` 只读远端、不写不推（但会做一次 `git fetch`，并在本地留一个
`refs/git-ai-manager/remote-notes-ai` 快照，用来判断两端是否一致）。
设置 `GIT_AI_MANAGER_DEBUG=1` 可以打印每条 git / git-ai 命令及耗时。
设置 `GIT_AI_AUTO_AI=0` 可以临时让 pre-commit 不做 AI 归属。

## 打包成 exe 发给别人

### 构建（在你自己机器上做一次）

```bash
# Windows
build-exe.cmd                  # 需要 Python 3.10+ 和 pip install pyinstaller

# macOS
./build-macos.command
```

产物：

```
Windows : dist\git-ai-manager\git-ai-manager.exe   ← 连同同级 _internal\ 一起发
macOS   : dist/git-ai-manager.app                  ← 整个 .app 发，右键→打开绕过未签名拦截
```

Windows 已经打包好一份：`dist\git-ai-manager-windows-x64.zip`（约 11 MB）。

### 对方怎么用

解压后（同一个 exe 三种用法）：

```
git-ai-manager.exe                 # 双击 = 图形界面
git-ai-manager.exe --selftest      # 自检：环境 + 扫描，确认打包可用
git-ai-manager.exe status --json   # 带参数 = 命令行
```

**目标机器不需要装 Python**，但需要装 git-ai（本工具调用它）。然后：双击 → 设置里填源码目录和深度 → 仓库页「一键安装钩子」即可。

### 注意事项

- 钩子里写的是 exe 的**绝对路径**：先放好位置再点安装；之后移动了文件夹，重新点一次安装即可。
- 配置/日志/映射都在 exe 旁边（`manager.json`、`manager-logs\`、`notes-repo-map\`），所以不同人、不同目录各自独立。
- 未签名的 exe 首次运行可能被 SmartScreen 拦（更多信息 → 仍要运行）；macOS 上是右键 → 打开。
- 打包后「设置」页**不再显示「hook 使用的 Python」**，而是一行提示「不需要」—— 因为钩子直接调用 exe。
  那个选项只在**源码方式**下有意义：钩子里写的是 `"python.exe" "git-ai-manager.py"`，需要一个稳定的解释器路径
  （留空则自动探测：优先本目录的 `.venv`，其次本机 Python）。只用 exe 的话，源码用的 `.venv\` 可以删掉。
- 重新打包时如果 exe 正在运行（或刚跑完还占着 `manager-logs\` 里的日志），PyInstaller 会报
  `WinError 32 另一个程序正在使用此文件`：关掉它再打包即可。
- 如果报的是 `WinError 5 拒绝访问 ... VCRUNTIME140.dll`：那是**杀软/企业 EDR 拦住了"删除"**（文件既没被占用也不是只读）。
  改名不受影响，所以把整个 `dist` 改名成 `dist-old` 再打包即可，旧目录稍后手动删。
- 打包期间如果正好有提交在跑（钩子会启动 exe），也会占用 `dist` 里的 DLL；等几秒再打即可。
- **提交钩子的启动开销实测 ~270 ms**（源码方式 ~230 ms，几乎无差别）。

### 改代码后重新打包的要点

1. **入口是 `entry.py`**：带参数只加载命令行核心，不带参数才加载 tkinter —— 否则每次 commit 都要为图形界面付启动开销。
2. 核心与界面是 `--add-data` 打进去的数据文件，PyInstaller 看不到它们的 import，所以 `entry.py` 里显式列了要用的标准库，tkinter 用 `--hidden-import` 引入。
3. windowed（无控制台）构建下 Python 的 stdout/stderr 默认被丢弃，代码里做了三件事：接管继承的 std 句柄（`exe status > out.txt` 可用）、实在没有控制台就把输出写进 `manager-logs\`、统一成 UTF-8。
4. 调用子进程统一加 `CREATE_NO_WINDOW`：无控制台进程每次调 `git` 都会新分配一个控制台，实测 ~200 ms/次。

## macOS

代码本身就是跨平台的，而且 **macOS 没有 Windows 那种「控制台 / 窗口」子系统之分，
所以一个 `.app` 同时就是界面和命令行** —— 不需要像 Windows 那样出两个产物。

### 第 0 步：先装 git-ai 本体

本工具只是"管理器"，真正记录 AI 归属的是 **git-ai 本体**；Mac 上也必须先有它。

```bash
# 1) 确认 git 版本够新（git-ai 会检查；太旧时它会直接提示）
git --version
#    太旧就升级（git-ai 自己的提示里也是这么写的）：
brew install git

# 2) 官方安装脚本（macOS / Linux 通用），默认装到 ~/.git-ai/bin/git-ai
curl -fsSL https://usegitai.com/install.sh | bash

# 3) 让 shell 能找到它（写进 ~/.zshrc 或 ~/.bash_profile；装完脚本一般也会提示）
echo 'export PATH="$HOME/.git-ai/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 4) 验证：应输出 1.7.4 这类版本号
git-ai version
```

> 也可以让 git-ai 自己写 PATH：`git-ai install --env`（顺带会装它自己那套钩子，
> 本工具不需要那套，用上面的 `export PATH` 更干净）。
> 如果公司内网有自己的分发渠道，拿到 `git-ai` 二进制放到 `~/.git-ai/bin/` 即可。

### 构建（在 Mac 上，或放进 CI）

```bash
chmod +x build-macos.command      # 从 Windows 提交过去的话可执行位可能丢了（脚本自己也会再 chmod 一次）
./build-macos.command
```

产物：`dist/git-ai-manager.app`。

### 用法

```bash
open dist/git-ai-manager.app                                   # 打开界面
./dist/git-ai-manager.app/Contents/MacOS/git-ai-manager status --json
                                                               # 命令行（会等待、能抓输出）
./dist/git-ai-manager.app/Contents/MacOS/git-ai-manager --selftest   # 自检
```

首次双击会被 Gatekeeper 拦（未签名）：**右键 → 打开**即可；命令行方式不受影响。

### 到手后要做的三步

```bash
APP=./dist/git-ai-manager.app/Contents/MacOS/git-ai-manager
"$APP" --selftest                       # 1. 自检：环境 / git-ai / 扫描
"$APP" install --depth 4                # 2. 装钩子（钩子里写的是绝对路径，所以先放好位置）
"$APP" status                           # 3. 确认
```

配置与日志写在 **.app 所在目录**（不会写进 .app 包内部）；`.app` 移动位置后请重新执行一次 `install`。

### git-ai 到底在哪里找（macOS）

本工具按这个顺序找 `git-ai`（`find_git_ai_with_origin()`）：

```
1. 环境变量 GIT_AI_EXE
2. PATH
3. <工具目录>/bin/git-ai            ← 便携包：把 git-ai 二进制放这里随 .app 一起发
4. ~/.git-ai/bin/git-ai             ← 官方脚本的默认安装位置
5. ~/.local/bin、~/.cargo/bin、~/bin
6. /usr/local/bin、/opt/homebrew/bin、/usr/bin、/opt/local/bin
```

**第 4 条很关键**：从 Finder / Dock 启动的 `.app` **不会继承你 shell 里的 PATH**
（`.zshrc` 里的 `export PATH` 只对终端生效）—— 这时靠第 4 条照样能找到 git-ai。
所以按上面的官方脚本安装（装到 `~/.git-ai/bin`）是最省事的做法。

找不到时的三种办法：

```bash
# a) 临时指定（或写进 shell rc / launchd，取决于你怎么启动）
export GIT_AI_EXE="$HOME/.git-ai/bin/git-ai"
open dist/git-ai-manager.app

# b) 用界面：设置 → git-ai 可执行文件 → 「自动检测」（会列出每一处找过的位置）
#    或「浏览…」手动指定

# c) 便携包：把 git-ai 放进 .app 所在目录的 bin/ 里
mkdir -p bin && cp ~/.git-ai/bin/git-ai bin/     # 与 .app 同级
```

### 放进 GitHub Actions（macOS runner）

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: '3.12'
- run: python3 -c "import tkinter" || brew install python-tk
- run: python3 -m pip install --upgrade pyinstaller
- run: chmod +x build-macos.command && ./build-macos.command
- uses: actions/upload-artifact@v4
  with:
    name: git-ai-manager-macos
    path: dist/git-ai-manager.app
```

说明：
- `actions/setup-python` 装的 python.org 版通常自带 tkinter；若报缺 tkinter 就照上面那行 `brew install python-tk`，
  并把构建脚本里的 `PY=python3` 改成 `/opt/homebrew/bin/python3`。
- macOS 产物**未签名/未公证**，同事首次打开需要右键 → 打开（或在「系统设置 → 隐私与安全性」里放行）。
- 如果你要同时出 Windows 和 macOS，两边产物共用同一套源码与 `manager.json` 结构，互不干扰。

## 别人机器上「全是 human add」怎么查

`git-ai stats <sha> --json` 里 `human_additions` 占满、`ai_additions` 为 0，说明 note 存在但被
明确写成了 human —— 也就是**提交时没有产生 AI checkpoint**。归属由 note 内容决定（AI 行是
`s_xxx::t_xxx` 会话，human 行是 `h_xxx`），跟 `git config user.email` 无关；邮箱只在事后
「把 human note 转成 AI」时用来判断是否本机用户。

用一条命令定位：

```bash
python git-ai-manager.py doctor --repo <仓库路径>          # 界面上是「仓库 → 诊断」
```

它会依次检查：git-ai 是否可用（含「非规范大小写会退化成纯 git 透传」这个坑）、daemon 是否
运行、有没有会被 git-ai 跳过的环境变量（`CI`/`GIT_AI_SKIP_ALL_HOOKS`/`GIT_AI_AUTO_AI=0` 等）、
`core.hooksPath` 是否把 `.git/hooks` 顶掉了、pre/post-commit 是否存在且可执行，以及最近
10 条提交的归属分布，最后给出结论。

按出现概率排序的原因：

1. **没装钩子** —— `.git/hooks` 不进版本库，`git clone` 下来就是空的（最常见）
2. `core.hooksPath` 被 husky 等工具设成了别的目录，`.git/hooks` 被忽略
3. daemon 没运行，或环境变量把 git-ai 的钩子关掉了
4. 提交时用了 `--no-verify`/`-n`，或 IDE 绕过了钩子
5. checkpoint 写了但没被 daemon 认领，git-ai 把提交当 untraced 补了一条 human note
6. git-ai 版本过老、没有 `mock_ai` preset

历史数据可以批量补救（只动本机用户自己产生的 human note）：

```bash
python git-ai-manager.py convert-human --email <对方邮箱> --apply
python git-ai-manager.py normalize --include-human --apply
```

补救完 `git-ai stats` 就会把那些行算成 `ai_additions`。

## 关于 git-ai 1.7.x 的三个坑（实测结论）

1. **`post_notes_updated` 钩子基本不会触发。** 它由 git-ai 的 “git core hooks” 代码路径触发，
   而该特性已 sunset；只有通过 `core.hooksPath` 安装了 git-ai 自己的钩子时才会跑到。
   所以 `notes-updated-ai.ps1` 这类钩子脚本实际上是死代码 —— 本工具改为在提交钩子和
   `sync-notes` 里自己完成归一化。
2. **daemon 在 git 进程退出后才发布 note**，所以提交钩子看不到自己这条（见上文）。
3. **用非规范大小写启动 git-ai 会退化成纯 git 透传。** Windows 上 `shutil.which("git-ai")`
   返回 `git-ai.EXE`（取自 PATHEXT），此时 `git-ai checkpoint ...` 实际执行的是
   `git checkpoint`，**AI 归属会被静默关闭**。本工具用 `realpath` 规范化路径并做了版本探测告警。

## 故障排查

- **提交后 note 还是 `mock_ai`**：下一次提交会自动修上一条；想立刻处理用
  `normalize --apply` 或 `sync-notes`（推送前会归一化）。
- **`sync-notes` 报 fetch-failed**：远端不可达或没有权限，先手动 `git fetch` 验证。
- **钩子没生效**：`git config --get core.hooksPath`，如果被设置成别的目录，`.git/hooks`
  下的钩子会被忽略；本工具的 `status` 会显示每个仓库的钩子状态（`mgr` = 本工具管理）。
- **换机器**：把本文件夹拷过去，`python git-ai-manager.py init`，在界面里重新指定
  源码目录和 Python 路径，再跑一次 `install`。
