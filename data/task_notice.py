"""Non-blocking, local-only Windows notifications for scheduled publishing."""
import os
from pathlib import Path
import subprocess
import sys


def message(kind, elapsed=0):
    seconds = max(0, int(float(elapsed)))
    if kind == 'start':
        return ('作业提醒正在定时发布',
                '检索课程 → 补齐正文 → 发布，通常约30–90秒。\n'
                '期间请暂缓操作北化在线、再次检索/刷新正文、修改名单规则或重复发布。'
                '可查看本地列表和预览，正常使用电脑。完成后再次通知。')
    if kind == 'success':
        return ('作业提醒定时流程已完成',
                f'本次用时{seconds}秒，可以恢复相关操作。\n'
                '处理结果以控制台与发送记录为准；未就绪正文仍可能使用平台提示。')
    if kind == 'failure':
        return ('作业提醒定时流程未完成',
                f'本次用时{seconds}秒，可以恢复相关操作。\n'
                '请查看控制台或 task_run.log；部分消息可能已发送，勿直接重复发布。')
    raise ValueError('unknown notice kind')


def launch(kind, elapsed=0):
    """Never wait for notification dismissal; failure cannot fail publication."""
    if os.name != 'nt':
        return False
    try:
        message(kind, elapsed)
        python = Path(sys.executable)
        hidden = python.with_name('pythonw.exe')
        subprocess.Popen([str(hidden if hidden.exists() else python),
            str(Path(__file__).resolve()), kind, str(max(0, int(elapsed)))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000, close_fds=True)
        return True
    except (OSError, ValueError):
        return False


def main():
    import alert
    title, body = message(sys.argv[1], sys.argv[2])
    # Direct local delivery: never consult alert.channel or webhook config.
    shown, _ = alert._ps_balloon(title, body)
    return 0 if shown else 1


if __name__ == '__main__':
    raise SystemExit(main())
