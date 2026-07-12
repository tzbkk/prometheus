"use strict";

const http = require("node:http");

const VALID_LOG_LEVELS = ["ERROR", "WARN", "INFO", "DEBUG"];
const HOST = "127.0.0.1";

class ApiServer {
    constructor({
        port = 9420,
        logger = null,
        lock = null,
        getStats = null,
        getConfig = null,
        setConfig = null,
        triggerDaemon = null,
    } = {}) {
        this.port = port;
        this.logger = logger;
        this.lock = lock;
        this.getStats = getStats;
        this.getConfig = getConfig;
        this.setConfig = setConfig;
        this.triggerDaemon = triggerDaemon;
        this.server = null;
    }

    address() {
        return this.server ? this.server.address() : null;
    }

    start() {
        return new Promise((resolve, reject) => {
            const server = http.createServer((req, res) => this._handle(req, res));
            this.server = server;

            const onError = (err) => {
                if (err && err.code === "EADDRINUSE") {
                    reject(new Error("port " + this.port + " already in use"));
                } else {
                    reject(err);
                }
            };

            server.once("error", onError);
            server.listen(this.port, HOST, () => {
                server.removeListener("error", onError);
                const addr = server.address();
                if (addr && typeof addr.port === "number") this.port = addr.port;
                resolve();
            });
        });
    }

    stop() {
        return new Promise((resolve) => {
            if (!this.server) return resolve();
            this.server.close(() => {
                this.server = null;
                resolve();
            });
        });
    }

    _handle(req, res) {
        const url = new URL(req.url, "http://" + HOST);
        const path = url.pathname;
        const method = req.method;

        const route = this._route(method, path);
        if (!route) return this._send(res, 404, { ok: false, data: {}, error: "not found" });

        if (route === "readBody") {
            return this._readBody(req, (err, body) => {
                if (err) {
                    return this._send(res, 400, {
                        ok: false,
                        data: {},
                        error: "invalid json: " + err.message,
                    });
                }
                this._handlePutConfig(res, body);
            });
        }

        switch (route) {
            case "health":
                return this._send(res, 200, { ok: true, data: {}, error: "" });
            case "logs":
                return this._handleLogs(res, url);
            case "stats":
                return this._handleStats(res);
            case "getConfig":
                return this._handleGetConfig(res);
            case "triggerDaemon":
                return this._handleTriggerDaemon(res);
            default:
                return this._send(res, 404, { ok: false, data: {}, error: "not found" });
        }
    }

    _route(method, path) {
        if (method === "GET" && path === "/health") return "health";
        if (method === "GET" && path === "/logs") return "logs";
        if (method === "GET" && path === "/stats") return "stats";
        if (method === "GET" && path === "/config") return "getConfig";
        if (method === "PUT" && path === "/config") return "readBody";
        if (method === "POST" && path === "/action/trigger-daemon") return "triggerDaemon";
        return null;
    }

    _handleLogs(res, url) {
        const since = this._parseInt(url.searchParams.get("since"), 0);
        const max = this._parseInt(url.searchParams.get("max"), 100);
        const logs = this.logger ? this.logger.getLogs(since, max) : [];
        this._send(res, 200, {
            ok: true,
            data: { logs, total: logs.length },
            error: "",
        });
    }

    _handleStats(res) {
        let stats = {};
        try {
            if (this.getStats) stats = this.getStats() || {};
        } catch (e) {
            stats = {};
        }
        this._send(res, 200, { ok: true, data: stats, error: "" });
    }

    _handleGetConfig(res) {
        let config = {};
        try {
            if (this.getConfig) config = this.getConfig() || {};
        } catch (e) {
            config = {};
        }
        this._send(res, 200, { ok: true, data: config, error: "" });
    }

    _handlePutConfig(res, body) {
        if (!body || typeof body !== "object" || Array.isArray(body)) {
            return this._send(res, 400, {
                ok: false,
                data: {},
                error: "validation: body must be a JSON object",
            });
        }

        const validationError = this._validateConfig(body);
        if (validationError) {
            return this._send(res, 400, {
                ok: false,
                data: {},
                error: "validation: " + validationError,
            });
        }

        try {
            if (this.setConfig) this.setConfig(body);
        } catch (e) {
            return this._send(res, 500, {
                ok: false,
                data: {},
                error: "setConfig failed: " + e.message,
            });
        }
        this._send(res, 200, {
            ok: true,
            data: { applied_next_cycle: true },
            error: "",
        });
    }

    _handleTriggerDaemon(res) {
        try {
            if (this.triggerDaemon) this.triggerDaemon();
        } catch (e) {
            return this._send(res, 500, {
                ok: false,
                data: {},
                error: "triggerDaemon failed: " + e.message,
            });
        }
        this._send(res, 200, {
            ok: true,
            data: { triggered: true },
            error: "",
        });
    }

    _validateConfig(body) {
        const has = Object.prototype.hasOwnProperty.call(body, "api_port");
        if (has) {
            const v = body.api_port;
            if (typeof v !== "number" || Number.isNaN(v) || !Number.isFinite(v)) {
                return "api_port must be a number";
            }
            if (v < 1024 || v > 65535) {
                return "api_port must be between 1024 and 65535";
            }
        }
        if (Object.prototype.hasOwnProperty.call(body, "daemon_interval_ms")) {
            const v = body.daemon_interval_ms;
            if (typeof v !== "number" || Number.isNaN(v) || !Number.isFinite(v) || v <= 0) {
                return "daemon_interval_ms must be a number greater than 0";
            }
        }
        if (Object.prototype.hasOwnProperty.call(body, "log_level")) {
            const v = body.log_level;
            if (typeof v !== "string" || !VALID_LOG_LEVELS.includes(v.toUpperCase())) {
                return "log_level must be one of ERROR, WARN, INFO, DEBUG";
            }
        }
        if (Object.prototype.hasOwnProperty.call(body, "log_buffer_lines")) {
            const v = body.log_buffer_lines;
            if (typeof v !== "number" || Number.isNaN(v) || !Number.isFinite(v) || v <= 0) {
                return "log_buffer_lines must be a number greater than 0";
            }
        }
        return null;
    }

    _parseInt(raw, fallback) {
        if (raw === null || raw === undefined || raw === "") return fallback;
        const n = Number(raw);
        return Number.isFinite(n) ? n : fallback;
    }

    _readBody(req, cb) {
        const chunks = [];
        let size = 0;
        const MAX = 1 * 1024 * 1024;
        req.on("data", (chunk) => {
            size += chunk.length;
            if (size > MAX) {
                req.destroy();
                cb(new Error("body too large"));
                return;
            }
            chunks.push(chunk);
        });
        req.on("end", () => {
            const raw = Buffer.concat(chunks).toString("utf8");
            if (raw.length === 0) return cb(new Error("empty body"));
            try {
                cb(null, JSON.parse(raw));
            } catch (e) {
                cb(e);
            }
        });
        req.on("error", cb);
    }

    _send(res, status, envelope) {
        const body = JSON.stringify(envelope);
        res.writeHead(status, {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": Buffer.byteLength(body),
        });
        res.end(body);
    }
}

module.exports = { ApiServer };
