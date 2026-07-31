const API_BASE=(typeof window!=="undefined"&&window.API_BASE)||"";
// Продакшен: необработанная ошибка не должна стирать весь интерфейс
// пользователю -- это хуже, чем сама ошибка. Логируем полную информацию
// в консоль (для отладки со скриншотом консоли), а на экране -- обычный
// тост тем же способом, что и остальные ошибки в приложении. Если по
// какой-то причине toast() ещё не готов -- не падаем молча, просто нет
// визуала, страница всё равно остаётся рабочей.
window.onerror = function(msg, src, line, col, err) {
  console.error('[JS Error]', msg, 'at', src + ':' + line + ':' + col, err);
  try { toast('Что-то пошло не так. Попробуйте ещё раз.', 'err'); } catch (_) {}
  return false;
};
window.addEventListener('unhandledrejection', function(e) {
  console.error('[Promise Error]', e.reason);
  try { toast((e.reason && e.reason.message) || 'Что-то пошло не так. Попробуйте ещё раз.', 'err'); } catch (_) {}
});

// Автопост SPA — полная версия
const App = {
  token: localStorage.getItem("ap_token"),
  user: null, cfg: null, view: "dashboard",
  channelId: null, tab: "queue", _chan: null,
  _onboardPosts: null, _consultHistory: [],
  // Флаг "пользователь осознанно пропустил онбординг" -- переживает перезагрузку.
  // Без этого renderDashboard() при chans.length===0 каждый раз возвращает на quick start.
  _onboardingSkipped: !!localStorage.getItem("ap_onboarding_skipped"),
};
const $ = id => document.getElementById(id);
const esc = s => (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":'&#39;'}[c]));
const fmt = n => (n||0).toLocaleString("ru-RU");

function trackGoal(goal, params={}){
  try{
    if(window.ym && window.YM_COUNTER_ID){
      window.ym(window.YM_COUNTER_ID,"reachGoal",goal,params);
    }
  }catch(_){}
}

// ── CTA/Journey Diagnostics: захват lp_session + UTM из URL лендинга (Part 4) ──
const _sentLandingEvents = new Set(); // дедуп в рамках одной загрузки страницы

// Возвращает true, если это похоже на свежий приход с лендинга/рекламы
// (не возврат уже знакомого с продуктом пользователя) -- boot() использует
// это, чтобы показать сразу форму регистрации, а не входа (см. ниже).
function captureLandingSession(){
  try{
    // 1. Telegram Mini App: ?startapp=lp_<id> приходит как start_param, не как обычный query-параметр
    const tg=window.Telegram?.WebApp;
    const startParam=tg?.initDataUnsafe?.start_param || new URLSearchParams(window.location.search).get("tgWebAppStartParam");
    if(startParam && startParam.startsWith("lp_")){
      const sessionId=startParam.slice(3);
      localStorage.setItem("ap_lp_session",sessionId);
      // Это открытие Mini App из лендинга, а не серверный /start у бота —
      // событие отражает именно это (см. diagnostic_notes на backend).
      logLandingEventWeb("bot_start_from_landing");
      logLandingEventWeb("web_register_opened");
      return true;
    }

    // 2. Обычный веб-переход: /?lp_session=<id>&utm_...
    const params=new URLSearchParams(window.location.search);
    const lpSession=params.get("lp_session");
    if(lpSession){
      localStorage.setItem("ap_lp_session",lpSession);
      const utm={
        utm_source:params.get("utm_source")||"",
        utm_medium:params.get("utm_medium")||"",
        utm_campaign:params.get("utm_campaign")||"",
        utm_content:params.get("utm_content")||"",
      };
      if(utm.utm_source||utm.utm_medium||utm.utm_campaign||utm.utm_content){
        localStorage.setItem("ap_lp_utm",JSON.stringify(utm));
      }
      logLandingEventWeb("web_register_opened");
      return true;
    }
  }catch(_){}
  return false;
}

function logLandingEventWeb(eventName){
  try{
    const sessionId=localStorage.getItem("ap_lp_session");
    if(!sessionId) return; // не из лендинга — не логируем, не нужный шум

    // Дедуп: одно и то же (session_id, event) не отправляем повторно за время
    // жизни вкладки. Для событий которые логически должны случиться один раз
    // за сессию лендинга (web_register_opened, register_success) дедуп
    // дополнительно переживает full page reload через localStorage —
    // иначе повторный boot() после регистрации или ре-рендер страницы
    // отправляет событие ещё раз.
    const dedupKey=sessionId+":"+eventName;
    if(_sentLandingEvents.has(dedupKey)) return;
    const PERSISTENT_DEDUP_EVENTS=["web_register_opened","register_success","bot_start_from_landing"];
    if(PERSISTENT_DEDUP_EVENTS.includes(eventName)){
      const sentKey="ap_lp_sent_"+eventName+"_"+sessionId;
      if(localStorage.getItem(sentKey)) return;
      localStorage.setItem(sentKey,"1");
    }
    _sentLandingEvents.add(dedupKey);

    const utm=JSON.parse(localStorage.getItem("ap_lp_utm")||"{}");
    fetch(API_BASE+"/api/landing-event",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        session_id:sessionId,
        event:eventName,
        url:window.location.href,
        utm_source:utm.utm_source||"",
        utm_medium:utm.utm_medium||"",
        utm_campaign:utm.utm_campaign||"",
        utm_content:utm.utm_content||"",
        user_agent:navigator.userAgent||"",
      }),
      keepalive:true,
    }).catch(()=>{});
  }catch(_){}
}

function logProductEvent(eventName, packageId){
  // Минимальная диагностика payment path (не для рекламной атрибуции —
  // для этого LandingEvent/Метрика). Требует токена, потому что эти события
  // происходят только после регистрации. Fire-and-forget — не блокирует
  // действие пользователя, не показывает ошибку при сбое.
  try{
    if(!App.token) return;
    fetch(API_BASE+"/api/product-event",{
      method:"POST",
      headers:{"Content-Type":"application/json","Authorization":"Bearer "+App.token},
      body:JSON.stringify({event:eventName, package_id:packageId||""}),
      keepalive:true,
    }).catch(()=>{});
  }catch(_){}
}

function cleanPostText(text){
  if(!text) return "";
  let t=text.trim();
  // Режем thinking-блок до разделителя ---
  const dashIdx=t.indexOf("\n---");
  if(dashIdx>=0 && dashIdx<300){
    t=t.slice(dashIdx+4).replace(/^-+/,"").trim();
  }
  // Убираем первый абзац если это рассуждение ИИ
  const thinkRe=/^(Беру|Взял|Нашёл|Нашел|Выбрал|Использую|Из поиска|По результатам|Проверил|Вижу|Смотрю|Ищу|Изучил|Анализирую)\b/i;
  const paras=t.split(/\n\s*\n/);
  if(paras.length>1 && thinkRe.test(paras[0].trim())){
    t=paras.slice(1).join("\n\n").trim();
  }
  return t;
}

function renderTg(text) {
  if (!text) return "";
  return esc(cleanPostText(text))
    .replace(/&lt;b&gt;(.*?)&lt;\/b&gt;/gs,"<b>$1</b>")
    .replace(/&lt;i&gt;(.*?)&lt;\/i&gt;/gs,"<i>$1</i>")
    .replace(/\n/g,"<br>");
}

function toast(msg, kind="") {
  // Последний рубеж защиты (P0 fix): что бы ни передали в toast — никогда
  // не показываем [object Object] или другой нечитаемый JS-объект.
  if (typeof msg !== "string") {
    if (msg && typeof msg.message === "string") msg = msg.message;
    else msg = "Не удалось выполнить действие. Попробуйте ещё раз.";
  }
  document.querySelectorAll(".toast").forEach(t=>t.remove());
  const t=document.createElement("div");
  t.className="toast "+kind; t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(()=>t.remove(),3200);
}

async function api(method, path, body) {
  const opts={method,headers:{}};
  const hadToken = !!App.token;
  if (App.token) opts.headers["Authorization"]="Bearer "+App.token;
  if (body!==undefined){opts.headers["Content-Type"]="application/json";opts.body=JSON.stringify(body);}
  const res=await fetch(API_BASE+"/api"+path,opts);

  let data=null;
  try{data=await res.json();}catch(_){}

  if (res.status===401){
    // Debugging requirements (P0 task): логируем контекст 401 явно, чтобы
    // на реальных логах/консоли можно было увидеть что произошло, вместо
    // догадок. path/hadToken/server detail — минимум для диагностики.
    console.log(`[auth] 401 from ${method} ${path}, hadToken=${hadToken}, server_detail=${JSON.stringify(data&&data.detail)}`);

    // КРИТИЧНО (P0 fix): register/login НЕ отправляют токен и не должны
    // попадать под "сессия истекла" вообще — это другая категория ошибки
    // (например бэкенд недоступен, неверный путь, или временный сбой).
    // Раньше любой 401 от ЛЮБОГО запроса (включая саму регистрацию) сразу
    // звал logout() и показывал жёстко закодированный текст "Сессия истекла"
    // ДО того как тело ответа даже читалось — это не давало пользователю
    // увидеть реальную причину и блокировало первую попытку регистрации.
    if (path === "/register" || path === "/login") {
      throw new Error((data && data.detail) || "Не удалось войти. Попробуйте ещё раз.");
    }

    // Для остальных запросов 401 значит реально невалидный/истёкший токен —
    // здесь оправдан logout() и чистый переход на экран входа, а не dead-end.
    if (hadToken) {
      logout();
      throw new Error("Сессия истекла, войдите снова.");
    }

    // 401 без токена на защищённом эндпоинте — не должно происходить в
    // обычном UI-потоке (кнопки защищённых действий не доступны без логина),
    // но если как-то случилось — не зовём logout() на пустом месте.
    throw new Error((data && data.detail) || "Нужно войти в аккаунт.");
  }

  if(!res.ok){
    // КРИТИЧНО (P0 fix): data.detail от FastAPI не всегда строка. При 422
    // (ошибка валидации Pydantic) это список объектов вида
    // [{"loc":[...],"msg":"...","type":"..."}] — если такой объект попадает
    // в new Error() напрямую, его message превращается в нечитаемое
    // "[object Object]" в toast. Нормализуем здесь централизованно.
    let detail = data && data.detail;
    let msg = "Ошибка запроса";
    if (typeof detail === "string") {
      msg = detail;
    } else if (Array.isArray(detail) && detail.length) {
      // Pydantic validation error array — берём первое читаемое сообщение.
      msg = detail.map(d => (d && typeof d.msg === "string") ? d.msg : null).filter(Boolean).join("; ") || "Проверьте введённые данные.";
    } else if (detail && typeof detail === "object") {
      msg = detail.message || detail.msg || "Ошибка запроса";
    }
    throw new Error(msg);
  }
  return data;
}

function logout(){
  App.token=null;App.user=null;
  localStorage.removeItem("ap_token");
  renderAuth();
}

// Task B rules 2-3: защищённые действия должны проверять auth до запроса
// и показывать понятное сообщение, а не давать сырому 401 всплыть из api().
// Используется как первая строка в обработчиках кнопок "Все каналы",
// "Сгенерировать пост", "Подключить канал", "Опубликовать сейчас",
// "Открыть настройки" и т.п.
function requireAuth(){
  if(App.token) return true;
  toast("Сначала войдите или зарегистрируйтесь, чтобы продолжить.","err");
  renderAuth();
  return false;
}

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

// ── Создание второго и следующих каналов (task item 2) ────────────────
// Минимальная форма с настройками — НЕ quick start (тот только для первого
// канала, см. renderNewChannelRouter). Без полного восстановления старого
// мёртвого renderNewChannel/ncGenerate — это новый, компактный flow.
function renderNewChannelSettings(){
  trackGoal("new_channel_settings_opened");
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <button class="back-link" style="margin-top:12px" onclick="go('dashboard')">← Все каналы</button>
    <div class="page-head" style="margin-top:8px">
      <h1>Новый канал</h1>
      <p style="color:var(--text-dim)">Базовые настройки — остальное можно донастроить позже во вкладке «Расширенные».</p>
    </div>

    <label class="field"><span class="field-label">Название канала</span>
      <input id="ncs_title" placeholder="Например: Новости M&A" style="width:100%"></label>

    <label class="field mt"><span class="field-label">Тема канала</span>
      <textarea id="ncs_about" rows="3" placeholder="О чём канал, для кого, какой тон" style="width:100%;font-size:15px"></textarea></label>

    <label class="field mt"><span class="field-label">@username канала <span class="text-faint">(можно позже)</span></span>
      <input id="ncs_chat" placeholder="@my_channel" style="width:100%"></label>

    <label class="field mt"><span class="field-label">Как часто писать новый пост</span>
      <select id="ncs_interval" style="width:100%">
        <option value="6">Каждые 6 часов</option>
        <option value="12" selected>Каждые 12 часов</option>
        <option value="24">Раз в сутки</option>
        <option value="48">Раз в 2 суток</option>
      </select></label>

    <div class="card mt">
      <div class="toggle-row">
        <div class="toggle-info"><b>Публиковать без проверки</b><small>Если включено — новые посты выходят в канал сами, по расписанию. Если выключено — пост ждёт вашего решения в очереди и сам не публикуется. Подключите уведомления в Телеграм: посты придут туда с кнопками «Опубликовать», «Отклонить», «Редактировать», и на решение будет ${App.cfg?.soft_control_minutes||30} мин — не ответите, опубликуем сами.</small></div>
        <label class="switch"><input type="checkbox" id="ncs_auto"><span class="slider"></span></label>
      </div>
    </div>

    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="ncsCreate()" id="ncs_btn">Создать канал</button>
    <!-- То же самое сказано под заголовком экрана, но там человек читает его
         ДО формы -- когда ещё не понял, что настроек мало. Владелец 28.07
         попросил сказать это прямо: короткий набор без пояснения читается как
         «всё, что есть». Здесь строка стоит в момент решения, и называет
         конкретные вещи, а не отсылает к вкладке. -->
    <div class="hint" style="text-align:center;margin-top:10px">
      Это только начало: стиль, источники, расписание и правила можно будет
      настроить подробнее уже в самом канале.
    </div>
  </div>`;
  setTimeout(()=>{const el=$("ncs_title");if(el) el.focus();},100);
}

let _ncsCreateInFlight=false;

async function ncsCreate(){
  if(!requireAuth()) return;
  if(_ncsCreateInFlight) return;
  const title=($("ncs_title").value||"").trim();
  const about=($("ncs_about").value||"").trim();
  if(!title) return toast("Укажите название канала","err");
  if(!about) return toast("Опишите тему канала","err");

  const tgChat=($("ncs_chat").value||"").trim();
  const intervalHours=parseFloat($("ncs_interval").value||"12");
  const autoPublish=!!$("ncs_auto").checked;

  // Topic validation (та же защита что и в quick start, не дублируем логику —
  // переиспользуем существующий /validate-topic эндпоинт).
  _ncsCreateInFlight=true;
  const btn=$("ncs_btn");
  btn.innerHTML='<span class="spinner"></span> Проверяю тему…';btn.disabled=true;
  try{
    const validation=await api("POST","/validate-topic",{topic:about});
    if(!validation.ok){
      toast(validation.message||"Не понял тему. Напишите проще.","err");
      btn.innerHTML="Создать канал";btn.disabled=false;
      _ncsCreateInFlight=false;
      return;
    }
  }catch(e){
    toast("Не удалось проверить тему. Попробуйте переформулировать.","err");
    btn.innerHTML="Создать канал";btn.disabled=false;
    _ncsCreateInFlight=false;
    return;
  }

  btn.innerHTML='<span class="spinner"></span> Создаю канал…';
  try{
    const chan=await api("POST","/channels",{
      title, about,
      tg_chat: tgChat,
      interval_hours: intervalHours,
      auto_publish: autoPublish,
    });
    trackGoal("new_channel_settings_created",{channel_id:chan.id});
    toast("Канал создан ✓","ok");
    go("channel",chan.id);
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    btn.innerHTML="Создать канал";btn.disabled=false;
  }finally{
    _ncsCreateInFlight=false;
  }
}

let _qsGenerateInFlight = false;

async function qsGenerate(){
  if(!requireAuth()) return;
  // КРИТИЧНО (P0 fix): защита от двойного клика через явный флаг, не только
  // через btn.disabled. disabled выставляется синхронно в начале функции,
  // но между двумя очень быстрыми кликами браузер может не успеть
  // перерисовать DOM-состояние кнопки до второго клика — флаг in-memory
  // гарантированно блокирует повторный вызов независимо от рендера.
  if (_qsGenerateInFlight) {
    toast("Пост уже генерируется, подождите несколько секунд.", "err");
    return;
  }
  const about=($("qs_about").value||"").trim();
  if(!about) return toast("Опишите тему","err");
  _qsGenerateInFlight = true;
  try{
    await _qsGenerateImpl(about);
  } finally {
    _qsGenerateInFlight = false;
  }
}

async function _qsGenerateImpl(about){
  console.log(`[qsGenerate] input_topic=«${about}» client_request_id=${App._qsRequestId}`);
  trackGoal("quick_start_submitted",{topic:about});
  const btn=$("qs_btn");
  btn.innerHTML='<span class="spinner"></span> Проверяю тему…';btn.disabled=true;

  // КРИТИЧНО (фикс по итогам ревью): валидируем тему ДО создания канала.
  // Раньше Channel создавался первым, а классификация происходила только
  // внутри /generate — из-за этого неподходящая тема всё равно попадала
  // в dashboard/settings как уже существующий канал. Этот вызов не создаёт
  // ничего в БД, поэтому при отказе канал просто не появляется.
  let validation;
  try{
    validation=await api("POST","/validate-topic",{topic:about});
  }catch(e){
    // Сбой самой проверки — не продолжаем генерацию (см. задачу: classify_topic
    // должен иметь safe fallback, который блокирует, а не пропускает молча).
    toast("Не удалось проверить тему. Попробуйте переформулировать.","err");
    btn.innerHTML="Сгенерировать пост";btn.disabled=false;
    return;
  }
  if(!validation.ok){
    if(validation.is_clarification){
      // Task E: уточняющий вопрос, не ошибка — спокойный, не алармистский UI.
      // Пользователь может согласиться продолжить с безопасной формулировкой
      // той же темы, не теряя то что уже ввёл.
      btn.innerHTML="Сгенерировать пост";btn.disabled=false;
      const qsCard=document.querySelector('#qs_about')?.closest('.card');
      if(qsCard){
        let clarifyBox=document.getElementById('qs_clarify');
        if(!clarifyBox){
          clarifyBox=document.createElement('div');
          clarifyBox.id='qs_clarify';
          clarifyBox.style.marginTop='10px';
          clarifyBox.style.padding='12px';
          clarifyBox.style.background='var(--surface2)';
          clarifyBox.style.borderRadius='10px';
          clarifyBox.style.fontSize='14px';
          qsCard.after(clarifyBox);
        }
        clarifyBox.innerHTML=`<div style="margin-bottom:8px">${esc(validation.message)}</div>
          <button class="btn-sm btn" onclick="document.getElementById('qs_about').value='Образовательный пост про уверенность, коммуникацию и уважение в интимных отношениях: '+document.getElementById('qs_about').value;document.getElementById('qs_clarify').remove();qsGenerate();">Да, такой формат подходит</button>`;
      } else {
        toast(validation.message,"err");
      }
      return;
    }
    toast(validation.message||"Не понял тему. Напишите проще.","err");
    btn.innerHTML="Сгенерировать пост";btn.disabled=false;
    return;
  }

  btn.innerHTML='<span class="spinner"></span> Пишу пост…';

  // Заголовок канала — авто, из темы (первые слова), пользователь сможет
  // переименовать позже в настройках. Не спрашиваем его сейчас намеренно —
  // лишнее поле на первом экране снижает activation (см. задачу).
  const title=about.length>40?about.slice(0,40).trim()+"…":about;

  let chan;
  try{
    chan=await api("POST","/channels",{
      title, about,
      // @username, введённый на шаге «Проанализировать мой канал». Раньше он
      // никуда не сохранялся, и человеку приходилось вводить его заново при
      // подключении.
      tg_chat: App._qsChannelHandle || "",
      // Короче, чем дефолт — первый пост должен читаться за 10 секунд (см. задачу).
      post_length:"700-1200 знаков, 2-4 коротких абзаца, простой заголовок",
      // Idempotency key (task item E): повторный клик с тем же ключом
      // вернёт уже созданный канал, не создаст дубль.
      client_request_id: App._qsRequestId || "",
    });
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    btn.innerHTML="Сгенерировать пост";btn.disabled=false;
    return;
  }
  App.channelId=chan.id;
  trackGoal("first_post_generation_started",{channel_id:chan.id});

  let post;
  try{
    post=await api("POST",`/channels/${chan.id}/generate`,{});
    // Защитная сеть на фронте: backend уже делает fallback при отказе модели,
    // но если что-то всё равно похоже на не-пост (короткий текст, латиница
    // в начале, явный вопрос) — не показываем это пользователю как результат.
    const looksWrong = !post.text || post.text.trim().length < 60
      || /^(what|please|sorry|i\s|let me|could you)/i.test(post.text.trim());
    if(looksWrong){
      trackGoal("first_post_generation_failed",{channel_id:chan.id,reason:"looks_wrong"});
      // Тема уже была одобрена validate-topic, значит это сбой генерации
      // (например web_search не нашёл фактов), а не проблема с темой как
      // таковой — поэтому канал НЕ удаляем, просто сообщаем об ошибке.
      // Пользователь может нажать "Сгенерировать пост" ещё раз для той же темы.
      toast("Не получилось найти свежий факт по этой теме. Попробуйте уточнить тему — например: «новости M&A в России».","err");
      btn.innerHTML="Сгенерировать пост";btn.disabled=false;
      return;
    }
  }catch(e){
    const errMsg=(e&&e.message)||"";
    const isTokenIssue=errMsg.toLowerCase().includes("токен")||errMsg.toLowerCase().includes("баланс");
    trackGoal("first_post_generation_failed",{channel_id:chan.id,reason:errMsg});
    if(isTokenIssue) logProductEvent("limit_reached");

    // Если тема была отклонена классификатором (defense-in-depth расхождение
    // между validate-topic и generate_for_channel) — backend уже удалил
    // канал сам (см. tasks.generate_for_channel). Для остальных технических
    // сбоев (web_search не нашёл фактов, временная ошибка API и т.п.) канал
    // остаётся пустым черновиком — удаляем его и здесь, чтобы не оставлять
    // в dashboard непроверенные дубли без единого поста (task item E, п.4-5).
    if(!isTokenIssue){
      try{await api("DELETE","/channels/"+chan.id);}catch(_){}
    }

    // Backend уже возвращает готовый русский текст для отклонённых тем
    // (unclear/adult/unsafe — см. tasks.generate_for_channel) и для других
    // ошибок генерации. Показываем его как есть, не подменяем дженериком —
    // иначе пользователь не узнает что именно с темой не так.
    const human=isTokenIssue
      ? "Закончились пробные посты. Пополните баланс в разделе «Тарифы»."
      : (errMsg || "Не удалось сгенерировать пост. Попробуйте ещё раз.");
    toast(human,"err");
    btn.innerHTML="Сгенерировать пост";btn.disabled=false;
    return;
  }

  // activation_1: первый пост сгенерирован — ключевая метрика онбординга
  trackGoal("first_post_generated",{channel_id:chan.id});
  logLandingEventWeb("first_post_generated");

  renderFirstPostResult(chan.id, post, about);
}

function renderFirstPostResult(channelId, post, about){
  tgHaptic("success"); // первый сгенерированный пост — эмоциональный пик онбординга
  App._qsAbout = about || App._qsAbout || ""; // помним тему для перегенерации
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <div class="page-head" style="text-align:center;margin-top:16px">
      <h1 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">Готово ✦</h1>
      <p style="color:var(--text-dim)">Вот первый пост для канала</p>
    </div>
    <div class="card" style="font-size:15px;line-height:1.7" id="fp_text">${renderTg(post.text)}</div>

    <div id="fp_feedback_block" style="margin-top:20px;padding:16px;background:var(--surface2);border-radius:12px;text-align:center">
      <p style="font-weight:500;margin-bottom:12px">Пост подходит?</p>
      <div style="display:flex;gap:10px;justify-content:center">
        <button class="btn" style="padding:10px 28px" onclick="fpFeedbackGood(${channelId})">Да</button>
        <button class="btn-outline" style="padding:10px 28px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface);cursor:pointer;font-family:inherit;color:var(--text)"
          onclick="fpFeedbackBad(${channelId})">Не совсем</button>
      </div>
    </div>

    <div id="fp_actions" style="display:none">
      <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
        onclick="go('connect_channel',${channelId})">Подключить Телеграм-канал</button>
    </div>

    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;justify-content:center">
      <button class="btn-outline btn-sm" onclick="qsRegenerate(${channelId})" id="fp_regen_btn">Ещё вариант</button>
      <button class="btn-outline btn-sm" onclick="qsRewrite(${channelId},'короче')" id="fp_short_btn">Сократить</button>
      <button class="btn-outline btn-sm" onclick="qsRewrite(${channelId},'живее')" id="fp_live_btn">Сделать живее</button>
    </div>

    <div style="display:flex;justify-content:space-between;margin-top:16px">
      <button class="btn-ghost btn-sm" style="color:var(--text-faint)" onclick="qsEdit(${channelId},${post.post_id})">Изменить текст</button>
      <button class="btn-ghost btn-sm" style="color:var(--text-faint)" onclick="go('dashboard')">Сохранить на потом</button>
    </div>
  </div>`;
}

function fpFeedbackGood(channelId){
  logProductEvent("first_post_feedback", "good");
  const fb=$("fp_feedback_block");
  if(fb){
    // Мост от хорошего первого поста к тарифам (иначе люди уходят до
    // оплаты) -- намеренно ведёт на тарифы, не собирает очередь прямо
    // здесь. Кнопка честно называет следующий шаг (выбор тарифа), не
    // обещает мгновенное действие, которого на самом деле не происходит.
    // Превью тем -- статичный формат из about канала (полноценной логики
    // подбора будущих тем в коде нет, генерировать по SPEC не нужно).
    const about=(App._qsAbout||"вашей теме").slice(0,60);
    fb.innerHTML=`<p style="color:var(--ok,#2a9d5c);font-weight:500">Отлично! ✓</p>
      <div id="queue_offer_block" style="margin-top:14px;text-align:left;background:var(--surface);border:1.5px solid var(--border);border-radius:10px;padding:14px">
        <p style="font-weight:600;margin-bottom:6px">Соберём очередь на неделю?</p>
        <p style="color:var(--text-dim);font-size:14px;line-height:1.5">Автопост подготовит 7 постов по вашей теме — по одному на каждый день. Вы просто просматриваете и публикуете.</p>
        <p style="color:var(--text-faint);font-size:13px;margin-top:8px">Например: «${esc(about)} — тема 1», «${esc(about)} — тема 2», «${esc(about)} — тема 3»…</p>
        <button class="btn" style="width:100%;justify-content:center;margin-top:12px;padding:12px" onclick="queueOfferClick()">Выбрать тариф и начать →</button>
      </div>`;
    logProductEvent("queue_offer_shown");
  }
  const actions=$("fp_actions");
  if(actions) actions.style.display="block";
}

function queueOfferClick(){
  logProductEvent("queue_offer_clicked");
  go("billing");
}

function fpFeedbackBad(channelId){
  logProductEvent("first_post_feedback", "bad");
  const fb=$("fp_feedback_block");
  if(!fb) return;
  const reasons=[
    {k:"too_generic",   label:"Слишком общий"},
    {k:"wrong_style",   label:"Не тот стиль"},
    {k:"wrong_topic",   label:"Не про мою тему"},
    {k:"too_dry",       label:"Слишком сухо"},
    {k:"too_salesy",    label:"Слишком рекламно"},
    {k:"other",         label:"Другое"},
  ];
  fb.innerHTML=`<p style="font-weight:500;margin-bottom:12px">Что не так?</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center">
      ${reasons.map(r=>`<button onclick="fpFeedbackReason('${r.k}',${channelId})"
        style="padding:8px 14px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface);cursor:pointer;font-size:14px;font-family:inherit;color:var(--text)"
        >${r.label}</button>`).join("")}
    </div>`;
}

function fpFeedbackReason(reason, channelId){
  logProductEvent("first_post_feedback_reason", reason);
  const fb=$("fp_feedback_block");
  if(fb) fb.innerHTML=`<p style="color:var(--text-dim);font-size:14px">Понял, спасибо. Попробуйте ещё вариант или уточните тему.</p>`;
  // Показываем кнопку подключения канала — пользователь может продолжить несмотря на недовольство
  const actions=$("fp_actions");
  if(actions) actions.style.display="block";
}

async function qsRegenerate(channelId){
  const btn=$("fp_regen_btn");
  btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const post=await api("POST",`/channels/${channelId}/generate`,{});
    renderFirstPostResult(channelId, post);
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    btn.innerHTML="Ещё вариант";btn.disabled=false;
  }
}

async function qsRewrite(channelId, mode){
  // "Сократить"/"Сделать живее" — переиспользуем тот же /generate с уточнённой
  // темой, без отдельного эндпоинта переписывания (см. задачу: не расширять
  // функциональность). Это не идеальная правка существующего текста, а новая
  // генерация с явной инструкцией по стилю — для onboarding этого достаточно.
  const btnId = mode==="короче" ? "fp_short_btn" : "fp_live_btn";
  const btn=$(btnId);
  if(btn){btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;}
  const hint = mode==="короче"
    ? `${App._qsAbout}. Сделай короче и проще — максимум 2 коротких абзаца.`
    : `${App._qsAbout}. Сделай живее и разговорнее, добавь конкретную деталь.`;
  try{
    const post=await api("POST",`/channels/${channelId}/generate`,{topic:hint});
    renderFirstPostResult(channelId, post);
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    if(btn){btn.innerHTML=mode==="короче"?"Сократить":"Сделать живее";btn.disabled=false;}
  }
}

function qsEdit(channelId, postId){
  // Редактирование первого поста — ведём в карточку канала, вкладка Очередь,
  // там уже есть полноценный редактор поста. Не дублируем эту логику здесь.
  go("channel", channelId);
}

// CONNECT CHANNEL — второй шаг онбординга: подключение publishing bot
async function renderConnectChannel(){
  let c;
  try{c=await api("GET","/channels/"+App.channelId);}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");return go("dashboard");}
  if(!$("app")) return;
  App._chan=c;
  const botUsername=App.cfg?.bot_username||"Trpst_bot";

  trackGoal("connect_channel_started",{channel_id:c.id});

  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <button class="back-link" style="margin-top:12px" onclick="go('channel',${c.id})">← Назад</button>
    <div class="page-head" style="text-align:center;margin-top:8px">
      <h1 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">Подключите Телеграм-канал</h1>
      <p style="color:var(--text-dim)">Чтобы АвтоПост мог публиковать посты, добавьте бота-публикатора администратором канала.</p>
    </div>

    <div class="card" style="text-align:center;padding:24px">
      <div style="font-size:13px;color:var(--text-faint);margin-bottom:6px">Бот для публикации</div>
      <div id="cc_bot_name" style="font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:600;color:var(--accent);margin-bottom:14px">@${esc(botUsername)}</div>
      <button class="btn-outline btn-sm" onclick="ccCopyBot('${esc(botUsername)}')" id="cc_copy_btn">📋 Скопировать @${esc(botUsername)}</button>
    </div>

    <div class="hint" style="margin-top:14px;line-height:1.7">
      1. Откройте канал → Управление → Администраторы<br>
      2. Добавьте <b>@${esc(botUsername)}</b><br>
      3. Включите право «Публиковать сообщения»<br>
      <a href="/how-to" target="_blank" rel="noopener">Подробная инструкция с картинками →</a>
    </div>

    <label class="field mt"><span class="field-label">@username канала или ссылка t.me/</span>
      <div class="row" style="gap:8px">
        <input id="cc_chat" value="${esc(c.tg_chat||"")}" placeholder="@my_channel или https://t.me/channel" style="flex:1">
      </div>
    </label>
    <div id="cc_msg" style="font-size:13px;margin-top:6px;min-height:18px"></div>

    <button class="btn" style="width:100%;justify-content:center;margin-top:12px;padding:14px"
      onclick="ccVerify()" id="cc_verify_btn">Проверить подключение</button>

    <button class="btn-ghost btn-sm" style="width:100%;justify-content:center;margin-top:10px;color:var(--text-faint)"
      onclick="go('channel',${c.id})">Подключу позже →</button>
  </div>`;
}

function ccCopyBot(username){
  const text="@"+username;
  if(navigator.clipboard?.writeText){
    navigator.clipboard.writeText(text).then(()=>toast("Скопировано","ok")).catch(()=>{});
  }
  const btn=$("cc_copy_btn");
  if(btn){const orig=btn.textContent;btn.textContent="✓ Скопировано";setTimeout(()=>{btn.textContent=orig;},1500);}
}

// Универсальная обёртка с реальным таймаутом (не просто текст-подсказка) —
// если промис не резолвится за timeoutMs, кнопка гарантированно
// разблокируется с понятным сообщением, вместо вечного spinner (task: "Fix
// loading / stuck button").
function withTimeout(promise, timeoutMs, timeoutMessage){
  let timedOut=false;
  const timeout=new Promise((_,reject)=>{
    setTimeout(()=>{timedOut=true;reject(new Error(timeoutMessage));},timeoutMs);
  });
  return Promise.race([promise, timeout]).then(
    result=>({timedOut:false, result}),
    err=>{ if(timedOut) return {timedOut:true, error:err}; throw err; }
  );
}

async function ccVerify(){
  if(!requireAuth()) return;
  const chatRaw=($("cc_chat")||{value:""}).value.trim();
  if(!chatRaw) return toast("Введите @username или ссылку на канал","err");
  const btn=$("cc_verify_btn"),msg=$("cc_msg");
  btn.innerHTML='<span class="spinner"></span> Проверяем канал…';btn.disabled=true;
  if(msg){msg.textContent="";}

  trackGoal("channel_verify_started",{channel_id:App.channelId,channel_input_raw:chatRaw});

  const TIMEOUT_MS=18000;
  const TIMEOUT_MSG="Операция занимает больше времени обычного. Попробуйте ещё раз или вернитесь позже.";

  const {timedOut, result, error} = await withTimeout((async()=>{
    await api("PATCH","/channels/"+App.channelId,{tg_chat:chatRaw});
    return await api("POST","/channels/"+App.channelId+"/verify");
  })(), TIMEOUT_MS, TIMEOUT_MSG);

  if(timedOut){
    trackGoal("publish_button_loading_timeout",{channel_id:App.channelId,stage:"verify"});
    if(msg){msg.textContent=TIMEOUT_MSG;msg.style.color="var(--accent-dark)";}
    btn.innerHTML="Проверить подключение";btn.disabled=false;
    return;
  }
  if(error){
    trackGoal("channel_verify_failed",{channel_id:App.channelId,reason:"exception"});
    if(msg){msg.textContent=error.message||"Не удалось проверить канал";msg.style.color="var(--red)";}
    btn.innerHTML="Проверить подключение";btn.disabled=false;
    return;
  }

  const r=result;
  if(r.ok){
    // activation_2: канал успешно подключён. ВАЖНО: подключение канала и
    // публикация поста — два разных шага (см. задачу), здесь НЕ публикуем
    // автоматически. Показываем экран подтверждения, решение — на пользователе.
    trackGoal("channel_connected",{channel_id:App.channelId});
    trackGoal("channel_verify_success",{channel_id:App.channelId});
    logLandingEventWeb("channel_connected");
    renderPublishConfirm(App.channelId, chatRaw);
  } else {
    trackGoal("channel_verify_failed",{channel_id:App.channelId,reason:r.message});
    if(msg){msg.textContent=r.message;msg.style.color="var(--red)";}
    btn.innerHTML="Проверить подключение";btn.disabled=false;
  }
}

// Step 5: экран подтверждения публикации — отдельный явный шаг, не
// автоматическое следствие успешной верификации (ключевой инвариант задачи:
// "Подключение канала ≠ публикация поста").
async function renderPublishConfirm(channelId, tgChat){
  let posts=[];
  try{posts=await api("GET",`/channels/${channelId}/posts`);}catch(e){}
  const pending=(posts||[]).find(p=>p.status==="pending"||p.status==="onboarding");

  trackGoal("publish_confirm_screen_shown",{channel_id:channelId});

  if(!$("app")) return;
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <div class="page-head" style="text-align:center;margin-top:24px">
      <div style="font-size:36px;margin-bottom:6px">✅</div>
      <h1 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">Канал подключён</h1>
      <p style="color:var(--text-dim)">${pending?"Первый пост готов. Опубликовать его сейчас?":"Пока нет готового поста — можно создать новый."}</p>
    </div>
    ${pending?`<div class="card" style="font-size:14px;line-height:1.6;max-height:200px;overflow:hidden;position:relative">
      ${renderTg(pending.text)}
      <div style="position:absolute;bottom:0;left:0;right:0;height:50px;background:linear-gradient(transparent,var(--surface))"></div>
    </div>`:`<div class="hint">Постов в очереди нет — можно создать новый из карточки канала.</div>`}

    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="ccConfirmPublish(${channelId},${pending?pending.id:"null"},'${esc(tgChat)}')"
      id="cpc_publish_btn" ${pending?"":"disabled"}>Опубликовать сейчас</button>

    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn-outline btn-sm" style="flex:1" onclick="_cancelPendingCcPublish(${pending?pending.id:"null"});go('channel',${channelId})">Оставить на проверке</button>
      <button class="btn-outline btn-sm" style="flex:1" onclick="_cancelPendingCcPublish(${pending?pending.id:"null"});ccGoSchedule(${channelId},${pending?pending.id:"null"})" ${pending?"":"disabled"}>Запланировать</button>
    </div>
  </div>`;
}

function ccGoSchedule(channelId, postId){
  // "Запланировать" — ведём в карточку канала, там уже есть полноценный
  // datetime-picker для постов (showPicker/doSchedule). Не дублируем здесь.
  go("channel", channelId);
  if(postId) setTimeout(()=>{ if(typeof showPicker==="function") showPicker(postId); },300);
}

// Опрашивает статус поста с коротким интервалом до подтверждения публикации
// или до истечения maxWaitMs. Используется после ложного timeout публикации,
// чтобы не показывать ошибку если Telegram-сторона на самом деле уже успешна
// (P0 fix item 1).
async function pollPostStatus(postId, maxWaitMs=20000, intervalMs=2000){
  const deadline=Date.now()+maxWaitMs;
  while(Date.now()<deadline){
    try{
      const status=await api("GET",`/posts/${postId}/status`);
      if(status.status==="published") return {confirmed:true, status};
    }catch(e){
      // Сетевая ошибка при опросе статуса — пробуем ещё раз, не сдаёмся сразу.
    }
    await new Promise(r=>setTimeout(r,intervalMs));
  }
  return {confirmed:false, status:null};
}

// КРИТИЧНО (UX fix): тот же принцип "минута на отмену" что и в publishPost()
// (app.part15.js) -- этот экран как раз и есть тот самый случай "сразу после
// подключения канала", который был явно назван проблемой (см. комментарий
// там). Раньше именно здесь публикация была абсолютно мгновенной.
const _pendingCcPublish = {}; // postId -> {intervalId, timeoutId}

function _cancelPendingCcPublish(postId){
  if(postId && _pendingCcPublish[postId]){
    clearInterval(_pendingCcPublish[postId].intervalId);
    clearTimeout(_pendingCcPublish[postId].timeoutId);
    delete _pendingCcPublish[postId];
  }
}

async function ccConfirmPublish(channelId, postId, tgChat){
  if(!requireAuth()) return;
  if(!postId) return;

  if(_pendingCcPublish[postId]){
    _cancelPendingCcPublish(postId);
    const btn=$("cpc_publish_btn");
    if(btn) btn.textContent="Опубликовать сейчас";
    toast("Публикация отменена","ok");
    return;
  }

  const btn=$("cpc_publish_btn");
  const _fmt=(s)=>`${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
  let secondsLeft=60;
  if(btn) btn.textContent=`Отменить (${_fmt(secondsLeft)})`;
  const intervalId=setInterval(()=>{
    secondsLeft--;
    if(secondsLeft<=0){clearInterval(intervalId);return;}
    const liveBtn=$("cpc_publish_btn");
    if(liveBtn) liveBtn.textContent=`Отменить (${_fmt(secondsLeft)})`;
  },1000);
  const timeoutId=setTimeout(()=>{
    delete _pendingCcPublish[postId];
    _doCcConfirmPublish(channelId, postId, tgChat);
  },60000);
  _pendingCcPublish[postId]={intervalId,timeoutId};
  toast("Опубликуется через минуту — можно отменить","ok");
}

async function _doCcConfirmPublish(channelId, postId, tgChat){
  const btn=$("cpc_publish_btn");
  btn.innerHTML='<span class="spinner"></span> Публикуем…';btn.disabled=true;

  trackGoal("first_post_publish_started",{
    channel_id:channelId, post_id:postId,
    was_publish_explicitly_confirmed:true,
    auto_publish_without_review:App._chan?.auto_publish||false,
  });

  const TIMEOUT_MS=18000;
  const TIMEOUT_MSG="Публикация занимает больше времени обычного. Проверяем статус. Не нажимайте повторно, чтобы не создать дубль.";

  const {timedOut, error} = await withTimeout(
    api("POST",`/posts/${postId}/publish`), TIMEOUT_MS, TIMEOUT_MSG
  );

  if(timedOut){
    // КРИТИЧНО (P0 fix): не показываем ошибку сразу. HTTP-запрос мог
    // зависнуть на фронте (медленная сеть, мобильное соединение), при этом
    // backend мог успешно опубликовать пост в Телеграм ДО таймаута. Сначала
    // проверяем реальный статус, и только если он не подтвердился —
    // показываем ошибку. Кнопка остаётся disabled всё это время, чтобы
    // исключить повторный клик и дублирующую публикацию.
    trackGoal("publish_button_loading_timeout",{channel_id:channelId,post_id:postId,stage:"publish"});
    btn.innerHTML='<span class="spinner"></span> Проверяем статус публикации…';
    const {confirmed}=await pollPostStatus(postId);
    if(confirmed){
      trackGoal("first_post_publish_success",{channel_id:channelId,post_id:postId,reconciled_after_timeout:true});
      trackGoal("first_post_published",{channel_id:channelId});
      logLandingEventWeb("first_post_published");
      await renderPublishSuccess(channelId, tgChat, postId);
      return;
    }
    trackGoal("first_post_publish_failed",{channel_id:channelId,post_id:postId,reason:"timeout_unconfirmed"});
    toast("Не удалось подтвердить публикацию. Проверьте канал или попробуйте ещё раз.","err");
    btn.innerHTML="Опубликовать сейчас";btn.disabled=false;
    return;
  }
  if(error){
    trackGoal("first_post_publish_failed",{channel_id:channelId,post_id:postId,reason:error.message});
    toast(error.message||"Не удалось опубликовать пост","err");
    btn.innerHTML="Опубликовать сейчас";btn.disabled=false;
    return;
  }

  // activation_3 + success_screen: публикация подтверждена явно пользователем.
  // already_published=true означает что предыдущая попытка (например после
  // ложного timeout) на самом деле успела опубликовать пост на backend —
  // показываем success так же, без дублирования сообщения в канале
  // (publish_post на backend идемпотентен).
  trackGoal("first_post_published",{channel_id:channelId});
  trackGoal("first_post_publish_success",{channel_id:channelId});
  logLandingEventWeb("first_post_published");
  await renderPublishSuccess(channelId, tgChat, postId);
}

async function renderPublishSuccess(channelId, tgChat, postId){
  const chatLabel = (tgChat||"").replace(/^https?:\/\/t\.me\//i,"").replace(/^@/,"");
  const tgUrl = `https://t.me/${chatLabel}`;
  trackGoal("success_screen_shown",{channel_id:channelId});

  // Task C rules 3-4: контекстная подсказка про очередь/автопубликацию.
  // Не критично если не получится загрузить — экран всё равно покажется.
  let contextLine = "";
  try{
    const chan = await api("GET", "/channels/"+channelId);
    const posts = await api("GET", `/channels/${channelId}/posts`);
    const pendingCount = (posts||[]).filter(p=>p.status==="pending"||p.status==="onboarding").length;
    if(pendingCount > 0){
      contextLine = `<p style="font-size:13px;color:var(--text-dim);margin-top:8px">В очереди уже есть посты, которые ждут вашего подтверждения.</p>`;
    } else if(!chan.auto_publish){
      contextLine = `<p style="font-size:13px;color:var(--text-dim);margin-top:8px">Новые посты будут ждать вашего подтверждения. Это можно изменить в настройках.</p>`;
    }
  }catch(_){}

  if(!$("app")) return;
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <div class="page-head" style="text-align:center;margin-top:32px">
      <div style="font-size:40px;margin-bottom:8px">✅</div>
      <h1 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">Готово — пост опубликован</h1>
      <p style="color:var(--text-dim)">Пост опубликован в канале @${esc(chatLabel)}</p>
      ${contextLine}
    </div>
    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="trackGoal('queue_opened',{channel_id:${channelId}});go('channel',${channelId})">Перейти в очередь</button>
    <button class="btn-outline btn-sm" style="width:100%;justify-content:center;margin-top:10px"
      onclick="go('new_channel')">Создать следующий пост</button>
    <div style="text-align:center;margin-top:14px">
      <a onclick="window.open('${tgUrl}','_blank')" style="font-size:13px;color:var(--text-faint);cursor:pointer;text-decoration:underline">Открыть пост в Телеграм</a>
    </div>
  </div>`;
}

function renderNewChannel(){
  _ncVoice="author";_ncFormat="story";_ncEmoji="minimal";_ncCta=false;_ncCtaText="";_ncHz=12;_ncStyleProfile="";
  $("app").innerHTML=topbar("dashboard","все каналы")+`<div class="wrap" style="max-width:680px">
    <div class="page-head"><h1>Новый канал</h1>
      <p>Расскажите о канале — ИИ покажет три варианта постов на выбор.</p></div>
    <div class="card">
      <label class="field"><span class="field-label">Название (видно только вам)</span>
        <input id="nc_title" placeholder="Например: Крипта без воды" maxlength="80"></label>

      <div class="field mt"><span class="field-label">Телеграм-канал</span>
        <div id="nc_verify_block">
          <div class="row" style="gap:8px">
            <input id="nc_chat" placeholder="@my_channel или https://t.me/channel" style="flex:1">
            <button class="btn-outline btn-sm" onclick="ncVerify()" id="nc_vbtn" style="white-space:nowrap">Проверить</button>
          </div>
          <div class="hint">Сначала добавьте бота <b>@${esc(App.cfg?.bot_username||"…")}</b> в админы канала, потом вставьте @username и нажмите «Проверить».</div>
          <div id="nc_vmsg" style="font-size:13px;margin-top:6px"></div>
          <button class="btn-ghost btn-sm" onclick="ncSkipVerify()"
            style="margin-top:8px;font-size:13px;color:var(--accent)">Подключу позже →</button>
        </div>
        <div id="nc_verify_skipped" class="hidden">
          <div style="display:flex;align-items:center;gap:10px;padding:12px 14px;background:var(--surface2);border-radius:12px">
            <span style="font-size:18px">🕓</span>
            <div style="flex:1;font-size:13px;color:var(--text-dim);line-height:1.5">
              Канал подключите позже в настройках. Сейчас ИИ покажет варианты постов.
            </div>
            <button class="btn-ghost btn-sm" onclick="ncShowVerify()" style="font-size:12px;color:var(--accent);white-space:nowrap">Подключить</button>
          </div>
        </div>
      </div>

      <label class="field mt"><span class="field-label">О чём канал</span>
        <textarea id="nc_about" rows="3" placeholder="Опишите идею — о чём, для кого, что интересно аудитории"></textarea></label>
      <div class="hint"><b>Примеры:</b><br>
        · <i>Крипта: новости, разбор монет, инвестиционные идеи</i><br>
        · <i>M&A в России: кто купил кого и зачем, простым языком</i><br>
        · <i>Психология отношений — понятно о сложном</i></div>

      <label class="field mt"><span class="field-label">Стиль и тон</span>
        <textarea id="nc_style" rows="2" placeholder="Как должен звучать канал"></textarea></label>
      <div class="hint"><b>Примеры:</b><br>
        · <i>Дерзко и по делу, как инсайдер</i><br>
        · <i>Тепло и честно, как умный друг</i></div>

      <div class="divider"></div>

      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Голос автора</div>
        <div class="seg" id="nc_vs">
          <button class="on" onclick="ncP('voice','author','nc_vs',this)">От себя</button>
          <button onclick="ncP('voice','expert','nc_vs',this)">Эксперт</button>
          <button onclick="ncP('voice','news','nc_vs',this)">Новости</button>
        </div>
      </div>
      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Формат поста</div>
        <div class="seg" id="nc_fs" style="flex-wrap:wrap">
          <button class="on" onclick="ncP('format','story','nc_fs',this)">История</button>
          <button onclick="ncP('format','tips','nc_fs',this)">Советы</button>
          <button onclick="ncP('format','news','nc_fs',this)">Новость</button>
          <button onclick="ncP('format','question','nc_fs',this)">Вопрос</button>
        </div>
      </div>
      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Эмодзи</div>
        <div class="seg" id="nc_es">
          <button onclick="ncP('emoji','none','nc_es',this)">Без эмодзи</button>
          <button class="on" onclick="ncP('emoji','minimal','nc_es',this)">1-2 штуки</button>
          <button onclick="ncP('emoji','rich','nc_es',this)">Активно</button>
        </div>
      </div>
      <div class="toggle-row" style="border:none;padding-top:0;margin-bottom:4px">
        <div class="toggle-info"><b>Призыв к действию</b><small>В конце каждого поста</small></div>
        <label class="switch"><input type="checkbox" id="nc_cta_sw"
          onchange="_ncCta=this.checked;$('nc_cta_i').classList.toggle('hidden',!this.checked)">
          <span class="slider"></span></label>
      </div>
      <input id="nc_cta_i" class="hidden" placeholder="Текст призыва" oninput="_ncCtaText=this.value" style="margin-bottom:12px">

      <div class="divider"></div>

      <div style="margin-bottom:12px">
        <div class="field-label" style="margin-bottom:4px">Скопировать стиль с канала (необязательно)</div>
        <div class="row" style="gap:8px">
          <input id="nc_ref" placeholder="https://t.me/example">
          <button class="btn-outline btn-sm" onclick="ncAnalyze()" id="nc_an" style="white-space:nowrap">Изучить</button>
        </div>
        <div id="nc_sp" class="hidden" style="margin-top:8px;padding:10px;background:var(--surface2);border-radius:10px;font-size:13px;color:var(--text-dim);white-space:pre-wrap"></div>
      </div>

      <div style="margin-bottom:20px">
        <div class="field-label" style="margin-bottom:8px">Тип канала</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px" id="nc_type_sel">
          <div class="type-card on" id="nc_type_thematic" onclick="ncPickType('thematic')"
            style="border:2px solid var(--accent);border-radius:12px;padding:14px;cursor:pointer;background:var(--accent-soft)">
            <div style="font-size:18px;margin-bottom:6px">✍️</div>
            <div style="font-weight:600;font-size:14px;margin-bottom:4px">Тематический</div>
            <div style="font-size:12px;color:var(--text-dim);line-height:1.4">Пишет по расписанию — психология, крипта-аналитика, лайфстайл</div>
          </div>
          <div class="type-card" id="nc_type_news" onclick="ncPickType('news')"
            style="border:2px solid var(--border-soft);border-radius:12px;padding:14px;cursor:pointer">
            <div style="font-size:18px;margin-bottom:6px">📡</div>
            <div style="font-weight:600;font-size:14px;margin-bottom:4px">Новостной</div>
            <div style="font-size:12px;color:var(--text-dim);line-height:1.4">Публикует только когда вышла новость — M&A, политика, крипто-новости</div>
          </div>
        </div>
        <div id="nc_type_hint" style="font-size:12px;color:var(--text-faint);margin-top:8px">
          Тематический: публикует по расписанию всегда. Токены за каждый пост.
        </div>
      </div>

      <div>
        <div class="field-label" style="margin-bottom:8px" id="nc_freq_label">Как часто писать новый пост</div>
        <div class="seg" id="nc_hzs" style="flex-wrap:wrap">
          <button onclick="ncHz(0.25,this)">15 мин</button>
          <button onclick="ncHz(0.5,this)">30 мин</button>
          <button onclick="ncHz(1,this)">1ч</button>
          <button onclick="ncHz(3,this)">3ч</button>
          <button onclick="ncHz(6,this)">6ч</button>
          <button class="on" onclick="ncHz(12,this)">12ч</button>
          <button onclick="ncHz(24,this)">24ч</button>
          <button onclick="ncHz(48,this)">48ч</button>
        </div>
      </div>
      <div class="toggle-row" style="margin-top:12px">
        <div class="toggle-info"><b>Искать новости в интернете</b></div>
        <label class="switch"><input type="checkbox" id="nc_web" checked><span class="slider"></span></label>
      </div>
    </div>

    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="ncGenerate()" id="nc_genbtn">✦ Сгенерировать три варианта поста</button>
    <div id="nc_results" class="hidden" style="margin-top:24px"></div>
  </div>`;
}

function ncPickType(type){
  _ncType=type;
  const thematic=$("nc_type_thematic"),news=$("nc_type_news"),hint=$("nc_type_hint"),label=$("nc_freq_label");
  if(type==="thematic"){
    if(thematic){thematic.style.border="2px solid var(--accent)";thematic.style.background="var(--accent-soft)";}
    if(news){news.style.border="2px solid var(--border-soft)";news.style.background="";}
    if(hint) hint.textContent="Тематический: пишем новый пост по расписанию, даже если новостей нет. Токены тратятся на каждый пост.";
    if(label) label.textContent="Как часто писать новый пост";
  } else {
    if(news){news.style.border="2px solid var(--accent)";news.style.background="var(--accent-soft)";}
    if(thematic){thematic.style.border="2px solid var(--border-soft)";thematic.style.background="";}
    if(hint) hint.textContent="Новостной: проверяем новости по расписанию и пишем, только если есть о чём. Токены тратятся только тогда.";
    if(label) label.textContent="Как часто проверять новости";
  }
}

function ncP(type,val,seg,btn){
  if(type==="voice") _ncVoice=val;
  if(type==="format") _ncFormat=val;
  if(type==="emoji") _ncEmoji=val;
  document.querySelectorAll(`#${seg} button`).forEach(b=>b.classList.remove("on"));
  btn.classList.add("on");
}
function ncHz(val,btn){
  _ncHz=val;
  document.querySelectorAll("#nc_hzs button").forEach(b=>b.classList.remove("on"));
  btn.classList.add("on");
}

function ncSkipVerify(){
  const block=$("nc_verify_block"),skipped=$("nc_verify_skipped");
  if(block) block.classList.add("hidden");
  if(skipped) skipped.classList.remove("hidden");
}
function ncShowVerify(){
  const block=$("nc_verify_block"),skipped=$("nc_verify_skipped");
  if(block) block.classList.remove("hidden");
  if(skipped) skipped.classList.add("hidden");
}

async function ncVerify(){
  const chat=($("nc_chat")||{value:""}).value.trim();
  if(!chat) return;
  const btn=$("nc_vbtn"),msg=$("nc_vmsg");
  btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const r=await api("POST","/verify_channel_only",{tg_chat:chat});
    msg.textContent=r.message;msg.style.color=r.ok?"var(--green)":"var(--red)";
  }catch(e){msg.textContent=e.message;msg.style.color="var(--red)";}
  btn.innerHTML="Проверить";btn.disabled=false;
}

async function ncAnalyze(){
  const link=($("nc_ref")||{value:""}).value.trim();if(!link) return;
  const btn=$("nc_an");btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const r=await api("POST","/analyze_style_only",{link});
    _ncStyleProfile=r.profile||"";
    $("nc_sp").textContent=_ncStyleProfile.replace(/#{1,3} /g,"").replace(/\*\*/g,"");
    $("nc_sp").classList.remove("hidden");
    trackGoal("style_analyzed",{source:"onboarding"});
    toast("Стиль изучен","ok");
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
  btn.innerHTML="Изучить";btn.disabled=false;
}

async function ncGenerate(){
  const title=($("nc_title").value||"").trim()||"Новый канал";
  const about=($("nc_about").value||"").trim();
  const style=($("nc_style").value||"").trim();
  if(!about) return toast("Опишите тему канала","err");
  const btn=$("nc_genbtn");
  btn.innerHTML='<span class="spinner"></span> Создаю канал…';btn.disabled=true;
  let chan;
  try{
    chan=await api("POST","/channels",{
      title,about,style,
      channel_type:_ncType,
      tg_chat:($("nc_chat")||{value:""}).value.trim(),
      interval_hours:_ncHz,
      use_web_search:($("nc_web")||{checked:true}).checked,
      style_profile:_ncStyleProfile,
      post_voice:_ncVoice,post_format:_ncFormat,
      emoji_style:_ncEmoji,cta_enabled:_ncCta,cta_text:_ncCtaText,
    });
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");btn.innerHTML="✦ Сгенерировать три варианта поста";btn.disabled=false;return;}
  App.channelId=chan.id;App._onboardPosts=[];
  trackGoal("channel_created",{source:"onboarding",channel_id:chan.id});

  $("nc_results").classList.remove("hidden");
  $("nc_results").innerHTML=`
    <h2 style="font-family:'Instrument Serif',serif;font-size:22px;font-weight:400;margin-bottom:4px">Варианты постов</h2>
    <p style="color:var(--text-dim);font-size:13px;margin-bottom:16px">Генерирую три варианта — выберите понравившийся.</p>
    <div id="ob_posts"></div>
    <div id="ob_load">
      ${[0,1,2].map(i=>`<div style="background:var(--surface);border:1.5px solid var(--border-soft);border-radius:var(--radius);padding:20px;margin-bottom:14px;opacity:${1-i*0.2}">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div style="width:60px;height:16px;background:var(--surface2);border-radius:6px"></div>
          <div style="width:80px;height:30px;background:var(--surface2);border-radius:8px"></div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;color:var(--text-faint);font-size:13px">
          <span class="spinner"></span> ${i===0?'Генерирую…':i===1?'Следующий вариант…':'Третий вариант…'}
        </div>
      </div>`).join("")}
    </div>`;

  btn.textContent="✓ Варианты готовы ниже ↓";btn.style.background="var(--green)";
  btn.onclick=()=>$("nc_results").scrollIntoView({behavior:"smooth"});

  const formats=[
    {key:"story",label:"История",desc:"Нарратив с выводом"},
    {key:"tips",label:"Советы",desc:"Конкретные шаги"},
    {key:"question",label:"Вопрос",desc:"Вовлечение"},
  ];
  let _ymOnboardPostGenerated=false;
  for(let i=0;i<formats.length;i++){
    const f=formats[i];
    try{
      const r=await api("POST",`/channels/${chan.id}/generate_format`,{post_format:f.key});
      App._onboardPosts.push({...f,text:r.text,post_id:r.post_id});
      if(!_ymOnboardPostGenerated){
        trackGoal("post_generated",{source:"onboarding",channel_id:chan.id});
        _ymOnboardPostGenerated=true;
      }
      // Убираем один скелетон по мере появления поста
      const loadEl=$("ob_load");
      if(loadEl){
        const firstSkeleton=loadEl.querySelector("div");
        if(firstSkeleton) firstSkeleton.remove();
      }
      const el=document.createElement("div");
      el.className="onboard-card";
      el.innerHTML=`<div class="onboard-header">
        <div>
          <span style="font-size:13px;font-weight:600;color:var(--accent)">${esc(f.label)}</span>
          <span style="font-size:12px;color:var(--text-faint);margin-left:8px">${esc(f.desc)}</span>
        </div>
        <button class="btn btn-sm" onclick="ncSelect(${i})">Выбрать →</button></div>
        <div style="margin-top:10px;font-size:14px;line-height:1.7;color:var(--text)">${renderTg(r.text)}</div>`;
      $("ob_posts").appendChild(el);
    }catch(e){
      App._onboardPosts.push({...f,text:null,error:e.message});
      // Убираем скелетон и показываем ошибку по этому варианту
      const loadEl=$("ob_load");
      if(loadEl){const fs=loadEl.querySelector("div");if(fs) fs.remove();}
      const errMsg=(e.message||"").toLowerCase();
      const human=errMsg.includes("токен")||errMsg.includes("limit")||errMsg.includes("закончил")
        ? "Посты закончились. Пополните баланс в разделе «Тарифы»."
        : errMsg.includes("529")||errMsg.includes("overload")
        ? "Серверы ИИ перегружены. Попробуйте через минуту."
        : (e.message||"Не удалось сгенерировать");
      const el=document.createElement("div");
      el.className="onboard-card";
      el.style.borderColor="var(--red)";
      el.innerHTML=`<div style="font-size:13px;color:var(--red)">⚠️ ${esc(human)}</div>`;
      $("ob_posts").appendChild(el);
    }
  }
  const loadEnd=$("ob_load");if(loadEnd) loadEnd.innerHTML="";
  // Если ни один пост не сгенерировался — особое сообщение
  const okPosts=App._onboardPosts.filter(p=>p.post_id);
  if(!okPosts.length){
    $("ob_posts").insertAdjacentHTML("beforeend",`
      <div style="padding:16px;background:var(--accent-soft);border-radius:var(--radius);text-align:center">
        <div style="font-size:14px;color:var(--accent-dark);margin-bottom:10px">Не удалось создать посты. Проверьте баланс в разделе «Тарифы».</div>
        <button class="btn" onclick="go('billing')">Перейти к тарифам →</button>
      </div>`);
    btn.disabled=false;btn.textContent="✦ Попробовать снова";
    btn.style.background="";btn.onclick=()=>ncGenerate();
    return;
  }
  $("ob_posts").insertAdjacentHTML("beforeend",`
    <div style="margin-top:20px;padding:16px;background:var(--accent-soft);border-radius:var(--radius);border:1px solid #e8d5bb">
      <div style="font-size:14px;font-weight:600;color:var(--accent-dark);margin-bottom:6px">Не понравился ни один?</div>
      <div style="font-size:13px;color:var(--text-dim);margin-bottom:10px">В расширенных настройках можно уточнить стиль через диалог с ИИ-консультантом.</div>
      <button class="btn" style="width:100%;justify-content:center"
        onclick="go('channel',${chan.id});setTimeout(()=>setTab('advanced'),400)">
        Настроить с помощью ИИ →</button>
    </div>`);
  setTimeout(()=>$("nc_results").scrollIntoView({behavior:"smooth"}),100);
  btn.disabled=false;
}

async function ncSelect(idx){
  const picked=App._onboardPosts[idx];if(!picked||!picked.post_id) return;
  try{
    for(let i=0;i<App._onboardPosts.length;i++){
      if(i!==idx&&App._onboardPosts[i].post_id)
        await api("POST",`/posts/${App._onboardPosts[i].post_id}/reject`).catch(()=>{});
    }
    await api("PATCH",`/channels/${App.channelId}`,{onboarded:true});
    trackGoal("onboarding_complete",{channel_id:App.channelId});
    toast("Канал настроен ✓","ok");go("channel",App.channelId);
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}

// CHANNEL
async function renderChannel(){
  await refreshUser();
  let c;
  try{c=await api("GET","/channels/"+App.channelId);}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");return go("dashboard");}
  if(!$("app")) return; // DOM ещё не готов — прерываем
  try{c.daily_times=JSON.parse(c.daily_times||"[]");}catch(_){c.daily_times=[];}
  // Сбрасываем вид очереди (список/календарь) при переходе на другой канал --
  // иначе выбранный день/месяц календаря одного канала подставлялся бы под
  // совсем другой канал.
  if(App._chan && App._chan.id!==c.id){_queueViewMode="list";_calMonth=null;_calSelectedDate=null;}
  App._chan=c;
  const notConnected=!c.tg_chat?`<div style="background:var(--accent-soft);border:1px solid #e8d5bb;border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:var(--accent-dark)">
    📡 Канал не подключён к Телеграм.
    <button class="btn-ghost btn-sm" onclick="App.tab='settings';renderChannel()" style="color:var(--accent);font-weight:600">Подключить →</button></div>`:"";

  // Тот же визуальный язык что и карточки на дашборде (renderChanCard,
  // app.part03.js) — раньше здесь были старые chip-пилюли, не обновлённые
  // при редизайне карточек.
  // Строку с хэндлом рисуем только когда хэндл есть. Раньше вместо него
  // стояло «канал будет указан после подключения» -- ровно та же мысль, что
  // в баннере на 60px выше, и появлялись они всегда вместе: баннер рисуется
  // при том же условии `!c.tg_chat`. Замер по методу поиска дублей постов
  // (основы слов + Жаккар) дал этой паре 0.5 -- выше, чем у любых двух
  // осмысленно разных строк на экране.
  const initial=(c.title||"?").trim().charAt(0).toUpperCase()||"?";
  const connected=!!(c.tg_chat && c.verified);
  // КРИТИЧНО (UX fix): раньше "канал не подключён" повторялся трижды на
  // экране одновременно -- баннер выше, статус-строка и подпись хэндла.
  // Когда tg_chat вообще не указан, баннер notConnected уже показывает это
  // с явной кнопкой "Подключить →" -- статус-строку в этом случае не
  // дублируем, оставляем только для промежуточного состояния "бот добавлен,
  // но ещё не подтверждён" (у него своего баннера нет).
  let statusLabel="", dotClass="status-dot-gray";
  if(!connected){
    if(c.tg_chat) statusLabel="Бот пока не подтверждён администратором";
  } else if(c.auto_publish){
    statusLabel="Автоматическая публикация"; dotClass="status-dot-green";
  } else {
    statusLabel="Вы подтверждаете каждый пост"; dotClass="status-dot-accent";
  }
  // Строку «⏱ Следующая генерация …» здесь не показываем. Блок очереди прямо
  // под вкладками говорит то же самое, но точнее, и делает это в любом
  // состоянии канала: «Ещё 3 мы готовим — обычно пару минут», «Очередь
  // заполнена», «Канал на паузе», «Мы сами пишем и публикуем — раз в 12 часов».
  // Получалось, что срок генерации написан на одном экране дважды. На
  // дашборде (app.part03.js) строка остаётся: там блока очереди нет и это
  // единственный способ увидеть, когда появится следующий пост.
  const subLine = c.enabled === false ? "На паузе" : "";
  // Тему канала здесь не показываем. Замерено на 390px: абзац с темой занимал
  // 62px из 250px шапки и отодвигал первый пост на 612px из 844 -- при том, что
  // он только для чтения, повторяет поле «Тема» во вкладке «Настройки» и не
  // меняется между заходами. На этот экран приходят решать судьбу постов; чтобы
  // решить, нужен текст поста, а не описание канала. Тема осталась там, где её
  // можно исправить, -- в «Настройках».

  $("app").innerHTML=topbar("dashboard","все каналы")+`<div class="wrap">
    ${notConnected}
    <div class="chan-header card" style="margin-bottom:16px">
      <div class="chan-card-top">
        <div class="tg-ava" style="width:52px;height:52px;font-size:22px">${esc(initial)}</div>
        <div style="min-width:0">
          <h2 style="font-family:'Instrument Serif',serif;font-size:26px;font-weight:400">${esc(c.title)}</h2>
          ${c.tg_chat?`<div class="chan-handle">${esc(c.tg_chat)}</div>`:""}
        </div>
      </div>
      ${statusLabel?`<div class="status-line"><span class="status-dot ${dotClass}"></span>${statusLabel}</div>`:""}
      ${subLine?`<div class="status-subline">${subLine}</div>`:""}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
        <button class="btn btn-sm" onclick="openGenPanel()">✦ Создать пост</button>
        <button class="${c.enabled?'btn-outline btn-sm':'btn btn-sm'}" onclick="toggleChannelEnabled()"
          id="pause_btn">${c.enabled?'⏸ Пауза':'▶ Возобновить'}</button>
      </div>
    </div>
    <div class="gen-panel hidden" id="genPanel">
      <div class="gen-title">Задайте тему поста (необязательно)</div>
      <div class="gen-row">
        <input id="genTopic" placeholder="Например: новая крупная сделка в России">
        <button class="btn" onclick="generateNow()" id="genRunBtn">Создать</button>
      </div>
      <div class="hint" style="margin-top:6px">Оставьте пустым — ИИ выберет тему сам</div>
    </div>
    <div class="tabs">
      <button class="tab ${App.tab==="queue"?"active":""}" onclick="setTab('queue')">Очередь</button>
      <button class="tab ${App.tab==="settings"?"active":""}" onclick="setTab('settings')">Настройки</button>
      <button class="tab ${App.tab==="advanced"?"active":""}" onclick="setTab('advanced')">Расширенные</button>
    </div>
    <div id="tabbody"></div>
  </div>`;
  renderTab();
}

function renderTimer(){} // убран — время публикации теперь на карточках постов

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
  // Оценка поста автором. Стоит рядом с «⋯» и намеренно ничего не делает с
  // самим постом -- не публикует, не отклоняет, не перегенерирует. Это только
  // накопление данных о качестве (C1): по «опубликован/отклонён» о качестве
  // судить нельзя, отклонить могли и из-за неподходящей темы.
  // Показываем на всех карточках, включая опубликованные: понять, что пост
  // был хорош, часто можно только постфактум.
  // Выбранное состояние показываем прозрачностью и фоном, а НЕ разными
  // эмодзи: вариант с модификатором тона (👍 против 👍🏻) на части платформ
  // рисуется одинаково, и тогда понять, поставлена оценка или нет, нельзя
  // вовсе. Прозрачность работает везде одинаково.
  const fb = p.feedback || null;
  const rateStyle = (active, color) =>
    `padding:14px 10px;line-height:1;font-size:15px;border-radius:8px;` +
    (active ? `opacity:1;background:${color}` : `opacity:.35`);
  const feedbackBtns = `
    <button class="btn-ghost btn-sm" onclick="ratePost(${p.id},'up')"
      title="${fb === "up" ? "Убрать оценку" : "Хороший пост"}" aria-label="Хороший пост"
      aria-pressed="${fb === "up"}"
      style="${rateStyle(fb === "up", "var(--green-bg,#e3f4e8)")}">👍</button>
    <button class="btn-ghost btn-sm" onclick="ratePost(${p.id},'down')"
      title="${fb === "down" ? "Убрать оценку" : "Плохой пост"}" aria-label="Плохой пост"
      aria-pressed="${fb === "down"}"
      style="${rateStyle(fb === "down", "var(--red-bg,#fbe9e9)")}">👎</button>`;

  const menuBtn = menuItems ? `
    <div style="position:relative">
      <!-- Замерено на 390px: цель была 32×27 — самая мелкая на экране, и стоит
           вплотную к «Опубликовать сейчас», то есть к необратимому действию.
           Промах пальцем здесь стоит поста, ушедшего в канал. У кнопки нет ни
           фона, ни рамки, поэтому увеличенные отступы расширяют область
           нажатия, ничего не меняя внешне. -->
      <button class="btn-ghost btn-sm" onclick="togglePostMenu(${p.id})" style="padding:14px 16px;line-height:1">⋯</button>
      <div id="pmenu_${p.id}" class="post-menu hidden">${menuItems}</div>
    </div>` : "";

  // Оценка и «⋯» уезжают вправо одной группой: margin-left:auto перенесён
  // с меню на обёртку, иначе кнопки оценки прилипали бы к «Изменить».
  const rightGroup = `<div style="display:flex;align-items:center;gap:2px;margin-left:auto">${feedbackBtns}${menuBtn}</div>`;

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
      ${rightGroup}
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
  // «N из M» осмысленно, пока очередь наполняется: M -- сколько постов мы
  // держим наготове. Но плановая генерация глубину очереди не проверяет
  // (tick -> generate_for_channel в tasks.py, гейт `pending_count < target`
  // стоит только в _refill_if_active), поэтому постов легко становится больше
  // цели -- и счётчик показывал «4 из 3». Когда запас набран, знаменатель
  // больше ничего не объясняет: показываем просто число.
  const counter = pendingCount >= minQueue
    ? `<b>${pendingCount}</b>`
    : `<b>${pendingCount}</b> из ${minQueue}`;
  const settingsLink = `onclick="setTab('settings');setTimeout(()=>{const el=document.getElementById('settings_automation_card');if(el) el.scrollIntoView({behavior:'smooth',block:'center'});},100)"`;

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
  // Здесь было «Очередь заполнена. Как только опубликуете один пост, мы
  // подготовим следующий» -- и это неправда. Проверено прямым вызовом решающих
  // функций: канал с полной очередью и истёкшим интервалом попадает в
  // `due_ids` в tick() и получает новый пост, публикации никто не ждёт.
  // Обещание держало паузу, которой в системе нет.
  // Интервал здесь намеренно не называем -- он написан в «Подробнее», и
  // повторять его на виду значило бы вернуть тот самый повтор, который убрали
  // в B6.
  const refillLine = pendingCount < minQueue
    ? `Ещё ${minQueue - pendingCount} мы готовим — обычно это занимает пару минут.`
    : `Запас готов — дальше посты добавляются по расписанию.`;

  // Механику «кнопка или таймер» показываем только когда постов ещё нет.
  // Как только пост появился, его карточка говорит это про себя сама -- либо
  // «сам не опубликуется», либо обратный отсчёт, -- и объяснение на уровне
  // экрана становится третьим пересказом одной мысли.
  // Замерено на 390x844 до правки: первый пост начинался на 612px, то есть
  // три четверти первого экрана уходили на шапку и объяснение, и на телефоне
  // с адресной строкой пост оказывался за сгибом. Полный текст никуда не
  // делся -- он под «Подробнее».
  const mechanicsLine = pendingCount > 0 ? "" : ((App.user?.tg_chat_id
    ? "Пост ждёт вашей кнопки «Опубликовать» — или таймера на карточке, если мы прислали его вам в Телеграм."
    : "Мы ничего не публикуем сами, пока не можем вас предупредить: каждый пост ждёт вашей кнопки.") + " ");

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
      ${mechanicsLine}${refillLine}
    </div>
    <button class="btn-ghost btn-sm" style="margin-top:0;padding:4px 0;color:var(--accent-dark)"
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

async function testPost(){
  const btn=$("testBtn");btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  await _silentSave();
  try{
    const r=await api("POST","/channels/"+App._chan.id+"/generate",{});
    const posts=await api("GET","/channels/"+App._chan.id+"/posts");
    const p=posts.find(x=>x.id===r.post_id)||{text:"",tokens_used:0,id:r.post_id};
    trackGoal("post_generated",{source:"test",channel_id:App._chan.id});
    // id="pc_{id}" -- без него publishPost() (app.part15.js) не находит
    // кнопку по её обычному пути поиска и не может показать отсчёт отмены,
    // из-за чего публикация тихо срабатывает через минуту без единого
    // видимого предупреждения на этой карточке.
    $("test_result").innerHTML=`<div class="card" id="pc_${p.id}" style="background:var(--surface2)">

      <div class="post-body">${renderTg(p.text)}</div>
      <div class="post-actions" style="margin-top:10px">
        <button class="btn btn-green btn-sm" onclick="publishPost(${p.id})">✓ Опубликовать</button>
        <button class="btn-danger btn-sm" onclick="rejectPost(${p.id})">Отклонить</button>
      </div></div>`;
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
  btn.innerHTML="▷ Создать тестовый пост";btn.disabled=false;
}

// ADVANCED
let _advInterval=null;
async function renderAdvanced(){
  const c=App._chan;
  let rules=[],sources=[];
  try{rules=await api("GET","/channels/"+c.id+"/rules");}catch(_){}
  try{sources=await api("GET","/channels/"+c.id+"/sources");}catch(_){}
  App._consultHistory=[];_advInterval=null;

  $("tabbody").innerHTML=`
    <div class="card">
      <div class="card-title">Голос и формат</div>
      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Голос автора</div>
        <div class="seg" id="seg_voice">
          <button class="${(c.post_voice||"author")==="author"?"on":""}" onclick="pickOpt('post_voice','author','seg_voice')">От себя</button>
          <button class="${(c.post_voice||"author")==="expert"?"on":""}" onclick="pickOpt('post_voice','expert','seg_voice')">Эксперт</button>
          <button class="${(c.post_voice||"author")==="news"?"on":""}" onclick="pickOpt('post_voice','news','seg_voice')">Новости</button>
        </div>
      </div>
      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Формат</div>
        <div class="seg" id="seg_format" style="flex-wrap:wrap">
          <button class="${(c.post_format||"story")==="story"?"on":""}" onclick="pickOpt('post_format','story','seg_format')">История</button>
          <button class="${(c.post_format||"story")==="tips"?"on":""}" onclick="pickOpt('post_format','tips','seg_format')">Советы</button>
          <button class="${(c.post_format||"story")==="news"?"on":""}" onclick="pickOpt('post_format','news','seg_format')">Новость</button>
          <button class="${(c.post_format||"story")==="question"?"on":""}" onclick="pickOpt('post_format','question','seg_format')">Вопрос</button>
        </div>
      </div>
      <div style="margin-bottom:14px">
        <div class="field-label" style="margin-bottom:8px">Эмодзи</div>
        <div class="seg" id="seg_emoji">
          <button class="${(c.emoji_style||"minimal")==="none"?"on":""}" onclick="pickOpt('emoji_style','none','seg_emoji')">Без</button>
          <button class="${(c.emoji_style||"minimal")==="minimal"?"on":""}" onclick="pickOpt('emoji_style','minimal','seg_emoji')">1-2 штуки</button>
          <button class="${(c.emoji_style||"minimal")==="rich"?"on":""}" onclick="pickOpt('emoji_style','rich','seg_emoji')">Активно</button>
        </div>
      </div>
      <div class="toggle-row" style="border:none">
        <div class="toggle-info"><b>Призыв к действию</b><small>В конце каждого поста</small></div>
        <label class="switch"><input type="checkbox" id="sw_cta" ${c.cta_enabled?"checked":""}
          onchange="$('cta_f').classList.toggle('hidden',!this.checked)"><span class="slider"></span></label>
      </div>
      <div id="cta_f" class="${c.cta_enabled?"":"hidden"}" style="margin-top:8px">
        <input id="f_cta" value="${esc(c.cta_text||"")}" placeholder="Подпишись чтобы не пропустить"></div>
    </div>

    <div class="card">
      <div class="card-title">Тип канала</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:4px">
        <div onclick="pickChannelType('thematic')" id="adv_type_thematic"
          style="border:2px solid ${(c.channel_type||'thematic')==='thematic'?'var(--accent)':'var(--border-soft)'};
          background:${(c.channel_type||'thematic')==='thematic'?'var(--accent-soft)':''};
          border-radius:12px;padding:12px;cursor:pointer">
          <div style="font-weight:600;font-size:13px;margin-bottom:2px">✍️ Тематический</div>
          <div style="font-size:11px;color:var(--text-dim)">Пишем по расписанию</div>
        </div>
        <div onclick="pickChannelType('news')" id="adv_type_news"
          style="border:2px solid ${(c.channel_type||'thematic')==='news'?'var(--accent)':'var(--border-soft)'};
          background:${(c.channel_type||'thematic')==='news'?'var(--accent-soft)':''};
          border-radius:12px;padding:12px;cursor:pointer">
          <div style="font-weight:600;font-size:13px;margin-bottom:2px">📡 Новостной</div>
          <div style="font-size:11px;color:var(--text-dim)">Пишем, только если есть новость</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title" id="adv_sched_title">${(c.channel_type||'thematic')==='news'?'Как часто проверять новости':'Как часто писать новый пост'}</div>
      <div style="margin-bottom:12px">
        <div class="field-label" style="margin-bottom:8px">Интервал</div>
        <div class="seg" id="seg_int" style="flex-wrap:wrap">
          ${[0.25,0.5,1,3,6,12,24,48].map(h=>{
            const lbl=h<1?`${h*60|0}м`:h<24?`${h}ч`:`${h/24|0}д`;
            return `<button class="${(c.interval_hours||12)==h?"on":""}" onclick="pickAdv(${h},this)">${lbl}</button>`;
          }).join("")}
        </div>
      </div>
      <label class="field">
        <span class="field-label">Разброс ±<span id="jlbl">${c.interval_jitter_minutes||0}</span> мин</span>
        <input type="range" id="f_jitter" min="0" max="120" value="${c.interval_jitter_minutes||0}"
          oninput="$('jlbl').textContent=this.value"
          style="padding:4px 0;height:auto;box-shadow:none;border:none;background:none">
        <div class="hint">Сдвигает время написания на случайную величину, чтобы посты появлялись не по секундомеру.</div>
      </label>
      <div style="margin-top:14px">
        <div class="field-label" style="margin-bottom:8px">Когда писать посты (UTC)</div>
        <div class="row" style="gap:10px">
          <div style="flex:1"><div style="font-size:11px;color:var(--text-faint);margin-bottom:4px">С</div>
            <input id="f_ws" placeholder="09:00" value="${c.publish_window_start||""}"></div>
          <div style="flex:1"><div style="font-size:11px;color:var(--text-faint);margin-bottom:4px">До</div>
            <input id="f_we" placeholder="22:00" value="${c.publish_window_end||""}"></div>
        </div>
        <div class="hint">${c.auto_publish
          ? "Пишем и публикуем посты только в это время. Москва = UTC+3."
          : "Новые посты пишем только в это время. Когда они выйдут в канал — решаете вы. Москва = UTC+3."}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Правила стиля</div>
      <div class="hint" style="margin-top:0;margin-bottom:12px">ИИ соблюдает эти правила при каждой генерации.</div>
      <div id="rules_list">
        ${rules.length?rules.map(r=>`<div class="src-row" id="rule_${r.id}">
          <span style="font-size:13px;color:var(--text-dim)">✦ ${esc(r.rule_text)}</span>
          <button class="btn-danger btn-sm" onclick="deleteRule(${r.id})">Удалить</button>
        </div>`).join(""):`<p style="font-size:13px;color:var(--text-faint)">Правил пока нет.</p>`}
      </div>
      <div class="row" style="margin-top:10px;gap:8px">
        <input id="new_rule" placeholder="Например: не использовать длинное тире" style="flex:1">
        <button class="btn btn-sm" onclick="addRule()">Добавить</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">ИИ-консультант</div>
      <div class="hint" style="margin-top:0;margin-bottom:12px">
        Объясните, как хотите, чтобы писались посты — ИИ задаст вопросы и предложит конкретные правила.
      </div>
      <div id="consult_msgs" style="max-height:260px;overflow-y:auto;margin-bottom:10px;display:flex;flex-direction:column;gap:8px"></div>
      <div class="row" style="gap:8px">
        <input id="consult_in" placeholder="Например: не хочу длинное тире и старые новости" style="flex:1"
          onkeydown="if(event.key==='Enter') sendConsult()">
        <button class="btn btn-sm" onclick="sendConsult()" id="consult_btn">→</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Источники информации</div>
      <div class="hint" style="margin-top:0;margin-bottom:12px">Сайты или RSS которые ИИ просматривает перед написанием.</div>
      <div class="row" style="gap:8px">
        <input id="srcUrl" placeholder="https://example.com" style="flex:1">
        <button class="btn btn-sm" onclick="addSource()">Добавить</button>
      </div>
      <div id="srcList" style="margin-top:10px">
        ${sources.length?sources.map(s=>`<div class="src-row">
          <span class="src-url">${esc(s.url)}</span>
          <button class="btn-danger btn-sm" onclick="delSource(${s.id})">Удалить</button>
        </div>`).join(""):`<p style="font-size:13px;color:var(--text-faint)">Нет источников — ИИ ищет сам.</p>`}
      </div>
    </div>

    <div class="row between mt-lg"><div></div>
      <button class="btn" onclick="saveAdvanced()">Сохранить</button></div>`;
}

function pickAdv(h,btn){
  _advInterval=h;
  document.querySelectorAll("#seg_int button").forEach(b=>b.classList.remove("on"));
  btn.classList.add("on");
}

async function sendConsult(){
  const input=$("consult_in");const msg=(input?.value||"").trim();if(!msg) return;
  input.value="";
  const msgs=$("consult_msgs");
  msgs.insertAdjacentHTML("beforeend",`<div style="align-self:flex-end;background:var(--accent-soft);border-radius:12px 12px 4px 12px;padding:10px 14px;max-width:85%;font-size:14px">${esc(msg)}</div>`);
  const btn=$("consult_btn");btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  App._consultHistory.push({role:"user",content:msg});
  try{
    const r=await api("POST","/channels/"+App._chan.id+"/consult",{message:msg,history:App._consultHistory.slice(-6)});
    App._consultHistory.push({role:"assistant",content:r.response});
    msgs.insertAdjacentHTML("beforeend",`<div style="align-self:flex-start;background:var(--surface2);border-radius:12px 12px 12px 4px;padding:10px 14px;max-width:85%;font-size:14px">
      ${esc(r.response)}
      ${r.suggested_rule ? (()=>{
        const rid="sr_"+Date.now();
        window["_sr_"+rid]=r.suggested_rule;
        return '<div style="margin-top:10px;padding:8px 12px;background:var(--green-bg);border-radius:8px;font-size:13px">'
          +'<b>Предлагаю правило:</b> '+esc(r.suggested_rule)+'<br>'
          +'<button class="btn btn-sm" style="margin-top:6px;background:var(--green);color:#fff" '
          +'onclick="addSuggestedRule(window[\'_sr_'+rid+'\'])">Добавить правило</button></div>';
      })() : ""}
    </div>`);
    msgs.scrollTop=msgs.scrollHeight;
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
  btn.innerHTML="→";btn.disabled=false;
}

async function addSuggestedRule(text){
  try{await api("POST","/channels/"+App._chan.id+"/rules",{rule_text:text});toast("Правило добавлено ✓","ok");renderAdvanced();}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function addRule(){
  const text=($("new_rule")||{value:""}).value.trim();if(!text) return;
  try{await api("POST","/channels/"+App._chan.id+"/rules",{rule_text:text});$("new_rule").value="";renderAdvanced();}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function deleteRule(id){
  try{await api("DELETE","/rules/"+id);renderAdvanced();}catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function addSource(){
  const url=($("srcUrl")||{value:""}).value.trim();if(!url) return;
  try{await api("POST","/channels/"+App._chan.id+"/sources",{url});$("srcUrl").value="";renderAdvanced();}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function delSource(id){
  try{await api("DELETE","/sources/"+id);renderAdvanced();}catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}

// SHARED ACTIONS
function pickLen(o){
  App._chan.post_length=o;
  document.querySelectorAll("#seg_len button").forEach(b=>b.classList.toggle("on",b.textContent===o));
}
function pickOpt(field,val,segId){
  App._chan[field]=val;
  document.querySelectorAll(`#${segId} button`).forEach(b=>b.classList.remove("on"));
  event.target.classList.add("on");
}
async function _silentSave(){
  if(!$("f_title")) return;
  try{await api("PATCH","/channels/"+App._chan.id,{
    title:($("f_title")||{value:App._chan.title}).value.trim(),
    about:$("f_about")?$("f_about").value:App._chan.about,
    style:$("f_style")?$("f_style").value:App._chan.style,
    post_length:App._chan.post_length,
    use_web_search:$("sw_web")?$("sw_web").checked:App._chan.use_web_search,
    auto_publish:$("sw_auto")?$("sw_auto").checked:App._chan.auto_publish,
  });}catch(_){}
}
async function saveChannel(){
  const newChat=($("f_chat")||{value:App._chan.tg_chat||""}).value.trim();
  const chatChanged=newChat!==(App._chan.tg_chat||"");
  const payload={
    title:($("f_title")||{value:App._chan.title}).value.trim(),
    about:$("f_about")?$("f_about").value:App._chan.about,
    style:$("f_style")?$("f_style").value:App._chan.style,
    post_length:App._chan.post_length,
    use_web_search:$("sw_web")?$("sw_web").checked:App._chan.use_web_search,
    auto_publish:$("sw_auto")?$("sw_auto").checked:App._chan.auto_publish,
  };
  if(chatChanged) payload.tg_chat=newChat;
  const notif={
    notify_published:$("sw_n2")?$("sw_n2").checked:false,
    notify_low_tokens:$("sw_n3")?$("sw_n3").checked:true,
  };
  try{
    await api("PATCH","/channels/"+App._chan.id,payload);
    await api("PATCH","/me",notif);
    // Обновляем локально без перерендера — чтобы тумблеры не сбросились
    if(App.user){ App.user.notify_published=notif.notify_published; App.user.notify_low_tokens=notif.notify_low_tokens; }
    toast("Сохранено ✓","ok");
  }catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function saveAdvanced(){
  const ih=_advInterval!=null?_advInterval:(App._chan.interval_hours||12);
  const payload={
    channel_type:App._chan.channel_type||"thematic",
    post_voice:App._chan.post_voice||"author",
    post_format:App._chan.post_format||"story",
    emoji_style:App._chan.emoji_style||"minimal",
    cta_enabled:$("sw_cta")?$("sw_cta").checked:false,
    cta_text:($("f_cta")||{value:""}).value,
    interval_hours:parseFloat(ih),
    interval_jitter_minutes:parseInt(($("f_jitter")||{value:0}).value)||0,
    publish_window_start:($("f_ws")||{value:""}).value,
    publish_window_end:($("f_we")||{value:""}).value,
  };
  try{
    await api("PATCH","/channels/"+App._chan.id,payload);
    App._chan={...App._chan,...payload};
    _advInterval=null;
    toast("Сохранено ✓","ok");
  }catch(e){
    const msg=e&&e.message?e.message:typeof e==="string"?e:"Ошибка сохранения";
    toast(msg,"err");
  }
}
function showVerifyInput(){
  const block=$("verifyInputBlock");if(block) block.classList.remove("hidden");
}

async function verifyChannel(){
  if(!requireAuth()) return;
  const chat=($("f_chat")||{value:""}).value.trim();if(!chat) return toast("Введите @username или ссылку","err");
  const btn=$("verBtn");if(btn) btn.innerHTML='<span class="spinner"></span>';
  try{
    await api("PATCH","/channels/"+App._chan.id,{tg_chat:chat});
    const r=await api("POST","/channels/"+App._chan.id+"/verify");
    if(r.ok){
      App._chan.tg_chat=chat;App._chan.verified=true;
      trackGoal("telegram_verified",{channel_id:App._chan.id});
      toast("Канал подключён ✓ Готовим первые посты","ok");
      // Раньше пользователь оставался в настройках и не понимал, что дальше:
      // очередь не обновлялась, сообщений не было, посты появлялись только
      // после ручной перезагрузки страницы. Теперь сразу показываем очередь
      // и явно говорим, что посты готовятся (генерация идёт на сервере в
      // течение минуты-двух, см. tick).
      App._justConnectedAt = Date.now();
      setTab("queue");
    } else {
      const msg=$("verMsg");if(msg){msg.textContent=r.message;msg.style.color="var(--red)";}
      if(btn) btn.innerHTML="Проверить";
    }
  }catch(e){
    const msg=$("verMsg");if(msg){msg.textContent=e.message;msg.style.color="var(--red)";}
    if(btn) btn.innerHTML="Проверить";
  }
}
async function analyzeChannel(){
  const link=($("f_analyze")||{value:""}).value.trim();if(!link) return;
  const btn=$("anBtn");btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const r=await api("POST","/channels/"+App._chan.id+"/analyze",{link});
    App._chan.style_profile=r.profile;
    const el=$("analyze_result");
    if(el) el.innerHTML=`<div class="hint" style="color:var(--green);margin-top:6px">✓ Изучено постов: ${r.analyzed_posts}. Стиль сохранён.</div>`;
    trackGoal("style_analyzed",{source:"channel",channel_id:App._chan.id});
    toast("Стиль изучен ✓","ok");
  }catch(e){const el=$("analyze_result");if(el) el.innerHTML=`<div class="hint" style="color:var(--red);margin-top:6px">${esc(e.message)}</div>`;}
  btn.innerHTML="Изучить";btn.disabled=false;
}
async function deleteChannel(){
  if(!confirm("Удалить канал и все посты?")) return;
  try{await api("DELETE","/channels/"+App._chan.id);toast("Удалено","ok");go("dashboard");}
  catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}
async function generateNow(){
  const about=App._chan.about||"";if(!about.trim()) return toast("Заполните тему канала","err");
  const topic=($("genTopic")||{value:""}).value.trim();
  const btn=$("genRunBtn");if(btn) btn.innerHTML='<span class="spinner"></span>';
  await _silentSave();
  let attempts=0;
  while(attempts<3){
    try{
      const r=await api("POST","/channels/"+App._chan.id+"/generate",topic?{topic}:{});
      trackGoal("post_generated",{source:"manual",channel_id:App._chan.id});
      toast("Готово ✓","ok");
      if($("genPanel")) $("genPanel").classList.add("hidden");
      if($("genTopic")) $("genTopic").value="";
      App.tab="queue";renderChannel();return;
    }catch(e){
      const is529=e.message.includes("529")||e.message.toLowerCase().includes("overload");
      const isLimit=e.message.toLowerCase().includes("токен")||e.message.toLowerCase().includes("баланс");
      if(isLimit) logProductEvent("limit_reached");
      attempts++;
      if(is529&&attempts<3){toast(`Серверы заняты, повтор через 15с… (${attempts}/3)`);await new Promise(r=>setTimeout(r,15000));}
      else{toast(is529?"Серверы перегружены. Попробуйте позже.":e.message,"err");if(btn) btn.innerHTML="Создать";return;}
    }
  }
}

// BILLING
async function renderBilling(){
  await refreshUser();
  logProductEvent("pricing_viewed");
  try{
    const r=await api("GET","/subscription");
    App._subscription=r.subscription; App._paymentMethod=r.payment_method||null;
  }catch(_){ App._subscription=null; App._paymentMethod=null; }
  const plans=[
    {id:"p1",name:"Старт",price:490,regular:990,channels:1,postsMin:15,postsMax:30},
    {id:"p2",name:"Про",price:990,regular:1990,channels:3,postsMin:30,postsMax:60,popular:true},
    {id:"p3",name:"Бизнес",price:2490,regular:4990,channels:10,postsMin:75,postsMax:150},
    {id:"p4",name:"Агентство",price:4990,regular:9990,channels:0,postsMin:150,postsMax:300},
  ];
  const sub=App._subscription||null;
  $("app").innerHTML=topbar("dashboard","назад")+`<div class="wrap">
    <div class="page-head"><h1>Тарифы</h1>
      <p>Осталось <b>${Math.floor((App.user?.token_balance||0)/40000)}–${Math.floor((App.user?.token_balance||0)/20000)}</b> постов.<br>
      <span style="font-size:13px;color:var(--text-faint)">Диапазон зависит от сложности: пост с поиском свежих новостей расходует больше, простой — меньше.</span></p></div>
    ${(!App.cfg?.yookassa_enabled&&!App.cfg?.yoomoney_enabled)?`<div class="card" style="border-color:var(--accent);background:var(--accent-soft);margin-bottom:16px">
      <p style="color:var(--accent-dark)">Приём платежей настраивается.</p></div>`:""}
    ${_subscriptionCard(sub)}
    ${_paymentMethodBlock()}
    ${plans.some(p=>p.regular)?`<div class="promo-bar">
      <b>Цены на время запуска.</b> Сервис ещё развивается — пока он в раннем доступе, тарифы держим ниже
      обычных.${App.cfg?.subscription_enabled?" Цена, по которой вы подписались, за вами сохранится.":""}
    </div>`:""}
    <div class="grid grid-2" style="margin-bottom:16px">
      ${plans.map(p=>`<div class="price-card" style="position:relative;${p.popular?"border-color:var(--accent)":""}">
        ${p.popular?`<div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-size:11px;font-weight:600;padding:2px 12px;border-radius:99px;white-space:nowrap">Популярный</div>`:""}
        <div class="p-name">${p.name}</div>
        ${p.regular?`<div class="p-regular">потом ${_rub(p.regular)} ₽</div>`:""}
        <div class="p-price" style="font-size:24px">${_rub(p.price)}\u00A0₽${App.cfg?.subscription_enabled?"/мес":""}</div>
        <div class="p-tokens" style="line-height:1.8">
          📺 ${p.channels===0?"Без лимита каналов":`${p.channels} ${_plural(p.channels,"канал","канала","каналов")}`}<br>
          ✦ ${p.postsMin}–${p.postsMax} постов${App.cfg?.subscription_enabled?"/мес":""}</div>
        <button class="btn" style="width:100%;justify-content:center;margin-top:8px" onclick="buy('${p.id}')">Выбрать</button>
      </div>`).join("")}
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">🎁 Реферальная программа</div>
      <p style="font-size:14px;color:var(--text-dim);margin-bottom:12px">Пригласите друга — каждому из вас придёт примерно 6–10 бесплатных постов (200 000 токенов).</p>
      <div id="ref_block" class="text-faint">Загрузка…</div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <button onclick="togglePayHistory()" id="pay_hist_btn"
        style="background:none;border:none;cursor:pointer;font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px;width:100%;padding:0;min-height:44px">
        📋 История платежей <span id="pay_hist_arrow" style="font-size:12px;color:var(--text-faint)">▶</span>
      </button>
      <div id="payList" class="hidden text-faint"></div>
    </div>
    <div style="text-align:center;margin-top:16px;padding-bottom:8px">
      <button class="btn-danger btn-sm" onclick="deleteAccount()" style="font-size:12px;opacity:.6">Удалить аккаунт</button>
    </div></div>`;
  try{
    const me=await api("GET","/me");const code=me.ref_code||"";
    $("ref_block").innerHTML=`
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
        <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:600;letter-spacing:.1em;background:var(--surface2);border:1px solid var(--border-soft);border-radius:10px;padding:10px 18px;flex:1;text-align:center">${esc(code)}</div>
        <button class="btn-outline btn-sm" onclick="navigator.clipboard.writeText('${esc(code)}').then(()=>toast('Скопировано','ok'))">Копировать</button>
      </div>
      <div style="font-size:13px;color:var(--text-dim);background:var(--surface2);border-radius:10px;padding:12px 14px;line-height:1.7">
        1. Откройте <a href="https://t.me/maintrpost_bot" target="_blank" style="color:var(--accent);display:inline-block;padding:14px 0">@maintrpost_bot</a><br>
        2. «Открыть АвтоПост» → Зарегистрироваться<br>
        3. Введите реферальный код: <b>${esc(code)}</b>
      </div>
      <div class="hint" style="margin-top:8px">Приглашений: <b>${me.referrals_count||0}</b></div>`;
  }catch(_){}
  // История платежей загружается лениво при раскрытии
  window._loadPayHistory = async function(){
    try{
      const ps=await api("GET","/payments");
      ps.forEach(p=>{
        if(p.status==="paid"){
          const key="ym_paid_"+(p.id||p.label||p.created_at);
          if(!localStorage.getItem(key)){
            trackGoal("payment_success",{package_id:p.package_id||"",tokens:p.tokens||0,rub:p.rub||0});
            localStorage.setItem(key,"1");
          }
        }
      });
      // В истории платежей не было главного — суммы. Строка выглядела как
      // «27.07.2026, 14:05 · 600 000 ток.»: внутренняя единица учёта на первом
      // месте и ни рубля. Человек заходит сюда посмотреть, сколько и за что
      // заплатил, поэтому деньги вынесены вперёд, а токены остались справочно.
      // Прежний класс .src-url не подошёл: у него white-space:nowrap, и вторая
      // строка в него не помещалась.
      $("payList").innerHTML=ps.length
        ?ps.map(p=>{
            const pkg=(App.cfg?.packages||[]).find(x=>x.id===p.package_id);
            const when=new Date(p.created_at+"Z").toLocaleString("ru-RU");
            return `<div class="src-row" style="align-items:flex-start">
              <div style="min-width:0">
                <div style="font-size:13px;color:var(--text);font-weight:600">${_rub(p.rub||0)}\u00A0₽${pkg?` · ${esc(pkg.title)}`:""}</div>
                <div style="font-size:12px;color:var(--text-faint);margin-top:2px">${when} · ${fmt(p.tokens)} токенов</div>
              </div>
              <span class="chip ${p.status==="paid"?"chip-green":"chip-orange"}">${p.status==="paid"?"оплачено":"ожидает оплаты"}</span>
            </div>`;
          }).join("")
        :`<p style="font-size:13px;color:var(--text-faint)">Платежей пока не было.</p>`;
    }catch(_){}
  };
}

function togglePayHistory(){
  const list=$("payList"),arrow=$("pay_hist_arrow");
  if(!list) return;
  const hidden=list.classList.contains("hidden");
  list.classList.toggle("hidden",!hidden);
  if(arrow) arrow.textContent=hidden?"▼":"▶";
  if(hidden && window._loadPayHistory) window._loadPayHistory();
}

// 1290 -> "1 290" (неразрывный пробел, чтобы цена не переносилась)
function _rub(n){ return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }

// Русское склонение по числу: 1 канал / 2 канала / 10 каналов.
function _plural(n, one, few, many){
  const a=Math.abs(n)%100, b=a%10;
  if(a>10&&a<20) return many;
  if(b>1&&b<5) return few;
  if(b===1) return one;
  return many;
}

// Управление сохранённым способом оплаты. Блок показывается ВСЕГДА, даже
// когда карта не привязана: ЮKassa подключает рекуррентные платежи только
// тем магазинам, где покупатель видит и может сам выполнить сценарий отвязки
// карты, не обращаясь в поддержку. Если показывать блок лишь при активной
// подписке, сценарий невозможно ни увидеть, ни продемонстрировать.
function _paymentMethodBlock(){
  const pm=App._paymentMethod||null;
  const has=!!pm;
  return `<div class="card" style="margin-bottom:16px">
    <div class="card-title">Способ оплаты</div>
    <div class="pm-row">
      <label class="pm-item">
        <input type="checkbox" id="pm_confirm" ${has?"":"disabled"} onchange="pmToggle()">
        <span>
          <b>${has?esc(pm.title):"Сохранённых карт нет"}</b>
          <small>${has
            ? "Привязана для автоматического продления подписки"
            : "Карта появится здесь после оплаты с сохранением способа оплаты"}</small>
        </span>
      </label>
      <button class="btn-danger btn-sm" id="pm_delete" disabled onclick="deletePaymentMethod()">Удалить карту</button>
    </div>
    <div class="hint" style="margin-top:10px">
      Отметьте карту и нажмите «Удалить карту» — мы удалим сохранённый способ оплаты
      и прекратим автоматические списания. Обращаться в поддержку не нужно.
    </div>
  </div>`;
}

// Кнопка удаления активна только после явной отметки чек-бокса -- защита от
// случайного нажатия на необратимое действие.
function pmToggle(){
  const c=$("pm_confirm"), b=$("pm_delete");
  if(c&&b) b.disabled=!c.checked;
}

async function deletePaymentMethod(){
  const c=$("pm_confirm");
  if(!c||!c.checked) return;
  if(!confirm("Удалить сохранённую карту?\n\nАвтоматические списания прекратятся. "+
              "Оплаченный период и уже начисленные посты останутся при вас.")) return;
  const b=$("pm_delete");
  if(b){ b.disabled=true; b.innerHTML='<span class="spinner"></span>'; }
  try{
    await api("DELETE","/subscription");
    logProductEvent("payment_method_deleted");
    toast("Карта удалена, списаний больше не будет","ok");
    renderBilling();
  }catch(e){
    toast(e&&e.message?e.message:"Не удалось удалить карту","err");
    if(b){ b.disabled=false; b.innerHTML="Удалить карту"; }
  }
}

function _subscriptionCard(sub){
  const days=App.cfg?.subscription_period_days||30;
  if(!sub){
    // Пока рекуррент не согласован с ЮKassa, обещать автосписание нельзя --
    // его не будет. Тогда это честная разовая оплата пакета.
    if(!App.cfg?.subscription_enabled){
      return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:16px;padding:14px 16px">
        <div style="font-size:13px;color:var(--text-dim);line-height:1.6">
          Оплата разовая: списываем один раз и сразу начисляем посты. Ничего не спишется автоматически —
          когда посты закончатся, просто оплатите снова.
        </div></div>`;
    }
    // Честно предупреждаем о характере платежа до того, как человек нажмёт
    // «Выбрать», а не только в момент подтверждения.
    return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:16px;padding:14px 16px">
      <div style="font-size:13px;color:var(--text-dim);line-height:1.6">
        Тарифы — это подписка: плата списывается автоматически раз в ${days} дней, пока вы её не отмените.
        Отменить подписку и отвязать карту можно в любой момент здесь же, деньги за оплаченный период не сгорают.
      </div></div>`;
  }
  const when=sub.next_charge_at
    ? new Date(sub.next_charge_at).toLocaleDateString("ru-RU",{day:"numeric",month:"long",year:"numeric"})
    : "—";
  if(sub.status==="suspended"){
    return `<div class="card" style="background:var(--accent-soft);border:none;margin-bottom:16px;padding:14px 16px">
      <div style="font-size:13px;color:var(--accent-dark);font-weight:600">Подписка «${esc(sub.title)}» приостановлена</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        Списать оплату не получилось. Автосписания остановлены — выберите тариф ниже, чтобы возобновить подписку.
      </div>
      <button class="btn-ghost btn-sm" style="margin-top:8px;padding:4px 0;color:var(--accent-dark)" onclick="cancelSubscription()">Отменить подписку и отвязать карту</button>
    </div>`;
  }
  return `<div class="card" style="background:var(--green-bg,var(--surface2));border:none;margin-bottom:16px;padding:14px 16px">
    <div style="font-size:13px;font-weight:600">Подписка «${esc(sub.title)}» активна</div>
    <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
      Следующее списание ${sub.rub?`${sub.rub} ₽ `:""}— ${when} Дальше каждые ${days} дней, пока не отмените.
    </div>
    <button class="btn-ghost btn-sm" style="margin-top:8px;padding:4px 0;color:var(--red,#c0392b)" onclick="cancelSubscription()">Отменить подписку и отвязать карту</button>
  </div>`;
}

async function cancelSubscription(){
  if(!confirm(
    "Отменить подписку и отвязать карту?\n\n"+
    "Автосписания прекратятся, сохранённый способ оплаты будет удалён — "+
    "списать по нему больше не сможем.\n\n"+
    "Оплаченный период и уже начисленные посты останутся при вас."
  )) return;
  try{
    await api("DELETE","/subscription");
    logProductEvent("subscription_cancelled");
    toast("Подписка отменена","ok");
    renderBilling();
  }catch(e){
    toast(e&&e.message?e.message:"Не удалось отменить подписку","err");
  }
}

async function buy(pid){
  logProductEvent("payment_cta_clicked", pid);
  // Регулярное списание обязано быть раскрыто ДО оплаты, явно и своими
  // словами -- человек должен понимать, что подписывается на повторяющийся
  // платёж, а не платит один раз. Отмена тут же названа, чтобы это не
  // выглядело ловушкой.
  const plan=(App.cfg?.packages||[]).find(p=>p.id===pid);
  const days=App.cfg?.subscription_period_days||30;
  if(plan && App.cfg?.subscription_enabled){
    const ok=confirm(
      `Тариф «${plan.title}» — ${plan.rub} ₽ каждые ${days} дней.\n\n`+
      `Первый платёж спишется сейчас, дальше — автоматически раз в ${days} дней, `+
      `пока вы не отмените подписку.\n\n`+
      `Отменить и отвязать карту можно в любой момент на этой же странице.`
    );
    if(!ok){ logProductEvent("payment_declined_at_confirm", pid); return; }
  }
  try{
    const r = await api("POST", "/billing/buy", {package_id: pid});
    trackGoal("payment_started",{package_id:pid});
    if(!r.payment_url){
      logProductEvent("payment_failed", pid);
      toast("Не удалось получить ссылку на оплату","err");
      return;
    }
    // Telegram Mini App — используем встроенный метод
    if(window.Telegram?.WebApp?.openLink){
      window.Telegram.WebApp.openLink(r.payment_url);
    } else {
      window.location.href = r.payment_url;
    }
  } catch(e){
    logProductEvent("payment_failed", pid);
    toast(e&&e.message?e.message:"Ошибка запроса","err");
  }
}
async function deleteAccount(){
  if(!confirm("Удалить аккаунт?\n\nЭто удалит все каналы, посты и данные.")) return;
  if(prompt("Введите DELETE:")!=="DELETE") return toast("Отменено");
  try{await api("DELETE","/me");toast("Удалено","ok");logout();}catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}

// COOKIE + KEYBOARD
async function verifyTgUsername(){
  const username=($("f_tg_username")||{value:""}).value.trim();
  if(!username) return toast("Введите @username","err");
  const btn=$("tg_check_btn"),msg=$("tg_check_msg");
  btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const r=await api("POST","/me/verify_tg",{username});
    msg.textContent=r.message;
    msg.style.color=r.ok?"var(--green)":"var(--red)";
    if(r.ok) App.user.tg_username=username;
  }catch(e){msg.textContent=e.message;msg.style.color="var(--red)";}
  btn.innerHTML="Проверить";btn.disabled=false;
}

async function toggleChannelEnabled(){
  const c=App._chan;
  const newVal=!c.enabled;
  try{
    await api("PATCH","/channels/"+c.id,{enabled:newVal});
    App._chan.enabled=newVal;
    if(newVal) App._chan.last_generated_at=new Date().toISOString(); // таймер с нуля
    const btn=$("pause_btn");
    if(btn){btn.textContent=newVal?"⏸ Пауза":"▶ Возобновить";btn.className=newVal?"btn-outline btn-sm":"btn btn-sm";}
    if(App.tab==="queue") renderQueue();
    toast(newVal?"Канал запущен — генерируем посты…":"Публикация приостановлена","ok");
  }catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}

function showPicker(id){
  const p=$("picker_"+id);if(!p) return;p.classList.remove("hidden");
  const dt=$("dt_"+id);if(dt) dt.value=new Date(Date.now()+3600000).toISOString().slice(0,16);
}
async function doSchedule(id){
  const dt=$("dt_"+id);if(!dt||!dt.value) return toast("Выберите дату","err");
  try{await api("POST","/posts/"+id+"/schedule",{scheduled_at:dt.value});toast("Запланировано ✓","ok");renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}
function toggleEdit(id){
  const ta=$("pt_"+id),pw=$("ppreview_"+id),sb=$("save_"+id);if(!ta) return;
  const hidden=ta.classList.contains("hidden");
  ta.classList.toggle("hidden",!hidden);
  if(pw) pw.classList.toggle("hidden",hidden);
  if(sb) sb.classList.toggle("hidden",!hidden);
}
async function savePost(id){
  const el=$("pt_"+id);if(!el) return;
  try{await api("PATCH","/posts/"+id,{text:el.value});toast("Сохранено ✓","ok");renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}
// КРИТИЧНО (UX fix): "Опубликовать сейчас" раньше публиковало мгновенно и
// безвозвратно в реальный Телеграм-канал -- ни одной секунды на передумать,
// даже для только что созданного поста, который никто ещё не смотрел
// (например сразу после подключения канала). Тот же принцип "минута на
// отмену", что уже есть в режиме "публикация после подтверждения" (см.
// SOFT_CONTROL_FINAL_GRACE_SECONDS), теперь и здесь: клик запускает 60-сек
// обратный отсчёт с кнопкой отмены вместо немедленной публикации.
const _pendingPublish = {}; // postId -> {intervalId, timeoutId}

function _publishBtnFor(id){
  const card=$("pc_"+id);
  return card?card.querySelector(`[onclick="publishPost(${id})"]`):null;
}

async function publishPost(id){
  if(!requireAuth()) return;

  // Повторный клик во время отсчёта -- это отмена, не повторный запуск.
  if(_pendingPublish[id]){
    clearInterval(_pendingPublish[id].intervalId);
    clearTimeout(_pendingPublish[id].timeoutId);
    delete _pendingPublish[id];
    const btn=_publishBtnFor(id);
    if(btn) btn.textContent="Опубликовать сейчас";
    toast("Публикация отменена","ok");
    return;
  }

  // P0 fix (third issue): если канал не подключён к Телеграм, не пытаемся
  // публиковать вообще — показываем понятное сообщение вместо того чтобы
  // дать запросу дойти до Telegram API и упасть с технической ошибкой.
  const chan = App._chan;
  if (chan && (!chan.tg_chat || !chan.verified)) {
    toast("Сначала подключите Телеграм-канал, потом можно будет опубликовать пост.", "err");
    return;
  }

  const ta=$("pt_"+id);
  if(ta&&!ta.classList.contains("hidden")) try{await api("PATCH","/posts/"+id,{text:ta.value});}catch(_){}

  const _fmt=(s)=>`${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
  const btn=_publishBtnFor(id);
  let secondsLeft=60;
  if(btn) btn.textContent=`Отменить (${_fmt(secondsLeft)})`;
  const intervalId=setInterval(()=>{
    secondsLeft--;
    if(secondsLeft<=0){clearInterval(intervalId);return;}
    const liveBtn=_publishBtnFor(id);
    if(liveBtn) liveBtn.textContent=`Отменить (${_fmt(secondsLeft)})`;
  },1000);
  const timeoutId=setTimeout(()=>{
    delete _pendingPublish[id];
    _doPublishPost(id);
  },60000);
  _pendingPublish[id]={intervalId,timeoutId};
  toast("Опубликуется через минуту — можно отменить","ok");
}

async function _doPublishPost(id){
  const btn=_publishBtnFor(id);
  if(btn){btn.innerHTML='<span class="spinner"></span> Публикуем…';btn.disabled=true;}

  const TIMEOUT_MS=18000;
  let timedOut=false, error=null, result=null;
  try {
    ({timedOut, error, result} = await withTimeout(api("POST","/posts/"+id+"/publish"), TIMEOUT_MS, "timeout"));
  } catch (e) {
    // withTimeout перебрасывает НЕ-таймаут ошибки (например HTTPException
    // 400 "Telegram сейчас отвечает медленно..." от бэкенда) -- раньше это
    // никто не ловил и необработанный reject стирал всю страницу целиком.
    error = e;
  }

  if(timedOut){
    if(btn) btn.innerHTML='<span class="spinner"></span> Проверяем статус…';
    const {confirmed}=await pollPostStatus(id);
    if(confirmed){tgHaptic("success");toast("Опубликовано ✓","ok");renderQueue();return;}
    tgHaptic("error");
    toast("Не удалось подтвердить публикацию. Проверьте канал или попробуйте ещё раз.","err");
    if(btn){btn.innerHTML="Опубликовать сейчас";btn.disabled=false;}
    return;
  }
  if(error){
    tgHaptic("error");
    toast((error&&error.message)||"Не удалось опубликовать пост. Попробуйте ещё раз.","err");
    if(btn){btn.innerHTML="Опубликовать сейчас";btn.disabled=false;}
    return;
  }
  tgHaptic("success");
  toast("Опубликовано ✓","ok");renderQueue();
}
async function rejectPost(id){
  try{await api("POST","/posts/"+id+"/reject");renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}

// Оценка поста (👍/👎). Повторное нажатие по той же кнопке снимает оценку --
// иначе поставленную случайно уже не убрать.
//
// Перерисовываем только эту карточку, а не всю очередь: renderQueue()
// перезапрашивал бы список и сбрасывал раскрытые посты и открытые меню, а
// оценка ничего в очереди не двигает -- ни порядок, ни состав.
async function ratePost(id, verdict){
  const posts = App._queuePosts || [];
  const post = posts.find(p => p.id === id);
  if(!post) return;
  const next = post.feedback === verdict ? "none" : verdict;
  const previous = post.feedback || null;
  post.feedback = next === "none" ? null : next;   // оптимистично, до ответа
  renderQueueBody();
  try{
    await api("POST","/posts/"+id+"/feedback",{verdict:next});
  }catch(e){
    post.feedback = previous;                       // не сохранилось -- откатываем
    renderQueueBody();
    toast(e&&e.message?e.message:"Не удалось сохранить оценку","err");
  }
}
async function deletePost(id){
  try{await api("DELETE","/posts/"+id);renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}
async function regenPost(id){
  const btn=$("regen_"+id);if(btn){btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;}
  try{
    await api("POST","/posts/"+id+"/reject");
    const r=await api("POST","/channels/"+App._chan.id+"/generate");
    toast("Готово ✓","ok");renderQueue();
  }catch(e){toast(e&&e.message?e.message:"Ошибка","err");if(btn){btn.innerHTML="↻ Заново";btn.disabled=false;}}
}

async function openTgConnect(){
  // Гарантируем что user загружен
  if(!App.user || !App.user.id){
    try{ App.user = await api("GET","/me"); }catch(e){}
  }
  const uid = App.user?.id;
  if(!uid){
    // Нет токена — пользователь не авторизован (например Mini App без localStorage)
    const twa = window.Telegram?.WebApp;
    if(twa && typeof twa.showAlert==='function'){
      // Домен берём из /api/config (App.cfg.public_url), не хардкодим --
      // при следующем переезде домена не должно снова "зависать" тут.
      const site=(App.cfg?.public_url||"https://projectautopost.ru").replace(/^https?:\/\//,"");
      twa.showAlert(`Войдите в аккаунт на сайте ${site}, а затем откройте уведомления снова.`);
    } else {
      toast("Не удалось определить аккаунт. Попробуйте войти заново.","err");
    }
    return;
  }
  const bot = App.cfg?.bot_username || "trpst_bot";
  const url = "https://t.me/" + bot + "?start=u" + uid;
  const twa = window.Telegram?.WebApp;
  if(twa?.openLink){
    twa.openLink(url);
  } else if(twa?.openTelegramLink){
    twa.openTelegramLink(url);
  } else {
    window.open(url,"_blank");
  }
}

function pickChannelType(type){
  App._chan.channel_type=type;
  const ta=$("adv_type_thematic"),tn=$("adv_type_news"),tl=$("adv_sched_title");
  if(ta) ta.style.border=type==="thematic"?"2px solid var(--accent)":"2px solid var(--border-soft)";
  if(ta) ta.style.background=type==="thematic"?"var(--accent-soft)":"";
  if(tn) tn.style.border=type==="news"?"2px solid var(--accent)":"2px solid var(--border-soft)";
  if(tn) tn.style.background=type==="news"?"var(--accent-soft)":"";
  if(tl) tl.textContent=type==="news"?"Как часто проверять новости":"Как часто писать новый пост";
}

function initCookieBanner(){
  if(localStorage.getItem("cookie_ok")) return;
  const b=document.createElement("div");
  b.style.cssText="position:fixed;bottom:0;left:0;right:0;background:#171b20;color:#e4e8ec;font-size:13px;padding:4px 20px;display:flex;align-items:center;justify-content:space-between;gap:16px;z-index:9999;";
  b.innerHTML=`<span>Мы используем cookies. <a href="/legal/privacy" target="_blank" style="color:#d8b15e;display:inline-block;padding:14px 0">Подробнее</a></span>
    <button style="background:#d8b15e;color:#1a1404;border:none;border-radius:6px;padding:6px 14px;min-height:44px;cursor:pointer;font-size:13px;font-weight:500">Понятно</button>`;
  document.body.appendChild(b);

  // Плашка прижата к низу и лежит ПОВЕРХ страницы, а запаса снизу у body не
  // было. Замерено на 390px: она целиком закрывала ссылки «Оферта»,
  // «Политика конфиденциальности» и «Возврат» -- перекрытие 19px из 19, и
  // прокрутить дальше нельзя, страница уже кончилась. То есть до нажатия
  // «Понятно» юридические документы были недоступны с телефона вовсе, хотя
  // сама плашка при этом предлагает прочитать «Подробнее».
  // Отступ считаем по фактической высоте: на узком экране текст переносится,
  // и плашка становится выше.
  // Внешний отступ плашки уменьшен с 12px до 4px: «Понятно» и «Подробнее»
  // выросли до 44px по высоте (было 28 и 16), и свой воздух теперь несут
  // они сами -- иначе плашка стала бы на 16px выше и опять полезла бы на
  // юридические ссылки, которые сама же предлагает прочитать.
  const pad=()=>{ document.body.style.paddingBottom=b.offsetHeight+"px"; };
  pad();
  window.addEventListener("resize",pad);

  b.querySelector("button").onclick=()=>{
    b.remove();
    document.body.style.paddingBottom="";
    window.removeEventListener("resize",pad);
    localStorage.setItem("cookie_ok","1");
  };
}
function initKeyboardDismiss(){
  document.addEventListener("touchstart",function(e){
    const a=document.activeElement;
    if(a&&(a.tagName==="INPUT"||a.tagName==="TEXTAREA"))
      if(!e.target.closest("input,textarea,button,select,a")) a.blur();
  },{passive:true});
}

// BOOT
// ── Telegram SDK: асинхронная, не блокирующая загрузка (P0 fix) ──────
// Раньше SDK подключался блокирующим <script> тегом в index.html ДО
// app.js — при медленном/недоступном telegram.org это держало весь boot
// до 15-20 секунд. Теперь SDK грузится программно, параллельно с
// остальной загрузкой приложения, с явным таймаутом, и НИКОГДА не
// блокирует boot()/auth/quick start/channel list (task items 1-3, 6, 8).
const TELEGRAM_SDK_TIMEOUT_MS = 1800;
let _telegramSdkPromise = null;

function loadTelegramSdkAsync(){
  if(_telegramSdkPromise) return _telegramSdkPromise;
  try{ performance.mark('telegram_sdk_started'); }catch(_){}
  console.log('[timing] telegram_sdk_started');

  _telegramSdkPromise = new Promise((resolve)=>{
    // Уже загружен (например повторный вызов после первого успеха).
    if(window.Telegram?.WebApp){
      resolve(true);
      return;
    }
    let settled=false;
    const finish=(ok)=>{
      if(settled) return;
      settled=true;
      resolve(ok);
    };

    const timer=setTimeout(()=>{
      try{ performance.mark('telegram_sdk_failed_or_timeout'); }catch(_){}
      console.log(`[timing] telegram_sdk_failed_or_timeout (>${TELEGRAM_SDK_TIMEOUT_MS}ms)`);
      finish(false);
    }, TELEGRAM_SDK_TIMEOUT_MS);

    const script=document.createElement('script');
    script.src='https://telegram.org/js/telegram-web-app.js';
    script.async=true;
    script.onload=()=>{
      clearTimeout(timer);
      try{ performance.mark('telegram_sdk_loaded'); }catch(_){}
      console.log('[timing] telegram_sdk_loaded');
      finish(true);
    };
    script.onerror=()=>{
      clearTimeout(timer);
      try{ performance.mark('telegram_sdk_failed_or_timeout'); }catch(_){}
      console.log('[timing] telegram_sdk_failed_or_timeout (onerror)');
      finish(false);
    };
    document.head.appendChild(script);
  });

  return _telegramSdkPromise;
}

function initTelegram(){
  // Guarded (task item 4-5): если SDK недоступен — тихо работаем как
  // обычное браузерное приложение, никогда не падаем и не блокируем boot.
  const tg=window.Telegram?.WebApp;
  if(!tg) return;
  try{
    if(typeof tg.ready==='function') tg.ready();
    if(typeof tg.expand==='function') tg.expand();           // на весь экран
    // Цвет — тот же --bg что и у веб-версии (см. static/styles.css :root),
    // не старый кремовый: перекрашен вместе с общей палитрой сайта.
    if(typeof tg.setHeaderColor==='function') tg.setHeaderColor("#eaf1f8");
    if(typeof tg.setBackgroundColor==='function') tg.setBackgroundColor("#eaf1f8");
    if(typeof tg.disableVerticalSwipes==='function') tg.disableVerticalSwipes(); // не закрывать свайпом случайно
    if(typeof tg.enableClosingConfirmation==='function') tg.enableClosingConfirmation(); // не закрывать случайным свайпом/кнопкой назад поверх открытого экрана
  }catch(_){}
}

// Тактильный отклик на ключевых действиях (Mini App onlу — на обычном
// вебе tg отсутствует, вызов просто не происходит).
function tgHaptic(kind){
  try{
    const h=window.Telegram?.WebApp?.HapticFeedback;
    if(!h) return;
    if(kind==="success"||kind==="error"||kind==="warning") h.notificationOccurred(kind);
    else h.impactOccurred(kind||"medium");
  }catch(_){}
}

// Запускаем загрузку SDK не дожидаясь её — если успеет загрузиться,
// initTelegram() вызовется повторно и безопасно доинициализирует
// Telegram-специфичные фичи. Если не успеет/упадёт — приложение уже
// полностью работает в обычном web-режиме, ничего не теряет.
function initTelegramAsync(){
  loadTelegramSdkAsync().then((ok)=>{
    if(ok) initTelegram();
  });
}

async function boot(){
  // Timing instrumentation (task item 4 + 7): измеряем каждый этап загрузки.
  try{ performance.mark('app_js_started'); }catch(_){}
  try{ performance.mark('boot_started'); }catch(_){}
  const t0 = performance.now();
  console.log(`[timing] boot() start, since navigation: ${t0.toFixed(0)}ms`);

  // КРИТИЧНО (P0 fix): Telegram SDK теперь грузится асинхронно и НЕ
  // блокирует ничего ниже — init запускается в фоне, boot() идёт дальше
  // не дожидаясь её. Раньше SDK был синхронным <script> тегом в HTML
  // ДО app.js, что при недоступности telegram.org держало весь boot до
  // 15-20 секунд (см. acceptance tests B/C в задаче).
  initTelegramAsync();
  initTelegram(); // если SDK уже был закэширован браузером — сработает сразу, без вреда если его ещё нет

  const _cameFromLanding = captureLandingSession();

  // КРИТИЧНО (BUG-003 fix): /config больше НЕ блокирует первый экран.
  // App.cfg используется только с ?. и фолбэками (bot_username, флаги
  // оплаты) — для первого рендера он не нужен. Раньше boot ждал ответа
  // /config до показа ЧЕГО-ЛИБО, и при холодном старте Railway (контейнер
  // спит после простоя, пробуждение 10-20 сек) пользователь всё это время
  // видел «Загрузка…». Теперь конфиг грузится в фоне, а экран появляется
  // сразу.
  App.cfg = App.cfg || {packages:[]};
  const tConfigStart = performance.now();
  api("GET","/config").then(cfg=>{
    App.cfg=cfg;
    console.log(`[timing] /config (background): ${(performance.now()-tConfigStart).toFixed(0)}ms`);
  }).catch(_=>{});

  initCookieBanner();initKeyboardDismiss();
  if(!App.token){
    console.log(`[timing] boot() total (no token, -> renderAuth): ${(performance.now()-t0).toFixed(0)}ms`);
    // КРИТИЧНО (UX fix): пришедшего только что с лендинга/рекламы -- у него
    // точно нет аккаунта -- показываем сразу форму регистрации, а не входа.
    // Раньше ЛЮБОЙ визит без токена (и свежий с рекламы, и просто истёкшая
    // сессия у старого пользователя) показывал форму входа по умолчанию --
    // новый человек должен был сам заметить и нажать "Зарегистрироваться".
    renderAuth(_cameFromLanding ? "register" : "login");
    try{ performance.mark('first_screen_visible'); performance.mark('boot_complete'); }catch(_){}
    console.log('[timing] first_screen_visible (renderAuth)');
    return;
  }

  // BUG-003: пока /me грузится (при холодном старте Railway это может быть
  // 10-20 сек), показываем живой скелет вместо статичного «Загрузка…» из
  // index.html -- пользователь видит, что приложение работает, а не зависло.
  $("app").innerHTML=`<div class="wrap" style="max-width:560px;margin-top:60px;text-align:center">
    <div style="font-size:28px;margin-bottom:12px">✦</div>
    <p style="color:var(--text-dim)">Загружаем ваши каналы…</p>
    <p style="color:var(--text-faint);font-size:13px;margin-top:8px">Первый запуск после паузы может занять до полуминуты.</p>
  </div>`;

  try{ performance.mark('me_request_started'); }catch(_){}
  const tMeStart = performance.now();
  try{
    // withTimeout(): та же причина что и у /channels в renderDashboard --
    // без таймаута зависший fetch держал бы пользователя на скелете
    // "Загружаем ваши каналы…" бесконечно, ни единой кнопки повтора.
    // Бросаем обычный Error при таймауте -- он не совпадёт ни с одной из
    // auth-подстрок ниже, поэтому пойдёт по ветке "временный сбой", НЕ
    // логаутнет пользователя, покажет кнопку "Попробовать снова".
    const {timedOut, result} = await withTimeout(api("GET","/me"), 25000, "timeout");
    if(timedOut) throw new Error("Сервер долго не отвечает. Проверьте соединение.");
    App.user=result;
    try{ performance.mark('me_request_finished'); }catch(_){}
    console.log(`[timing] /me: ${(performance.now()-tMeStart).toFixed(0)}ms`);

    // payment_returned: пользователь вернулся со страницы оплаты. Это НЕ
    // означает успешную оплату — просто фиксирует факт возврата, чтобы
    // отличить "ушёл оплачивать и не вернулся" от "вернулся, но не оплатил".
    try{
      const params=new URLSearchParams(window.location.search);
      if(params.get("paid")){
        logProductEvent("payment_returned");
        params.delete("paid");
        const newUrl=window.location.pathname+(params.toString()?"?"+params.toString():"");
        window.history.replaceState({},"",newUrl);
      }
    }catch(_){}

    try{ performance.mark('channels_request_started'); }catch(_){}
    const tRouteStart = performance.now();
    await go("dashboard");
    try{ performance.mark('channels_request_finished'); }catch(_){}
    console.log(`[timing] go('dashboard') incl. /channels + render: ${(performance.now()-tRouteStart).toFixed(0)}ms`);

    try{ performance.mark('first_screen_visible'); }catch(_){}
    console.log('[timing] first_screen_visible (dashboard)');
  }catch(e){
    console.log(`[timing] /me failed after ${(performance.now()-tMeStart).toFixed(0)}ms: ${e&&e.message}`);
    // КРИТИЧНО (P0 fix, acceptance test C/D): logout() должен срабатывать
    // ТОЛЬКО при реальной невалидности сессии (401 -- api() уже сам вызвал
    // logout() в этом случае, внутри себя). Раньше ЛЮБАЯ ошибка /me —
    // включая временный сбой сети, таймаут, или backend ещё не поднялся
    // после деплоя/restart — тоже звала logout(), что СТИРАЛО валидный
    // токен пользователя. Это и есть прямая причина "hard refresh ломает
    // вход" и "после деплоя зарегистрироваться/войти не получается":
    // временная недоступность backend интерпретировалась как "сессия
    // истекла" и убивала токен, который на самом деле был совершенно рабочим.
    const isAuthFailure = e && e.message && (
      e.message.includes("Сессия истекла") ||
      e.message.includes("Не авторизован") ||
      e.message.includes("Пользователь не найден")
    );
    if (isAuthFailure) {
      // api() уже вызвал logout() сам для этого случая — здесь просто
      // показываем экран входа, не дублируем logout() второй раз.
      renderAuth();
    } else {
      // Временная проблема (сеть, cold start backend после деплоя) — токен
      // НЕ трогаем. Показываем мягкую ошибку с возможностью повторить,
      // вместо того чтобы выкинуть пользователя на экран регистрации.
      toast("Не удалось загрузить данные. Проверьте соединение и обновите страницу.", "err");
      $("app").innerHTML = `<div class="wrap" style="max-width:480px;text-align:center;margin-top:60px">
        <p style="color:var(--text-dim)">Не удалось подключиться к серверу.</p>
        <button class="btn" style="margin-top:12px" onclick="boot()">Попробовать снова</button>
      </div>`;
    }
    try{ performance.mark('first_screen_visible'); }catch(_){}
    console.log('[timing] first_screen_visible (error fallback)');
  }
  try{ performance.mark('boot_complete'); }catch(_){}
  console.log(`[timing] boot() total: ${(performance.now()-t0).toFixed(0)}ms`);
}

// GLOBALS
window.go=go;window.logout=logout;window.setTab=setTab;
window.pickLen=pickLen;window.pickOpt=pickOpt;window.pickAdv=pickAdv;
window.saveChannel=saveChannel;window.saveAdvanced=saveAdvanced;
window.showVerifyInput=showVerifyInput;window.verifyChannel=verifyChannel;window.analyzeChannel=analyzeChannel;
window.deleteChannel=deleteChannel;window.openGenPanel=openGenPanel;window.generateNow=generateNow;
window.addSource=addSource;window.delSource=delSource;
window.toggleHistory=toggleHistory;window.toggleExpand=toggleExpand;window.showPicker=showPicker;window.doSchedule=doSchedule;
window.toggleEdit=toggleEdit;window.savePost=savePost;window.publishPost=publishPost;
window.rejectPost=rejectPost;window.deletePost=deletePost;window.regenPost=regenPost;
window.testPost=testPost;window.buy=buy;window.togglePayHistory=togglePayHistory;window.deleteAccount=deleteAccount;
window.ncPickType=ncPickType;window.pickChannelType=pickChannelType;window.openTgConnect=openTgConnect;window.toggleChannelEnabled=toggleChannelEnabled;window.verifyTgUsername=verifyTgUsername;window.ncVerify=ncVerify;window.ncAnalyze=ncAnalyze;window.ncGenerate=ncGenerate;
window.ncSelect=ncSelect;window.ncP=ncP;window.ncHz=ncHz;window.ncSkipVerify=ncSkipVerify;window.ncShowVerify=ncShowVerify;
window.sendConsult=sendConsult;window.addSuggestedRule=addSuggestedRule;
window.addRule=addRule;window.deleteRule=deleteRule;window.trackGoal=trackGoal;

// Ждём загрузки DOM перед запуском
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
