import base64
import hashlib
import io
import json
import time
import sys
from types import SimpleNamespace
from unittest.mock import patch
from helpers import IsolatedCase
from browser_fixture_server import png_bytes
import cache_store
import content_pipeline as cp
import content_summary as cs
import image_delivery as delivery
import notify
import weekly
import hub


class CoursePolicies(IsolatedCase):
    def body(self, course, html):
        return {'course':course, 'title':'本周作业', 'deadline':'2099-01-01 23:59',
                'content_ready':True, 'source_version':'v1',
                'content_blocks':cp.parse_blocks(html, 'https://course.buct.edu.cn/')}

    def test_lab_always_has_portal_note_even_when_unavailable(self):
        for text in ('<p>所有数据分别作不确定度分析。</p>', '<p>提交图片</p>'):
            x=self.body('大学物理实验(II)',text)
            x.update(content_stale=True, content_error='failed')
            self.assertEqual(cs.summarize(x)['status'],'reference_only')
            self.assertIn('详细要求见北化在线',weekly.format_message([x],7))
            self.assertNotIn('不确定度分析',weekly.format_message([x],7))

    def test_physics_preserves_summary_assignment_and_exercises_without_boilerplate(self):
        x=self.body('普通物理(Ⅱ)', '<p>1. 提交图片</p><p>本周作业包括两部分：</p>'
          '<p>第一部分是磁学知识点总结，包含定理、公式。</p><p>第二部分是习题。</p><p>习题：8-27，9-8。</p>')
        s=cs.summarize(x)
        self.assertEqual(s['status'],'ready')
        self.assertIn('磁学知识点总结',s['text']);self.assertIn('8-27，9-8',s['text'])
        self.assertNotIn('提交图片',s['text'])
        self.assertIn('详情参见北化在线',weekly.format_message([x],7))

    def test_unknown_content_needs_no_manual_gate(self):
        x=self.body('课程','<p>陌生要求。</p>')
        msg=weekly.format_message([x],7)
        self.assertIn('本周作业',msg);self.assertIn('2099-01-01',msg)
        self.assertIn('详细要求见北化在线',msg);self.assertNotIn('待确认',msg)

    def test_class_duplicate_does_not_hide_personal_failure(self):
        from datetime import datetime, timedelta
        x=self.body('课程','<p>陌生要求。</p>')
        x['deadline']=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        with patch.object(weekly,'load_fresh_publish_cache',return_value=({'homeworks':[x]}, {'publishable':True})), \
             patch.object(weekly,'load_manual',return_value=[]),patch.object(weekly,'_elective_lids_from_disk',return_value=set()), \
             patch.object(hub,'ROOT',self.root),patch.object(hub.fetch_log,'log_fetch'), \
             patch.object(notify,'load_config',return_value={'channel':'wecom'}), \
             patch.object(notify,'personal_ready',return_value=(True,'')), \
             patch.object(notify,'recent_duplicate',side_effect=[{'time':'today','source':'weekly'},None]), \
             patch.object(notify,'log_push'),patch.object(notify,'dispatch',return_value=False) as send:
            result=hub.run_publish(7,False,'wecom')
        self.assertEqual(send.call_count,1)
        self.assertEqual(send.call_args.kwargs['audience'],'personal')
        self.assertFalse(result['ok']);self.assertFalse(result['deduped'])


class OriginalImageDelivery(IsolatedCase):
    def setUp(self):
        super().setUp();self.use_explicit_account('image-owner')
        self.raw=png_bytes(4,3);self.aid=hashlib.sha256(self.raw).hexdigest()
        assetdir=cache_store.cache_settings()['assets_dir'];assetdir.mkdir(parents=True)
        self.path=assetdir/(self.aid+'.png');self.path.write_bytes(self.raw)
        self.display=assetdir/'display.png';self.display.write_bytes(png_bytes(2,2))
        cache_store.begin_scan('s',mode='test',expected_lids=['1'],tests_requested=False,course_index_version='i')
        cache_store.commit_course_scan('s',lid='1',course='复变函数与积分变换（A）',
            homeworks=[{'hwtid':'1','title':'图片任务','deadline':'2099-01-01 23:59'}],
            tests=[],homework_status='ok',tests_status='disabled',index_version='i')
        self.key=cache_store.load_items()[0]['item_key']
        cache_store.save_content(self.key,{'status':'ready','source_version':'v1','blocks':[
            {'id':'p1','type':'paragraph','text':'','segments':[
                {'type':'image','asset_id':self.aid,'status':'ready'}]}],
            'asset_ids':[self.aid],'assets':[{'asset_id':self.aid,'sha256':self.aid,
                'path':str(self.display),'display_path':str(self.display),'original_path':str(self.path),
                'mime':'image/png','width':4,'height':3,'bytes':len(self.raw),
                'source_url':'https://course.buct.edu.cn/meol/common/openfile.jsp?id=unit'}]})
        self.item=cache_store.get_item(self.key)
        self.images=delivery.collect([self.item])
        self.cfg={'wecom_webhook':'https://unit.invalid/class', 'dedup_window_sec':300,
                  'personal':{'enabled':True,'wecom_webhook':'https://unit.invalid/personal'},
                  '_image_manifest':self.images}
        self.ledger=patch.object(notify,'PART_LOG',self.root/'parts.jsonl');self.ledger.start();self.addCleanup(self.ledger.stop)

    def test_original_bytes_not_display_or_crop_and_audience_filtering(self):
        self.assertEqual(delivery.original_bytes(self.images[0]),self.raw)
        self.assertEqual(cs.summarize(self.item)['status'],'images')
        group, personal=weekly.split_items([self.item],{'1'})
        self.assertEqual(delivery.collect(group),[]);self.assertEqual(delivery.collect(personal),self.images)
        self.assertIn('作业原图随提醒发送',weekly.format_message([self.item],7))

    def test_changed_stale_missing_or_wrong_scope_images_are_rejected(self):
        bad=dict(self.images[0],source_version='old')
        with self.assertRaises(ValueError):delivery.original_bytes(bad)
        with cache_store.connection(write=True) as con:
            con.execute('UPDATE assignment_contents SET source_checked_at=1 WHERE item_key=?',(self.key,))
        with self.assertRaises(ValueError):delivery.original_bytes(self.images[0])
        self.use_explicit_account('another-owner')
        with self.assertRaises(ValueError):delivery.original_bytes(self.images[0])

    def test_modified_file_is_not_sent(self):
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError):delivery.original_bytes(self.images[0])

    def test_inline_places_original_image_between_assignments_in_one_message(self):
        other={'course':'普通物理', 'title':'下一项作业', 'deadline':'2099-01-02 23:59'}
        text=weekly.format_message([self.item, other],7)
        requests=[]
        def response(req, **kwargs):
            requests.append(req)
            return io.BytesIO(self.raw if req.get_method()=='GET' else b'{"errcode":0}')
        with patch.object(notify.urllib.request,'urlopen',side_effect=response),patch.object(notify.time,'sleep'):
            self.assertTrue(notify.dispatch(text,'wecom',self.cfg))
        posts=[json.loads(req.data) for req in requests if req.get_method()=='POST']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0]['msgtype'],'markdown_v2')
        body=posts[0]['markdown_v2']['content']
        self.assertLess(body.index('图片任务'),body.index('![作业原图]'))
        self.assertLess(body.index('![作业原图]'),body.index('下一项作业'))
        self.assertNotIn('作业原图随提醒发送',body)
        self.assertLessEqual(len(body.encode('utf-8')),4096)

    def test_inline_unavailable_public_url_falls_back_to_original_upload(self):
        text=weekly.format_message([self.item],7)
        requests=[]
        def response(req, **kwargs):
            requests.append(req)
            return io.BytesIO(b'login page' if req.get_method()=='GET' else b'{"errcode":0}')
        with patch.object(notify.urllib.request,'urlopen',side_effect=response),patch.object(notify.time,'sleep'):
            self.assertTrue(notify.dispatch(text,'wecom',self.cfg))
        posts=[json.loads(req.data) for req in requests if req.get_method()=='POST']
        self.assertEqual([p['msgtype'] for p in posts],['text','text','image'])
        self.assertEqual(base64.b64decode(posts[-1]['image']['base64']),self.raw)

    def test_http_payload_is_byte_exact_and_personal_target_ignores_class_env(self):
        requests=[]
        timeouts=[]
        def response(req,**kwargs):
            requests.append(req)
            timeouts.append(kwargs['timeout'])
            obj=io.BytesIO(b'{"errcode":0}')
            return obj
        with patch.object(notify.urllib.request,'urlopen',side_effect=response), \
             patch.object(notify.time,'sleep'),patch.dict('os.environ',{notify.WECOM_ENV:'https://unit.invalid/env-class'}):
            self.assertTrue(notify.dispatch('提醒','wecom',self.cfg,'personal'))
        self.assertTrue(all(r.full_url=='https://unit.invalid/personal' for r in requests))
        payloads=[json.loads(r.data) for r in requests]
        self.assertEqual([x['msgtype'] for x in payloads],['text','text','image'])
        self.assertEqual(timeouts,[20,20,60])
        image=payloads[-1]['image']
        self.assertEqual(base64.b64decode(image['base64']),self.raw)
        self.assertEqual(image['md5'],hashlib.md5(self.raw).hexdigest())

    def test_partial_failure_retries_only_unsent_image(self):
        calls=[]
        def send(text,cfg,**kwargs):
            calls.append(kwargs.get('image_bytes'))
            return len(calls)!=3
        with patch.object(notify,'send_wecom',side_effect=send),patch.object(notify.time,'sleep'):
            self.assertFalse(notify.dispatch('提醒','wecom',self.cfg))
            self.assertTrue(notify.dispatch('提醒','wecom',self.cfg))
            self.assertTrue(notify.dispatch('提醒','wecom',self.cfg))
        self.assertEqual(calls,[None,None,self.raw,self.raw])
        self.assertFalse((self.root/'push_log.jsonl').exists(),'component successes must not mark whole task complete')

    def test_none_never_sends_and_long_text_stays_within_payload_limit(self):
        with patch.object(notify,'send_wecom',side_effect=AssertionError('forbidden')):
            self.assertTrue(notify.dispatch('演练','none',self.cfg))
        text='中文😀\n'*1000
        chunks=delivery.text_chunks(text)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(x.encode('utf-8'))<=1800 for x in chunks))

    def test_unconfigured_personal_target_cannot_fall_back_to_class(self):
        cfg={**self.cfg, 'personal':{'enabled':True, 'wecom_webhook':''}}
        with patch.object(notify,'send_wecom',side_effect=AssertionError('wrong group')):
            self.assertFalse(notify.dispatch('提醒','wecom',cfg,'personal'))

    def test_dedup_uses_actual_env_target_and_personal_cannot_equal_it(self):
        with patch.dict('os.environ',{notify.WECOM_ENV:'https://unit.invalid/test-one'}):
            first=notify.dedup_target('wecom',self.cfg)
        with patch.dict('os.environ',{notify.WECOM_ENV:'https://unit.invalid/test-two'}):
            second=notify.dedup_target('wecom',self.cfg)
        self.assertNotEqual(first,second)
        with patch.dict('os.environ',{notify.WECOM_ENV:'https://unit.invalid/personal'}):
            self.assertFalse(notify.personal_ready(self.cfg)[0])

    def test_no_send_preview_exposes_images_without_delivery(self):
        from datetime import datetime, timedelta
        self.item['deadline']=(datetime.now()+timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        with patch.object(weekly,'load_fresh_publish_cache',return_value=({'homeworks':[self.item]}, {'publishable':True})), \
             patch.object(weekly,'load_manual',return_value=[]),patch.object(weekly,'_elective_lids_from_disk',return_value=set()), \
             patch.object(hub,'ROOT',self.root),patch.object(hub.fetch_log,'log_fetch'), \
             patch.object(notify,'dispatch',side_effect=AssertionError('forbidden')):
            result=hub.run_publish(7,True,'none')
        self.assertEqual(result['images'][0]['asset_id'],self.aid)
        self.assertEqual(result['images'][0]['item_key'],self.key)
        self.assertFalse(result['sent'])

    def test_scheduled_sender_passes_same_image_plan_to_notify_cli(self):
        with patch.object(weekly,'ROOT',self.root), \
             patch.object(weekly.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run:
            self.assertEqual(weekly.send_one('提醒','class',images=self.images)[0],0)
        cmd=run.call_args.args[0]
        self.assertIn('--image-manifest',cmd)
        self.assertEqual(json.loads((self.root/'last_images.json').read_text(encoding='utf-8')),self.images)
        self.assertEqual(run.call_args.kwargs['timeout'],300)

    def test_notify_cli_none_does_not_send_any_bundle_component(self):
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps(self.images),encoding='utf-8')
        with patch.object(sys,'argv',['notify.py','--message','演练','--channel','none','--image-manifest',str(manifest)]), \
             patch.object(notify,'load_config',return_value=self.cfg),patch.object(notify,'PUSH_LOG',self.root/'push.jsonl'), \
             patch.object(notify,'recent_duplicate',return_value=None), \
             patch.object(notify.urllib.request,'urlopen',side_effect=AssertionError('delivery forbidden')):
            self.assertEqual(notify.main(),0)
        self.assertFalse((self.root/'parts.jsonl').exists())
