"""Read-only Windows task status and redacted local execution receipts."""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parent
_cache=None
_at=0
_lock=threading.Lock()


def _query_windows():
    if os.name!='nt':
        return {'available':False,'error':'当前系统不支持 Windows 计划任务查询','tasks':[]}
    expected=str(ROOT).replace("'","''")
    script=r'''[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
$s=New-Object -ComObject Schedule.Service
$s.Connect()
$result=@()
foreach($name in @('BUCT-Homework-Weekly','BUCT-Homework-Catchup')) {
 try {$t=$s.GetFolder('\').GetTask($name)} catch {
  $e=$_.Exception; while($e.InnerException){$e=$e.InnerException}
  if ($e.HResult -eq -2147024894) {$result+=@{name=$name;registered=$false};continue}
  throw
 }
 $next=$t.NextRunTime; $last=$t.LastRunTime
 $file=if($name -eq 'BUCT-Homework-Weekly'){'task_run.py'}else{'task_catchup.py'}
 $belongs=$false; $days=7
 foreach($a in $t.Definition.Actions) {
  if($a.Arguments.IndexOf((Join-Path 'EXPECTED_ROOT' $file),[StringComparison]::OrdinalIgnoreCase) -ge 0){$belongs=$true}
  if($a.Arguments -match '--days\s+(\d+)'){$days=[int]$Matches[1]}
 }
 $at=$null;foreach($tr in $t.Definition.Triggers){if($tr.StartBoundary){$at=([datetime]$tr.StartBoundary).ToString('HH:mm');break}}
 $result+=@{name=$name;registered=$true;enabled=$t.Enabled;state=[int]$t.State;
  next_run=$(if($next.Year -gt 2000){$next.ToString('yyyy-MM-dd HH:mm:ss')}else{$null});
  last_run=$(if($last.Year -gt 2000){$last.ToString('yyyy-MM-dd HH:mm:ss')}else{$null});
  last_result=[long]$t.LastTaskResult;matches_project=$belongs;schedule_time=$at;days=$days}
}
ConvertTo-Json -InputObject @($result) -Compress -Depth 4
'''.replace('EXPECTED_ROOT',expected)
    try:
        r=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10,
            creationflags=0x08000000)
        if r.returncode:
            raise ValueError('task query failed')
        tasks=json.loads(r.stdout)
        return {'available':True,'tasks':tasks if isinstance(tasks,list) else [tasks]}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return {'available':False,'error':'计划任务状态暂时无法读取，请稍后刷新','tasks':[]}


def _records(name):
    path=ROOT/name
    if not path.exists():
        return []
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-262144))
        lines=stream.read().decode('utf-8',errors='replace').splitlines()
    records=[]
    for line in lines:
        try:
            row=json.loads(line)
            if isinstance(row,dict):records.append(row)
        except ValueError:pass
    return records


def _safe_error(text):
    text=re.sub(r'https?://\S+','[链接已隐藏]',str(text or ''))
    text=re.sub(r'(?i)(?:key|token|secret)\s*[=:]\s*\S+','[凭据已隐藏]',text)
    return text[-240:]


def snapshot(force=False):
    global _cache,_at
    with _lock:
        if force or _cache is None or time.monotonic()-_at>30:
            _cache=_query_windows();_at=time.monotonic()
        result=dict(_cache)
    result['tasks']=[dict(task, next_run=task.get('next_run') if task.get('enabled') else None)
                     for task in result.get('tasks',[])]
    runs=_records('task_log.jsonl')
    last=runs[-1] if runs else {}
    result['last_attempt']={k:last.get(k) for k in ('time','kind','ok','exit_code','channel','elapsed_sec')}
    result['last_attempt']['error']=_safe_error(last.get('error'))
    receipts={}
    for row in _records('push_log.jsonl'):
        if (row.get('ok') and row.get('channel') in ('wecom','pushplus')
                and row.get('source') in ('weekly','task-run')
                and not str(row.get('note','')).startswith('duplicate')):
            receipts[row.get('audience','class')]={'time':row.get('time'),'channel':row.get('channel')}
    result['last_success']=receipts
    result['revision']=hashlib.sha256(json.dumps(result.get('tasks',[]),sort_keys=True).encode()).hexdigest()[:16]
    return result
