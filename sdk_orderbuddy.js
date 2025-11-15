// sdk_orderbuddy.js — Integrate HeyGen SDK with OrderBuddy SSE bridge
// SDK mode is default. Pass ?legacy=1 to use legacy path.

(function(){
  const params = new URLSearchParams(location.search);
  if (params.get('legacy') === '1') return; // legacy explicitly requested

  const statusEl = document.getElementById('status');
  const media = document.getElementById('mediaElement');
  const autoplayHint = document.getElementById('autoplayHint');
  function log(m){ if(statusEl){ statusEl.innerHTML += String(m) + '<br>'; statusEl.scrollTop = statusEl.scrollHeight; } }

  let client = null;
  let session = null;
  let lastReq = null;
  let hbTimer = null;
  let playTimer = null;
  let speakQueue = [];
  let draining = false;
  let readyToSpeak = false;
  let firstDrainDone = false;

  async function ensureSDK(){
    try { return await import('https://esm.sh/@heygen/streaming-avatar@2?bundle'); }
    catch(e1){ try{ return await import('/node_modules/@heygen/streaming-avatar/lib/index.esm.js'); }
      catch(e2){ try{ return await import('https://unpkg.com/@heygen/streaming-avatar@2/lib/index.esm.js'); }
        catch(e3){ log('SDK load failed'); throw e3; } } }
  }

  async function getJSON(url){ const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+': '+r.status); return r.json(); }

  async function pickInteractiveAvatarId(){
    try{ const r=await getJSON('/heyg/avatars'); const a=(r.data||[]).find(v=>v?.is_interactive||v?.isInteractive); return a?.avatar_id||''; }catch{ return ''; }
  }

  function startHeartbeat(){ stopHeartbeat(); hbTimer = setInterval(async()=>{ try{ if(client&&session) await client.keepAlive(); }catch{} }, 20000); }
  function stopHeartbeat(){ if(hbTimer){ clearInterval(hbTimer); hbTimer=null; } }

  function ensurePlay(media){
    if (!media) return;
    const tryPlay = () => media.play().catch(()=>{});
    tryPlay();
    if (playTimer) clearInterval(playTimer);
    playTimer = setInterval(()=>{
      if (!media.paused && !media.ended && media.readyState >= 2) {
        clearInterval(playTimer); playTimer = null; return;
      }
      tryPlay();
    }, 1000);
  }

  function waitForPlaying(el, timeoutMs=4000){
    return new Promise((resolve)=>{
      const t0 = Date.now();
      let okTicks = 0;
      const tick = () => {
        if (el && !el.paused && !el.ended && el.readyState >= 2){
          okTicks++;
          if (okTicks >= 2) return resolve();
        }
        if (Date.now() - t0 > timeoutMs) return resolve();
        setTimeout(tick, 100);
      };
      tick();
    });
  }

  function canSpeak(){ return !!(client && session && readyToSpeak); }

  async function sayOnce(text){
    if (!text || !canSpeak()) return false;
    try {
      await client.speak({ text, taskType: 'repeat', taskMode: 'async' });
      log('SDK: spoke.');
      return true;
    } catch (err) {
      const msg = (String(err?.message||'') + ' ' + String(err?.responseText||'')).toLowerCase();
      if (msg.includes('invalid session') || msg.includes('10002') || msg.includes('session is not in correct state')){
        // Re-enqueue and let recovery happen
        speakQueue.unshift(text);
        return false;
      }
      log('SDK: speak failed: ' + (err?.message||err));
      return false;
    }
  }

  async function drainQueue(){
    if (draining) return; draining = true;
    try {
      while (speakQueue.length && canSpeak()){
        const text = speakQueue.shift();
        const ok = await sayOnce(text);
        if (!ok) break;
      }
    } finally { draining = false; }
  }

  async function attachHandlers(mod){
    const StreamingAvatar = mod && (mod.default || mod.StreamingAvatar);
    if (!StreamingAvatar) throw new Error('StreamingAvatar not found');
    const cfg = await getJSON('/config');
    const tk = await getJSON('/heyg/token').catch(()=>({token:''}));
    const token = tk.token || '';
    if (!token) { log('Missing token from /heyg/token'); return; }

    client = new StreamingAvatar({ token, basePath: cfg.HEYGEN_SERVER_URL || 'https://api.heygen.com' });

    let avatarNameOrId = cfg.HEYGEN_AVATAR || '';
    if (!avatarNameOrId) avatarNameOrId = await pickInteractiveAvatarId();
    const voiceId = cfg.HEYGEN_VOICE || '';
    const req = { quality: (cfg.HEYGEN_QUALITY || 'medium'), voice: voiceId ? { voiceId } : undefined };
    if (avatarNameOrId) {
      if (/^avtr_|^[0-9a-f]{8}-/.test(avatarNameOrId)) req.avatarId = avatarNameOrId; else req.avatarName = avatarNameOrId;
    }
    lastReq = req;

    log('SDK: creating session…');
    session = await client.createStartAvatar({ ...req, disableIdleTimeout: true, useSilencePrompt: false });
    startHeartbeat();
    // Ensure no default/demo prompt is speaking
    try { await client.interrupt(); } catch {}

    client.on && client.on('stream_ready', (ev)=>{
      const stream = ev.detail; if(!stream) return;
      media.srcObject = stream; media.muted=false;
      ensurePlay(media);
      media.play().catch(()=>{
        autoplayHint?.classList.remove('hide');
        const once=()=>{ media.play().catch(()=>{}); autoplayHint?.classList.add('hide'); document.removeEventListener('click', once, true);} ;
        document.addEventListener('click', once, true);
      });
      const onPlaying = () => {
        readyToSpeak = true;
        setTimeout(()=>{ try{ drainQueue(); }catch(e){} }, 100);
        media.removeEventListener('playing', onPlaying);
      };
      media.addEventListener('playing', onPlaying, { once: true });
    });
    // Nudge playback when speech begins
    client.on && client.on('avatar_start_talking', ()=> { ensurePlay(media); drainQueue(); });
    client.on && client.on('avatar_talking_message', ()=> { ensurePlay(media); drainQueue(); });
    client.on && client.on('connection_quality_changed', ()=>{
      if (!media.srcObject && client.mediaStream) { media.srcObject = client.mediaStream; ensurePlay(media); }
    });
    client.on && client.on('stream_disconnected', async ()=>{
      log('SDK: stream disconnected; re-establishing…');
      try { stopHeartbeat(); try{ await client.stopAvatar(); }catch{}; session = await client.createStartAvatar({ ...(lastReq||{}), useSilencePrompt: false }); startHeartbeat(); log('SDK: session re-established.'); drainQueue(); }
      catch(e){ log('SDK: re-establish failed: '+(e.message||e)); }
    });

    // SSE bridge from Python: speak events
    const es = new EventSource('/events');
    es.addEventListener('ready', ()=>log('Bridge ready.'));
    es.addEventListener('session', ()=>log('OrderBuddy: session start'));
    es.addEventListener('speak', async (e)=>{
      try{
        const { text } = JSON.parse(e.data||'{}');
        if (!text) return;
        speakQueue.push(text);
        drainQueue();
      }catch{}
    });

    log('SDK: connected. Use the Python app to drive speech.');
  }

  document.addEventListener('DOMContentLoaded', async ()=>{
    try{ const mod = await ensureSDK(); await attachHandlers(mod); }
    catch(e){ log('SDK init failed: ' + (e.message||e)); }
  });
})();



