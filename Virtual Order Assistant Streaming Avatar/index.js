'use strict';

const statusElement = document.querySelector('#status');
const mediaElement  = document.querySelector('#mediaElement');
const autoplayHint  = document.querySelector('#autoplayHint');
function log(m){ statusElement.innerHTML += m + '<br>'; statusElement.scrollTop = statusElement.scrollHeight; }

const params = new URLSearchParams(location.search);
const overrideAvatar = params.get('avatar') || '';
const overrideAvatarId = params.get('avatar_id') || '';
const overrideVoice  = params.get('voice') || '';

let session = null;
let pc = null;

async function getJSON(url){ const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+' → '+r.status); return r.json(); }
async function postJSON(url, body){ const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); if(!r.ok){ const t=await r.text(); throw new Error(url+' → '+r.status+' '+t);} return await r.json(); }

function isUUIDLike(str){ return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(str) || /^avtr_/.test(str); }

async function resolveInteractiveAvatar(cfg){
  // First, try overrides or env
  const voice = overrideVoice || cfg.HEYGEN_VOICE || 'en_us_002';
  let payload = { quality: cfg.HEYGEN_QUALITY || 'low', voice: { voice_id: voice } };

  if (overrideAvatarId) { payload.avatar_id = overrideAvatarId; return payload; }
  if (overrideAvatar)   { payload.avatar_name = overrideAvatar; return payload; }

  if (cfg.HEYGEN_AVATAR) {
    // If it's an id-like string, prefer avatar_id, else avatar_name
    if (isUUIDLike(cfg.HEYGEN_AVATAR)) payload.avatar_id = cfg.HEYGEN_AVATAR;
    else payload.avatar_name = cfg.HEYGEN_AVATAR;
    return payload;
  }

  // Nothing set — fetch interactive avatars and pick the first available
  log('Fetching interactive avatars…');
  const list = await getJSON('/heyg/avatars');
  const candidates = (list.data || []).filter(a => a?.is_interactive);
  if (!candidates.length) throw new Error('No interactive avatars available on your HeyGen account.');
  const picked = candidates[0];
  log(`Using interactive avatar: ${picked.name} (${picked.avatar_id})`);
  payload.avatar_id = picked.avatar_id;
  return payload;
}

async function connectAvatar(){
  log('Connecting to HeyGen (via proxy)…');
  const cfg = await getJSON('/config');
  // Resolve a valid avatar payload: avatar_id (preferred) or avatar_name
  const payload = await resolveInteractiveAvatar(cfg);

  // 1) new
  const newRes = await postJSON('/heyg/streaming.new', payload);
  session = newRes.data;

  // 2) peer connection
  pc = new RTCPeerConnection({ iceServers: session.ice_servers2 });
  pc.ontrack = (e)=>{
    if (e.streams && e.streams[0]) {
      mediaElement.srcObject = e.streams[0];
      mediaElement.muted = false;
      mediaElement.play().catch(()=>{
        autoplayHint.classList.remove('hide');
        const once = () => { mediaElement.play().catch(()=>{}); autoplayHint.classList.add('hide'); document.removeEventListener('click', once, true); };
        document.addEventListener('click', once, true);
      });
    }
  };
  pc.onicecandidate = ({candidate}) => { if(candidate) postJSON('/heyg/streaming.ice', { session_id: session.session_id, candidate: candidate.toJSON() }).catch(console.error); };
  pc.oniceconnectionstatechange = () => log('ICE: '+pc.iceConnectionState);
  await pc.setRemoteDescription(new RTCSessionDescription(session.sdp));
  const local = await pc.createAnswer();
  await pc.setLocalDescription(local);

  // 3) start
  await postJSON('/heyg/streaming.start', { session_id: session.session_id, sdp: local });
  pc.getReceivers().forEach(r => r.jitterBufferTarget = 500);
  log('Avatar connected.');
}

// Python bridge (SSE)
(function bridge(){
  const es = new EventSource('/events');
  es.addEventListener('ready', ()=>log('Bridge ready.'));
  es.addEventListener('session', ()=>log('OrderBuddy: session start'));
  es.addEventListener('speak', async (e)=>{
    const { text } = JSON.parse(e.data||'{}');
    if (!text) return;
    let tries = 20;
    const send = async () => {
      if (!session) return false;
      try { await postJSON('/heyg/streaming.task', { session_id: session.session_id, text }); return true; } catch { return false; }
    };
    if (!(await send())) {
      const t=setInterval(async()=>{ if(await send()){ clearInterval(t); log('Avatar spoke bridged text.'); } if(--tries<=0) clearInterval(t); }, 300);
    } else { log('Avatar spoke bridged text.'); }
  });
})();

// Menu
async function loadMenu(){
  try {
    const m = await getJSON('menu.json');
    const el = document.getElementById('menu'); el.innerHTML='';
    const fmt = v => '$'+Number(v).toFixed(2);
    Object.entries(m).forEach(([cat, items])=>{
      const wrap=document.createElement('div'); wrap.className='menu-cat';
      const h=document.createElement('h3'); h.textContent=cat[0].toUpperCase()+cat.slice(1); wrap.appendChild(h);
      Object.entries(items).forEach(([name,val])=>{
        const row=document.createElement('div'); row.className='item';
        const left=document.createElement('div'); left.className='name'; left.textContent=name;
        const right=document.createElement('div'); right.className='price';
        if (val && typeof val==='object') right.textContent=Object.entries(val).map(([k,p])=>k+' '+fmt(p)).join(' · ');
        else right.textContent=fmt(val);
        row.appendChild(left); row.appendChild(right); wrap.appendChild(row);
      });
      el.appendChild(wrap);
    });
  } catch(e){ log('Menu load failed: '+e.message); }
}

document.addEventListener('DOMContentLoaded', async ()=>{
  log('Page loaded.');
  await loadMenu();
  try { await connectAvatar(); } catch(e){ console.error(e); log('Failed to connect avatar — '+e.message); }
});
