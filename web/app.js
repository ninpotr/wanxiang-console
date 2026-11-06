/* 万相3.0 视频创作台 —— 前端逻辑 */

const $ = (id) => document.getElementById(id);

const state = {
  options: null,
  settings: null,
  assets: [],          // 已暂存的参考素材
  resolution: '1080P',
  ratio: 'adaptive',
  durationMode: 'custom',   // custom | smart
  duration: 5,
  audio: true,
  promptExtend: true,
  watermark: false,
  mode: 'all',             // 创作模式：all / frames / i2v / t2v / r2v / edit
  optimizeResult: null,     // 手动整理后的文本；发送时优先用它
  optimizeFailed: false,    // 上一次整理是否失败（失败就不当成整理结果）
  lastEstimate: null,
  jobId: null,
  pollTimer: null,
  optimizeResult: null,
};

const IMAGE_TYPES = [
  ['reference_image', '参考图'],
  ['first_frame', '首帧'],
  ['last_frame', '尾帧'],
];
const VIDEO_TYPES = [['reference_video', '参考视频']];
const AUDIO_TYPES = [['reference_audio', '参考音频']];

const RATIO_BOX = {
  adaptive: null, '21:9': [26, 11], '16:9': [26, 15], '4:3': [22, 17],
  '1:1': [19, 19], '3:4': [17, 22], '9:16': [15, 26],
};

/* ---------------------------------------------------------------- 工具 */

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

let toastTimer = null;
let toastHover = false;

/** 关掉提示条。鼠标悬停时不会自动消失，方便把长句子读完。 */
function hideToast() {
  clearTimeout(toastTimer);
  toastTimer = null;
  $('toast').hidden = true;
}

function toast(message, bad = false) {
  const el = $('toast');
  $('toast-text').textContent = message;
  el.className = 'toast' + (bad ? ' bad' : '');
  el.hidden = false;
  toastHover = false;

  clearTimeout(toastTimer);
  // 字越多留越久，最长的给到 12 秒
  const ms = Math.min(12000, Math.max(4200, message.length * 110));
  toastTimer = setTimeout(() => { if (!toastHover) hideToast(); }, ms);
}

$('toast-close').addEventListener('click', hideToast);
$('toast').addEventListener('mouseenter', () => { toastHover = true; clearTimeout(toastTimer); });
$('toast').addEventListener('mouseleave', () => {
  toastHover = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 2200);
});

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }
  return data;
}

/* ---------------------------------------------------------------- 标签页 */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    tab.classList.add('active');
    $(`view-${tab.dataset.view}`).classList.add('active');
    if (tab.dataset.view === 'history') loadAssets();
    if (tab.dataset.view === 'analysis') loadAnalysisSources();
    if (tab.dataset.view === 'dash') loadDashboard();
    if (tab.dataset.view === 'canvas') {
      initPostTabs();
      loadUpscaleSources();
      loadUpscaleHistory();
      initWatermarkTab();
      loadWatermarkSources();
      loadWatermarkHistory();
    }
    if (tab.dataset.view === 'settings') loadSettings();
    if (tab.dataset.view === 'voice') {
      initVoiceTab();
    } else {
      stopVoicePolling();
    }
    if (tab.dataset.view === 'logs') {
      loadLogs();
      if ($('log-auto').checked) startLogTimer();
    } else {
      stopLogTimer();
    }
  });
});

/* ---------------------------------------------------------------- 视频分析大师 */

const analysisRounds = [];
let analysisDroppedFile = null;

$('analysis-file').addEventListener('change', () => {
  if ($('analysis-file').files.length) {
    analysisDroppedFile = null;
    $('analysis-asset').value = '';
  }
});
$('analysis-asset').addEventListener('change', () => {
  if ($('analysis-asset').value) {
    $('analysis-file').value = '';
    analysisDroppedFile = null;
  }
});
$('analysis-source-retry').addEventListener('click', () => {
  $('analysis-asset').dataset.loaded = 'false';
  loadAnalysisSources({ force: true });
});
$('analysis-run').addEventListener('click', runVideoAnalysis);

function analysisAssetLabel(job) {
  const createdAt = String(job.created_at || '').replace('T', ' ').slice(0, 16);
  const prompt = String(job.original_prompt || job.prompt || job.dir || '')
    .replace(/\s+/g, ' ').trim();
  const shortPrompt = prompt.length > 58 ? `${prompt.slice(0, 58)}…` : prompt;
  return `${createdAt || '日期未知'} · ${shortPrompt || '未命名视频'}`;
}

async function loadAnalysisSources({ force = false } = {}) {
  const select = $('analysis-asset');
  if (!force && select.dataset.loaded === 'true') return;
  const status = $('analysis-source-status');
  const retry = $('analysis-source-retry');
  status.textContent = '正在读取本机已保存的视频…';
  status.className = 'analysis-source-status loading';
  retry.disabled = true;
  select.innerHTML = '<option value="">读取中…</option>';
  try {
    const data = await api('/api/history');
    const jobs = (data.jobs || []).filter((job) => job.has_video);
    select.innerHTML = '<option value="">选择已生成的视频…</option>' + jobs.map((job) =>
      `<option value="${escapeHtml(job.dir)}" title="${escapeHtml(job.original_prompt || job.prompt || '')}">${escapeHtml(analysisAssetLabel(job))}</option>`
    ).join('');
    if (!jobs.length) {
      select.innerHTML = '<option value="">还没有已保存的视频</option>';
      status.textContent = '没有找到已保存的视频；可以先生成视频，或直接选择本地文件。';
      status.className = 'analysis-source-status';
    } else {
      status.textContent = `已找到 ${jobs.length} 个已保存视频。`;
      status.className = 'analysis-source-status success';
    }
    select.dataset.loaded = 'true';
    retry.disabled = false;
  } catch (error) {
    select.innerHTML = '<option value="">读取视频列表失败</option>';
    status.textContent = `读取失败：${error.message}`;
    status.className = 'analysis-source-status error';
    retry.disabled = false;
  }
}

function readAnalysisBaseline() {
  const avgViews = Number($('analysis-avg-views').value);
  if (!Number.isInteger(avgViews) || avgViews < 1) {
    throw new Error('请填写大于 0 的账号近期平均播放量。');
  }
  const baseline = { avg_views: avgViews };
  const optionalFields = [
    ['analysis-retention', 'retention_2s'],
    ['analysis-completion', 'completion_rate'],
    ['analysis-like-rate', 'like_rate'],
    ['analysis-comment-rate', 'comment_rate'],
    ['analysis-share-rate', 'share_rate'],
    ['analysis-follow-rate', 'follow_rate'],
  ];
  for (const [id, key] of optionalFields) {
    const input = $(id);
    if (input.value.trim() === '') continue;
    const value = Number(input.value);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      throw new Error(`${input.previousElementSibling.textContent}必须在 0 到 100 之间。`);
    }
    baseline[key] = value;
  }
  return baseline;
}

async function runVideoAnalysis() {
  const file = $('analysis-file').files[0] || analysisDroppedFile;
  const asset = $('analysis-asset').value;
  const includeAudioMetrics = $('analysis-audio-metrics').checked;
  if (!file && !asset) {
    $('analysis-status').textContent = '先选择一个本地视频，或从创作台已生成的视频中选择。';
    $('analysis-status').className = 'analysis-status error';
    return;
  }

  let baseline;
  try {
    baseline = readAnalysisBaseline();
  } catch (error) {
    $('analysis-status').textContent = error.message;
    $('analysis-status').className = 'analysis-status error';
    return;
  }

  const button = $('analysis-run');
  button.disabled = true;
  button.textContent = '分析中…';
  $('analysis-status').textContent = '正在检查视频、抽取代表画面并调用视觉模型。通常需要几十秒，请不要关闭页面。';
  $('analysis-status').className = 'analysis-status loading';

  try {
    let result;
    let sourceKey;
    if (file) {
      const form = new FormData();
      form.append('file', file, file.name);
      form.append('baseline', JSON.stringify(baseline));
      form.append('include_audio_metrics', String(includeAudioMetrics));
      result = await api('/api/video-analysis', { method: 'POST', body: form });
      sourceKey = `upload:${file.name}`;
    } else {
      result = await api(`/api/video-analysis/assets/${encodeURIComponent(asset)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          baseline,
          include_audio_metrics: includeAudioMetrics,
        }),
      });
      sourceKey = `asset:${asset}`;
    }
    const previous = [...analysisRounds].reverse().find((round) => round.sourceKey === sourceKey);
    const version = analysisRounds.filter((round) => round.sourceKey === sourceKey).length + 1;
    analysisRounds.push({ result, sourceKey, version, previousScore: previous?.result.analysis.score });
    renderAnalysisRounds();
    $('analysis-status').textContent = `第 ${version} 轮分析完成。修改后重新选择新版本，再分析比较。`;
    $('analysis-status').className = 'analysis-status success';
  } catch (error) {
    $('analysis-status').textContent = `分析失败：${error.message}`;
    $('analysis-status').className = 'analysis-status error';
  } finally {
    button.disabled = false;
    button.textContent = '开始分析';
  }
}

function renderAnalysisRounds() {
  const rounds = $('analysis-rounds');
  $('analysis-empty').hidden = analysisRounds.length > 0;
  rounds.innerHTML = [...analysisRounds].reverse().map((round, displayIndex) => {
    const { result, version, previousScore } = round;
    const analysis = result.analysis;
    const forecast = analysis.forecast;
    const views = forecast.views;
    const scoreDelta = previousScore == null ? '' : `
      <span class="analysis-delta ${analysis.score >= previousScore ? 'up' : 'down'}">
        ${analysis.score >= previousScore ? '+' : ''}${analysis.score - previousScore} 分（较上一轮）
      </span>`;
    const issues = analysis.issues.length
      ? analysis.issues.map((issue) => `
        <div class="analysis-issue">
          <span class="analysis-time">${escapeHtml(issue.time)}</span>
          <div><strong>${escapeHtml(issue.problem)}</strong>
            <p><b>证据：</b>${escapeHtml(issue.evidence)}</p>
            <p>${escapeHtml(issue.reason)}</p>
            <p class="analysis-fix"><b>这样改：</b>${escapeHtml(issue.fix)}</p>
          </div>
        </div>`).join('')
      : '<p class="analysis-muted">没有发现明确问题；先保持当前做得好的部分。</p>';
    const strengths = analysis.strengths.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
    const roleLabels = {
      hook: '开场钩子', setup: '铺垫', payoff: '兑现', reaction: '反应',
      transition: '转场', filler: '重复/空等', unclear: '看不清',
    };
    const decisionLabels = {
      keep: '保留', shorten: '压短', cut: '考虑删减', review: '结合原片确认',
    };
    const timeline = (analysis.timeline || []).map((entry) => `
      <article class="analysis-timeline-entry">
        <span class="analysis-timeline-time">${escapeHtml(entry.time)}</span>
        <div class="analysis-timeline-content">
          <div class="analysis-timeline-labels">
            <span>${escapeHtml(roleLabels[entry.role] || entry.role)}</span>
            <b class="decision-${escapeHtml(entry.decision)}">${escapeHtml(decisionLabels[entry.decision] || entry.decision)}</b>
          </div>
          <strong>${escapeHtml(entry.visual)}</strong>
          <p>${escapeHtml(entry.reason)}</p>
        </div>
      </article>`).join('');
    const blackBands = result.video_info.black_bands || {};
    const edgeLabel = blackBands.orientation === 'top_bottom' ? '上下' : '左右';
    const blackBandText = blackBands.status === 'possible'
      ? `在 ${blackBands.frame_count}/${blackBands.sample_count} 张抽样帧中发现一致的疑似${edgeLabel}暗边，中位估计约占画面 ${blackBands.median_area_percent}%。时间点：${(blackBands.timestamps || []).map(escapeHtml).join('、')}。`
      : blackBands.frame_count
        ? `${blackBands.frame_count}/${blackBands.sample_count} 张抽样帧有边缘暗带信号（${(blackBands.timestamps || []).map(escapeHtml).join('、')}），但样本量或分布不足以确认是黑边。`
        : '抽样帧中未识别到一致的对称边缘暗带；这不代表每一帧都没有黑边。';
    const audioMetrics = result.video_info.audio_metrics || {};
    const audioSamples = audioMetrics.samples || [];
    const audioChart = audioMetrics.status === 'analyzed' && audioSamples.length
      ? `<div class="analysis-audio-scroll"><div class="analysis-audio-bars" role="img" aria-label="每秒 RMS 响度曲线，单位 dBFS">${
        audioSamples.map((sample) => {
          const level = Number(sample.rms_dbfs);
          const valid = sample.rms_dbfs !== null
            && sample.rms_dbfs !== undefined
            && Number.isFinite(level);
          const height = valid ? Math.max(3, Math.min(100, ((level + 60) / 60) * 100)) : 3;
          const label = valid ? `${level.toFixed(1)} dBFS` : '低于测量范围';
          return `<span title="${escapeHtml(sample.second)} 秒：${escapeHtml(label)}" aria-label="${escapeHtml(sample.second)} 秒：${escapeHtml(label)}" style="height:${height}%"></span>`;
        }).join('')
      }</div></div>
      <div class="analysis-audio-range"><span>0 秒</span><span>${escapeHtml(audioSamples[audioSamples.length - 1].second)} 秒</span></div>`
      : '';
    const audioStatus = audioMetrics.status === 'analyzed'
      ? `${audioMetrics.sample_count || 0} 个逐秒响度值${audioMetrics.truncated ? `；视频超过 ${audioSamples.length} 秒，仅显示前 ${audioSamples.length} 秒` : ''}`
      : audioMetrics.status === 'no_audio'
        ? '未检测到音轨，本轮没有响度数据。'
        : audioMetrics.status === 'not_requested'
          ? '本轮未启用音频响度分析。'
          : '没有可用的音频响度数据。';
    const actions = analysis.actions.map((action) => `
      <article class="analysis-action">
        <span class="analysis-priority">优先 ${escapeHtml(action.priority)}</span>
        <strong>${escapeHtml(action.title)}</strong>
        <p>${escapeHtml(action.steps)}</p>
        <small>预期改善：${escapeHtml(action.expected_effect)}</small>
      </article>`).join('');
    const sampledFrames = result.video_info.sampled_frames || [];
    const sampled = sampledFrames.length
      ? `${sampledFrames.length} 张（${escapeHtml(sampledFrames[0])}–${escapeHtml(sampledFrames[sampledFrames.length - 1])}）`
      : '—';
    return `
      <article class="analysis-report">
        <header class="analysis-report-head">
          <div>
            <span class="analysis-kicker">${escapeHtml(result.video_name)} · 第 ${version} 轮</span>
            <h2>${escapeHtml(analysis.headline)}</h2>
          </div>
          <div class="analysis-score"><strong>${escapeHtml(analysis.score)}</strong><span>画面评分</span>${scoreDelta}</div>
        </header>
        <p class="analysis-summary">${escapeHtml(analysis.summary)}</p>
        <div class="analysis-forecast">
          <span class="analysis-kicker">播放量情景估算 · 置信度${escapeHtml(forecast.confidence)}</span>
          <strong>${Number(views.low).toLocaleString()} ~ ${Number(views.high).toLocaleString()}</strong>
          <span>较可能约 ${Number(views.likely).toLocaleString()} 次 · 账号近期平均 ${Number(views.baseline).toLocaleString()} 次</span>
          <p>${escapeHtml(forecast.assumptions)}</p>
        </div>
        <section class="analysis-section">
          <h3>值得保留</h3><ul class="analysis-strengths">${strengths}</ul>
        </section>
        <section class="analysis-section analysis-evidence">
          <h3>本机画面测量 · 辅助线索</h3>
          <p>${blackBandText}</p>
          <small>${escapeHtml(blackBands.caveat || '自动筛查只作参考。')}</small>
          <small>抽帧策略：前 3 秒每 0.5 秒一张，之后约每秒一张，最多 40 张；不自动测量主体占屏比例。</small>
        </section>
        <section class="analysis-section analysis-audio">
          <h3>每秒 RMS 响度</h3>
          <p>${escapeHtml(audioStatus)}</p>
          ${audioChart}
          <small>${escapeHtml(audioMetrics.caveat || '响度曲线不表示具体声音内容、听感或音乐节拍。')}</small>
        </section>
        <details class="analysis-timeline" open>
          <summary>逐段镜头拆解 · ${analysis.timeline.length} 张时间抽样</summary>
          <p class="analysis-timeline-note">剪辑建议依据抽样画面，不是精确镜头边界；单帧看不清的动作标为“结合原片确认”。</p>
          <div class="analysis-timeline-list">${timeline}</div>
        </details>
        <section class="analysis-section">
          <h3>问题与原因</h3>${issues}
        </section>
        <section class="analysis-section">
          <h3>下一版，按这个顺序改</h3><div class="analysis-actions">${actions}</div>
        </section>
        <section class="analysis-prompt">
          <div><h3>下一版创作提示词</h3><p>${escapeHtml(analysis.revised_prompt)}</p></div>
          <button class="ghost sm" data-analysis-prompt="${analysisRounds.length - 1 - displayIndex}">带入创作</button>
        </section>
        <footer class="analysis-report-foot">
          <span>抽样画面：${sampled} · ${escapeHtml(result.model)}</span>
          <span>声音仅提供每秒响度数值（如已启用），不识别台词或音乐；实时推荐流量和实际表现无法由此确认。</span>
        </footer>
      </article>`;
  }).join('');

  rounds.querySelectorAll('[data-analysis-prompt]').forEach((button) => {
    button.addEventListener('click', () => {
      const round = analysisRounds[Number(button.dataset.analysisPrompt)];
      if (!round) return;
      promptEl.value = round.result.analysis.revised_prompt;
      state.optimizeResult = null;
      promptEl.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('.tab[data-view="create"]').click();
      toast('已把建议提示词带入创作页；请先检查并按你的实际内容修改。');
    });
  });
}

/* ---------------------------------------------------------------- 素材 */

const fileMedia = $('file-media');
const fileFirst = $('file-first');
const fileLast = $('file-last');
const fileDoc = $('file-doc');

$('btn-pick-media').addEventListener('click', () => fileMedia.click());
$('btn-pick-doc').addEventListener('click', () => fileDoc.click());

fileMedia.addEventListener('change', async (event) => {
  await uploadFiles([...event.target.files]);
  event.target.value = '';
});

fileDoc.addEventListener('change', async (event) => {
  await uploadFiles([...event.target.files]);
  event.target.value = '';
});

/* -------- 首尾帧：两个格子 + 中间交换箭头 -------- */

const FRAME_SLOTS = [
  { key: 'first', type: 'first_frame', file: fileFirst, label: '首帧' },
  { key: 'last', type: 'last_frame', file: fileLast, label: '尾帧' },
];

for (const slot of FRAME_SLOTS) {
  $(`slot-${slot.key}`).addEventListener('click', (event) => {
    // ✕ 是删除，不是换图
    if (event.target.id === `slot-${slot.key}-x`) {
      state.assets = state.assets.filter((a) => a.media_type !== slot.type);
      renderChips();
      refreshEstimate();
      return;
    }
    slot.file.click();
  });

  slot.file.addEventListener('change', async (event) => {
    const picked = [...event.target.files];
    event.target.value = '';
    if (!picked.length) return;
    await applyFrame(slot.type, picked[0]);
  });
}

$('btn-swap-frames').addEventListener('click', () => {
  const first = state.assets.find((a) => a.media_type === 'first_frame');
  const last = state.assets.find((a) => a.media_type === 'last_frame');
  if (!first && !last) {
    toast('还没放首帧和尾帧');
    return;
  }
  if (first) first.media_type = 'last_frame';
  if (last) last.media_type = 'first_frame';
  renderChips();
  refreshEstimate();
  toast('已交换首尾帧');
});

/** 往首帧/尾帧格子里放一张图。 */
async function applyFrame(type, file) {
  // 首尾帧和参考类素材互斥（官方规则）。先问清楚再动手，
  // 别等用户填完参数提交才报错。
  const hasRef = state.assets.some((a) =>
    ['reference_image', 'reference_video', 'reference_audio'].includes(a.media_type));
  if (hasRef) {
    const ok = window.confirm(
      '首尾帧和参考类素材（参考图/参考视频/参考音频）在官方规则里互斥，不能同时使用。\n\n' +
      '要继续的话，会把已添加的参考素材移除。确定吗？');
    if (!ok) return;
    state.assets = state.assets.filter((a) =>
      !['reference_image', 'reference_video', 'reference_audio'].includes(a.media_type));
  }

  // 同一个格子里的旧图先拿掉
  state.assets = state.assets.filter((a) => a.media_type !== type);

  const label = type === 'first_frame' ? '首帧' : '尾帧';
  await uploadFiles([file], { forcedTypes: [type], quiet: true });
  const placed = state.assets.find((a) => a.media_type === type);

  if (placed && placed.errors.length) {
    toast(`${label}没通过检查：${placed.errors[0]}`, true);
  } else if (state.assets.some((a) => a.media_type === 'first_frame')
             && state.assets.some((a) => a.media_type === 'last_frame')) {
    toast('首尾帧都齐了：视频会从首帧开始、到尾帧结束。');
  } else {
    toast(`${label}已设置。想指定${type === 'first_frame' ? '结尾' : '开头'}，再放另一张。`);
  }
}

/** 把 state 里的首帧/尾帧画到两个格子上。 */
function renderFrameSlots() {
  for (const slot of FRAME_SLOTS) {
    const el = $(`slot-${slot.key}`);
    const thumb = $(`slot-${slot.key}-thumb`);
    const x = $(`slot-${slot.key}-x`);
    const asset = state.assets.find((a) => a.media_type === slot.type);

    el.classList.toggle('filled', Boolean(asset));
    el.classList.toggle('bad', Boolean(asset && asset.errors.length));
    x.hidden = !asset;

    if (asset) {
      thumb.innerHTML = `<img src="${asset.preview_url}" alt="">`;
      el.title = `${slot.label}：${asset.name}\n点一下换一张，右上角 ✕ 移除`;
    } else {
      thumb.innerHTML = '<span class="frame-icon">▧</span>';
      el.title = slot.key === 'first'
        ? '首帧：视频严格从这张图开始。只放首帧就是「首帧生视频」。\n首尾帧和参考类素材互斥。'
        : '尾帧：视频严格到这张图结束。放尾帧就必须同时有首帧。';
    }
  }

  // 只有一边有内容时，交换箭头才有意义
  const hasAny = state.assets.some((a) =>
    a.media_type === 'first_frame' || a.media_type === 'last_frame');
  $('btn-swap-frames').style.opacity = hasAny ? '1' : '.35';
}

async function uploadFiles(files, options = {}) {
  if (!files.length) return;
  const forced = options.forcedTypes || [];

  const form = new FormData();
  files.forEach((file) => form.append('files', file));

  try {
    const data = await api('/api/staging', { method: 'POST', body: form });

    let fixedCount = 0;
    let frameCount = 0;
    data.files.forEach((info, index) => {
      const fix = info.auto_fix || {};
      if (fix.applied) fixedCount += 1;
      // 走「首尾帧」入口时，按顺序强制指定类型
      const mediaType = forced[index] || defaultTypeFor(info.kind);
      if (mediaType === 'first_frame' || mediaType === 'last_frame') frameCount += 1;
      state.assets.push({
        staging_id: info.staging_id,
        name: info.name,
        kind: info.kind,
        staged_path: info.staged_path,
        // 加时间戳：图被就地改写过，不加的话浏览器会显示缓存的旧图
        preview_url: `${info.preview_url}?v=${Date.now()}`,
        duration: info.duration || 0,
        errors: info.errors || [],
        warnings: info.warnings || [],
        auto_fix: fix.applied ? fix : null,
        media_type: mediaType,
      });
    });

    renderChips();
    refreshEstimate();

    // 首尾帧走的是 applyFrame()，提示由它统一给，这里别重复报
    if (!options.quiet) {
      if (fixedCount) {
        toast(`${fixedCount} 张图带了透明通道，已自动压平到白底（官方不支持透明通道）`);
      }
      if (!data.ffprobe) toast('本机没检测到 ffprobe，已跳过素材规格预检');
    }
  } catch (error) {
    toast(`上传到本地暂存失败：${error.message}`, true);
  }
}

function defaultTypeFor(kind) {
  if (kind === 'video') return 'reference_video';
  if (kind === 'audio') return 'reference_audio';
  return 'reference_image';
}

function typeOptions(kind) {
  if (kind === 'video') return VIDEO_TYPES;
  if (kind === 'audio') return AUDIO_TYPES;
  return IMAGE_TYPES;
}

/** 计算每个素材在提示词里的编号：图和视频分别计数（图1、图2、视频1） */
function computeLabels() {
  const counters = { image: 0, video: 0, audio: 0 };
  const prefix = { image: '图', video: '视频', audio: '音频' };
  return state.assets.map((asset) => {
    if (asset.media_type === 'first_frame') return '首帧';
    if (asset.media_type === 'last_frame') return '尾帧';
    counters[asset.kind] = (counters[asset.kind] || 0) + 1;
    return `${prefix[asset.kind] || '素材'}${counters[asset.kind]}`;
  });
}

function renderChips() {
  const row = $('chip-row');
  const labels = computeLabels();
  row.innerHTML = '';

  state.assets.forEach((asset, index) => {
    // 首帧/尾帧由上面那两个格子代表，这里不再重复列一遍
    if (asset.media_type === 'first_frame' || asset.media_type === 'last_frame') return;

    const label = labels[index];
    const chip = document.createElement('div');
    chip.className = 'chip' + (asset.errors.length ? ' bad' : '');

    const thumb = asset.kind === 'image'
      ? `<img class="chip-thumb" src="${asset.preview_url}" alt="">`
      : `<div class="chip-thumb doc">${asset.kind === 'video' ? '▶' : '♪'}</div>`;

    const options = typeOptions(asset.kind)
      .map(([value, text]) => `<option value="${value}"${value === asset.media_type ? ' selected' : ''}>${text}</option>`)
      .join('');

    const problem = asset.errors.length
      ? `<div class="chip-warn">${escapeHtml(asset.errors[0])}</div>`
      : (asset.auto_fix
        ? `<div class="chip-fix" title="官方要求 PNG 不带透明通道，已把透明区域压平到白底">已自动去掉透明通道</div>`
        : '');

    chip.innerHTML = `
      ${thumb}
      <div class="chip-body">
        <div class="chip-label">${escapeHtml(label)}</div>
        <div class="chip-name" title="${escapeHtml(asset.name)}">${escapeHtml(asset.name)}</div>
        ${problem}
      </div>
      <select class="chip-type" data-index="${index}">${options}</select>
      <button class="chip-x" data-remove="${index}" title="移除">✕</button>`;

    row.appendChild(chip);
  });

  row.querySelectorAll('.chip-type').forEach((select) => {
    select.addEventListener('change', (event) => {
      state.assets[Number(event.target.dataset.index)].media_type = event.target.value;
      renderChips();
      refreshEstimate();
    });
  });

  row.querySelectorAll('.chip-x').forEach((button) => {
    button.addEventListener('click', () => {
      state.assets.splice(Number(button.dataset.remove), 1);
      renderChips();
      refreshEstimate();
    });
  });

  renderFrameSlots();
  syncExtendLock();
  updateHint();
}

/** 有文件/网页素材时，官方要求 prompt_extend 必须为 true，这里锁住开关 */
function syncExtendLock() {
  const types = new Set(mediaTypes());
  const locked = types.has('file') || types.has('link');
  const button = $('btn-extend');
  button.classList.toggle('locked', locked);
  if (locked) {
    state.promptExtend = true;
    button.classList.add('on');
  }
  button.title = locked ? '传入文件或网页素材时，官方要求必须开启智能改写' : '';
}

function mediaTypes() {
  return state.assets.map((asset) => asset.media_type);
}

function inputVideoSeconds() {
  return state.assets
    .filter((asset) => asset.media_type === 'reference_video')
    .reduce((sum, asset) => sum + (Number(asset.duration) || 0), 0);
}

/** 参考资料里最容易踩的坑，在这里就提示出来，别等提交后才报错 */
function updateHint() {
  const hint = $('mode-hint');
  const types = new Set(mediaTypes());
  const problems = [];

  const hasFrame = types.has('first_frame') || types.has('last_frame');
  const hasRef = ['reference_image', 'reference_video', 'reference_audio'].some((t) => types.has(t));
  const hasDoc = types.has('file') || types.has('link');

  if (hasFrame && (hasRef || hasDoc)) {
    problems.push('首帧/尾帧与参考类素材互斥，官方会直接拒绝这个组合，请二选一。');
  }
  if (types.has('last_frame') && !types.has('first_frame')) {
    problems.push('只传尾帧不合法，必须同时传首帧。');
  }

  const seconds = inputVideoSeconds();
  if (seconds > 0 && state.durationMode === 'custom' && seconds + state.duration > 30) {
    problems.push(`输入视频 ${seconds.toFixed(1)} 秒 + 输出 ${state.duration} 秒超过 30 秒上限。`);
  }

  const bad = state.assets.filter((asset) => asset.errors.length);
  bad.forEach((asset) => problems.push(`${asset.name}：${asset.errors[0]}`));

  // 没有错误时，说明现在处于什么模式（不是报错，只是让人心里有数）
  const notes = [];
  if (!problems.length) {
    if (types.has('first_frame') && types.has('last_frame')) {
      notes.push('首尾帧模式：视频严格从第 1 张图开始、到第 2 张图结束。');
    } else if (types.has('first_frame')) {
      notes.push('首帧生视频：视频从这张图开始，结尾由模型发挥。想指定结尾，再点一次「首尾帧」。');
    } else if (hasRef) {
      notes.push('参考生视频：模型参考素材里的形象/动作/音色，不锁定首尾画面。');
    }
  }

  hint.textContent = problems.length ? problems.join('　·　') : notes.join('　·　');
  hint.style.color = problems.length ? 'var(--danger)' : 'var(--text-faint)';
  // 标记这一行现在是校验结果。控件说明的悬停提示看到这个标记就不覆盖它，
  // 免得把红色报错盖掉。
  hint.dataset.source = problems.length ? 'validation' : '';

  $('btn-send').disabled = problems.length > 0;
  return problems;
}

/* ---------------------------------------------------------------- @ 引用素材 */

const promptEl = $('prompt');
const mentionMenu = $('mention-menu');
let mentionStart = -1;

promptEl.addEventListener('input', () => {
  // 用户手动改过，之前整理的结果就作废了，避免发出去的和你看到的不一致
  state.optimizeResult = null;
  state.optimizeFailed = false;

  const caret = promptEl.selectionStart;
  const before = promptEl.value.slice(0, caret);
  const match = before.match(/@([^\s@]*)$/);

  if (!match || !state.assets.length) { hideMention(); return; }

  const labels = computeLabels();
  mentionStart = caret - match[0].length;
  mentionMenu.innerHTML = labels.map((label, index) => `
    <button class="mention-item" data-label="${escapeHtml(label)}">
      <b>${escapeHtml(label)}</b>
      <span class="chip-name">${escapeHtml(state.assets[index].name)}</span>
    </button>`).join('');
  mentionMenu.querySelectorAll('.mention-item').forEach((item) => {
    item.addEventListener('click', () => insertMention(item.dataset.label));
  });
  mentionMenu.hidden = false;
});

promptEl.addEventListener('blur', () => setTimeout(hideMention, 180));

function hideMention() { mentionMenu.hidden = true; }

function insertMention(label) {
  const caret = promptEl.selectionStart;
  const value = promptEl.value;
  promptEl.value = value.slice(0, mentionStart) + label + value.slice(caret);
  const position = mentionStart + label.length;
  promptEl.setSelectionRange(position, position);
  promptEl.focus();
  hideMention();
}

/* ---------------------------------------------------------------- 控制项 */

const composer = document.querySelector('.composer');
const pops = { resolution: $('pop-resolution'), ratio: $('pop-ratio'), duration: $('pop-duration') };

function closePops() { Object.values(pops).forEach((pop) => { pop.hidden = true; }); }

function openPop(key, anchor) {
  const pop = pops[key];
  const alreadyOpen = !pop.hidden;
  closePops();
  if (alreadyOpen) return;

  // 先显示再量尺寸，否则隐藏状态下 offsetWidth 是 0，右边会溢出。
  pop.hidden = false;
  const box = composer.getBoundingClientRect();
  const btn = anchor.getBoundingClientRect();
  const maxLeft = Math.max(0, box.width - pop.offsetWidth);
  pop.style.left = Math.max(0, Math.min(btn.left - box.left, maxLeft)) + 'px';
  pop.style.top = (btn.bottom - box.top + 10) + 'px';
}

document.addEventListener('click', (event) => {
  if (!event.target.closest('.popover') && !event.target.closest('.ctl')) closePops();
});

$('btn-resolution').addEventListener('click', (e) => { renderResolutionPop(); openPop('resolution', e.currentTarget); });
$('btn-ratio').addEventListener('click', (e) => { renderRatioPop(); openPop('ratio', e.currentTarget); });
$('btn-duration').addEventListener('click', (e) => { renderDurationPop(); openPop('duration', e.currentTarget); });

function renderResolutionPop() {
  const pop = pops.resolution;
  pop.innerHTML = `
    <div class="pop-group">
      <div class="pop-title">选择清晰度</div>
      <div class="pill-row">
        ${state.options.resolutions.map((r) => `
          <button class="pill${r === state.resolution ? ' active' : ''}" data-value="${r}">${r}</button>`).join('')}
      </div>
    </div>
    <div class="pop-note">1080P 的单价约是 480P 的 4 倍，长视频差距很明显。</div>`;

  pop.querySelectorAll('[data-value]').forEach((button) => {
    button.addEventListener('click', () => {
      state.resolution = button.dataset.value;
      $('btn-resolution').innerHTML = `<span class="ctl-ico">✳</span>${state.resolution}`;
      closePops();
      refreshEstimate();
    });
  });
}

function renderRatioPop() {
  const pop = pops.ratio;
  pop.innerHTML = `
    <div class="pop-group">
      <div class="pop-title">选择比例</div>
      <div class="ratio-row">
        ${state.options.ratios.map((ratio) => {
          const box = RATIO_BOX[ratio.value];
          const shape = box
            ? `<span class="ratio-shape" style="width:${box[0]}px;height:${box[1]}px"></span>`
            : '<span class="ratio-shape" style="width:19px;height:19px;border-radius:3px"></span>';
          return `<button class="ratio-btn${ratio.value === state.ratio ? ' active' : ''}" data-value="${ratio.value}">
            ${shape}<span>${escapeHtml(ratio.label)}</span></button>`;
        }).join('')}
      </div>
    </div>
    <div class="pop-note">「自动」由模型按输入素材推荐；有参考视频时官方也建议用自动。</div>`;

  pop.querySelectorAll('[data-value]').forEach((button) => {
    button.addEventListener('click', () => {
      state.ratio = button.dataset.value;
      const label = state.options.ratios.find((r) => r.value === state.ratio).label;
      $('btn-ratio').innerHTML = `<span class="ctl-ico">▭</span>${escapeHtml(label)}`;
      closePops();
    });
  });
}

function renderDurationPop() {
  const pop = pops.duration;
  const min = state.options.duration.min;
  const max = state.options.duration.max;

  // 刻度：首尾必有（它们是滑轨的两端），中间挑几个整数感的
  const marks = [min, ...[5, 10, 20].filter((v) => v > min && v < max), max];
  // 换算成滑轨上的百分比位置。滑轨是线性的，不能等分排。
  const pct = (value) => (((value - min) / (max - min)) * 100).toFixed(3);

  pop.innerHTML = `
    <div class="pop-group">
      <div class="pop-title">时长模式</div>
      <div class="pill-row">
        <button class="pill${state.durationMode === 'smart' ? ' active' : ''}" data-mode="smart">智能时长</button>
        <button class="pill${state.durationMode === 'custom' ? ' active' : ''}" data-mode="custom">自定义时长</button>
      </div>
    </div>
    <div class="pop-group" id="dur-slider-group">
      <div class="pop-title">选择时长</div>
      <div class="slider-wrap">
        <div class="slider-bubble" id="dur-bubble">${state.duration} 秒</div>
        <input type="range" id="dur-range" min="${min}" max="${max}" step="1" value="${state.duration}">
        <div class="slider-marks" id="dur-marks">
          ${marks.map((m) => `<span data-mark="${m}" style="left:${pct(m)}%">${m}s</span>`).join('')}
        </div>
      </div>
    </div>
    <div class="pop-note" id="dur-note"></div>`;

  const range = pop.querySelector('#dur-range');
  const group = pop.querySelector('#dur-slider-group');
  group.style.display = state.durationMode === 'smart' ? 'none' : 'block';

  pop.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      state.durationMode = button.dataset.mode;
      renderDurationPop();
      syncDurationButton();
      refreshEstimate();
    });
  });

  const bubble = pop.querySelector('#dur-bubble');

  if (range) {
    range.addEventListener('input', () => {
      state.duration = Number(range.value);
      paintMarks();
      paintBubble();
      syncDurationButton();
      refreshEstimate();
    });
  }

  function paintMarks() {
    pop.querySelectorAll('[data-mark]').forEach((span) => {
      span.classList.toggle('on', Number(span.dataset.mark) === state.duration);
    });
  }

  /** 数值气泡跟着滑块走。刻度只是参照点，中间的值只能靠它读出来。 */
  function paintBubble() {
    const ratio = (state.duration - min) / (max - min);
    bubble.textContent = `${state.duration} 秒`;
    // 和滑块的圆心行程对齐：两端各内缩半个滑块（10px）
    bubble.style.left = `calc(10px + (100% - 20px) * ${ratio.toFixed(4)})`;
  }

  paintMarks();
  paintBubble();

  const note = pop.querySelector('#dur-note');
  if (state.durationMode === 'smart') {
    note.textContent = '智能时长：传 duration = -1，模型按提示词和素材自行推荐时长。'
      + '提交前无法知道实际秒数，成本只能给区间。';
  } else if (inputVideoSeconds() > 0) {
    const allowed = Math.max(0, max - inputVideoSeconds());
    note.textContent = `有参考视频时：输入 ${inputVideoSeconds().toFixed(1)} 秒 + 输出，合计不能超过 30 秒。本条最多 ${allowed} 秒。`;
  } else {
    note.textContent = '无视频输入时，输出时长可以是 2~30 秒的任意整数。';
  }
}

function syncDurationButton() {
  const text = state.durationMode === 'smart' ? '智能时长' : `${state.duration}s`;
  $('btn-duration').innerHTML = `<span class="ctl-ico">◷</span>${text}<span class="caret">▾</span>`;
}

function bindToggle(id, key, onChange) {
  const button = $(id);
  button.addEventListener('click', () => {
    if (button.classList.contains('locked')) return;
    state[key] = !state[key];
    button.classList.toggle('on', state[key]);
    if (onChange) onChange();
    refreshEstimate();
  });
}

bindToggle('btn-audio', 'audio');
bindToggle('btn-extend', 'promptExtend');
bindToggle('btn-watermark', 'watermark');

// 有鼠标的设备上，title 要等一秒才弹出来，容易以为没有说明。
// 这几个开关的含义不看说明猜不出来，所以补一行常驻提示。
const CTL_HINTS = {
  'btn-audio': '声音：模型自己生成台词/BGM/音效，写进提示词即可。想克隆音色可传参考音频。',
  'btn-extend': '智能改写：服务端 prompt_extend，提交时阿里自动改你的提示词。改写结果接口不返回，看不到。',
  'btn-watermark': '水印：官方默认不加「AI生成」角标。',
  'btn-optimize-manual': '整理提示词：本地的一层，把你写的提示词整理成结构化格式，结果会弹出来给你看和改。不点它也能直接生成。',
};
Object.entries(CTL_HINTS).forEach(([id, text]) => {
  $(id).addEventListener('mouseenter', () => {
    const hint = $('mode-hint');
    // 正在显示校验报错时不动它
    if (hint.dataset.source === 'validation') return;
    hint.dataset.source = 'ctl';
    hint.style.color = 'var(--text-faint)';
    hint.textContent = text;
  });
  $(id).addEventListener('mouseleave', () => {
    const hint = $('mode-hint');
    if (hint.dataset.source !== 'ctl') return;
    hint.dataset.source = '';
    hint.textContent = '';
    updateHint();
  });
});

/* ---------------------------------------------------------------- 成本预估 */

let estimateTimer = null;
function refreshEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(runEstimate, 220);
}

async function runEstimate() {
  const duration = state.durationMode === 'smart' ? -1 : state.duration;
  try {
    const data = await api('/api/estimate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: state.settings?.model || 'wan3.0-video',
        resolution: state.resolution,
        duration,
        input_video_seconds: inputVideoSeconds(),
      }),
    });
    state.lastEstimate = data;
    renderCost(data);
  } catch {
    $('cost-box').innerHTML = '<span class="cost-note">成本预估不可用</span>';
  }
}

function renderCost(data) {
  const box = $('cost-box');
  const unit = Number(data.unit_price || 0);
  const unitText = unit ? `¥${unit.toFixed(2)}/秒` : '价格表缺这一档';

  if (!data.price_known) {
    box.innerHTML = `<span class="cost-value warn">—</span><span class="cost-note">${escapeHtml(data.note || unitText)}</span>`;
    return;
  }

  if (data.exact) {
    box.innerHTML =
      `<span class="cost-value">约 ¥${data.estimated.toFixed(2)}</span>` +
      `<span class="cost-note">${unitText} · ${escapeHtml(data.note)}</span>`;
    return;
  }

  if (data.low != null && data.high != null) {
    box.innerHTML =
      `<span class="cost-value smart">¥${data.low.toFixed(2)} ~ ¥${data.high.toFixed(2)}</span>` +
      `<span class="cost-note">${unitText} · 智能时长无法预先确定秒数</span>`;
    return;
  }

  box.innerHTML = `<span class="cost-value warn">—</span><span class="cost-note">${escapeHtml(data.note || '无法预估')}</span>`;
}

/* ---------------------------------------------------------------- 提示词整理 */

$('optimize-close').addEventListener('click', () => { $('optimize-drawer').hidden = true; });
$('optimize-discard').addEventListener('click', () => { $('optimize-drawer').hidden = true; });
$('optimize-apply').addEventListener('click', () => {
  const text = $('optimize-text').value.trim();
  if (!text) { toast('整理结果为空', true); return; }

  promptEl.value = text;

  if (state.optimizeFailed) {
    // 整理没成功，textarea 里放的是用户原文，不能当成「整理后的结果」记下来
    state.optimizeResult = null;
    $('optimize-drawer').hidden = true;
    toast('已跳过提示词整理，将直接用原文生成。');
  } else {
    state.optimizeResult = text;
    $('optimize-drawer').hidden = true;
    toast('已应用整理结果。服务端仍会再做一次智能改写（这个结果看不到）。');
  }
});

async function runOptimize(promptText) {
  const drawer = $('optimize-drawer');
  const textarea = $('optimize-text');
  const applyButton = $('optimize-apply');

  drawer.hidden = false;
  textarea.value = '正在按官方 wan3-pe Skill 整理……';
  textarea.disabled = true;
  applyButton.textContent = '用这段提示词';
  $('optimize-hint').className = 'hint';
  $('optimize-hint').textContent = '调用文本模型，几秒到十几秒';

  try {
    const data = await api('/api/prompt/optimize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: promptText,
        assets: state.assets.map((asset, index) => ({
          kind: asset.kind,
          path: asset.staged_path,
          name: asset.name,
          label: computeLabels()[index],
        })),
      }),
    });
    textarea.value = data.text;
    state.optimizeFailed = false;
    $('optimize-hint').textContent = data.used_vision
      ? `${data.model}（已附上 ${data.images_attached} 张图片读取素材）`
      : `${data.model}（未附图，素材只保留编号引用）`;
  } catch (error) {
    textarea.value = promptText;
    state.optimizeFailed = true;
    // 整理是手动的，失败不影响生成，直接关掉面板照常发就行
    applyButton.textContent = '关掉，直接用原文生成';
    const hint = $('optimize-hint');
    hint.className = 'hint error';
    hint.textContent = error.message;
  } finally {
    textarea.disabled = false;
  }
}

/* ---------------------------------------------------------------- 生成 */

$('btn-send').addEventListener('click', submitJob);
bindManualOptimize();

async function submitJob() {
  const original = promptEl.value.trim();
  const problems = updateHint();

  if (problems.length) { toast(problems[0], true); return; }
  if (!original && !state.assets.length) {
    toast('提示词和参考素材至少要有一个', true);
    return;
  }

  // 发送即生成，中间不插任何步骤 —— 和阿里控制台一致。
  //
  // 「整理提示词」不在流程里，它只在用户点输入框右上角那个按钮时才发生
  // （见 bindManualOptimize）。官方的智能改写由服务端在提交时自动完成，
  // 不需要本地多做任何事。
  const finalPrompt = state.optimizeResult || original;
  await sendFinal(finalPrompt, original);
}

/**
 * 手动触发提示词整理。
 *
 * 刻意做成手动的：控制台没有这一步，把它塞进自动流程会多调一次模型、
 * 多一次确认，而且会掩盖真正的问题（比如模型没权限）。
 */
function bindManualOptimize() {
  const button = $('btn-optimize-manual');
  button.addEventListener('click', async () => {
    if (!state.settings?.prompt_helper?.enabled) {
      toast('提示词整理没启用。要用的话去「设置 → 提示词整理」打开。', true);
      return;
    }
    const text = promptEl.value.trim();
    if (!text && !state.assets.length) {
      toast('先写点提示词，或者加个参考素材。', true);
      return;
    }
    await runOptimize(text);
  });
}

async function sendFinal(promptText, originalText) {
  const button = $('btn-send');
  button.disabled = true;

  try {
    const data = await api('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: promptText,
        original_prompt: originalText,
        optimized_prompt: state.optimizeResult || '',
        optimize_enabled: Boolean(state.optimizeResult),
        model: state.settings?.model || 'wan3.0-video',
        resolution: state.resolution,
        ratio: state.ratio,
        duration: state.durationMode === 'smart' ? -1 : state.duration,
        audio: state.audio,
        seed: -1,
        prompt_extend: state.promptExtend,
        watermark: state.watermark,
        assets: state.assets.map((asset) => ({
          path: asset.staged_path,
          name: asset.name,
          kind: asset.kind,
          media_type: asset.media_type,
          duration: asset.duration,
        })),
      }),
    });

    state.jobId = data.job_id;
    state.optimizeResult = null;
    openJobDrawer();
    pollJob();
  } catch (error) {
    toast(`提交失败：${error.message}`, true);
    button.disabled = false;
  }
}

function openJobDrawer() {
  $('job-drawer').hidden = false;
  $('job-log').innerHTML = '';
  $('job-step').textContent = '排队中';
  $('job-bar').style.width = '0%';
  $('job-download').hidden = true;
  $('job-diagnose').hidden = true;
  $('job-cancel').disabled = false;
}

$('job-close').addEventListener('click', () => {
  $('job-drawer').hidden = true;
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  $('btn-send').disabled = false;
});

$('job-cancel').addEventListener('click', async () => {
  if (!state.jobId) return;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { method: 'POST' });
    toast('已请求取消');
  } catch (error) {
    toast(error.message, true);
  }
});

function pollJob() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (!state.jobId) return;
    try {
      const job = await api(`/api/jobs/${state.jobId}`);
      renderJob(job);
      if (['succeeded', 'failed', 'canceled'].includes(job.status)) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        $('btn-send').disabled = false;
      }
    } catch (error) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      $('btn-send').disabled = false;
      toast(`查询任务失败：${error.message}`, true);
    }
  }, 2000);
}

function renderJob(job) {
  $('job-step').textContent = job.step || job.status;
  $('job-bar').style.width = `${job.progress || 0}%`;

  $('job-log').innerHTML = (job.log || [])
    .map((line) => `<div><span class="t">${escapeHtml(line.time)}</span>${escapeHtml(line.message)}</div>`)
    .join('');

  if (job.status === 'succeeded') {
    $('job-title').textContent = '生成完成';
    $('job-cancel').disabled = true;
    const link = $('job-download');
    link.href = job.video_url;
    link.hidden = false;

    const cost = job.meta?.cost;
    const usage = job.meta?.usage;
    if (usage) {
      const lines = [
        `实际时长 ${usage.output_video_duration ?? '?'} 秒 · 计费 ${usage.duration ?? '?'} 秒`,
        `分辨率 ${usage.SR ?? '?'} · 比例 ${usage.ratio ?? '?'} · ${usage.fps ?? '?'}fps`,
      ];
      if (cost && cost.amount != null) lines.push(`本次花费 约 ¥${Number(cost.amount).toFixed(2)}`);
      lines.push(`水印：${job.meta?.parameters?.watermark ? '已添加' : '未添加'}`);
      $('job-log').insertAdjacentHTML('beforeend', `<div class="job-usage">${escapeHtml(lines.join('\n'))}</div>`);
    }
  } else if (job.status === 'failed') {
    $('job-title').textContent = '生成失败';
    $('job-cancel').disabled = true;
    $('job-diagnose').hidden = false;

    // 失败原因原样显示，另外把 request_id 单独列出来——提工单要用
    const ids = job.meta?.request_ids || {};
    const idLines = Object.entries(ids)
      .filter(([, value]) => value)
      .map(([key, value]) => `${key}：${value}`);

    const detail = [job.error || '未知错误', '', ...idLines].join('\n');
    $('job-log').insertAdjacentHTML('beforeend',
      `<div class="job-usage" style="color:var(--danger)">${escapeHtml(detail)}</div>`);
  }
}

/* ---------------------------------------------------------------- 创作模式 */

/**
 * 模式决定输入区长什么样 —— 这和素材组合规则是一一对应的。
 * 控制台也是这个逻辑：选「首尾帧」才出现两个格子，选「全能参考」出现上传按钮。
 *
 *   frames: both  两个格子都显示
 *           first 只显示首帧
 *           none  不显示格子
 */
const MODE_UI = {
  all:    { frames: 'none',  media: true,  note: '素材任意组合，模型自动理解意图' },
  frames: { frames: 'both',  media: false, note: '严格指定第一帧和最后一帧' },
  i2v:    { frames: 'first', media: false, note: '只放首帧，结尾由模型发挥' },
  t2v:    { frames: 'none',  media: false, note: '不用素材，直接写提示词' },
  r2v:    { frames: 'none',  media: true,  note: '参考素材里的形象 / 动作 / 音色' },
  edit:   { frames: 'none',  media: true,  note: '传一段视频 + 编辑指令' },
};

/** 哪些素材类型属于「参考类」 */
const REFERENCE_TYPES = ['reference_image', 'reference_video', 'reference_audio'];
const FRAME_TYPES = ['first_frame', 'last_frame'];

function setComposerMode(key, options = {}) {
  const ui = MODE_UI[key];
  if (!ui) return;

  // 切到不显示某类素材的模式时，先把不兼容的清掉并告知，
  // 否则用户会以为素材还在、提交后又被拒
  if (!options.keepAssets) {
    const hidden = state.assets.filter((a) => {
      if (REFERENCE_TYPES.includes(a.media_type)) return !ui.media;
      if (FRAME_TYPES.includes(a.media_type)) {
        if (ui.frames === 'none') return true;
        if (ui.frames === 'first' && a.media_type === 'last_frame') return true;
      }
      return false;
    });
    if (hidden.length) {
      const names = hidden.map((a) => a.name).slice(0, 3).join('、');
      const more = hidden.length > 3 ? ` 等 ${hidden.length} 个` : '';
      const ok = window.confirm(
        `切到这个模式后，${names}${more}素材不会再用到。\n\n继续的话会把它们移除，确定吗？`);
      if (!ok) return false;
      state.assets = state.assets.filter((a) => !hidden.includes(a));
    }
  }

  state.mode = key;
  const info = modelData?.modes.find((m) => m.key === key);
  $('mode-chip-name').textContent = info?.name || key;
  $('mode-chip-note').textContent = ui.note;

  $('frame-pair').hidden = ui.frames === 'none';
  // 「只放首帧」模式把尾帧和交换箭头一起藏起来
  $('slot-last').hidden = ui.frames === 'first';
  $('btn-swap-frames').hidden = ui.frames === 'first';
  $('media-pair').hidden = !ui.media;

  renderChips();
  refreshEstimate();
  return true;
}

function openModePicker() {
  openModelPanel();
}

/* ---------------------------------------------------------------- 模型面板 */

/* 对应控制台左上角那个「万相3.0-视频生成 ▾」：看当前模型、看资料和价格、
   切换模型、按创作模式了解需要准备什么素材。 */

let modelData = null;
let mpPicked = null;    // 当前在列表里选中的模型

async function openModelPanel() {
  $('model-panel').hidden = false;
  $('mp-detail').innerHTML = '<div class="bars-empty">读取中…</div>';
  try {
    modelData = await api('/api/models');
    mpPicked = modelData.current;
    renderModelPanel();
  } catch (error) {
    $('mp-detail').innerHTML = `<div class="bars-empty">读取失败：${escapeHtml(error.message)}</div>`;
  }
}

function closeModelPanel() {
  $('model-panel').hidden = true;
}

function renderModelPanel() {
  if (!modelData) return;

  // 模式标签
  // 标签就是创作模式。点一下真的切界面，不只是高亮。
  $('mp-tabs').innerHTML = modelData.modes.map((mode) =>
    `<button class="mp-tab${state.mode === mode.key ? ' active' : ''}" data-mode="${mode.key}">${escapeHtml(mode.name)}</button>`).join('');

  $('mp-tabs').querySelectorAll('[data-mode]').forEach((tab) => {
    tab.addEventListener('click', () => {
      // 返回 false 表示用户在确认框里点了取消
      if (setComposerMode(tab.dataset.mode) === false) return;
      renderModelPanel();
      closeModelPanel();
      const info = modelData.modes.find((m) => m.key === tab.dataset.mode);
      toast(`已切到「${info.name}」模式。${info.setup}`);
    });
  });

  // 模型列表
  const keyword = $('mp-search').value.trim().toLowerCase();
  const models = modelData.models.filter((m) =>
    !keyword
    || m.display.toLowerCase().includes(keyword)
    || m.id.toLowerCase().includes(keyword)
    || (m.tagline || '').toLowerCase().includes(keyword));

  $('mp-list').innerHTML = models.length
    ? models.map((m) => `
      <button class="mp-item${m.id === mpPicked ? ' active' : ''}" data-model="${escapeHtml(m.id)}">
        <span class="mp-item-mark">◈</span>${escapeHtml(m.display)}
      </button>`).join('')
    : '<div class="bars-empty">没有匹配的模型。</div>';

  $('mp-list').querySelectorAll('[data-model]').forEach((item) => {
    item.addEventListener('click', () => {
      mpPicked = item.dataset.model;
      renderModelPanel();
    });
  });

  renderModelDetail();
}

function renderModelDetail() {
  const model = modelData.models.find((m) => m.id === mpPicked);
  if (!model) {
    $('mp-detail').innerHTML = '<div class="bars-empty">选一个模型。</div>';
    return;
  }

  const isCurrent = model.id === modelData.current;
  const mode = modelData.modes.find((m) => m.key === state.mode);
  const quota = modelData.free_quota_seconds;

  const priceRows = model.pricing.map((tier) => {
    const discounted = tier.discount < 1;
    const value = discounted
      ? `<span class="strike">${tier.list}</span><span class="now num">${tier.actual}</span>`
      : `<span class="now num">${tier.actual}</span>`;
    return `<dt>视频生成（${escapeHtml(tier.resolution)}）</dt>
            <dd>${value} 元/每秒${discounted ? `　<span style="color:var(--text-faint);font-size:12px">限时${(tier.discount * 10).toFixed(1)}折</span>` : ''}</dd>`;
  }).join('');

  $('mp-detail').innerHTML = `
    <div class="mp-title">
      <h3>${escapeHtml(model.display)}</h3>
      <button class="mp-copy" data-copy="${escapeHtml(model.id)}">复制模型名</button>
    </div>
    <p class="mp-desc">${escapeHtml(model.description)}</p>

    ${mode ? `
    <div class="mp-note">
      <b>${escapeHtml(mode.name)}</b> —— ${escapeHtml(mode.desc)}<br>
      怎么用：${escapeHtml(mode.setup)}
    </div>` : ''}

    <div class="mp-sub">模型信息</div>
    <dl class="mp-rows">
      <dt>模型 ID</dt><dd>${escapeHtml(model.id)}</dd>
      <dt>输入模态</dt>
      <dd>${model.input_modalities.map((m) => `<span class="pill">${escapeHtml(m)}</span>`).join('')}</dd>
      <dt>输出模态</dt>
      <dd>${model.output_modalities.map((m) => `<span class="pill">${escapeHtml(m)}</span>`).join('')}</dd>
      <dt>最高时长</dt><dd>30 秒（输出 30fps）</dd>
      <dt>免费额度</dt>
      <dd>${quota ? `${quota} 秒（输入+输出合计，仅华北2北京）` : '—'}</dd>
      ${priceRows}
      <dt>RPM</dt><dd class="num">${model.rpm}</dd>
    </dl>

    <div class="mp-actions">
      ${isCurrent
        ? '<span style="font-size:13px;color:var(--ok)">✓ 当前正在用这个模型</span>'
        : `<button class="primary" id="mp-use">用这个模型</button>`}
      <span style="font-size:12.5px;color:var(--text-faint)">价格可在「设置 → 价格表」里改</span>
    </div>`;

  const copyBtn = $('mp-detail').querySelector('[data-copy]');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(copyBtn.dataset.copy);
        toast('模型名已复制');
      } catch {
        toast('复制失败，请手动选中', true);
      }
    });
  }

  const useBtn = $('mp-detail').querySelector('#mp-use');
  if (useBtn) useBtn.addEventListener('click', () => switchModel(model.id));
}

async function switchModel(modelId) {
  try {
    const data = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: modelId }),
    });
    state.settings = { ...state.settings, ...data.settings };
    if (modelData) modelData.current = modelId;
    syncModelLabel();
    renderModelPanel();
    refreshEstimate();
    toast(`已切换到 ${modelId}`);
  } catch (error) {
    toast(`切换失败：${error.message}`, true);
  }
}

/** 顶栏和面板里的当前模型名。 */
function syncModelLabel() {
  const id = state.settings?.model;
  const info = modelData?.models.find((m) => m.id === id);
  $('brand-model-name').textContent = info?.display || id || '万相3.0-视频生成';
}

$('btn-model-panel').addEventListener('click', () => {
  if ($('model-panel').hidden) openModelPanel(); else closeModelPanel();
});

// 输入区里的模式标签，点开同一个面板
$('btn-mode').addEventListener('click', openModePicker);
document.querySelectorAll('[data-mp-close]').forEach((el) => {
  el.addEventListener('click', closeModelPanel);
});
$('mp-search').addEventListener('input', () => {
  if (modelData) renderModelPanel();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('model-panel').hidden) closeModelPanel();
});

/* ---------------------------------------------------------------- 概览 */

let dashData = null;
let dashTimer = null;

async function loadDashboard() {
  try {
    dashData = await api('/api/stats');
  } catch (error) {
    $('dash-tiles').innerHTML = `<div class="tile"><div class="tile-label">读取失败</div>
      <div class="tile-value" style="font-size:15px">${escapeHtml(error.message)}</div></div>`;
    return;
  }
  renderTiles();
  renderDailyChart();
  renderResolutionChart();
  renderTopList();
}

/* -------- 统计块 -------- */

function renderTiles() {
  const t = dashData.totals;

  const tiles = [
    {
      label: '总花费',
      value: `¥${t.cost.toFixed(2)}`,
      note: t.avg_cost != null ? `平均每条 ¥${t.avg_cost.toFixed(2)}` : '还没有数据',
    },
    {
      label: '生成条数',
      value: String(t.count),
      note: t.favorite ? `收藏 ${t.favorite} 条` : ' ',
    },
    {
      label: '总计费时长',
      value: `${t.seconds.toFixed(1)}`,
      unit: '秒',
      note: t.avg_seconds != null ? `平均每条 ${t.avg_seconds} 秒` : ' ',
    },
    {
      label: '成功率',
      value: t.success_rate != null ? `${t.success_rate.toFixed(0)}%` : '—',
      note: t.failed ? `${t.failed} 条失败（不计费）` : '全部成功',
    },
  ];

  $('dash-tiles').innerHTML = tiles.map((tile) => `
    <div class="tile">
      <div class="tile-label">${escapeHtml(tile.label)}</div>
      <div class="tile-value">${escapeHtml(tile.value)}${tile.unit ? `<span class="tile-unit">${escapeHtml(tile.unit)}</span>` : ''}</div>
      <div class="tile-note">${escapeHtml(tile.note || ' ')}</div>
    </div>`).join('');
}

/* -------- 每天花费（纵向柱状图） -------- */

/** 顶端圆角、底端贴基线的柱子。直接写 path 是因为 SVG 的 rect
    没法只圆两个角。 */
function barPath(x, y, w, h, r) {
  if (h <= 0.5) return '';
  const rr = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} `
       + `L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} `
       + `L${x + w},${y + h} Z`;
}

function renderDailyChart() {
  const host = $('dash-daily');
  const daily = dashData.daily || [];

  if (!daily.length) {
    host.innerHTML = '<div class="bars-empty">还没有数据。生成一条视频就会出现。</div>';
    $('dash-daily-sub').textContent = '';
    return;
  }

  const width = Math.max(320, host.clientWidth || 640);
  const height = 200;
  const padTop = 18, padBottom = 26, padLeft = 46, padRight = 8;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const maxCost = Math.max(...daily.map((d) => d.cost), 0.01);
  // 纵轴取一个好看的整刻度
  const step = niceStep(maxCost);
  const yMax = Math.ceil(maxCost / step) * step;

  const n = daily.length;
  // 柱子之间留 2px 的底色缝隙（规范要求），宽度不足以留缝时靠最小宽度兜底
  const slot = plotW / n;
  const barW = Math.max(3, Math.min(46, slot - 2));

  const yOf = (v) => padTop + plotH - (v / yMax) * plotH;

  const gridLines = [];
  for (let v = 0; v <= yMax + 1e-9; v += step) {
    const y = yOf(v);
    gridLines.push(`<line class="grid-line" x1="${padLeft}" y1="${y.toFixed(1)}" x2="${width - padRight}" y2="${y.toFixed(1)}"/>`);
    // 刻度上 0 就写 ¥0，别写成 ¥0.00
    const label = v === 0 ? '¥0' : `¥${step < 1 ? v.toFixed(2) : v.toFixed(0)}`;
    gridLines.push(`<text class="tick" x="${padLeft - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${label}</text>`);
  }

  // 日期标签太密就抽稀，避免叠字
  const labelEvery = Math.ceil(n / Math.max(1, Math.floor(plotW / 64)));
  const xLabels = daily.map((d, i) => {
    if (i % labelEvery !== 0 && i !== n - 1) return '';
    const x = padLeft + slot * i + slot / 2;
    const short = d.date.slice(5).replace('-', '/');
    return `<text class="tick" x="${x.toFixed(1)}" y="${height - 8}" text-anchor="middle">${short}</text>`;
  }).join('');

  const bars = daily.map((d, i) => {
    const x = padLeft + slot * i + (slot - barW) / 2;
    const y = yOf(d.cost);
    const h = padTop + plotH - y;
    const path = barPath(x, y, barW, h, 4);
    const tip = `${d.date}\n¥${d.cost.toFixed(2)} · ${d.count} 条`;
    // 命中区覆盖整列（远大于柱子本身），鼠标不用精准对准
    return `<g class="col" role="img"
              aria-label="${escapeHtml(d.date)} 花费 ${d.cost.toFixed(2)} 元，${d.count} 条">
      <rect class="bar-hit" x="${(padLeft + slot * i).toFixed(1)}" y="${padTop}"
            width="${slot.toFixed(1)}" height="${plotH}" data-tip="${escapeHtml(tip)}"/>
      ${path ? `<path class="bar" d="${path}"/>` : ''}
    </g>`;
  }).join('');

  host.innerHTML = `
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img"
         aria-label="每天花费柱状图，共 ${n} 天">
      ${gridLines.join('')}
      <line class="axis-line" x1="${padLeft}" y1="${padTop + plotH}" x2="${width - padRight}" y2="${padTop + plotH}"/>
      ${bars}
      ${xLabels}
    </svg>`;

  const total = daily.reduce((sum, d) => sum + d.cost, 0);
  const days = daily.filter((d) => d.count > 0).length;
  $('dash-daily-sub').textContent = `${days} 天有生成 · 合计 ¥${total.toFixed(2)}`;

  bindVizTips(host);
}

/** 把最大值取成一个好读的刻度（1/2/2.5/5 × 10^n）。 */
function niceStep(max) {
  const raw = max / 3;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const pick = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return pick * mag;
}

/* -------- 按分辨率（横向条形） -------- */

const RES_ORDER = ['480P', '720P', '1080P'];

function renderResolutionChart() {
  const host = $('dash-resolution');
  const rows = dashData.by_resolution || [];

  if (!rows.length) {
    host.innerHTML = '<div class="bars-empty">还没有数据。</div>';
    return;
  }

  const total = rows.reduce((sum, r) => sum + r.cost, 0) || 1;
  const max = Math.max(...rows.map((r) => r.cost), 0.01);
  // 序数梯度：越贵/越高的分辨率颜色越深
  const ramp = { '480P': 'var(--viz-ord-1)', '720P': 'var(--viz-ord-2)', '1080P': 'var(--viz-ord-3)' };
  const fallback = 'var(--viz-ord-2)';

  host.innerHTML = `<div class="hbars">${rows.map((row) => {
    const pct = (row.cost / max) * 100;
    const share = (row.cost / total) * 100;
    return `
      <div class="hbar-row">
        <span class="hbar-name">${escapeHtml(row.resolution)}</span>
        <span class="hbar-track"><span class="hbar-fill" style="width:${pct.toFixed(1)}%;background:${ramp[row.resolution] || fallback}"></span></span>
        <span class="hbar-value">¥${row.cost.toFixed(2)} <small>${share.toFixed(0)}% · ${row.count}条</small></span>
      </div>`;
  }).join('')}</div>`;
}

/* -------- 最贵的几条 -------- */

function renderTopList() {
  const host = $('dash-top');
  const rows = dashData.top_costs || [];

  if (!rows.length) {
    host.innerHTML = '<div class="bars-empty">还没有花钱的生成记录。</div>';
    return;
  }

  host.innerHTML = rows.map((row) => `
    <div class="top-row" data-goto="${escapeHtml(row.dir)}">
      <span class="top-prompt">${escapeHtml(row.prompt || '(无提示词)')}</span>
      <span class="top-cost">¥${Number(row.cost || 0).toFixed(2)}
        <small style="color:var(--viz-muted)">${escapeHtml(row.resolution || '')} · ${row.billable_seconds || 0}s</small>
      </span>
    </div>`).join('');

  host.querySelectorAll('[data-goto]').forEach((row) => {
    row.addEventListener('click', () => {
      document.querySelector('[data-view="history"]').click();
      toast('已切到资产页，找到那条点开看详情。');
    });
  });
}

/* -------- 悬停提示 -------- */

let vizTip = null;

function bindVizTips(root) {
  root.querySelectorAll('[data-tip]').forEach((el) => {
    el.addEventListener('mouseenter', () => {
      if (!vizTip) {
        vizTip = document.createElement('div');
        vizTip.className = 'viz-tip';
        document.body.appendChild(vizTip);
      }
      vizTip.textContent = el.dataset.tip;
      vizTip.hidden = false;
    });
    el.addEventListener('mousemove', (event) => {
      if (!vizTip || vizTip.hidden) return;
      vizTip.style.left = `${Math.min(event.clientX + 12, window.innerWidth - 190)}px`;
      vizTip.style.top = `${event.clientY - 42}px`;
    });
    el.addEventListener('mouseleave', () => { if (vizTip) vizTip.hidden = true; });
  });
}

/* ---------------------------------------------------------------- 后处理 · 视频超清 */

let upscaleSource = null;      // { type: 'asset'|'staged', value, label }
let upscaleJobId = null;
let upscaleTimer = null;
let postTabsReady = false;

function initPostTabs() {
  if (postTabsReady) return;
  postTabsReady = true;

  document.querySelectorAll('.post-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.post-tab').forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.post-pane').forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      $(`pane-${tab.dataset.tool}`).classList.add('active');
      if (tab.dataset.tool === 'canvas') initCanvasTab();
      if (tab.dataset.tool === 'watermark') {
        initWatermarkTab();
        loadWatermarkSources();
        loadWatermarkHistory();
      }
    });
  });

  // 目标分辨率
  $('ups-target').querySelectorAll('[data-target]').forEach((btn) => {
    btn.addEventListener('click', () => {
      $('ups-target').querySelectorAll('[data-target]').forEach((b) => b.classList.remove('on'));
      btn.classList.add('on');
    });
  });

  // 资产下拉
  $('ups-source').addEventListener('change', (event) => {
    const value = event.target.value;
    if (!value) {
      upscaleSource = null;
      $('ups-chosen').textContent = '还没选';
      return;
    }
    upscaleSource = { type: 'asset', value, label: value };
    $('ups-chosen').textContent = value.slice(0, 34);
  });

  // 上传本地视频
  $('ups-pick').addEventListener('click', () => $('ups-file').click());
  $('ups-file').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;

    $('ups-chosen').textContent = `上传中… ${file.name}`;
    const form = new FormData();
    form.append('files', file);
    try {
      const data = await api('/api/staging', { method: 'POST', body: form });
      const info = data.files[0];
      upscaleSource = { type: 'staged', value: info.staged_path, label: info.name };
      $('ups-chosen').textContent = `${info.name}（${info.width}×${info.height}）`;
      $('ups-source').value = '';
    } catch (error) {
      $('ups-chosen').textContent = '上传失败';
      toast(`上传失败：${error.message}`, true);
    }
  });

  $('ups-run').addEventListener('click', runUpscale);
}

async function loadUpscaleSources() {
  try {
    const data = await api('/api/assets?hours=1');
    const withVideo = (data.assets || []).filter((a) => a.has_video);
    $('ups-source').innerHTML = '<option value="">从资产里选…</option>' +
      withVideo.map((a) => {
        const label = `${(a.prompt || a.dir).slice(0, 26)} · ${a.resolution || '?'}`;
        return `<option value="${escapeHtml(a.dir)}">${escapeHtml(label)}</option>`;
      }).join('');
  } catch {
    /* 资产读不到不影响上传本地视频 */
  }
}

async function runUpscale() {
  if (!upscaleSource) {
    toast('先选一个来源：从资产里挑，或者上传本地视频', true);
    return;
  }

  const target = $('ups-target').querySelector('.on')?.dataset.target || 'native';
  const model = $('ups-model').value;
  const maxTemp = Number($('ups-max-temp').value) || 0;
  const maxUtil = Number($('ups-max-util').value) || 0;

  $('ups-run').disabled = true;
  $('ups-result').hidden = true;
  $('ups-progress').hidden = false;
  $('ups-bar').style.width = '0%';
  $('ups-stage').textContent = '提交中…';

  try {
    const job = await api('/api/upscale', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_type: upscaleSource.type,
        source: upscaleSource.value,
        target,
        scale: 2,
        model,
        max_temp: maxTemp,
        max_util: maxUtil,
      }),
    });
    upscaleJobId = job.id;
    pollUpscale();
  } catch (error) {
    $('ups-progress').hidden = true;
    $('ups-run').disabled = false;
    toast(`启动失败：${error.message}`, true);
  }
}

function pollUpscale() {
  clearInterval(upscaleTimer);
  upscaleTimer = setInterval(async () => {
    if (!upscaleJobId) return;
    try {
      const job = await api(`/api/upscale/${upscaleJobId}`);
      $('ups-bar').style.width = `${job.progress}%`;
      $('ups-stage').textContent = stageText(job);

      if (job.status === 'done') {
        clearInterval(upscaleTimer);
        upscaleTimer = null;
        $('ups-run').disabled = false;
        renderUpscaleResult(job);
        const mins = (job.result.elapsed / 60).toFixed(0);
        toast(`处理完成：${job.result.width}×${job.result.height}，${job.result.frames} 帧，用时 ${mins} 分钟`);
        loadAssets();       // 资产里的视频多了一版，刷新一下
        loadUpscaleHistory();
      } else if (job.status === 'failed') {
        clearInterval(upscaleTimer);
        upscaleTimer = null;
        $('ups-run').disabled = false;
        $('ups-stage').textContent = `失败：${job.error || '未知原因'}`;
        toast(`处理失败：${job.error || '未知原因'}`, true);
        loadUpscaleHistory();
      }
    } catch (error) {
      clearInterval(upscaleTimer);
      upscaleTimer = null;
      $('ups-run').disabled = false;
      $('ups-stage').textContent = `查询失败：${error.message}`;
    }
  }, 1200);
}

function stageText(job) {
  const parts = [job.stage || '处理中'];
  if (job.total > 0 && job.status !== 'done') {
    parts.push(`${job.done}/${job.total} 帧`);
    if (job.eta > 0) parts.push(`还要约 ${Math.ceil(job.eta / 60)} 分钟`);
  }
  return parts.join('　');
}

function renderUpscaleResult(job) {
  const r = job.result || {};
  const box = $('ups-result');
  box.hidden = false;
  const t = r.throttle || {};
  const sameSize = r.width === r.source.width && r.height === r.source.height;
  box.innerHTML = `
    <div class="ups-result-grid">
      <div>
        <video src="/api/upscale/${job.id}/video" controls preload="metadata"></video>
        <div class="ups-caption">结果 ${r.width}×${r.height}${sameSize ? '（分辨率未变，画质提升）' : ''}</div>
      </div>
      ${job.comparison ? `
      <div>
        <a href="/api/upscale/${job.id}/comparison" target="_blank" title="点开看大图">
          <img src="/api/upscale/${job.id}/comparison" alt="对比图">
        </a>
        <div class="ups-caption">左：原图　右：处理后（100% 像素，点开看大图）</div>
      </div>` : ''}
      <div>
        <div class="ups-result-meta">
          原始　<b>${r.source.width}×${r.source.height}</b>　${r.source.duration.toFixed(1)} 秒　${r.source.frames} 帧<br>
          输出　<b>${r.width}×${r.height}</b>　${(r.size / 1048576).toFixed(1)} MB　${r.frames} 帧<br>
          放大　×${r.scale} 超采样后缩回<br>
          处理　用时 <b>${(r.elapsed / 60).toFixed(1)} 分钟</b><br>
          ${t.gpu_available ? `显卡　温度峰值 ${t.peak_temp}°C　平均占用 ${t.mean_util}%<br>` : ''}
          音频　${r.has_audio ? '已保留' : '原视频无音频'}
        </div>
        <div class="ups-result-actions">
          <a href="/api/upscale/${job.id}/video" download>下载结果</a>
          ${job.comparison ? `<a href="/api/upscale/${job.id}/comparison" download>下载对比图</a>` : ''}
        </div>
      </div>
    </div>`;
}

/** 历史超清结果。服务重启后任务记录从磁盘读回来，成品文件永远有入口。 */
async function loadUpscaleHistory() {
  let data;
  try {
    data = await api('/api/upscale/history');
  } catch {
    return;
  }
  renderUpscaleHistory(data.jobs || []);
}

function renderUpscaleHistory(jobs) {
  const box = $('ups-history');
  const list = $('ups-history-list');
  const usable = jobs.filter((j) => j.has_video || j.status === 'failed');
  if (!usable.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  list.innerHTML = usable.map((j) => {
    const ok = j.status === 'done' && j.has_video;
    const r = j.result || {};
    const meta = [
      j.created_at,
      r.width ? `${r.width}×${r.height}` : '',
      r.size ? `${(r.size / 1048576).toFixed(1)} MB` : '',
    ].filter(Boolean).join('　');
    return `
      <div class="ups-history-item${ok ? '' : ' bad'}">
        <div class="ups-history-main">
          <b>${escapeHtml(j.source_label || j.source)}</b>
          <span class="ups-history-sub">${escapeHtml(meta)}</span>
          ${ok ? '' : `<span class="ups-history-err">${escapeHtml(j.error || j.stage || '未完成')}</span>`}
        </div>
        <div class="ups-history-actions">
          ${ok ? `<a href="/api/upscale/${j.id}/video" target="_blank">播放</a>
                  <a href="/api/upscale/${j.id}/video" download>下载</a>` : ''}
          ${j.comparison ? `<a href="/api/upscale/${j.id}/comparison" target="_blank">对比图</a>` : ''}
          <button class="ghost sm" data-forget="${j.id}">移除记录</button>
        </div>
      </div>`;
  }).join('');

  list.querySelectorAll('[data-forget]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await api(`/api/upscale/${btn.dataset.forget}`, { method: 'DELETE' });
        loadUpscaleHistory();
      } catch (error) {
        toast(error.message, true);
      }
    });
  });
}

/** 资产卡片上的「超清」按钮：直接跑，跳到后处理看进度。 */
async function quickUpscale(dir) {
  upscaleSource = { type: 'asset', value: dir, label: dir };
  document.querySelector('[data-view="canvas"]').click();
  document.querySelector('.post-tab[data-tool="upscale"]').click();
  $('ups-chosen').textContent = dir.slice(0, 34);
  await loadUpscaleSources();
  runUpscale();
}

/* ---------------------------------------------------------------- 后处理 · AI 去水印工作台 */

/**
 * 这是 video-subtitle-remover 桌面界面的网页版：
 * 左边是视频/图片预览 + 时间轴 + 可拖动多选框，右边是算法、检测模型、
 * STTN/ProPainter 高级参数、A/B 区间和任务队列。真正执行仍然调用
 * video-subtitle-remover 的 SubtitleRemover，所以行为和桌面版一致。
 */

const WM_PRESETS = {
  doubao: [0.660, 0.885, 1.000, 1.000],
  wanxiang: [0.740, 0.895, 1.000, 1.000],
  liblib: [0.740, 0.905, 1.000, 1.000],
  jimeng: [0.720, 0.900, 1.000, 1.000],
  kling: [0.720, 0.900, 1.000, 1.000],
  hailuo: [0.720, 0.900, 1.000, 1.000],
  'generic-br': [0.700, 0.885, 1.000, 1.000],
  'generic-bl': [0.000, 0.885, 0.300, 1.000],
  'generic-tr': [0.700, 0.000, 1.000, 0.115],
  'generic-tl': [0.000, 0.000, 0.300, 0.115],
};

const WM_ADV_DEFAULTS = {
  sttnNeighborStride: 5,
  sttnReferenceLength: 10,
  sttnMaxLoadNum: 50,
  propainterMaxLoadNum: 24,
  subtitleAreaDeviationPixel: 10,
  subtitleYXAxisDifferencePixel: 10,
  subtitleAreaYAxisDifferencePixel: 20,
  subtitleAreaPixelToleranceXPixel: 20,
  subtitleAreaPixelToleranceYPixel: 20,
  subtitleTimelineBackwardFrameCount: 3,
  subtitleTimelineForwardFrameCount: 3,
};

const WM_MODE_LABELS = {
  'sttn-auto': 'STTN 智能擦除',
  'sttn-det': 'STTN + 文字检测',
  'lama': 'LAMA + 文字检测',
  'lama-direct': 'LAMA 直接框选',
  'propainter': 'ProPainter',
  'opencv': 'OpenCV',
};

const WM_MODE_INPUTS = {
  sttnNeighborStride: 'wm-sttn-stride',
  sttnReferenceLength: 'wm-sttn-ref',
  sttnMaxLoadNum: 'wm-sttn-load',
  propainterMaxLoadNum: 'wm-pp-load',
  subtitleAreaDeviationPixel: 'wm-dev-px',
  subtitleYXAxisDifferencePixel: 'wm-yx-diff',
  subtitleAreaYAxisDifferencePixel: 'wm-y-diff',
  subtitleAreaPixelToleranceXPixel: 'wm-tol-x',
  subtitleAreaPixelToleranceYPixel: 'wm-tol-y',
  subtitleTimelineBackwardFrameCount: 'wm-back',
  subtitleTimelineForwardFrameCount: 'wm-forward',
};

const wmState = {
  source: null,
  media: { type: '', width: 0, height: 0, duration: 0 },
  areas: [],
  active: -1,
  draft: null,
  drag: null,
  ab: { a: null, b: null },
  running: false,
  busy: false,
  jobId: null,
  jobKind: 'run',
  timer: null,
  queue: [],
  queueSeq: 0,
  activeQueueId: null,
  queueRunning: false,
  ready: false,
};

const wmClamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));

function wmFormatTime(seconds) {
  if (!Number.isFinite(seconds)) return '00:00.0';
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
}

function wmNormalizeArea(a) {
  let x1 = wmClamp(Number(a.x1 ?? a[0]));
  let y1 = wmClamp(Number(a.y1 ?? a[1]));
  let x2 = wmClamp(Number(a.x2 ?? a[2]));
  let y2 = wmClamp(Number(a.y2 ?? a[3]));
  if (x1 > x2) [x1, x2] = [x2, x1];
  if (y1 > y2) [y1, y2] = [y2, y1];
  return { x1, y1, x2, y2 };
}

function wmAreaFromArray(a) {
  return { x1: Number(a[0]), y1: Number(a[1]), x2: Number(a[2]), y2: Number(a[3]) };
}

function wmMediaInfo() {
  const video = $('wm-video');
  const image = $('wm-image');
  if (video && !video.hidden && video.videoWidth) {
    return {
      type: 'video', width: video.videoWidth, height: video.videoHeight,
      duration: video.duration || 0,
    };
  }
  if (image && !image.hidden && image.naturalWidth) {
    return {
      type: 'image', width: image.naturalWidth, height: image.naturalHeight,
      duration: 0,
    };
  }
  return { type: '', width: 0, height: 0, duration: 0 };
}

function wmContentRect(sw, sh, mw, mh) {
  if (!mw || !mh) return { x: 0, y: 0, w: sw, h: sh };
  const aspect = mw / mh;
  let w, h;
  if (sw / sh > aspect) {
    h = sh;
    w = sh * aspect;
  } else {
    w = sw;
    h = sw / aspect;
  }
  return { x: (sw - w) / 2, y: (sh - h) / 2, w, h };
}

function wmStageGeometry() {
  const stage = $('wm-stage');
  const sw = stage.clientWidth;
  const sh = stage.clientHeight;
  const info = wmMediaInfo();
  const content = wmContentRect(sw, sh, info.width, info.height);
  return { sw, sh, content, info };
}

function wmSetLog(lines) {
  const el = $('wm-log');
  if (!el) return;
  el.textContent = (lines || []).join('\n');
  el.scrollTop = el.scrollHeight;
}

function wmAppendLog(line) {
  const el = $('wm-log');
  if (!el) return;
  el.textContent += (el.textContent ? '\n' : '') + line;
  el.scrollTop = el.scrollHeight;
}

function wmSetBusy(busy) {
  wmState.busy = busy;
  const ids = [
    'wm-source', 'wm-pick', 'wm-preset', 'wm-detect', 'wm-auto-detect',
    'wm-mode', 'wm-detect-model', 'wm-hw', 'wm-run', 'wm-add-queue',
    'wm-start-queue', 'wm-clear-queue', 'wm-reset-adv', 'wm-open-gui',
    ...Object.values(WM_MODE_INPUTS),
  ];
  ids.forEach((id) => {
    const el = $(id);
    if (el) el.disabled = busy;
  });
  $('wm-stop').disabled = !busy;
  if (!busy) wmUpdateModeForMedia(wmState.media.type || 'video');
}

function wmUpdateModeForMedia(type) {
  const select = $('wm-mode');
  if (!select) return;
  const image = type === 'image';
  [...select.options].forEach((opt) => {
    opt.disabled = image && !['lama', 'lama-direct'].includes(opt.value);
  });
  if (image && !['lama', 'lama-direct'].includes(select.value)) {
    // 图片默认直接按框重绘：比 OCR 模式多去掉紧挨文字的 logo
    select.value = 'lama-direct';
  }
}

function wmRenderSelectionList() {
  const list = $('wm-selection-list');
  const count = $('wm-sel-count');
  if (count) count.textContent = String(wmState.areas.length);
  if (!list) return;
  if (!wmState.areas.length) {
    list.innerHTML = '<div class="ups-note">还没有选区，直接在画面上拖动框选。</div>';
    return;
  }
  list.innerHTML = wmState.areas.map((a, i) => {
    const label = `${a.x1.toFixed(3)}, ${a.y1.toFixed(3)} → ${a.x2.toFixed(3)}, ${a.y2.toFixed(3)}`;
    return `<div class="wm-sel-item${i === wmState.active ? ' on' : ''}" data-wm-sel="${i}">
      <b>#${i + 1}</b><span>${label}</span>
      <button data-wm-del="${i}" title="删除这个选区">✕</button>
    </div>`;
  }).join('');

  list.querySelectorAll('[data-wm-sel]').forEach((item) => {
    item.addEventListener('click', (event) => {
      if (event.target.closest('[data-wm-del]')) return;
      wmState.active = Number(item.dataset.wmSel);
      wmDraw();
      wmRenderSelectionList();
    });
  });
  list.querySelectorAll('[data-wm-del]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      wmState.areas.splice(Number(btn.dataset.wmDel), 1);
      wmState.active = Math.min(wmState.active, wmState.areas.length - 1);
      wmDraw();
      wmRenderSelectionList();
    });
  });
}

function wmDraw() {
  const canvas = $('wm-canvas');
  const stage = $('wm-stage');
  if (!canvas || !stage) return;
  const dpr = window.devicePixelRatio || 1;
  const sw = stage.clientWidth;
  const sh = stage.clientHeight;
  const width = Math.max(1, Math.round(sw * dpr));
  const height = Math.max(1, Math.round(sh * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, sw, sh);

  const info = wmMediaInfo();
  if (!info.width || !info.height) return;
  const c = wmContentRect(sw, sh, info.width, info.height);

  const drawBox = (a, active, label) => {
    const x = c.x + a.x1 * c.w;
    const y = c.y + a.y1 * c.h;
    const w = Math.max(1, (a.x2 - a.x1) * c.w);
    const h = Math.max(1, (a.y2 - a.y1) * c.h);
    ctx.save();
    ctx.lineWidth = active ? 2.5 : 2;
    ctx.strokeStyle = active ? '#1677ff' : '#ff4d4f';
    ctx.fillStyle = active ? 'rgba(22,119,255,.08)' : 'rgba(255,77,79,.05)';
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);

    if (label) {
      ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif';
      const text = `#${label}`;
      const tw = ctx.measureText(text).width + 10;
      ctx.fillStyle = active ? '#1677ff' : '#ff4d4f';
      ctx.fillRect(x, Math.max(0, y - 17), tw, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(text, x + 5, Math.max(11, y - 5));
    }

    if (active) {
      const handles = [
        [x, y], [x + w / 2, y], [x + w, y],
        [x, y + h / 2], [x + w, y + h / 2],
        [x, y + h], [x + w / 2, y + h], [x + w, y + h],
      ];
      ctx.fillStyle = '#fff';
      ctx.strokeStyle = '#1677ff';
      ctx.lineWidth = 1.5;
      handles.forEach(([hx, hy]) => {
        ctx.beginPath();
        ctx.rect(hx - 4, hy - 4, 8, 8);
        ctx.fill();
        ctx.stroke();
      });
    }
    ctx.restore();
  };

  wmState.areas.forEach((a, i) => drawBox(a, i === wmState.active, i + 1));
  if (wmState.draft) drawBox(wmState.draft, true, '新');
}

function wmSetAreas(areas) {
  wmState.areas = (areas || []).map(wmNormalizeArea).filter(
    (a) => a.x2 - a.x1 > 0.002 && a.y2 - a.y1 > 0.002,
  );
  wmState.active = wmState.areas.length ? wmState.areas.length - 1 : -1;
  wmDraw();
  wmRenderSelectionList();
}

function wmCanvasPoint(event) {
  const canvas = $('wm-canvas');
  const stage = $('wm-stage');
  const info = wmMediaInfo();
  if (!info.width || !info.height) return null;
  const rect = canvas.getBoundingClientRect();
  const px = event.clientX - rect.left;
  const py = event.clientY - rect.top;
  const c = wmContentRect(stage.clientWidth, stage.clientHeight, info.width, info.height);
  if (px < c.x || px > c.x + c.w || py < c.y || py > c.y + c.h) return null;
  return {
    x: wmClamp((px - c.x) / c.w),
    y: wmClamp((py - c.y) / c.h),
    px,
    py,
  };
}

function wmHandleAt(px, py) {
  if (wmState.active < 0 || !wmState.areas[wmState.active]) return null;
  const info = wmMediaInfo();
  const stage = $('wm-stage');
  const a = wmState.areas[wmState.active];
  const c = wmContentRect(stage.clientWidth, stage.clientHeight, info.width, info.height);
  const x1 = c.x + a.x1 * c.w;
  const x2 = c.x + a.x2 * c.w;
  const y1 = c.y + a.y1 * c.h;
  const y2 = c.y + a.y2 * c.h;
  const tol = 10;
  const nl = Math.abs(px - x1) <= tol;
  const nr = Math.abs(px - x2) <= tol;
  const nt = Math.abs(py - y1) <= tol;
  const nb = Math.abs(py - y2) <= tol;
  const inX = px >= x1 - tol && px <= x2 + tol;
  const inY = py >= y1 - tol && py <= y2 + tol;
  if (nl && nt) return 'nw';
  if (nr && nt) return 'ne';
  if (nl && nb) return 'sw';
  if (nr && nb) return 'se';
  if (nl && inY) return 'w';
  if (nr && inY) return 'e';
  if (nt && inX) return 'n';
  if (nb && inX) return 's';
  return null;
}

function wmBoxAt(p) {
  for (let i = wmState.areas.length - 1; i >= 0; i -= 1) {
    const a = wmState.areas[i];
    if (p.x >= a.x1 && p.x <= a.x2 && p.y >= a.y1 && p.y <= a.y2) return i;
  }
  return -1;
}

function wmApplyMoveDrag(p) {
  const d = wmState.drag;
  const a = wmState.areas[d.index];
  if (!a) return;
  const dx = p.x - d.start.x;
  const dy = p.y - d.start.y;
  const width = d.orig.x2 - d.orig.x1;
  const height = d.orig.y2 - d.orig.y1;
  const x1 = wmClamp(d.orig.x1 + dx, 0, 1 - width);
  const y1 = wmClamp(d.orig.y1 + dy, 0, 1 - height);
  wmState.areas[d.index] = { x1, y1, x2: x1 + width, y2: y1 + height };
}

function wmApplyResizeDrag(p) {
  const d = wmState.drag;
  const a = wmState.areas[d.index];
  if (!a) return;
  const min = 0.005;
  let { x1, y1, x2, y2 } = d.orig;
  if (d.handle.includes('w')) x1 = wmClamp(p.x, 0, x2 - min);
  if (d.handle.includes('e')) x2 = wmClamp(p.x, x1 + min, 1);
  if (d.handle.includes('n')) y1 = wmClamp(p.y, 0, y2 - min);
  if (d.handle.includes('s')) y2 = wmClamp(p.y, y1 + min, 1);
  wmState.areas[d.index] = { x1, y1, x2, y2 };
}

function wmPointerDown(event) {
  if (wmState.busy) return;
  const p = wmCanvasPoint(event);
  if (!p) return;
  const canvas = $('wm-canvas');
  canvas.focus();
  canvas.setPointerCapture?.(event.pointerId);

  const handle = wmHandleAt(p.px, p.py);
  if (handle) {
    wmState.drag = {
      kind: 'resize', handle, index: wmState.active, orig: { ...wmState.areas[wmState.active] },
    };
    return;
  }

  const hit = wmBoxAt(p);
  if (hit >= 0) {
    wmState.active = hit;
    wmState.drag = { kind: 'move', index: hit, start: p, orig: { ...wmState.areas[hit] } };
    wmDraw();
    wmRenderSelectionList();
    return;
  }

  wmState.active = -1;
  wmState.draft = { x1: p.x, y1: p.y, x2: p.x, y2: p.y };
  wmState.drag = { kind: 'new', start: p };
  wmDraw();
  wmRenderSelectionList();
}

function wmPointerMove(event) {
  if (!wmState.drag) return;
  const p = wmCanvasPoint(event);
  if (!p) return;
  if (wmState.drag.kind === 'new') {
    wmState.draft = {
      x1: Math.min(wmState.drag.start.x, p.x),
      y1: Math.min(wmState.drag.start.y, p.y),
      x2: Math.max(wmState.drag.start.x, p.x),
      y2: Math.max(wmState.drag.start.y, p.y),
    };
  } else if (wmState.drag.kind === 'move') {
    wmApplyMoveDrag(p);
  } else if (wmState.drag.kind === 'resize') {
    wmApplyResizeDrag(p);
  }
  wmDraw();
}

function wmPointerUp() {
  if (!wmState.drag) return;
  const kind = wmState.drag.kind;
  if (kind === 'new' && wmState.draft) {
    const a = wmNormalizeArea(wmState.draft);
    if (a.x2 - a.x1 > 0.004 && a.y2 - a.y1 > 0.004) {
      wmState.areas.push(a);
      wmState.active = wmState.areas.length - 1;
      const preset = $('wm-preset');
      if (preset) preset.value = 'auto';
    }
  }
  wmState.drag = null;
  wmState.draft = null;
  wmDraw();
  wmRenderSelectionList();
}

function wmPointerCancel() {
  wmState.drag = null;
  wmState.draft = null;
  wmDraw();
}

function wmCurrentAdvanced() {
  const result = {};
  Object.entries(WM_MODE_INPUTS).forEach(([key, id]) => {
    const value = Number($(id)?.value);
    if (Number.isFinite(value)) result[key] = value;
  });
  return result;
}

function wmApplyAdvanced(values) {
  const source = { ...WM_ADV_DEFAULTS, ...(values || {}) };
  Object.entries(WM_MODE_INPUTS).forEach(([key, id]) => {
    const el = $(id);
    if (el) el.value = String(source[key] ?? WM_ADV_DEFAULTS[key]);
  });
}

function wmSavePrefs() {
  try {
    localStorage.setItem('wan3-wm-prefs', JSON.stringify({
      mode: $('wm-mode')?.value,
      detectModel: $('wm-detect-model')?.value,
      hw: $('wm-hw')?.checked,
      advanced: wmCurrentAdvanced(),
    }));
  } catch { /* 忽略隐私模式下的 localStorage 限制 */ }
}

function wmLoadPrefs() {
  try {
    const raw = localStorage.getItem('wan3-wm-prefs');
    if (!raw) return;
    const prefs = JSON.parse(raw);
    if (prefs.mode && $(`wm-mode`).querySelector(`option[value="${prefs.mode}"]`)) {
      $('wm-mode').value = prefs.mode;
    }
    if (prefs.detectModel) $('wm-detect-model').value = prefs.detectModel;
    if (typeof prefs.hw === 'boolean') $('wm-hw').checked = prefs.hw;
    wmApplyAdvanced(prefs.advanced);
  } catch { /* 损坏的偏好直接忽略 */ }
}

function wmSourceLabel(source) {
  if (!source) return '还没选';
  return `${source.label}${source.media === 'image' ? ' · 图片' : ' · 视频'}`;
}

function wmSetSource(source) {
  wmState.source = source;
  wmState.areas = [];
  wmState.active = -1;
  wmState.draft = null;
  wmState.drag = null;
  wmState.ab = { a: null, b: null };
  wmState.media = { type: '', width: 0, height: 0, duration: 0 };
  $('wm-result').hidden = true;
  $('wm-live').hidden = true;
  $('wm-progress').hidden = true;
  wmSetLog([]);
  wmRenderSelectionList();
  wmUpdateAbLabel();
  wmDraw();

  const video = $('wm-video');
  const image = $('wm-image');
  video.pause();
  video.removeAttribute('src');
  video.load();
  video.hidden = true;
  image.hidden = true;
  image.removeAttribute('src');
  $('wm-empty').hidden = false;
  $('wm-empty').textContent = '正在加载…';
  $('wm-chosen').textContent = wmSourceLabel(source);

  if (source.media === 'image') {
    $('wm-timeline').hidden = true;
    $('wm-ab-row').hidden = true;
    image.onload = () => {
      image.hidden = false;
      $('wm-empty').hidden = true;
      wmState.media = {
        type: 'image', width: image.naturalWidth, height: image.naturalHeight, duration: 0,
      };
      wmUpdateModeForMedia('image');
      wmDraw();
    };
    image.onerror = () => {
      $('wm-empty').textContent = '图片加载失败';
      toast('图片加载失败，可能文件已被清理', true);
    };
    image.src = source.url;
  } else {
    $('wm-timeline').hidden = false;
    $('wm-ab-row').hidden = false;
    video.muted = true;
    video.onloadedmetadata = () => {
      video.hidden = false;
      $('wm-empty').hidden = true;
      wmState.media = {
        type: 'video', width: video.videoWidth, height: video.videoHeight,
        duration: video.duration || 0,
      };
      $('wm-seek').value = '0';
      wmUpdateTime();
      wmUpdateModeForMedia('video');
      wmDraw();
    };
    video.onerror = () => {
      $('wm-empty').textContent = '视频加载失败';
      toast('视频加载失败，可能文件已被清理', true);
    };
    video.src = source.url;
    video.load();
  }
}

function wmClearSource() {
  wmState.source = null;
  wmState.areas = [];
  wmState.active = -1;
  wmState.ab = { a: null, b: null };
  wmState.media = { type: '', width: 0, height: 0, duration: 0 };
  wmUpdateAbLabel();
  const video = $('wm-video');
  const image = $('wm-image');
  video.pause();
  video.removeAttribute('src');
  video.load();
  video.hidden = true;
  image.hidden = true;
  image.removeAttribute('src');
  $('wm-empty').hidden = false;
  $('wm-empty').textContent = '从右侧选一条资产，或上传本地视频 / 图片';
  $('wm-chosen').textContent = '还没选';
  $('wm-timeline').hidden = true;
  $('wm-ab-row').hidden = true;
  $('wm-result').hidden = true;
  $('wm-live').hidden = true;
  $('wm-progress').hidden = true;
  wmRenderSelectionList();
  wmDraw();
}

function wmUpdateTime() {
  const video = $('wm-video');
  const duration = video.duration || 0;
  const current = video.currentTime || 0;
  $('wm-time').textContent = `${wmFormatTime(current)} / ${wmFormatTime(duration)}`;
  if (duration > 0 && !wmState.drag) {
    $('wm-seek').value = String(Math.round((current / duration) * 1000));
  }
}

function wmUpdateAbLabel() {
  const { a, b } = wmState.ab;
  const label = $('wm-ab-label');
  if (a == null || b == null) {
    label.textContent = '整段';
  } else if (b <= a) {
    label.textContent = `无效（A=${a.toFixed(2)}s, B=${b.toFixed(2)}s）`;
  } else {
    label.textContent = `${a.toFixed(2)}s → ${b.toFixed(2)}s`;
  }
}

function wmCurrentAb() {
  const { a, b } = wmState.ab;
  if (wmState.media.type !== 'video' || a == null || b == null || b <= a) return null;
  return [a, b];
}

function wmSetAbPoint(which) {
  if (wmState.media.type !== 'video') {
    toast('A/B 只对视频有效', true);
    return;
  }
  const t = $('wm-video').currentTime || 0;
  wmState.ab[which] = t;
  wmUpdateAbLabel();
}

function wmClearAb() {
  wmState.ab = { a: null, b: null };
  wmUpdateAbLabel();
}

function wmPayload(opts = {}) {
  const source = opts.source || wmState.source;
  const autoDetect = opts.autoDetect !== undefined
    ? opts.autoDetect : $('wm-auto-detect').checked;
  let areas = (opts.areas || wmState.areas).map((a) => [a.x1, a.y1, a.x2, a.y2]);
  let mode = opts.mode || $('wm-mode').value;
  if (wmState.media.type === 'image' && !['lama', 'lama-direct'].includes(mode)) {
    mode = 'lama';
  }
  let ab = opts.ab !== undefined ? opts.ab : wmCurrentAb();
  if (ab && !Array.isArray(ab)) {
    const a = Number(ab.a);
    const b = Number(ab.b);
    ab = (Number.isFinite(a) && Number.isFinite(b) && b > a) ? [a, b] : null;
  }
  return {
    source_type: source.type,
    source: source.value,
    mode,
    detect_model: $('wm-detect-model').value,
    preset: $('wm-preset').value || 'auto',
    areas,
    ab,
    auto_detect: autoDetect,
    hardware_acceleration: $('wm-hw').checked,
    advanced: wmCurrentAdvanced(),
    detect_only: Boolean(opts.detectOnly),
  };
}

async function wmRun(opts = {}) {
  if (!wmState.source) {
    toast('先选一条资产，或者上传本地视频/图片', true);
    return;
  }
  if (wmState.running) {
    toast('已经有去水印任务在跑，等它结束或先点停止', true);
    return;
  }
  const source = opts.source || wmState.source;
  const autoDetect = opts.detectOnly
    ? true
    : (opts.autoDetect !== undefined ? opts.autoDetect : $('wm-auto-detect').checked);
  const areas = opts.areas || wmState.areas;
  const mode = opts.mode || $('wm-mode').value;
  if (!autoDetect && !areas.length && ['sttn-auto', 'lama-direct'].includes(mode)) {
    toast('先在画面上框出水印区域，或勾上「自动检测」', true);
    return;
  }

  wmState.running = true;
  wmState.jobKind = opts.detectOnly ? 'detect' : 'run';
  wmState.activeQueueId = opts.task ? opts.task.id : null;
  if (opts.task) {
    opts.task.status = 'running';
    opts.task.progress = 0;
    wmRenderQueue();
  }
  wmSetBusy(true);
  $('wm-result').hidden = true;
  $('wm-live').hidden = true;
  $('wm-progress').hidden = false;
  $('wm-bar').style.width = '0%';
  $('wm-stage-text').textContent = opts.detectOnly ? '提交检测…' : '提交处理…';
  wmSetLog([]);

  try {
    const job = await api('/api/watermark', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(wmPayload({
        ...opts, source, areas, mode, autoDetect,
      })),
    });
    wmState.jobId = job.id;
    if (opts.task) opts.task.jobId = job.id;
    wmPoll();
  } catch (error) {
    wmState.running = false;
    wmState.jobId = null;
    if (opts.task) {
      opts.task.status = 'failed';
      opts.task.jobId = null;
    }
    wmSetBusy(false);
    $('wm-stage-text').textContent = `启动失败：${error.message}`;
    toast(`启动失败：${error.message}`, true);
    wmRenderQueue();
  }
}

function wmPoll() {
  clearInterval(wmState.timer);
  wmState.timer = setInterval(async () => {
    if (!wmState.jobId) return;
    let job;
    try {
      job = await api(`/api/watermark/${wmState.jobId}`);
    } catch (error) {
      clearInterval(wmState.timer);
      wmState.timer = null;
      wmState.running = false;
      wmState.jobId = null;
      wmSetBusy(false);
      $('wm-stage-text').textContent = `查询失败：${error.message}`;
      return;
    }
    wmApplyJob(job);
  }, 1000);
}

function wmApplyJob(job) {
  $('wm-bar').style.width = `${job.progress}%`;
  const parts = [job.stage || '处理中'];
  if (job.total > 0 && job.status !== 'done') parts.push(`${job.done}/${job.total}`);
  $('wm-stage-text').textContent = parts.join('　');
  wmSetLog(job.log_tail || []);

  if (job.has_live_preview) {
    $('wm-live').hidden = false;
    $('wm-live-img').src = `/api/watermark/${job.id}/live-preview?v=${Date.now()}`;
  } else if (job.detect_only && job.has_preview) {
    $('wm-live').hidden = false;
    $('wm-live-img').src = `/api/watermark/${job.id}/preview?v=${Date.now()}`;
  }

  const activeTask = wmState.activeQueueId
    ? wmState.queue.find((t) => t.id === wmState.activeQueueId)
    : null;
  if (activeTask && job.total) {
    activeTask.progress = job.progress;
    wmRenderQueue();
  }

  if (!['done', 'failed', 'canceled'].includes(job.status)) return;

  clearInterval(wmState.timer);
  wmState.timer = null;
  wmState.running = false;
  wmState.jobId = null;
  wmSetBusy(false);

  if (job.status === 'done') {
    wmSetLog(job.log_tail || []);
    if (job.detect_only) {
      wmApplyDetectedBoxes(job);
      wmRenderDetectResult(job);
      toast(`检测完成：找到 ${(job.result?.boxes || []).length} 处水印`);
    } else {
      if (job.auto_detect) wmApplyDetectedBoxes(job);
      wmRenderRunResult(job);
      toast('去水印完成，可以预览和下载了');
    }
    loadWatermarkHistory();
  } else if (job.status === 'canceled') {
    $('wm-stage-text').textContent = '已停止';
    toast('任务已停止');
  } else {
    $('wm-stage-text').textContent = `失败：${job.error || '未知原因'}`;
    toast(`处理失败：${job.error || '未知原因'}`, true);
    loadWatermarkHistory();
  }

  if (activeTask) {
    activeTask.status = job.status;
    activeTask.progress = job.progress;
    activeTask.jobId = job.id;
    wmRenderQueue();
  }
  if (wmState.queueRunning) {
    setTimeout(wmStartNextQueueTask, 700);
  }
}

function wmApplyDetectedBoxes(job) {
  const boxes = job.result?.boxes || [];
  const info = wmMediaInfo();
  const width = info.width || job.result?.width || 0;
  const height = info.height || job.result?.height || 0;
  if (!width || !height || !boxes.length) return;
  wmSetAreas(boxes.map((b) => [b[0] / width, b[1] / height,
    b[2] / width, b[3] / height]));
  $('wm-auto-detect').checked = false;
}

function wmRenderDetectResult(job) {
  const box = $('wm-result');
  const boxes = job.result?.boxes || [];
  box.hidden = false;
  box.innerHTML = `
    <div class="ups-result-grid">
      <div>
        <a href="/api/watermark/${job.id}/preview?v=${Date.now()}" target="_blank" title="点开看大图">
          <img src="/api/watermark/${job.id}/preview?v=${Date.now()}" alt="检测预览">
        </a>
        <div class="ups-caption">红框 = 将被重绘的区域</div>
      </div>
      <div>
        <div class="ups-result-meta">
          来源　<b>${escapeHtml(job.source_label || '')}</b><br>
          检测到　<b>${boxes.length}</b> 处候选<br>
          检测模型　<b>${escapeHtml(job.detect_model || 'fast')}</b><br>
          时间　${escapeHtml(job.created_at || '')}
        </div>
        <div class="ups-result-actions">
          <a href="/api/watermark/${job.id}/preview" download>下载预览图</a>
        </div>
      </div>
    </div>`;
}

function wmRenderRunResult(job) {
  const box = $('wm-result');
  const media = job.media_type === 'image'
    ? `<img src="/api/watermark/${job.id}/file?v=${Date.now()}" alt="去水印结果">`
    : `<video src="/api/watermark/${job.id}/file" controls preload="metadata"></video>`;
  const size = job.result?.bytes
    ? `${(job.result.bytes / 1048576).toFixed(1)} MB` : '—';
  box.hidden = false;
  box.innerHTML = `
    <div class="ups-result-grid">
      <div>${media}<div class="ups-caption">处理结果</div></div>
      <div>
        <div class="ups-result-meta">
          来源　<b>${escapeHtml(job.source_label || '')}</b><br>
          算法　<b>${escapeHtml(WM_MODE_LABELS[job.mode] || job.mode || '')}</b><br>
          区域　<b>${(job.result?.boxes || []).length || '—'}</b> 处<br>
          文件　<b>${size}</b><br>
          时间　${escapeHtml(job.created_at || '')} → ${escapeHtml(job.finished_at || '')}
        </div>
        <div class="ups-result-actions">
          <a href="/api/watermark/${job.id}/file" download>下载结果</a>
        </div>
      </div>
    </div>`;
}

async function loadWatermarkSources() {
  try {
    const data = await api('/api/assets?hours=168');
    const withVideo = (data.assets || []).filter((a) => a.has_video);
    $('wm-source').innerHTML = '<option value="">从资产里选…</option>' +
      withVideo.map((a) => {
        const label = `${(a.prompt || a.dir).slice(0, 26)} · ${a.resolution || '?'}`;
        return `<option value="${escapeHtml(a.dir)}">${escapeHtml(label)}</option>`;
      }).join('');
    if (wmState.source?.type === 'asset') $('wm-source').value = wmState.source.value;
  } catch {
    /* 资产读不到不影响上传本地文件 */
  }
}

function wmAssetSource(dir) {
  return {
    type: 'asset',
    value: dir,
    label: dir,
    media: 'video',
    url: `/api/assets/${encodeURIComponent(dir)}/video`,
  };
}

function wmStagedSource(info) {
  const name = String(info.name || '');
  const image = info.kind === 'image' || /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(name);
  return {
    type: 'staged',
    value: info.staged_path,
    label: name,
    media: image ? 'image' : 'video',
    url: info.preview_url,
  };
}

async function wmUpload(file) {
  $('wm-chosen').textContent = `上传中… ${file.name}`;
  const form = new FormData();
  form.append('files', file);
  try {
    const data = await api('/api/staging', { method: 'POST', body: form });
    const info = data.files[0];
    const source = wmStagedSource(info);
    $('wm-source').value = '';
    wmSetSource(source);
  } catch (error) {
    $('wm-chosen').textContent = '上传失败';
    toast(`上传失败：${error.message}`, true);
  }
}

function wmTaskFromCurrent() {
  if (!wmState.source) return null;
  return {
    id: ++wmState.queueSeq,
    label: wmState.source.label,
    source: { ...wmState.source },
    media: wmState.media.type,
    areas: wmState.areas.map((a) => ({ ...a })),
    ab: { ...wmState.ab },
    mode: $('wm-mode').value,
    detectModel: $('wm-detect-model').value,
    hw: $('wm-hw').checked,
    advanced: wmCurrentAdvanced(),
    autoDetect: $('wm-auto-detect').checked,
    status: 'pending',
    progress: 0,
    jobId: null,
  };
}

function wmAddQueue() {
  const task = wmTaskFromCurrent();
  if (!task) {
    toast('先选一条资产或上传文件', true);
    return;
  }
  if (!task.autoDetect && !task.areas.length
      && ['sttn-auto', 'lama-direct'].includes(task.mode)) {
    toast('这个算法需要先框出水印区域', true);
    return;
  }
  wmState.queue.push(task);
  wmRenderQueue();
  toast('已加入队列');
}

function wmRenderQueue() {
  const box = $('wm-queue');
  if (!box) return;
  if (!wmState.queue.length) {
    box.innerHTML = '<div class="ups-note">队列是空的。加进来后可以一次顺序处理多条。</div>';
    return;
  }
  box.innerHTML = wmState.queue.map((task) => {
    const cls = task.id === wmState.activeQueueId ? ' on' : '';
    const status = task.status === 'running'
      ? `${task.progress.toFixed(0)}%` : (task.status || 'pending');
    return `<div class="wm-queue-item${cls}" data-wm-task="${task.id}">
      <div class="wm-queue-main">
        <b>${escapeHtml(task.label)}</b>
        <span class="wm-queue-sub">${escapeHtml(WM_MODE_LABELS[task.mode] || task.mode)} · ${escapeHtml(status)}</span>
      </div>
      <button class="link-btn" data-wm-task-del="${task.id}" title="从队列移除">✕</button>
    </div>`;
  }).join('');

  box.querySelectorAll('[data-wm-task]').forEach((item) => {
    item.addEventListener('click', (event) => {
      if (event.target.closest('[data-wm-task-del]')) return;
      const task = wmState.queue.find((t) => t.id === Number(item.dataset.wmTask));
      if (task) wmLoadTask(task);
    });
  });
  box.querySelectorAll('[data-wm-task-del]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const id = Number(btn.dataset.wmTaskDel);
      const task = wmState.queue.find((t) => t.id === id);
      if (!task) return;
      if (task.status === 'running') {
        toast('这个任务正在跑，先点停止', true);
        return;
      }
      wmState.queue = wmState.queue.filter((t) => t.id !== id);
      wmRenderQueue();
    });
  });
}

function wmLoadTask(task) {
  wmState.activeQueueId = task.id;
  const source = { ...task.source };
  wmSetSource(source);
  wmSetAreas(task.areas || []);
  $('wm-mode').value = task.mode;
  $('wm-detect-model').value = task.detectModel || 'fast';
  $('wm-hw').checked = task.hw !== false;
  $('wm-auto-detect').checked = Boolean(task.autoDetect);
  wmApplyAdvanced(task.advanced);
  wmState.ab = { ...(task.ab || { a: null, b: null }) };
  wmUpdateAbLabel();
  wmUpdateModeForMedia(task.media || 'video');
  wmDraw();
}

async function wmStartQueue() {
  if (wmState.running) {
    toast('当前还有任务在跑', true);
    return;
  }
  if (!wmState.queue.some((t) => t.status === 'pending')) {
    toast('队列里没有待处理任务', true);
    return;
  }
  wmState.queueRunning = true;
  wmStartNextQueueTask();
}

function wmStartNextQueueTask() {
  const task = wmState.queue.find((t) => t.status === 'pending');
  if (!task) {
    wmState.queueRunning = false;
    wmRenderQueue();
    return;
  }
  wmLoadTask(task);
  wmRenderQueue();
  wmRun({
    task,
    source: task.source,
    areas: task.areas.map((a) => ({ ...a })),
    mode: task.mode,
    autoDetect: task.autoDetect,
    ab: task.ab,
  });
}

async function loadWatermarkHistory() {
  let data;
  try {
    data = await api('/api/watermark/history');
  } catch {
    return;
  }
  renderWatermarkHistory(data.jobs || []);
}

function renderWatermarkHistory(jobs) {
  const box = $('wm-history');
  const list = $('wm-history-list');
  const usable = jobs.filter((j) => j.has_file || j.has_preview || j.status === 'failed');
  if (!usable.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  list.innerHTML = usable.map((j) => {
    const ok = j.status === 'done' && (j.has_file || j.has_preview);
    const meta = [
      j.created_at,
      j.detect_only ? '仅检测' : (j.media_type === 'image' ? '图片' : '视频'),
      WM_MODE_LABELS[j.mode] || j.mode,
      j.result?.bytes ? `${(j.result.bytes / 1048576).toFixed(1)} MB` : '',
    ].filter(Boolean).join('　');
    return `
      <div class="ups-history-item${ok ? '' : ' bad'}">
        <div class="ups-history-main">
          <b>${escapeHtml(j.source_label || j.source)}</b>
          <span class="ups-history-sub">${escapeHtml(meta)}</span>
          ${ok ? '' : `<span class="ups-history-err">${escapeHtml(j.error || j.stage || '未完成')}</span>`}
        </div>
        <div class="ups-history-actions">
          ${j.has_file ? `<a href="/api/watermark/${j.id}/file" target="_blank">${j.media_type === 'image' ? '打开' : '播放'}</a>
                          <a href="/api/watermark/${j.id}/file" download>下载</a>` : ''}
          ${j.has_preview ? `<a href="/api/watermark/${j.id}/preview" target="_blank">检测图</a>` : ''}
          <button class="ghost sm" data-wm-forget="${j.id}">移除记录</button>
        </div>
      </div>`;
  }).join('');

  list.querySelectorAll('[data-wm-forget]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await api(`/api/watermark/${btn.dataset.wmForget}`, { method: 'DELETE' });
        loadWatermarkHistory();
      } catch (error) {
        toast(error.message, true);
      }
    });
  });
}

function wmInitMediaEvents() {
  const video = $('wm-video');
  const image = $('wm-image');
  video.addEventListener('timeupdate', wmUpdateTime);
  video.addEventListener('loadedmetadata', wmUpdateTime);
  video.addEventListener('play', () => { $('wm-play').textContent = '❚❚'; });
  video.addEventListener('pause', () => { $('wm-play').textContent = '▶'; });
  video.addEventListener('ended', () => { $('wm-play').textContent = '▶'; });
  image.addEventListener('load', wmDraw);

  $('wm-play').addEventListener('click', () => {
    if (wmState.media.type !== 'video') return;
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  });
  $('wm-seek').addEventListener('input', () => {
    if (!video.duration) return;
    video.currentTime = video.duration * (Number($('wm-seek').value) / 1000);
    wmUpdateTime();
  });
  $('wm-frame-prev').addEventListener('click', () => {
    if (wmState.media.type !== 'video') return;
    video.pause();
    video.currentTime = Math.max(0, video.currentTime - 1 / 30);
  });
  $('wm-frame-next').addEventListener('click', () => {
    if (wmState.media.type !== 'video') return;
    video.pause();
    video.currentTime = Math.min(video.duration || 0, video.currentTime + 1 / 30);
  });
  $('wm-set-a').addEventListener('click', () => wmSetAbPoint('a'));
  $('wm-set-b').addEventListener('click', () => wmSetAbPoint('b'));
  $('wm-clear-ab').addEventListener('click', wmClearAb);

  const canvas = $('wm-canvas');
  canvas.tabIndex = 0;
  canvas.addEventListener('pointerdown', wmPointerDown);
  canvas.addEventListener('pointermove', wmPointerMove);
  canvas.addEventListener('pointerup', wmPointerUp);
  canvas.addEventListener('pointercancel', wmPointerCancel);
  window.addEventListener('pointerup', wmPointerUp);
  canvas.addEventListener('keydown', (event) => {
    if (event.key === 'Delete' || event.key === 'Backspace') {
      if (wmState.active >= 0) {
        wmState.areas.splice(wmState.active, 1);
        wmState.active = Math.min(wmState.active, wmState.areas.length - 1);
        wmDraw();
        wmRenderSelectionList();
        event.preventDefault();
      }
    } else if (event.key === 'Escape') {
      wmState.drag = null;
      wmState.draft = null;
      wmDraw();
    }
  });

  if (window.ResizeObserver) {
    new ResizeObserver(() => wmDraw()).observe($('wm-stage'));
  } else {
    window.addEventListener('resize', wmDraw);
  }
}

function initWatermarkTab() {
  if (wmState.ready) return;
  wmState.ready = true;

  wmLoadPrefs();
  wmInitMediaEvents();

  $('wm-source').addEventListener('change', (event) => {
    const dir = event.target.value;
    if (!dir) {
      wmClearSource();
      return;
    }
    wmState.activeQueueId = null;
    wmSetSource(wmAssetSource(dir));
  });

  $('wm-pick').addEventListener('click', () => $('wm-file').click());
  $('wm-file').addEventListener('change', (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (file) wmUpload(file);
  });

  $('wm-preset').addEventListener('change', () => {
    const value = $('wm-preset').value;
    const preset = WM_PRESETS[value];
    if (preset) wmSetAreas([preset]);
  });

  $('wm-detect').addEventListener('click', () => {
    if (!wmState.source) {
      toast('先选一条资产或上传文件', true);
      return;
    }
    wmRun({ detectOnly: true, autoDetect: true });
  });

  $('wm-clear-areas').addEventListener('click', () => {
    wmState.areas = [];
    wmState.active = -1;
    wmDraw();
    wmRenderSelectionList();
  });

  $('wm-clear-log').addEventListener('click', () => wmSetLog([]));

  $('wm-run').addEventListener('click', () => wmRun());
  $('wm-add-queue').addEventListener('click', wmAddQueue);
  $('wm-start-queue').addEventListener('click', wmStartQueue);
  $('wm-clear-queue').addEventListener('click', () => {
    wmState.queue = wmState.queue.filter((t) => t.status === 'running');
    if (!wmState.queue.some((t) => t.status === 'running')) {
      wmState.queueRunning = false;
    }
    wmRenderQueue();
  });

  $('wm-stop').addEventListener('click', async () => {
    if (!wmState.jobId) return;
    const id = wmState.jobId;
    wmState.queueRunning = false;
    $('wm-stop').disabled = true;
    try {
      await api(`/api/watermark/${id}/cancel`, { method: 'POST' });
      $('wm-stage-text').textContent = '正在停止…';
    } catch (error) {
      toast(`停止失败：${error.message}`, true);
      $('wm-stop').disabled = false;
    }
  });

  $('wm-open-gui').addEventListener('click', async () => {
    try {
      await api('/api/watermark/open-gui', { method: 'POST' });
      toast('桌面版已启动；网页任务不受影响');
    } catch (error) {
      toast(`打开失败：${error.message}`, true);
    }
  });

  $('wm-reset-adv').addEventListener('click', () => {
    wmApplyAdvanced(WM_ADV_DEFAULTS);
    wmSavePrefs();
  });
  ['wm-mode', 'wm-detect-model', 'wm-hw'].forEach((id) => {
    $(id).addEventListener('change', wmSavePrefs);
  });
  Object.values(WM_MODE_INPUTS).forEach((id) => {
    $(id).addEventListener('change', wmSavePrefs);
  });

  $('wm-mode').addEventListener('change', () => {
    if (wmState.media.type === 'image'
        && !['lama', 'lama-direct'].includes($('wm-mode').value)) {
      $('wm-mode').value = 'lama';
    }
  });

  if (!localStorage.getItem('wan3-wm-prefs')) wmApplyAdvanced(WM_ADV_DEFAULTS);
  wmUpdateModeForMedia('video');
  wmClearSource();
  wmSetBusy(false);
}

/** 资产卡片上的「去水印」：载入资产并直接按默认设置跑一次。 */
async function quickWatermark(dir) {
  document.querySelector('[data-view="canvas"]').click();
  document.querySelector('.post-tab[data-tool="watermark"]').click();
  if (wmState.running) {
    toast('当前有去水印任务在跑，等它结束再处理这条资产', true);
    return;
  }
  await loadWatermarkSources();
  $('wm-source').value = dir;
  wmState.activeQueueId = null;
  wmSetSource(wmAssetSource(dir));
  $('wm-auto-detect').checked = true;
  $('wm-mode').value = 'sttn-auto';
  $('wm-preset').value = 'auto';
  wmRun({ autoDetect: true, mode: 'sttn-auto' });
}

/* ---------------------------------------------------------------- 图片拼接（原画布） */

/**
 * 图片拼接画布。
 *
 * 关键设计：预览和导出**用同一个绘制函数** drawComposite()，
 * 只是 scale 不同。分成两套代码的话，导出的图和看到的迟早对不上。
 */

const canvasState = {
  width: 1080,
  height: 1080,
  cols: 2,
  rows: 2,
  gap: 0,
  radius: 0,
  background: '#ffffff',
  cells: [],            // 每格 { img, fit, anchor } 或 null
  selected: -1,
};

const RATIO_PRESETS = [
  ['1:1', 1, 1], ['4:3', 4, 3], ['3:4', 3, 4],
  ['16:9', 16, 9], ['9:16', 9, 16], ['3:2', 3, 2], ['2:3', 2, 3], ['2:1', 2, 1],
];

// [列, 行]
const GRID_PRESETS = [
  [1, 1], [2, 1], [1, 2], [2, 2], [3, 1], [1, 3], [3, 2], [2, 3], [3, 3], [4, 2], [2, 4],
];

const ANCHORS = [
  [0, 0], [0.5, 0], [1, 0],
  [0, 0.5], [0.5, 0.5], [1, 0.5],
  [0, 1], [0.5, 1], [1, 1],
];

let canvasReady = false;
let pendingCell = -1;

function initCanvasTab() {
  if (canvasReady) { renderCanvas(); return; }
  canvasReady = true;

  // 比例
  $('cp-ratios').innerHTML = RATIO_PRESETS.map(([label, w, h]) =>
    `<button class="cpratio" data-w="${w}" data-h="${h}" data-label="${label}">${label}</button>`).join('');
  $('cp-ratios').querySelectorAll('[data-w]').forEach((btn) => {
    btn.addEventListener('click', () => {
      // 长边固定 1080，短边按比例算 —— 每次点都得到同样尺寸，不会越点越大
      const w = Number(btn.dataset.w), h = Number(btn.dataset.h);
      const LONG = 1080;
      if (w >= h) {
        canvasState.width = LONG;
        canvasState.height = Math.round((LONG * h) / w);
      } else {
        canvasState.height = LONG;
        canvasState.width = Math.round((LONG * w) / h);
      }
      $('cp-w').value = canvasState.width;
      $('cp-h').value = canvasState.height;
      markRatio(btn.dataset.label);
      renderCanvas();
    });
  });

  // 网格
  $('cp-grids').innerHTML = GRID_PRESETS.map(([c, r]) =>
    `<button class="cpgridbtn" data-cols="${c}" data-rows="${r}">${c}×${r}</button>`).join('');
  $('cp-grids').querySelectorAll('[data-cols]').forEach((btn) => {
    btn.addEventListener('click', () => setCanvasGrid(Number(btn.dataset.cols), Number(btn.dataset.rows)));
  });

  // 尺寸输入
  $('cp-w').addEventListener('change', () => {
    canvasState.width = clamp(Number($('cp-w').value), 64, 8000);
    $('cp-w').value = canvasState.width;
    markRatio(null);
    renderCanvas();
  });
  $('cp-h').addEventListener('change', () => {
    canvasState.height = clamp(Number($('cp-h').value), 64, 8000);
    $('cp-h').value = canvasState.height;
    markRatio(null);
    renderCanvas();
  });

  // 间距 / 圆角
  $('cp-gap').addEventListener('input', () => {
    canvasState.gap = clamp(Number($('cp-gap').value) || 0, 0, 200);
    renderCanvas();
  });
  $('cp-radius').addEventListener('input', () => {
    canvasState.radius = clamp(Number($('cp-radius').value) || 0, 0, 200);
    renderCanvas();
  });

  // 背景色
  $('cp-colors').querySelectorAll('.cpcolor').forEach((btn) => {
    btn.addEventListener('click', () => {
      canvasState.background = btn.dataset.color;
      markColor(btn);
      renderCanvas();
    });
  });
  $('cp-color-custom').addEventListener('input', (event) => {
    canvasState.background = event.target.value;
    markColor(null);
    renderCanvas();
  });

  // 九宫格对齐
  $('cp-anchor').innerHTML = ANCHORS.map(([ax, ay], i) =>
    `<button data-ax="${ax}" data-ay="${ay}" data-i="${i}" title="对齐"></button>`).join('');
  $('cp-anchor').querySelectorAll('[data-ax]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cell = canvasState.cells[canvasState.selected];
      if (!cell) return;
      cell.anchor = [Number(btn.dataset.ax), Number(btn.dataset.ay)];
      markAnchor();
      renderCanvas();
    });
  });

  // 填充方式
  $('cp-fit').querySelectorAll('[data-fit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cell = canvasState.cells[canvasState.selected];
      if (!cell) return;
      cell.fit = btn.dataset.fit;
      markFit();
      renderCanvas();
    });
  });

  $('cp-replace').addEventListener('click', () => pickImageFor(canvasState.selected));
  $('cp-clear').addEventListener('click', () => {
    if (canvasState.selected < 0) return;
    canvasState.cells[canvasState.selected] = null;
    renderCanvas();
  });

  // 选文件
  $('cp-file').addEventListener('change', async (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (file && pendingCell >= 0) await loadImageIntoCell(pendingCell, file);
    pendingCell = -1;
  });

  // 导出
  $('cp-export').addEventListener('click', () => exportCanvas(1));
  $('cp-export-2x').addEventListener('click', () => exportCanvas(2));

  setCanvasGrid(2, 2);
  markRatio('1:1');
  markColor($('cp-colors').querySelector('.cpcolor'));
  $('cp-w').value = canvasState.width;
  $('cp-h').value = canvasState.height;
  window.addEventListener('resize', () => {
    if ($('view-canvas').classList.contains('active')) renderCanvas();
  });
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, Number.isFinite(v) ? v : lo)); }

function setCanvasGrid(cols, rows) {
  const old = canvasState.cells;
  canvasState.cols = cols;
  canvasState.rows = rows;
  canvasState.cells = Array.from({ length: cols * rows }, (_, i) => old[i] || null);
  canvasState.selected = -1;
  $('cp-grids').querySelectorAll('[data-cols]').forEach((btn) => {
    btn.classList.toggle('on', Number(btn.dataset.cols) === cols && Number(btn.dataset.rows) === rows);
  });
  renderCanvas();
}

function markRatio(label) {
  $('cp-ratios').querySelectorAll('[data-label]').forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.label === label);
  });
}
function markColor(btn) {
  $('cp-colors').querySelectorAll('.cpcolor').forEach((b) => b.classList.toggle('on', b === btn));
}
function markFit() {
  const cell = canvasState.cells[canvasState.selected];
  $('cp-fit').querySelectorAll('[data-fit]').forEach((b) => {
    b.classList.toggle('on', cell && b.dataset.fit === cell.fit);
  });
}
function markAnchor() {
  const cell = canvasState.cells[canvasState.selected];
  $('cp-anchor').querySelectorAll('[data-ax]').forEach((b) => {
    b.classList.toggle('on', cell && Number(b.dataset.ax) === cell.anchor[0]
      && Number(b.dataset.ay) === cell.anchor[1]);
  });
}

/* -------- 绘制（预览和导出共用） -------- */

function roundRectPath(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  if (rr <= 0) { ctx.rect(x, y, w, h); return; }
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function cellRect(index, W, H, scale) {
  const gap = canvasState.gap * scale;
  const cw = (W - gap * (canvasState.cols - 1)) / canvasState.cols;
  const ch = (H - gap * (canvasState.rows - 1)) / canvasState.rows;
  const col = index % canvasState.cols;
  const row = Math.floor(index / canvasState.cols);
  return { x: col * (cw + gap), y: row * (ch + gap), w: cw, h: ch };
}

function drawComposite(ctx, W, H, scale) {
  ctx.clearRect(0, 0, W, H);

  if (canvasState.background !== 'transparent') {
    ctx.fillStyle = canvasState.background;
    ctx.fillRect(0, 0, W, H);
  }

  const radius = canvasState.radius * scale;

  canvasState.cells.forEach((cell, i) => {
    const r = cellRect(i, W, H, scale);

    if (!cell || !cell.img) {
      ctx.save();
      roundRectPath(ctx, r.x, r.y, r.w, r.h, radius);
      ctx.fillStyle = canvasState.background === 'transparent' ? '#f1f2f4' : 'rgba(0,0,0,.045)';
      ctx.fill();
      // 中间画个加号，提示这里可以放图
      const cx = r.x + r.w / 2, cy = r.y + r.h / 2;
      const arm = Math.min(r.w, r.h) * 0.08;
      ctx.strokeStyle = 'rgba(0,0,0,.22)';
      ctx.lineWidth = Math.max(1.5 * scale, 1);
      ctx.beginPath();
      ctx.moveTo(cx - arm, cy); ctx.lineTo(cx + arm, cy);
      ctx.moveTo(cx, cy - arm); ctx.lineTo(cx, cy + arm);
      ctx.stroke();
      ctx.restore();
      return;
    }

    const img = cell.img;
    const scaleImg = cell.fit === 'contain'
      ? Math.min(r.w / img.width, r.h / img.height)
      : Math.max(r.w / img.width, r.h / img.height);
    const dw = img.width * scaleImg;
    const dh = img.height * scaleImg;
    const dx = r.x + (r.w - dw) * cell.anchor[0];
    const dy = r.y + (r.h - dh) * cell.anchor[1];

    ctx.save();
    roundRectPath(ctx, r.x, r.y, r.w, r.h, radius);
    ctx.clip();
    ctx.drawImage(img, dx, dy, dw, dh);
    ctx.restore();
  });
}

function renderCanvas() {
  if (!$('canvas-preview')) return;

  const stage = document.querySelector('.canvas-stage');
  const host = $('canvas-frame');
  if (!stage) return;

  const availW = Math.max(240, stage.clientWidth - 2);
  const availH = Math.max(200, window.innerHeight * 0.62);
  let dispW = Math.min(availW, 720);
  let dispH = dispW * (canvasState.height / canvasState.width);
  if (dispH > availH) { dispH = availH; dispW = dispH * (canvasState.width / canvasState.height); }

  const dpr = window.devicePixelRatio || 1;
  const canvas = $('canvas-preview');
  canvas.style.width = `${dispW}px`;
  canvas.style.height = `${dispH}px`;
  canvas.width = Math.round(dispW * dpr);
  canvas.height = Math.round(dispH * dpr);

  const ctx = canvas.getContext('2d');
  const scale = (dispW / canvasState.width) * dpr;
  drawComposite(ctx, canvas.width, canvas.height, scale);

  // 命中区
  const hit = $('canvas-hit');
  hit.innerHTML = '';
  const shownW = canvasState.width;
  const shownH = canvasState.height;
  canvasState.cells.forEach((cell, i) => {
    const r = cellRect(i, shownW, shownH, 1);
    const div = document.createElement('div');
    div.className = `canvas-cell-hit${i === canvasState.selected ? ' selected' : ''}`;
    div.style.left = `${(r.x / shownW) * 100}%`;
    div.style.top = `${(r.y / shownH) * 100}%`;
    div.style.width = `${(r.w / shownW) * 100}%`;
    div.style.height = `${(r.h / shownH) * 100}%`;
    div.dataset.cell = String(i);
    div.innerHTML = `<span class="cell-num">${i + 1}</span>`;

    div.addEventListener('click', () => {
      canvasState.selected = i;
      renderCanvas();
      renderCellPanel();
    });

    // 拖图进来
    div.addEventListener('dragover', (e) => { e.preventDefault(); div.classList.add('drop'); });
    div.addEventListener('dragleave', () => div.classList.remove('drop'));
    div.addEventListener('drop', async (e) => {
      e.preventDefault();
      div.classList.remove('drop');
      const file = [...(e.dataTransfer.files || [])].find((f) => f.type.startsWith('image/'));
      if (file) await loadImageIntoCell(i, file);
    });

    hit.appendChild(div);
  });

  renderCellPanel();

  const unloaded = canvasState.cells.filter((c) => !c || !c.img).length;
  $('canvas-hint').textContent = unloaded
    ? `还有 ${unloaded} 个格子空着 · 点格子选图，或把图片拖进去`
    : '全部格子都放好图了 · 可以导出';
  $('cp-export-note').textContent =
    `导出尺寸 ${canvasState.width * 1} × ${canvasState.height * 1} px（2 倍则是 ${canvasState.width * 2} × ${canvasState.height * 2}）`;
}

function renderCellPanel() {
  const cell = canvasState.cells[canvasState.selected];
  $('cp-cell-empty').hidden = Boolean(cell);
  $('cp-cell-body').hidden = !cell;
  if (cell) {
    markFit();
    markAnchor();
  }
}

function pickImageFor(index) {
  if (index < 0) return;
  pendingCell = index;
  $('cp-file').click();
}

function loadImageIntoCell(index, file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const existing = canvasState.cells[index];
      canvasState.cells[index] = {
        img,
        name: file.name,
        fit: existing?.fit || 'cover',
        anchor: existing?.anchor || [0.5, 0.5],
      };
      canvasState.selected = index;
      renderCanvas();
      resolve();
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      toast(`「${file.name}」读不出来，换一张试试`, true);
      resolve();
    };
    img.src = url;
  });
}

function exportCanvas(multiplier) {
  const W = canvasState.width * multiplier;
  const H = canvasState.height * multiplier;

  if (W * H > 40_000_000) {
    toast('导出尺寸太大了（超过 4000 万像素），调小一点', true);
    return;
  }

  const off = document.createElement('canvas');
  off.width = W;
  off.height = H;
  drawComposite(off.getContext('2d'), W, H, multiplier);

  off.toBlob((blob) => {
    if (!blob) { toast('导出失败', true); return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    a.href = url;
    a.download = `拼接_${W}x${H}_${stamp}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`已导出 ${W}×${H} PNG`);
  }, 'image/png');
}

/* ---------------------------------------------------------------- 资产 */

let assetData = { assets: [], cloud: [], counts: {} };

async function loadAssets() {
  const grid = $('assets-grid');
  grid.innerHTML = '<div class="empty-state">读取中……</div>';
  $('cloud-panel').hidden = true;

  try {
    assetData = await api('/api/assets?hours=24');
    fillAssetFilters();
    renderAssets();
    renderCloud();
  } catch (error) {
    grid.innerHTML = `<div class="empty-state">读取失败：${escapeHtml(error.message)}</div>`;
  }
}

function fillAssetFilters() {
  const models = [...new Set(assetData.assets.map((a) => a.model).filter(Boolean))];
  const resolutions = [...new Set(assetData.assets.map((a) => a.resolution).filter(Boolean))];

  const modelSel = $('asset-model');
  const resSel = $('asset-resolution');
  const keepModel = modelSel.value;
  const keepRes = resSel.value;

  modelSel.innerHTML = '<option value="">全部模型</option>' +
    models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
  resSel.innerHTML = '<option value="">全部分辨率</option>' +
    resolutions.map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join('');

  modelSel.value = keepModel;
  resSel.value = keepRes;
}

function filteredAssets() {
  const keyword = $('asset-search').value.trim().toLowerCase();
  const model = $('asset-model').value;
  const resolution = $('asset-resolution').value;
  const onlyFav = $('asset-only-fav').checked;

  return assetData.assets.filter((asset) => {
    if (onlyFav && !asset.favorite) return false;
    if (model && asset.model !== model) return false;
    if (resolution && asset.resolution !== resolution) return false;
    if (keyword) {
      const haystack = `${asset.prompt} ${asset.original_prompt} ${asset.dir} ${(asset.tags || []).join(' ')}`.toLowerCase();
      if (!haystack.includes(keyword)) return false;
    }
    return true;
  });
}

function renderAssets() {
  const grid = $('assets-grid');
  const counts = assetData.counts || {};
  const shown = filteredAssets();

  $('asset-stats').textContent =
    `本地 ${counts.local || 0} 条 · 收藏 ${counts.favorite || 0} 条` +
    (counts.cloud_missing ? ` · 云端有 ${counts.cloud_missing} 条还没存到本地` : '');

  if (!assetData.assets.length) {
    grid.innerHTML = '<div class="empty-state">本地还没有资产。生成一条视频就会出现在这里。</div>';
    return;
  }
  if (!shown.length) {
    grid.innerHTML = '<div class="empty-state">没有符合条件的资产。</div>';
    return;
  }

  grid.innerHTML = shown.map((asset) => {
    const ok = asset.status === 'succeeded';
    const durationText = asset.duration === -1 ? '智能时长'
      : asset.duration ? `${asset.duration}s` : '—';
    const cost = asset.cost != null ? `¥${Number(asset.cost).toFixed(2)}` : '—';
    const dir = encodeURIComponent(asset.dir);

    const media = asset.has_video
      ? `<img src="/api/assets/${dir}/thumb" alt="" loading="lazy" data-thumb>`
      : `<div class="acard-empty">${escapeHtml(asset.error || '没有视频文件')}</div>`;

    const statusText = ok ? '成功' : (asset.status || '未知');

    return `
      <div class="acard${asset.favorite ? ' fav' : ''}" data-dir="${escapeHtml(asset.dir)}">
        <div class="acard-media">
          ${media}
          <span class="acard-badge ${ok ? 'ok' : 'bad'}">${escapeHtml(statusText)}</span>
          <button class="acard-star${asset.favorite ? ' on' : ''}" data-fav="${escapeHtml(asset.dir)}"
                  data-value="${asset.favorite ? '1' : '0'}"
                  title="${asset.favorite ? '取消收藏' : '收藏'}">★</button>
        </div>
        <div class="acard-body">
          <div class="acard-prompt" title="${escapeHtml(asset.prompt)}">${escapeHtml(asset.prompt || '(无提示词)')}</div>
          <div class="acard-meta">
            <span>${escapeHtml(asset.model || '')}</span>
            <span>${escapeHtml(asset.resolution || '')}</span>
            <span>${durationText}</span>
            <span>${cost}</span>
            ${asset.actual_seconds ? `<span>${asset.actual_seconds}s</span>` : ''}
          </div>
          ${asset.error ? `<div class="acard-note">${escapeHtml(asset.error).slice(0, 80)}</div>` : ''}
          <div class="acard-actions">
            ${asset.has_video ? `<a href="/api/assets/${dir}/video" download>下载</a>` : ''}
            ${asset.has_video ? `<button data-upscale="${escapeHtml(asset.dir)}" title="用 AI 把这条视频放大到 2K">超清</button>` : ''}
            ${asset.has_video ? `<button data-watermark="${escapeHtml(asset.dir)}" title="去掉豆包 / 万相 / LibLib 等平台的 AI 水印">去水印</button>` : ''}
            <button data-reuse="${escapeHtml(asset.dir)}">复用参数</button>
            <button data-meta="${escapeHtml(asset.dir)}">看参数</button>
            <button class="danger" data-del="${escapeHtml(asset.dir)}">删除</button>
          </div>
        </div>
      </div>`;
  }).join('');

  // 缩略图加载失败（多半是本机没有 ffmpeg）时给个说明，别留一片空白
  grid.querySelectorAll('[data-thumb]').forEach((img) => {
    img.addEventListener('error', () => {
      const box = document.createElement('div');
      box.className = 'acard-empty';
      box.textContent = '缩略图生成失败（本机可能没装 ffmpeg）';
      img.replaceWith(box);
    });
  });

  // 点缩略图进详情页（直接播放）。收藏按钮要排除掉，
  // 否则点收藏会顺带把详情页也打开。
  grid.querySelectorAll('.acard-media').forEach((media) => {
    media.style.cursor = 'pointer';
    media.addEventListener('click', (event) => {
      if (event.target.closest('[data-fav]')) return;
      const card = media.closest('.acard');
      if (card) openAssetDetail(card.dataset.dir);
    });
  });

  grid.querySelectorAll('[data-fav]').forEach((btn) => {
    btn.addEventListener('click', () => toggleFavorite(btn.dataset.fav, btn.dataset.value !== '1'));
  });
  grid.querySelectorAll('[data-reuse]').forEach((btn) => {
    btn.addEventListener('click', () => reuseAsset(btn.dataset.reuse));
  });
  grid.querySelectorAll('[data-upscale]').forEach((btn) => {
    btn.addEventListener('click', () => quickUpscale(btn.dataset.upscale));
  });
  grid.querySelectorAll('[data-watermark]').forEach((btn) => {
    btn.addEventListener('click', () => quickWatermark(btn.dataset.watermark));
  });
  grid.querySelectorAll('[data-meta]').forEach((btn) => {
    btn.addEventListener('click', () => showMeta(btn.dataset.meta));
  });
  grid.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', () => removeAsset(btn.dataset.del));
  });
}

function renderCloud() {
  const panel = $('cloud-panel');
  const list = $('cloud-list');
  const cloud = assetData.cloud || [];

  if (assetData.cloud_error) {
    panel.hidden = false;
    $('cloud-sub').textContent = `云端查询失败：${assetData.cloud_error}`;
    list.innerHTML = '';
    $('cloud-toggle').hidden = true;
    return;
  }
  if (!cloud.length) { panel.hidden = true; return; }

  panel.hidden = false;
  $('cloud-toggle').hidden = false;

  const missing = cloud.filter((c) => !c.in_local);
  $('cloud-sub').textContent = missing.length
    ? `账号下最近 24 小时共 ${cloud.length} 条，其中 ${missing.length} 条本地没有`
    : `账号下最近 24 小时共 ${cloud.length} 条，本地都有`;

  list.innerHTML = cloud.map((task) => `
    <div class="cloud-item">
      <span class="ci-model">${escapeHtml(task.model || '?')}</span>
      <span class="ci-time">${escapeHtml((task.created_at || '').replace('T', ' '))}</span>
      <span class="ci-id">${escapeHtml((task.task_id || '').slice(0, 13))}…</span>
      ${task.in_local
        ? '<span class="ci-have">本地已有</span>'
        : `<button data-adopt="${escapeHtml(task.task_id)}">补下载到本地</button>`}
    </div>`).join('');

  list.querySelectorAll('[data-adopt]').forEach((btn) => {
    btn.addEventListener('click', () => adoptTask(btn.dataset.adopt, btn));
  });
}

async function toggleFavorite(dir, value) {
  try {
    await api(`/api/assets/${encodeURIComponent(dir)}/favorite`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    });
    const asset = assetData.assets.find((a) => a.dir === dir);
    if (asset) asset.favorite = value;
    assetData.counts.favorite = assetData.assets.filter((a) => a.favorite).length;
    renderAssets();
  } catch (error) {
    toast(`操作失败：${error.message}`, true);
  }
}

async function adoptTask(taskId, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '下载中…';
  try {
    const data = await api('/api/assets/adopt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId }),
    });
    toast(`已补下载：${(data.size_bytes / 1024 / 1024).toFixed(1)} MB`);
    await loadAssets();
  } catch (error) {
    toast(`补下载失败：${error.message}`, true);
    button.disabled = false;
    button.textContent = original;
  }
}

async function removeAsset(dir) {
  const asset = assetData.assets.find((a) => a.dir === dir);
  const label = (asset?.prompt || dir).slice(0, 30);
  if (!window.confirm(`确定删除「${label}」？\n\n视频、参数和素材副本会一起删掉，不可恢复。`)) return;
  try {
    await api(`/api/assets/${encodeURIComponent(dir)}`, { method: 'DELETE' });
    toast('已删除');
    await loadAssets();
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

/** 素材类型 -> 显示用的种类 */
function kindForMediaType(type) {
  if (type === 'reference_video') return 'video';
  if (type === 'reference_audio') return 'audio';
  return 'image';
}

/** 把历史资产的参数和参考素材一起填回创作页。 */
async function reuseAsset(dir) {
  const asset = assetData.assets.find((a) => a.dir === dir);
  if (!asset) return;

  // 参考素材：refs/ 里存着当初的副本，直接带回来，不用用户重新上传
  let restored = 0;
  state.assets = [];
  try {
    const data = await api(`/api/assets/${encodeURIComponent(dir)}/refs`);
    (data.refs || []).forEach((ref, index) => {
      state.assets.push({
        staging_id: `reuse-${index}-${ref.name}`,
        name: ref.name,
        kind: kindForMediaType(ref.type),
        // 直接指 refs/ 里的副本，提交时会照常上传换新的 oss:// 链接
        staged_path: ref.path,
        preview_url: ref.preview,
        duration: ref.duration || 0,
        errors: [],
        warnings: [],
        auto_fix: null,
        media_type: ref.type,
        reused: true,
      });
      restored += 1;
    });
  } catch (error) {
    console.warn('参考素材没带回来：', error.message);
  }

  promptEl.value = asset.prompt || asset.original_prompt || '';
  state.optimizeResult = null;
  state.optimizeFailed = false;

  if (asset.resolution) {
    state.resolution = asset.resolution;
    $('btn-resolution').innerHTML = `<span class="ctl-ico">✳</span>${escapeHtml(asset.resolution)}`;
  }
  if (asset.ratio) {
    state.ratio = asset.ratio;
    const label = state.options.ratios.find((r) => r.value === asset.ratio);
    if (label) $('btn-ratio').innerHTML = `<span class="ctl-ico">▭</span>${escapeHtml(label.label)}`;
  }
  if (asset.duration === -1) {
    state.durationMode = 'smart';
  } else if (asset.duration) {
    state.durationMode = 'custom';
    state.duration = asset.duration;
  }
  if (typeof asset.audio === 'boolean') setToggle('btn-audio', 'audio', asset.audio);
  if (typeof asset.prompt_extend === 'boolean') setToggle('btn-extend', 'promptExtend', asset.prompt_extend);
  if (typeof asset.watermark === 'boolean') setToggle('btn-watermark', 'watermark', asset.watermark);

  // 按带回来的素材切到对应模式，否则素材会被界面藏起来、用户以为丢了
  const hasLastFrame = state.assets.some((a) => a.media_type === 'last_frame');
  const hasFirstFrame = state.assets.some((a) => a.media_type === 'first_frame');
  const hasRefs = state.assets.some((a) => REFERENCE_TYPES.includes(a.media_type));
  if (hasLastFrame) setComposerMode('frames', { keepAssets: true });
  else if (hasFirstFrame) setComposerMode('i2v', { keepAssets: true });
  else if (hasRefs) setComposerMode('r2v', { keepAssets: true });

  syncDurationButton();
  renderChips();
  refreshEstimate();
  document.querySelector('[data-view="create"]').click();

  toast(restored
    ? `参数和 ${restored} 个参考素材都填回来了，改完可以直接生成。`
    : '参数已填回创作页，可以直接改完再生成。');
}

function setToggle(id, key, value) {
  state[key] = value;
  $(id).classList.toggle('on', value);
}

$('asset-search').addEventListener('input', renderAssets);
$('asset-model').addEventListener('change', renderAssets);
$('asset-resolution').addEventListener('change', renderAssets);
$('asset-only-fav').addEventListener('change', renderAssets);
$('asset-refresh').addEventListener('click', () => {
  loadAssets();
  toast('已刷新');
});
$('cloud-toggle').addEventListener('click', () => {
  const list = $('cloud-list');
  list.hidden = !list.hidden;
  $('cloud-toggle').textContent = list.hidden ? '展开' : '收起';
});

/* ---------------------------------------------------------------- 作品详情 */

const RATIO_LABEL = { adaptive: '自动', '21:9': '21:9', '16:9': '16:9', '4:3': '4:3', '1:1': '1:1', '3:4': '3:4', '9:16': '9:16' };
const STATUS_LABEL = { succeeded: '成功', failed: '失败', running: '生成中', canceled: '已取消', queued: '排队中' };

let detailDir = null;

function fmtBool(value, onText = '开', offText = '关') {
  if (value === true) return onText;
  if (value === false) return offText;
  return '—';
}

function fmtBytes(bytes) {
  if (!bytes) return '—';
  return bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`;
}

function openAssetDetail(dir) {
  const asset = assetData.assets.find((a) => a.dir === dir);
  if (!asset) return;
  detailDir = dir;

  const enc = encodeURIComponent(dir);
  const video = $('detail-video');
  const right = $('detail-right');

  $('detail-title').textContent = asset.prompt ? asset.prompt.slice(0, 40) : '作品详情';
  $('detail-sub').textContent = `${(asset.created_at || '').replace('T', ' ')} · ${asset.model || ''}`;

  const favBtn = $('detail-fav');
  favBtn.classList.toggle('on', Boolean(asset.favorite));

  // 播放
  if (asset.has_video) {
    video.src = `/api/assets/${enc}/video`;
    video.hidden = false;
  } else {
    video.removeAttribute('src');
    video.hidden = true;
  }

  // 底部操作（原来的「看参数」保留，显示原始 meta.json）
  $('detail-actions').innerHTML = [
    asset.has_video ? `<a href="/api/assets/${enc}/video" download>下载视频</a>` : '',
    `<button data-reuse="${escapeHtml(dir)}">复用参数重新生成</button>`,
    `<button data-meta="${escapeHtml(dir)}">看参数（原始 JSON）</button>`,
    `<button class="danger" data-del="${escapeHtml(dir)}">删除</button>`,
  ].join('');

  // 右侧参数
  const rows = [];
  const add = (label, value, mono = false) => {
    if (value === null || value === undefined || value === '' || value === '—') return;
    rows.push(`<dt>${escapeHtml(label)}</dt><dd class="${mono ? 'mono' : ''}">${value}</dd>`);
  };

  add('模型', escapeHtml(asset.model || ''));
  add('分辨率', escapeHtml(asset.resolution || ''));
  add('比例', escapeHtml(RATIO_LABEL[asset.ratio] || asset.ratio || ''));
  add('时长', asset.duration === -1 ? '智能时长（模型自定）'
    : asset.duration ? `${asset.duration} 秒` : '');
  add('声音', fmtBool(asset.audio, '有声音', '无声'));
  add('水印', asset.watermark === false ? '未添加'
    : asset.watermark === true ? '已添加「AI生成」' : '');
  add('智能改写', fmtBool(asset.prompt_extend));
  add('生成模式', escapeHtml(asset.mode || ''));

  // 实际结果
  const actual = [];
  if (asset.actual_seconds) actual.push(`实际 ${asset.actual_seconds} 秒`);
  if (asset.billable_seconds) actual.push(`计费 ${asset.billable_seconds} 秒`);
  add('实际产出', actual.join(' · '));
  add('花费', asset.cost != null ? `¥${Number(asset.cost).toFixed(2)}` : '');
  add('文件大小', fmtBytes(asset.video_bytes));
  add('状态', STATUS_LABEL[asset.status] || escapeHtml(asset.status || ''));

  right.innerHTML = `
    <div class="dsec">
      <div class="dsec-title">提示词</div>
      <div class="dprompt">${escapeHtml(asset.prompt || '(无)')}</div>
    </div>

    ${asset.optimized_prompt && asset.optimized_prompt !== asset.prompt ? `
    <div class="dsec">
      <div class="dsec-title">整理后的提示词（仅供参考）</div>
      <div class="dprompt dim">${escapeHtml(asset.optimized_prompt)}</div>
    </div>` : ''}

    ${asset.original_prompt && asset.original_prompt !== asset.prompt ? `
    <div class="dsec">
      <div class="dsec-title">你最初写的</div>
      <div class="dprompt dim">${escapeHtml(asset.original_prompt)}</div>
    </div>` : ''}

    <div class="dsec">
      <div class="dsec-title">参数</div>
      <dl class="dtable">${rows.join('')}</dl>
    </div>

    ${asset.refs && asset.refs.length ? `
    <div class="dsec">
      <div class="dsec-title">参考素材（${asset.refs.length}）</div>
      <div class="drefs">
        ${asset.refs.map((ref) => {
          const name = typeof ref === 'string' ? ref : ref.name;
          const type = typeof ref === 'string' ? '' : ref.type;
          const src = typeof ref === 'string'
            ? `/api/assets/${enc}/refs/${encodeURIComponent(name)}`
            : ref.preview;
          const kind = kindForMediaType(type);
          return kind === 'image'
            ? `<img src="${src}" alt="${escapeHtml(name)}" title="${escapeHtml(name)}">`
            : `<div class="dref-doc" title="${escapeHtml(name)}">${kind === 'video' ? '▶' : '♪'}</div>`;
        }).join('')}
      </div>
    </div>` : ''}

    <div class="dsec">
      <div class="dsec-title">记录</div>
      <dl class="dtable">
        <dt>创建时间</dt><dd>${escapeHtml((asset.created_at || '').replace('T', ' '))}</dd>
        ${asset.task_id ? `<dt>task_id</dt><dd class="mono">${escapeHtml(asset.task_id)}<button class="dcopy" data-copy="${escapeHtml(asset.task_id)}">复制</button></dd>` : ''}
      </dl>
    </div>

    ${asset.error ? `<div class="dsec"><div class="dsec-title">错误</div><div class="dprompt">${escapeHtml(asset.error)}</div></div>` : ''}
  `;

  // 事件
  right.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        toast('已复制');
      } catch {
        toast('复制失败，请手动选中', true);
      }
    });
  });

  $('detail-actions').querySelectorAll('[data-reuse]').forEach((btn) => {
    btn.addEventListener('click', () => { closeAssetDetail(); reuseAsset(btn.dataset.reuse); });
  });
  $('detail-actions').querySelectorAll('[data-meta]').forEach((btn) => {
    btn.addEventListener('click', () => showMeta(btn.dataset.meta));
  });
  $('detail-actions').querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      await removeAsset(btn.dataset.del);
      closeAssetDetail();
    });
  });

  $('asset-detail').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeAssetDetail() {
  const video = $('detail-video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  $('asset-detail').hidden = true;
  detailDir = null;
  document.body.style.overflow = '';
}

$('detail-close').addEventListener('click', closeAssetDetail);
document.querySelectorAll('[data-detail-close]').forEach((el) => {
  el.addEventListener('click', closeAssetDetail);
});
$('detail-fav').addEventListener('click', async () => {
  if (!detailDir) return;
  const asset = assetData.assets.find((a) => a.dir === detailDir);
  if (!asset) return;
  await toggleFavorite(detailDir, !asset.favorite);
  $('detail-fav').classList.toggle('on', Boolean(asset.favorite));
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('asset-detail').hidden) closeAssetDetail();
});

async function showMeta(dir) {
  try {
    const meta = await api(`/api/history/${encodeURIComponent(dir)}/meta`);
    const text = JSON.stringify(meta, null, 2);
    const win = window.open('', '_blank');
    if (!win) { toast('浏览器拦截了弹窗', true); return; }
    win.document.write(
      `<title>${escapeHtml(dir)} · meta.json</title>` +
      `<pre style="font:12.5px/1.65 ui-monospace,Menlo,Consolas,monospace;padding:22px;white-space:pre-wrap">${escapeHtml(text)}</pre>`
    );
  } catch (error) {
    toast(error.message, true);
  }
}

/* ---------------------------------------------------------------- 日志 */

let logTimer = null;
let logRaw = [];

const LOG_VIEW_ACTIVE = () => $('view-logs').classList.contains('active');

async function loadLogs() {
  const view = $('log-view');
  try {
    const data = await api('/api/logs?lines=800');
    logRaw = data.lines || [];

    if (!data.exists || !logRaw.length) {
      view.innerHTML = '<div class="log-empty">还没有日志。生成一条任务后这里会有内容。</div>';
    } else {
      renderLogLines();
    }

    const totalKb = (data.files || []).reduce((sum, f) => sum + f.size_kb, 0);
    $('log-foot').innerHTML =
      `<span>共 ${logRaw.length} 行 · 日志文件合计 ${totalKb.toFixed(1)} KB</span>` +
      `<span>${escapeHtml(data.file || '')}</span>`;
  } catch (error) {
    view.innerHTML = `<div class="log-empty">读取日志失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderLogLines() {
  const view = $('log-view');
  const keyword = $('log-filter').value.trim().toLowerCase();

  const rows = logRaw.filter((line) => !keyword || line.toLowerCase().includes(keyword));

  if (!rows.length) {
    view.innerHTML = `<div class="log-empty">没有匹配「${escapeHtml(keyword)}」的行。</div>`;
    return;
  }

  view.innerHTML = rows.map((line) => {
    let html = escapeHtml(line);
    // 高亮 request_id 的值，出问题时一眼能找到
    html = html.replace(
      /request_id=([0-9a-f-]{8,})/gi,
      'request_id=<mark>$1</mark>'
    );
    if (keyword) {
      const safe = escapeHtml(keyword).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      html = html.replace(new RegExp(`(${safe})`, 'gi'), '<mark>$1</mark>');
    }
    const cls = /\|\s*ERROR\s*\|/.test(line) ? 'log-line err'
      : /\|\s*WARNING\s*\|/.test(line) ? 'log-line warn' : 'log-line';
    return `<div class="${cls}">${html}</div>`;
  }).join('');

  view.scrollTop = view.scrollHeight;
}

$('log-filter').addEventListener('input', renderLogLines);

$('log-auto').addEventListener('change', (event) => {
  if (event.target.checked) startLogTimer(); else stopLogTimer();
});

function startLogTimer() {
  stopLogTimer();
  logTimer = setInterval(() => { if (LOG_VIEW_ACTIVE()) loadLogs(); }, 3000);
}
function stopLogTimer() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

$('log-reveal').addEventListener('click', async () => {
  try {
    await api('/api/logs/reveal', { method: 'POST' });
  } catch (error) {
    toast(error.message, true);
  }
});

$('log-download').addEventListener('click', () => {
  window.location.href = '/api/logs/download';
});

$('log-export').addEventListener('click', () => {
  window.location.href = '/api/diagnose/export';
  toast('诊断包已开始下载。里面不含 API Key。');
});

$('job-diagnose').addEventListener('click', () => {
  window.location.href = '/api/diagnose/export';
  toast('诊断包已开始下载。里面含这次调用记录的 request_id。');
});

/* ---------------------------------------------------------------- 设置 */

async function loadSettings() {
  const data = await api('/api/settings');
  state.options = data.options;
  state.settings = data.settings;

  const s = data.settings;


  const regionSelect = $('set-region');
  regionSelect.innerHTML = data.options.regions.map((region) => `
    <option value="${region.value}"${region.value === s.region ? ' selected' : ''}>
      ${escapeHtml(region.label)}${region.has_shared ? '' : '（需业务空间 ID）'}
    </option>`).join('');

  $('set-endpoint-style').value = s.endpoint_style;
  $('set-workspace').value = s.workspace_id || '';
  $('set-model').value = s.model;
  $('set-output-dir').value = s.output_dir;
  $('set-poll-interval').value = s.poll_interval_seconds;
  $('set-keep-refs').checked = Boolean(s.keep_reference_copies);

  $('set-helper-enabled').checked = Boolean(s.prompt_helper.enabled);
  $('set-helper-model').value = s.prompt_helper.model;
  $('set-helper-vision').value = s.prompt_helper.vision_model;
  $('set-helper-readimages').checked = Boolean(s.prompt_helper.read_images);

  $('set-api-key').value = '';
  $('key-help').textContent = s.has_api_key
    ? `已保存：${s.api_key_masked}（留空即不修改）`
    : '还没有配置 API Key。';

  renderPricing(s.pricing);
  syncEndpointFields();
  syncModelLabel();
}

function syncEndpointFields() {
  const style = $('set-endpoint-style').value;
  const region = $('set-region').value;
  const regionInfo = state.options.regions.find((r) => r.value === region);
  const needWorkspace = style === 'workspace' || (regionInfo && !regionInfo.has_shared);
  $('workspace-field').style.display = needWorkspace ? 'block' : 'none';
  if (regionInfo && !regionInfo.has_shared && style === 'shared') {
    $('set-endpoint-style').value = 'workspace';
    $('workspace-field').style.display = 'block';
  }
}

$('set-endpoint-style').addEventListener('change', syncEndpointFields);
$('set-region').addEventListener('change', syncEndpointFields);

function renderPricing(pricing) {
  const editor = $('pricing-editor');
  editor.innerHTML = Object.entries(pricing.models).map(([model, tiers]) => `
    <div class="price-model">${escapeHtml(model)}</div>
    <div class="price-row price-head"><span>分辨率</span><span>原价</span><span>折扣</span></div>
    ${Object.entries(tiers).map(([res, value]) => `
      <div class="price-row">
        <span>${escapeHtml(res)}</span>
        <input type="number" step="0.01" min="0" data-model="${escapeHtml(model)}" data-res="${escapeHtml(res)}" data-field="list" value="${value.list}">
        <input type="number" step="0.05" min="0" max="1" data-model="${escapeHtml(model)}" data-res="${escapeHtml(res)}" data-field="discount" value="${value.discount}">
      </div>`).join('')}`).join('');
}

function collectPricing() {
  const pricing = JSON.parse(JSON.stringify(state.settings.pricing));
  $('pricing-editor').querySelectorAll('input[data-model]').forEach((input) => {
    const { model, res, field } = input.dataset;
    const value = Number(input.value);
    if (Number.isFinite(value)) pricing.models[model][res][field] = value;
  });
  return pricing;
}

$('btn-save-settings').addEventListener('click', async () => {
  const payload = {
    region: $('set-region').value,
    endpoint_style: $('set-endpoint-style').value,
    workspace_id: $('set-workspace').value.trim(),
    model: $('set-model').value,
    output_dir: $('set-output-dir').value.trim(),
    poll_interval_seconds: Number($('set-poll-interval').value),
    keep_reference_copies: $('set-keep-refs').checked,
    prompt_helper: {
      enabled: $('set-helper-enabled').checked,
      model: $('set-helper-model').value.trim(),
      vision_model: $('set-helper-vision').value.trim(),
      read_images: $('set-helper-readimages').checked,
    },
    pricing: collectPricing(),
  };

  const key = $('set-api-key').value.trim();
  if (key) payload.api_key = key;

  try {
    const data = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.settings = { ...state.settings, ...data.settings };
    $('set-api-key').value = '';
    $('key-help').textContent = data.settings.has_api_key
      ? `已保存：${data.settings.api_key_masked}（留空即不修改）`
      : '还没有配置 API Key。';
    toast('已保存');
    refreshEstimate();
    syncModelLabel();
  } catch (error) {
    toast(`保存失败：${error.message}`, true);
  }
});

$('btn-test-helper').addEventListener('click', async () => {
  const box = $('helper-result');
  const model = $('set-helper-model').value.trim();

  if (!model) { box.className = 'test-result bad'; box.textContent = '先填一个模型名。'; return; }

  box.className = 'test-result';
  box.textContent = `正在试「${model}」……`;

  try {
    const data = await api('/api/prompt/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
    box.className = 'test-result ' + (data.ok ? 'ok' : 'bad');
    box.textContent = data.message;
  } catch (error) {
    box.className = 'test-result bad';
    box.textContent = error.message;
  }
});

$('btn-test-vision').addEventListener('click', async () => {
  const box = $('vision-result');
  const model = $('set-helper-vision').value.trim();

  if (!model) { box.className = 'test-result bad'; box.textContent = '先填一个视觉模型名。'; return; }

  box.className = 'test-result';
  box.textContent = `正在试视觉模型「${model}」……`;
  try {
    const data = await api('/api/prompt/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, slot: '视觉模型' }),
    });
    box.className = 'test-result ' + (data.ok ? 'ok' : 'bad');
    box.textContent = data.message;
  } catch (error) {
    box.className = 'test-result bad';
    box.textContent = error.message;
  }
});

$('btn-test-settings').addEventListener('click', async () => {
  const box = $('test-result');
  box.className = 'test-result';
  box.textContent = '正在测试……';
  try {
    const data = await api('/api/settings/test', { method: 'POST' });
    box.className = 'test-result ' + (data.ok ? 'ok' : 'bad');
    box.textContent = data.message + (data.base_url ? `\n${data.base_url}` : '');
  } catch (error) {
    box.className = 'test-result bad';
    box.textContent = error.message;
  }
});

function syncSubmitLabel() {
  syncModelLabel();
}

/* ---------------------------------------------------------------- 代码版本监控 */

/**
 * 检测服务端代码有没有在我们看不到的地方被改掉。
 *
 * 为什么需要：改完前端代码后，用户浏览器里跑的仍是旧 JS。旧 JS 配上新服务端，
 * 轻则接口对不上报错，重则提交出错误的参数——而且用户完全看不出来。
 *
 * 前端变了 -> 红色横幅 + 禁用发送，必须刷新才能继续
 * 服务端重启 -> 黄色横幅提示（任务会被自动接回，刷新一下更保险）
 */
let knownVersion = null;

function showStale(text, blocking) {
  const banner = $('stale-banner');
  $('stale-text').textContent = text;
  banner.classList.toggle('blocking', Boolean(blocking));
  banner.hidden = false;
  if (blocking) {
    $('btn-send').disabled = true;
    $('btn-send').title = '页面代码已过期，请先刷新';
  }
}

async function checkVersion() {
  try {
    const v = await api('/api/version');
    if (!knownVersion) { knownVersion = v; return; }

    if (v.web !== knownVersion.web) {
      showStale('网页代码已更新，当前页面还是旧版，继续操作可能出错。请刷新页面。', true);
    } else if (v.instance !== knownVersion.instance) {
      showStale('服务端已重启过。正在跑的任务会自动接回，建议刷新页面。', false);
    }
  } catch {
    /* 服务端可能正好在重启，下一次再查 */
  }
}

$('stale-reload').addEventListener('click', () => window.location.reload());

/* ---------------------------------------------------------------- 启动 */

/**
 * 找服务端正在跑但我们没在跟踪的任务。
 *
 * 主要场景：服务重启后自动接回了中断的任务，那个任务的 job_id 是新的，
 * 前端不知道。不主动查的话用户会以为任务消失了，其实它正在恢复。
 */
async function adoptRunningJobs() {
  try {
    const data = await api('/api/jobs');
    const jobs = data.jobs || [];
    if (!jobs.length) return;

    const job = jobs[0];
    if (state.jobId === job.id) return;

    console.info('接上一个正在运行的任务:', job.dir);
    state.jobId = job.id;
    openJobDrawer();
    $('job-title').textContent = '恢复中断的任务';
    pollJob();
  } catch {
    /* 服务还没起来或网络问题，下次再试 */
  }
}

(async function init() {
  await loadSettings();

  // 拿一次模型资料：顶栏要显示中文模型名，面板打开时也不用再等
  try {
    modelData = await api('/api/models');
  } catch {
    /* 拿不到不影响生成，标签退回显示模型 ID */
  }
  syncModelLabel();

  // 应用默认创作模式（全能参考）——决定输入区显示哪些素材入口
  setComposerMode(state.mode, { keepAssets: true });

  await refreshEstimate();
  syncDurationButton();

  // 打开页面时先找一次，之后每 10 秒兜一次——重启用不着刷新页面
  await adoptRunningJobs();
  setInterval(adoptRunningJobs, 10000);

  // 盯住代码版本。改了前端就应该立刻提示刷新，别让旧 JS 静悄悄地跑。
  await checkVersion();
  setInterval(checkVersion, 15000);

  // 窗口变宽变窄时重画柱状图（SVG 是按容器宽度算的）
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if ($('view-dash').classList.contains('active') && dashData) {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(renderDailyChart, 180);
    }
  });
})();

/* ---------------------------------------------------------------- 声音 */

const voiceState = {
  ready: false,
  options: null,
  profiles: [],
  status: null,
  mode: 'instruct',
  selectedProfileId: '',
  statusTimer: null,
  lastJob: null,
  starting: false,
  droppedFiles: [],
};

function stopVoicePolling() {
  if (voiceState.statusTimer) {
    clearInterval(voiceState.statusTimer);
    voiceState.statusTimer = null;
  }
}

function startVoicePolling() {
  stopVoicePolling();
  voiceState.statusTimer = setInterval(() => { loadVoiceStatus(); }, 8000);
}

function setVoiceStatus(kind, text, note = '') {
  $('voice-dot').className = `voice-dot ${kind || 'off'}`;
  $('voice-status-text').textContent = text;
  $('voice-status-note').textContent = note || '';
}

async function loadVoiceStatus() {
  try {
    const status = await api('/api/voice/status');
    voiceState.status = status;
    if (status.running) voiceState.starting = false;
    if (voiceState.starting && !status.running) return;
    if (!status.running) {
      setVoiceStatus('off', '本机语音服务未启动', status.error || '点「启动服务」拉起本机模型服务');
      $('voice-start').hidden = false;
      $('voice-unload').hidden = true;
      return;
    }
    $('voice-start').hidden = true;
    if (status.loading) {
      setVoiceStatus('warn', '模型加载中…', '首次加载约 30–60 秒，请稍等');
      $('voice-unload').hidden = true;
      return;
    }
    if (status.model_loaded) {
      setVoiceStatus(
        'ok',
        `服务就绪 · ${status.device || '本机'}`,
        `采样率 ${status.sample_rate || '?'} Hz · 空闲 ${status.idle_seconds || 0} 秒后释放显存`,
      );
      $('voice-unload').hidden = false;
      return;
    }
    setVoiceStatus('warn', '服务已启动 · 模型未加载', '第一次生成时会自动加载模型');
    $('voice-unload').hidden = true;
  } catch (err) {
    setVoiceStatus('err', '本机语音服务状态读取失败', err.message);
    $('voice-start').hidden = false;
    $('voice-unload').hidden = true;
  }
}

async function loadVoiceOptions() {
  if (voiceState.options) return;
  voiceState.options = await api('/api/voice/options');
  const emotions = voiceState.options.emotions || [];
  $('voice-emotion').innerHTML = emotions
    .map((item) => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`)
    .join('');
  $('voice-emotion').value = 'angry';
  updateVoiceInstruct();
}

function updateVoiceInstruct() {
  if (!voiceState.options) return;
  const key = $('voice-emotion').value;
  const item = (voiceState.options.emotions || []).find((x) => x.key === key);
  const custom = key === 'custom';
  $('voice-instruct').hidden = !custom;
  if (!custom) $('voice-instruct').value = item ? item.instruct : '';
  if (item && item.speed) {
    $('voice-speed').value = item.speed;
    $('voice-speed-value').textContent = Number(item.speed).toFixed(2);
  }
}

async function loadVoiceProfiles() {
  const data = await api('/api/voice/profiles');
  voiceState.profiles = data.profiles || [];
  renderVoiceProfiles();
  renderVoiceProfileSelect();
}

function renderVoiceProfileSelect() {
  const select = $('voice-tts-profile');
  const previous = voiceState.selectedProfileId || select.value;
  if (!voiceState.profiles.length) {
    select.innerHTML = '<option value="">先建一个音色</option>';
    voiceState.selectedProfileId = '';
    return;
  }
  select.innerHTML = voiceState.profiles
    .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} · ${(p.duration || 0).toFixed(2)}s · ${p.source_count || 1} 段参考</option>`)
    .join('');
  if (voiceState.profiles.some((p) => p.id === previous)) select.value = previous;
  voiceState.selectedProfileId = select.value;
}

function renderVoiceProfiles() {
  const box = $('voice-profile-list');
  if (!voiceState.profiles.length) {
    box.innerHTML = '<div class="voice-empty">还没有音色。先在右边上传一段参考音频或视频。</div>';
    return;
  }
  box.innerHTML = voiceState.profiles.map((p) => `
    <div class="voice-profile-item" data-profile="${escapeHtml(p.id)}">
      <div class="voice-profile-head">
        <span class="voice-profile-name">${escapeHtml(p.name)}</span>
        <span class="voice-profile-meta">${(p.duration || 0).toFixed(2)} 秒 · ${p.source_count || 1} 段参考 · ${escapeHtml(p.created_at || '')}</span>
      </div>
      ${p.prompt_text
        ? `<div class="voice-profile-prompt">参考台词：${escapeHtml(p.prompt_text)}</div>`
        : '<div class="voice-profile-prompt" style="color:var(--text-faint)">还没有参考台词（原声复刻模式需要）</div>'}
      ${(p.warnings || []).length
        ? `<div class="voice-profile-prompt" style="color:var(--danger)">${p.warnings.map(escapeHtml).join('<br>')}</div>`
        : ''}
      <audio controls preload="none" src="${escapeHtml(p.reference_url)}"></audio>
      <div class="voice-profile-actions">
        <button data-action="use">用作合成音色</button>
        <button data-action="transcribe">自动识别台词</button>
        <button data-action="edit">编辑</button>
        <button data-action="delete" class="danger">删除</button>
      </div>
      <div class="voice-edit" hidden>
        <input data-edit="name" value="${escapeHtml(p.name)}" placeholder="音色名称" maxlength="40">
        <textarea data-edit="prompt_text" rows="2" placeholder="参考台词">${escapeHtml(p.prompt_text || '')}</textarea>
        <input data-edit="notes" value="${escapeHtml(p.notes || '')}" placeholder="备注">
        <div class="voice-edit-actions">
          <button class="primary" data-action="save-edit">保存</button>
          <button class="ghost sm" data-action="cancel-edit">取消</button>
        </div>
      </div>
    </div>`).join('');

  box.querySelectorAll('.voice-profile-item').forEach((item) => {
    const id = item.dataset.profile;
    item.querySelectorAll('[data-action]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = button.dataset.action;
        if (action === 'use') selectVoiceProfile(id);
        if (action === 'edit') item.querySelector('.voice-edit').hidden = false;
        if (action === 'cancel-edit') item.querySelector('.voice-edit').hidden = true;
        if (action === 'save-edit') saveVoiceProfileEdit(item, id);
        if (action === 'delete') deleteVoiceProfile(id);
        if (action === 'transcribe') transcribeVoiceProfile(id);
      });
    });
  });
}

function selectVoiceProfile(id) {
  voiceState.selectedProfileId = id;
  $('voice-tts-profile').value = id;
  document.querySelector('.voice-tab[data-voice-tool="tts"]').click();
  $('voice-tts-text').focus();
}

async function saveVoiceProfileEdit(item, id) {
  const name = item.querySelector('[data-edit="name"]').value.trim();
  const prompt_text = item.querySelector('[data-edit="prompt_text"]').value.trim();
  const notes = item.querySelector('[data-edit="notes"]').value.trim();
  try {
    await api(`/api/voice/profiles/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, prompt_text, notes }),
    });
    toast('音色已更新');
    await loadVoiceProfiles();
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteVoiceProfile(id) {
  const profile = voiceState.profiles.find((p) => p.id === id);
  if (!confirm(`删除音色「${profile ? profile.name : id}」？参考音频也会一起删掉。`)) return;
  try {
    await api(`/api/voice/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('音色已删除');
    await loadVoiceProfiles();
  } catch (err) {
    toast(err.message, true);
  }
}

async function transcribeVoiceProfile(id) {
  toast('正在识别参考台词…');
  try {
    const data = await api(`/api/voice/profiles/${encodeURIComponent(id)}/transcribe`, { method: 'POST' });
    await api(`/api/voice/profiles/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt_text: data.text }),
    });
    toast(`识别结果：${data.text}`);
    await loadVoiceProfiles();
  } catch (err) {
    toast(err.message, true);
  }
}

async function createVoiceProfile() {
  const name = $('voice-new-name').value.trim();
  const picked = Array.from($('voice-new-file').files || []);
  const files = picked.length ? picked : voiceState.droppedFiles.slice();
  if (!name) { toast('先填音色名称', true); return; }
  if (!files.length) { toast('先选参考音频或视频', true); return; }

  const form = new FormData();
  form.append('name', name);
  form.append('prompt_text', $('voice-new-prompt').value.trim());
  form.append('notes', $('voice-new-notes').value.trim());
  files.forEach((file) => form.append('files', file));

  $('voice-create').disabled = true;
  $('voice-create-progress').hidden = false;
  $('voice-create-stage').textContent = `上传 ${files.length} 段参考并抽取音频…`;
  try {
    const data = await api('/api/voice/profiles', { method: 'POST', body: form });
    toast(`音色「${data.profile.name}」已保存`);
    $('voice-new-name').value = '';
    $('voice-new-file').value = '';
    voiceState.droppedFiles = [];
    $('voice-create-note').textContent = '';
    $('voice-new-prompt').value = '';
    $('voice-new-notes').value = '';
    voiceState.selectedProfileId = data.profile.id;
    await loadVoiceProfiles();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $('voice-create').disabled = false;
    $('voice-create-progress').hidden = true;
  }
}

async function runVoiceTTS() {
  const profileId = $('voice-tts-profile').value;
  const text = $('voice-tts-text').value.trim();
  if (!profileId) { toast('先建一个音色', true); return; }
  if (!text) { toast('先填台词', true); return; }

  let instruct = '';
  if (voiceState.mode === 'instruct') {
    const key = $('voice-emotion').value;
    if (key === 'custom') {
      instruct = $('voice-instruct').value.trim();
    } else {
      const item = (voiceState.options?.emotions || []).find((x) => x.key === key);
      instruct = item ? item.instruct : '';
    }
    if (!instruct) { toast('情绪指令不能为空', true); return; }
  }

  const payload = {
    profile_id: profileId,
    text,
    mode: voiceState.mode,
    instruct,
    speed: parseFloat($('voice-speed').value) || 1,
    seed: parseInt($('voice-seed').value, 10) || 0,
    variants: parseInt($('voice-variants').value, 10) || 1,
  };

  $('voice-run').disabled = true;
  $('voice-run-progress').hidden = false;
  $('voice-run-stage').textContent = '生成中…首次会加载模型，请稍等';
  $('voice-run-bar').style.width = '35%';
  try {
    const data = await api('/api/voice/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    $('voice-run-bar').style.width = '100%';
    renderVoiceResult(data.job);
    toast('台词已生成并保存');
    await loadVoiceHistory();
    await loadVoiceStatus();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $('voice-run').disabled = false;
    $('voice-run-progress').hidden = true;
    $('voice-run-bar').style.width = '0%';
  }
}

function renderVoiceResult(job) {
  voiceState.lastJob = job;
  const box = $('voice-result');
  if (!job || !(job.variants || []).length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `
    <div class="voice-candidate-head" style="margin-bottom:10px">
      <b>${escapeHtml(job.text)}</b>
      <span class="voice-profile-meta">${escapeHtml(job.profile?.name || '')} · ${job.variants.length} 个候选 · ${escapeHtml(job.created_at || '')}</span>
    </div>
    ${job.variants.map((v) => `
      <div class="voice-candidate">
        <div class="voice-candidate-head">
          <b>候选 ${v.index}</b>
          <span class="voice-profile-meta">${(v.duration || 0).toFixed(2)} 秒 · seed ${v.seed ?? '-'}</span>
        </div>
        <audio controls preload="metadata" src="${escapeHtml(v.wav_url)}"></audio>
        <div class="voice-candidate-actions">
          <a href="${escapeHtml(v.wav_url)}" download>下载 WAV</a>
          ${v.mp3_url ? `<a href="${escapeHtml(v.mp3_url)}" download>下载 MP3</a>` : ''}
          <button data-reveal="${escapeHtml(job.dir)}">打开文件夹</button>
        </div>
      </div>`).join('')}`;
  box.querySelectorAll('[data-reveal]').forEach((button) => {
    button.addEventListener('click', () => revealVoicePath(button.dataset.reveal));
  });
}

async function loadVoiceHistory() {
  try {
    const data = await api('/api/voice/history?limit=30');
    const jobs = data.jobs || [];
    const box = $('voice-history');
    if (!jobs.length) {
      box.innerHTML = '<div class="voice-empty">还没有生成记录。</div>';
      return;
    }
    box.innerHTML = jobs.map((job) => {
      const variant = (job.variants || [])[0];
      return `<div class="voice-history-item">
        <div class="voice-history-text">${escapeHtml(job.text)}</div>
        <div class="voice-history-meta">${escapeHtml(job.profile?.name || '')} · ${(variant?.duration || 0).toFixed(2)} 秒 · ${escapeHtml((job.created_at || '').replace('T', ' ').slice(0, 16))}</div>
        ${variant ? `<audio controls preload="none" src="${escapeHtml(variant.wav_url)}"></audio>` : ''}
        <div class="voice-history-actions">
          ${variant ? `<a href="${escapeHtml(variant.wav_url)}" download>下载 WAV</a>` : ''}
          <button data-reveal="${escapeHtml(job.dir)}">打开文件夹</button>
          <button class="danger" data-delete="${escapeHtml(job.id)}">删除</button>
        </div>
      </div>`;
    }).join('');
    box.querySelectorAll('[data-reveal]').forEach((button) => {
      button.addEventListener('click', () => revealVoicePath(button.dataset.reveal));
    });
    box.querySelectorAll('[data-delete]').forEach((button) => {
      button.addEventListener('click', () => deleteVoiceJob(button.dataset.delete));
    });
  } catch (err) {
    $('voice-history').innerHTML = `<div class="voice-empty">${escapeHtml(err.message)}</div>`;
  }
}

async function revealVoicePath(path) {
  try {
    await api('/api/voice/reveal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteVoiceJob(id) {
  if (!confirm('删除这条生成记录和音频文件？')) return;
  try {
    await api(`/api/voice/history/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('已删除');
    await loadVoiceHistory();
  } catch (err) {
    toast(err.message, true);
  }
}

async function startVoiceService() {
  voiceState.starting = true;
  $('voice-start').disabled = true;
  setVoiceStatus('warn', '正在启动本机语音服务…', '首次启动会创建进程，模型在第一次生成时才加载');
  try {
    await api('/api/voice/service/start', { method: 'POST' });
    voiceState.starting = false;
    toast('本机语音服务已启动');
    await loadVoiceStatus();
  } catch (err) {
    voiceState.starting = false;
    setVoiceStatus('err', '启动失败', err.message);
    toast(err.message, true);
  } finally {
    $('voice-start').disabled = false;
  }
}

async function unloadVoiceService() {
  $('voice-unload').disabled = true;
  try {
    await api('/api/voice/service/unload', { method: 'POST' });
    toast('已释放显存');
    await loadVoiceStatus();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $('voice-unload').disabled = false;
  }
}

function initVoiceTab() {
  if (!voiceState.ready) {
    voiceState.ready = true;

    document.querySelectorAll('.voice-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.voice-tab').forEach((t) => t.classList.remove('active'));
        document.querySelectorAll('.voice-pane').forEach((p) => p.classList.remove('active'));
        tab.classList.add('active');
        $(`voice-pane-${tab.dataset.voiceTool}`).classList.add('active');
        if (tab.dataset.voiceTool === 'tts') loadVoiceHistory();
      });
    });

    $('voice-create').addEventListener('click', createVoiceProfile);
    $('voice-new-file').addEventListener('change', () => {
      if ($('voice-new-file').files.length) {
        voiceState.droppedFiles = [];
        $('voice-create-note').textContent = '';
      }
    });
    $('voice-run').addEventListener('click', runVoiceTTS);
    $('voice-start').addEventListener('click', startVoiceService);
    $('voice-unload').addEventListener('click', unloadVoiceService);
    $('voice-emotion').addEventListener('change', updateVoiceInstruct);
    $('voice-speed').addEventListener('input', () => {
      $('voice-speed-value').textContent = parseFloat($('voice-speed').value).toFixed(2);
    });
    document.querySelectorAll('#voice-mode-seg [data-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        document.querySelectorAll('#voice-mode-seg [data-mode]').forEach((b) => b.classList.remove('on'));
        button.classList.add('on');
        voiceState.mode = button.dataset.mode;
        $('voice-mode-note').textContent = voiceState.mode === 'instruct'
          ? '用自然语言控制语气，推荐。'
          : '按参考台词复刻音色；需要台词与音频逐段对应，多段参考建议先用情绪指令。';
      });
    });
    $('voice-tts-profile').addEventListener('change', () => {
      voiceState.selectedProfileId = $('voice-tts-profile').value;
    });
  }

  loadVoiceOptions().catch((err) => toast(err.message, true));
  loadVoiceProfiles().catch((err) => toast(err.message, true));
  loadVoiceStatus();
  loadVoiceHistory();
  startVoicePolling();

  const voiceTool = new URLSearchParams(location.search).get('voice');
  if (voiceTool) {
    const target = document.querySelector(`.voice-tab[data-voice-tool="${voiceTool}"]`);
    if (target) target.click();
  }
}

// 支持 ?view=voice 直接打开某个标签页（方便分享和自动化截图）
(() => {
  const initialView = new URLSearchParams(location.search).get('view');
  if (!initialView) return;
  const tab = document.querySelector(`.tab[data-view="${initialView}"]`);
  if (tab) tab.click();
})();

/* ---------------------------------------------------------------- 全页面拖拽素材 */

/**
 * 图片 / 视频 / 音频可以直接拖进页面：
 *   创作页      -> 加入参考素材（沿用 uploadFiles）
 *   去水印页    -> 载入视频/图片
 *   超清页      -> 载入视频
 *   图片拼接    -> 放进选中的格子
 *   声音页      -> 加入音色参考（等填名称后保存）
 *   分析大师    -> 作为待分析视频
 * 画布里每个格子自己的 drop 事件会先 preventDefault，这里会自动跳过，
 * 不会把同一张图重复加进参考素材。
 */
(() => {
  const overlay = $('drop-overlay');
  const overlayText = $('drop-overlay-text');
  const overlaySub = $('drop-overlay-sub');
  let depth = 0;

  const hasFiles = (event) => {
    const types = event.dataTransfer?.types;
    return types ? [...types].includes('Files') : false;
  };
  const isVideoFile = (file) => (
    file.type.startsWith('video/')
    || /\.(mp4|mov|mkv|avi|webm|flv|m4v|wmv|ts)$/i.test(file.name)
  );
  const isImageFile = (file) => (
    file.type.startsWith('image/')
    || /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(file.name)
  );
  const isAudioFile = (file) => (
    file.type.startsWith('audio/')
    || /\.(wav|mp3|m4a|flac|aac|ogg|wma|opus)$/i.test(file.name)
  );

  function dropContext() {
    const view = document.querySelector('.view.active')?.id || '';
    const tool = document.querySelector('.post-tab.active')?.dataset.tool || '';
    if (view === 'view-create') {
      return { key: 'create', title: '松开即可添加参考素材', sub: '图片 / 视频 / 音频 · 自动识别类型' };
    }
    if (view === 'view-canvas' && tool === 'watermark') {
      return { key: 'watermark', title: '松开即可载入去水印素材', sub: '视频 / 图片' };
    }
    if (view === 'view-canvas' && tool === 'upscale') {
      return { key: 'upscale', title: '松开即可载入超清视频', sub: 'MP4 / MOV 等视频文件' };
    }
    if (view === 'view-canvas' && tool === 'canvas') {
      return { key: 'collage', title: '松开即可放入选中的格子', sub: '图片' };
    }
    if (view === 'view-voice') {
      return { key: 'voice', title: '松开即可加入音色参考', sub: '音频 / 视频（保存音色时使用）' };
    }
    if (view === 'view-analysis') {
      return { key: 'analysis', title: '松开即可载入分析视频', sub: 'MP4 / MOV' };
    }
    return null;
  }

  function showOverlay(context) {
    if (!context) return;
    overlayText.textContent = context.title;
    overlaySub.textContent = context.sub;
    overlay.hidden = false;
  }

  function hideOverlay() {
    depth = 0;
    overlay.hidden = true;
  }

  document.addEventListener('dragenter', (event) => {
    if (!hasFiles(event) || event.target.closest?.('input[type=file]')) return;
    event.preventDefault();
    depth += 1;
    const context = dropContext();
    if (context) showOverlay(context);
  });

  document.addEventListener('dragover', (event) => {
    if (!hasFiles(event) || event.target.closest?.('input[type=file]')) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    const context = dropContext();
    if (context && overlay.hidden) showOverlay(context);
  });

  document.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    depth -= 1;
    if (depth <= 0 || event.relatedTarget === null) hideOverlay();
  });

  async function handleDroppedFiles(files) {
    const context = dropContext();
    if (!context) {
      toast('当前页面不接收拖拽，先切到创作 / 后处理 / 声音 / 分析页', true);
      return;
    }

    if (context.key === 'create') {
      await uploadFiles(files);
      toast(`已添加 ${files.length} 个素材`);
      return;
    }

    if (context.key === 'watermark') {
      const media = files.find((file) => isVideoFile(file) || isImageFile(file));
      if (!media) {
        toast('去水印只支持视频或图片', true);
        return;
      }
      if (files.length > 1) toast('去水印一次载入一个文件，已取第一个');
      await wmUpload(media);
      return;
    }

    if (context.key === 'upscale') {
      const video = files.find(isVideoFile);
      if (!video) {
        toast('超清需要视频文件', true);
        return;
      }
      try {
        const form = new FormData();
        form.append('files', video);
        const data = await api('/api/staging', { method: 'POST', body: form });
        const info = data.files[0];
        upscaleSource = { type: 'staged', value: info.staged_path, label: info.name };
        $('ups-chosen').textContent = `${info.name}（${info.width || '?'}×${info.height || '?'}）`;
        $('ups-source').value = '';
        toast('视频已载入，选好目标后点开始画质增强');
      } catch (error) {
        toast(`载入失败：${error.message}`, true);
      }
      return;
    }

    if (context.key === 'collage') {
      const image = files.find(isImageFile);
      if (!image) {
        toast('图片拼接需要图片文件', true);
        return;
      }
      await loadImageIntoCell(canvasState.selected ?? 0, image);
      toast('已放入当前选中的格子');
      return;
    }

    if (context.key === 'voice') {
      const media = files.filter((file) => isAudioFile(file) || isVideoFile(file));
      if (!media.length) {
        toast('音色参考需要音频或视频文件', true);
        return;
      }
      voiceState.droppedFiles = media;
      $('voice-create-note').textContent =
        `已拖入 ${media.length} 个文件：${media.map((f) => f.name).join('、').slice(0, 80)}；填好名称后点「保存音色」`;
      toast(`已加入 ${media.length} 段音色参考`);
      return;
    }

    if (context.key === 'analysis') {
      const video = files.find((file) => (
        file.type.startsWith('video/') || /\.(mp4|mov)$/i.test(file.name)
      ));
      if (!video) {
        toast('分析大师需要 MP4 / MOV 视频', true);
        return;
      }
      analysisDroppedFile = video;
      $('analysis-file').value = '';
      $('analysis-asset').value = '';
      $('analysis-status').textContent = `已拖入「${video.name}」，点「开始分析」即可。`;
      $('analysis-status').className = 'analysis-status';
      toast('视频已载入分析大师');
    }
  }

  document.addEventListener('drop', async (event) => {
    if (!hasFiles(event) || event.target.closest?.('input[type=file]')) return;
    // 画布格子自己处理了 drop：defaultPrevented 已经是 true，交给它
    if (event.defaultPrevented) {
      hideOverlay();
      return;
    }
    event.preventDefault();
    const files = [...(event.dataTransfer?.files || [])];
    hideOverlay();
    if (!files.length) return;
    try {
      await handleDroppedFiles(files);
    } catch (error) {
      toast(`拖入处理失败：${error.message}`, true);
    }
  });
})();
