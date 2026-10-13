Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$python = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\envs\default\Scripts\python.exe"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:platformRows = @()

# ---------------------------------------------------------------- 基础调用
function Invoke-Py($scriptName, $argsStr) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.WorkingDirectory = $scriptDir
    $psi.Arguments = "$scriptName $argsStr"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEnd()
        $err = $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        return @{ExitCode = $p.ExitCode; Out = $out; Err = $err }
    } catch {
        return @{ExitCode = -1; Out = ""; Err = $_.Exception.Message }
    }
}

function Run-Hub($argsStr) {
    $r = Invoke-Py "hub.py" $argsStr
    if ($r.ExitCode -ne 0) { Log-Append "hub.py 异常: $($r.Err)"; return $null }
    # 兜底：若 stdout 里混入了非 JSON 的提示文字，截取第一个 { 到最后一个 } 再解析
    $txt = $r.Out
    try { return ($txt | ConvertFrom-Json) } catch { }
    $i = $txt.IndexOf('{'); $j = $txt.LastIndexOf('}')
    if ($i -ge 0 -and $j -gt $i) {
        try { return ($txt.Substring($i, $j - $i + 1) | ConvertFrom-Json) } catch { }
    }
    Log-Append "JSON 解析失败: $($r.Out)"
    return $null
}

function Run-ManualPy($argsStr) {
    $r = Invoke-Py "manual.py" $argsStr
    if ($r.ExitCode -eq 0) { return $r.Out.Trim() }
    return "ERROR: $($r.Err)"
}

function Log-Append($line) {
    if ($txtLog.Text -eq "") { $txtLog.Text = $line } else { $txtLog.Text = $txtLog.Text + "`r`n" + $line }
}
function Show-Message($msg) { $txtLog.Text = ($msg -replace "`r?`n", "`r`n") }

# ---------------------------------------------------------------- 列表渲染
function Render-List($rows) {
    $lv.Items.Clear()
    foreach ($r in $rows) {
        $li = New-Object System.Windows.Forms.ListViewItem($r.source)
        $li.SubItems.Add($r.course) | Out-Null
        $li.SubItems.Add($r.title) | Out-Null          # 平台上的作业标题，正文要点「查看作业内容」
        $li.SubItems.Add($r.deadline) | Out-Null
        $li.SubItems.Add($r.left) | Out-Null
        $li.SubItems.Add($r.status) | Out-Null
        $li.Tag = @{source = $r.source; id = $r.manual_id; hwtid = $r.hwtid; content_url = $r.content_url; submit_url = $r.submit_url; result_url = $r.result_url; content = $r.content }
        $lv.Items.Add($li) | Out-Null
    }
}

function Refresh-View {
    # 手动条目（实时从 manual.json 取，按当前天数窗口过滤；勾选「含全部」则不限窗口）
    $arg = if ($chkAll.Checked) { "--all" } else { "--days $([int]$numDays.Value)" }
    $mres = Run-Hub "list $arg"
    $manualRows = if ($mres) { $mres.rows } else { @() }
    $all = @($script:platformRows) + @($manualRows)
    Render-List $all
}

function Fetch-Platform {
    $full = $chkSweep.Checked        # 勾上=全量扫描（发给全班，不能漏课）；切勿写成 -not
    $est = if ($full) { "约 3~6 分钟（逐门课扫全部课程）" } else { "约 10~30 秒" }
    Log-Append "正在检索北化在线作业（$est，界面会暂时无响应，请稍候）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $arg = if ($chkAll.Checked) { "--all --fetch" } else { "--days $([int]$numDays.Value) --fetch" }
        if (-not $full) { $arg += " --pool"; Log-Append "⚠ 警告：检测到非全量模式，这会漏掉「你已交、同学还没交」的课。请关窗重开 console.bat。" }
        if (-not $chkTests.Checked) { $arg += " --no-tests" }
        $res = Run-Hub "list $arg"
        if ($null -eq $res) { return }
        if ($res.error) {
            Log-Append $res.error
            [System.Windows.Forms.MessageBox]::Show($res.error, "检索失败", "OK", "Error") | Out-Null
            return
        }
        $script:platformRows = $res.rows | Where-Object { $_.source -eq '平台' }
        $scope = if ($chkAll.Checked) { '全部，含已过期' } else { '窗口内' }
        $mode = if ($full) { '按截止日期·全量' } else { '⚠ 首页待提交池(已废弃，会漏课!)' }
        Log-Append "已检索北化在线：平台作业 $($script:platformRows.Count) 条（$scope，$mode）。"
        if ($res.sweep) {
            $missed = @($res.sweep.missed_from_pool)
            if ($missed.Count -gt 0) {
                Log-Append "提示：首页视图漏掉 $($missed.Count) 门有作业的课（全量扫描已兜住）：$((($missed | ForEach-Object { $_.course }) -join '、'))"
            }
            if (@($res.sweep.errors).Count -gt 0) {
                Log-Append "⚠️ 有 $(@($res.sweep.errors).Count) 门课抓取失败，覆盖不完整（详见检索输出）。"
            }
        }
        if ($res.warnings -and @($res.warnings.failed_courses).Count -gt 0) {
            Log-Append "⚠️ 抓取失败课程：$((@($res.warnings.failed_courses) | Select-Object -First 5) -join '、')"
        }
        # 本次抓取用时（含分阶段明细）
        if ($res.elapsed_human) {
            $t = $res.timing
            if ($t -and ($t.homework -or $t.test)) {
                Log-Append ("⏱ 本次抓取用时 $($res.elapsed_human)  ——  登录 $([int]$t.login)s / 作业 $([int]$t.homework)s / 在线测试 $([int]$t.test)s")
            } else {
                Log-Append "⏱ 本次抓取用时 $($res.elapsed_human)"
            }
        }
        Refresh-View
    } finally {
        [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::Default
    }
}

# ---------------------------------------------------------------- 手动管理
function Selected-ManualId {
    if ($lv.SelectedItems.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("先在列表里选中一条「手动」作业。", "提示", "OK") | Out-Null
        return $null
    }
    $tag = $lv.SelectedItems[0].Tag
    if ($tag.source -ne '手动' -or $null -eq $tag.id) {
        [System.Windows.Forms.MessageBox]::Show("只能管理手动添加的作业（平台作业只读）。", "提示", "OK") | Out-Null
        return $null
    }
    return $tag.id
}

function Show-EditDialog($id) {
    $path = Join-Path $scriptDir "manual.json"
    if (-not (Test-Path $path)) { return }
    $items = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($items -isnot [System.Array]) { $items = @($items) }
    $it = $items | Where-Object { $_.id -eq $id }
    if ($null -eq $it) {
        [System.Windows.Forms.MessageBox]::Show("找不到该条目。", "错误", "OK", "Error") | Out-Null
        return
    }

    $ef = New-Object System.Windows.Forms.Form
    $ef.Text = "编辑作业 id=$id"
    $ef.Size = New-Object System.Drawing.Size(470, 340)
    $ef.StartPosition = "CenterScreen"
    $ef.FormBorderStyle = "FixedDialog"
    $ef.MaximizeBox = $false

    $lblC = New-Object System.Windows.Forms.Label; $lblC.Text = "科目名："; $lblC.Location = New-Object System.Drawing.Point(20, 20); $lblC.Size = New-Object System.Drawing.Size(410, 20); $ef.Controls.Add($lblC)
    $tbC = New-Object System.Windows.Forms.TextBox; $tbC.Location = New-Object System.Drawing.Point(20, 42); $tbC.Size = New-Object System.Drawing.Size(410, 24); $tbC.Text = $it.course; $ef.Controls.Add($tbC)

    $lblT = New-Object System.Windows.Forms.Label; $lblT.Text = "作业内容："; $lblT.Location = New-Object System.Drawing.Point(20, 82); $lblT.Size = New-Object System.Drawing.Size(410, 20); $ef.Controls.Add($lblT)
    $tbT = New-Object System.Windows.Forms.TextBox; $tbT.Location = New-Object System.Drawing.Point(20, 104); $tbT.Size = New-Object System.Drawing.Size(410, 24); $tbT.Text = $it.title; $ef.Controls.Add($tbT)

    $lblD = New-Object System.Windows.Forms.Label; $lblD.Text = "截止时间（如 2026-09-17 23:59，留空清除）："; $lblD.Location = New-Object System.Drawing.Point(20, 144); $lblD.Size = New-Object System.Drawing.Size(410, 20); $ef.Controls.Add($lblD)
    $tbD = New-Object System.Windows.Forms.TextBox; $tbD.Location = New-Object System.Drawing.Point(20, 166); $tbD.Size = New-Object System.Drawing.Size(410, 24); $tbD.Text = $it.deadline; $ef.Controls.Add($tbD)

    $lblN = New-Object System.Windows.Forms.Label; $lblN.Text = "备注（可留空）："; $lblN.Location = New-Object System.Drawing.Point(20, 206); $lblN.Size = New-Object System.Drawing.Size(410, 20); $ef.Controls.Add($lblN)
    $tbN = New-Object System.Windows.Forms.TextBox; $tbN.Location = New-Object System.Drawing.Point(20, 228); $tbN.Size = New-Object System.Drawing.Size(410, 24); $tbN.Text = $it.note; $ef.Controls.Add($tbN)

    $btnSave = New-Object System.Windows.Forms.Button; $btnSave.Text = "保存"; $btnSave.Location = New-Object System.Drawing.Point(20, 272); $btnSave.Size = New-Object System.Drawing.Size(100, 30); $ef.Controls.Add($btnSave)
    $btnCancel = New-Object System.Windows.Forms.Button; $btnCancel.Text = "取消"; $btnCancel.Location = New-Object System.Drawing.Point(140, 272); $btnCancel.Size = New-Object System.Drawing.Size(100, 30); $ef.Controls.Add($btnCancel)

    $btnSave.Add_Click({
        $c = $tbC.Text.Trim(); $t = $tbT.Text.Trim(); $d = $tbD.Text.Trim(); $n = $tbN.Text.Trim()
        if (-not $c -or -not $t -or -not $d) {
            [System.Windows.Forms.MessageBox]::Show("科目、内容、截止时间 都要填。", "缺字段", "OK", "Warning") | Out-Null
            return
        }
        $res = Run-ManualPy "edit $id --course `"$c`" --title `"$t`" --deadline `"$d`" --note `"$n`""
        [System.Windows.Forms.MessageBox]::Show($res, "保存结果", "OK") | Out-Null
        $ef.Close()
    })
    $btnCancel.Add_Click({ $ef.Close() })

    $ef.ShowDialog() | Out-Null
    Refresh-View
}

# ---------------------------------------------------------------- 查看平台作业内容
function Selected-PlatformRow {
    if ($lv.SelectedItems.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show("先在列表里选中一条平台作业。", "提示", "OK") | Out-Null
        return $null
    }
    $tag = $lv.SelectedItems[0].Tag
    if ($tag.source -ne '平台' -or -not $tag.hwtid) {
        [System.Windows.Forms.MessageBox]::Show("只能查看平台作业的内容（手动作业没有平台链接）。", "提示", "OK") | Out-Null
        return $null
    }
    return $tag
}

function View-Content {
    $tag = Selected-PlatformRow; if ($null -eq $tag) { return }

    # 这一行已经取过正文，直接显示缓存，不必再抓一次
    if ($tag.content) {
        Show-Message ("===== 作业内容（hwtid=$($tag.hwtid)）=====`r`n" + $tag.content)
        Log-Append "作业内容已显示在上方（本次已取过，用缓存）。"
        return
    }

    Log-Append "正在获取作业内容（hwtid=$($tag.hwtid)，约 10 秒）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $res = Run-Hub "content --hwtid $($tag.hwtid)"
        if ($null -eq $res) { return }
        if (-not $res.ok) {
            $msg = if ($res.error) { $res.error } else { "未知错误" }
            Log-Append "获取失败: $msg"
            [System.Windows.Forms.MessageBox]::Show($msg, "获取失败", "OK", "Error") | Out-Null
            return
        }
        $txt = if ($res.content_text) { $res.content_text } else { "（该作业没有填写正文）" }
        Show-Message ("===== 作业内容（hwtid=$($tag.hwtid)）=====`r`n" + $txt + "`r`n`r`n网页：" + $res.url)
        # 回填到列表的「作业内容」列已取消（列只剩标题），仅内存缓存，重复点不再抓
        $tag.content = $txt
        Log-Append "作业内容已显示在上方。想看完整页面（附件/排版）点「浏览器打开」。"
    } finally {
        [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::Default
    }
}

function Open-In-Browser {
    $tag = Selected-PlatformRow; if ($null -eq $tag) { return }
    # 已提交 -> 查看结果页；未提交 -> 提交作业页；都没有 -> 内容页
    $url = if ($tag.result_url) { $tag.result_url } elseif ($tag.submit_url) { $tag.submit_url } else { $tag.content_url }
    if (-not $url) {
        [System.Windows.Forms.MessageBox]::Show("该作业没有可打开的链接。", "提示", "OK") | Out-Null
        return
    }
    try {
        Start-Process $url
        Log-Append "已在浏览器打开: $url"
    } catch {
        [System.Windows.Forms.MessageBox]::Show("无法打开浏览器：$($_.Exception.Message)", "错误", "OK", "Error") | Out-Null
    }
}

# ---------------------------------------------------------------- 发布
function Publish($NoSend) {
    $days = [int]$numDays.Value
    # 必须在函数内自己算：$full 只定义在 Fetch-Platform 里，
    # 在这里引用会得到 $null，`-not $null` 恒为 True，于是永远走 --pool 分支（漏课）。
    $full = $chkSweep.Checked         # 同上，勾上=全量
    $flag = if ($NoSend) { "--no-send" } else { "" }
    Log-Append "正在$(if($NoSend){'生成预览'}else{'发布'})（未来 $days 天，约 10~30 秒）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $poolArg = if ($full) { "" } else { " --pool" }
        $res = Run-Hub "publish --days $days $flag$poolArg"
        if ($null -eq $res) { return }
        if ($res.error) {
            [System.Windows.Forms.MessageBox]::Show($res.error, "错误", "OK", "Error") | Out-Null
            Log-Append $res.error
            return
        }
        Show-Message $res.message
        if ($res.elapsed_human) { Log-Append "⏱ 本次抓取用时 $($res.elapsed_human)" }
        if ($NoSend) {
            Log-Append "已生成预览（未发送），共 $($res.count) 条。"
        } elseif ($res.deduped) {
            # 去重生效：内容与刚才发过的完全一样，没有重复打扰
            [System.Windows.Forms.MessageBox]::Show($res.note, "重复内容已跳过", "OK", "Information") | Out-Null
            Log-Append $res.note
        } else {
            if ($res.ok) {
                [System.Windows.Forms.MessageBox]::Show("已通过 $($res.channel) 发布，共 $($res.count) 条。", "发布成功", "OK") | Out-Null
                Log-Append "已发布($($res.channel))，共 $($res.count) 条。"
            } else {
                [System.Windows.Forms.MessageBox]::Show("发布失败，详见日志。", "错误", "OK", "Error") | Out-Null
                Log-Append "发布失败。"
            }
        }
        Refresh-View
    } finally {
        [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::Default
    }
}

# ================================================================ 界面
$form = New-Object System.Windows.Forms.Form
$script:buildTag = (Get-Item $MyInvocation.MyCommand.Path).LastWriteTime.ToString("MM-dd HH:mm")
$form.Text = "作业提醒控制台  [脚本构建 $($script:buildTag)]"
$form.Size = New-Object System.Drawing.Size(940, 700)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.ClientSize = New-Object System.Drawing.Size(940, 710)

# 顶部按钮行
$btnFetch = New-Object System.Windows.Forms.Button
$btnFetch.Text = "检索北化在线"; $btnFetch.Location = New-Object System.Drawing.Point(12, 12); $btnFetch.Size = New-Object System.Drawing.Size(130, 30)
$form.Controls.Add($btnFetch)

$btnPreview = New-Object System.Windows.Forms.Button
$btnPreview.Text = "预览"; $btnPreview.Location = New-Object System.Drawing.Point(758, 12); $btnPreview.Size = New-Object System.Drawing.Size(80, 30)
$form.Controls.Add($btnPreview)

$btnPublish = New-Object System.Windows.Forms.Button
$btnPublish.Text = "提前发布"; $btnPublish.Location = New-Object System.Drawing.Point(844, 12); $btnPublish.Size = New-Object System.Drawing.Size(84, 30)
$form.Controls.Add($btnPublish)

$lblDays = New-Object System.Windows.Forms.Label; $lblDays.Text = "未来"; $lblDays.Location = New-Object System.Drawing.Point(418, 19); $lblDays.Size = New-Object System.Drawing.Size(36, 20); $form.Controls.Add($lblDays)
$numDays = New-Object System.Windows.Forms.NumericUpDown
$numDays.Location = New-Object System.Drawing.Point(456, 15); $numDays.Size = New-Object System.Drawing.Size(50, 24)
$numDays.Minimum = 1; $numDays.Maximum = 30; $numDays.Value = 7
$form.Controls.Add($numDays)
$lblDays2 = New-Object System.Windows.Forms.Label; $lblDays2.Text = "天"; $lblDays2.Location = New-Object System.Drawing.Point(512, 19); $lblDays2.Size = New-Object System.Drawing.Size(24, 20); $form.Controls.Add($lblDays2)

$chkAll = New-Object System.Windows.Forms.CheckBox; $chkAll.Text = "含全部平台作业"; $chkAll.Location = New-Object System.Drawing.Point(160, 16); $chkAll.Size = New-Object System.Drawing.Size(130, 22); $chkAll.Checked = $false; $form.Controls.Add($chkAll)

# 在线测试与作业同一套截止规则；关掉可以省掉一轮扫描（约省一半时间）
$chkTests = New-Object System.Windows.Forms.CheckBox; $chkTests.Text = "抓在线测试"; $chkTests.Location = New-Object System.Drawing.Point(298, 16); $chkTests.Size = New-Object System.Drawing.Size(106, 24); $chkTests.Checked = $true; $form.Controls.Add($chkTests)

# 【重要】「全量扫描」不再做成勾选框 —— 它唯一的作用就是退回「首页待提交池」模式，
# 而那个模式会让「自己已交、同学还没交」的课整门消失（复变函数、大学物理实验都因此丢过）。
# 这里保留一个恒为 $true 的同名变量，下游所有判断代码零改动，但用户无法关掉它。
$chkSweep = New-Object System.Windows.Forms.CheckBox
$chkSweep.Text = "全量扫描"; $chkSweep.Checked = $true; $chkSweep.Visible = $false
$form.Controls.Add($chkSweep)

# 这三个原在顶部，会盖住「手动添加作业」分组框标题（y=46 撞 y=50），
# 现统一放到列表下方的 row4（与 row3 同一水平区间，见文件末尾的坐标说明）
$btnBlacklist = New-Object System.Windows.Forms.Button
$btnBlacklist.Text = "课程黑名单"; $btnBlacklist.Location = New-Object System.Drawing.Point(136, 490); $btnBlacklist.Size = New-Object System.Drawing.Size(110, 30)
$form.Controls.Add($btnBlacklist)

$btnTestBl = New-Object System.Windows.Forms.Button
$btnTestBl.Text = "测试黑名单"; $btnTestBl.Location = New-Object System.Drawing.Point(256, 490); $btnTestBl.Size = New-Object System.Drawing.Size(110, 30)
$form.Controls.Add($btnTestBl)

$btnCourses = New-Object System.Windows.Forms.Button
$btnCourses.Text = "刷新课程表"; $btnCourses.Location = New-Object System.Drawing.Point(12, 490); $btnCourses.Size = New-Object System.Drawing.Size(114, 30)
$form.Controls.Add($btnCourses)

# 这个标签的内容长度会变（clipboard / wecom / pushplus / none），
# 所以不能写死宽度 —— 之前写死 94px，在 125% DPI 下 "推送渠道: wecom" 直接被截断。
$lblChannel = New-Object System.Windows.Forms.Label
$lblChannel.Location = New-Object System.Drawing.Point(548, 20)
$lblChannel.AutoSize = $true
$lblChannel.MaximumSize = New-Object System.Drawing.Size(200, 20)
$form.Controls.Add($lblChannel)
try {
    $cfg = Get-Content (Join-Path $scriptDir "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $lblChannel.Text = "推送渠道: $($cfg.channel)"
} catch { $lblChannel.Text = "推送渠道: 未知" }

# 手动添加分组
$gp = New-Object System.Windows.Forms.GroupBox
$gp.Text = "手动添加作业（老师发在微信群 / 其他群、平台抓不到的。截止日期输入支持 9.10 / 9月10日 / 9-10 23:59 / 2026-09-10 23:59）"
$gp.Location = New-Object System.Drawing.Point(12, 84); $gp.Size = New-Object System.Drawing.Size(916, 86)
$form.Controls.Add($gp)

# 控件必须 Add 到 $gp（不能 Add 到 $form）——否则会被 GroupBox 的 client-area 重绘背景覆盖
$lblC = New-Object System.Windows.Forms.Label; $lblC.Text = "科目"; $lblC.Location = New-Object System.Drawing.Point(15, 22); $lblC.Size = New-Object System.Drawing.Size(150, 18); $gp.Controls.Add($lblC)
$tbC = New-Object System.Windows.Forms.TextBox; $tbC.Location = New-Object System.Drawing.Point(15, 42); $tbC.Size = New-Object System.Drawing.Size(150, 22); $gp.Controls.Add($tbC)

$lblT = New-Object System.Windows.Forms.Label; $lblT.Text = "内容"; $lblT.Location = New-Object System.Drawing.Point(180, 22); $lblT.Size = New-Object System.Drawing.Size(200, 18); $gp.Controls.Add($lblT)
$tbT = New-Object System.Windows.Forms.TextBox; $tbT.Location = New-Object System.Drawing.Point(180, 42); $tbT.Size = New-Object System.Drawing.Size(200, 22); $gp.Controls.Add($tbT)

$lblD = New-Object System.Windows.Forms.Label; $lblD.Text = "截止（9.10）"; $lblD.Location = New-Object System.Drawing.Point(395, 22); $lblD.Size = New-Object System.Drawing.Size(100, 18); $gp.Controls.Add($lblD)
$tbD = New-Object System.Windows.Forms.TextBox; $tbD.Location = New-Object System.Drawing.Point(395, 42); $tbD.Size = New-Object System.Drawing.Size(100, 22); $gp.Controls.Add($tbD)

$lblN = New-Object System.Windows.Forms.Label; $lblN.Text = "备注（可留空）"; $lblN.Location = New-Object System.Drawing.Point(505, 22); $lblN.Size = New-Object System.Drawing.Size(180, 18); $gp.Controls.Add($lblN)
$tbN = New-Object System.Windows.Forms.TextBox; $tbN.Location = New-Object System.Drawing.Point(505, 42); $tbN.Size = New-Object System.Drawing.Size(200, 22); $gp.Controls.Add($tbN)

$btnAdd = New-Object System.Windows.Forms.Button; $btnAdd.Text = "添加"; $btnAdd.Location = New-Object System.Drawing.Point(720, 40); $btnAdd.Size = New-Object System.Drawing.Size(80, 26); $gp.Controls.Add($btnAdd)

# 列表（分组框占 84~170，列表从 178 起）
$lv = New-Object System.Windows.Forms.ListView
$lv.View = 'Details'; $lv.FullRowSelect = $true; $lv.GridLines = $true
$lv.Location = New-Object System.Drawing.Point(12, 178); $lv.Size = New-Object System.Drawing.Size(916, 268)
foreach ($c in @(@('来源', 58), @('科目', 120), @('标题', 230), @('截止', 140), @('剩余', 110), @('状态', 76))) {
    $col = New-Object System.Windows.Forms.ColumnHeader
    $col.Text = $c[0]; $col.Width = $c[1]
    $lv.Columns.Add($col) | Out-Null
}
$form.Controls.Add($lv)

# 手动管理按钮行（列表占 158~438，按钮从 446 起）
$btnDone = New-Object System.Windows.Forms.Button; $btnDone.Text = "标记完成"; $btnDone.Location = New-Object System.Drawing.Point(12, 454); $btnDone.Size = New-Object System.Drawing.Size(110, 30); $form.Controls.Add($btnDone)
$btnEdit = New-Object System.Windows.Forms.Button; $btnEdit.Text = "编辑"; $btnEdit.Location = New-Object System.Drawing.Point(132, 454); $btnEdit.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnEdit)
$btnDel = New-Object System.Windows.Forms.Button; $btnDel.Text = "删除"; $btnDel.Location = New-Object System.Drawing.Point(232, 454); $btnDel.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnDel)
$btnClean = New-Object System.Windows.Forms.Button; $btnClean.Text = "清理过期"; $btnClean.Location = New-Object System.Drawing.Point(332, 454); $btnClean.Size = New-Object System.Drawing.Size(110, 30); $form.Controls.Add($btnClean)
$btnRefresh = New-Object System.Windows.Forms.Button; $btnRefresh.Text = "刷新"; $btnRefresh.Location = New-Object System.Drawing.Point(452, 454); $btnRefresh.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnRefresh)
$btnContent = New-Object System.Windows.Forms.Button; $btnContent.Text = "查看作业内容"; $btnContent.Location = New-Object System.Drawing.Point(560, 454); $btnContent.Size = New-Object System.Drawing.Size(120, 30); $btnContent.Enabled = $false; $form.Controls.Add($btnContent)
$btnOpen = New-Object System.Windows.Forms.Button; $btnOpen.Text = "浏览器打开"; $btnOpen.Location = New-Object System.Drawing.Point(690, 454); $btnOpen.Size = New-Object System.Drawing.Size(110, 30); $btnOpen.Enabled = $false; $form.Controls.Add($btnOpen)

$btnRefreshAll = New-Object System.Windows.Forms.Button; $btnRefreshAll.Text = "刷新视图"; $btnRefreshAll.Location = New-Object System.Drawing.Point(808, 454); $btnRefreshAll.Size = New-Object System.Drawing.Size(120, 30); $form.Controls.Add($btnRefreshAll)

# 日志 / 预览（row3 454~484、row4 490~520，日志从 528 起，高 170 → 到 698）
$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Location = New-Object System.Drawing.Point(12, 528); $txtLog.Size = New-Object System.Drawing.Size(916, 170)
$txtLog.Multiline = $true; $txtLog.ScrollBars = "Vertical"; $txtLog.ReadOnly = $true
$txtLog.WordWrap = $true
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 9)
$form.Controls.Add($txtLog)

# 事件绑定
$btnFetch.Add_Click({ Fetch-Platform })
$btnPreview.Add_Click({ Publish -NoSend $true })
$btnPublish.Add_Click({ Publish -NoSend $false })


# 列表项类型：重写 ToString()，否则 CheckedListBox 会显示 PSObject 的属性包
if (-not ("BlItem" -as [type])) {
    Add-Type -TypeDefinition @"
public class BlItem {
    public string Key;
    public string Kind;      // cand | cur
    public string Label;     // 列表里显示的短标签
    public string Detail;    // 右侧详情栏显示的完整说明
    public BlItem(string key, string kind, string label, string detail) {
        Key = key; Kind = kind; Label = label; Detail = detail;
    }
    public override string ToString() { return Label; }
}
"@
}

# ---------------------------------------------------------------- 黑名单管理
function Show-BlacklistDialog {
    $kind = $script:blKind          # "course" | "test"
    $cmdList = if ($kind -eq "course") { "blacklist-list" } else { "test-blacklist-list" }
    $script:blRes = Run-Hub $cmdList
    if ($null -eq $script:blRes) { return }

    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = if ($kind -eq "course") { "课程黑名单（整门课都不抓：作业 + 在线测试）" } else { "测试黑名单（只跳在线测试，作业照抓）" }
    $dlg.ClientSize = New-Object System.Drawing.Size(960, 620)
    $dlg.StartPosition = "CenterParent"
    $dlg.FormBorderStyle = "FixedDialog"
    $dlg.MaximizeBox = $false; $dlg.MinimizeBox = $false

    $lblInfo = New-Object System.Windows.Forms.Label
    $lblInfo.Location = New-Object System.Drawing.Point(14, 10); $lblInfo.Size = New-Object System.Drawing.Size(930, 40)
    $dlg.Controls.Add($lblInfo)

    $lblList = New-Object System.Windows.Forms.Label
    $lblList.Text = "勾选条目后可「加入」或「移出」"
    $lblList.Location = New-Object System.Drawing.Point(14, 54); $lblList.Size = New-Object System.Drawing.Size(560, 18)
    $dlg.Controls.Add($lblList)

    # 左：短标签列表（自动换行禁用 + 横向滚动兜底）
    $lb = New-Object System.Windows.Forms.CheckedListBox
    $lb.Location = New-Object System.Drawing.Point(14, 74); $lb.Size = New-Object System.Drawing.Size(600, 460)
    $lb.CheckOnClick = $true
    $lb.HorizontalScrollbar = $true
    $lb.Font = New-Object System.Drawing.Font("Consolas", 10)
    $dlg.Controls.Add($lb)

    # 右：详情栏（选中即显示完整说明，永不截断）
    $lblDetail = New-Object System.Windows.Forms.Label
    $lblDetail.Text = "详情（点左侧条目查看）"
    $lblDetail.Location = New-Object System.Drawing.Point(626, 54); $lblDetail.Size = New-Object System.Drawing.Size(320, 18)
    $dlg.Controls.Add($lblDetail)

    $txtDetail = New-Object System.Windows.Forms.TextBox
    $txtDetail.Location = New-Object System.Drawing.Point(626, 74); $txtDetail.Size = New-Object System.Drawing.Size(320, 460)
    $txtDetail.Multiline = $true; $txtDetail.ReadOnly = $true
    $txtDetail.ScrollBars = "Vertical"; $txtDetail.WordWrap = $true
    $txtDetail.BackColor = [System.Drawing.Color]::FromArgb(250, 250, 250)
    $dlg.Controls.Add($txtDetail)

    $btnAdd = New-Object System.Windows.Forms.Button
    $btnAdd.Location = New-Object System.Drawing.Point(14, 548); $btnAdd.Size = New-Object System.Drawing.Size(190, 34)
    $dlg.Controls.Add($btnAdd)

    $btnRemove = New-Object System.Windows.Forms.Button
    $btnRemove.Text = "移出勾选项（恢复抓取）"
    $btnRemove.Location = New-Object System.Drawing.Point(212, 548); $btnRemove.Size = New-Object System.Drawing.Size(190, 34)
    $dlg.Controls.Add($btnRemove)

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Text = "刷新列表"; $btnRefresh.Location = New-Object System.Drawing.Point(766, 548); $btnRefresh.Size = New-Object System.Drawing.Size(90, 34)
    $dlg.Controls.Add($btnRefresh)

    $btnClose = New-Object System.Windows.Forms.Button
    $btnClose.Text = "关闭"; $btnClose.Location = New-Object System.Drawing.Point(862, 548); $btnClose.Size = New-Object System.Drawing.Size(84, 34)
    $dlg.Controls.Add($btnClose)

    function Reload-Rows {
        $res = $script:blRes
        $btnAdd.Text = if ($kind -eq "course") { "加入勾选项（需确认）" } else { "添加 lid（需确认）" }
        $lb.Items.Clear()
        $txtDetail.Text = ""
        if ($kind -eq "course") {
            $cands = @($res.candidates); $cur = @($res.blacklist)
            $lblInfo.Text = ("学期起点 $($res.semester_start)　课程 $($res.total_courses) 门　" +
                             "已拉黑 $($cur.Count) 门　待确认候选 $($cands.Count) 门`r`n" +
                             "程序不会自动拉黑：勾选候选 → 点「加入勾选项（需确认）」→ 再确认一次才生效。")
            $i = 0
            foreach ($c in $cands) {
                $i++
                $tag = if ($c.reason -eq "stale") { "陈旧  " } else { "无入口" }
                $label = "{0,3}. [{1}] {2}   ({3})" -f $i, $tag, $c.course, $c.lid
                $detail = "课程：$($c.course)`r`n编号：$($c.lid)`r`n状态：待确认候选`r`n理由：$($c.detail)`r`n最近作业：$(if ($c.last_homework) { $c.last_homework } else { '（从未抓到）' })`r`n扫描失败次数：$($c.fail_streak)"
                $lb.Items.Add((New-Object BlItem("$($c.lid)", "cand", $label, $detail))) | Out-Null
            }
            foreach ($b in $cur) {
                $i++
                $label = "{0,3}. [已拉黑] {1}   ({2})" -f $i, $b.name, $b.lid
                $detail = "课程：$($b.name)`r`n编号：$($b.lid)`r`n状态：已拉黑（不抓取）`r`n理由：$($b.reason)`r`n说明：$($b.detail)`r`n确认时间：$($b.confirmed_at)"
                $lb.Items.Add((New-Object BlItem("$($b.lid)", "cur", $label, $detail))) | Out-Null
                $i++
            }
        } else {
            $cur = @($res.blacklist)
            $lblInfo.Text = ("已屏蔽在线测试的课 $($cur.Count) 门 —— 这些课的作业仍然照常抓取。`r`n" +
                             "加入方式：点「添加 lid（需确认）」后在弹窗里填课程编号（如 24206）。")
            $i = 0
            foreach ($b in $cur) {
                $i++
                $label = "{0,3}. [已屏蔽测试] {1}   ({2})" -f $i, $b.name, $b.lid
                $detail = "课程：$($b.name)`r`n编号：$($b.lid)`r`n状态：已屏蔽「在线测试」（作业照抓）`r`n说明：$($b.note)`r`n确认时间：$($b.confirmed_at)"
                $lb.Items.Add((New-Object BlItem("$($b.lid)", "cur", $label, $detail))) | Out-Null
                $i++
            }
        }
    }
    Reload-Rows

    $lb.Add_SelectedIndexChanged({
        try {
            $o = $lb.SelectedItem
            if ($o) { $txtDetail.Text = $o.Detail } else { $txtDetail.Text = "" }
        } catch { }
    })

    $btnAdd.Add_Click({
        $ids = @()
        if ($kind -eq "course") {
            for ($i = 0; $i -lt $lb.CheckedItems.Count; $i++) {
                $o = $lb.CheckedItems[$i]
                if ($o.Kind -eq "cand") { $ids += $o.Key }
            }
        } else {
            $in = [Microsoft.VisualBasic.Interaction]::InputBox("填要屏蔽在线测试的课程编号（多个用空格分隔）", "测试黑名单", "")
            if ($in) { $ids = @($in -split "\s+" | Where-Object { $_ }) }
        }
        if ($ids.Count -eq 0) {
            [System.Windows.Forms.MessageBox]::Show("没有勾选任何「待确认候选」。`r`n已拉黑的条目不能重复加入；要恢复请勾选后点「移出勾选项」。", "提示", "OK", "Information") | Out-Null
            return
        }
        $listText = ($ids | ForEach-Object { "   • $_" }) -join "`r`n"
        $ans = [System.Windows.Forms.MessageBox]::Show(
            "确认把这些加入黑名单？`r`n`r`n$listText`r`n`r`n加入后不再抓取，可用「移出勾选项」恢复。",
            "二次确认", "YesNo", "Question")
        if ($ans -ne "Yes") { return }
        $cmd = if ($kind -eq "course") { "blacklist-apply --lids $($ids -join ' ')" } else { "test-blacklist-apply --lids $($ids -join ' ')" }
        $r2 = Run-Hub $cmd
        if ($null -eq $r2) { return }
        if ($r2.error) { [System.Windows.Forms.MessageBox]::Show($r2.error, "失败", "OK", "Error") | Out-Null; return }
        Log-Append "已加入黑名单 $($ids.Count) 门：$($ids -join ', ')"
        $script:blRes = Run-Hub $cmdList
        Reload-Rows
    })

    $btnRemove.Add_Click({
        $ids = @()
        for ($i = 0; $i -lt $lb.CheckedItems.Count; $i++) {
            $o = $lb.CheckedItems[$i]
            if ($o.Kind -eq "cur") { $ids += $o.Key }
        }
        if ($ids.Count -eq 0) {
            [System.Windows.Forms.MessageBox]::Show("没有勾选任何「已拉黑 / 已屏蔽」条目。", "提示", "OK", "Information") | Out-Null
            return
        }
        $ans = [System.Windows.Forms.MessageBox]::Show("确认把 $($ids.Count) 门移出黑名单（恢复抓取）？", "确认", "YesNo", "Question")
        if ($ans -ne "Yes") { return }
        $cmd = if ($kind -eq "course") { "blacklist-remove --lids $($ids -join ' ')" } else { "test-blacklist-remove --lids $($ids -join ' ')" }
        $r2 = Run-Hub $cmd
        if ($null -eq $r2) { return }
        if ($r2.error) { [System.Windows.Forms.MessageBox]::Show($r2.error, "失败", "OK", "Error") | Out-Null; return }
        Log-Append "已移出黑名单 $($ids.Count) 门：$($ids -join ', ')"
        $script:blRes = Run-Hub $cmdList
        Reload-Rows
    })

    $btnRefresh.Add_Click({ $script:blRes = Run-Hub $cmdList; Reload-Rows })
    $btnClose.Add_Click({ $dlg.Close() })
    $dlg.ShowDialog() | Out-Null
}

$script:blKind = "course"

$btnBlacklist.Add_Click({
    $script:blKind = "course"
    Show-BlacklistDialog
    Refresh-View
})

$btnTestBl.Add_Click({
    $script:blKind = "test"
    Show-BlacklistDialog
    Refresh-View
})

$btnRefreshAll.Add_Click({
    $n = @($script:platformRows).Count
    Render-List @($script:platformRows)
    Log-Append "已刷新视图（按上次检索结果重画 $n 条）。"
})

$btnCourses.Add_Click({
    Log-Append "正在刷新课程表（从「我的课程」拉全部课程，约 10~20 秒）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $res = Run-Hub "refresh-courses"
        if ($null -eq $res) { return }
        if ($res.error) {
            Log-Append "刷新课程表失败：$($res.error)"
            [System.Windows.Forms.MessageBox]::Show($res.error, "刷新失败", "OK", "Error") | Out-Null
            return
        }
        Log-Append "课程表已刷新：共 $($res.count) 门。再点「检索北化在线」即可用新表扫描。"
    } finally {
        [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::Default
    }
})
$btnAdd.Add_Click({
    $c = $tbC.Text.Trim(); $t = $tbT.Text.Trim(); $d = $tbD.Text.Trim(); $n = $tbN.Text.Trim()
    if (-not $c -or -not $t -or -not $d) {
        [System.Windows.Forms.MessageBox]::Show("科目、内容、截止时间 都要填。", "缺字段", "OK", "Warning") | Out-Null
        return
    }
    $res = Run-ManualPy "add --course `"$c`" --title `"$t`" --deadline `"$d`" --note `"$n`""
    [System.Windows.Forms.MessageBox]::Show($res, "添加结果", "OK") | Out-Null
    $tbC.Text = ""; $tbT.Text = ""; $tbD.Text = ""; $tbN.Text = ""
    Refresh-View
})
$btnDone.Add_Click({
    $id = Selected-ManualId; if ($null -eq $id) { return }
    $res = Run-ManualPy "done $id"
    [System.Windows.Forms.MessageBox]::Show($res, "结果", "OK") | Out-Null
    Refresh-View
})
$btnDel.Add_Click({
    $id = Selected-ManualId; if ($null -eq $id) { return }
    if ([System.Windows.Forms.MessageBox]::Show("确定删除这条？", "确认", 'YesNo', 'Question') -ne 'Yes') { return }
    $res = Run-ManualPy "rm $id"
    [System.Windows.Forms.MessageBox]::Show($res, "结果", "OK") | Out-Null
    Refresh-View
})
$btnEdit.Add_Click({
    $id = Selected-ManualId; if ($null -eq $id) { return }
    Show-EditDialog $id
})
$btnClean.Add_Click({
    $res = Run-ManualPy "clean"
    [System.Windows.Forms.MessageBox]::Show($res, "结果", "OK") | Out-Null
    Refresh-View
})
$btnRefresh.Add_Click({ Refresh-View })
$btnContent.Add_Click({ View-Content })
$btnOpen.Add_Click({ Open-In-Browser })
# 只有选中「平台」行时，查看内容/浏览器打开才可用
$lv.Add_SelectedIndexChanged({
    $ok = $false
    if ($lv.SelectedItems.Count -gt 0) {
        $t = $lv.SelectedItems[0].Tag
        $ok = ($t.source -eq '平台') -and ($null -ne $t.hwtid) -and ($t.hwtid -ne "")
    }
    $btnContent.Enabled = $ok
    $btnOpen.Enabled = $ok
})
$lv.Add_DoubleClick({
    if ($lv.SelectedItems.Count -eq 0) { return }
    $tag = $lv.SelectedItems[0].Tag
    if ($tag.source -eq '手动' -and $null -ne $tag.id) { Show-EditDialog $tag.id }
    elseif ($tag.source -eq '平台') { View-Content }
})

# 启动：先写就绪提示，再刷新列表
# （顺序有讲究：Refresh-View 出错时会往日志框追加错误，日志框已有内容才写得进去）
[System.Windows.Forms.Application]::EnableVisualStyles() | Out-Null

# 强制全量扫描：旧版本可能把这个勾选框关过，状态会随控件名持久化在注册表里，
# 下次启动被恢复成「未勾选」——那会让检索退回首页待提交池并整门漏课。
# 所以启动时无条件纠正，用户没有关掉它的入口（控件本身已隐藏）。
$chkSweep.Checked = $true
Log-Append "===== 控制台就绪  脚本构建时间 $($script:buildTag)  =====  若时间早于你最后一次改动，说明这是旧进程，请关窗重开。"
Log-Append "「检索北化在线」一律按【截止日期】全量扫描所有课程（约 1-2 分钟），不再有「首页待提交池」模式。"
Log-Append "因为这份提醒是发给全班的：你交没交，不影响这门课该不该提醒全班。"
Log-Append "选中「平台」行 -> 「查看作业内容」读题目要求 / 「浏览器打开」进平台页面（已交=查看结果，未交=提交作业）。"
Log-Append "「课程黑名单」= 整门课都不抓（作业+测试）；「测试黑名单」= 只跳在线测试、作业照抓。两者都要勾选后二次确认才生效。"
Log-Append "「刷新课程表」在拿到新课程后点一次。提示：检索一律为全量扫描（约 1-2 分钟）—— 只有全量才不会漏掉「你已交、同学还没交」的课。"
Refresh-View
$form.ShowDialog() | Out-Null
