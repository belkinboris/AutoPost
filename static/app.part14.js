

// BILLING
async function renderBilling(){
  await refreshUser();
  logProductEvent("pricing_viewed");
  try{
    const r=await api("GET","/subscription");
    App._subscription=r.subscription; App._paymentMethod=r.payment_method||null;
  }catch(_){ App._subscription=null; App._paymentMethod=null; }
  const plans=[
    {id:"p1",name:"Старт",price:490,regular:990,channels:1,postsMin:15,postsMax:30},
    {id:"p2",name:"Про",price:990,regular:1990,channels:3,postsMin:30,postsMax:60,popular:true},
    {id:"p3",name:"Бизнес",price:2490,regular:4990,channels:10,postsMin:75,postsMax:150},
    {id:"p4",name:"Агентство",price:4990,regular:9990,channels:0,postsMin:150,postsMax:300},
  ];
  const sub=App._subscription||null;
  $("app").innerHTML=topbar("dashboard","назад")+`<div class="wrap">
    <div class="page-head"><h1>Тарифы</h1>
      <p>Осталось <b>${Math.floor((App.user?.token_balance||0)/40000)}–${Math.floor((App.user?.token_balance||0)/20000)}</b> постов.<br>
      <span style="font-size:13px;color:var(--text-faint)">Диапазон зависит от сложности: пост с поиском свежих новостей расходует больше, простой — меньше.</span></p></div>
    ${(!App.cfg?.yookassa_enabled&&!App.cfg?.yoomoney_enabled)?`<div class="card" style="border-color:var(--accent);background:var(--accent-soft);margin-bottom:16px">
      <p style="color:var(--accent-dark)">Приём платежей настраивается.</p></div>`:""}
    ${_subscriptionCard(sub)}
    ${_paymentMethodBlock()}
    ${plans.some(p=>p.regular)?`<div class="promo-bar">
      <b>Цены на время запуска.</b> Сервис ещё развивается — пока он в раннем доступе, тарифы держим ниже
      обычных.${App.cfg?.subscription_enabled?" Цена, по которой вы подписались, за вами сохранится.":""}
    </div>`:""}
    <div class="grid grid-2" style="margin-bottom:16px">
      ${plans.map(p=>`<div class="price-card" style="position:relative;${p.popular?"border-color:var(--accent)":""}">
        ${p.popular?`<div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-size:11px;font-weight:600;padding:2px 12px;border-radius:99px;white-space:nowrap">Популярный</div>`:""}
        <div class="p-name">${p.name}</div>
        ${p.regular?`<div class="p-regular">потом ${_rub(p.regular)} ₽</div>`:""}
        <div class="p-price" style="font-size:24px">${_rub(p.price)}\u00A0₽${App.cfg?.subscription_enabled?"/мес":""}</div>
        <div class="p-tokens" style="line-height:1.8">
          📺 ${p.channels===0?"Без лимита каналов":`${p.channels} ${_plural(p.channels,"канал","канала","каналов")}`}<br>
          ✦ ${p.postsMin}–${p.postsMax} постов${App.cfg?.subscription_enabled?"/мес":""}</div>
        <button class="btn" style="width:100%;justify-content:center;margin-top:8px" onclick="buy('${p.id}')">Выбрать</button>
      </div>`).join("")}
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">🎁 Реферальная программа</div>
      <p style="font-size:14px;color:var(--text-dim);margin-bottom:12px">Пригласите друга — каждому из вас придёт примерно 6–10 бесплатных постов (200 000 токенов).</p>
      <div id="ref_block" class="text-faint">Загрузка…</div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <button onclick="togglePayHistory()" id="pay_hist_btn"
        style="background:none;border:none;cursor:pointer;font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px;width:100%;padding:0 0 12px">
        📋 История платежей <span id="pay_hist_arrow" style="font-size:12px;color:var(--text-faint)">▶</span>
      </button>
      <div id="payList" class="hidden text-faint"></div>
    </div>
    <div style="text-align:center;margin-top:16px;padding-bottom:8px">
      <button class="btn-danger btn-sm" onclick="deleteAccount()" style="font-size:12px;opacity:.6">Удалить аккаунт</button>
    </div></div>`;
  try{
    const me=await api("GET","/me");const code=me.ref_code||"";
    $("ref_block").innerHTML=`
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
        <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:600;letter-spacing:.1em;background:var(--surface2);border:1px solid var(--border-soft);border-radius:10px;padding:10px 18px;flex:1;text-align:center">${esc(code)}</div>
        <button class="btn-outline btn-sm" onclick="navigator.clipboard.writeText('${esc(code)}').then(()=>toast('Скопировано','ok'))">Копировать</button>
      </div>
      <div style="font-size:13px;color:var(--text-dim);background:var(--surface2);border-radius:10px;padding:12px 14px;line-height:1.7">
        1. Открой <a href="https://t.me/maintrpost_bot" target="_blank" style="color:var(--accent)">@maintrpost_bot</a><br>
        2. «Открыть АвтоПост» → Зарегистрироваться<br>
        3. Ввести реферальный код: <b>${esc(code)}</b>
      </div>
      <div class="hint" style="margin-top:8px">Приглашений: <b>${me.referrals_count||0}</b></div>`;
  }catch(_){}
  // История платежей загружается лениво при раскрытии
  window._loadPayHistory = async function(){
    try{
      const ps=await api("GET","/payments");
      ps.forEach(p=>{
        if(p.status==="paid"){
          const key="ym_paid_"+(p.id||p.label||p.created_at);
          if(!localStorage.getItem(key)){
            trackGoal("payment_success",{package_id:p.package_id||"",tokens:p.tokens||0,rub:p.rub||0});
            localStorage.setItem(key,"1");
          }
        }
      });
      $("payList").innerHTML=ps.length
        ?ps.map(p=>`<div class="src-row">
            <span class="src-url">${new Date(p.created_at+"Z").toLocaleString("ru-RU")} · ${fmt(p.tokens)} ток.</span>
            <span class="chip ${p.status==="paid"?"chip-green":"chip-orange"}">${p.status==="paid"?"оплачено":"ожидает"}</span>
          </div>`).join("")
        :`<p style="font-size:13px;color:var(--text-faint)">Платежей пока не было.</p>`;
    }catch(_){}
  };
}

function togglePayHistory(){
  const list=$("payList"),arrow=$("pay_hist_arrow");
  if(!list) return;
  const hidden=list.classList.contains("hidden");
  list.classList.toggle("hidden",!hidden);
  if(arrow) arrow.textContent=hidden?"▼":"▶";
  if(hidden && window._loadPayHistory) window._loadPayHistory();
}

// 1290 -> "1 290" (неразрывный пробел, чтобы цена не переносилась)
function _rub(n){ return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " "); }

// Русское склонение по числу: 1 канал / 2 канала / 10 каналов.
function _plural(n, one, few, many){
  const a=Math.abs(n)%100, b=a%10;
  if(a>10&&a<20) return many;
  if(b>1&&b<5) return few;
  if(b===1) return one;
  return many;
}

// Управление сохранённым способом оплаты. Блок показывается ВСЕГДА, даже
// когда карта не привязана: ЮKassa подключает рекуррентные платежи только
// тем магазинам, где покупатель видит и может сам выполнить сценарий отвязки
// карты, не обращаясь в поддержку. Если показывать блок лишь при активной
// подписке, сценарий невозможно ни увидеть, ни продемонстрировать.
function _paymentMethodBlock(){
  const pm=App._paymentMethod||null;
  const has=!!pm;
  return `<div class="card" style="margin-bottom:16px">
    <div class="card-title">Способ оплаты</div>
    <div class="pm-row">
      <label class="pm-item">
        <input type="checkbox" id="pm_confirm" ${has?"":"disabled"} onchange="pmToggle()">
        <span>
          <b>${has?esc(pm.title):"Сохранённых карт нет"}</b>
          <small>${has
            ? "Привязана для автоматического продления подписки"
            : "Карта появится здесь после оплаты с сохранением способа оплаты"}</small>
        </span>
      </label>
      <button class="btn-danger btn-sm" id="pm_delete" disabled onclick="deletePaymentMethod()">Удалить карту</button>
    </div>
    <div class="hint" style="margin-top:10px">
      Отметьте карту и нажмите «Удалить карту» — мы удалим сохранённый способ оплаты
      и прекратим автоматические списания. Обращаться в поддержку не нужно.
    </div>
  </div>`;
}

// Кнопка удаления активна только после явной отметки чек-бокса -- защита от
// случайного нажатия на необратимое действие.
function pmToggle(){
  const c=$("pm_confirm"), b=$("pm_delete");
  if(c&&b) b.disabled=!c.checked;
}

async function deletePaymentMethod(){
  const c=$("pm_confirm");
  if(!c||!c.checked) return;
  if(!confirm("Удалить сохранённую карту?\n\nАвтоматические списания прекратятся. "+
              "Оплаченный период и уже начисленные посты останутся при вас.")) return;
  const b=$("pm_delete");
  if(b){ b.disabled=true; b.innerHTML='<span class="spinner"></span>'; }
  try{
    await api("DELETE","/subscription");
    logProductEvent("payment_method_deleted");
    toast("Карта удалена, списаний больше не будет","ok");
    renderBilling();
  }catch(e){
    toast(e&&e.message?e.message:"Не удалось удалить карту","err");
    if(b){ b.disabled=false; b.innerHTML="Удалить карту"; }
  }
}

function _subscriptionCard(sub){
  const days=App.cfg?.subscription_period_days||30;
  if(!sub){
    // Пока рекуррент не согласован с ЮKassa, обещать автосписание нельзя --
    // его не будет. Тогда это честная разовая оплата пакета.
    if(!App.cfg?.subscription_enabled){
      return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:16px;padding:14px 16px">
        <div style="font-size:13px;color:var(--text-dim);line-height:1.6">
          Оплата разовая: списываем один раз и сразу начисляем посты. Ничего не спишется автоматически —
          когда посты закончатся, просто оплатите снова.
        </div></div>`;
    }
    // Честно предупреждаем о характере платежа до того, как человек нажмёт
    // «Выбрать», а не только в момент подтверждения.
    return `<div class="card" style="background:var(--surface2);border:none;margin-bottom:16px;padding:14px 16px">
      <div style="font-size:13px;color:var(--text-dim);line-height:1.6">
        Тарифы — это подписка: плата списывается автоматически раз в ${days} дней, пока вы её не отмените.
        Отменить подписку и отвязать карту можно в любой момент здесь же, деньги за оплаченный период не сгорают.
      </div></div>`;
  }
  const when=sub.next_charge_at
    ? new Date(sub.next_charge_at).toLocaleDateString("ru-RU",{day:"numeric",month:"long",year:"numeric"})
    : "—";
  if(sub.status==="suspended"){
    return `<div class="card" style="background:var(--accent-soft);border:none;margin-bottom:16px;padding:14px 16px">
      <div style="font-size:13px;color:var(--accent-dark);font-weight:600">Подписка «${esc(sub.title)}» приостановлена</div>
      <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
        Списать оплату не получилось. Автосписания остановлены — выберите тариф ниже, чтобы возобновить подписку.
      </div>
      <button class="btn-ghost btn-sm" style="margin-top:8px;padding:4px 0;color:var(--accent-dark)" onclick="cancelSubscription()">Отменить подписку и отвязать карту</button>
    </div>`;
  }
  return `<div class="card" style="background:var(--green-bg,var(--surface2));border:none;margin-bottom:16px;padding:14px 16px">
    <div style="font-size:13px;font-weight:600">Подписка «${esc(sub.title)}» активна</div>
    <div style="font-size:13px;color:var(--text-dim);margin-top:2px">
      Следующее списание ${sub.rub?`${sub.rub} ₽ `:""}— ${when} Дальше каждые ${days} дней, пока не отмените.
    </div>
    <button class="btn-ghost btn-sm" style="margin-top:8px;padding:4px 0;color:var(--red,#c0392b)" onclick="cancelSubscription()">Отменить подписку и отвязать карту</button>
  </div>`;
}

async function cancelSubscription(){
  if(!confirm(
    "Отменить подписку и отвязать карту?\n\n"+
    "Автосписания прекратятся, сохранённый способ оплаты будет удалён — "+
    "списать по нему больше не сможем.\n\n"+
    "Оплаченный период и уже начисленные посты останутся при вас."
  )) return;
  try{
    await api("DELETE","/subscription");
    logProductEvent("subscription_cancelled");
    toast("Подписка отменена","ok");
    renderBilling();
  }catch(e){
    toast(e&&e.message?e.message:"Не удалось отменить подписку","err");
  }
}

async function buy(pid){
  logProductEvent("payment_cta_clicked", pid);
  // Регулярное списание обязано быть раскрыто ДО оплаты, явно и своими
  // словами -- человек должен понимать, что подписывается на повторяющийся
  // платёж, а не платит один раз. Отмена тут же названа, чтобы это не
  // выглядело ловушкой.
  const plan=(App.cfg?.packages||[]).find(p=>p.id===pid);
  const days=App.cfg?.subscription_period_days||30;
  if(plan && App.cfg?.subscription_enabled){
    const ok=confirm(
      `Тариф «${plan.title}» — ${plan.rub} ₽ каждые ${days} дней.\n\n`+
      `Первый платёж спишется сейчас, дальше — автоматически раз в ${days} дней, `+
      `пока вы не отмените подписку.\n\n`+
      `Отменить и отвязать карту можно в любой момент на этой же странице.`
    );
    if(!ok){ logProductEvent("payment_declined_at_confirm", pid); return; }
  }
  try{
    const r = await api("POST", "/billing/buy", {package_id: pid});
    trackGoal("payment_started",{package_id:pid});
    if(!r.payment_url){
      logProductEvent("payment_failed", pid);
      toast("Не удалось получить ссылку на оплату","err");
      return;
    }
    // Telegram Mini App — используем встроенный метод
    if(window.Telegram?.WebApp?.openLink){
      window.Telegram.WebApp.openLink(r.payment_url);
    } else {
      window.location.href = r.payment_url;
    }
  } catch(e){
    logProductEvent("payment_failed", pid);
    toast(e&&e.message?e.message:"Ошибка запроса","err");
  }
}
async function deleteAccount(){
  if(!confirm("Удалить аккаунт?\n\nЭто удалит все каналы, посты и данные.")) return;
  if(prompt("Введите DELETE:")!=="DELETE") return toast("Отменено");
  try{await api("DELETE","/me");toast("Удалено","ok");logout();}catch(e){toast(e&&e.message?e.message:"Ошибка запроса","err");}
}

// COOKIE + KEYBOARD
async function verifyTgUsername(){
  const username=($("f_tg_username")||{value:""}).value.trim();
  if(!username) return toast("Введи @username","err");
  const btn=$("tg_check_btn"),msg=$("tg_check_msg");
  btn.innerHTML='<span class="spinner"></span>';btn.disabled=true;
  try{
    const r=await api("POST","/me/verify_tg",{username});
    msg.textContent=r.message;
    msg.style.color=r.ok?"var(--green)":"var(--red)";
    if(r.ok) App.user.tg_username=username;
  }catch(e){msg.textContent=e.message;msg.style.color="var(--red)";}
  btn.innerHTML="Проверить";btn.disabled=false;
}

async function toggleChannelEnabled(){
  const c=App._chan;
  const newVal=!c.enabled;
  try{
    await api("PATCH","/channels/"+c.id,{enabled:newVal});
    App._chan.enabled=newVal;
    if(newVal) App._chan.last_generated_at=new Date().toISOString(); // таймер с нуля
    const btn=$("pause_btn");
    if(btn){btn.textContent=newVal?"⏸ Пауза":"▶ Возобновить";btn.className=newVal?"btn-outline btn-sm":"btn btn-sm";}
    if(App.tab==="queue") renderQueue();
    toast(newVal?"Канал запущен — генерируем посты…":"Публикация приостановлена","ok");
  }catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}

function showPicker(id){
  const p=$("picker_"+id);if(!p) return;p.classList.remove("hidden");
  const dt=$("dt_"+id);if(dt) dt.value=new Date(Date.now()+3600000).toISOString().slice(0,16);
}
async function doSchedule(id){
  const dt=$("dt_"+id);if(!dt||!dt.value) return toast("Выберите дату","err");
  try{await api("POST","/posts/"+id+"/schedule",{scheduled_at:dt.value});toast("Запланировано ✓","ok");renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}
function toggleEdit(id){
  const ta=$("pt_"+id),pw=$("ppreview_"+id),sb=$("save_"+id);if(!ta) return;
  const hidden=ta.classList.contains("hidden");
  ta.classList.toggle("hidden",!hidden);
  if(pw) pw.classList.toggle("hidden",hidden);
  if(sb) sb.classList.toggle("hidden",!hidden);
}
async function savePost(id){
  const el=$("pt_"+id);if(!el) return;
  try{await api("PATCH","/posts/"+id,{text:el.value});toast("Сохранено ✓","ok");renderQueue();}
  catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
}