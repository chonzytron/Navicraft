// --- State ---
let currentResult = null;
let scanPollTimer = null;
let activeProvider = null;
let activeMode = 'songs';
let activeServer = null;
let availableServers = [];

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

// --- Utils ---
function toast(msg, type='info'){
  const t=$('#toast');t.textContent=msg;
  t.className=`toast toast-${type==='success'?'ok':type==='error'?'err':'info'}`;
  setTimeout(()=>t.classList.add('show'),10);
  setTimeout(()=>t.classList.remove('show'),3500);
}
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
function fmtDur(s){if(!s)return'';const m=Math.floor(s/60),sec=Math.floor(s%60);return`${m}:${String(sec).padStart(2,'0')}`}
function fmtTotal(s){const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h>0?`${h}h ${m}m`:`${m}m`}
function num(n){return(n||0).toLocaleString()}
function clampInput(el){el.value=el.value.replace(/[^0-9]/g,'');if(el.value&&+el.value>999)el.value=999;if(el.value&&+el.value<1)el.value=1}

async function api(path,opts={},timeoutMs=300000){
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{
    const r=await fetch(`/api${path}`,{headers:{'Content-Type':'application/json'},signal:ctrl.signal,...opts});
    if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.detail||`Error ${r.status}`)}
    return r.json();
  }catch(e){
    if(e.name==='AbortError')throw new Error('Request timed out. The AI may be slow — try again.');
    throw e;
  }finally{clearTimeout(timer)}
}

// --- Init ---
async function init(){
  loadStats();
  loadProviders();
  loadServers();
  pollScan();
  pollEnrichment();
  pollMoodScan();
}

// --- Media server status ---
async function loadServers(){
  try{
    const s=await api('/servers');
    availableServers=s.servers||[];

    // Show/hide status pills based on configured servers
    const hasND=availableServers.some(sv=>sv.id==='navidrome');
    const hasPlex=availableServers.some(sv=>sv.id==='plex');
    $('#ndBar').style.display=hasND?'':'none';
    $('#plexBar').style.display=hasPlex?'':'none';

    // Set default active server and test configured servers
    if(hasND){
      setServer('navidrome');
      testServer('navidrome');
      if(hasPlex) testServer('plex');
    }else if(hasPlex){
      setServer('plex');
      testServer('plex');
    }
  }catch(e){
    console.warn('Failed to load servers:',e);
  }
}

function setServer(id){
  activeServer=id;
}

async function testServer(id){
  if(id==='plex'){
    $('#plexDot').className='nd-dot';
    try{
      await api('/plex/test',{},10000);
      $('#plexDot').className='nd-dot ok';
    }catch(e){
      $('#plexDot').className='nd-dot err';
      toast(`Plex unreachable: ${e.message}`,'error');
    }
  }else{
    $('#ndDot').className='nd-dot';
    try{
      await api('/navidrome/test',{},10000);
      $('#ndDot').className='nd-dot ok';
    }catch(e){
      $('#ndDot').className='nd-dot err';
      toast(`Navidrome unreachable: ${e.message}`,'error');
    }
  }
}

// --- AI Provider selector ---
async function loadProviders(){
  try{
    const p=await api('/ai/providers');
    if(p.available.length<2)return; // only show when there's a real choice
    // populate model labels
    p.available.forEach(pv=>{
      if(pv.id==='claude'){
        const m=pv.model.replace('claude-','').replace(/-\d{8}$/,'');
        $('#pvdClaudeModel').textContent=m;
      }
      if(pv.id==='gemini'){
        const m=pv.model.replace('gemini-','');
        $('#pvdGeminiModel').textContent=m;
      }
    });
    setProvider(p.default);
    $('#pvdWrap').classList.add('on');
  }catch{}
}

function setProvider(id){
  activeProvider=id;
  $('#pvdClaude').classList.toggle('on',id==='claude');
  $('#pvdGemini').classList.toggle('on',id==='gemini');
}

async function loadStats(){
  try{
    const s=await api('/library/stats');
    $('#vSongs').textContent=num(s.song_count);
    $('#vAlbums').textContent=num(s.album_count);
    $('#vArtists').textContent=num(s.artist_count);
    $('#vDuration').textContent=fmtTotal(s.total_duration||0);
    $('#stats').style.display='';
  }catch(e){console.warn('Stats load failed:',e)}
}

function togglePreview(e){
  e.preventDefault();
  $('#previewToggle').classList.toggle('on');
}

function setMode(m){
  activeMode=m;
  $('#modeSongs').classList.toggle('on',m==='songs');
  $('#modeDuration').classList.toggle('on',m==='duration');
  $('#maxSongs').style.display=m==='songs'?'':'none';
  $('#targetMin').style.display=m==='duration'?'':'none';
}

// --- Unified activity log ---
let _logEntries=[];
let _logAutoHideTimer=null;
let _logLocked=false;

function _log(text,sub){
  _logEntries.push({text,sub:sub||null});
  _renderLog();
  if(!_logLocked)_resetLogTimer();
}

function _renderLog(){
  const el=$('#processLog');
  el.innerHTML=_logEntries.map(({text,sub})=>{
    if(sub)return`<div class="scan-log-line">${esc(text)}<br><span class="scan-log-sub">${esc(sub)}</span></div>`;
    return`<div class="scan-log-line">${esc(text)}</div>`;
  }).join('');
  el.classList.add('on');
}

function _clearLog(){
  _logEntries=[];
  _logLocked=false;
  const el=$('#processLog');
  el.onclick=null;
  el.innerHTML='';
  el.classList.remove('on','clickable');
  if(_logAutoHideTimer){clearTimeout(_logAutoHideTimer);_logAutoHideTimer=null;}
}

function _resetLogTimer(){
  if(_logAutoHideTimer)clearTimeout(_logAutoHideTimer);
  _logAutoHideTimer=setTimeout(()=>{_clearLog();_logAutoHideTimer=null;},30000);
}

// --- Scan ---
let _lastScanPhase=null;

async function triggerScan(){
  try{
    _clearLog();
    await api('/scan',{method:'POST'});
    pollScan();
  }catch(e){toast(e.message,'error')}
}

function pollScan(){
  if(scanPollTimer)clearInterval(scanPollTimer);
  let seenActive=false;
  scanPollTimer=setInterval(async()=>{
    try{
      const s=await api('/scan/status');
      if(s.phase!=='idle')seenActive=true;
      if(s.phase!==_lastScanPhase){
        _lastScanPhase=s.phase;
        if(s.phase==='discovering') _log('Discovering music files...');
        else if(s.phase==='scanning') _log('Scanning library...');
        else if(s.phase==='syncing') _log('Syncing media server IDs...');
        else if(s.phase==='enriching') _log('Fetching popularity data...');
        else if(s.phase==='cleanup'||s.phase==='health_check') _log('Running health check & cleanup...');
        else if(s.phase==='idle'&&seenActive){
          if(s.log&&s.log.length)s.log.forEach(e=>{
            const m=e.match(/^(.*?)(\s*\([^)]+\))$/);
            if(m)_log(m[1].trim(),m[2].trim());else _log(e);
          });
          loadStats();
        }
      }
      if(s.phase==='idle'&&(seenActive||!s.scanning)){
        clearInterval(scanPollTimer);
        scanPollTimer=null;
      }
    }catch{}
  },1500);
  setTimeout(()=>{if(scanPollTimer){clearInterval(scanPollTimer);scanPollTimer=null}},600000);
}

// --- Generate (SSE streaming) ---
function _fmtRange(lo,hi){
  if(lo==null&&hi==null)return'';
  return`${lo!=null?lo:'?'}–${hi!=null?hi:'?'}`;
}

function _joinList(arr,max=6){
  if(!arr||!arr.length)return'';
  if(arr.length<=max)return arr.join(', ');
  return`${arr.slice(0,max).join(', ')} +${arr.length-max} more`;
}

function renderProgressEvent(data){
  const phase=data.phase;
  const msg=data.message;
  switch(phase){
    case'pass1':
      _log('Pass 1 — analyzing your prompt');
      break;
    case'pass1_done':{
      const f=data.filters||{};
      const parts=[];
      if(f.genres&&f.genres.length)parts.push(`Genres: ${_joinList(f.genres,8)}`);
      if(f.artists&&f.artists.length)parts.push(`Artists: ${_joinList(f.artists,6)}`);
      if(f.moods&&f.moods.length)parts.push(`Moods: ${_joinList(f.moods,6)}`);
      const yr=_fmtRange(f.year_min,f.year_max);
      if(yr)parts.push(`Years: ${yr}`);
      const bpm=_fmtRange(f.bpm_min,f.bpm_max);
      if(bpm)parts.push(`BPM: ${bpm}`);
      if(f.keywords&&f.keywords.length)parts.push(`Keywords: ${_joinList(f.keywords,5)}`);
      if(f.exclude_genres&&f.exclude_genres.length)parts.push(`Not: ${_joinList(f.exclude_genres,4)}`);
      if(f.exclude_artists&&f.exclude_artists.length)parts.push(`Not artists: ${_joinList(f.exclude_artists,4)}`);
      _log('Pass 1 complete — intent extracted',parts.length?parts.join(' · '):'No specific filters — using open search');
      break;
    }
    case'filtering':
      _log('Filtering library with extracted criteria');
      break;
    case'filtering_done':{
      const n=data.candidates_found||0;
      const ua=data.unique_artists||0;
      const head=`Filtered to ${n.toLocaleString()} candidates across ${ua.toLocaleString()} artists`;
      const sample=data.sample_artists||[];
      const sub=sample.length?`Considering: ${_joinList(sample,12)}`:null;
      _log(head,sub);
      break;
    }
    case'broadening':
      _log(msg||'Broadening search — too few matches');
      break;
    case'pass2':
      _log(msg||'Pass 2 — AI is selecting songs from the candidate pool');
      break;
    case'pass2_done':{
      const n=data.selected_count||0;
      const name=data.playlist_name||'';
      _log(`Pass 2 complete — AI picked ${n} songs`,name?`"${name}"`:null);
      break;
    }
    case'matching':
      _log('Matching selections back to library tracks');
      break;
    case'saving':
      _log(msg||'Saving playlist to server');
      break;
    default:
      if(msg)_log(msg);
  }
}

async function generate(){
  const prompt=$('#prompt').value.trim();
  if(!prompt){toast('Enter a prompt','error');return}

  const maxSongs=activeMode==='songs'?parseInt($('#maxSongs').value)||30:100;
  const targetMin=activeMode==='duration'?parseInt($('#targetMin').value)||90:null;
  const autoCreate=!$('#previewToggle').classList.contains('on');

  _clearLog();
  _logLocked=true;
  $('#genBtn').disabled=true;
  $('#results').classList.remove('on');
  _log('Starting generation…');

  const body={prompt,max_songs:maxSongs,auto_create:autoCreate};
  if(targetMin)body.target_duration_min=targetMin;
  if(activeProvider)body.provider=activeProvider;
  if(activeServer)body.server=activeServer;

  try{
    const resp=await fetch('/api/generate',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });

    if(!resp.ok){
      const e=await resp.json().catch(()=>({}));
      throw new Error(e.detail||`Error ${resp.status}`);
    }

    const reader=resp.body.getReader();
    const decoder=new TextDecoder();
    let buffer='';
    let gotResult=false;
    let eventType=null; // persists across chunks so large payloads aren't lost

    while(true){
      const {done,value}=await reader.read();
      if(done)break;
      buffer+=decoder.decode(value,{stream:true});

      // Parse SSE events from buffer
      const lines=buffer.split('\n');
      buffer=lines.pop(); // keep incomplete line in buffer
      for(const line of lines){
        if(line.startsWith('event: ')){
          eventType=line.slice(7).trim();
        }else if(line.startsWith('data: ')&&eventType){
          const data=JSON.parse(line.slice(6));
          if(eventType==='progress'){
            renderProgressEvent(data);
          }else if(eventType==='result'){
            currentResult=data;
            gotResult=true;
          }else if(eventType==='error'){
            throw new Error(data.detail||'Generation failed');
          }
          eventType=null;
        }
      }
    }

    if(gotResult){
      renderResults(currentResult);
    }else{
      throw new Error('No result received from server');
    }
  }catch(e){
    _log(`Error: ${e.message}`);
    toast(e.message,'error');
  }finally{
    $('#genBtn').disabled=false;
    _logLocked=false;
    // Allow the user to click to dismiss once generation has finished
    const el=$('#processLog');
    if(el.classList.contains('on')){
      setTimeout(()=>{ el.onclick=_clearLog; el.classList.add('clickable'); },500);
    }
  }
}

function renderResults(r){
  $('#plName').textContent=r.name;
  $('#plDesc').textContent=r.description;
  $('#plMatch').textContent=`${r.total_matched} songs`;
  $('#plDur').textContent=`${fmtTotal(r.total_duration)}`;
  $('#plCandidates').textContent=`from ${r.candidates_found} candidates`;

  const list=$('#songList');list.innerHTML='';
  r.songs.forEach((s,i)=>{
    const li=document.createElement('li');li.className='song';
    li.innerHTML=`
      <span class="s-num">${i+1}</span>
      <div class="s-info">
        <div class="s-title">${esc(s.title)}</div>
        <div class="s-artist">${esc(s.artist)}${s.year?' · '+s.year:''}</div>
      </div>
      <div class="s-album">${esc(s.album)}</div>
      <div class="s-dur">${fmtDur(s.duration)}</div>
    `;
    list.appendChild(li);
  });

  // Show save buttons for available servers
  const hasND=availableServers.some(sv=>sv.id==='navidrome');
  const hasPlex=availableServers.some(sv=>sv.id==='plex');
  $('#saveNavidromeBtn').style.display=hasND?'':'none';
  $('#savePlexBtn').style.display=hasPlex?'':'none';
  $('#savedPills').style.display='none';
  $('#savedPills').innerHTML='';

  if(r.created){
    // Auto-saved to the server that was used during generation
    const label=activeServer==='plex'?'Plex':'Navidrome';
    $('#savedPills').innerHTML=`<div class="saved-pill">&#10003; Saved to ${esc(label)}</div>`;
    $('#savedPills').style.display='';
    if(activeServer==='navidrome')$('#saveNavidromeBtn').style.display='none';
    if(activeServer==='plex')$('#savePlexBtn').style.display='none';
  }

  $('#results').classList.add('on');
  $('#results').scrollIntoView({behavior:'smooth',block:'start'});
}

// --- Save ---
async function saveToServer(server){
  if(!currentResult||!currentResult.songs.length)return;
  const idField=server==='plex'?'plex_id':'navidrome_id';
  const ids=currentResult.songs.map(s=>s[idField]).filter(Boolean);
  const label=server==='plex'?'Plex':'Navidrome';
  if(!ids.length){toast(`No ${label} IDs — verify server connection and trigger a scan`,'error');return}
  try{
    await api('/playlists',{method:'POST',body:JSON.stringify({name:currentResult.name,song_ids:ids,server:server})});
    toast(`Playlist saved to ${label}!`,'success');
    // Hide the button for this server and show saved pill
    const btn=server==='plex'?$('#savePlexBtn'):$('#saveNavidromeBtn');
    btn.style.display='none';
    const pill=document.createElement('div');
    pill.className='saved-pill';
    pill.innerHTML=`&#10003; Saved to ${esc(label)}`;
    $('#savedPills').appendChild(pill);
    $('#savedPills').style.display='';
  }catch(e){toast(`Save failed: ${e.message}`,'error')}
}

function reset(){
  $('#results').classList.remove('on');
  _clearLog();
  currentResult=null;
  $('#prompt').value='';
  $('#prompt').focus();
  window.scrollTo({top:0,behavior:'smooth'});
}

// --- Enrichment progress ---
let _enrichWasIncomplete=false;
let _enrichTimer=null;
let _enrichLogEntry=null;

function _upsertLogEntry(ref,text,sub){
  // Find existing entry by identity so concurrent pollers can't stomp each
  // other's slots when the log is cleared and re-populated out of order.
  const idx=ref?_logEntries.indexOf(ref):-1;
  if(idx>=0){
    ref.text=text;
    ref.sub=sub||null;
    _renderLog();
    if(!_logLocked)_resetLogTimer();
    return ref;
  }
  const entry={text,sub:sub||null};
  _logEntries.push(entry);
  _renderLog();
  $('#processLog').classList.add('on');
  if(!_logLocked)_resetLogTimer();
  return entry;
}

function pollEnrichment(){
  if(_enrichTimer)clearInterval(_enrichTimer);
  const check=async()=>{
    try{
      const s=await api('/popularity/status');
      const incomplete=s.remaining>0||s.deezer_missing>0||s.lastfm_missing>0||s.musicbrainz_missing>0;
      const dzPct=Math.round(s.deezer_percent??0);
      const lfPct=Math.round(s.lastfm_percent??0);
      const mbPct=Math.round(s.musicbrainz_percent??0);
      const sub=`Deezer: ${dzPct}% · Last.fm: ${lfPct}% · MBrainz: ${mbPct}%`;
      if(incomplete){
        _enrichLogEntry=_upsertLogEntry(_enrichLogEntry,'Popularity enrichment in progress',sub);
        _enrichWasIncomplete=true;
      }else if(_enrichWasIncomplete){
        _enrichLogEntry=_upsertLogEntry(_enrichLogEntry,'Popularity enrichment complete',sub);
        _enrichWasIncomplete=false;
        _enrichLogEntry=null;
      }
    }catch{}
  };
  check();
  _enrichTimer=setInterval(check,15000);
}

// --- Mood scan progress ---
let _moodTimer=null;
let _moodLogEntry=null;

function _moodSub(s){
  const pct=Math.round(s.percent??0);
  const overall=`${pct}% (${s.scanned??0}/${s.total??0} tracks)`;
  return s.batch_total>0?`${overall} · batch ${s.batch_current}/${s.batch_total}`:overall;
}

function pollMoodScan(){
  if(_moodTimer)clearInterval(_moodTimer);
  const check=async()=>{
    try{
      const s=await api('/mood/status');
      const total=s.total??0;
      const scanned=s.scanned??0;
      if(total<=0)return;
      const incomplete=scanned<total;
      const pct=Math.round(s.percent??0);
      const sub=_moodSub(s);
      let text;
      if(s.running){
        // A batch is actively processing (manual trigger, continuous, or scheduled).
        text='Mood enrichment in progress';
      }else if(incomplete){
        // Not running right now. Distinguish "waiting to resume" from "paused".
        // continuous=user started play; enabled=scheduled window is armed.
        const waiting=s.continuous||s.enabled;
        text=`Mood enrichment ${waiting?'on standby':'paused'} — ${pct}%`;
      }else{
        text='Mood enrichment complete';
      }
      _moodLogEntry=_upsertLogEntry(_moodLogEntry,text,sub);
    }catch{}
  };
  check();
  _moodTimer=setInterval(check,15000);
}

async function triggerMoodScanFromConfig(){
  const btn=$('#cfgMoodScanBtn');
  const isRunning=btn.dataset.running==='true';
  btn.disabled=true;
  try{
    if(isRunning){
      await api('/mood/continuous',{method:'POST',body:JSON.stringify({action:'stop'})});
      _setMoodScanBtn(false,null);
    }else{
      const r=await api('/mood/scan',{method:'POST'});
      if(r.status==='complete'){
        toast('All tracks already scanned','info');
      }else if(r.status==='already_running'){
        _setMoodScanBtn(true,null);
      }else{
        _setMoodScanBtn(true,null);
      }
    }
    // Reset poller so it picks up new state immediately
    _moodLogEntry=null;
    if(_moodTimer)clearInterval(_moodTimer);
    pollMoodScan();
  }catch(e){toast(`Mood scan: ${e.message}`,'error')}
  finally{btn.disabled=false}
}

function _setMoodScanBtn(running,remaining){
  const btn=$('#cfgMoodScanBtn');
  const lbl=$('#cfgMoodScanStatus');
  if(!btn)return;
  btn.dataset.running=running?'true':'false';
  btn.textContent=running?'Stop':'Scan Now';
  if(lbl)lbl.textContent=running?'Scanning…':remaining!=null?`Manual Scan (${remaining} remaining)`:'Manual Scan';
}

// --- Config modal ---
const cfgFieldMap={
  navidrome_url:'cfgNavidromeUrl',
  navidrome_user:'cfgNavidromeUser',
  navidrome_password:'cfgNavidromePassword',
  plex_url:'cfgPlexUrl',
  plex_token:'cfgPlexToken',
  claude_api_key:'cfgClaudeApiKey',
  claude_model:'cfgClaudeModel',
  gemini_api_key:'cfgGeminiApiKey',
  gemini_model:'cfgGeminiModel',
  lastfm_api_key:'cfgLastfmApiKey',
  scan_interval_hours:'cfgScanIntervalHours',
};
// Select-based fields (use .value like inputs but are <select> elements)
const cfgSelectMap={
  timezone:'cfgTimezone',
  mood_scan_from_hour:'cfgMoodScanFromHour',
  mood_scan_to_hour:'cfgMoodScanToHour',
};
// Numeric input fields (not text, use .value)
const cfgNumberMap={
  navicraft_watcher_interval:'cfgWatcherInterval',
};
// Toggle fields need special handling (not text inputs)
const cfgToggleMap={
  mood_scan_enabled:'cfgMoodScanEnabled',
  navicraft_watcher_enabled:'cfgWatcherEnabled',
};

function _populateHourSelects(){
  ['cfgMoodScanFromHour','cfgMoodScanToHour'].forEach(id=>{
    const sel=$('#'+id);
    if(!sel||sel.options.length>0)return;
    for(let h=0;h<24;h++){
      const opt=document.createElement('option');
      opt.value=String(h);
      opt.textContent=`${String(h).padStart(2,'0')}:00`;
      sel.appendChild(opt);
    }
  });
}

function _syncMoodFields(){
  const on=$('#cfgMoodScanEnabled').classList.contains('on');
  $('#moodScheduleFields').classList.toggle('disabled',!on);
}

function _syncWatcherFields(){
  const on=$('#cfgWatcherEnabled').classList.contains('on');
  $('#watcherFields').classList.toggle('disabled',!on);
}

function toggleWatcherHelp(e){
  e.preventDefault();
  e.stopPropagation();
  const help=$('#watcherHelp');
  const btn=e.currentTarget;
  const open=help.hasAttribute('hidden');
  if(open){help.removeAttribute('hidden');btn.setAttribute('aria-expanded','true')}
  else{help.setAttribute('hidden','');btn.setAttribute('aria-expanded','false')}
}

async function openConfig(){
  _populateHourSelects();
  try{
    const cfg=await api('/config');
    for(const[key,elId]of Object.entries(cfgFieldMap)){
      const el=$('#'+elId);
      if(el)el.value=cfg[key]||'';
    }
    for(const[key,elId]of Object.entries(cfgSelectMap)){
      const el=$('#'+elId);
      if(el)el.value=cfg[key]||'';
    }
    for(const[key,elId]of Object.entries(cfgNumberMap)){
      const el=$('#'+elId);
      if(el)el.value=cfg[key]||'';
    }
    for(const[key,elId]of Object.entries(cfgToggleMap)){
      const el=$('#'+elId);
      if(el)el.classList.toggle('on',cfg[key]==='true');
    }
  }catch(e){toast('Failed to load config','error');return}
  _syncMoodFields();
  _syncWatcherFields();
  // Populate mood scan button state
  try{
    const ms=await api('/mood/status');
    _setMoodScanBtn(ms.running,ms.running?null:ms.remaining);
  }catch{}
  $('#cfgOverlay').classList.add('on');
  document.body.classList.add('modal-open');
}

function closeConfig(){
  $('#cfgOverlay').classList.remove('on');
  document.body.classList.remove('modal-open');
}

async function saveConfig(){
  const body={};
  for(const[key,elId]of Object.entries(cfgFieldMap)){
    const el=$('#'+elId);
    if(el)body[key]=el.value;
  }
  for(const[key,elId]of Object.entries(cfgSelectMap)){
    const el=$('#'+elId);
    if(el)body[key]=el.value;
  }
  for(const[key,elId]of Object.entries(cfgNumberMap)){
    const el=$('#'+elId);
    if(el)body[key]=el.value;
  }
  for(const[key,elId]of Object.entries(cfgToggleMap)){
    const el=$('#'+elId);
    if(el)body[key]=el.classList.contains('on')?'true':'false';
  }
  try{
    await api('/config',{method:'PUT',body:JSON.stringify(body)});
    toast('Settings saved','success');
    closeConfig();
    // Reload server status and providers with new config
    loadServers();
    loadProviders();
  }catch(e){toast(`Save failed: ${e.message}`,'error')}
}

// Theme toggle (dark is default; light preference persists in localStorage)
function toggleTheme(){
  const isLight=document.documentElement.getAttribute('data-theme')==='light';
  if(isLight){
    document.documentElement.removeAttribute('data-theme');
    try{localStorage.setItem('navicraft-theme','dark')}catch(e){}
  }else{
    document.documentElement.setAttribute('data-theme','light');
    try{localStorage.setItem('navicraft-theme','light')}catch(e){}
  }
}

// Close modal on Escape
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeConfig()});

// Enter = generate (Shift+Enter = newline)
$('#prompt').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();generate()}
});

init();
