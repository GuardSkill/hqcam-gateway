# -*- coding: utf-8 -*-
"""网关 Web 界面（登录页 + 控制台）。

为什么单独放一个模块：
  hq_live_server.py 已经 1500+ 行，页面 HTML/CSS/JS 塞进去会没法维护。
  这里只放**纯字符串常量**，不做任何 import，避免循环依赖。

两个页面：
  LOGIN_HTML  —— 输入 HQ_TOKEN 的登录页（未授权时显示）
  APP_HTML    —— 控制台：预览 / 云台 / 音量 / 喇叭测试 / WAV 喊话 / 状态

设计约束：
  * 内网 NAS 环境，**不能引外部 CDN**（字体/图标/框架全部内联）
  * 手机也要能用 → 响应式 + 触摸目标 ≥ 44px
  * 所有请求走同源 Cookie（登录后自动带 hq_token），URL 里不再需要 token
"""

# =============================================================================
# 登录页
# =============================================================================
LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>海雀摄像头 · 登录</title>
<style>
:root{--bg:#0f1115;--card:#171b22;--line:#262c37;--fg:#e6eaf2;--dim:#8b96a8;
--acc:#4da3ff;--err:#ff5c5c}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,#1b2432 0%,var(--bg) 55%);
color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;padding:20px}
.card{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);
border-radius:18px;padding:32px 26px 24px;box-shadow:0 24px 60px rgba(0,0,0,.45)}
.logo{width:56px;height:56px;border-radius:16px;margin:0 auto 16px;display:flex;
align-items:center;justify-content:center;font-size:28px;
background:linear-gradient(135deg,#1e3a5f,#12202f);border:1px solid #2b4a6f}
h1{margin:0 0 6px;font-size:19px;text-align:center;font-weight:600;letter-spacing:.3px}
.sub{margin:0 0 22px;text-align:center;color:var(--dim);font-size:13px}
label{display:block;font-size:12px;color:var(--dim);margin-bottom:7px}
input[type=password]{width:100%;padding:13px 14px;border-radius:11px;font-size:15px;
background:#0e1218;border:1px solid var(--line);color:var(--fg);outline:none;
transition:border-color .15s,box-shadow .15s;font-family:inherit}
input[type=password]:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(77,163,255,.15)}
button{width:100%;margin-top:14px;padding:13px;border:0;border-radius:11px;cursor:pointer;
background:var(--acc);color:#06121f;font-size:15px;font-weight:600;font-family:inherit;
transition:filter .15s,transform .06s}
button:hover{filter:brightness(1.08)}
button:active{transform:scale(.985)}
.err{margin:14px 0 0;min-height:18px;font-size:13px;color:var(--err);text-align:center}
.hint{margin:18px 0 0;padding-top:16px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px;line-height:1.7}
code{background:#0e1218;padding:2px 6px;border-radius:5px;color:#9ecbff;font-size:11.5px}
</style>
</head>
<body>
<form class="card" method="POST" action="/login">
  <div class="logo">🐦</div>
  <h1>海雀摄像头</h1>
  <p class="sub">本地网关 · 需要访问令牌</p>
  <label for="tk">访问令牌（HQ_TOKEN）</label>
  <input id="tk" type="password" name="token" placeholder="粘贴部署时设置的令牌"
         autocomplete="current-password" autofocus required>
  <button type="submit">进 入</button>
  <p class="err">{{ERR}}</p>
  <p class="hint">令牌是部署时环境变量 <code>HQ_TOKEN</code> 的值。<br>
  登录成功后浏览器会记住一年，之后从飞牛图标点进来不用再输。</p>
</form>
</body>
</html>
"""


# =============================================================================
# 控制台
# =============================================================================
APP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>海雀摄像头 · 控制台</title>
<style>
:root{--bg:#0f1115;--card:#171b22;--card2:#1c222b;--line:#262c37;--fg:#e6eaf2;
--dim:#8b96a8;--acc:#4da3ff;--ok:#3ddc84;--warn:#ffb020;--err:#ff5c5c}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
padding-bottom:env(safe-area-inset-bottom)}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;
padding:12px 16px;background:rgba(15,17,21,.86);backdrop-filter:blur(12px);
border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:15px;font-weight:600;letter-spacing:.3px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:0 0 auto;
box-shadow:0 0 0 0 rgba(61,220,132,.5)}
.dot.on{background:var(--ok);animation:pulse 2s infinite}
.dot.bad{background:var(--err)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(61,220,132,.45)}
70%{box-shadow:0 0 0 7px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
.spacer{flex:1}
.hbtn{background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:9px;
padding:7px 11px;font-size:12px;cursor:pointer;font-family:inherit;text-decoration:none}
.hbtn:hover{color:var(--fg);border-color:#38414f}
main{max-width:1180px;margin:0 auto;padding:16px;display:grid;gap:14px;
grid-template-columns:repeat(12,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:15px 16px}
.card h2{margin:0 0 13px;font-size:13px;font-weight:600;color:var(--dim);
letter-spacing:.6px;text-transform:uppercase;display:flex;align-items:center;gap:7px}
.card h2 .k{font-size:11px;color:#5d6879;font-weight:400;text-transform:none;letter-spacing:0}
.video{grid-column:span 8;padding:0;overflow:hidden;position:relative;background:#000}
.video .wrap{position:relative;width:100%;aspect-ratio:16/9;background:#05070a;
display:flex;align-items:center;justify-content:center}
.video img{width:100%;height:100%;object-fit:contain;display:block}
.video .ph{position:absolute;inset:0;display:flex;flex-direction:column;gap:8px;
align-items:center;justify-content:center;color:#4a5666;font-size:13px}
.video .bar{position:absolute;left:0;right:0;bottom:0;display:flex;gap:8px;padding:10px 12px;
background:linear-gradient(transparent,rgba(0,0,0,.72));opacity:0;transition:opacity .2s}
.video:hover .bar,.video:focus-within .bar{opacity:1}
.ptz{grid-column:span 4}
.side{grid-column:span 4}
.wide{grid-column:span 8}
.full{grid-column:span 12}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
audio{color-scheme:dark;flex:1;min-width:0;height:38px}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:250px;margin:0 auto}
.pad button{aspect-ratio:1;border-radius:13px;border:1px solid var(--line);background:var(--card2);
color:var(--fg);font-size:19px;cursor:pointer;display:flex;align-items:center;justify-content:center;
transition:background .12s,transform .06s;font-family:inherit}
.pad button:hover{background:#232b36}
.pad button:active,.pad button.act{background:var(--acc);color:#06121f;transform:scale(.95)}
.pad .mid{border-style:dashed;color:var(--dim);font-size:13px}
.seg{display:flex;gap:6px;margin-top:12px}
.seg button{flex:1;padding:9px 4px;border-radius:9px;border:1px solid var(--line);
background:transparent;color:var(--dim);font-size:12px;cursor:pointer;font-family:inherit}
.seg button.on{background:rgba(77,163,255,.14);border-color:var(--acc);color:var(--acc)}
.row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.row:last-child{margin-bottom:0}
.row label{font-size:12.5px;color:var(--dim);min-width:44px}
input[type=range]{flex:1;-webkit-appearance:none;height:5px;border-radius:3px;
background:linear-gradient(90deg,var(--acc) var(--p,50%),#2b3341 var(--p,50%));outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:19px;height:19px;
border-radius:50%;background:#fff;border:3px solid var(--acc);cursor:pointer}
input[type=range]::-moz-range-thumb{width:15px;height:15px;border-radius:50%;background:#fff;
border:3px solid var(--acc);cursor:pointer}
.val{font-variant-numeric:tabular-nums;font-size:13px;min-width:34px;text-align:right;color:var(--fg)}
.btn{padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card2);
color:var(--fg);font-size:13px;cursor:pointer;font-family:inherit;transition:background .12s}
.btn:hover{background:#232b36}
.btn.pri{background:var(--acc);border-color:var(--acc);color:#06121f;font-weight:600}
.btn.pri:hover{filter:brightness(1.08)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btns{display:flex;flex-wrap:wrap;gap:8px}
.file{display:none}
.kv{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;font-size:12.5px}
.kv dt{color:var(--dim)}
.kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;
background:rgba(61,220,132,.13);color:var(--ok)}
.tag.warn{background:rgba(255,176,32,.13);color:var(--warn)}
.tag.err{background:rgba(255,92,92,.13);color:var(--err)}
details{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
summary{cursor:pointer;font-size:12px;color:var(--dim);outline:none}
details .links{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
details .links a{font-size:11.5px;color:#8fb8e0;text-decoration:none;padding:5px 9px;
border:1px solid var(--line);border-radius:7px;background:#141920}
details .links a:hover{color:var(--acc);border-color:#38506e}
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,80px);z-index:60;
background:#232c39;border:1px solid #334052;color:var(--fg);padding:11px 18px;
border-radius:11px;font-size:13px;opacity:0;transition:all .22s;pointer-events:none;
max-width:88vw;text-align:center;box-shadow:0 12px 30px rgba(0,0,0,.4)}
#toast.show{opacity:1;transform:translate(-50%,0)}
@media(max-width:900px){
  .video,.ptz,.side,.wide,.full{grid-column:span 12}
  .two{grid-template-columns:1fr;gap:14px}
  main{padding:12px;gap:12px}
}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>海雀摄像头 2S 3K</h1>
  <span class="spacer"></span>
  <span class="tag" id="uptag">—</span>
  <a class="hbtn" href="/logout">退出</a>
</header>

<main>
  <!-- 实时预览 -->
  <section class="card video">
    <div class="wrap">
      <img id="live" alt="实时画面">
      <div class="ph" id="ph">
        <span style="font-size:22px">📷</span>
        <span id="phtxt">正在连接画面…</span>
      </div>
      <div class="bar">
        <button class="btn" onclick="snap()">📸 抓拍</button>
        <button class="btn" onclick="refreshLive()">⟳ 重连</button>
      </div>
    </div>
  </section>

  <!-- 云台 -->
  <section class="card ptz">
    <h2>云台 <span class="k">PtzCtrl</span></h2>
    <div class="pad">
      <button data-dir="upleft">↖</button>
      <button data-dir="up">↑</button>
      <button data-dir="upright">↗</button>
      <button data-dir="left">←</button>
      <button data-dir="stop" class="mid">停</button>
      <button data-dir="right">→</button>
      <button data-dir="downleft">↙</button>
      <button data-dir="down">↓</button>
      <button data-dir="downright">↘</button>
    </div>
    <div class="seg" id="ptzSeg">
      <button data-ms="200">短 200ms</button>
      <button data-ms="500" class="on">中 500ms</button>
      <button data-ms="1200">长 1.2s</button>
    </div>
  </section>

  <!-- 音量 + 状态 -->
  <section class="card side">
    <h2>喇叭音量 <span class="k">Get/SetDeviceVol</span></h2>
    <div class="row">
      <label>音量</label>
      <input type="range" id="vol" min="0" max="100" value="80">
      <span class="val" id="volv">—</span>
    </div>
    <div class="btns">
      <button class="btn pri" onclick="applyVol()">应用音量</button>
      <button class="btn" onclick="readVol()">读取当前</button>
    </div>
    <p style="margin:12px 0 0;font-size:11.5px;color:#5d6879;line-height:1.6">
      设备音量为 0 时对讲会「成功但没声音」，喊话前建议 ≥ 80。</p>
  </section>

  <!-- 对讲 -->
  <section class="card wide">
    <h2>对讲 / 喊话 <span class="k">StartTalkback · A-law 16kHz</span></h2>
    <div class="btns" style="margin-bottom:12px">
      <button class="btn" onclick="tone(1000,1500,this)">🔊 1kHz 1.5s</button>
      <button class="btn" onclick="tone(600,3000,this)">🔊 600Hz 3s</button>
      <button class="btn" onclick="tone(1500,800,this)">🔊 1.5kHz 0.8s</button>
    </div>
    <div class="row" style="margin-top:14px">
      <label>WAV</label>
      <input type="file" id="wav" class="file" accept=".wav,audio/wav,audio/*">
      <button class="btn pri" onclick="document.getElementById('wav').click()">上传 WAV 喊话</button>
      <span style="font-size:11.5px;color:#5d6879">16kHz 单声道最佳</span>
    </div>
    <p style="margin:10px 0 0;font-size:11.5px;color:#5d6879;line-height:1.6">
      浏览器实时麦克风需要 HTTPS（http 下浏览器禁用录音），所以网页端用「上传 WAV」替代。</p>
  </section>

  <!-- 声音 + 状态 -->
  <section class="card full">
    <h2>现场声音 &amp; 运行状态</h2>
    <div class="two">
      <div>
        <div class="row">
          <audio id="aud" controls></audio>
          <button class="btn" onclick="listen(this)">▶ 听现场</button>
        </div>
        <p style="margin:10px 0 0;font-size:11.5px;color:#5d6879;line-height:1.6">
          点击「听现场」拉取实时音频流（A-law 16kHz → WAV）。</p>
      </div>
      <dl class="kv">
        <dt>视频</dt><dd id="k-video">—</dd>
        <dt>音频</dt><dd id="k-audio">—</dd>
        <dt>运行</dt><dd id="k-up">—</dd>
        <dt>重连</dt><dd id="k-re">—</dd>
        <dt>对讲已发</dt><dd id="k-talk">—</dd>
      </dl>
    </div>
    <details>
      <summary>高级 / 调试接口</summary>
      <div class="links">
        <a href="/av.ts">/av.ts 音视频合流</a>
        <a href="/live.ts">/live.ts 纯视频 TS</a>
        <a href="/audio.wav">/audio.wav</a>
        <a href="/status.json">/status.json</a>
        <a href="/rx.json">/rx.json</a>
      </div>
    </details>
  </section>
</main>

<div id="toast"></div>
<script>
(function(){
  var $ = function(s){ return document.querySelector(s); };
  var toastT = null;
  function toast(msg, ms){
    var t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastT);
    toastT = setTimeout(function(){ t.classList.remove('show'); }, ms || 2200);
  }
  async function api(path, opt){
    var r = await fetch(path, opt || {});
    if (r.status === 401) { location.href = '/login'; throw new Error('需要登录'); }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r;
  }
  async function jget(path){ return (await api(path)).json(); }
  window.jget = jget;

  /* ---------------- 预览 ---------------- */
  var live = $('#live'), ph = $('#ph'), phtxt = $('#phtxt');
  var liveOk = false;
  function refreshLive(){
    liveOk = false;
    ph.style.display = 'flex';
    phtxt.textContent = '正在连接画面…';
    live.src = '/mjpeg?w=960&fps=2&t=' + Date.now();
  }
  live.addEventListener('load', function(){
    liveOk = true; ph.style.display = 'none';
  });
  live.addEventListener('error', function(){
    if (liveOk) return;
    phtxt.textContent = '画面加载失败，点「重连」或稍候自动重试';
    setTimeout(refreshLive, 6000);
  });
  window.refreshLive = refreshLive;
  refreshLive();

  /* ---------------- 云台 ---------------- */
  var ptzMs = 500;
  document.querySelectorAll('#ptzSeg button').forEach(function(b){
    b.addEventListener('click', function(){
      ptzMs = +b.dataset.ms;
      document.querySelectorAll('#ptzSeg button').forEach(function(x){ x.classList.remove('on'); });
      b.classList.add('on');
    });
  });
  document.querySelectorAll('[data-dir]').forEach(function(b){
    b.addEventListener('click', async function(){
      var dir = b.dataset.dir;
      b.classList.add('act');
      setTimeout(function(){ b.classList.remove('act'); }, dir === 'stop' ? 250 : ptzMs);
      try {
        await jget('/ptz?dir=' + dir + '&ms=' + ptzMs);
        if (dir !== 'stop') toast('云台 ' + dir);
      } catch (e) { toast('云台失败：' + e.message); }
    });
  });

  /* ---------------- 音量 ---------------- */
  var vol = $('#vol'), volv = $('#volv');
  function paintVol(){
    vol.style.setProperty('--p', vol.value + '%');
    volv.textContent = vol.value;
  }
  vol.addEventListener('input', paintVol);
  paintVol();
  async function readVol(){
    try {
      var d = await jget('/vol');
      vol.value = d.read_after; paintVol();
      toast('当前音量 ' + d.read_after);
    } catch (e) { toast('读取失败：' + e.message); }
  }
  async function applyVol(){
    var v = +vol.value;
    try {
      var d = await jget('/vol?v=' + v);
      paintVol();
      toast(d.read_after === v ? ('音量已设为 ' + v) : ('设置后读到 ' + d.read_after));
    } catch (e) { toast('设置失败：' + e.message); }
  }
  window.readVol = readVol; window.applyVol = applyVol;

  /* ---------------- 喇叭测试 ---------------- */
  async function tone(hz, ms, btn){
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = '播放中…';
    try {
      await jget('/talk/tone?hz=' + hz + '&ms=' + ms + '&via=auto&vol=100');
      toast('已播放 ' + hz + 'Hz / ' + ms + 'ms');
    } catch (e) { toast('失败：' + e.message); }
    btn.disabled = false; btn.textContent = old;
  }
  window.tone = tone;

  /* ---------------- WAV 上传喊话 ---------------- */
  $('#wav').addEventListener('change', async function(e){
    var f = e.target.files[0];
    if (!f) return;
    toast('正在上传 ' + f.name + ' …', 8000);
    try {
      await api('/talk.wav?via=auto&vol=100', {
        method: 'POST', body: f, headers: { 'Content-Type': 'audio/wav' }
      });
      toast('喊话已发送');
    } catch (err) { toast('上传失败：' + err.message); }
    e.target.value = '';
  });

  /* ---------------- 抓拍 ---------------- */
  function snap(){
    var a = document.createElement('a');
    var ts = new Date();
    function p(n){ return (n < 10 ? '0' : '') + n; }
    a.download = 'hqcam_' + ts.getFullYear() + p(ts.getMonth()+1) + p(ts.getDate()) + '_' +
                 p(ts.getHours()) + p(ts.getMinutes()) + p(ts.getSeconds()) + '.jpg';
    a.href = '/snapshot.jpg?t=' + Date.now();
    document.body.appendChild(a); a.click(); a.remove();
    toast('已保存快照');
  }
  window.snap = snap;

  /* ---------------- 现场声音 ---------------- */
  function listen(btn){
    var a = $('#aud');
    a.src = '/audio.wav?t=' + Date.now();
    var p = a.play();
    if (p && p.catch) p.catch(function(){ toast('浏览器拦截了自动播放，请再点播放键'); });
    toast('正在拉取音频流…');
  }
  window.listen = listen;

  /* ---------------- 状态轮询 ---------------- */
  var dot = $('#dot'), uptag = $('#uptag');
  function fmtBytes(n){
    if (n == null) return '—';
    var u = ['B','KB','MB','GB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + u[i];
  }
  function fmtUp(s){
    s = Math.floor(s || 0);
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    if (d) return d + ' 天 ' + h + ' 小时';
    if (h) return h + ' 小时 ' + m + ' 分';
    return m + ' 分 ' + (s % 60) + ' 秒';
  }
  async function tick(){
    try {
      var s = await jget('/status.json');
      dot.className = 'dot ' + (s.state === 'live' ? 'on' : (s.state === 'idle' ? '' : 'bad'));
      uptag.textContent = s.state === 'live' ? '在线' : s.state;
      uptag.className = 'tag ' + (s.state === 'live' ? '' : 'warn');
      $('#k-video').textContent = '2880×1620 HEVC · ' + (s.frames || 0) + ' 帧 · ' +
                                  fmtBytes(s.bytes);
      var a = s.audio || {};
      $('#k-audio').textContent = 'A-law 16kHz · ' + (a.frames || 0) + ' 帧 · ' +
                                  fmtBytes(a.bytes);
      $('#k-up').textContent = fmtUp(s.uptime);
      $('#k-re').textContent = (s.reconnects || 0) + ' 次';
      $('#k-talk').textContent = (a.talk_frames || 0) + ' 帧';
    } catch (e) {
      dot.className = 'dot bad'; uptag.textContent = '离线'; uptag.className = 'tag err';
    }
  }
  tick();
  setInterval(tick, 3000);
})();
</script>
</body>
</html>
"""
