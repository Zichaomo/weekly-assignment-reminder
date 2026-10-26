import importlib.util
spec=importlib.util.spec_from_file_location('launcher',r'D:\Codex\DeepAstra\launch.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.command
def command(args,interactive=False):
    cmd=original(args,interactive)
    at=cmd.index('--ignore-user-config')
    cmd[at:at]=['-c','windows.sandbox="elevated"']
    return cmd
m.command=command
raise SystemExit(m.main())
