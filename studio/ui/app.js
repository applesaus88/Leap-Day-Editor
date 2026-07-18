'use strict';
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
const api=()=>window.pywebview.api;
const CELL=16;                       // native world px per cell
// tuning-panel multiplier choices: 0–2.5 by 0.25, plus a few big jumps
const MULT_VALUES=[0,0.25,0.5,0.75,1,1.25,1.5,1.75,2,2.25,2.5,5,10,100];

const CAT_COLOR={solid:'#7d8aa0',platform:'#4a90d9',fruit:'#e7a13a',hazard:'#d9534f',
  enemy_marker:'#b05cd9',autotile:'#3aa98a',decoration:'#5a6072',theme:'#c78a4a','-':'transparent'};

const THEME_BG=['#101018','#241a2e','#102a30','#16241a','#2e1410','#1a1024','#202428'];

const state={
  catalog:{}, chunk:null,
  tool:'paint', layer:'active', selTile:'generic_06', selEnemy:'valentinesBlob',
  view:{scale:2,ox:40,oy:40},
  onion:true, showGrid:true, gameView:true,   // gameView = accurate pivot placement
  layerVis:{bg:true,active:true,fg:true,grid2:true,enemy:true},
  gridScope:true,    // false = place on grid 1 only; true = grids 1 + 2 (enemy grid)
  sel:null, clipboard:null, floating:null, hover:null,
  activePath:null, connStart:null, rot:0, showRot:true,
  undo:[], redo:[],
  drag:null, dragEnemy:null, spaceDown:false,
  libEdit:false,          // true when the open chunk is a custom-library chunk
  themeNames:[],          // Level.Theme roster (from the backend)
  dev:false, devTok:null, devOverrides:{}, artNames:[], devArtPick:false, // dev: hand-edit a sprite's anchor/rot/arrow/art
  firebars:{},            // carrier token -> {length,double,start,clockwise,circular} for canvas drawing
  enemyTuning:{},         // "chunk|sx|sy" -> {projectile,health,walk,h} per-INDIVIDUAL-enemy tuning
  axe:{},                 // global axe-boomerang tunables {range,speed,spin} (blank = baked default)
  projectiles:[],         // projectile roster for the per-enemy panel (from the backend)
  selEnemyCell:null,      // {sx,sy,properties} of the placed enemy whose tuning panel is open
  power:false,            // power mode: unlock advanced/risky tools
  preview:null,           // when set, a read-only full-level preview is open
};
// a tile token may carry a rotation suffix, e.g. "trap117@90"
function splitTok(t){const m=/^(.*?)@(\d+(?:\.\d+)?)$/.exec(t);return m?[m[1],parseInt(m[2])]:[t,0];}
function joinTok(base,ang){return ang?`${base}@${ang}`:base;}
const sprites={};   // token -> {img,w,h} | null
let ctx, cv;

function setStatus(t,err){const s=$('#status');s.textContent=t;s.style.color=err?'#d56':'';}
// 'enemy' is a virtual layer (enemies live in state.chunk.enemies, not a tile grid);
// it has no grid, and stray tile ops on it fall back to the block/active grid.
function layerGrid(L){if(L==='enemy')return null;return L==='active'?state.chunk.grid:state.chunk[L];}
function ensureLayer(L){if(L==='enemy'||L==='active')return state.chunk.grid;
  if(!state.chunk[L]) state.chunk[L]=Array.from({length:state.chunk.h},()=>Array(state.chunk.w).fill('-'));
  return state.chunk[L];}
// tick a Layers radio + highlight its row WITHOUT firing its onchange handler.
function selectLayerRadio(val){const rr=document.querySelector(`input[name=layer][value=${val}]`);
  if(rr){rr.checked=true;document.querySelectorAll('.layerrow').forEach(x=>x.classList.toggle('active',x.contains(rr)));}}
function tileCat(name){const t=(state.catalog.tiles||[]).find(x=>x.name===name);return t?t.category:'solid';}
// human-readable identity of a tile token (from its game script class, baked
// into the catalog by tools/label_tiles.py) — many trapNN tokens share the
// same placeholder art, so the label is how you tell e.g. a Mace from a Cannon.
function tileLabel(name){const b=String(name).split('@')[0];const t=(state.catalog.tiles||[]).find(x=>x.name===b);return t&&t.label?t.label:'';}
// A friendly display name for a palette/roster swatch: prefer the element panel's
// label (so homingcannonUp reads "🚩 flag", homingcannonDown "🟢 respawn", a cannon
// carrier reads "💥 Cannon", …), then a catalog tile label, else the raw token.
function objLabel(name){const b=String(name).split('@')[0];const k=(typeof TOKEN_KIND!=='undefined')&&TOKEN_KIND[b];
  if(k&&ELEM_PANELS[k]&&ELEM_PANELS[k].label) return ELEM_PANELS[k].label;
  return tileLabel(name)||name;}
// element config panels (traps + enemies), loaded from the backend registry
// (core/elements.py). A selected tile that maps to a kind opens its sidebar.
let ELEM_PANELS={};   // kind -> {label, mechanism, fields?, variants?}
let TOKEN_KIND={};    // token -> kind
let HIDDEN_CARRIERS=new Set();   // extra carrier/variant tiles hidden from the palette
async function loadElementPanels(){try{
  const r=await api().get_element_panels();
  ELEM_PANELS={};(r.panels||[]).forEach(p=>ELEM_PANELS[p.kind]=p);
  TOKEN_KIND=r.token_kind||{};
  // show ONE representative tile per configurable kind; hide its other carriers
  // so the palette has a single "cannon" / "firebar" / … you customise in-panel.
  const byKind={};Object.keys(TOKEN_KIND).forEach(t=>{(byKind[TOKEN_KIND[t]]=byKind[TOKEN_KIND[t]]||[]).push(t);});
  HIDDEN_CARRIERS=new Set();
  Object.values(byKind).forEach(toks=>toks.slice(1).forEach(t=>HIDDEN_CARRIERS.add(t)));
}catch(e){}}
function trapKindOf(tok){return TOKEN_KIND[String(tok||'').split('@')[0]]||null;}
// the element panel shows only when a configurable tile is the active selection;
// its title + controls adapt to the kind (mace knobs / field form / variant picker).
function updateFirebarPanel(){
  const el=$('#firebarPanel');if(!el)return;
  // enemy mode: a shooting enemy (woolyTrunky, Cupid, ghost pot…) opens its
  // projectile panel; other enemies have none. paint mode: the selected tile.
  const kind=(state.tool==='enemy')?trapKindOf(state.selEnemy):trapKindOf(state.selTile);
  state.trapKind=kind;el.hidden=!kind;
  if(!kind){state.panelKind=null;return;}
  const p=ELEM_PANELS[kind]||{},mech=p.mechanism;
  $('#fbTitle').textContent=p.label||kind;
  $('#fbMace').hidden=(mech!=='mace');
  $('#fbFields').hidden=(mech!=='fields');
  $('#fbVariants').hidden=(mech!=='variant');
  $('#fbBtn').hidden=(mech==='variant');   // variant places on click, no button
  // only (re)build the dynamic controls when the KIND changes — otherwise placing
  // (which re-runs this) would reset the fields you just set back to defaults.
  const changed=(state.panelKind!==kind);state.panelKind=kind;
  if(mech==='fields'){if(changed)renderElemFields(p);$('#fbBtn').textContent=p.enemy?'Apply projectile':'Use this — paint it';}
  else if(mech==='variant'){if(changed)renderElemVariants(p);}
  else $('#fbBtn').textContent='🔥 Use this — paint it';
}
function renderElemFields(p){
  const box=$('#fbFields');box.innerHTML='';
  (p.fields||[]).forEach(f=>{
    const lab=document.createElement('label');
    let inp;
    if(f.type==='select'){
      inp=document.createElement('select');
      (f.options||[]).forEach(o=>{const opt=document.createElement('option');opt.value=o.value;opt.textContent=o.label;if(o.value===f.default)opt.selected=true;inp.appendChild(opt);});
      lab.appendChild(document.createTextNode(f.label+' '));lab.appendChild(inp);
    }else if(f.type==='bool'){
      inp=document.createElement('input');inp.type='checkbox';inp.checked=!!f.default;
      lab.className='fbchk';lab.appendChild(inp);lab.appendChild(document.createTextNode(' '+f.label));
    }else{
      inp=document.createElement('input');inp.type='number';inp.value=f.default;
      if(f.min!=null)inp.min=f.min;if(f.max!=null)inp.max=f.max;if(f.step!=null)inp.step=f.step;
      lab.appendChild(document.createTextNode(f.label+' '));lab.appendChild(inp);
    }
    inp.dataset.key=f.key;inp.dataset.ftype=f.type;
    box.appendChild(lab);
  });
}
function renderElemVariants(p){
  const box=$('#fbVariants');box.innerHTML='';
  (p.variants||[]).forEach(v=>{
    const btn=document.createElement('button');btn.textContent=v.label;btn.className='accent';btn.style.cssText='margin:2px 4px 2px 0;width:auto';
    btn.onclick=async()=>applyElementResult(await api().place_element(state.trapKind,{token:v.token}));
    box.appendChild(btn);
  });
}
function applyElementResult(r){
  if(!r||r.error){setStatus((r&&r.error)||'element failed',true);return;}
  if(r.render){state.firebars=state.firebars||{};state.firebars[r.token]=r.render;}
  const p=ELEM_PANELS[r.kind]||{};
  if(p.enemy){                         // projectile override on a shooting enemy
    state.selEnemy=r.token;setTool('enemy');
    syncPaletteSel();ensureSprite(r.token);draw();
    $('#fbInfo').textContent=r.token;updateFirebarPanel();
    setStatus(`${p.label||r.kind}: ${r.summary} — paint this enemy (applies to all of them).`);
    return;
  }
  state.selTile=r.token;state.rot=0;setTool('paint');
  updateSelInfo();syncPaletteSel();ensureSprite(r.token);draw();
  $('#fbInfo').textContent=r.token;
  setStatus(`${p.label||r.kind} ready (${r.summary}) — paint cells.${r.reused?' reused':''}`);
}

// ---------- per-individual-enemy tuning panel ----------
// Unlike the element panels (which configure an enemy TYPE for the whole build),
// this targets ONE placed enemy by (chunk, sx, sy). Tuning is CHUNK DATA: the
// panel edits live in memory as you type, and are committed (+ orphans pruned)
// when the chunk is SAVED. It's baked at build time into libnativemod.so, which
// edits just that instance in-game (core/nativemod.py).
function etKey(sx,sy){return `${state.chunk.name}|${sx}|${sy}`;}
function selectEnemyCell(ex){
  state.selEnemyCell=ex?{sx:Math.round(ex.sx),sy:Math.round(ex.sy),properties:ex.properties}:null;
  renderEnemyTune();
}
function renderEnemyTune(){
  const el=$('#enemyTunePanel');if(!el)return;
  const cell=state.selEnemyCell;
  if(!cell||!state.chunk){el.hidden=true;return;}
  el.hidden=false;
  const sel=$('#etProj');                        // (re)build the projectile options once
  if(sel&&sel._built!==(state.projectiles||[]).length){
    sel.innerHTML='<option value="">— leave default —</option>';
    (state.projectiles||[]).forEach(o=>{const opt=document.createElement('option');opt.value=o.value;opt.textContent=o.label;sel.appendChild(opt);});
    sel._built=(state.projectiles||[]).length;
  }
  const rec=(state.enemyTuning||{})[etKey(cell.sx,cell.sy)]||{};
  $('#etProj').value=rec.projectile||'';
  $('#etHealth').value=(rec.health==null?'':rec.health);
  $('#etWalk').value=(rec.walk==null?'':rec.walk);
  // multiplier dropdowns: 0–2.5 by 0.25, plus 5 / 10 / 100 (built once)
  [['#etShootMult'],['#etFireMult']].forEach(([id])=>{const s=$(id);
    if(s&&!s._built){s.innerHTML=MULT_VALUES.map(v=>`<option value="${v}">${v}×${v===1?' (normal)':''}</option>`).join('');s._built=true;}});
  $('#etShootMult').value=(rec.shootmult==null?'1':String(rec.shootmult));
  $('#etFireMult').value=(rec.firemult==null?'1':String(rec.firemult));
  $('#etMuzzleX').value=(rec.muzzle_x==null?'':rec.muzzle_x);
  $('#etMuzzleY').value=(rec.muzzle_y==null?'':rec.muzzle_y);
  // edits apply straight to the in-memory model — no per-enemy save button
  $('#etProj').onchange=$('#etHealth').onchange=$('#etWalk').onchange=$('#etShootMult').onchange=$('#etFireMult').onchange=$('#etMuzzleX').onchange=$('#etMuzzleY').onchange=commitEnemyTuneLocal;
  renderAxeSettings();   // show the global axe-boomerang controls when this enemy throws the axe
  const id=tileLabel(cell.properties);
  $('#etInfo').textContent=`col ${cell.sx}, row ${cell.sy} · ${cell.properties||''}${id?` (${id})`:''}`;
}
// global axe-boomerang tunables (state.axe). The panel only shows when the
// selected enemy's projectile is the axe (its motion is what these control).
function renderAxeSettings(){
  const wrap=$('#axeSettings');if(!wrap)return;
  const isAxe=($('#etProj').value==='axe');
  wrap.hidden=!isAxe;
  if(!isAxe)return;
  const a=state.axe||{};
  $('#axeRange').value=(a.range==null?'':a.range);
  $('#axeSpeed').value=(a.speed==null?'':a.speed);
  $('#axeSpin').value=(a.spin==null?'':a.spin);
  $('#axeHang').value=(a.hang==null?'':a.hang);
  $('#axeRange').oninput=$('#axeSpeed').oninput=$('#axeSpin').oninput=$('#axeHang').oninput=commitAxeSettings;
}
function commitAxeSettings(){
  const a={};
  const r=$('#axeRange').value,s=$('#axeSpeed').value,p=$('#axeSpin').value,h=$('#axeHang').value;
  if(r!=='')a.range=parseFloat(r);
  if(s!=='')a.speed=parseFloat(s);
  if(p!=='')a.spin=parseFloat(p);
  if(h!=='')a.hang=parseFloat(h);
  state.axe=a;
  api().set_axe(a);   // persist to the project (used by the build)
  setStatus('axe boomerang set (global) — Build + playtest to confirm');
}
// update the in-memory tuning for the selected enemy from the panel fields.
// (Persisted to the project when the chunk is SAVED — see ACTIONS.saveLevel.)
function commitEnemyTuneLocal(){
  const cell=state.selEnemyCell;if(!cell||!state.chunk)return;
  const key=etKey(cell.sx,cell.sy);
  const proj=$('#etProj').value,hp=$('#etHealth').value,wk=$('#etWalk').value,sm=$('#etShootMult').value,fm=$('#etFireMult').value;
  const mx=$('#etMuzzleX').value,my=$('#etMuzzleY').value;
  const rec={h:state.chunk.h};
  if(proj)rec.projectile=proj;
  if(hp!=='')rec.health=parseInt(hp);
  if(wk!=='')rec.walk=parseFloat(wk);
  if(sm!==''&&parseFloat(sm)!==1)rec.shootmult=parseFloat(sm);   // 1× = no change
  if(fm!==''&&parseFloat(fm)!==1)rec.firemult=parseFloat(fm);
  if(mx!=='')rec.muzzle_x=parseFloat(mx);   // projectile spawn offset (forward / up)
  if(my!=='')rec.muzzle_y=parseFloat(my);
  state.enemyTuning=state.enemyTuning||{};
  if(Object.keys(rec).length<=1)delete state.enemyTuning[key];   // only h -> nothing tuned
  else state.enemyTuning[key]=rec;
  renderAxeSettings();   // toggle the axe panel when the projectile changes to/from axe
  setStatus('enemy tuned — kept when you Save level → mod');
}
function pruneEnemyTuning(sx,sy){        // enemy deleted -> drop its tuning from memory
  if(state.chunk&&state.enemyTuning)delete state.enemyTuning[etKey(sx,sy)];
}
function moveEnemyTuning(from,to){       // enemy dragged -> its tuning follows to the new cell
  if(!state.enemyTuning)return;
  const rec=state.enemyTuning[etKey(from.sx,from.sy)];
  if(!rec)return;
  delete state.enemyTuning[etKey(from.sx,from.sy)];
  state.enemyTuning[etKey(to.sx,to.sy)]=rec;
}
// gather / sync a chunk's tunings for the save round-trip
function chunkTunings(name){const out={},pre=name+'|';
  Object.keys(state.enemyTuning||{}).forEach(k=>{if(k.startsWith(pre))out[k]=state.enemyTuning[k];});return out;}
function syncChunkTunings(name,et){const pre=name+'|';
  Object.keys(state.enemyTuning||{}).forEach(k=>{if(k.startsWith(pre))delete state.enemyTuning[k];});
  Object.assign(state.enemyTuning=state.enemyTuning||{},et||{});renderEnemyTune();}

// ---------- sprites ----------
async function fetchSprites(tokens){
  const need=[...new Set(tokens)].filter(t=>t&&t!=='-'&&!(t in sprites));
  if(!need.length) return;
  let recs={}; try{recs=await api().get_sprites(need);}catch(e){}
  await Promise.all(need.map(t=>new Promise(res=>{
    const rec=recs[t];
    if(!rec){sprites[t]=null;return res();}
    const img=new Image();
    // keep the draw anchor (pivot px/py + within-cell offset ox/oy) and any
    // manual direction arrow — blit()/drawRotationMarks() read them; dropping
    // them (the old behaviour) is why pivoted/offset tiles drew at the corner.
    img.onload=()=>{sprites[t]={img,w:rec.w,h:rec.h,px:rec.px,py:rec.py,
      ox:rec.ox,oy:rec.oy,rot:rec.rot,arrow:rec.arrow,ov:rec.ov};res();};
    img.onerror=()=>{sprites[t]=null;res();};
    img.src=rec.uri;
  })));
}
// fetch a single token's sprite on demand (if not already cached) then redraw —
// covers placing a tile/enemy before the bulk palette load has reached it.
function ensureSprite(tok){if(tok&&tok!=='-'&&!(tok in sprites))fetchSprites([tok]).then(()=>{draw();if(state.dev)syncDev();});}

// ---------- palette ----------
function renderPalette(el,items,getName,selKey){
  el.innerHTML='';
  items.forEach(it=>{
    const name=getName(it);
    const disp=objLabel(name);const ident=(disp!==name)?name:tileLabel(name);
    const sw=document.createElement('div');sw.className='swatch';sw.dataset.name=name;sw.dataset.label=disp;
    sw.title=(disp!==name)?`${disp} — ${name}`:name;
    const chip=document.createElement('div');chip.className='chip';chip.dataset.token=name;
    const rec=sprites[name];
    if(rec&&rec.img) chip.style.backgroundImage=`url(${rec.img.src})`;
    else chip.style.background=CAT_COLOR[it.category]||'#888';
    sw.appendChild(chip);
    const lbl=document.createElement('div');lbl.textContent=disp;sw.appendChild(lbl);
    if(ident){const id=document.createElement('div');id.className='ident';id.textContent=ident;sw.appendChild(id);}
    if(name===state[selKey]) sw.classList.add('sel');
    sw.onclick=()=>{state[selKey]=name;el.querySelectorAll('.swatch').forEach(x=>x.classList.remove('sel'));sw.classList.add('sel');$('#selName').textContent=name+(ident?` (${ident})`:'');ensureSprite(name);updateFirebarPanel();if(state.dev)syncDev();};
    el.appendChild(sw);
  });
}
function buildPalettes(){
  const tiles=(state.catalog.tiles||[]).filter(t=>!HIDDEN_CARRIERS.has(t.name));
  renderPalette($('#tilePalette'),[{name:'-',category:'-'}].concat(tiles),t=>t.name,'selTile');
  renderPalette($('#enemyPalette'),(state.catalog.enemies||[]).map(e=>({name:e.properties,category:'enemy_marker'})),e=>e.name,'selEnemy');
  updateFirebarPanel();
}
async function refreshPaletteArt(){
  const chips=$$('.chip');
  await fetchSprites(chips.map(c=>c.dataset.token));
  chips.forEach(c=>{const r=sprites[c.dataset.token];if(r&&r.img){c.style.backgroundImage=`url(${r.img.src})`;c.style.backgroundColor='transparent';}});
}
function filterPalette(inp,pal){const q=$(inp).value.toLowerCase();
  $$(pal+' .swatch').forEach(s=>{const hay=(s.dataset.name+' '+(s.dataset.label||'')).toLowerCase();
    s.style.display=hay.includes(q)?'':'none';});}

// ---------- roster / "Browse all" gallery ----------
function buildGallery(kind){
  state.galleryKind=kind;
  $$('.gtab').forEach(b=>b.classList.toggle('active',b.dataset.gtab===kind));
  const grid=$('#galleryGrid'); grid.innerHTML='';
  const selKey=kind==='enemies'?'selEnemy':'selTile';
  const items=kind==='enemies'
    ? (state.catalog.enemies||[]).map(e=>({name:e.properties,category:'enemies'}))
    : [{name:'-',category:'empty'}].concat((state.catalog.tiles||[]).map(t=>({name:t.name,category:t.category})));
  const groups={}; items.forEach(it=>{(groups[it.category]=groups[it.category]||[]).push(it);});
  Object.keys(groups).sort().forEach(cat=>{
    const h=document.createElement('div');h.className='gcat';h.textContent=`${cat} · ${groups[cat].length}`;grid.appendChild(h);
    const roster=document.createElement('div');roster.className='groster';grid.appendChild(roster);
    groups[cat].forEach(it=>{
      const disp=objLabel(it.name);
      const sw=document.createElement('div');sw.className='gswatch';sw.dataset.name=it.name;sw.dataset.label=disp;
      if(it.name===state[selKey])sw.classList.add('sel');
      const chip=document.createElement('div');chip.className='gchip';chip.dataset.token=it.name;
      const r=sprites[it.name]; if(r&&r.img)chip.style.backgroundImage=`url(${r.img.src})`;else chip.style.background=CAT_COLOR[it.category]||'#555';
      sw.appendChild(chip);
      const lbl=document.createElement('div');lbl.textContent=disp;sw.appendChild(lbl);
      sw.onclick=()=>{state[selKey]=it.name;$('#selName').textContent=it.name;ensureSprite(it.name);
        setTool(kind==='enemies'?'enemy':'paint');syncPaletteSel();hideGallery();};
      roster.appendChild(sw);
    });
  });
  fetchSprites(items.map(i=>i.name)).then(()=>{
    grid.querySelectorAll('.gchip').forEach(c=>{const r=sprites[c.dataset.token];if(r&&r.img){c.style.backgroundImage=`url(${r.img.src})`;c.style.backgroundColor='transparent';}});
  });
}
function showGallery(){$('#galleryModal').classList.remove('hidden');buildGallery(state.galleryKind||'tiles');$('#gallerySearch').value='';$('#gallerySearch').focus();}
function hideGallery(){$('#galleryModal').classList.add('hidden');}
function filterGallery(){const q=$('#gallerySearch').value.toLowerCase();
  $$('#galleryGrid .gswatch').forEach(s=>{const hay=(s.dataset.name+' '+(s.dataset.label||'')).toLowerCase();
    s.style.display=hay.includes(q)?'':'none';});
  $$('#galleryGrid .gcat').forEach(h=>{const roster=h.nextElementSibling;
    const any=roster&&[...roster.children].some(c=>c.style.display!=='none');
    h.style.display=any?'':'none';if(roster)roster.style.display=any?'':'none';});
}

// ---------- canvas ----------
function resizeCanvas(){
  const wrap=$('#canvasWrap'),dpr=window.devicePixelRatio||1;
  cv.width=wrap.clientWidth*dpr; cv.height=wrap.clientHeight*dpr;
  cv.style.width=wrap.clientWidth+'px'; cv.style.height=wrap.clientHeight+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  clampView();draw();
}
function s2w(sx,sy){return [(sx-state.view.ox)/state.view.scale,(sy-state.view.oy)/state.view.scale];}
function s2cell(sx,sy){const [wx,wy]=s2w(sx,sy);return [Math.floor(wx/CELL),Math.floor(wy/CELL)];}

// draw a sprite for a cell whose origin (top-left) is (x,y).
// game-accurate: place the sprite by its pivot exactly as the game does — the
// pivot point sits at the cell origin, so wide/tall sprites overflow in the
// authored direction (e.g. a 2-cell decoration fills the empty cell to its
// right, not the tile to its left). Else: contain the sprite to one cell.
function blit(rec,x,y){
  // @angle-token rotation is baked into rec by the resolver; a dev-override `rot`
  // is a live rotation applied here about the sprite's pivot. If a rotated sprite
  // lands in the wrong place, nudge it back with the offset X/Y controls (they
  // translate the whole rotated sprite in screen space).
  const rot=rec.rot||0, rr=rot*Math.PI/180;
  if(state.gameView){
    const px=rec.px??0, py=rec.py??1;
    // prefab local-position offset (cells -> px); resolver already flips y to screen-down
    const ox=(rec.ox??0)*CELL, oy=(rec.oy??0)*CELL;
    const dx=x+ox-px*rec.w, dy=y+oy-(1-py)*rec.h;
    if(rot){const pvx=x+ox, pvy=y+oy;        // rotate about the pivot point (cell origin+offset)
      ctx.save();ctx.translate(pvx,pvy);ctx.rotate(rr);ctx.translate(-pvx,-pvy);
      ctx.drawImage(rec.img,dx,dy,rec.w,rec.h);ctx.restore();}
    else ctx.drawImage(rec.img,dx,dy,rec.w,rec.h);
  }else{
    const sc=Math.min(CELL/rec.w,CELL/rec.h),dw=rec.w*sc,dh=rec.h*sc;
    if(rot){ctx.save();ctx.translate(x+CELL/2,y+CELL/2);ctx.rotate(rr);
      ctx.drawImage(rec.img,-dw/2,-dh/2,dw,dh);ctx.restore();}
    else ctx.drawImage(rec.img,x+(CELL-dw)/2,y+(CELL-dh)/2,dw,dh);
  }
}
// draw an enemy's art for a marker whose cell origin is (x,y). ONE code path so
// a placed enemy, the brush ghost, and a floating paste all render identically.
// An enemy spawns AT its marker; its art is usually bigger than a cell, so it's
// centred on the cell (sits on the purple marker like in-game) — unless a dev
// override hand-anchored it (rec.ov), which uses the pivot placement instead.
function enemyBlit(rec,x,y){
  // An enemy spawns at its cell CENTRE in-game (transform at col*16+8, row*16+8),
  // unlike a tile whose pivot sits at the cell ORIGIN. So a hand-anchored enemy must
  // have its pivot placed at the cell centre, else it renders half a block up-left of
  // its marker (the +CELL/2 on both axes). Non-override enemies are already centred.
  if(rec.ov){blit(rec,x+CELL/2,y+CELL/2);return;}
  ctx.drawImage(rec.img,x+CELL/2-rec.w/2,y+CELL/2-rec.h/2,rec.w,rec.h);
}
function drawLayer(grid,alpha){
  if(!grid) return;
  ctx.globalAlpha=alpha;
  for(let r=0;r<state.chunk.h;r++)for(let c=0;c<state.chunk.w;c++){
    const t=grid[r][c]; if(t==='-') continue;
    if(state.firebars&&state.firebars[splitTok(t)[0]]) continue;  // drawn by drawFirebars (cog + dots)
    const rec=sprites[t];
    const x=c*CELL,y=r*CELL;
    if(rec&&rec.img) blit(rec,x,y);
    else{ctx.fillStyle=CAT_COLOR[tileCat(t)]||'#3a3f4b';ctx.fillRect(x+1,y+1,CELL-2,CELL-2);}
  }
  ctx.globalAlpha=1;
}
function draw(){
  if(!ctx) return;
  const dpr=window.devicePixelRatio||1;
  ctx.clearRect(0,0,cv.width/dpr,cv.height/dpr);
  if(!state.chunk) return;
  const v=state.view;
  ctx.save(); ctx.translate(v.ox,v.oy); ctx.scale(v.scale,v.scale);
  ctx.imageSmoothingEnabled=false;
  // theme background behind the level
  ctx.fillStyle=THEME_BG[(state.chunk.bg_color||0)%THEME_BG.length]||'#101018';
  ctx.fillRect(0,0,state.chunk.w*CELL,state.chunk.h*CELL);
  // layers (z: bg, active, fg); onion fades non-active layers. On the enemy layer,
  // keep the block (active) layer bright so you can see what you're placing enemies on.
  for(const L of ['bg','active','fg']){
    if(!state.layerVis[L]) continue;
    const focus=(L===state.layer)||(L==='active'&&(state.layer==='enemy'||state.layer==='grid2'));
    const a=(focus||!state.onion)?1:0.35;
    drawLayer(layerGrid(L),a);
  }
  // second grid: an ALIGNED copy of grid 1, drawn on top so items placed on it
  // overlap the main tiles at the same cells.
  if(state.layerVis.grid2 && state.chunk.grid2){
    const foc=state.layer==='grid2';
    drawLayer(state.chunk.grid2, (foc||!state.onion)?1:0.35);
  }
  drawAutotileMarks();
  drawObjects();
  drawFirebars();
  drawRotationMarks();
  if(state.showGrid) drawGrid();
  drawSelection();
  drawFloating();
  drawBrushGhost();
  drawStampGhost();
  ctx.restore();
}
// A placed firebar (carrier mace token with a registered config) is drawn as its
// real geometry: ghost balls along the spinning arm (length cells out from the
// pivot, both sides if double, in the start direction) + a spin/swing indicator
// on the pivot. So the editor shows length/position instead of a lone ball.
const FB_DIR={right:[1,0],left:[-1,0],up:[0,-1],down:[0,1]};
let fbDot=null;          // firebar ball sprite Image (tile_fire-2, from backend)
// A firebar renders like the game: a grey cog at the pivot + exactly `length`
// ball sprites forming the arm, in the start direction (both sides if double).
// One placed token = one firebar; a small spin/swing badge marks the motion.
// drawFirebarAt is shared by placed cells AND the brush ghost so they match.
// The game spawns the Mace pivot at the cell's TOP-RIGHT corner (half a cell right
// + up), NOT the centre — so draw the whole firebar shifted by that amount so the
// editor shows it exactly where it ends up in-game. Coordinates are unchanged (the
// token still lives in cell (c,r)); this is display-only.
const FB_OFF_X = CELL/2, FB_OFF_Y = -CELL/2;
function drawFirebarAt(c,r,cfg){
  ctx.save(); ctx.translate(FB_OFF_X, FB_OFF_Y);
  const [dx,dy]=FB_DIR[cfg.start]||FB_DIR.right;
  const arms=cfg.double?[[dx,dy],[-dx,-dy]]:[[dx,dy]];
  for(const [ax,ay] of arms) for(let k=1;k<=cfg.length;k++)
    drawFireDot((c+ax*k)*CELL,(r+ay*k)*CELL);
  drawCog(c*CELL,r*CELL);
  drawArrowMark(c*CELL+CELL*0.76, r*CELL+CELL*0.24,
                cfg.circular?(cfg.clockwise?'cw':'ccw'):'S', CELL*0.24);
  ctx.restore();
}
function drawFirebars(){
  const fb=state.firebars; if(!fb)return;
  const g=state.chunk.grid;
  for(let r=0;r<state.chunk.h;r++)for(let c=0;c<state.chunk.w;c++){
    const tok=g[r][c]; if(tok==='-')continue;
    const cfg=fb[splitTok(tok)[0]]; if(cfg)drawFirebarAt(c,r,cfg);
  }
}
// the firebar ball: the real tile_fire-2 sprite (a red ring; over the dark grid
// its hollow centre reads grey, like the game). Falls back to a drawn red ring.
function drawFireDot(x,y){
  if(fbDot&&fbDot.complete&&fbDot.naturalWidth){
    const s=CELL/Math.max(fbDot.naturalWidth,fbDot.naturalHeight);
    const w=fbDot.naturalWidth*s, h=fbDot.naturalHeight*s;
    ctx.drawImage(fbDot,x+(CELL-w)/2,y+(CELL-h)/2,w,h);
  }else{
    ctx.lineWidth=CELL*0.14;ctx.strokeStyle='#d23a2c';
    _roundRectPath(ctx,x+CELL*0.18,y+CELL*0.18,CELL*0.64,CELL*0.64,CELL*0.2);ctx.stroke();
  }
}
// a grey gear at the pivot (the firebar's rotation centre).
function drawCog(x,y){
  const cx=x+CELL/2, cy=y+CELL/2, R=CELL*0.30;
  ctx.fillStyle='#8b919c';                       // teeth ring
  for(let i=0;i<8;i++){const a=i*Math.PI/4;
    ctx.save();ctx.translate(cx+Math.cos(a)*R,cy+Math.sin(a)*R);ctx.rotate(a);
    ctx.fillRect(-CELL*0.06,-CELL*0.06,CELL*0.12,CELL*0.12);ctx.restore();}
  ctx.fillStyle='#9aa0ab';ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.fill();        // body
  ctx.fillStyle='#3c4049';ctx.beginPath();ctx.arc(cx,cy,CELL*0.12,0,7);ctx.fill();// hub
}
function drawRotationMarks(){
  // arrow showing each tile's orientation (0=up, 90=left, 270=right, 180=down)
  // so 90 vs 270 etc. are distinguishable at a glance. Two sources: a `@<deg>`
  // rotation suffix on the token, OR a hand-authored arrow baked into the sprite
  // (sprite_overrides.json) for directional tiles that share one symmetric sprite.
  if(!state.showRot)return;
  const g=state.chunk.grid;
  for(let r=0;r<state.chunk.h;r++)for(let c=0;c<state.chunk.w;c++){
    const tok=g[r][c]; if(tok==='-')continue;
    let arrow=splitTok(tok)[1];
    if(!arrow){const rc=sprites[tok]; if(rc&&rc.arrow!=null)arrow=rc.arrow; else continue;}
    drawArrowMark(c*CELL+CELL/2, r*CELL+CELL/2, arrow);
  }
}
// Arrow marks render as a BOLD yellow rounded square with a thick BLACK arrow on
// top — readable over any busy art. `arrow` is either degrees (0=up 45=up-left
// 90=left … 315=up-right) → straight arrow, or 'cw'/'ccw' → spin arrow. S is the
// badge half-size in the target context's units (world px on the main canvas).
function drawArrowMark(cx,cy,arrow,S){
  S=S||CELL*0.28;          // small badge by default (matches the firebar icon)
  if(arrow==='cw'||arrow==='ccw') spinArrowG(ctx,cx,cy,arrow==='cw',S);
  else if(arrow==='S'||arrow==='swing') swingMarkG(ctx,cx,cy,S);
  else orientArrowG(ctx,cx,cy,+arrow,S);
}
// a bold black "S" in the yellow badge — marks a swinging (non-rotating) firebar.
function swingMarkG(g,cx,cy,S){
  _arrowBadge(g,cx,cy,S);
  g.fillStyle='#000';g.textAlign='center';g.textBaseline='middle';
  g.font=`bold ${Math.round(S*1.6)}px sans-serif`;
  g.fillText('S',cx,cy+S*0.06);
}
function _roundRectPath(g,x,y,w,h,r){
  g.beginPath();g.moveTo(x+r,y);
  g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);
  g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath();
}
function _arrowBadge(g,cx,cy,S){            // yellow rounded square behind the glyph
  _roundRectPath(g,cx-S,cy-S,S*2,S*2,S*0.26);
  g.fillStyle='#ffd400';g.fill();
  g.lineWidth=S*0.12;g.strokeStyle='rgba(0,0,0,.6)';g.stroke();
}
// straight black arrow inside a yellow badge of half-size S.
function orientArrowG(g,cx,cy,ang,S){
  _arrowBadge(g,cx,cy,S);
  const rad=-ang*Math.PI/180, ux=Math.sin(rad), uy=-Math.cos(rad), px=-uy, py=ux;
  const tipx=cx+ux*S*0.72, tipy=cy+uy*S*0.72;     // head tip
  const basex=cx+ux*S*0.04, basey=cy+uy*S*0.04;   // head base centre
  const tailx=cx-ux*S*0.66, taily=cy-uy*S*0.66;   // stem tail
  g.strokeStyle='#000';g.fillStyle='#000';g.lineCap='round';g.lineJoin='round';
  g.lineWidth=S*0.34;
  g.beginPath();g.moveTo(tailx,taily);g.lineTo(basex,basey);g.stroke();   // thick stem
  const hw=S*0.56;                                 // head half-width
  g.beginPath();g.moveTo(tipx,tipy);
  g.lineTo(basex+px*hw,basey+py*hw);
  g.lineTo(basex-px*hw,basey-py*hw);
  g.closePath();g.fill();                           // big triangular head
}
// black circular spin arrow inside a yellow badge; cw=true clockwise.
function spinArrowG(g,cx,cy,cw,S){
  _arrowBadge(g,cx,cy,S);
  const R=S*0.52, gap=Math.PI*0.55;
  let a0,a1;
  if(cw){a0=-Math.PI/2+gap/2; a1=a0+(2*Math.PI-gap);}      // canvas +angle = CW (y-down)
  else  {a0=-Math.PI/2-gap/2; a1=a0-(2*Math.PI-gap);}
  g.strokeStyle='#000';g.fillStyle='#000';g.lineCap='round';g.lineJoin='round';
  g.lineWidth=S*0.28;
  g.beginPath();g.arc(cx,cy,R,a0,a1,!cw);g.stroke();
  const ex=cx+Math.cos(a1)*R, ey=cy+Math.sin(a1)*R;        // head at arc end
  const tx=cw?-Math.sin(a1):Math.sin(a1), ty=cw?Math.cos(a1):-Math.cos(a1);  // travel dir
  const hL=S*0.52, px=-ty, py=tx;
  g.beginPath();g.moveTo(ex+tx*hL*0.5,ey+ty*hL*0.5);
  g.lineTo(ex-tx*hL*0.5+px*hL*0.62,ey-ty*hL*0.5+py*hL*0.62);
  g.lineTo(ex-tx*hL*0.5-px*hL*0.62,ey-ty*hL*0.5-py*hL*0.62);
  g.closePath();g.fill();
}
function drawAutotileMarks(){
  // autotile edge variants are generated procedurally at runtime (no per-edge
  // sprites to preview), so just flag autotile cells so the designer sees them.
  ctx.fillStyle='#3aa98a';
  for(const L of ['bg','active','fg']){const g=layerGrid(L);if(!g||!state.layerVis[L])continue;
    for(let r=0;r<state.chunk.h;r++)for(let c=0;c<state.chunk.w;c++){
      if(String(g[r][c]).startsWith('Autotile')){ctx.beginPath();
        ctx.moveTo(c*CELL,r*CELL);ctx.lineTo(c*CELL+5,r*CELL);ctx.lineTo(c*CELL,r*CELL+5);ctx.fill();}
    }}
}
function drawFloating(){
  const f=state.floating;if(!f)return;
  for(let dr=0;dr<f.h;dr++)for(let dc=0;dc<f.w;dc++){const t=f.cells[dr][dc];if(t==='-')continue;
    const rec=sprites[t],x=(f.x+dc)*CELL,y=(f.y+dr)*CELL;
    ctx.globalAlpha=0.85;if(rec&&rec.img)blit(rec,x,y);else{ctx.fillStyle=CAT_COLOR[tileCat(t)]||'#3a3f4b';ctx.fillRect(x+1,y+1,CELL-2,CELL-2);}ctx.globalAlpha=1;}
  ctx.strokeStyle='#56c271';ctx.lineWidth=1.5/state.view.scale;
  ctx.strokeRect(f.x*CELL,f.y*CELL,f.w*CELL,f.h*CELL);
}
function drawBrushGhost(){
  if(state.preview||!state.hover||state.drag)return;
  if(state.tool!=='paint'&&state.tool!=='enemy')return;
  const [c,r]=state.hover;if(!inBounds(c,r))return;
  const isEnemy=state.tool==='enemy', tok=isEnemy?state.selEnemy:state.selTile;
  const fcfg=!isEnemy&&state.firebars&&state.firebars[splitTok(tok)[0]];
  ctx.globalAlpha=0.45;const rec=sprites[tok];
  // render through the SAME path the placed version uses, so the ghost preview
  // matches exactly what gets stamped (firebars draw cog+dots, enemies centre, tiles blit).
  if(fcfg)drawFirebarAt(c,r,fcfg);
  else if(tok!=='-'&&rec&&rec.img)(isEnemy?enemyBlit:blit)(rec,c*CELL,r*CELL);
  ctx.globalAlpha=1;ctx.strokeStyle='#ffffff88';ctx.lineWidth=1/state.view.scale;
  ctx.strokeRect(c*CELL+0.5,r*CELL+0.5,CELL-1,CELL-1);
}
function drawObjects(){
  const ctr=(x,y)=>[x*CELL+CELL/2,y*CELL+CELL/2], hs=2.2/state.view.scale;
  // paths (lines + grabbable vertex handles; active path brighter; direction
  // arrows along each segment; a diamond start marker on the first vertex)
  ctx.lineWidth=1.2/state.view.scale;
  const ah=3.6/state.view.scale;        // arrow half-size in world px
  (state.chunk.paths||[]).forEach(p=>{
    if(!p.pts||!p.pts.length)return;
    const active=p===state.activePath;const col=active?'#9be8ff':'#5ad1ff';
    ctx.strokeStyle=col;ctx.beginPath();
    p.pts.forEach((pt,i)=>{const[cx,cy]=ctr(pt[0],pt[1]);i?ctx.lineTo(cx,cy):ctx.moveTo(cx,cy);});
    ctx.stroke();
    // direction arrowhead at each segment midpoint (travel = pts[0] -> pts[n])
    ctx.fillStyle=col;
    for(let i=0;i+1<p.pts.length;i++){
      const[ax,ay]=ctr(p.pts[i][0],p.pts[i][1]),[bx,by]=ctr(p.pts[i+1][0],p.pts[i+1][1]);
      const mx=(ax+bx)/2,my=(ay+by)/2;let dx=bx-ax,dy=by-ay;const L=Math.hypot(dx,dy)||1;dx/=L;dy/=L;
      ctx.beginPath();ctx.moveTo(mx+dx*ah,my+dy*ah);
      ctx.lineTo(mx-dx*ah-dy*ah*0.7,my-dy*ah+dx*ah*0.7);
      ctx.lineTo(mx-dx*ah+dy*ah*0.7,my-dy*ah-dx*ah*0.7);ctx.closePath();ctx.fill();
    }
    // vertex handles; the first vertex (path origin) drawn as a diamond
    p.pts.forEach((pt,i)=>{const[cx,cy]=ctr(pt[0],pt[1]);
      if(i===0){ctx.save();ctx.translate(cx,cy);ctx.rotate(Math.PI/4);
        ctx.fillStyle='#fff';ctx.fillRect(-hs*1.4,-hs*1.4,hs*2.8,hs*2.8);ctx.restore();}
      else{ctx.fillStyle=col;ctx.fillRect(cx-hs,cy-hs,hs*2,hs*2);}});
  });
  // conns (dashed link + endpoint handles)
  (state.chunk.conns||[]).forEach(cn=>{
    ctx.strokeStyle='#ffcf5a';ctx.setLineDash([2,2]);ctx.lineWidth=1.2/state.view.scale;ctx.beginPath();
    const[a,b]=ctr(cn.sx,cn.sy),[m,n]=ctr(cn.mx,cn.my);ctx.moveTo(a,b);ctx.lineTo(m,n);ctx.stroke();ctx.setLineDash([]);
    ctx.fillStyle='#ffcf5a';ctx.fillRect(a-hs,b-hs,hs*2,hs*2);ctx.fillRect(m-hs,n-hs,hs*2,hs*2);
  });
  // pending connection start
  if(state.connStart){const[a,b]=ctr(state.connStart[0],state.connStart[1]);
    ctx.strokeStyle='#ffcf5a';ctx.lineWidth=1.4/state.view.scale;ctx.strokeRect(a-CELL/2,b-CELL/2,CELL,CELL);}
  // enemies (their own layer; hidden when the enemy layer's visibility is off)
  if(state.layerVis.enemy!==false)
  (state.chunk.enemies||[]).forEach(e=>{
    const x=e.sx*CELL,y=e.sy*CELL,rec=sprites[e.properties];
    if(rec&&rec.img){
      enemyBlit(rec,x,y);
      if(state.showRot&&rec.arrow!=null)drawArrowMark(x+CELL/2,y+CELL/2,rec.arrow);
    }
    else {ctx.fillStyle='#ff5a5a55';ctx.fillRect(x+1,y+1,CELL-2,CELL-2);}   // no resolvable art → filled marker
    ctx.strokeStyle=(state.dragEnemy===e)?'#56c271':'#ff5a5a';
    ctx.lineWidth=1.4/state.view.scale;ctx.strokeRect(x+1,y+1,CELL-2,CELL-2);
  });
}
function drawGrid(){
  ctx.strokeStyle='rgba(255,255,255,.07)';ctx.lineWidth=1/state.view.scale;
  ctx.beginPath();
  for(let c=0;c<=state.chunk.w;c++){ctx.moveTo(c*CELL,0);ctx.lineTo(c*CELL,state.chunk.h*CELL);}
  for(let r=0;r<=state.chunk.h;r++){ctx.moveTo(0,r*CELL);ctx.lineTo(state.chunk.w*CELL,r*CELL);}
  ctx.stroke();
  // border
  ctx.strokeStyle='rgba(255,255,255,.25)';ctx.strokeRect(0,0,state.chunk.w*CELL,state.chunk.h*CELL);
}
function drawSelection(){
  if(!state.sel)return;const s=normSel(state.sel);
  ctx.fillStyle='rgba(86,194,113,.18)';ctx.strokeStyle='var(--accent)';
  ctx.fillRect(s.x0*CELL,s.y0*CELL,(s.x1-s.x0+1)*CELL,(s.y1-s.y0+1)*CELL);
  ctx.strokeStyle='#56c271';ctx.lineWidth=1.5/state.view.scale;
  ctx.strokeRect(s.x0*CELL,s.y0*CELL,(s.x1-s.x0+1)*CELL,(s.y1-s.y0+1)*CELL);
}
function normSel(s){return {x0:Math.min(s.x0,s.x1),y0:Math.min(s.y0,s.y1),x1:Math.max(s.x0,s.x1),y1:Math.max(s.y0,s.y1)};}

// ---------- undo ----------
function snapshot(){state.undo.push(JSON.stringify(state.chunk));if(state.undo.length>80)state.undo.shift();state.redo.length=0;}
function undo(){if(!state.undo.length)return;state.redo.push(JSON.stringify(state.chunk));state.chunk=JSON.parse(state.undo.pop());syncMeta();draw();}
function redo(){if(!state.redo.length)return;state.undo.push(JSON.stringify(state.chunk));state.chunk=JSON.parse(state.redo.pop());syncMeta();draw();}

// ---------- editing ops ----------
const inBounds=(c,r)=>state.chunk&&c>=0&&r>=0&&c<state.chunk.w&&r<state.chunk.h;
function setCell(c,r,token){if(inBounds(c,r))ensureLayer(state.layer)[r][c]=token;}
function enemyAt(c,r){return (state.chunk.enemies||[]).find(e=>Math.round(e.sx)===c&&Math.round(e.sy)===r);}
function removeEnemyAt(c,r){pruneEnemyTuning(c,r);state.chunk.enemies=(state.chunk.enemies||[]).filter(e=>!(Math.round(e.sx)===c&&Math.round(e.sy)===r));}

function applyPaint(c,r){
  if(state.tool==='paint'){const tok=joinTok(state.selTile,state.rot);setCell(c,r,tok);ensureSprite(tok);}
  else if(state.tool==='erase'){setCell(c,r,'-');}
}
// rotate the tile under the cursor by ±90° (and remember it as the brush rotation)
function rotateCell(c,r,delta){
  const g=ensureLayer(state.layer);
  if(inBounds(c,r)&&g[r][c]!=='-'){
    const [base,ang]=splitTok(g[r][c]);const na=((ang+delta)%360+360)%360;
    snapshot();const tok=joinTok(base,na);g[r][c]=tok;ensureSprite(tok);state.rot=na;
  }else{state.rot=((state.rot+delta)%360+360)%360;}
  updateSelInfo();draw();
}
function updateSelInfo(){const id=tileLabel(state.selTile);$('#selName').textContent=state.selTile+(id?` (${id})`:'')+(state.rot?` ↻${state.rot}°`:'');updateFirebarPanel();if(state.dev)syncDev();}

// ---------- dev mode: hand-edit a sprite's draw anchor / arrow ----------
// The resolver places most sprites right, but some composite/placeholder tiles
// need a nudged draw anchor (pivot px/py + within-cell offset ox/oy) or a manual
// direction arrow. Dev mode edits the SELECTED tile/enemy and bakes the fix into
// tiles/sprite_overrides.json — persisted and applied to the editor permanently.
// keep the @<angle> on the token so EACH rotation gets its own sprite fix (the
// resolver stores overrides per full token). Enemies have no @angle variants.
function devToken(){return state.tool==='enemy'?state.selEnemy:state.selTile;}
async function setDevMode(on){
  state.dev=on; $('#devPanel').hidden=!on;
  if(on){
    try{state.devOverrides=await api().get_sprite_overrides()||{};}catch(e){}
    if(!state.artNames.length){                     // load art-name list once
      try{const names=await api().list_art_names()||[];
        state.artNames=names;
        // the autocomplete datalist is just a convenience — cap it so a 14k-option
        // list doesn't lag the panel; the ▦ Browse picker covers the full roster.
        $('#devArtList').innerHTML=names.slice(0,1500).map(n=>`<option value="${n}">`).join('');}catch(e){}
    }
    state.devTok=devToken();ensureSprite(state.devTok);syncDev();
  }else if(state.devArtPick){devArtPick(false);}
  draw();
}

// ---------- dev: bake baseline shoot speeds, PER ENEMY ----------
// Pick an enemy → a placeholder row for every projectile it could spawn; fill the
// ones you want. Global defaults (all projects) a placement's "shoot speed ×" scales.
function setDevShoot(on){
  state.devShoot=on; $('#devShootPanel').hidden=!on;
  if(on){
    const sel=$('#devShootEnemy');
    if(sel&&!sel._built){
      sel.innerHTML=(state.shootEnemies||[]).map(e=>`<option value="${e.cls}">${e.label}</option>`).join('');
      sel._built=true; sel.onchange=renderShootBakes;
    }
    renderShootBakes();
  }
}
// one placeholder row per projectile this enemy could spawn; prefill saved bakes.
function renderShootBakes(){
  const host=$('#devShootRows'); if(!host)return;
  const cls=$('#devShootEnemy')?$('#devShootEnemy').value:''; const bakes=state.shootBakes||{};
  host.innerHTML='';
  (state.projectiles||[]).forEach(o=>{
    const cur=bakes[`${cls}|${o.value}`];
    const row=document.createElement('div'); row.className='devrow dsRow'; row.dataset.proj=o.value;
    row.innerHTML=`<label style="flex:1">${o.label}</label>`+
      `<input class="devnum dsSpd" type="number" min="0" step="5" placeholder="default" value="${cur==null?'':cur}">`;
    host.appendChild(row);
  });
}
// merge the current enemy's rows into the full bake table (keeping other enemies').
function collectShootBakes(){
  const cls=$('#devShootEnemy')?$('#devShootEnemy').value:''; const out={};
  Object.keys(state.shootBakes||{}).forEach(k=>{ if(!k.startsWith(cls+'|'))out[k]=state.shootBakes[k]; });
  $$('#devShootRows .dsRow').forEach(row=>{
    const v=row.querySelector('.dsSpd').value;
    if(v!=='')out[`${cls}|${row.dataset.proj}`]=parseFloat(v);
  });
  return out;
}
// pull the selected token's current anchor/arrow into the dev inputs
function syncDev(){
  if(!state.dev)return;
  const tok=devToken(); state.devTok=tok;
  $('#devTok').textContent=tok||'(none)';
  const rec=sprites[tok];
  const px=(rec&&rec.px!=null)?rec.px:0, py=(rec&&rec.py!=null)?rec.py:1;
  const ox=(rec&&rec.ox!=null)?rec.ox:0, oy=(rec&&rec.oy!=null)?rec.oy:0;
  $('#devPx').value=$('#devPxN').value=px;
  $('#devPy').value=$('#devPyN').value=py;
  $('#devOx').value=ox; $('#devOy').value=oy;
  $('#devArt').value=((state.devOverrides[tok]||{}).art)||'';
  drawDevPreview();
}
// read the dev inputs into an override-shaped object. arrow is degrees OR a
// 'cw'/'ccw' spin token; art (if set) redirects to a different sprite source.
function devVals(){
  const num=(id,d)=>{const v=parseFloat($(id).value);return isNaN(v)?d:v;};
  const art=$('#devArt').value.trim();
  return {px:num('#devPx',0),py:num('#devPy',1),ox:num('#devOx',0),oy:num('#devOy',0),
          rot:0,                    // rotation removed — each orientation is its own block/token
          arrow:null,               // arrows removed too
          art:art||null};
}
// live-apply inputs to the in-memory sprite so the main canvas updates as you
// drag (not yet persisted — Save commits, Reset reverts to automatic). Art is a
// different IMAGE, so it round-trips through the backend (devPreviewArt).
function devLive(src){
  if(!state.dev||!state.devTok)return;
  const pair={px:'devPxN',pxN:'devPx',py:'devPyN',pyN:'devPy'}[src];
  if(pair)$('#'+pair).value=$('#dev'+src.charAt(0).toUpperCase()+src.slice(1)).value;
  const rec=sprites[state.devTok]; if(rec){const v=devVals();
    rec.px=v.px;rec.py=v.py;rec.ox=v.ox;rec.oy=v.oy;rec.rot=v.rot;rec.arrow=v.arrow;rec.ov=true;}
  draw();drawDevPreview();
}
// live-preview a different art SOURCE (its image differs, so resolve on the
// backend without persisting), keeping the current anchor/rot/arrow inputs.
async function devPreviewArt(){
  if(!state.dev||!state.devTok)return;
  const tok=state.devTok, r=await api().preview_sprite_override(tok,devVals());
  if(r&&r.rec)loadRecInto(tok,r.rec,()=>{draw();drawDevPreview();});
  else if(r&&r.rec===null){setStatus('that art name didn’t resolve',true);}
}
// ---- visual sprite picker (big thumbnail roster) ----
// Shows EVERY sprite (no cap). Thumbnails load lazily as they scroll into view
// (IntersectionObserver), so even a few-thousand-sprite roster stays responsive.
let _artTimer=null, _artIO=null;
function openArtPicker(){
  if(!state.dev)return;
  if(!state.artNames.length){setStatus('load your .xapk first to browse sprites',true);return;}
  state.devTok=devToken();
  $('#artModalTok').textContent=state.devTok||'(none)';
  $('#artModal').classList.remove('hidden');
  $('#artSearch').value='';                               // default: show the whole roster
  renderArtGrid(); $('#artSearch').focus();
}
function closeArtPicker(){$('#artModal').classList.add('hidden');if(_artIO){_artIO.disconnect();_artIO=null;}}
let _artJob=0;
function renderArtGrid(){
  const q=$('#artSearch').value.trim().toLowerCase();
  const matches=q?state.artNames.filter(n=>n.toLowerCase().includes(q)):state.artNames;
  const cur=$('#devArt').value.trim();
  $('#artCount').textContent=`${matches.length} sprite${matches.length===1?'':'s'}`;
  const grid=$('#artGrid'); grid.innerHTML='';
  if(_artIO)_artIO.disconnect();
  // lazy-load thumbnails as their swatch scrolls within ~1 screen of the viewport
  _artIO=new IntersectionObserver(ents=>{
    const hit=ents.filter(e=>e.isIntersecting).map(e=>e.target);
    if(!hit.length)return;
    hit.forEach(t=>_artIO.unobserve(t));
    fetchSprites(hit.map(t=>t._name)).then(()=>hit.forEach(t=>{
      const r=sprites[t._name];
      if(r&&r.img){t._chip.style.backgroundImage=`url(${r.img.src})`;t._chip.style.backgroundColor='transparent';}}));
  },{root:grid,rootMargin:'400px'});
  // Build the (up to ~14k) swatches in batches across frames so a big roster
  // never freezes the view — a single synchronous build is what made it look
  // empty/broken. A job token cancels an in-flight build when the search changes.
  const job=++_artJob; let i=0;
  (function chunk(){
    if(job!==_artJob)return;
    const frag=document.createDocumentFragment(); const batch=[];
    for(const end=Math.min(i+250,matches.length); i<end; i++){
      const name=matches[i];
      const sw=document.createElement('div');sw.className='gswatch';sw.dataset.name=name;sw._name=name;
      if(name===cur)sw.classList.add('sel');
      const chip=document.createElement('div');chip.className='gchip';sw._chip=chip;
      const r=sprites[name]; if(r&&r.img){chip.style.backgroundImage=`url(${r.img.src})`;}else chip.style.background='#2a2f3a';
      sw.appendChild(chip);
      const lbl=document.createElement('div');lbl.textContent=name;sw.appendChild(lbl);
      sw.onclick=()=>chooseArt(name);
      frag.appendChild(sw); batch.push(sw);
    }
    grid.appendChild(frag); batch.forEach(sw=>_artIO.observe(sw));
    if(i<matches.length) requestAnimationFrame(chunk);
  })();
}
function chooseArt(name){$('#devArt').value=name;closeArtPicker();devPreviewArt();
  setStatus('previewing art "'+name+'" — Save fix to bake it');}
// arm/disarm the canvas art-eyedropper (next canvas click lifts that token's art)
function devArtPick(on){
  state.devArtPick=(on===undefined)?!state.devArtPick:on;
  const b=$('#devArtPick'); if(b)b.classList.toggle('active',state.devArtPick);
  if(cv)cv.style.cursor=state.devArtPick?'copy':(state.tool==='pan'?'grab':'crosshair');
  if(state.devArtPick)setStatus('art eyedropper: click a tile/enemy on the canvas to lift its art');
}
// (re)load a backend record's image into sprites[tok] (art may have changed the
// actual pixels, so we rebuild the Image rather than keep the old one).
function loadRecInto(tok,rec,then){
  if(!rec){sprites[tok]=null;then&&then();return;}
  const img=new Image();
  const set=()=>{sprites[tok]={img,w:rec.w,h:rec.h,px:rec.px,py:rec.py,ox:rec.ox,oy:rec.oy,
    rot:rec.rot,arrow:rec.arrow,ov:rec.ov};then&&then();};
  img.onload=set; img.onerror=()=>{sprites[tok]=null;then&&then();}; img.src=rec.uri;
}
// preview: the sprite placed in a 3×3 cell reference, with the cell-origin
// crosshair (where the pivot lands), its draw-box, and the arrow/spin marker.
function drawDevPreview(){
  const cvp=$('#devPrev');if(!cvp)return;const p=cvp.getContext('2d');
  const W=cvp.width,H=cvp.height;p.clearRect(0,0,W,H);p.imageSmoothingEnabled=false;
  const Z=Math.floor(Math.min(W,H)/3), x0=Z, y0=Z;   // origin cell = middle of 3×3
  p.strokeStyle='rgba(255,255,255,.10)';p.lineWidth=1;
  for(let i=0;i<=3;i++){p.beginPath();p.moveTo(i*Z+.5,0);p.lineTo(i*Z+.5,3*Z);p.stroke();
    p.beginPath();p.moveTo(0,i*Z+.5);p.lineTo(3*Z,i*Z+.5);p.stroke();}
  p.strokeStyle='rgba(86,194,113,.5)';p.strokeRect(x0+.5,y0+.5,Z,Z);     // placed cell
  const rec=state.devTok&&sprites[state.devTok];
  if(rec&&rec.img){
    const v=devVals(),z=Z/CELL;
    const dx=x0+(v.ox||0)*Z-(v.px||0)*rec.w*z;
    const dy=y0+(v.oy||0)*Z-(1-(v.py==null?1:v.py))*rec.h*z;
    const pvx=x0+(v.ox||0)*Z, pvy=y0+(v.oy||0)*Z, rot=v.rot||0;
    p.save();
    if(rot){p.translate(pvx,pvy);p.rotate(rot*Math.PI/180);p.translate(-pvx,-pvy);}
    p.drawImage(rec.img,dx,dy,rec.w*z,rec.h*z);
    p.strokeStyle='rgba(255,210,74,.55)';p.strokeRect(dx+.5,dy+.5,rec.w*z,rec.h*z);  // draw-box
    p.restore();
    if(v.arrow!=null)(v.arrow==='cw'||v.arrow==='ccw'
      ? spinArrowG(p,x0+Z/2,y0+Z/2,v.arrow==='cw',Z*0.45)
      : orientArrowG(p,x0+Z/2,y0+Z/2,+v.arrow,Z*0.45));
  }
  p.strokeStyle='#ff5a5a';p.lineWidth=1;                 // cell-origin crosshair (pivot target)
  p.beginPath();p.moveTo(x0-4,y0);p.lineTo(x0+4,y0);p.moveTo(x0,y0-4);p.lineTo(x0,y0+4);p.stroke();
}
function fillRect(sel,token){const s=normSel(sel);const g=ensureLayer(state.layer);
  for(let r=s.y0;r<=s.y1;r++)for(let c=s.x0;c<=s.x1;c++)if(inBounds(c,r))g[r][c]=token;}
function copyRegion(){if(!state.sel)return;const s=normSel(state.sel),g=layerGrid(state.layer)||[];
  const cells=[];for(let r=s.y0;r<=s.y1;r++){const row=[];for(let c=s.x0;c<=s.x1;c++)row.push((g[r]&&g[r][c])||'-');cells.push(row);}
  state.clipboard={w:s.x1-s.x0+1,h:s.y1-s.y0+1,cells};enterStamp();}
function pasteAt(c,r){if(!state.clipboard)return;snapshot();const g=ensureLayer(state.layer);
  for(let dr=0;dr<state.clipboard.h;dr++)for(let dc=0;dc<state.clipboard.w;dc++){const t=state.clipboard.cells[dr][dc];if(t!=='-'&&inBounds(c+dc,r+dr)){g[r+dr][c+dc]=t;ensureSprite(t);}}draw();}
// After a copy the brush BECOMES the copied region: a ghost of the clipboard
// follows the cursor (so you see where it lands) and each click stamps it, until
// Esc / right-click / picking another tool. Clipboard survives, so you can even
// stamp across chunks.
function enterStamp(){
  if(!state.clipboard)return;
  if(state.tool!=='stamp')state.stampPrev=state.tool;
  state.tool='stamp'; state.sel=null;                 // the selection did its job
  state.clipboard.cells.forEach(row=>row.forEach(ensureSprite));   // art ready for the ghost (incl. cross-chunk)
  $$('.tool').forEach(b=>b.classList.remove('active'));
  if(cv)cv.style.cursor='crosshair';
  setStatus(`stamp ${state.clipboard.w}×${state.clipboard.h} — click to paste · Esc / right-click to stop`);
  draw();
}
function exitStamp(){ if(state.tool==='stamp') setTool(state.stampPrev||'paint'); }
function drawStampGhost(){
  if(state.preview||state.tool!=='stamp'||!state.clipboard||!state.hover||state.drag)return;
  const cb=state.clipboard,[hc,hr]=state.hover;
  for(let dr=0;dr<cb.h;dr++)for(let dc=0;dc<cb.w;dc++){
    const t=cb.cells[dr][dc];if(t==='-'||!inBounds(hc+dc,hr+dr))continue;
    const rec=sprites[t],x=(hc+dc)*CELL,y=(hr+dr)*CELL;ctx.globalAlpha=0.5;
    if(rec&&rec.img)blit(rec,x,y);else{ctx.fillStyle=CAT_COLOR[tileCat(t)]||'#3a3f4b';ctx.fillRect(x+1,y+1,CELL-2,CELL-2);}
    ctx.globalAlpha=1;
  }
  ctx.strokeStyle='#ffcc33';ctx.lineWidth=1.5/state.view.scale;   // amber = "floating paste"
  ctx.strokeRect(hc*CELL+.5,hr*CELL+.5,cb.w*CELL-1,cb.h*CELL-1);
}

// ---------- path / connection tools ----------
function findPathVertex(c,r){
  for(const p of (state.chunk.paths||[])) for(let i=0;i<p.pts.length;i++)
    if(p.pts[i][0]===c&&p.pts[i][1]===r) return {path:p,idx:i};
  return null;
}
// total path length in cells (for the HUD readout)
function pathLen(p){let L=0;for(let i=0;i+1<p.pts.length;i++)L+=Math.hypot(p.pts[i+1][0]-p.pts[i][0],p.pts[i+1][1]-p.pts[i][1]);return L;}
function pathHud(p){$('#hudCell').textContent=`path · ${p.pts.length} pts · len ${pathLen(p).toFixed(1)}`;}
// snap (c,r) to a horizontal/vertical line through (px,py) — Shift axis-lock
function axisLock(c,r,px,py){return Math.abs(c-px)>=Math.abs(r-py)?[c,py]:[px,r];}
// nearest segment of any path within `tol` cells of (c,r); returns insert info
function findPathSegment(c,r,tol){
  let best=null;
  for(const p of (state.chunk.paths||[])){
    if(p===state.activePath)continue;
    for(let i=0;i+1<p.pts.length;i++){
      const[ax,ay]=p.pts[i],[bx,by]=p.pts[i+1];const dx=bx-ax,dy=by-ay;const len2=dx*dx+dy*dy||1;
      let t=((c-ax)*dx+(r-ay)*dy)/len2;t=Math.max(0,Math.min(1,t));
      const qx=ax+t*dx,qy=ay+t*dy;const d=Math.hypot(c-qx,r-qy);
      if(d<=tol&&(!best||d<best.d))best={path:p,idx:i+1,d};
    }
  }
  return best;
}
function pathDown(c,r,e){
  const hit=findPathVertex(c,r);
  if(hit){                                   // grab a vertex; Alt = move whole path
    snapshot();
    state.drag=e&&e.altKey?{pathmove:true,path:hit.path,sx:c,sy:r,base:hit.path.pts.map(pt=>[pt[0],pt[1]])}
                          :{pathv:true,path:hit.path,idx:hit.idx};
    return;
  }
  if(state.activePath){                       // extending the open path
    const last=state.activePath.pts[state.activePath.pts.length-1];
    const[nc,nr]=(e&&e.shiftKey&&last)?axisLock(c,r,last[0],last[1]):[c,r];
    snapshot();state.activePath.pts.push([nc,nr]);pathHud(state.activePath);draw();return;
  }
  const seg=findPathSegment(c,r,0.6);         // click near a segment = insert a vertex there
  if(seg){snapshot();seg.path.pts.splice(seg.idx,0,[c,r]);
    state.drag={pathv:true,path:seg.path,idx:seg.idx};pathHud(seg.path);draw();return;}
  snapshot();                                  // otherwise start a new path
  const p={x:c,y:r,pts:[[c,r]]};(state.chunk.paths=state.chunk.paths||[]).push(p);state.activePath=p;
  setStatus('path: click to add points · click a segment to insert · Shift=straight · Alt-drag=move whole · Enter/double-click to finish');
  draw();
}
function finishPath(){if(!state.activePath)return;
  if(state.activePath.pts.length<2)state.chunk.paths=state.chunk.paths.filter(p=>p!==state.activePath);
  state.activePath=null;setStatus('path finished');draw();}
function connDown(c,r){snapshot();
  if(state.connStart){(state.chunk.conns=state.chunk.conns||[]).push({sx:state.connStart[0],sy:state.connStart[1],mx:c,my:r});state.connStart=null;setStatus('connection added');}
  else{state.connStart=[c,r];setStatus('connection: click the target cell');}
  draw();}
function deletePathElement(c,r){
  const hit=findPathVertex(c,r);
  if(hit){snapshot();hit.path.pts.splice(hit.idx,1);
    if(hit.path.pts.length<2)state.chunk.paths=state.chunk.paths.filter(p=>p!==hit.path);draw();return;}
  const conns=state.chunk.conns||[];const ci=conns.findIndex(cn=>(cn.sx===c&&cn.sy===r)||(cn.mx===c&&cn.my===r));
  if(ci>=0){snapshot();conns.splice(ci,1);draw();}
}

// ---------- pointer interaction ----------
function attachCanvas(){
  cv.addEventListener('mousedown',onDown);
  window.addEventListener('mousemove',onMove);
  window.addEventListener('mouseup',onUp);
  cv.addEventListener('wheel',onWheel,{passive:false});
  cv.addEventListener('dblclick',()=>finishPath());
  cv.addEventListener('contextmenu',e=>e.preventDefault());
}
function onWheel(e){e.preventDefault();const v=state.view;const f=e.deltaY<0?1.15:1/1.15;
  const mx=e.offsetX,my=e.offsetY;const[wx,wy]=s2w(mx,my);
  v.scale=Math.max(0.5,Math.min(16,v.scale*f));
  v.ox=mx-wx*v.scale;v.oy=my-wy*v.scale;clampView();hud();draw();}
function startEnemyDrag(ex){state.drag={enemy:ex};state.dragEnemy=ex;
  state.dragEnemyFrom={sx:Math.round(ex.sx),sy:Math.round(ex.sy)};snapshot();}
function deleteAt(c,r){              // right-click delete
  removeEnemyAt(c,r);
  // on the enemy layer, delete only the enemy — leave the block underneath intact.
  // on the enemy layer, delete only the enemy — leave the block underneath.
  if(state.layer!=='enemy' && inBounds(c,r)) ensureLayer(state.layer)[r][c]='-';
}
function inSel(c,r){if(!state.sel)return false;const s=normSel(state.sel);return c>=s.x0&&c<=s.x1&&r>=s.y0&&r<=s.y1;}
function liftSelection(c,r){         // cut the selected region into a floating buffer
  snapshot();const s=normSel(state.sel),g=ensureLayer(state.layer);
  const cells=[];for(let rr=s.y0;rr<=s.y1;rr++){const row=[];for(let cc=s.x0;cc<=s.x1;cc++){row.push((g[rr]&&g[rr][cc])||'-');if(inBounds(cc,rr))g[rr][cc]='-';}cells.push(row);}
  state.floating={w:s.x1-s.x0+1,h:s.y1-s.y0+1,cells,x:s.x0,y:s.y0,offx:c-s.x0,offy:r-s.y0};
  state.drag={moving:true};draw();
}
function stampFloating(){const f=state.floating,g=ensureLayer(state.layer);
  for(let dr=0;dr<f.h;dr++)for(let dc=0;dc<f.w;dc++){const t=f.cells[dr][dc];if(t!=='-'&&inBounds(f.x+dc,f.y+dr))g[f.y+dr][f.x+dc]=t;}
  state.sel={x0:f.x,y0:f.y,x1:f.x+f.w-1,y1:f.y+f.h-1};state.floating=null;}
function onDown(e){
  if(!state.chunk)return;
  const pan=(state.tool==='pan'||state.spaceDown||e.button===1);
  // whole-level preview is READ-ONLY: only pan/zoom, no editing.
  if(state.preview){
    if(pan)state.drag={pan:true,sx:e.offsetX,sy:e.offsetY,ox:state.view.ox,oy:state.view.oy};
    return;
  }
  const [c,r]=s2cell(e.offsetX,e.offsetY);
  // dev: art eyedropper — grab the token under the cursor (enemy first, then the
  // active-layer tile) as the dev-panel art source, so you can lift the exact art
  // you can already see instead of hunting for its name.
  if(state.dev&&state.devArtPick&&e.button===0){
    const en=enemyAt(c,r), g=layerGrid(state.layer);
    const tok=en?en.properties:(g&&inBounds(c,r)&&g[r][c]!=='-'?splitTok(g[r][c])[0]:null);
    devArtPick(false);
    if(tok){$('#devArt').value=tok;devPreviewArt();setStatus('picked art from "'+tok+'" — Save fix to bake it');}
    else setStatus('no tile/enemy there to pick');
    return;
  }
  // stamp mode (entered by Copy/Paste): left-click pastes the clipboard here,
  // right-click stops stamping. Handled before delete so right-click cancels.
  if(state.tool==='stamp'&&!pan){
    if(e.button===2||!state.clipboard){exitStamp();return;}
    if(e.button===0){pasteAt(c,r);return;}   // stays in stamp mode → paste again
  }
  // right-click ALWAYS deletes (enemy + tile), with drag-to-delete
  if(e.button===2){
    if(state.tool==='path'||state.tool==='conn'){deletePathElement(c,r);return;}
    snapshot();deleteAt(c,r);state.drag={rmdrag:true};draw();return;
  }
  if(pan){state.drag={pan:true,sx:e.offsetX,sy:e.offsetY,ox:state.view.ox,oy:state.view.oy};return;}
  if(state.tool==='eyedrop'){const g=layerGrid(state.layer);if(g&&inBounds(c,r)&&g[r][c]!=='-'){state.selTile=g[r][c];updateSelInfo();syncPaletteSel();}return;}
  if(state.tool==='path'){pathDown(c,r,e);return;}
  if(state.tool==='conn'){connDown(c,r);return;}
  if(state.tool==='select'){
    const ex=enemyAt(c,r);              // click an enemy = grab & move it (+ open its tuning)
    if(ex){selectEnemyCell(ex);startEnemyDrag(ex);return;}
    if(inSel(c,r)){liftSelection(c,r);return;}   // click inside selection = drag-move it
    selectEnemyCell(null);
    state.sel={x0:c,y0:r,x1:c,y1:r};state.drag={selecting:true};draw();return;
  }
  if(state.tool==='enemy'){
    const ex=enemyAt(c,r);
    if(ex){ selectEnemyCell(ex);
            if(e.shiftKey){snapshot();ex.properties=state.selEnemy;ensureSprite(state.selEnemy);draw();}  // shift-click = retype
            else startEnemyDrag(ex); }                                       // click = move + open tuning
    else{snapshot();const ne={sx:c,sy:r,properties:state.selEnemy};state.chunk.enemies.push(ne);
          ensureSprite(state.selEnemy);selectEnemyCell(ne);draw();}  // enemy layer only — the spawn marker is added into `active` at build (to_xml), so the block underneath is preserved
    return;
  }
  if(state.tool==='rect'){state.sel={x0:c,y0:r,x1:c,y1:r};state.drag={rect:true};draw();return;}
  snapshot();state.drag={paint:true};applyPaint(c,r);draw();
}
function onMove(e){
  if(!state.chunk)return;
  const rect=cv.getBoundingClientRect();const ox=e.clientX-rect.left,oy=e.clientY-rect.top;
  const [c,r]=s2cell(ox,oy); hud(c,r); state.hover=[c,r];
  const d=state.drag;
  if(!d){if(state.tool==='paint'||state.tool==='enemy'||state.tool==='stamp')draw();return;}   // brush/stamp ghost follows cursor
  if(d.pan){state.view.ox=d.ox+(ox-d.sx);state.view.oy=d.oy+(oy-d.sy);clampView();draw();return;}
  if(d.rmdrag){deleteAt(c,r);draw();return;}
  if(d.paint){applyPaint(c,r);draw();return;}
  if(d.selecting||d.rect){state.sel.x1=c;state.sel.y1=r;draw();return;}
  if(d.moving){const f=state.floating;f.x=c-f.offx;f.y=r-f.offy;draw();return;}
  if(d.pathv){let nc=c,nr=r;
    if(e.shiftKey){const ref=d.path.pts[d.idx-1>=0?d.idx-1:d.idx+1];if(ref)[nc,nr]=axisLock(c,r,ref[0],ref[1]);}
    d.path.pts[d.idx]=[nc,nr];if(d.idx===0){d.path.x=nc;d.path.y=nr;}pathHud(d.path);draw();return;}
  if(d.pathmove){const dx=c-d.sx,dy=r-d.sy;
    d.path.pts=d.base.map(([x,y])=>[x+dx,y+dy]);d.path.x=d.path.pts[0][0];d.path.y=d.path.pts[0][1];pathHud(d.path);draw();return;}
  if(d.enemy){if(inBounds(c,r)){d.enemy.sx=c;d.enemy.sy=r;}draw();return;}
}
function onUp(){
  const d=state.drag;state.drag=null;state.dragEnemy=null;if(!d)return;
  if(d.rect){snapshot();fillRect(state.sel,state.selTile);state.sel=null;draw();}
  if(d.moving){stampFloating();draw();}
  if(d.enemy){                          // an enemy was dragged — follow its tuning to the new cell
    const from=state.dragEnemyFrom,to={sx:Math.round(d.enemy.sx),sy:Math.round(d.enemy.sy)};
    if(from&&(from.sx!==to.sx||from.sy!==to.sy))moveEnemyTuning(from,to);
    if(state.selEnemyCell)selectEnemyCell(d.enemy);
  }
  state.dragEnemyFrom=null;
  if(d.enemy||d.pathv||d.pathmove)draw();
}

function hud(c,r){
  if(c!=null){let lbl=`x ${c}, y ${r}`;
    if(state.chunk){const g=layerGrid(state.layer);const t=(g&&inBounds(c,r))?g[r][c]:'-';
      if(t&&t!=='-'){lbl+=`  [${t}]`;const id=tileLabel(t);if(id)lbl+=` · ${id}`;}}
    $('#hudCell').textContent=lbl;}
  $('#hudZoom').textContent=`${Math.round(state.view.scale*100)}%`;
  $('#hudLayer').textContent=state.layer;
}
function fitView(){
  if(!state.chunk)return;const wrap=$('#canvasWrap');
  const sx=wrap.clientWidth/(state.chunk.w*CELL+8),sy=wrap.clientHeight/(state.chunk.h*CELL+8);
  state.view.scale=Math.max(0.5,Math.min(16,Math.min(sx,sy)));
  state.view.ox=(wrap.clientWidth-state.chunk.w*CELL*state.view.scale)/2;
  state.view.oy=(wrap.clientHeight-state.chunk.h*CELL*state.view.scale)/2;
  clampView();hud();draw();
}
// keep the chunk inside the viewport so an edge (especially the BOTTOM) is never
// scrolled away with no way back: if an axis fits, hold the whole chunk visible
// (with a small margin); if it overflows, allow panning but stop at each edge so
// both top and bottom stay reachable. Called after every pan / zoom / resize.
function clampView(){
  if(!state.chunk)return;
  const wrap=$('#canvasWrap'),M=8;
  const fit=(o,content,view)=> content<=view-2*M
    ? Math.min(Math.max(o,M),view-content-M)      // fits: keep fully on-screen
    : Math.min(Math.max(o,view-content-M),M);     // overflows: clamp to either edge
  state.view.ox=fit(state.view.ox,state.chunk.w*CELL*state.view.scale,wrap.clientWidth);
  state.view.oy=fit(state.view.oy,state.chunk.h*CELL*state.view.scale,wrap.clientHeight);
}

// ---------- chunk load / meta ----------
let allChunkNames=[];
async function refreshChunkList(){allChunkNames=await api().list_chunks($('#activeOnly').checked);renderChunkOptions();}
function renderChunkOptions(){const q=$('#chunkSearch').value.toLowerCase();const sel=$('#chunkList');sel.innerHTML='';
  const shown=allChunkNames.filter(n=>n.toLowerCase().includes(q));
  shown.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);});
  $('#chunkCount').textContent=`${shown.length} of ${allChunkNames.length} chunks`;}
async function loadChunkModel(c,lib){
  if(c.error){setStatus(c.error,true);return;}
  if(state.preview){state.preview=null;$('#previewBar').hidden=true;}   // opening a chunk exits preview
  state.libEdit=!!lib;
  state.chunk=c;state.undo.length=0;state.redo.length=0;
  // enemies are their own layer now — strip any 'ennemy0' spawn markers baked into
  // the block grid so blocks and enemies don't fight over the same cell. The markers
  // are re-added into `active` at build (chunkfmt to_xml), so the game still spawns them.
  if(c.grid)for(const row of c.grid)for(let i=0;i<row.length;i++)if(row[i]==='ennemy0')row[i]='-';
  state.sel=null;state.floating=null;state.activePath=null;state.connStart=null;
  state.selEnemyCell=null;renderEnemyTune();
  $('#curChunk').textContent=c.name;syncMeta();
  const toks=new Set();[c.grid,c.bg,c.fg].forEach(g=>g&&g.forEach(row=>row.forEach(t=>toks.add(t))));
  (c.enemies||[]).forEach(e=>toks.add(e.properties));
  await fetchSprites([...toks]);
  fitView();
}
function syncMeta(){const c=state.chunk;$('#metaW').value=c.w;$('#metaH').value=c.h;$('#metaDiff').value=c.difficulty;$('#metaBg').value=c.bg_color??0;}
function syncPaletteSel(){$$('#tilePalette .swatch').forEach(s=>s.classList.toggle('sel',s.dataset.name===state.selTile));}

// ---------- by-date authoring (ordered level shaping) ----------
let dayState=null;          // {date, rows, order, slots, force_date}
async function refreshCalendar(){
  const cal=await api().get_calendar();
  const sel=$('#dateSel');sel.innerHTML='';
  (cal.days||[]).forEach(d=>{const o=document.createElement('option');o.value=d.date;
    const lock=cal.force_date===d.date?'🔒 ':'';
    o.textContent=`${lock}${d.date}  (${d.edited}/${d.editable} edited)`;sel.appendChild(o);});
  if(!cal.days||!cal.days.length){$('#dateInfo').textContent='no capture index (run tools/capture_january.py)';return;}
  if(cal.force_date)sel.value=cal.force_date;
  await loadDay(sel.value);
}
async function refreshLibrary(){
  const r=await api().get_library();const sel=$('#libList');if(!sel)return;
  const keep=sel.value;sel.innerHTML='';
  (r.library||[]).forEach(c=>{const o=document.createElement('option');o.value=c.name;
    o.textContent=`${c.name}  (${c.w}×${c.h})`;sel.appendChild(o);});
  if(keep)sel.value=keep;
}
async function refreshThemes(){
  const t=await api().get_themes();state.themeNames=t.names||[];
  const sel=$('#themeSel');if(!sel)return;
  if(!sel.dataset.built){sel.innerHTML='';
    const def=document.createElement('option');def.value='';def.textContent='(native — game default)';sel.appendChild(def);
    state.themeNames.forEach((nm,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`${i} · ${nm}`;sel.appendChild(o);});
    sel.dataset.built='1';
    sel.onchange=async()=>{const v=sel.value;await api().set_day_theme(dayState?dayState.date:$('#dateSel').value,v);
      setStatus(v===''?'theme cleared (native)':'theme → '+state.themeNames[+v]+' (locks this day)');await refreshCalendar();};}
}
function themeLabel(idx){return (idx==null||idx<0)?'?':(state.themeNames[idx]||('theme '+idx));}
function applyDay(r){
  if(r.error){$('#dateInfo').textContent=r.error;$('#daySeq').innerHTML='';dayState=null;return;}
  dayState=r;
  const gp=r.rows.filter(x=>x.role==='gameplay');
  const ed=gp.filter(x=>x.edited).length, del=gp.filter(x=>x.deleted).length;
  const locked=r.force_date===r.date;
  // theme: forced overrides native; reflect in the dropdown + info line
  const sel=$('#themeSel');if(sel)sel.value=(r.force_theme==null?'':String(r.force_theme));
  const themeTxt=r.force_theme!=null
    ? `🎨 <b>${themeLabel(r.force_theme)}</b> (forced)`
    : (r.native_theme!=null?`🎨 ${themeLabel(r.native_theme)}`:'🎨 theme not captured');
  $('#dateInfo').innerHTML=`${gp.length} sections · <b>${ed} edited</b> · ${del} emptied · ${themeTxt}`
    +(locked?' · <b style="color:#7fd17f">🔒 builds this day</b>':' · <i>not locked — press 🔒</i>');
  renderDaySeq();
  if(dayState&&dayState.date)loadDayThumbs(dayState.date);   // filmstrip thumbnails (async)
}
// fetch each chunk's grid once and re-render the day panel with thumbnails.
async function loadDayThumbs(date){
  try{const t=await api().get_day_thumbs(date);
    if(dayState&&dayState.date===date){dayState.thumbs=(t&&t.thumbs)||{};renderDaySeq();}}catch(e){}
}
// draw a tiny category-coloured silhouette of a chunk (walls/hazards/etc.) +
// purple enemy dots — recognisable at thumbnail size without loading sprites.
// render a section EXACTLY like the main editor: draw the whole chunk (all layers +
// enemies + firebars, real sprites at real size/pivot) to an offscreen canvas by
// pointing the editor's globals at it, then scale that into the thumbnail. So the
// preview matches the level 1:1 instead of using a simplified drawing.
function renderThumb(destCv,d){
  const g=destCv.getContext('2d'),W=destCv.width,H=destCv.height;
  g.clearRect(0,0,W,H); g.imageSmoothingEnabled=false;
  if(!d||!d.w||!d.h)return;
  const CW=d.w*CELL, CH=d.h*CELL;
  const off=document.createElement('canvas'); off.width=CW; off.height=CH;
  const offCtx=off.getContext('2d'); offCtx.imageSmoothingEnabled=false;
  const sCtx=ctx,sChunk=state.chunk,sView=state.view,sLayer=state.layer,
        sVis=state.layerVis,sOnion=state.onion,sShowRot=state.showRot;
  ctx=offCtx;
  state.chunk={grid:d.grid,bg:d.bg,fg:d.fg,enemies:d.enemies,w:d.w,h:d.h,bg_color:d.bg_color};
  state.view={scale:1,ox:0,oy:0}; state.layer='active';
  state.layerVis={bg:true,active:true,fg:true,grid2:true,enemy:true}; state.onion=false; state.showRot=false;
  try{
    offCtx.fillStyle=THEME_BG[(d.bg_color||0)%THEME_BG.length]||'#101018';
    offCtx.fillRect(0,0,CW,CH);
    for(const L of ['bg','active','fg']) drawLayer(layerGrid(L),1);
    (d.enemies||[]).forEach(e=>{const rec=sprites[e.properties]; if(rec&&rec.img) enemyBlit(rec,e.sx*CELL,e.sy*CELL);});
    drawFirebars();
  }catch(err){}
  finally{ctx=sCtx;state.chunk=sChunk;state.view=sView;state.layer=sLayer;
    state.layerVis=sVis;state.onion=sOnion;state.showRot=sShowRot;}
  const s=Math.min(W/CW,H/CH),dw=CW*s,dh=CH*s;
  g.drawImage(off,(W-dw)/2,(H-dh)/2,dw,dh);
}

// ---------- visual chunk picker (thumbnails) ----------
// Replaces typed prompts for insert/replace/append. Lists custom chunks first,
// then game chunks (active season, or all). Thumbnails lazy-load on scroll.
let _ckPick=null,_ckIO=null,_ckTimer=null,_ckNames=[],_ckClear=false;
function openChunkPicker(title,onPick,withClear){
  _ckPick=onPick; _ckClear=!!withClear;
  $('#chunkPickTitle').textContent=title||'Pick a section';
  $('#chunkPickModal').classList.remove('hidden');
  $('#chunkPickSearch').value='';
  loadChunkPickNames().then(renderChunkPickGrid);
  $('#chunkPickSearch').focus();
}
async function loadChunkPickNames(){
  let game=[],lib=[];
  try{game=await api().list_chunks(false)||[];}catch(e){}   // always all seasons
  try{const L=await api().get_library();lib=(L.library||[]).map(x=>x.name);}catch(e){}
  // custom chunks first, then game chunks; "(empty corridor)" sentinel optional
  _ckNames=(_ckClear?['']:[]).concat(lib,game);
}
function closeChunkPicker(){$('#chunkPickModal').classList.add('hidden');if(_ckIO){_ckIO.disconnect();_ckIO=null;}}
function chooseChunk(name){const cb=_ckPick;closeChunkPicker();if(cb)cb(name);}
function renderChunkPickGrid(){
  const q=$('#chunkPickSearch').value.trim().toLowerCase();
  const matches=q?_ckNames.filter(n=>n.toLowerCase().includes(q)):_ckNames;
  $('#chunkPickCount').textContent=matches.length+' section'+(matches.length===1?'':'s');
  const grid=$('#chunkPickGrid');grid.innerHTML='';
  if(_ckIO)_ckIO.disconnect();
  _ckIO=new IntersectionObserver(ents=>{
    const hit=ents.filter(e=>e.isIntersecting&&e.target._name).map(e=>e.target);
    if(!hit.length)return; hit.forEach(t=>_ckIO.unobserve(t));
    api().chunk_thumbs(hit.map(t=>t._name)).then(async res=>{
      const th=(res&&res.thumbs)||{};
      // load the REAL sprites for every tile/enemy in these thumbs, then draw
      const toks=new Set(), addGrid=gr=>gr&&gr.forEach(row=>row&&row.forEach(x=>{if(x&&x!=='-')toks.add(x);}));
      hit.forEach(t=>{const d=th[t._name]; if(!d)return;
        addGrid(d.grid); addGrid(d.bg); addGrid(d.fg);
        (d.enemies||[]).forEach(e=>toks.add(e.properties));});
      await fetchSprites([...toks]);
      hit.forEach(t=>{const d=th[t._name]; if(d&&d.w)renderThumb(t._cv,d);});
    }).catch(()=>{});
  },{root:grid,rootMargin:'350px'});
  const frag=document.createDocumentFragment();
  matches.forEach(name=>{
    const sw=document.createElement('div');sw.className='gswatch';sw.dataset.name=name;sw._name=name;
    const cv=document.createElement('canvas');cv.className='ckthumb';cv.width=168;cv.height=224;sw._cv=cv;sw.appendChild(cv);
    const lbl=document.createElement('div');lbl.textContent=name||'— empty gap —';sw.appendChild(lbl);
    sw.onclick=()=>chooseChunk(name);
    frag.appendChild(sw);
  });
  grid.appendChild(frag);
  grid.querySelectorAll('.gswatch').forEach(sw=>{if(sw._name)_ckIO.observe(sw);});
}
async function loadDay(date){if(!date)return;applyDay(await api().get_day_sequence(date));}
async function pushOrder(){applyDay(await api().set_day_order(dayState.date,dayState.order));}
function renderDaySeq(){
  const box=$('#daySeq');box.innerHTML='';
  dayState.rows.forEach(row=>{
    const el=document.createElement('div');el.className='seqrow '+row.role;
    if(row.role!=='gameplay'){
      const nm=`${row.name}${row.edited?' <b>✎</b>':''}${row.removed?' <i>(removed)</i>':''}`;
      let btns=`<button data-repn="${row.name}" title="swap this piece for another">⇄</button>`
        +(row.edited?`<button data-restoren="${row.name}" title="restore original">↺</button>`:'');
      // checkpoints can be removed from the day (the finish/specials cannot).
      if(row.role==='checkpoint'&&row.struct_ord!=null){
        btns+=row.removed
          ?`<button data-cprestore="${row.struct_ord}" title="put this checkpoint back">↺ restore</button>`
          :`<button data-cpremove="${row.struct_ord}" title="remove this checkpoint from the day">✕ remove</button>`;
      }
      if(row.removed)el.classList.add('removed');
      el.innerHTML=`<span class="seqrole">${row.role}</span>`
        +`<span class="seqname" data-editn="${row.name}" title="double-click to edit this section">${nm}</span>`
        +`<span class="seqbtns">${btns}</span>`;
    }else{
      const gp=row.gp;
      el.draggable=true;el.dataset.gp=gp;                 // drag to reorder
      if(row.extra)el.classList.add('extra');
      const nm=row.deleted?'<i>— empty gap —</i>':`${row.name}${row.edited?' <b>✎</b>':''}${row.extra?' <span class="xtag">+extra</span>':''}`;
      el.innerHTML=`<span class="seqdrag" title="drag to reorder">⠿</span>`
        +`<span class="seqmove power-only"><button data-mv="up" data-gp="${gp}">▲</button>`
        +`<button data-mv="dn" data-gp="${gp}">▼</button></span>`
        +`<span class="seqname" data-edit="${gp}" title="double-click to edit this section">${nm}</span>`
        +`<span class="seqbtns"><button data-ins="${gp}" title="add a section here (pushes everything above — checkpoints included — up)">＋</button>`
        +`<button data-rep="${gp}" title="swap for another section">⇄</button>`
        +`<button data-cpflag="${gp}" class="${row.checkpoint?'on':''}" title="${row.checkpoint?'unset custom checkpoint':'make this a custom checkpoint (respawn point here)'}">${row.checkpoint?'⚑':'⚐'}</button>`
        +`<button data-del="${gp}" title="delete this section — everything above slides down (checkpoints included). For a climbable gap instead, use ⇄ → empty.">✕</button></span>`;
      if(row.checkpoint)el.classList.add('iscp');
    }
    const th=dayState.thumbs&&dayState.thumbs[row.name];   // filmstrip thumbnail
    if(th&&th.w){const tc=document.createElement('canvas');tc.className='seqthumb';tc.width=32;tc.height=40;
      renderThumb(tc,th);el.insertBefore(tc,el.querySelector('.seqname'));}
    box.appendChild(el);
  });
  // append-to-end control (EXPERIMENTAL longer levels, POWER MODE only): adds a
  // section past the level's native length. Extras may not render — playtest.
  const add=document.createElement('div');add.className='seqrow addrow power-only';add.hidden=!state.power;
  add.innerHTML='<button data-act-append style="flex:1">＋ Add section to end (experimental longer level)</button>';
  box.appendChild(add);
  add.querySelector('[data-act-append]').onclick=appendGp;
  box.querySelectorAll('[data-mv]').forEach(b=>b.onclick=()=>moveGp(+b.dataset.gp,b.dataset.mv==='up'?-1:1));
  box.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delGp(+b.dataset.del));
  box.querySelectorAll('[data-rep]').forEach(b=>b.onclick=()=>repGp(+b.dataset.rep));
  box.querySelectorAll('[data-ins]').forEach(b=>b.onclick=()=>insertGp(+b.dataset.ins));
  box.querySelectorAll('[data-edit]').forEach(b=>b.ondblclick=()=>editGp(+b.dataset.edit));
  box.querySelectorAll('[data-editn]').forEach(b=>b.ondblclick=()=>editName(b.dataset.editn));
  box.querySelectorAll('[data-repn]').forEach(b=>b.onclick=()=>replaceName(b.dataset.repn));
  box.querySelectorAll('[data-restoren]').forEach(b=>b.onclick=()=>replaceName(b.dataset.restoren,''));
  // checkpoint remove / restore / custom-flag
  box.querySelectorAll('[data-cpremove]').forEach(b=>b.onclick=async()=>{applyDay(await api().remove_day_checkpoint(dayState.date,+b.dataset.cpremove));setStatus('checkpoint removed from this day');});
  box.querySelectorAll('[data-cprestore]').forEach(b=>b.onclick=async()=>{applyDay(await api().restore_day_checkpoint(dayState.date,+b.dataset.cprestore));setStatus('checkpoint restored');});
  box.querySelectorAll('[data-cpflag]').forEach(b=>b.onclick=async()=>{const gp=+b.dataset.cpflag;const cur=(dayState.rows.find(r=>r.gp===gp)||{}).checkpoint;applyDay(await api().toggle_custom_checkpoint(dayState.date,gp,!cur));setStatus(cur?'custom checkpoint removed':'custom checkpoint set here');});
  wireDayDrag(box);
  applyPower();                                            // honour power state for new rows
}
// ---- drag-to-reorder the day's gameplay sections (Mario-Maker style) --------
let _dragGp=null,_dropAt=null;
function clearDropMarks(box){box.querySelectorAll('.dropbefore,.dropafter')
  .forEach(el=>el.classList.remove('dropbefore','dropafter'));}
function wireDayDrag(box){
  box.querySelectorAll('.seqrow.gameplay').forEach(el=>{
    el.ondragstart=e=>{_dragGp=+el.dataset.gp;_dropAt=null;el.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',el.dataset.gp);}catch(_){}};
    el.ondragend=()=>{_dragGp=null;_dropAt=null;clearDropMarks(box);
      box.querySelectorAll('.dragging').forEach(x=>x.classList.remove('dragging'));};
    el.ondragover=e=>{if(_dragGp==null)return;e.preventDefault();
      const r=el.getBoundingClientRect(),before=(e.clientY-r.top)<r.height/2;
      clearDropMarks(box);el.classList.add(before?'dropbefore':'dropafter');
      _dropAt={gp:+el.dataset.gp,before};};
    el.ondrop=e=>{if(_dragGp==null||!_dropAt)return;e.preventDefault();
      const from=_dragGp;let to=_dropAt.gp+(_dropAt.before?0:1);
      _dragGp=null;_dropAt=null;clearDropMarks(box);
      if(to===from||to===from+1)return;                   // dropped in place
      const o=dayState.order,[x]=o.splice(from,1);
      if(from<to)to--;
      o.splice(to,0,x);
      pushOrder();};
  });
}
async function editName(name){loadChunkModel(await api().load_chunk(name));
  setStatus('editing '+name+' — Save level → mod when done (changes every place this chunk appears)');}
// the chunk currently open in the canvas — pre-fill replace/insert prompts with
// it so "edit a chunk, then drop it into a slot" doesn't need retyping the name.
function editingName(){return (state.chunk&&state.chunk.name)||'';}
async function replaceName(name,src){
  const s=src!==undefined?src:prompt('Replace "'+name+'" with which chunk\'s content?\n(blank = restore original)',editingName()||name);
  if(s===null)return;
  await api().replace_chunk(name,s.trim());
  if(dayState)await loadDay(dayState.date);
  setStatus(s.trim()?('replaced '+name+' ← '+s.trim()):('restored '+name));}
async function moveGp(gp,dir){const o=dayState.order,j=gp+dir;if(j<0||j>=o.length)return;[o[gp],o[j]]=[o[j],o[gp]];await pushOrder();}
async function delGp(gp){applyDay(await api().delete_day_chunk(dayState.date,gp));}
// end/checkpoint/special chunks are FUNCTIONAL — an end chunk (finish/endzone)
// has a FinalChunk component that ENDS the level when reached, so it only works
// at the level's end; checkpoints/specials are fixed too. Putting one in a
// gameplay slot breaks the level. Warn before allowing it.
function structuralKind(name){
  const b=String(name||'').toLowerCase();
  if(b==='finish'||b==='finish2'||(b.startsWith('finish')&&!b.includes('endzone'))||/^end\d*_/.test(b.split('/').pop())||b.includes('endchunks/'))return 'the END piece — it will be tagged as the finish, so the level ENDS at this section (later sections become unreachable)';
  if(b.includes('endzone'))return 'an ENDZONE piece (decorative ending ramp — position-sensitive, has no override tag, so it may not render right)';
  if(b.includes('checkpoint'))return 'a CHECKPOINT piece — it will be tagged so this section becomes a checkpoint';
  if(b.includes('tom_tv')||b.includes('reward_powerup')||b.includes('enable_notifications')||b.includes('king_poster')||b.includes('bonus_room'))return 'a SPECIAL piece';
  if(b==='chunk0'||b.includes('start'))return 'the START piece';
  return null;
}
function okStructural(name){
  const k=structuralKind(name);
  return !k || confirm('"'+name+'" is '+k+'.\n\nPlace it here?');
}
function repGp(gp){
  // Swap = pick a section visually, then DELETE the slot's chunk + INSERT the
  // new one in its place via the order/override path (which places each chunk by
  // its OWN resolved Levels/vNNN path, so it renders). NEVER the in-place content
  // overwrite (replace_chunk), which can leave the slot blank.
  openChunkPicker('Swap this section for…',async name=>{
    if(name&&!okStructural(name))return;
    dayState.order.splice(gp,1,name);   // remove old at gp + insert new there
    await pushOrder();
    setStatus(name?('swapped in '+name):('cleared this section'));
  },true);   // offer the "empty gap" option
}
function insertGp(gp){
  openChunkPicker('Add a section here…',async name=>{
    if(!name||!okStructural(name))return;
    applyDay(await api().insert_day_chunk(dayState.date,gp,name));
    setStatus('added '+name);
  });
}
function appendGp(){
  if(!dayState){setStatus('pick a date first',true);return;}
  openChunkPicker('Add a section to the END (experimental longer level)',async name=>{
    if(!name||!okStructural(name))return;
    dayState.order.push(name);
    await pushOrder();
    setStatus('added '+name+' to the end (experimental) — Play to see if the longer level renders');
  });
}
async function editGp(gp){const n=dayState.order[gp];if(!n){setStatus('empty gap — swap in a section first (⇄)',true);return;}
  loadChunkModel(await api().load_chunk(n));setStatus('editing '+n+' — Save level → mod when done');}

// ---------- actions ----------
const ACTIONS={
  async lockDate(){const d=$('#dateSel').value;if(!d)return;
    await api().lock_date(d);setStatus('🔒 build locked to '+d+' — VIP unlocked');await refreshCalendar();},
  async pickFirebar(){
    const kind=state.trapKind;if(!kind)return;
    const p=ELEM_PANELS[kind]||{};let settings={};
    if(p.mechanism==='mace'){
      const mode=$('#fbDir').value,circular=(mode==='cw'||mode==='ccw');
      // spin: cw/ccw = direction. swing: start left/right = which way it sets off.
      settings={length:+$('#fbLen').value||3,start:$('#fbStart').value,
        clockwise:(mode==='cw'||mode==='swingL'),double:$('#fbDouble').checked,circular};
    }else if(p.mechanism==='fields'){
      // enemy projectile panels attach the override to the SELECTED enemy token
      // (__carrier__); trap panels keep the chosen sprite/tile as the carrier.
      if(p.enemy)settings.__carrier__=String(state.selEnemy||'').split('@')[0];
      else settings.token=String(state.selTile||'').split('@')[0];
      $('#fbFields').querySelectorAll('input,select').forEach(inp=>{
        settings[inp.dataset.key]=inp.dataset.ftype==='bool'?inp.checked
          :(inp.dataset.ftype==='select'?inp.value:(+inp.value));});
    }
    applyElementResult(await api().place_element(kind,settings));
  },
  clearEnemyTuning(){
    const cell=state.selEnemyCell;if(!cell||!state.chunk)return;
    if(state.enemyTuning)delete state.enemyTuning[etKey(cell.sx,cell.sy)];
    renderEnemyTune();setStatus('enemy tuning cleared (kept on Save level → mod)');
  },
  async resetDay(){if(!dayState)return;dayState.order=[...dayState.slots];await pushOrder();setStatus('order reset to original');},
  async restoreDay(){const d=dayState?dayState.date:$('#dateSel').value;if(!d){setStatus('pick a date first',true);return;}
    if(!confirm('Fully restore '+d+' to the original game level?\nThis clears ALL your edits and reordering for this day (other days are untouched).'))return;
    const r=await api().restore_day(d);if(r.error){setStatus(r.error,true);return;}
    applyDay(r);updateEdited((await api().get_state()).edited_levels);
    setStatus('⟲ '+d+' restored to original — cleared '+((r.removed_edits||[]).length)+' edit(s)');},
  async playtestDay(){if(!dayState){setStatus('pick a date first',true);return;}
    await api().lock_date(dayState.date);
    setStatus('building '+dayState.date+' → installing → launching… (can take ~30s)');
    const r=await api().playtest(null);
    if(r.error){setStatus('playtest failed: '+r.error,true);$('#logOut').textContent=(r.log||[]).join('\n')+'\n\nERROR: '+r.error;$('#logModal').classList.remove('hidden');}
    else{setStatus('▶ playing '+(r.force_date||dayState.date)+' on the emulator');await refreshCalendar();}},
  async viewFullLevel(){const d=$('#dateSel').value;if(!d){setStatus('pick a date first',true);return;}
    setStatus('assembling the whole level…');
    const r=await api().preview_day(d);
    if(r.error){setStatus(r.error,true);return;}
    await loadChunkModel(r); state.preview=d;     // set AFTER (loadChunkModel clears it)
    $('#previewInfo').textContent=d+(r.chunk_count?' · '+r.chunk_count+' chunks':'');
    $('#previewBar').hidden=false; $('#curChunk').textContent='👁 whole level — '+d;
    setStatus('👁 whole level for '+d+' (read-only)');},
  exitPreview(){state.preview=null;$('#previewBar').hidden=true;state.chunk=null;
    $('#curChunk').textContent='— pick a chunk —';draw();setStatus('exited preview');},
  async newProj(){const n=prompt('Project name','My Leap Day Mod');if(n===null)return;applyState(await api().new_project(n));setStatus('new project');},
  async openProj(){const st=await api().load_project();if(st.error)return;applyState(st);setStatus('loaded '+st.project_name);},
  async saveProj(){const r=await api().save_project();setStatus(r.saved?('saved '+r.saved):r.error,!!r.error);},
  async loadXapk(){setStatus('extracting game…');const st=await api().pick_xapk();if(st.error){setStatus(st.error,true);return;}
    applyState(st);await refreshChunkList();await refreshCalendar();setStatus('game loaded — loading sprites…');refreshPaletteArt().then(()=>setStatus('game loaded — pick a date or chunk'));},
  resize(){if(!state.chunk)return;snapshot();let w=+$('#metaW').value;const h=+$('#metaH').value;
    if($('#enforceWidth').checked){const legal=[14,28,42],sw=legal.reduce((a,b)=>Math.abs(b-w)<Math.abs(a-w)?b:a);
      if(sw!==w){setStatus('width snapped to '+sw+' (legal widths: 14/28/42)');w=sw;$('#metaW').value=sw;}}
    const re=g=>g?Array.from({length:h},(_,r)=>Array.from({length:w},(_,c)=>(g[r]&&g[r][c])||'-')):null;
    state.chunk.grid=re(state.chunk.grid);if(state.chunk.bg)state.chunk.bg=re(state.chunk.bg);if(state.chunk.fg)state.chunk.fg=re(state.chunk.fg);
    state.chunk.w=w;state.chunk.h=h;fitView();},
  fit(){fitView();},
  async saveLevel(){const c=state.chunk;if(!c){setStatus('nothing to save',true);return;}
    c.difficulty=parseFloat($('#metaDiff').value);c.bg_color=parseInt($('#metaBg').value);
    const tun=chunkTunings(c.name);          // per-enemy tuning travels with the chunk
    if(state.libEdit){await api().save_library_chunk(c.name,c,tun);await refreshLibrary();
      setStatus('saved custom chunk '+c.name);return;}
    const r=await api().save_level(c.name,c,tun);updateEdited(r.edited_levels);
    if(r.enemy_tuning)syncChunkTunings(c.name,r.enemy_tuning);   // adopt the reconciled set (orphans pruned)
    if(r.to_library){await refreshLibrary();
      setStatus('"'+c.name+'" is a new chunk → saved to Custom chunks. Insert it into a day (＋) to place it — a new name can\'t overwrite a game chunk.');
      return;}
    if($('#dateSel').value)loadDay($('#dateSel').value);   // refresh day edit-progress
    setStatus('saved '+c.name+' into mod');},
  async editChunk(){const n=$('#chunkList').value;if(!n){setStatus('select a chunk',true);return;}loadChunkModel(await api().load_chunk(n));},
  async blankChunk(){const n=prompt('New level overwrites which existing chunk name?\n(must be a real chunk so the game loads it)',$('#chunkList').value||'');if(!n)return;loadChunkModel(await api().blank_chunk(n));},
  async removeLevel(){const n=$('#editedList').value;if(!n)return;updateEdited((await api().remove_level(n)).edited_levels);},
  // ---- custom-chunk library ----
  async newLibChunk(){const n=prompt('Name for the new custom chunk:','my_chunk');if(!n)return;
    const w=parseInt(prompt('Width','14'))||14,h=parseInt(prompt('Height','19'))||19;
    const r=await api().new_library_chunk(n.trim(),w,h);
    if(r.error){setStatus(r.error,true);return;}
    await refreshLibrary();loadChunkModel(r,true);setStatus('new custom chunk '+r.name+' — paint it, then Save level → mod');},
  async editLibChunk(){const n=$('#libList').value;if(!n){setStatus('select a custom chunk',true);return;}
    loadChunkModel(await api().load_library_chunk(n),true);setStatus('editing custom chunk '+n);},
  async renameLibChunk(){const o=$('#libList').value;if(!o)return;const n=prompt('Rename custom chunk:',o);if(!n||n.trim()===o)return;
    const r=await api().rename_library_chunk(o,n.trim());if(r.error){setStatus(r.error,true);return;}await refreshLibrary();},
  async removeLibChunk(){const n=$('#libList').value;if(!n)return;if(!confirm('Delete custom chunk "'+n+'"?'))return;
    await api().remove_library_chunk(n);await refreshLibrary();},
  build(){doBuild(false);}, buildInstall(){doBuild(true);},
  async playtest(){const c=state.chunk;
    if(c){c.difficulty=parseFloat($('#metaDiff').value);c.bg_color=parseInt($('#metaBg').value);}
    // lock to the day being edited so the build loads the level you injected into
    const d=$('#dateSel')&&$('#dateSel').value;
    if(d)await api().lock_date(d);
    else if(!c){setStatus('pick a date (or open a chunk) first',true);return;}
    setStatus('playtest: building '+(d||'')+' → installing → launching into your level… (~30s)');
    const r=await api().playtest(c||null);
    if(r.error){setStatus('playtest failed: '+r.error,true);$('#logOut').textContent=(r.log||[]).join('\n')+'\n\nERROR: '+r.error;$('#logModal').classList.remove('hidden');}
    else setStatus('▶ playing '+(r.force_date||'your level')+' — climb to reach your injected chunks');},
  undo(){undo();}, redo(){redo();},
  openGallery(){showGallery();}, closeGallery(){hideGallery();},
  closeArt(){closeArtPicker();}, closeChunkPick(){closeChunkPicker();},
  closeLog(){$('#logModal').classList.add('hidden');},
  // ---- dev mode: bake / clear a sprite anchor+arrow fix ----
  async devSave(){const tok=state.devTok;if(!tok){setStatus('select a tile first',true);return;}
    const r=await api().set_sprite_override(tok,devVals());
    if(r&&!r.error){state.devOverrides=r.overrides||state.devOverrides;
      loadRecInto(tok,r.rec,()=>{draw();drawDevPreview();});
      setStatus('🛠 saved sprite fix for '+tok+' — baked into the editor');}
    else setStatus((r&&r.error)||'could not save (load your .xapk first)',true);},
  async devReset(){const tok=state.devTok;if(!tok)return;
    const r=await api().clear_sprite_override(tok);
    if(r&&!r.error){state.devOverrides=r.overrides||state.devOverrides;
      loadRecInto(tok,r.rec,()=>{syncDev();draw();});
      setStatus('reset '+tok+' to automatic placement');}
    else setStatus((r&&r.error)||'could not reset',true);},
  async devShootSave(){const bakes=collectShootBakes();
    const r=await api().save_shoot_bakes(bakes);
    if(r&&!r.error){state.shootBakes=r||{};renderShootBakes();
      setStatus('⚙ shoot-speed bakes saved ('+Object.keys(state.shootBakes).length+' combos) — rebuild to apply');}
    else setStatus((r&&r.error)||'could not save bakes',true);},
};
async function doBuild(install){setStatus(install?'building + installing…':'building…');
  const r=await api().build(install);
  $('#logOut').textContent=(r.log||[]).join('\n')+(r.error?('\n\nERROR: '+r.error):'\n\n✔ '+JSON.stringify({levels:r.levels_applied,signed:r.signed,installed:r.installed_on||false},null,1));
  $('#logModal').classList.remove('hidden');setStatus(r.error?('build failed: '+r.error):'build complete',!!r.error);}
function updateEdited(list){const sel=$('#editedList');sel.innerHTML='';(list||[]).forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);});}
// character picker: build once, reflect the current force_character
function buildForceChar(names,cur){
  const sel=$('#forceChar'); if(!sel)return;
  if(!sel.dataset.built){
    sel.innerHTML='<option value="">— off (use selection) —</option>'
      +names.map((n,i)=>`<option value="${i}">${i}: ${n}</option>`).join('');
    sel.onchange=async e=>{const st=await api().set_force_character(e.target.value);
      setStatus(e.target.value===''?'character force OFF':'forced: '+st.character_names[+e.target.value]+' — build + playtest');};
    sel.dataset.built='1';
  }
  sel.value=(cur==null?'':String(cur));
}
// "grapple as" picker: clone a character's look onto Lick (who keeps the grapple)
function buildGrappleSkin(names,cur){
  const sel=$('#grappleSkin'); if(!sel)return;
  if(!sel.dataset.built){
    sel.innerHTML='<option value="">— off —</option>'
      +names.map((n,i)=>`<option value="${i}">${i}: ${n}</option>`).join('');
    sel.onchange=async e=>{const st=await api().set_grapple_skin(e.target.value);
      if(st.character_names)buildForceChar(st.character_names,st.force_character);
      setStatus(e.target.value===''?'grappling hook off':'🪝 grapple as '+st.character_names[+e.target.value]+' (looks like them, plays as Lick) — build + playtest');};
    sel.dataset.built='1';
  }
  sel.value=(cur==null?'':String(cur));
}
function applyState(st){$('#projName').value=st.project_name||'';updateEdited(st.edited_levels);refreshLibrary();
  if(st.enforce_width!==undefined)$('#enforceWidth').checked=!!st.enforce_width;
  if(st.spawn_mult!==undefined)$('#spawnMult').value=(Number(st.spawn_mult)>1?st.spawn_mult:'');
  if(st.grappling_hook!==undefined)$('#grapplingHook').checked=!!st.grappling_hook;
  if(st.clone_package!==undefined)$('#clonePkg').checked=!!st.clone_package;
  if(st.strip_store!==undefined)$('#stripStore').checked=!!st.strip_store;
  if(st.keep_music_bg!==undefined)$('#keepMusicBg').checked=!!st.keep_music_bg;
  if(st.bg_mode!==undefined)$('#bgMode').value=st.bg_mode;
  if(st.keep_music_bg!==undefined)$('#bgMode').disabled=!st.keep_music_bg;
  if(st.smooth_camera!==undefined)$('#smoothCamera').checked=!!st.smooth_camera;
  if(st.lock_camera_y!==undefined)$('#lockCameraY').checked=!!st.lock_camera_y;
  if(st.lock_y_cap_top!==undefined)$('#lockYCapTop').checked=!!st.lock_y_cap_top;
  if(st.brick_dead_sides!==undefined)$('#brickDeadSides').checked=!!st.brick_dead_sides;
  if(st.hide_timer!==undefined)$('#hideTimer').checked=!!st.hide_timer;
  if(st.hide_progress!==undefined)$('#hideProgress').checked=!!st.hide_progress;
  if(st.checkpoint_fruit_cost!==undefined)$('#cpFruitCost').value=(st.checkpoint_fruit_cost==null?'':st.checkpoint_fruit_cost);
  if(st.force_checkpoint_mode!==undefined)$('#cpMode').value=(st.force_checkpoint_mode==null?'':String(st.force_checkpoint_mode));
  if(st.flag_checkpoints!==undefined)$('#flagCheckpoints').checked=!!st.flag_checkpoints;
  if(st.firebars)state.firebars=st.firebars;
  if(st.enemy_tuning)state.enemyTuning=st.enemy_tuning;
  if(st.axe)state.axe=st.axe;
  if(st.projectiles)state.projectiles=st.projectiles;
  if(st.shoot_bakes)state.shootBakes=st.shoot_bakes;
  if(st.shoot_classes)state.shootClasses=st.shoot_classes;
  if(st.shoot_enemies)state.shootEnemies=st.shoot_enemies;
  if(st.character_names){buildForceChar(st.character_names,st.force_character);buildGrappleSkin(st.character_names,st.grapple_skin);}
  if(st.firebar_dot&&(!fbDot||fbDot._src!==st.firebar_dot)){fbDot=new Image();fbDot._src=st.firebar_dot;fbDot.onload=draw;fbDot.src=st.firebar_dot;}
  state.power=!!st.power_mode;$('#powerMode').checked=state.power;applyPower();
  // opening/creating a project changes the edited chunks, so the day filmstrip
  // thumbnails are stale — reload the current day so the previews match the mod.
  if(dayState&&dayState.date)loadDay(dayState.date);}
// show/hide power-only UI. Off = the friendly, guarded editor; on = advanced
// tools (sprite-fix, experimental longer levels, raw placement) that can crash.
function applyPower(){
  $$('.power-only').forEach(el=>{el.hidden=!state.power;});
  if(!state.power&&state.dev){$('#devMode').checked=false;setDevMode(false);}   // close dev panel when leaving
  if(!state.power&&state.devShoot){$('#devShoot').checked=false;setDevShoot(false);}
  document.body.classList.toggle('power',state.power);
}

// ---------- wiring ----------
function setTool(t){
  if(state.tool==='path'&&t!=='path')finishPath();   // switching away ends the open path
  state.connStart=null;
  state.tool=t;$$('.tool').forEach(b=>b.classList.toggle('active',b.dataset.tool===t));
  // keep the Layers panel in step: enemy tool -> enemy layer; a tile tool off the
  // enemy layer -> grid 1. Set .checked directly so we don't re-fire the radio handler.
  if(t==='enemy'){selectLayerRadio('enemy');state.layer='enemy';}
  else if(state.layer==='enemy'){selectLayerRadio('active');state.layer='active';}
  cv.style.cursor=t==='pan'?'grab':(t==='eyedrop'?'cell':'crosshair');updateFirebarPanel();if(state.dev)syncDev();draw();}
function wire(){
  $$('[data-act]').forEach(b=>b.onclick=()=>ACTIONS[b.dataset.act]&&ACTIONS[b.dataset.act]());
  $$('.tool').forEach(b=>b.onclick=()=>setTool(b.dataset.tool));
  $('#tileSearch').oninput=()=>filterPalette('#tileSearch','#tilePalette');
  $('#enemySearch').oninput=()=>filterPalette('#enemySearch','#enemyPalette');
  $('#activeOnly').onchange=refreshChunkList;
  $('#enforceWidth').onchange=async e=>{await api().set_setting('enforce_width',e.target.checked);
    setStatus(e.target.checked?'width lock ON — chunks snap to 14/28/42':'width lock OFF');};
  $('#spawnMult').onchange=async e=>{let v=parseFloat(e.target.value);
    if(!isFinite(v)||v<1)v=1; if(v>100)v=100;
    e.target.value=(v>1?v:'');
    await api().set_setting('spawn_mult',v);
    setStatus(v>1?`every enemy fires/spawns ×${v} faster`:'enemy spawn/fire rate back to stock');};
  $('#projName').onchange=async e=>{const st=await api().set_project_name(e.target.value);
    if(st&&st.project_name)e.target.value=st.project_name;};
  $('#grapplingHook').onchange=async e=>{await api().set_grappling_hook(e.target.checked);
    setStatus(e.target.checked?'🪝 grappling hook from spawn ON (any character, permanent) — build to test':'grappling hook OFF');};
  $('#keepMusicBg').onchange=async e=>{await api().set_keep_music_bg(e.target.checked);
    $('#bgMode').disabled=!e.target.checked;
    setStatus(e.target.checked?'🎵 offscreen music+background ON — Playtest to hear it (wide 28/42 sections)':'offscreen music+background OFF');};
  $('#bgMode').onchange=async e=>{await api().set_bg_mode(e.target.value);
    setStatus(e.target.value==='bare'?'background: bare sky everywhere (scenery stripped)':'background: full animated scenery on every screen');};
  $('#smoothCamera').onchange=async e=>{await api().set_smooth_camera(e.target.checked);
    setStatus(e.target.checked?'🎥 smooth camera ON — wide sections follow the player (Playtest to see it)':'smooth camera OFF — stock screen-snap');};
  $('#lockCameraY').onchange=async e=>{await api().set_lock_camera_y(e.target.checked);
    setStatus(e.target.checked?'🔒 camera Y locked to every section (Playtest to see it)':'camera Y lock OFF');};
  $('#lockYCapTop').onchange=async e=>{await api().set_lock_y_cap_top(e.target.checked);
    setStatus(e.target.checked?'📦 lock camera Y boxes in the top too':'lock camera Y caps bottom only — top open to see the next section');};
  $('#brickDeadSides').onchange=async e=>{await api().set_brick_dead_sides(e.target.checked);
    setStatus(e.target.checked?'🧱 dead side areas of wide sections will be bricked in the build':'brick dead sides OFF');};
  $('#hideTimer').onchange=async e=>{await api().set_hide_timer(e.target.checked);
    setStatus(e.target.checked?'⏱ timer hidden at Playtest':'timer shown');};
  $('#hideProgress').onchange=async e=>{await api().set_hide_progress(e.target.checked);
    setStatus(e.target.checked?'📊 progress bar hidden at Playtest':'progress bar shown');};
  $('#cpFruitCost').onchange=async e=>{const v=e.target.value.trim();
    const st=await api().set_checkpoint_fruit_cost(v===''?null:v);
    if(st&&st.error){setStatus('⚠ '+st.error);e.target.value='';}
    else setStatus(v===''?'checkpoint fruit cost: game default (20)':('🍎 every checkpoint now costs '+v+' fruits — build to test'));};
  $('#cpMode').onchange=async e=>{const v=e.target.value;
    const st=await api().set_checkpoint_mode(v===''?null:v);
    if(st&&st.error){setStatus('⚠ '+st.error);e.target.value='';}
    else setStatus(v===''?'checkpoint mode: game default':(v==='1'?'🚩 VIP auto checkpoints — free unlock as you pass (build + playtest)':'🚩 VIP fruit checkpoints — pay the fruit cost below (build + playtest)'));};
  $('#flagCheckpoints').onchange=async e=>{await api().set_flag_checkpoints(e.target.checked);
    setStatus(e.target.checked?'🚩 checkpoint chests reskinned as non-blocking flags — pair with VIP auto (build + playtest)':'flag-style checkpoints off');};
  $('#clonePkg').onchange=async e=>{await api().set_setting('clone_package',e.target.checked);
    setStatus(e.target.checked?'📲 builds install ALONGSIDE the original (separate app — playtest on device)':'builds replace the original game');};
  $('#stripStore').onchange=async e=>{await api().set_setting('strip_store',e.target.checked);
    setStatus(e.target.checked?'ads/store components will be stripped':'ads/store components kept');};
  $('#powerMode').onchange=async e=>{await api().set_setting('power_mode',e.target.checked);
    state.power=e.target.checked;applyPower();
    setStatus(state.power?'⚡ Power mode ON — advanced tools unlocked (these can crash levels)':'Power mode OFF — friendly mode');};
  $('#chunkSearch').oninput=renderChunkOptions;
  $('#chunkList').ondblclick=()=>ACTIONS.editChunk();   // double-click a chunk to open it
  $('#libList').ondblclick=()=>ACTIONS.editLibChunk();  // double-click a custom chunk to open it
  $('#dateSel').onchange=e=>loadDay(e.target.value);
  $$('.gtab').forEach(b=>b.onclick=()=>buildGallery(b.dataset.gtab));
  $('#gallerySearch').oninput=filterGallery;
  $('#chunkPickSearch').oninput=()=>{clearTimeout(_ckTimer);_ckTimer=setTimeout(renderChunkPickGrid,140);};
  $('#chunkPickSearch').onkeydown=e=>{if(e.key==='Escape')closeChunkPicker();};
  $('#chunkPickModal').onclick=e=>{if(e.target.id==='chunkPickModal')closeChunkPicker();};
  $('#gallerySearch').onkeydown=e=>{if(e.key==='Escape')hideGallery();};
  $('#galleryModal').onclick=e=>{if(e.target.id==='galleryModal')hideGallery();};
  $('#onion').onchange=e=>{state.onion=e.target.checked;draw();};
  $('#showGrid').onchange=e=>{state.showGrid=e.target.checked;draw();};
  $('#gameView').onchange=e=>{state.gameView=e.target.checked;draw();};
  $('#showRot').onchange=e=>{state.showRot=e.target.checked;draw();};
  $('#devMode').onchange=e=>setDevMode(e.target.checked);
  $('#devShoot').onchange=e=>setDevShoot(e.target.checked);
  $('#devPx').oninput=()=>devLive('px');   $('#devPxN').oninput=()=>devLive('pxN');
  $('#devPy').oninput=()=>devLive('py');   $('#devPyN').oninput=()=>devLive('pyN');
  $('#devOx').oninput=()=>devLive();       $('#devOy').oninput=()=>devLive();
  $('#devArt').onchange=devPreviewArt;
  $('#devArtBrowse').onclick=openArtPicker;
  $('#devArtPick').onclick=()=>devArtPick();
  $('#artSearch').oninput=()=>{clearTimeout(_artTimer);_artTimer=setTimeout(renderArtGrid,140);};
  $('#artSearch').onkeydown=e=>{if(e.key==='Escape')closeArtPicker();};
  $('#artModal').onclick=e=>{if(e.target.id==='artModal')closeArtPicker();};
  $$('input[name=layer]').forEach(r=>r.onchange=()=>{
    if(r.value==='grid2'&&!state.gridScope){          // grid-2 locked out -> stay on grid 1
      setStatus('grid 2 (overlap) is locked — tick “place on grids 1 + 2” to use it',true);
      selectLayerRadio(state.layer); return;}
    state.layer=r.value;
    $$('.layerrow').forEach(x=>x.classList.toggle('active',x.contains(r)));
    if(r.value==='enemy')setTool('enemy');            // the enemy layer places enemies
    else if(state.tool==='enemy')setTool('paint');    // a tile layer paints tiles
    hud();draw();});
  $('#gridScope').onchange=e=>{state.gridScope=e.target.checked;
    $('#grid2Row').classList.toggle('locked',!state.gridScope);
    if(!state.gridScope&&state.layer==='grid2'){selectLayerRadio('active');state.layer='active';
      if(state.tool==='enemy')setTool('paint');}
    setStatus(state.gridScope?'placing on grids 1 + 2':'grid 1 only');draw();};
  $('#grid2Row').classList.toggle('locked',!state.gridScope);
  $$('.vis').forEach(v=>v.onchange=()=>{state.layerVis[v.dataset.layer]=v.checked;draw();});
  window.addEventListener('resize',resizeCanvas);
  // also refit when the canvas area itself changes size for reasons other than
  // a window resize (e.g. the toolbar wraps to more/fewer rows when Power mode
  // toggles items, or a long status line appears) so nothing ever gets clipped.
  if(window.ResizeObserver){new ResizeObserver(resizeCanvas).observe($('#canvasWrap'));}
  window.addEventListener('keydown',onKey);
  window.addEventListener('keyup',e=>{if(e.code==='Space')state.spaceDown=false;});
}
function onKey(e){
  if(e.target.tagName==='INPUT')return;
  if(e.code==='Space'){state.spaceDown=true;e.preventDefault();return;}
  const mod=e.metaKey||e.ctrlKey;
  if(mod&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo();return;}
  if(mod&&e.key.toLowerCase()==='c'){copyRegion();return;}
  if(mod&&e.key.toLowerCase()==='v'){enterStamp();return;}   // show the paste ghost; click to place
  if(e.key==='Enter'){finishPath();return;}
  if(e.key==='Escape'){if(state.tool==='stamp'){exitStamp();return;}state.activePath=null;state.connStart=null;state.floating&&stampFloating();draw();return;}
  if(e.key===']'||e.key==='['){const hv=state.hover||[-1,-1];rotateCell(hv[0],hv[1],e.key===']'?90:-90);return;}
  const map={b:'paint',r:'rect',m:'select',i:'eyedrop',e:'erase',n:'enemy',p:'path',k:'conn'};
  if(map[e.key]) setTool(map[e.key]);
  if(e.key==='Delete'||e.key==='Backspace'){if(state.sel){snapshot();fillRect(state.sel,'-');draw();}}
}
let lastCell=[0,0];
const _hud=hud; hud=function(c,r){if(c!=null)lastCell=[c,r];_hud(c,r);};

async function boot(){
  cv=$('#cv');ctx=cv.getContext('2d');
  wire();attachCanvas();
  state.catalog=await api().get_catalog();
  await loadElementPanels();
  buildPalettes();
  await refreshThemes();
  applyState(await api().get_state());
  await refreshLibrary();
  await refreshCalendar();
  resizeCanvas();
  setStatus('ready — Load Game (.xapk) to begin');
}
window.addEventListener('pywebviewready',boot);
