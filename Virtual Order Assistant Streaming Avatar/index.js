'use strict';

const statusElement = document.querySelector('#status');
function log(m){ if(statusElement){ statusElement.innerHTML += m + '<br>'; statusElement.scrollTop = statusElement.scrollHeight; } }

async function getJSON(url){ const r=await fetch(url,{cache:'no-store'}); if(!r.ok) throw new Error(url+': '+r.status); return r.json(); }

// Simple menu renderer (legacy avatar glue removed; SDK handles avatar)
async function loadMenu(){
  try {
    const m = await getJSON('menu.json');
    const el = document.getElementById('menu'); if (!el) return; el.innerHTML='';
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
  log('SDK default active.');
  await loadMenu();
});

