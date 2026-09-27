/* Sim2Sale — Cloudflare Worker: minimal dashboard + Vultr orchestration API.
   No backend needed. Key via env secret VULTR_INFERENCE_API_KEY or x-vultr-key header / apiKey body (testing). */

const VULTR_BASE = "https://api.vultrinference.com/v1";
const DEFAULT_MODEL = "kimi-k2-instruct";
const MAX_TRIALS = 48; // worker request budget; bigger runs -> local backend jobs endpoint
const CONC = 20;

const MODELS = ["kimi-k2-instruct", "Qwen/Qwen3.6-27B", "XiaomiMiMo/MiMo-V2.5-Pro", "moonshotai/Kimi-K2.6"];

const VAR_SYS = "You are a senior product strategist + engineer. Invent variations that are BOTH appealing AND buildable. Valid JSON only, no markdown.";
const PER_SYS = "You are a market-research expert that creates diverse, realistic buyer personas. Valid JSON only, no markdown.";
const AGENT_RULES = "Stay in character. Be opinionated like a real shopper. Never mention you are an AI. Valid JSON only.";

const json = (d, s = 200) =>
  new Response(JSON.stringify(d), { status: s, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });
const err = (m, s = 400) => json({ error: m }, s);

function keyOf(req, body, env) {
  return req.headers.get("x-vultr-key") || (body && body.apiKey) || env.VULTR_INFERENCE_API_KEY || "";
}

function salvage(t) {
  let s = (t || "").trim().replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "");
  try { return JSON.parse(s); }
  catch {
    const a = s.indexOf("{"), b = s.lastIndexOf("}");
    if (a >= 0 && b > a) return JSON.parse(s.slice(a, b + 1));
    throw new Error("non-JSON model output");
  }
}

async function chat(apiKey, model, system, user, temp, maxTokens) {
  const r = await fetch(`${VULTR_BASE}/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
    body: JSON.stringify({
      model, temperature: temp, max_tokens: maxTokens,
      response_format: { type: "json_object" },
      messages: [{ role: "system", content: system }, { role: "user", content: user }],
    }),
  });
  if (!r.ok) throw new Error(`vultr ${r.status}`);
  const d = await r.json();
  return salvage(d.choices[0].message.content || "{}");
}

const slug = (s, fb) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || fb;
const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;
const std = (a) => { const m = mean(a); return Math.sqrt(mean(a.map((v) => (v - m) ** 2))); };
const top = (arr) => {
  const c = {};
  for (const v of arr) if (v) c[v] = (c[v] || 0) + 1;
  let bk = null, bv = 0;
  for (const k in c) if (c[k] > bv) { bv = c[k]; bk = k; }
  return bk;
};

async function mapConc(items, n, fn) {
  const out = [];
  for (let i = 0; i < items.length; i += n) out.push(...await Promise.all(items.slice(i, i + n).map(fn)));
  return out;
}

function aggregate(trials, variations) {
  const summaries = variations.map((v) => {
    const vt = trials.filter((t) => t.variation_id === v.id);
    const s = vt.map((t) => t.intent_score);
    const vals = vt.map((t) => t.value_score).filter((x) => x != null);
    return {
      variation_id: v.id, variation_name: v.name, n: vt.length,
      mean_intent: +mean(s).toFixed(2), std_intent: +std(s).toFixed(2),
      pct_high_intent: +((s.filter((x) => x >= 8).length / s.length) * 100).toFixed(1),
      buy_rate: +((vt.filter((t) => t.decision === "buy").length / vt.length) * 100).toFixed(1),
      mean_value: vals.length ? +(mean(vals).toFixed(2)) : null,
      top_objection: top(vt.map((t) => t.objection)),
      top_liked_feature: top(vt.map((t) => t.liked_feature)),
      top_tweak: top(vt.map((t) => t.suggested_tweak)),
    };
  });
  const by_persona = {};
  for (const t of trials) {
    by_persona[t.persona_id] = by_persona[t.persona_id] || {};
    (by_persona[t.persona_id][t.variation_id] = by_persona[t.persona_id][t.variation_id] || []).push(t.intent_score);
  }
  for (const p in by_persona) for (const v in by_persona[p]) by_persona[p][v] = +mean(by_persona[p][v]).toFixed(2);
  const ranking = [...summaries].sort((a, b) => b.mean_intent - a.mean_intent || b.buy_rate - a.buy_rate).map((s) => s.variation_id);
  return { summaries, by_persona, ranking };
}

const HTML = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
+ '<meta name="viewport" content="width=device-width,initial-scale=1">'
+ '<title>Sim2Sale</title>'
+ '<style>'
+ ':root{--bg:#f4f4f1;--card:#ffffff;--line:#e3e3de;--tx:#171717;--dim:#8b8b86;--ac:#171717;--bad:#c92a2a}'
+ '*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 -apple-system,Inter,SF Pro,Segoe UI,Roboto,sans-serif}'
+ '.w{max-width:860px;margin:0 auto;padding:18px 16px 60px}'
+ 'header{display:flex;align-items:center;gap:10px;padding:14px 2px 18px}.logo{font-weight:800;font-size:14px;background:#171717;color:#ffffff;border-radius:8px;padding:3px 7px;letter-spacing:.5px}.name{font-weight:800;letter-spacing:.5px;font-size:19px}'
+ 'select,input{background:#ffffff;border:1px solid var(--line);color:var(--tx);border-radius:10px;padding:9px 11px;font-size:14px;outline:none;width:100%}'
+ 'input:focus{border-color:var(--ac)}'
+ '.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:640px){.grid{grid-template-columns:1fr}}'
+ '.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 14px}'
+ '.card h3{margin:0 0 9px;font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.4px}'
+ '.row{display:flex;gap:8px;align-items:center}.row input{flex:1;min-width:0}'
+ 'details.card summary{cursor:pointer;color:var(--dim);font-size:13px;font-weight:600}'
+ '.step{display:flex;align-items:center;gap:10px}.step b{min-width:26px;text-align:center;font-size:17px}'
+ '.step .lbl{font-size:12px;color:var(--dim);font-weight:700;min-width:34px}'
+ '.step button{width:32px;height:32px;border-radius:9px;border:1px solid var(--line);background:#ffffff;color:var(--tx);font-size:17px;cursor:pointer}'
+ '.step button:active{transform:scale(.93)}'
+ '#run{width:100%;margin:12px 0;border:0;border-radius:14px;background:#171717;color:#ffffff;font-weight:800;font-size:18px;padding:14px;cursor:pointer}'
+ '#run:disabled{opacity:.45;cursor:wait}'
+ '.stages{display:flex;gap:8px;margin:4px 0 12px}.stg{flex:1;text-align:center;font-size:20px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px;filter:grayscale(1);opacity:.5}'
+ '.stg.act{filter:none;opacity:1;border-color:var(--ac);animation:pl 1s infinite alternate}.stg.ok{filter:none;opacity:1}'
+ '@keyframes pl{to{background:#e9e9e4}}'
+ '.bar{height:5px;background:var(--line);border-radius:4px;overflow:hidden;margin-bottom:14px}.bar i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}'
+ '#hero{display:none;background:#ffffff;border:2px solid #171717;border-radius:16px;padding:16px;text-align:center;margin-bottom:10px}'
+ '#hero .t{font-size:13px;color:var(--dim)}#hero .n{font-size:24px;font-weight:800}#hero .s{font-size:44px;font-weight:800;color:var(--ac)}'
+ '.vrow{display:grid;grid-template-columns:1fr auto;gap:4px 10px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:8px 0}'
+ '.vrow .nm{font-weight:700}.vrow .sc{font-weight:800;color:#171717;font-size:18px}.vrow .tr{grid-column:1/-1;height:8px;background:#efefeb;border-radius:5px;overflow:hidden}.vrow .tr i{display:block;height:100%;background:#171717}'
+ '.vrow .mt{grid-column:1/-1;color:var(--dim);font-size:12.5px}'
+ 'table.heat{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}table.heat th,table.heat td{padding:8px;border:1px solid var(--line);text-align:center}table.heat th{background:#efefeb;color:var(--dim);font-weight:600}'
+ '.chip{display:inline-block;background:#ffffff;border:1px solid var(--line);border-radius:20px;padding:5px 11px;margin:3px;font-size:12.5px;color:var(--dim)}'
+ '#toast{display:none;background:#ffffff;border:1px solid var(--bad);color:var(--bad);border-radius:12px;padding:10px 12px;margin-bottom:10px;font-size:13.5px}'
+ '.doclink{display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:10px;padding:7px 11px;font-size:13px;font-weight:700;color:var(--tx);text-decoration:none;background:var(--card)}'
+ '.samplebadge{display:inline-block;background:#171717;color:#fff;border-radius:8px;padding:3px 9px;font-size:12px;font-weight:700;margin-left:8px;vertical-align:middle}'
+ 'h2{font-size:13px;color:var(--dim);letter-spacing:.4px;margin:18px 0 4px}'
+ 'textarea{background:#ffffff;border:1px solid var(--line);color:var(--tx);border-radius:10px;padding:9px 11px;font-size:14px;outline:none;width:100%;resize:vertical;font-family:inherit}'
+ 'textarea:focus{border-color:var(--ac)}'
+ '.up{display:flex;align-items:center;gap:10px}#upbtn{border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px;font-weight:700;cursor:pointer;background:#fff;white-space:nowrap}'
+ '#fname{font-size:12.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
+ '.srow{display:flex;align-items:center;gap:10px;margin-top:9px}input[type=range]{flex:1;accent-color:#171717;padding:0;border:0}#scaleN{font-weight:800;min-width:70px;text-align:right;font-size:13px;white-space:nowrap}'
+ '.tgrid{display:grid;gap:6px;grid-template-columns:repeat(auto-fill,minmax(26px,1fr));margin:4px 0 2px}'
+ '.sq{aspect-ratio:1;border-radius:7px;border:1px solid var(--line);opacity:0;transform:scale(.5);transition:opacity .3s,transform .3s}'
+ '.sq.in{opacity:1;transform:none}.sq.buy{border:2px solid #171717}'
+ '.vlab{font-size:12.5px;font-weight:700;margin:10px 0 2px}.vlab span{color:var(--dim);font-weight:600}'
+ '.pimg{width:64px;height:64px;object-fit:cover;border-radius:12px;border:1px solid var(--line);margin-bottom:6px}'
+ '.vthumb{width:22px;height:22px;object-fit:cover;border-radius:6px;vertical-align:-5px;margin-right:6px;border:1px solid var(--line)}'
+ '</style></head><body><div class="w">'
+ '<header><span class="logo">S2S</span><span class="name">Sim2Sale</span><span class="sp"></span>'
+ '<a class="doclink" href="/docs" title="How it works"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 1h5l3 3v11H4V1z" stroke="currentColor" stroke-width="1.5"/><path d="M9 1v3h3" stroke="currentColor" stroke-width="1.5"/><path d="M6 8h4M6 10.5h4" stroke="currentColor" stroke-width="1.5"/></svg><span>Docs</span></a></header>'
+ '<div id="toast"></div>'
+ '<div class="card" style="margin-bottom:10px"><h3>Question</h3><input id="q" placeholder="Which idea wins?"></div>'
+ '<div class="card" style="margin-bottom:10px"><h3>Product</h3>'
+ '<label class="up"><input id="file" type="file" accept=".txt,.md,.json,.csv,.text" hidden><span id="upbtn">Upload file</span><span id="fname"></span></label>'
+ '<div style="height:8px"></div><textarea id="pd" rows="3" placeholder="Describe the product — or upload a file above"></textarea>'
+ '<div style="height:8px"></div><input id="pn" placeholder="Product name"></div>'
+ '<div class="card" style="margin-bottom:10px"><h3>Audience</h3><input id="aud" placeholder="Audience — e.g. EU gym-goers 18-35"></div>'
+ '<details class="card"><summary>Advanced</summary><div style="height:9px"></div>'
+ '<input id="cons" placeholder="Tech limits — e.g. no glass, max 60g">'
+ '<div class="srow"><span style="font-size:13px;color:var(--dim);font-weight:700">Scale</span><input id="scale" type="range" min="20" max="48" step="1" value="24"><b id="scaleN"></b></div>'
+ '<div style="height:8px"></div><input id="key" type="password" placeholder="Vultr API key" autocomplete="off">'
+ '<div style="height:8px"></div><select id="model" title="model"><option value="">Default model</option></select></details>'
+ '<div style="height:10px"></div>'
+ '<button id="run">Run</button>'
+ '<div class="stages"><div class="stg" id="s1">01</div><div class="stg" id="s2">02</div><div class="stg" id="s3">03</div></div>'
+ '<div class="bar"><i id="fill"></i></div>'
+ '<div id="out"></div>'
+ '</div><script>'
+ 'var $=function(id){return document.getElementById(id)};'
+ 'var LS="s2s_key";$("key").value=localStorage.getItem(LS)||"";'
+ 'var FALLBACK=["kimi-k2-instruct","moonshotai/Kimi-K2.6","Qwen/Qwen3.6-27B","XiaomiMiMo/MiMo-V2.5-Pro","deepseek-ai/DeepSeek-V4-Pro"];'
+ 'function fillModels(list){var m=$("model"),cur=m.value;m.options.length=0;'
+ 'var d=document.createElement("option");d.value="";d.textContent="Default";m.appendChild(d);'
+ 'list.forEach(function(x){var o=document.createElement("option");o.value=x;o.textContent=x;m.appendChild(o)});'
+ 'if(cur)m.value=cur}'
+ 'fillModels(FALLBACK);'
+ 'async function syncModels(){if(!$("key").value)return;try{'
+ 'var r=await fetch("/api/models",{headers:{"x-vultr-key":$("key").value}});if(!r.ok)return;'
+ 'var d=await r.json();if(d.data&&d.data.length)fillModels(d.data)}catch(e){}}'
+ 'syncModels();var keyT=null;'
+ '$("key").oninput=function(){localStorage.setItem(LS,$("key").value);clearTimeout(keyT);keyT=setTimeout(syncModels,800)};'
+ 'function scaleP(){var n=parseInt($("scale").value);return Math.max(5,Math.min(12,Math.round(n/4)))}'
+ 'function scalePaint(){$("scaleN").textContent=(4*scaleP())+" calls"}'
+ 'scalePaint();$("scale").oninput=scalePaint;'
+ 'var PNAME="";'
+ '$("file").onchange=function(){var f=$("file").files[0];if(!f)return;var rd=new FileReader();'
+ 'rd.onload=function(){var t=String(rd.result||"");var nm=f.name.replace(/\\.[^.]+$/,"");'
+ 'try{var j=JSON.parse(t);if(j&&(j.description||j.desc||j.text)){'
+ '$("pd").value=j.description||j.desc||j.text;if(j.name||j.title)$("pn").value=j.name||j.title;else $("pn").value=nm;'
+ 'PNAME=f.name;$("fname").textContent=f.name;return}}catch(e){}'
+ '$("pd").value=t.slice(0,4000);if(!$("pn").value)$("pn").value=nm;PNAME=f.name;$("fname").textContent=f.name};'
+ 'rd.readAsText(f)};'
+ 'var PRODUCTS=['
+ '{q:"Which Ray-Ban Meta Gen 2 finish wins with US tech buyers?",pn:"Ray-Ban Meta (Gen 2) Smart Glasses",pd:"Classic Wayfarer with Meta AI, 12MP camera, open-ear audio. Base USD 459.",aud:"US tech buyers 25-45, early adopters and creators",img:"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/6500/6500000_sd.jpg",'
+ 'vars:[{id:"matte-black",name:"Matte Black",like:"Meta AI + 12MP camera",obj:"USD 459 is steep for sunglasses",tweak:"Student discount",img:"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/6500/6500000_sd.jpg"},{id:"transitions",name:"Transitions Grey",like:"auto-darkening lenses",obj:"USD 539 premium over base",tweak:"Include case bundle",img:"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/6500/6500000_sd.jpg"},{id:"polished-white",name:"Polished White",like:"standout look",obj:"white shows wear fast",tweak:"Matte white option",img:"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/6500/6500000_sd.jpg"}],'
+ 'segs:[{id:"early-adopter",name:"Early Adopter"},{id:"creator",name:"Creator"},{id:"commuter",name:"Commuter"},{id:"gift-buyer",name:"Gift Buyer"}],'
+ 'means:{"early-adopter":{"matte-black":8.6,"transitions":7.8,"polished-white":6.9},"creator":{"matte-black":8.1,"transitions":7.5,"polished-white":7.2},"commuter":{"matte-black":7.4,"transitions":8.0,"polished-white":5.8},"gift-buyer":{"matte-black":7.0,"transitions":6.3,"polished-white":6.1}}},'
+ '{q:"Which HOKA Clifton 11 colorway wins with daily runners?",pn:"HOKA Clifton 11",pd:"Cushioned daily road-running shoe. Base USD 155.",aud:"US runners 20-50, road training",img:"https://cdn.shopify.com/s/files/1/0751/7203/files/100301256-BLKWHT-1.png?v=1782496810",'
+ 'vars:[{id:"blkwht",name:"Black / White",like:"goes with everything",obj:"shows dust fast",tweak:"Darker sole",img:"https://cdn.shopify.com/s/files/1/0751/7203/files/100301256-BLKWHT-1.png?v=1782496810"},{id:"whtwht",name:"White / White",like:"clean race look",obj:"stains in one run",tweak:"Stain coating",img:"https://cdn.shopify.com/s/files/1/0751/7203/files/100301256-WHTWHT-1.png?v=1782496809"},{id:"galag",name:"Galactic Grey",like:"hides wear",obj:"boring palette",tweak:"Brighter accents",img:"https://cdn.shopify.com/s/files/1/0751/7203/files/100301256-GALAGRSTELGRY-1.png?v=1782496810"}],'
+ 'segs:[{id:"marathoner",name:"Marathoner"},{id:"jogger",name:"Casual Jogger"},{id:"nurse",name:"On-feet Nurse"},{id:"walker",name:"Walker"}],'
+ 'means:{"marathoner":{"blkwht":8.4,"whtwht":7.0,"galag":7.2},"jogger":{"blkwht":7.8,"whtwht":7.3,"galag":7.1},"nurse":{"blkwht":7.2,"whtwht":6.2,"galag":8.3},"walker":{"blkwht":7.0,"whtwht":6.1,"galag":7.6}}},'
+ '{q:"Which JBL Vibe bundle wins with budget listeners?",pn:"JBL Vibe 200TWS",pd:"True wireless earbuds. Base USD 29.99.",aud:"Students and commuters, price-sensitive",img:"https://www.jbl.com/dw/image/v2/BFND_PRD/on/demandware.static/-/Sites-masterCatalog_Harman/default/dw2a3e45c4/1.JBL_Wave_Vibe%20200TWS_Hero_Ivory.png?sw=535&sh=535",'
+ 'vars:[{id:"base",name:"Base",like:"price",obj:"micro-USB charging",tweak:"USB-C port"},{id:"plus-case",name:"Plus Case",like:"extra 20h battery",obj:"USD 55 total",tweak:"Bundle discount"},{id:"sport",name:"Sport Fit",like:"stay-put wings",obj:"bulkier case",tweak:"Slimmer case"}],'
+ 'segs:[{id:"student",name:"Student"},{id:"commuter2",name:"Commuter"},{id:"gym",name:"Gym-goer"},{id:"gift2",name:"Gift Buyer"}],'
+ 'means:{"student":{"base":8.5,"plus-case":7.1,"sport":7.4},"commuter2":{"base":7.6,"plus-case":7.8,"sport":6.8},"gym":{"base":6.4,"plus-case":6.9,"sport":8.6},"gift2":{"base":7.2,"plus-case":7.9,"sport":6.6}}},'
+ '{q:"Which quilted leather jacket wins with city riders?",pn:"Diamond Quilted Biker Jacket",pd:"Black leather winter biker jacket. Base USD 189.",aud:"City riders 25-44, style plus warmth",img:"https://alexgear.com/cdn/shop/files/Men-Quilted-Black-Leather-Biker-Jacket.jpg?v=1720773215&width=1920",'
+ 'vars:[{id:"classic",name:"Classic",like:"clean silhouette",obj:"thin for deep winter",tweak:"Removable liner"},{id:"quilted",name:"Quilted",like:"warmth plus look",obj:"USD 229 premium",tweak:"Quilt only shoulders"},{id:"perforated",name:"Perforated",like:"shoulder-season use",obj:"not winterproof",tweak:"Zip-in wind layer"}],'
+ 'segs:[{id:"rider",name:"Rider"},{id:"style",name:"Style Buyer"},{id:"value",name:"Value Hunter"},{id:"gift3",name:"Gift Buyer"}],'
+ 'means:{"rider":{"classic":7.8,"quilted":8.2,"perforated":6.9},"style":{"classic":7.5,"quilted":8.0,"perforated":7.3},"value":{"classic":7.9,"quilted":6.4,"perforated":6.8},"gift3":{"classic":7.4,"quilted":7.6,"perforated":6.5}}}'
+ '];'
+ 'var lastProd=-1;'
+ 'function agg(trials,variations){var S=variations.map(function(v){var vt=trials.filter(function(t){return t.variation_id===v.id});'
+ 'var s=vt.map(function(t){return t.intent_score});var m=s.reduce(function(a,b){return a+b},0)/s.length;'
+ 'var sd=Math.sqrt(s.reduce(function(a,b){return a+(b-m)*(b-m)},0)/s.length);'
+ 'var hi=s.filter(function(x){return x>=8}).length/s.length*100;var br=vt.filter(function(t){return t.decision==="buy"}).length/vt.length*100;'
+ 'function topK(f){var c={};vt.forEach(function(t){var x=f(t);if(x)c[x]=(c[x]||0)+1});var bk=null,bv=0;for(var k in c)if(c[k]>bv){bv=c[k];bk=k}return bk}'
+ 'return{variation_id:v.id,variation_name:v.name,n:vt.length,mean_intent:+m.toFixed(2),std_intent:+sd.toFixed(2),pct_high_intent:+hi.toFixed(1),buy_rate:+br.toFixed(1),mean_value:null,top_objection:topK(function(t){return t.objection}),top_liked_feature:topK(function(t){return t.liked_feature}),top_tweak:topK(function(t){return t.suggested_tweak})}});'
+ 'var bp={};trials.forEach(function(t){bp[t.persona_id]=bp[t.persona_id]||{};var a=bp[t.persona_id][t.variation_id]=bp[t.persona_id][t.variation_id]||[];a.push(t.intent_score)});'
+ 'for(var p in bp)for(var v in bp[p]){var a=bp[p][v];bp[p][v]=+(a.reduce(function(x,y){return x+y},0)/a.length).toFixed(2)}'
+ 'var rk=S.slice().sort(function(a,b){return b.mean_intent-a.mean_intent||b.buy_rate-a.buy_rate}).map(function(s){return s.variation_id});'
+ 'return{summaries:S,by_persona:bp,ranking:rk}}'
+ 'function genSample(P){var trials=[];var vus=P.vars.map(function(v){return{id:v.id,name:v.name,img:v.img||P.img}});var pus=P.segs.map(function(s){return{id:s.id,name:s.name}});'
+ 'P.vars.forEach(function(v){P.segs.forEach(function(sg){var m=P.means[sg.id][v.id];for(var r=0;r<3;r++){'
+ 'var sc=Math.max(1,Math.min(10,Math.round(m+(Math.random()*2.6-1.3))));var dec=sc>=8?"buy":(sc<=4?"no":"maybe");var buy=dec==="buy";'
+ 'trials.push({persona_id:sg.id,variation_id:v.id,intent_score:sc,decision:dec,value_score:Math.max(1,Math.min(10,sc-1)),price_fairness:Math.max(1,Math.min(5,Math.round(sc/2))),willingness_to_pay:buy?"Yes at list":"Depends on price",liked_feature:v.like,objection:buy?null:v.obj,suggested_tweak:buy?null:v.tweak,reasoning:buy?("Does exactly what I need: "+v.like+"."):("Close, but "+v.obj+". "+v.tweak+" would flip me.")})}})});'
+ 'var a=agg(trials,vus);'
+ 'return{sample:1,model:"kimi-k2-instruct",use_case:"variation-test",total_trials:trials.length,ranking:a.ranking,summaries:a.summaries,by_persona:a.by_persona,trials:trials,variations_used:vus,personas_used:pus,product_img:P.img,product_name:P.pn}}'
+ 'function toast(m){var t=$("toast");t.style.display="block";t.textContent=m}'
+ 'function wait(ms){return new Promise(function(r){setTimeout(r,ms)})}'
+ 'async function demoRun(){var i;do{i=Math.floor(Math.random()*PRODUCTS.length)}while(i===lastProd&&PRODUCTS.length>1);lastProd=i;var P=PRODUCTS[i];'
+ '$("q").value=P.q;$("pn").value=P.pn;$("pd").value=P.pd;$("aud").value=P.aud;'
+ 'stage(0);await wait(500);stage(1);await wait(500);stage(2);await wait(500);stage(3);render(genSample(P))}'
+ 'function stage(i){["s1","s2","s3"].forEach(function(s,j){var e=$(s);e.className="stg"+(j<i?" ok":(j===i?" act":""))});'
+ '$("fill").style.width=(i/3*100)+"%"}'
+ 'function heat(c){var t=Math.max(1,Math.min(10,c||1));var L=Math.round(95-(t-1)*8.5);var fg=L>55?"#171717":"#ffffff";return "background:hsl(0,0%,"+L+"%);color:"+fg+";font-weight:700"}'
+ 'function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;")}'
+ 'async function post(p,b){var r=await fetch(p,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(b)});'
+ 'var d=await r.json();if(!r.ok)throw new Error(d.error||("HTTP "+r.status));return d}'
+ 'function base(){return{apiKey:$("key").value,model:$("model").value,'
+ 'product:{name:$("pn").value||PNAME||"Demo product",description:$("pd").value||"A new product idea"},'
+ 'business_question:$("q").value||"Which idea wins?",target_audience:$("aud").value||"General consumers",'
+ 'tech_constraints:$("cons").value}}'
+ '$("run").onclick=async function(){'
+ 'var btn=$("run");btn.disabled=true;$("out").innerHTML="";$("toast").style.display="none";'
+ 'var cfg=scaleP(),nv=4,np=cfg,nr=1;'
+ 'try{'
+ 'if(!$("key").value){await demoRun();btn.disabled=false;return;}'
+ 'stage(0);var b=base();b.n=nv;'
+ 'var v=await post("/api/variations",b);'
+ 'stage(1);b.n=np;b.product=b.product;var p=await post("/api/personas",b);'
+ 'stage(2);var t=await post("/api/trials",{apiKey:b.apiKey,model:b.model,product:b.product,'
+ 'business_question:b.business_question,variations:v.variations,personas:p.personas,runs:nr});'
+ 'stage(3);render(t);'
+ '}catch(e){toast(e.message)}$("run").disabled=false;btn.disabled=false};'
+ 'function render(t){var h="";var w=t.summaries[0];var badge=t.sample?"<span class=samplebadge>Sample</span>":"";'
+ 'var pim=t.product_img?("<img class=pimg src=\'"+t.product_img+"\' loading=lazy referrerpolicy=no-referrer>"):"";'
+ 'h+="<div id=hero style=display:block>"+pim+"<div class=t>Winner</div><div class=n>"+esc(w.variation_name)+"</div><div class=s>"+w.mean_intent+"</div></div>";'
+ 'h+="<h2>Grid"+badge+"</h2>";'
+ 't.variations_used.forEach(function(v){var cells=t.trials.filter(function(x){return x.variation_id===v.id});'
+ 'var sm=null;t.summaries.forEach(function(s){if(s.variation_id===v.id)sm=s});'
+ 'var th=v.img?("<img class=vthumb src=\'"+v.img+"\' loading=lazy referrerpolicy=no-referrer>"):"";'
+ 'h+="<div class=vlab>"+th+esc(v.name)+" <span>"+sm.mean_intent+" · "+cells.length+" calls</span></div><div class=tgrid>";'
+ 'cells.forEach(function(tr){var sc=Math.max(1,Math.min(10,tr.intent_score));var L=Math.round(95-(sc-1)*8.5);'
+ 'var who="";t.personas_used.forEach(function(p){if(p.id===tr.persona_id)who=p.name});'
+ 'h+="<span class=sq"+(tr.decision==="buy"?" buy":"")+" style=\'background:hsl(0,0%,"+L+"%)\' title=\'"+esc(who)+" — "+esc(v.name)+": "+sc+"/10 "+tr.decision+"\'></span>"});h+="</div>"});'
+ 'h+="<h2>Ranking"+badge+"</h2>";t.summaries.forEach(function(s){var w2=s.mean_intent*10;'
+ 'h+="<div class=vrow><span class=nm>"+esc(s.variation_name)+"</span><span class=sc>"+s.mean_intent+"</span>";'
+ 'h+="<div class=tr><i style=width:"+w2+"%></i></div>";'
+ 'h+="<div class=mt>n="+s.n+" · buy "+s.buy_rate+"% · top "+s.pct_high_intent+"%"+(s.top_objection?" · "+esc(s.top_objection):"")+"</div></div>"});'
+ 'h+="<h2>Segments</h2><table class=heat><tr><th></th>";'
+ 't.variations_used.forEach(function(v){h+="<th>"+esc(v.name)+"</th>"});h+="</tr>";'
+ 't.personas_used.forEach(function(p){h+="<tr><th>"+esc(p.name)+"</th>";'
+ 't.variations_used.forEach(function(v){var c=(t.by_persona[p.id]||{})[v.id];'
+ 'h+="<td style=\'"+heat(c)+"\'>"+(c==null?"–":c)+"</td>"});h+="</tr>"});h+="</table>";'
+ 'var chips=t.summaries.map(function(s){return s.top_tweak}).filter(Boolean);'
+ 'if(chips.length){h+="<h2>Fixes</h2>"+chips.map(function(c){return "<span class=chip>"+esc(c)+"</span>"}).join("")}'
+ '$("out").innerHTML=h;var sqs=document.querySelectorAll(".sq");for(var qi=0;qi<sqs.length;qi++){(function(el,q){setTimeout(function(){el.className+=" in"},q*16)})(sqs[qi],qi)}window.scrollTo(0,document.body.scrollHeight)}'
+ '</script></body></html>';

const DOCS = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
+ '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sim2Sale — Docs</title>'
+ '<style>'
+ ':root{--bg:#f4f4f1;--card:#ffffff;--line:#e3e3de;--tx:#171717;--dim:#8b8b86}'
+ '*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.6 -apple-system,Inter,SF Pro,Segoe UI,Roboto,sans-serif}'
+ '.w{max-width:720px;margin:0 auto;padding:18px 16px 60px}'
+ 'header{display:flex;align-items:center;gap:10px;padding:14px 2px 18px}.logo{font-weight:800;font-size:14px;background:#171717;color:#fff;border-radius:8px;padding:3px 7px}.name{font-weight:800;font-size:19px}.sp{flex:1}'
+ '.doclink{display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:10px;padding:7px 11px;font-size:13px;font-weight:700;color:var(--tx);text-decoration:none;background:var(--card)}'
+ '.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:15px 16px;margin-bottom:10px}'
+ '.card h3{margin:0 0 8px;font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.4px}'
+ 'p{margin:0 0 10px;font-size:14px}p:last-child{margin:0}'
+ 'code{background:#efefeb;border-radius:6px;padding:1px 6px;font-size:13px}'
+ 'table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border:1px solid var(--line);text-align:left}th{background:#efefeb;color:var(--dim)}'
+ '.flow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}@media(max-width:600px){.flow{grid-template-columns:1fr}}'
+ '.stepno{font-size:22px;font-weight:800}'
+ '</style></head><body><div class="w">'
+ '<header><span class="logo">S2S</span><span class="name">Sim2Sale</span><span class="sp"></span>'
+ '<a class="doclink" href="/">Back</a></header>'
+ '<div class="card"><h3>WHAT IT DOES</h3><p>Sim2Sale tests product ideas before you build them. You describe a product and an audience; it generates candidate variations, builds buyer personas for that audience, then simulates each persona buying each variation — and ranks what wins, with whom, and why.</p><p>This page is only a test console. Every simulation is created by <code>Vultr Serverless Inference</code> — all model calls run on Vultr, nothing intelligent happens in the browser or on Cloudflare.</p></div>'
+ '<div class="flow">'
+ '<div class="card"><h3>01 — VARIATIONS</h3><p><span class="stepno">01</span></p><p>One model call turns your product + tech limits into distinct candidates (pack, price, feature angles), each scored 1–5 for feasibility. Anything unbuildable is dropped before it costs you trials.</p></div>'
+ '<div class="card"><h3>02 — SEGMENTS</h3><p><span class="stepno">02</span></p><p>One model call turns your audience line into buyer personas spanning ages, incomes, values and buying styles — skeptics included. Each becomes a roleplayed buying agent.</p></div>'
+ '<div class="card"><h3>03 — TRIALS</h3><p><span class="stepno">03</span></p><p>Every persona × variation × run is one structured interview: intent 1–10, buy / maybe / no, value, price fairness, objection, liked feature, one fix. Calls fan out 20 at a time.</p></div>'
+ '</div>'
+ '<div class="card"><h3>SCORING</h3><p>Winner = highest mean intent, buy rate breaks ties. Ranking bars show mean intent; the Segments grid shows mean intent per persona × variation; Fixes collects the most-requested change per candidate.</p></div>'
+ '<div class="card"><h3>WHERE IT RUNS</h3><p><code>Vultr Serverless Inference</code> is the engine: variation generation, persona generation, and every buyer trial are LLM calls to Vultr\u2019s OpenAI-compatible endpoint (<code>/v1/chat/completions</code>), in JSON mode so answers stay machine-readable, with retries and a neutral fallback so one failure never kills a run.</p><p>Cloudflare is only the test harness for this demo page: it relays your inputs to Vultr, fans the trial calls out 20 at a time, aggregates the scores, and renders the result. No model runs on Cloudflare. The test harness caps runs at <code>48 calls</code> (Scale slider: 20–48); production sweeps run against the backend jobs endpoint instead.</p></div>'
+ '<div class="card"><h3>KEY + MODELS</h3><p>Your Vultr key stays in your browser (localStorage) unless you bake it in with <code>wrangler secret put VULTR_INFERENCE_API_KEY</code>. Resolution order: request header, request body, server secret. The model list refreshes live from Vultr; Default is <code>kimi-k2-instruct</code>.</p></div>'
+ '<div class="card"><h3>DEMO MODE</h3><p>Running without a key renders a labeled sample simulation — each run rotates a repo-grounded product (Ray-Ban Meta, HOKA Clifton, JBL Vibe, quilted biker jacket, all real crawler records with photos) — so the interface can be evaluated offline. It is badged Sample and never mixed with live Vultr results.</p></div>'
+ '<div class="card"><h3>API</h3><table><tr><th>Method</th><th>Path</th><th>Does</th></tr>'
+ '<tr><td>GET</td><td><code>/api/health</code></td><td>liveness</td></tr>'
+ '<tr><td>GET</td><td><code>/api/models</code></td><td>live Vultr model list</td></tr>'
+ '<tr><td>POST</td><td><code>/api/variations</code></td><td>generate candidates</td></tr>'
+ '<tr><td>POST</td><td><code>/api/personas</code></td><td>generate personas</td></tr>'
+ '<tr><td>POST</td><td><code>/api/trials</code></td><td>run matrix, return ranking</td></tr>'
+ '</table></div>'
+ '</div></body></html>';

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: { "access-control-allow-origin": "*", "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "content-type,x-vultr-key" } });
    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html"))
      return new Response(HTML, { headers: { "content-type": "text/html;charset=utf-8" } });
    if (req.method === "GET" && url.pathname === "/docs")
      return new Response(DOCS, { headers: { "content-type": "text/html;charset=utf-8" } });
    if (req.method === "GET" && url.pathname === "/api/health") return json({ ok: true, name: "sim2sale" });
    if (req.method === "GET" && url.pathname === "/api/models") {
      const k = keyOf(req, {}, env);
      if (!k) return err("missing Vultr key (x-vultr-key header or secret)", 401);
      const r = await fetch(`${VULTR_BASE}/models`, { headers: { authorization: `Bearer ${k}` } });
      if (!r.ok) return err(`vultr ${r.status}`, 502);
      const d = await r.json();
      return json({ data: (d.data || []).map((m) => m.id || m) });
    }
    if (req.method !== "POST") return err("not found", 404);
    let body = {};
    try { body = await req.json(); } catch { return err("invalid JSON"); }
    const apiKey = keyOf(req, body, env);
    if (!apiKey) return err("missing Vultr key — paste it in the UI or set secret VULTR_INFERENCE_API_KEY", 401);
    const model = body.model || DEFAULT_MODEL;

    try {
      if (url.pathname === "/api/variations") {
        const n = Math.max(1, Math.min(8, body.n || 3));
        const p = body.product || {};
        const data = await chat(apiKey, model, VAR_SYS,
          `Product: ${p.name || "?"} (${p.category || "general"})\nBase: ${p.description || ""}\nPrice: ${p.base_price || "?"}\nQuestion: ${body.business_question || "Which wins?"}\nBrief: ${body.variation_brief || `Explore ${n} diverse buildable candidates`}\nConstraints (NEVER violate): ${body.tech_constraints || "prefer low-effort adjacent ideas"}\nGenerate ${n} DIVERSE variations.\nJSON: {"variations": [{"id": "kebab", "name": "", "description": "", "price": "", "features": [], "feasibility": 1-5, "build_effort": "low|medium|high", "why_different": ""}]}`,
          0.8, 2500);
        const seen = new Set(), kept = [], dropped = [];
        (data.variations || []).forEach((v, i) => {
          const id = slug(v.id, `auto-${i + 1}`);
          if (seen.has(id)) return;
          seen.add(id);
          const item = { id, name: v.name || id, description: v.description || "", price: v.price || null, features: v.features || [], feasibility: v.feasibility ?? 3, build_effort: v.build_effort || null, why_different: v.why_different || null };
          ((item.feasibility ?? 3) >= 3 ? kept : dropped).push(item);
        });
        if (!kept.length) return err("0 variations passed feasibility — loosen constraints", 502);
        return json({ variations: kept, dropped, model });
      }

      if (url.pathname === "/api/personas") {
        const n = Math.max(1, Math.min(12, body.n || 5));
        const p = body.product || {};
        const data = await chat(apiKey, model, PER_SYS,
          `Product: ${p.name || "?"} — ${p.description || ""}\nQuestion: ${body.business_question || "Which wins?"}\nAudience: ${body.target_audience || "general consumers"}\nGenerate ${n} diverse personas (ages, incomes, skeptics + fans).\nJSON: {"personas": [{"id": "kebab", "name": "", "segment": "", "age_range": "", "occupation": "", "income_level": "low|mid|high", "values": [], "pain_points": [], "buying_style": "", "bio": ""}]}`,
          0.8, 2000);
        const personas = (data.personas || []).map((x, i) => ({ id: slug(x.id, `p${i + 1}`, ), name: x.name || `P${i + 1}`, segment: x.segment || null, age_range: x.age_range || null, occupation: x.occupation || null, income_level: x.income_level || null, values: x.values || [], pain_points: x.pain_points || [], buying_style: x.buying_style || null, bio: x.bio || null }));
        if (!personas.length) return err("persona generation empty", 502);
        return json({ personas, model });
      }

      if (url.pathname === "/api/trials") {
        const variations = body.variations || [], personas = body.personas || [];
        const runs = Math.max(1, Math.min(2, body.runs || 1));
        const total = variations.length * personas.length * runs;
        if (!variations.length || !personas.length) return err("variations + personas required");
        if (total > MAX_TRIALS) return err(`too large: ${total} trials (worker cap ${MAX_TRIALS}) — lower counts or use the local backend`, 400);
        const p = body.product || {};
        const tasks = [];
        for (const v of variations) for (const pe of personas) for (let k = 0; k < runs; k++) tasks.push([v, pe]);
        const trials = await mapConc(tasks, CONC, async ([v, pe], idx) => {
          await new Promise((r) => setTimeout(r, (idx % CONC) * 120));
          const sys = `Roleplay a realistic consumer for a buying-intent study.\nName: ${pe.name}\nIncome: ${pe.income_level || "?"}\nValues: ${(pe.values || []).join(", ")}\nPains: ${(pe.pain_points || []).join(", ")}\nStyle: ${pe.buying_style || "?"}\nBio: ${pe.bio || ""}\n${AGENT_RULES}`;
          const usr = `Product: ${p.name} — ${p.description}\nCandidate: ${v.name} — ${v.description} @ ${v.price || "?"} (${(v.features || []).join(", ")})\nHow likely to buy now (1-10)?\nJSON: {"intent_score": 1-10, "decision": "buy|maybe|no", "value_score": 1-10, "price_fairness": 1-5, "willingness_to_pay": "", "liked_feature": "", "objection": "", "suggested_tweak": "", "reasoning": ""}`;
          try {
            let d = null, lastErr = null;
            for (let a = 0; a < 2 && !d; a++) {
              try { d = await chat(apiKey, model, sys, usr, 0.7, 1000); } catch (e) { lastErr = e; }
            }
            if (!d) throw lastErr;
            const score = Math.max(1, Math.min(10, parseInt(d.intent_score) || 5));
            let dec = d.decision;
            if (!["buy", "maybe", "no"].includes(dec)) dec = score >= 8 ? "buy" : score <= 3 ? "no" : "maybe";
            return { persona_id: pe.id, variation_id: v.id, intent_score: score, decision: dec, value_score: d.value_score ?? null, price_fairness: d.price_fairness ?? null, willingness_to_pay: d.willingness_to_pay || null, liked_feature: d.liked_feature || null, objection: d.objection || null, suggested_tweak: d.suggested_tweak || null, reasoning: d.reasoning || null };
          } catch (e) { return { persona_id: pe.id, variation_id: v.id, intent_score: 5, decision: "maybe", reasoning: "[fallback: " + ((e && e.message) || e) + "]" }; }
        });
        const { summaries, by_persona, ranking } = aggregate(trials, variations);
        return json({ model, use_case: "variation-test", total_trials: trials.length, ranking, summaries, by_persona, trials, variations_used: variations, personas_used: personas });
      }

      return err("not found", 404);
    } catch (e) { return err(`inference failed: ${e.message}`, 502); }
  },
};
