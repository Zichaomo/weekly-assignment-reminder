# 阶段 A-B 实施验收（2026-10-26，离线隔离）

> 结论边界：本轮完成的是**本地隔离开发与离线验收**，不是在真实北化在线账号上的线上验收。
> 未连接学校平台、未登录、未发送消息、未注册计划任务、未提交或推送 git。不能据此声明
> “整体线上验收通过”。

## 本轮真实阻断诊断（2026-10-26，暂停点）

用户要求到节点保存，现暂停。新会话能打开 `/meol/personal.do`，包含“在线总时长”“登录时间”；login.is_logged_in=True，但scrape.page_is_login=True、page_authenticated=False。个人页保留“统一身份认证登录”入口文字，body子串匹配误拒绝已认证页面。真实列表/正文未成功抓到；下次修此判断并补脱敏回归，复用已更新会话，不重复要求用户重登。

## 真实验收续接（2026-10-26）

- 用户已明确授权访问真实平台验收，并允许发送测试群。
- 新增固定旧v2 DDL迁移回归2项，涵盖重复迁移、资产路径/正文/人工视图保留、身份切换失效与单课覆盖；DeepAstra run `20261026T063430Z-61ac1ff6` 正常完成，仅新增测试。
- 主助手使用console.bat实际使用的WorkBuddy Python运行全部64项：**全部通过，8.642s**。实际环境同时安装Playwright/Pillow，无需因系统Python缺依赖而安装。
- 首次实际环境测试出现测试清理错误：终止虚拟环境启动器后子解释器仍短暂持锁。调整测试为等待有时限的holder自然退出，保留独立崩溃释放测试；复跑通过。未修改生产锁实现。
- 首次真实抓取在登录检查处失败，16.84s，退出码1；没有取得课程数据。用户已在login.py窗口完成认证，login.py退出0并保存会话。再次抓取14.70s仍失败，后续只读诊断确认是scrape认证判定误报（见下）。
- 已向**明确配置的wecom_webhook_test**发送1条开发验收消息，接口返回成功。已检查与显式prod不同，使用override避免环境变量/主群回退；未给正式群发送。消息明确说明尚无线上性能结论。
- 私有结果与抓取前本地快照：`.deepastra-work/live-acceptance-20261026/`。不提交日志、真实课程或凭据。首次认证失败会按设计失效旧发布覆盖，恢复登录后需要重新全量刷新。
- 相同范围5轮前后性能比较、真实正文/样本和线上最终验收仍未完成；测试消息成功不等于作业发布流程验收成功。

---

## 主助手最终复核补记（2026-10-26，优先于下方实现方记录）

本轮已到达离线验收节点，按用户额度要求暂停。不是线上验收完成。

- 主助手独立运行最终 Python 测试：默认 Python 共 **62 项，61 项通过、1 项因未安装 Pillow 跳过**（7.347s）；内置带 Pillow Python **62 项全部通过**（7.218s）。测试中的登录失败输出为模拟用例的预期输出，没有访问平台。
- 主助手独立运行最终 `node tests/browser_smoke.js` 通过：2 个持久化选区、冷开 0 个任务、409 保存冲突 1 次、断线后轮询 2 次、外部请求 0、favicon 204。查看了脱敏裁剪及正文截图；发现空占位行残留后已修复并加断言，最终浏览器重跑通过。
- 修复账号切换/认证失效只清 scan_runs 的漏洞：每课覆盖同时失效；单课扫描即使标 complete 也不能复活另一课覆盖；全部刷新后才恢复。
- 扫描与正文写入增加身份代次检查，阻止失效前任务写回。旧正文从普通读接口隐藏，人工裁剪历史保留并标 needs_review；待复核视图不自动套用。
- 补接列表 CLI、正文 CLI、正文入口/详情、扫描中认证失败的全范围失效。增加直接列表 CLI 和正文入口模拟回归。
- 身份变更后正文版本包含代次，即使原文相同，旧编辑窗口也不能用旧版本号保存裁剪。
- 已补配置 account_scope 示例及格式说明；README 纠正不合法中文 scope 示例。告警/计划任务三个保护文件哈希与此前一致。
- **后续仍需真实环境验收**：相同课程范围至少5轮性能比较与30%目标、真实首屏1秒/正文500ms目标、真实账号切换/课程HTML样本与迁移、实际定时/推送兼容。当前没有执行这些线上动作。OCR/HTTP及自动裁剪仍属后续阶段。

---

## 实现方本轮命令与结果（主助手后续结果见上）

| 检查 | 命令 | 实际结果 |
| --- | --- | --- |
| Python 语法 | `D:/Miniconda3/python.exe -B -m compileall -q data tests` | 通过 |
| 配置示例 JSON | `D:/Miniconda3/python.exe -B -c "json.loads(...config.example.json...)"` | 通过，JSON 可解析 |
| 浏览器脚本语法 | `node --check tests/browser_smoke.js` | 通过 |
| 控制台内联 JS 语法 | 提取 `console.html` 的 `<script>` 后 `node --check` | 通过 |
| Python 全套 unittest | `D:/Miniconda3/python.exe -B -m unittest discover -s tests -v` | **共 60 项：59 项通过，1 项跳过**（默认环境无 Pillow，EXIF 方向用例跳过），耗时 7.388s |
| 本地 Edge 浏览器 smoke | `node tests/browser_smoke.js`（`PLAYWRIGHT_PATH` 指向既有 Playwright） | **通过**：persisted_regions=2，cold_open_task_posts=0，conflict_view_posts=1，task_polls=2，external_requests=0，favicon=204 |

浏览器截图（供主助手查看，均为脱敏 fixture 数据）：

- `tests/artifacts/browser-smoke-crop.png`：重载后裁剪弹窗，两个已保存区域与撤销/恢复/保存按钮。
- `tests/artifacts/browser-smoke.png`：冲突处理后的正文详情，含表格单元格内图片。

## 本轮完成项

- **fixture 硬隔离**：`server.load_channel` 固定返回 `none`；`server.run_task`、`server.submit_task`、
  `hub.run_publish`、`hub.run_content`、`hub.run_refresh_courses` 及 `os.startfile` 明确抛
  `FixtureIsolationError`；`hub.run_list` 仅在 `fetch=False` 时放行；配置读取替换为空字典。
  临时数据库、资产、锁、课程索引和日志全部落在临时目录。直接探测 `/api/tasks` 的平台抓取入口
  返回 500，不可能启动线上任务。
- **浏览器网络边界**：`page.route` 只允许 fixture 的 `127.0.0.1:<随机端口>`。外部 URL 一律 abort，
  同时记录并在结尾断言 `external_requests=0`；页面请求 origin 也断言只有 fixture。
- **favicon 明确处理**：fixture 的 `/favicon.ico` 返回 204，不再用“忽略所有 404”掩盖。
- **Console 错误断言收紧**：只接受两类明确预期事件——实际保存冲突 URL 的 HTTP 409，以及
  任务轮询测试中同一 URL 的 `net::ERR_FAILED`；其余任何 console error 都失败。
- **冷开与日期变更**：冷打开、把日期窗口改为 14 天都不产生 `/api/tasks` POST。
- **图片与表格单元格**：使用 400×300 正文图片与 480×360 表格单元格图片，断言两张图片都真实加载、
  尺寸达到要求，且表格结构中的图片正常渲染。
- **多区域裁剪**：拖出两个区域，撤销后剩一个，恢复后回到两个；保存后重新加载正文，仍从 API
  读到两个持久化区域。
- **真实界面 409 冲突**：拦截实际 `view POST` 并返回 409；断言只提交一次、弹窗明确提示“没有覆盖”、
  弹窗出现前裁剪框仍打开、服务端已保存的两个区域和 `fixture-v1` 版本未被覆盖。
- **任务轮询断线恢复**（额外覆盖）：首次状态请求断线时出现“连接中断”，下一次轮询成功并清除提示，
  证明轮询会重试恢复。
- **进程清理**：Node `finally` 关闭浏览器、终止 Python 子进程，并校验目标位于系统临时目录且名称
  符合 `homework-browser-smoke-*` 后才递归清理 fixture 目录。
- **配置与文档**：`data/config.example.json` 增加 `cache`、`content`、`scan` 三段实际支持的参数；
  README 补充缓存/正文/裁剪/依赖/账号切换与限制，并纠正“提速只有减少课程”的旧断言。
- **仓库卫生**：`.gitignore` 加入 `tests/artifacts/` 与 `data/*.lock`；删除本轮遗留
  `data/_patch_probe.txt`，未触碰 `.deepastra-test/`。

## 未完成 / 未覆盖

- **线上 5 轮、30% 提速目标未测**。本轮没有真实账号扫描，不能给出线上加速比例。
- **实际首屏 / 正文 500ms 指标未测**。服务端会返回 `first_screen_ms`，但没有真实数据与真实网络条件，
  不能据此宣称达标。
- **20–30 份真实作业样本未收集**。正文解析、图片和表格只用合成 fixture 验证，未与真实平台 HTML 对照。
- **OCR / HTTP 后续项未做**。没有 OCR 识别，也没有改抓取协议；这些仍属于后续工作。
- **浏览器未覆盖场景**：真实非 headless Edge 交互、真实平台登录态、真实账号切换、课程黑名单/选修课
  界面操作、刷新课程表、手动条目新增/编辑/删除、超大图片/坏图、安装 Pillow 后的 EXIF 浏览器裁剪、
  多标签页、窗口关闭守护、实际定时任务与推送。
- **账号切换限制**：账号代次隔离逻辑沿用已完成实现，本轮没有重做，也没有连接真实平台验证切换流程；
  只确认既有 60 项离线测试仍通过。
- **Pillow 全量测试**：本轮按要求只跑默认环境；带 Pillow 的 60 项全通过是上一轮基线，本轮未重新执行。

## 实现方委派范围保护项确认（不包含主助手后续修复）

- 未修改 `alert.py`、`task_register.py`、`task_run.py` 及 README 既有无窗口运行部分。
- 未修改 `cache_store.py`、`scrape.py` 和既有账号测试。
- 未读取真实 `data/config.json` / `data/storage_state.json`，未输出任何凭据。
- 未执行 git commit / push，未注册任务，未访问学校平台或发送消息。
