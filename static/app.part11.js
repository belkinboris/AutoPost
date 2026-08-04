
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

async function renderQueue(silent){
  // silent -- фоновое обновление (поллинг генерации, см. _scheduleGeneratingPoll):
  // владелец пожаловался, что экран целиком мигал "Загрузка…" каждые
  // несколько секунд, пока очередь дозаполнялась -- раздражает и не несёт
  // новой информации в 9 случаях из 10 (между тиками ничего не меняется).
  // Молча обновляем данные и перерисовываем только список, без разрушения
  // и пересоздания контейнера.
  if(!silent || !$("postList")) $("tabbody").innerHTML=`<div id="postList"><div class="text-faint" style="padding:20px">Загрузка…</div></div>`;
  let posts=[];
  try{
    // Канал освежаем вместе с постами -- не только ради queue_target/
    // queue_ceiling, но и ради c.generating (C14, пункт 6): это поле живёт
    // на канале, а не на постах, и без повторного запроса индикатор
    // "генерируется следующий пост" не исчез бы после завершения генерации
    // без ручной перезагрузки страницы.
    const [freshChan, freshPosts] = await Promise.all([
      api("GET","/channels/"+App._chan.id),
      api("GET","/channels/"+App._chan.id+"/posts"),
    ]);
    App._chan=freshChan;
    posts=freshPosts;
  }catch(e){}
  App._queuePosts=posts; // календарь и переключение вида работают без повторного запроса

  // Прогноз автопубликаций для календаря -- только у автопилота: без него
  // решение всегда за пользователем, и показывать даты было бы обещанием
  // того, чего система не делает. Запрашиваем один раз при заходе на вкладку,
  // а не при каждой перерисовке -- renderQueueBody() вызывается часто (после
  // публикации, отклонения и т.д.), лишний запрос на каждый клик не нужен.
  // Смена частоты подхватится при следующем заходе на «Очередь» -- тем же
  // способом, каким уже обновляется сам App._chan.
  App._schedulePreview=[];
  if(App._chan.auto_publish){
    try{
      const r=await api("GET","/channels/"+App._chan.id+"/schedule_preview");
      App._schedulePreview=r.slots||[];
    }catch(e){}
  }

  if(!silent || !$("postList")) $("tabbody").innerHTML=`<div id="postList"></div>`;
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

  // Единая модель очереди (C14): очередь -- это порядок публикации, то есть
  // порядок по scheduled_at, а не по дате создания (в которой API отдаёт
  // список). Без этой сортировки свежесозданный автопилот-пост с временем
  // через 2 часа показывался ВЫШЕ поста с дедлайном подтверждения через 5
  // минут -- просто потому что создан позже. Одновременно это и есть
  // "пересортировка при переносе даты" из задачи владельца: позиция в
  // очереди -- это и есть scheduled_at, отдельного поля "место в очереди"
  // нет, поэтому смена времени (showPicker/doSchedule) меняет порядок сама,
  // без какой-либо отдельной логики. Посты без scheduled_at (онбординг-
  // черновики) -- в конец, тем же порядком, что и _channel_dict на бэкенде.
  pending.sort((a,b)=>{
    const at=a.scheduled_at?new Date(a.scheduled_at+"Z").getTime():Infinity;
    const bt=b.scheduled_at?new Date(b.scheduled_at+"Z").getTime():Infinity;
    return at-bt;
  });

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

  const viewToggle=`<div style="display:flex;gap:8px;margin-bottom:4px">
    <button class="btn-sm ${_queueViewMode==="list"?"btn":"btn-outline"}" onclick="setQueueViewMode('list')">📋 Список</button>
    <button class="btn-sm ${_queueViewMode==="calendar"?"btn":"btn-outline"}" onclick="setQueueViewMode('calendar')">🗓 Календарь</button>
  </div>`;

  let html=queueStatus+viewToggle;

  if(_queueViewMode==="calendar"){
    html+=renderQueueCalendar(posts, App._schedulePreview||[]);
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
    html+=_renderQueueSlots(pending.length, minQueue, (App.user?.token_balance||0)<=0,
                            !!c.generating, c.queue_ceiling||minQueue);
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
  _scheduleGeneratingPoll();
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
    if(App.tab==="queue" && !Object.keys(_pendingPublish).length) renderQueue(true);
    else _scheduleFirstPostsPoll(pendingCount);
  }, 15000);
}

// C14, пункт 6: пока Channel.generating_since (см. renderQueue -- канал
// освежается вместе с постами) говорит, что фоновая догенерация идёт,
// перечитываем очередь, чтобы индикатор "генерируется следующий пост"
// пропал сам, как только пост появится, без ручной перезагрузки.
//
// Найдено владельцем 02.08: при долгом дозаполнении очереди (несколько
// постов подряд, каждый на своём тике) экран мигал "Загрузка…" каждые
// несколько секунд подряд минутами -- обновление теперь тихое (см. silent
// в renderQueue), интервал реже (было 4с), и не должно прерывать отсчёт
// отмены "Опубликовать сейчас" -- renderQueueBody() снимает такие отсчёты
// безусловно, а тихий фоновый поллинг не должен молча отменять решение,
// которое человек в этот момент принимает.
let _generatingPollTimer=null;
function _scheduleGeneratingPoll(){
  if(_generatingPollTimer){clearTimeout(_generatingPollTimer);_generatingPollTimer=null;}
  if(!App._chan?.generating) return;
  _generatingPollTimer=setTimeout(()=>{
    if(App.tab==="queue" && !Object.keys(_pendingPublish).length) renderQueue(true);
    else _scheduleGeneratingPoll(); // отсчёт отмены идёт -- не мешаем, попробуем позже
  }, 8000);
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
// Прокрутка к карточке автоматизации в настройках. Одна на все места, где
// мы отправляем человека «в настройки очереди»: раньше эта строка жила
// копией внутри _renderQueueStatus, и вторая копия в слоте очереди
// разъехалась бы с ней при первом же переименовании якоря.
function _settingsQueueLink(){
  return `onclick="setTab('settings');setTimeout(()=>{const el=document.getElementById('settings_automation_card');if(el) el.scrollIntoView({behavior:'smooth',block:'center'});},100)"`;
}

function _renderQueueStatus(c, pendingCount, opts){
  const {minQueue, connected, paused} = opts;
  // «N из M»: знаменатель показываем ВСЕГДА, в том числе когда N > M.
  // Раньше он в этом случае исчезал -- ровно в тот момент, когда нужен
  // больше всего. Аудит 02.08: человек уменьшает глубину очереди с 6 до 3,
  // видит «В очереди 6» без единого пояснения и не понимает, почему новые
  // посты перестали появляться. Уже написанные посты мы не выбрасываем
  // (человек их не отклонял), очередь рассасывается публикациями -- об этом
  // говорит overflowLine.
  //
  // Переполнение теперь возможно только по воле пользователя: плановое
  // пополнение глубину проверяет (_refill_queue), кнопка «Написать сейчас»
  // с выбранным временем -- намеренно нет.
  const counter = `<b>${pendingCount}</b> из ${minQueue}`;
  const overflowLine = pendingCount > minQueue
    ? ` Сейчас постов больше запаса: готовые никуда не денутся, а новые мы начнём писать, когда их останется меньше ${minQueue}.`
    : "";
  const settingsLink = _settingsQueueLink();

  if(paused){
    return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;font-weight:600">Канал на паузе</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">Пока канал на паузе, новые посты не создаются и ничего не публикуется. В очереди ${counter}.</div>
    </div>`;
  }

  // Пустой баланс проверяем ДО всех остальных состояний -- это единственная
  // причина, по которой не сработает вообще ничего: ни расписание, ни резерв,
  // ни кнопка «Написать пост сейчас» (generate_for_channel выходит на
  // `user.token_balance <= 0` первой же проверкой).
  //
  // Найдено владельцем на живом канале 28.07: он подключил канал, увидел
  // зелёное «Канал подключён — готовим первые посты… страница обновится сама»
  // и ждал. Посты не появлялись, причина не показывалась нигде -- она вылезла
  // только после ручного нажатия «Написать пост сейчас», красной плашкой
  // «лимит закончился». То есть экран пять минут обещал работу, которая не
  // могла начаться (правило 5 в CLAUDE.md).
  if((App.user?.token_balance || 0) <= 0){
    return `<div class="card" style="background:var(--red-bg,var(--accent-soft));border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;color:var(--red,var(--accent-dark));font-weight:600">Посты не создаются — закончились токены</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        ${pendingCount > 0
          ? `В очереди ${counter} — эти посты никуда не денутся, их можно опубликовать. Но новые мы не пишем: ни по расписанию, ни кнопкой.`
          : `Ни по расписанию, ни кнопкой «Написать пост сейчас» — пополните баланс, и мы продолжим с того же места.`}
      </div>
      <button class="btn btn-sm" style="margin-top:10px" onclick="go('billing')">Пополнить баланс →</button>
    </div>`;
  }

  // Плановая генерация остановлена после нескольких неудач подряд.
  //
  // Прод-инцидент 03.08: генерация падала на каждой попытке, тик повторял её
  // раз в минуту, и человек пять минут смотрел на очередь, которая не растёт,
  // не имея ни одной подсказки. Стоп сам по себе эту беду не лечит -- молча
  // остановиться ничем не лучше, чем молча повторять. Поэтому говорим прямо:
  // что случилось, почему мы перестали пробовать и какой рычаг есть у него.
  if(c.generation_stopped){
    return `<div class="card" style="background:var(--red-bg,var(--accent-soft));border:none;margin-bottom:14px;padding:14px 16px">
      <div style="font-size:13px;color:var(--red,var(--accent-dark));font-weight:600">Мы не смогли написать новый пост</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        ${c.generation_stopped_reason ? esc(c.generation_stopped_reason)+" " : ""}Пробовали несколько раз подряд и остановились, чтобы не тратить ваши токены впустую. В очереди ${counter} — эти посты никуда не денутся.
      </div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:6px">
        Нажмите «Написать пост сейчас» — попробуем снова. Помогает и другое: опубликовать или удалить пост из очереди, поменять описание канала.
      </div>
      <button class="btn-ghost btn-sm" style="margin-top:6px;padding:4px 0" ${settingsLink}>Открыть настройки</button>
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
        Мы сами пишем и публикуем посты — ${_intervalLabel(c.interval_hours||12)}. Подтверждать ничего не нужно. В очереди ${counter}.${overflowLine}${
        pendingCount >= minQueue && !overflowLine
          ? " Запас набран — следующий напишем после ближайшей публикации."
          : ""}
      </div>
      <button class="btn-ghost btn-sm" style="margin-top:6px;padding:4px 0;color:var(--blue)" ${settingsLink}>Изменить</button>
    </div>`;
  }

  // Режим "ничего не выходит без решения пользователя".
  const softControlMin = App.cfg?.soft_control_minutes || 30;
  // Здесь было «Очередь заполнена. Как только опубликуете один пост, мы
  // подготовим следующий» -- и это неправда. Проверено прямым вызовом решающих
  // функций: канал с полной очередью и истёкшим интервалом попадает в
  // `due_ids` в tick() и получает новый пост, публикации никто не ждёт.
  // Обещание держало паузу, которой в системе нет.
  // Интервал здесь намеренно не называем -- он написан в «Подробнее», и
  // повторять его на виду значило бы вернуть тот самый повтор, который убрали
  // в B6.
  //
  // «Запас готов — дальше посты добавляются по расписанию» тоже оказалось
  // неправдой, только уже после C14 (аудит 02.08): при полной очереди
  // _refill_queue выходит на первой же проверке `pending_count >= target` и
  // не пишет ничего, сколько бы времени ни прошло. Место освобождает
  // публикация -- это и говорим.
  const refillLine = pendingCount < minQueue
    ? `Ещё ${minQueue - pendingCount} ${_plural(minQueue - pendingCount, "пост", "поста", "постов")} мы готовим — обычно это занимает пару минут.`
    : `Запас набран — следующий пост мы напишем, когда в очереди освободится место, то есть после ближайшей публикации.${overflowLine}`;

  // Механику «кнопка или таймер» показываем только когда постов ещё нет.
  // Как только пост появился, его карточка говорит это про себя сама -- либо
  // «сам не опубликуется», либо обратный отсчёт, -- и объяснение на уровне
  // экрана становится третьим пересказом одной мысли.
  // Замерено на 390x844 до правки: первый пост начинался на 612px, то есть
  // три четверти первого экрана уходили на шапку и объяснение, и на телефоне
  // с адресной строкой пост оказывался за сгибом. Полный текст никуда не
  // делся -- он под «Подробнее».
  //
  // Единая модель очереди (C14, владелец 01-02.08): "или таймера" здесь
  // раньше подразумевало, что таймер -- это АЛЬТЕРНАТИВНЫЙ способ решения
  // (наравне с кнопкой). На деле таймер ничего не решает сам -- он либо
  // ждёт вашей кнопки, либо (не дождавшись) переносит пост в конец очереди.
  // Публикует только кнопка.
  const mechanicsLine = pendingCount > 0 ? "" : ((App.user?.tg_chat_id
    ? "Пост публикуется только по вашей кнопке «Опубликовать» — если пришлём карточку в Телеграм, на решение будет время, а не успеете — пост просто уедет в конец очереди."
    : "Мы ничего не публикуем сами, пока не можем вас предупредить: каждый пост ждёт вашей кнопки сколько угодно.") + " ");

  // КРИТИЧНО (правило 4 в CLAUDE.md, решение владельца 01-02.08): здесь было
  // «не отреагируете за N мин — опубликуем сами» -- то есть третий, негласный
  // путь публикации, который владелец явно отверг: таймер подтверждения
  // БОЛЬШЕ НЕ публикует, он переносит пост в конец очереди
  // (tasks._requeue_unconfirmed_post). Плюс было отдельное и тоже неверное
  // «пост, созданный вручную, ждёт решения сколько угодно» -- в единой модели
  // ручной пост встаёт в ту же очередь и получает тот же таймер, что и
  // плановый, разницы больше нет.
  return `<div class="card" style="background:var(--accent-soft);border:none;margin-bottom:14px;padding:14px 16px">
    <div style="font-size:13px;color:var(--accent-dark);font-weight:600">В очереди ${counter}</div>
    <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
      ${mechanicsLine}${refillLine}
    </div>
    <button class="btn-ghost btn-sm" style="margin-top:0;padding:4px 0;color:var(--accent-dark)"
      onclick="toggleQueueHelp()" id="queue_help_btn">Подробнее ▾</button>
    <div id="queue_help" class="hidden" style="font-size:13px;color:var(--text-dim);margin-top:8px;line-height:1.6;border-top:1px solid var(--border-soft);padding-top:8px">
      Новые посты мы пишем сами — ${_intervalLabel(c.interval_hours||12)}, плюс держим в запасе ${minQueue}. Каждый пост, плановый или написанный вами кнопкой «Написать сейчас», встаёт в одну и ту же очередь со своим временем.<br>
      ${App.user?.tg_chat_id
        ? `Мы присылаем карточку в Телеграм, и за ${softControlMin} мин до времени поста в очереди у неё есть обратный отсчёт: не подтвердите — пост НЕ опубликуется, а переедет в конец очереди с новым временем. Число постов при этом не меняется.<br>`
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
//
// tokensExhausted (C14, владелец 01.08, пункт 5): при нулевом балансе кнопка
// "+ Написать пост сейчас" всё равно упала бы (generate_for_channel выходит
// на token_balance<=0 первой же проверкой) -- молчаливая пустота на месте
// слота выглядела бы так, будто система просто не успела, хотя она и не
// собирается. Баннер _renderQueueStatus уже объясняет это выше, но именно
// пустой слот -- то место, где человек и тянется нажать кнопку, поэтому
// объяснение нужно продублировать прямо здесь (правило 5 в CLAUDE.md).
function _renderQueueSlots(pendingCount, minQueue, tokensExhausted, generating, ceiling){
  const missing = Math.max(0, minQueue - pendingCount);
  // Запас набран -- раньше здесь просто ничего не рисовалось, вместе с
  // кнопкой «Написать пост сейчас». При очереди из одного поста (владелец
  // 02.08 попросил разрешить такую глубину) пустое место под единственным
  // постом читается как «дальше ничего не будет», а способа увеличить
  // запас на экране не видно вовсе -- он в настройках.
  //
  // Кнопку без времени здесь НЕ показываем сознательно: при полной очереди
  // сервер отвечает на неё «Очередь уже заполнена» (generate_channel,
  // respect_queue_depth), и предлагать действие, которое заведомо не
  // сработает, значит обещать несуществующее (правило 5 в CLAUDE.md).
  // Работает только путь с явным временем -- его и предлагаем.
  if(!missing){
    // Про «запас набран, следующий напишем после ближайшей публикации» здесь
    // сознательно ни слова: это уже сказано в карточке статуса над очередью
    // (обе ветки _renderQueueStatus). Слот отвечает на другой вопрос -- что
    // человек может с этим сделать. Проверено на скриншоте: с обеими
    // фразами экран повторял одну мысль дважды подряд.
    const canGrow = (ceiling||minQueue) > minQueue;
    const growLine = canGrow
      ? `Хотите держать наготове больше — увеличьте «Глубину очереди» в настройках: сейчас ${minQueue}, можно до ${ceiling}.`
      : `Больше на вашем тарифе не держим.`;
    // Без queue-slot-muted: приглушённый стиль сделан для мест, которые мы
    // не можем заполнить (кончились токены), а здесь -- живая подсказка с
    // двумя кнопками, и на скриншоте она читалась хуже, чем должна.
    return `<div class="queue-slot">
      <div class="queue-slot-hint">${growLine}</div>
      <div style="display:flex;gap:6px;align-items:center;justify-content:center;margin-top:10px">
        <button class="btn-ghost btn-sm" ${_settingsQueueLink()}>Настройки очереди</button>
        <button class="btn-ghost btn-sm" title="Написать пост на выбранное время" onclick="toggleQueueGenPicker()">📅 Написать на своё время</button>
      </div>
      <div id="queue_gen_picker" class="hidden" style="margin-top:10px;padding:12px;background:var(--surface2);border-radius:10px;border:1px solid var(--border-soft)">
        <div class="row" style="gap:8px">
          <input type="datetime-local" id="queue_gen_dt" style="flex:1">
          <button class="btn btn-sm" onclick="genQueuePost(true)">Написать</button>
          <button class="btn-ghost btn-sm" onclick="$('queue_gen_picker').classList.add('hidden')">✕</button>
        </div>
      </div>
    </div>`;
  }
  // Рисуем не больше трёх заглушек: у оплатившего цель очереди 7, и при одном
  // готовом посте шесть пунктирных рамок подряд превратили бы экран в забор.
  // Точное число недостающих постов и так названо словами в статусе выше.
  const shown = Math.min(missing, 3);
  // C14, пункт 6 (владелец 01.08): пока идёт фоновая догенерация -- строка
  // "генерируется следующий пост" НАД кнопкой, а не вместо неё (кнопка
  // остаётся рабочей: пользователь может параллельно написать ещё один
  // пост вручную, это независимые действия). generating -- реальный флаг
  // с сервера (Channel.generating_since через tasks._set_generating), а не
  // декоративный таймер на фиксированное время (правило 5 в CLAUDE.md).
  const generatingLine = generating
    ? `<div class="queue-slot-hint" style="display:flex;align-items:center;gap:6px;justify-content:center;margin-bottom:8px"><span class="spinner"></span> Генерируется следующий пост…</div>`
    : "";
  let out = "";
  for(let i=0;i<shown;i++){
    if(tokensExhausted){
      out += i===0
        ? `<div class="queue-slot queue-slot-muted">
             <div class="queue-slot-hint">Токены закончились — это место останется пустым, пока не пополните баланс</div>
             <button class="btn-outline btn-sm" style="margin-top:8px" onclick="go('billing')">Пополнить баланс →</button>
           </div>`
        : `<div class="queue-slot queue-slot-muted"><div class="queue-slot-hint">Место для ещё одного поста</div></div>`;
      continue;
    }
    out += i===0
      ? `<div class="queue-slot">
           ${generatingLine}
           <div style="display:flex;gap:6px;align-items:center;justify-content:center">
             <button class="btn-outline btn-sm" id="queue_gen_btn" onclick="genQueuePost()">+ Написать пост сейчас</button>
             <button class="btn-ghost btn-sm" title="Выбрать время публикации" onclick="toggleQueueGenPicker()">📅</button>
           </div>
           <div class="queue-slot-hint">Не дожидаясь расписания — или выберите время кнопкой 📅</div>
           <div id="queue_gen_picker" class="hidden" style="margin-top:10px;padding:12px;background:var(--surface2);border-radius:10px;border:1px solid var(--border-soft)">
             <div class="row" style="gap:8px">
               <input type="datetime-local" id="queue_gen_dt" style="flex:1">
               <button class="btn btn-sm" onclick="genQueuePost(true)">Написать</button>
               <button class="btn-ghost btn-sm" onclick="$('queue_gen_picker').classList.add('hidden')">✕</button>
             </div>
           </div>
         </div>`
      : `<div class="queue-slot queue-slot-muted"><div class="queue-slot-hint">Место для ещё одного поста</div></div>`;
  }
  return out;
}

// C14, пункт 4: пикер даты/времени у "Написать пост сейчас" -- пост встаёт
// в очередь на выбранное место, а не на стандартный следующий слот.
function toggleQueueGenPicker(){
  const el=$("queue_gen_picker"); if(!el) return;
  el.classList.toggle("hidden");
  const dt=$("queue_gen_dt");
  if(dt && !dt.value) dt.value=_toLocalDatetimeInputValue(new Date(Date.now()+3600000));
}

let _genQueueInFlight=false;
async function genQueuePost(useTime){
  if(!requireAuth()) return;
  if(_genQueueInFlight) return;
  let payload={};
  if(useTime){
    const dt=$("queue_gen_dt");
    if(!dt||!dt.value) return toast("Выберите дату","err");
    payload={scheduled_at:_localDatetimeInputToUTCISOString(dt.value)};
  }
  _genQueueInFlight=true;
  const btn=$("queue_gen_btn");
  if(btn){btn.innerHTML='<span class="spinner"></span> Пишу пост…';btn.disabled=true;}
  try{
    await api("POST","/channels/"+App._chan.id+"/generate",payload);
    trackGoal("post_generated",{source:"queue_slot",channel_id:App._chan.id,scheduled:!!useTime});
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

function renderQueueCalendar(posts, forecastSlots){
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

  // Прогноз -- не настоящие посты, у них нет карточки и нечего открывать по
  // клику. Считаем отдельно от byDate, чтобы не путать с реальными постами:
  // публикация всё ещё честно происходит по расписанию в момент тика, а не
  // потому что дата была нарисована в календаре заранее.
  const forecastDates=new Set((forecastSlots||[]).map(iso=>_dateKey(new Date(iso))));

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
    const hasForecast=forecastDates.has(key);
    const isToday=key===todayKey;
    const isSelected=key===_calSelectedDate;
    const dots=items.slice(0,3).map(it=>`<span class="cal-dot cal-dot-${it._calKind}"></span>`).join("")
      +(hasForecast?`<span class="cal-dot cal-dot-forecast"></span>`:"");
    const more=items.length>3?`<span class="cal-more">+${items.length-3}</span>`:"";
    cells+=`<div class="cal-cell${isToday?" cal-cell-today":""}${isSelected?" cal-cell-selected":""}${items.length?" cal-cell-has":""}"
      ${items.length?`onclick="selectCalDate('${key}')"`:""}>
      <div class="cal-daynum">${day}</div>
      ${items.length||hasForecast?`<div class="cal-dots">${dots}${more}</div>`:""}
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
  <div class="cal-legend"><span><span class="cal-dot cal-dot-published"></span> Опубликован</span><span><span class="cal-dot cal-dot-scheduled"></span> Запланирован</span>${forecastSlots&&forecastSlots.length?`<span><span class="cal-dot cal-dot-forecast"></span> Ожидается по расписанию</span>`:""}</div>
  ${selectedBlock}`;
}


// Настраиваемая глубина очереди (C14, владелец 01.08): базово 3 поста,
// можно увеличить до потолка тарифа (queue_ceiling из _channel_dict, 7 у
// оплатившего). Значения выше потолка показаны, но заблокированы -- честнее,
// чем прятать их совсем: видно, куда расти, а не только что доступно сейчас.
function _renderQueueDepthRow(c){
  const ceiling = c.queue_ceiling || 3;
  const current = c.queue_depth || c.queue_target || 3;
  // Границы приходят с сервера: список, записанный руками, разошёлся бы с
  // зажимом в patch_channel молча -- кнопка была бы, а значение не
  // сохранялось бы. Минимум 1 (владелец 02.08): «держать наготове ровно
  // один пост» -- законный сценарий, человек хочет видеть следующий пост и
  // решать по нему, а не разбирать запас на неделю.
  const minDepth = c.queue_min_depth || 1;
  const options = [];
  for(let n=minDepth;n<=7;n++) options.push(n);
  // Подсказка называла условие, которого в коде нет: потолок поднимает не
  // тариф «Про», а ЛЮБОЙ платёж со статусом paid (tasks.queue_target_for_user
  // ищет просто `Payment.status == "paid"`, User.plan там не участвует).
  // Человек с оплаченным минимальным тарифом читал, что ему нужен «Про», —
  // хотя очередь у него уже открыта (аудит 02.08).
  //
  // Про уменьшение глубины говорим прямо: готовые посты мы не удаляем, и
  // если их сейчас больше нового значения, очередь сойдётся не сразу.
  const hint = ceiling < 7
    ? `Сколько готовых постов держим наготове одновременно. Сейчас доступно до ${ceiling}; после любой оплаты — до 7. Если уменьшить, уже написанные посты останутся: очередь сойдётся к новому значению по мере публикаций.`
    : `Сколько готовых постов держим наготове одновременно. Если уменьшить, уже написанные посты останутся: очередь сойдётся к новому значению по мере публикаций.`;
  return `<div class="toggle-row" style="align-items:flex-start">
    <div class="toggle-info" style="flex:1">
      <b>Глубина очереди</b><small>${hint}</small>
      <div class="seg" id="seg_queue_depth" style="max-width:320px;margin-top:8px">
        ${options.map(n=>{
          const disabled = n > ceiling;
          const on = n === current && !disabled;
          return `<button class="${on?"on":""}" ${disabled?`disabled title="Откроется после оплаты любого тарифа" style="opacity:.4;cursor:not-allowed"`:""} onclick="pickOpt('queue_depth',${n},'seg_queue_depth')">${n}</button>`;
        }).join("")}
      </div>
    </div>
  </div>`;
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
      <!-- Подтверждение показывает только «✓ Проверено», без подписи канала.
           Замер на 390px: строка «✓ Проверено · @канал» не помещалась в 214px
           и обрезалась многоточием -- то есть вторая копия хэндла была ещё и
           нечитаемой, а первая, целая, стоит в шапке на 365px выше, на том же
           экране. Сам хэндл никуда не делся: «Изменить» открывает поле, где он
           лежит целиком и правится. -->
      <label class="field mt"><span class="field-label">@username, ссылка t.me/ или ID</span>
        ${c.verified
          ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;background:var(--green-bg);border-radius:10px;margin-bottom:6px;flex-wrap:nowrap;overflow:hidden">
               <span style="color:var(--green);font-weight:600;font-size:13px">✓ Проверено</span>
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
        <div class="toggle-info"><b>Публиковать без проверки</b><small>Если включено — новые посты выходят в канал сами, когда приходит их время в очереди. Если выключено — пост тоже стоит в очереди со своим временем, но публикуется только после вашего «Опубликовать»; не успеете до этого времени — пост не выйдет, а переедет в конец очереди с новым временем. Подключите уведомления в Телеграм: посты придут туда с кнопками «Опубликовать», «Отклонить», «Редактировать», и за ${App.cfg?.soft_control_warning_minutes||10} мин до срока придёт предупреждение.</small></div>
        <label class="switch"><input type="checkbox" id="sw_auto" ${c.auto_publish?"checked":""}><span class="slider"></span></label>
      </div>
      <div class="toggle-row">
        <div class="toggle-info"><b>Искать новости в интернете</b></div>
        <label class="switch"><input type="checkbox" id="sw_web" ${c.use_web_search?"checked":""}><span class="slider"></span></label>
      </div>
      ${_renderQueueDepthRow(c)}
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
        <div class="toggle-info"><b>Пост ждёт подтверждения</b><small>Предупредим за ${App.cfg?.soft_control_warning_minutes||10} мин до срока — не успеете, пост уйдёт в конец очереди</small></div>
        <label class="switch"><input type="checkbox" id="sw_n4" ${App.user?.notify_approval_pending?"checked":""}><span class="slider"></span></label>
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
      <p style="font-size:13px;color:var(--text-dim);margin-bottom:12px">Напишем пост прямо сейчас — посмотрите, что получается с текущими настройками. Пост обычный: тратит токены и встаёт в очередь${c.auto_publish ? ', а когда придёт его время — выйдет в канал сам, как и остальные' : ', публиковать его или нет — решаете вы'}.</p>
      <button class="btn-outline" onclick="testPost()" id="testBtn">▷ Написать пост сейчас</button>
      <div id="test_result" style="margin-top:12px"></div>
    </div>
    <div class="row between mt-lg">
      <button class="btn-danger btn-sm" onclick="deleteChannel()">Удалить канал</button>
      <button class="btn" onclick="saveChannel()">Сохранить</button>
    </div>`;
}