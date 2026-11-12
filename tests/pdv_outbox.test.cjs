/* State-machine tests. Browser UI separately exercises native IndexedDB. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/js/pdv-outbox.js','utf8');
function memoryIDB(){
  const stores={state:new Map(),sales:new Map()};let chain=Promise.resolve();
  const clone=v=>v===undefined?undefined:structuredClone(v);
  const db={close(){},transaction(name){
    const jobs=[],t={};t.objectStore=()=>({get:key=>request(()=>clone(stores[name].get(key))),getAll:()=>request(()=>[...stores[name].values()].map(clone)),add:value=>request(()=>{if(stores[name].has(value.request_id))throw Error('ConstraintError');stores[name].set(value.request_id,clone(value));return value.request_id;}),put:(value,key)=>request(()=>{stores[name].set(key??value.request_id,clone(value));return key??value.request_id;})});
    function request(action){const r={};jobs.push(()=>{r.result=action();r.onsuccess?.();});return r;}
    const previous=chain;chain=new Promise(resolve=>setImmediate(async()=>{await previous;try{while(jobs.length)jobs.shift()();t.oncomplete?.();}catch(e){t.error=e;t.onerror?.();}resolve();}));return t;
  }};
  return {open(){const r={};setImmediate(()=>{r.result=db;r.onsuccess?.();});return r;}};
}
function load(idb){const ctx={indexedDB:idb,fetch:()=>{},structuredClone};vm.createContext(ctx);vm.runInContext(source,ctx);return ctx.PDVOutbox;}
const payload=()=>({organization:'tenant-a',user:1,request_id:'intent-one',device:'device-one',items:[{sku:'A',quantity:'1',unit_price:'10.01'}],payments:[{forma:1,valor:'10.01'}]});
const response=(status,data,type='application/json')=>({ok:status===200,status,headers:{get:()=>type},json:async()=>data});
const context=()=>response(200,{organization:'tenant-a',user:1,csrf:'synthetic-only'});
test('durable pending intent and cart survive a new application context',async()=>{const idb=memoryIDB(),first=load(idb);await first.state.put('cart',{quantity:'2'});await first.enqueue(payload());const second=load(idb);assert.equal((await second.sales.all())[0].status,'PENDENTE');assert.equal((await second.state.get('cart')).quantity,'2');});
test('no connection preserves the pending operation',async()=>{const o=load(memoryIDB());await o.enqueue(payload());await o.sync(async()=>{throw Error('offline');});assert.equal((await o.sales.all())[0].status,'PENDENTE');});
test('lost confirmation then automatic retry uses identical UUID and confirms once',async()=>{const o=load(memoryIDB());await o.enqueue(payload());let calls=0;const accepted=new Set();const fetcher=async(url,opts)=>{if(url.endsWith('contexto/'))return context();const body=JSON.parse(opts.body);accepted.add(body.request_id);if(++calls===1)throw Error('response lost');return response(200,{receipt:'/pdv/vendas/confirmed/comprovante/'});};await o.sync(fetcher);assert.equal((await o.sales.all())[0].status,'PENDENTE');await o.sync(fetcher);assert.equal(accepted.size,1);assert.equal((await o.sales.all())[0].status,'CONFIRMADA');});
test('confirmed operation cannot be overwritten by a stale pending attempt',async()=>{const o=load(memoryIDB());await o.enqueue(payload());const original=(await o.sales.all())[0];await o.sales.put({...original,status:'CONFIRMADA',receipt:'/receipt'});await o.sales.put({...original,status:'PENDENTE'});assert.equal((await o.sales.all())[0].status,'CONFIRMADA');});
test('price/stock/closed-cash conflict keeps the original operation for review',async()=>{const o=load(memoryIDB());await o.enqueue(payload());await o.sync(async url=>url.endsWith('contexto/')?context():response(409,{error:'Preço ou estoque mudou'}));const row=(await o.sales.all())[0];assert.equal(row.status,'CONFLITO');assert.equal(row.payload.items[0].unit_price,'10.01');assert.equal(row.payload.request_id,'intent-one');});
test('revoked permission records a preserved conflict',async()=>{const o=load(memoryIDB());await o.enqueue(payload());await o.sync(async url=>url.endsWith('contexto/')?context():response(403,{error:'Permissão retirada'}));assert.equal((await o.sales.all())[0].status,'CONFLITO');});
test('another tenant or user never submits the old operators queue',async()=>{for(const identity of [{organization:'tenant-b',user:1},{organization:'tenant-a',user:2}]){const o=load(memoryIDB());await o.enqueue(payload());let sent=false;await o.sync(async url=>{if(url.endsWith('contexto/'))return response(200,identity);sent=true;return response(200,{});});assert.equal(sent,false);assert.equal((await o.sales.all())[0].status,'PENDENTE');}});
test('expired login or server error does not discard the pending sale',async()=>{const o=load(memoryIDB());await o.enqueue(payload());await o.sync(async url=>url.endsWith('contexto/')?context():response(200,{},'text/html'));assert.equal((await o.sales.all())[0].status,'PENDENTE');await o.sync(async url=>url.endsWith('contexto/')?context():response(503,{}));assert.equal((await o.sales.all())[0].status,'PENDENTE');});
test('interrupted ENVIANDO recovers and duplicate local enqueue is denied',async()=>{const o=load(memoryIDB());await o.enqueue(payload());await assert.rejects(o.enqueue(payload()));const row=(await o.sales.all())[0];row.status='ENVIANDO';await o.sales.put(row);await o.sync(async url=>url.endsWith('contexto/')?context():response(200,{receipt:'/receipt'}));assert.equal((await o.sales.all())[0].status,'CONFIRMADA');});
test('monetary values are handled in integer cents',()=>{const ctx={document:{getElementById:()=>null,querySelector:()=>null},window:{addEventListener(){}},navigator:{onLine:false},PDVOutbox:{},crypto:{},setInterval(){}};vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/js/pdv-offline.js','utf8'),ctx);assert.equal(ctx.PDVOfflineMoney.cents('10,01'),1001);assert.equal(ctx.PDVOfflineMoney.cents('0.10')+ctx.PDVOfflineMoney.cents('0.20'),30);assert.equal(ctx.PDVOfflineMoney.amount(1001),'10.01');assert.throws(()=>ctx.PDVOfflineMoney.cents('10.001'));});

test('form action controls do not break submit; only logout or tenant switch lock identity',async()=>{
  let submit;const locks=[];
  const ctx={navigator:{},window:{isSecureContext:false},location:{pathname:'/pdv/vendas/synthetic/'},addEventListener(){},document:{documentElement:{setAttribute(){}},addEventListener(name,handler){if(name==='submit')submit=handler;}},PDVOutbox:{state:{async put(key,value){locks.push([key,value]);}}}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/js/vertyx.js','utf8'),ctx);
  let prevented=0,resubmitted=0;
  const event=action=>({target:{action:{name:'action',value:'editar'},dataset:{},getAttribute:()=>action,requestSubmit(){resubmitted++;}},preventDefault(){prevented++;}});
  await submit(event(null));assert.equal(prevented,0);assert.equal(locks.length,0);
  for(const path of ['/contas/logout/','/vertyx/organizacao/']){const e=event(path);await submit(e);assert.equal(e.target.dataset.pdvIdentityLocked,'true');await submit(e);}
  assert.equal(prevented,2);assert.equal(resubmitted,2);assert.deepEqual(locks,[['active',null],['active',null]]);
});

test('theme changes preserve the selected preference and notify the existing UI',()=>{
  const attrs=new Map(),prefs=new Map([['vertyx_theme','light']]),events=[];
  const ctx={navigator:{},addEventListener(){},window:{dispatchEvent(e){events.push(e);}},
    localStorage:{getItem:key=>prefs.get(key),setItem:(key,value)=>prefs.set(key,value)},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    document:{documentElement:{setAttribute:(key,value)=>attrs.set(key,value),getAttribute:key=>attrs.get(key)},
      addEventListener(){},querySelectorAll:()=>[]}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/js/vertyx.js','utf8'),ctx);
  assert.equal(attrs.get('data-theme'),'light');
  ctx.window.toggleVertyxTheme();
  assert.equal(attrs.get('data-theme'),'dark');
  assert.equal(prefs.get('vertyx_theme'),'dark');
  assert.equal(events.at(-1).type,'vertyxThemeChanged');
  assert.equal(events.at(-1).detail.theme,'dark');
});
