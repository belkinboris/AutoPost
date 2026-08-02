

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
  if(App._chan.queue_depth) payload.queue_depth=App._chan.queue_depth;
  if(chatChanged) payload.tg_chat=newChat;
  const notif={
    notify_published:$("sw_n2")?$("sw_n2").checked:false,
    notify_approval_pending:$("sw_n4")?$("sw_n4").checked:false,
    notify_low_tokens:$("sw_n3")?$("sw_n3").checked:true,
  };
  try{
    const updated=await api("PATCH","/channels/"+App._chan.id,payload);
    await api("PATCH","/me",notif);
    // Обновляем локально без перерендера — чтобы тумблеры не сбросились.
    // queue_depth берём из ответа, а не из payload -- бэкенд мог зажать
    // значение в потолок тарифа (см. patch_channel), и степпер должен
    // показать то, что реально сохранилось, а не то, что нажали.
    if(updated) App._chan.queue_depth=updated.queue_depth;
    if(App.user){
      App.user.notify_published=notif.notify_published;
      App.user.notify_approval_pending=notif.notify_approval_pending;
      App.user.notify_low_tokens=notif.notify_low_tokens;
    }
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