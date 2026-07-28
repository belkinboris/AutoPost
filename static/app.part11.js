
function closePostMenu(postId){
  const el=$(`pmenu_${postId}`);
  if(el) el.classList.add("hidden");
}
document.addEventListener("click",e=>{
  if(!e.target.closest(".post-menu") && !e.target.closest('[onclick^="togglePostMenu"]')){
    document.querySelectorAll(".post-menu").forEach(el=>el.classList.add("hidden"));
  }
});

let _queueViewMode="list"; // "list" | "calendar" -- сбрасывается на "list" при каждом заходе на новый канал (см. renderChannel)

async function renderQueue(){
  $("tabbody").innerHTML=`<div id="postList"><div class="text-faint" style="padding:20px">Загрузка…</div></div>`;
  let posts=[];
  try{posts=await api("GET","/channels/"+App._chan.id+"/posts");}catch(e){}
  App._queuePosts=posts; // календарь и переключение вида работают без повторного запроса

  $("tabbody").innerHTML=`<div id="postList"></div>`;
  renderQueueBody();
}

function setQueueViewMode(mode){
  _queueViewMode=mode;
  renderQueueBody();
}

function renderQueueBody(){
  // Если где-то шёл отсчёт отмены публикации (см. publishPost/_pendingPublish
  // в app.part15.js) -- полная перерисовка сейчас уничтожит ту кнопку, но
  // таймер в памяти продолжил бы тикать невидимо и опубликовал бы пост без
  // единого предупреждения на экране. Уход с этой кнопки -- считаем неявной
  // отменой, безопаснее не публиковать, чем опубликовать незаметно.
  Object.keys(_pendingPublish).forEach(id=>{
    clearInterval(_pendingPublish[id].intervalId);
    clearTimeout(_pendingPublish[id].timeoutId);
    delete _pendingPublish[id];
  });
  const posts=App._queuePosts||[];
  const pending=posts.filter(p=>p.status==="pending"||p.status==="onboarding"||p.status==="scheduled");
  const history=posts.filter(p=>p.status==="published"||p.status==="rejected");
  const c=App._chan;

  // ── Статус очереди ────────────────────────────────────────────────────
  // Заменяет прежний абстрактный баннер «Публикация после подтверждения».
  // Тот баннер (а) был написан канцеляритом без подлежащих («Подтвердить или
  // отклонить» — что? «Опубликуется сам» — кто?), (б) висел всегда, даже
  // когда ничего не требовал от пользователя, и (в) обещал «опубликуется сам
  // через 30 мин» на уровне всего канала, хотя таймер реально заводится
  // только у постов регулярной генерации по расписанию — посты из
  // онбординга, ручные и догенерация резерва очереди идут с
  // force_pending=True и таймера не имеют вовсе (см. tasks.py).
  // Теперь здесь только факты о состоянии очереди, а обещание про таймер
  // переехало на конкретную карточку поста, у которой этот таймер есть.
  // Цель по глубине очереди приходит с канала (зависит от оплаты владельца),
  // App.cfg.min_queue -- только фолбэк для старого закэшированного фронта.
  const minQueue = c.queue_target || App.cfg?.min_queue || 3;
  const connected = !!(c.tg_chat && c.verified);
  const paused = c.enabled === false;
  const queueStatus = _renderQueueStatus(c, pending.length, {minQueue, connected, paused});

  const viewToggle=`<div style="display:flex;gap:8px;margin-bottom:14px">
    <button class="btn-sm ${_queueViewMode==="list"?"btn":"btn-outline"}" onclick="setQueueViewMode('list')">📋 Список</button>
    <button class="btn-sm ${_queueViewMode==="calendar"?"btn":"btn-outline"}" onclick="setQueueViewMode('calendar')">🗓 Календарь</button>
  </div>`;

  let html=queueStatus+viewToggle;

  if(_queueViewMode==="calendar"){
    html+=renderQueueCalendar(posts);
    $("postList").innerHTML=html;
    return;
  }

  if(paused){
    // На паузе tick() не генерирует и не публикует ничего -- предлагать здесь
    // ручную генерацию было бы обманом ожиданий (пост создастся, но так и
    // будет лежать), поэтому единственное осмысленное действие -- снять паузу.
    html+=`<div class="empty"><div class="empty-icon">⏸</div><h3>Канал на паузе</h3>
      <p>Новые посты не создаются и не публикуются, пока канал на паузе.</p></div>`;
    html+=pending.map(p=>renderPostCard(p, p.scheduled_at?new Date(p.scheduled_at+"Z").getTime():null, c.enabled)).join("");
  } else {
    html+=pending.map((p)=>{
      // КРИТИЧНО (фикс путаницы из задачи): pubMs передаём ТОЛЬКО для
      // реально запланированных постов (p.scheduled_at стоит явно через
      // "Запланировать"). Раньше здесь вычислялось спекулятивное время
      // публикации для ЛЮБОГО pending-поста на основе интервала канала —
      // это и создавало конфликт "Ждёт подтверждения" + синий таймер.
      // Pending-пост не имеет реального времени публикации, пока пользователь
      // явно не подтвердит или не запланирует его.
      const pubMs=p.scheduled_at?new Date(p.scheduled_at+"Z").getTime():null;
      return renderPostCard(p, pubMs, c.enabled);
    }).join("");
    // Пустые слоты-заглушки до minQueue: показывают, сколько постов система
    // вообще держит наготове (раньше это число нигде не было видно, и понять
    // "сколько постов должно быть в очереди" было невозможно), и дают явный
    // способ создать пост прямо сейчас, не уходя в настройки.
    html+=_renderQueueSlots(pending.length, minQueue);
  }
  if(history.length){
    html+=`<div style="margin-top:20px">
      <button onclick="toggleHistory()" id="history_btn"
        style="background:none;border:none;cursor:pointer;font-size:13px;color:var(--text-faint);font-weight:500;padding:8px 0;display:flex;align-items:center;gap:6px">
        📁 История публикаций (${history.length}) <span id="history_arrow">▶</span>
      </button>
      <div id="history_list" class="hidden">${history.map(p=>renderPostCard(p)).join("")}</div>
    </div>`;
  }
  $("postList").innerHTML=html;
  _scheduleFirstPostsPoll(pending.length);
  startNearestCountdown();
  // Тикает все карточки с реальным дедлайном автопубликации (см.
  // data-approval-countdown в renderPostCard). Функция сама выходит, если
  // таких карточек на экране нет.
  startDashboardCountdowns();
  _scheduleApprovalRefresh(pending);
}

// Пока ждём первые посты после подключения канала -- сами перечитываем
// очередь. Пользователь не должен догадываться, что нужно перезагрузить
// страницу; именно на этом он и спотыкался.
let _firstPostsPollTimer=null;
function _scheduleFirstPostsPoll(pendingCount){
  if(_firstPostsPollTimer){clearTimeout(_firstPostsPollTimer);_firstPostsPollTimer=null;}
  if(!App._justConnectedAt) return;
  const waited = Date.now() - App._justConnectedAt;
  if(pendingCount > 0 || waited > 5*60*1000){
    App._justConnectedAt = null;   // дождались или перестаём ждать
    return;
  }
  _firstPostsPollTimer=setTimeout(()=>{
    if(App.tab==="queue") renderQueue();
  }, 15000);
}

// Когда ближайший дедлайн автопубликации истекает, пост публикуется на
// сервере (tick) -- перечитываем очередь, чтобы карточка не осталась висеть
// в состоянии "публикуется…" до следующего ручного захода на вкладку.
let _approvalRefreshTimer=null;
function _scheduleApprovalRefresh(pending){
  if(_approvalRefreshTimer){clearTimeout(_approvalRefreshTimer);_approvalRefreshTimer=null;}
  const deadlines=pending
    .filter(p=>p.approval_deadline)
    .map(p=>new Date(p.approval_deadline).getTime())
    .filter(ms=>ms>Date.now());
  if(!deadlines.length) return;
  const soonest=Math.min(...deadlines);
  _approvalRefreshTimer=setTimeout(()=>{
    if(App.tab==="queue") renderQueue();
  }, (soonest-Date.now())+8000);
}

// ── Статус очереди: что происходит прямо сейчас ───────────────────────
// Отвечает на три вопроса, на которые интерфейс раньше не отвечал вообще:
// сколько постов в очереди, когда появится следующий и что вообще будет с
// готовым постом. Формулировки — с явными подлежащими, без канцелярита.
function _renderQueueStatus(c, pendingCount, opts){
  const {minQueue, connected, paused} = opts;
  const counter = `<b>${pendingCount}</b> из ${minQueue}`;
  const settingsLink = `onclick="setTab('settings');setTimeout(()=>{const el=document.getElementById('settings_automation_card');if(el) el.scrollIntoView({behavior:'smooth',block:'center'});},100)"`;

  if(paused){
    return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;font-weight:600">Канал на паузе</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">Пока канал на паузе, новые посты не создаются и ничего не публикуется. В очереди ${counter}.</div>
    </div>`;
  }

  if(!connected){
    // Главный фикс: раньше это объяснение показывалось ТОЛЬКО при пустой
    // очереди. У пользователя с одним постом из онбординга (типичный случай:
    // канал создан, Telegram ещё не подключён) на экране не было вообще
    // ничего, что объясняло бы, почему второй пост так и не появился.
    //
    // Сознательно НЕ повторяем здесь заголовок "Канал не подключён" и кнопку
    // "Подключить" -- баннер прямо над вкладками уже говорит ровно это (см.
    // renderChannel). Тут только то, чего там нет: что происходит с очередью.
    return `<div class="card" style="background:var(--accent-soft);border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;color:var(--accent-dark);font-weight:600">Новые посты пока не создаются</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        В очереди ${counter}. Сами по расписанию посты начнут появляться после подключения канала.
        А написать пост можно прямо сейчас — кнопкой ниже.
      </div>
    </div>`;
  }

  // Только что подключили канал, а постов ещё нет: генерация идёт на сервере
  // и занимает минуту-две. Без этого экран выглядел так, будто ничего не
  // произошло, и приходилось перезагружать страницу вручную.
  const justConnected = App._justConnectedAt && (Date.now() - App._justConnectedAt < 5*60*1000);
  if(justConnected && pendingCount === 0){
    return `<div class="card" style="background:var(--green-bg,var(--surface2));border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px">
        <span class="spinner"></span> Канал подключён — готовим первые посты
      </div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:4px">
        Обычно это занимает одну-две минуты. Страница обновится сама, перезагружать не нужно.
      </div>
    </div>`;
  }

  if(c.auto_publish){
    return `<div class="card" style="background:var(--blue-bg);border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;color:var(--blue);font-weight:600">Автопилот включён</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        Мы сами пишем и публикуем посты — ${_intervalLabel(c.interval_hours||12)}. Подтверждать ничего не нужно. В очереди ${counter}.
      </div>
      <button class="btn-ghost btn-sm" style="margin-top:6px;padding:4px 0;color:var(--blue)" ${settingsLink}>Изменить</button>
    </div>`;
  }

  // Режим "ничего не выходит без решения пользователя".
  const softControlMin = App.cfg?.soft_control_minutes || 30;
  const refillLine = pendingCount < minQueue
    ? `Ещё ${minQueue - pendingCount} мы готовим — обычно это занимает пару минут.`
    : `Очередь заполнена. Как только опубликуете один пост, мы подготовим следующий.`;

  // КРИТИЧНО: здесь было «Ни один пост не попадёт в канал, пока вы не нажмёте
  // „Опубликовать“» -- и это неправда для основного сценария. Пост, который мы
  // пишем по расписанию, получает PostApproval с дедлайном (см. needs_approval
  // в tasks.py -- таймер заводится ВСЕГДА, независимо от Telegram), и по
  // истечении таймера публикуется сам. Правда была написана только в свёрнутом
  // «Подробнее», а на виду стояло обещание, которого система не выполняет.
  // Теперь на виду то же, что показывает карточка поста: либо кнопка, либо
  // видимый таймер. Гарантия при этом не ослаблена -- молча по-прежнему не
  // уходит ничего.
  return `<div class="card" style="background:var(--accent-soft);border:none;margin-bottom:14px;padding:14px 16px">
    <div style="font-size:13px;color:var(--accent-dark);font-weight:600">В очереди ${counter}</div>
    <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
      ${App.user?.tg_chat_id
        ? "Пост ждёт вашей кнопки «Опубликовать» — или таймера на карточке, если мы прислали его вам в Телеграм."
        : "Мы ничего не публикуем сами, пока не можем вас предупредить: каждый пост ждёт вашей кнопки."} ${refillLine}
    </div>
    <button class="btn-ghost btn-sm" style="margin-top:6px;padding:4px 0;color:var(--accent-dark)"
      onclick="toggleQueueHelp()" id="queue_help_btn">Подробнее ▾</button>
    <div id="queue_help" class="hidden" style="font-size:13px;color:var(--text-dim);margin-top:8px;line-height:1.6;border-top:1px solid var(--border-soft);padding-top:8px">
      Новые посты мы пишем сами — ${_intervalLabel(c.interval_hours||12)}, плюс держим в запасе ${minQueue}.<br>
      ${App.user?.tg_chat_id
        ? `Пост по расписанию мы присылаем вам в Телеграм, и у него есть обратный отсчёт: не отреагируете за ${softControlMin} мин — опубликуем сами. Такой пост видно по таймеру на карточке.<br>
      Пост, который вы создали вручную, ждёт вашего решения сколько угодно — сам он не опубликуется.<br>`
        : `Пока уведомления не подключены, обратный отсчёт не запускается: предупредить вас нам нечем, поэтому ни один пост не уходит в канал сам. Каждый ждёт вашей кнопки сколько угодно.<br>`}
      ${App.user?.tg_chat_id
        ? `Мы дублируем такие посты вам в Телеграм — можно решать с телефона, не заходя на сайт.`
        : `<a href="#" onclick="setTab('settings');return false">Подключите уведомления в Телеграм</a>, чтобы решать с телефона, не заходя на сайт.`}
      <button class="btn-ghost btn-sm" style="margin-top:6px;padding:4px 0;color:var(--accent-dark)" ${settingsLink}>Открыть настройки</button>
    </div>
  </div>`;
}

function toggleQueueHelp(){
  const el=$("queue_help"), btn=$("queue_help_btn");
  if(!el) return;
  const hidden=el.classList.contains("hidden");
  el.classList.toggle("hidden",!hidden);
  if(btn) btn.textContent=hidden?"Свернуть ▴":"Подробнее ▾";
}

// Пустые слоты до minQueue. Делают видимой саму норму «сколько постов должно
// быть наготове» и дают явную кнопку создать пост сейчас, вместо того чтобы
// гадать, когда он появится сам.
function _renderQueueSlots(pendingCount, minQueue){
  const missing = Math.max(0, minQueue - pendingCount);
  if(!missing) return "";
  // Рисуем не больше трёх заглушек: у оплатившего цель очереди 7, и при одном
  // готовом посте шесть пунктирных рамок подряд превратили бы экран в забор.
  // Точное число недостающих постов и так названо словами в статусе выше.
  const shown = Math.min(missing, 3);
  let out = "";
  for(let i=0;i<shown;i++){
    out += i===0
      ? `<div class="queue-slot">
           <button class="btn-outline btn-sm" id="queue_gen_btn" onclick="genQueuePost()">+ Написать пост сейчас</button>
           <div class="queue-slot-hint">Не дожидаясь расписания</div>
         </div>`
      : `<div class="queue-slot queue-slot-muted"><div class="queue-slot-hint">Место для ещё одного поста</div></div>`;
  }
  return out;
}

let _genQueueInFlight=false;
async function genQueuePost(){
  if(!requireAuth()) return;
  if(_genQueueInFlight) return;
  _genQueueInFlight=true;
  const btn=$("queue_gen_btn");
  if(btn){btn.innerHTML='<span class="spinner"></span> Пишу пост…';btn.disabled=true;}
  try{
    await api("POST","/channels/"+App._chan.id+"/generate",{});
    trackGoal("post_generated",{source:"queue_slot",channel_id:App._chan.id});
    toast("Пост готов ✓","ok");
    await renderQueue();
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    if(btn){btn.innerHTML="+ Написать пост сейчас";btn.disabled=false;}
  }finally{
    _genQueueInFlight=false;
  }
}

// ── КАЛЕНДАРЬ (task item: вид очереди по датам) ────────────────────────
// Показывает посты, у которых есть конкретная дата: опубликованные
// (published_at) и явно запланированные (scheduled_at, статус "scheduled").
// Посты в режиме "публикация после подтверждения" (pending) намеренно не
// показываются на календаре -- у них ещё нет фиксированной даты публикации,
// она зависит от того, когда/подтвердит ли пользователь пост (см. очередь
// в виде списка для них).
let _calMonth=null; // Date (1-е число месяца, локальное время) -- null = текущий месяц при первом открытии
let _calSelectedDate=null; // "YYYY-MM-DD" -- какой день сейчас раскрыт под календарём

function _dateKey(d){
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}

function changeCalMonth(delta){
  const m=_calMonth||new Date();
  _calMonth=new Date(m.getFullYear(), m.getMonth()+delta, 1);
  _calSelectedDate=null;
  renderQueueBody();
}

function selectCalDate(key){
  _calSelectedDate=(_calSelectedDate===key)?null:key;
  renderQueueBody();
}

function renderQueueCalendar(posts){
  const monthDate=_calMonth||new Date();
  const year=monthDate.getFullYear(), month=monthDate.getMonth();

  const byDate={};
  posts.forEach(p=>{
    let d=null, kind=null;
    if(p.status==="published" && p.published_at){ d=new Date(p.published_at+"Z"); kind="published"; }
    else if(p.status==="scheduled" && p.scheduled_at){ d=new Date(p.scheduled_at+"Z"); kind="scheduled"; }
    if(!d) return;
    const key=_dateKey(d);
    (byDate[key]=byDate[key]||[]).push({...p, _calKind:kind, _calTime:d});
  });

  const firstOfMonth=new Date(year,month,1);
  const daysInMonth=new Date(year,month+1,0).getDate();
  // Понедельник = 0 (российская неделя)
  const leadingBlanks=(firstOfMonth.getDay()+6)%7;
  const todayKey=_dateKey(new Date());

  let cells="";
  for(let i=0;i<leadingBlanks;i++) cells+=`<div class="cal-cell cal-cell-empty"></div>`;
  for(let day=1;day<=daysInMonth;day++){
    const key=`${year}-${String(month+1).padStart(2,"0")}-${String(day).padStart(2,"0")}`;
    const items=(byDate[key]||[]).sort((a,b)=>a._calTime-b._calTime);
    const isToday=key===todayKey;
    const isSelected=key===_calSelectedDate;
    const dots=items.slice(0,3).map(it=>`<span class="cal-dot cal-dot-${it._calKind}"></span>`).join("");
    const more=items.length>3?`<span class="cal-more">+${items.length-3}</span>`:"";
    cells+=`<div class="cal-cell${isToday?" cal-cell-today":""}${isSelected?" cal-cell-selected":""}${items.length?" cal-cell-has":""}"
      ${items.length?`onclick="selectCalDate('${key}')"`:""}>
      <div class="cal-daynum">${day}</div>
      ${items.length?`<div class="cal-dots">${dots}${more}</div>`:""}
    </div>`;
  }

  const monthLabel=monthDate.toLocaleDateString("ru-RU",{month:"long",year:"numeric"});
  const weekHead=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"].map(d=>`<div class="cal-cell cal-cell-head">${d}</div>`).join("");

  let selectedBlock="";
  if(_calSelectedDate && byDate[_calSelectedDate]){
    const dLabel=new Date(_calSelectedDate+"T00:00:00").toLocaleDateString("ru-RU",{day:"numeric",month:"long"});
    selectedBlock=`<div style="margin-top:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <h3 style="margin:0">${dLabel}</h3>
        <button class="btn-ghost btn-sm" onclick="selectCalDate('${_calSelectedDate}')">✕ Закрыть</button>
      </div>
      ${byDate[_calSelectedDate].map(p=>renderPostCard(p, p.scheduled_at?new Date(p.scheduled_at+"Z").getTime():null, App._chan.enabled)).join("")}
    </div>`;
  }

  return `<div class="cal-nav">
    <button class="btn-ghost btn-sm" onclick="changeCalMonth(-1)">‹</button>
    <div class="cal-month-label">${monthLabel}</div>
    <button class="btn-ghost btn-sm" onclick="changeCalMonth(1)">›</button>
  </div>
  <div class="cal-grid">${weekHead}${cells}</div>
  <div class="cal-legend"><span><span class="cal-dot cal-dot-published"></span> Опубликован</span><span><span class="cal-dot cal-dot-scheduled"></span> Запланирован</span></div>
  ${selectedBlock}`;
}


// SETTINGS
function renderSettings(){
  const c=App._chan;
  const lens=["50-100 слов","100-200 слов","200-350 слов"];
  $("tabbody").innerHTML=`
    <div class="card">
      <div class="card-title">Телеграм</div>
      <label class="field"><span class="field-label">Название</span>
        <input id="f_title" value="${esc(c.title)}"></label>
      <label class="field mt"><span class="field-label">@username, ссылка t.me/ или ID</span>
        ${c.verified
          ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;background:var(--green-bg);border-radius:10px;margin-bottom:6px;flex-wrap:nowrap;overflow:hidden">
               <span style="color:var(--green);font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">✓ Проверено · ${esc(c.tg_chat)}</span>
               <button class="btn-ghost btn-sm" onclick="showVerifyInput()" style="flex-shrink:0;font-size:12px">Изменить</button>
             </div>
             <div id="verifyInputBlock" class="hidden">
               <div class="row" style="gap:8px">
                 <input id="f_chat" value="${esc(c.tg_chat)}" placeholder="@my_channel" style="flex:1">
                 <button class="btn-outline btn-sm" onclick="verifyChannel()" id="verBtn" style="white-space:nowrap">Проверить</button>
               </div>
               <div class="hint">Добавьте бота <b>@${esc(App.cfg?.bot_username||"…")}</b> администратором с правом публикации. <a href="/how-to" target="_blank" rel="noopener">Как это сделать →</a></div>
               <div id="verMsg" style="font-size:13px;margin-top:6px"></div>
             </div>`
          : `<div class="row" style="gap:8px">
               <input id="f_chat" value="${esc(c.tg_chat)}" placeholder="@my_channel" style="flex:1">
               <button class="btn-outline btn-sm" onclick="verifyChannel()" id="verBtn" style="white-space:nowrap">Проверить</button>
             </div>
             <div class="hint">Добавьте бота <b>@${esc(App.cfg?.bot_username||"…")}</b> администратором с правом публикации. <a href="/how-to" target="_blank" rel="noopener">Как это сделать →</a></div>
             <div id="verMsg" style="font-size:13px;margin-top:6px"></div>`
        }
      </label>
    </div>
    <div class="card">
      <div class="card-title">О канале</div>
      <label class="field"><span class="field-label">Тема</span>
        <textarea id="f_about" rows="3">${esc(c.about)}</textarea></label>
      <label class="field mt"><span class="field-label">Стиль и тон</span>
        <textarea id="f_style" rows="2">${esc(c.style)}</textarea></label>
      <div style="margin-top:16px">
        <div class="field-label">Длина поста</div>
        <div class="seg" style="max-width:400px" id="seg_len">
          ${lens.map(o=>`<button class="${c.post_length===o?"on":""}" onclick="pickLen('${o}')">${o}</button>`).join("")}
        </div>
      </div>
      <div style="margin-top:16px">
        <div class="field-label" style="margin-bottom:6px">Скопировать стиль с канала</div>
        <div class="row" style="gap:8px">
          <input id="f_analyze" placeholder="https://t.me/example" style="flex:1">
          <button class="btn-outline btn-sm" onclick="analyzeChannel()" id="anBtn" style="white-space:nowrap">Изучить</button>
        </div>
        <div id="analyze_result"></div>
      </div>
    </div>
    <div class="card" id="settings_automation_card">
      <div class="card-title">Автоматизация</div>
      <div class="toggle-row">
        <div class="toggle-info"><b>Публиковать без проверки</b><small>Если включено — новые посты выходят в канал сами, по расписанию. Если выключено — пост ждёт вашего решения в очереди и сам не публикуется. Подключите уведомления в Телеграм: посты придут туда с кнопками «Опубликовать», «Отклонить», «Редактировать», и на решение будет ${App.cfg?.soft_control_minutes||30} мин — не ответите, опубликуем сами.</small></div>
        <label class="switch"><input type="checkbox" id="sw_auto" ${c.auto_publish?"checked":""}><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><b>Искать новости в интернете</b></div>
        <label class="switch"><input type="checkbox" id="sw_web" ${c.use_web_search?"checked":""}><span class="slider"></span></label>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Уведомления в Телеграм</div>
      <div style="margin-bottom:14px" id="tg_notif_block">
        ${App.user?.tg_chat_id
          ? '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--green-bg);border-radius:10px;font-size:14px;color:var(--green)">✅ Подключено — уведомления активны</div>'
          : '<div style="font-size:13px;color:var(--text-dim);margin-bottom:10px;line-height:1.6">Нажмите кнопку — бот пришлёт приветствие и начнёт отправлять уведомления.</div>'
            + '<button class="btn" onclick="openTgConnect()" style="display:inline-flex;margin-bottom:4px">💬 Подключить уведомления →</button>'
            + '<div class="hint" style="margin-top:8px">Откроется бот — нажмите Start</div>'
        }
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><b>Пост опубликован</b><small>Уведомление после каждой публикации</small></div>
        <label class="switch"><input type="checkbox" id="sw_n2" ${App.user?.notify_published?"checked":""}><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><b>Баланс заканчивается</b><small>Уведомим, когда постов почти не останется</small></div>
        <label class="switch"><input type="checkbox" id="sw_n3" ${App.user?.notify_low_tokens!==false?"checked":""}><span class="slider"></span></label>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Проверить настройки</div>
      <!-- Слово «тестовый» обещало пробный прогон без последствий, а кнопка
           дёргает тот же /generate, что и «Написать пост сейчас» в очереди:
           пост настоящий, тратит токены и остаётся в очереди. Называем вещи
           одинаково в обоих местах и сразу говорим про расход. -->
      <p style="font-size:13px;color:var(--text-dim);margin-bottom:12px">Напишем пост прямо сейчас — посмотрите, что получается с текущими настройками. Пост обычный: тратит токены и встаёт в очередь, сам не опубликуется.</p>
      <button class="btn-outline" onclick="testPost()" id="testBtn">▷ Написать пост сейчас</button>
      <div id="test_result" style="margin-top:12px"></div>
    </div>
    <div class="row between mt-lg">
      <button class="btn-danger btn-sm" onclick="deleteChannel()">Удалить канал</button>
      <button class="btn" onclick="saveChannel()">Сохранить</button>
    </div>`;
}