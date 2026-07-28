 // убран — время публикации теперь на карточках постов

function openGenPanel(){const p=$("genPanel");if(!p) return;p.classList.toggle("hidden");if(!p.classList.contains("hidden")) $("genTopic").focus();}

function setTab(t){
  App.tab=t;
  document.querySelectorAll(".tab").forEach(b=>{
    const map={queue:"Очередь",settings:"Настройки",advanced:"Расширенные"};
    b.classList.toggle("active",b.textContent.trim()===map[t]);
  });
  renderTab();
}
function renderTab(){
  if(App.tab==="queue") renderQueue();
  else if(App.tab==="settings") renderSettings();
  else if(App.tab==="advanced") renderAdvanced();
}

// QUEUE
function toggleHistory(){
  const list=$("history_list"),arrow=$("history_arrow");
  if(!list) return;
  const hidden=list.classList.contains("hidden");
  list.classList.toggle("hidden",!hidden);
  if(arrow) arrow.textContent=hidden?"▼":"▶";
}

function toggleExpand(id){
  const pb=$("pb_"+id),btn=$("pexp_"+id);if(!pb||!btn) return;
  const short=pb.classList.contains("post-preview-short");
  pb.classList.toggle("post-preview-short",!short);
  btn.textContent=short?"Свернуть ↑":"Читать полностью ↓";
}

function renderPostCard(p, pubMs, channelEnabled){
  const editable=p.status==="pending"||p.status==="onboarding";
  const sched=p.status==="scheduled";
  const isPaused=channelEnabled===false;
  const isFailed=p.status==="failed"; // заготовка — backend пока не выставляет этот статус (см. ниже)

  // ── Один главный визуальный индикатор статуса ─────────────────────────
  // Важно (по новой точной спецификации): для scheduled синим показываем
  // ТОЛЬКО живой countdown, а дату/время — отдельной серой строкой ниже.
  // Раньше дата была частью того же синего pill — это неправильно по задаче.
  let statusPill="", subLine="";
  if(p.status==="published"){
    const ts=p.published_at?new Date(p.published_at+"Z").toLocaleString("ru-RU",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"}):"";
    statusPill=`<div class="status-pill status-pill-green">Опубликован</div>`;
    if(ts) subLine=`<div class="status-subline">Опубликован ${ts}</div>`;
  } else if(isFailed){
    // Заготовка под статус "ошибка публикации" — backend сейчас не
    // устанавливает Post.status="failed" ни в одном сценарии (ошибки
    // публикации остаются в pending/scheduled с уведомлением через бота).
    // Индикатор готов к моменту когда такой статус появится.
    statusPill=`<div class="status-pill status-pill-red">Ошибка публикации</div>`;
    if(p.publish_error) subLine=`<div class="status-subline" style="color:var(--red)">${esc(p.publish_error)}</div>`;
  } else if(p.status==="rejected"){
    statusPill=`<div class="status-pill status-pill-gray">Удалён</div>`;
  } else if(isPaused){
    statusPill=`<div class="status-pill status-pill-gray">На паузе</div>`;
  } else if(sched && p.scheduled_at){
    const sd=new Date(p.scheduled_at+"Z");const diff=sd-Date.now();
    const h=Math.floor(diff/3600000),m=Math.floor((diff%3600000)/60000),sec=Math.floor((diff%60000)/1000);
    const countdown=diff>0?(h>0?`через ${h}ч ${m}м`:`через ${m}:${String(sec).padStart(2,"0")}`):"скоро";
    const ts=sd.toLocaleString("ru-RU",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
    statusPill=`<div class="status-pill status-pill-blue" id="countdown_${p.id}" data-target-ms="${sd.getTime()}">⏱ ${countdown}</div>`;
    subLine=`<div class="status-subline">Опубликуется ${ts}</div>`;
  } else if(editable){
    // КРИТИЧНО (фикс путаницы из задачи): pending-пост НИКОГДА не должен
    // показывать синий countdown публикации, даже если у канала включена
    // auto_publish. Синий countdown — это только для status="scheduled"
    // (пост явно поставлен в расписание через "Запланировать"). Раньше здесь
    // была ветка, которая путала "канал настроен на автопубликацию по
    // расписанию" с "этот конкретный пост скоро опубликуется" — это и
    // создавало конфликт "Ждёт подтверждения" + синий таймер одновременно.
    //
    // Обещание про автопубликацию показываем ТОЛЬКО если у поста реально
    // заведён дедлайн (PostApproval). Такой таймер есть лишь у постов
    // регулярной генерации по расписанию; посты из онбординга, созданные
    // вручную и догенерация резерва очереди идут с force_pending=True и сами
    // не опубликуются никогда (см. tasks.py). Раньше очередь обещала
    // "опубликуется сам через 30 мин" на уровне всего канала — то есть всем
    // постам подряд, включая те, у которых никакого таймера нет.
    const created=new Date(p.created_at+"Z").toLocaleString("ru-RU",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
    if(p.approval_deadline){
      const dl=new Date(p.approval_deadline).getTime();
      const diff=dl-Date.now();
      const mm=Math.max(0,Math.floor(diff/60000)),ss=Math.max(0,Math.floor((diff%60000)/1000));
      const label=diff>0?`⏱ через ${mm}:${String(ss).padStart(2,"0")}, если не подтвердите`:"⏱ публикуется…";
      statusPill=`<div class="status-pill status-pill-yellow" data-approval-countdown="${dl}">${label}</div>`;
      subLine=`<div class="status-subline">Не отреагируете — опубликуем этот пост сами</div>`;
    } else {
      statusPill=`<div class="status-pill status-pill-yellow">Ждёт вашего решения</div>`;
      subLine=`<div class="status-subline">Создан ${created} · сам не опубликуется</div>`;
    }
  }

  // ── Кнопки: одна primary + один secondary, остальное в меню "..." ────
  const channelConnected = App._chan && App._chan.tg_chat && App._chan.verified;
  const publishDisabledAttr = channelConnected ? "" : `disabled title="Сначала подключите Телеграм-канал"`;
  let primaryBtn="", secondaryBtn="", menuItems="";
  if(isFailed){
    primaryBtn=`<button class="btn btn-sm" onclick="toggleEdit(${p.id})" id="edit_${p.id}">Исправить</button>`;
    secondaryBtn=`<button class="btn-outline btn-sm" onclick="publishPost(${p.id})" ${publishDisabledAttr}>Повторить</button>`;
    menuItems=`<button class="menu-item menu-item-danger" onclick="closePostMenu(${p.id});deletePost(${p.id})">Удалить</button>`;
  } else if(editable){
    primaryBtn=`<button class="btn btn-green btn-sm" onclick="publishPost(${p.id})" ${publishDisabledAttr}>Опубликовать сейчас</button>`;
    secondaryBtn=`<button class="btn-ghost btn-sm" onclick="toggleEdit(${p.id})" id="edit_${p.id}">Изменить</button>`;
    menuItems=`
      <button class="menu-item" onclick="closePostMenu(${p.id});showPicker(${p.id})">⏰ Запланировать</button>
      <button class="menu-item menu-item-danger" onclick="closePostMenu(${p.id});rejectPost(${p.id})">Отклонить</button>
      <button class="menu-item" onclick="closePostMenu(${p.id});regenPost(${p.id})" id="regen_${p.id}">↻ Сгенерировать заново</button>`;
  } else if(sched){
    primaryBtn=`<button class="btn-outline btn-sm" onclick="toggleEdit(${p.id})" id="edit_${p.id}">Изменить</button>`;
    secondaryBtn=`<button class="btn-ghost btn-sm" onclick="publishPost(${p.id})" ${publishDisabledAttr}>Опубликовать сейчас</button>`;
    menuItems=`
      <button class="menu-item" onclick="closePostMenu(${p.id});showPicker(${p.id})">📅 Перенести</button>
      <button class="menu-item menu-item-danger" onclick="closePostMenu(${p.id});rejectPost(${p.id})">Отклонить</button>`;
  } else if(p.status==="published"){
    const chatLabel=(App._chan?.tg_chat||"").replace(/^https?:\/\/t\.me\//i,"").replace(/^@/,"");
    const tgUrl=p.tg_message_id&&chatLabel?`https://t.me/${chatLabel}/${p.tg_message_id}`:`https://t.me/${chatLabel}`;
    primaryBtn=`<button class="btn-outline btn-sm" onclick="window.open('${tgUrl}','_blank')">Открыть в Телеграм</button>`;
    secondaryBtn=`<button class="btn-ghost btn-sm" onclick="regenPost(${p.id})">Создать похожий</button>`;
    menuItems=`<button class="menu-item menu-item-danger" onclick="closePostMenu(${p.id});deletePost(${p.id})">Удалить из списка</button>`;
  } else {
    menuItems=`<button class="menu-item menu-item-danger" onclick="closePostMenu(${p.id});deletePost(${p.id})">Удалить</button>`;
  }
  const menuBtn = menuItems ? `
    <div style="position:relative;margin-left:auto">
      <!-- Замерено на 390px: цель была 32×27 — самая мелкая на экране, и стоит
           вплотную к «Опубликовать сейчас», то есть к необратимому действию.
           Промах пальцем здесь стоит поста, ушедшего в канал. У кнопки нет ни
           фона, ни рамки, поэтому увеличенные отступы расширяют область
           нажатия, ничего не меняя внешне. -->
      <button class="btn-ghost btn-sm" onclick="togglePostMenu(${p.id})" style="padding:14px 16px;line-height:1">⋯</button>
      <div id="pmenu_${p.id}" class="post-menu hidden">${menuItems}</div>
    </div>` : "";

  return `<div class="post-card" id="pc_${p.id}">
    ${statusPill}
    ${subLine}
    <div id="ppreview_${p.id}" style="position:relative">
      <div id="pb_${p.id}" class="post-body post-preview-short" style="margin-top:8px">${renderTg(p.text)}</div>
      <button id="pexp_${p.id}" class="expand-btn" onclick="toggleExpand(${p.id})">Читать полностью ↓</button>
    </div>
    ${(editable||sched||isFailed)?`<textarea id="pt_${p.id}" class="post-body hidden" style="width:100%;min-height:120px;margin-top:8px">${esc(p.text)}</textarea>`:""}
    <div id="picker_${p.id}" class="hidden" style="margin-top:10px;padding:12px;background:var(--surface2);border-radius:10px;border:1px solid var(--border-soft)">
      <div class="field-label" style="margin-bottom:6px">Дата и время (UTC)</div>
      <div class="row" style="gap:8px">
        <input type="datetime-local" id="dt_${p.id}" style="flex:1">
        <button class="btn btn-sm" onclick="doSchedule(${p.id})">Запланировать</button>
        <button class="btn-ghost btn-sm" onclick="$('picker_${p.id}').classList.add('hidden')">✕</button>
      </div>
    </div>
    <div class="post-actions" style="margin-top:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${primaryBtn}${secondaryBtn}
      <button class="btn-ghost btn-sm hidden" id="save_${p.id}" onclick="savePost(${p.id})">💾 Сохранить</button>
      ${menuBtn}
    </div></div>`;
}

// Живой countdown с секундами для ближайшего scheduled/auto-publish поста
// (первая карточка с countdown_ — по построению ближайшая по времени, см.
// renderQueue). Остальные карточки обновляются раз в минуту через обычный
// re-render всей очереди — не перегружаем UI частыми перерисовками.
let _countdownTimer=null, _countdownTargetMs=null, _countdownPostId=null;

function startNearestCountdown(){
  if(_countdownTimer){clearInterval(_countdownTimer);_countdownTimer=null;}
  const el=document.querySelector('[id^="countdown_"]');
  if(!el) return;
  _countdownPostId=el.id.replace("countdown_","");
  _countdownTargetMs=parseInt(el.dataset.targetMs||"0",10);
  if(!_countdownTargetMs) return;

  _countdownTimer=setInterval(()=>{
    const liveEl=$(`countdown_${_countdownPostId}`);
    if(!liveEl){clearInterval(_countdownTimer);_countdownTimer=null;return;}
    const diff=_countdownTargetMs-Date.now();
    if(diff<=0){
      liveEl.textContent="⏱ скоро";
      clearInterval(_countdownTimer);_countdownTimer=null;
      // Время публикации подошло — обновляем всю очередь чтобы подхватить
      // реальный статус с backend (auto-publish тикает на сервере).
      setTimeout(()=>{ if(App.tab==="queue") renderQueue(); },3000);
      return;
    }
    const h=Math.floor(diff/3600000),m=Math.floor((diff%3600000)/60000),sec=Math.floor((diff%60000)/1000);
    liveEl.textContent=h>0?`⏱ через ${h}ч ${m}м`:`⏱ через ${m}:${String(sec).padStart(2,"0")}`;
  },1000);
}

// Живой countdown для карточек каналов на дашборде ("публикация после
// подтверждения" — сколько осталось до автопубликации). В отличие от
// startNearestCountdown (очередь внутри канала, только ближайший пост),
// здесь каналов обычно немного и у каждого свой независимый таймер —
// тикаем все сразу одним интервалом.
let _dashCountdownTimer=null;
function startDashboardCountdowns(){
  if(_dashCountdownTimer){clearInterval(_dashCountdownTimer);_dashCountdownTimer=null;}
  if(!document.querySelector("[data-approval-countdown]")) return;
  const tick=()=>{
    const els=document.querySelectorAll("[data-approval-countdown]");
    if(!els.length){clearInterval(_dashCountdownTimer);_dashCountdownTimer=null;return;}
    els.forEach(el=>{
      const targetMs=parseInt(el.dataset.approvalCountdown||"0",10);
      if(!targetMs) return;
      const diff=targetMs-Date.now();
      if(diff<=0){el.textContent="⏱ публикуется…";return;}
      const m=Math.floor(diff/60000),sec=Math.floor((diff%60000)/1000);
      el.textContent=`⏱ через ${m}:${String(sec).padStart(2,"0")}, если не подтвердите`;
    });
  };
  tick();
  _dashCountdownTimer=setInterval(tick,1000);
}

function togglePostMenu(postId){
  // Закрываем все остальные открытые меню перед открытием текущего.
  document.querySelectorAll(".post-menu").forEach(el=>{
    if(el.id!==`pmenu_${postId}`) el.classList.add("hidden");
  });
  const el=$(`pmenu_${postId}`);
  if(el) el.classList.toggle("hidden");
}