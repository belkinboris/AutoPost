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

// Свой диалог подтверждения вместо window.confirm(). Причина не только
// косметика: у нативного confirm() кнопки "OK"/"Cancel" всегда на английском
// и их нельзя переименовать — для решений о деньгах (смена тарифа, возврат)
// владелец 31.07 попросил по-русски и в стиле приложения. extra — необязательный
// HTML-блок под текстом (например ссылка «Изменить способ оплаты»).
function customConfirm(title, message, {confirmLabel="Подтвердить", cancelLabel="Отмена", extra=""}={}){
  return new Promise(resolve=>{
    const wrap=document.createElement("div");
    wrap.style.cssText="position:fixed;inset:0;background:rgba(23,27,32,.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px";
    wrap.innerHTML=`<div class="card" style="max-width:380px;width:100%;padding:20px;margin:0">
      <div style="font-size:16px;font-weight:600;margin-bottom:8px">${esc(title)}</div>
      <div style="font-size:14px;color:var(--text-dim);line-height:1.6;white-space:pre-line">${esc(message)}</div>
      ${extra}
      <div style="display:flex;gap:8px;margin-top:18px">
        <button class="btn-outline btn-sm" id="cc_cancel" style="flex:1;justify-content:center">${esc(cancelLabel)}</button>
        <button class="btn btn-sm" id="cc_ok" style="flex:1;justify-content:center">${esc(confirmLabel)}</button>
      </div>
    </div>`;
    document.body.appendChild(wrap);
    const done=result=>{ wrap.remove(); resolve(result); };
    wrap.querySelector("#cc_cancel").onclick=()=>done(false);
    wrap.querySelector("#cc_ok").onclick=()=>done(true);
    wrap.onclick=e=>{ if(e.target===wrap) done(false); };
  });
}

function trackGoal(goal, params={}){
  try{
    if(window.ym && window.YM_COUNTER_ID){
      window.ym(window.YM_COUNTER_ID,"reachGoal",goal,params);
    }
  }catch(_){}
}

// Отправляет цель "payment_success" в Метрику по каждому оплаченному платежу,
// который ещё не отправляли (дедуп через localStorage — платёж не должен
// засчитаться дважды). Найдено владельцем 31.07: реально это раньше вызывалось
// ТОЛЬКО из раскрытия "Истории платежей" в настройках (см. renderBilling) —
// случайное действие, которое почти никто не делает сразу после оплаты. Из-за
// этого Яндекс.Директ не видел почти ни одной покупки: цель физически не
// уходила в Метрику вовремя, хотя сама оплата проходила нормально. Теперь
// вызывается сразу при возврате со страницы оплаты (см. boot()), а вызов из
// истории платежей остаётся подстраховкой на случай, если платёж успел
// подтвердиться в ЮKassa только уже после возврата пользователя.
async function _reportPaidPayments(ps){
  try{
    const list = ps || await api("GET","/payments");
    list.forEach(p=>{
      if(p.status==="paid"){
        const key="ym_paid_"+(p.id||p.label||p.created_at);
        if(!localStorage.getItem(key)){
          trackGoal("payment_success",{package_id:p.package_id||"",tokens:p.tokens||0,rub:p.rub||0});
          localStorage.setItem(key,"1");
        }
      }
    });
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

// Пикеры даты/времени (showPicker/doSchedule, genQueuePost) используют
// <input type="datetime-local"> -- браузер трактует его value как ЛОКАЛЬНОЕ
// время без часового пояса. Раньше сюда клали/читали `.toISOString()`
// (UTC) напрямую -- поле показывало и отправляло время со сдвигом на
// часовой пояс пользователя (например, у пользователя в Москве вместо
// 22:30 по Москве уходило 22:30 UTC = 01:30 следующего дня по Москве).
// Найдено владельцем 02.08.
function _toLocalDatetimeInputValue(date){
  const pad=n=>String(n).padStart(2,"0");
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function _localDatetimeInputToUTCISOString(value){
  // value -- "YYYY-MM-DDTHH:mm" без таймзоны; new Date(...) в браузере
  // парсит такую строку как ЛОКАЛЬНОЕ время -- ровно то, что ввёл человек.
  return new Date(value).toISOString();
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