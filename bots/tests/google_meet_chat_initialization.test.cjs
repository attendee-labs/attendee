const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// Execute the production StyleManager with a deterministic DOM and clock,
// without loading the payload's unrelated WebRTC/browser interceptors.
const payload = fs.readFileSync(path.join(__dirname, '../google_meet_bot_adapter/google_meet_chromedriver_payload.js'), 'utf8');
const styleManagerSource = payload.slice(payload.indexOf('class StyleManager {'), payload.indexOf('// Video track manager'));

function fixture({ buttonAt = 0, inputAt = 0, enabledAt = 0, initiallyOpen = false } = {}) {
    let now = 0;
    let open = initiallyOpen;
    let clicks = 0;
    const timers = [];
    const events = [];
    const input = { get disabled() { return now < enabledAt; } };
    const button = {
        disabled: false,
        getAttribute: () => String(open),
        click: () => { clicks++; open = !open; }
    };
    const window = {
        ws: { sendJson: event => events.push(event) },
        initialData: { recordingView: 'speaker_view' },
        googleMeetInitialData: { modifyDomForVideoRecording: false }
    };
    const context = vm.createContext({
        window, console,
        document: { querySelector: selector => selector.startsWith('button')
            ? (now >= buttonAt ? button : null)
            : (open && now >= inputAt ? input : null) },
        setTimeout: (callback, delay) => timers.push({ callback, delay }),
        setInterval: () => { throw new Error('Unexpected interval'); }
    });
    const manager = vm.runInContext(styleManagerSource + '\nnew StyleManager()', context);
    manager.showAllOfGMeetUI = () => {};
    async function tick() {
        const timer = timers.shift();
        assert.ok(timer, 'expected a scheduled retry/poll');
        now += timer.delay;
        timer.callback();
        await Promise.resolve();
    }
    async function drain() {
        for (let i = 0; timers.length; i++) {
            assert.ok(i < 300, 'retry loop must be bounded');
            await tick();
        }
    }
    return { manager, events, tick, drain, get now() { return now; }, get clicks() { return clicks; } };
}

const readinessEvents = f => f.events.filter(event => event.type === 'ChatStatusChange');
const errors = f => f.events.filter(event => event.type === 'Error');

test('ready input keeps the existing one-shot success behavior', async () => {
    const f = fixture();
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 1);
    assert.equal(f.clicks, 2);
    assert.equal(errors(f).length, 0);
});

test('input appearing after the original three-second timeout recovers without toggling the panel closed', async () => {
    const f = fixture({ inputAt: 5000 });
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 1);
    assert.equal(f.clicks, 2);
    assert.equal(errors(f).length, 0);
    assert.equal(f.events.filter(event => event.type === 'UiInteraction').length, 1);
});

test('a missing chat button is retried', async () => {
    const f = fixture({ buttonAt: 2500 });
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 1);
    assert.equal(f.clicks, 2);
});

test('a disabled input is not ready until enabled', async () => {
    const f = fixture({ enabledAt: 5000 });
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 1);
    assert.ok(f.now >= 5000);
});

test('an already-open panel is not closed before checking its input', async () => {
    const f = fixture({ initiallyOpen: true });
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 1);
    assert.equal(f.clicks, 1);
});

test('unavailable chat exhausts five attempts with one final diagnostic', async () => {
    const f = fixture({ inputAt: Infinity });
    const initialization = f.manager.openChatPanel();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 0);
    assert.equal(errors(f).length, 1);
    assert.match(errors(f)[0].message, /after 5 attempts/);
    assert.equal(f.now, 30000);
    assert.equal(f.clicks, 1);
});

test('stop during polling cancels readiness and further interactions', async () => {
    const f = fixture({ inputAt: 200 });
    const initialization = f.manager.openChatPanel();
    await f.tick();
    f.manager.stop();
    await f.drain();
    await initialization;
    assert.equal(readinessEvents(f).length, 0);
    assert.equal(errors(f).length, 0);
    assert.equal(f.clicks, 1);
});

test('stop during backoff cancels retries without reporting exhaustion', async () => {
    const f = fixture({ buttonAt: Infinity });
    const initialization = f.manager.openChatPanel();
    f.manager.stop();
    await f.drain();
    await initialization;
    assert.equal(f.events.length, 1); // Only the first retry diagnostic.
    assert.equal(f.clicks, 0);
});

test('starting again cancels the previous initialization', async () => {
    const f = fixture();
    const first = f.manager.openChatPanel();
    const second = f.manager.openChatPanel();
    await f.drain();
    await Promise.all([first, second]);
    assert.equal(readinessEvents(f).length, 1);
    assert.equal(f.clicks, 2);
});

test('chat retries do not block audio startup', async () => {
    const f = fixture({ buttonAt: Infinity });
    let audioStarted = false;
    f.manager.startSilenceDetection = () => { audioStarted = true; };
    await f.manager.start();
    assert.equal(audioStarted, true);
    assert.equal(readinessEvents(f).length, 0);
    f.manager.stop();
    await f.drain();
});
