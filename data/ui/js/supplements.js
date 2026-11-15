const supplementDrafts = new Map();
const supplementDialog = document.createElement('dialog');
supplementDialog.id = 'dlgSupplement';
supplementDialog.className = 'dlg supplement-dialog';
supplementDialog.setAttribute('aria-labelledby', 'supplementTitle');
supplementDialog.innerHTML = `
  <header class="dlg-head">
    <div><h3 id="supplementTitle">补充作业</h3><p id="supplementMeta"></p></div>
    <button class="x" id="supplementClose" aria-label="关闭作业补充">关闭</button>
  </header>
  <div class="scroll supplement-scroll">
    <div class="field"><label for="supplementText">补充要求</label>
      <textarea id="supplementText" rows="5" maxlength="4000" placeholder="例如 PPT 第 12 页，完成 1-3、1-5；写明课件页码与题号便于核对。"></textarea>
    </div>
    <div class="field"><label for="supplementFiles">PPT 截图或作业图片</label>
      <input id="supplementFiles" type="file" accept="image/png,image/jpeg" multiple />
      <p class="settings-help">可直接粘贴截图。最多 6 张，每张 PNG / JPG 不超过 2 MB。</p>
    </div>
    <details id="supplementLatest" class="supplement-latest" hidden open>
      <summary>最新保存的补充</summary><p id="supplementLatestText" class="supplement-body"></p>
      <div id="supplementLatestImages" class="supplement-gallery"></div>
    </details>
    <div id="supplementImages" class="supplement-editor-images"></div>
    <p id="supplementStatus" role="status" aria-live="polite"></p>
  </div>
  <footer class="dlg-foot">
    <span class="note">保存后用于后续提醒</span>
    <button class="btn ghost danger" id="supplementClear">清除补充</button>
    <button class="btn solid" id="supplementSave">保存补充</button>
  </footer>`;
document.body.append(supplementDialog);
let supplementEditor = null;
let supplementInitial = '';
let supplementSaving = false;
let supplementReading = false;

function supplementSnapshot() {
  return JSON.stringify({ text: $('#supplementText').value, images: supplementEditor?.images });
}

function renderSupplementImages() {
  $('#supplementImages').innerHTML = supplementEditor.images.map((image, index) => `
    <figure class="shot"><img src="${image.data ? 'data:' + image.mime + ';base64,' + image.data : assetUrl(image.asset_id)}" alt="补充截图 ${index + 1}" />
      <figcaption><span>截图 ${index + 1}</span><button class="btn ghost danger sm" data-remove-supplement="${index}">移除</button></figcaption>
    </figure>`).join('');
}

function readSupplementImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = () => reject(new Error('图片读取失败，请重新选择'));
    reader.readAsDataURL(file);
  });
}

async function openSupplement(item) {
  const response = requireOk(await api(itemUrl(item, 'supplement')));
  const current = response.supplement;
  const draft = supplementDrafts.get(item.item_key);
  const recovered = draft?.identity_generation === current.identity_generation ? draft : null;
  const images = [...(recovered?.images || current.images)];
  // A screenshot removed by the other editor can still belong to this draft.
  // Re-upload its local bytes when adopting the newly loaded revision.
  const currentImages = new Set(current.images.map(image => image.asset_id));
  for (let index = 0; index < images.length; index++) {
    const image = images[index];
    if (image.asset_id && !currentImages.has(image.asset_id)) {
      const response = await fetch(assetUrl(image.asset_id), { signal: AbortSignal.timeout(20000) });
      if (!response.ok) throw new Error('草稿截图缓存缺失，请重新上传');
      const file = await response.blob();
      images[index] = { data: await readSupplementImage(file), mime: image.mime };
    }
  }
  supplementEditor = { ...current, item, text: recovered?.text ?? current.text, images };
  $('#supplementMeta').textContent = item.course + ' · ' + item.title;
  $('#supplementText').value = supplementEditor.text;
  $('#supplementFiles').value = '';
  supplementInitial = JSON.stringify({ text: current.text, images: current.images });
  const changed = recovered && recovered.revision !== current.revision;
  $('#supplementLatest').hidden = !changed;
  $('#supplementLatestText').textContent = current.text || '未填写补充文字';
  $('#supplementLatestImages').innerHTML = current.images.map(image => `<img src="${assetUrl(image.asset_id)}" alt="最新保存的截图" />`).join('');
  $('#supplementStatus').textContent = changed ? '其他页面已更新。已恢复本页草稿，请核对最新补充后保存。'
    : recovered ? '已恢复本页草稿，请核对后保存。' : '补充内容关联本条平台作业，刷新正文后继续保留。';
  renderSupplementImages();
  openDlg(supplementDialog);
}

function closeSupplement() {
  if (supplementSaving || supplementReading) return;
  if (supplementSnapshot() !== supplementInitial) {
    if (!confirm('补充尚未保存，关闭后保留本页草稿？')) return;
    supplementDrafts.set(supplementEditor.item.item_key, {
      ...supplementEditor, text: $('#supplementText').value,
    });
  } else supplementDrafts.delete(supplementEditor.item.item_key);
  supplementDialog.close();
}

function setSupplementBusy() {
  for (const element of supplementDialog.querySelectorAll('button,input,textarea')) {
    element.disabled = supplementSaving || supplementReading;
  }
}

async function addSupplementFiles(files) {
  if (supplementSaving || supplementReading) return;
  if (files.length + supplementEditor.images.length > 6) throw new Error('最多添加 6 张补充图片');
  supplementReading = true;
  setSupplementBusy();
  try {
    const additions = [];
    for (const file of files) {
      if (!['image/png', 'image/jpeg'].includes(file.type)) throw new Error('请选择 PNG 或 JPG 图片');
      if (file.size > 2 * 1024 * 1024) throw new Error('每张补充图片最多 2 MB');
      const data = await readSupplementImage(file);
      additions.push({ data, mime: file.type });
    }
    supplementEditor.images.push(...additions);
    renderSupplementImages();
    $('#supplementStatus').textContent = '截图已加入草稿，保存后生效。';
  } finally {
    supplementReading = false;
    setSupplementBusy();
    $('#supplementFiles').value = '';
  }
}

async function saveSupplement(clear = false) {
  if (supplementSaving || supplementReading) return;
  if (clear && !confirm('清除这条作业的全部补充文字和截图？')) return;
  supplementSaving = true;
  setSupplementBusy();
  try {
    const result = await api(itemUrl(supplementEditor.item, 'supplement'), {
      expected_revision: supplementEditor.revision,
      expected_identity_generation: supplementEditor.identity_generation,
      text: clear ? '' : $('#supplementText').value,
      images: clear ? [] : supplementEditor.images.map(image => image.data ? { data: image.data } : { asset_id: image.asset_id }),
    });
    if (!result.ok) {
      $('#supplementStatus').textContent = '未保存。' + (result.error || '请求失败') + '。文字与截图草稿仍保留。';
      return;
    }
    supplementDrafts.delete(supplementEditor.item.item_key);
    supplementDialog.close();
    await refreshList();
    toast(clear ? '已清除作业补充' : '作业补充已保存');
  } finally {
    supplementSaving = false;
    setSupplementBusy();
  }
}

$('#supplementClose').onclick = closeSupplement;
supplementDialog.oncancel = event => { event.preventDefault(); closeSupplement(); };
supplementDialog.addEventListener('click', event => {
  if (event.target === supplementDialog) closeSupplement();
  const remove = event.target.closest('[data-remove-supplement]');
  if (remove && !supplementSaving && !supplementReading) {
    supplementEditor.images.splice(Number(remove.dataset.removeSupplement), 1);
    renderSupplementImages();
  }
});
$('#supplementFiles').onchange = guarded(event => addSupplementFiles([...event.target.files]));
supplementDialog.addEventListener('paste', guarded(async event => {
  const files = [...(event.clipboardData?.files || [])];
  if (!files.length) return;
  event.preventDefault();
  await addSupplementFiles(files);
}));
$('#supplementSave').onclick = guarded(() => saveSupplement());
$('#supplementClear').onclick = guarded(() => saveSupplement(true));
document.addEventListener('click', guarded(async event => {
  if (event.target.closest('#supplementBtn')) await openSupplement(currentItem());
  const view = event.target.closest('[data-supplement-view]');
  if (view) {
    $('#viewImage').src = assetUrl(view.dataset.supplementView);
    $('#viewMeta').textContent = currentItem().course + ' · ' + currentItem().title;
    $('#viewNote').textContent = '作业补充截图';
    $('#viewCrop').hidden = true;
    openDlg($('#dlgView'));
  }
}));
