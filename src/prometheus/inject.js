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
    idOnly:         E.PROMETHEUS_ID_ONLY === "true" || E.PROMETHEUS_ID_ONLY === "1",
};
try {
    CFG.startupSequence = JSON.parse(E.PROMETHEUS_STARTUP_SEQUENCE || "[]");
} catch (e) {
    CFG.startupSequence = [];
}
try {
    CFG.guilds = JSON.parse(E.PROMETHEUS_GUILDS_JSON || "[]");
    if (!Array.isArray(CFG.guilds)) CFG.guilds = [];
} catch (e) {
    CFG.guilds = [];
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
    scanDepth: CFG.scanDepth, feedIdPrefix: CFG.feedIdPrefix,
    idOnly: CFG.idOnly, guilds: CFG.guilds.length
}));

const capturedIds = new Set();
if (CFG.guilds.length === 0) {
    try { (fs.readFileSync(CFG.dataDir+"/ids.json","utf8")||"").split("\n").filter(Boolean).forEach(id => capturedIds.add(id)); } catch(e) {}
    try { fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean).forEach(l=>{try{const d=JSON.parse(l);if(d.id)capturedIds.add(d.id)}catch(e){}}); } catch(e) {}
    logger.log("INFO", "Loaded "+capturedIds.size+" IDs");
    setInterval(()=>{try{fs.writeFileSync(CFG.dataDir+"/ids.json",[...capturedIds].join("\n"))}catch(e){}},10000);
} else {
    logger.log("INFO", "Multi-guild mode: per-guild preload deferred to getGuildState, skipping flat ids.json");
}

const mediaSeen = new Set();
const mediaQueue = [];
const deadMediaQueue = [];
if (!CFG.idOnly) {
    try { (fs.readFileSync(CFG.dataDir+"/dead_media.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{deadMediaQueue.push(JSON.parse(l))}catch(e){}}); } catch(e) {}
    try { (fs.readFileSync(CFG.dataDir+"/media_index.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{var e=JSON.parse(l);if(e.url)mediaSeen.add(e.url)}catch(e){}}); } catch(e) {}
    try { (fs.readFileSync(CFG.dataDir+"/dead_media_permanent.jsonl","utf8")||"").split("\n").filter(Boolean).forEach(l=>{try{mediaSeen.add(JSON.parse(l).url)}catch(e){}}); } catch(e) {}
    try { fs.mkdirSync(CFG.dataDir+"/media",{recursive:true}); } catch(e) {}
}

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

const guildStates = new Map();
let currentGuildId = CFG.channelId;

    function getGuildState(guildId) {
        guildId = String(guildId);
        if (!guildId) return null;
    if (!guildStates.has(guildId)) {
        const dataDir = path.join(CFG.dataDir, guildId);
        try { fs.mkdirSync(dataDir, {recursive:true}); } catch(e) {}
        const gs = { capturedIds: new Set(), dataDir: dataDir, bottomReached: false };
        try {
            (fs.readFileSync(dataDir + "/feeds.jsonl", "utf8") || "")
                .split("\n").filter(Boolean).forEach(function(l) {
                    try { const d = JSON.parse(l); if (d.id) gs.capturedIds.add(d.id); } catch(e) {}
                });
        } catch(e) {}
        guildStates.set(guildId, gs);
        logger.log("INFO", "[guild " + guildId + "] state init: dataDir=" + dataDir + " preloaded=" + gs.capturedIds.size);
    }
    return guildStates.get(guildId);
}

function isGuildAllowed(guildId) {
    if (CFG.guilds.length === 0) return true;
    if (!guildId) return false;
    return CFG.guilds.some(function(g) { return String(g.guild_id) === String(guildId); });
}

function saveFeed(o) {
    try {
        const fid = o.id || "";
        if (!fid) return;
        const sign = (o.channelInfo && o.channelInfo.sign) || {};
        const gid = sign.guild_id != null ? String(sign.guild_id) : "";
        const multiGuild = CFG.guilds.length > 0;

        if (multiGuild) {
            if (!isGuildAllowed(gid)) return;
        } else {
            if (sign.guild_id && sign.guild_id !== CFG.channelId) return;
        }

        const gs = multiGuild ? getGuildState(gid) : null;
        const ids = multiGuild ? gs.capturedIds : capturedIds;
        const outDir = multiGuild ? gs.dataDir : CFG.dataDir;
        if (ids.has(fid)) return;

        fs.appendFileSync(outDir + "/feeds.jsonl", JSON.stringify(o) + "\n");
        ids.add(fid);
        if (multiGuild) capturedIds.add(fid);
        if (!CFG.idOnly) queueMedia(extractMediaUrls(o), fid);
    } catch(e) {
        try { fs.appendFileSync(CFG.dataDir + "/prometheus.log", `[${new Date().toISOString()}] saveFeed err: ${e.message} fid=${(o && o.id) || '?'}\n`); } catch(e2) {}
    }
}

const INJECT_JS = `
(function(){if(window.__P)return;window.__P=1;
window._oRAF=requestAnimationFrame;window._oCAF=cancelAnimationFrame;
requestAnimationFrame=function(cb){return setTimeout(function(){cb(performance.now())},0)};
cancelAnimationFrame=function(id){clearTimeout(id)};
var PREFIX=${JSON.stringify(CFG.feedIdPrefix)},D=${CFG.scanDepth},AD=${CFG.scanArrayDepth};
var o=JSON.parse;JSON.parse=function(){var r=o.apply(this,arguments);try{if(r&&typeof r==='object'){var f=[];(function F(x,d,cfid){if(d>D||!x||typeof x!=='object')return;var fid=cfid;if(x.id&&typeof x.id==='string'&&x.id.startsWith(PREFIX)&&(x.createTime||x.poster||x.title)){fid=x.id;f.push(x)}if(Array.isArray(x.feeds))x.feeds.forEach(function(i){if(i&&i.id&&typeof i.id==='string'&&i.id.startsWith(PREFIX))f.push(i)});if(d<AD)for(var k in x)if(x[k]&&typeof x[k]==='object')F(x[k],d+1,fid)})(r,0,'');for(var i=0;i<f.length;i++)console.log('[P]'+JSON.stringify(f[i]))}}catch(e){}return r}})();
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
    if (!CFG.idOnly) setInterval(flushDeadMedia, 10000);

    setInterval(function() {
        var n = 0;
        while (mediaQueue.length > 0 && n < 20) {
            var item = mediaQueue.shift();
            downloadOne(item.url, item.type, item.source);
            n++;
        }
    }, 2000);

    app.on('web-contents-created', (ev, wc) => {
        wc.on('dom-ready', () => {
            wc.executeJavaScript(INJECT_JS).catch(()=>{});
        });
        wc.on('console-message', (e, lvl, msg) => {
            if (msg.startsWith('[P]')) { try { saveFeed(JSON.parse(msg.substring(3))); } catch(err) {} }
            else if (msg.startsWith('[Prometheus]')) logger.log("INFO", "R: "+msg);
            else if (lvl > 2) {
                // Filter known QQ renderer noise to DEBUG; real errors stay ERROR
                const noise = /UnitedConfigService|ResizeObserver loop|AioTimestampService|Failed to parse feed\.json_feed/;
                logger.log(noise.test(msg) ? "DEBUG" : "ERROR", "E: " + msg.slice(0, 200));
            }
        });
    });

    function switchToGuild(guildConfig) {
        const tag = guildConfig ? `[guild: ${guildConfig.name}] ` : '';
        const tagJS = JSON.stringify(tag);
        BrowserWindow.getAllWindows().forEach(win => {
            const u = win.webContents.getURL();
            if (!u.includes(CFG.urlGuildPage) || u.includes(CFG.urlHiddenWin)) return;
            win.webContents.executeJavaScript(`
                (function(){
                    if (location.hash.includes('/main/guild')) { console.log('[Prometheus] ' + ${tagJS} + 'Guild OK'); return; }
                    var menu = ${JSON.stringify(CFG.guildMenuText)};
                    document.querySelectorAll('*').forEach(el => {
                        if (el.children.length<3 && el.textContent.trim()===menu) el.click();
                    });
                    location.hash = '#/main/guild';
                    console.log('[Prometheus] ' + ${tagJS} + 'Hash -> guild');
                })();
            `).catch(()=>{});
        });
    }

    function clickChannel(guildConfig) {
        const channelId = (guildConfig && guildConfig.guild_id) || CFG.channelId;
        const channelName = (guildConfig && guildConfig.name) || CFG.channelName;
        const tag = guildConfig ? `[guild: ${guildConfig.name}] ` : '';
        const tagJS = JSON.stringify(tag);
        const all = electron.webContents.getAllWebContents();
        all.forEach(wc => {
            try {
                if (!wc.getURL().includes(CFG.urlChannelPage)) return;
                wc.executeJavaScript(`
                    (function(){
                        var CNAME = ${JSON.stringify(channelName)};
                        if (location.href.includes('/channels/${channelId}') || location.href.includes('/g/${channelId}')) {
                            if (!window.__PClicked) { window.__PClicked = true; console.log('[Prometheus] ' + ${tagJS} + 'Already on channel'); }
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
                            console.log('[Prometheus] ' + ${tagJS} + 'Clicking: ' + best.textContent.trim().slice(0,30));
                            best.click();
                            window.__PClicked = true;
                        } else {
                            console.log('[Prometheus] ' + ${tagJS} + 'Channel element not found. Body text sample: ' + (document.body ? document.body.innerText.slice(0,100) : 'no body'));
                        }
                    })();
                `).catch(()=>{});
            } catch(e) {}
        });
    }

    let scrollCount = 0;
    let currentGuildConfig = null;
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

    function doScroll(done) {
        const tag = CFG.guilds.length > 0 ? `[guild: ${currentGuildConfig ? currentGuildConfig.name : CFG.channelId}] ` : '';
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
                    if (scrollCount % 20 === 0) logger.log("DEBUG", tag + "ScrollJS "+scrollCount+" feeds:"+capturedIds.size+" "+r);
                }).catch(function(){});
                scrollCount++;
                if (scrollCount >= CFG.scrollMax) {
                    timelineBottomReached = true;
                    lock.setBottomReached(true);
                }
                if (scrollCount % 50 === 0) {
                    logger.log("INFO", tag + "Scroll #"+scrollCount+" feeds:"+capturedIds.size);
                    probeBottom(wc);
                }
                break;
            } catch(e) {}
        }
        const guildDone = CFG.guilds.length > 0 && currentGuildId && getGuildState(currentGuildId) && getGuildState(currentGuildId).bottomReached;
        if (scrollCount < CFG.scrollMax && !timelineBottomReached && !guildDone) {
            setTimeout(() => doScroll(done), CFG.scrollInterval);
        } else {
            logger.log("INFO", tag + "doScroll end: scrollCount="+scrollCount+" bottom="+timelineBottomReached+" feeds="+capturedIds.size);
            if (done) done();
        }
    }

    function runMultiGuildSequence() {
        const guilds = CFG.guilds;
        let idx = 0;
        logger.log("INFO", "Multi-guild startup: " + guilds.length + " guilds queued");
        switchToGuild();
        setTimeout(() => switchToGuild(), 9000);
        function nextGuild() {
            if (idx >= guilds.length) {
                logger.log("INFO", "All " + guilds.length + " guilds completed (total feeds=" + capturedIds.size + ")");
                return;
            }
            const g = guilds[idx];
            idx++;
            const gs = getGuildState(g.guild_id);
            currentGuildId = g.guild_id;
            currentGuildConfig = g;
            gs.bottomReached = false;
            scrollCount = 0;
            timelineBottomReached = false;
            probeLastScrollHeight = 0;
            probeStall = 0;
            logger.log("INFO", `[guild: ${g.name}] start (id=${g.guild_id} num=${g.guild_number})`);
            clickChannel(g);
            setTimeout(() => clickChannel(g), 5000);
            setTimeout(() => clickChannel(g), 10000);
            setTimeout(() => doScroll(() => {
                gs.bottomReached = true;
                logger.log("INFO", `[guild: ${g.name}] completed (feeds=${gs.capturedIds.size})`);
                if (idx < guilds.length) {
                    switchToGuild();
                    setTimeout(nextGuild, 9000);
                }
            }), 15000);
        }
        setTimeout(nextGuild, 18000);
    }

    const actions = { switch_guild: switchToGuild, click_channel: clickChannel, start_scroll: () => doScroll() };
    if (CFG.guilds.length > 0) {
        setTimeout(runMultiGuildSequence, 6000);
    } else if (CFG.startupSequence.length > 0) {
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

    let daemonCycle = 0;
    let lastDaemonTs = null;

    function findChannelWc() {
        const all = electron.webContents.getAllWebContents();
        for (const wc of all) {
            try { if (wc.getURL().includes(CFG.urlChannelPage)) return wc; } catch(e) {}
        }
        return null;
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
        if (CFG.idOnly) return;
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
        logger.log("INFO", "Daemon #"+daemonCycle+": "+capturedIds.size+" feeds, "+mediaQueue.length+" media queued, bottom="+timelineBottomReached);

        setTimeout(() => {
            try {
                daemonScrollDown(wc, () => {
                    try {
                        const hash = computeStateHash();
                        const state = {
                            bottomReached: timelineBottomReached,
                            bottomTime: new Date().toISOString(),
                            feeds: capturedIds.size,
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
            } catch(e) { logger.log("ERROR", "Daemon cycle err: "+e.message); try { lock.release(); } catch(e2) {} }
        }, 10000);
    }

    if (CFG.daemonMode && !CFG.idOnly && CFG.guilds.length === 0) {
        setTimeout(() => {
            try {
                const lines = fs.readFileSync(CFG.dataDir+"/feeds.jsonl","utf8").split("\n").filter(Boolean);
                logger.log("INFO", "Daemon: starting with "+lines.length+" existing feeds (interval="+CFG.daemonInterval+"ms)");
                let requeued = 0;
                for (const l of lines) {
                    try { const d = JSON.parse(l); if (d.id) { const before = mediaQueue.length; queueMedia(extractMediaUrls(d), d.id); if (mediaQueue.length > before) requeued++; } } catch(e) {}
                }
                if (requeued > 0) logger.log("INFO", "Daemon: requeued media from "+requeued+" feeds ("+mediaQueue.length+" queued)");
                setInterval(daemonLoop, CFG.daemonInterval);
                setTimeout(() => daemonLoop(), 15000);
            } catch(e) { logger.log("ERROR", "Daemon startup err: "+e.message); }
        }, 150000);
    } else if (CFG.idOnly) {
        logger.log("INFO", "Daemon startup skipped: ID_ONLY fast mode");
    } else if (CFG.guilds.length > 0) {
        logger.log("INFO", "Daemon startup skipped: multi-guild mode (per-guild state, daemon uses single-guild state)");
    }

    const apiServer = new ApiServer({
        port: CFG.apiPort,
        logger: logger,
        lock: lock,
        getStats: function() {
            return {
                feeds: capturedIds.size,
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
