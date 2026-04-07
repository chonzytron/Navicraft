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
    await api('/scan',{method:'POST'});
    toast('Scan started','info');
    pollScan();
  }catch(e){toast(e.message,'error')}
}

function pollScan(){
  if(scanPollTimer)clearInterval(scanPollTimer);
  scanPollTimer=setInterval(async()=>{
    try{
      const s=await api('/scan/status');
      if(s.scanning){
        $('#scanBar').classList.add('on');
        $('#scanMsg').textContent=s.message||'Scanning...';
        $('#scanPct').textContent=s.total?`${s.current}/${s.total}`:'';
      }else{
        $('#scanBar').classList.remove('on');
        if(s.phase==='idle'){
          clearInterval(scanPollTimer);
          scanPollTimer=null;
          loadStats();
        }
      }
    }catch{}
  },1500);
  setTimeout(()=>{if(scanPollTimer){clearInterval(scanPollTimer);scanPollTimer=null}},600000);
}

// --- Generate (SSE streaming) ---
function phaseLabel(phase){
  const labels={
    pass1:'Pass 1: Analyzing your prompt...',
    filtering:'Searching your library...',
    broadening:'Broadening search...',
    pass2:'Pass 2: AI is selecting songs...',
    matching:'Building playlist...',
    saving:'Saving to server...',
  };
  return labels[phase]||null;
}

async function generate(){
  const prompt=$('#prompt').value.trim();
  if(!prompt){toast('Enter a prompt','error');return}

  const maxSongs=activeMode==='songs'?parseInt($('#maxSongs').value)||30:100;
  const targetMin=activeMode==='duration'?parseInt($('#targetMin').value)||90:null;
  const autoCreate=!$('#previewToggle').classList.contains('on');

  $('#genBtn').disabled=true;
  $('#results').classList.remove('on');
  $('#loading').classList.add('on');
  $('#loadMsg').textContent='Starting...';
  $('#loadSub').textContent='';

  const body={prompt,max_songs:maxSongs,auto_create:autoCreate};
  if(targetMin)body.target_duration_min=targetMin;
  if(activeProvider)body.provider=activeProvider;
  if(activeServer)body.server=activeServer;

  let startTime=Date.now();
  let elapsed;
  const elapsedTimer=setInterval(()=>{
    elapsed=Math.floor((Date.now()-startTime)/1000);
    $('#loadSub').textContent=`${elapsed}s elapsed`;
  },1000);

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
            const label=phaseLabel(data.phase)||data.message||'Working...';
            $('#loadMsg').textContent=label;
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
    toast(e.message,'error');
  }finally{
    clearInterval(elapsedTimer);
    $('#genBtn').disabled=false;
    $('#loading').classList.remove('on');
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

// --- Export .m3u ---
async function exportM3U(){
  if(!currentResult||!currentResult.songs.length){toast('No songs to export','error');return}
  try{
    const resp=await fetch('/api/export/m3u',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:currentResult.name,songs:currentResult.songs}),
    });
    if(!resp.ok)throw new Error('Export failed');
    const blob=await resp.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;
    a.download=`${currentResult.name.replace(/[^a-zA-Z0-9 _-]/g,'')}.m3u`;
    document.body.appendChild(a);a.click();document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('M3U file downloaded','success');
  }catch(e){toast(`Export failed: ${e.message}`,'error')}
}

function reset(){
  $('#results').classList.remove('on');
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
        const dzPct=s.deezer_percent??0;
        const lfPct=s.lastfm_percent??0;
        const mbPct=s.musicbrainz_percent??0;
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

async function openConfig(){
  try{
    const cfg=await api('/config');
    for(const[key,elId]of Object.entries(cfgFieldMap)){
      const el=$('#'+elId);
      if(el)el.value=cfg[key]||'';
    }
  }catch(e){toast('Failed to load config','error');return}
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
