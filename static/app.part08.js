

function renderNewChannel(){
  _ncVoice="author";_ncFormat="story";_ncEmoji="minimal";_ncCta=false;_ncCtaText="";_ncHz=12;_ncStyleProfile="";
  $("app").innerHTML=topbar("dashboard","все каналы")+`<div class="wrap" style="max-width:680px">
    <div class="page-head"><h1>Новый канал</h1>
      <p>Расскажите о канале — ИИ покажет три варианта постов на выбор.</p></div>
    <div class="card">
      <label class="field"><span class="field-label">Название (видно только вам)</span>
        <input id="nc_title" placeholder="Например: Крипта без воды" maxlength="80"></label>

      <div class="field mt"><span class="field-label">Telegram-канал</span>
        <div id="nc_verify_block">
          <div class="row" style="gap:8px">
            <input id="nc_chat" placeholder="@my_channel или https://t.me/channel" style="flex:1">
            <button class="btn-outline btn-sm" onclick="ncVerify()" id="nc_vbtn" style="white-space:nowrap">Проверить</button>
          </div>
          <div class="hint">Сначала добавь бота <b>@${esc(App.cfg?.bot_username||"…")}</b> в админы канала, потом вставь @username и нажми «Проверить».</div>
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