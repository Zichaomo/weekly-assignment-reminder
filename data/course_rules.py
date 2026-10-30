"""Editable local content rules; defaults preserve the original course behavior."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

MODES = {'auto': '自动识别题号与任务', 'reference': '仅提示查看北化在线',
         'physics': '去除通用提交说明', 'full': '保留全部纯文字', 'images': '发送正文原图'}
DEFAULTS = [
    {'id': 'lab', 'course': '大学物理实验', 'match': 'prefix', 'mode': 'reference', 'lines': 3},
    {'id': 'physics', 'course': '普通物理', 'match': 'prefix', 'mode': 'physics', 'lines': 3},
    {'id': 'embedded', 'course': '嵌入式系统创新应用与实践', 'match': 'exact', 'mode': 'full', 'lines': 3},
    {'id': 'complex', 'course': '复变函数', 'match': 'prefix', 'mode': 'images', 'lines': 3},
]
LOCK = threading.RLock()


class Conflict(ValueError):
    pass


def path():
    config = Path(os.environ.get('HOMEWORK_CONFIG_PATH') or Path(__file__).with_name('config.json'))
    return config.with_name('course_rules.json')


def validate(rule):
    if not isinstance(rule, dict):
        raise ValueError('规则格式错误')
    result = {k: rule.get(k) for k in ('id', 'course', 'match', 'mode', 'lines')}
    if (not isinstance(result['id'], str) or not result['id'] or len(result['id']) > 100
            or not isinstance(result['course'], str) or not result['course'].strip()
            or len(result['course']) > 160):
        raise ValueError('请填写课程名称与规则编号')
    result['course'] = result['course'].strip()
    if result['match'] not in ('exact', 'prefix') or result['mode'] not in MODES:
        raise ValueError('匹配方式或正文策略无效')
    if type(result['lines']) is not int or not 1 <= result['lines'] <= 20:
        raise ValueError('推送正文行数须为 1–20 的整数')
    return result


def snapshot():
    with LOCK:
        try:
            raw = path().read_bytes()
        except FileNotFoundError:
            raw = b''
        overrides = json.loads(raw) if raw else []
        if not isinstance(overrides, list) or len(overrides) > 300:
            raise ValueError('课程规则文件格式错误')
        overrides = [validate(r) for r in overrides]
        rules = {r['id']: dict(r, builtin=True, customized=False) for r in DEFAULTS}
        for r in overrides:
            rules[r['id']] = dict(r, builtin=r['id'] in rules, customized=True)
        return {'revision': hashlib.sha256(raw).hexdigest(), 'rules': list(rules.values()), 'modes': MODES}


def resolve(course):
    name = str(course or '').strip()
    matches = [r for r in snapshot()['rules'] if
               name == r['course'] or (r['match'] == 'prefix' and name.startswith(r['course']))]
    # Exact match wins; among prefixes, the most specific course name wins.
    return max(matches, key=lambda r: (r['match'] == 'exact', len(r['course'])),
               default={'mode': 'auto', 'lines': 3})


def change(payload):
    with LOCK:
        state = snapshot()
        if payload.get('expected_revision') != state['revision']:
            raise Conflict('规则已变化，请关闭后重新打开课程规则再修改')
        action = payload.get('action')
        if action not in ('save', 'reset'):
            raise ValueError('未知规则操作')
        rule = validate(payload.get('rule')) if action == 'save' else None
        rid = rule['id'] if rule else payload.get('id')
        overrides = [r for r in state['rules'] if r['customized'] and r['id'] != rid]
        if rule:
            if any(r['id'] != rid and r['course'] == rule['course'] and r['match'] == rule['match']
                   for r in state['rules']):
                raise ValueError('同一课程与匹配方式已有规则，请编辑已有规则')
            overrides.append(rule)
        overrides = [validate(r) for r in overrides]
        if len(overrides) > 300:
            raise ValueError('规则最多 300 条')
        target = path()
        fd, tmp = tempfile.mkstemp(prefix='course-rules-', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(overrides, stream, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return snapshot()


def preview(payload):
    import cache_store
    import content_summary
    rule = validate(payload.get('rule'))
    key = payload.get('item_key')
    if key:
        item = cache_store.get_item(key)
        if not item:
            raise ValueError('当前账号下找不到这条作业')
        name = str(item.get('course') or '').strip()
        if name != rule['course'] and not (rule['match'] == 'prefix' and name.startswith(rule['course'])):
            raise ValueError('这条作业不匹配当前课程规则')
    else:
        text = payload.get('text', '')
        if not isinstance(text, str) or len(text) > 30000:
            raise ValueError('试用正文最多 30000 字')
        item = {'course': rule['course'], 'content_ready': True, 'source_version': 'preview',
                'content_blocks': [{'id': 'preview', 'type': 'paragraph', 'text': text}]}
    result = content_summary.summarize(item, rule=rule)
    lines = [s for s in result['text'].splitlines() if s.strip()]
    result['push_text'] = ('\n'.join(lines[:rule['lines']]) +
        ('\n……（详情参见北化在线）' if len(lines) > rule['lines'] else ''))
    if result['status'] in ('needs_review', 'unavailable', 'reference_only'):
        result['push_text'] = '详细要求见北化在线'
    if result['status'] == 'images':
        result['push_text'] = f"作业原图随提醒发送（{len(result['asset_ids'])} 张）"
    return result
