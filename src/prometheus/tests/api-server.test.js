"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

const { ApiServer } = require("../api-server.js");

function makeServer({ port = 0, ...overrides } = {}) {
    const logs = [
        { level: "INFO", timestamp: "2025-01-01T00:00:00.000Z", message: "m1", seq: 1 },
        { level: "WARN", timestamp: "2025-01-01T00:00:01.000Z", message: "m2", seq: 2 },
        { level: "ERROR", timestamp: "2025-01-01T00:00:02.000Z", message: "m3", seq: 3 },
    ];
    const mockLogger = {
        getLogs: (since = 0, max = 100) =>
            logs.filter((l) => l.seq > since).slice(0, max),
        getStats: () => ({
            total_logged: 42,
            buffer_count: 3,
            current_level: "INFO",
            log_file_size: 1024,
        }),
    };
    const deps = {
        port,
        logger: mockLogger,
        lock: { pid: 1234, dirty: false, cycle: 5 },
        getStats: () => ({ cycles: 10, posts: 200, last_cycle_ts: 1700000000000 }),
        getConfig: () => ({
            api_port: 9420,
            daemon_interval_ms: 300000,
            log_level: "INFO",
            log_buffer_lines: 500,
        }),
        setConfig: () => {},
        triggerDaemon: () => {},
        ...overrides,
    };
    const calls = {};
    if (deps.getStats) {
        calls.getStats = 0;
        const orig = deps.getStats;
        deps.getStats = (...a) => (calls.getStats++, orig(...a));
    }
    if (deps.getConfig) {
        calls.getConfig = 0;
        const orig = deps.getConfig;
        deps.getConfig = (...a) => (calls.getConfig++, orig(...a));
    }
    if (deps.setConfig) {
        calls.setConfig = 0;
        const orig = deps.setConfig;
        const recorded = [];
        deps.setConfig = (body) => (calls.setConfig++, recorded.push(body), orig(body));
        calls._setConfigArgs = recorded;
    }
    if (deps.triggerDaemon) {
        calls.triggerDaemon = 0;
        const orig = deps.triggerDaemon;
        deps.triggerDaemon = (...a) => (calls.triggerDaemon++, orig(...a));
    }
    const server = new ApiServer(deps);
    return { server, calls };
}

async function startServer(opts = {}) {
    const { server, calls } = makeServer(opts);
    await server.start();
    const port = server.address().port;
    const base = `http://127.0.0.1:${port}`;
    return { server, port, calls, base };
}

async function fetchJSON(base, path, init = {}) {
    const res = await fetch(base + path, init);
    const body = await res.json();
    return { status: res.status, body };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("1. GET /health returns correct envelope", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/health");
        assert.equal(status, 200);
        assert.deepEqual(body, { ok: true, data: {}, error: "" });
    } finally {
        await server.stop();
    }
});

test("2. GET /logs?since=N returns correct offset", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/logs?since=1&max=10");
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.error, "");
        assert.equal(body.data.logs.length, 2);
        assert.equal(body.data.logs[0].seq, 2);
        assert.equal(body.data.logs[1].seq, 3);
        assert.equal(body.data.total, 2);
    } finally {
        await server.stop();
    }
});

test("2b. GET /logs without query uses defaults (since=0, max=100)", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/logs");
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.data.logs.length, 3);
        assert.equal(body.data.total, 3);
    } finally {
        await server.stop();
    }
});

test("3. GET /stats returns stats fields", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/stats");
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.error, "");
        assert.equal(body.data.cycles, 10);
        assert.equal(body.data.posts, 200);
        assert.equal(calls.getStats, 1);
    } finally {
        await server.stop();
    }
});

test("4. GET /config returns config", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config");
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.error, "");
        assert.equal(body.data.api_port, 9420);
        assert.equal(body.data.daemon_interval_ms, 300000);
        assert.equal(body.data.log_level, "INFO");
        assert.equal(body.data.log_buffer_lines, 500);
        assert.equal(calls.getConfig, 1);
    } finally {
        await server.stop();
    }
});

test("5. PUT /config with valid values succeeds", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                api_port: 10000,
                daemon_interval_ms: 60000,
                log_level: "DEBUG",
                log_buffer_lines: 1000,
            }),
        });
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.error, "");
        assert.deepEqual(body.data, { applied_next_cycle: true });
        assert.equal(calls.setConfig, 1);
        assert.deepEqual(calls._setConfigArgs[0], {
            api_port: 10000,
            daemon_interval_ms: 60000,
            log_level: "DEBUG",
            log_buffer_lines: 1000,
        });
    } finally {
        await server.stop();
    }
});

test("5b. PUT /config partial update (only one field) succeeds", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ log_level: "WARN" }),
        });
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.deepEqual(body.data, { applied_next_cycle: true });
        assert.equal(calls.setConfig, 1);
    } finally {
        await server.stop();
    }
});

test("6. PUT /config with api_port=-1 → validation fails", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_port: -1 }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.deepEqual(body.data, {});
        assert.match(body.error, /validation/);
        assert.match(body.error, /api_port/);
        assert.equal(calls.setConfig, 0);
    } finally {
        await server.stop();
    }
});

test("6b. PUT /config with api_port=80 (below 1024) → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_port: 80 }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /api_port/);
    } finally {
        await server.stop();
    }
});

test("6c. PUT /config with api_port=70000 (above 65535) → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_port: 70000 }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /api_port/);
    } finally {
        await server.stop();
    }
});

test("7. PUT /config with invalid log_level → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ log_level: "VERBOSE" }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /validation/);
        assert.match(body.error, /log_level/);
    } finally {
        await server.stop();
    }
});

test("7b. PUT /config with daemon_interval_ms=0 → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ daemon_interval_ms: 0 }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /daemon_interval_ms/);
    } finally {
        await server.stop();
    }
});

test("7c. PUT /config with log_buffer_lines=-5 → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ log_buffer_lines: -5 }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /log_buffer_lines/);
    } finally {
        await server.stop();
    }
});

test("7d. PUT /config with non-number api_port (string) → validation fails", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_port: "notanumber" }),
        });
        assert.equal(status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /api_port/);
    } finally {
        await server.stop();
    }
});

test("8. POST /action/trigger-daemon calls callback", async () => {
    const { server, base, calls } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/action/trigger-daemon", {
            method: "POST",
        });
        assert.equal(status, 200);
        assert.equal(body.ok, true);
        assert.equal(body.error, "");
        assert.deepEqual(body.data, { triggered: true });
        assert.equal(calls.triggerDaemon, 1);
    } finally {
        await server.stop();
    }
});

test("9. Unknown path → 404 envelope", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/nope");
        assert.equal(status, 404);
        assert.deepEqual(body, { ok: false, data: {}, error: "not found" });
    } finally {
        await server.stop();
    }
});

test("9b. Known path with wrong method → 404 envelope", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/health", { method: "POST" });
        assert.equal(status, 404);
        assert.deepEqual(body, { ok: false, data: {}, error: "not found" });
    } finally {
        await server.stop();
    }
});

test("10. Only 127.0.0.1 binding (no 0.0.0.0)", async () => {
    const { server } = await startServer();
    try {
        const addr = server.address();
        assert.equal(addr.address, "127.0.0.1");
        assert.notEqual(addr.address, "0.0.0.0");
    } finally {
        await server.stop();
    }
});

test("11. start() throws when port already in use", async () => {
    const occupier = http.createServer((_req, res) => res.end());
    await new Promise((resolve) => occupier.listen(0, "127.0.0.1", resolve));
    const takenPort = occupier.address().port;

    const { server } = makeServer({ port: takenPort });
    await assert.rejects(
        () => server.start(),
        /already in use/
    );
    occupier.close();
});

test("12. constructor applies default port 9420 when not specified", () => {
    const server = new ApiServer({ logger: { getLogs: () => [], getStats: () => ({}) } });
    assert.equal(server.port, 9420);
});

test("13. address() returns null before start, valid after start", async () => {
    const { server } = makeServer();
    assert.equal(server.address(), null);
    await server.start();
    try {
        const addr = server.address();
        assert.ok(addr);
        assert.equal(typeof addr.port, "number");
    } finally {
        await server.stop();
    }
    assert.equal(server.address(), null);
});

test("14. PUT /config with empty body → 400 (invalid JSON)", async () => {
    const { server, base } = await startServer();
    try {
        const res = await fetch(base + "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: "not json",
        });
        const body = await res.json();
        assert.equal(res.status, 400);
        assert.equal(body.ok, false);
        assert.match(body.error, /json|invalid/i);
    } finally {
        await server.stop();
    }
});

test("15. PUT /config with valid extra unknown field → still succeeds (ignored)", async () => {
    const { server, base } = await startServer();
    try {
        const { status, body } = await fetchJSON(base, "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ log_level: "ERROR", unknown_field: "x" }),
        });
        assert.equal(status, 200);
        assert.equal(body.ok, true);
    } finally {
        await server.stop();
    }
});
