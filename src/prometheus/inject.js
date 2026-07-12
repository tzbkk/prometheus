// Prometheus: QQ Guild Feed Archiver v3
// Reads PROMETHEUS_* env vars (not the JSON config) because this file gets
// copied INTO the AppImage at setup time and can no longer reach the project
// tree. start_qq.sh sources prometheus.conf.json → exports these env vars.
global.__promStartTime = Date.now();
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

const { Logger } = require("./logger.js");
const { Lock } = require("./lock.js");
const { ApiServer } = require("./api-server.js");

const E = process.env;
const CFG = {
    dataDir:        E.PROMETHEUS_DATA_DIR || E.PROMETHEUS_DATA || (process.env.HOME + "/Projects/prometheus/data"),
    channelId:      E.PROMETHEUS_CHANNEL_ID      || "7743321643036658",
    channelName:    E.PROMETHEUS_CHANNEL_NAME    || "擅长捉弄的高木同学",
    guildMenuText:  E.PROMETHEUS_GUILD_MENU_TEXT || "频道",
    feedIdPrefix:   E.PROMETHEUS_FEED_ID_PREFIX  || "B_",
    scanDepth:      parseInt(E.PROMETHEUS_SCAN_DEPTH || "5", 10),
    scanArrayDepth: parseInt(E.PROMETHEUS_SCAN_ARRAY_DEPTH || "3", 10),
    urlGuildPage:   E.PROMETHEUS_URL_GUILD_PAGE   || "index.html",
    urlHiddenWin:   E.PROMETHEUS_URL_HIDDEN_WINDOW || "hiddenWindow",
    urlChannelPage: E.PROMETHEUS_URL_CHANNEL_PAGE || "pd.qq.com",
    scrollX:        parseInt(E.PROMETHEUS_SCROLL_X || "400", 10),
    scrollY:        parseInt(E.PROMETHEUS_SCROLL_Y || "400", 10),
    scrollDeltaY:   parseInt(E.PROMETHEUS_SCROLL_DELTA_Y || "-500", 10),
    scrollInterval: parseInt(E.PROMETHEUS_SCROLL_INTERVAL_MS || "800", 10),
    scrollMax:      parseInt(E.PROMETHEUS_SCROLL_MAX_ITERATIONS || "5000", 10),
    daemonMode:     E.PROMETHEUS_DAEMON_MODE === "true" || E.PROMETHEUS_DAEMON_MODE === "1" || E.PROMETHEUS_DAEMON_MODE === undefined,
    daemonInterval: parseInt(E.PROMETHEUS_DAEMON_INTERVAL_MS || "300000", 10),
};
try {
    CFG.startupSequence = JSON.parse(E.PROMETHEUS_STARTUP_SEQUENCE || "[]");
} catch (e) {
    CFG.startupSequence = [];
}

CFG.apiPort = parseInt(E.PROMETHEUS_API_PORT || "9420", 10);
CFG.logBufferLines = parseInt(E.PROMETHEUS_LOG_BUFFER_LINES || "500", 10);
CFG.logLevel = E.PROMETHEUS_LOG_LEVEL || "INFO";
CFG.apiVersion = E.PROMETHEUS_API_VERSION || "1";
try {
    const confPath = path.join(__dirname, "..", "..", "..", "conf", "prometheus.conf.json");
    const fileConf = JSON.parse(fs.readFileSync(confPath, "utf8"));
    if (!E.PROMETHEUS_API_PORT && fileConf.api_port) CFG.apiPort = fileConf.api_port;
    if (!E.PROMETHEUS_LOG_BUFFER_LINES && fileConf.log_buffer_lines) CFG.logBufferLines = fileConf.log_buffer_lines;
    if (!E.PROMETHEUS_LOG_LEVEL && fileConf.log_level) CFG.logLevel = fileConf.log_level;
    if (!E.PROMETHEUS_API_VERSION && fileConf.api_version) CFG.apiVersion = fileConf.api_version;
} catch(e) {}

const logger = new Logger({
    logDir: path.join(path.dirname(CFG.dataDir), "log", "prometheus"),
    logLevel: CFG.logLevel,
    bufferLines: CFG.logBufferLines
});

const lock = new Lock({dataDir: CFG.dataDir, logger: logger});

try { fs.mkdirSync(CFG.dataDir, {recursive:true}); } catch(e) {}
logger.log("INFO", "=== Start === config: " + JSON.stringify({
    channelId: CFG.channelId, channelName: CFG.channelName,
    scanDepth: CFG.scanDepth, feedIdPrefix: CFG.feedIdPrefix
}));

const capturedIds = new Set();
try { (fs.readFileSync(CFG.dataDir+"/ids.json","utf8")||"").split("\n").filter(Boolean).forEach(id => capturedIds.add(id)); } catch(e) {}
try { fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean).forEach(l=>{try{const d=JSON.parse(l);if(d.id)capturedIds.add(d.id)}catch(e){}}); } catch(e) {}
logger.log("INFO", "Loaded "+capturedIds.size+" IDs");
setInterval(()=>{try{fs.writeFileSync(CFG.dataDir+"/ids.json",[...capturedIds].join("\n"))}catch(e){}},10000);

const mediaSeen = new Set();
const mediaQueue = [];
const deadMediaQueue = [];
try { (fs.readFileSync(CFG.dataDir+"/dead_media.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{deadMediaQueue.push(JSON.parse(l))}catch(e){}}); } catch(e) {}
try { (fs.readFileSync(CFG.dataDir+"/media_index.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{var e=JSON.parse(l);if(e.url)mediaSeen.add(e.url)}catch(e){}}); } catch(e) {}
try { (fs.readFileSync(CFG.dataDir+"/dead_media_permanent.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{mediaSeen.add(JSON.parse(l).url)}catch(e){}}); } catch(e) {}
try { fs.mkdirSync(CFG.dataDir+"/media",{recursive:true}); } catch(e) {}

const STATE_FILES = ['feeds.jsonl'];
function computeStateHash() {
    const parts = [];
    for (const f of STATE_FILES) {
        try {
            const data = fs.readFileSync(path.join(CFG.dataDir, f));
            const h = crypto.createHash('sha256');
            h.update(data);
            parts.push(h.digest('hex'));
        } catch(e) {
            parts.push('missing');
        }
    }
    return crypto.createHash('sha256').update(parts.join(':')).digest('hex');
}
function readState() {
    try {
        const raw = fs.readFileSync(path.join(CFG.dataDir, 'state.json'), 'utf8');
        const state = JSON.parse(raw);
        if (!state.hash) return null;
        const currentHash = computeStateHash();
        if (currentHash !== state.hash) {
            logger.log("DEBUG", "State: hash mismatch (saved=" + (state.hash||'').slice(0,16) + " cur=" + currentHash.slice(0,16) + ") — data grew since last cycle, ok");
        }
        return state;
    } catch(e) { return null; }
}

function extractMediaUrls(obj) {
    var urls = [];
    var seen = {};
    function add(url, type) {
        if (url && typeof url === 'string' && !seen[url]) {
            seen[url] = 1;
            urls.push({url: url, type: type});
        }
    }
    (function scan(x, d) {
        if (d > 8 || !x) return;
        if (typeof x === 'string') {
            if (x.startsWith('https://') && (x.indexOf('qpic.cn')>=0 || x.indexOf('myqcloud.com')>=0 ||
                x.indexOf('photo.store')>=0 || x.indexOf('qlogo')>=0 || x.indexOf('qchannelvideo')>=0)) {
                add(x, x.indexOf('video')>=0||x.indexOf('.mp4')>=0 ? 'video' : 'image');
            }
            return;
        }
        if (typeof x !== 'object') return;
        for (var k in x) { if (x[k] !== null && x[k] !== undefined) scan(x[k], d+1); }
    })(obj, 0);
    return urls;
}

function queueMedia(urls, source) {
    urls.forEach(function(u) {
        if (!mediaSeen.has(u.url)) { mediaSeen.add(u.url); mediaQueue.push({url: u.url, type: u.type, source: source}); }
    });
}

function saveFeed(o) { try { const fid=o.id||""; if(!fid||capturedIds.has(fid))return; const sign=(o.channelInfo&&o.channelInfo.sign)||{}; if(sign.guild_id&&sign.guild_id!==CFG.channelId)return; fs.appendFileSync(CFG.dataDir+"/feeds.jsonl",JSON.stringify(o)+"\n"); capturedIds.add(fid); queueMedia(extractMediaUrls(o), fid); } catch(e) { try { fs.appendFileSync(CFG.dataDir+"/prometheus.log", `[${new Date().toISOString()}] saveFeed err: ${e.message} fid=${(o&&o.id)||'?'}\n`); } catch(e2){} } }

const capturedCommentKeys = new Set();
try { (fs.readFileSync(CFG.dataDir+"/comments.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{const k=computeCommentKey(JSON.parse(l));if(k)capturedCommentKeys.add(k)}catch(e){}}); } catch(e) {}
logger.log("INFO", "Loaded "+capturedCommentKeys.size+" comment keys");

function computeCommentKey(o) {
    try {
        var d=o.d||{};
        var vc=null;
        if(d.d&&d.d.data&&Array.isArray(d.d.data.vecComment))vc=d.d.data.vecComment;
        else if(Array.isArray(d.vecComment))vc=d.vecComment;
        else if(d.found&&d.found[0]&&Array.isArray(d.found[0].vecComment))vc=d.found[0].vecComment;
        if(!vc||vc.length===0)return null;
        var ids=vc.map(function(c){return c.id||''}).filter(Boolean).sort();
        if(ids.length===0)return null;
        return ids.join(',');
    } catch(e) { return null; }
}

function saveComment(jsonStr) {
    try {
        const o = JSON.parse(jsonStr);
        const key = computeCommentKey(o);
        if (key) {
            if (capturedCommentKeys.has(key)) return;
            capturedCommentKeys.add(key);
        }
        fs.appendFileSync(CFG.dataDir+"/comments.jsonl", JSON.stringify(o) + "\n");
        var d=o.d||{};
        var vc=null;
        if(d.d&&d.d.data&&Array.isArray(d.d.data.vecComment))vc=d.d.data.vecComment;
        else if(Array.isArray(d.vecComment))vc=d.vecComment;
        else if(d.found&&d.found[0]&&Array.isArray(d.found[0].vecComment))vc=d.found[0].vecComment;
        if(vc)vc.forEach(function(c){queueMedia(extractMediaUrls(c),c.id||'comment')});
    } catch(e) {}
}

function deepScanComments(obj, source, depth) {
    if (depth > 5 || !obj || typeof obj !== 'object') return;
    try {
        if (Array.isArray(obj.vecComment) && obj.vecComment.length > 0) {
            saveComment(JSON.stringify({_s: source, ts: Date.now(), d: {totalNum: obj.totalNum, vecComment: obj.vecComment}}));
            logger.log("INFO", "COMMENT HIT via " + source + " (" + obj.vecComment.length + " comments)");
        }
    } catch(e) {}
    for (const k in obj) {
        if (obj[k] && typeof obj[k] === 'object') deepScanComments(obj[k], source, depth + 1);
    }
}

const INJECT_JS = `
(function(){if(window.__P)return;window.__P=1;
window._oRAF=requestAnimationFrame;window._oCAF=cancelAnimationFrame;
requestAnimationFrame=function(cb){return setTimeout(function(){cb(performance.now())},0)};
cancelAnimationFrame=function(id){clearTimeout(id)};
var PREFIX=${JSON.stringify(CFG.feedIdPrefix)},D=${CFG.scanDepth},AD=${CFG.scanArrayDepth};
var seenC={};
var o=JSON.parse;JSON.parse=function(){var r=o.apply(this,arguments);try{if(r&&typeof r==='object'){var f=[],cc=[];(function F(x,d,cfid){if(d>D||!x||typeof x!=='object')return;var fid=cfid;if(x.id&&typeof x.id==='string'&&x.id.startsWith(PREFIX)&&(x.createTime||x.poster||x.title)){fid=x.id;f.push(x)}if(Array.isArray(x.feeds))x.feeds.forEach(function(i){if(i&&i.id&&typeof i.id==='string'&&i.id.startsWith(PREFIX))f.push(i)});if(Array.isArray(x.vecComment)&&x.vecComment.length>0){var fid2=fid||x.feedId||(window.__prom_fetching_pid||'');var key=fid2+'_'+x.vecComment.length+'_'+(x.vecComment[0]&&x.vecComment[0].id||'');if(!seenC[key]){seenC[key]=1;cc.push({feedId:fid2,totalNum:x.totalNum,vecComment:x.vecComment})}}if(d<AD)for(var k in x)if(x[k]&&typeof x[k]==='object')F(x[k],d+1,fid)})(r,0,'');for(var i=0;i<f.length;i++)console.log('[P]'+JSON.stringify(f[i]));for(var i=0;i<cc.length;i++)console.log('[PC]'+JSON.stringify({_s:'json_parse_comments',ts:Date.now(),d:cc[i]}))}}catch(e){}return r}})();
`;

// Renderer-side hooks for IPC/fetch/XHR/Vuex — injected after QQ settles
const COMMENT_HOOK_JS = `
(function(){if(window.__PC)return;window.__PC=1;

var log=function(tag,obj){try{console.log('[PC]'+JSON.stringify({_s:tag,ts:Date.now(),d:obj}))}catch(e){}};

// 0. bkn capture — extract from any passing URL
window.__prom_bkn=window.__prom_bkn||'';

// 1. IPC hook — scan ALL invoke responses and emit events for vecComment
try{(function(){
    var ir=window.ipcRenderer;
    if(!ir)return;
    if(typeof ir.invoke==='function'){
        var oi=ir.invoke;
        ir.invoke=function(ch,a){
            var r=oi.call(this,ch,a);
            if(r&&typeof r.then==='function')return r.then(function(d){
                try{
                    var found=[];
                    (function S(x,dep){if(dep>6||!x||typeof x!=='object')return;if(Array.isArray(x.vecComment)&&x.vecComment.length>0)found.push({totalNum:x.totalNum,vecComment:x.vecComment});for(var k in x)if(x[k]&&typeof x[k]==='object')S(x[k],dep+1)})(d,0);
                    if(found.length>0)log('ipc_invoke_comments',{c:ch,found:found});
                }catch(e){}
                return d
            });
            return r
        };
    }
    if(typeof ir.emit==='function'){
        var oe=ir.emit;
        ir.emit=function(ch){
            try{
                for(var i=1;i<arguments.length;i++){
                    var a=arguments[i];
                    if(a&&typeof a==='object'){
                        var found=[];
                        (function S(x,dep){if(dep>6||!x||typeof x!=='object')return;if(Array.isArray(x.vecComment)&&x.vecComment.length>0)found.push(x.vecComment);for(var k in x)if(x[k]&&typeof x[k]==='object')S(x[k],dep+1)})(a,0);
                        if(found.length>0)log('ipc_emit_comments',{c:ch,count:found.reduce(function(s,f){return s+f.length},0)});
                    }
                }
            }catch(e){}
            return oe.apply(this,arguments)
        };
    }
})()}catch(e){}

// 2. fetch hook — capture OIDB header and bkn only, no response logging
try{(function(){
    if(typeof fetch!=='function')return;
    var of=fetch;
    fetch=function(i,n){
        var url=typeof i==='string'?i:(i&&i.url)||'';
        try{var bknM=url.match(/[?&]bkn=(\d+)/);if(bknM&&bknM[1])window.__prom_bkn=bknM[1];}catch(e){}
        var hdrs=(n&&n.headers)||{};
        if(!(hdrs instanceof Object)||typeof hdrs.entries==='function'){
            hdrs={}; if(n&&n.headers&&typeof n.headers.entries==='function'){try{var ent=n.headers.entries();while(true){try{var kv=ent.next();if(kv.done)break;hdrs[kv.value[0]]=kv.value[1]}catch(e){break}}}catch(e){}}
        }
        try{
            if(hdrs['x-oidb']&&(typeof url==='string')&&url.toLowerCase().indexOf('getfeedcomments')>=0){
                window.__prom_comment_oidb=hdrs['x-oidb'];
                window.__prom_comment_appid=hdrs['x-qq-client-appid']||'537355866';
                console.log('[Prometheus] Captured comment OIDB header');
            }
        }catch(e){}
        return of.call(this,i,n);
    };
})()}catch(e){}

// 4. Expose direct comment fetcher for main process to call
//     Main process sets window.__prom_bkn before calling this
window.__prom_getComments=function(postId,guildId){
    var bkn=window.__prom_bkn;
    if(!bkn){log('bkn_missing',{postId:postId});return;}
    var url='https://pd.qq.com/qunng/guild/gotrpc/auth/trpc.qchannel.commreader.ComReader/GetFeedComments?bkn='+bkn+'&_t='+Date.now()+'&_v=1.0.1&client_platform=pcqqwebview';
    var body=JSON.stringify({feedId:postId,listNum:20,from:1,src:0,attchInfo:"",needInsertComment:[],needInsertCommentID:"",needInsertReplyID:"",channelSign:{guild_number:guildId},extInfo:{mapInfo:[{key:"qc-tabid",value:""},{key:"qc-pageid",value:""}]},rankingType:1,replyListNum:1,render_sticker:true});
    var hdrs={'Content-Type':'application/json'};
    if(window.__prom_comment_oidb){
        hdrs['x-oidb']=window.__prom_comment_oidb;
        hdrs['x-qq-client-appid']=window.__prom_comment_appid||'537355866';
    }
    window.__prom_fetching_pid=postId;
    setTimeout(function(){if(window.__prom_fetching_pid===postId)window.__prom_fetching_pid='';},5000);
    return fetch(url,{method:'POST',credentials:'include',headers:hdrs,body:body}).then(function(r){return r});
};

// 5. Explore QQ bridge objects
try{(function(){
    ['__NTV','__NEXT','nt','QQNT','qq','Bridge','ipc','NTAPI','QQ','__QQ','$NT'].forEach(function(n){
        try{var o=window[n];if(o&&typeof o==='object')log('bridge_found',{n:n,t:Object.prototype.toString.call(o),k:Object.keys(o).slice(0,30)})}catch(e){}
    });
})()}catch(e){}

// 6. Vue deep dive
setTimeout(function(){
    try{
        var el=document.getElementById('app');
        if(!el||!el.__vue_app__)return;
        var app=el.__vue_app__;
        try{
            var rt=app.config.globalProperties['$router'];
            if(rt)log('vue_router',{options:JSON.stringify(rt.options?Object.keys(rt.options):'no options'),routes:(rt.options&&rt.options.routes)?rt.options.routes.map(function(r){return r.path||r.name||r}).slice(0,30):null});
        }catch(e){}
        try{
            var qc=app._context.provides['VUE_QUERY_CLIENT']||app.config.globalProperties['$query'];
            if(qc){
                var cache=qc.getQueryCache?qc.getQueryCache():null;
                if(cache){
                    var queries=cache.getAll?cache.getAll():[];
                    log('vq_cache',{qcount:queries.length,qkeys:queries.slice(0,20).map(function(q){var k=q.queryKey;return JSON.stringify(k).slice(0,100)})});
                }
            }
        }catch(e){}
        try{
            function walkVNode(vn,depth){
                if(depth>8||!vn)return;
                var type=vn.type;
                if(typeof type==='string')return;
                if(typeof type==='object'){
                    var name=type.__name||type.name||'anon';
                    if(name.indexOf('Feed')>=0||name.indexOf('Guild')>=0||name.indexOf('Channel')>=0||name.indexOf('Post')>=0||name.indexOf('Comment')>=0){
                        log('vue_feed_comp',{name:name,keys:vn.setupState?Object.keys(vn.setupState).slice(0,30):null});
                    }
                }
                if(vn.component&&vn.component.subTree)walkVNode(vn.component.subTree,depth+1);
                if(vn.children&&Array.isArray(vn.children))vn.children.forEach(function(c){if(typeof c==='object')walkVNode(c,depth+1)});
                if(vn.component&&vn.component.ctx)vn=vn.component.ctx;
            }
            walkVNode(el.__vue_app__._instance,0);
        }catch(e){}
    }catch(e){}
},25000);
})();
`;

let electron; try { electron=require("electron") } catch(e) { logger.log("WARN", "No electron") }

if (electron && electron.app) {
    const { app, BrowserWindow, net } = electron;
    let globalBkn = "";

    function guessExt(url, type) {
        var m = url.match(/\.(jpg|jpeg|png|gif|webp|mp4|webm|mov)(\?|$)/i);
        if (m) return '.' + m[1].toLowerCase();
        if (type === 'video') return '.mp4';
        return '.jpg';
    }

    function appendMediaIndex(url, file, type, source, status, size) {
        try { fs.appendFileSync(CFG.dataDir+"/media_index.jsonl",
            JSON.stringify({url:url,file:file,type:type,source:source,status:status,size:size,ts:Date.now()})+"\n");
        } catch(e) {}
    }

    function downloadOne(url, type, source) {
        var hash = crypto.createHash('sha256').update(url).digest('hex').slice(0,16);
        var filename = hash + guessExt(url, type);
        var filepath = CFG.dataDir+"/media/"+filename;
        if (fs.existsSync(filepath) && fs.statSync(filepath).size > 0) { appendMediaIndex(url, filename, type, source, 'cached', fs.statSync(filepath).size); return; }
        try { lock.addPendingMedia(url, filename, 0); } catch(e) {}
        try {
            var req = net.request(url);
            var chunks = [];
            req.on('response', function(resp) {
                resp.on('data', function(c) { chunks.push(c); });
                resp.on('end', function() {
                    var buf = Buffer.concat(chunks);
                    try { fs.writeFileSync(filepath, buf); logger.log("INFO", "Media: "+filename+" ("+buf.length+"B) from "+source); }
                    catch(e) { logger.log("ERROR", "Media write err: " + e.message); }
                    try { lock.removePendingMedia(url); } catch(e) {}
                    appendMediaIndex(url, filename, type, source, buf.length > 0 ? 'ok' : 'empty', buf.length);
                    if (buf.length === 0 && source && source.startsWith('B_')) { var ex = deadMediaQueue.find(function(x){return x.url===url}); if (ex) { ex.retries=(ex.retries||0)+1; } else { deadMediaQueue.push({url:url, type:type, source:source, retries:1}); } flushDeadMedia(); }
                });
            });
            req.on('error', function(e) { appendMediaIndex(url, filename, type, source, 'error:'+e.message, 0); try { lock.removePendingMedia(url); } catch(e2) {}; if (source && source.startsWith('B_')) { var ex = deadMediaQueue.find(function(x){return x.url===url}); if (ex) { ex.retries=(ex.retries||0)+1; } else { deadMediaQueue.push({url:url, type:type, source:source, retries:1}); } flushDeadMedia(); } });
            req.end();
        } catch(e) { appendMediaIndex(url, filename, type, source, 'error:'+e.message, 0); try { lock.removePendingMedia(url); } catch(e2) {}; if (source && source.startsWith('B_')) { var ex = deadMediaQueue.find(function(x){return x.url===url}); if (ex) { ex.retries=(ex.retries||0)+1; } else { deadMediaQueue.push({url:url, type:type, source:source, retries:1}); } flushDeadMedia(); } }
    }

    function flushDeadMedia() {
        try { fs.writeFileSync(CFG.dataDir+"/dead_media.jsonl", deadMediaQueue.map(function(x){return JSON.stringify(x)}).join("\n")+(deadMediaQueue.length?"\n":"")); } catch(e) {}
    }
    setInterval(flushDeadMedia, 10000);

    setInterval(function() {
        var n = 0;
        while (mediaQueue.length > 0 && n < 20) {
            var item = mediaQueue.shift();
            downloadOne(item.url, item.type, item.source);
            n++;
        }
    }, 2000);

    app.on('web-contents-created', (ev, wc) => {
        try {
            const origSend = wc.send.bind(wc);
            wc.send = function(ch, ...args) {
                try { for (const a of args) { if (a && typeof a === 'object') deepScanComments(a, 'wc_send:'+ch, 0); } } catch(e) {}
                return origSend(ch, ...args);
            };
        } catch(e) { logger.log("WARN", "wc.send wrap err: " + e.message); }
        wc.on('dom-ready', () => {
            wc.executeJavaScript(INJECT_JS).catch(()=>{});
            // Comment hooks (IPC/fetch/XHR/bridge) inject after a brief settle
            setTimeout(() => {
                wc.executeJavaScript(COMMENT_HOOK_JS).catch(()=>{});
            }, 5000);
        });
        wc.on('console-message', (e, lvl, msg) => {
            if (msg.startsWith('[PC]')) {
                try { saveComment(msg.substring(4)); } catch(err) {}
                try { const m = msg.match(/[?&]bkn=(\d+)/); if (m) globalBkn = m[1]; } catch(e) {}
            }
            else if (msg.startsWith('[P]')) { try { saveFeed(JSON.parse(msg.substring(3))); } catch(err) {} }
            else if (msg.startsWith('[Prometheus]')) logger.log("INFO", "R: "+msg);
            else if (lvl > 2) logger.log("ERROR", "E: "+msg.slice(0,200));
        });
    });

    function switchToGuild() {
        BrowserWindow.getAllWindows().forEach(win => {
            const u = win.webContents.getURL();
            if (!u.includes(CFG.urlGuildPage) || u.includes(CFG.urlHiddenWin)) return;
            win.webContents.executeJavaScript(`
                (function(){
                    if (location.hash.includes('/main/guild')) { console.log('[Prometheus] Guild OK'); return; }
                    var menu = ${JSON.stringify(CFG.guildMenuText)};
                    document.querySelectorAll('*').forEach(el => {
                        if (el.children.length<3 && el.textContent.trim()===menu) el.click();
                    });
                    location.hash = '#/main/guild';
                    console.log('[Prometheus] Hash -> guild');
                })();
            `).catch(()=>{});
        });
    }

    function clickChannel() {
        const all = electron.webContents.getAllWebContents();
        all.forEach(wc => {
            try {
                if (!wc.getURL().includes(CFG.urlChannelPage)) return;
                wc.executeJavaScript(`
                    (function(){
                        var CNAME = ${JSON.stringify(CFG.channelName)};
                        if (location.href.includes('/channels/${CFG.channelId}') || location.href.includes('/g/${CFG.channelId}')) {
                            if (!window.__PClicked) { window.__PClicked = true; console.log('[Prometheus] Already on channel'); }
                            return;
                        }
                        var all = document.querySelectorAll('*');
                        var best = null;
                        for (var i = 0; i < all.length; i++) {
                            var el = all[i];
                            if (el.children.length > 3) continue;
                            var t = el.textContent.trim();
                            if (t === CNAME) { best = el; break; }
                            if (t.startsWith(CNAME)) {
                                var rem = t.slice(CNAME.length).trim();
                                if (rem === '' || /^\\d+$/.test(rem)) { best = el; break; }
                            }
                        }
                        if (best) {
                            console.log('[Prometheus] Clicking: ' + best.textContent.trim().slice(0,30));
                            best.click();
                            window.__PClicked = true;
                        } else {
                            console.log('[Prometheus] Channel element not found. Body text sample: ' + (document.body ? document.body.innerText.slice(0,100) : 'no body'));
                        }
                    })();
                `).catch(()=>{});
            } catch(e) {}
        });
    }

    let scrollCount = 0;
    const savedState = readState();

    const lockBottom = lock.readBottomReached();
    let timelineBottomReached = (lockBottom !== undefined)
        ? lockBottom
        : ((savedState && savedState.bottomReached) || false);

    const recovery = lock.checkAndRecover();
    if (recovery && recovery.crashed) {
        logger.log("WARN", "Crash recovery: dirty lock detected, bottomReached=" + recovery.bottomReached);
        if (recovery.bottomReached !== undefined) {
            timelineBottomReached = recovery.bottomReached;
        }
        if (recovery.pendingMedia && recovery.pendingMedia.length > 0) {
            logger.log("INFO", "Crash recovery: re-downloading " + recovery.pendingMedia.length + " pending media");
            recovery.pendingMedia.forEach(function(m) {
                if (!mediaSeen.has(m.url)) {
                    mediaQueue.push({url: m.url, type: m.type || 'image', source: m.source || 'recovery'});
                }
            });
        }
    }

    if (timelineBottomReached && lock.readBottomReached() !== true) {
        lock.setBottomReached(true);
    }

    // Probe QQ's DOM/Vue state for "timeline bottom" signals. Logs only when something interesting found
    // (or every 200 iter as snapshot) to keep noise low.
    let probeLastLog = 0;
    let probeStall = 0;
    let probeLastScrollHeight = 0;
    const PROBE_STALL_MAX = 30;
    function probeBottom(wc) {
        wc.executeJavaScript(`
            (function(){
                var txt = (document.body && document.body.innerText) || '';
                var inds = ['已经到底','到底了','到底啦','没有更多','没有更多了','已加载全部','已到底','No more','已显示全部','已经加载完成','已经到底啦','没有啦','加载完毕','END'];
                var found = inds.filter(function(s){ return txt.indexOf(s) >= 0; });
                var info = {ind:found};
                info.url = location.href.slice(-80);
                var allScrollers = document.querySelectorAll('.vue-recycle-scroller');
                info.allScrollerCount = allScrollers.length;
                info.scrollers = [];
                for (var i=0; i<allScrollers.length; i++) {
                    var s = allScrollers[i];
                    var items = s.querySelectorAll('.vue-recycle-scroller__item-view').length;
                    info.scrollers.push({
                        idx:i,
                        cls: s.className.slice(0,80),
                        pcls: (s.parentElement && s.parentElement.className||'').slice(0,80),
                        items: items,
                        scrollHeight: s.scrollHeight,
                        clientHeight: s.clientHeight,
                        scrollTop: s.scrollTop,
                        rect: JSON.stringify({w:s.offsetWidth, h:s.offsetHeight, top: s.getBoundingClientRect().top})
                    });
                }
                var bigScrolls = [];
                document.querySelectorAll('*').forEach(function(el){
                    if (el.scrollHeight > 1000 && el.clientHeight > 100 && el.scrollHeight > el.clientHeight + 50) {
                        bigScrolls.push({tag:el.tagName, cls:(el.className||'').slice(0,60), sh:el.scrollHeight, ch:el.clientHeight, st:el.scrollTop, items: el.querySelectorAll('[class*="item-view"],[class*="feed"],[class*="Feed"]').length});
                    }
                });
                info.bigScrolls = bigScrolls.slice(0,5);
                return JSON.stringify(info).slice(0,2000);
            })()
        `).then(function(r){
            try {
                var d = JSON.parse(r);
                var hasInd = d.ind && d.ind.length > 0;
                var curSH = (d.bigScrolls && d.bigScrolls[0] && d.bigScrolls[0].sh) || 0;
                var curST = (d.bigScrolls && d.bigScrolls[0] && d.bigScrolls[0].st) || 0;
                var curCH = (d.bigScrolls && d.bigScrolls[0] && d.bigScrolls[0].ch) || 1;
                var atScrollBottom = curCH > 0 && (curST + curCH) / curSH > 0.985;
                var now = Date.now();
                if (hasInd || atScrollBottom || now - probeLastLog > 60000) {
                    logger.log("DEBUG", "Probe " + scrollCount + " sh="+curSH+" st="+curST+" ch="+curCH+" bottom="+atScrollBottom+" feeds="+capturedIds.size+" ind="+JSON.stringify(d.ind||[]));
                    probeLastLog = now;
                }
                if (curSH > 0) {
                    if (curSH > probeLastScrollHeight) { probeStall = 0; probeLastScrollHeight = curSH; }
                    else if (atScrollBottom) {
                        probeStall++;
                        if (probeStall >= PROBE_STALL_MAX && !timelineBottomReached) {
                            timelineBottomReached = true;
                            lock.setBottomReached(true);
                            logger.log("INFO", "BOTTOM DETECTED: scrollHeight stuck at " + curSH + " for " + probeStall + " probes, feeds=" + capturedIds.size);
                        }
                    }
                }
                if (hasInd) {
                    timelineBottomReached = true;
                    lock.setBottomReached(true);
                    logger.log("INFO", "BOTTOM DETECTED via UI text: " + JSON.stringify(d.ind));
                }
            } catch(e) {}
        }).catch(function(){});
    }

    function doScroll() {
        const all = electron.webContents.getAllWebContents();
        for (const wc of all) {
            try {
                const u = wc.getURL();
                if (!u.includes(CFG.urlChannelPage)) continue;
                // Jump straight to bottom + dispatch wheel event to trigger QQ's lazy-load
                wc.executeJavaScript(`
                    (function(){
                        var scrollers = document.querySelectorAll('.vue-recycle-scroller');
                        var target = null;
                        for (var i=0; i<scrollers.length; i++) {
                            if (scrollers[i].scrollHeight > 100 && scrollers[i].clientHeight > 100) {
                                target = scrollers[i]; break;
                            }
                        }
                        if (!target) return JSON.stringify({err:'no scroller'});
                        target.scrollTop = target.scrollHeight;
                        target.dispatchEvent(new WheelEvent('wheel', {deltaY: 100000, bubbles: true, cancelable: true}));
                        target.dispatchEvent(new Event('scroll', {bubbles: true}));
                        return JSON.stringify({st: target.scrollTop, sh: target.scrollHeight, ch: target.clientHeight});
                    })()
                `).then(function(r){
                    if (scrollCount % 20 === 0) logger.log("DEBUG", "ScrollJS "+scrollCount+" feeds:"+capturedIds.size+" "+r);
                }).catch(function(){});
                scrollCount++;
                if (scrollCount >= CFG.scrollMax) {
                    timelineBottomReached = true;
                    lock.setBottomReached(true);
                }
                if (scrollCount % 50 === 0) {
                    logger.log("INFO", "Scroll #"+scrollCount+" feeds:"+capturedIds.size);
                    probeBottom(wc);
                }
                break;
            } catch(e) {}
        }
        if (scrollCount < CFG.scrollMax && !timelineBottomReached) setTimeout(doScroll, CFG.scrollInterval);
        else logger.log("INFO", "doScroll end: scrollCount="+scrollCount+" bottom="+timelineBottomReached+" feeds="+capturedIds.size);
    }

    const actions = { switch_guild: switchToGuild, click_channel: clickChannel, start_scroll: () => doScroll() };
    if (CFG.startupSequence.length > 0) {
        CFG.startupSequence.forEach(step => {
            const fn = actions[step.action];
            if (fn) setTimeout(fn, step.delay_ms);
        });
        logger.log("INFO", "Scheduled " + CFG.startupSequence.length + " startup steps");
    } else {
        setTimeout(switchToGuild, 6000);
        setTimeout(switchToGuild, 15000);
        setTimeout(clickChannel, 18000);
        setTimeout(clickChannel, 28000);
        setTimeout(clickChannel, 38000);
        setTimeout(() => doScroll(), 45000);
        setTimeout(clickChannel, 55000);
    }

    function traversePosts(maxPosts, startDelay) {
        setTimeout(() => {
            try {
                const lines = fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean);
                const ids = [];
                for (let i = 0; i < lines.length && ids.length < maxPosts; i++) {
                    try { const d = JSON.parse(lines[i]); if (d.id && (d.commentCount||0) > 0) ids.push(d.id); } catch(e) {}
                }
                if (ids.length === 0) { logger.log("INFO", "Fetch: no posts with comments"); return; }
                logger.log("INFO", "Fetch: queued " + ids.length + " posts, globalBkn=" + globalBkn);

                const guildId = CFG.channelId;
                const all = electron.webContents.getAllWebContents();
                for (const wc of all) {
                    try {
                        const u = wc.getURL();
                        if (!u.includes(CFG.urlChannelPage)) continue;

                        const discoveryIds = ids.slice(0, 3);
                        const apiIds = ids;

                        discoveryIds.forEach((postId, idx) => {
                            const delay = idx * 16000;
                            setTimeout(() => {
                                wc.executeJavaScript(`
                                    (function(){
                                        var pid=${JSON.stringify(postId)};
                                        if(window.__prom_comment_oidb){
                                            console.log('[Prometheus] VR: skip '+pid.slice(0,16)+' (oidb captured)');
                                            return;
                                        }
                                        var el=document.getElementById('app');
                                        if(!el||!el.__vue_app__){return;}
                                        var rt=el.__vue_app__.config.globalProperties['\$router'];
                                        if(!rt){return;}
                                        var gid=${JSON.stringify(guildId)};
                                        console.log('[Prometheus] VR: '+pid.slice(0,16));
                                        rt.push({path:'/g/'+gid+'/post/'+pid,query:{_t:Date.now()}}).catch(function(){});
                                        setTimeout(function(){rt.push('/g/'+gid).catch(function(){})},13000);
                                    })();
                                `).catch(()=>{});
                            }, delay);
                        });

                        const apiStart = discoveryIds.length * 16000 + 2000;
                        apiIds.forEach((postId, idx) => {
                            const delay = apiStart + idx * 100;
                            setTimeout(() => {
                                wc.executeJavaScript(`
                                    (function(){
                                        if(!window.__prom_getComments){return;}
                                        var bkn=window.__prom_bkn||'${globalBkn}';
                                        if(!bkn){return;}
                                        window.__prom_bkn=bkn;
                                        var pid=${JSON.stringify(postId)};
                                        var gid=${JSON.stringify(guildId)};
                                        var oidb=window.__prom_comment_oidb?'yes':'no';
                                        console.log('[Prometheus] API: '+pid.slice(0,16)+' oidb='+oidb);
                                        window.__prom_getComments(pid,gid).then(function(r){
                                            console.log('[Prometheus] API done: '+pid.slice(0,16)+' status='+r.status);
                                        }).catch(function(e){
                                            console.log('[Prometheus] API err: '+pid.slice(0,16)+' '+e.message);
                                        });
                                    })();
                                `).catch(()=>{});
                            }, delay);
                        });
                        break;
                    } catch(e) {}
                }
            } catch(e) { logger.log("ERROR", "Fetch error: "+e.message); }
        }, startDelay);
    }

    setTimeout(() => traversePosts(30, 35000), 0);

    const traversedPostIds = new Set();
    let daemonCycle = 0;
    let lastDaemonTs = null;

    function findChannelWc() {
        const all = electron.webContents.getAllWebContents();
        for (const wc of all) {
            try { if (wc.getURL().includes(CFG.urlChannelPage)) return wc; } catch(e) {}
        }
        return null;
    }

    function daemonFetchComments(wc, postIds) {
        const guildId = CFG.channelId;
        postIds.forEach((postId, idx) => {
            setTimeout(() => {
                wc.executeJavaScript(`
                    (function(){
                        if(!window.__prom_getComments||!window.__prom_bkn)return;
                        var pid=${JSON.stringify(postId)},gid=${JSON.stringify(guildId)};
                        window.__prom_getComments(pid,gid).then(function(){
                            console.log('[Prometheus] D-API done: '+pid.slice(0,16));
                        }).catch(function(e){
                            console.log('[Prometheus] D-API err: '+pid.slice(0,16)+' '+e.message);
                        });
                    })();
                `).catch(()=>{});
            }, idx * 100);
        });
    }

    function daemonScrollToTop(wc) {
        wc.executeJavaScript(`
            (function(){
                var scrollers = document.querySelectorAll('.vue-recycle-scroller');
                for (var i=0; i<scrollers.length; i++) {
                    var s = scrollers[i];
                    if (s.scrollHeight > 100 && s.clientHeight > 100) {
                        s.scrollTop = 0;
                        s.dispatchEvent(new WheelEvent('wheel', {deltaY: -100000, bubbles: true, cancelable: true}));
                        return;
                    }
                }
            })()
        `).catch(()=>{});
    }

    function daemonScrollDown(wc, done) {
        let n = 0, stall = 0, last = capturedIds.size;
        const THRESHOLD = 20, MIN = 10, MAX = 300;
        const tick = () => {
            if ((n >= MIN && stall >= THRESHOLD) || n >= MAX) {
                logger.log("INFO", "Daemon scroll-down done: n="+n+" stall="+stall+" feeds="+capturedIds.size);
                if (done) done();
                return;
            }
            wc.executeJavaScript(`
                (function(){
                    var scrollers = document.querySelectorAll('.vue-recycle-scroller');
                    for (var i=0; i<scrollers.length; i++) {
                        var s = scrollers[i];
                        if (s.scrollHeight > 100 && s.clientHeight > 100) {
                            s.scrollTop = s.scrollHeight;
                            s.dispatchEvent(new WheelEvent('wheel', {deltaY: 100000, bubbles: true, cancelable: true}));
                            s.dispatchEvent(new Event('scroll', {bubbles: true}));
                            return;
                        }
                    }
                })()
            `).catch(()=>{});
            n++;
            if (capturedIds.size === last) { stall++; } else { stall = 0; last = capturedIds.size; }
            setTimeout(tick, CFG.scrollInterval);
        };
        tick();
    }

    function daemonLoop() {
        daemonCycle++;
        try { lock.acquire(daemonCycle); } catch(e) { logger.log("ERROR", "Lock acquire failed: " + e.message); return; }
        if (!timelineBottomReached) { logger.log("INFO", "Daemon #"+daemonCycle+": skip (initial scroll in progress, feeds="+capturedIds.size+")"); lock.release(); return; }
        const wc = findChannelWc();
        if (!wc) {
            logger.log("INFO", "Daemon #"+daemonCycle+": no webContents, navigating to guild...");
            lock.release();
            switchToGuild();
            setTimeout(() => { clickChannel(); }, 5000);
            return;
        }

        if (deadMediaQueue.length > 0) {
            logger.log("INFO", "Daemon #"+daemonCycle+": retrying " + deadMediaQueue.length + " dead media downloads");
            for (let i = deadMediaQueue.length - 1; i >= 0; i--) {
                const item = deadMediaQueue[i];
                if ((item.retries||0) >= 3) {
                    deadMediaQueue.splice(i, 1);
                    try { fs.appendFileSync(CFG.dataDir+"/dead_media_permanent.jsonl", JSON.stringify({url:item.url})+"\n"); } catch(e) {}
                } else {
                    item.retries = (item.retries||0) + 1;
                    mediaQueue.push({url:item.url, type:item.type, source:item.source});
                }
            }
            flushDeadMedia();
        }

        daemonScrollToTop(wc);
        logger.log("INFO", "Daemon #"+daemonCycle+": "+capturedIds.size+" feeds, "+capturedCommentKeys.size+" comment batches, "+mediaQueue.length+" media queued, "+traversedPostIds.size+" traversed, bottom="+timelineBottomReached);

        setTimeout(() => {
            try {
                const lines = fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean);
                const newIds = [];
                for (const l of lines) {
                    try {
                        const d = JSON.parse(l);
                        if (d.id && (d.commentCount||0) > 0 && !traversedPostIds.has(d.id)) newIds.push(d.id);
                    } catch(e) {}
                }
                if (newIds.length > 0) {
                    logger.log("INFO", "Daemon: "+newIds.length+" new posts with comments");
                    newIds.forEach(id => traversedPostIds.add(id));
                }

                if (daemonCycle % 6 === 0) {
                    const recentIds = [];
                    for (let i = lines.length - 1; i >= 0 && recentIds.length < 10; i--) {
                        try { const d = JSON.parse(lines[i]); if (d.id && (d.commentCount||0) > 0) recentIds.push(d.id); } catch(e) {}
                    }
                    if (recentIds.length > 0) {
                        logger.log("INFO", "Daemon: re-polling "+recentIds.length+" recent posts for new comments");
                        daemonFetchComments(wc, recentIds);
                    }
                }

                daemonScrollDown(wc, () => {
                    try {
                        const hash = computeStateHash();
                        const state = {
                            bottomReached: timelineBottomReached,
                            bottomTime: new Date().toISOString(),
                            feeds: capturedIds.size,
                            comments: capturedCommentKeys.size,
                            hash: hash,
                            hashFiles: STATE_FILES,
                            hashTime: new Date().toISOString()
                        };
                        fs.writeFileSync(path.join(CFG.dataDir, 'state.json'), JSON.stringify(state, null, 2));
                        lastDaemonTs = Date.now();
                        logger.log("INFO", "State: written hash=" + hash.slice(0, 16) + " feeds=" + capturedIds.size);
                        lock.release();
                    } catch(e) { logger.log("ERROR", "State write err: " + e.message); try { lock.release(); } catch(e2) {} }
                });
            } catch(e) { logger.log("ERROR", "Daemon traverse err: "+e.message); try { lock.release(); } catch(e2) {} }
        }, 10000);
    }

    if (CFG.daemonMode) {
        setTimeout(() => {
            try {
                const lines = fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean);
                for (const l of lines) { try { const d = JSON.parse(l); if (d.id && (d.commentCount||0) === 0) traversedPostIds.add(d.id); } catch(e) {} }
                logger.log("INFO", "Daemon: marked "+traversedPostIds.size+" existing posts as traversed, starting daemon (interval="+CFG.daemonInterval+"ms)");
                let requeued = 0;
                for (const l of lines) {
                    try { const d = JSON.parse(l); if (d.id) { const before = mediaQueue.length; queueMedia(extractMediaUrls(d), d.id); if (mediaQueue.length > before) requeued++; } } catch(e) {}
                }
                if (requeued > 0) logger.log("INFO", "Daemon: requeued media from "+requeued+" feeds ("+mediaQueue.length+" queued)");
                setInterval(daemonLoop, CFG.daemonInterval);
                setTimeout(() => daemonLoop(), 15000);
            } catch(e) { logger.log("ERROR", "Daemon startup err: "+e.message); }
        }, 150000);
    }

    const apiServer = new ApiServer({
        port: CFG.apiPort,
        logger: logger,
        lock: lock,
        getStats: function() {
            return {
                feeds: capturedIds.size,
                comments: capturedCommentKeys.size,
                media_total: mediaSeen.size,
                media_queued: mediaQueue.length,
                dead_media: deadMediaQueue.length,
                daemon_cycle: daemonCycle,
                last_scan_ts: lastDaemonTs ? Math.floor(lastDaemonTs / 1000) : null,
                bottom_reached: timelineBottomReached,
                uptime_seconds: Math.floor((Date.now() - (global.__promStartTime||Date.now())) / 1000)
            };
        },
        getConfig: function() {
            var safe = {};
            for (var k in CFG) {
                if (typeof CFG[k] !== 'function' && k !== 'startupSequence') {
                    safe[k] = CFG[k];
                }
            }
            return safe;
        },
        setConfig: function(newCfg) {
            for (var k in newCfg) {
                if (k === 'daemonInterval') CFG.daemonInterval = newCfg[k];
                else if (k === 'apiPort') CFG.apiPort = newCfg[k];
                else if (k in CFG) CFG[k] = newCfg[k];
            }
            logger.log("INFO", "Config updated via API: " + JSON.stringify(Object.keys(newCfg)));
        },
        triggerDaemon: function() {
            logger.log("INFO", "Manual daemon trigger via API");
            daemonLoop();
        }
    });
    try {
        apiServer.start();
        logger.log("INFO", "API server started on port " + CFG.apiPort);
    } catch(e) {
        logger.log("ERROR", "API server failed to start: " + e.message);
    }
}

try { require(path.join(__dirname, "..", "application.asar", "app_launcher", "index.js")); logger.log("INFO", "QQ loaded"); }
catch(e) { logger.log("ERROR", "QQ err: " + e.message); require(path.join(process.resourcesPath, "app", "application.asar", "app_launcher", "index.js")); }
