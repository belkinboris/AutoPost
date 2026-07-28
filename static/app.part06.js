

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