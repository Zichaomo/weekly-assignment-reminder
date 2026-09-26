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
    Log-Append "正在检索北化在线作业（约 10~30 秒，界面会暂时无响应，请稍候）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $arg = if ($chkAll.Checked) { "--all --fetch" } else { "--days $([int]$numDays.Value) --fetch" }
        $res = Run-Hub "list $arg"
        if ($null -eq $res) { return }
        if ($res.error) {
            Log-Append $res.error
            [System.Windows.Forms.MessageBox]::Show($res.error, "检索失败", "OK", "Error") | Out-Null
            return
        }
        $script:platformRows = $res.rows | Where-Object { $_.source -eq '平台' }
        Log-Append "已检索北化在线，平台作业 $($script:platformRows.Count) 条$(if($chkAll.Checked){'（全部，含已过期）'}else{'（窗口内）'})。"
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
    $flag = if ($NoSend) { "--no-send" } else { "" }
    Log-Append "正在$(if($NoSend){'生成预览'}else{'发布'})（未来 $days 天，约 10~30 秒）..."
    [System.Windows.Forms.Cursor]::Current = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $res = Run-Hub "publish --days $days $flag"
        if ($null -eq $res) { return }
        if ($res.error) {
            [System.Windows.Forms.MessageBox]::Show($res.error, "错误", "OK", "Error") | Out-Null
            Log-Append $res.error
            return
        }
        Show-Message $res.message
        if ($NoSend) {
            Log-Append "已生成预览（未发送），共 $($res.count) 条。"
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
$form.Text = "作业提醒控制台"
$form.Size = New-Object System.Drawing.Size(940, 700)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.ClientSize = New-Object System.Drawing.Size(940, 690)

# 顶部按钮行
$btnFetch = New-Object System.Windows.Forms.Button
$btnFetch.Text = "检索北化在线"; $btnFetch.Location = New-Object System.Drawing.Point(12, 12); $btnFetch.Size = New-Object System.Drawing.Size(130, 30)
$form.Controls.Add($btnFetch)

$btnPreview = New-Object System.Windows.Forms.Button
$btnPreview.Text = "预览"; $btnPreview.Location = New-Object System.Drawing.Point(720, 12); $btnPreview.Size = New-Object System.Drawing.Size(90, 30)
$form.Controls.Add($btnPreview)

$btnPublish = New-Object System.Windows.Forms.Button
$btnPublish.Text = "提前发布"; $btnPublish.Location = New-Object System.Drawing.Point(820, 12); $btnPublish.Size = New-Object System.Drawing.Size(100, 30)
$form.Controls.Add($btnPublish)

$lblDays = New-Object System.Windows.Forms.Label; $lblDays.Text = "未来"; $lblDays.Location = New-Object System.Drawing.Point(310, 18); $lblDays.Size = New-Object System.Drawing.Size(36, 20); $form.Controls.Add($lblDays)
$numDays = New-Object System.Windows.Forms.NumericUpDown
$numDays.Location = New-Object System.Drawing.Point(348, 13); $numDays.Size = New-Object System.Drawing.Size(50, 24)
$numDays.Minimum = 1; $numDays.Maximum = 30; $numDays.Value = 7
$form.Controls.Add($numDays)
$lblDays2 = New-Object System.Windows.Forms.Label; $lblDays2.Text = "天"; $lblDays2.Location = New-Object System.Drawing.Point(400, 18); $lblDays2.Size = New-Object System.Drawing.Size(24, 20); $form.Controls.Add($lblDays2)

$chkAll = New-Object System.Windows.Forms.CheckBox; $chkAll.Text = "含全部平台作业"; $chkAll.Location = New-Object System.Drawing.Point(160, 16); $chkAll.Size = New-Object System.Drawing.Size(130, 22); $chkAll.Checked = $false; $form.Controls.Add($chkAll)

$lblChannel = New-Object System.Windows.Forms.Label; $lblChannel.Location = New-Object System.Drawing.Point(440, 18); $lblChannel.Size = New-Object System.Drawing.Size(260, 20); $form.Controls.Add($lblChannel)
try {
    $cfg = Get-Content (Join-Path $scriptDir "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $lblChannel.Text = "推送渠道: $($cfg.channel)"
} catch { $lblChannel.Text = "推送渠道: 未知" }

# 手动添加分组
$gp = New-Object System.Windows.Forms.GroupBox
$gp.Text = "手动添加作业（老师发在微信群 / 其他群、平台抓不到的。截止日期输入支持 9.10 / 9月10日 / 9-10 23:59 / 2026-09-10 23:59）"
$gp.Location = New-Object System.Drawing.Point(12, 50); $gp.Size = New-Object System.Drawing.Size(916, 100)
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

# 列表（手动 GroupBox 占 50~150，列表从 158 起）
$lv = New-Object System.Windows.Forms.ListView
$lv.View = 'Details'; $lv.FullRowSelect = $true; $lv.GridLines = $true
$lv.Location = New-Object System.Drawing.Point(12, 158); $lv.Size = New-Object System.Drawing.Size(916, 280)
foreach ($c in @(@('来源', 58), @('科目', 120), @('标题', 230), @('截止', 140), @('剩余', 110), @('状态', 76))) {
    $col = New-Object System.Windows.Forms.ColumnHeader
    $col.Text = $c[0]; $col.Width = $c[1]
    $lv.Columns.Add($col) | Out-Null
}
$form.Controls.Add($lv)

# 手动管理按钮行（列表占 158~438，按钮从 446 起）
$btnDone = New-Object System.Windows.Forms.Button; $btnDone.Text = "标记完成"; $btnDone.Location = New-Object System.Drawing.Point(12, 446); $btnDone.Size = New-Object System.Drawing.Size(110, 30); $form.Controls.Add($btnDone)
$btnEdit = New-Object System.Windows.Forms.Button; $btnEdit.Text = "编辑"; $btnEdit.Location = New-Object System.Drawing.Point(132, 446); $btnEdit.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnEdit)
$btnDel = New-Object System.Windows.Forms.Button; $btnDel.Text = "删除"; $btnDel.Location = New-Object System.Drawing.Point(232, 446); $btnDel.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnDel)
$btnClean = New-Object System.Windows.Forms.Button; $btnClean.Text = "清理过期"; $btnClean.Location = New-Object System.Drawing.Point(332, 446); $btnClean.Size = New-Object System.Drawing.Size(110, 30); $form.Controls.Add($btnClean)
$btnRefresh = New-Object System.Windows.Forms.Button; $btnRefresh.Text = "刷新"; $btnRefresh.Location = New-Object System.Drawing.Point(452, 446); $btnRefresh.Size = New-Object System.Drawing.Size(90, 30); $form.Controls.Add($btnRefresh)
$btnContent = New-Object System.Windows.Forms.Button; $btnContent.Text = "查看作业内容"; $btnContent.Location = New-Object System.Drawing.Point(560, 446); $btnContent.Size = New-Object System.Drawing.Size(120, 30); $btnContent.Enabled = $false; $form.Controls.Add($btnContent)
$btnOpen = New-Object System.Windows.Forms.Button; $btnOpen.Text = "浏览器打开"; $btnOpen.Location = New-Object System.Drawing.Point(690, 446); $btnOpen.Size = New-Object System.Drawing.Size(110, 30); $btnOpen.Enabled = $false; $form.Controls.Add($btnOpen)

# 日志 / 预览（按钮行 446~476，日志从 484 起，高 194 → 到 678；ClientSize 高 690 留 12px 边距）
$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Location = New-Object System.Drawing.Point(12, 484); $txtLog.Size = New-Object System.Drawing.Size(916, 194)
$txtLog.Multiline = $true; $txtLog.ScrollBars = "Vertical"; $txtLog.ReadOnly = $true
$txtLog.WordWrap = $true
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 9)
$form.Controls.Add($txtLog)

# 事件绑定
$btnFetch.Add_Click({ Fetch-Platform })
$btnPreview.Add_Click({ Publish -NoSend $true })
$btnPublish.Add_Click({ Publish -NoSend $false })
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

# 启动
[System.Windows.Forms.Application]::EnableVisualStyles() | Out-Null
Refresh-View
Log-Append "控制台已就绪。点「检索北化在线」拉取平台作业；「提前发布」会立即推送。"
Log-Append "选中「平台」行 -> 「查看作业内容」读题目要求 / 「浏览器打开」进平台页面（已交=查看结果，未交=提交作业）。"
Log-Append "要看已过期的旧作业：勾选「全部作业」后重新点「检索北化在线」。"
$form.ShowDialog() | Out-Null
