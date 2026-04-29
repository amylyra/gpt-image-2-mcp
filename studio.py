"""HTTP endpoints (outside MCP):

  - GET  /upload         — minimal HTML drag-drop form
  - POST /upload         — accept image, return {asset_id, image_url, expires_at, ...}
  - GET  /images/{asset_id}?expires=...&sig=...  — HMAC-verified image fetch
  - GET  /app            — Studio UI: tool tabs, input forms, output preview (v1.0)
  - POST /api/{tool}     — JSON wrapper around any registered MCP tool (v1.0)

The /api/{tool} endpoint is just a thin REST shim around the registered tool fns
so the Studio HTML can drive them from `fetch`. It honors MCP_BEARER_TOKEN.

Tools now return `[Image, ..., render_md_str, json_str]` (FastMCP block list);
the shim unpacks the trailing JSON for clean REST output via `unpack_tool_result`.
"""

from __future__ import annotations

from io import BytesIO

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from image_io import normalize_image
from security import (
    bearer_required,
    check_bearer,
    check_rate_limit,
    client_ip_from_request,
    verify_url,
)
from storage import (
    DEFAULT_URL_TTL,
    cloud_storage_configured,
    has_asset,
    make_signed_url,
    resolve_asset,
    store_asset,
    upload_to_cloud,
)
from tools import unpack_tool_result


_STUDIO_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>gpt-image-2-mcp · studio</title>
<style>
  *{box-sizing:border-box;}
  body{font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;margin:0;color:#0f1320;background:#f7f8fb;}
  header{padding:14px 24px;border-bottom:1px solid #e6e8f0;background:#fff;display:flex;align-items:center;gap:14px;}
  header h1{font-size:15px;margin:0;font-weight:600;}
  header .meta{font-size:12px;color:#5b6478;}
  main{display:grid;grid-template-columns:340px 1fr;gap:0;height:calc(100vh - 49px);}
  nav{border-right:1px solid #e6e8f0;background:#fff;overflow-y:auto;}
  nav .tab{display:block;width:100%;text-align:left;padding:10px 16px;border:0;border-left:3px solid transparent;background:#fff;cursor:pointer;font:inherit;font-size:13px;color:#0f1320;}
  nav .tab:hover{background:#f7f8fb;}
  nav .tab.on{background:#eef2ff;border-left-color:#6366f1;font-weight:600;}
  nav .tab .desc{display:block;font-size:11px;color:#5b6478;font-weight:400;margin-top:2px;}
  section{padding:20px 28px;overflow-y:auto;}
  .panel{display:none;}
  .panel.on{display:block;}
  .panel h2{font-size:16px;margin:0 0 4px;}
  .panel .hint{color:#5b6478;font-size:12px;margin:0 0 18px;line-height:1.5;}
  label{display:block;font-size:12px;color:#5b6478;margin:10px 0 4px;font-weight:500;}
  input[type=text],input[type=number],textarea,select{width:100%;padding:8px 10px;border:1px solid #d6d9e3;border-radius:6px;font:inherit;font-size:13px;background:#fff;}
  textarea{min-height:80px;resize:vertical;font-family:inherit;}
  input[type=text]:focus,textarea:focus,select:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 2px #eef2ff;}
  .row{display:flex;gap:10px;}
  .row > *{flex:1;}
  button.go{margin-top:14px;background:#0f1320;color:#fff;border:0;padding:10px 16px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;}
  button.go:hover{background:#1f2436;}
  button.go:disabled{background:#9ca3af;cursor:not-allowed;}
  .drop{border:2px dashed #cbd0dc;border-radius:10px;padding:18px;text-align:center;cursor:pointer;background:#fafbfd;font-size:12px;color:#5b6478;}
  .drop.over{background:#eef2ff;border-color:#6366f1;}
  .drop input[type=file]{display:none;}
  .out{margin-top:24px;padding:16px;border:1px solid #e6e8f0;border-radius:8px;background:#fff;}
  .out img{max-width:100%;border-radius:6px;display:block;margin-bottom:10px;}
  .out pre{background:#0f1320;color:#e6e8f0;padding:12px;border-radius:6px;overflow:auto;font-size:11px;margin:0;line-height:1.5;}
  .out .err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca;padding:10px 12px;border-radius:6px;font-size:13px;}
  .history{margin-top:18px;}
  .history h3{font-size:12px;color:#5b6478;font-weight:500;margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em;}
  .history .chip{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11px;padding:4px 8px;border:1px solid #e6e8f0;border-radius:4px;background:#fff;cursor:pointer;margin:2px 4px 2px 0;color:#0f1320;}
  .history .chip:hover{background:#eef2ff;border-color:#6366f1;}
  .pill{display:inline-block;font-size:10px;padding:1px 6px;border-radius:10px;background:#dbeafe;color:#1e40af;margin-left:6px;font-weight:500;letter-spacing:.02em;}
  .pill.det{background:#dcfce7;color:#166534;}
  .pill.ai{background:#fef3c7;color:#92400e;}
</style></head><body>
<header>
  <h1>gpt-image-2 Studio</h1>
  <span class="meta">8 tools · deterministic-by-default</span>
  <span style="margin-left:auto;display:flex;gap:6px;align-items:center;">
    <label style="font-size:11px;color:#5b6478;margin:0;">Bearer:</label>
    <input type="password" id="bearer" placeholder="optional — leave blank if MCP_BEARER_TOKEN unset"
           style="width:240px;padding:4px 8px;font-size:11px;border:1px solid #d6d9e3;border-radius:4px;font-family:ui-monospace,Menlo,monospace;">
  </span>
</header>
<main>
  <nav id="nav"></nav>
  <section id="panels"></section>
</main>
<script>
// Single source of truth — used by every aspect_ratio dropdown. Order: square,
// portrait, landscape, social, infographic. '' means "let server pick default".
const ASPECTS_FULL = ['1:1','4:5','5:4','16:9','9:16','3:2','2:3','social-square','social-vertical','social-horizontal','infographic-portrait','infographic-landscape'];
const ASPECTS_WITH_BLANK = [''].concat(ASPECTS_FULL);

// Layout → default content JSON. Swapped into the textarea when layout changes.
const INFOGRAPHIC_DEFAULTS = {
  'square-list': '{"title":"Spring Sale","subtitle":"Limited-time savings","bullets":["30% off Cleansers","20% off Serums","Free shipping over $50"],"footer":"proven.com/spring"}',
  'portrait-stat': '{"stat":"47%","subtitle":"of users prefer X","footer":"Q1 2026, n=1,200"}',
  'landscape-comparison': '{"title":"Before vs After","left":["• Manual exports","• 4 hour cycle","• 12% error rate"],"right":["• Automated","• 8 minute cycle","• <1% error rate"]}'
};

const TOOLS = [
  {id:'upload_image', name:'Upload', desc:'Get an asset_id from a URL or file', mode:'det',
   fields:[{k:'image',type:'text',hint:'http(s) URL — drag-drop above for files'}]},
  {id:'generate_image', name:'Generate', desc:'Text → image (gpt-image-2)', mode:'ai',
   fields:[{k:'prompt',type:'textarea',req:true},
           {k:'aspect_ratio',type:'select',opts:ASPECTS_FULL,default:'1:1'},
           {k:'quality',type:'select',opts:['low','medium','high'],default:'medium'},
           {k:'count',type:'number',default:1,min:1,max:4},
           {k:'output_format',type:'select',opts:['png','jpeg','webp'],default:'png'}]},
  {id:'edit_image', name:'Edit', desc:'Edit 1–16 sources, optional mask', mode:'ai',
   fields:[{k:'image',type:'text',req:true,hint:'asset_id, URL, or comma-separated list'},
           {k:'prompt',type:'textarea',req:true},
           {k:'mask',type:'text',hint:'optional mask asset_id/URL'},
           {k:'aspect_ratio',type:'select',opts:ASPECTS_WITH_BLANK,default:''},
           {k:'quality',type:'select',opts:['low','medium','high'],default:'medium'},
           {k:'count',type:'number',default:1,min:1,max:4},
           {k:'output_format',type:'select',opts:['png','jpeg','webp'],default:'png'}]},
  {id:'add_text_overlay', name:'Add Text Overlay', desc:'Headline / badge / caption / watermark', mode:'det',
   fields:[{k:'image',type:'text',req:true},
           {k:'text',type:'text',req:true},
           {k:'position',type:'select',opts:['top','top-left','top-right','top-center','center','bottom','bottom-left','bottom-right','bottom-center'],default:'bottom-center'},
           {k:'style',type:'select',opts:['headline','badge','caption','watermark'],default:'headline'},
           {k:'color',type:'text',default:'#ffffff'}]},
  {id:'add_link_cta', name:'Add Link / CTA', desc:'CTA + URL — exact, character-for-character', mode:'det',
   fields:[{k:'image',type:'text',req:true},
           {k:'cta_text',type:'text',req:true,default:'Shop Now'},
           {k:'url_text',type:'text',req:true,default:'proven.com/spring'},
           {k:'position',type:'select',opts:['top','top-center','center','bottom','bottom-center'],default:'bottom-center'},
           {k:'style',type:'select',opts:['button','bar','pill','underlined-link'],default:'button'},
           {k:'color_scheme',type:'select',opts:['white-on-coral','white-on-black','black-on-white','coral-on-white'],default:'white-on-coral'}]},
  {id:'composite_images', name:'Composite', desc:'Combine 2–16 sources into one new image', mode:'ai',
   fields:[{k:'images',type:'text',req:true,hint:'comma-separated asset_ids or URLs (2–16)'},
           {k:'prompt',type:'textarea',req:true},
           {k:'aspect_ratio',type:'select',opts:ASPECTS_FULL,default:'1:1'},
           {k:'quality',type:'select',opts:['low','medium','high'],default:'medium'},
           {k:'count',type:'number',default:1,min:1,max:4},
           {k:'output_format',type:'select',opts:['png','jpeg','webp'],default:'png'}]},
  {id:'create_infographic', name:'Infographic', desc:'AI background + deterministic text', mode:'det',
   fields:[{k:'background_prompt',type:'textarea',req:true,hint:'background visual style — text is composited on top'},
           {k:'layout',type:'select',opts:['square-list','portrait-stat','landscape-comparison'],default:'square-list',onchange:'syncInfographicContent'},
           {k:'content',type:'textarea',req:true,default:INFOGRAPHIC_DEFAULTS['square-list'],hint:'JSON matching the layout schema (auto-swapped when layout changes)'},
           {k:'color',type:'text',default:'#0f1320'},
           {k:'quality',type:'select',opts:['low','medium','high'],default:'medium'}]},
  {id:'refine_image', name:'Refine', desc:'Iterate via Responses API', mode:'ai',
   fields:[{k:'prompt',type:'textarea',req:true},
           {k:'image',type:'text',hint:'first turn only — leave blank when chaining'},
           {k:'previous_response_id',type:'text',hint:'chain — mutually exclusive with image_generation_call_id and image'},
           {k:'image_generation_call_id',type:'text',hint:'chain handle from prior refine'},
           {k:'reasoning_effort',type:'select',opts:['low','medium','high'],default:'low'}]}
];
const COERCE = ['count','number'];
const JSON_FIELDS = ['content','bbox'];
const LIST_FIELDS = ['images','image']; // 'image' only for edit_image — server tolerates list
const HISTORY = JSON.parse(localStorage.getItem('gptimg2.history')||'[]');

// Bearer token plumbing — when MCP_BEARER_TOKEN is set on the server, /upload
// and /api/{tool} return 401 without an Authorization header. We let the user
// paste a token into a header bar; it's persisted to localStorage so reloads
// don't lose it. Stored client-side only — never sent anywhere except this server.
function getBearer(){return localStorage.getItem('gptimg2.bearer')||'';}
function setBearer(v){if(v)localStorage.setItem('gptimg2.bearer',v);else localStorage.removeItem('gptimg2.bearer');}
function authHeaders(extra={}){
  const t=getBearer();
  return t?Object.assign({},extra,{'authorization':'Bearer '+t}):extra;
}

function el(t,attrs={},...kids){const e=document.createElement(t);Object.entries(attrs).forEach(([k,v])=>{if(k=='class')e.className=v;else if(k.startsWith('on'))e[k]=v;else e.setAttribute(k,v);});kids.forEach(k=>e.append(k));return e;}

function buildNav(){
  const nav=document.getElementById('nav');
  TOOLS.forEach((t,i)=>{
    const b=el('button',{class:'tab'+(i===0?' on':''),onclick:()=>show(t.id)},
      t.name+(t.mode==='det'?' ':''),
      el('span',{class:'pill '+(t.mode==='det'?'det':'ai')},t.mode==='det'?'deterministic':'AI'),
      el('span',{class:'desc'},t.desc));
    b.dataset.tool=t.id;
    nav.append(b);
  });
}

function buildPanels(){
  const root=document.getElementById('panels');
  TOOLS.forEach((t,i)=>{
    const p=el('div',{class:'panel'+(i===0?' on':''),id:'p_'+t.id},
      el('h2',{},t.name),
      el('p',{class:'hint'},t.desc));
    if(['edit_image','add_text_overlay','add_link_cta','composite_images','refine_image','upload_image'].includes(t.id)){
      const drop=el('label',{class:'drop'},'Drag image here or click to upload',el('input',{type:'file',accept:'image/*'}));
      const inp=drop.querySelector('input');
      drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('over');});
      drop.addEventListener('dragleave',e=>{e.preventDefault();drop.classList.remove('over');});
      drop.addEventListener('drop',async e=>{e.preventDefault();drop.classList.remove('over');if(e.dataTransfer.files[0])await uploadFile(e.dataTransfer.files[0],t.id);});
      inp.addEventListener('change',async()=>{if(inp.files[0])await uploadFile(inp.files[0],t.id);});
      p.append(drop);
    }
    t.fields.forEach(f=>{
      p.append(el('label',{},f.k+(f.req?' *':'')+(f.hint?' — '+f.hint:'')));
      let inp;
      if(f.type==='textarea'){inp=el('textarea',{name:f.k,id:t.id+'_'+f.k});if(f.default)inp.value=f.default;}
      else if(f.type==='select'){
        inp=el('select',{name:f.k,id:t.id+'_'+f.k});
        f.opts.forEach(o=>{const opt=el('option',{value:o},o||'(default)');if(o===f.default)opt.selected=true;inp.append(opt);});
        if(f.onchange){inp.addEventListener('change',()=>window[f.onchange]&&window[f.onchange]());}
      } else if(f.type==='number'){
        inp=el('input',{type:'number',name:f.k,id:t.id+'_'+f.k,min:f.min||0,max:f.max||999});
        if(f.default!=null)inp.value=f.default;
      } else {
        inp=el('input',{type:'text',name:f.k,id:t.id+'_'+f.k});
        if(f.default)inp.value=f.default;
      }
      p.append(inp);
    });
    p.append(el('button',{class:'go',onclick:()=>run(t)},'Run '+t.name));
    p.append(el('div',{class:'out',id:'out_'+t.id,style:'display:none'}));
    if(HISTORY.length){
      const h=el('div',{class:'history'},el('h3',{},'Recent asset_ids — click to insert'));
      HISTORY.slice(0,12).forEach(aid=>{
        h.append(el('span',{class:'chip',onclick:()=>insertId(t.id,aid)},aid));
      });
      p.append(h);
    }
    root.append(p);
  });
}

function show(id){
  document.querySelectorAll('nav .tab').forEach(b=>b.classList.toggle('on',b.dataset.tool===id));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',p.id==='p_'+id));
}

function insertId(toolId,aid){
  const tool=TOOLS.find(t=>t.id===toolId);
  const target=tool.fields.find(f=>['image','images','previous_response_id','image_generation_call_id'].includes(f.k))||tool.fields[0];
  const inp=document.getElementById(toolId+'_'+target.k);
  if(!inp)return;
  if(inp.value && (target.k==='images'||target.k==='image'))inp.value+=','+aid;
  else inp.value=aid;
}

function renderResult(out, url, body){
  let html='';
  if(url)html+='<img src="'+url+'">';
  if(url)html+='<p><a href="'+url+'" download target="_blank" style="font-size:12px;color:#6366f1;">Download / open in new tab</a></p>';
  html+='<pre>'+JSON.stringify(body,null,2)+'</pre>';
  out.innerHTML=html;
}

async function uploadFile(file,toolId){
  const out=document.getElementById('out_'+toolId);
  out.style.display='block';out.innerHTML='<pre>Uploading…</pre>';
  try{
    const r=await fetch('/upload',{method:'POST',body:file,headers:authHeaders({'content-type':file.type||'application/octet-stream'})});
    const j=await r.json();
    if(!r.ok){out.innerHTML='<div class="err">Upload failed: '+(j.error||r.status)+(r.status===401?' — paste a Bearer token in the header bar.':'')+'</div>';return;}
    pushHistory(j.asset_id);
    insertId(toolId,j.asset_id);
    renderResult(out, j.image_url, j);
  }catch(e){out.innerHTML='<div class="err">'+e+'</div>';}
}

// Hook for the Infographic panel — swap the default content JSON when layout changes.
function syncInfographicContent(){
  const sel=document.getElementById('create_infographic_layout');
  const ta=document.getElementById('create_infographic_content');
  if(!sel||!ta)return;
  const def=INFOGRAPHIC_DEFAULTS[sel.value];
  // Only overwrite when the textarea is at a known default (don't clobber user edits).
  if(def && Object.values(INFOGRAPHIC_DEFAULTS).includes(ta.value)){
    ta.value=def;
  }
}

function pushHistory(aid){
  if(!aid)return;
  const h=[aid,...HISTORY.filter(x=>x!==aid)].slice(0,30);
  HISTORY.length=0;HISTORY.push(...h);
  localStorage.setItem('gptimg2.history',JSON.stringify(h));
  // Re-render the chip strip in every panel so it stays in sync.
  TOOLS.forEach(t=>{
    const panel=document.getElementById('p_'+t.id);
    if(!panel)return;
    panel.querySelectorAll('.history').forEach(n=>n.remove());
    if(!HISTORY.length)return;
    const h2=el('div',{class:'history'},el('h3',{},'Recent asset_ids — click to insert'));
    HISTORY.slice(0,12).forEach(x=>{
      h2.append(el('span',{class:'chip',onclick:()=>insertId(t.id,x)},x));
    });
    panel.append(h2);
  });
}

function gather(tool){
  const body={};
  tool.fields.forEach(f=>{
    const inp=document.getElementById(tool.id+'_'+f.k);
    if(!inp)return;
    let v=inp.value;
    if(v==='')return;
    if(f.type==='number')v=Number(v);
    if(JSON_FIELDS.includes(f.k)){try{v=JSON.parse(v);}catch(e){throw new Error(f.k+' must be valid JSON: '+e.message);}}
    if(LIST_FIELDS.includes(f.k) && typeof v==='string' && v.includes(',')){
      v=v.split(',').map(s=>s.trim()).filter(Boolean);
    }
    body[f.k]=v;
  });
  return body;
}

async function run(tool){
  const out=document.getElementById('out_'+tool.id);
  out.style.display='block';
  out.innerHTML='<pre>Running '+tool.id+'…</pre>';
  let body;
  try{body=gather(tool);}catch(e){out.innerHTML='<div class="err">'+e.message+'</div>';return;}
  try{
    const r=await fetch('/api/'+tool.id,{method:'POST',headers:authHeaders({'content-type':'application/json'}),body:JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok){out.innerHTML='<div class="err">'+(j.error||r.status)+(r.status===401?' — paste a Bearer token in the header bar.':'')+'</div>';return;}
    if(j.asset_id)pushHistory(j.asset_id);
    if(Array.isArray(j.results))j.results.forEach(x=>x.asset_id&&pushHistory(x.asset_id));
    if(Array.isArray(j.images))j.images.forEach(x=>x.asset_id&&pushHistory(x.asset_id));
    const url=j.image_url||(j.images&&j.images[0]&&j.images[0].image_url)||(j.results&&j.results[0]&&j.results[0].image_url);
    renderResult(out, url, j);
  }catch(e){out.innerHTML='<div class="err">'+e+'</div>';}
}

buildNav();buildPanels();
const bearerInput=document.getElementById('bearer');
bearerInput.value=getBearer();
bearerInput.addEventListener('input',()=>setBearer(bearerInput.value.trim()));
</script></body></html>"""


_UPLOAD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>gpt-image-2-mcp · upload</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#0f1320;}
  h1{font-size:20px;margin-bottom:8px;}
  p{color:#5b6478;font-size:14px;line-height:1.5;}
  .drop{border:2px dashed #cbd0dc;border-radius:12px;padding:36px 12px;text-align:center;cursor:pointer;background:#fafbfd;}
  .drop.over{background:#eef2ff;border-color:#6366f1;}
  pre{background:#0f1320;color:#e6e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px;}
  .row{display:flex;gap:8px;align-items:center;}
  input[type=file]{display:none;}
  button{background:#0f1320;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px;}
</style></head><body>
<h1>Upload an image</h1>
<p>Drag a file or click to choose. You'll get back an <code>asset_id</code> and a signed <code>image_url</code> usable as input to any tool.</p>
<label class="drop" id="drop"><input type="file" id="f" accept="image/*">Drop image or click to select</label>
<pre id="out" style="display:none"></pre>
<script>
const drop=document.getElementById('drop'),f=document.getElementById('f'),out=document.getElementById('out');
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop',ev=>{ev.preventDefault();drop.classList.remove('over');if(ev.dataTransfer.files[0])upload(ev.dataTransfer.files[0]);});
f.addEventListener('change',()=>{if(f.files[0])upload(f.files[0]);});
async function upload(file){
  out.style.display='block';out.textContent='Uploading…';
  const r=await fetch('/upload',{method:'POST',body:file,headers:{'content-type':file.type||'application/octet-stream'}});
  const j=await r.json();out.textContent=JSON.stringify(j,null,2);
}
</script></body></html>"""


def register_routes(mcp) -> None:
    """Register /upload and /images/{asset_id} on the FastMCP HTTP app."""

    @mcp.custom_route("/upload", methods=["GET"])
    async def upload_form(request: Request) -> Response:
        return HTMLResponse(_UPLOAD_HTML)

    @mcp.custom_route("/upload", methods=["POST"])
    async def http_upload(request: Request) -> Response:
        ok, reason = check_bearer(request.headers.get("authorization"))
        if not ok:
            return JSONResponse(
                {"error": f"Unauthorized ({reason}). Set Authorization: Bearer <MCP_BEARER_TOKEN>."},
                status_code=401,
            )
        allowed, retry_after = check_rate_limit(client_ip_from_request(request))
        if not allowed:
            return JSONResponse(
                {"error": "Rate limit exceeded", "retry_after_seconds": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        ct = request.headers.get("content-type", "")
        if "multipart/form-data" in ct:
            form = await request.form()
            file = form.get("file") or form.get("image")
            if file is None:
                return JSONResponse({"error": "No 'file' field in form data"}, status_code=400)
            raw = await file.read()
            await form.close()
        else:
            raw = await request.body()
            if not raw:
                return JSONResponse({"error": "Empty request body"}, status_code=400)

        try:
            normalized, mime = normalize_image(raw, max_dim=2048, target_format="PNG")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        from PIL import Image as PILImage
        w, h = PILImage.open(BytesIO(normalized)).size

        asset_id = store_asset(normalized, mime)
        signed_url, expires_at = make_signed_url(asset_id)
        durable_url = None
        if cloud_storage_configured():
            durable_url = await upload_to_cloud(normalized, mime, asset_id=asset_id)

        body = {
            "asset_id": asset_id,
            "image_url": durable_url or signed_url,
            "expires_at": None if durable_url else expires_at.isoformat(),
            "refresh_url_supported": True,
            "width": w,
            "height": h,
            "size_kb": len(normalized) // 1024,
            "usage": "Pass either asset_id or image_url to any tool.",
        }
        if durable_url:
            body["image_url_signed"] = signed_url
        else:
            body["ttl_seconds"] = DEFAULT_URL_TTL
        return JSONResponse(body, status_code=201)

    @mcp.custom_route("/app", methods=["GET"])
    async def studio_app(request: Request) -> Response:
        return HTMLResponse(_STUDIO_HTML)

    @mcp.custom_route("/api/{tool_name}", methods=["POST"])
    async def http_call_tool(request: Request) -> Response:
        ok, reason = check_bearer(request.headers.get("authorization"))
        if not ok:
            return JSONResponse(
                {"error": f"Unauthorized ({reason})."},
                status_code=401,
            )
        allowed, retry_after = check_rate_limit(client_ip_from_request(request))
        if not allowed:
            return JSONResponse(
                {"error": "Rate limit exceeded", "retry_after_seconds": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        name = request.path_params["tool_name"]
        tools = getattr(mcp._tool_manager, "_tools", {})
        if name not in tools:
            return JSONResponse({"error": f"Unknown tool: {name}"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
        # Drop None values so optional kwargs use their function-level defaults.
        kwargs = {k: v for k, v in body.items() if v is not None}
        try:
            result = await tools[name].fn(**kwargs)
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001 — surface for the UI; logged by uvicorn
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
        try:
            data = unpack_tool_result(result)
        except TypeError as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(data, status_code=200)

    @mcp.custom_route("/images/{asset_id}", methods=["GET"])
    async def http_get_image(request: Request) -> Response:
        asset_id = request.path_params["asset_id"]
        expires_str = request.query_params.get("expires", "")
        sig = request.query_params.get("sig", "")
        try:
            expires = int(expires_str)
        except (TypeError, ValueError):
            # Don't leak existence — same shape as bad_sig.
            return JSONResponse({"error": "Not found"}, status_code=404)

        ok, reason = verify_url(asset_id, expires, sig)
        if not ok:
            if reason == "expired":
                return JSONResponse({"error": "URL expired"}, status_code=410)
            return JSONResponse({"error": "Not found"}, status_code=404)

        if not has_asset(asset_id):
            return JSONResponse({"error": "Not found"}, status_code=404)

        try:
            img_bytes, mime = resolve_asset(asset_id)
        except KeyError:
            return JSONResponse({"error": "Not found"}, status_code=404)

        return Response(
            content=img_bytes,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=300"},
        )
