// Jitsi platform payload (audio-only v1).
//
// Bridges the Jitsi web app (lib-jitsi-meet, exposed as window.APP.conference) to the
// python side via the websocket contract from web_bot_adapter.handle_websocket:
// - UsersUpdate / ChatMessage / ChatStatusChange / SilenceStatus / MeetingStatusChange /
//   ParticipantSpeechStartStopEvent as JSON (binary frame type 1)
// - per-participant audio as binary frame type 5 (drives the audio-chunk transcription path)
// - mixed audio as binary frame type 3, only when initialData.sendMixedAudio
// The MP3 recording itself is captured by ffmpeg from the pulseaudio sink monitor — the
// Jitsi app plays remote audio through the default output on its own, so this payload
// only taps the tracks and must never mute or reroute the audible playback.
//
// BotOutputManager comes from shared_chromedriver_payload.js, which is loaded before this file.

(() => {
    // The conference runs in the top frame; skip iframes and opaque origins, which this
    // script is also injected into (each would otherwise open its own websocket).
    if (window.self !== window.top) {
        return;
    }

    class WebSocketClient {
        static MESSAGE_TYPES = {
            JSON: 1,
            AUDIO: 3,
            PER_PARTICIPANT_AUDIO: 5,
        };

        constructor() {
            const url = `ws://localhost:${window.initialData.websocketPort}`;
            console.log('WebSocketClient url', url);
            this.ws = new WebSocket(url);
            this.ws.binaryType = 'arraybuffer';
            this.ws.onopen = () => { console.log('WebSocket Connected'); };
            this.ws.onerror = (error) => { console.error('WebSocket Error:', error); };
            this.ws.onclose = () => { console.log('WebSocket Disconnected'); };
            this.mediaSendingEnabled = false;
        }

        async enableMediaSending() {
            this.mediaSendingEnabled = true;
            await window.styleManager.start();
        }

        async disableMediaSending() {
            window.styleManager.stop();
            // Give in-flight frames a bit of time before the python side stops reading
            await new Promise(resolve => setTimeout(resolve, 2000));
            this.mediaSendingEnabled = false;
        }

        // The python side requires binary frames for everything, JSON included
        sendJson(data) {
            if (this.ws.readyState !== WebSocket.OPEN) {
                console.error('WebSocket is not connected');
                return;
            }
            try {
                const jsonBytes = new TextEncoder().encode(JSON.stringify(data));
                const message = new Uint8Array(4 + jsonBytes.length);
                new DataView(message.buffer).setInt32(0, WebSocketClient.MESSAGE_TYPES.JSON, true);
                message.set(jsonBytes, 4);
                this.ws.send(message.buffer);
            } catch (error) {
                console.error('Error sending WebSocket message:', error, data);
            }
        }

        sendMixedAudio(audioData) {
            if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) return;
            try {
                const message = new Uint8Array(4 + audioData.buffer.byteLength);
                new DataView(message.buffer).setInt32(0, WebSocketClient.MESSAGE_TYPES.AUDIO, true);
                message.set(new Uint8Array(audioData.buffer), 4);
                this.ws.send(message.buffer);
            } catch (error) {
                console.error('Error sending mixed audio:', error);
            }
        }

        sendPerParticipantAudio(participantId, audioData) {
            if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) return;
            try {
                const participantIdBytes = new TextEncoder().encode(participantId);
                const message = new Uint8Array(4 + 1 + participantIdBytes.length + audioData.buffer.byteLength);
                const dataView = new DataView(message.buffer);
                dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.PER_PARTICIPANT_AUDIO, true);
                dataView.setUint8(4, participantIdBytes.length);
                message.set(participantIdBytes, 5);
                message.set(new Uint8Array(audioData.buffer), 5 + participantIdBytes.length);
                this.ws.send(message.buffer);
            } catch (error) {
                console.error('Error sending per participant audio:', error);
            }
        }
    }

    function conferenceRoom() {
        return window.APP?.conference?._room || null;
    }

    // Reads an AudioData frame into a mono Float32Array (channels averaged)
    function frameToMonoFloat32(frame) {
        const numChannels = frame.numberOfChannels;
        const numSamples = frame.numberOfFrames;
        const audioData = new Float32Array(numSamples);
        if (numChannels > 1) {
            const channelData = new Float32Array(numSamples);
            for (let channel = 0; channel < numChannels; channel++) {
                frame.copyTo(channelData, { planeIndex: channel });
                for (let i = 0; i < numSamples; i++) audioData[i] += channelData[i];
            }
            for (let i = 0; i < numSamples; i++) audioData[i] /= numChannels;
        } else {
            frame.copyTo(audioData, { planeIndex: 0 });
        }
        return audioData;
    }

    const userManager = {
        currentUsersMap: new Map(),

        buildUsersList() {
            const room = conferenceRoom();
            if (!room || !room.isJoined()) return null;
            const users = [{
                deviceId: room.myUserId(),
                fullName: window.initialData.botName,
                humanized_status: 'in_meeting',
                isCurrentUser: true,
            }];
            for (const participant of room.getParticipants()) {
                users.push({
                    deviceId: participant.getId(),
                    fullName: participant.getDisplayName() || 'Unknown',
                    humanized_status: 'in_meeting',
                    isCurrentUser: false,
                });
            }
            return users;
        },

        syncUsers() {
            const usersList = this.buildUsersList();
            if (!usersList) return;

            const previousIds = new Set(this.currentUsersMap.keys());
            const currentIds = new Set(usersList.map(user => user.deviceId));

            const newUsers = usersList.filter(user => !previousIds.has(user.deviceId));
            const updatedUsers = usersList.filter(user => previousIds.has(user.deviceId) && JSON.stringify(this.currentUsersMap.get(user.deviceId)) !== JSON.stringify(user));
            const removedUsers = [...previousIds].filter(id => !currentIds.has(id)).map(id => ({
                ...this.currentUsersMap.get(id),
                humanized_status: 'not_in_meeting',
            }));

            for (const id of previousIds) {
                if (!currentIds.has(id)) this.currentUsersMap.delete(id);
            }
            for (const user of usersList) {
                this.currentUsersMap.set(user.deviceId, user);
            }

            if (newUsers.length > 0 || updatedUsers.length > 0 || removedUsers.length > 0) {
                window.ws.sendJson({ type: 'UsersUpdate', newUsers, updatedUsers, removedUsers });
            }
        },
    };

    const styleManager = {
        started: false,
        audioContext: null,
        destination: null,
        analyser: null,
        analyserData: null,
        meetingAudioStream: null,
        silenceCheckInterval: null,
        silenceThreshold: 0.5,
        // jitsi track id -> {source, stopped: {value}}
        attachedTracks: new Map(),
        pendingTracks: [],

        ensureMixInitialized() {
            if (this.audioContext) return;
            this.audioContext = new AudioContext({ sampleRate: 48000 });
            this.destination = this.audioContext.createMediaStreamDestination();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 8192;
            this.analyserData = new Uint8Array(this.analyser.frequencyBinCount);
            this.audioContext.createMediaStreamSource(this.destination.stream).connect(this.analyser);
            this.meetingAudioStream = this.destination.stream;
        },

        getMeetingAudioStream() {
            return this.meetingAudioStream;
        },

        addAudioTrack(jitsiTrack) {
            if (this.started) {
                this.attachTrack(jitsiTrack);
            } else {
                this.pendingTracks.push(jitsiTrack);
            }
        },

        attachTrack(jitsiTrack) {
            try {
                const trackId = jitsiTrack.getId ? jitsiTrack.getId() : jitsiTrack.getParticipantId();
                if (this.attachedTracks.has(trackId)) return;
                const mediaStreamTrack = jitsiTrack.getTrack();
                if (!mediaStreamTrack) return;
                this.ensureMixInitialized();
                // Tap only — the jitsi app keeps playing this track through its own audio
                // elements, which is what the ffmpeg recording captures
                const source = this.audioContext.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
                source.connect(this.destination);
                const stopped = { value: false };
                this.attachedTracks.set(trackId, { source, stopped });
                if (window.initialData.sendPerParticipantAudio) {
                    this.runPerParticipantProcessor(mediaStreamTrack, jitsiTrack.getParticipantId(), stopped);
                }
            } catch (error) {
                console.error('Error attaching audio track:', error);
            }
        },

        removeTrack(jitsiTrack) {
            // A track can be removed before start() drains pendingTracks — drop it there too
            this.pendingTracks = this.pendingTracks.filter(track => track !== jitsiTrack);
            const trackId = jitsiTrack.getId ? jitsiTrack.getId() : jitsiTrack.getParticipantId();
            const attached = this.attachedTracks.get(trackId);
            if (!attached) return;
            attached.stopped.value = true;
            try { attached.source.disconnect(); } catch (e) { /* already gone */ }
            this.attachedTracks.delete(trackId);
        },

        async runPerParticipantProcessor(mediaStreamTrack, participantId, stopped) {
            try {
                const processor = new MediaStreamTrackProcessor({ track: mediaStreamTrack });
                const reader = processor.readable.getReader();
                while (!stopped.value) {
                    const { value: frame, done } = await reader.read();
                    if (done) break;
                    try {
                        const audioData = frameToMonoFloat32(frame);
                        // Skip all-zero buffers so silent tracks do not produce chunks
                        if (audioData.some(sample => sample !== 0)) {
                            window.ws.sendPerParticipantAudio(participantId, audioData);
                        }
                    } finally {
                        frame.close();
                    }
                }
                reader.releaseLock();
            } catch (error) {
                console.error('Per participant audio processor error:', error);
            }
        },

        async runMixedAudioProcessor() {
            try {
                const mixedTrack = this.destination.stream.getAudioTracks()[0];
                const processor = new MediaStreamTrackProcessor({ track: mixedTrack });
                const reader = processor.readable.getReader();
                while (this.started) {
                    const { value: frame, done } = await reader.read();
                    if (done) break;
                    try {
                        window.ws.sendMixedAudio(frameToMonoFloat32(frame));
                    } finally {
                        frame.close();
                    }
                }
                reader.releaseLock();
            } catch (error) {
                console.error('Mixed audio processor error:', error);
            }
        },

        checkAudioActivity() {
            if (!this.analyser) return;
            this.analyser.getByteTimeDomainData(this.analyserData);
            let sumDeviation = 0;
            for (let i = 0; i < this.analyserData.length; i++) {
                sumDeviation += Math.abs(this.analyserData[i] - 128);
            }
            const averageDeviation = sumDeviation / this.analyserData.length;
            if (averageDeviation > this.silenceThreshold) {
                // Silence is the absence of these events — isSilent: true is never sent
                window.ws.sendJson({ type: 'SilenceStatus', volume: averageDeviation, isSilent: false });
            }
        },

        async start() {
            this.started = true;
            this.ensureMixInitialized();
            for (const track of this.pendingTracks.splice(0)) {
                this.attachTrack(track);
            }
            this.silenceCheckInterval = setInterval(() => this.checkAudioActivity(), 1000);
            if (window.initialData.sendMixedAudio) {
                this.runMixedAudioProcessor();
            }
            // No chat panel to open on jitsi — sending via the app api is available right away
            window.ws.sendJson({ type: 'ChatStatusChange', change: 'ready_to_send' });
        },

        stop() {
            this.started = false;
            if (this.silenceCheckInterval) {
                clearInterval(this.silenceCheckInterval);
                this.silenceCheckInterval = null;
            }
            for (const attached of this.attachedTracks.values()) {
                attached.stopped.value = true;
            }
        },
    };

    const jitsiBridge = {
        attachedRoom: null,
        previousDominantSpeakerId: null,

        // Re-attaches when the app swaps the room object (e.g. on a lobby rejoin)
        poll() {
            const room = conferenceRoom();
            if (room && room !== this.attachedRoom && window.JitsiMeetJS) {
                this.attachRoom(room);
            }
            if (this.attachedRoom) {
                userManager.syncUsers();
            }
        },

        attachRoom(room) {
            this.attachedRoom = room;
            const events = window.JitsiMeetJS.events.conference;
            // The app swaps the room object on a lobby rejoin without us being able to
            // deregister the old object's listeners. Gate every handler on the room still
            // being the active one, so a stale room (e.g. firing CONFERENCE_LEFT) cannot
            // end the session or emit bogus updates.
            const ifActive = (handler) => (...args) => {
                if (room === this.attachedRoom) handler(...args);
            };

            room.on(events.CONFERENCE_JOINED, ifActive(() => userManager.syncUsers()));
            room.on(events.USER_JOINED, ifActive(() => userManager.syncUsers()));
            room.on(events.USER_LEFT, ifActive(() => userManager.syncUsers()));
            room.on(events.DISPLAY_NAME_CHANGED, ifActive(() => userManager.syncUsers()));

            room.on(events.TRACK_ADDED, ifActive((track) => {
                if (track.getType() === 'audio' && !(track.isLocal && track.isLocal())) {
                    styleManager.addAudioTrack(track);
                }
            }));
            room.on(events.TRACK_REMOVED, ifActive((track) => {
                if (track.getType() === 'audio' && !(track.isLocal && track.isLocal())) {
                    styleManager.removeTrack(track);
                }
            }));

            room.on(events.DOMINANT_SPEAKER_CHANGED, ifActive((participantId) => {
                if (!window.initialData.recordParticipantSpeechStartStopEvents || !window.ws.mediaSendingEnabled) return;
                const timestamp = Date.now();
                if (this.previousDominantSpeakerId && this.previousDominantSpeakerId !== participantId) {
                    window.ws.sendJson({ type: 'ParticipantSpeechStartStopEvent', participantId: this.previousDominantSpeakerId, isSpeechStart: false, timestamp });
                }
                // lib-jitsi-meet can fire this with an empty id when nobody is dominant —
                // stop the previous speaker but never start a null one
                if (participantId && participantId !== this.previousDominantSpeakerId) {
                    window.ws.sendJson({ type: 'ParticipantSpeechStartStopEvent', participantId, isSpeechStart: true, timestamp });
                }
                this.previousDominantSpeakerId = participantId || null;
            }));

            room.on(events.KICKED, ifActive(() => {
                window.ws.sendJson({ type: 'MeetingStatusChange', change: 'removed_from_meeting' });
            }));
            room.on(events.CONFERENCE_LEFT, ifActive(() => {
                // Fires both when the conference ends and when the bot leaves on its own —
                // the python side maps it to BOT_LEFT_MEETING while in the LEAVING state
                window.ws.sendJson({ type: 'MeetingStatusChange', change: 'meeting_ended' });
            }));
            room.on(events.CONFERENCE_FAILED, ifActive((reason) => {
                if (String(reason).includes('destroyed')) {
                    window.ws.sendJson({ type: 'MeetingStatusChange', change: 'meeting_ended' });
                }
            }));

            room.on(events.MESSAGE_RECEIVED, ifActive((participantId, text, timestamp, nick, isPrivate, messageId) => {
                window.ws.sendJson({
                    type: 'ChatMessage',
                    message_uuid: messageId || crypto.randomUUID(),
                    participant_uuid: participantId,
                    timestamp: Math.floor(Date.now() / 1000),
                    text: text,
                    to_bot: isPrivate === true,
                });
            }));

            // Audio tracks that were added before we attached (e.g. after a lobby rejoin)
            for (const participant of room.getParticipants()) {
                const audioTracks = participant.getTracksByMediaType ? participant.getTracksByMediaType('audio') : [];
                for (const track of audioTracks) {
                    styleManager.addAudioTrack(track);
                }
            }
        },
    };

    function sendChatMessage(text) {
        const room = conferenceRoom();
        if (!room) {
            console.error('Cannot send chat message, no conference room');
            return false;
        }
        room.sendTextMessage(text);
        return true;
    }

    window.ws = new WebSocketClient();
    window.styleManager = styleManager;
    window.sendChatMessage = sendChatMessage;
    // BotOutputManager is defined in shared_chromedriver_payload.js. Mic callbacks use the
    // app api; everything else stays a no-op (audio-only adapter).
    window.botOutputManager = new BotOutputManager({
        turnOnMic: () => window.APP?.conference?.muteAudio(false),
        turnOffMic: () => window.APP?.conference?.muteAudio(true),
    });

    setInterval(() => jitsiBridge.poll(), 1000);
})();
