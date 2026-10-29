import time
from unittest.mock import patch
from datetime import datetime, timedelta
from helpers import IsolatedCase
import content_summary as cs
import cache_store
import content_pipeline as cp
import hub
import weekly


def item(text, **extra):
    return dict(content_ready=True, content_blocks=cp.parse_blocks(text,'https://course.buct.edu.cn/'),
                source_version='v1', **extra)


class SummaryRules(IsolatedCase):
    def test_embedded_course_keeps_all_requirements_and_only_trims_line_edges(self):
        x = item('<pre>  课下作业一\n\n　 有四个按键 B1，B2，B3，B4。  \n'
                 '  电脑串口助手发送 meanwhile  blink flow，实现流水灯与闪烁灯。\n'
                 '\t提交图片\n 最后附上运行结果。 </pre>')
        x['course'] = '嵌入式系统创新应用与实践'
        summary = cs.summarize(x)
        expected = ('课下作业一\n有四个按键 B1，B2，B3，B4。\n'
                    '电脑串口助手发送 meanwhile  blink flow，实现流水灯与闪烁灯。\n提交图片\n最后附上运行结果。')
        self.assertEqual(summary['status'], 'ready')
        self.assertEqual(summary['text'], expected)
        self.assertEqual(summary['origin'], 'course_rule')
        msg = weekly.format_message([x], 7)
        self.assertIn(expected.splitlines()[2], msg)
        self.assertNotIn('最后附上运行结果', msg)
        self.assertIn('详情参见北化在线', msg)
        for quote in summary['excerpts']:
            block = next(b for b in x['content_blocks'] if b['id'] == quote['block_id'])
            self.assertEqual(block['text'][quote['start']:quote['end']].strip(), quote['text'])

    def test_embedded_course_retains_ordered_list_numbers_and_long_full_text(self):
        x = item('<ol start="3"><li>控制灯的方向</li><li>控制灯的速率</li></ol><p>'+('详细要求'*350)+'</p>')
        x['course'] = '嵌入式系统创新应用与实践'
        summary = cs.summarize(x)
        self.assertEqual(summary['status'], 'ready')
        self.assertTrue(summary['text'].startswith('3. 控制灯的方向\n4. 控制灯的速率'))
        self.assertTrue(summary['text'].endswith('详细要求'*350))

    def test_embedded_rule_does_not_override_review_or_other_course_safety(self):
        for extra in ('<img src="/task.png">', '<a href="/task.pdf">附件</a>', '<table><tr><td>要求</td></tr></table>'):
            x = item('<p>控制灯的方向</p>'+extra)
            x['course'] = '嵌入式系统创新应用与实践'
            self.assertEqual(cs.summarize(x)['status'], 'needs_review')
        x = item('<p>控制灯的方向</p>')
        x['course'] = '其他嵌入式课程'
        self.assertEqual(cs.summarize(x)['status'], 'needs_review')
        x['course'] = '嵌入式系统创新应用与实践'
        x['content_review'] = {'source_version':'v1', 'mode':'omit', 'text':''}
        self.assertEqual(cs.summarize(x)['origin'], 'manual')
        self.assertEqual(cs.summarize(x)['status'], 'generic_only')
        x['content_stale'] = True
        self.assertEqual(cs.summarize(x)['status'], 'unavailable')

    def test_long_body_message_keeps_first_three_lines_and_other_assignments(self):
        x = item(''.join('<p>习题：1-'+str(n)+'。</p>' for n in range(1, 5)))
        x.update(course='嵌入式', title='长任务', deadline='2099-01-02 23:59')
        other = item('<p>习题：6-9、6-10。</p>')
        other.update(course='电路', title='另一个任务', deadline='2099-01-02 23:59')
        full_text = cs.summarize(x)['text']
        for title in (None, '个人作业提醒'):
            msg = weekly.format_message([x, other], 7, title=title)
            for n in range(1, 4):
                self.assertIn('习题：1-'+str(n)+'。', msg)
            self.assertNotIn('习题：1-4。', msg)
            self.assertEqual(msg.count('……（详情参见北化在线）'), 1)
            self.assertIn('另一个任务', msg)
            self.assertIn('习题：6-9、6-10。', msg)
            self.assertIn('2099-01-02', msg)
        self.assertEqual(cs.summarize(x)['text'], full_text, 'source summary must remain complete')

    def test_three_line_or_shorter_body_is_not_marked_as_omitted(self):
        for count in (1, 2, 3):
            x = item(''.join('<p>习题：2-'+str(n)+'。</p>' for n in range(1, count+1)))
            msg = weekly.format_message([x], 7)
            self.assertNotIn('详情参见北化在线', msg)
            self.assertIn('习题：2-'+str(count)+'。', msg)

    def test_manual_confirmation_uses_same_limit_and_blank_lines_do_not_count(self):
        x = item('<p>完整原文。</p>')
        x['content_review'] = {'source_version':'v1', 'mode':'include',
                              'text':'第一项\n\n第二项\n  \n第三项\n第四项'}
        msg = weekly.format_message([x], 7)
        self.assertIn('第一项\n   第二项\n   第三项\n   ……（详情参见北化在线）', msg)
        self.assertNotIn('第四项', msg)
        self.assertIn('第四项', cs.summarize(x)['text'])

    def test_bare_circuit_question_numbers_are_source_backed(self):
        text = '6-9，6-10，7-6，7-8，7-12'
        x = item('<p>' + text + '</p>')
        r = cs.summarize(x)
        self.assertEqual(r['status'], 'ready')
        self.assertEqual(r['text'], text)
        self.assertEqual(r['excerpts'][0]['text'], text)
        self.assertIn(text, weekly.format_message([x], 7))

    def test_submission_slot_is_generic_but_concrete_exercises_are_not_hidden(self):
        for text in ('第一周作业提交这里', '第六周作业提交在这里。', '作业提交此处'):
            self.assertEqual(cs.summarize(item('<p>'+text+'</p>'))['status'], 'generic_only')
        for text in ('第一周作业提交这里，完成6-9、6-10', '6-9元', '6-9，6-10，另交报告'):
            self.assertEqual(cs.summarize(item('<p>'+text+'</p>'))['status'], 'needs_review')

    def test_physics_exercises_keep_all_numbers_and_drop_boilerplate(self):
        x=item('<p>1. 提交图片</p><p>2. 一页作为一张照片，正放，不要倒置。</p>'
            '<p>3. 扫描推荐使用扫描全能王等扫描软件。</p>'
            '<p>4. 无论是计算题，还是选择题、填空题，都需要给出具体的求解过程、求解思路；</p>'
            '<p>5. 一定要在截止时间之前完成作业提交；</p>'
            '<p>6. 晚交作业超过3次，会扣除平时成绩分！</p>'
            '<p>习题：10-11，10-13，10-15，10-16，10-17，10-18，10-20，10-21。</p>')
        r=cs.summarize(x)
        self.assertEqual(r['status'],'ready')
        self.assertEqual(r['text'],'习题：10-11，10-13，10-15，10-16，10-17，10-18，10-20，10-21。')
        self.assertEqual(r['source_version'],'v1')

    def test_lab_instructions_have_no_publishable_excerpt(self):
        r=cs.summarize(item('<p>实验报告要求：</p><p>实验报告包括内容：实验步骤、实验数据整理、数据处理、原始数据（有教师签字）</p>'
            '<p>补充：若实验报告内容不完整，将扣除相应分数，此外，原始数据中若缺少教师签字，则认为实验数据无效。</p>'
            '<p>实验报告提交：请各位同学将写好的实验报告扫描或拍照后，制成pdf格式，文件统一命名为：班级-姓名-实验名称，然后上传到北化在线。</p>'
            '<p>请各位同学按时提交实验报告，迟交实验报告的同学将被扣除相应分数。</p>'))
        self.assertEqual(r['status'],'generic_only');self.assertEqual(r['text'],'')

    def test_unknown_extra_requirement_prevents_partial_publish(self):
        r=cs.summarize(item('<p>习题：1-2、1-3。</p><p>另须提交实验记录。</p>'))
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['text'],'')

    def test_new_exception_inside_generic_paragraph_is_not_dropped(self):
        r=cs.summarize(item('<p>实验报告包括内容：本周新增第3题及误差分析。</p>'))
        self.assertEqual(r['status'],'needs_review')

    def test_lab_prefix_cannot_hide_a_real_task_without_numbers(self):
        for text in ('实验报告包括内容：绘制金属电阻率随温度变化曲线。',
                     '实验报告提交：附上测量数据和曲线。'):
            self.assertEqual(cs.summarize(item('<p>'+text+'</p>'))['status'],'needs_review')

    def test_images_and_attachments_require_review_even_next_to_exercises(self):
        for body in ('<img src="/question.png">','<a href="/sheet.pdf">题目附件</a>'):
            self.assertEqual(cs.summarize(item('<p>习题：1-2。</p>'+body))['status'],'needs_review')

    def test_unavailable_or_old_content_never_reuses_previous_summary(self):
        for fields in ({'content_stale':True},{'content_error':'timeout'},{'content_identity_stale':True}):
            x=item('<p>习题：1-2。</p>',**fields)
            x['content_summary']={'status':'ready','text':'旧的错误摘要'}
            self.assertEqual(cs.summarize(x)['status'],'unavailable')

    def test_mixed_paragraph_excerpts_preserve_source_offsets(self):
        x=item('<p>提交图片；习题：1-2、1-3。</p>')
        r=cs.summarize(x);self.assertEqual(r['status'],'ready')
        for quote in r['excerpts']:
            block=next(b for b in x['content_blocks'] if b['id']==quote['block_id'])
            self.assertEqual(block['text'][quote['start']:quote['end']].strip(),quote['text'])

    def test_separate_heading_does_not_guess_the_following_range(self):
        self.assertEqual(cs.summarize(item('<p>习题：</p><p>1-2、1-3</p>'))['status'],'needs_review')

    def test_long_exercise_list_is_not_cut(self):
        r=cs.summarize(item('<p>习题：'+','.join(str(i) for i in range(500))+'</p>'))
        self.assertEqual(r['status'],'needs_review');self.assertEqual(r['text'],'')

    def test_generic_body_does_not_remove_title_or_deadline_from_message(self):
        x=item('<p>提交图片</p><p>一页作为一张照片，正放，不要倒置。</p>')
        x.update(course='实验课程',title='本周实验报告',deadline='2099-01-02 23:59')
        msg=weekly.format_message([x],7)
        self.assertIn('本周实验报告',msg);self.assertIn('2099-01-02',msg)
        self.assertNotIn('正放，不要倒置',msg)

    def test_old_cached_excerpt_is_not_published_when_body_is_stale(self):
        x=item('<p>习题：1-2。</p>',content_stale=True)
        x.update(course='课程',title='本周任务',deadline='2099-01-02 23:59',
            content_summary={'status':'ready','text':'错误旧摘要'})
        msg=weekly.format_message([x],7)
        self.assertNotIn('错误旧摘要',msg);self.assertNotIn('习题：1-2',msg)

    def test_preview_and_weekly_formatter_use_same_current_summary(self):
        self.use_explicit_account('summary-fixture')
        version='idx';dl=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        cache_store.begin_scan('s',mode='test',expected_lids=['1'],tests_requested=False,course_index_version=version)
        cache_store.commit_course_scan('s',lid='1',course='物理',homeworks=[{'hwtid':'1','title':'本周作业','deadline':dl}],tests=[],homework_status='ok',tests_status='disabled',index_version=version)
        cache_store.update_scan('s',status='complete',finished_at=time.time())
        key=cache_store.load_items()[0]['item_key']
        x=item('<p>提交图片</p><p>习题：1-2、1-3。</p>')
        cache_store.save_content(key,{'status':'ready','source_version':'v1','blocks':x['content_blocks'],'text':'提交图片\n习题：1-2、1-3。'})
        loaded=cache_store.get_item(key)
        row=hub.build_rows([loaded],[],7)[0]
        self.assertEqual(row['content_summary']['text'],'习题：1-2、1-3。')
        msg=weekly.format_message(weekly.merge_and_filter({'homeworks':[loaded]},[],7),7)
        self.assertIn(row['content_summary']['text'],msg);self.assertNotIn('提交图片',msg)
        with patch.object(weekly,'load_fresh_publish_cache',return_value=({'homeworks':[loaded],'tests':[]},{'publishable':True,'age_sec':0})), \
             patch.object(hub,'ROOT',self.root),patch.object(weekly,'load_manual',return_value=[]), \
             patch.object(weekly,'_elective_lids_from_disk',return_value=set()), \
             patch.object(hub.fetch_log,'log_fetch'), \
             patch.object(weekly,'send_one',side_effect=AssertionError('delivery forbidden')):
            preview=hub.run_publish(7,True,'none')
        self.assertIn(row['content_summary']['text'],preview['message'])
        self.assertFalse(preview['sent'])
        cache_store.save_content(key,{'status':'ready','source_version':'v2','blocks':cp.parse_blocks('<p>习题：2-1。</p>','https://course.buct.edu.cn/'),'text':'习题：2-1。'})
        current=cache_store.get_item(key)
        self.assertNotIn('1-2',current['content_summary']['text'])
        self.assertEqual(current['content_summary']['source_version'],current['source_version'])


class ManualContentReview(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.use_explicit_account('review-owner')
        cache_store.begin_scan('s', mode='test', expected_lids=['1'], tests_requested=False, course_index_version='i')
        cache_store.commit_course_scan('s', lid='1', course='电路',
            homeworks=[{'hwtid':'1', 'title':'本周任务', 'deadline':'2099-01-02 23:59'}],
            tests=[], homework_status='ok', tests_status='disabled', index_version='i')
        self.key = cache_store.load_items()[0]['item_key']
        self.write_body('v1')

    def write_body(self, version):
        cache_store.save_content(self.key, {'status':'ready', 'source_version':version,
            'blocks':cp.parse_blocks('<p>须整理测量结果和曲线。</p>', 'https://course.buct.edu.cn/'),
            'text':'须整理测量结果和曲线。'})

    def save(self, mode, text='', version=None):
        cache_store.save_content_review(self.key, expected_version=version or
            cache_store.get_item(self.key)['source_version'], mode=mode, text=text)

    def test_confirmation_is_persistent_and_used_by_formatter_and_list(self):
        self.save('include', '绘制曲线并计算误差')
        cache_store.init_db()
        current = cache_store.get_item(self.key)
        summary = current['content_summary']
        self.assertEqual(summary['origin'], 'manual')
        self.assertEqual(summary['text'], '绘制曲线并计算误差')
        self.assertIn(summary['text'], weekly.format_message([current], 7))
        self.assertNotIn('须整理测量结果', weekly.format_message([current], 7))
        row = hub.build_rows([current], [], 99999)[0]
        self.assertEqual(row['content_summary'], summary)

    def test_omit_preserves_reminder_and_auto_restores_review(self):
        self.save('omit', '不能带入推送的文字')
        current = cache_store.get_item(self.key)
        msg = weekly.format_message([current], 7)
        self.assertIn('本周任务', msg)
        self.assertIn('2099-01-02', msg)
        self.assertNotIn('须整理', msg)
        self.assertNotIn('待确认', msg)
        self.assertEqual(current['content_summary']['status'], 'generic_only')
        self.save('auto')
        self.assertEqual(cache_store.get_item(self.key)['content_summary']['status'], 'needs_review')
        self.assertNotIn('content_review', cache_store.get_item(self.key))

    def test_same_source_refresh_keeps_confirmation_but_changes_invalidate_it(self):
        self.save('include', '人工确认文本')
        self.write_body('v1')
        self.assertEqual(cache_store.get_item(self.key)['content_summary']['text'], '人工确认文本')
        self.write_body('v2')
        current = cache_store.get_item(self.key)
        self.assertTrue(current['content_review_stale'])
        self.assertEqual(current['content_summary']['status'], 'needs_review')
        self.assertNotIn('人工确认文本', weekly.format_message([current], 7))
        with self.assertRaises(cache_store.VersionConflict):
            self.save('include', '旧页面确认', version='v1')
        self.write_body('v1')
        self.assertTrue(cache_store.get_item(self.key)['content_review_stale'], 'reverting source cannot revive old decisions')
        self.save('omit')
        self.assertEqual(cache_store.get_item(self.key)['content_summary']['origin'], 'manual')

    def test_expired_content_and_failed_fetch_cannot_be_confirmed_or_published(self):
        self.save('include', '人工确认文本')
        with cache_store.connection(write=True) as con:
            con.execute('UPDATE assignment_contents SET source_checked_at=? WHERE item_key=?', (1, self.key))
        with self.assertRaises(cache_store.VersionConflict):
            self.save('omit')
        current = cache_store.get_item(self.key)
        self.assertEqual(current['content_summary']['status'], 'unavailable')
        self.assertNotIn('人工确认文本', weekly.format_message([current], 7))
        self.write_body('v1')
        with cache_store.connection(write=True) as con:
            con.execute("UPDATE assignment_contents SET status='stale_error',error='failed' WHERE item_key=?", (self.key,))
        with self.assertRaises(cache_store.VersionConflict):
            self.save('include', '失败的原文')

    def test_other_scope_and_old_identity_cannot_read_or_save_confirmation(self):
        self.save('include', '私有确认文本')
        self.use_explicit_account('other-owner')
        self.assertIsNone(cache_store.get_item(self.key))
        with self.assertRaises(cache_store.VersionConflict):
            cache_store.save_content_review(self.key, expected_version='v1', mode='omit')
        self.use_explicit_account('review-owner')
        cache_store.bind_identity('first-user')
        cache_store.bind_identity('second-user', allow_change=True)
        current = cache_store.get_item(self.key)
        self.assertNotIn('content_review', current)
        self.assertNotIn('私有确认文本', weekly.format_message([current], 7))
        with self.assertRaises(cache_store.VersionConflict):
            cache_store.save_content_review(self.key, expected_version='v1', mode='omit')

    def test_invalid_confirmation_is_rejected_without_overwriting_previous(self):
        self.save('include', '保留原确认')
        for mode, text in (('wrong', ''), ('include',''), ('include','x'*1201), ('include', [])):
            with self.assertRaises(ValueError):
                self.save(mode, text)
        self.assertEqual(cache_store.get_item(self.key)['content_summary']['text'], '保留原确认')
