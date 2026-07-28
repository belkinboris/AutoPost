

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