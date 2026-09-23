/* Перевод интерфейса AI Hatshy (KZ / EN) поверх русских шаблонов макета.
   Словарь точных строк — /static/i18n.json; строки с числами и датами — правила ниже.
   Переводятся только надписи интерфейса (полное совпадение текстового узла, placeholder, title);
   пользовательские данные (названия встреч, транскрипт, поручения) не трогаются. При возврате на RU
   восстанавливаются исходные строки. */
(function () {
  const CYR = /[А-Яа-яЁё]/;
  const ORIG = new WeakMap(), ORIG_ATTR = new WeakMap();
  let lang = 'ru', DICT = null, loading = null, obs = null, busy = false;
  const MS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const MG = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const MN = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
  const M = {
    en: { s: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
          l: ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'] },
    kk: { s: ['қаң', 'ақп', 'нау', 'сәу', 'мам', 'мау', 'шіл', 'там', 'қыр', 'қаз', 'қар', 'жел'],
          l: ['қаңтар', 'ақпан', 'наурыз', 'сәуір', 'мамыр', 'маусым', 'шілде', 'тамыз', 'қыркүйек', 'қазан', 'қараша', 'желтоқсан'] }
  };
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  const RS = MS.join('|'), RG = MG.join('|'), RN = MN.join('|');
  // [регулярка, en, kk] — функции получают массив групп
  const RULES = [
    [new RegExp(`^(\\d{1,2}) (${RS})$`), g => `${M.en.s[MS.indexOf(g[2])]} ${g[1]}`, g => `${g[1]} ${M.kk.s[MS.indexOf(g[2])]}`],
    [new RegExp(`^до (\\d{1,2}) (${RS})$`), g => `by ${M.en.s[MS.indexOf(g[2])]} ${g[1]}`, g => `${g[1]} ${M.kk.s[MS.indexOf(g[2])]} дейін`],
    [new RegExp(`^(\\d{1,2}) (${RG})(?: (\\d{4}))?(?: г\\.)?$`), g => `${M.en.l[MG.indexOf(g[2])]} ${g[1]}${g[3] ? ', ' + g[3] : ''}`,
      g => `${g[3] ? g[3] + ' ж. ' : ''}${g[1]} ${M.kk.l[MG.indexOf(g[2])]}`],
    [new RegExp(`^(${RN}) (\\d{4}) · (\\d+) совещани\\S* · (\\d+) поручени\\S*$`),
      g => `${M.en.l[MN.indexOf(g[1])]} ${g[2]} · ${g[3]} meetings · ${g[4]} action items`,
      g => `${cap(M.kk.l[MN.indexOf(g[1])])} ${g[2]} · ${g[3]} кеңес · ${g[4]} тапсырма`],
    [/^Обработка (\d+)%$/, g => `Processing ${g[1]}%`, g => `Өңделуде ${g[1]}%`],
    [/^Загрузка (\d+)%…$/, g => `Uploading ${g[1]}%…`, g => `Жүктелуде ${g[1]}%…`],
    [/^через (\d+) (?:день|дня|дней)$/, g => `in ${g[1]} day${g[1] === '1' ? '' : 's'}`, g => `${g[1]} күннен кейін`],
    [/^просрочено на (\d+) (?:день|дня|дней)$/, g => `overdue by ${g[1]} day${g[1] === '1' ? '' : 's'}`, g => `${g[1]} күнге кешікті`],
    [/^(\d+) (?:участник|участника|участников)$/, g => `${g[1]} participant${g[1] === '1' ? '' : 's'}`, g => `${g[1]} қатысушы`],
    [/^(\d+) (?:подключение|подключения|подключений)$/, g => `${g[1]} connection${g[1] === '1' ? '' : 's'}`, g => `${g[1]} қосылым`],
    [/^Файл (\S+)$/, g => `${g[1]} file`, g => `${g[1]} файлы`],
    [/^Говорящий (\d+)$/, g => `Speaker ${g[1]}`, g => `Сөйлеуші ${g[1]}`],
    [/^(\d+) с речи · (из совещания|запись)$/, g => `${g[1]} s of speech · ${g[2] === 'запись' ? 'recording' : 'from a meeting'}`,
      g => `${g[1]} с сөйлеу · ${g[2] === 'запись' ? 'жазба' : 'кеңестен'}`],
    [/^Показаны (\d+) из (\d+) ответственных · остальные — через фильтр «Ответственный»$/,
      g => `Showing ${g[1]} of ${g[2]} owners · use the «Owner» filter for the rest`,
      g => `${g[2]} жауаптының ${g[1]}-і көрсетілген · қалғаны «Жауапты» сүзгісі арқылы`],
    [/^Сейчас: (.+) · локально$/, g => `Now: ${g[1]} · local`, g => `Қазір: ${g[1]} · жергілікті`],
    [/^Сейчас: (.+) · внешний API$/, g => `Now: ${g[1]} · external API`, g => `Қазір: ${g[1]} · сыртқы API`],
    [/^(.+) · локально$/, g => `${g[1]} · local`, g => `${g[1]} · жергілікті`],
    [/^(.+) · внешний API$/, g => `${g[1]} · external API`, g => `${g[1]} · сыртқы API`],
    [/^Выделено (.+) – (.+) · ([\d,]+) с$/, g => `Selected ${g[1]} – ${g[2]} · ${g[3].replace(',', '.')} s`, g => `Белгіленді ${g[1]} – ${g[2]} · ${g[3]} с`],
    [/^Последняя отправка: HTTP (\d+) · (.+)$/, g => `Last delivery: HTTP ${g[1]} · ${g[2]}`, g => `Соңғы жіберу: HTTP ${g[1]} · ${g[2]}`],
    [/^Ошибка: (.+) · (.+)$/, g => `Error: ${g[1].replace('нет соединения', 'no connection')} · ${g[2]}`, g => `Қате: ${g[1].replace('нет соединения', 'байланыс жоқ')} · ${g[2]}`],
    [/^Отправить в «(.+)»$/, g => `Send to «${g[1]}»`, g => `«${g[1]}» жүйесіне жіберу`],
    [/^Отправлено в «(.+)» \(HTTP (\d+)\)$/, g => `Sent to «${g[1]}» (HTTP ${g[2]})`, g => `«${g[1]}» жүйесіне жіберілді (HTTP ${g[2]})`],
    [/^Отправлено в «(.+)»: HTTP (\d+)(.*)$/, g => `Sent to «${g[1]}»: HTTP ${g[2]}${g[3] ? ' — error' : ''}`, g => `«${g[1]}» жүйесіне жіберілді: HTTP ${g[2]}${g[3] ? ' — қате' : ''}`],
    [/^Интеграция · (.*)$/, g => `Integration · ${g[1]}`, g => `Интеграция · ${g[1]}`],
    [/^Голосовой профиль · (.*)$/, g => `Voice profile · ${g[1]}`, g => `Дауыс профилі · ${g[1]}`],
    [/^Голосовой профиль «(.+)» сохранён · (\d+) с речи$/, g => `Voice profile «${g[1]}» saved · ${g[2]} s of speech`, g => `«${g[1]}» дауыс профилі сақталды · ${g[2]} с сөйлеу`],
    [/^Напоминание отправлено: (.+)$/, g => `Reminder sent: ${g[1]}`, g => `Еске салу жіберілді: ${g[1]}`],
    [/^Создано ИИ из протокола, уверенность (\d+)%$/, g => `Created by AI from the minutes, confidence ${g[1]}%`, g => `ЖИ хаттамадан жасады, сенімділік ${g[1]}%`],
    [/^Срок через (\d+) дн\.: (.+)$/, g => `Due in ${g[1]} d: ${g[2]}`, g => `Мерзімге ${g[1]} күн қалды: ${g[2]}`],
    [/^Просрочено: (.+)$/, g => `Overdue: ${g[1]}`, g => `Мерзімі өтті: ${g[1]}`],
    [/^Срок сегодня: (.+)$/, g => `Due today: ${g[1]}`, g => `Мерзімі бүгін: ${g[1]}`],
    [/^(.+) · срок был (\d{1,2}) ([а-я]+)$/, g => `${g[1]} · was due ${g[3] in {} ? g[3] : (MG.includes(g[3]) ? M.en.l[MG.indexOf(g[3])] + ' ' + g[2] : g[2] + ' ' + g[3])}`,
      g => `${g[1]} · мерзімі ${g[2]} ${MG.includes(g[3]) ? M.kk.l[MG.indexOf(g[3])] : g[3]} болған`],
    [/^(.+) · до (\d{1,2}) ([а-я]+)$/, g => `${g[1]} · by ${MG.includes(g[3]) ? M.en.l[MG.indexOf(g[3])] + ' ' + g[2] : g[2] + ' ' + g[3]}`,
      g => `${g[1]} · ${g[2]} ${MG.includes(g[3]) ? M.kk.l[MG.indexOf(g[3])] : g[3]} дейін`],
    [/^(\d+) (?:ответственному|ответственным)$/, g => `${g[1]} owners`, g => `${g[1]} жауаптыға`],
    [/^Сформировано (.+)$/, g => `Generated ${g[1]}`, g => `Жасалды ${g[1]}`]
  ];

  function tr(txt) {
    const k = txt.trim();
    if (!k || !CYR.test(k)) return txt;
    const d = DICT && DICT[lang];
    if (d && d[k] !== undefined) return txt.replace(k, d[k]);
    for (const [rx, en, kk] of RULES) {
      const g = k.match(rx);
      if (g) return txt.replace(k, (lang === 'en' ? en : kk)(g));
    }
    return txt;
  }
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'OPTION_SKIP']);
  function trTextNode(n) {
    const p = n.parentNode;
    if (!p || SKIP.has(p.nodeName)) return;
    const cur = n.nodeValue;
    if (lang === 'ru') { return; }
    if (!cur || !CYR.test(cur)) return;
    const t = tr(cur);
    if (t !== cur) { ORIG.set(n, cur); n.nodeValue = t; }
  }
  function trAttrs(el) {
    for (const a of ['placeholder', 'title']) {
      const v = el.getAttribute && el.getAttribute(a);
      if (!v || !CYR.test(v)) continue;
      const t = tr(v);
      if (t !== v) {
        const o = ORIG_ATTR.get(el) || {}; o[a] = v; ORIG_ATTR.set(el, o);
        el.setAttribute(a, t);
      }
    }
  }
  function walk(root) {
    if (root.nodeType === 3) { trTextNode(root); return; }
    if (root.nodeType !== 1) return;
    trAttrs(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = w.nextNode())) { if (n.nodeType === 3) trTextNode(n); else trAttrs(n); }
  }
  function restore(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) { if (ORIG.has(n)) { n.nodeValue = ORIG.get(n); ORIG.delete(n); } }
      else if (ORIG_ATTR.has(n)) { const o = ORIG_ATTR.get(n); for (const a in o) n.setAttribute(a, o[a]); ORIG_ATTR.delete(n); }
    }
  }
  function ensureObserver() {
    if (obs) return;
    obs = new MutationObserver(ms => {
      if (lang === 'ru' || busy) return;
      busy = true;
      try {
        for (const m of ms) {
          if (m.type === 'characterData') trTextNode(m.target);
          else if (m.type === 'attributes') trAttrs(m.target);
          else m.addedNodes.forEach(walk);
        }
      } finally { busy = false; }
    });
    obs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['placeholder', 'title'] });
  }
  const TITLE_RU = document.title;
  window.__hsSetLang = function (l) {
    l = l === 'kk' || l === 'en' ? l : 'ru';
    if (l === lang && (l === 'ru' || DICT)) return;
    const prev = lang;
    lang = l;
    document.documentElement.lang = l === 'kk' ? 'kk' : l;
    if (l === 'ru') { restore(document.body); document.title = TITLE_RU; return; }
    const apply = () => {
      if (lang !== l) return;
      if (prev !== 'ru') restore(document.body);
      ensureObserver();
      busy = true;
      try { walk(document.body); } finally { busy = false; }
      document.title = tr(TITLE_RU);
    };
    if (DICT) apply();
    else (loading = loading || fetch('/static/i18n.json').then(r => r.json()).catch(() => ({})))
      .then(d => { DICT = d || {}; apply(); });
  };
})();
