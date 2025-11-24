// server.js v8 â€” Adds /heyg/avatars list + proxy; keeps SSE bridge.
// Run: npm i express cors dotenv node-fetch

const express = require('express');
const path = require('path');
const cors = require('cors');
const dotenv = require('dotenv');
const fetchMod = (...args) => import('node-fetch').then(({default: fetch}) => fetch(...args));

// --- Simple structured logging with in-memory ring buffer ---
const LOGS = [];
const LOG_LIMIT = 500;
function pushLog(event, data){
  const entry = { ts: new Date().toISOString(), event, ...data };
  LOGS.push(entry);
  if (LOGS.length > LOG_LIMIT) LOGS.splice(0, LOGS.length - LOG_LIMIT);
  // Also print to console for live debugging
  try { console.log(JSON.stringify(entry)); } catch { console.log(`[log] ${event}`); }
}
function newReqId(){ return Math.random().toString(36).slice(2,8) + '-' + (Date.now()%100000); }
async function fetchWithTimeout(url, opts = {}, timeoutMs = 12000) {
  const ac = new AbortController();
  const id = setTimeout(() => ac.abort(), timeoutMs);
  try {
    return await fetchMod(url, { ...opts, signal: ac.signal });
  } finally {
    clearTimeout(id);
  }
}
dotenv.config();

const app = express();

// --- Lightweight CORS for local dev (no extra deps) ---
app.use((req,res,next)=>{
  res.setHeader("Access-Control-Allow-Origin","*");
  res.setHeader("Access-Control-Allow-Methods","GET,POST,PUT,PATCH,DELETE,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","Content-Type, Authorization");
  if(req.method==="OPTIONS"){ return res.sendStatus(204); }
  next();
});
app.use(cors());
app.use(express.json({ limit: '5mb' }));
app.use(express.static(path.join(__dirname, '.')));
// Serve node_modules explicitly so scoped package paths resolve in the browser
app.use('/node_modules', express.static(path.join(__dirname, 'node_modules')));

// Attach a request id and basic request logging
app.use((req,res,next)=>{
  req._id = req._id || newReqId();
  req._t0 = Date.now();
  const b = (req.body && typeof req.body === 'object') ? JSON.stringify(req.body).slice(0, 512) : '';
  pushLog('http_in', { id:req._id, method:req.method, path:req.path, body_preview:b });
  res.on('finish', ()=>{
    pushLog('http_out', { id:req._id, method:req.method, path:req.path, status: res.statusCode, ms: Date.now()-req._t0 });
  });
  next();
});

function heygenBase(){ return process.env.HEYGEN_SERVER_URL || 'https://api.heygen.com'; }
function heygenKey(){ return process.env.HEYGEN_API_KEY || ''; }
function liveAvatarConfig(){
  const mode = String(process.env.LIVEAVATAR_MODE || 'FULL').toUpperCase();
  return {
    apiKey: process.env.LIVEAVATAR_API_KEY || process.env.HEYGEN_API_KEY || '',
    apiUrl: process.env.LIVEAVATAR_API_URL || process.env.HEYGEN_SERVER_URL || 'https://api.liveavatar.com',
    avatarId: process.env.LIVEAVATAR_AVATAR_ID || process.env.HEYGEN_AVATAR || '',
    voiceId: process.env.LIVEAVATAR_VOICE_ID || process.env.HEYGEN_VOICE || '',
    contextId: process.env.LIVEAVATAR_CONTEXT_ID || '',
    language: process.env.LIVEAVATAR_LANGUAGE || 'en',
    mode,
  };
}

// /config for client
app.get('/config', (_req, res) => {
  const live = liveAvatarConfig();
  res.json({
    HEYGEN_SERVER_URL: heygenBase(),
    HEYGEN_AVATAR: process.env.HEYGEN_AVATAR || '',
    HEYGEN_VOICE: process.env.HEYGEN_VOICE || 'en_us_002',
    HEYGEN_QUALITY: process.env.HEYGEN_QUALITY || 'low',
    LIVEAVATAR_API_URL: live.apiUrl,
    LIVEAVATAR_MODE: live.mode,
    LIVEAVATAR_AVATAR_ID: live.avatarId,
  });
});

async function proxy(path, body, reqId) {
  const base = heygenBase();
  const key = heygenKey();
  if (!key) throw new Error('HEYGEN_API_KEY missing');
  const url = base + path;
  const t0 = Date.now();
  pushLog('heyg_proxy_request', { id:reqId, url, body_preview: JSON.stringify(body||{}).slice(0, 512) });
  const r = await fetchWithTimeout(url, {
    method: 'POST',
    headers: { 'Content-Type':'application/json', 'X-Api-Key': key },
    body: JSON.stringify(body || {}),
  });
  const txt = await r.text();
  pushLog('heyg_proxy_response', { id:reqId, url, status:r.status, ms: Date.now()-t0, body_preview: txt.slice(0, 512) });
  if (!r.ok) throw new Error(`HeyGen ${path} ${r.status}: ${txt}`);
  return txt;
}

async function proxyWithRetry(path, body, reqId){
  const maxAttempts = 3;
  let attempt = 0;
  while (true) {
    try {
      attempt++;
      return await proxy(path, body, `${reqId||''}#${attempt}`);
    } catch (e) {
      const msg = String(e && e.message || e);
      const statusMatch = msg.match(/\s(\d{3}):/);
      const status = statusMatch ? Number(statusMatch[1]) : null;
      const retriable = !status || status >= 500; // network/timeout or 5xx
      pushLog('heyg_proxy_error', { id:reqId, attempt, path, retriable, error: msg.slice(0, 500) });
      if (!retriable || attempt >= maxAttempts) throw e;
      const base = 300 * Math.pow(2, attempt-1);
      const jitter = Math.floor(Math.random()*200);
      await new Promise(r => setTimeout(r, base + jitter));
    }
  }
}

app.get('/heyg/avatars', async (_req, res) => {
  try {
    const r = await fetchWithTimeout(heygenBase() + '/v2/avatars', { headers: { 'X-Api-Key': heygenKey() } }, 10000);
    const txt = await r.text();
    res.type('application/json').send(txt);
  } catch(e) {
    res.status(500).send(String(e.message || e));
  }
});

function replyError(res, e){
  const msg = String(e && e.message || e || 'error');
  const m = msg.match(/\s(\d{3}):/); // extract status from "... 400: ..."
  const status = m ? Number(m[1]) : 500;
  res.status(status).send(msg);
}
app.post('/heyg/streaming.new', async (req, res) => {
  try { const out = await proxyWithRetry('/v1/streaming.new', req.body, req._id); res.type('application/json').send(out); }
  catch(e){ replyError(res, e); }
});
app.post('/heyg/streaming.start', async (req, res) => {
  try { const out = await proxyWithRetry('/v1/streaming.start', req.body, req._id); res.type('application/json').send(out); }
  catch(e){ replyError(res, e); }
});
app.post('/heyg/streaming.ice', async (req, res) => {
  try { const out = await proxyWithRetry('/v1/streaming.ice', req.body, req._id); res.type('application/json').send(out); }
  catch(e){ replyError(res, e); }
});
app.post('/heyg/streaming.task', async (req, res) => {
  try { const out = await proxyWithRetry('/v1/streaming.task', req.body, req._id); res.type('application/json').send(out); }
  catch(e){ replyError(res, e); }
});

// Allow clients to explicitly close sessions (if supported). If the API 404s, clients should ignore.
app.post('/heyg/streaming.close', async (req, res) => {
  try { const out = await proxyWithRetry('/v1/streaming.close', req.body, req._id); res.type('application/json').send(out); }
  catch(e){ replyError(res, e); }
});


// LiveAvatar session token helper (new SDK path)
app.post('/liveavatar/session', async (req, res) => {
  try {
    const env = String(process.env.NODE_ENV || 'development').toLowerCase();
    if (env === 'production') return res.status(403).json({ error: 'Disabled in production' });
    const cfg = liveAvatarConfig();
    const apiKey = cfg.apiKey;
    if (!apiKey) return res.status(400).json({ error: 'LIVEAVATAR_API_KEY not configured' });
    const apiUrl = (cfg.apiUrl || 'https://api.liveavatar.com').replace(/\/$/, '');
    const mode = String(req.body?.mode || cfg.mode || 'FULL').toUpperCase();
    const avatarId = String(req.body?.avatar_id || cfg.avatarId || '').trim();
    if (!avatarId) return res.status(400).json({ error: 'LIVEAVATAR_AVATAR_ID not configured' });

    const personaOverrides = req.body?.avatar_persona || {};
    const persona = {
      voice_id: personaOverrides.voice_id || cfg.voiceId,
      context_id: personaOverrides.context_id || cfg.contextId,
      language: personaOverrides.language || cfg.language,
    };
    const body = { mode, avatar_id: avatarId };
    if (mode === 'FULL') {
      const cleaned = {};
      Object.entries(persona).forEach(([k, v]) => { if (v) cleaned[k] = v; });
      if (Object.keys(cleaned).length) body.avatar_persona = cleaned;
    }

    pushLog('liveavatar_session_request', { id:req._id, mode, avatar_id: avatarId });
    const resp = await fetchWithTimeout(`${apiUrl}/v1/sessions/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-KEY': apiKey },
      body: JSON.stringify(body),
    }, 15000);

    const raw = await resp.text();
    let payload = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch {}
    if (!resp.ok) {
      const message = payload?.data?.[0]?.message || payload?.error || raw || 'Failed to create LiveAvatar session';
      pushLog('liveavatar_session_error', { id:req._id, status: resp.status, error: message });
      return res.status(resp.status).json({ error: message });
    }

    const data = payload?.data || {};
    const token = data.session_token || '';
    if (!token) return res.status(502).json({ error: 'Missing session_token from LiveAvatar response' });
    res.json({ session_token: token, session_id: data.session_id || '', mode });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message || e) });
  }
});
// SSE for Python bridge
const clients = new Set();
app.get('/events', (req, res) => {
  res.setHeader('Content-Type','text/event-stream');
  res.setHeader('Cache-Control','no-cache');
  res.setHeader('Connection','keep-alive');
  res.flushHeaders();
  res.write(`event: ready\ndata: ok\n\n`);
  clients.add(res);
  req.on('close', ()=>clients.delete(res));
});
function broadcast(event, payload){ const data = JSON.stringify(payload||{}); for (const r of clients){ try{ r.write(`event: ${event}\ndata: ${data}\n\n`);}catch{}} }
app.post('/session/start', (_req,res)=>{ broadcast('session',{action:'start'}); res.json({ok:true}); });
app.post('/speak', (req,res)=>{
  const text = String(req.body?.text || '').trim();
  const audio_b64 = req.body?.audio_b64;
  if (!text && !audio_b64) return res.status(400).json({ error: 'missing text or audio' });
  broadcast('speak', { text, audio_b64 });
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3000;
// Simple health endpoint for sanity checks
app.get('/health', (_req, res) => res.status(200).send('ok'));

// Expose recent logs for easy troubleshooting
app.get('/_logs', (_req, res) => { res.json({ logs: LOGS }); });

// DEV-ONLY: return a short-lived HeyGen streaming token (never expose raw API keys in prod)
app.get('/heyg/token', async (_req, res) => {
  try {
    const env = String(process.env.NODE_ENV || 'development').toLowerCase();
    if (env === 'production') return res.status(403).json({ error: 'Disabled in production' });
    const key = heygenKey();
    if (!key) return res.status(400).json({ error: 'HEYGEN_API_KEY not configured' });

    const resp = await fetchWithTimeout(heygenBase() + '/v1/streaming.create_token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Api-Key': key },
      body: JSON.stringify({ ttl: 600 }),
    }, 10000);

    const raw = await resp.text();
    let payload = {};
    try { payload = raw ? JSON.parse(raw) : {}; } catch {}
    const token = payload?.data?.token || payload?.token || '';
    if (!resp.ok) {
      const msg = payload?.error || raw || 'Token request failed';
      return res.status(resp.status).json({ error: msg });
    }
    if (!token) {
      return res.status(502).json({ error: 'Token missing from HeyGen response' });
    }
    res.json({ token });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message || e) });
  }
});


app.listen(PORT, ()=>{
  console.log(`Bridge active on http://localhost:${PORT}`);
  console.log('LiveAvatar bridge ready (legacy HeyGen proxy still exposed for compatibility).');
});

