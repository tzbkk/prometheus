"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const { Logger } = require("../logger.js");

function makeTempDir() {
    return fs.mkdtempSync(path.join(os.tmpdir(), "prometheus-log-test-"));
}

function readLog(logDir, logFile = "prometheus.log") {
    return fs.readFileSync(path.join(logDir, logFile), "utf8");
}

test("constructor accepts options with defaults", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir });
    assert.equal(log.logLevel, "INFO");
    assert.equal(log.bufferLines, 500);
    assert.equal(log.logFile, "prometheus.log");
});

test("constructor creates logDir if missing (recursive)", () => {
    const base = makeTempDir();
    const nested = path.join(base, "a", "b", "c");
    assert.equal(fs.existsSync(nested), false);
    // eslint-disable-next-line no-new
    new Logger({ logDir: nested });
    assert.equal(fs.existsSync(nested), true);
});

test("each level outputs correctly to file (ERROR/WARN/INFO/DEBUG)", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "DEBUG" });
    log.log("ERROR", "err msg");
    log.log("WARN", "warn msg");
    log.log("INFO", "info msg");
    log.log("DEBUG", "debug msg");

    const content = readLog(dir);
    assert.match(content, /\[ERROR\] \S+ err msg/);
    assert.match(content, /\[WARN\] \S+ warn msg/);
    assert.match(content, /\[INFO\] \S+ info msg/);
    assert.match(content, /\[DEBUG\] \S+ debug msg/);
});

test("file line format is [LEVEL] ISO_TIMESTAMP message", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "DEBUG" });
    log.log("INFO", "hello world");
    const lines = readLog(dir).trimEnd().split("\n");
    assert.equal(lines.length, 1);
    assert.match(lines[0], /^\[INFO\] \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z hello world$/);
});

test("logLevel=INFO -> DEBUG not output", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("ERROR", "e");
    log.log("WARN", "w");
    log.log("INFO", "i");
    log.log("DEBUG", "d");

    const content = readLog(dir);
    assert.ok(content.includes("e"));
    assert.ok(content.includes("w"));
    assert.ok(content.includes("i"));
    assert.ok(!content.includes(")d(") && !/\bd\b/.test(content.split("\n").pop()), "DEBUG should not be written");
});

test("DEBUG level below threshold does not create a buffer entry", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO", bufferLines: 10 });
    log.log("DEBUG", "filtered");
    assert.equal(log.getStats().buffer_count, 0);
    assert.equal(log.getStats().total_logged, 0);
});

test("ring buffer overflow keeps last N entries", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO", bufferLines: 3 });
    for (let i = 0; i < 5; i++) log.log("INFO", `msg-${i}`);

    const stats = log.getStats();
    assert.equal(stats.buffer_count, 3, "buffer capped at 3");
    assert.equal(stats.total_logged, 5, "total counts all");

    const entries = log.getLogs(0, 100);
    assert.equal(entries.length, 3);
    assert.equal(entries[0].message, "msg-2");
    assert.equal(entries[1].message, "msg-3");
    assert.equal(entries[2].message, "msg-4");
});

test("buffer entry shape: {level, timestamp, message, seq}", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "DEBUG" });
    log.log("WARN", "shape-test");
    const [entry] = log.getLogs(0, 10);
    assert.deepEqual(Object.keys(entry).sort(), ["level", "message", "seq", "timestamp"]);
    assert.equal(entry.level, "WARN");
    assert.equal(entry.message, "shape-test");
    assert.equal(typeof entry.timestamp, "string");
    assert.equal(typeof entry.seq, "number");
});

test("seq auto-increment is continuous", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("INFO", "a");
    log.log("INFO", "b");
    log.log("INFO", "c");
    const entries = log.getLogs(0, 100);
    assert.deepEqual(entries.map((e) => e.seq), [1, 2, 3]);
});

test("seq keeps incrementing after ring buffer overflow (not reset)", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO", bufferLines: 2 });
    for (let i = 0; i < 4; i++) log.log("INFO", `m${i}`);
    const entries = log.getLogs(0, 100);
    assert.deepEqual(entries.map((e) => e.seq), [3, 4]);
});

test("getLogs(sinceSeq) returns entries with seq > sinceSeq", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    for (let i = 0; i < 5; i++) log.log("INFO", `x-${i}`);
    const from3 = log.getLogs(3, 100);
    assert.deepEqual(from3.map((e) => e.seq), [4, 5]);

    const from0 = log.getLogs(0, 100);
    assert.deepEqual(from0.map((e) => e.seq), [1, 2, 3, 4, 5]);
});

test("getLogs respects maxLines (default 100)", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    for (let i = 0; i < 10; i++) log.log("INFO", `n-${i}`);
    const limited = log.getLogs(0, 3);
    assert.equal(limited.length, 3);
    assert.deepEqual(limited.map((e) => e.seq), [1, 2, 3]);
});

test("getLogs default maxLines is 100", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO", bufferLines: 150 });
    for (let i = 0; i < 150; i++) log.log("INFO", `b-${i}`);
    const def = log.getLogs(0);
    assert.equal(def.length, 100);
});

test("getStats returns {total_logged, buffer_count, current_level, log_file_size}", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "WARN" });
    log.log("WARN", "s1");
    log.log("ERROR", "s2");
    const stats = log.getStats();
    assert.deepEqual(Object.keys(stats).sort(), ["buffer_count", "current_level", "log_file_size", "total_logged"]);
    assert.equal(stats.total_logged, 2);
    assert.equal(stats.buffer_count, 2);
    assert.equal(stats.current_level, "WARN");
    assert.ok(stats.log_file_size > 0);
});

test("getStats log_file_size reflects actual file bytes", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("INFO", "size-check");
    const realSize = fs.statSync(path.join(dir, "prometheus.log")).size;
    assert.equal(log.getStats().log_file_size, realSize);
});

test("rotation: pre-create >10MB file, trigger rotation, verify .1 exists", () => {
    const dir = makeTempDir();
    const file = path.join(dir, "prometheus.log");
    const big = Buffer.alloc(11 * 1024 * 1024, 0x61);
    fs.writeFileSync(file, big);

    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("INFO", "trigger-rotation");

    assert.ok(fs.existsSync(file), "active log recreated");
    assert.ok(fs.existsSync(file + ".1"), ".1 rotated file exists");
    const newContent = fs.readFileSync(file, "utf8");
    assert.match(newContent, /\[INFO\] .* trigger-rotation/);
    const rotated = fs.readFileSync(file + ".1");
    assert.equal(rotated.length, big.length);
});

test("rotation chain: shifts .log.2 -> .log.3 (delete oldest), .log.1 -> .log.2", () => {
    const dir = makeTempDir();
    const file = path.join(dir, "prometheus.log");
    fs.writeFileSync(file, "ACTIVE".repeat(2 * 1024 * 1024));
    fs.writeFileSync(file + ".1", "ONE".repeat(1024));
    fs.writeFileSync(file + ".2", "TWO".repeat(1024));
    fs.writeFileSync(file + ".3", "THREE".repeat(1024));

    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("INFO", "chain-rotation");

    assert.ok(fs.existsSync(file + ".1"));
    assert.ok(fs.existsSync(file + ".2"));
    assert.ok(fs.existsSync(file + ".3"));
    assert.ok(!fs.existsSync(file + ".4"));

    assert.ok(fs.readFileSync(file + ".1", "utf8").includes("ACTIVE"));
    assert.ok(fs.readFileSync(file + ".2", "utf8").includes("ONE"));
    assert.ok(fs.readFileSync(file + ".3", "utf8").includes("TWO"));
});

test("no rotation when file under 10MB", () => {
    const dir = makeTempDir();
    const file = path.join(dir, "prometheus.log");
    fs.writeFileSync(file, "small".repeat(1024));
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    log.log("INFO", "no-rotate");
    assert.ok(!fs.existsSync(file + ".1"));
});

test("unknown level string does not throw and is ignored", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "INFO" });
    assert.doesNotThrow(() => log.log("TRACE", "ignored"));
    assert.equal(log.getStats().total_logged, 0);
});

test("logLevel accepts lowercase and mixed case", () => {
    const dir = makeTempDir();
    const log = new Logger({ logDir: dir, logLevel: "info" });
    log.log("info", "lower-ok");
    log.log("debug", "lower-filtered");
    assert.ok(readLog(dir).includes("lower-ok"));
    assert.equal(log.getStats().total_logged, 1);
});
