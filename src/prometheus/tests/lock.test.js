"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const { Lock } = require("../lock.js");

function makeTempDir() {
    return fs.mkdtempSync(path.join(os.tmpdir(), "prometheus-lock-test-"));
}

function mockLogger() {
    const calls = [];
    const logger = { log: (level, msg) => calls.push({ level, msg }) };
    Object.defineProperty(logger, "_calls", { value: calls });
    return logger;
}

const lockPath = (dir) => path.join(dir, "prometheus.lock");
const readLock = (dir) => JSON.parse(fs.readFileSync(lockPath(dir), "utf8"));
const writeLock = (dir, obj) =>
    fs.writeFileSync(lockPath(dir), JSON.stringify(obj, null, 2));

test("acquire writes correct lock file structure", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(7);
    const data = readLock(dir);
    assert.equal(data.pid, process.pid);
    assert.equal(data.dirty, true);
    assert.equal(data.cycle, 7);
    assert.deepEqual(data.pending_media, []);
    assert.equal(typeof data.ts, "number");
});

test("double acquire with live PID and dirty throws", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(1);
    assert.throws(() => lock.acquire(2), /another instance running/);
});

test("acquire with live PID but dirty=false does not throw", () => {
    const dir = makeTempDir();
    writeLock(dir, {
        pid: process.pid,
        dirty: false,
        cycle: 3,
        bottomReached: true,
        pending_media: [],
        ts: Date.now(),
    });
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(5);
    const data = readLock(dir);
    assert.equal(data.cycle, 5);
    assert.equal(data.dirty, true);
    assert.equal(data.bottomReached, true);
});

test("stale lock with dead PID and dirty can be overwritten", () => {
    const dir = makeTempDir();
    writeLock(dir, {
        pid: 99999,
        dirty: true,
        cycle: 3,
        pending_media: [],
        ts: Date.now(),
    });
    const logger = mockLogger();
    const lock = new Lock({ dataDir: dir, logger });
    lock.acquire(5);
    const data = readLock(dir);
    assert.equal(data.pid, process.pid);
    assert.equal(data.cycle, 5);
    assert.equal(data.dirty, true);
    assert.ok(
        logger._calls.some((c) => c.level === "WARN" && /99999/.test(String(c.msg))),
        "should log warning mentioning dead pid"
    );
});

test("acquire preserves bottomReached from previous lock", () => {
    const dir = makeTempDir();
    writeLock(dir, {
        pid: 99999,
        dirty: true,
        cycle: 2,
        bottomReached: true,
        pending_media: [],
        ts: Date.now(),
    });
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(3);
    const data = readLock(dir);
    assert.equal(data.bottomReached, true);
});

test("addPendingMedia / removePendingMedia correctly modify and persist", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(1);

    lock.addPendingMedia("https://example.com/a.jpg", "/data/media/a.jpg", 1024);
    let data = readLock(dir);
    assert.equal(data.pending_media.length, 1);
    assert.deepEqual(data.pending_media[0], {
        url: "https://example.com/a.jpg",
        file: "/data/media/a.jpg",
        expected_size: 1024,
    });

    lock.addPendingMedia("https://example.com/b.mp4", "/data/media/b.mp4", 2048);
    data = readLock(dir);
    assert.equal(data.pending_media.length, 2);

    lock.removePendingMedia("https://example.com/a.jpg");
    data = readLock(dir);
    assert.equal(data.pending_media.length, 1);
    assert.equal(data.pending_media[0].url, "https://example.com/b.mp4");

    lock.removePendingMedia("https://nope.example.com");
    data = readLock(dir);
    assert.equal(data.pending_media.length, 1);
});

test("release sets dirty=false and keeps file", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(1);
    assert.ok(fs.existsSync(lockPath(dir)), "lock exists after acquire");
    lock.release();
    assert.ok(fs.existsSync(lockPath(dir)), "lock still exists after release");
    const data = readLock(dir);
    assert.equal(data.dirty, false);
    assert.deepEqual(data.pending_media, []);
});

test("checkAndRecover: no lock -> null", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    const result = lock.checkAndRecover();
    assert.equal(result, null);
});

test("checkAndRecover: dirty=true -> recovery data from lock", () => {
    const dir = makeTempDir();
    const pending = [
        {
            url: "https://example.com/x.jpg",
            file: "/data/x.jpg",
            expected_size: 500,
        },
    ];
    writeLock(dir, {
        pid: 99999,
        dirty: true,
        cycle: 2,
        bottomReached: true,
        pending_media: pending,
        ts: Date.now(),
    });
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    const result = lock.checkAndRecover();
    assert.deepEqual(result, {
        crashed: true,
        bottomReached: true,
        pendingMedia: pending,
    });
});

test("checkAndRecover: dirty=false -> null (clean idle)", () => {
    const dir = makeTempDir();
    writeLock(dir, {
        pid: 99999,
        dirty: false,
        cycle: 1,
        bottomReached: true,
        pending_media: [],
        ts: Date.now(),
    });
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    const result = lock.checkAndRecover();
    assert.equal(result, null);
});

test("setBottomReached persists to lock file", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.setBottomReached(true);
    const data = readLock(dir);
    assert.equal(data.bottomReached, true);
});

test("setBottomReached on existing lock preserves other fields", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(3);
    lock.addPendingMedia("https://example.com/a.jpg", "/data/a.jpg", 100);
    lock.setBottomReached(true);
    const data = readLock(dir);
    assert.equal(data.bottomReached, true);
    assert.equal(data.cycle, 3);
    assert.equal(data.dirty, true);
    assert.equal(data.pending_media.length, 1);
});

test("setBottomReached is idempotent (no rewrite if same value)", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.setBottomReached(true);
    const ts1 = readLock(dir).ts;
    lock.setBottomReached(true);
    const ts2 = readLock(dir).ts;
    assert.equal(ts1, ts2);
});

test("readBottomReached: returns undefined when no lock", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    assert.equal(lock.readBottomReached(), undefined);
});

test("readBottomReached: returns value from lock", () => {
    const dir = makeTempDir();
    writeLock(dir, {
        pid: 99999,
        dirty: false,
        cycle: 1,
        bottomReached: true,
        pending_media: [],
        ts: Date.now(),
    });
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    assert.equal(lock.readBottomReached(), true);
});

test("constructor creates dataDir if missing (recursive)", () => {
    const base = makeTempDir();
    const nested = path.join(base, "a", "b", "c");
    assert.equal(fs.existsSync(nested), false);
    // eslint-disable-next-line no-new
    new Lock({ dataDir: nested, logger: mockLogger() });
    assert.equal(fs.existsSync(nested), true);
});

test("lock file path is dataDir + /prometheus.lock", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    assert.equal(lock.lockPath, path.join(dir, "prometheus.lock"));
});

test("no .tmp file left after write", () => {
    const dir = makeTempDir();
    const lock = new Lock({ dataDir: dir, logger: mockLogger() });
    lock.acquire(1);
    lock.setBottomReached(true);
    lock.addPendingMedia("https://x", "/x", 1);
    assert.equal(
        fs.existsSync(path.join(dir, "prometheus.lock.tmp")),
        false,
        "no tmp file should remain"
    );
});
