// server.js v8 — Adds /heyg/avatars list + proxy; keeps SSE bridge.
// Run: npm i express cors dotenv node-fetch

const express = require('express');
const path = require('path');
const cors = require('cors');
const dotenv = require('dotenv');
const fetch = (...args) => import('node-fetch').then(({default: fetch}) => fetch(...args));
dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, '.')));

function heygenBase(){ return process.env.HEYGEN_SERVER_URL || 'https://api.heygen.com'; }
function heygenKey(){ return process.env.HEYGEN_API_KEY || ''; }

// /config for client
app.get('/config', (_req, res) => {
  res.json({
    HEYGEN_SERVER_URL: heygenBase(),
    HEYGEN_AVATAR: process.env.HEYGEN_AVATAR || '',
    HEYGEN_VOICE: process.env.HEYGEN_VOICE || 'en_us_002',
    HEYGEN_QUALITY: process.env.HEYGEN_QUALITY || 'low',
  });
});

async function proxy(path, body) {
  const base = heygenBase();
  const key = heygenKey();
  if (!key) throw new Error('HEYGEN_API_KEY missing');
  const r = await fetch(base + path, {
    method: 'POST',
    headers: { 'Content-Type':'application/json', 'X-Api-Key': key },
    body: JSON.stringify(body || {}),
  });
  const txt = await r.text();
  if (!r.ok) throw new Error(`HeyGen ${path} ${r.status}: ${txt}`);
  return txt;
}

app.get('/heyg/avatars', async (_req, res) => {
  try {
    const r = await fetch(heygenBase() + '/v2/avatars', { headers: { 'X-Api-Key': heygenKey() }});
    const txt = await r.text();
    res.type('application/json').send(txt);
  } catch(e) {
    res.status(500).send(String(e.message || e));
  }
});

app.post('/heyg/streaming.new', async (req, res) => {
  try { const out = await proxy('/v1/streaming.new', req.body); res.type('application/json').send(out); }
  catch(e){ res.status(500).send(String(e.message || e)); }
});
app.post('/heyg/streaming.start', async (req, res) => {
  try { const out = await proxy('/v1/streaming.start', req.body); res.type('application/json').send(out); }
  catch(e){ res.status(500).send(String(e.message || e)); }
});
app.post('/heyg/streaming.ice', async (req, res) => {
  try { const out = await proxy('/v1/streaming.ice', req.body); res.type('application/json').send(out); }
  catch(e){ res.status(500).send(String(e.message || e)); }
});
app.post('/heyg/streaming.task', async (req, res) => {
  try { const out = await proxy('/v1/streaming.task', req.body); res.type('application/json').send(out); }
  catch(e){ res.status(500).send(String(e.message || e)); }
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
app.post('/speak', (req,res)=>{ const text=String(req.body?.text||'').trim(); if(!text) return res.status(400).json({error:'missing text'}); broadcast('speak',{text}); res.json({ok:true}); });

const PORT = process.env.PORT || 3000;
app.listen(PORT, ()=>{
  console.log(`Bridge active on http://localhost:${PORT}`);
  console.log('Using server-side HeyGen proxy + avatar auto-select.');
});
