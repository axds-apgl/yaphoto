'use strict';
const csrfToken = document.querySelector('meta[name="yaphoto-csrf"]').content;
const $ = id => document.getElementById(id),
  number = n => Number(n || 0).toLocaleString('ru-RU');
let filter = '',
  offset = 0,
  startOffset = 0,
  total = 0,
  loading = false,
  generation = 0,
  months = [],
  items = new Map(),
  currentMonth = '',
  bubbleTimer, jobRunning = false,
  lastSummary = '',
  refreshTimer, catalogRetry, catalogNeedsReset = false,
  catalogRevision = null;

function toast(text) {
  $('toast').textContent = text;
  $('toast').style.display = 'block';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('toast').style.display = 'none', 5000)
}
async function api(url, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(url, {
      ...(data ? {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-YaPhoto-Token': csrfToken
        },
        body: JSON.stringify(data)
      } : {}),
      signal: controller.signal
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Ошибка запроса');
    return result
  } catch (e) {
    if (e.name === 'AbortError') throw Error(
      'Сервер не ответил вовремя. Проверьте статус процесса.');
    throw e
  } finally {
    clearTimeout(timer)
  }
}

function media() {
  return $('photo').checked ? ($('video').checked ? 'all' : 'photo') : ($('video').checked ?
    'video' : 'none')
}

function settings(command) {
  return {
    command,
    media: media(),
    limit: $('limit').value,
    workers: $('workers').value,
    attempts: $('attempts').value,
    timeout: $('timeout').value,
    order: $('order').value,
    dates: $('dates').checked,
    previews: $('previews').checked,
    only_missing: $('only_missing').checked
  }
}
async function start(command) {
  if (command === 'upgrade-previews' && media() !== 'photo') return toast(
    'Улучшение превью доступно только для фото');
  if (media() === 'none') return toast('Выберите фото или видео');
  try {
    await api('/api/start', settings(command));
    await poll()
  } catch (e) {
    toast(e.message)
  }
}
document.querySelectorAll('[data-command]').forEach(b => b.onclick = () => start(b.dataset
  .command));
$('start').onclick = () => start('download');
$('stop').onclick = async () => {
  try {
    await api('/api/stop', {});
    poll()
  } catch (e) {
    toast(e.message)
  }
};
$('upload').onclick = () => $('cookies').click();
$('cookies').onchange = async e => {
  try {
    const file = e.target.files[0];
    if (!file) return;
    const cookies = JSON.parse(await file.text());
    await api('/api/cookies', {
      cookies
    });
    $('cookie-note').textContent = 'Cookies загружены. Нажмите «Проверить».';
    toast('Cookies сохранены локально')
  } catch (e) {
    toast(e.message)
  } finally {
    $('cookies').value = ''
  }
};

function monthLabel(month) {
  if (!month || month === 'Без даты') return 'Без даты';
  return new Date(month + '-02T12:00:00').toLocaleDateString('ru-RU', {
    month: 'long',
    year: 'numeric'
  }).replace(' г.', '')
}

function dateLabel(date) {
  if (date === 'Без даты') return date;
  return new Date(date + 'T12:00:00').toLocaleDateString('ru-RU', {
    day: 'numeric',
    month: 'long',
    year: 'numeric'
  }).replace(' г.', '')
}

function updateSummary(s) {
  for (const k of ['total', 'original', 'preview', 'error']) $(k).textContent = number(s[k]);
  $('types').textContent = `${number(s.photo)} фото · ${number(s.video)} видео`;
  lastSummary = JSON.stringify(s)
}

function makeTile(item) {
  items.set(item.id, item);
  const tile = document.createElement('div');
  tile.className = 'tile ' + item.state;
  tile.dataset.id = item.id;
  tile.tabIndex = 0;
  tile.setAttribute('role', 'button');
  tile.setAttribute('aria-label', item.name);
  const pic = document.createElement('div');
  pic.className = 'picture';
  const ph = document.createElement('span');
  ph.className = 'placeholder';
  ph.textContent = item.media_kind === 'video' ? '▷' : '▧';
  pic.append(ph);
  if (['original', 'preview'].includes(item.state)) {
    const im = document.createElement('img');
    im.alt = '';
    im.loading = 'lazy';
    im.dataset.id = item.id;
    thumbObserver.observe(im);
    pic.append(im);
    if (item.media_kind === 'video') {
      const p = document.createElement('span');
      p.className = 'play';
      p.textContent = '▶';
      pic.append(p)
    }
    if (item.state === 'original') {
      const s = document.createElement('span');
      s.className = 'checkmark';
      s.textContent = '✓';
      pic.append(s)
    } else {
      const s = document.createElement('span');
      s.className = 'badge';
      s.textContent = `Превью · ${item.width}×${item.height}`;
      pic.append(s)
    }
  } else if (item.state === 'error') {
    const s = document.createElement('span');
    s.className = 'error-tag';
    s.textContent = 'Нет копии';
    pic.append(s)
  }
  const name = document.createElement('div');
  name.className = 'tile-name';
  name.textContent = item.name;
  tile.append(pic, name);
  tile.onclick = () => view(item);
  tile.onkeydown = e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      view(item)
    }
  };
  return tile
}
const thumbObserver = new IntersectionObserver(entries => {
  for (const e of entries)
    if (e.isIntersecting) {
      thumbObserver.unobserve(e.target);
      loadThumb(e.target)
    }
}, {
  root: $('scroll'),
  rootMargin: '300px'
});
async function loadThumb(im, tries = 0) {
  if (!im.isConnected) return;
  try {
    const url = '/api/thumb?id=' + im.dataset.id;
    const r = await fetch(url, {
      signal: AbortSignal.timeout(8000)
    });
    if (r.status === 202) {
      if (tries < 12) setTimeout(() => loadThumb(im, tries + 1), 1800);
      return
    }
    if (!r.ok) return;
    const blob = URL.createObjectURL(await r.blob());
    im.onload = () => URL.revokeObjectURL(blob);
    im.src = blob
  } catch {
    if (tries < 12) setTimeout(() => loadThumb(im, tries + 1), 1800)
  }
}
async function loadPage(reset = false, month = '', awaitCheck = 0) {
  if (loading && !reset) return;
  reset = reset || catalogNeedsReset;
  clearTimeout(catalogRetry);
  const gen = reset ? ++generation : generation;
  if (reset && !awaitCheck) catalogNeedsReset = true;
  let retryPending = false;
  const retry = (check = awaitCheck) => {
    retryPending = true;
    catalogRetry = setTimeout(() => {
      if (gen === generation) loadPage(reset, month, check)
    }, 1500)
  };
  loading = true;
  $('sentinel').textContent = 'Загрузка…';
  const query = new URLSearchParams({
    media: media(),
    state: filter,
    order: $('gallery-order').value,
    limit: 60
  });
  if (reset && !awaitCheck) query.set('refresh', '1');
  if (month) query.set('month', month);
  else query.set('offset', reset ? 0 : offset);
  try {
    const data = await api('/api/catalog?' + query);
    if (gen !== generation) return;
    if (data.loading || (awaitCheck && data.checked < awaitCheck)) {
      $('sentinel').textContent = data.error || 'Читаем каталог…';
      retry(data.check || awaitCheck);
      return
    }
    if (awaitCheck && !catalogNeedsReset && data.revision === catalogRevision) {
      $('sentinel').textContent = offset < total ? 'Прокрутите, чтобы загрузить ещё' :
        'Всё показано';
      return
    }
    const previousScroll = $('scroll').scrollTop;
    if (reset) {
      items.clear();
      thumbObserver.disconnect();
      $('gallery').replaceChildren();
      $('scroll').scrollTop = 0;
      catalogNeedsReset = false;
      startOffset = data.offset
    }
    catalogRevision = data.revision;
    updateSummary(data.summary);
    months = data.months;
    total = data.total;
    offset = data.next;
    if (reset) buildTimeline();
    renderItems(data.items);
    updatePrevious();
    if (!data.items.length && reset) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = total ? 'Нет файлов для выбранной даты' :
        'Медиатека пока пуста. Загрузите cookies и отсканируйте каталог.';
      $('gallery').append(empty)
    }
    $('gallery-note').textContent =
      `${number(total)} файлов · ${filter?({original:'оригиналы',preview:'превью',error:'ошибки'}[filter]):'все состояния'}`;
    $('sentinel').textContent = offset < total ? 'Прокрутите, чтобы загрузить ещё' :
      'Всё показано';
    if (awaitCheck) $('scroll').scrollTop = previousScroll;
    updatePosition();
    if (!awaitCheck && reset && data.refreshing) retry(data.check)
  } catch (e) {
    if (gen !== generation) return;
    toast(e.message);
    $('sentinel').textContent = 'Не удалось загрузить каталог. Повторяем…';
    retry()
  } finally {
    if (gen === generation) {
      loading = false;
      if (!retryPending) requestAnimationFrame(() => {
        if (reset && month && startOffset > 0) loadPrevious();
        else fillGallery()
      })
    }
  }
}

function renderItems(entries, prepend = false) {
  const fragment = document.createDocumentFragment();
  for (const item of entries) {
    if (items.has(item.id)) continue;
    let group = fragment.lastElementChild;
    if (!group || group.dataset.date !== item.date) {
      group = document.createElement('section');
      group.dataset.date = item.date;
      const title = document.createElement('h3');
      title.textContent = dateLabel(item.date);
      const grid = document.createElement('div');
      grid.className = 'grid';
      group.append(title, grid);
      fragment.append(group)
    }
    group.lastElementChild.append(makeTile(item))
  }
  const boundary = prepend ? fragment.lastElementChild : fragment.firstElementChild;
  const existing = prepend ? $('gallery').firstElementChild : $('gallery').lastElementChild;
  if (boundary && existing && boundary.dataset.date === existing.dataset.date) {
    if (prepend) existing.lastElementChild.prepend(...boundary.lastElementChild.children);
    else existing.lastElementChild.append(...boundary.lastElementChild.children);
    boundary.remove()
  }
  if (prepend) $('gallery').prepend(fragment);
  else $('gallery').append(fragment)
}

function updatePrevious() {
  $('previous').hidden = startOffset === 0;
  $('previous').textContent = '↑ Показать предыдущие';
  $('previous').disabled = false
}

function fillGallery() {
  if (!loading && offset < total &&
    $('scroll').scrollHeight - $('scroll').scrollTop - $('scroll').clientHeight < 700) loadPage()
}

async function loadPrevious() {
  if (loading || startOffset === 0) return;
  const gen = generation;
  const before = startOffset;
  const first = Math.max(0, before - 60);
  loading = true;
  $('previous').disabled = true;
  $('previous').textContent = 'Загрузка…';
  try {
    const query = new URLSearchParams({
      media: media(),
      state: filter,
      order: $('gallery-order').value,
      offset: first,
      limit: before - first
    });
    const data = await api('/api/catalog?' + query);
    if (gen !== generation) return;
    if (data.loading) {
      $('previous').textContent = 'Каталог ещё загружается. Повторить';
      return
    }
    const anchor = $('gallery').querySelector('.tile');
    const anchorTop = anchor?.getBoundingClientRect().top;
    renderItems(data.items, true);
    startOffset = data.offset;
    updatePrevious();
    if (anchor && anchorTop !== undefined) {
      $('scroll').scrollTop += anchor.getBoundingClientRect().top - anchorTop
    }
    updatePosition()
  } catch (e) {
    if (gen === generation) {
      toast(e.message);
      $('previous').textContent = 'Повторить загрузку предыдущих'
    }
  } finally {
    if (gen === generation) {
      loading = false;
      $('previous').disabled = false
    }
  }
}
$('previous').onclick = loadPrevious;
new IntersectionObserver(es => {
  if (es[0].isIntersecting && offset < total && !loading) loadPage()
}, {
  root: $('scroll'),
  rootMargin: '500px'
}).observe($('sentinel'));
$('scroll').addEventListener('scroll', () => {
  updatePosition();
  if ($('scroll').scrollTop < 180 && startOffset > 0 && !loading) loadPrevious();
  if ($('scroll').scrollHeight - $('scroll').scrollTop - $('scroll').clientHeight < 700 &&
    offset < total && !loading) loadPage()
}, {
  passive: true
});

function buildTimeline() {
  const scale = $('scale');
  scale.replaceChildren();
  const years = new Map();
  const counts = new Map();
  const seen = new Map();
  for (const month of months) {
    const year = month.slice(0, 4);
    if (!years.has(year)) years.set(year, years.size);
    counts.set(year, (counts.get(year) || 0) + 1)
  }
  const positions = months.map(month => {
    const year = month.slice(0, 4);
    const index = seen.get(year) || 0;
    seen.set(year, index + 1);
    return years.get(year) + index / counts.get(year)
  });
  const lastPosition = positions.at(-1) || 1;
  let previous = '';
  months.forEach((m, i) => {
    const tick = document.createElement('div');
    tick.className = 'tick';
    tick.dataset.month = m;
    tick.dataset.position = positions[i] / lastPosition;
    tick.style.top = (positions[i] / lastPosition * 100) + '%';
    const year = m.slice(0, 4);
    if (year !== previous) {
      tick.classList.add('year');
      tick.textContent = year;
      previous = year
    }
    tick.title = monthLabel(m);
    scale.append(tick)
  });
  compactScale()
}

function compactScale() {
  const ticks = [...$('scale').children];
  const height = $('scale').clientHeight;
  let lastYear = -30;
  const visibleYears = [];
  for (const tick of ticks.filter(t => t.classList.contains('year'))) {
    const y = Number(tick.dataset.position) * height;
    const visible = y - lastYear >= 30;
    tick.style.visibility = visible ? 'visible' : 'hidden';
    if (visible) {
      lastYear = y;
      visibleYears.push(y)
    }
  }
  let lastTick = -12;
  for (const tick of ticks.filter(t => !t.classList.contains('year'))) {
    const y = Number(tick.dataset.position) * height;
    const visible = y - lastTick >= 9 && visibleYears.every(year => Math.abs(year - y) >= 15);
    tick.style.visibility = visible ? 'visible' : 'hidden';
    if (visible) lastTick = y
  }
}
new ResizeObserver(compactScale).observe($('scale'));

function monthPosition(month) {
  const tick = [...$('scale').children].find(t => t.dataset.month === month);
  return $('scale').offsetTop + Number(tick?.dataset.position || 0) * $('scale').clientHeight
}

function showBubble(month, y) {
  $('date-bubble').textContent = monthLabel(month);
  $('date-bubble').style.top = Math.max(35, Math.min($('timeline').clientHeight - 60, y)) + 'px';
  $('date-bubble').style.display = 'block';
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => $('date-bubble').style.display = 'none', 1300)
}

function updatePosition() {
  const rect = $('scroll').getBoundingClientRect();
  const group = [...$('gallery').children].find(g => g.dataset.date && g.getBoundingClientRect()
    .bottom > rect.top + 30);
  if (!group) return;
  const m = group.dataset.date.slice(0, 7);
  if (m === currentMonth) return;
  currentMonth = m;
  document.querySelectorAll('.tick').forEach(t => t.classList.toggle('active', t.dataset.month ===
    m));
  if (months.includes(m)) showBubble(m, monthPosition(m))
}

function jump(month) {
  if (!month) return;
  currentMonth = '';
  loadPage(true, month);
  showBubble(month, monthPosition(month))
}
let dragMonth = '';

function scrub(e) {
  if (!months.length) return;
  const rect = $('scale').getBoundingClientRect();
  const position = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
  const ticks = [...$('scale').children];
  const nearest = ticks.reduce((best, tick) =>
    Math.abs(Number(tick.dataset.position) - position) <
    Math.abs(Number(best.dataset.position) - position) ? tick : best);
  dragMonth = nearest.dataset.month;
  showBubble(dragMonth, e.clientY - $('timeline').getBoundingClientRect().top)
}
$('scale').onpointerdown = e => {
  if (e.button !== 0) return;
  $('scale').setPointerCapture(e.pointerId);
  scrub(e)
};
$('scale').onpointermove = e => {
  if ($('scale').hasPointerCapture(e.pointerId)) scrub(e)
};
$('scale').onpointerup = e => {
  if ($('scale').hasPointerCapture(e.pointerId)) {
    $('scale').releasePointerCapture(e.pointerId);
    jump(dragMonth)
  }
};
$('calendar').onclick = () => {
  if ($('jump-date').showPicker) $('jump-date').showPicker();
  else {
    $('jump-date').style.cssText = 'opacity:1;pointer-events:auto;width:150px';
    $('jump-date').focus()
  }
};
$('jump-date').onchange = e => {
  const target = e.target.value;
  const exact = months.includes(target) ? target : months.reduce((best, m) => Math.abs(new Date(
    m + '-01') - new Date(target + '-01')) < Math.abs(new Date(best + '-01') - new Date(
    target + '-01')) ? m : best, months[0]);
  jump(exact)
};
$('bottom-date').onclick = () => jump(months.at(-1));
document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
  filter = b.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(x => x.classList.toggle('selected', x ===
    b));
  loadPage(true)
});
$('gallery-order').onchange = () => loadPage(true);
for (const id of ['photo', 'video']) $(id).onchange = () => {
  if (media() === 'none') return;
  loadPage(true)
};

function view(item) {
  $('viewer-name').textContent = item.name;
  $('viewer-info').textContent =
    `${dateLabel(item.date)} · ${item.media_kind==='video'?'Видео':'Фото'} · ${{original:'Оригинал',preview:'Сохранённое превью',error:'Нет локальной копии',pending:'Ожидает скачивания'}[item.state]} · ${number(Math.round(item.size/1024))} КБ`;
  $('viewer-media').replaceChildren();
  const copied = ['original', 'preview'].includes(item.state);
  $('open-file').hidden = !copied;
  if (copied) {
    const el = document.createElement(item.media_kind === 'video' ? 'video' : 'img');
    el.src = '/api/media?id=' + item.id;
    if (item.media_kind === 'photo') el.onerror = () => {
      el.onerror = null;
      el.src = '/api/thumb?id=' + item.id;
      $('viewer-info').textContent += ' · Просмотр миниатюры; оригинал доступен по ссылке'
    };
    if (item.media_kind === 'video') el.controls = true;
    else el.alt = item.name;
    $('viewer-media').append(el);
    $('open-file').href = '/api/media?id=' + item.id
  } else $('viewer-media').textContent = 'После скачивания здесь появится медиа';
  $('viewer').showModal()
}
$('close-viewer').onclick = () => $('viewer').close();
$('viewer').addEventListener('close', () => {
  $('viewer-media').replaceChildren()
});
$('viewer').onclick = e => {
  if (e.target === $('viewer')) $('viewer').close()
};
async function poll() {
  try {
    const job = await api('/api/state');
    const ended = jobRunning && !job.running;
    jobRunning = job.running;
    $('start').disabled = job.running;
    $('stop').disabled = !job.running || job.stopping || !['download', 'upgrade-previews']
      .includes(job.command);
    document.querySelectorAll('[data-command],#upload').forEach(b => b.disabled = job.running || (
      b.dataset.command === 'upgrade-previews' && media() !== 'photo'));
    $('status').textContent = job.stopping ? 'Останавливаем после текущих файлов…' : job.running ?
      ({
        download: 'Скачивание',
        scan: 'Сканирование',
        verify: 'Проверка файлов',
        check: 'Проверка подключения',
        'upgrade-previews': 'Улучшение превью'
      } [job.command]) : job.command ? (job.code === 0 ? 'Завершено' : job.code === 2 ?
        'Завершено — проверьте итог' : 'Процесс завершился с ошибкой') : 'Готов к работе';
    $('live').style.color = job.running ? '#3d70ef' : '#a9b3c2';
    $('log').replaceChildren(...job.log.slice(-18).map(line => {
      const d = document.createElement('div');
      d.textContent = line;
      return d
    }));
    $('log').scrollTop = $('log').scrollHeight;
    const p = job.progress || {};
    if (p.overall) $('overall').textContent = p.overall;
    else if (!job.command) $('overall').textContent = 'Готов к следующему запуску';
    const pct = (p.overall || $('overall').textContent || '').match(/(\d+(?:\.\d+)?)%/);
    if (job.running && !pct) $('progress').removeAttribute('value');
    else $('progress').value = pct ? Number(pct[1]) : job.command ? 100 : 0;
    $('percentage').textContent = pct ? pct[1] + '%' : !job.running && job.command ? '100%' : '';
    const activeWorkers = new Set();
    for (const [id, w] of Object.entries(p.workers || {})) {
      activeWorkers.add(id);
      let row = [...$('worker-list').children].find(el => el.dataset.worker === id);
      if (!row) {
        row = document.createElement('div');
        row.className = 'worker';
        row.dataset.worker = id;
        const label = document.createElement('div');
        const bar = document.createElement('progress');
        bar.max = 100;
        const code = document.createElement('strong');
        code.textContent = 'W' + id;
        row.append(code, bar, label);
        $('worker-list').append(row)
      }
      const request = Object.values(p.requests || {}).find(r => String(r.worker) === id);
      row.lastElementChild.textContent =
        (w.attempt ? w.attempt + ' · ' : '') +
        `${request ? request.phase : w.stage} · ${w.name}`;
      const bar = row.children[1];
      if (request?.total) {
        bar.value = Math.min(100, request.done / request.total * 100);
        bar.setAttribute('aria-label', `W${id}: ${bar.value.toFixed(0)}%`)
      } else if (request) bar.removeAttribute('value');
      else bar.value = 100
    }
    for (const row of [...$('worker-list').children]) {
      if (!activeWorkers.has(row.dataset.worker)) row.remove()
    }
    if (ended) {
      $('connection').textContent = job.command === 'check' && job.code === 0 ? '● Подключено' :
        $('connection').textContent;
      loadPage(true)
    }
    if (job.running && !refreshTimer) refreshTimer = setTimeout(async () => {
      refreshTimer = null;
      try {
        const rect = $('scroll').getBoundingClientRect();
        const visible = [...document.querySelectorAll('.tile')].filter(t => {
          const r = t.getBoundingClientRect();
          return r.bottom > rect.top - 200 && r.top < rect.bottom + 200
        }).slice(0, 100);
        const q = new URLSearchParams({
          limit: 100,
          media: media(),
          ids: visible.map(t => t.dataset.id).join(',')
        });
        const d = await api('/api/catalog?' + q);
        updateSummary(d.summary);
        for (const item of d.items) {
          const tile = visible.find(t => t.dataset.id === item.id);
          if (tile && items.get(item.id)?.state !== item.state) tile.replaceWith(makeTile(
            item))
        }
      } catch {}
    }, 5000)
  } catch (e) {
    $('status').textContent = 'Нет связи с локальным сервером'
  } finally {
    clearTimeout(poll.timer);
    poll.timer = setTimeout(poll, 1200)
  }
}
const savedIds = ['limit', 'workers', 'timeout', 'attempts', 'order', 'dates', 'previews',
  'only_missing', 'photo', 'video', 'gallery-order'
];
try {
  const saved = JSON.parse(localStorage.getItem('yaphoto-settings') || '{}');
  for (const id of savedIds) {
    const el = $(id);
    if (saved[id] !== undefined) {
      if (el.type === 'checkbox') el.checked = Boolean(saved[id]);
      else el.value = saved[id]
    }
  }
} catch {}
for (const id of savedIds) $(id).addEventListener('change', () => {
  try {
    const state = {};
    for (const key of savedIds) {
      const el = $(key);
      state[key] = el.type === 'checkbox' ? el.checked : el.value
    }
    localStorage.setItem('yaphoto-settings', JSON.stringify(state))
  } catch {}
});
$('scale').tabIndex = 0;
$('scale').setAttribute('role', 'slider');
$('scale').setAttribute('aria-label', 'Месяц галереи');
$('scale').onkeydown = e => {
  let index = Math.max(0, months.indexOf(currentMonth));
  if (e.key === 'ArrowDown') index = Math.min(months.length - 1, index + 1);
  else if (e.key === 'ArrowUp') index = Math.max(0, index - 1);
  else if (e.key === 'Home') index = 0;
  else if (e.key === 'End') index = months.length - 1;
  else return;
  e.preventDefault();
  jump(months[index])
};
loadPage(true);
poll();
