const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const root = resolve(__dirname, '../..');
function section(file, start, end) {
    const text = readFileSync(resolve(root, file), 'utf8');
    const from = text.indexOf(start);
    const to = text.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `Missing source boundaries in ${file}`);
    return text.slice(from, to);
}
function runtime() {
    const messages = [];
    const timers = new Map();
    let timerId = 0;
    const quietConsole = {log() {}, error() {}, warn() {}};
    const context = vm.createContext({
        console: quietConsole, realConsole: quietConsole, TextEncoder, TextDecoder,
        Date: {now: () => 1723456789000},
        setTimeout: fn => {timers.set(++timerId, fn); return timerId;},
        clearTimeout: id => timers.delete(id),
        window: {initialData: {recordParticipantScreenshareStartStopEvents: true}}
    });
    context.window.ws = {sendJson: event => messages.push(JSON.parse(JSON.stringify(event)))};
    vm.runInContext(section('web_bot_adapter/shared_chromedriver_payload.js', 'class ParticipantEventQueue', '// Holds the state'), context);
    return {context, messages, timers, run: code => vm.runInContext(code, context), events: () => messages.filter(m => m.type === 'ParticipantScreenshareStartStopEvent').map(m => [m.participantId, m.isScreenshareStart])};
}
function meet() {
    const rt = runtime();
    rt.run(section('google_meet_bot_adapter/google_meet_chromedriver_payload.js', 'class UserManager', '\nclass '));
    rt.run('window.userManager = new UserManager(window.ws)');
    rt.sync = users => { rt.context.users = users; rt.run('window.userManager.newUsersListSynced(users)'); };
    return rt;
}
const person = id => ({deviceId: id, fullName: id, status: 1});
const presentation = (id, owner) => ({...person(id), parentDeviceId: owner});

test('Meet: joining during share emits the participant before its sharing start', () => {
    const rt = meet();
    rt.sync([person('A'), presentation('screen', 'A')]);
    assert.equal(rt.messages[0].type, 'UsersUpdate');
    assert.deepEqual(rt.events(), [['A', true]]);
    assert.equal(rt.messages[1].timestamp, 1723456789000);
});
test('Meet: duplicate rosters and multiple presentation devices collapse per participant', () => {
    const rt = meet();
    const users = [person('A'), presentation('one', 'A'), presentation('two', 'A')];
    rt.sync(users); rt.sync(users); rt.sync(users.slice(0, 2));
    assert.deepEqual(rt.events(), [['A', true]]);
    rt.sync([person('A')]);
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
});
test('Meet: simultaneous sharing, leaving, handoff and restarting preserve sessions', () => {
    const rt = meet();
    rt.sync([person('A'), person('B'), presentation('one', 'A')]);
    rt.sync([person('A'), person('B'), presentation('one', 'A'), presentation('two', 'B')]);
    rt.sync([person('B'), presentation('two', 'B')]);
    rt.sync([person('A'), person('B'), presentation('two', 'B'), presentation('three', 'A')]);
    assert.deepEqual(rt.events(), [['A', true], ['B', true], ['A', false], ['A', true]]);
});
test('Meet: flag off still processes roster but emits no share events', () => {
    const rt = meet(); rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
    rt.sync([person('A'), presentation('one', 'A')]);
    assert.equal(rt.messages[0].type, 'UsersUpdate'); assert.deepEqual(rt.events(), []);
});
test('Meet: a presentation arriving before its owner waits for attribution', () => {
    const rt = meet();
    rt.context.user = presentation('screen', 'A');
    rt.run('window.userManager.singleUserSynced(user)');
    assert.deepEqual(rt.messages, []);
    rt.context.user = person('A');
    rt.run('window.userManager.singleUserSynced(user)');
    assert.equal(rt.messages[0].type, 'UsersUpdate');
    assert.deepEqual(rt.events(), [['A', true]]);
});
test('Meet: owner departure closes a retained presentation', () => {
    const rt = meet();
    rt.sync([person('A'), presentation('screen', 'A')]);
    rt.context.user = {...person('A'), status: 6};
    rt.run('window.userManager.singleUserSynced(user)');
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
    rt.sync([presentation('screen', 'A')]);
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
});

function teams() {
    const rt = runtime();
    rt.context.virtualStreamToPhysicalStreamMappingManager = {upsertVirtualStream() {}, removeVirtualStreamsForParticipant() {}};
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'class ParticipantScreenshareStartStopManager', '\nclass ReceiverManager'));
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'function syncVirtualStreamsFromParticipant', '\nfunction extractCallId'));
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'class UserManager', '\nvar realConsole'));
    rt.run('window.userManager = new UserManager(window.ws)');
    rt.context.decodeWebSocketBody = body => body;
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'function extractCallId', '\nconst subCode'));
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'class CallManager', '\nconst callManager'));
    rt.run('window.callManager = new CallManager()');
    rt.run('window.participantScreenshareStartStopManager = new ParticipantScreenshareStartStopManager()');
    rt.sync = (participant, snapshot = false) => {rt.context.participant = participant; rt.context.snapshot = snapshot; rt.run('syncVirtualStreamsFromParticipant(participant, snapshot)');};
    rt.roster = participants => {rt.context.body = {participants}; rt.run('handleRosterUpdate({body, headers: {"X-Microsoft-Skype-Chain-ID": "call-1"}})');};
    rt.snapshot = participants => {rt.context.callParticipants = participants; rt.run('window.callManager.activeCall = {_callId: "call-1", participants: callParticipants}; window.callManager.syncParticipants()');};
    return rt;
}
const stream = (direction = 'sendonly', sourceId = 10, type = 'applicationsharing-video') => ({sourceId, type, direction});
const participant = (streams, id = 'A', endpoint = 'one') => ({details: {id}, state: 'active', endpoints: {[endpoint]: {call: {mediaStreams: streams}}}});
test('Teams: metadata-only and webcam-only roster deltas never stop sharing', () => {
    const rt = teams(); rt.sync(participant([stream()]));
    rt.sync({details: {id: 'A'}}); rt.sync({details: {id: 'A'}, endpoints: {one: {}}});
    rt.sync(participant([stream('sendonly', 99, 'video')]));
    assert.deepEqual(rt.events(), [['A', true]]);
    rt.sync(participant([stream('inactive')]));
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
});
test('Teams: independently tracks endpoints and stops only after last source', () => {
    const rt = teams(); rt.sync(participant([stream()], 'A', 'one')); rt.sync(participant([stream()], 'A', 'two'));
    rt.sync(participant([stream('recvonly')], 'A', 'one'));
    assert.deepEqual(rt.events(), [['A', true]]);
    rt.sync(participant([stream('recvonly')], 'A', 'two'));
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
});
test('Teams: a complete empty media snapshot closes the session', () => {
    const rt = teams(); rt.sync(participant([stream()])); rt.sync(participant([]), true);
    assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
});
test('Teams: simultaneous shares, leave and restart do not stop the other participant', () => {
    const rt = teams(); rt.sync(participant([stream()])); rt.sync(participant([stream()], 'B'));
    rt.sync({details: {id: 'A'}, state: 'inactive'}); rt.sync(participant([stream()]));
    assert.deepEqual(rt.events(), [['A', true], ['B', true], ['A', false], ['A', true]]);
});
test('Teams: real roster handler accepts sparse stops for known users', () => {
    const rt = teams();
    const alice = {...participant([stream()]), details: {id: 'A', displayName: 'Alice'}};
    rt.roster({A: alice});
    assert.equal(rt.messages[0].type, 'UsersUpdate');
    rt.roster({A: participant([stream('inactive')])});
    rt.roster({A: participant([stream()])});
    rt.roster({A: {details: {id: 'A'}, state: 'inactive'}});
    rt.roster({unknown: participant([stream()], 'unknown')});
    assert.deepEqual(rt.events(), [['A', true], ['A', false], ['A', true], ['A', false]]);
    assert.equal(rt.run('window.userManager.currentUsersMap.size'), 1);
});
test('Teams: complete call snapshots reconcile streams and absent participants', () => {
    const rt = teams();
    const alice = {id: 'A', displayName: 'Alice', endpoints: {endpointDetails: [{endpointId: 'one', mediaStreams: [stream()]}]}};
    rt.snapshot([alice]);
    assert.equal(rt.messages[0].type, 'UsersUpdate');
    rt.snapshot(undefined);
    rt.snapshot([{id: 'A', displayName: 'Alice'}]);
    rt.snapshot([{displayName: 'Incomplete participant'}]);
    assert.deepEqual(rt.events(), [['A', true]]);
    rt.snapshot([{...alice, endpoints: {endpointDetails: [{endpointId: 'one', mediaStreams: []}]}}]);
    rt.snapshot([alice]);
    rt.snapshot([]);
    assert.deepEqual(rt.events(), [['A', true], ['A', false], ['A', true], ['A', false]]);
});

function zoom() {
    const rt = runtime();
    rt.run(section('zoom_web_bot_adapter/zoom_web_chromedriver_payload.js', 'class UserManager', '// This code intercepts'));
    rt.run('window.userManager = new UserManager(window.ws)');
    const listeners = new Map();
    let joinOptions;
    let snapshot;
    rt.context.window.zoomInitialData = {};
    rt.context.initialData = rt.context.window.initialData;
    rt.context.document = {getElementById: () => ({style: {}})};
    rt.context.ZoomMtg = {
        preLoadWasm() {}, prepareWebSDK() {},
        init: options => options.success({}),
        join: options => {joinOptions = options;},
        inMeetingServiceListener: (event, callback) => listeners.set(event, callback),
        getAttendeeslist: options => options.success(snapshot)
    };
    rt.run(readFileSync(resolve(root, 'zoom_web_bot_adapter/zoom_web_chromedriver_page.js'), 'utf8'));
    rt.run('startMeeting("test-signature")');
    rt.user = (id, sharingStatus, event = 'onUserUpdate', extra = {}) => listeners.get(event)({userId: id, userName: `User ${id}`, self: id === 1, sharingStatus, ...extra});
    rt.snapshot = result => {snapshot = {result}; joinOptions.success({});};
    rt.enter = result => {snapshot = {result}; listeners.get('onJoinSpeed')({level: 13});};
    return rt;
}
const zoomRoster = users => ({attendeesList: [{userId: 1, self: true, userName: 'Bot', sharingStatus: 'stopped'}, ...users]});
test('Zoom Web: source-verified sharingStatus contract matches the pinned SDK', () => {
    const page = readFileSync(resolve(root, 'zoom_web_bot_adapter/zoom_web_chromedriver_page.html'), 'utf8');
    assert.ok(page.includes('https://source.zoom.us/5.1.4/zoom-meeting-5.1.4.min.js'),
        'Recheck sharingStatus in SDK user events and getAttendeeslist before updating this version contract');
});
test('Zoom Web: join success seeds SDK sharingStatus after participant attribution', () => {
    const rt = zoom();
    rt.snapshot(zoomRoster([{userId: 2, self: false, userName: 'Alice', sharingStatus: 'sharing'}]));
    assert.deepEqual(rt.events(), [['2', true]]);
    assert.equal(rt.messages[0].type, 'UsersUpdate');
    assert.equal(rt.messages[1].type, 'UsersUpdate');
    assert.equal(rt.messages[2].type, 'ParticipantScreenshareStartStopEvent');
    rt.user(2, 'sharing', 'onUserJoin');
    assert.deepEqual(rt.events(), [['2', true]]);
});
test('Zoom Web: SDK user updates preserve paused sessions and ignore missing status', () => {
    const rt = zoom();
    for (const status of ['sharing', 'paused', undefined, 'sharing']) rt.user(2, status);
    assert.deepEqual(rt.events(), [['2', true]]);
    rt.user(2, 'stopped');
    assert.deepEqual(rt.events(), [['2', true], ['2', false]]);
});
test('Zoom Web: entering after the waiting room retries the initial paused share', () => {
    const rt = zoom();
    rt.snapshot('You are be put in waiting room');
    assert.deepEqual(rt.events(), []);
    rt.enter(zoomRoster([{userId: 2, self: false, sharingStatus: 'paused', userName: 'Alice'}]));
    rt.snapshot({attendeesList: []});
    rt.snapshot({attendeesList: [{userId: 2, sharingStatus: 'stopped'}]});
    assert.deepEqual(rt.events(), [['2', true]]);
});
test('Zoom Web: overlapping shares, departure and restart are independent', () => {
    const rt = zoom(); rt.user(2, 'sharing', 'onUserJoin'); rt.user(3, 'sharing');
    rt.user(2, 'sharing', 'onUserLeave');
    rt.user(2, 'sharing', 'onUserJoin');
    assert.deepEqual(rt.events(), [['2', true], ['3', true], ['2', false], ['2', true]]);
});
test('Zoom Web: flag off and the bot itself emit no sharing events', () => {
    const rt = zoom(); rt.user(2); rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
    rt.user(2, 'sharing');
    rt.context.ZoomMtg.getAttendeeslist = () => assert.fail('Disabled feature must not query the roster');
    rt.snapshot(zoomRoster([]));
    assert.deepEqual(rt.events(), []);
    rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = true;
    rt.user(1, 'sharing');
    assert.deepEqual(rt.events(), []);
});
test('Teams: the disabled setting leaves media mapping active without share events', () => {
    const rt = teams(); rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
    let updates = 0;
    rt.context.virtualStreamToPhysicalStreamMappingManager.upsertVirtualStream = () => {updates++;};
    rt.sync(participant([stream()]));
    assert.equal(updates, 1); assert.deepEqual(rt.events(), []);
});

function transport(adapter) {
    const rt = runtime();
    class Socket {
        static OPEN = 1;
        OPEN = 1; CLOSING = 2; CLOSED = 3;
        readyState = 0;
        listeners = new Map();
        failSend = false;
        addEventListener(event, callback) {this.listeners.set(event, callback);}
        send(data) {
            if (this.failSend) throw new Error('Transient send failure');
            assert.equal(new DataView(data).getInt32(0, true), 1);
            rt.messages.push(JSON.parse(new TextDecoder().decode(new Uint8Array(data, 4))));
        }
        open() {this.readyState = this.OPEN; this.onopen(); this.listeners.get('open')?.();}
        close() {this.readyState = this.CLOSED; this.onclose(); this.listeners.get('close')?.();}
    }
    rt.context.WebSocket = Socket;
    rt.context.originalWebSocket = Socket;
    rt.run(section(`${adapter}_bot_adapter/${adapter}_chromedriver_payload.js`, 'class WebSocketClient', '\nclass '));
    rt.run('window.ws = new WebSocketClient(); window.events = new ParticipantScreenshareEvents(window.ws)');
    rt.socket = rt.context.window.ws.ws;
    rt.sendTransitions = () => rt.run("window.ws.sendJson({type: 'UsersUpdate', newUsers: [{deviceId: 'A'}], removedUsers: [], updatedUsers: []}); window.events.update('A', true); window.events.update('A', false)");
    return rt;
}
for (const adapter of ['google_meet', 'teams', 'zoom_web']) {
    test(`${adapter}: socket opening flushes attribution, start and stop in order`, () => {
        const rt = transport(adapter);
        rt.sendTransitions();
        assert.deepEqual(rt.messages, []);
        rt.context.Date.now = () => 1723456799000;
        rt.socket.open();
        assert.equal(rt.messages[0].type, 'UsersUpdate');
        assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
        assert.equal(rt.messages[1].timestamp, 1723456789000);
        rt.run("window.events.update('A', false)");
        assert.equal(rt.messages.length, 3);
    });
    test(`${adapter}: failed sends retry without new roster observations`, () => {
        const rt = transport(adapter);
        rt.socket.open(); rt.socket.failSend = true;
        rt.sendTransitions();
        assert.deepEqual(rt.messages, []);
        assert.equal(rt.timers.size, 1);
        rt.socket.failSend = false;
        [...rt.timers.values()][0]();
        assert.equal(rt.messages[0].type, 'UsersUpdate');
        assert.deepEqual(rt.events(), [['A', true], ['A', false]]);
        assert.equal(rt.timers.size, 0);
    });
    test(`${adapter}: disconnect discards pending messages and stops retrying`, () => {
        const rt = transport(adapter);
        rt.socket.open(); rt.socket.failSend = true;
        rt.sendTransitions(); rt.socket.close();
        rt.run("window.events.update('A', true)");
        assert.equal(rt.timers.size, 0);
        assert.equal(rt.context.window.ws.participantEventQueue.messages.length, 0);
        assert.deepEqual(rt.messages, []);
    });
    test(`${adapter}: opt-out keeps the existing unbuffered sender`, () => {
        const rt = transport(adapter);
        rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
        rt.sendTransitions(); rt.socket.open();
        assert.equal(rt.context.window.ws.participantEventQueue, undefined);
        assert.deepEqual(rt.messages, []);
        rt.sendTransitions();
        assert.equal(rt.messages.length, 1);
        assert.equal(rt.messages[0].type, 'UsersUpdate');
    });
}
