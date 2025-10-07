class StyleManager {
    constructor() {
        this.audioContext = null;
        this.audioTracks = [];
        this.silenceThreshold = 0.0;
        this.silenceCheckInterval = null;
        this.frameStyleElement = null;
        this.frameAdjustInterval = null;
        this.neededInteractionsInterval = null;

        // Stream used which combines the audio tracks from the meeting. Does NOT include the bot's audio
        this.meetingAudioStream = null;
    }

    addAudioTrack(audioTrack) {
        this.audioTracks.push(audioTrack);
    }

    checkAudioActivity() {
        // Get audio data
        this.analyser.getByteTimeDomainData(this.audioDataArray);
        
        // Calculate deviation from the center value (128)
        let sumDeviation = 0;
        for (let i = 0; i < this.audioDataArray.length; i++) {
            // Calculate how much each sample deviates from the center (128)
            sumDeviation += Math.abs(this.audioDataArray[i] - 128);
        }
        
        const averageDeviation = sumDeviation / this.audioDataArray.length;
        
        // If average deviation is above threshold, we have audio activity
        if (averageDeviation > this.silenceThreshold) {
            window.ws.sendJson({
                type: 'SilenceStatus',
                isSilent: false
            });
        }
    }

    checkNeededInteractions() {
        // Check if bot has been removed from the meeting
        const removedFromMeetingElement = document.getElementById('calling-retry-screen-title');
        if (removedFromMeetingElement && 
            removedFromMeetingElement.textContent.includes("You've been removed from this meeting")) {
            window.ws.sendJson({
                type: 'MeetingStatusChange',
                change: 'removed_from_meeting'
            });
            console.log('Bot was removed from meeting, sent notification');
        }

        // We need to open the chat window to be able to track messages
        const chatButton = document.querySelector('button#chat-button');
        if (chatButton && !this.chatButtonClicked) {
            chatButton.click();
            this.chatButtonClicked = true;
            
            // Wait until the chat input element appears in the DOM
            this.waitForChatInputAndSendReadyMessage();
        }
    }

    waitForChatInputAndSendReadyMessage() {
        const checkForChatInput = () => {
            const chatInput = document.querySelector('[aria-label="Type a message"]');
            if (chatInput) {
                // Chat input is now available, send the ready message
                window.ws.sendJson({
                    type: 'ChatStatusChange',
                    change: 'ready_to_send'
                });
                console.log('Chat input element found, ready to send messages');
            } else {
                // Chat input not found yet, check again in 500ms
                setTimeout(checkForChatInput, 500);
            }
        };
        
        // Start checking for the chat input element
        checkForChatInput();
    }

    startSilenceDetection() {
         // Set up audio context and processing as before
         this.audioContext = new AudioContext();

         this.audioSources = this.audioTracks.map(track => {
             const mediaStream = new MediaStream([track]);
             return this.audioContext.createMediaStreamSource(mediaStream);
         });
 
         // Create a destination node
         const destination = this.audioContext.createMediaStreamDestination();
 
         // Connect all sources to the destination
         this.audioSources.forEach(source => {
             source.connect(destination);
         });
 
         // Create analyzer and connect it to the destination
         this.analyser = this.audioContext.createAnalyser();
         this.analyser.fftSize = 256;
         const bufferLength = this.analyser.frequencyBinCount;
         this.audioDataArray = new Uint8Array(bufferLength);
 
         // Create a source from the destination's stream and connect it to the analyzer
         const mixedSource = this.audioContext.createMediaStreamSource(destination.stream);
         mixedSource.connect(this.analyser);
 
         this.mixedAudioTrack = destination.stream.getAudioTracks()[0];

        // Process and send mixed audio if enabled
        if (window.initialData.sendMixedAudio && this.mixedAudioTrack) {
            this.processMixedAudioTrack();
        }

        // Clear any existing interval
        if (this.silenceCheckInterval) {
            clearInterval(this.silenceCheckInterval);
        }
                
        if (this.neededInteractionsInterval) {
            clearInterval(this.neededInteractionsInterval);
        }
                
        // Check for audio activity every second
        this.silenceCheckInterval = setInterval(() => {
            this.checkAudioActivity();
        }, 1000);

        // Check for needed interactions every 5 seconds
        this.neededInteractionsInterval = setInterval(() => {
            this.checkNeededInteractions();
        }, 5000);

        this.meetingAudioStream = destination.stream;
    }
    
    getMeetingAudioStream() {
        return this.meetingAudioStream;
    }

    async processMixedAudioTrack() {
        try {
            // Create processor to get raw audio frames from the mixed audio track
            const processor = new MediaStreamTrackProcessor({ track: this.mixedAudioTrack });
            const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
            
            // Get readable stream of audio frames
            const readable = processor.readable;
            const writable = generator.writable;

            // Transform stream to intercept and send audio frames
            const transformStream = new TransformStream({
                async transform(frame, controller) {
                    if (!frame) {
                        return;
                    }

                    try {
                        // Check if controller is still active
                        if (controller.desiredSize === null) {
                            frame.close();
                            return;
                        }

                        // Copy the audio data
                        const numChannels = frame.numberOfChannels;
                        const numSamples = frame.numberOfFrames;
                        const audioData = new Float32Array(numSamples);
                        
                        // Copy data from each channel
                        // If multi-channel, average all channels together to create mono output
                        if (numChannels > 1) {
                            // Temporary buffer to hold each channel's data
                            const channelData = new Float32Array(numSamples);
                            
                            // Sum all channels
                            for (let channel = 0; channel < numChannels; channel++) {
                                frame.copyTo(channelData, { planeIndex: channel });
                                for (let i = 0; i < numSamples; i++) {
                                    audioData[i] += channelData[i];
                                }
                            }
                            
                            // Average by dividing by number of channels
                            for (let i = 0; i < numSamples; i++) {
                                audioData[i] /= numChannels;
                            }
                        } else {
                            // If already mono, just copy the data
                            frame.copyTo(audioData, { planeIndex: 0 });
                        }

                        // Send mixed audio data via websocket
                        const timestamp = performance.now();
                        window.ws.sendMixedAudio(timestamp, audioData);
                        
                        // Pass through the original frame
                        controller.enqueue(frame);
                    } catch (error) {
                        console.error('Error processing mixed audio frame:', error);
                        frame.close();
                    }
                },
                flush() {
                    console.log('Mixed audio transform stream flush called');
                }
            });

            // Create an abort controller for cleanup
            const abortController = new AbortController();

            try {
                // Connect the streams
                await readable
                    .pipeThrough(transformStream)
                    .pipeTo(writable, {
                        signal: abortController.signal
                    })
                    .catch(error => {
                        if (error.name !== 'AbortError') {
                            console.error('Mixed audio pipeline error:', error);
                        }
                    });
            } catch (error) {
                console.error('Mixed audio stream pipeline error:', error);
                abortController.abort();
            }

        } catch (error) {
            console.error('Error setting up mixed audio processor:', error);
        }
    }
 
    makeMainVideoFillFrame = function() {
        /* ── 0.  Cleanup from earlier runs ─────────────────────────────── */
        if (this.blanket?.isConnected) this.blanket.remove();
        if (this.frameStyleElement?.isConnected) this.frameStyleElement.remove();
        if (this.frameAdjustInterval) {
            cancelAnimationFrame(this.frameAdjustInterval);   // ← was clearInterval
            this.frameAdjustInterval = null;
        }
    
        /* ── 1.  Inject the blanket ────────────────────────────────────── */
        const blanket = document.createElement("div");
        blanket.id = "attendee-blanket";
        Object.assign(blanket.style, {
            position: "fixed",
            inset: "0",
            background: "#fff",
            zIndex: 1998,              // below the video we’ll promote
            pointerEvents: "none"      // lets events fall through
        });
        document.body.appendChild(blanket);
        this.blanket = blanket;
    
        /* ── 2.  Promote the central video and its descendants ─────────── */
        const style = document.createElement("style");
        style.textContent = `
            /* central pane fills the viewport, highest z‑index */
            [data-test-segment-type="central"] {
                position: fixed !important;
                inset: 0 !important;
                width: 100vw !important;
                height: 100vh !important;
                z-index: 1999 !important;   /* > blanket */
            }
            /* make sure its children inherit size & events normally */
            [data-test-segment-type="central"], 
            [data-test-segment-type="central"] * {
                pointer-events: auto !important;
            }
        `;
        document.head.appendChild(style);
        this.frameStyleElement = style;
    
        /* ── 3.  Keep the central element the right size ───────────────── */
        const adjust = () => {
            this.adjustCentralElement?.();
            this.frameAdjustInterval = requestAnimationFrame(adjust);  // ← RAF loop
        };
        adjust();  // kick it off
    }
    
    adjustCentralElement = function() {
        // Get the central element
        const centralElement = document.querySelector('[data-test-segment-type="central"]');
        
        // Function to resize the central element
        function adjustCentralElementSize(element) {
            if (element?.style) {
                element.style.width  = `${window.initialData.videoFrameWidth}px`;
                element.style.height = `${window.initialData.videoFrameHeight}px`;
                element.style.position = 'fixed';
            }
        }
    
        function adjustChildElement(element) {
            if (element?.style) {
                element.style.position = 'fixed';
                element.style.width  = '100%';
                element.style.height = '100%';
                element.style.top  = '0';
                element.style.left = '0';
            }
        }
        
        if (centralElement) {
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]?.children[0]?.children[0]);
            adjustChildElement(centralElement?.children[0]);
            adjustCentralElementSize(centralElement);
        }
    }
    
    restoreOriginalFrame = function() {
        // If we have a reference to the style element, remove it
        if (this.frameStyleElement) {
            this.frameStyleElement.remove();
            this.frameStyleElement = null;
            console.log('Removed video frame style element');
        }
        
        // Cancel the RAF loop if it exists
        if (this.frameAdjustInterval) {
            cancelAnimationFrame(this.frameAdjustInterval);   // ← was clearInterval
            this.frameAdjustInterval = null;
        }
    }

    stop() {
        // Clear any existing interval
        if (this.silenceCheckInterval) {
            clearInterval(this.silenceCheckInterval);
            this.silenceCheckInterval = null;
        }
        
        if (this.neededInteractionsInterval) {
            clearInterval(this.neededInteractionsInterval);
            this.neededInteractionsInterval = null;
        }
        
        // Restore original frame layout
        this.restoreOriginalFrame();
        
        console.log('Stopped StyleManager');
    }

    start() {
        this.startSilenceDetection();
        this.makeMainVideoFillFrame();

        console.log('Started StyleManager');
    }
    
    addVideoTrack(trackEvent) {
        console.log('addVideoTrack', trackEvent, ' is currently a no-op');
    }
}

class DominantSpeakerManager {
    constructor() {
        this.dominantSpeakerStreamId = null;
        this.captionAudioTimes = [];
    }

    getLastSpeakerIdForTimestampMs(timestampMs) {
        // Find the caption audio times that are before timestampMs
        const captionAudioTimesBeforeTimestampMs = this.captionAudioTimes.filter(captionAudioTime => captionAudioTime.timestampMs <= timestampMs);
        if (captionAudioTimesBeforeTimestampMs.length === 0) {
            return null;
        }
        // Return the caption audio time with the highest timestampMs
        return captionAudioTimesBeforeTimestampMs.reduce((max, captionAudioTime) => captionAudioTime.timestampMs > max.timestampMs ? captionAudioTime : max).speakerId;
    }

    addCaptionAudioTime(timestampMs, speakerId) {
        this.captionAudioTimes.push({
            timestampMs: timestampMs,
            speakerId: speakerId
        });
    }

    setDominantSpeakerStreamId(dominantSpeakerStreamId) {
        this.dominantSpeakerStreamId = dominantSpeakerStreamId.toString();
    }

    getDominantSpeaker() {
        return virtualStreamToPhysicalStreamMappingManager.virtualStreamIdToParticipant(this.dominantSpeakerStreamId);
    }
}

// Virtual to Physical Stream Mapping Manager
// Microsoft Teams has virtual streams which are referenced by a sourceId
// An instance of the teams client has a finite number of phyisical streams which are referenced by a streamId
// This class manages the mapping between virtual and physical streams
class VirtualStreamToPhysicalStreamMappingManager {
    constructor() {
        this.virtualStreams = new Map();
        this.physicalStreamsByClientStreamId = new Map();
        this.physicalStreamsByServerStreamId = new Map();

        this.physicalClientStreamIdToVirtualStreamIdMapping = {}
        this.virtualStreamIdToPhysicalClientStreamIdMapping = {}
    }

    getVirtualVideoStreamIdToSend() {
        // If there is an active screenshare stream return that stream's virtual id

        // If there is an active dominant speaker video stream return that stream id

        // Otherwise return the first virtual stream id that has an associated physical stream
        //realConsole?.log('Object.values(this.virtualStreams)', Object.values(this.virtualStreams));
        //realConsole?.log("STARTFILTER");
        const virtualSteamsThatHavePhysicalStreams = []
        for (const virtualStream of this.virtualStreams.values()) {
            const hasCorrespondingPhysicalStream = this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStream.sourceId];
            const isNotVirtualStreamForBot = !this.physicalClientStreamIdToVirtualStreamIdMapping[virtualStream.sourceId];

            //realConsole?.log('zzzphysicalClientStreamIds', physicalClientStreamIds);
            //realConsole?.log('zzzvirtualStream.sourceId.toString()', virtualStream.sourceId.toString());
            //realConsole?.log('zzzthis.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);
            //realConsole?.log('zzzvirtualStream', virtualStream);
            //const cond1 = (virtualStream.type === 'video' || virtualStream.type === 'applicationsharing-video');
            //const cond2 = !physicalClientStreamIds.includes(virtualStream.sourceId.toString());
            //const cond3 = hasCorrespondingPhysicalStream;
            //realConsole?.log('zzzcond1', cond1, 'cond2', cond2, 'cond3', cond3);


            if ((virtualStream.isScreenShare || virtualStream.isWebcam) && isNotVirtualStreamForBot && hasCorrespondingPhysicalStream)
            {
                virtualSteamsThatHavePhysicalStreams.push(virtualStream);
            }
        };
        //realConsole?.log("ENDFILTER");
        //realConsole?.log('zzzvirtualSteamsThatHavePhysicalStreams', virtualSteamsThatHavePhysicalStreams);
        //realConsole?.log('this.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);
        if (virtualSteamsThatHavePhysicalStreams.length == 0)
            return null;

        const firstActiveScreenShareStream = virtualSteamsThatHavePhysicalStreams.find(virtualStream => virtualStream.isScreenShare && virtualStream.isActive);
        //realConsole?.log('zzzfirstActiveScreenShareStream', firstActiveScreenShareStream);
        if (firstActiveScreenShareStream)
            return firstActiveScreenShareStream.sourceId;

        const dominantSpeaker = dominantSpeakerManager.getDominantSpeaker();
        //realConsole?.log('zzzdominantSpeaker', dominantSpeaker);
        if (dominantSpeaker)
        {
            const dominantSpeakerVideoStream = virtualSteamsThatHavePhysicalStreams.find(virtualStream => virtualStream.participant.id === dominantSpeaker.id && virtualStream.isWebcam && virtualStream.isActive);
            if (dominantSpeakerVideoStream)
                return dominantSpeakerVideoStream.sourceId;
        }

        return virtualSteamsThatHavePhysicalStreams[0]?.sourceId;
    }

    getVideoStreamIdToSend() {
        
        const virtualVideoStreamIdToSend = this.getVirtualVideoStreamIdToSend();
        if (!virtualVideoStreamIdToSend)
        {
            return this.physicalStreamsByServerStreamId.keys().find(physicalServerStreamId => physicalServerStreamId.includes('Video'));
        }
        //realConsole?.log('virtualVideoStreamIdToSend', virtualVideoStreamIdToSend);
        //realConsole?.log('this.physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);

        //realConsole?.log('Object.entries(this.physicalClientStreamIdToVirtualStreamIdMapping)', Object.entries(this.physicalClientStreamIdToVirtualStreamIdMapping));

        // Find the physical client stream ID that maps to this virtual stream ID
        const physicalClientStreamId = this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualVideoStreamIdToSend];
            
        //realConsole?.log('physicalClientStreamId', physicalClientStreamId);
        //realConsole?.log('this.physicalStreamsByClientStreamId', this.physicalStreamsByClientStreamId);

        const physicalStream = this.physicalStreamsByClientStreamId.get(physicalClientStreamId);
        if (!physicalStream)
            return null;

        //realConsole?.log('physicalStream', physicalStream);
            
        return physicalStream.serverStreamId;
    }

    upsertPhysicalStreams(physicalStreams) {
        for (const physicalStream of physicalStreams) {
            this.physicalStreamsByClientStreamId.set(physicalStream.clientStreamId, physicalStream);
            this.physicalStreamsByServerStreamId.set(physicalStream.serverStreamId, physicalStream);
        }
        realConsole?.log('physicalStreamsByClientStreamId', this.physicalStreamsByClientStreamId);
        realConsole?.log('physicalStreamsByServerStreamId', this.physicalStreamsByServerStreamId);
    }

    upsertVirtualStream(virtualStream) {
        realConsole?.log('upsertVirtualStream', virtualStream, 'this.virtualStreams', this.virtualStreams);
        this.virtualStreams.set(virtualStream.sourceId.toString(), {...virtualStream, sourceId: virtualStream.sourceId.toString()});
    }
    
    removeVirtualStreamsForParticipant(participantId) {
        const virtualStreamsToRemove = Array.from(this.virtualStreams.values()).filter(virtualStream => virtualStream.participant.id === participantId);
        for (const virtualStream of virtualStreamsToRemove) {
            this.virtualStreams.delete(virtualStream.sourceId.toString());
        }
    }

    upsertPhysicalClientStreamIdToVirtualStreamIdMapping(physicalClientStreamId, virtualStreamId) {
        const physicalClientStreamIdString = physicalClientStreamId.toString();
        const virtualStreamIdString = virtualStreamId.toString();
        if (virtualStreamIdString === '-1')
        {
            // Find and delete from the inverse mapping first
            const virtualStreamIdToDelete = this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString];
            if (virtualStreamIdToDelete) {
                delete this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStreamIdToDelete];
            }
            else {
                realConsole?.error('Entry for virtual stream id ', virtualStreamIdToDelete, ' not found in', this.virtualStreamIdToPhysicalClientStreamIdMapping);
            }
            // Then delete from the main mapping
            delete this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString];
        }
        else {
            this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamIdString] = virtualStreamIdString;
            this.virtualStreamIdToPhysicalClientStreamIdMapping[virtualStreamIdString] = physicalClientStreamIdString;
        }
        realConsole?.log('physicalClientStreamId', physicalClientStreamIdString, 'virtualStreamId', virtualStreamIdString, 'physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping, 'virtualStreamIdToPhysicalClientStreamIdMapping', this.virtualStreamIdToPhysicalClientStreamIdMapping);
    }

    virtualStreamIdToParticipant(virtualStreamId) {
        return this.virtualStreams.get(virtualStreamId)?.participant;
    }

    physicalServerStreamIdToParticipant(physicalServerStreamId) {
        realConsole?.log('physicalServerStreamId', physicalServerStreamId);
        realConsole?.log('physicalClientStreamIdToVirtualStreamIdMapping', this.physicalClientStreamIdToVirtualStreamIdMapping);

        const physicalClientStreamId = this.physicalStreamsByServerStreamId.get(physicalServerStreamId)?.clientStreamId;
        realConsole?.log('physicalClientStreamId', physicalClientStreamId);
        if (!physicalClientStreamId)
            return null;

        const virtualStreamId = this.physicalClientStreamIdToVirtualStreamIdMapping[physicalClientStreamId];
        if (!virtualStreamId)
            return null;

        const participant = this.virtualStreams.get(virtualStreamId)?.participant;
        if (!participant)
            return null;

        return participant;
    }
}



class RTCInterceptor {
    constructor(callbacks) {
        // Store the original RTCPeerConnection
        const originalRTCPeerConnection = window.RTCPeerConnection;
        
        // Store callbacks
        const onPeerConnectionCreate = callbacks.onPeerConnectionCreate || (() => {});
        const onDataChannelCreate = callbacks.onDataChannelCreate || (() => {});
        const onDataChannelSend = callbacks.onDataChannelSend || (() => {});
        
        // Override the RTCPeerConnection constructor
        window.RTCPeerConnection = function(...args) {
            // Create instance using the original constructor
            const peerConnection = Reflect.construct(
                originalRTCPeerConnection, 
                args
            );
            
            // Notify about the creation
            onPeerConnectionCreate(peerConnection);
            
            // Override createDataChannel
            const originalCreateDataChannel = peerConnection.createDataChannel.bind(peerConnection);
            peerConnection.createDataChannel = (label, options) => {
                const dataChannel = originalCreateDataChannel(label, options);
                
                // Intercept send method
                const originalSend = dataChannel.send;
                dataChannel.send = function(data) {
                    try {
                        onDataChannelSend({
                            channel: dataChannel,
                            data: data,
                            peerConnection: peerConnection
                        });
                    } catch (error) {
                        realConsole?.error('Error in data channel send interceptor:', error);
                    }
                    return originalSend.apply(this, arguments);
                };
                
                onDataChannelCreate(dataChannel, peerConnection);
                return dataChannel;
            };
            
            // Intercept createOffer
            const originalCreateOffer = peerConnection.createOffer.bind(peerConnection);
            peerConnection.createOffer = async function(options) {
                const offer = await originalCreateOffer(options);
                realConsole?.log('from peerConnection.createOffer:', offer.sdp);
                /*
                console.log('Created Offer SDP:', {
                    type: offer.type,
                    sdp: offer.sdp,
                    parsedSDP: parseSDP(offer.sdp)
                });
                */
                realConsole?.log('from peerConnection.createOffer: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(offer.sdp));
                return offer;
            };

            // Intercept createAnswer
            const originalCreateAnswer = peerConnection.createAnswer.bind(peerConnection);
            peerConnection.createAnswer = async function(options) {
                const answer = await originalCreateAnswer(options);
                realConsole?.log('from peerConnection.createAnswer:', answer.sdp);
                /*
                console.log('Created Answer SDP:', {
                    type: answer.type,
                    sdp: answer.sdp,
                    parsedSDP: parseSDP(answer.sdp)
                });
                */
                realConsole?.log('from peerConnection.createAnswer: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(answer.sdp));
                return answer;
            };
       
            

/*

how the mapping works:
the SDP contains x-source-streamid:<some value>
this corresponds to the stream id / source id in the participants hash
So that correspondences allows us to map a participant stream to an SDP. But how do we go from SDP to the raw low level track id? 
The tracks have a streamId that looks like this mainVideo-39016. The SDP has that same streamId contained within it in the msid: header
3396





*/
            // Override setLocalDescription with detailed logging
            const originalSetLocalDescription = peerConnection.setLocalDescription;
            peerConnection.setLocalDescription = async function(description) {
                realConsole?.log('from peerConnection.setLocalDescription:', description.sdp);
                /*
                console.log('Setting Local SDP:', {
                    type: description.type,
                    sdp: description.sdp,
                    parsedSDP: parseSDP(description.sdp)
                });
                */
                realConsole?.log('from peerConnection.setLocalDescription: extractStreamIdToSSRCMappingFromSDP = ', extractStreamIdToSSRCMappingFromSDP(description.sdp));
                return originalSetLocalDescription.apply(this, arguments);
            };

            // Override setRemoteDescription with detailed logging
            const originalSetRemoteDescription = peerConnection.setRemoteDescription;
            peerConnection.setRemoteDescription = async function(description) {
                realConsole?.log('from peerConnection.setRemoteDescription:', description.sdp);
                /*
                console.log('Setting Remote SDP:', {
                    type: description.type,
                    parsedSDP: parseSDP(description.sdp)
                });
                */
                const mapping = extractStreamIdToSSRCMappingFromSDP(description.sdp);
                realConsole?.log('from peerConnection.setRemoteDescription: extractStreamIdToSSRCMappingFromSDP = ', mapping);
                virtualStreamToPhysicalStreamMappingManager.upsertPhysicalStreams(mapping);
                return originalSetRemoteDescription.apply(this, arguments);
            };

            function extractMSID(rawSSRCEntry) {
                if (!rawSSRCEntry) return null;
                
                const parts = rawSSRCEntry.split(' ');
                for (const part of parts) {
                    if (part.startsWith('msid:')) {
                        return part.substring(5).split(' ')[0];
                    }
                }
                return null;
            }

            function extractStreamIdToSSRCMappingFromSDP(sdp)
            {
                const parsedSDP = parseSDP(sdp);
                const mapping = [];
                const sdpMediaList = parsedSDP.media || [];

                for (const sdpMediaEntry of sdpMediaList) {
                    const sdpMediaEntryAttributes = sdpMediaEntry.attributes || {};
                    //realConsole?.log('sdpMediaEntryAttributes', sdpMediaEntryAttributes);
                    //realConsole?.log(sdpMediaEntry);
                    const sdpMediaEntrySSRCNumbersRaw = sdpMediaEntryAttributes.ssrc || [];
                    const sdpMediaEntrySSRCNumbers = [...new Set(sdpMediaEntrySSRCNumbersRaw.map(x => extractMSID(x)))];

                    const streamIds = sdpMediaEntryAttributes['x-source-streamid'] || [];
                    if (streamIds.length > 1)
                        console.warn('Warning: x-source-streamid has multiple stream ids');
                    
                    const streamId = streamIds[0];

                    for(const ssrc of sdpMediaEntrySSRCNumbers)
                        if (ssrc && streamId)
                            mapping.push({clientStreamId: streamId, serverStreamId: ssrc});
                }

                return mapping;
            }

            // Helper function to parse SDP into a more readable format
            function parseSDP(sdp) {
                const parsed = {
                    media: [],
                    attributes: {},
                    version: null,
                    origin: null,
                    session: null,
                    connection: null,
                    timing: null,
                    bandwidth: null
                };
            
                const lines = sdp.split('\r\n');
                let currentMedia = null;
            
                for (const line of lines) {
                    // Handle session-level fields
                    if (line.startsWith('v=')) {
                        parsed.version = line.substr(2);
                    } else if (line.startsWith('o=')) {
                        parsed.origin = line.substr(2);
                    } else if (line.startsWith('s=')) {
                        parsed.session = line.substr(2);
                    } else if (line.startsWith('c=')) {
                        parsed.connection = line.substr(2);
                    } else if (line.startsWith('t=')) {
                        parsed.timing = line.substr(2);
                    } else if (line.startsWith('b=')) {
                        parsed.bandwidth = line.substr(2);
                    } else if (line.startsWith('m=')) {
                        // Media section
                        currentMedia = {
                            type: line.split(' ')[0].substr(2),
                            description: line,
                            attributes: {},
                            connection: null,
                            bandwidth: null
                        };
                        parsed.media.push(currentMedia);
                    } else if (line.startsWith('a=')) {
                        // Handle attributes that may contain multiple colons
                        const colonIndex = line.indexOf(':');
                        let key, value;
                        
                        if (colonIndex === -1) {
                            // Handle flag attributes with no value
                            key = line.substr(2);
                            value = true;
                        } else {
                            key = line.substring(2, colonIndex);
                            value = line.substring(colonIndex + 1);
                        }
            
                        if (currentMedia) {
                            if (!currentMedia.attributes[key]) {
                                currentMedia.attributes[key] = [];
                            }
                            currentMedia.attributes[key].push(value);
                        } else {
                            if (!parsed.attributes[key]) {
                                parsed.attributes[key] = [];
                            }
                            parsed.attributes[key].push(value);
                        }
                    } else if (line.startsWith('c=') && currentMedia) {
                        currentMedia.connection = line.substr(2);
                    } else if (line.startsWith('b=') && currentMedia) {
                        currentMedia.bandwidth = line.substr(2);
                    }
                }
            
                return parsed;
            }

            return peerConnection;
        };
    }
}


class ChatMessageManager {
    constructor(ws) {
        this.ws = ws;
    }

    // The more sophisticated approach gets blocked by trusted html csp
    stripHtml(html) {
        return html.replace(/<[^>]*>/g, '');
    }

    handleChatMessage(chatMessage) {
        try {
            if (!chatMessage.clientMessageId)
                return;
            if (!chatMessage.from)
                return;
            if (!chatMessage.content)
                return;
            if (!chatMessage.originalArrivalTime)
                return;

            const timestamp_ms = new Date(chatMessage.originalArrivalTime).getTime();
            this.ws.sendJson({
                type: 'ChatMessage',
                message_uuid: chatMessage.clientMessageId,
                participant_uuid: chatMessage.from,
                timestamp: Math.floor(timestamp_ms / 1000),
                text: this.stripHtml(chatMessage.content),
            });
        }
        catch (error) {
            console.error('Error in handleChatMessage', error);
        }
    }
}

// User manager
class UserManager {
    constructor(ws) {
        this.allUsersMap = new Map();
        this.currentUsersMap = new Map();
        this.deviceOutputMap = new Map();

        this.ws = ws;
    }


    getDeviceOutput(deviceId, outputType) {
        return this.deviceOutputMap.get(`${deviceId}-${outputType}`);
    }

    updateDeviceOutputs(deviceOutputs) {
        for (const output of deviceOutputs) {
            const key = `${output.deviceId}-${output.deviceOutputType}`; // Unique key combining device ID and output type

            const deviceOutput = {
                deviceId: output.deviceId,
                outputType: output.deviceOutputType, // 1 = audio, 2 = video
                streamId: output.streamId,
                disabled: output.deviceOutputStatus.disabled,
                lastUpdated: Date.now()
            };

            this.deviceOutputMap.set(key, deviceOutput);
        }

        // Notify websocket clients about the device output update
        this.ws.sendJson({
            type: 'DeviceOutputsUpdate',
            deviceOutputs: Array.from(this.deviceOutputMap.values())
        });
    }

    getUserByDeviceId(deviceId) {
        return this.allUsersMap.get(deviceId);
    }

    // constants for meeting status
    MEETING_STATUS = {
        IN_MEETING: 1,
        NOT_IN_MEETING: 6
    }

    getCurrentUsersInMeeting() {
        return Array.from(this.currentUsersMap.values()).filter(user => user.status === this.MEETING_STATUS.IN_MEETING);
    }

    getCurrentUsersInMeetingWhoAreScreenSharing() {
        return this.getCurrentUsersInMeeting().filter(user => user.parentDeviceId);
    }

    convertUser(user) {
        const currentUserId = window.callManager?.getCurrentUserId();
        return {
            deviceId: user.details.id,
            displayName: user.details.displayName,
            fullName: user.details.displayName,
            profile: '',
            status: user.state,
            humanized_status: user.state === "active" ? "in_meeting" : "not_in_meeting",
            isCurrentUser: (!!currentUserId) && (user.details.id === currentUserId),
            isHost: user.meetingRole === "organizer"
        }
    }

    singleUserSynced(user) {
      const convertedUser = this.convertUser(user);
      console.log('singleUserSynced called w', convertedUser);
      // Create array with new user and existing users, then filter for unique deviceIds
      // keeping the first occurrence (new user takes precedence)
      const allUsers = [...this.currentUsersMap.values(), convertedUser];
      console.log('allUsers', allUsers);
      const uniqueUsers = Array.from(
        new Map(allUsers.map(singleUser => [singleUser.deviceId, singleUser])).values()
      );
      this.newUsersListSynced(uniqueUsers);
    }

    newUsersListSynced(newUsersList) {
        console.log('newUsersListSynced called w', newUsersList);
        // Get the current user IDs before updating
        const previousUserIds = new Set(this.currentUsersMap.keys());
        const newUserIds = new Set(newUsersList.map(user => user.deviceId));
        const updatedUserIds = new Set([])

        // Update all users map
        for (const user of newUsersList) {
            if (previousUserIds.has(user.deviceId) && JSON.stringify(this.currentUsersMap.get(user.deviceId)) !== JSON.stringify(user)) {
                updatedUserIds.add(user.deviceId);
            }

            this.allUsersMap.set(user.deviceId, {
                deviceId: user.deviceId,
                displayName: user.displayName,
                fullName: user.fullName,
                profile: user.profile,
                status: user.status,
                humanized_status: user.humanized_status,
                parentDeviceId: user.parentDeviceId,
                isCurrentUser: user.isCurrentUser,
                isHost: user.isHost
            });
        }

        // Calculate new, removed, and updated users
        const newUsers = newUsersList.filter(user => !previousUserIds.has(user.deviceId));
        const removedUsers = Array.from(previousUserIds)
            .filter(id => !newUserIds.has(id))
            .map(id => this.currentUsersMap.get(id));

        if (removedUsers.length > 0) {
            console.log('removedUsers', removedUsers);
        }

        // Clear current users map and update with new list
        this.currentUsersMap.clear();
        for (const user of newUsersList) {
            this.currentUsersMap.set(user.deviceId, {
                deviceId: user.deviceId,
                displayName: user.displayName,
                fullName: user.fullName,
                profilePicture: user.profilePicture,
                status: user.status,
                humanized_status: user.humanized_status,
                parentDeviceId: user.parentDeviceId,
                isCurrentUser: user.isCurrentUser,
                isHost: user.isHost
            });
        }

        const updatedUsers = Array.from(updatedUserIds).map(id => this.currentUsersMap.get(id));

        if (newUsers.length > 0 || removedUsers.length > 0 || updatedUsers.length > 0) {
            this.ws.sendJson({
                type: 'UsersUpdate',
                newUsers: newUsers,
                removedUsers: removedUsers,
                updatedUsers: updatedUsers
            });
        }
    }
}
var realConsole;
// Websocket client
class WebSocketClient {
    // Message types
    static MESSAGE_TYPES = {
        JSON: 1,
        VIDEO: 2,  // Reserved for future use
        AUDIO: 3,   // Reserved for future use
        ENCODED_MP4_CHUNK: 4,
        PER_PARTICIPANT_AUDIO: 5
    };
  
    constructor() {
        const url = `ws://localhost:${window.initialData.websocketPort}`;
        console.log('WebSocketClient url', url);
        this.ws = new WebSocket(url);
        this.ws.binaryType = 'arraybuffer';
        
        this.ws.onopen = () => {
            console.log('WebSocket Connected');
        };
        
        this.ws.onmessage = (event) => {
            this.handleMessage(event.data);
        };
        
        this.ws.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };
        
        this.ws.onclose = () => {
            console.log('WebSocket Disconnected');
        };
  
        this.mediaSendingEnabled = false;
        /*
        We no longer need this because we're not using MediaStreamTrackProcessor's
        this.lastVideoFrameTime = performance.now();
        this.blackFrameInterval = null;
        */
    }
  
    /*
    startBlackFrameTimer() {
      if (this.blackFrameInterval) return; // Don't start if already running
      
      this.blackFrameInterval = setInterval(() => {
          try {
              const currentTime = performance.now();
              if (currentTime - this.lastVideoFrameTime >= 500 && this.mediaSendingEnabled) {
                  // Create black frame data (I420 format)
                  const width = window.initialData.videoFrameWidth, height = window.initialData.videoFrameHeight;
                  const yPlaneSize = width * height;
                  const uvPlaneSize = (width * height) / 4;
                  
                  const frameData = new Uint8Array(yPlaneSize + 2 * uvPlaneSize);
                  // Y plane (black = 0)
                  frameData.fill(0, 0, yPlaneSize);
                  // U and V planes (black = 128)
                  frameData.fill(128, yPlaneSize);
                  
                  // Fix: Math.floor() the milliseconds before converting to BigInt
                  const currentTimeMicros = BigInt(Math.floor(currentTime) * 1000);
                  this.sendVideo(currentTimeMicros, '0', width, height, frameData);
              }
          } catch (error) {
              console.error('Error in black frame timer:', error);
          }
      }, 250);
    }
  
    stopBlackFrameTimer() {
        if (this.blackFrameInterval) {
            clearInterval(this.blackFrameInterval);
            this.blackFrameInterval = null;
        }
    }
    */
  
    enableMediaSending() {
        this.mediaSendingEnabled = true;
        window.styleManager.start();
        window.callManager.syncParticipants();

        // No longer need this because we're not using MediaStreamTrackProcessor's
        //this.startBlackFrameTimer();
    }

    async disableMediaSending() {
        window.styleManager.stop();
        //window.fullCaptureManager.stop();
        // Give the media recorder a bit of time to send the final data
        await new Promise(resolve => setTimeout(resolve, 2000));
        this.mediaSendingEnabled = false;

        // No longer need this because we're not using MediaStreamTrackProcessor's
        //this.stopBlackFrameTimer();
    }

  
    handleMessage(data) {
        const view = new DataView(data);
        const messageType = view.getInt32(0, true); // true for little-endian
        
        // Handle different message types
        switch (messageType) {
            case WebSocketClient.MESSAGE_TYPES.JSON:
                const jsonData = new TextDecoder().decode(new Uint8Array(data, 4));
                console.log('Received JSON message:', JSON.parse(jsonData));
                break;
            // Add future message type handlers here
            default:
                console.warn('Unknown message type:', messageType);
        }
    }
    
    sendJson(data) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected');
            return;
        }
  
        try {
            // Convert JSON to string then to Uint8Array
            const jsonString = JSON.stringify(data);
            const jsonBytes = new TextEncoder().encode(jsonString);
            
            // Create final message: type (4 bytes) + json data
            const message = new Uint8Array(4 + jsonBytes.length);
            
            // Set message type (1 for JSON)
            new DataView(message.buffer).setInt32(0, WebSocketClient.MESSAGE_TYPES.JSON, true);
            
            // Copy JSON data after type
            message.set(jsonBytes, 4);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket message:', error);
            console.error('Message data:', data);
        }
    }
  
    sendClosedCaptionUpdate(item) {
        if (!this.mediaSendingEnabled)
            return;
    
        this.sendJson({
            type: 'CaptionUpdate',
            caption: item
        });
    }

    sendMixedAudio(timestamp, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for audio send', this.ws.readyState);
            return;
        }
  
        if (!this.mediaSendingEnabled) {
          return;
        }
  
        try {
            // Create final message: type (4 bytes) + audio data
            const message = new Uint8Array(4 + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (3 for AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.AUDIO, true);
            
            // Copy audio data after type
            message.set(new Uint8Array(audioData.buffer), 4);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
    }
  
    sendPerParticipantAudio(participantId, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for per participant audio send', this.ws.readyState);
            return;
        }
    
        if (!this.mediaSendingEnabled) {
          return;
        }
    
        try {
            // Convert participantId to UTF-8 bytes
            const participantIdBytes = new TextEncoder().encode(participantId);
            
            // Create final message: type (4 bytes) + participantId length (1 byte) + 
            // participantId bytes + audio data
            const message = new Uint8Array(4 + 1 + participantIdBytes.length + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (5 for PER_PARTICIPANT_AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.PER_PARTICIPANT_AUDIO, true);
            
            // Set participantId length as uint8 (1 byte)
            dataView.setUint8(4, participantIdBytes.length);
            
            // Copy participantId bytes
            message.set(participantIdBytes, 5);
            
            // Copy audio data after type, length and participantId
            message.set(new Uint8Array(audioData.buffer), 5 + participantIdBytes.length);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
      }

    sendAudio(timestamp, streamId, audioData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            realConsole?.error('WebSocket is not connected for audio send', this.ws.readyState);
            return;
        }
  
        if (!this.mediaSendingEnabled) {
          return;
        }
  
        try {
            // Create final message: type (4 bytes) + timestamp (8 bytes) + audio data
            const message = new Uint8Array(4 + 8 + 4 + audioData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (3 for AUDIO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.AUDIO, true);
            
            // Set timestamp as BigInt64
            dataView.setBigInt64(4, BigInt(timestamp), true);
  
            // Set streamId length and bytes
            dataView.setInt32(12, streamId, true);
  
            // Copy audio data after type and timestamp
            message.set(new Uint8Array(audioData.buffer), 16);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            realConsole?.error('Error sending WebSocket audio message:', error);
        }
    }
  
    sendVideo(timestamp, streamId, width, height, videoData) {
        if (this.ws.readyState !== originalWebSocket.OPEN) {
            console.error('WebSocket is not connected for video send', this.ws.readyState);
            return;
        }
  
        if (!this.mediaSendingEnabled) {
          return;
        }
        
        this.lastVideoFrameTime = performance.now();
  
        try {
            // Convert streamId to UTF-8 bytes
            const streamIdBytes = new TextEncoder().encode(streamId);
            
            // Create final message: type (4 bytes) + timestamp (8 bytes) + streamId length (4 bytes) + 
            // streamId bytes + width (4 bytes) + height (4 bytes) + video data
            const message = new Uint8Array(4 + 8 + 4 + streamIdBytes.length + 4 + 4 + videoData.buffer.byteLength);
            const dataView = new DataView(message.buffer);
            
            // Set message type (2 for VIDEO)
            dataView.setInt32(0, WebSocketClient.MESSAGE_TYPES.VIDEO, true);
            
            // Set timestamp as BigInt64
            dataView.setBigInt64(4, BigInt(timestamp), true);
  
            // Set streamId length and bytes
            dataView.setInt32(12, streamIdBytes.length, true);
            message.set(streamIdBytes, 16);
  
            // Set width and height
            const streamIdOffset = 16 + streamIdBytes.length;
            dataView.setInt32(streamIdOffset, width, true);
            dataView.setInt32(streamIdOffset + 4, height, true);
  
            // Copy video data after headers
            message.set(new Uint8Array(videoData.buffer), streamIdOffset + 8);
            
            // Send the binary message
            this.ws.send(message.buffer);
        } catch (error) {
            console.error('Error sending WebSocket video message:', error);
        }
    }
  }

class WebSocketInterceptor {
    constructor(callbacks = {}) {
        this.originalWebSocket = window.WebSocket;
        this.callbacks = {
            onSend: callbacks.onSend || (() => {}),
            onMessage: callbacks.onMessage || (() => {}),
            onOpen: callbacks.onOpen || (() => {}),
            onClose: callbacks.onClose || (() => {}),
            onError: callbacks.onError || (() => {})
        };
        
        window.WebSocket = this.createWebSocketProxy();
    }

    createWebSocketProxy() {
        const OriginalWebSocket = this.originalWebSocket;
        const callbacks = this.callbacks;
        
        return function(url, protocols) {
            const ws = new OriginalWebSocket(url, protocols);
            
            // Intercept send
            const originalSend = ws.send;
            ws.send = function(data) {
                try {
                    callbacks.onSend({
                        url,
                        data,
                        ws
                    });
                } catch (error) {
                    realConsole?.log('Error in WebSocket send callback:');
                    realConsole?.log(error);
                }
                
                return originalSend.apply(ws, arguments);
            };
            
            // Intercept onmessage
            ws.addEventListener('message', function(event) {
                try {
                    callbacks.onMessage({
                        url,
                        data: event.data,
                        event,
                        ws
                    });
                } catch (error) {
                    realConsole?.log('Error in WebSocket message callback:');
                    realConsole?.log(error);
                }
            });
            
            // Intercept connection events
            ws.addEventListener('open', (event) => {
                callbacks.onOpen({ url, event, ws });
            });
            
            ws.addEventListener('close', (event) => {
                callbacks.onClose({ 
                    url, 
                    code: event.code, 
                    reason: event.reason,
                    event,
                    ws 
                });
            });
            
            ws.addEventListener('error', (event) => {
                callbacks.onError({ url, event, ws });
            });
            
            return ws;
        };
    }
}

function decodeWebSocketBody(encodedData) {
    const byteArray = Uint8Array.from(atob(encodedData), c => c.charCodeAt(0));
    return JSON.parse(pako.inflate(byteArray, { to: "string" }));
}

function syncVirtualStreamsFromParticipant(participant) {
    if (participant.state === 'inactive') {
        virtualStreamToPhysicalStreamMappingManager.removeVirtualStreamsForParticipant(participant.details?.id);
        return;
    }

    const mediaStreams = [];
    
    // Check if participant has endpoints
    if (participant.endpoints) {
        // Iterate through all endpoints
        Object.values(participant.endpoints).forEach(endpoint => {
            // Check if endpoint has call and mediaStreams
            if (endpoint.call && Array.isArray(endpoint.call.mediaStreams)) {
                // Add all mediaStreams from this endpoint to our array
                mediaStreams.push(...endpoint.call.mediaStreams);
            }
        });
    }
    
    for (const mediaStream of mediaStreams) {
        const isScreenShare = mediaStream.type === 'applicationsharing-video';
        const isWebcam = mediaStream.type === 'video';
        const isActive = mediaStream.direction === 'sendrecv' || mediaStream.direction === 'sendonly';
        virtualStreamToPhysicalStreamMappingManager.upsertVirtualStream(
            {...mediaStream, participant: {displayName: participant.details?.displayName, id: participant.details?.id}, isScreenShare, isWebcam, isActive}
        );
    }
}

function handleRosterUpdate(eventDataObject) {
    try {
        const decodedBody = decodeWebSocketBody(eventDataObject.body);
        realConsole?.log('handleRosterUpdate decodedBody', decodedBody);
        // Teams includes a user with no display name. Not sure what this user is but we don't want to sync that user.
        const participants = Object.values(decodedBody.participants).filter(participant => participant.details?.displayName);

        for (const participant of participants) {
            window.userManager.singleUserSynced(participant);
            syncVirtualStreamsFromParticipant(participant);
        }
    } catch (error) {
        realConsole?.error('Error handling roster update:');
        realConsole?.error(error);
    }
}

function handleConversationEnd(eventDataObject) {

    let eventDataObjectBody = {};
    try {
        eventDataObjectBody = JSON.parse(eventDataObject.body);
    } catch (error) {
        realConsole?.error('Error parsing eventDataObject.body:', error);

        try {
            eventDataObjectBody = decodeWebSocketBody(eventDataObject.body);
        } catch (error) {
            realConsole?.error('Error decoding eventDataObject.body:', error);
        }
    }

    realConsole?.log('handleConversationEnd, eventDataObjectBody', eventDataObjectBody);

    const subCode = eventDataObjectBody?.subCode;
    const subCodeValueForDeniedRequestToJoin = 5854;

    if (subCode === subCodeValueForDeniedRequestToJoin)
    {
        // For now this won't do anything, but good to have it in our logs
        window.ws?.sendJson({
            type: 'MeetingStatusChange',
            change: 'request_to_join_denied'
        });
        return;
    }

    realConsole?.log('handleConversationEnd, sending meeting ended message');
    window.ws?.sendJson({
        type: 'MeetingStatusChange',
        change: 'meeting_ended'
    });
}

const originalWebSocket = window.WebSocket;
// Example usage:
const wsInterceptor = new WebSocketInterceptor({
    /*
    onSend: ({ url, data }) => {
        if (url.startsWith('ws://localhost:8097'))
            return;
        
        //realConsole?.log('websocket onSend', url, data);        
    },
    */
    onMessage: ({ url, data }) => {
        realConsole?.log('onMessage', url, data);
        if (data.startsWith("3:::")) {
            const eventDataObject = JSON.parse(data.slice(4));
            
            realConsole?.log('Event Data Object:', eventDataObject);
            if (eventDataObject.url.endsWith("rosterUpdate/") || eventDataObject.url.endsWith("rosterUpdate")) {
                handleRosterUpdate(eventDataObject);
            }
            if (eventDataObject.url.endsWith("conversation/conversationEnd/")) {
                handleConversationEnd(eventDataObject);
            }
            /*
            Not sure if this is needed
            if (eventDataObject.url.endsWith("controlVideoStreaming/")) {
                handleControlVideoStreaming(eventDataObject);
            }
            */
        }
    }
});


const ws = new WebSocketClient();
window.ws = ws;
const userManager = new UserManager(ws);
window.userManager = userManager;

const chatMessageManager = new ChatMessageManager(ws);
window.chatMessageManager = chatMessageManager;

//const videoTrackManager = new VideoTrackManager(ws);
const virtualStreamToPhysicalStreamMappingManager = new VirtualStreamToPhysicalStreamMappingManager();
const dominantSpeakerManager = new DominantSpeakerManager();

const styleManager = new StyleManager();
window.styleManager = styleManager;

if (!realConsole) {
    if (document.readyState === 'complete') {
        createIframe();
    } else {
        document.addEventListener('DOMContentLoaded', createIframe);
    }
    function createIframe() {
        const iframe = document.createElement('iframe');
        iframe.src = 'about:blank';
        document.body.appendChild(iframe);
        realConsole = iframe.contentWindow.console;
    }
}

const processDominantSpeakerHistoryMessage = (item) => {
    realConsole?.log('processDominantSpeakerHistoryMessage', item);
    const newDominantSpeakerAudioVirtualStreamId = item.history[0];
    dominantSpeakerManager.setDominantSpeakerStreamId(newDominantSpeakerAudioVirtualStreamId);
    realConsole?.log('newDominantSpeakerParticipant', dominantSpeakerManager.getDominantSpeaker());
}

function convertTimestampAudioSentToUnixTimeMs(timestampAudioSent) {
    const fractional_seconds_since_1900 = timestampAudioSent / 10000000;
    const fractional_seconds_since_1970 = fractional_seconds_since_1900 - 2_208_988_800;
    return Math.floor(fractional_seconds_since_1970 * 1000);
}

class UtteranceIdGenerator {
    constructor(generate = () => crypto.randomUUID()) {
      this._activeIds = new Map();  // Map<speakerKey, utteranceId>
      this._generate = generate;    // Injectable for tests
    }
  
    /**
     * @param {string} speakerKey  – any stable identifier for the speaker
     * @param {boolean} isFinal    – true only on the last chunk of an utterance
     * @returns {string}           – the utteranceId to attach to this chunk
     */
    next(speakerKey = 'default', isFinal = false) {
      // Reuse or create
      let id = this._activeIds.get(speakerKey);
      if (!id) {
        id = this._generate();
        // Only keep it around if more chunks are expected
        if (!isFinal) this._activeIds.set(speakerKey, id);
      } else if (isFinal) {
        // Utterance ends: remove from the map after returning the same ID
        this._activeIds.delete(speakerKey);
      }
  
      return id;
    }
  
    /** Optional: free all state (e.g., when a call ends) */
    dispose() {
      this._activeIds.clear();
    }
}

const utteranceIdGenerator = new UtteranceIdGenerator();

const processClosedCaptionData = (item) => {
    realConsole?.log('processClosedCaptionData', item);

    // If we're collecting per participant audio, we actually need the caption data because it's the most accurate
    // way to estimate when someone started speaking.
    if (window.initialData.sendPerParticipantAudio)
    {
        const timeStampAudioSentUnixMs = convertTimestampAudioSentToUnixTimeMs(item.timestampAudioSent);
        dominantSpeakerManager.addCaptionAudioTime(timeStampAudioSentUnixMs, item.userId);
    }

    // If we don't need the captions, we can leave.
    if (!window.initialData.collectCaptions)
    {
        return;
    }

    if (!window.ws) {
        return;
    }

    const itemConverted = {
        deviceId: item.userId,
        captionId: utteranceIdGenerator.next(item.userId, item.isFinal),
        text: item.text,
        audioTimestamp: item.timestampAudioSent,
        isFinal: item.isFinal
    };
    
    window.ws.sendClosedCaptionUpdate(itemConverted);
}

const decodeMainChannelData = (data) => {
    const decodedData = new Uint8Array(data);
    for (let i = 0; i < decodedData.length; i++) {
        if (decodedData[i] === 91 || decodedData[i] === 123) { // ASCII code for '[' or '{'
            const candidateJsonString = new TextDecoder().decode(decodedData.slice(i));
            try {
                return JSON.parse(candidateJsonString);
            }
            catch(e) {
                if (e instanceof SyntaxError) {
                    // If JSON parsing fails, continue looking for the next '[' or '{' character
                    // as binary data may contain bytes that coincidentally match these character codes
                    continue;
                }
                realConsole?.error('Failed to parse main channel data:', e);
                return;            
            }        
        }
    }
}

const handleMainChannelEvent = (event) => {
    try {
        const parsedData = decodeMainChannelData(event.data);
        if (!parsedData) {
            realConsole?.error('handleMainChannelEvent: Failed to parse main channel data, returning, data:', event.data);
            return;
        }
        realConsole?.log('handleMainChannelEvent parsedData', parsedData);
        // When you see this parsedData [{"history":[1053,2331],"type":"dsh"}]
        // it corresponds to active speaker
        if (Array.isArray(parsedData)) {
            for (const item of parsedData) {
                // This is a dominant speaker history message
                if (item.type === 'dsh') {
                    processDominantSpeakerHistoryMessage(item);
                }
            }
        }
        else
        {
            if (parsedData.recognitionResults) {
                for(const item of parsedData.recognitionResults) {
                    processClosedCaptionData(item);
                }
            }
        }
    } catch (e) {
        realConsole?.error('handleMainChannelEvent: Failed to parse main channel data:', e);
    }
}

const processSourceRequest = (item) => {
    const sourceId = item?.controlVideoStreaming?.controlInfo?.sourceId;
    const streamMsid = item?.controlVideoStreaming?.controlInfo?.streamMsid;

    if (!sourceId || !streamMsid) {
        return;
    }

    virtualStreamToPhysicalStreamMappingManager.upsertPhysicalClientStreamIdToVirtualStreamIdMapping(streamMsid.toString(), sourceId.toString());
}

const handleMainChannelSend = (data) => {

    try {
        const parsedData = decodeMainChannelData(data);
        if (!parsedData) {
            realConsole?.error('handleMainChannelSend: Failed to parse main channel data, returning, data:', data);
            return;
        }
        realConsole?.log('handleMainChannelSend parsedData', parsedData);  
        // if it is an array
        if (Array.isArray(parsedData)) {
            for (const item of parsedData) {
                // This is a source request. It means the teams client is asking for the server to start serving a source from one of the streams
                // that the server provides to the client
                if (item.type === 'sr') {
                    processSourceRequest(item);
                }
            }
        }
    } catch (e) {
        realConsole?.error('handleMainChannelSend: Failed to parse main channel data:', e);
    }
}

const handleVideoTrack = async (event) => {  
    try {
      // Create processor to get raw frames
      const processor = new MediaStreamTrackProcessor({ track: event.track });
      const generator = new MediaStreamTrackGenerator({ kind: 'video' });
      
      // Add track ended listener
      event.track.addEventListener('ended', () => {
          console.log('Video track ended:', event.track.id);
          //videoTrackManager.deleteVideoTrack(event.track);
      });
      
      // Get readable stream of video frames
      const readable = processor.readable;
      const writable = generator.writable;
  
      const firstStreamId = event.streams[0]?.id;

      console.log('firstStreamId', firstStreamId);
  
      // Check if of the users who are in the meeting and screensharers
      // if any of them have an associated device output with the first stream ID of this video track
      /*
      const isScreenShare = userManager
          .getCurrentUsersInMeetingWhoAreScreenSharing()
          .some(user => firstStreamId && userManager.getDeviceOutput(user.deviceId, DEVICE_OUTPUT_TYPE.VIDEO).streamId === firstStreamId);
      if (firstStreamId) {
          videoTrackManager.upsertVideoTrack(event.track, firstStreamId, isScreenShare);
      }
          */
  
      // Add frame rate control variables
      const targetFPS = 24;
      const frameInterval = 1000 / targetFPS; // milliseconds between frames
      let lastFrameTime = 0;
  
      const transformStream = new TransformStream({
          async transform(frame, controller) {
              if (!frame) {
                  return;
              }
  
              try {
                  // Check if controller is still active
                  if (controller.desiredSize === null) {
                      frame.close();
                      return;
                  }
  
                  const currentTime = performance.now();
  
                  // Add SSRC logging 
                  // 
                  /*
                  if (event.track.getSettings) {
                      //console.log('Track settings:', event.track.getSettings());
                  }
                  //console.log('Track ID:', event.track.id);
                 
                  if (event.streams && event.streams[0]) {
                      //console.log('Stream ID:', event.streams[0].id);
                      event.streams[0].getTracks().forEach(track => {
                          if (track.getStats) {
                              track.getStats().then(stats => {
                                  stats.forEach(report => {
                                      if (report.type === 'outbound-rtp' || report.type === 'inbound-rtp') {
                                          console.log('RTP Stats (including SSRC):', report);
                                      }
                                  });
                              });
                          }
                      });
                  }*/
  
                  /*
                  if (Math.random() < 0.00025) {
                    //const participant = virtualStreamToPhysicalStreamMappingManager.physicalServerStreamIdToParticipant(firstStreamId);
                    //realConsole?.log('videoframe from stream id', firstStreamId, ' corresponding to participant', participant);
                    //realConsole?.log('frame', frame);
                    //realConsole?.log('handleVideoTrack, randomsample', event);
                  }
                    */
                 // if (Math.random() < 0.02)
                   //realConsole?.log('firstStreamId', firstStreamId, 'streamIdToSend', virtualStreamToPhysicalStreamMappingManager.getVideoStreamIdToSend());
                  
                  if (firstStreamId && firstStreamId === virtualStreamToPhysicalStreamMappingManager.getVideoStreamIdToSend()) {
                      // Check if enough time has passed since the last frame
                      if (currentTime - lastFrameTime >= frameInterval) {
                          // Copy the frame to get access to raw data
                          const rawFrame = new VideoFrame(frame, {
                              format: 'I420'
                          });
  
                          // Get the raw data from the frame
                          const data = new Uint8Array(rawFrame.allocationSize());
                          rawFrame.copyTo(data);
  
                          /*
                          const currentFormat = {
                              width: frame.displayWidth,
                              height: frame.displayHeight,
                              dataSize: data.length,
                              format: rawFrame.format,
                              duration: frame.duration,
                              colorSpace: frame.colorSpace,
                              codedWidth: frame.codedWidth,
                              codedHeight: frame.codedHeight
                          };
                          */
                          // Get current time in microseconds (multiply milliseconds by 1000)
                          const currentTimeMicros = BigInt(Math.floor(currentTime * 1000));
                          ws.sendVideo(currentTimeMicros, firstStreamId, frame.displayWidth, frame.displayHeight, data);
  
                          rawFrame.close();
                          lastFrameTime = currentTime;
                      }
                  }
                  
                  // Always enqueue the frame for the video element
                  controller.enqueue(frame);
              } catch (error) {
                  realConsole?.error('Error processing frame:', error);
                  frame.close();
              }
          },
          flush() {
              realConsole?.log('Transform stream flush called');
          }
      });
  
      // Create an abort controller for cleanup
      const abortController = new AbortController();
  
      try {
          // Connect the streams
          await readable
              .pipeThrough(transformStream)
              .pipeTo(writable, {
                  signal: abortController.signal
              })
              .catch(error => {
                  if (error.name !== 'AbortError') {
                      realConsole?.error('Pipeline error:', error);
                  }
              });
      } catch (error) {
          realConsole?.error('Stream pipeline error:', error);
          abortController.abort();
      }
  
    } catch (error) {
        realConsole?.error('Error setting up video interceptor:', error);
    }
  };


  const handleAudioTrack = async (event) => {
    let lastAudioFormat = null;  // Track last seen format
    const audioDataQueue = [];
    const ACTIVE_SPEAKER_LATENCY_MS = 2000;
    
    // Start continuous background processing of the audio queue
    const processAudioQueue = () => {
        while (audioDataQueue.length > 0 && 
            Date.now() - audioDataQueue[0].audioArrivalTime >= ACTIVE_SPEAKER_LATENCY_MS) {
            const { audioData, audioArrivalTime } = audioDataQueue.shift();

            // Get the dominant speaker and assume that's who the participant speaking is
            const dominantSpeakerId = dominantSpeakerManager.getLastSpeakerIdForTimestampMs(audioArrivalTime);

            // Send audio data through websocket
            if (dominantSpeakerId) {
                ws.sendPerParticipantAudio(dominantSpeakerId, audioData);
            }
        }
    };

    // Set up background processing every 100ms
    const queueProcessingInterval = setInterval(processAudioQueue, 100);
    
    // Clean up interval when track ends
    event.track.addEventListener('ended', () => {
        clearInterval(queueProcessingInterval);
        console.log('Audio track ended, cleared queue processing interval');
    });
    
    try {
      // Create processor to get raw frames
      const processor = new MediaStreamTrackProcessor({ track: event.track });
      const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
      
      // Get readable stream of audio frames
      const readable = processor.readable;
      const writable = generator.writable;
  
      // Transform stream to intercept frames
      const transformStream = new TransformStream({
          async transform(frame, controller) {
              if (!frame) {
                  return;
              }
  
              try {
                  // Check if controller is still active
                  if (controller.desiredSize === null) {
                      frame.close();
                      return;
                  }
  
                  // Copy the audio data
                  const numChannels = frame.numberOfChannels;
                  const numSamples = frame.numberOfFrames;
                  const audioData = new Float32Array(numSamples);
                  
                  // Copy data from each channel
                  // If multi-channel, average all channels together
                  if (numChannels > 1) {
                      // Temporary buffer to hold each channel's data
                      const channelData = new Float32Array(numSamples);
                      
                      // Sum all channels
                      for (let channel = 0; channel < numChannels; channel++) {
                          frame.copyTo(channelData, { planeIndex: channel });
                          for (let i = 0; i < numSamples; i++) {
                              audioData[i] += channelData[i];
                          }
                      }
                      
                      // Average by dividing by number of channels
                      for (let i = 0; i < numSamples; i++) {
                          audioData[i] /= numChannels;
                      }
                  } else {
                      // If already mono, just copy the data
                      frame.copyTo(audioData, { planeIndex: 0 });
                  }
  
                  // console.log('frame', frame)
                  // console.log('audioData', audioData)
  
                  // Check if audio format has changed
                  const currentFormat = {
                      numberOfChannels: 1,
                      originalNumberOfChannels: frame.numberOfChannels,
                      numberOfFrames: frame.numberOfFrames,
                      sampleRate: frame.sampleRate,
                      format: frame.format,
                      duration: frame.duration
                  };
  
                  // If format is different from last seen format, send update
                  if (!lastAudioFormat || 
                      JSON.stringify(currentFormat) !== JSON.stringify(lastAudioFormat)) {
                      lastAudioFormat = currentFormat;
                      ws.sendJson({
                          type: 'AudioFormatUpdate',
                          format: currentFormat
                      });
                  }
  
                  // If the audioData buffer is all zeros, we still want to send it. It's only one mixed audio stream.
                  // It seems to help with the transcription.
                  //if (audioData.every(value => value === 0)) {
                  //    return;
                  //}

                  // Add to queue with timestamp - the background thread will process it
                  audioDataQueue.push({
                    audioArrivalTime: Date.now(),
                    audioData: audioData
                  });

                  // Pass through the original frame
                  controller.enqueue(frame);
              } catch (error) {
                  console.error('Error processing frame:', error);
                  frame.close();
              }
          },
          flush() {
              console.log('Transform stream flush called');
              // Clear the interval when the stream ends
              clearInterval(queueProcessingInterval);
          }
      });
  
      // Create an abort controller for cleanup
      const abortController = new AbortController();
  
      try {
          // Connect the streams
          await readable
              .pipeThrough(transformStream)
              .pipeTo(writable, {
                  signal: abortController.signal
              })
              .catch(error => {
                  if (error.name !== 'AbortError') {
                      console.error('Pipeline error:', error);
                  }
                  // Clear the interval on error
                  clearInterval(queueProcessingInterval);
              });
      } catch (error) {
          console.error('Stream pipeline error:', error);
          abortController.abort();
          // Clear the interval on error
          clearInterval(queueProcessingInterval);
      }
  
    } catch (error) {
        console.error('Error setting up audio interceptor:', error);
        // Clear the interval on error
        clearInterval(queueProcessingInterval);
    }
  };
  

// LOOK FOR https://api.flightproxy.skype.com/api/v2/cpconv

// LOOK FOR https://teams.live.com/api/chatsvc/consumer/v1/threads?view=msnp24Equivalent&threadIds=19%3Ameeting_Y2U4ZDk5NzgtOWQwYS00YzNjLTg2ODktYmU5MmY2MGEyNzJj%40thread.v2
new RTCInterceptor({
    onPeerConnectionCreate: (peerConnection) => {
        realConsole?.log('New RTCPeerConnection created:', peerConnection);
        peerConnection.addEventListener('datachannel', (event) => {
            realConsole?.log('datachannel', event);
            realConsole?.log('datachannel label', event.channel.label);

            if (event.channel.label === "collections") {               
                event.channel.addEventListener("message", (messageEvent) => {
                    console.log('RAWcollectionsevent', messageEvent);
                    handleCollectionEvent(messageEvent);
                });
            }
        });

        peerConnection.addEventListener('track', (event) => {
            console.log('New track:', {
                trackId: event.track.id,
                trackKind: event.track.kind,
                streams: event.streams,
            });
            // We need to capture every audio track in the meeting,
            // but we don't need to do anything with the video tracks
            if (event.track.kind === 'audio') {
                window.styleManager.addAudioTrack(event.track);
                if (window.initialData.sendPerParticipantAudio) {
                    handleAudioTrack(event);
                }
            }
            if (event.track.kind === 'video') {
                window.styleManager.addVideoTrack(event);
            }
        });

        /*
        We are no longer setting up per-frame MediaStreamTrackProcessor's because it taxes the CPU too much
        For now, we are just using the ScreenAndAudioRecorder to record the video stream
        but we're keeping this code around for reference
        peerConnection.addEventListener('track', (event) => {
            // Log the track and its associated streams

            if (event.track.kind === 'audio') {
                realConsole?.log('got audio track');
                realConsole?.log(event);
                try {
                    handleAudioTrack(event);
                } catch (e) {
                    realConsole?.log('Error handling audio track:', e);
                }
            }
            if (event.track.kind === 'video') {
                realConsole?.log('got video track');
                realConsole?.log(event);
                try {
                    handleVideoTrack(event);
                } catch (e) {
                    realConsole?.log('Error handling video track:', e);
                }
            }
        });
        */

        peerConnection.addEventListener('connectionstatechange', (event) => {
            realConsole?.log('connectionstatechange', event);
        });
        


        // This is called when the browser detects that the SDP has changed
        peerConnection.addEventListener('negotiationneeded', (event) => {
            realConsole?.log('negotiationneeded', event);
        });

        peerConnection.addEventListener('onnegotiationneeded', (event) => {
            realConsole?.log('onnegotiationneeded', event);
        });

        // Log the signaling state changes
        peerConnection.addEventListener('signalingstatechange', () => {
            console.log('Signaling State:', peerConnection.signalingState);
        });

        // Log the SDP being exchanged
        const originalSetLocalDescription = peerConnection.setLocalDescription;
        peerConnection.setLocalDescription = function(description) {
            realConsole?.log('Local SDP:', description);
            return originalSetLocalDescription.apply(this, arguments);
        };

        const originalSetRemoteDescription = peerConnection.setRemoteDescription;
        peerConnection.setRemoteDescription = function(description) {
            realConsole?.log('Remote SDP:', description);
            return originalSetRemoteDescription.apply(this, arguments);
        };

        // Log ICE candidates
        peerConnection.addEventListener('icecandidate', (event) => {
            if (event.candidate) {
                //console.log('ICE Candidate:', event.candidate);
            }
        });
    },
    onDataChannelCreate: (dataChannel, peerConnection) => {
        realConsole?.log('New DataChannel created:', dataChannel);
        realConsole?.log('On PeerConnection:', peerConnection);
        realConsole?.log('Channel label:', dataChannel.label);
        realConsole?.log('Channel keys:', typeof dataChannel);

        //if (dataChannel.label === 'collections') {
          //  dataChannel.addEventListener("message", (event) => {
         //       console.log('collectionsevent', event)
        //    });
        //}


      if (dataChannel.label === 'main-channel') {
        dataChannel.addEventListener("message", (mainChannelEvent) => {
            handleMainChannelEvent(mainChannelEvent);
        });
      }
    },
    onDataChannelSend: ({channel, data, peerConnection}) => {
        if (channel.label === 'main-channel') {
            handleMainChannelSend(data);
        }
        
        
        /*
        realConsole?.log('DataChannel send intercepted:', {
            channelLabel: channel.label,
            data: data,
            readyState: channel.readyState
        });*/

        // It looks like it sends a payload like this:
        /*
            [{"type":"sr","controlVideoStreaming":{"sequenceNumber":11,"controlInfo":{"sourceId":1267,"streamMsid":1694,"fmtParams":[{"max-fs":920,"max-mbps":33750,"max-fps":3000,"profile-level-id":"64001f"}]}}}]

            The streamMsid corresponds to the streamId in the streamIdToSSRCMapping object. We can use it to get the actual stream's id by putting it through the mapping and getting the ssrc.
            The sourceId corresponds to sourceId of the participant that you get from the roster update event.
            Annoyingly complicated, but it seems to work.



        */
    }
});

function addClickRipple() {
    document.addEventListener('click', function(e) {
      const ripple = document.createElement('div');
      
      // Apply styles directly to the element
      ripple.style.position = 'fixed';
      ripple.style.borderRadius = '50%';
      ripple.style.width = '20px';
      ripple.style.height = '20px';
      ripple.style.marginLeft = '-10px';
      ripple.style.marginTop = '-10px';
      ripple.style.background = 'red';
      ripple.style.opacity = '0';
      ripple.style.pointerEvents = 'none';
      ripple.style.transform = 'scale(0)';
      ripple.style.transition = 'transform 0.3s, opacity 0.3s';
      ripple.style.zIndex = '9999999';
      
      ripple.style.left = e.pageX + 'px';
      ripple.style.top = e.pageY + 'px';
      document.body.appendChild(ripple);
  
      // Force reflow so CSS transition will play
      getComputedStyle(ripple).transform;
      
      // Animate
      ripple.style.transform = 'scale(3)';
      ripple.style.opacity = '0.7';
  
      // Remove after animation
      setTimeout(() => {
        ripple.remove();
      }, 300);
    }, true);
}

if (window.initialData.addClickRipple) {
    addClickRipple();
}



function turnOnCamera() {
    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn camera on"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOnMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Unmute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    }
}

function turnOffMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Mute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    }
}

function turnOnMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Unmute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn camera on"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOffMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Mute mic"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn camera off"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it off");
        cameraButton.click();
    } else {
        console.log("Camera off button not found");
    }
}

const _getUserMedia = navigator.mediaDevices.getUserMedia;

class BotOutputManager {
    constructor() {
        
        // For outputting video
        this.botOutputVideoElement = null;
        this.videoSource = null;
        this.botOutputVideoElementCaptureStream = null;

        // For outputting image
        this.botOutputCanvasElement = null;
        this.botOutputCanvasElementCaptureStream = null;
        
        // For outputting audio
        this.audioContextForBotOutput = null;
        this.gainNode = null;
        this.destination = null;
        this.botOutputAudioTrack = null;

        // For outputting a stream
        this.botOutputMediaStream = null;
        this.botOutputPeerConnection = null;
    }

    playMediaStream(stream) {
        if (this.botOutputMediaStream) {
            this.botOutputMediaStream.disconnect();
        }
        this.botOutputMediaStream = stream;

        turnOffMicAndCamera();

        // after 1000 ms
        setTimeout(() => {
            turnOnMicAndCamera();
        }, 1000);
    }

    async getBotOutputPeerConnectionOffer() {
        try
        {
            // 2) Create the RTCPeerConnection
            this.botOutputPeerConnection = new RTCPeerConnection();

            // 3) Receive the server's *video* and *audio*
            const ms = new MediaStream();
            this.botOutputPeerConnection.ontrack = (ev) => {
                ms.addTrack(ev.track);
                // If we've received both video and audio, play the stream
                if (ms.getVideoTracks().length > 0 && ms.getAudioTracks().length > 0) {
                    botOutputManager.playMediaStream(ms);
                }
            };

            // We still want to receive the server's video
            this.botOutputPeerConnection.addTransceiver('video', { direction: 'recvonly' });

            // ❗ Instead of recvonly audio, we now **send** our mic upstream:
            const meetingAudioStream = window.styleManager.getMeetingAudioStream();
            for (const track of meetingAudioStream.getAudioTracks()) {
                this.botOutputPeerConnection.addTrack(track, meetingAudioStream);
            }

            // Create/POST offer → set remote answer
            const offer = await this.botOutputPeerConnection.createOffer();
            await this.botOutputPeerConnection.setLocalDescription(offer);
            return { sdp: this.botOutputPeerConnection.localDescription.sdp, type: this.botOutputPeerConnection.localDescription.type };
        }
        catch (e) {
            return { error: e.message };
        }
    }

    async startBotOutputPeerConnection(offerResponse) {
        await this.botOutputPeerConnection.setRemoteDescription(offerResponse);

        // Start latency measurement for the bot output peer connection
        this.startLatencyMeter(this.botOutputPeerConnection, "bot-output");
    }

    startLatencyMeter(pc, label="rx") {
        setInterval(async () => {
            const stats = await pc.getStats();
            let rtt_ms = 0, jb_a_ms = 0, jb_v_ms = 0, dec_v_ms = 0;

            stats.forEach(r => {
                if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.nominated) {
                    rtt_ms = (r.currentRoundTripTime || 0) * 1000;
                }
                if (r.type === 'inbound-rtp' && r.kind === 'audio') {
                    const d = (r.jitterBufferDelay || 0);
                    const n = (r.jitterBufferEmittedCount || 1);
                    jb_a_ms = (d / n) * 1000;
                }
                if (r.type === 'inbound-rtp' && r.kind === 'video') {
                    const d = (r.jitterBufferDelay || 0);
                    const n = (r.jitterBufferEmittedCount || 1);
                    jb_v_ms = (d / n) * 1000;
                    dec_v_ms = ((r.totalDecodeTime || 0) / (r.framesDecoded || 1)) * 1000;
                }
            });

            const est_audio_owd = (rtt_ms / 2) + jb_a_ms;
            const est_video_owd = (rtt_ms / 2) + jb_v_ms + dec_v_ms;

            const logStatement = `[${label}] est one-way: audio≈${est_audio_owd|0}ms, video≈${est_video_owd|0}ms  (rtt=${rtt_ms|0}, jb_a=${jb_a_ms|0}, jb_v=${jb_v_ms|0}, dec_v=${dec_v_ms|0})`;
            console.log(logStatement);
            window.ws.sendJson({
                type: 'BOT_OUTPUT_PEER_CONNECTION_STATS',
                logStatement: logStatement
            });
        }, 60000);
    }

    displayImage(imageBytes) {
        try {
            // Wait for the image to be loaded onto the canvas
            return this.writeImageToBotOutputCanvas(imageBytes)
                .then(() => {
                // If the stream is already broadcasting, don't do anything
                if (this.botOutputCanvasElementCaptureStream)
                {
                    console.log("Stream already broadcasting, skipping");
                    return;
                }

                // Now that the image is loaded, capture the stream and turn on camera
                this.botOutputCanvasElementCaptureStream = this.botOutputCanvasElement.captureStream(1);
                // Wait for 3 seconds before turning on camera, this is necessary for teams only
                setTimeout(turnOnCamera, 3000);
            })
            .catch(error => {
                console.error('Error in botOutputManager.displayImage:', error);
            });
        } catch (error) {
            console.error('Error in botOutputManager.displayImage:', error);
        }
    }

    writeImageToBotOutputCanvas(imageBytes) {
        if (!this.botOutputCanvasElement) {
            // Create a new canvas element with fixed dimensions
            this.botOutputCanvasElement = document.createElement('canvas');
            this.botOutputCanvasElement.width = 1280; // Fixed width
            this.botOutputCanvasElement.height = 640; // Fixed height
        }
        
        return new Promise((resolve, reject) => {
            // Create an Image object to load the PNG
            const img = new Image();
            
            // Convert the image bytes to a data URL
            const blob = new Blob([imageBytes], { type: 'image/png' });
            const url = URL.createObjectURL(blob);
            
            // Draw the image on the canvas when it loads
            img.onload = () => {
                // Revoke the URL immediately after image is loaded
                URL.revokeObjectURL(url);
                
                const canvas = this.botOutputCanvasElement;
                const ctx = canvas.getContext('2d');
                
                // Clear the canvas
                ctx.fillStyle = 'black';
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                
                // Calculate aspect ratios
                const imgAspect = img.width / img.height;
                const canvasAspect = canvas.width / canvas.height;
                
                // Calculate dimensions to fit image within canvas with letterboxing
                let renderWidth, renderHeight, offsetX, offsetY;
                
                if (imgAspect > canvasAspect) {
                    // Image is wider than canvas (horizontal letterboxing)
                    renderWidth = canvas.width;
                    renderHeight = canvas.width / imgAspect;
                    offsetX = 0;
                    offsetY = (canvas.height - renderHeight) / 2;
                } else {
                    // Image is taller than canvas (vertical letterboxing)
                    renderHeight = canvas.height;
                    renderWidth = canvas.height * imgAspect;
                    offsetX = (canvas.width - renderWidth) / 2;
                    offsetY = 0;
                }
                
                this.imageDrawParams = {
                    img: img,
                    offsetX: offsetX,
                    offsetY: offsetY,
                    width: renderWidth,
                    height: renderHeight
                };

                // Clear any existing draw interval
                if (this.drawInterval) {
                    clearInterval(this.drawInterval);
                }

                ctx.drawImage(
                    this.imageDrawParams.img,
                    this.imageDrawParams.offsetX,
                    this.imageDrawParams.offsetY,
                    this.imageDrawParams.width,
                    this.imageDrawParams.height
                );

                // Set up interval to redraw the image every 1 second
                this.drawInterval = setInterval(() => {
                    ctx.drawImage(
                        this.imageDrawParams.img,
                        this.imageDrawParams.offsetX,
                        this.imageDrawParams.offsetY,
                        this.imageDrawParams.width,
                        this.imageDrawParams.height
                    );
                }, 1000);
                
                // Resolve the promise now that image is loaded
                resolve();
            };
            
            // Handle image loading errors
            img.onerror = (error) => {
                URL.revokeObjectURL(url);
                reject(new Error('Failed to load image'));
            };
            
            // Set the image source to start loading
            img.src = url;
        });
    }


    initializeBotOutputAudioTrack() {
        if (this.botOutputAudioTrack) {
            return;
        }

        // Create AudioContext and nodes
        this.audioContextForBotOutput = new AudioContext();
        this.gainNode = this.audioContextForBotOutput.createGain();
        this.destination = this.audioContextForBotOutput.createMediaStreamDestination();

        // Set initial gain
        this.gainNode.gain.value = 1.0;

        // Connect gain node to both destinations
        this.gainNode.connect(this.destination);
        this.gainNode.connect(this.audioContextForBotOutput.destination);  // For local monitoring

        this.botOutputAudioTrack = this.destination.stream.getAudioTracks()[0];
        
        // Initialize audio queue for continuous playback
        this.audioQueue = [];
        this.nextPlayTime = 0;
        this.isPlaying = false;
        this.sampleRate = 44100; // Default sample rate
        this.numChannels = 1;    // Default channels
        this.turnOffMicTimeout = null;
    }

    playPCMAudio(pcmData, sampleRate = 44100, numChannels = 1) {
        turnOnMic();

        // Make sure audio context is initialized
        this.initializeBotOutputAudioTrack();
        
        // Update properties if they've changed
        if (this.sampleRate !== sampleRate || this.numChannels !== numChannels) {
            this.sampleRate = sampleRate;
            this.numChannels = numChannels;
        }
        
        // Convert Int16 PCM data to Float32 with proper scaling
        let audioData;
        if (pcmData instanceof Float32Array) {
            audioData = pcmData;
        } else {
            // Create a Float32Array of the same length
            audioData = new Float32Array(pcmData.length);
            // Scale Int16 values (-32768 to 32767) to Float32 range (-1.0 to 1.0)
            for (let i = 0; i < pcmData.length; i++) {
                // Division by 32768.0 scales the range correctly
                audioData[i] = pcmData[i] / 32768.0;
            }
        }
        
        // Add to queue with timing information
        const chunk = {
            data: audioData,
            duration: audioData.length / (numChannels * sampleRate)
        };
        
        this.audioQueue.push(chunk);
        
        // Start playing if not already
        if (!this.isPlaying) {
            this.processAudioQueue();
        }
    }
    
    processAudioQueue() {
        if (this.audioQueue.length === 0) {
            this.isPlaying = false;

            if (this.turnOffMicTimeout) {
                clearTimeout(this.turnOffMicTimeout);
                this.turnOffMicTimeout = null;
            }
            
            // Delay turning off the mic by 2 second and check if queue is still empty
            this.turnOffMicTimeout = setTimeout(() => {
                // Only turn off mic if the queue is still empty
                if (this.audioQueue.length === 0)
                    turnOffMic();
            }, 2000);
            
            return;
        }
        
        this.isPlaying = true;
        
        // Get current time and next play time
        const currentTime = this.audioContextForBotOutput.currentTime;
        this.nextPlayTime = Math.max(currentTime, this.nextPlayTime);
        
        // Get next chunk
        const chunk = this.audioQueue.shift();
        
        // Create buffer for this chunk
        const audioBuffer = this.audioContextForBotOutput.createBuffer(
            this.numChannels,
            chunk.data.length / this.numChannels,
            this.sampleRate
        );
        
        // Fill the buffer
        if (this.numChannels === 1) {
            const channelData = audioBuffer.getChannelData(0);
            channelData.set(chunk.data);
        } else {
            for (let channel = 0; channel < this.numChannels; channel++) {
                const channelData = audioBuffer.getChannelData(channel);
                for (let i = 0; i < chunk.data.length / this.numChannels; i++) {
                    channelData[i] = chunk.data[i * this.numChannels + channel];
                }
            }
        }
        
        // Create source and schedule it
        const source = this.audioContextForBotOutput.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.gainNode);
        
        // Schedule precisely
        source.start(this.nextPlayTime);
        this.nextPlayTime += chunk.duration;
        
        // Schedule the next chunk processing
        const timeUntilNextProcess = (this.nextPlayTime - currentTime) * 1000 * 0.8;
        setTimeout(() => this.processAudioQueue(), Math.max(0, timeUntilNextProcess));
    }

}

const botOutputManager = new BotOutputManager();
window.botOutputManager = botOutputManager;

navigator.mediaDevices.getUserMedia = function(constraints) {
    return _getUserMedia.call(navigator.mediaDevices, constraints)
      .then(originalStream => {
        realConsole?.log("Intercepted getUserMedia:", constraints);
  
        // Stop any original tracks so we don't actually capture real mic/cam
        originalStream.getTracks().forEach(t => t.stop());
  
        // Create a new MediaStream to return
        const newStream = new MediaStream();
  
        // Video sending not supported yet
        /* 
        if (constraints.video && botOutputVideoElementCaptureStream) {
            console.log("Adding video track", botOutputVideoElementCaptureStream.getVideoTracks()[0]);
            newStream.addTrack(botOutputVideoElementCaptureStream.getVideoTracks()[0]);
        }
        */
        if (constraints.video && botOutputManager.botOutputMediaStream) {
            console.log("Adding botOutputMediaStream", botOutputManager.botOutputMediaStream.getVideoTracks()[0]);
            newStream.addTrack(botOutputManager.botOutputMediaStream.getVideoTracks()[0]);
        }

        if (constraints.video && botOutputManager.botOutputCanvasElementCaptureStream) {
            realConsole?.log("Adding canvas track", botOutputManager.botOutputCanvasElementCaptureStream.getVideoTracks()[0]);
            newStream.addTrack(botOutputManager.botOutputCanvasElementCaptureStream.getVideoTracks()[0]);
        }

        if (constraints.audio) {  // Only create once
            botOutputManager.initializeBotOutputAudioTrack();
            newStream.addTrack(botOutputManager.botOutputAudioTrack);
        }  
  
        if (botOutputManager.botOutputMediaStream) {
            // connect the botOutputMediaStream stream to the audio context
            if (botOutputManager.botOutputMediaStream.getAudioTracks().length > 0) {
                botOutputManager.initializeBotOutputAudioTrack();
                const botOutputMediaStreamSource = botOutputManager.audioContextForBotOutput.createMediaStreamSource(botOutputManager.botOutputMediaStream);
                botOutputMediaStreamSource.connect(botOutputManager.gainNode);
                console.log("Connected botOutputMediaStream audio track to audio context");
            }
        }

        return newStream;
      })
      .catch(err => {
        console.error("Error in custom getUserMedia override:", err);
        throw err;
      });
  };

(function () {
    const _bind = Function.prototype.bind;
    Function.prototype.bind = function (thisArg, ...args) {
      if (this.name === 'onMessageReceived') {
        const bound = _bind.apply(this, [thisArg, ...args]);
        return function (...callArgs) {
          const eventData = callArgs[0];
          if (eventData?.data?.chatServiceBatchEvent?.[0]?.message)
          {
            const message = eventData.data.chatServiceBatchEvent[0].message;
            realConsole?.log('chatMessage', message);
            window.chatMessageManager?.handleChatMessage(message);
          }
          return bound.apply(this, callArgs);
        };
      }
      return _bind.apply(this, [thisArg, ...args]);
    };
  })();

class CallManager {
    constructor() {
        this.activeCall = null;
    }

    setActiveCall() {
        if (this.activeCall) {
            return;
        }

        if (window.callingDebug?.observableCall) {
            this.activeCall = window.callingDebug.observableCall;
        }

        if (window.msteamscalling?.deref)
        {
            const microsoftCalling = window.msteamscalling.deref();
            if (microsoftCalling?.callingService?.getActiveCall) {
                const call = microsoftCalling.callingService.getActiveCall();
                if (call) {
                    this.activeCall = call;
                }
            }
        }
    }

    getCurrentUserId() {
        this.setActiveCall();
        if (!this.activeCall) {
            return;
        }

        return this.activeCall.callerMri;
        // We're using callerMri because it includes the 8: prefix. If callerMri stops working, we can easily use the thing below.
        // return this.activeCall.currentUserSkypeIdentity?.id;
    }

    syncParticipants() {
        this.setActiveCall();
        if (!this.activeCall) {
            return;
        }

        const participantsRaw = this.activeCall.participants;
        const participants = participantsRaw.map(participant => {
            return {
                id: participant.id,
                displayName: participant.displayName,
                endpoints: participant.endpoints,
                meetingRole: participant.meetingRole
            };
        }).filter(participant => participant.displayName);

        for (const participant of participants) {
            const endpoints = (participant?.endpoints?.endpointDetails || []).map(endpoint => {
                if (!endpoint.endpointId) {
                    return null;
                }

                if (!endpoint.mediaStreams) {
                    return null;
                }

                return [
                    endpoint.endpointId,
                    {
                        call: {
                            mediaStreams: endpoint.mediaStreams
                        }
                    }
                ]
            }).filter(endpoint => endpoint);

            // Transform this funny format of a participant into Teams "standard" format
            const participantConverted = {
                details: {id: participant.id, displayName: participant.displayName},
                meetingRole: participant.meetingRole,
                state: "active",
                endpoints: Object.fromEntries(endpoints)
            };
            window.userManager.singleUserSynced(participantConverted);
            syncVirtualStreamsFromParticipant(participantConverted);
        }
    }

    enableClosedCaptions() {
        this.setActiveCall();
        if (this.activeCall) {
            this.activeCall.startClosedCaption();
            return true;
        }
        return false;
    }

    setClosedCaptionsLanguage(language) {
        this.setActiveCall();
        if (this.activeCall) {
            this.activeCall.setClosedCaptionsLanguage(language);
            // Unfortunately, this is needed for improved reliability.
            // It seems like when the host joins at the same time as the bot, they reset the cc language to the default.
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(language);
                }
            }, 1000);         
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(language);
                }
            }, 3000);
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(language);
                }
            }, 5000);
            setTimeout(() => {
                if (this.activeCall) {
                    this.activeCall.setClosedCaptionsLanguage(language);
                    // Set an interval that runs every 60 seconds and makes sure the current closed caption language is equal to the language
                    // This is for debugging purposes
                    setInterval(() => {
                        if (this.activeCall && this.activeCall.getClosedCaptionsLanguage) {
                            if (this.activeCall.getClosedCaptionsLanguage() !== language) {
                                window.ws?.sendJson({
                                    type: "closedCaptionsLanguageMismatch",
                                    desiredLanguage: language,
                                    currentLanguage: this.activeCall.getClosedCaptionsLanguage()
                                });
                            }
                        }
                    }, 60000);
                }
            }, 10000);
            return true;
        }
        return false;
    }
}

const callManager = new CallManager();
window.callManager = callManager;