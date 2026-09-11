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
    const context = vm.createContext({console: {log() {}}, Date: {now: () => 1723456789000}, window: {initialData: {recordParticipantScreenshareStartStopEvents: true}}});
    context.window.ws = {sendJson: event => messages.push(JSON.parse(JSON.stringify(event)))};
    vm.runInContext(section('web_bot_adapter/shared_chromedriver_payload.js', 'class ParticipantScreenshareEvents', '// Holds the state'), context);
    return {context, messages, run: code => vm.runInContext(code, context), events: () => messages.filter(m => m.type === 'ParticipantScreenshareStartStopEvent').map(m => [m.participantId, m.isScreenshareStart])};
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

function teams() {
    const rt = runtime();
    rt.context.virtualStreamToPhysicalStreamMappingManager = {upsertVirtualStream() {}, removeVirtualStreamsForParticipant() {}};
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'class ParticipantScreenshareStartStopManager', '\nclass ReceiverManager'));
    rt.run(section('teams_bot_adapter/teams_chromedriver_payload.js', 'function syncVirtualStreamsFromParticipant', '\nfunction extractCallId'));
    rt.run('window.participantScreenshareStartStopManager = new ParticipantScreenshareStartStopManager()');
    rt.sync = (participant, snapshot = false) => {rt.context.participant = participant; rt.context.snapshot = snapshot; rt.run('syncVirtualStreamsFromParticipant(participant, snapshot)');};
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

function zoom() {
    const rt = runtime();
    rt.run(section('zoom_web_bot_adapter/zoom_web_chromedriver_payload.js', 'class ZoomWebScreenshareEvents', '// This code intercepts'));
    rt.run('window.userManager = new UserManager(window.ws); window.zoomWebScreenshareEvents = new ZoomWebScreenshareEvents(window.ws)');
    rt.user = id => {rt.context.user = {userId: id, userName: `User ${id}`, state: 'active'}; rt.run('window.userManager.singleUserSynced(user)');};
    rt.sync = state => {rt.context.state = state; rt.run('window.zoomWebScreenshareEvents.sync(state)');};
    return rt;
}
const zoomState = users => ({meeting: {currentUser: {userId: 1}}, attendeesList: {attendeesList: [{userId: 1, sharerOn: false}, ...users]}});
test('Zoom Web: initial roster waits for participant attribution, then emits start', () => {
    const rt = zoom(); rt.sync(zoomState([{userId: 2, sharerOn: true}])); assert.deepEqual(rt.events(), []);
    rt.user(2); assert.deepEqual(rt.events(), [['2', true]]);
    assert.equal(rt.messages[0].type, 'UsersUpdate');
});
test('Zoom Web: pause and resume preserve a session', () => {
    const rt = zoom(); rt.user(2);
    for (const sharerPause of [false, true, false]) rt.sync(zoomState([{userId: 2, sharerOn: true, sharerPause}]));
    assert.deepEqual(rt.events(), [['2', true]]);
    rt.sync(zoomState([{userId: 2, sharerOn: false}]));
    assert.deepEqual(rt.events(), [['2', true], ['2', false]]);
});
test('Zoom Web: missing or incomplete state on reconnect is not a stop', () => {
    const rt = zoom(); rt.user(2); rt.sync(zoomState([{userId: 2, sharerOn: true}]));
    rt.sync({}); rt.sync({meeting: {currentUser: {userId: 1}}, attendeesList: {attendeesList: []}});
    rt.sync(zoomState([{userId: 2}]));
    assert.deepEqual(rt.events(), [['2', true]]);
});
test('Zoom Web: overlapping shares, departure and restart are independent', () => {
    const rt = zoom(); rt.user(2); rt.user(3);
    rt.sync(zoomState([{userId: 2, sharerOn: true}]));
    rt.sync(zoomState([{userId: 2, sharerOn: true}, {userId: 3, sharerOn: true}]));
    rt.sync(zoomState([{userId: 3, sharerOn: true}]));
    rt.sync(zoomState([{userId: 2, sharerOn: true}, {userId: 3, sharerOn: true}]));
    assert.deepEqual(rt.events(), [['2', true], ['3', true], ['2', false], ['2', true]]);
});
test('Zoom Web: flag off and the bot itself emit no sharing events', () => {
    const rt = zoom(); rt.user(2); rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
    rt.sync(zoomState([{userId: 2, sharerOn: true}])); assert.deepEqual(rt.events(), []);
    rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = true;
    rt.sync({meeting: {currentUser: {userId: 1}}, attendeesList: {attendeesList: [{userId: 1, sharerOn: true}]}});
    assert.deepEqual(rt.events(), []);
});

test('Zoom Web: Redux bootstrap and subscription both reach the sharing observer', () => {
    const rt = zoom(); rt.user(2);
    let state = zoomState([{userId: 2, sharerOn: true}]);
    let subscriber;
    rt.context.window.__reduxStore = {getState: () => state, subscribe: fn => {subscriber = fn;}};
    rt.context.window.liveTranscriptListWatcher = {processLiveTranscriptChange() {}};
    rt.run(section('zoom_web_bot_adapter/zoom_web_redux_interceptor.js', 'function onReduxStoreFound()', '\n  if (window.__reduxStore)'));
    rt.run('onReduxStoreFound()');
    assert.deepEqual(rt.events(), [['2', true]]);
    state = zoomState([{userId: 2, sharerOn: false}]); subscriber();
    assert.deepEqual(rt.events(), [['2', true], ['2', false]]);
});
test('Teams: the disabled setting leaves media mapping active without share events', () => {
    const rt = teams(); rt.context.window.initialData.recordParticipantScreenshareStartStopEvents = false;
    let updates = 0;
    rt.context.virtualStreamToPhysicalStreamMappingManager.upsertVirtualStream = () => {updates++;};
    rt.sync(participant([stream()]));
    assert.equal(updates, 1); assert.deepEqual(rt.events(), []);
});
