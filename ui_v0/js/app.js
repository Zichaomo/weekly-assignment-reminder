
function parts(it){
  const [y,m,d] = it.date.split('-').map(Number);
  const offset = (Date.UTC(y,m-1,d) - Date.UTC(2026,10,8)) / 864e5;
  const wd = WD[new Date(Date.UTC(y,m-1,d)).getUTCDay()];
  const ms = new Date(`${it.date}T${it.time}:00+08:00`) - NOW;
  const h = Math.floor(ms/36e5), mi = Math.floor(ms%36e5/6e4);
  const remain = h < 24 ? `${h} 小时 ${mi} 分` : `${Math.floor(h/24)} 天 ${h%24} 小时`;
  const remainShort = h < 24 ? `剩 ${h} 小时` : `剩 ${Math.floor(h/24)} 天 ${h%24} 时`;
  const rel = offset===0?'今天':offset===1?'明天':offset===2?'后天':wd;
  const heat = offset===0?'hot':offset===1?'warm':'';
  return { offset, wd, ms, remain, remainShort, rel, heat, md:`${m}月${d}日` };
}
items.forEach(it => it.p = parts(it));

let selected = 1, target = 'class', query = '';
const $ = s => document.querySelector(s);
const icons = () => lucide.createIcons({ attrs:{ 'stroke-width':1.75 } });

function tags(it){
  let t = `<span class="src ${it.source}">${SRC[it.source]}</span>`;
  if (it.elective) t += `<span class="src elective">选修</span>`;
  return t;
}

function renderStats(){
  const c = k => items.filter(k).length;
  $('#stats').innerHTML =
    `<span>共<b>${items.length}</b></span>
     <span class="hot">今天截止<b>${c(i=>i.p.offset===0)}</b></span>
     <span>明天截止<b>${c(i=>i.p.offset===1)}</b></span>
     <span>在线测试<b>${c(i=>i.source==='quiz')}</b></span>
     <span>选修<b>${c(i=>i.elective)}</b></span>`;
}

function renderList(){
  const q = query.trim();
  const list = items.filter(i => !q || (i.course+i.title).includes(q));
  const groups = [
    { h:'<span class="tag-hot">今天</span> · 11月8日 周日', f:i=>i.p.offset===0 },
    { h:'明天 · 11月9日 周一', f:i=>i.p.offset===1 },
    { h:'本周稍后 · 11月10日 — 14日', f:i=>i.p.offset>=2 },
  ];
  const html = groups.map(g => {
    const rows = list.filter(g.f);
    if (!rows.length) return '';
    return `<div class="group" role="group"><div class="group-h">${g.h}</div>${rows.map(it => `
      <button class="item" role="option" aria-selected="${it.id===selected}" data-id="${it.id}">
        <span class="when"><span class="t">${it.time}</span><span class="d">${it.p.offset<2?it.p.rel:it.p.wd+' '+it.date.slice(5).replace('-','/').replace(/^0/,'')}</span></span>
        <span class="meta"><span class="course"><span class="c">${it.course}</span>${tags(it)}</span><span class="title">${it.title}</span></span>
        <span class="remain ${it.p.heat}">${it.p.remainShort}</span>
      </button>`).join('')}</div>`;
  }).join('');
  $('#list').innerHTML = html || '<div class="empty">没有匹配的作业</div>';
}

function renderDetail(){
  const it = items.find(i => i.id===selected); const p = it.p;
  const pct = Math.max(4, Math.min(100, 100 - p.ms/(7*864e5)*100));
  const probs = it.probs ? `<div class="probs">${it.probs.map(x=>`<span class="prob"><small>${it.probLabel}</small>${x}</span>`).join('')}</div>` : '';
  const isManual = it.source==='manual';
  $('#detail').innerHTML = `<div class="detail-inner fade">
    <div class="eyebrow">${it.course}${tags(it)}</div>
    <h2>${it.title}</h2>
    <div class="deadline">
      <div><div class="k">截止时间</div><div class="v">${p.md} ${p.wd} <span class="num">${it.time}</span></div></div>
      <div class="timeline" aria-hidden="true">
        <div class="bar ${p.heat}"><i style="width:${pct}%"></i></div>
        <div class="scale"><span>现在 · 11月8日 10:00</span><span>7 天后</span></div>
      </div>
      <div class="count"><div class="k">${p.rel}截止 · 还剩</div><div class="v ${p.heat}">${p.remain}</div></div>
    </div>
    <div class="flags">
      ${it.submitted ? '<span class="done"><i data-lucide="circle-check"></i>本人已提交</span>' : '<span><i data-lucide="circle-dashed"></i>本人未提交</span>'}
      ${it.elective ? '<span class="off"><i data-lucide="circle-minus"></i>不进入班级提醒（选修）</span>' : '<span class="on"><i data-lucide="users"></i>进入班级提醒</span>'}
      <span class="on"><i data-lucide="user"></i>进入个人提醒</span>
    </div>
    <div class="sec">
      <div class="sec-h">任务摘要</div>
      <p class="summary">${it.summary}</p>${probs}
    </div>
    <div class="sec">
      <div class="sec-h">${isManual?'补录原文':'北化在线原文'}${isManual?'':'<button class="link" data-later><i data-lucide="rotate-cw"></i>刷新正文</button>'}</div>
      <div class="origin">${it.origin}<span class="src-line">${isManual?'由本人手动添加 · 11月7日 19:05':'抓取于 11月8日 10:00 · 北化在线'}</span></div>
    </div>
    ${it.image ? `<div class="sec">
      <div class="sec-h">作业图片<span class="num">1 张</span></div>
      <figure class="shot">
        <button class="shot-img" data-view aria-label="查看原图"><img src="${it.image.src}" width="${it.image.w}" height="${it.image.h}" alt="手写作业：习题5，第5题(1)(3)(4)(5)(6)(7)，第6题(3)，第9题(1)(5)(6)(7*)" /></button>
        <figcaption>
          <span>${it.image.caption} · <span class="num">${it.image.w}×${it.image.h}</span> · 当前使用原图</span>
          <span class="shot-acts">
            <button class="btn ghost sm" data-view><i data-lucide="maximize-2"></i>查看原图</button>
            <button class="btn line sm" data-crop><i data-lucide="crop"></i>裁剪图片</button>
          </span>
        </figcaption>
      </figure>
    </div>` : ''}
  </div>`;
  $('#footNote').textContent = isManual ? '手动补录 · 仅保存在本机提醒中' : (it.submitted ? '已提交，仍会提醒班级同学' : (it.elective ? '选修课，仅推送到个人群' : '推送内容尚未确认'));
  document.querySelectorAll('[data-for="platform"]').forEach(b => b.hidden = isManual);
  document.querySelectorAll('[data-for="manual"]').forEach(b => b.hidden = !isManual);
  $('#detail').scrollTop = 0;
}

function renderPreview(){
  const list = target==='class' ? items.filter(i=>!i.elective) : items;
  $('#seg').dataset.v = target;
  $('#seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', b.dataset.v===target));
  $('#pvMeta').innerHTML = target==='class'
    ? `<span>发送到 · 计科 2401 班级群</span><span>${list.length} 项 · 已排除选修 1 项</span>`
    : `<span>发送到 · 我的作业提醒</span><span>${list.length} 项 · 含选修</span>`;
  $('#bubble').innerHTML = `
    <div class="bh">【本周作业提醒】</div>
    <div class="bs">截至 11月8日 周日 10:00，共 ${list.length} 项，请留意截止时间。</div>
    <ol>${list.map(i=>`<li>
      <div class="l1">${i.course} · ${i.title}${i.elective?'（选修）':''}</div>
      <div class="l2">截止：${i.p.md} ${i.p.wd} ${i.time}${i.p.offset<2?` <span class="hot">${i.p.rel}</span>`:''}</div>
      <div class="l3">要求：${i.summary}</div></li>`).join('')}</ol>
    <div class="tail">以平台显示为准，如有遗漏请在群里补充。</div>`;
}

/* 浮层开合 */
function setPop(btn, pop, open){
  pop.classList.toggle('open', open);
  btn.setAttribute('aria-expanded', open);
}
const pvBtn = $('#pvBtn'), pv = $('#preview'), menuBtn = $('#menuBtn'), menu = $('#menu');
pvBtn.addEventListener('click', e => { e.stopPropagation(); setPop(menuBtn, menu, false); setPop(pvBtn, pv, !pv.classList.contains('open')); });
menuBtn.addEventListener('click', e => { e.stopPropagation(); setPop(pvBtn, pv, false); setPop(menuBtn, menu, !menu.classList.contains('open')); });
$('#pvClose').addEventListener('click', () => { setPop(pvBtn, pv, false); pvBtn.focus(); });
[pv, menu].forEach(p => p.addEventListener('click', e => e.stopPropagation()));
document.addEventListener('click', () => { setPop(pvBtn, pv, false); setPop(menuBtn, menu, false); });
document.addEventListener('keydown', e => { if (e.key==='Escape'){ setPop(pvBtn, pv, false); setPop(menuBtn, menu, false); } });

$('#seg').addEventListener('click', e => { const b = e.target.closest('button'); if (b){ target = b.dataset.v; renderPreview(); } });
$('#list').addEventListener('click', e => {
  const b = e.target.closest('.item'); if (!b) return;
  selected = +b.dataset.id;
  document.querySelectorAll('.item').forEach(x => x.setAttribute('aria-selected', x===b));
  renderDetail(); icons();
});
$('#q').addEventListener('input', e => { query = e.target.value; renderList(); icons(); });
document.querySelectorAll('.chip.toggle, .chip').forEach(c => c.addEventListener('click', () => c.setAttribute('aria-pressed', c.getAttribute('aria-pressed')!=='true')));

let tt;
function toast(msg){
  const t = $('#toast'); t.querySelector('span').textContent = msg;
  if (t.showPopover) { if (t.matches(':popover-open')) t.hidePopover(); t.showPopover(); }
  requestAnimationFrame(() => t.classList.add('show'));
  clearTimeout(tt); tt = setTimeout(() => t.classList.remove('show'), 1800);
}
document.addEventListener('click', e => { if (e.target.closest('[data-later]')) { setPop(menuBtn, menu, false); toast('后续接入'); } }, true);
$('#publish').addEventListener('click', () => toast('演示模式，未发送'));

renderStats(); renderList(); renderDetail(); renderPreview(); icons();
