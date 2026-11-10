const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

async function runtime(respond, stored = {}) {
  const handlers = {}, requests = [], timers = new Map(), session = new Map(Object.entries(stored));
  let timerId = 0;
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {hidden:false,textContent:'',innerHTML:'',addEventListener(name, fn) { handlers[id + ':' + name] = fn; },querySelectorAll(){return [];}});
    return elements.get(id);
  }
  const D = {holdings:[],indices:[],events:[]};
  const A = {D,live:true,rerender(){},renderRail(){},start(){},progress(){return '';},researchContent(){return 'progress';},refreshDrawer(){}};
  const context = vm.createContext({App:A, console, location:{hash:'#research',reload(){}},
    window:{},document:{getElementById:element,querySelector:element,addEventListener(name,fn){handlers[name] = fn;}},
    sessionStorage:{getItem:k=>session.get(k)||null,setItem:(k,v)=>session.set(k,v),removeItem:k=>session.delete(k)},
    setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    fetch:async (url,options)=>{requests.push({url,options});const body=await respond(url,options);return {ok:true,status:200,json:async()=>body};}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/api.js'),'utf8'),context);
  await flush();
  return {D,A,handlers,requests,timers,session,elements};
}

function defaults(url) {
  if (url === '/api/me') return {user:'alice'};
  if (url === '/api/holdings') return {holdings:[{code:'sz002079',qty:'1',cost:'9'}]};
  if (url === '/api/brief') return null;
  if (url === '/api/brief?date=2026-10-28') return {date:'2026-10-28',title:'往期',events:[]};
  if (url === '/api/briefs') return {today:'2026-10-29',briefs:[{date:'2026-10-28',title:'往期'}]};
  if (url === '/api/research') return {records:[]};
  if (url === '/api/system') return {sources:[],usage:{brief:0,research:0},limits:{brief:5,research:30}};
  if (url.startsWith('/api/quotes')) return {quotes:{}};
  throw Error('unexpected ' + url);
}
function target(selector, value={}) { return {closest:s => s === selector ? value : null}; }

test('research submits captured text and polls independently from brief', async () => {
  const r = await runtime((url,options) => {
    if (url === '/api/research' && options.method === 'POST') return {job_id:'research1'};
    if (url === '/api/brief/generate') return {job_id:'brief1'};
    if (url.startsWith('/api/jobs/')) return {id:url.split('/').at(-1),kind:url.endsWith('brief1')?'brief':'research',status:'running',steps:[],input:{}};
    return defaults(url);
  });
  r.D.question = '解禁有什么变化？';
  await r.handlers.submit({target:target('[data-research-form]'),preventDefault(){}}); await flush();
  const sent = r.requests.find(x=>x.url==='/api/research' && x.options.method==='POST');
  assert.equal(JSON.parse(sent.options.body).question,'解禁有什么变化？');
  await r.handlers.click({target:target('[data-generate]')}); await flush();
  assert.equal(r.D.researchJob.id,'research1');
  assert.equal(r.D.job.id,'brief1');
  assert.equal(r.timers.size,2);
});

test('draft survives adding and removing a different stock; save invalidates previous brief', async () => {
  let saved;
  const r = await runtime((url,options) => {
    if (url==='/api/holdings' && options.method==='PUT') { saved=JSON.parse(options.body);return {holdings:saved}; }
    // A changed portfolio has no briefs of its own, so nothing older may stand in for it.
    if (url==='/api/briefs' && saved) return {today:'2026-10-29',briefs:[]};
    return defaults(url);
  });
  r.handlers.input({target:{name:'qty',value:'0.1234567890123456789',closest:s=>s==='[data-holding-row]'?{dataset:{holdingRow:'sz002079'}}:null}});
  r.D.searchResults = [{code:'sh600519',name:'贵州茅台'}];
  await r.handlers.click({target:target('[data-add-code]',{dataset:{addCode:'sh600519'}})});
  await r.handlers.click({target:target('[data-remove-holding]',{dataset:{removeHolding:'sh600519'}})});
  r.D.brief = {title:'旧组合'};
  await r.handlers.click({target:target('[data-save-holdings]')}); await flush();
  assert.equal(saved[0].qty,'0.1234567890123456789');
  assert.equal(saved.length,1);
  assert.equal(r.D.brief,null);
  assert.equal(r.D.holdingMessage,'已保存');
});

test('opens on the latest brief until today has one', async () => {
  const r = await runtime(defaults);
  assert.equal(r.D.briefDate,'2026-10-28');
  assert.equal(r.D.brief.title,'往期');
  const fresh = await runtime(url => url==='/api/brief'?{date:'2026-10-29',title:'今日',events:[]}:defaults(url));
  assert.equal(fresh.D.briefDate,null);
  assert.equal(fresh.D.brief.title,'今日');
});

test('picking a past date loads that brief and picking today returns to it', async () => {
  const r = await runtime(url => url==='/api/brief?date=2026-10-29'?null:defaults(url));
  assert.equal(r.D.today,'2026-10-29');
  r.handlers.change({target:{id:'brief-date',value:'2026-10-28'}}); await flush();
  assert.equal(r.D.briefDate,'2026-10-28');
  assert.equal(r.D.brief.title,'往期');
  r.handlers.change({target:{id:'brief-date',value:'2026-10-29'}}); await flush();
  assert.equal(r.D.briefDate,null);
  assert.equal(r.D.brief,null);
});

test('refresh restores pending research and historical result selection uses job endpoint', async () => {
  const finished = {id:'r1',kind:'research',status:'done',steps:[],input:{kind:'research'},result:{title:'已保存研究'}};
  const r = await runtime(url => url==='/api/jobs/r1'?finished:defaults(url), {'jianwei-research-job:alice':'r1'});
  assert.equal(r.D.research.title,'已保存研究');
  assert.equal(r.session.has('jianwei-research-job:alice'),false);
  await r.handlers.click({target:target('[data-research-record]',{dataset:{researchRecord:'r1'}}),preventDefault(){}}); await flush();
  assert.equal(r.D.research.title,'已保存研究');
  assert.equal(r.requests.filter(x=>x.url==='/api/jobs/r1').length,2);
});
