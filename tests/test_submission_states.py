"""Submission status must survive the per-course SQLite commit boundary."""
from datetime import datetime, timedelta

from helpers import IsolatedCase
import cache_store
import hub


class SubmissionStateTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.use_explicit_account('submission-fixture')
        cache_store.begin_scan('status-scan', mode='deadline-only', expected_lids=['1'],
                               tests_requested=True, course_index_version='fixture')
        self.deadline = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
        self.homeworks = [
            {'hwtid':'done', 'title':'已提交', 'deadline':self.deadline,
             'submit_url':'https://course.buct.edu.cn/submit', 'result_url':'https://course.buct.edu.cn/result'},
            {'hwtid':'todo', 'title':'未提交', 'deadline':self.deadline,
             'submit_url':'https://course.buct.edu.cn/submit'},
            {'hwtid':'unknown', 'title':'入口不完整', 'deadline':self.deadline},
        ]
        cache_store.commit_course_scan('status-scan', lid='1', course='状态课程',
            homeworks=self.homeworks, tests=[{'test_id':'test', 'title':'未做测试',
            'deadline':self.deadline, 'start_url':'https://course.buct.edu.cn/start'}],
            homework_status='ok', tests_status='ok', index_version='fixture')

    def test_scan_commit_persists_status_before_final_json_processing(self):
        with cache_store.connection() as connection:
            values = {row['platform_id']: (row['submit_state'],row['submitted'])
                      for row in connection.execute('SELECT platform_id,submit_state,submitted FROM assignments')}
        self.assertEqual(values['done'], ('submitted',1))
        self.assertEqual(values['todo'], ('unsubmitted',0))
        self.assertEqual(values['unknown'], ('unknown',None))
        self.assertEqual(values['test'], ('unsubmitted',0))

    def test_old_null_status_cache_is_readable_without_rewriting_the_database(self):
        with cache_store.connection(write=True) as connection:
            connection.execute('UPDATE assignments SET submit_state=NULL,submitted=NULL')
        items = {item['platform_id']:item for item in cache_store.load_items()}
        self.assertEqual(items['done']['submit_state'], 'submitted')
        self.assertIs(items['done']['submitted'], True)
        self.assertEqual(items['todo']['submit_state'], 'unsubmitted')
        self.assertEqual(items['unknown']['submit_state'], 'unknown')
        with cache_store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM assignments WHERE submit_state IS NULL').fetchone()[0],4)

    def test_list_model_exposes_status_and_boolean_without_filtering_submitted_rows(self):
        rows = hub.build_rows(cache_store.load_items(),[],7)
        self.assertEqual(len(rows),4)
        values = {row['title']:row for row in rows}
        self.assertEqual(values['已提交']['submit_state'],'submitted')
        self.assertIs(values['已提交']['submitted'],True)
        self.assertEqual(values['未提交']['submit_state'],'unsubmitted')
