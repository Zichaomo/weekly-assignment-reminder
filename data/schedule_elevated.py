"""UAC helper for the same validated, project-scoped scheduled-task action."""
import argparse
import json
import os
from pathlib import Path
import re
import schedule_control
import schedule_status

ROOT=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--request',required=True)
    args=ap.parse_args();request=Path(args.request).resolve()
    if request.parent != (ROOT/'schedule_requests').resolve() or not re.fullmatch(r'[a-f0-9]{32}\.json',request.name):
        return 1
    data=json.loads(request.read_text(encoding='utf-8'))
    verify=bool(data.get('verify_cycle'))
    result=None
    try:
        if verify:
            cancelled=schedule_control.change('cancel',allow_elevation=False,principal_user=data['request_user'])
            state=schedule_status.snapshot(True)
            stopped=cancelled['ok'] and state.get('available') and len(state['tasks'])==2 and all(not t.get('enabled') for t in state['tasks'])
        result=schedule_control.change(data['action'],data['time'],data['days'],data['catchup'],allow_elevation=False,principal_user=data['request_user'])
        if verify:result['cancel_verified']=stopped
        result['schedule']=schedule_status.snapshot(True)
    except Exception as exc:
        result={'ok':False,'error':'系统授权后的操作未完成，请刷新状态检查。','error_type':type(exc).__name__}
    finally:
        # No credential or raw task XML is returned or logged by this helper.
        if result is None:result={'ok':False,'error':'操作未完成，请刷新状态检查。'}
        out=request.with_suffix('.result.json');temp=out.with_suffix('.tmp')
        temp.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');os.replace(temp,out)
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
