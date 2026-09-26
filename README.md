# 每周作业提醒（北化在线）

自动抓取北京化工大学「北化在线」THEOL 平台的待提交作业截止时间，
每周日汇总未来 7 天的作业并推送。

## 快速开始

```powershell
# 1) 首次使用 / 登录态过期时：双击 login.bat —— 自动填充账号密码 + 自动点「登录」，无需你动手
#    （若平台弹滑块，窗口会留着让你拖一下；脚本检测到登录成功就自动保存会话并退出）
# 2) 日常操作（检索 / 添加 / 管理 / 提前发布）：双击 console.bat
# 3) 每周日 09:00 自动跑抓取 + 汇总 + 推送，一般无需手动
```

### 首次部署（换机器 / 刚 clone 下来必做）

1. 装依赖：`pip install playwright`（**不要** `playwright install`，本项目复用系统 Edge，不下载 Chromium）
2. 建配置：`copy data\config.example.json data\config.json`，再按里面注释填你的推送渠道
   （`data/config.json` 已被 .gitignore 忽略 —— webhook 的 `key=` 就是群发口令，不能进仓库）
3. 登录：`login.bat` 会打开你的 Edge 并自动填充账号密码；
   **前提是 Edge 里已保存北化在线的账号密码**（用哪个 Edge 用户无所谓，登录页能自动填充即可）
4. 路径：脚本用 `%USERPROFILE%` / `Path.home()` 定位，不写死用户名，换机器直接跑。
   若你的 Python 不在 `%USERPROFILE%\.workbuddy\binaries\python\envs\default\`，改 `data\console.ps1` 第 4 行即可。

> 所有脚本必须用 WorkBuddy 管理的 Python：
> `%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
> 系统 python 没装 playwright。
> **双击 `console.bat` 即可打开统一控制台**（检索 → 添加/管理 → 提前发布）；定时任务每周日 09:00 自动跑完整流程。

## 统一控制台（推荐，一个界面搞定全部）

`console.bat` 双击打开一个 Windows 窗体，把四件事合到一个界面里：

1. **检索北化在线作业** —— 点「检索北化在线」实时抓取平台待交作业（复用已有登录态）。
2. **手动添加作业** —— 窗体上方填 科目 / 内容 / 截止 / 备注，点「添加」。
3. **管理作业** —— 列表里选中「手动」行，可 标记完成 / 编辑 / 删除 / 清理过期，双击行也能编辑。
4. **提前发布** —— 点「提前发布」立即把「未来 N 天」（默认 7，可改）的作业汇总推送到 `config.json` 配的渠道；
   想先看内容再发，先点「预览」。
5. **查看作业内容** —— 列表里选中「平台」行，点「查看作业内容」把作业正文（题目/要求）抓下来显示在下方文本框；
   点「浏览器打开」进平台页面：**已提交 → 「查看结果」页，未提交 → 「提交作业」页**。

> 列表里「平台」行来自北化在线、只读（可查看内容）；「手动」行来自 `manual.json`、可管理。
> 抓取需要有效登录态；过期时点「检索北化在线」报错，双击 `login.bat` 再试。
> 默认只显示「未来 N 天」的作业。**想翻已过期的旧作业（比如回看已经交了的）**：
> 勾选顶部「含全部平台作业」再点「检索北化在线」，列表会列出该学期全部作业。

## 目录结构

```
每周作业提醒/
├── README.md           # 本文件
├── console.bat         # 双击 → 统一控制台（推荐入口）
├── login.bat           # 双击 → 自动登录（Edge 自动填充 + 自动点登录，卡住转人工）
└── data/               # 全部脚本与数据
    ├── *.py            # 所有 Python 脚本
    ├── *.ps1           # 全部 PowerShell 窗体
    ├── config.json     # 推送渠道配置
    ├── manual.json     # 手动录入的作业
    ├── hw.json         # 最近一次抓取的原始数据
    ├── storage_state.json   # 登录会话（Cookie + LocalStorage）
    ├── last_message.txt     # 最近一次生成的推送文本
    └── debug/          # 探查脚本与调试截图，正常运行不需要
```

> 所有 Python 脚本用 `__file__` 定位 ROOT，所以可以从任意目录调，绝对路径也 OK：
> `python "D:\项目\每周作业提醒\data\weekly.py" --days 7`

## 自动登录（login.bat）

登录态失效时双击 `login.bat` 即可，**正常情况下不需要你输入任何东西**：

1. 用持久 Edge profile（`%USERPROFILE%\.workbuddy\profiles\buct-course-edge`，存过北化账号密码的那份）打开 `sso_login.jsp`。
2. 若会话还有效 → 直接保存 `storage_state.json` 并退出。
3. 否则落到登录页：脚本在 iframe `login-normal.html` 里点一下用户名触发 **Edge 自动填充**，
   再点密码框、点「登录」按钮（若自动填充没灌进 DOM，还会派发 input/change 事件让 Vue 同步值）。
4. 登录成功 → 自动保存会话 + 截图 `data/login_result.png` + 关窗口，退出码 0。

**唯一需要你动手的情况**：平台弹验证码（登录页确实加载了腾讯验证码 SDK `turing.captcha.gtimg.com`）。
这时脚本**不关窗口**，会打印提示并继续轮询 300 秒 —— 你在那个窗口里拖一下滑块就行，
一登录成功它立刻保存会话并退出。登录时若门户弹「是否添加常用设备？」，脚本会自动选「是」，
把本机记为常用设备，之后再登录一般就不弹验证码了。

```powershell
# 等价命令行（解释器路径见上）
python data\login.py             # 自动登录优先，卡住转人工（login.bat 用的就是它）
python data\login.py --manual    # 跳过自动填充，直接人工登录（老行为）
python data\login.py --no-wait   # 自动提交后只等 45 秒，不转人工（给定时任务用）
python data\login.py --check     # 只检查 storage_state.json 是否仍有效
```

> 密码不落盘：脚本只保存登录后的 cookie / storage_state，账号密码仍由 Edge 密码管理器保管。
> **改过北化密码后**：Edge 里存的是旧密码，自动登录会失败 —— 此时按提示手动登录一次，
> 浏览器问「是否保存密码」选「保存」，之后就恢复全自动。

## 在终端里跑

```powershell
cd "D:\项目\每周作业提醒\data"
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" notify.py --self-test
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" weekly.py --days 7
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `data/login.py` | 自动登录：打开持久 Edge profile（存过密码的那份）→ 自动填充 → 自动点「登录」→ 保存 `storage_state.json`；卡住则同窗口转人工 |
| `data/scrape.py` | 登录后抓课程作业列表（每条带 `hwtid` 与 内容/提交/结果 三个链接）；加 `--content <hwtid>` 抓单条作业正文 |
| `data/weekly.py` | 端到端入口：抓取 → 按截止时间筛选 → 生成汇总文本 → 调 `notify.py` 推送 |
| `data/notify.py` | 推送层，多渠道可插拔（见下） |
| `data/config.json` | 推送渠道配置 |
| `data/wechat_send.py` | PC 微信 UI 自动化（**已验证在微信 4.x 上不可用**，保留备用） |
| `data/hw.json` / `data/last_message.txt` | 最近一次抓取的原始数据 / 生成的汇总文本 |
| `data/manual.py` | 手动补录作业（老师发在微信群/其他企业微信群、平台抓不到的） |
| `data/manual.json` | 手动录入的作业清单，汇总时与平台抓取结果合并 |
| `data/hub.py` | 统一控制台后端：给 `console.ps1` 提供「列表 / 发布 / 作业正文」数据，复用 `weekly.py` 与 `notify.py` 逻辑 |
| `data/console.ps1` | `console.bat` 调用的 Windows Forms 单窗口界面（无需修改） |
| `data/debug/` | 探查脚本与调试截图，正常运行不需要 |

## 手动补录（老师发在微信群 / 企业微信群的作业）

平台抓不到的作业，手动补一条即可，汇总时会自动合并、带上 📌 标记和备注。

```powershell
# 方式一（推荐）：双击 console.bat → 在「手动添加作业」区填 科目名 / 作业内容 / 截止时间 / 备注，点「添加」
# 方式二：命令行（适合脚本/批量）
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py add `
    --course "高等数学" --title "习题 3.1" --deadline "2026-09-17 23:59" --note "老师发在企业微信群"
```

> 说明：手动录入的图形表单已整合进 `console.bat` 的统一控制台，双击后直接填即可，无需单独入口。

管理命令（老师取消作业、改截止时间、交了标记完成，都走这里）：

```powershell
# 推荐：双击 console.bat → 列表选中「手动」行 → 标记完成 / 删除 / 编辑 / 清理过期
# 命令行等价：
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py list      # 查看全部
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py done 3     # 标记 id=3 已完成（不再提醒）
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py rm 3       # 删除 id=3
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py edit 3 --deadline "2026-09-20 23:59" --title "改后的标题"  # 改某条
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\manual.py clean     # 清掉 7 天前已过期的条目
```

> **过期作业会自动掉出汇总**：手动条目和平台作业一样，只保留「现在 ~ 未来 N 天」窗口内的。
> 截止时间过了就不在周报里出现；要彻底移除用 `done`/`rm`/`clean`。
> 只有「截止时间填得不规范、解析不出来」的手动条目才会永久保留（标「未标注」），避免漏掉老师口头布置的活。

截止时间写法（支持多种格式，只给日期默认当天 23:59）：

| 输入 | 解析为 |
| --- | --- |
| `2026-09-17 23:59` | 2026-09-17 23:59 |
| `2026-09-17` | 2026-09-17 23:59 |
| `2026/9/17 23:59` | 2026-09-17 23:59 |
| `9-17 23:59` / `9/17 23:59` / `9.10 23:59` / `9月10日 23:59` | 当年 9-17 23:59（跨年算明年） |
| `9-17` / `9/17` / `9.10` / `9月10日` | 当年 9-17 23:59 |

## 查看作业内容（平台作业正文）

每门课的作业列表页 `hwtask.jsp` 里，同一行挂了三个链接，共享同一个 `hwtid`：

| 位置 | 链接 | 什么时候有 |
| --- | --- | --- |
| 标题 | `hwtask.view.jsp?hwtid=XXX` | 恒有 —— **作业内容页**（题目/要求/附件） |
| 提交作业列 | `write.jsp?hwtid=XXX` | 未提交时才有 |
| 查看结果列 | `taskanswer.jsp?hwtid=XXX` | 已提交时才有 |

所以「查作业内容」不需要管已交还是没交，拿 `hwtid` 直接开 `hwtask.view.jsp` 就行；
而「浏览器打开」会按状态跳到对应的那一列（已交 = 查看结果，未交 = 提交作业）。

正文不是渲染在 iframe 里的，而是同步放在页面一个隐藏域：

```html
<input type="hidden" name="107_content" id="107_content" value="&lt;ol&gt;…实体编码的作业正文…&lt;/ol&gt;">
```

所以直接正则取 `name="\d+_content"` 的 `value` 再 HTML 解码即可，不必进 ueditor iframe 抓。

```powershell
# 命令行也能单独取某条作业的正文（hwtid 从列表的 Tag 里来）
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\hub.py content --hwtid 81878
```

> 「浏览器打开」走的是系统默认浏览器，它用的是**浏览器自己的登录态**，
> 和 `storage_state.json`（给自动化脚本用的）是两套。如果浏览器里没登录北化在线，
> 会先跳统一认证页，登录一次即可。

## 推送渠道

改 `data/config.json` 的 `channel` 字段，可选：

| channel | 说明 | 需要配置 |
| --- | --- | --- |
| `clipboard`（默认） | 复制到剪贴板 + 弹 Windows 通知，你 Ctrl+V 到任意微信会话。零配置、零风险 | 无需 |
| `pushplus` | 微信里收到公众号消息。pushplus.plus 扫码关注拿 token | 填 `pushplus_token` |
| `wecom` | 企业微信群机器人 | 填 `wecom_webhook` |
| `wechat` | PC 微信客户端 UI 自动化 | 填 `wechat_target` |
| `none` | 只落盘，不推送 | 无需 |

自检配置是否就绪：

```powershell
& "$env:USERPROFILE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" data\notify.py --self-test
```

## 定时

已配置自动化任务「北化在线作业周报」：**每周日 09:00** 自动跑 `data/weekly.py --days 7`。
改时间或暂停，在 WorkBuddy 的自动化面板里操作。

## 已知坑

- **登录态会过期。** CAS 的 `CASTGC` / 门户的 `TGC` 是长效票据但不保证长期有效。过期表现是
  `scrape.py` 报「未登录」，双击 `login.bat` 自动登录即可，密码不落盘。
- **登录表单在跨域 iframe 里。** `portal.buct.edu.cn/` 首页只是壳，真正的账号密码表单在
  `portal.buct.edu.cn/normal/login-normal.html`（字段 `input[name=username]` / `input[name=password]`，
  按钮文案「登录」），脚本必须进 frame 操作。
- **Edge 自动填充不会立刻灌进 DOM。** 必须先用 Playwright 点一下用户名框触发，实测 1~3 秒才出现值；
  刚 `goto` 完就读 `input_value` 必然是空的。这也是当初误判「自动登录不可行」的原因之一。
- **别改回「有死代码的 main」**：登录逻辑曾有一段永远执行不到的分支（`if args.check:` 后面直接
  `return`，紧跟其后的 `if args.auto:` 成了死代码），导致「自动填充成功但从来没点过登录按钮」。
  改动 login.py 后必须实跑验证，不能只看返回 Success。
- **登录页加载了腾讯验证码 SDK**（`turing.captcha.gtimg.com/1/tcaptcha-frame.*.js`）和阿里无痕风控
  （`g.alicdn.com/AWSC/et/*`）。实测本机 IP + 常用设备下点登录**不弹**验证码；万一弹了，
  脚本会保留窗口并轮询，人工拖一下滑块即可，不要试图自动过滑块。
- **入口必须是 SSO，不是首页。** `https://course.buct.edu.cn/meol` 是未登录首页；
  正确入口是 `https://course.buct.edu.cn/meol/homepage/common/sso_login.jsp`，
  它会拿 CAS 票据换平台会话，免密直达 `personal.do`。
- **课程作业的链接每课不同**（`course_column_preview_transfer.jsp?columnId=xxxxxx`），
  必须先展开课程页左侧「课程活动」菜单再取链接，不能写死。
- **PC 微信 4.x 是 Qt 渲染**，uiautomation 取不到控件树，Ctrl+F 搜索也常被吞，
  UI 自动化这条路实测走不通，别再往里投入时间。
- **双击 .bat 时控制台中文会乱码**。所有 .bat 第一行 `chcp 65001` 已设，且 Python 子进程
  强制 UTF-8 输出。如果你自己在终端里跑脚本也遇到乱码，先执行 `chcp 65001` 再 `set PYTHONIOENCODING=utf-8`。