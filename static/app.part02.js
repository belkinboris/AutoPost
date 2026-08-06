

// КРИТИЧНО (UX fix): withTimeout() -- голый await api() здесь мог зависнуть
// навсегда, если fetch не резолвится и не реджектится (нестабильная сеть,
// зависшее TCP-соединение внутри Telegram Mini App WebView) -- вызывающий
// код (renderDashboard и т.п.) тоже вис бы бесконечно на скелете загрузки
// без единой кнопки "Попробовать снова".
async function refreshUser(){
  try{
    const {timedOut, result} = await withTimeout(api("GET","/me"), 25000, "timeout");
    if(!timedOut) App.user=result;
  }catch(_){}
}

async function go(view,channelId){
  // Task B rule 2: все представления через go() — защищённые действия
  // залогиненного пользователя. Проверяем один раз здесь, не дублируя в
  // каждом отдельном обработчике (renderDashboard, renderQuickStart и т.д.).
  if(!requireAuth()) return;
  App.view=view;
  if(channelId!==undefined) App.channelId=channelId;
  if(view==="dashboard") return renderDashboard();
  if(view==="new_channel") return renderNewChannelRouter();
  if(view==="connect_channel") return renderConnectChannel();
  if(view==="channel") return renderChannel();
  if(view==="billing") return renderBilling();
}

async function renderNewChannelRouter(){
  // Task item 4: quick start — только для самого первого канала. Если у
  // пользователя уже есть хотя бы один канал, "Новый канал" должен вести
  // на полноценную форму с настройками, не на упрощённый онбординг.
  let chans=[];
  try{ chans = await api("GET","/channels"); }catch(_){}
  if(chans.length===0) return renderQuickStart();

  // Найдено владельцем 31.07: лимит каналов раньше проверялся только на
  // сервере, ПОСЛЕ того как человек заполнял всю форму (название, тема,
  // username) и жал "Создать канал" — обидно тратить время впустую, если
  // тариф всё равно не позволит. Проверяем здесь же, до формы.
  await refreshUser();
  const limit=App.user?.channel_limit ?? 0;
  if(limit>0 && chans.length>=limit) return renderChannelLimitReached(chans.length, limit);
  return renderNewChannelSettings();
}

function renderChannelLimitReached(count, limit){
  $("app").innerHTML=topbar("dashboard","назад")+`<div class="wrap" style="max-width:480px;text-align:center;margin-top:60px">
    <div style="font-size:32px;margin-bottom:12px">📺</div>
    <h2>Каналов на тарифе больше нет</h2>
    <p style="color:var(--text-dim);margin:12px 0 20px">
      На вашем тарифе доступно ${limit} ${_plural(limit,"канал","канала","каналов")}, а у вас уже ${count}.
      Чтобы добавить ещё один, выберите тариф побольше.
    </p>
    <button class="btn" style="width:100%;justify-content:center" onclick="go('billing')">Перейти к тарифам</button>
  </div>`;
}

// AUTH
function _tgAuthInitData(){
  try{ return window.Telegram?.WebApp?.initData || ""; }catch(_){ return ""; }
}
function _tgAuthFirstName(){
  try{ return window.Telegram?.WebApp?.initDataUnsafe?.user?.first_name || "Телеграм"; }catch(_){ return "Телеграм"; }
}
function toggleTgEmailFallback(){
  const c=$("email_auth_card");
  if(c) c.style.display = c.style.display==="none" ? "" : "none";
}
async function tgContinueAuth(){
  const initData=_tgAuthInitData();
  if(!initData) return;
  const btn=$("tgContinueBtn");
  const originalLabel=btn?btn.innerHTML:"";
  if(btn){btn.innerHTML='<span class="spinner"></span> Входим…';btn.disabled=true;}
  const body={init_data:initData};
  try{
    const lpSession=localStorage.getItem("ap_lp_session");
    if(lpSession){
      body.lp_session=lpSession;
      const utm=JSON.parse(localStorage.getItem("ap_lp_utm")||"{}");
      if(utm.utm_source) body.utm_source=utm.utm_source;
      if(utm.utm_medium) body.utm_medium=utm.utm_medium;
      if(utm.utm_campaign) body.utm_campaign=utm.utm_campaign;
      if(utm.utm_content) body.utm_content=utm.utm_content;
    }
  }catch(_){}
  try{
    const r=await api("POST","/auth/telegram_miniapp",body);
    App.token=r.token;localStorage.setItem("ap_token",r.token);
    trackGoal(r.is_new?"register_success":"login_success");
    tgHaptic("success");
    await boot();
  }catch(e){
    tgHaptic("error");
    toast(e&&e.message?e.message:"Не удалось войти через Телеграм","err");
    if(btn){btn.innerHTML=originalLabel;btn.disabled=false;}
  }
}

function renderAuth(mode="login"){
  // Task item 4 (Mini App): если открыто внутри Telegram (initData
  // присутствует) и токена ещё нет — предлагаем вход в одно нажатие вместо
  // формы email/пароль. Форма email остаётся доступна ссылкой ниже — не
  // теряем пользователей, у которых уже есть аккаунт с другого устройства/
  // браузера (initData там недоступна, а localStorage Telegram WebView не
  // делится с обычным браузером).
  const tgInitData=_tgAuthInitData();
  $("app").innerHTML=`<div class="auth-wrap"><div class="auth-box">
    <div class="auth-logo">Авто<span>пост</span></div>
    <div class="auth-sub">ИИ пишет посты для вашего Телеграм-канала — на автопилоте или после подтверждения</div>
    ${tgInitData?`<div class="card" style="text-align:center">
      <div style="font-size:14px;color:var(--text-dim);margin-bottom:14px">Вы открыли АвтоПост в Телеграм</div>
      <button class="btn" style="width:100%;justify-content:center;padding:14px" id="tgContinueBtn" onclick="tgContinueAuth()">Продолжить как ${esc(_tgAuthFirstName())}</button>
      <div style="margin-top:12px">
        <button class="btn-ghost btn-sm" style="color:var(--text-faint)" onclick="toggleTgEmailFallback()">Уже есть аккаунт с email? →</button>
      </div>
    </div>`:""}
    <div class="card" id="email_auth_card" style="${tgInitData?"display:none;margin-top:14px":""}">
      <label class="field"><span class="field-label">Email</span>
        <input id="em" type="email" placeholder="you@mail.ru" autocomplete="username"></label>
      <label class="field mt"><span class="field-label">Пароль</span>
        <input id="pw" type="password" placeholder="минимум 6 символов"></label>
      ${mode==="register"?`<label class="field mt"><span class="field-label">Реферальный код (необязательно)</span>
        <input id="ref" placeholder="код друга"></label>`:""}
      <button class="btn" style="width:100%;margin-top:18px;justify-content:center" id="authBtn">
        ${mode==="login"?"Войти":"Создать аккаунт"}</button>
      ${mode==="register"?`<div style="font-size:12px;color:var(--text-faint);text-align:center;margin-top:10px;line-height:1.5">
        Регистрируясь, вы принимаете <a href="/legal/offer" target="_blank">условия оферты</a>
        и <a href="/legal/privacy" target="_blank">политику конфиденциальности</a></div>`:""}
      <div class="auth-switch">${mode==="login"
        ?`Нет аккаунта? <a id="sw">Зарегистрироваться →</a>`
        :`Уже есть аккаунт? <a id="sw">Войти</a>`}</div>
    </div></div></div>`;
  $("authBtn").onclick=async()=>{
    const email=$("em").value.trim(),password=$("pw").value;
    if(!email||!password) return toast("Заполните email и пароль","err");
    const body={email,password};
    if(mode==="register"&&$("ref")&&$("ref").value.trim()) body.ref_code=$("ref").value.trim();
    if(mode==="register"){
      try{
        const lpSession=localStorage.getItem("ap_lp_session");
        if(lpSession){
          body.lp_session=lpSession;
          const utm=JSON.parse(localStorage.getItem("ap_lp_utm")||"{}");
          if(utm.utm_source) body.utm_source=utm.utm_source;
          if(utm.utm_medium) body.utm_medium=utm.utm_medium;
          if(utm.utm_campaign) body.utm_campaign=utm.utm_campaign;
          if(utm.utm_content) body.utm_content=utm.utm_content;
        }
      }catch(_){}
    }
    try{
      const isRegister = mode === "register";
      const r=await api("POST",isRegister?"/register":"/login",body);
      App.token=r.token;localStorage.setItem("ap_token",r.token);
      trackGoal(isRegister?"register_success":"login_success");
      // register_success в LandingEvent пишет backend /api/register
      // после реального создания пользователя — фронт не дублирует это событие.
      await boot();
    }catch(e){
      // КРИТИЧНО (Task A fix): явная, предсказуемая классификация ошибки —
      // не полагаемся на хрупкое совпадение подстрок типа "401" (могло
      // случайно сработать не на том сообщении). api() уже гарантирует, что
      // для /login и /register никогда не бросается "Сессия истекла" (это
      // исключено на уровне api() для этих двух путей) — здесь только
      // явные, ожидаемые варианты текста с backend и сети.
      const raw = (e && e.message) || "";
      let msg;
      if (raw.includes("Failed to fetch") || raw.includes("NetworkError") || raw.includes("network")) {
        msg = "Не удалось подключиться. Проверьте интернет и попробуйте ещё раз.";
      } else if (raw.includes("уже есть") || raw.toLowerCase().includes("already")) {
        msg = "Этот email уже зарегистрирован.";
      } else if (raw.includes("Неверный email или пароль")) {
        msg = "Неверный email или пароль.";
      } else if (raw.includes("6 символ")) {
        msg = "Пароль должен быть не менее 6 символов.";
      } else if (raw) {
        // Любой другой текст с backend — показываем как есть, не подменяем
        // на дженерик и тем более не на "сессия истекла".
        msg = raw;
      } else {
        msg = "Что-то пошло не так. Попробуйте ещё раз.";
      }
      toast(msg,"err");
    }
  };
  if($("sw")) $("sw").onclick=()=>renderAuth(mode==="login"?"register":"login");
  $("pw").onkeydown=e=>{if(e.key==="Enter") $("authBtn").click();};
}

// TOPBAR
function topbar(backView,backLabel){
  const back=backView?`<div class="back-row"><button class="back-link" onclick="go('${backView}')">← ${backLabel||"назад"}</button></div>`:"";
  // Task D fix: не показываем "токены" пользователю и не считаем точное
  // количество постов через жёсткое деление — это создавало неточный текст
  // вида "осталось ~1 пост" при старом малом лимите. После увеличения
  // бесплатной квоты до 200k порог пересчитан пропорционально (раньше был
  // 20000 при квоте ~111000, те же ~18% от квоты).
  const low=App.user&&App.user.token_balance<36000;
  if(low && !window._quotaWarningLogged){
    window._quotaWarningLogged=true; // раз за вкладку, не на каждый рендер topbar()
    logProductEvent("quota_warning_seen");
  }
  const lowBanner=low?`<div style="background:#fef3c7;border-bottom:1px solid #f59e0b;padding:8px 20px;font-size:13px;text-align:center;color:#92400e">
    ⚠️ Баланс заканчивается.
    <a onclick="go('billing')" style="color:#92400e;font-weight:600;cursor:pointer;text-decoration:underline">Пополнить →</a></div>`:"";
  // Найдено владельцем 31.07: шапка на КАЖДОЙ странице писала обезличенное
  // "Тарифы", даже когда тариф уже оплачен -- узнать, какой именно тариф
  // активен, можно было только зайдя в сам раздел оплаты. Теперь показываем
  // название тарифа прямо здесь, если он есть (App.user.plan_title из /api/me).
  const planLabel=App.user?.plan_title?`Тариф: ${esc(App.user.plan_title)}`:"Тарифы";
  return `<div class="topbar">
    <a class="brand" onclick="go('dashboard')"><span class="brand-name">Авто<span>пост</span></span></a>
    <div class="topbar-right">
      <div class="token-pill" onclick="go('billing')">
        <span class="dot" style="background:var(--accent)"></span>
        <span style="font-size:13px;font-weight:500;color:var(--text-dim)">${planLabel}</span>
      </div>
      <button class="btn-ghost btn-sm" onclick="logout()">Выйти</button>
    </div></div>${lowBanner}${back}`;
}

// DASHBOARD
// Все ветки начинаются одинаково («каждые …»), потому что подставляется в
// середину фразы: «Мы сами пишем и публикуем посты — ${_intervalLabel(h)}».
// Раньше при интервале меньше часа выпадало и «каждые» («…посты — 30 мин»),
// а 24 часа превращались в «каждые 1д» — счётчик дней делением нацело.
function _intervalLabel(h){
  if(h<1) return `каждые ${Math.round(h*60)} мин`;
  if(h===1) return "каждый час";
  if(h<24) return `каждые ${h} ${_plural(h,"час","часа","часов")}`;
  if(h===24) return "раз в сутки";
  const d=h/24;
  return Number.isInteger(d)
    ? `каждые ${d} ${_plural(d,"день","дня","дней")}`
    : `каждые ${h} ${_plural(h,"час","часа","часов")}`;
}
// Текст обратного отсчёта на карточке канала. Один и тот же и для первой
// отрисовки, и для тика раз в секунду (startDashboardCountdowns) -- иначе
// они разъезжаются: так на карточке уже висело «⏱ через 4350:00», потому
// что тикающий формат MM:SS остался от времён, когда дедлайн был 30 минут,
// а после C14 он равен времени поста в очереди, то есть дням.
function _publishCountdownText(kind, diff){
  if(kind==="auto"){
    return diff>0 ? `📤 Опубликуем через ${humanDuration(diff)}` : "📤 Публикуем…";
  }
  if(kind==="confirm-timer"){
    // Таймер есть только когда карточка реально доставлена в Телеграм
    // (см. правило 4 в CLAUDE.md). Он НЕ публикует -- он переносит.
    return diff>0
      ? `📝 Ждём решения — через ${humanDuration(diff)} перенесём в конец очереди`
      : "📝 Время вышло — переносим пост в конец очереди";
  }
  return diff>0
    ? `📝 Место в очереди — через ${humanDuration(diff)}, ждёт вашей кнопки`
    : "📝 Время подошло — пост ждёт вашей кнопки";
}

// Что происходит с ближайшим постом канала: когда он выйдет (автопилот) или
// когда подойдёт его место в очереди (режим подтверждения).
//
// Владелец 02.08: на карточке канала было написано время следующей
// ГЕНЕРАЦИИ, а человека интересует публикация — когда пост увидят
// подписчики. Генерация -- внутренняя кухня, она важна только когда
// публиковать ещё нечего; тогда и показываем её (последняя ветка).
//
// Возвращает {text, at, kind}: `at` -- время в миллисекундах для живого
// отсчёта (null, если отсчитывать нечего), `kind` -- какая из формулировок
// выше верна для этого канала.
function _nextPublishInfo(c){
  if(c.enabled===false) return {text:"⏸ На паузе — ничего не публикуется", at:null, kind:null};
  // Публиковать некуда: без подтверждённого бота tick() этот канал не берёт
  // вообще (см. `c.verified` в tasks.tick), и любое время публикации здесь
  // было бы обещанием, которого система не выполняет.
  if(!c.tg_chat || !c.verified) return {text:"⚠️ Канал не подключён — публиковать некуда", at:null, kind:null};

  if(c.next_post_at){
    if(c.auto_publish){
      const at=new Date(c.next_post_at).getTime();
      return {text:_publishCountdownText("auto", at-Date.now()), at, kind:"auto"};
    }
    // Режим подтверждения. Если карточка в Телеграм доставлена, у поста
    // идёт таймер переноса (approval_deadline == scheduled_at после C14) --
    // тогда честнее считать по нему, он и есть то, что произойдёт само.
    const kind=c.approval_deadline?"confirm-timer":"confirm";
    const at=new Date(c.approval_deadline||c.next_post_at).getTime();
    return {text:_publishCountdownText(kind, at-Date.now()), at, kind};
  }

  // Пост без времени в очереди (онбординг-черновик) -- публиковать по
  // расписанию нечего, но пост есть и ждёт человека.
  if((c.queue_count||0)>0) return {text:"📝 Пост готов и ждёт вашего решения", at:null, kind:null};

  // Публиковать нечего вообще -- вот теперь про генерацию: это единственный
  // случай, когда «когда напишем» и есть ответ на «когда выйдет».
  return {text:_nextGenerationLabel(c), at:null, kind:null};
}

// Возвращает готовую строку под заголовком карточки канала целиком (вместе
// со значком), а не хвост чужой фразы: у состояний разный смысл и разный
// значок, и склеивать их с одним общим началом «⏱ Следующая генерация …»
// значило бы подгонять правду под шаблон.
function _nextGenerationLabel(c){
  if(c.enabled===false) return "На паузе";
  // Пустой баланс -- первым делом: generate_for_channel в tasks.py выходит на
  // `user.token_balance <= 0` самой первой проверкой, поэтому при нуле не
  // сработает ничего -- ни расписание, ни резерв, ни кнопка. Карточка же
  // бодро обещала «в ближайшие минуты» каналу, на котором не могло появиться
  // ни одного поста (правило 5 в CLAUDE.md; на экране очереди эта проверка
  // была, на дашборде -- нет).
  if((App.user?.token_balance||0)<=0) return "⚠️ Новые посты не пишем — закончились токены";
  // Генерация остановлена после нескольких неудач подряд (аудит 05.08).
  // Экран очереди это уже показывал (app.part11.js), а дашборд -- нет, и
  // карточка обещала «в ближайшие минуты» каналу, где _refill_queue выходит
  // на gen_fail_streak и не пишет НИЧЕГО (правило 5: не обещать того, чего
  // система не делает). generation_stopped_reason приходит с сервера.
  if(c.generation_stopped) return "⚠️ " + (c.generation_stopped_reason || "Новые посты пока не пишем — была ошибка");
  // Единая модель очереди (C14, решение владельца 01-02.08): _refill_queue
  // в tasks.py держит очередь заполненной до queue_target одинаково для
  // обоих режимов публикации (autopilot больше не публикует пост напрямую
  // мимо очереди -- см. generate_for_channel) -- поэтому "очередь не полна,
  // следующий пост появится на ближайшем тике" верно для любого режима, а
  // не только для "публикация после подтверждения".
  const minQueue=c.queue_target||App.cfg?.min_queue||3;
  const inQueue=typeof c.queue_count==="number"?c.queue_count:0;
  if(inQueue<minQueue) return "⏱ Следующий пост — в ближайшие минуты";
  // Очередь полна. Здесь стояла формула «last_generated_at + интервал» --
  // её нет ни в одной строке сервера: _refill_queue смотрит не на время
  // последней генерации, а на длину очереди, и пишет новый пост только
  // когда в ней освободилось место. Освобождает место публикация, поэтому
  // честный ответ -- время ближайшего поста в очереди (аудит 02.08).
  if(c.next_post_at){
    const diff=new Date(c.next_post_at).getTime()-Date.now();
    if(diff>0) return `⏱ Запас набран, следующий напишем через ${humanDuration(diff)} — после ближайшей публикации`;
  }
  return "⏱ Запас набран, следующий напишем после ближайшей публикации";
}