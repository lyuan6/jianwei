const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function app(protocol) {
  const ctx = vm.createContext({ window: {}, location: { protocol }, console });
  for (const file of ['data.js', 'ui.js', 'brief.js', 'pages.js']) {
    if (ctx.window.App) ctx.App = ctx.window.App;
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../web', file), 'utf8'), ctx);
  }
  return ctx.App;
}

test('site exposes the paper-and-ink brand icon', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const icon = fs.readFileSync(path.join(__dirname, '../web/icon.svg'), 'utf8');
  assert.match(html, /rel="icon" type="image\/svg\+xml" href="icon\.svg"/);
  assert.match(html, /rel="apple-touch-icon" href="apple-touch-icon\.png"/);
  assert.ok(fs.existsSync(path.join(__dirname, '../web/apple-touch-icon.png')));
  assert.ok(fs.existsSync(path.join(__dirname, '../web/favicon.ico')));
  assert.match(icon, /fill="#1C1B18"/);
  assert.match(icon, /stroke="#F5F2EA"/);
});

test('online starts without sample events, holdings or fake charts', () => {
  const a = app('https:');
  a.D.loading = false;
  const html = a.pages.brief();
  assert.equal(a.D.events.length, 0);
  assert.equal(a.D.holdings.length, 0);
  assert.match(html, /今日晨报尚未生成/);
  assert.match(html, /data-generate/);
  assert.doesNotMatch(html, /<svg|明日解禁|9660/);
  assert.match(a.pages.research(), /输入问题/);
});

test('live report escapes model text and shows unchecked numbers and real source links', () => {
  const a = app('https:');
  const event = { code: 'sz002079', level: 'high', tag: '解禁', date: '2026-10-26', title: '<img src=x onerror=alert(1)>',
    figures: [{ label: '股数', value: '999', ref: 'R1', verification: '未核对' }],
    points: ['<script>alert(1)</script>'], risk: '风险', refs: ['R1'] };
  a.D.holdings = [{code:'sz002079', name:'苏州固锝', close:null, chg:null}];
  a.D.loading = false;
  a.D.events = [event];
  a.D.brief = { title:'今日 1 项重点、0 项留意', lede:'<b>文本</b>', events:[event], status:'完成',
    stocks:[{code:'sz002079', events:[event], steps:[{tool:'get_unlocks', arguments:{code:'sz002079'}, summary:'摘要', ref:'R1', elapsed_seconds:1.2, error:false}],
      references:{R1:[{url:'https://example.test/source', title:'真实来源'}, {url:'javascript:alert(1)', title:'无效链接'}]}}] };
  const html = a.pages.brief();
  assert.match(html, /未核对/);
  assert.match(html, /999/);
  assert.match(html, /href="https:\/\/example.test\/source"/);
  assert.doesNotMatch(html, /href="javascript:|<script>|<img |<b>文本<\/b>/);
  assert.match(html, /&lt;b&gt;文本/);
  assert.match(html, /1.2 秒/);
  assert.match(html, /工具结果/);
});

test('past briefs can be picked and do not offer generation', () => {
  const a = app('https:');
  a.D.loading = false;
  a.D.today = '2026-10-29';
  a.D.briefs = [{ date: '2026-10-28', title: '今日 0 项重点、2 项留意' }];
  let html = a.pages.brief();
  assert.match(html, /<option value="2026-10-29" selected>今天<\/option><option value="2026-10-28">2026-10-28/);
  assert.match(html, /data-generate/);
  a.D.briefDate = '2026-10-28';
  a.D.brief = { date: '2026-10-28', title: '今日 0 项重点、2 项留意', lede: '', events: [], stocks: [], status: '完成' };
  html = a.pages.brief();
  assert.match(html, /value="2026-10-28" selected/);
  assert.match(html, /今日 0 项重点、2 项留意/);
  assert.doesNotMatch(html, /data-generate|class="indices"/);
});

test('direct file preview retains sample content', () => {
  const a = app('file:');
  assert.equal(a.D.holdings.length, 8);
  assert.match(a.pages.brief(), /今日 2 项重点、2 项留意/);
});

test('holdings keep draft values while quotes update', () => {
  const a = app('https:');
  a.D.holdings = [{code:'sz002079', name:'苏州固锝', qty:'1', cost:'9', close:'8.04'}];
  a.D.holdingDraft = [{code:'sz002079', name:'苏州固锝', qty:'0.1234567890123456789', cost:'9.8600'}];
  const html = a.pages.holdings();
  assert.match(html, /value="0.1234567890123456789"/);
  assert.match(html, /value="9.8600"/);
  assert.match(html, /8.04/);
  assert.match(html, /data-remove-holding/);
});

test('research renders table, gaps, workpaper and real sources without interpreting model HTML', () => {
  const a = app('https:');
  a.D.research = {title:'<script>bad</script>', paragraphs:[{text:'占比999%', refs:['R1'], numbers:[{value:'999',verification:'未核对'}]}],
    table:{caption:'比例', columns:['指标','百分数','来源'], rows:[{cells:['解禁','11.91','公告原文 R1'], refs:['R1'], numbers:[]}]},
    gaps:['缺少计划'], steps:[], references:{R1:[{url:'https://example.test/real',title:'原文'}]}};
  a.D.researchJob = {status:'done',steps:[{tool:'get_unlocks',arguments:{code:'sz002079'},summary:'1 条',ref:'R1',elapsed_seconds:1,error:false}]};
  a.D.researchRecords = [{id:'abc',input:{question:'历史问题'},status:'done'}];
  const html = a.pages.research();
  assert.match(html, /<table/);
  assert.match(html, /未核对：999/);
  assert.match(html, /工作底稿/);
  assert.match(html, /https:\/\/example.test\/real/);
  assert.match(html, /<th>来源<\/th><th>来源链接<\/th>/);
  assert.match(html, /公告原文 R1/);
  assert.match(html, /data-research-record="abc"/);
  assert.doesNotMatch(html, /<script>/);
});

test('stock chart uses only supplied bars and shows partial failures', () => {
  const a = app('https:');
  a.D.stock = {symbol:'sz002079',history:{bars:[{date:'2026-10-23',close:'8.12'},{date:'2026-10-26',close:'8.04'}],adjustment:'前复权',source:'腾讯',url:'https://gu.qq.com/sz002079'},
    events:[],errors:[{source:'公告',detail:'数据不可用'}]};
  const html = a.drawer('sz002079');
  assert.match(html, /2 个交易日/);
  assert.match(html, /前复权/);
  assert.match(html, /数据不可用/);
  assert.match(html, /<svg/);
  assert.doesNotMatch(html, /90 日|NaN|Infinity/);
  a.D.stock.history = null;
  assert.doesNotMatch(a.drawer('sz002079'), /<svg/);
});

test('system distinguishes configured from observed healthy', () => {
  const a = app('https:');
  a.D.system = {sources:[{name:'DeepSeek',use:'模型',configured:true,state:'unknown',checked_at:null}],usage:{brief:1,research:2},limits:{brief:5,research:30}};
  const html = a.pages.system();
  assert.match(html, /已配置/);
  assert.match(html, /无记录/);
  assert.doesNotMatch(html, /正常|100%/);
});
