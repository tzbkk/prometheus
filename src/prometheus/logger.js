"use strict";

const fs = require("node:fs");
const path = require("node:path");

const LEVELS = { ERROR: 0, WARN: 1, INFO: 2, DEBUG: 3 };
const ROTATE_THRESHOLD = 10 * 1024 * 1024;
const MAX_ROTATED = 3;

class Logger {
    constructor({ logDir, logLevel = "INFO", bufferLines = 500, logFile = "prometheus.log" } = {}) {
        if (!logDir) throw new Error("logDir is required");
        this.logDir = logDir;
        this.logLevel = String(logLevel).toUpperCase();
        this.bufferLines = bufferLines;
        this.logFile = logFile;
        this.logPath = path.join(logDir, logFile);

        this.buffer = [];
        this.seq = 0;
        this.totalLogged = 0;

        fs.mkdirSync(logDir, { recursive: true });
    }

    log(level, msg) {
        const lv = String(level).toUpperCase();
        const rank = LEVELS[lv];
        if (rank === undefined) return;
        const threshold = LEVELS[this.logLevel];
        if (threshold === undefined) return;
        if (rank > threshold) return;

        this.seq += 1;
        const timestamp = new Date().toISOString();
        const entry = { level: lv, timestamp, message: String(msg), seq: this.seq };
        this.totalLogged += 1;

        this.buffer.push(entry);
        if (this.buffer.length > this.bufferLines) this.buffer.shift();

        const line = `[${lv}] ${timestamp} ${entry.message}\n`;
        try {
            if (fs.existsSync(this.logPath) && fs.statSync(this.logPath).size > ROTATE_THRESHOLD) {
                this._rotate();
            }
            fs.appendFileSync(this.logPath, line);
        } catch (e) {}
    }

    _rotate() {
        for (let i = MAX_ROTATED; i >= 1; i--) {
            const src = `${this.logPath}.${i}`;
            const dst = `${this.logPath}.${i + 1}`;
            if (fs.existsSync(src)) {
                if (i === MAX_ROTATED) {
                    fs.unlinkSync(src);
                } else {
                    fs.renameSync(src, dst);
                }
            }
        }
        fs.renameSync(this.logPath, `${this.logPath}.1`);
    }

    getLogs(sinceSeq = 0, maxLines = 100) {
        const out = [];
        for (let i = 0; i < this.buffer.length && out.length < maxLines; i++) {
            if (this.buffer[i].seq > sinceSeq) out.push(this.buffer[i]);
        }
        return out;
    }

    getStats() {
        let size = 0;
        try {
            if (fs.existsSync(this.logPath)) size = fs.statSync(this.logPath).size;
        } catch (e) {}
        return {
            total_logged: this.totalLogged,
            buffer_count: this.buffer.length,
            current_level: this.logLevel,
            log_file_size: size,
        };
    }
}

module.exports = { Logger, LEVELS };
