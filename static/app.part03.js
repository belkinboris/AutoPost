

async function renderDashboard(){
  // Skeleton сразу, без ожидания данных (task item 4 acceptance criteria:
  // "dashboard shows skeleton/loader immediately, no blank screen").
  $("app").innerHTML=`<div class="wrap"><div class="text-faint" style="padding:40px;text-align:center">Загрузка…</div></div>`;

  const tUserStart = performance.now();
  await refreshUser();
  console.log(`[timing] refreshUser() в renderDashboard: ${(performance.now()-tUserStart).toFixed(0)}ms`);

  let chans=[];
  let channelsLoadFailed=false;
  const tChansStart = performance.now();
  try{
    // withTimeout(): без него зависший (не отклонённый, не разрешённый)
    // fetch держал бы пользователя на скелете "Загрузка…" бесконечно, без
    // единой кнопки повтора -- ровно то, что видно как "вечная загрузка"
    // при первом открытии Mini App на нестабильной сети.
    const {timedOut, result} = await withTimeout(api("GET","/channels"), 25000, "timeout");
    if(timedOut) throw new Error("Сервер долго не отвечает. Проверьте соединение.");
    chans=result;
  }catch(e){
    channelsLoadFailed=true;
    const msg=(e&&e.message)||"";
    const isAuthFailure = msg.includes("Сессия истекла") || msg.includes("Не авторизован") || msg.includes("Пользователь не найден");
    if(isAuthFailure){
      // КРИТИЧНО (Task B fix): api() уже вызвал logout() сам для этого
      // случая — здесь просто показываем экран входа и ВЫХОДИМ, не
      // продолжаем выполнение. Раньше этот catch проглатывал ошибку молча,
      // chans оставался пустым массивом, и код ниже интерпретировал это
      // как "новый пользователь без каналов" -> показывал quick start.
      // Реальная причина — истёкшая сессия, не отсутствие каналов. Это и
      // была причина "quick start появляется неожиданно" + "не авторизован
      // до hard reload": пользователь оказывался на quick start с мёртвым
      // токеном, и любое защищённое действие там падало с 401 заново.
      return renderAuth();
    }
    // Не auth-ошибка (сеть, временный сбой backend) — показываем тост,
    // но НЕ показываем quick start как будто пользователь новый.
    toast(msg||"Ошибка запроса","err");
  }
  console.log(`[timing] /channels: ${(performance.now()-tChansStart).toFixed(0)}ms, count=${chans.length}`);

  if(channelsLoadFailed){
    // Сеть/временный сбой — даём пользователю явный повтор, не выдаём
    // молчаливый quick start или пустой дашборд.
    $("app").innerHTML = `<div class="wrap" style="max-width:480px;text-align:center;margin-top:60px">
      <p style="color:var(--text-dim)">Не удалось загрузить список каналов.</p>
      <button class="btn" style="margin-top:12px" onclick="go('dashboard')">Попробовать снова</button>
    </div>`;
    return;
  }

  if(!chans.length){
    if(App._onboardingSkipped){
      // Пользователь явно нажал "Пропустить" — показываем пустой dashboard
      // с явным призывом создать канал, не зацикливаем обратно на quick start.
      $("app").innerHTML=topbar()+`<div class="wrap">
        <div class="page-head"><h1>Ваши каналы</h1><p>Пока нет ни одного канала.</p></div>
        <div class="grid grid-3">
          <div class="add-card" onclick="go('new_channel')"><div class="plus">+</div>
            <div style="font-size:14px;font-weight:500">Новый канал</div></div>
        </div></div>`;
      return;
    }
    return renderQuickStart(); // новый пользователь — сразу к первому посту, без пустого дашборда
  }
  $("app").innerHTML=topbar()+`<div class="wrap">
    <div class="page-head"><h1>Ваши каналы</h1><p>ИИ пишет посты сам — вам остаётся выбрать лучший.</p></div>
    <div class="grid grid-3" id="chans"><div class="text-faint">Загрузка…</div></div></div>`;
  $("chans").innerHTML=chans.map(c=>renderChanCard(c)).join("")+`<div class="add-card" onclick="go('new_channel')"><div class="plus">+</div>
    <div style="font-size:14px;font-weight:500">Новый канал</div></div>`;
  startDashboardCountdowns();
}

function renderChanCard(c){
  const initial=(c.title||"?").trim().charAt(0).toUpperCase()||"?";
  const connected=!!(c.tg_chat && c.verified);
  // Та же пара, что и на экране канала: «канал не указан» под названием и
  // «Канал ещё не подключён.» в рамке под ним. Рамка появляется при том же
  // условии и вдобавок даёт ссылку «Как подключить →» -- подпись без хэндла
  // не добавляет ничего, кроме повтора.
  const handle=c.tg_chat?esc(c.tg_chat):"";

  if(!connected){
    const hint=c.tg_chat?"Бот пока не подтверждён администратором.":"Канал ещё не подключён.";
    return `<div class="chan-card" onclick="go('channel',${c.id})">
      <div class="chan-card-top">
        <div class="tg-ava">${initial}</div>
        <div style="min-width:0">
          <h3>${esc(c.title)}</h3>
          ${handle?`<div class="chan-handle">${handle}</div>`:""}
        </div>
      </div>
      <div class="chan-connect-hint">${hint} <a href="/how-to" target="_blank" rel="noopener" onclick="event.stopPropagation()">Как подключить →</a></div>
    </div>`;
  }

  let statusLabel, dotClass, subLine;
  if(c.auto_publish){
    statusLabel="Автоматическая публикация"; dotClass="status-dot-green";
    subLine=c.enabled===false?"На паузе":`⏱ Следующая генерация ${_nextGenerationLabel(c)}`;
  } else {
    statusLabel="Вы подтверждаете каждый пост"; dotClass="status-dot-accent";
    subLine=c.enabled===false?"На паузе":`⏱ Следующая генерация ${_nextGenerationLabel(c)}`;
  }

  const countdownAttr=(!c.auto_publish && c.approval_deadline)
    ? ` data-approval-countdown="${new Date(c.approval_deadline).getTime()}"` : "";
  const sublineHtml=countdownAttr
    ? `<div class="status-subline"${countdownAttr}>⏱ считаем…</div>`
    : `<div class="status-subline">${esc(subLine)}</div>`;

  // renderTg(), не esc(): next_post_preview содержит настоящую Telegram-разметку
  // (<b>/<i> как в посте) — esc() экранировал её в текст, и в превью буквально
  // было видно "<b>...</b>" вместо жирного выделения.
  // «Посты скоро появятся» верно только для работающего канала. Планировщик
  // берёт каналы с enabled == True (см. tick и _refill_if_active в tasks.py),
  // поэтому на паузе не создаётся ничего — а карточка обещала обратное, причём
  // строкой ниже той, где написано «На паузе». Про саму паузу тут не
  // повторяем: это уже сказано в статусе выше, здесь только последствие.
  const emptyLine=c.enabled===false
    ? "Новые посты не создаются"
    : "Очередь пуста — посты скоро появятся";
  const preview=c.next_post_preview
    ? `<div class="chan-preview">${renderTg(c.next_post_preview)}</div>`
    : `<div class="chan-preview chan-preview-empty">${emptyLine}</div>`;

  return `<div class="chan-card" onclick="go('channel',${c.id})">
    <div class="chan-card-top">
      <div class="tg-ava">${initial}</div>
      <div style="min-width:0">
        <h3>${esc(c.title)}</h3>
        <div class="chan-handle">${handle}</div>
      </div>
    </div>
    <div class="status-line"><span class="status-dot ${dotClass}"></span>${statusLabel}</div>
    ${sublineHtml}
    ${preview}
    <div class="chan-foot"><span>Опубликовано: ${c.published_count}</span><span>В очереди: ${c.queue_count}</span></div>
  </div>`;
}

// ONBOARDING — переменные используются старой полной формой (renderNewChannel),
// доступной из вкладки "Расширенные" внутри уже созданного канала.
let _ncType="thematic";
let _ncVoice="author",_ncFormat="story",_ncEmoji="minimal",_ncCta=false,_ncCtaText="",_ncHz=12,_ncStyleProfile="";

// QUICK START — минимальный онбординг: тема -> первый пост, без подключения канала
function renderQuickStart(){
  trackGoal("quick_start_viewed");

  // КРИТИЧНО (P0 fix, task item 1): каждый новый quick start начинается с
  // полностью чистого состояния.
  App._qsRequestId = "qs" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  App.channelId = null;
  App._qsAbout = "";
  App._chan = null;
  // Сбрасываем и @username: иначе канал, введённый в прошлой попытке
  // онбординга, молча приклеился бы к следующему созданному каналу.
  App._qsChannelHandle = "";

  // Экран выбора: что делать сначала?
  // «Пропустить» здесь было дважды: сверху слева и под кнопками выбора. Обе
  // вели в qsSkip(), то есть одна и та же команда стояла на экране два раза --
  // на первом же экране после регистрации. Верхнюю убрали: она вдобавок
  // притворялась навигацией (место back-link, но стрелка вправо), а нижняя
  // стоит там, где вторичное действие и ожидают.
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <div class="page-head" style="text-align:center;margin-top:32px">
      <h1 style="font-family:'Instrument Serif',serif;font-size:30px;font-weight:400">Что сделать сначала?</h1>
      <p style="color:var(--text-dim)">Выберите — с чего начнём</p>
    </div>
    <div style="display:flex;flex-direction:column;gap:12px;margin-top:24px">
      <button class="btn" style="width:100%;justify-content:center;padding:16px;font-size:16px"
        onclick="qsChooseGenerate()">Сгенерировать первый пост</button>
      <button onclick="qsChooseAnalyze()"
        style="width:100%;padding:16px;font-size:16px;border-radius:10px;border:1.5px solid var(--border);background:var(--surface);cursor:pointer;font-family:inherit;color:var(--text)">
        Проанализировать мой Телеграм-канал
      </button>
    </div>
    <div style="text-align:center;margin-top:20px">
      <button class="btn-ghost btn-sm" style="color:var(--text-faint)" onclick="qsSkip()">Пропустить</button>
    </div>
  </div>`;
}

function qsChooseGenerate(){
  logProductEvent("onboarding_choice_selected", "generate_first_post");
  renderQuickStartGenerate();
}

function qsChooseAnalyze(){
  logProductEvent("onboarding_choice_selected", "analyze_existing_channel");
  // Безопасный первый шаг: запросить @username канала.
  // Не обещаем полноценный анализ — показываем что принято к сведению
  // и переходим к генерации с подсказкой темы из имени канала.
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <button class="back-link" style="margin-top:12px" onclick="renderQuickStart()">← Назад</button>
    <div class="page-head" style="text-align:center;margin-top:8px">
      <h1 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">Ваш канал</h1>
      <p style="color:var(--text-dim)">Укажите @username или ссылку на канал</p>
    </div>
    <div class="card">
      <input id="qs_channel_link" placeholder="@mychannel или t.me/mychannel" style="font-size:15px;width:100%">
    </div>
    <p style="color:var(--text-faint);font-size:13px;margin-top:8px;text-align:center">
      Запомним канал, чтобы не вводить его при подключении. Стиль скопируем позже,
      в настройках канала — там для этого есть отдельный шаг.
    </p>
    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="qsAnalyzeSubmit()">Продолжить</button>
    <div style="text-align:center;margin-top:12px">
      <button class="btn-ghost btn-sm" style="color:var(--text-faint)" onclick="renderQuickStartGenerate()">
        Пропустить — начать с темы
      </button>
    </div>
  </div>`;
  setTimeout(()=>{const el=$("qs_channel_link");if(el) el.focus();},100);
}

function qsAnalyzeSubmit(){
  const raw=($("qs_channel_link").value||"").trim();
  if(!raw) return toast("Укажите @username канала","err");
  const handle=raw.replace(/^https?:\/\/t\.me\//,"@").replace(/^t\.me\//,"@").replace(/^@+/,"@");
  // Раньше handle уходил в renderQuickStartGenerate как ТЕМА и подставлялся в
  // поле «о чём сделать пост». Человек, попросивший «пиши в стиле моего
  // канала», получал пост про строку «@durov», а у канала навсегда
  // оставалась тема «@durov». Теперь запоминаем его как username канала --
  // он пригодится при подключении, -- а тему спрашиваем отдельно.
  App._qsChannelHandle = handle;
  renderQuickStartGenerate();
}

function renderQuickStartGenerate(prefillTopic){
  // Экран с textarea — существующий quick start flow без изменений логики генерации.
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <button class="back-link" style="margin-top:12px" onclick="renderQuickStart()">← Назад</button>
    <div class="page-head" style="text-align:center;margin-top:8px">
      <h1 style="font-family:'Instrument Serif',serif;font-size:30px;font-weight:400">О чём сделать первый пост?</h1>
      <!-- Было двумя абзацами подряд, и оба говорили одно и то же: «сначала
           покажем пример поста» и «канал подключите позже». Второй абзац
           добавлял к этому только перечисление настроек -- его и оставили. -->
      <p style="color:var(--text-dim)">Сначала покажем пример поста. Канал, стиль, длину и расписание настроите потом.</p>
      ${App._qsChannelHandle?`<p style="color:var(--text-faint);font-size:13px;margin-top:6px">Канал ${esc(App._qsChannelHandle)} запомнили — подключим его после первого поста.</p>`:""}
    </div>
    <div class="card">
      <textarea id="qs_about" rows="3" placeholder="Например: M&A сделки в России, Roblox, салон красоты, криптоновости" style="font-size:15px"></textarea>
    </div>
    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="qsGenerate()" id="qs_btn">Сгенерировать пост</button>
  </div>`;
  if(prefillTopic){const el=$("qs_about");if(el) el.value=prefillTopic;}
  setTimeout(()=>{const el=$("qs_about");if(el) el.focus();},100);
}

function qsSkip(){
  // Флаг "пропустил онбординг" — теперь сохраняется в localStorage,
  // чтобы переживать перезагрузку. Без этого renderDashboard() при
  // chans.length===0 снова показывал quick start.
  trackGoal("quick_start_skipped");
  logProductEvent("onboarding_choice_selected", "skip");
  App._onboardingSkipped = true;
  localStorage.setItem("ap_onboarding_skipped", "1");
  go("dashboard");
}