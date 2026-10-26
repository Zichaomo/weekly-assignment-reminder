from pathlib import Path
p=Path(r'D:\Codex\DeepAstra\launch.py')
s=p.read_text(encoding='utf-8')
backup=p.with_name('launch.py.before-windows-fix.bak')
if not backup.exists():
    backup.write_text(s,encoding='utf-8')
needle='    cmd = [shutil.which("codex") or "codex"]'
if 'options["windows.sandbox"]' not in s:
    assert needle in s
    s=s.replace(needle,'    # --ignore-user-config also drops the native Windows sandbox selection.\n    if os.name == "nt":\n        options["windows.sandbox"] = "elevated"\n'+needle)
s=s.replace('tmp.write_text(json.dumps(value, indent=2) + "\\n")','tmp.write_text(json.dumps(value, indent=2) + "\\n", encoding="utf-8")')
s=s.replace('Path(args.key_file).expanduser().read_text().strip()', 'Path(args.key_file).expanduser().read_text(encoding="utf-8-sig").strip()')
s=s.replace('log.read_text(errors="replace")', 'log.read_text(encoding="utf-8", errors="replace")')
s=s.replace('with log.open("w") as out:', 'with log.open("w", encoding="utf-8") as out:')
s=s.replace('env=env, text=True, start_new_session=True)', 'env=env, text=True, encoding="utf-8", start_new_session=True)')
s=s.replace('Path(args.prompt_file).read_text()', 'Path(args.prompt_file).read_text(encoding="utf-8-sig")')
compile(s,str(p),'exec')
p.write_text(s,encoding='utf-8')
print('Launcher updated; backup preserved.')
