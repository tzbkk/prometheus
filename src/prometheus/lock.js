"use strict";

const fs = require("node:fs");
const path = require("node:path");

const LOCK_FILE = "prometheus.lock";
const STATE_FILE = "state.json";

class Lock {
    constructor({ dataDir, logger } = {}) {
        if (!dataDir) throw new Error("dataDir is required");
        if (!logger) throw new Error("logger is required");
        this.dataDir = dataDir;
        this.logger = logger;
        this.lockPath = path.join(dataDir, LOCK_FILE);
        this.statePath = path.join(dataDir, STATE_FILE);
        fs.mkdirSync(dataDir, { recursive: true });
    }

    _atomicWrite(state) {
        state.ts = Date.now();
        const tmp = this.lockPath + ".tmp";
        fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
        fs.renameSync(tmp, this.lockPath);
    }

    acquire(cycleNum) {
        const old = this._readLock();
        if (old && old.pid && old.dirty) {
            let alive = false;
            try {
                process.kill(old.pid, 0);
                alive = true;
            } catch (e) {
                alive = false;
            }
            if (alive) {
                throw new Error("another instance running (pid=" + old.pid + ")");
            }
            this.logger.log(
                "WARN",
                "stale lock from dead pid=" + old.pid + ", overwriting"
            );
        }
        const state = {
            pid: process.pid,
            dirty: true,
            cycle: cycleNum,
            bottomReached: old ? old.bottomReached : undefined,
            pending_media: [],
            ts: Date.now(),
        };
        this._atomicWrite(state);
        return state;
    }

    _readLock() {
        if (!fs.existsSync(this.lockPath)) return null;
        try {
            return JSON.parse(fs.readFileSync(this.lockPath, "utf8"));
        } catch (e) {
            return null;
        }
    }

    _writeLock(state) {
        this._atomicWrite(state);
    }

    addPendingMedia(url, file, expectedSize) {
        const state = this._readLock();
        if (!state) throw new Error("no lock held");
        if (!Array.isArray(state.pending_media)) state.pending_media = [];
        state.pending_media.push({ url, file, expected_size: expectedSize });
        this._writeLock(state);
    }

    removePendingMedia(url) {
        const state = this._readLock();
        if (!state) throw new Error("no lock held");
        if (!Array.isArray(state.pending_media)) return;
        state.pending_media = state.pending_media.filter((m) => m.url !== url);
        this._writeLock(state);
    }

    release() {
        const state = this._readLock();
        if (state) {
            state.dirty = false;
            state.pending_media = [];
            this._writeLock(state);
        }
    }

    setBottomReached(value) {
        const state = this._readLock();
        if (state && state.bottomReached === value) return;
        const next = state || {
            pid: process.pid,
            dirty: false,
            cycle: 0,
            pending_media: [],
        };
        next.bottomReached = value;
        this._atomicWrite(next);
    }

    readBottomReached() {
        const state = this._readLock();
        if (!state) return undefined;
        return state.bottomReached;
    }

    checkAndRecover() {
        const lockState = this._readLock();
        if (!lockState) return null;
        if (lockState.dirty) {
            return {
                crashed: true,
                bottomReached: lockState.bottomReached,
                pendingMedia: Array.isArray(lockState.pending_media)
                    ? lockState.pending_media
                    : [],
            };
        }
        return null;
    }
}

module.exports = { Lock };
