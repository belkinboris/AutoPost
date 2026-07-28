

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