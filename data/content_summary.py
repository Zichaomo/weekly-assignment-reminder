"""Conservative, source-backed homework excerpts. No model or network calls."""
import re

RULE_VERSION = '2026-10-29.3'
FULL_TEXT_COURSES = {'嵌入式系统创新应用与实践'}
QUESTION_LIST = re.compile(r'\d+\s*[-－—]\s*\d+(?:\s*[,，、]\s*\d+\s*[-－—]\s*\d+)*\s*[。.]?')
SUBMISSION_SLOT = re.compile(r'(?:(?:第[一二三四五六七八九十百零两\d]+[周章节次])|本周)?\s*作业\s*(?:请)?\s*提交(?:在)?(?:这里|此处)[。！!]?')
ANCHOR = re.compile(r'^(?:习题|题号|页码|作业范围|本次要求|本周要求|本次作业|本周作业|完成|解答|求解|证明|计算|阅读|预习|复习|第[一二三四五六七八九十百\d]+[章节页题])')
EXCEPTION = re.compile(r'本次|本周|新增|改为|额外|另外|例外|除外|注意|仅需|不再|特别|补做')
CONCRETE = re.compile(r'习题|题号|页码|第[一二三四五六七八九十百\d]+[章节页题]|\d+\s*[-－—]\s*\d+')
GENERIC = [re.compile(x) for x in (
    r'实验报告要求\s*[:：]?$',
    r'实验报告包括内容\s*[:：]\s*实验步骤[、，,]\s*实验数据整理[、，,]\s*数据处理[、，,]\s*原始数据[（(]有教师签字[）)][。.]?$',
    r'实验报告提交\s*[:：]请各位同学将写好的实验报告扫描或拍照后[，,]制成[pP][dD][fF]格式[，,]文件统一命名为\s*[:：]班级[-－]姓名[-－]实验名称[，,]然后上传到北化在线[。.]?$',
    r'(?:补充\s*[:：])?若实验报告内容不完整.*',
    r'(?:此外[，,]\s*)?原始数据中若缺少教师签字.*',
    r'请各位同学将写好的实验报告扫描或拍照.*', r'文件统一命名为\s*[:：].*',
    r'请各位同学按时提交实验报告.*', r'对于卡点提交导致报告无法正常上传的情况.*',
    r'提交图片[。.]?$', r'一页作为一张照片[，,].*',
    r'扫描推荐使用.*扫描软件.*', r'无论是计算题.*求解(?:过程|思路).*',
    r'一定要在截止时间之前完成作业提交.*', r'晚交作业超过\d+次.*')]


def _pieces(blocks):
    for block in blocks or []:
        if block.get('type') == 'paragraph':
            yield block.get('id'), block.get('text') or ''.join(
                s.get('text', '') for s in block.get('segments', []))
        elif block.get('type') == 'list':
            for item in block.get('items', []):
                yield from _pieces(item.get('blocks', []))
        elif block.get('type') == 'table':
            for row in block.get('cells', []):
                for cell in row:
                    if cell.get('blocks'):
                        yield from _pieces(cell['blocks'])
                    elif cell.get('text'):
                        yield block.get('id'), cell['text']


def _has_visual(blocks):
    for b in blocks or []:
        if b.get('type') in ('image', 'attachment', 'table'):
            return True
        if _has_visual(b.get('segments', [])):
            return True
        if any(_has_visual(it.get('blocks', [])) for it in b.get('items', [])):
            return True
        if any(_has_visual(c.get('blocks', [])) for row in b.get('cells', []) for c in row):
            return True
    return False


def summarize(item):
    """Recompute from the current body; never trust a saved/previous summary."""
    result = {'status': 'unavailable', 'text': '', 'excerpts': [],
              'rule_version': RULE_VERSION, 'source_version': item.get('source_version'),
              'reason': '正文尚未就绪'}
    course = str(item.get('course') or '').strip()
    if course.startswith('大学物理实验'):
        result.update(status='reference_only', text='详细要求见北化在线', origin='course_rule',
                      reason='本课程每周要求不同，统一提醒查看北化在线完整要求')
        return result
    if (not item.get('content_ready') or item.get('content_stale')
            or item.get('content_error') or item.get('content_identity_stale')):
        result['reason'] = '正文缺失、过期或获取失败，尚不能确认作业内容'
        return result
    if item.get('content_review_stale'):
        result.update(status='needs_review', reason='原文已更新，自动提醒查看北化在线；可按需重新确认正文')
        return result
    review = item.get('content_review')
    if review and review.get('source_version') == item.get('source_version'):
        result.update(status='ready' if review['mode'] == 'include' else 'generic_only',
                      text=review.get('text', ''), origin='manual',
                      reason='已人工确认推送正文' if review['mode'] == 'include'
                      else '已人工确认不推送正文，仍保留标题和截止时间')
        return result
    blocks = item.get('content_blocks') or []
    pieces = list(_pieces(blocks))
    if not pieces and item.get('content_text'):
        pieces = [(None, item['content_text'])]
    if course.startswith('复变函数'):
        import content_pipeline
        images = list(content_pipeline._iter_images(blocks))
        if images and all(im.get('status') == 'ready' and im.get('asset_id') for im in images):
            result.update(status='images', text='', origin='course_rule',
                          asset_ids=list(dict.fromkeys(im['asset_id'] for im in images)),
                          reason='本课程正文图片按原图随提醒发送，无需人工确认')
            return result
    if course.startswith('普通物理') and not _has_visual(blocks):
        kept = []
        for block_id, text in pieces:
            for match in re.finditer(r'[^\n。；;]+[。；;]?', text):
                quote = match.group().strip()
                clean = re.sub(r'^\s*\d+[.、．)]\s*', '', quote).rstrip('；;')
                generic = (SUBMISSION_SLOT.fullmatch(quote) or
                    (not EXCEPTION.search(clean) and not CONCRETE.search(clean)
                     and any(p.fullmatch(clean) for p in GENERIC)))
                if quote and not generic:
                    kept.append({'block_id': block_id, 'start':match.start(),
                                 'end':match.end(), 'text':quote})
        if kept:
            result.update(status='ready', text='\n'.join(x['text'] for x in kept),
                          excerpts=kept, origin='course_rule', reason='去除已知通用提交说明，保留其余全部要求')
        else:
            result.update(status='generic_only', reason='仅通用提交说明，没有具体任务')
        return result
    # The user confirmed that every textual requirement in this course matters.
    # Keep wording, internal spaces, command arguments and list numbering intact.
    if str(item.get('course') or '').strip() in FULL_TEXT_COURSES and not _has_visual(blocks):
        import content_pipeline
        raw = content_pipeline.flatten_blocks_text(blocks) if blocks else item.get('content_text', '')
        text = '\n'.join(line.strip() for line in raw.splitlines() if line.strip())
        if text:
            excerpts = []
            for block_id, source in pieces:
                for match in re.finditer(r'[^\r\n]+', source):
                    if match.group().strip():
                        excerpts.append({'block_id': block_id, 'start': match.start(),
                                         'end': match.end(), 'text': match.group().strip()})
            result.update(status='ready', text=text, excerpts=excerpts, origin='course_rule',
                          reason='本课程保留全部文字要求，仅清理行首行尾空白及空行')
            return result
    selected, unknown, generic = [], [], 0
    for block_id, text in pieces:
        # Split sentences without rewriting their contents; offsets refer to the
        # original paragraph, even when boilerplate and exercises share a block.
        for match in re.finditer(r'[^\n。；;]+[。；;]?', text):
            quote = match.group().strip()
            if not quote:
                continue
            clean = re.sub(r'^\s*\d+[.、．)]\s*', '', quote).rstrip('；;')
            if QUESTION_LIST.fullmatch(quote):
                selected.append({'block_id': block_id, 'start': match.start(),
                                 'end': match.end(), 'text': quote})
            elif ANCHOR.search(clean) and re.search(r'\d|第[一二三四五六七八九十百]+[章节页题]', clean):
                selected.append({'block_id': block_id, 'start': match.start(),
                                 'end': match.end(), 'text': quote})
            elif SUBMISSION_SLOT.fullmatch(quote):
                generic += 1
            elif (not EXCEPTION.search(clean) and not CONCRETE.search(clean)
                  and any(p.fullmatch(clean) for p in GENERIC)):
                generic += 1
            else:
                unknown.append(quote)
    result['excerpts'] = selected
    visual = _has_visual(blocks)
    if selected and not unknown and not visual:
        text = '\n'.join(x['text'] for x in selected)
        # Never cut a long exercise list mid-question to fit a message.
        if len(text) <= 1200:
            result.update(status='ready', text=text, reason='明确作业信息，逐段引自当前原文')
            return result
    if generic and not selected and not unknown and not visual:
        result.update(status='generic_only', reason='仅识别到通用提交说明，没有具体题目')
    else:
        result.update(status='needs_review', reason='自动提醒查看北化在线，不影响定时发送；可按需调整推送正文')
    return result
