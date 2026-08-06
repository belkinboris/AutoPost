

// BILLING
async function renderBilling(){
  await refreshUser();
  logProductEvent("pricing_viewed");
  try{
    const r=await api("GET","/subscription");
    App._subscription=r.subscription; App._paymentMethod=r.payment_method||null;
  }catch(_){ App._subscription=null; App._paymentMethod=null; }
  // Карточки тарифов строятся ИЗ ОТВЕТА СЕРВЕРА, а не из своей таблицы.
  // Аудит 05.08: здесь была захардкоженная копия цен -- четвёртая по счёту
  // (config._DEFAULT_PACKAGES, config.PLANS, лендинг, здесь). Цены менялись
  // уже трижды, и в проде пакеты можно переопределить через окружение
  // (TOKEN_PACKAGES): списывалось бы по одной цене, а на экране стояла бы
  // другая. Сервер (/api/config) -- единственный источник: он же отдаёт
  // channels, popular и стоимость поста для диапазона.
  // post_tokens_min -- простой пост (20k), post_tokens_max -- сложный (40k).
  // Деление через Math.max/Math.min ниже, а не напрямую: даже если значения
  // в конфиге когда-нибудь перепутают местами, диапазон останется верным.
  const tokMin=App.cfg?.post_tokens_min||20000;
  const tokMax=App.cfg?.post_tokens_max||40000;
  const plans=(App.cfg?.packages||[]).map(p=>({
    id:p.id, name:p.title, price:p.rub, regular:p.rub_regular||0,
    channels:p.channels??0, tokens:p.tokens, popular:!!p.popular,
    postsMin:Math.floor((p.tokens||0)/Math.max(tokMin,tokMax)),
    postsMax:Math.floor((p.tokens||0)/Math.min(tokMin,tokMax)),
  }));
  if(!plans.length){
    // Конфиг не догрузился -- честная ошибка вместо пустого экрана тарифов.
    $("app").innerHTML=topbar("dashboard","назад")+`<div class="wrap"><div class="card" style="text-align:center;padding:24px">
      <p style="color:var(--text-dim)">Не удалось загрузить тарифы. Обновите страницу — если не поможет, напишите нам.</p>
    </div></div>`;
    return;
  }
  const sub=App._subscription||null;
  // Найдено владельцем 31.07: у кого уже есть тариф, тому не нужны во весь
  // экран четыре карточки "Старт/Про/Бизнес/Агентство" -- нужен только свой
  // тариф и способ его сменить. currentPlanId сначала берём из активной
  // подписки (авторитетнее), иначе из App.user.plan_title (одноразовая
  // оплата без рекуррента -- Subscription-строки нет, но тариф уже куплен).
  let currentPlanId=null;
  if(sub && sub.package_id) currentPlanId=sub.package_id;
  else if(App.user?.plan_title){
    const match=plans.find(p=>p.name===App.user.plan_title);
    if(match) currentPlanId=match.id;
  }
  const hasPlan=!!currentPlanId;
  const currentPlan=plans.find(p=>p.id===currentPlanId)||null;
  // Решение владельца 31.07: даунгрейд запрещён полностью (кто хочет тариф
  // проще -- отменяет подписку, это уже есть выше). Апгрейд стоит дешевле
  // полной цены на долю неизрасходованного остатка ТЕКУЩЕГО тарифа --
  // ровно та же формула, что и на сервере (см. /api/subscription/upgrade в
  // main.py): здесь только превью для красивой цены на кнопке, реальную
  // сумму сервер всё равно считает заново сам, клиенту в этом не доверяет.
  const currentPriceRub=sub?(sub.rub||0):(currentPlan?.price||0);
  const currentTokens=currentPlan?.tokens||0;
  const hasCard=!!App._paymentMethod;
  $("app").innerHTML=topbar("dashboard","назад")+`<div class="wrap">
    <div class="page-head"><h1>Тарифы</h1>
      <p>Осталось <b>${Math.floor((App.user?.token_balance||0)/Math.max(tokMin,tokMax))}–${Math.floor((App.user?.token_balance||0)/Math.min(tokMin,tokMax))}</b> постов.<br>
      <span style="font-size:13px;color:var(--text-faint)">Диапазон зависит от сложности: пост с поиском свежих новостей расходует больше, простой — меньше.</span></p></div>
    ${(!App.cfg?.yookassa_enabled&&!App.cfg?.yoomoney_enabled)?`<div class="card" style="border-color:var(--accent);background:var(--accent-soft);margin-bottom:16px">
      <p style="color:var(--accent-dark)">Приём платежей настраивается.</p></div>`:""}
    ${_subscriptionCard(sub)}
    ${plans.some(p=>p.regular)?`<div class="promo-bar">
      <b>Цены на время запуска.</b> Сервис ещё развивается — пока он в раннем доступе, тарифы держим ниже
      обычных.${App.cfg?.subscription_enabled?" Цена, по которой вы подписались, за вами сохранится.":""}
    </div>`:""}
    ${hasPlan?`<div style="text-align:center;margin-bottom:16px">
      <button class="btn-outline btn-sm" onclick="togglePlansGrid()" id="plans_toggle_btn">Показать все тарифы</button>
    </div>`:""}
    <div id="plansGrid" class="grid grid-2 ${hasPlan?"hidden":""}" style="margin-bottom:16px">
      ${plans.map(p=>{
        const isCurrent=p.id===currentPlanId;
        const isDowngrade=!!sub && !isCurrent && p.price<currentPriceRub;
        const isUpgrade=!!sub && !isCurrent && p.price>currentPriceRub;
        let upgradePrice=null, creditRub=0;
        if(isUpgrade){
          const unusedFraction=currentTokens>0?Math.min(1,(App.user?.token_balance||0)/currentTokens):0;
          creditRub=Math.round(currentPriceRub*unusedFraction);
          upgradePrice=Math.max(0,p.price-creditRub);
        }
        const priceHtml=(isUpgrade && creditRub>0)
          ?`<div class="p-regular" style="text-decoration:line-through">${_rub(p.price)} ₽</div>
            <div class="p-price" style="font-size:24px">${_rub(upgradePrice)} ₽</div>`
          :`${p.regular?`<div class="p-regular">потом ${_rub(p.regular)} ₽</div>`:""}
            <div class="p-price" style="font-size:24px">${_rub(p.price)}\u00A0₽${App.cfg?.subscription_enabled?"/мес":""}</div>`;
        let actionHtml;
        if(isCurrent){
          // Кнопка возврата — только на карточке текущего тарифа, только
          // пока условия из static/legal/refund.html реально выполняются
          // (см. /api/subscription: refund_eligible/refund_reason). Владелец
          // 31.07: основной способ возврата — эта кнопка, а не письмо,
          // которое можно не увидеть вовремя.
          const refundHtml=sub
            ?(sub.refund_eligible
              ?`<button class="btn-outline btn-sm" style="width:100%;justify-content:center;margin-top:8px;color:var(--red,#c0392b);border-color:var(--red,#c0392b)" onclick="refundSubscription()">Вернуть ${_rub(sub.refund_amount_rub||0)} ₽ и отменить</button>`
              :`<div class="hint" style="text-align:center;margin-top:8px">Возврат недоступен: ${esc(sub.refund_reason||"")}</div>`)
            :"";
          actionHtml=`<div class="hint" style="text-align:center;margin-top:8px">Уже подключён</div>${refundHtml}`;
        } else if(isDowngrade){
          actionHtml=`<div class="hint" style="text-align:center;margin-top:8px">Тариф ниже вашего — чтобы перейти, отмените текущую подписку выше</div>`;
        } else if(isUpgrade && !hasCard){
          actionHtml=`<div class="hint" style="text-align:center;margin-top:8px">Нужен сохранённый способ оплаты — отмените подписку и оформите новый тариф заново</div>`;
        } else if(isUpgrade){
          actionHtml=`<button class="btn" style="width:100%;justify-content:center;margin-top:8px" onclick="upgradePlan('${p.id}')">Перейти на «${esc(p.name)}»</button>`
            +(creditRub>0?`<div class="hint" style="text-align:center;margin-top:6px">Цена снижена — учтён неизрасходованный остаток тарифа «${esc(currentPlan?.name||"")}»</div>`:"");
        } else {
          actionHtml=`<button class="btn" style="width:100%;justify-content:center;margin-top:8px" onclick="buy('${p.id}')">Выбрать</button>`;
        }
        return `<div class="price-card" style="position:relative;${(p.popular&&!isCurrent)?"border-color:var(--accent)":""}${isCurrent?"border-color:var(--green,#2e7d32)":""}">
        ${isCurrent?`<div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:var(--green,#2e7d32);color:#fff;font-size:11px;font-weight:600;padding:2px 12px;border-radius:99px;white-space:nowrap">Ваш тариф</div>`
          :p.popular?`<div style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;font-size:11px;font-weight:600;padding:2px 12px;border-radius:99px;white-space:nowrap">Популярный</div>`:""}
        <div class="p-name">${p.name}</div>
        ${priceHtml}
        <div class="p-tokens" style="line-height:1.8">
          📺 ${p.channels===0?"Без лимита каналов":`${p.channels} ${_plural(p.channels,"канал","канала","каналов")}`}<br>
          ✦ ${p.postsMin}–${p.postsMax} постов${App.cfg?.subscription_enabled?"/мес":""}</div>
        ${actionHtml}
      </div>`;
      }).join("")}
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">🎁 Реферальная программа</div>
      <p style="font-size:14px;color:var(--text-dim);margin-bottom:12px">Пригласите друга — каждому из вас придёт примерно 6–10 бесплатных постов (200 000 токенов).</p>
      <div id="ref_block" class="text-faint">Загрузка…</div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <button onclick="togglePayHistory()" id="pay_hist_btn"
        style="background:none;border:none;cursor:pointer;font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px;width:100%;padding:0;min-height:44px">
        📋 История платежей <span id="pay_hist_arrow" style="font-size:12px;color:var(--text-faint)">▶</span>
      </button>
      <div id="payList" class="hidden text-faint"></div>
    </div>
    ${_paymentMethodBlock()}
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
        <div style="font-weight:600;color:var(--text);margin-bottom:4px">Отправьте другу — эти шаги для него, не для вас:</div>
        <div>1. Откройте <a href="https://t.me/maintrpost_bot" target="_blank" style="color:var(--accent)">бота в Telegram</a> или сайт <a href="https://projectautopost.ru" target="_blank" style="color:var(--accent)">projectautopost.ru</a></div>
        <div>2. Зарегистрируйтесь</div>
        <div>3. Введите реферальный код: <b>${esc(code)}</b></div>
      </div>
      <div class="hint" style="margin-top:8px">Приглашений: <b>${me.referrals_count||0}</b></div>`;
  }catch(_){}
  // История платежей загружается лениво при раскрытии
  window._loadPayHistory = async function(){
    try{
      const ps=await api("GET","/payments");
      // Подстраховка: основной момент отправки цели "payment_success" --
      // возврат со страницы оплаты (см. boot() в app.part16.js), этот вызов
      // только досылает то, что могло не подтвердиться вовремя. Дедуп через
      // localStorage внутри _reportPaidPayments не даст засчитать платёж дважды.
      _reportPaidPayments(ps);
      // В истории платежей не было главного — суммы. Строка выглядела как
      // «27.07.2026, 14:05 · 600 000 ток.»: внутренняя единица учёта на первом
      // месте и ни рубля. Человек заходит сюда посмотреть, сколько и за что
      // заплатил, поэтому деньги вынесены вперёд, а токены остались справочно.
      // Прежний класс .src-url не подошёл: у него white-space:nowrap, и вторая
      // строка в него не помещалась.
      $("payList").innerHTML=ps.length
        ?ps.map(p=>{
            const pkg=(App.cfg?.packages||[]).find(x=>x.id===p.package_id);
            const when=new Date(p.created_at+"Z").toLocaleString("ru-RU");
            return `<div class="src-row" style="align-items:flex-start">
              <div style="min-width:0">
                <div style="font-size:13px;color:var(--text);font-weight:600">${_rub(p.rub||0)}\u00A0₽${pkg?` · ${esc(pkg.title)}`:""}</div>
                <div style="font-size:12px;color:var(--text-faint);margin-top:2px">${when} · ${fmt(p.tokens)} токенов</div>
              </div>
              <span class="chip ${p.status==="paid"?"chip-green":"chip-orange"}">${p.status==="paid"?"оплачено":"ожидает оплаты"}</span>
            </div>`;
          }).join("")
        :`<p style="font-size:13px;color:var(--text-faint)">Платежей пока не было.</p>`;
    }catch(_){}
  };
}

function togglePlansGrid(){
  const grid=$("plansGrid"),btn=$("plans_toggle_btn");
  if(!grid) return;
  const hidden=grid.classList.contains("hidden");
  grid.classList.toggle("hidden",!hidden);
  if(btn) btn.textContent=hidden?"Скрыть тарифы":"Показать все тарифы";
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

let _refundInFlight=false;

// Самообслуживаемый возврат (POST /subscription/refund) вместо письма на
// почту — владелец 31.07 может не увидеть письмо вовремя. Условия (3 дня,
// токены не тронуты) проверяет сервер заново, кнопка вообще не показывается,
// если /api/subscription уже сказал refund_eligible=false.
async function refundSubscription(){
  if(_refundInFlight) return;
  const sub=App._subscription;
  if(!sub) return;
  const ok=await customConfirm(
    "Вернуть деньги и отменить подписку?",
    `Вернём ${_rub(sub.refund_amount_rub||0)} ₽ на карту, с которой была оплата, и сразу отменим `+
    `подписку — отменить этот возврат будет нельзя.`,
    {confirmLabel:"Вернуть и отменить", cancelLabel:"Передумал(а)"}
  );
  if(!ok) return;
  _refundInFlight=true;
  logProductEvent("subscription_refund_started");
  try{
    const r=await api("POST","/subscription/refund");
    logProductEvent("subscription_refunded");
    toast(`Возврат оформлен — ${_rub(r.refunded_rub)} ₽`,"ok");
    renderBilling();
  }catch(e){
    logProductEvent("subscription_refund_failed");
    toast(e&&e.message?e.message:"Не удалось оформить возврат","err");
  }finally{
    _refundInFlight=false;
  }
}

async function buy(pid){
  logProductEvent("payment_cta_clicked", pid);
  // Регулярное списание обязано быть раскрыто ДО оплаты, явно и своими
  // словами -- человек должен понимать, что подписывается на повторяющийся
  // платёж, а не платит один раз. Отмена тут же названа, чтобы это не
  // выглядело ловушкой.
  //
  // АУДИТ 05.08: здесь стоял голый window.confirm() -- единственное место в
  // всей коммерческой части, где не используется customConfirm (см. его
  // же обоснование в app.part01.js: нативные кнопки OK/Cancel всегда на
  // английском и не переименовываются). upgradePlan() и refundSubscription()
  // уже используют customConfirm для куда менее рискованных решений
  // (смена/возврат тарифа у уже платящего человека) -- а самое первое,
  // самое решающее место воронки, где человек ещё никому не доверяет,
  // встречало его системным попапом с "OK"/"Cancel" на чужом языке поверх
  // мобильного экрана. Плюс: в WebView Телеграма (а сюда заходят и оттуда,
  // см. openLink ниже) нативные диалоги на некоторых клиентах ведут себя
  // ненадёжно. Заменено на тот же customConfirm, что и везде в биллинге.
  const plan=(App.cfg?.packages||[]).find(p=>p.id===pid);
  const days=App.cfg?.subscription_period_days||30;
  if(plan && App.cfg?.subscription_enabled){
    const ok=await customConfirm(
      `Тариф «${plan.title}» — ${plan.rub} ₽ каждые ${days} дней`,
      `Первый платёж спишется сейчас, дальше — автоматически раз в ${days} дней, `+
      `пока вы не отмените подписку.\n\n`+
      `Отменить и отвязать карту можно в любой момент на этой же странице.`,
      {confirmLabel:"Оплатить", cancelLabel:"Передумал(а)"}
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

let _upgradeInFlight=false;

// Смена тарифа на более дорогой -- списывается СРАЗУ по уже сохранённой
// карте (см. POST /subscription/upgrade), без редиректа на ЮKassa. Сумму
// на кнопке считаем здесь только для превью -- сколько реально спишут,
// сервер решает заново сам по свежим данным, клиентскому числу не доверяет.
async function upgradePlan(pid){
  if(_upgradeInFlight) return;
  const plan=(App.cfg?.packages||[]).find(p=>p.id===pid);
  const title=plan?.title||pid;
  // Свой диалог вместо window.confirm() — у нативного кнопки только на
  // английском (владелец 31.07). Выбор способа оплаты именно для ЭТОЙ
  // доплаты (например через СБП вместо привязанной карты) — отдельная,
  // более сложная задача (нужен redirect-платёж на неполную сумму), пока не
  // делали — вместо этого прямо говорим, каким способом спишется, и что
  // делать, если хочется другим.
  const ok=await customConfirm(
    `Перейти на тариф «${title}»?`,
    `Спишем доплату сейчас по сохранённой карте (неизрасходованный остаток `+
    `текущего тарифа уже учтён в цене). Дальше подписка продлевается по `+
    `полной цене «${title}».\n\n`+
    `Хотите заплатить другим способом — сначала отмените подписку ниже и `+
    `оформите тариф заново.`
  );
  if(!ok) return;
  _upgradeInFlight=true;
  logProductEvent("subscription_upgrade_started", pid);
  try{
    const r=await api("POST","/subscription/upgrade",{package_id:pid});
    trackGoal("subscription_upgraded",{package_id:pid,charged_rub:r.charged_rub,credit_rub:r.credit_rub});
    toast(`Тариф изменён — списано ${_rub(r.charged_rub)} ₽`,"ok");
    renderBilling();
  }catch(e){
    logProductEvent("subscription_upgrade_failed", pid);
    toast(e&&e.message?e.message:"Не удалось сменить тариф","err");
  }finally{
    _upgradeInFlight=false;
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
  if(!username) return toast("Введите @username","err");
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
  const dt=$("dt_"+id);if(dt) dt.value=_toLocalDatetimeInputValue(new Date(Date.now()+3600000));
}
async function doSchedule(id){
  const dt=$("dt_"+id);if(!dt||!dt.value) return toast("Выберите дату","err");
  try{
    await api("POST","/posts/"+id+"/schedule",{scheduled_at:_localDatetimeInputToUTCISOString(dt.value)});
    toast("Запланировано ✓","ok");renderQueue();
  }catch(e){toast(e&&e.message?e.message:"Ошибка","err");}
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