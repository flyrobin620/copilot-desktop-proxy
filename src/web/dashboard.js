'use strict';
const $ = id => document.getElementById(id);
const fmt = value => Number(value || 0).toLocaleString('en-US');
const short = value => Intl.NumberFormat('en-US', {notation:'compact', maximumFractionDigits:1}).format(value || 0);
const series = [
  ['total_tokens','Total','#26344e'],['input_tokens','Input','#3265ec'],
  ['output_tokens','Output','#28b17b'],['cache_read_input_tokens','Cache read','#19a4c6'],
  ['cache_creation_input_tokens','Cache write','#e9a229']
];
let report = null, page = 1, metric = 'all', generation = 0, controller;
const params = new URLSearchParams(location.search);
if (['day','week','month'].includes(params.get('period'))) $('period').value = params.get('period');
$('endpoint').textContent = location.origin + '/usage';
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; }
function coloredDot(color) { const d=node('span',undefined,'dot'); d.style.setProperty('--series-color',color); return d; }
function makeMetric(label,value,color,note) { const card=node('article',undefined,'metric'); const title=node('div',undefined,'metric-label-row');title.append(coloredDot(color),node('span',label));card.append(title,node('strong',value),node('small',note));return card; }
function renderMetrics() {
  const total=report.totals; const notes={total_tokens:'This proxy · includes cache',input_tokens:'Uncached input',output_tokens:'Generated output',cache_read_input_tokens:'Cached input reused',cache_creation_input_tokens:'Input written to cache'};
  const cards=series.map(([k,l,c])=>makeMetric(l,fmt(total[k]),c,notes[k]));
  cards.push(makeMetric('Requests',fmt(total.requests),'#8962d9','In selected period'));
  const rate=total.requests?((total.requests-total.errors)/total.requests*100).toFixed(1)+'%':'—';
  cards.push(makeMetric('Success rate',rate,'#28b17b',fmt(total.errors)+' incomplete / failed'));
  $('metrics').replaceChildren(...cards);
}
function fillRows(id, rows, empty, columns) { if(!rows.length){const tr=node('tr');const td=node('td',empty,'empty-row');td.colSpan=columns;tr.append(td);rows=[tr];}$(id).replaceChildren(...rows); }
function renderTables() {
  $('model-count').textContent=report.models.length+' model'+(report.models.length===1?'':'s')+' · '+fmt(report.totals.requests)+' requests';
  fillRows('models',report.models.map(m=>{const tr=node('tr');[m.model,fmt(m.requests),...series.slice(1).map(([k])=>fmt(m[k])),fmt(m.total_tokens),fmt(m.errors)].forEach((v,i)=>tr.append(node('td',v,i===6?'total':'')));return tr;}),'No model activity in this period.',8);
  fillRows('events',report.events.map(e=>{const tr=node('tr'); const time=node('td',new Date(e.timestamp).toLocaleString());time.title=e.timestamp; const model=node('td',e.model);model.append(node('span',e.protocol+(e.streaming?' · stream':''),'protocol'));const state=node('td');state.append(node('span',e.outcome==='success'?'Success':e.outcome+' · '+e.status,'outcome'+(e.outcome==='success'?'':' failed')));tr.append(time,model,state,node('td',(e.duration_ms/1000).toFixed(2)+'s'));for(const [k] of [...series.slice(1),series[0]])tr.append(node('td',e.usage_known?fmt(e[k]):'—',k==='total_tokens'?'total':''));return tr;}),'Requests will appear here after you use the proxy.',9);
  $('event-count').textContent='Page '+report.page+' / '+report.pages+' · '+fmt(report.totals.requests)+' events'+(report.totals.unknown_usage?' · '+fmt(report.totals.unknown_usage)+' without reported usage':'');
  $('previous').disabled=report.page<=1;$('next').disabled=report.page>=report.pages;
}
function svgNode(tag,attrs,text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;}
function detail(point){$('chart-detail').textContent=point.bucket.replace('T',' ')+'  ·  '+series.map(([k,l])=>l+': '+fmt(point[k])).join('    ·    ');}
function renderChart(){
  const trend=report.trend;const active=metric==='all'?series:series.filter(([k])=>k===metric);const W=900,H=255,L=66,R=20,T=16,B=37;const iw=W-L-R,ih=H-T-B;
  const max=Math.max(1,...trend.flatMap(p=>active.map(([k])=>p[k])));const top=Math.ceil(max/4)*4;
  const x=i=>L+i*iw/Math.max(1,trend.length-1), y=v=>T+ih-v/top*ih;
  const svg=svgNode('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':'Token usage over time'});
  for(let i=0;i<=4;i++){let yy=T+ih*i/4;svg.append(svgNode('line',{x1:L,y1:yy,x2:W-R,y2:yy,class:'grid-line'}),svgNode('text',{x:L-11,y:yy+4,'text-anchor':'end',class:'axis-label'},short(top*(4-i)/4)));}
  const step=Math.max(1,Math.ceil(trend.length/7));trend.forEach((p,i)=>{if(i%step===0||i===trend.length-1)svg.append(svgNode('text',{x:x(i),y:H-10,'text-anchor':'middle',class:'axis-label'},report.period==='day'?p.bucket.slice(-5):p.bucket.slice(5).replace('-','/')));});
  for(const [key,label,color]of active){svg.append(svgNode('polyline',{points:trend.map((p,i)=>`${x(i)},${y(p[key])}`).join(' '),stroke:color,class:'trend-line'}));}
  trend.forEach((p,i)=>{const peak=Math.max(...active.map(([k])=>p[k]));const circle=svgNode('circle',{cx:x(i),cy:y(peak),r:4,fill:active[0][2],class:'chart-point',tabindex:'0',role:'button','aria-label':p.bucket+', '+fmt(p.total_tokens)+' total tokens'});circle.append(svgNode('title',{},p.bucket+' · '+fmt(p.total_tokens)+' tokens'));circle.addEventListener('pointerenter',()=>detail(p));circle.addEventListener('focus',()=>detail(p));circle.addEventListener('click',()=>detail(p));svg.append(circle);});
  $('chart').replaceChildren(svg);
  if(!report.totals.requests){const empty=node('div',undefined,'chart-empty');empty.append(node('strong','Your next request starts the story.'),node('p','Use Claude Code or the API to begin tracking usage.'));$('chart').append(empty);}
  $('legend').replaceChildren(...active.map(([,l,c])=>{const s=node('span');s.append(coloredDot(c),node('span',l));return s;}));
  $('granularity').textContent=report.period==='day'?'HOURLY':'DAILY';
  detail([...trend].reverse().find(p=>p.requests)||trend[trend.length-1]);
}
function renderControls(){const items=[['all','All','#64748b'],...series];$('metric-controls').replaceChildren(...items.map(([k,l,c])=>{const b=node('button',undefined,k===metric?'active':'');b.setAttribute('aria-pressed',String(k===metric));b.append(coloredDot(c),node('span',l));b.addEventListener('click',()=>{metric=k;renderControls();if(report)renderChart();});return b;}));}
async function refresh(){
  const version=++generation;if(controller)controller.abort();controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),12000);$('refresh').disabled=true;
  const query=new URLSearchParams({period:$('period').value,model:$('model').value,page:String(page),offset:String(new Date().getTimezoneOffset())});
  try{const response=await fetch('/usage?'+query,{cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error('The usage endpoint returned HTTP '+response.status+'.');const data=await response.json();if(version!==generation)return;if(data.source!=='local-proxy')throw new Error('The endpoint did not return local proxy usage.');report=data;page=data.page;
    const selected=$('model').value;const options=[new Option('All models',''),...data.available_models.map(m=>new Option(m,m))];if(selected&&!data.available_models.includes(selected))options.push(new Option(selected,selected));$('model').replaceChildren(...options);$('model').value=selected;
    $('range').textContent=new Date(data.start).toLocaleDateString()+' – '+new Date(data.end).toLocaleString()+' · local time';
    $('updated').textContent='Updated '+new Date().toLocaleTimeString();$('status').className='status';$('status').replaceChildren(node('span'),document.createTextNode('Statistics live'));
    $('error').hidden=data.recording_healthy;$('error').textContent=data.recording_healthy?'':'Some usage records could not be saved. Totals may be incomplete; check disk space and database access.';
    $('coverage').textContent=(data.recording_since?'Recording since '+new Date(data.recording_since).toLocaleString()+'. ':'Recording begins with the first request. ')+'Totals include this proxy’s recorded requests within the selected period. Restarting the proxy keeps this history.';
    $('json-link').href='/usage?'+query;history.replaceState(null,'','?period='+data.period);renderMetrics();renderTables();renderChart();
  }catch(error){if(version!==generation)return;$('error').hidden=false;$('error').textContent='Unable to refresh. Start the proxy and try again. '+(error.name==='AbortError'?'Request timed out.':error.message);$('status').className='status offline';$('status').replaceChildren(node('span'),document.createTextNode('Connection lost'));}finally{clearTimeout(timeout);if(version===generation)$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',refresh);for(const id of ['period','model'])$(id).addEventListener('change',()=>{page=1;refresh();});$('previous').addEventListener('click',()=>{page=Math.max(1,page-1);refresh();});$('next').addEventListener('click',()=>{page++;refresh();});
$('export').addEventListener('click',()=>{if(!report)return;const rows=[['Model','Requests','Input','Output','Cache read','Cache write','Total tokens','Errors','Scope','Proxy endpoint'],...report.models.map(m=>[m.model,m.requests,...series.slice(1).map(([k])=>m[k]),m.total_tokens,m.errors,'This local proxy',location.origin])];const csv=rows.map(row=>row.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\r\n');const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));const link=node('a');link.href=url;link.download='local-proxy-usage-'+report.period+'-'+new Date().toISOString().slice(0,10)+'.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
renderControls();refresh();setInterval(()=>{if($('auto').checked&&!document.hidden&&!$('refresh').disabled)refresh();},30000);
