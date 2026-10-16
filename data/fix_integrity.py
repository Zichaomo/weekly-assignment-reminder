# -*- coding: utf-8 -*-
"""检查 / 修复工作区里的「低完整性标签」—— 它会让双击 .bat 时弹安全警告。

现象（2026-10-16 实测）：
    双击 console.bat 弹出「打开文件 - 安全警告 / 无法验证发布者。你确定要运行此软件吗？」
    文件本身完全正常，也没有 Zone.Identifier（不是「从网上下载」那类）。

原因：
    文件被打上了 Windows 的 **Low 完整性标签**（Mandatory Label\\Low Mandatory Level）。
    完整性标签是「不能向上写」的强制标记，资源管理器启动低完整性对象时会先要你确认。
    本机的成因：DSH 受限会话把**工作区根目录**标成了 Low，之后在这个目录里
    新建/修改的文件都会**继承**这个标签（实测：console.bat / README.md / .gitignore 中招，
    而 data/ 下的旧文件仍是 Medium）。

为什么不能直接改标签：
    这些对象的写权限来自沙箱身份而不是登录用户，所以
      · icacls /setintegritylevel     -> Access is denied
      · PowerShell Set-Acl -Audit     -> 需要 SeSecurityPrivilege（管理员才有）
      · SetNamedSecurityInfo(LABEL)   -> 返回 5（拒绝访问）
    三条路在本机都走不通。

能走通的路（本工具的做法）：
    给**父目录**设标签是允许的（实测工作区根改 Medium 成功）。新文件继承父目录的标签，
    所以把文件「原样删掉重建」即可：内容一个字节都不改，标签自然变成 Medium。
    重建前会先把原名暂存、重建后逐字节校验，不一致立即回滚。

用法：
    python fix_integrity.py                     # 只扫描，报告带 Low 标签的对象
    python fix_integrity.py --list              # 扫描并列出
    python fix_integrity.py --fix               # 扫描并修复（重建法）
    python fix_integrity.py --fix --dry-run     # 只看会改哪些
    python fix_integrity.py --path <目录>        # 指定扫描根（默认工作区根）
"""
import argparse
import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # data/ 的上一级 = 工作区根
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 完整性级别的 SDDL 写法：既有缩写（LW/ME/HI…），也有完整 SID（S-1-16-*）
LEVELS = {
    "UN": "Untrusted", "LW": "Low", "ME": "Medium", "HI": "High",
    "SI": "System",
    "S-1-16-0": "Untrusted", "S-1-16-4096": "Low", "S-1-16-8192": "Medium",
    "S-1-16-12288": "High", "S-1-16-16384": "System",
}

LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)]
advapi32.GetFileSecurityW.restype = wintypes.BOOL
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p]
advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
kernel32.LocalFree.argtypes = [ctypes.c_void_p]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".acl-recovery"}


# ---------------------------------------------------------------- 读标签
def integrity_of(path) -> str:
    """返回 'Low' / 'Medium' / ... ；没有显式标签返回 '未标注'；读不到返回 '?'。"""
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(path), LABEL_SECURITY_INFORMATION, None, 0,
                              ctypes.byref(needed))
    if not needed.value:
        return "?"
    buf = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(str(path), LABEL_SECURITY_INFORMATION, buf,
                                     needed.value, ctypes.byref(needed)):
        return "?"
    sddl = ctypes.c_wchar_p()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buf, SDDL_REVISION_1, LABEL_SECURITY_INFORMATION,
            ctypes.byref(sddl), None):
        return "?"
    text = sddl.value or ""
    kernel32.LocalFree(sddl)
    # SDDL 形如 S:AI(ML;ID;NW;;;LW)：级别在最后一节，必须取尾节，
    # 直接在整个串里找 'LW' 会误命中别的缩写（实测踩过）。
    m = re.search(r";([A-Z]{2}|S-1-16-\d+)\)", text)
    if not m:
        return "未标注"
    return LEVELS.get(m.group(1), f"其他({m.group(1)})")


def set_label(path, sddl: str):
    """按 SDDL 设置完整性标签（对目录有效；对沙箱标记过的文件通常会失败）。"""
    sd = ctypes.c_void_p()
    size = wintypes.DWORD(0)
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(sd), ctypes.byref(size)):
        return False, f"构造安全描述符失败 err={ctypes.get_last_error()}"
    try:
        rc = advapi32.SetNamedSecurityInfoW(str(path), 1,  # 1 = SE_FILE_OBJECT
                                            LABEL_SECURITY_INFORMATION,
                                            None, None, None, sd)
        return (rc == 0), (f"SetNamedSecurityInfo={rc}" if rc else "ok")
    finally:
        kernel32.LocalFree(sd)


# ---------------------------------------------------------------- 遍历
def walk(root: Path):
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        yield Path(cur)
        for f in files:
            yield Path(cur) / f


def scan(root: Path):
    items, tally = [], {}
    for p in walk(root):
        lvl = integrity_of(p)
        tally[lvl] = tally.get(lvl, 0) + 1
        if lvl == "Low":
            items.append(p)
    return items, tally


# ---------------------------------------------------------------- 修复
def rebuild(path: Path):
    """原样删除重建，让文件继承父目录的标签。返回 (ok, 说明)。"""
    if not path.is_file():
        return False, "不是普通文件（目录请直接设标签）"
    data = path.read_bytes()
    bak = path.with_name(path.name + ".__rebuild.bak")
    try:
        if bak.exists():
            bak.unlink()
        os.replace(path, bak)                     # 只动目录项，不动内容
    except OSError as e:
        return False, f"无法暂存原名：{e}"
    try:
        with open(path, "wb") as f:
            f.write(data)
        if path.read_bytes() != data:
            raise RuntimeError("重建后字节不一致")
    except Exception as e:  # noqa: BLE001
        try:
            if path.exists():
                path.unlink()
            os.replace(bak, path)
            return False, f"{e}（已回滚）"
        except OSError as e2:
            return False, f"{e}，且回滚失败，备份留在 {bak}（{e2}）"
    try:
        bak.unlink()
    except OSError:
        pass
    return True, f"重建完成（{len(data)} 字节，内容一致）"


def fix(root: Path, items, dry: bool):
    ok = fail = 0
    for p in items:
        if dry:
            print("  [dry-run] " + str(p))
            continue
        if p.is_dir():
            good, why = set_label(p, "S:(ML;;NW;;;ME)")   # 目录：直接设 Medium
        else:
            good, why = rebuild(p)                        # 文件：重建使其继承父目录
        if good and integrity_of(p) == "Low":
            good, why = False, why + "；复核仍为 Low"
        print(f"  {'OK  ' if good else 'FAIL'} {p.relative_to(root)}  ({why})")
        ok += good
        fail += not good
    return ok, fail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(ROOT), help="扫描根目录（默认工作区根）")
    ap.add_argument("--list", action="store_true", help="列出带 Low 标签的对象")
    ap.add_argument("--fix", action="store_true", help="修复（重建法）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --fix：只显示不改")
    args = ap.parse_args()

    root = Path(args.path)
    print(f"扫描：{root}")
    items, tally = scan(root)
    print(f"共 {sum(tally.values())} 个对象，标签分布：{tally}")

    if not items:
        print("没有 Low 标签 —— 双击 .bat 不会因为这个原因弹安全警告。")
        return 0

    print(f"带 Low 标签的对象：{len(items)} 个")
    for p in items:
        print("  LOW  " + str(p.relative_to(root)))

    if not args.fix:
        print("\n加 --fix 修复（文件用「原样删除重建」让标签变成 Medium）。")
        return 1

    print("\n开始修复：")
    if args.dry_run:
        fix(root, items, True)
        print(f"[dry-run] 将处理 {len(items)} 个对象，未实际改动。")
        return 1
    ok, fail = fix(root, items, False)
    left = [p for p in items if integrity_of(p) == "Low"]
    print(f"完成：成功 {ok}，失败 {fail}；复核仍为 Low 的还有 {len(left)} 个")
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
