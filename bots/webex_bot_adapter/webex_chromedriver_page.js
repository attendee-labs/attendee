// Runs the official Webex Browser SDK as a guest and reports what happens to the Python adapter over
// window.ws, using the same message types as the other web bots. The adapter follows the join by
// reading window.webexJoinState.

const initialData = window.webexInitialData;
// What the controller wants over the websocket. The adapter has no callback for audio or captions nobody asked for, and
// calling a missing one crashes its websocket handler, which loses every later message.
const {
    sendMixedAudio: wantsMixedAudio,
    sendPerParticipantAudio: wantsPerParticipantAudio,
    collectCaptions: wantsCaptions,
    recordParticipantSpeechStartStopEvents: wantsSpeechEvents,
} = window.initialData;

const AUDIO_SAMPLE_RATE = 48000;
// The SDK asks for 3 by default. Each slot holds one recent speaker, so with more slots fewer speakers lose theirs when
// someone else starts talking. Measured on a four person meeting: 6 slots cut the word error rate from 40% to 34%.
const NUM_ACTIVE_SPEAKER_AUDIO_SLOTS = 6;
const MAX_VIDEO_PANES = 9;
const VIDEO_SIZE_HINT = { width: 640, height: 360 };
// Webex reports active speakers in ~1 second steps and the list briefly empties between words, so a
// speaker only counts as having stopped after being absent for this long.
const SPEECH_STOP_DELAY_MS = 1500;
// How much of a new speaker's speech it takes for them to take over an audio slot from its previous member
const SLOT_MEMBER_TIME_CONSTANT_MS = 1000;
// A slot's audio is only sent once a member has been reported speaking during at least this share of it
const SLOT_MEMBER_MIN_SCORE = 0.2;
// How far another member's score has to beat the slot's member for the slot to change hands. While two people talk at
// once both are reported speaking and their scores briefly cross; without this margin the slots swapped members for
// about a second each time (40 times in a 10 minute four person meeting, where Webex never moved anyone).
const SLOT_MEMBER_SWITCH_MARGIN = 0.2;
// The RMS below which the adapter treats per participant audio as silence
const AUDIBLE_RMS = 0.01;
const AUDIO_ACTIVITY_REPORT_INTERVAL_MS = 1000;
const CAPTIONS_ANNOUNCEMENT_TIMEOUT_MS = 20000;
const WEBEX_ERROR_CODE_MEETING_NOT_FOUND = 2403014;

const joinState = { phase: 'joining', othersInMeeting: false };
window.webexJoinState = joinState;

let webex;
let meeting;
let wasAdmitted = false;
let isLeaving = false;
const knownMembers = new Map();

function send(message) {
    window.ws.sendJson(message);
}

// ---------- Failures ----------

function sendMeetingStatusChange(change, extra = {}) {
    send({ type: 'MeetingStatusChange', change, ...extra });
}

// `method` tells the adapter which failure it is: meeting_not_found, incorrect_password, blocked_by_captcha,
// removed_from_waiting_room, or a generic step name such as "join".
function sendFailedToJoin(method, details = {}) {
    sendMeetingStatusChange('failed_to_join', { reason: { method, ...details } });
}

function describeError(error) {
    return {
        errorName: error?.name,
        errorCode: error?.code,
        errorMessage: error?.message,
        errorBody: error?.error?.body ?? error?.error?.message,
    };
}

function classifyJoinError(error) {
    const webexErrorCode = error?.error?.body?.errorCode;
    if (webexErrorCode === WEBEX_ERROR_CODE_MEETING_NOT_FOUND) return 'meeting_not_found';
    if (error?.name === 'PasswordError') return 'incorrect_password';
    if (error?.name === 'CaptchaError') return 'blocked_by_captcha';
    return null;
}

function failJoin(error, step) {
    console.error(`${step} failed`, error);
    sendFailedToJoin(classifyJoinError(error) ?? step, describeError(error));
}

// A meeting that requires a password must have it verified before joining. This only applies when joining by
// meeting number: invite links carry the credential themselves.
async function verifyMeetingPassword() {
    if (meeting.passwordStatus !== 'REQUIRED') return true;
    const { isPasswordValid, requiredCaptcha, failureReason } = await meeting.verifyPassword(initialData.meetingPassword || '');
    if (!isPasswordValid) sendFailedToJoin(requiredCaptcha ? 'blocked_by_captcha' : 'incorrect_password', { failureReason });
    return isPasswordValid;
}

// Webex reports why the meeting was destroyed, and whether that is a denial or a removal depends on whether we were admitted
function handleMeetingDestroyed({ reason }) {
    // When the bot leaves by itself Webex reports no reason, and then reports it as a removal
    if (isLeaving) return;

    if (reason === 'SELF_REMOVED') {
        if (wasAdmitted) sendMeetingStatusChange('removed_from_meeting');
        else sendFailedToJoin('removed_from_waiting_room');
    } else if (reason === 'MEETING_INACTIVE_TERMINATING') {
        sendMeetingStatusChange('meeting_ended');
    } else {
        sendFailedToJoin('join', { errorName: reason, errorMessage: `Meeting was destroyed with reason ${reason}` });
    }
}

// ---------- Join progress ----------

// Webex shows a guest waiting for a host that has not started the meeting exactly like a normal lobby, and hides
// roles from lobby guests. Whether anyone else is in the meeting is the only signal that tells them apart.
function updateOthersInMeeting() {
    joinState.othersInMeeting = [...knownMembers.values()].some((user) => !user.isCurrentUser && user.humanized_status === 'in_meeting');
}

function handleSelfLobbyWaiting() {
    joinState.phase = 'lobby';
}

function handleSelfGuestAdmitted() {
    wasAdmitted = true;
    joinState.phase = 'admitted';
}

function waitUntilAdmitted() {
    return new Promise((resolve) => {
        if (joinState.phase !== 'lobby') resolve();
        else meeting.once('meeting:self:guestAdmitted', resolve);
    });
}

// ---------- Participants ----------

function toUser(member) {
    return {
        deviceId: member.id,
        fullName: member.name || 'Unknown',
        isCurrentUser: !!member.isSelf,
        isHost: !!member.isHost,
        // Only the destroy reason reports the bot being removed, so a member in the lobby or who left is just "not in meeting"
        humanized_status: member.status === 'IN_MEETING' ? 'in_meeting' : 'not_in_meeting',
    };
}

function handleMembersUpdate({ delta = {} }) {
    const { added = [], updated = [], removed = [] } = delta;
    const newUsers = [];
    const updatedUsers = [];
    for (const user of [...added, ...updated].map(toUser)) {
        (knownMembers.has(user.deviceId) ? updatedUsers : newUsers).push(user);
        knownMembers.set(user.deviceId, user);
    }
    const removedUsers = removed.map(toUser);
    removedUsers.forEach((user) => knownMembers.delete(user.deviceId));

    if (newUsers.length || updatedUsers.length || removedUsers.length) {
        send({ type: 'UsersUpdate', newUsers, updatedUsers, removedUsers });
    }
    updateOthersInMeeting();
}

// ---------- Active speaker ----------

const activeSpeakerIds = new Set();
const pendingSpeechStops = new Map();
// Who Webex says is speaking right now, without the stop delay, which is what the audio slots learn their member from
let reportedSpeakerIds = new Set();

function sendSpeechEvent(participantId, isSpeechStart, timestamp) {
    if (!wantsSpeechEvents) return;
    send({ type: 'ParticipantSpeechStartStopEvent', participantId, isSpeechStart, timestamp });
}

function handleActiveSpeakerChanged({ memberIds = [] }) {
    const reported = new Set(memberIds);
    const now = Date.now();
    reportedSpeakerIds = reported;

    for (const id of reported) {
        clearTimeout(pendingSpeechStops.get(id));
        pendingSpeechStops.delete(id);
        if (!activeSpeakerIds.has(id)) {
            activeSpeakerIds.add(id);
            sendSpeechEvent(id, true, now);
        }
    }
    for (const id of activeSpeakerIds) {
        if (reported.has(id) || pendingSpeechStops.has(id)) continue;
        pendingSpeechStops.set(
            id,
            setTimeout(() => {
                pendingSpeechStops.delete(id);
                activeSpeakerIds.delete(id);
                sendSpeechEvent(id, false, now);
            }, SPEECH_STOP_DELAY_MS)
        );
    }
}

// ---------- Screen share ----------

let screenSharerId = null;

function sendScreenshareEvent(participantId, isScreenshareStart) {
    send({ type: 'ScreenshareStartStopEvent', participantId, isScreenshareStart, timestamp: Date.now() });
}

// Webex has one share at a time, so a new share replaces the current one without a stop in between
function handleRemoteShareStarted({ memberId }) {
    if (memberId === screenSharerId) return;
    if (screenSharerId) sendScreenshareEvent(screenSharerId, false);
    screenSharerId = memberId;
    sendScreenshareEvent(memberId, true);
}

function handleRemoteShareStopped() {
    if (!screenSharerId) return;
    sendScreenshareEvent(screenSharerId, false);
    screenSharerId = null;
}

// ---------- Audio ----------

let audioContext = null;
let mixedAudioDestination = null;

function toMonoSamples(frame) {
    const samples = new Float32Array(frame.numberOfFrames);
    const channel = new Float32Array(frame.numberOfFrames);
    for (let plane = 0; plane < frame.numberOfChannels; plane++) {
        frame.copyTo(channel, { planeIndex: plane });
        for (let i = 0; i < samples.length; i++) samples[i] += channel[i] / frame.numberOfChannels;
    }
    return samples;
}

async function forEachAudioFrame(track, onSamples) {
    const reader = new MediaStreamTrackProcessor({ track }).readable.getReader();
    try {
        while (true) {
            const { done, value: frame } = await reader.read();
            if (done) break;
            try {
                onSamples(toMonoSamples(frame));
            } finally {
                frame.close();
            }
        }
    } catch (error) {
        console.error('Stopped reading audio frames', error);
    }
}

// Webex sends each recent speaker in an audio slot of their own and keeps them in it while they stay among the recent
// speakers, so people talking over each other arrive separately (measured: over 99% of a slot's speech was one
// speaker's). It does not say which member is in which slot: the slots never report a source and the RTP packets carry
// no contributing source. So each slot learns its member from the active speaker reports, as an average over the
// frames where the slot is audible of whether each member was reported speaking. A new speaker in a slot takes it over
// after about SLOT_MEMBER_TIME_CONSTANT_MS of their speech. Each frame is held back for perParticipantAudioUtteranceDelayMs,
// so the reports that cover it have arrived, and then sent as the audio of the slot's member.
const audioSlots = [];

function isAudible(samples) {
    let sumOfSquares = 0;
    for (const sample of samples) sumOfSquares += sample * sample;
    return Math.sqrt(sumOfSquares / samples.length) >= AUDIBLE_RMS;
}

function updateSlotMemberScores(slot, samples) {
    if (!isAudible(samples)) return;
    const step = samples.length / (AUDIO_SAMPLE_RATE / 1000) / SLOT_MEMBER_TIME_CONSTANT_MS;
    for (const id of reportedSpeakerIds) {
        if (id !== meeting.members.selfId && !slot.memberScores.has(id)) slot.memberScores.set(id, 0);
    }
    for (const [id, score] of slot.memberScores) {
        slot.memberScores.set(id, score + step * ((reportedSpeakerIds.has(id) ? 1 : 0) - score));
    }
}

// A slot keeps its member unless another member's score beats theirs by SLOT_MEMBER_SWITCH_MARGIN. The slots without a
// member then take the highest scoring members, best scores first, so a member is in at most one slot.
function updateSlotMembers() {
    const assignedIds = new Set();
    for (const slot of audioSlots) {
        const score = slot.memberScores.get(slot.memberId) ?? 0;
        const bestScore = Math.max(0, ...slot.memberScores.values());
        if (score >= SLOT_MEMBER_MIN_SCORE && bestScore < score + SLOT_MEMBER_SWITCH_MARGIN) {
            assignedIds.add(slot.memberId);
        } else {
            slot.memberId = null;
        }
    }

    const candidates = audioSlots.filter((slot) => !slot.memberId).flatMap((slot) => [...slot.memberScores].map(([id, score]) => ({ slot, id, score })));
    candidates.sort((a, b) => b.score - a.score);
    for (const { slot, id, score } of candidates) {
        if (score < SLOT_MEMBER_MIN_SCORE || slot.memberId || assignedIds.has(id)) continue;
        slot.memberId = id;
        assignedIds.add(id);
    }
}

function handleSlotAudio(slot, samples) {
    const now = Date.now();
    updateSlotMemberScores(slot, samples);
    slot.queue.push({ samples, arrivedAt: now });

    while (slot.queue.length && now - slot.queue[0].arrivedAt >= initialData.perParticipantAudioUtteranceDelayMs) {
        const { samples: delayedSamples } = slot.queue.shift();
        updateSlotMembers();
        if (slot.memberId) window.ws.sendPerParticipantAudio(slot.memberId, delayedSamples);
    }
}

// The adapter leaves the meeting after a long silence. Without mixed audio it only knows about audio from these reports,
// so they are sent like the Teams and Google Meet bots do, whatever audio the controller asked for.
let lastAudioActivityReportAt = 0;

function reportAudioActivity(samples) {
    const now = Date.now();
    if (now - lastAudioActivityReportAt < AUDIO_ACTIVITY_REPORT_INTERVAL_MS) return;
    // A sample that rounds to a non-zero 16 bit value, the same test the adapter applies to mixed audio
    if (!samples.some((sample) => Math.abs(sample) >= 1 / 32768)) return;
    lastAudioActivityReportAt = now;
    send({ type: 'SilenceStatus', isSilent: false });
}

function handleAudio(samples) {
    // Summing the slots can exceed full scale, and the adapter's float to int16 conversion wraps on overflow
    for (let i = 0; i < samples.length; i++) samples[i] = Math.max(-1, Math.min(1, samples[i]));
    reportAudioActivity(samples);
    if (wantsMixedAudio) window.ws.sendMixedAudio(samples);
}

function addAudioSlot(remoteMedia) {
    // Playing the stream puts the meeting audio into the browser's output, which is what the recorder captures,
    // and keeps WebRTC delivering the stream to the Web Audio graph
    const element = document.createElement('audio');
    element.autoplay = true;
    element.srcObject = remoteMedia.stream;
    document.getElementById('remoteMedia').appendChild(element);

    if (!audioContext) {
        audioContext = new AudioContext({ sampleRate: AUDIO_SAMPLE_RATE });
        mixedAudioDestination = audioContext.createMediaStreamDestination();
        forEachAudioFrame(mixedAudioDestination.stream.getAudioTracks()[0], handleAudio);
    }
    audioContext.createMediaStreamSource(remoteMedia.stream).connect(mixedAudioDestination);

    if (wantsPerParticipantAudio) {
        const slot = { queue: [], memberScores: new Map(), memberId: null };
        audioSlots.push(slot);
        // A slot whose track has ended must not keep a member from the slots that replaced it
        forEachAudioFrame(remoteMedia.stream.getAudioTracks()[0], (samples) => handleSlotAudio(slot, samples)).then(() => audioSlots.splice(audioSlots.indexOf(slot), 1));
    }
}

function handleRemoteAudioCreated(audioGroup) {
    audioGroup.getRemoteMedia().forEach(addAudioSlot);
}

// ---------- Video ----------

function addVideoTile(container, remoteMedia) {
    const video = document.createElement('video');
    video.autoplay = true;
    video.muted = true; // the audio is played by the audio slots
    video.srcObject = remoteMedia.stream;

    const label = document.createElement('div');
    label.className = 'label';

    const tile = document.createElement('div');
    tile.className = 'tile';
    tile.append(video, label);
    container.appendChild(tile);

    const refresh = () => {
        label.textContent = knownMembers.get(remoteMedia.memberId)?.fullName ?? '';
        tile.style.display = remoteMedia.sourceState === 'live' ? '' : 'none';
    };
    remoteMedia.on('sourceUpdate', refresh);
    refresh();
    remoteMedia.setSizeHint(VIDEO_SIZE_HINT.width, VIDEO_SIZE_HINT.height);
}

function renderVideoLayout({ screenShareVideo, activeSpeakerVideoPanes = {} }) {
    const container = document.getElementById('videos');
    container.replaceChildren();

    if (screenShareVideo) addVideoTile(container, screenShareVideo);
    for (const group of Object.values(activeSpeakerVideoPanes)) {
        group.getRemoteMedia().forEach((remoteMedia) => addVideoTile(container, remoteMedia));
    }
}

// ---------- Captions ----------

const sentCaptionSignatures = new Map();

// Interim captions are "<transcriptId>_<speakerId>" and the final one is "<transcriptId>". Using the transcript id for
// both makes the adapter treat them as one caption.
function transcriptId(caption) {
    return String(caption.id).split('_')[0];
}

function memberIdForPerson(personId) {
    const members = Object.values(meeting.members.membersCollection.members);
    return members.find((member) => member.participant?.person?.id === personId)?.id ?? personId;
}

function handleCaptionReceived({ captions }) {
    for (const caption of captions) {
        const captionId = transcriptId(caption);
        const signature = `${caption.text}|${caption.isFinal}`;
        if (sentCaptionSignatures.get(captionId) === signature) continue;
        sentCaptionSignatures.set(captionId, signature);

        send({
            type: 'CaptionUpdate',
            caption: {
                captionId,
                deviceId: memberIdForPerson(caption.speaker?.speakerId),
                text: caption.text,
                isFinal: !!caption.isFinal,
            },
        });
    }
}

// startTranscription() swallows its own errors, and fails with "can not turn on captions before llm connected" if it
// runs right after joining. The first call also registers the SDK's listeners, so call it once, wait for the
// transcription announcement (sent once the LLM connection is up), then call it again.
async function startCaptions() {
    meeting.on('meeting:caption-received', handleCaptionReceived);

    try {
        const announced = new Promise((resolve) => meeting.once('meeting:startedReceivingTranscription', resolve));
        const options = { spokenLanguage: initialData.captionsLanguage || 'en' };
        await meeting.startTranscription(options);
        await Promise.race([announced, new Promise((resolve) => setTimeout(resolve, CAPTIONS_ANNOUNCEMENT_TIMEOUT_MS))]);
        await meeting.startTranscription(options);
    } catch (error) {
        console.error('Starting captions failed', error);
    }

    if (meeting.transcription?.status !== 'enabled') send({ type: 'ClosedCaptionStatusChange', change: 'save_caption_not_allowed' });
}

// ---------- Bot output (webcam image, audio) ----------

// BotOutputManager (shared_chromedriver_payload.js) replaces getUserMedia with the bot's canvas and audio streams, so the
// SDK's own createCameraStream / createMicrophoneStream hand back the bot's output.
const publishedStreams = new Map();

function publishBotStream(kind) {
    if (!publishedStreams.has(kind)) {
        const publish = async () => {
            const { createCameraStream, createMicrophoneStream } = webex.meetings.mediaHelpers;
            const stream = await (kind === 'camera' ? createCameraStream() : createMicrophoneStream());
            await meeting.publishStreams({ [kind]: stream });
        };
        publishedStreams.set(
            kind,
            publish().catch((error) => {
                publishedStreams.delete(kind);
                send({ type: 'WebexBotOutputError', output: kind, ...describeError(error) });
            })
        );
    }
    return publishedStreams.get(kind);
}

function setUpBotOutput() {
    window.botOutputManager = new BotOutputManager({
        turnOnWebcam: () => publishBotStream('camera'),
        turnOnMic: () => publishBotStream('microphone'),
        callOriginalGetUserMedia: false,
    });
}

// ---------- Main ----------

const REMOTE_MEDIA_CONFIG = {
    audio: { numOfActiveSpeakerStreams: NUM_ACTIVE_SPEAKER_AUDIO_SLOTS, numOfScreenShareStreams: 1 },
    video: {
        preferLiveVideo: true,
        // When nothing records or streams video, a layout without panes keeps Webex from sending any, which saves the
        // CPU of decoding it
        initialLayoutId: initialData.disableIncomingVideo ? 'NoVideo' : 'AllEqual',
        layouts: {
            AllEqual: {
                screenShareVideo: { size: 'best' },
                activeSpeakerVideoPaneGroups: [{ id: 'main', numPanes: MAX_VIDEO_PANES, size: 'best', priority: 255 }],
            },
            NoVideo: {},
        },
    },
};

function listenToMeeting() {
    meeting.on('meeting:self:lobbyWaiting', handleSelfLobbyWaiting);
    meeting.on('meeting:self:guestAdmitted', handleSelfGuestAdmitted);
    meeting.on('DESTROY_MEETING', handleMeetingDestroyed);
    meeting.members.on('members:update', handleMembersUpdate);
    meeting.on('media:activeSpeakerChanged', handleActiveSpeakerChanged);
    meeting.on('meeting:startedSharingRemote', handleRemoteShareStarted);
    meeting.on('meeting:stoppedSharingRemote', handleRemoteShareStopped);
    meeting.on('media:remoteAudio:created', handleRemoteAudioCreated);
    meeting.on('media:remoteVideo:layoutChanged', renderVideoLayout);
}

async function createMeeting() {
    // A bare meeting number would otherwise be taken for a phone number
    const isMeetingNumber = /^[\d\s]+$/.test(initialData.meetingUrl);
    return webex.meetings.create(initialData.meetingUrl, isMeetingNumber ? 'MEETING_ID' : undefined);
}

async function joinMeeting() {
    if (!(await verifyMeetingPassword())) return false;

    // Media can't be negotiated from the lobby ("Confluence url for the device is null"), so join, wait to be
    // admitted, then add media.
    await meeting.join({ name: initialData.botName, pin: initialData.meetingPassword || undefined, enableMultistream: true });
    await waitUntilAdmitted();

    await meeting.addMedia({
        // audioEnabled and videoEnabled must stay true: the SDK computes receive = enabled && receive, so false would
        // silently turn receiving off. Sending also stays enabled, with no local tracks so nothing is sent, because
        // the send transceivers can't be added later and the bot publishes its webcam image and audio on demand.
        audioEnabled: true,
        videoEnabled: true,
        remoteMediaManagerConfig: REMOTE_MEDIA_CONFIG,
    });
    return true;
}

async function main() {
    webex = window.Webex.init({ credentials: { access_token: initialData.guestToken }, config: { logger: { level: 'info' } } });
    await new Promise((resolve) => webex.once('ready', resolve));
    await webex.meetings.register();

    try {
        meeting = await createMeeting();
    } catch (error) {
        failJoin(error, 'create');
        return;
    }
    listenToMeeting();

    try {
        if (!(await joinMeeting())) return;
        joinState.phase = 'joined';
        setUpBotOutput();
        if (wantsCaptions) await startCaptions();
    } catch (error) {
        failJoin(error, 'join');
    }
}

window.leaveMeeting = async () => {
    isLeaving = true;
    try {
        await meeting?.leave();
    } catch (error) {
        // Leaving a meeting that was never joined, or that has already ended, is expected to fail
        console.error('Leaving the meeting failed', error);
    }
};

if (initialData.meetingUrl) {
    main().catch((error) => failJoin(error, 'init'));
}
