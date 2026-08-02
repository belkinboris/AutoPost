

async function testPost(){
  const btn=$("testBtn");
  // Запоминаем подпись, а не пишем её здесь заново: в разметке кнопка
  // называется «▷ Написать пост сейчас», а восстанавливалась как «▷ Создать
  // тестовый пост» -- старое название, которого нет больше нигде. После
  // первого же нажатия кнопка переименовывалась сама, и человек не понимал,
  // тестовый это пост или настоящий (он настоящий: встаёт в очередь).
  const label=btn.innerHTML;
  btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
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
  btn.innerHTML=label;btn.disabled=false;
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