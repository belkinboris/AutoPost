

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
  if(chans.length>0) return renderNewChannelSettings();
  return renderQuickStart();
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
  return `<div class="topbar">
    <a class="brand" onclick="go('dashboard')"><span class="brand-name">Авто<span>пост</span></span></a>
    <div class="topbar-right">
      <div class="token-pill" onclick="go('billing')">
        <span class="dot" style="background:var(--accent)"></span>
        <span style="font-size:13px;font-weight:500;color:var(--text-dim)">Тарифы</span>
      </div>
      <button class="btn-ghost btn-sm" onclick="logout()">Выйти</button>
    </div></div>${lowBanner}${back}`;
}

// DASHBOARD
function _intervalLabel(h){
  if(h<1) return `${Math.round(h*60)} мин`;
  if(h===1) return "каждый час";
  if(h<24) return `каждые ${h}ч`;
  return `каждые ${h/24|0}д`;
}
function _nextGenerationLabel(c){
  if(c.enabled===false) return "на паузе";
  // КРИТИЧНО (фикс противоречия): резерв очереди догенерируется на ближайшем
  // тике планировщика (раз в минуту), а НЕ по интервалу расписания -- см.
  // _refill_if_active в tasks.py. Пока очередь не заполнена до min_queue,
  // честный ответ "в ближайшие минуты". Раньше здесь всегда считался
  // интервал, и шапка канала обещала "через 12ч" ровно в тот момент, когда
  // пост на самом деле появлялся через минуту.
  const minQueue=c.queue_target||App.cfg?.min_queue||3;
  if(typeof c.queue_count==="number" && c.queue_count<minQueue) return "в ближайшие минуты";
  // Время следующей ГЕНЕРАЦИИ (не публикации!) = последняя генерация + интервал,
  // но не в прошлом. Публикация — отдельное понятие, происходит либо по явному
  // подтверждению пользователя, либо для scheduled-постов (см. renderPostCard).
  const intervalMs=(c.interval_hours||12)*3600000;
  const now=Date.now();
  let next;
  if(c.last_generated_at){
    next=new Date(c.last_generated_at+"Z").getTime()+intervalMs;
    if(next<now) next=now+60000;
  } else {
    next=now+intervalMs;
  }
  const diff=next-now;
  const h=Math.floor(diff/3600000),m=Math.floor((diff%3600000)/60000);
  const ts=new Date(next).toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"});
  return h>0?`через ${h}ч ${m}м (в ${ts})`:`через ${m}м (в ${ts})`;
}