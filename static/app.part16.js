

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
        // Настоящая цель для Яндекс.Директ/Метрики -- здесь, а не в
        // раскрытии "Истории платежей" (см. _reportPaidPayments). Два
        // вызова: сразу и через 5с -- вебхук ЮKassa может подтвердить
        // платёж на пару секунд позже, чем браузер вернётся с редиректом.
        _reportPaidPayments();
        setTimeout(_reportPaidPayments, 5000);
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
