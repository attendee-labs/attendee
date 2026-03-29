/**
 * Webex Bot Chromedriver Payload
 * Integrates Webex Web SDK with the attendee bot adapter framework
 *
 * This file is loaded by the WebBotAdapter and handles:
 * - Webex SDK initialization and meeting join
 * - Multistream audio recording with participant correlation
 * - Video display and participant tracking
 * - Transcription and active speaker detection
 * - WebSocket communication with Python adapter
 */

// ============================================================================
// GLOBAL STATE
// ============================================================================
let webex;
let meeting;
let globalAudioMediaGroup;

// Participant tracking
const memberIdToAudioCSI = new Map(); // memberId -> CSI for audio correlation
const transcriptionToMemberId = new Map();
let transcriptionEnabled = false;
let lastTranscriptionPayload = null;

// Audio recording tracking
const audioStreamRecorders = new Map(); // Map<streamId, recorderData>
const streamIndexToMemberId = new Map(); // Map<streamIndex, memberId> for correlation
let currentActiveSpeakers = new Set(); // Currently speaking memberIds

// Enhanced correlation tracking (from original app.js)
const streamSlotAudioActivity = new Map(); // Map<slotIndex, { hasActivity, lastActivityTime, amplitude, ... }>
const memberToSlotHistory = new Map(); // Map<memberId, Map<slotIndex, { count, lastTime, confidence }>>
const slotToMemberMap = new Map(); // Map<slotIndex, { memberId, memberName, confidence, lastUpdated, method }>

// Legacy tracking (for backward compatibility)
const audioSlotToMemberId = new Map(); // Map<slotIndex, memberId>
const memberIdToSlotIndex = new Map(); // Map<memberId, slotIndex>
let nextAvailableSlot = 0;

// Video tracking
const remoteVideos = new Map(); // Map<participantId, { memberId, memberName, remoteMedia }>
const memberIdToRemoteMedia = new Map(); // memberId -> remoteMedia for video

// UI tracking
const activeSpeakers = new Map(); // Map<memberId, { name, isSpeaking, lastSpokeAt }>

// Configuration
const CORRELATION_CONFIG = {
    ACTIVITY_THRESHOLD: 5,
    CORRELATION_DELAY_MS: 300,
    CONFIDENCE_THRESHOLD: 0.6,
    SLOT_STICKY_DURATION_MS: 10000,
    ACTIVITY_HISTORY_SIZE: 20,
};

const UTTERANCE_CONFIG = {
    SILENCE_THRESHOLD_MS: 3000,
    MAX_UTTERANCE_DURATION_MS: 300000,
    SILENCE_DETECTION_INTERVAL_MS: 100,
};

// State tracking for WebexUIMethods
window.webexState = {
    status: 'initializing', // initializing, joining, joined, error, left
    inWaitingRoom: false,
    errorType: null,
    errorMessage: null,
    errorCode: null,
};

// ============================================================================
// ENHANCED AUDIO ACTIVITY TRACKING & CORRELATION
// ============================================================================

function initializeStreamActivityTracking() {
    for (let i = 0; i < 3; i++) {
        streamSlotAudioActivity.set(i, {
            hasActivity: false,
            lastActivityTime: null,
            amplitude: 0,
            memberHistory: [],
            activityHistory: [],
        });
    }
}

function updateStreamActivity(slotIndex, amplitude) {
    const activity = streamSlotAudioActivity.get(slotIndex);
    if (!activity) return;

    const hasActivity = amplitude > CORRELATION_CONFIG.ACTIVITY_THRESHOLD;
    activity.amplitude = amplitude;
    activity.hasActivity = hasActivity;

    if (hasActivity) {
        activity.lastActivityTime = Date.now();
    }

    activity.activityHistory.push({
        timestamp: Date.now(),
        amplitude: amplitude,
        hasActivity: hasActivity
    });

    if (activity.activityHistory.length > CORRELATION_CONFIG.ACTIVITY_HISTORY_SIZE) {
        activity.activityHistory.shift();
    }

    streamSlotAudioActivity.set(slotIndex, activity);
}

function getActiveSlotsWithAudio() {
    const activeSlots = new Set();
    const now = Date.now();

    streamSlotAudioActivity.forEach((activity, slotIndex) => {
        if (activity.lastActivityTime && (now - activity.lastActivityTime) < 500) {
            activeSlots.add(slotIndex);
        }
    });

    return activeSlots;
}

function updateSlotToMemberMapping(slotIndex, memberId, memberName, confidence, method) {
    const timestamp = Date.now();

    slotToMemberMap.set(slotIndex, {
        memberId,
        memberName,
        confidence,
        lastUpdated: timestamp,
        method
    });

    const activity = streamSlotAudioActivity.get(slotIndex);
    if (activity) {
        activity.memberHistory.push({ memberId, memberName, timestamp, confidence });
        if (activity.memberHistory.length > 10) {
            activity.memberHistory.shift();
        }
    }

    if (!memberToSlotHistory.has(memberId)) {
        memberToSlotHistory.set(memberId, new Map());
    }
    const slotHistory = memberToSlotHistory.get(memberId);
    const existing = slotHistory.get(slotIndex) || { count: 0, lastTime: 0, confidence: 0 };
    slotHistory.set(slotIndex, {
        count: existing.count + 1,
        lastTime: timestamp,
        confidence: Math.max(existing.confidence, confidence)
    });
}

function getMemberSlotHistoryScore(memberId, slotIndex) {
    const history = memberToSlotHistory.get(memberId);
    if (!history) return 0;

    const slotData = history.get(slotIndex);
    if (!slotData) return 0;

    const timeSinceLast = Date.now() - slotData.lastTime;
    const recency = Math.max(0, 1 - (timeSinceLast / CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS));
    const frequency = Math.min(1, slotData.count / 5);

    return (slotData.confidence * 0.5) + (recency * 0.3) + (frequency * 0.2);
}

function getMostLikelySlotForMember(memberId) {
    const history = memberToSlotHistory.get(memberId);
    if (!history || history.size === 0) return null;

    let bestSlot = null;
    let bestScore = 0;

    history.forEach((data, slotIndex) => {
        const score = getMemberSlotHistoryScore(memberId, slotIndex);
        if (score > bestScore) {
            bestScore = score;
            bestSlot = slotIndex;
        }
    });

    return bestScore > 0.3 ? bestSlot : null;
}

function getMemberForSlot(slotIndex) {
    const mapping = slotToMemberMap.get(slotIndex);
    if (!mapping) {
        const fallbackMemberId = audioSlotToMemberId.get(slotIndex);
        if (fallbackMemberId) {
            const member = getMemberById(fallbackMemberId);
            return {
                memberId: fallbackMemberId,
                memberName: member?.name || 'Unknown',
                confidence: 0.5,
                lastUpdated: Date.now(),
                method: 'fallback_legacy'
            };
        }
        return null;
    }

    const age = Date.now() - mapping.lastUpdated;
    if (age > CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS * 2) {
        return mapping; // Still return as fallback
    }

    return mapping;
}

function correlateActiveSpeakersToSlots(memberIds) {
    if (!memberIds || memberIds.length === 0) return;

    const activeSlotsWithAudio = getActiveSlotsWithAudio();
    const timestamp = Date.now();

    // Single speaker case
    if (memberIds.length === 1) {
        const memberId = memberIds[0];
        const member = getMemberById(memberId);
        const memberName = member?.name || 'Unknown';

        if (activeSlotsWithAudio.size === 1) {
            const [slotIndex] = activeSlotsWithAudio;
            updateSlotToMemberMapping(slotIndex, memberId, memberName, 0.95, 'single_speaker_single_slot');
            streamIndexToMemberId.set(slotIndex, memberId);
            return;
        }

        const historicalSlot = getMostLikelySlotForMember(memberId);
        if (historicalSlot !== null) {
            updateSlotToMemberMapping(historicalSlot, memberId, memberName, 0.7, 'single_speaker_historical');
            streamIndexToMemberId.set(historicalSlot, memberId);
            return;
        }

        const firstAvailableSlot = findAvailableSlot();
        updateSlotToMemberMapping(firstAvailableSlot, memberId, memberName, 0.5, 'single_speaker_fallback');
        streamIndexToMemberId.set(firstAvailableSlot, memberId);
        return;
    }

    // Multiple speakers - use scoring algorithm
    // (Implementation matches original app.js lines 300-390)
    const correlationScores = new Map();

    memberIds.forEach(memberId => {
        const scores = new Map();

        for (let slotIndex = 0; slotIndex < 3; slotIndex++) {
            let score = 0;

            if (activeSlotsWithAudio.has(slotIndex)) {
                const activity = streamSlotAudioActivity.get(slotIndex);
                score += 0.4 * (activity.amplitude / 100);
            }

            const historicalScore = getMemberSlotHistoryScore(memberId, slotIndex);
            score += 0.4 * historicalScore;

            const recentScore = getRecentSlotActivityScore(slotIndex);
            score += 0.2 * recentScore;

            const currentMapping = slotToMemberMap.get(slotIndex);
            if (currentMapping && currentMapping.memberId !== memberId) {
                const timeSinceUpdate = timestamp - currentMapping.lastUpdated;
                if (timeSinceUpdate < CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS) {
                    score *= 0.3;
                }
            }

            scores.set(slotIndex, score);
        }

        correlationScores.set(memberId, scores);
    });

    const assignedSlots = new Set();
    const assignedMembers = new Set();

    const allPairs = [];
    correlationScores.forEach((scores, memberId) => {
        scores.forEach((score, slotIndex) => {
            allPairs.push({ memberId, slotIndex, score });
        });
    });
    allPairs.sort((a, b) => b.score - a.score);

    for (const pair of allPairs) {
        if (assignedSlots.has(pair.slotIndex) || assignedMembers.has(pair.memberId)) {
            continue;
        }

        const member = getMemberById(pair.memberId);
        const memberName = member?.name || 'Unknown';
        const confidence = Math.min(pair.score, 1.0);

        if (confidence >= CORRELATION_CONFIG.CONFIDENCE_THRESHOLD) {
            updateSlotToMemberMapping(pair.slotIndex, pair.memberId, memberName, confidence, 'multi_speaker_scored');
            streamIndexToMemberId.set(pair.slotIndex, pair.memberId);
            assignedSlots.add(pair.slotIndex);
            assignedMembers.add(pair.memberId);
        }
    }

    memberIds.forEach(memberId => {
        if (!assignedMembers.has(memberId)) {
            const member = getMemberById(memberId);
            const memberName = member?.name || 'Unknown';
            const fallbackSlot = findAvailableSlot(assignedSlots);
            updateSlotToMemberMapping(fallbackSlot, memberId, memberName, 0.4, 'multi_speaker_fallback');
            streamIndexToMemberId.set(fallbackSlot, memberId);
            assignedSlots.add(fallbackSlot);
        }
    });
}

function getRecentSlotActivityScore(slotIndex) {
    const activity = streamSlotAudioActivity.get(slotIndex);
    if (!activity || !activity.activityHistory.length) return 0;

    const recentHistory = activity.activityHistory.slice(-5);
    const activeCount = recentHistory.filter(h => h.hasActivity).length;
    return activeCount / recentHistory.length;
}

function findAvailableSlot(assignedSlots = new Set()) {
    for (let i = 0; i < 3; i++) {
        if (!assignedSlots.has(i)) {
            const mapping = slotToMemberMap.get(i);
            if (!mapping || (Date.now() - mapping.lastUpdated) > CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS) {
                return i;
            }
        }
    }

    let oldestSlot = 0;
    let oldestTime = Date.now();
    slotToMemberMap.forEach((mapping, slot) => {
        if (!assignedSlots.has(slot) && mapping.lastUpdated < oldestTime) {
            oldestTime = mapping.lastUpdated;
            oldestSlot = slot;
        }
    });
    return oldestSlot;
}

function assignMemberToAudioSlot(memberId, memberName) {
    if (memberIdToSlotIndex.has(memberId)) {
        return memberIdToSlotIndex.get(memberId);
    }

    let assignedSlot = null;

    for (let slot = 0; slot < 3; slot++) {
        if (!audioSlotToMemberId.has(slot)) {
            assignedSlot = slot;
            break;
        }
    }

    if (assignedSlot === null) {
        for (let slot = 0; slot < 3; slot++) {
            const currentMember = audioSlotToMemberId.get(slot);
            if (currentMember && !currentActiveSpeakers.has(currentMember)) {
                memberIdToSlotIndex.delete(currentMember);
                assignedSlot = slot;
                break;
            }
        }
    }

    if (assignedSlot === null) {
        assignedSlot = nextAvailableSlot;
        const displacedMember = audioSlotToMemberId.get(assignedSlot);
        if (displacedMember) {
            memberIdToSlotIndex.delete(displacedMember);
        }
        nextAvailableSlot = (nextAvailableSlot + 1) % 3;
    }

    audioSlotToMemberId.set(assignedSlot, memberId);
    memberIdToSlotIndex.set(memberId, assignedSlot);

    return assignedSlot;
}

function getSlotForMember(memberId) {
    return memberIdToSlotIndex.get(memberId) || null;
}

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

function getMemberById(memberId) {
    if (!meeting || !meeting.members) return null;
    const members = meeting.members.membersCollection.members;
    return members[memberId] || null;
}

function getRosterMembersInMeeting() {
    if (!meeting || !meeting.members) return [];
    const collection = meeting.members.membersCollection;
    const members = collection.members || collection.getAll?.() || {};
    const list = Array.isArray(members) ? members : Object.values(members);
    return list.filter((m) => m && m.isInMeeting === true);
}

// ============================================================================
// CONTINUOUS AUDIO STREAMING (adapted for framework)
// ============================================================================

/**
 * Start streaming per-participant audio continuously to Python adapter
 * This replaces the utterance-based approach from the original app.js
 */
function startStreamRecording(stream, csi, slotIndex, remoteMedia = null) {
    if (!stream) return;

    const streamId = stream.id;

    if (audioStreamRecorders.has(streamId)) {
        return;
    }

    try {
        const audioTracks = stream.getAudioTracks();
        if (audioTracks.length === 0) {
            return;
        }

        console.log(`[Audio] Starting continuous streaming for slot ${slotIndex}, CSI: ${csi}`);

        // Create audio context for amplitude detection
        const audioContext = new AudioContext();
        const source = audioContext.createMediaStreamSource(stream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);

        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        // Function to detect audio activity and update correlation tracking
        const hasAudioActivity = () => {
            analyser.getByteFrequencyData(dataArray);
            const average = dataArray.reduce((sum, value) => sum + value, 0) / bufferLength;
            updateStreamActivity(slotIndex, average);
            return average > CORRELATION_CONFIG.ACTIVITY_THRESHOLD;
        };

        // Create MediaStreamTrackProcessor for continuous audio frames
        const processor = new MediaStreamTrackProcessor({ track: audioTracks[0] });
        const readable = processor.readable;

        let lastAudioFormat = null;

        // Transform stream to send audio frames via WebSocket
        const transformStream = new TransformStream({
            async transform(frame, controller) {
                if (!frame) {
                    return;
                }

                try {
                    if (controller.desiredSize === null) {
                        frame.close();
                        return;
                    }

                    // Copy audio data
                    const numChannels = frame.numberOfChannels;
                    const numSamples = frame.numberOfFrames;
                    const audioData = new Float32Array(numSamples);

                    // Mix down to mono if multi-channel
                    if (numChannels > 1) {
                        const channelData = new Float32Array(numSamples);
                        for (let channel = 0; channel < numChannels; channel++) {
                            frame.copyTo(channelData, { planeIndex: channel });
                            for (let i = 0; i < numSamples; i++) {
                                audioData[i] += channelData[i];
                            }
                        }
                        for (let i = 0; i < numSamples; i++) {
                            audioData[i] /= numChannels;
                        }
                    } else {
                        frame.copyTo(audioData, { planeIndex: 0 });
                    }

                    // Send audio format update if changed
                    const currentFormat = {
                        numberOfChannels: 1,
                        originalNumberOfChannels: frame.numberOfChannels,
                        numberOfFrames: frame.numberOfFrames,
                        sampleRate: frame.sampleRate,
                        format: frame.format,
                        duration: frame.duration
                    };

                    if (!lastAudioFormat || JSON.stringify(currentFormat) !== JSON.stringify(lastAudioFormat)) {
                        lastAudioFormat = currentFormat;
                        window.ws.sendJson({
                            type: 'AudioFormatUpdate',
                            format: currentFormat
                        });
                    }

                    // Update activity detection
                    hasAudioActivity();

                    // Determine participant ID using correlation
                    let participantId = null;
                    const slotMapping = getMemberForSlot(slotIndex);

                    if (slotMapping && slotMapping.confidence >= CORRELATION_CONFIG.CONFIDENCE_THRESHOLD) {
                        participantId = slotMapping.memberId;
                    } else if (streamIndexToMemberId.has(slotIndex)) {
                        participantId = streamIndexToMemberId.get(slotIndex);
                    }

                    // Send per-participant audio if we have a participant ID
                    if (participantId && window.ws) {
                        window.ws.sendPerParticipantAudio(participantId, audioData);
                    }

                    frame.close();

                } catch (error) {
                    console.error('[Audio] Error processing frame:', error);
                    frame.close();
                }
            },
            flush() {
                console.log('[Audio] Transform stream flush called');
            }
        });

        // Start the pipeline
        const abortController = new AbortController();
        readable
            .pipeThrough(transformStream)
            .pipeTo(new WritableStream(), { signal: abortController.signal })
            .catch(error => {
                if (error.name !== 'AbortError') {
                    console.error('[Audio] Pipeline error:', error);
                }
            });

        // Store recorder data
        audioStreamRecorders.set(streamId, {
            stream: stream,
            csi: csi,
            index: slotIndex,
            remoteMedia: remoteMedia,
            audioContext: audioContext,
            abortController: abortController,
        });

        console.log(`[Audio] Stream recording started for slot ${slotIndex}`);

    } catch (error) {
        console.error('[Audio] Failed to start stream recording:', error);
    }
}

function stopStreamRecording(streamId) {
    if (!streamId) return;

    const recorderData = audioStreamRecorders.get(streamId);
    if (!recorderData) return;

    try {
        if (recorderData.abortController) {
            recorderData.abortController.abort();
        }

        if (recorderData.audioContext) {
            recorderData.audioContext.close();
        }

        audioStreamRecorders.delete(streamId);
        console.log('[Audio] Stopped stream recording', { streamId, csi: recorderData.csi });
    } catch (error) {
        console.error('[Audio] Error stopping stream recording:', error);
        audioStreamRecorders.delete(streamId);
    }
}

// ============================================================================
// WEBEX SDK INITIALIZATION AND MEETING JOIN
// ============================================================================

async function initializeWebexSDK() {
    const accessToken = window.webexInitialData.accessToken;

    if (!accessToken) {
        throw new Error('No access token provided');
    }

    console.log('[Webex] Initializing SDK...');

    webex = window.Webex.init({
        credentials: {
            access_token: accessToken
        },
        config: {
            logger: {
                level: 'info'
            },
            meetings: {
                reconnection: {
                    enabled: true
                },
                enableRtx: true,
                experimental: {
                    enableUnifiedMeetings: true,
                    enableMultistream: true
                }
            }
        }
    });

    await new Promise((resolve, reject) => {
        webex.once('ready', resolve);
        setTimeout(() => reject(new Error('SDK initialization timeout')), 30000);
    });

    console.log('[Webex] SDK ready');

    // Register with meetings service
    await webex.meetings.register();
    console.log('[Webex] Registered with meetings service');

    return webex;
}

async function joinWebexMeeting() {
    const destination = window.webexInitialData.meetingDestination;
    const password = window.webexInitialData.meetingPassword;
    const enableTranscription = window.webexInitialData.enableTranscription;

    console.log('[Webex] Creating meeting...', { destination });
    window.webexState.status = 'joining';

    meeting = await webex.meetings.create(destination);
    console.log('[Webex] Meeting created', { id: meeting.id });

    // Setup event listeners
    setupMultistreamListeners();
    setupMeetingListeners();

    // Join with multistream enabled
    const joinOptions = {
        pin: password || undefined,
        moderator: false,
        moveToResource: false,
        allowMediaInLobby: true,
        enableMultistream: true
    };

    const remoteMediaManagerConfig = {
        audio: {
            numOfActiveSpeakerStreams: 3,
            numOfScreenShareStreams: 1
        },
        video: {
            preferLiveVideo: true,
            initialLayoutId: 'AllEqual',
            layouts: {
                AllEqual: {
                    activeSpeakerVideoPaneGroups: [
                        { id: 'main', numPanes: 9, size: 'best', priority: 255 }
                    ]
                }
            }
        }
    };

    const mediaOptions = {
        receiveAudio: true,
        receiveVideo: !window.initialData.disableIncomingVideo,
        sendAudio: false,
        sendVideo: false,
        allowMediaInLobby: true,
        receiveTranscription: enableTranscription,
        remoteMediaManagerConfig
    };

    console.log('[Webex] Joining with multistream...');

    try {
        await meeting.joinWithMedia({ joinOptions, mediaOptions });

        console.log('[Webex] Successfully joined meeting');
        window.webexState.status = 'joined';
        window.webexState.inWaitingRoom = false;

        // Notify Python adapter that bot joined
        window.ws.sendJson({
            type: 'MeetingStatusChange',
            change: 'joined'
        });

        // Notify Python adapter that chat is ready
        // (Webex can send messages immediately after joining)
        window.ws.sendJson({
            type: 'ChatStatusChange',
            change: 'ready_to_send'
        });

        // Send initial roster
        sendRosterUpdate();

        console.log('[Webex] Bot fully initialized and ready');

    } catch (error) {
        console.error('[Webex] Failed to join meeting:', error);
        window.webexState.status = 'error';
        window.webexState.errorType = 'join_failed';
        window.webexState.errorMessage = error.message;
        window.webexState.errorCode = error.code;

        throw error;
    }
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

function setupMultistreamListeners() {
    initializeStreamActivityTracking();

    // Remote Audio Streams
    meeting.on('media:remoteAudio:created', (audioMediaGroup) => {
        console.log('[Audio] Remote audio created', { count: audioMediaGroup.getRemoteMedia().length });

        globalAudioMediaGroup = audioMediaGroup;
        const remoteMediaArray = audioMediaGroup.getRemoteMedia();

        remoteMediaArray.forEach((remoteMedia, index) => {
            const csi = remoteMedia.csi;

            console.log(`[Audio] Setting up slot ${index}`, {
                csi: csi !== undefined && csi !== null ? csi : 'undefined',
                memberId: remoteMedia.memberId || 'N/A',
                sourceState: remoteMedia.sourceState,
                hasStream: !!remoteMedia.stream
            });

            // Try to map CSI to member
            if (csi !== undefined && csi !== null) {
                try {
                    const member = meeting.members.findMemberByCsi(csi);
                    if (member && member.id) {
                        memberIdToAudioCSI.set(member.id, csi);
                        console.log('[Audio] Initial CSI mapping', {
                            csi,
                            memberId: member.id,
                            memberName: member.name,
                            streamIndex: index
                        });
                    }
                } catch (error) {
                    console.warn('[Audio] Could not map CSI initially', { csi, error: error.message });
                }
            }

            // Start recording if stream available
            if (remoteMedia.stream) {
                startStreamRecording(remoteMedia.stream, remoteMedia.csi, index, remoteMedia);
            }

            // Listen for source updates
            remoteMedia.on('sourceUpdate', (data) => {
                const updatedCsi = remoteMedia.csi;

                console.log('[Audio] Source updated', {
                    streamIndex: index,
                    csi: updatedCsi !== undefined && updatedCsi !== null ? updatedCsi : 'undefined',
                    state: data?.state || remoteMedia.sourceState,
                    hasStream: !!remoteMedia.stream
                });

                // Update CSI mapping
                if (updatedCsi !== undefined && updatedCsi !== null) {
                    try {
                        const member = meeting.members.findMemberByCsi(updatedCsi);
                        if (member && member.id) {
                            memberIdToAudioCSI.set(member.id, updatedCsi);
                            console.log('[Audio] Updated CSI mapping', {
                                csi: updatedCsi,
                                memberId: member.id,
                                memberName: member.name
                            });
                        }
                    } catch (error) {
                        console.warn('[Audio] Could not map updated CSI', { csi: updatedCsi });
                    }
                }

                // Start recording if stream becomes available
                if (remoteMedia.stream) {
                    const streamId = remoteMedia.stream.id;
                    if (!audioStreamRecorders.has(streamId)) {
                        console.log('[Audio] Starting recording from sourceUpdate', {
                            csi: updatedCsi,
                            state: data?.state
                        });
                        startStreamRecording(remoteMedia.stream, updatedCsi, index, remoteMedia);
                    }
                }
            });

            // Stop on remoteMedia stopped
            remoteMedia.on('stopped', () => {
                console.log('[Audio] RemoteMedia stopped', {
                    csi: remoteMedia.csi,
                    streamIndex: index
                });
                stopStreamRecording(remoteMedia.stream?.id);
            });
        });

        console.log('[Audio] Recording initialized for all streams');
    });

    // Active Speaker Changes - for correlation and silence detection
    meeting.on('media:activeSpeakerChanged', (payload) => {
        const speakers = payload.activeSpeakers || [];
        const memberIds = speakers.map(s => s.memberId).filter(Boolean);

        console.log('[Speaker] Active speakers changed', { memberIds });

        // Update current active speakers
        currentActiveSpeakers.clear();
        memberIds.forEach(id => currentActiveSpeakers.add(id));

        // Update active speaker UI tracking
        memberIds.forEach(memberId => {
            const member = getMemberById(memberId);
            if (member) {
                activeSpeakers.set(memberId, {
                    name: member.name,
                    isSpeaking: true,
                    lastSpokeAt: new Date()
                });
            }
        });

        // Mark others as not speaking
        activeSpeakers.forEach((data, memberId) => {
            if (!memberIds.includes(memberId)) {
                data.isSpeaking = false;
            }
        });

        // Correlate active speakers to audio slots
        if (memberIds.length > 0) {
            setTimeout(() => {
                correlateActiveSpeakersToSlots(memberIds);
            }, CORRELATION_CONFIG.CORRELATION_DELAY_MS);
        }

        // Send silence status update to Python adapter for automatic leave detection
        window.ws.sendJson({
            type: 'SilenceStatus',
            isSilent: memberIds.length === 0
        });
    });

    // Transcription
    meeting.on('meeting:receiveTranscription:started', () => {
        transcriptionEnabled = true;
        console.log('[Transcription] Started');
    });

    meeting.on('meeting:caption-received', (payload) => {
        lastTranscriptionPayload = payload;

        console.log('[Transcription] Caption received', {
            captionCount: payload.captions?.length || 0
        });

        if (payload.captions && Array.isArray(payload.captions)) {
            payload.captions.forEach(caption => {
                const { personId, text, timestamp, isFinal } = caption;

                let memberId = transcriptionToMemberId.get(personId);

                if (!memberId) {
                    const members = getRosterMembersInMeeting();
                    const member = members.find(m => m.id === personId);

                    if (member) {
                        memberId = member.id;
                        transcriptionToMemberId.set(personId, memberId);

                        if (isFinal) {
                            assignMemberToAudioSlot(memberId, member.name);
                        }

                        console.log('[Transcription] Correlation established', {
                            personId,
                            memberId,
                            memberName: member.name,
                            slot: getSlotForMember(memberId)
                        });
                    }
                }

                // Send caption to Python adapter
                if (isFinal && memberId) {
                    const member = getMemberById(memberId);

                    window.ws.sendJson({
                        type: 'CaptionUpdate',
                        caption: {
                            participant_uuid: memberId,
                            participant_full_name: member?.name || 'Unknown',
                            text: text,
                            timestamp_ms: timestamp || Date.now(),
                            is_final: isFinal
                        }
                    });
                }
            });
        }
    });

    meeting.on('meeting:receiveTranscription:stopped', () => {
        transcriptionEnabled = false;
        console.log('[Transcription] Stopped');
    });
}

function setupMeetingListeners() {
    // Roster updates - track participant join/leave/updates
    meeting.members.on('members:update', (payload) => {
        const full = payload.full || [];
        const delta = payload.delta || {};

        console.log('[Roster] Members updated', {
            fullCount: full.length,
            added: (delta.added || []).length,
            updated: (delta.updated || []).length
        });

        // Send roster update to Python adapter (uses ParticipantManager)
        sendRosterUpdate(delta);
    });

    // Meeting status events
    meeting.on('meeting:reconnectionSuccess', () => {
        console.log('[Meeting] Reconnected successfully');
    });

    meeting.on('meeting:reconnectionFailed', () => {
        console.log('[Meeting] Reconnection failed');
        // Could send MeetingStatusChange here if needed
    });

    // Handle meeting end/disconnect
    meeting.on('meeting:stopped', (reason) => {
        console.log('[Meeting] Meeting stopped', { reason });

        window.webexState.status = 'left';

        window.ws.sendJson({
            type: 'MeetingStatusChange',
            change: 'meeting_ended'
        });
    });

    // Meeting left by self
    meeting.on('meeting:self:left', () => {
        console.log('[Meeting] Self left meeting');

        window.ws.sendJson({
            type: 'MeetingStatusChange',
            change: 'meeting_ended'
        });
    });

    // Meeting removed (kicked out)
    meeting.on('meeting:self:requestedToJoin', () => {
        console.log('[Meeting] Requested to join (in waiting room)');
        window.webexState.inWaitingRoom = true;
    });

    meeting.on('meeting:self:guestAdmitted', () => {
        console.log('[Meeting] Admitted from waiting room');
        window.webexState.inWaitingRoom = false;
    });

    // Lobby/Waiting room events
    meeting.on('meeting:self:lobbyWaiting', () => {
        console.log('[Meeting] In lobby/waiting room');
        window.webexState.inWaitingRoom = true;
    });

    // Chat messages (if supported by Webex SDK)
    // Note: Webex SDK may not expose chat messages in the same way as Zoom
    // This would need to be verified with Webex SDK documentation
    if (meeting.sendMessage) {
        // Enable chat receiving if available
        console.log('[Chat] Chat functionality available');
    }
}

// ============================================================================
// PARTICIPANT TRACKING (UserManager equivalent)
// ============================================================================

class ParticipantManager {
    constructor() {
        this.allParticipants = new Map(); // All participants ever seen
        this.currentParticipants = new Map(); // Currently in meeting
    }

    formatParticipant(member, botName) {
        return {
            deviceId: member.id,
            fullName: member.name || 'Unknown',
            displayName: member.name || 'Unknown',
            isCurrentUser: member.name === botName || member.isSelf,
            isHost: member.isHost || false,
            status: member.isInMeeting ? 1 : 6, // 1=IN_MEETING, 6=NOT_IN_MEETING
            humanized_status: member.isInMeeting ? 'in_meeting' : 'not_in_meeting',
            meetingId: meeting?.id,
        };
    }

    updateParticipants(members, botName) {
        const previousIds = new Set(this.currentParticipants.keys());
        const newIds = new Set();
        const updatedIds = new Set();

        const newUsers = [];
        const updatedUsers = [];
        const removedUsers = [];

        // Process current members
        members.forEach(member => {
            const formatted = this.formatParticipant(member, botName);
            newIds.add(member.id);

            // Store in all participants
            this.allParticipants.set(member.id, formatted);

            // Check if new or updated
            if (!previousIds.has(member.id)) {
                newUsers.push(formatted);
            } else {
                const previous = this.currentParticipants.get(member.id);
                if (JSON.stringify(previous) !== JSON.stringify(formatted)) {
                    updatedUsers.push(formatted);
                    updatedIds.add(member.id);
                }
            }
        });

        // Find removed users
        previousIds.forEach(id => {
            if (!newIds.has(id)) {
                const participant = this.currentParticipants.get(id);
                if (participant) {
                    removedUsers.push({
                        ...participant,
                        status: 6,
                        humanized_status: 'not_in_meeting'
                    });
                }
            }
        });

        // Update current participants map
        this.currentParticipants.clear();
        members.forEach(member => {
            const formatted = this.formatParticipant(member, botName);
            this.currentParticipants.set(member.id, formatted);
        });

        // Send update if there are changes
        if (newUsers.length > 0 || removedUsers.length > 0 || updatedUsers.length > 0) {
            console.log('[Participants] Update:', {
                new: newUsers.length,
                removed: removedUsers.length,
                updated: updatedUsers.length
            });

            window.ws.sendJson({
                type: 'UsersUpdate',
                newUsers,
                removedUsers,
                updatedUsers
            });
        }
    }

    getParticipantById(id) {
        return this.currentParticipants.get(id) || this.allParticipants.get(id);
    }
}

const participantManager = new ParticipantManager();

function sendRosterUpdate(delta = null) {
    const members = getRosterMembersInMeeting();
    const botName = window.initialData.botName;

    // Use ParticipantManager to handle tracking and send updates
    participantManager.updateParticipants(members, botName);
}

// ============================================================================
// CHAT FUNCTIONS
// ============================================================================

window.sendChatMessage = function(text, toUserUuid = null) {
    if (!meeting) {
        console.error('[Chat] No active meeting');
        return;
    }

    try {
        // Webex SDK chat API
        const chatOptions = {
            message: text
        };

        if (toUserUuid) {
            chatOptions.toPersonId = toUserUuid;
        }

        meeting.sendMessage(chatOptions)
            .then(() => {
                console.log('[Chat] Message sent', { text, to: toUserUuid || 'all' });
            })
            .catch(error => {
                console.error('[Chat] Failed to send message:', error);
            });
    } catch (error) {
        console.error('[Chat] Error sending message:', error);
    }
};

// ============================================================================
// MAIN INITIALIZATION
// ============================================================================

(async function initializeWebexBot() {
    try {
        console.log('[Webex] Bot initializing...');
        console.log('[Webex] Initial data:', {
            botName: window.initialData.botName,
            destination: window.webexInitialData.meetingDestination,
            hasPassword: !!window.webexInitialData.meetingPassword,
            transcription: window.webexInitialData.enableTranscription
        });

        // Wait for WebSocket connection
        await new Promise((resolve) => {
            const checkWs = setInterval(() => {
                if (window.ws && window.ws.readyState === WebSocket.OPEN) {
                    clearInterval(checkWs);
                    resolve();
                }
            }, 100);
        });

        console.log('[Webex] WebSocket connected');

        // Initialize SDK
        await initializeWebexSDK();

        // Join meeting
        await joinWebexMeeting();

        console.log('[Webex] Bot successfully initialized and joined meeting');

    } catch (error) {
        console.error('[Webex] Initialization error:', error);

        window.webexState.status = 'error';
        window.webexState.errorType = error.name || 'unknown';
        window.webexState.errorMessage = error.message;
        window.webexState.errorCode = error.code;

        // Notify Python adapter of failure
        if (window.ws) {
            window.ws.sendJson({
                type: 'MeetingStatusChange',
                change: 'failed_to_join',
                reason: {
                    errorCode: error.code,
                    errorMessage: error.message,
                    method: 'join'
                }
            });
        }
    }
})();

console.log('[Webex] Payload loaded');

