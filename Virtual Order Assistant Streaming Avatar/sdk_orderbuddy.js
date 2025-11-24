// sdk_orderbuddy.js — LiveAvatar SDK bridge + OrderBuddy SSE glue
(function(){
  const params = new URLSearchParams(location.search);
  if (params.get('legacy') === '1') return; // legacy explicitly requested

  const statusEl = document.getElementById('status');
  const media = document.getElementById('mediaElement');
  const autoplayHint = document.getElementById('autoplayHint');
  function log(m){ if(statusEl){ statusEl.innerHTML += String(m) + '<br>'; statusEl.scrollTop = statusEl.scrollHeight; } }

  let session = null;
  let sessionState = 'INACTIVE';
  let hbTimer = null;
  let playTimer = null;
  let speakQueue = [];
  let draining = false;
  let readyToSpeak = false;
  let unloadHookAttached = false;
  let establishingPromise = null;
  let eventStream = null;

  async function ensureSDK(){
    try { return await import('https://esm.sh/@heygen/liveavatar-web-sdk@0.0.9?bundle'); }
    catch(e1){ try{ return await import('/node_modules/@heygen/liveavatar-web-sdk/dist/index.esm.js'); }
      catch(e2){ try{ return await import('https://cdn.jsdelivr.net/npm/@heygen/liveavatar-web-sdk@0.0.9/dist/index.esm.js'); }
        catch(e3){ log('LiveAvatar SDK load failed'); throw e3; } } }
  }

  async function getJSON(url, opts){
    const r = await fetch(url, { cache:'no-store', ...(opts||{}) });
    if (!r.ok) throw new Error(url + ': ' + r.status);
    return r.json();
  }

  function startHeartbeat(){
    stopHeartbeat();
    hbTimer = setInterval(async ()=>{
      try{
        if (session && sessionState === 'CONNECTED') await session.keepAlive();
      }catch(e){
        log('LiveAvatar keep-alive failed: ' + (e?.message||e));
      }
    }, 20000);
  }
  function stopHeartbeat(){ if (hbTimer){ clearInterval(hbTimer); hbTimer = null; } }

  function ensurePlay(el){
    if (!el) return;
    const tryPlay = () => el.play().catch(()=>{});
    tryPlay();
    if (playTimer) clearInterval(playTimer);
    playTimer = setInterval(()=>{
      if (!el.paused && !el.ended && el.readyState >= 2){
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

  function canSpeak(){ return !!(session && readyToSpeak && sessionState === 'CONNECTED'); }

  function sayEntry(entry){
    if (!entry || !canSpeak()) return false;
    try {
      if (entry.audio_b64) {
        session.repeatAudio(entry.audio_b64);
      } else if (entry.text) {
        session.repeat(entry.text);
      } else {
        return true;
      }
      log('LiveAvatar: queued speech.');
      return true;
    } catch (err) {
      log('LiveAvatar speak failed: ' + (err?.message||err));
      return false;
    }
  }

  async function drainQueue(){
    if (draining) return; draining = true;
    try {
      while (speakQueue.length && canSpeak()){
        const entry = speakQueue.shift();
        const ok = sayEntry(entry);
        if (!ok){
          speakQueue.unshift(entry);
          break;
        }
        await new Promise(r=>setTimeout(r, 100));
      }
    } finally { draining = false; }
  }

  async function shutdownSession(reason){
    try {
      stopHeartbeat();
      if (session) await session.stop();
    } catch {}
    session = null;
    readyToSpeak = false;
    sessionState = 'INACTIVE';
    if (reason) log('LiveAvatar session closed (' + reason + ').');
  }

  function ensureEventStream(){
    if (eventStream) return;
    const es = new EventSource('/events');
    eventStream = es;
    es.addEventListener('ready', ()=>log('Bridge ready.'));
    es.addEventListener('session', ()=>log('OrderBuddy: session start'));
    es.addEventListener('speak', (e)=>{
      try{
        const payload = JSON.parse(e.data||'{}');
        speakQueue.push({ text: payload?.text || '', audio_b64: payload?.audio_b64 || '' });
        drainQueue();
      }catch(err){
        console.warn('LiveAvatar SSE parse failed', err);
      }
    });
  }

  async function fetchSessionToken(){
    const resp = await fetch('/liveavatar/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).catch((err)=>{ throw new Error('LiveAvatar session request failed: ' + (err?.message||err)); });
    if (!resp.ok){
      let errTxt = '';
      try { errTxt = await resp.text(); } catch {}
      throw new Error('LiveAvatar token failed: ' + (errTxt || resp.status));
    }
    return resp.json();
  }

  async function createSession(mod){
    if (establishingPromise) return establishingPromise;
    establishingPromise = (async ()=>{
      const { LiveAvatarSession, SessionEvent } = mod;
      readyToSpeak = false;
      const cfg = await getJSON('/config');
      const tokenPayload = await fetchSessionToken();
      const token = tokenPayload.session_token || tokenPayload.token || '';
      if (!token) throw new Error('Missing LiveAvatar session token');

      const options = {
        apiUrl: cfg.LIVEAVATAR_API_URL || cfg.HEYGEN_SERVER_URL || 'https://api.liveavatar.com',
        voiceChat: false,
      };
      session = new LiveAvatarSession(token, options);
      sessionState = session.state || 'INACTIVE';

      session.on(SessionEvent.SESSION_STATE_CHANGED, (state)=>{
        sessionState = state;
        log('LiveAvatar state: ' + state);
        if (state !== 'CONNECTED') readyToSpeak = false;
      });
      session.on(SessionEvent.SESSION_CONNECTION_QUALITY_CHANGED, (quality)=>{
        log('LiveAvatar quality: ' + quality);
      });
      session.on(SessionEvent.SESSION_STREAM_READY, ()=>{
        log('LiveAvatar: stream ready.');
        if (!media) return;
        try { session.attach(media); } catch (err) { log('Attach failed: ' + (err?.message||err)); }
        media.muted = false;
        ensurePlay(media);
        media.play().catch(()=>{
          autoplayHint?.classList.remove('hide');
          const once=()=>{ media.play().catch(()=>{}); autoplayHint?.classList.add('hide'); document.removeEventListener('click', once, true);} ;
          document.addEventListener('click', once, true);
        });
        waitForPlaying(media).then(()=>{
          readyToSpeak = true;
          setTimeout(()=>{ try{ drainQueue(); }catch{} }, 50);
        });
      });
      session.on(SessionEvent.SESSION_DISCONNECTED, (reason)=>{
        readyToSpeak = false;
        stopHeartbeat();
        log('LiveAvatar disconnected: ' + reason);
        session = null;
        setTimeout(()=>{ createSession(mod).catch(e=>log('LiveAvatar re-init failed: ' + (e?.message||e))); }, 4000);
      });

      await session.start();
      log('LiveAvatar session started.');
      startHeartbeat();
    })().catch((err)=>{
      log('LiveAvatar init failed: ' + (err?.message||err));
      throw err;
    }).finally(()=>{ establishingPromise = null; });
    return establishingPromise;
  }

  document.addEventListener('DOMContentLoaded', async ()=>{
    try{
      const mod = await ensureSDK();
      await createSession(mod);
      ensureEventStream();
      if (!unloadHookAttached){
        unloadHookAttached = true;
        window.addEventListener('beforeunload', ()=>{ shutdownSession('window closing'); });
      }
      log('LiveAvatar ready. Use the Python app to drive speech.');
    }catch(e){
      const base = e?.message || e;
      log('SDK init failed: ' + base);
      if (typeof console !== 'undefined' && console.error) console.error(e);
    }
  });
})();
