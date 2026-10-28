import time
from unittest.mock import Mock
from helpers import FakePage, IsolatedCase
import scrape

BASE='https://course.buct.edu.cn'
DIRECT=BASE+'/meol/jpk/course/layout/newpage/index.jsp?courseId=123'
GATEWAY=BASE+'/meol/lesson/enter_course.jsp?lid=123&t=hw'

class CourseEntryTests(IsolatedCase):
    def page(self, *, final=DIRECT, text='高等数学 课程作业 在线测试', error=False):
        page=FakePage(text=text,url='about:blank')
        def goto(url,**kwargs):
            page.gotos.append(url)
            if url==DIRECT:
                if error:raise TimeoutError('fixture timeout')
                page.url=final
            else:page.url=url
        page.goto=Mock(side_effect=goto)
        return page

    def test_direct_entry_requires_native_path_id_name_and_menu(self):
        page=self.page()
        self.assertTrue(scrape.enter_course_page(page,'123','高等数学',time.monotonic()+20))
        self.assertEqual(page.gotos,[DIRECT])

    def test_unconfirmed_landing_falls_back_to_gateway(self):
        cases=[
            {'final':DIRECT.replace('courseId=123','courseId=999')},
            {'final':DIRECT+'&lid=999'},
            {'final':DIRECT.replace('course.buct.edu.cn','evil.example')},
            {'final':DIRECT.replace('https:','http:')},
            {'final':DIRECT.replace('course.buct.edu.cn','course.buct.edu.cn:444')},
            {'final':DIRECT.replace('layout/newpage/index.jsp','other.jsp')},
            {'text':'其他课程 课程作业'}, {'text':'高等数学'}, {'error':True}]
        for case in cases:
            with self.subTest(case=case):
                page=self.page(**case)
                self.assertFalse(scrape.enter_course_page(page,'123','高等数学',time.monotonic()+20))
                self.assertEqual(page.gotos,[DIRECT,GATEWAY])

    def test_login_landing_stops_without_gateway_replay(self):
        page=self.page(final='https://sidportal.buct.edu.cn/login')
        with self.assertRaisesRegex(RuntimeError,'认证上下文失效'):
            scrape.enter_course_page(page,'123','高等数学',time.monotonic()+20)
        self.assertEqual(page.gotos,[DIRECT])

    def test_disabled_direct_entry_uses_legacy_path(self):
        self.write_config({'scan':{'direct_course_entry':False}})
        page=self.page()
        self.assertFalse(scrape.enter_course_page(page,'123','高等数学',time.monotonic()+20))
        self.assertEqual(page.gotos,[GATEWAY])

    def test_unknown_name_or_non_numeric_id_uses_gateway(self):
        page=self.page()
        self.assertFalse(scrape.enter_course_page(page,'123','',time.monotonic()+20))
        self.assertEqual(page.gotos,[GATEWAY])

    def test_fallback_does_not_reset_course_budget(self):
        page=self.page(text='其他课程')
        with self.assertRaisesRegex(RuntimeError,'预算'):
            scrape.enter_course_page(page,'123','高等数学',time.monotonic()-1)
        self.assertEqual(page.gotos,[DIRECT])
