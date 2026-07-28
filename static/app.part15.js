
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