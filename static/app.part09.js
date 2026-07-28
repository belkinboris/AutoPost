

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
          <div class="chan-handle">${c.tg_chat?esc(c.tg_chat):'<span style="color:var(--text-faint);font-style:italic;font-weight:400">канал будет указан после подключения</span>'}</div>
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

function renderTimer(){}