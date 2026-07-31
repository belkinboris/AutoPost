

// ── Создание второго и следующих каналов (task item 2) ────────────────
// Минимальная форма с настройками — НЕ quick start (тот только для первого
// канала, см. renderNewChannelRouter). Без полного восстановления старого
// мёртвого renderNewChannel/ncGenerate — это новый, компактный flow.
function renderNewChannelSettings(){
  trackGoal("new_channel_settings_opened");
  $("app").innerHTML=`<div class="wrap" style="max-width:560px">
    <button class="back-link" style="margin-top:12px" onclick="go('dashboard')">← Все каналы</button>
    <div class="page-head" style="margin-top:8px">
      <h1>Новый канал</h1>
      <p style="color:var(--text-dim)">Базовые настройки — остальное можно донастроить позже во вкладке «Расширенные».</p>
    </div>

    <label class="field"><span class="field-label">Название канала</span>
      <input id="ncs_title" placeholder="Например: Новости M&A" style="width:100%"></label>

    <label class="field mt"><span class="field-label">Тема канала</span>
      <textarea id="ncs_about" rows="3" placeholder="О чём канал, для кого, какой тон" style="width:100%;font-size:15px"></textarea></label>

    <label class="field mt"><span class="field-label">@username канала <span class="text-faint">(можно позже)</span></span>
      <input id="ncs_chat" placeholder="@my_channel" style="width:100%"></label>

    <label class="field mt"><span class="field-label">Как часто писать новый пост</span>
      <select id="ncs_interval" style="width:100%">
        <option value="6">Каждые 6 часов</option>
        <option value="12" selected>Каждые 12 часов</option>
        <option value="24">Раз в сутки</option>
        <option value="48">Раз в 2 суток</option>
      </select></label>

    <div class="card mt">
      <div class="toggle-row">
        <div class="toggle-info"><b>Публиковать без проверки</b><small>Если включено — новые посты выходят в канал сами, по расписанию. Если выключено — пост ждёт вашего решения в очереди и сам не публикуется. Подключите уведомления в Телеграм: посты придут туда с кнопками «Опубликовать», «Отклонить», «Редактировать», и на решение будет ${App.cfg?.soft_control_minutes||30} мин — не ответите, опубликуем сами.</small></div>
        <label class="switch"><input type="checkbox" id="ncs_auto"><span class="slider"></span></label>
      </div>
    </div>

    <button class="btn" style="width:100%;justify-content:center;margin-top:16px;padding:14px"
      onclick="ncsCreate()" id="ncs_btn">Создать канал</button>
    <!-- То же самое сказано под заголовком экрана, но там человек читает его
         ДО формы -- когда ещё не понял, что настроек мало. Владелец 28.07
         попросил сказать это прямо: короткий набор без пояснения читается как
         «всё, что есть». Здесь строка стоит в момент решения, и называет
         конкретные вещи, а не отсылает к вкладке. -->
    <div class="hint" style="text-align:center;margin-top:10px">
      Это только начало: стиль, источники, расписание и правила можно будет
      настроить подробнее уже в самом канале.
    </div>
  </div>`;
  setTimeout(()=>{const el=$("ncs_title");if(el) el.focus();},100);
}

let _ncsCreateInFlight=false;

async function ncsCreate(){
  if(!requireAuth()) return;
  if(_ncsCreateInFlight) return;
  const title=($("ncs_title").value||"").trim();
  const about=($("ncs_about").value||"").trim();
  if(!title) return toast("Укажите название канала","err");
  if(!about) return toast("Опишите тему канала","err");

  const tgChat=($("ncs_chat").value||"").trim();
  const intervalHours=parseFloat($("ncs_interval").value||"12");
  const autoPublish=!!$("ncs_auto").checked;

  // Topic validation (та же защита что и в quick start, не дублируем логику —
  // переиспользуем существующий /validate-topic эндпоинт).
  _ncsCreateInFlight=true;
  const btn=$("ncs_btn");
  btn.innerHTML='<span class="spinner"></span> Проверяю тему…';btn.disabled=true;
  try{
    const validation=await api("POST","/validate-topic",{topic:about});
    if(!validation.ok){
      toast(validation.message||"Не понял тему. Напишите проще.","err");
      btn.innerHTML="Создать канал";btn.disabled=false;
      _ncsCreateInFlight=false;
      return;
    }
  }catch(e){
    toast("Не удалось проверить тему. Попробуйте переформулировать.","err");
    btn.innerHTML="Создать канал";btn.disabled=false;
    _ncsCreateInFlight=false;
    return;
  }

  btn.innerHTML='<span class="spinner"></span> Создаю канал…';
  try{
    const chan=await api("POST","/channels",{
      title, about,
      tg_chat: tgChat,
      interval_hours: intervalHours,
      auto_publish: autoPublish,
    });
    trackGoal("new_channel_settings_created",{channel_id:chan.id});
    toast("Канал создан ✓","ok");
    go("channel",chan.id);
  }catch(e){
    toast(e&&e.message?e.message:"Ошибка запроса","err");
    btn.innerHTML="Создать канал";btn.disabled=false;
  }finally{
    _ncsCreateInFlight=false;
  }
}

let _qsGenerateInFlight = false;

async function qsGenerate(){
  if(!requireAuth()) return;
  // КРИТИЧНО (P0 fix): защита от двойного клика через явный флаг, не только
  // через btn.disabled. disabled выставляется синхронно в начале функции,
  // но между двумя очень быстрыми кликами браузер может не успеть
  // перерисовать DOM-состояние кнопки до второго клика — флаг in-memory
  // гарантированно блокирует повторный вызов независимо от рендера.
  if (_qsGenerateInFlight) {
    toast("Пост уже генерируется, подождите несколько секунд.", "err");
    return;
  }
  const about=($("qs_about").value||"").trim();
  if(!about) return toast("Опишите тему","err");
  _qsGenerateInFlight = true;
  try{
    await _qsGenerateImpl(about);
  } finally {
    _qsGenerateInFlight = false;
  }
}