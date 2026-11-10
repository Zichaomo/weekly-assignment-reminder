// Real connected UI against an isolated backend. Only exact loopback origin is allowed.
const {spawn}=require('child_process');
const fs=require('fs'),path=require('path'),assert=require('assert');
const {PYTHON,chromium}=require('./browser_runtime');
const ROOT=path.resolve(__dirname,'..');
const output=path.join(__dirname,'artifacts','v0-connected');fs.mkdirSync(output,{recursive:true});
let child,browser,info,timer;
const errors=[],external=[],posts=[];
async function main(){
  timer=setTimeout(()=>{child?.kill();browser?.close();process.exitCode=1;},100000);
  child=spawn(PYTHON,['-B',path.join(__dirname,'browser_fixture_server.py')],{cwd:ROOT,env:{...process.env,PYTHONIOENCODING:'utf-8'},windowsHide:true});
  let stderr='';child.stderr.on('data',b=>stderr+=b);
  info=await new Promise((resolve,reject)=>{let data='';const timeout=setTimeout(()=>reject(Error('fixture timeout '+stderr)),15000);child.stdout.on('data',b=>{data+=b;const line=data.split(/\r?\n/).find(x=>x.startsWith('{'));if(line){clearTimeout(timeout);resolve(JSON.parse(line));}});child.on('exit',()=>reject(Error(stderr)));});
  const base='http://127.0.0.1:'+info.port;
  browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1280,height:800},locale:'zh-CN'});
  await context.route('**/*',async route=>{if(new URL(route.request().url()).origin!==base){external.push(route.request().url());return route.abort();}return route.continue();});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  page.on('request',r=>{if(r.method()==='POST')posts.push({url:new URL(r.url()).pathname,body:r.postDataJSON()});});
  await page.goto(base);await page.waitForSelector('#list .item');
  let openedUrl=null;
  await page.route('**/api/open',route=>{openedUrl=route.request().postDataJSON().url;return route.fulfill({json:{ok:true}});});
  await page.locator('#list .item').filter({hasText:'fixture作业（脱敏）'}).click();assert((await page.locator('.flags').innerText()).includes('本人已提交'));await page.locator('#openPlatform').click();await page.waitForFunction(()=>document.querySelector('#detail').textContent.includes('fixture作业'));await page.waitForTimeout(100);assert.strictEqual(openedUrl,'https://course.buct.edu.cn/fixture-result');
  assert.strictEqual(posts.filter(x=>x.url==='/api/tasks').length,0,'cold open is local only');
  assert(await page.locator('.brand svg').count(),'local icons loaded');
  for(const [width,height] of [[1280,800],[1440,900],[1100,600]]){
    await page.setViewportSize({width,height});await page.waitForTimeout(350);
    const bounds=await page.evaluate(()=>{const w=document.querySelector('.window').getBoundingClientRect();return {top:w.top,bottom:w.bottom,scroll:document.documentElement.scrollHeight,height:innerHeight};});
    assert(bounds.top>=0&&bounds.bottom<=height+.5,JSON.stringify(bounds));assert(bounds.scroll<=height,JSON.stringify(bounds));
  }
  await page.setViewportSize({width:1280,height:800});
  await page.locator('#q').fill('不存在的课程');assert.strictEqual(await page.locator('#list .item').count(),0);assert(await page.locator('#detail').innerText().then(x=>x.includes('选择作业')));await page.locator('#q').fill('');
  await page.locator('#numDays').fill('6');await page.locator('#numDays').dispatchEvent('change');await page.waitForTimeout(150);
  assert.strictEqual(posts.filter(x=>x.url==='/api/tasks').length,0,'date filter starts no tasks');
  await page.locator('#menuBtn').click();await page.locator('#openCourses').click();await page.waitForSelector('#dlgCourses[open]');
  await page.locator('#cmAvail [data-lid="101"]').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>document.querySelector('#cmJoined').textContent.includes('101'));
  await page.locator('#cmJoined [data-lid="101"]').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>document.querySelector('#cmAvail').textContent.includes('101'));
  await page.locator('[data-tab="quiz"]').click();await page.locator('#cmAvail [data-lid="101"]').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>document.querySelector('#cmJoined').textContent.includes('101'));
  assert(posts.some(x=>x.url==='/api/blacklist'&&x.body.kind==='test'));
  await page.locator('[data-tab="elective"]').click();await page.locator('#cmAddInput').fill('101');await page.locator('#cmAdd [type="submit"]').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>document.querySelector('#cmJoined').textContent.includes('101'));
  await page.screenshot({path:path.join(output,'courses.png')});
  await page.locator('#cmJoined [data-lid="101"]').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>!document.querySelector('#cmJoined').textContent.includes('101'));
  await page.locator('#dlgCourses .dlg-foot [data-close]').click();
  await page.locator('#addBtn').click();await page.locator('#fCourse').fill('平台外科目 <img src=x onerror=alert(1)>');await page.locator('#fTitle').fill('真实手动测试 <script>alert(1)</script>');
  const date=new Date(Date.now()+864e5*2).toLocaleDateString('sv-SE');await page.locator('#fDate').fill(date);await page.locator('#fTime').fill('23:59');await page.locator('#fNote').fill('手动备注 & 安全转义');
  await page.locator('#hwForm [type="submit"]').click();await page.waitForSelector('#dlgForm:not([open])',{state:'attached'});await page.waitForSelector('#list .src.manual');
  await page.locator('#list .item').filter({hasText:'真实手动测试'}).click();assert(await page.locator('#reviewBtn').isHidden());assert(await page.locator('#openPlatform').isHidden());assert.strictEqual(await page.locator('#detail script').count(),0);
  await page.locator('#editBtn').click();assert.strictEqual(await page.locator('#fNote').inputValue(),'手动备注 & 安全转义');await page.locator('#fTitle').fill('已编辑的手动作业');await page.locator('#hwForm [type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#list').textContent.includes('已编辑的手动作业'));
  await page.locator('#doneBtn').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>!document.querySelector('#list').textContent.includes('已编辑的手动作业'));
  await page.locator('#list .item').filter({hasText:'fixture作业（脱敏）'}).click();await page.locator('[data-crop]').first().click();await page.waitForSelector('#dlgCrop[open]');
  const img=await page.locator('#cropImage').boundingBox();await page.mouse.move(img.x+img.width*.1,img.y+img.height*.15);await page.mouse.down();await page.mouse.move(img.x+img.width*.7,img.y+img.height*.8,{steps:8});await page.mouse.up();
  await page.screenshot({path:path.join(output,'crop.png')});
  const viewPattern=/\/api\/assignments\/[^/]+\/view$/;
  await page.route(viewPattern,route=>route.fulfill({status:409,json:{ok:false,error:'fixture version conflict'}}));
  await page.locator('#cropSave').click();await page.waitForFunction(()=>document.querySelector('#cropStatus').textContent.includes('fixture version conflict'));assert(await page.locator('#dlgCrop').evaluate(d=>d.open));
  await page.unroute(viewPattern);await page.locator('#cropSave').click();await page.waitForSelector('#dlgCrop:not([open])',{state:'attached'});
  const content=await page.request.get(base+'/api/assignments/'+encodeURIComponent(info.item_key)+'/content').then(r=>r.json());assert(content.view.regions[0].delivery_asset_id);assert(Math.abs(content.view.regions[0].box[0]-.1)<.01);
  let rule=await page.request.get(base+'/api/course-rules').then(r=>r.json());let saved=await page.request.post(base+'/api/course-rules',{data:{action:'save',expected_revision:rule.revision,rule:{id:'fixture-image-rule',course:'fixture课程 A',match:'exact',mode:'images',lines:3}}}).then(r=>r.json());assert(saved.ok,JSON.stringify(saved));
  await page.locator('#pvBtn').click();
  await page.waitForSelector('#bubble img');
  assert(!(await page.locator('#bubble').innerText()).includes('作业图片随提醒发送'));
  const sources=await page.locator('#bubble img').evaluateAll(images=>images.map(i=>i.getAttribute('src')));
  assert(sources.some(src=>src.includes(content.view.regions[0].delivery_asset_id)));
  assert((await page.locator('#bubble').innerText()).includes('截止'));await page.screenshot({path:path.join(output,'preview.png')});await page.locator('#pvClose').click();
  await page.locator('[data-crop]').first().click();await page.locator('#cropReset').click();await page.locator('#cropSave').click();await page.waitForSelector('#dlgCrop:not([open])',{state:'attached'});
  await page.locator('#reviewBtn').click();await page.locator('#reviewText').fill('人工确认第 1、2 题');await page.locator('#reviewSave').click();await page.waitForSelector('#dlgReview:not([open])',{state:'attached'});await page.waitForFunction(()=>document.querySelector('.summary').textContent.includes('人工确认'));
  await page.locator('#menuBtn').click();await page.locator('#btnCourseRules').click();await page.waitForSelector('#ruleMask.on');await page.locator('#ruleClose').click();
  await page.locator('#menuBtn').click();await page.locator('#openSystem').click();await page.waitForSelector('#dlgSystem[open]');assert((await page.locator('#scheduleSummary').innerText()).includes('2099'));await page.locator('#btnScheduleSet').click();await page.waitForSelector('#maskSchedule.on');await page.locator('#scheduleClose').click();await page.locator('#systemClose').click();
  await page.screenshot({path:path.join(output,'home.png')});
  // Network boundary simulations exercise task polling without allowing any
  // platform, publish, course-refresh or system operation to reach the fixture.
  const simulated=new Map();let counter=0,failFetch=false;
  await page.route('**/api/tasks',async route=>{
    const p=route.request().postDataJSON();if(p.op==='preview')return route.continue();
    const id='mock-ui-'+(++counter);simulated.set(id,{p,polls:0,fail:p.op==='publish'||(p.op==='fetch'&&failFetch)});
    return route.fulfill({json:{ok:true,task_id:id,status:'queued'}});
  });
  await page.route('**/api/tasks/mock-ui-*',route=>{
    const t=simulated.get(new URL(route.request().url()).pathname.split('/').at(-1));t.polls++;
    const running=t.polls===1;
    return route.fulfill({json:{ok:true,status:running?'running':t.fail?'error':'done',progress:running?{course_done:1}:null,result:running?null:t.fail?{ok:false,error:'fixture '+t.p.op+' failure'}:{ok:true,sent:false}}});
  });
  await page.locator('#chkTests').uncheck();await page.locator('#chkAll').check();await page.locator('#fetchBtn').click();await page.waitForFunction(()=>!document.querySelector('#fetchBtn').disabled);
  assert([...simulated.values()].some(t=>t.p.op==='fetch'&&t.p.tests===false&&t.p.all===true));
  await page.locator('#refreshContent').click();await page.waitForFunction(()=>state.tasks.size===0);assert([...simulated.values()].some(t=>t.p.op==='content'&&t.p.force&&t.p.item_keys[0]===info.item_key));
  await page.locator('#retryCourse').click();await page.waitForFunction(()=>state.tasks.size===0);assert([...simulated.values()].some(t=>t.p.lids?.[0]==='101'));
  failFetch=true;const countBefore=simulated.size;await page.locator('#fetchBtn').click();await page.waitForFunction(()=>!document.querySelector('#fetchBtn').disabled);assert.strictEqual(simulated.size,countBefore+1,'failed scan must not enqueue bodies');assert((await page.locator('#toast').innerText()).includes('fixture fetch failure'));
  await page.locator('#menuBtn').click();await page.locator('#openCourses').click();await page.locator('#refreshCourses').click();await page.waitForFunction(()=>state.tasks.size===0);assert([...simulated.values()].some(t=>t.p.op==='refresh-courses'));await page.locator('#dlgCourses .dlg-foot [data-close]').click();
  await page.locator('#pvBtn').click();await page.waitForFunction(()=>!!state.preview);await page.locator('#publish').click();await page.locator('#cfOk').click();await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('fixture publish failure'));assert(await page.locator('#dlgConfirm').evaluate(d=>d.open),'failed publication must stay reviewable');await page.locator('#dlgConfirm [data-close]').click();
  assert.strictEqual(external.length,0,JSON.stringify(external));assert.deepStrictEqual(errors,[]);
  assert([...simulated.values()].every(t=>t.polls>=2),'simulated tasks must be polled to a business result');
  fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({ok:true,externalRequests:external,pageErrors:errors,simulatedOperations:[...simulated.values()].map(t=>t.p.op),postOperations:posts.map(x=>({url:x.url,op:x.body?.op,action:x.body?.action}))},null,2));
  console.log('Connected v0 browser regression passed: viewport, offline icons, lists, manual CRUD, safe text, crop mapping, persistent preview, restore, review, rules, schedule. No sends.');
}
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{clearTimeout(timer);await browser?.close();child?.kill();});
