"""Explicit UI actions for this project's two default scheduled tasks."""
import base64
from datetime import datetime, timedelta
import json
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from xml.sax.saxutils import escape

ROOT=Path(__file__).resolve().parent
NAMES=('BUCT-Homework-Weekly','BUCT-Homework-Catchup')


def next_sunday(time_text, now=None):
    now=now or datetime.now()
    hour,minute=map(int,time_text.split(':'))
    day=(6-now.weekday())%7
    date=(now+timedelta(days=day)).replace(hour=hour,minute=minute,second=0,microsecond=0)
    if date<=now:date+=timedelta(days=7)
    return date.isoformat(timespec='seconds')


def definition(time_text, days, catchup=False):
    python=Path(sys.executable)
    hidden=python.with_name('pythonw.exe')
    python=hidden if hidden.exists() else python
    script=ROOT/('task_catchup.py' if catchup else 'task_run.py')
    args=f'"{script}" --wecom config --days {days}'
    if catchup:args+=f' --delay-min 15 --target-time {time_text}'
    else:args+=' --kind scheduled'
    repeat='' if catchup else '<Repetition><Interval>PT30M</Interval><Duration>PT2H</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>'
    start=next_sunday('00:05' if catchup else time_text)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>BUCT homework reminder</Description></RegistrationInfo>
<Triggers><CalendarTrigger>{repeat}<StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
<ScheduleByWeek><DaysOfWeek><Sunday/></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger></Triggers>
<Principals><Principal id="Author"><UserId>__CURRENT_USER__</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>
<Enabled>true</Enabled><ExecutionTimeLimit>PT90M</ExecutionTimeLimit></Settings>
<Actions Context="Author"><Exec><Command>{escape(str(python))}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory></Exec></Actions></Task>'''


def _elevated_change(action, time_text, days, catchup, *, verify_cycle=False):
    folder=ROOT/'schedule_requests';folder.mkdir(exist_ok=True)
    request=folder/(uuid.uuid4().hex+'.json')
    result=request.with_suffix('.result.json')
    request.write_text(json.dumps({'action':action,'time':time_text,'days':days,
        'catchup':catchup,'verify_cycle':verify_cycle,
        'request_user':(os.environ.get('USERDOMAIN','')+'\\'+getpass.getuser()).lstrip('\\')}),encoding='utf-8')
    python=Path(sys.executable);hidden=python.with_name('pythonw.exe')
    python=hidden if hidden.exists() else python
    def literal(value):return "'"+str(value).replace("'","''")+"'"
    arguments=f'"{ROOT / "schedule_elevated.py"}" --request "{request}"'
    script=("$ErrorActionPreference='Stop';try {$p=Start-Process -FilePath " +literal(python)
        +' -ArgumentList '+literal(arguments)
        +" -Verb RunAs -WindowStyle Hidden -Wait -PassThru;exit $p.ExitCode} catch {exit 1223}")
    try:
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
            base64.b64encode(script.encode('utf-16-le')).decode()],capture_output=True,
            timeout=100,creationflags=0x08000000)
        if result.exists():return json.loads(result.read_text(encoding='utf-8'))
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return {'ok':False,'error':'系统授权尚未完成或操作超时，请完成确认后刷新状态检查。'}
    return {'ok':False,'error':'未完成Windows权限确认，定时设置未保存。'}


def change(action, time_text='09:00', days=7, catchup=True, *, allow_elevation=True, principal_user=None):
    if action not in ('set','cancel'):raise ValueError('未知的定时操作')
    if not isinstance(time_text,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',time_text):
        raise ValueError('请选择有效的发布时间')
    if type(days) is not int or not 1<=days<=30 or type(catchup) is not bool:
        raise ValueError('提醒天数应为1至30天，补推选项应为开或关')
    if os.name!='nt':raise ValueError('当前系统不支持Windows计划任务设置')
    payload={'action':action,'root':str(ROOT),'catchup':catchup,'user':principal_user,
             'xml':[definition(time_text,days),definition(time_text,days,True)]}
    encoded=base64.b64encode(json.dumps(payload,ensure_ascii=False).encode('utf-8')).decode()
    script=r'''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD'))|ConvertFrom-Json
$s=New-Object -ComObject Schedule.Service; $s.Connect(); $f=$s.GetFolder([string][char]92)
$names=@('BUCT-Homework-Weekly','BUCT-Homework-Catchup');$old=@{};$changed=@()
$user=if($p.user){[string]$p.user}else{[Security.Principal.WindowsIdentity]::GetCurrent().Name}
$stage='inspect'
try {
 foreach($name in $names) {
  try {$t=$f.GetTask($name)} catch {
   $e=$_.Exception;while($e.InnerException){$e=$e.InnerException}
   if($e.HResult -eq -2147024894){$old[$name]=$null;continue};throw
  }
  if($t.State -eq 4 -or $t.State -eq 2){throw 'busy'}
  $file=if($name -eq $names[0]){'task_run.py'}else{'task_catchup.py'}
  $match=$false
  foreach($a in $t.Definition.Actions){if($a.Arguments.IndexOf((Join-Path $p.root $file),[StringComparison]::OrdinalIgnoreCase)-ge 0){$match=$true}}
  if(-not $match){throw 'foreign task'}
  $old[$name]=@{xml=$t.Xml;enabled=$t.Enabled}
 }
 $stage='validate';$definitions=@()
 foreach($xml in $p.xml){
  $xml=$xml.Replace('__CURRENT_USER__',[Security.SecurityElement]::Escape($user))
  $d=$s.NewTask(0);$d.XmlText=$xml;$definitions+=,$d.XmlText
 }
 for($i=0;$i -lt 2;$i++) {
  $stage='write-'+$i
  $name=$names[$i];$changed+=,$name
  if($p.action -eq 'cancel' -or ($i -eq 1 -and -not $p.catchup)) {
   if($old[$name]){$t=$f.GetTask($name);$t.Enabled=$false}
  } else {$null=$f.RegisterTask($name,$definitions[$i],6,$user,$null,3,$null)}
 }
 @{ok=$true;rollback_ok=$true}|ConvertTo-Json -Compress
} catch {
 $failure=$_.Exception;while($failure.InnerException){$failure=$failure.InnerException}
 $detail=$failure.Message; $code=$failure.HResult
 $rolled=$true
 foreach($name in $changed){try {
  if($old[$name]){$null=$f.RegisterTask($name,$old[$name].xml,6,$user,$null,3,$null);$t=$f.GetTask($name);$t.Enabled=$old[$name].enabled}
  else {$f.DeleteTask($name,0)}
 } catch {$rolled=$false}}
 @{ok=$false;rollback_ok=$rolled;stage=$stage;code=$code;detail=$detail}|ConvertTo-Json -Compress
}'''.replace('PAYLOAD',encoded)
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
            base64.b64encode(script.encode('utf-16-le')).decode()],capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=30,creationflags=0x08000000)
        state=json.loads(result.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return {'ok':False,'error':'设置结果暂时无法确认，请刷新定时状态后检查；未触发立即发布。'}
    if not state.get('ok'):
        if state.get('code') == -2147024891 and allow_elevation:
            return _elevated_change(action,time_text,days,catchup)
        reason=('Windows拒绝修改计划任务，需要系统权限确认。' if state.get('code') == -2147024891
                else '任务正在运行，请稍后重试。' if state.get('detail') == 'busy'
                else '任务指向其他项目，已拒绝修改。' if state.get('detail') == 'foreign task'
                else '定时操作失败，请刷新状态检查。')
        return {'ok':False,'error':reason,
                'stage':state.get('stage'),'code':state.get('code'),
                'rollback_ok':state.get('rollback_ok',False)}
    return {'ok':True,'note':'定时发布及补推已停用' if action=='cancel' else '定时设置已保存，不会立即发布'}
