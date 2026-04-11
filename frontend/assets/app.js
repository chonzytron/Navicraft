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

// --- Scan ---
async function triggerScan(){
  try{
    _clearScanLog();
    await api('/scan',{method:'POST'});
    toast('Scan started','info');
    pollScan(true);
  }catch(e){toast(e.message,'error')}
}

function pollScan(showLog=false){
  if(scanPollTimer)clearInterval(scanPollTimer);
  let seenActive=false;
  scanPollTimer=setInterval(async()=>{
    try{
      const s=await api('/scan/status');
      if(s.phase!=='idle')seenActive=true;
      if(s.phase==='idle'&&(seenActive||!s.scanning)){
        clearInterval(scanPollTimer);
        scanPollTimer=null;
        $('#scanBar').classList.remove('on');
        if(seenActive)loadStats();
        if(showLog&&s.log&&s.log.length) _showScanLog(s.log);
      }else if(s.phase!=='idle'){
        $('#scanBar').classList.add('on');
        $('#scanMsg').textContent=s.message||'Scanning...';
        $('#scanPct').textContent=s.total?`${s.current}/${s.total}`:'';
      }
    }catch{}
  },1500);
  setTimeout(()=>{if(scanPollTimer){clearInterval(scanPollTimer);scanPollTimer=null}},600000);
}

let scanLogTimer=null;

function _showScanLog(entries){
  const el=$('#scanLog');
  el.onclick=null;
  el.innerHTML=entries.map(l=>{
    const m=l.match(/^(.*?)(\s*\([^)]+\))$/);
    if(m)return`<div class="scan-log-line">${esc(m[1].trim())}<br><span class="scan-log-sub">${esc(m[2].trim())}</span></div>`;
    return`<div class="scan-log-line">${esc(l)}</div>`;
  }).join('');
  el.classList.add('on');
  if(scanLogTimer)clearTimeout(scanLogTimer);
  scanLogTimer=setTimeout(()=>{_clearScanLog();scanLogTimer=null},30000);
  setTimeout(()=>{ el.onclick=_clearScanLog; },500);
}

function _clearScanLog(){
  const el=$('#scanLog');
  el.onclick=null;
  el.innerHTML='';
  el.classList.remove('on');
  if(scanLogTimer){clearTimeout(scanLogTimer);scanLogTimer=null}
}

// --- Generate (SSE streaming) ---
let processLogEntries=[];
let processLogLive=false;

function _clearProcessLog(){
  processLogEntries=[];
  const el=$('#processLog');
  el.onclick=null;
  el.innerHTML='';
  el.classList.remove('on','clickable');
  processLogLive=false;
}

function _addProcessLog(text,sub){
  processLogEntries.push({text,sub:sub||null});
  _renderProcessLog();
}

function _renderProcessLog(){
  const el=$('#processLog');
  el.innerHTML=processLogEntries.map(({text,sub})=>{
    if(sub){
      return `<div class="scan-log-line">${esc(text)}<br><span class="scan-log-sub">${esc(sub)}</span></div>`;
    }
    return `<div class="scan-log-line">${esc(text)}</div>`;
  }).join('');
  el.classList.add('on');
}

function _fmtRange(lo,hi){
  if(lo==null&&hi==null)return'';
  return `${lo!=null?lo:'?'}–${hi!=null?hi:'?'}`;
}

function _joinList(arr,max=6){
  if(!arr||!arr.length)return'';
  if(arr.length<=max)return arr.join(', ');
  return `${arr.slice(0,max).join(', ')} +${arr.length-max} more`;
}

function renderProgressEvent(data){
  const phase=data.phase;
  const msg=data.message;
  switch(phase){
    case'pass1':
      _addProcessLog('Pass 1 — analyzing your prompt');
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
      _addProcessLog('Pass 1 complete — intent extracted',parts.length?parts.join(' · '):'No specific filters — using open search');
      break;
    }
    case'filtering':
      _addProcessLog('Filtering library with extracted criteria');
      break;
    case'filtering_done':{
      const n=data.candidates_found||0;
      const ua=data.unique_artists||0;
      const head=`Filtered to ${n.toLocaleString()} candidates across ${ua.toLocaleString()} artists`;
      const sample=data.sample_artists||[];
      const sub=sample.length?`Considering: ${_joinList(sample,12)}`:null;
      _addProcessLog(head,sub);
      break;
    }
    case'broadening':
      _addProcessLog(msg||'Broadening search — too few matches');
      break;
    case'pass2':
      _addProcessLog(msg||'Pass 2 — AI is selecting songs from the candidate pool');
      break;
    case'pass2_done':{
      const n=data.selected_count||0;
      const name=data.playlist_name||'';
      _addProcessLog(`Pass 2 complete — AI picked ${n} songs`,name?`"${name}"`:null);
      break;
    }
    case'matching':
      _addProcessLog('Matching selections back to library tracks');
      break;
    case'saving':
      _addProcessLog(msg||'Saving playlist to server');
      break;
    default:
      if(msg)_addProcessLog(msg);
  }
}

async function generate(){
  const prompt=$('#prompt').value.trim();
  if(!prompt){toast('Enter a prompt','error');return}

  const maxSongs=activeMode==='songs'?parseInt($('#maxSongs').value)||30:100;
  const targetMin=activeMode==='duration'?parseInt($('#targetMin').value)||90:null;
  const autoCreate=!$('#previewToggle').classList.contains('on');

  _clearScanLog();
  _clearProcessLog();
  processLogLive=true;
  $('#genBtn').disabled=true;
  $('#results').classList.remove('on');
  _addProcessLog('Starting generation…');

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
    _addProcessLog(`Error: ${e.message}`);
    toast(e.message,'error');
  }finally{
    $('#genBtn').disabled=false;
    processLogLive=false;
    // Allow the user to click to dismiss once generation has finished
    const el=$('#processLog');
    if(el.classList.contains('on')){
      setTimeout(()=>{ el.onclick=_clearProcessLog; el.classList.add('clickable'); },500);
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
  _clearProcessLog();
  currentResult=null;
  $('#prompt').value='';
  $('#prompt').focus();
  window.scrollTo({top:0,behavior:'smooth'});
}


// --- Enrichment progress ---
let enrichTimer=null;
function pollEnrichment(){
  if(enrichTimer)clearInterval(enrichTimer);
  const check=async()=>{
    try{
      const s=await api('/popularity/status');
      const incomplete=s.remaining>0||s.deezer_missing>0||s.lastfm_missing>0||s.musicbrainz_missing>0;
      if(incomplete){
        $('#enrichBar').classList.add('on');
        const dzPct=Math.round(s.deezer_percent??0);
        const lfPct=Math.round(s.lastfm_percent??0);
        const mbPct=Math.round(s.musicbrainz_percent??0);
        $('#enrichFillDeezer').style.width=`${dzPct}%`;
        $('#enrichTextDeezer').textContent=`${dzPct}%`;
        $('#enrichFillLastfm').style.width=`${lfPct}%`;
        $('#enrichTextLastfm').textContent=`${lfPct}%`;
        $('#enrichFillMusicbrainz').style.width=`${mbPct}%`;
        $('#enrichTextMusicbrainz').textContent=`${mbPct}%`;
      }else{
        $('#enrichBar').classList.remove('on');
      }
    }catch{}
  };
  check();
  enrichTimer=setInterval(check,15000);
}

// --- Mood scan progress ---
let moodTimer=null;
function _updateMoodUI(s){
  const pct=Math.round(s.percent??0);
  $('#moodBar').classList.add('on');
  $('#moodFill').style.width=`${pct}%`;
  $('#moodText').textContent=`${pct}%`;
  $('#moodLabel').classList.toggle('active',!!s.continuous);
  $('#moodLabel').title=s.continuous?'Click to pause':'Click to start continuous scanning';
}

function _syncMoodFields(){
  const on=$('#cfgMoodScanEnabled').classList.contains('on');
  $('#moodScheduleFields').classList.toggle('disabled',!on);
}

function pollMoodScan(){
  if(moodTimer)clearInterval(moodTimer);
  const check=async()=>{
    try{
      const s=await api('/mood/status');
      _updateMoodUI(s);
    }catch{}
  };
  check();
  moodTimer=setInterval(check,15000);
}

async function toggleContinuousMood(){
  const isPlaying=$('#moodLabel').classList.contains('active');
  const action=isPlaying?'stop':'start';
  // Immediate visual feedback
  $('#moodLabel').classList.toggle('active',!isPlaying);
  try{
    await api('/mood/continuous',{method:'POST',body:JSON.stringify({action})});
    toast(action==='start'?'Continuous mood scan started':'Continuous mood scan paused','info');
    // Switch to fast polling
    if(moodTimer)clearInterval(moodTimer);
    const fastPoll=setInterval(async()=>{
      try{
        const s=await api('/mood/status');
        _updateMoodUI(s);
        if(!s.running&&!s.continuous){
          clearInterval(fastPoll);
          pollMoodScan();
        }
      }catch{clearInterval(fastPoll);pollMoodScan()}
    },3000);
  }catch(e){
    // Revert on failure
    $('#moodLabel').classList.toggle('active',isPlaying);
    toast(`Failed: ${e.message}`,'error');
  }
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
// Toggle fields need special handling (not text inputs)
const cfgToggleMap={
  mood_scan_enabled:'cfgMoodScanEnabled',
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
    for(const[key,elId]of Object.entries(cfgToggleMap)){
      const el=$('#'+elId);
      if(el)el.classList.toggle('on',cfg[key]==='true');
    }
  }catch(e){toast('Failed to load config','error');return}
  _syncMoodFields();
  $('#cfgOverlay').classList.add('on');
}

function closeConfig(){
  $('#cfgOverlay').classList.remove('on');
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

// Close modal on Escape
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeConfig()});

// Enter = generate (Shift+Enter = newline)
$('#prompt').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();generate()}
});

init();
