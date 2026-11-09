/* 演示数据：均为虚构，不连接后端 */
const NOW = new Date('2026-11-08T10:00:00+08:00');
const WD = ['周日','周一','周二','周三','周四','周五','周六'];
const SRC = { platform:'平台作业', quiz:'在线测试', manual:'手动补录' };

const items = [
  { id:1, course:'复变函数与积分变换', title:'第八周习题', date:'2026-11-08', time:'23:59', source:'platform', submitted:true,
    summary:'完成习题 5 中第 5、6、9 题的指定小题，带 * 为选做。', probs:['5 (1,3,4,5,6,7)','6 (3)','9 (1,5,6,7*)'], probLabel:'习题5',
    image:{ src:'img/homework.png', w:1511, h:334, caption:'课堂板书照片 · 11月6日' },
    origin:'各位同学：本周作业为习题 5 —— 第 5 题 (1)(3)(4)(5)(6)(7)，第 6 题 (3)，第 9 题 (1)(5)(6)(7*)，带 * 为选做。拍照或扫描合并为一个 PDF 后在本页面提交，截止时间 2026 年 11 月 8 日 23:59。' },
  { id:2, course:'普通物理（Ⅱ）', title:'第六章习题', date:'2026-11-09', time:'23:59', source:'platform',
    summary:'完成 6-2、6-5、6-8，写出计算过程。', probs:['6-2','6-5','6-8'], probLabel:'习题',
    origin:'第六章 静电场中的导体与电介质 课后习题：6-2、6-5、6-8。请写明已知量、所用公式与计算过程，结果保留三位有效数字。' },
  { id:3, course:'大学物理实验（Ⅱ）', title:'光学实验预习', date:'2026-11-10', time:'09:00', source:'platform',
    summary:'详细要求见北化在线。',
    origin:'本周进行光学实验（分光计的调节与使用）。请于实验前完成预习报告，包括实验目的、原理、仪器与步骤，详细要求见附件《光学实验预习要求.pdf》，课堂上检查。' },
  { id:4, course:'马克思主义基本原理', title:'专题在线测试', date:'2026-11-12', time:'20:00', source:'quiz',
    summary:'在北化在线完成专题测试。',
    origin:'专题在线测试已发布，共 20 道题，限时 30 分钟，仅有一次作答机会，请合理安排时间。' },
  { id:5, course:'嵌入式系统创新应用与实践', title:'传感器数据采集', date:'2026-11-13', time:'23:59', source:'platform', elective:true,
    summary:'采集传感器数据，绘制时间曲线，提交源码与结果说明。',
    origin:'使用开发板采集温湿度或光照传感器数据，采样时长不少于 10 分钟，绘制数据随时间变化的曲线。提交源代码压缩包、曲线截图，以及不少于 300 字的结果说明。' },
  { id:6, course:'大学英语', title:'单元练习', date:'2026-11-14', time:'23:59', source:'manual',
    summary:'完成本单元练习，听力部分不要漏做。',
    origin:'（班级群消息 · 11月7日 18:42）同学们把 Unit 5 的单元练习做完，14 号晚上 23:59 之前提交，听力部分记得做。' },
];

/* 本学期课程表（虚构编号） */
const COURSES = [
  { code:'MAT20412', name:'复变函数与积分变换', teacher:'周老师', credit:3 },
  { code:'PHY10322', name:'普通物理（Ⅱ）', teacher:'林老师', credit:4 },
  { code:'PHY10541', name:'大学物理实验（Ⅱ）', teacher:'陈老师', credit:1.5 },
  { code:'MAR10031', name:'马克思主义基本原理', teacher:'王老师', credit:3 },
  { code:'CST30815', name:'嵌入式系统创新应用与实践', teacher:'许老师', credit:2 },
  { code:'ENG10213', name:'大学英语', teacher:'Ms. Liu', credit:2 },
  { code:'PED10303', name:'体育（三）', teacher:'赵老师', credit:1 },
  { code:'MAR10508', name:'形势与政策', teacher:'孙老师', credit:0.5 },
  { code:'GEN10102', name:'大学生职业生涯规划', teacher:'何老师', credit:1 },
  { code:'CHE20117', name:'化工安全概论', teacher:'郑老师', credit:1.5 },
];

const LISTS = {
  course: {
    label:'课程黑名单', icon:'ban', count:2, members:['PED10303','MAR10508'],
    desc:'整门课的作业和测试都不抓取，也不会出现在任何提醒里。',
    addVerb:'加入课程黑名单', addEffect:'该课程的作业和测试将不再抓取。',
    removeEffect:'该课程的作业和测试将恢复抓取。'
  },
  quiz: {
    label:'测试黑名单', icon:'file-x-2', count:1, members:['ENG10213'],
    desc:'作业照常抓取，只跳过这门课的在线测试。',
    addVerb:'加入测试黑名单', addEffect:'作业照常抓取，在线测试将被跳过。',
    removeEffect:'该课程的在线测试将恢复抓取。'
  },
  elective: {
    label:'选修课名单', icon:'user-round', count:1, members:['CST30815'],
    desc:'照常抓取，只进入个人提醒，不进入班级提醒。',
    addVerb:'加入选修课名单', addEffect:'作业仍会抓取，但只推送到个人群。',
    removeEffect:'该课程作业将重新进入班级提醒。'
  },
};

/* 手动作业编辑表单的示例内容 */
const MANUAL_SAMPLE = { course:'ENG10213', title:'单元练习', date:'2026-11-14', time:'23:59',
  note:'Unit 5 单元练习，听力部分不要漏做。来源：班级群 11月7日 18:42。' };
