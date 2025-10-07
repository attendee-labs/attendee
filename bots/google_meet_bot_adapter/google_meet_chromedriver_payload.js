function sendChatMessage(text) {
    
    // First try to find the chat input textarea
    let chatInput = document.querySelector('textarea[aria-label="Send a message"]');
    
    if (!chatInput) {
        console.error('Chat input not found');
        return false;
    }
    
    // Type the message
    chatInput.focus();
    chatInput.value = text;
    
    // Trigger input event to ensure the UI updates
    chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    
    // Send Enter keypress to submit the message
    const enterEvent = new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true
    });
    chatInput.dispatchEvent(enterEvent);
    
    return true;
}

class StyleManager {
    constructor() {
        this.videoTrackIdToSSRC = new Map();
        this.videoElementToCaptureCanvasElements = new Map();
        this.mainElement = null;

        this.audioContext = null;
        this.audioTracks = [];
        this.silenceThreshold = 0.5;
        this.silenceCheckInterval = null;
        this.memoryUsageCheckInterval = null;
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
                volume: averageDeviation,
                isSilent: false
            });
        }
    }

    checkMemoryUsage() {
        // Useful for debugging memory usage
        window.ws.sendJson({
            type: 'MemoryUsage',
            memoryUsage: {
                jsHeapSizeLimit: performance.memory?.jsHeapSizeLimit,
                totalJSHeapSize: performance.memory?.totalJSHeapSize,
                usedJSHeapSize: performance.memory?.usedJSHeapSize
            }
        });
    }

    checkNeededInteractions() {
        // Check for recording notification dialog
        const recordingDialog = document.querySelector('div[aria-modal="true"][role="dialog"]');
        
        if (recordingDialog && recordingDialog.textContent.includes('This video call is being recorded')) {           
            // Find and click the "Join now" button (usually the confirm/OK button)
            const joinNowButton = recordingDialog.querySelector('button[data-mdc-dialog-action="ok"]');
            
            if (joinNowButton) {
                try {
                    joinNowButton.click();
                    window.ws.sendJson({
                        type: 'UiInteraction',
                        message: 'Automatically accepted recording notification'
                    });
                } catch (error) {
                    window.ws.sendJson({
                        type: 'Error',
                        message: 'Error clicking button to accept recording notification'
                    });
                }                
            } else {                
                window.ws.sendJson({
                    type: 'Error',
                    message: 'Found recording dialog but could not find button to accept recording notification'
                });
            }
        }

        // Check if bot has been removed from the meeting
        const removedFromMeetingElement = document.querySelector('.roSPhc');
        if (removedFromMeetingElement && (removedFromMeetingElement.textContent.includes('You\'ve been removed from the meeting') || removedFromMeetingElement.textContent.includes('Your host ended the meeting for everyone'))) {
            window.ws.sendJson({
                type: 'MeetingStatusChange',
                change: 'removed_from_meeting'
            });
        }
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
         this.analyser.fftSize = 8192;
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

        if (this.memoryUsageCheckInterval) {
            clearInterval(this.memoryUsageCheckInterval);
        }

        if (this.neededInteractionsInterval) {
            clearInterval(this.neededInteractionsInterval);
        }
                
        // Check for audio activity every second
        this.silenceCheckInterval = setInterval(() => {
            this.checkAudioActivity();
        }, 1000);

        // Check for memory usage every 60 seconds
        this.memoryUsageCheckInterval = setInterval(() => {
            this.checkMemoryUsage();
        }, 60000);

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

    stop() {
        this.showAllOfGMeetUI();
    }

    async hideBotVideoElement() {
        const numAttempts = 10; // 1 second / 100ms = 10 attempts
        
        if (window.userManager.getCurrentUsersInMeeting().length > 1)
        {
            const deviceIdOfBot = window.userManager.currentUserId;
            if (!deviceIdOfBot)
            {
                window.ws.sendJson({
                    type: 'Error',
                    message: 'In hideBotVideoElement, window.userManager.currentUserId was null.'
                });
                return;
            }
            const botVideoElement = document.querySelector(`[data-participant-id="${deviceIdOfBot}"]`);
            if (!botVideoElement)
            {
                window.ws.sendJson({
                    type: 'Error',
                    message: 'In hideBotVideoElement, no bot video element found.'
                });
                return;
            }
            const botOtherOptionsButton = botVideoElement.querySelector('button[aria-label="More options"]');
            if (!botOtherOptionsButton)
            {
                window.ws.sendJson({
                    type: 'Error',
                    message: 'In hideBotVideoElement, no bot other options button found.'
                });
                return;
            }

            botOtherOptionsButton.click();
            
            // Wait for minimize button to appear with polling
            let botMinimizeButton = null;
            for (let i = 0; i < numAttempts; i++) {
                await new Promise(resolve => setTimeout(resolve, 100));
                botMinimizeButton = document.querySelector('li[aria-label="Minimize"]');
                if (botMinimizeButton) {
                    break;
                }
                const wasLastAttempt = i === numAttempts - 1;
                if (wasLastAttempt) {
                    window.ws.sendJson({
                        type: 'Error',
                        message: 'In hideBotVideoElement, no bot minimize button found after polling.'
                    });
                    return;
                }
            }
            
            botMinimizeButton.click();
        }
        else
        {
            window.ws.sendJson({
                type: 'Error',
                message: 'In hideBotVideoElement, no other users in meeting. So unable to minimize bot video element.'
            });
        }


        // Wait for minimized element to appear with polling
        let botMinimizedElement = null;
        for (let i = 0; i < numAttempts; i++) {
            await new Promise(resolve => setTimeout(resolve, 100));
            botMinimizedElement = document.querySelector('div[jsname="Qiayqc"]');
            if (botMinimizedElement) {
                break;
            }
            const wasLastAttempt = i === numAttempts - 1;
            if (wasLastAttempt) {
                window.ws.sendJson({
                    type: 'Error',
                    message: 'In hideBotVideoElement, no bot minimized element found after polling.'
                });
                return;
            }
        }
        
        botMinimizedElement.style.display = 'none';
    }

    showAllOfGMeetUI() {
        // Restore all elements that were hidden by onlyShowSubsetofGMeetUI
        document.querySelectorAll('body *').forEach(element => {
            if (element.style.display === 'none') {
                // Only reset display property if we set it to 'none'
                // We can check if the element is a direct child of body or not in main/ancestors
                const isInMainTree = this.mainElement && 
                    (this.mainElement === element || 
                     this.mainElement.contains(element) || 
                     element.contains(this.mainElement));
                
                if (!isInMainTree) {
                    // Reset the display property to its default or empty string
                    // This will restore the element's original display value
                    element.style.display = '';
                }
            }
        });
        
        console.log('Restored all hidden elements to their original display values');
    }

    async onlyShowSubsetofGMeetUI() {
        try {
            // Find the main element that contains all the video elements
            this.mainElement = document.querySelector('main');
            if (!this.mainElement) {
                console.error('No <main> element found in the DOM');
                window.ws.sendJson({
                    type: 'Error',
                    message: 'No <main> element found in the DOM'
                });
                return;
            }

            const ancestors = [];
            let parent = this.mainElement.parentElement;
            while (parent) {
                ancestors.push(parent);
                parent = parent.parentElement;
            }
            
            // Hide all elements except main, its ancestors, and its descendants
            document.querySelectorAll('body *').forEach(element => {
                if (element !== this.mainElement && 
                    !ancestors.includes(element) && 
                    !this.mainElement.contains(element)) {
                    element.style.display = 'none';
                }
            });

            await this.hideBotVideoElement();
        } catch (error) {
            console.error('Error in onlyShowSubsetofGMeetUI:', error);
            window.ws.sendJson({
                type: 'Error',
                message: 'Error in onlyShowSubsetofGMeetUI: ' + error.message
            });
        }
    }

    unpinPinnedVideos() {
        const participantList = document.querySelector('div[aria-label="Participants"][role="list"]');
        // console.log('participantList', participantList);
        if (!participantList) {
            return;
        }

        const participantListItems = participantList.querySelectorAll('div[role="listitem"]');
        // console.log('participantListItems', participantListItems);
        for (const participantListItem of participantListItems) {
            // Look for the pinned icon directly using aria-hidden and checking text content in one step
            const pinnedElements = Array.from(participantListItem.querySelectorAll('i[aria-hidden="true"]'))
                .filter(el => el.textContent === 'keep');
            
            if (pinnedElements.length === 0) {
                continue;
            }

            const actionsButton = participantListItem.querySelector('button[aria-label="More actions"]');
            // console.log('actionsButton', actionsButton);
            if (!actionsButton) {
                continue;
            }

            actionsButton.click();
            setTimeout(() => {
                // Updated selector to find the unpin menu item by looking for the aria-label containing "Unpin"
                const unpinButton = document.querySelector('li[aria-label*="Unpin"][role="menuitem"]');
                // console.log('unpinButton', unpinButton);
                if (!unpinButton) {
                    return;
                }
                // Add the missing click action
                unpinButton.click();

                window.ws.sendJson({
                    type: 'UiInteraction',
                    message: 'Unpinned pinned video',
                    participantListItemDom: participantListItem.outerHTML
                });
            }, 200);
        }
    }

    async openParticipantList() {
        const peopleButton = document.querySelector('button[aria-label="People"]');
        if (peopleButton) {
            // Initially the participant list element does not exist. Clicking this button opens it up.
            peopleButton.click();

            const numAttempts = 30;
            for (let i = 0; i < numAttempts; i++) {
                // Sleep for 250 milliseconds
                await new Promise(resolve => setTimeout(resolve, 100));
                const participantList = document.querySelector('div[aria-label="Participants"][role="list"]');
                if (participantList) {
                    break;
                }
                const wasLastAttempt = i === numAttempts - 1;
                if (wasLastAttempt) {
                    console.log('Failed to find participant list after', numAttempts, 'attempts');
                    window.ws.sendJson({
                        type: 'Error',
                        message: 'Failed to open participant list in openParticipantList'
                    });
                }
            }

            // We need to click this again so that the participant list is minimized.
            peopleButton.click();
        }
    }

    async openChatPanel() {
        const chatButton = document.querySelector('button[aria-label="Chat with everyone"]');
        if (chatButton) {
            // Click to open the chat panel
            chatButton.click();

            // Wait for the chat input element to appear
            const numAttempts = 30;
            for (let i = 0; i < numAttempts; i++) {
                // Sleep for 100 milliseconds
                await new Promise(resolve => setTimeout(resolve, 100));
                const chatInput = document.querySelector('textarea[aria-label="Send a message"]');
                if (chatInput) {
                    break;
                }
                const wasLastAttempt = i === numAttempts - 1;
                if (wasLastAttempt) {
                    console.log('Failed to find chat input after', numAttempts, 'attempts');
                    window.ws.sendJson({
                        type: 'Error',
                        message: 'Failed to find chat input in openChatPanel'
                    });
                    return;
                }
            }

            // Click the chat button again to close/minimize the panel
            chatButton.click();

            window.ws.sendJson({
                type: 'ChatStatusChange',
                change: 'ready_to_send'
            });
        }
    }

    async start() {
        if (window.initialData.recordingView === 'gallery_view')
        {
            await this.openParticipantList();

            // Sleep for half a second to let the panel close, so that opening the chat panel will work
            await new Promise(resolve => setTimeout(resolve, 500));
        }

        await this.openChatPanel();

        await this.onlyShowSubsetofGMeetUI();
        

        if (window.initialData.recordingView === 'gallery_view')
        {
            this.unpinInterval = setInterval(() => {
                this.unpinPinnedVideos();
            }, 1000);
        }

        // Add keyboard listener for toggling canvas visibility
        //document.addEventListener('keydown', this.handleKeyDown.bind(this));

        this.startSilenceDetection();

        console.log('Started StyleManager');
    }

    /*
    handleKeyDown(event) {
        // Toggle canvas visibility when 's' key is pressed
        if (event.key === 's') {
        }
    }*/


    addVideoTrack(trackEvent) {
        const firstStreamId = trackEvent.streams[0]?.id;
        const trackId = trackEvent.track?.id;

        this.videoTrackIdToSSRC.set(trackId, firstStreamId);
    }
}

// Video track manager
class VideoTrackManager {
    constructor(ws) {
        this.videoTracks = new Map();
        this.ws = ws;
        this.trackToSendCache = null;
    }

    deleteVideoTrack(videoTrack) {
        this.videoTracks.delete(videoTrack.id);
        this.trackToSendCache = null;
    }

    upsertVideoTrack(videoTrack, streamId, isScreenShare) {
        const existingVideoTrack = this.videoTracks.get(videoTrack.id);

        // Create new object with track info and firstSeenAt timestamp
        const trackInfo = {
            originalTrack: videoTrack,
            isScreenShare: isScreenShare,
            firstSeenAt: existingVideoTrack ? existingVideoTrack.firstSeenAt : Date.now(),
            streamId: streamId
        };
 
        console.log('upsertVideoTrack for', videoTrack.id, '=', trackInfo);
        
        this.videoTracks.set(videoTrack.id, trackInfo);
        this.trackToSendCache = null;
    }

    getStreamIdToSendCached() {
        return this.getTrackToSendCached()?.streamId;
    }

    getTrackToSendCached() {
        if (this.trackToSendCache) {
            return this.trackToSendCache;
        }

        this.trackToSendCache = this.getTrackToSend();
        return this.trackToSendCache;
    }

    getTrackToSend() {
        const screenShareTracks = Array.from(this.videoTracks.values()).filter(track => track.isScreenShare);
        const mostRecentlyCreatedScreenShareTrack = screenShareTracks.reduce((max, track) => {
            return track.firstSeenAt > max.firstSeenAt ? track : max;
        }, screenShareTracks[0]);

        if (mostRecentlyCreatedScreenShareTrack) {
            return mostRecentlyCreatedScreenShareTrack;
        }

        const nonScreenShareTracks = Array.from(this.videoTracks.values()).filter(track => !track.isScreenShare);
        const mostRecentlyCreatedNonScreenShareTrack = nonScreenShareTracks.reduce((max, track) => {
            return track.firstSeenAt > max.firstSeenAt ? track : max;
        }, nonScreenShareTracks[0]);

        if (mostRecentlyCreatedNonScreenShareTrack) {
            return mostRecentlyCreatedNonScreenShareTrack;
        }

        return null;
    }
}

// Caption manager
class CaptionManager {
    constructor(ws) {
        this.captions = new Map();
        this.ws = ws;
    }

    singleCaptionSynced(caption) {
        this.captions.set(caption.captionId, caption);
        this.ws.sendClosedCaptionUpdate(caption);
    }
}

const DEVICE_OUTPUT_TYPE = {
    AUDIO: 1,
    VIDEO: 2
}

// Chat message manager
class ChatMessageManager {
    constructor(ws) {
        this.ws = ws;
    }

    handleChatMessage(chatMessageRaw) {
        try {
            const chatMessage = chatMessageRaw.chatMessage;
            console.log('handleChatMessage', chatMessage);

            this.ws.sendJson({
                type: 'ChatMessage',
                message_uuid: chatMessage.messageId,
                participant_uuid: chatMessage.deviceId,
                timestamp: Math.floor(chatMessage.timestamp / 1000),
                text: chatMessage.chatMessageContent.text,
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
        this.currentUserId = null;

        this.ws = ws;
    }

    deviceForStreamIsActive(streamId) {
        for(const deviceOutput of this.deviceOutputMap.values()) {
            if (deviceOutput.streamId === streamId) {
                return !deviceOutput.disabled;
            }
        }

        return false;
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

    getUserByStreamId(streamId) {
        // Look through device output map and find the corresponding device id. Then look up the user by device id.
        for (const deviceOutput of this.deviceOutputMap.values()) {
            if (deviceOutput.streamId === streamId) {
                return this.allUsersMap.get(deviceOutput.deviceId);
            }
        }

        return null;
    }

    getUserByDeviceId(deviceId) {
        return this.allUsersMap.get(deviceId);
    }

    getUserByFullName(fullName) {
        return Array.from(this.allUsersMap.values()).find(user => user.fullName === fullName);
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

    singleUserSynced(user) {
      // Create array with new user and existing users, then filter for unique deviceIds
      // keeping the first occurrence (new user takes precedence)
      const allUsers = [...this.currentUsersMap.values(), user];
      const uniqueUsers = Array.from(
        new Map(allUsers.map(user => [user.deviceId, user])).values()
      );
      this.newUsersListSynced(uniqueUsers);
    }
    
    newUsersListSynced(newUsersListRaw) {
        const newUsersList = newUsersListRaw.map(user => {
            const userStatusMap = {
                1: 'in_meeting',
                6: 'not_in_meeting',
                7: 'removed_from_meeting'
            }

            const { isCurrentUserString, ...userWithoutIsCurrentUserString } = user;
            if (isCurrentUserString && this.currentUserId === null) {
                this.currentUserId = user.deviceId;
            }

            return {
                ...userWithoutIsCurrentUserString,
                humanized_status: userStatusMap[user.status] || "unknown",
                isCurrentUser: user.deviceId === this.currentUserId,
                isHost: !!user.isHost
            }
        })
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

class ReceiverManager {
    constructor() {
        this.receiverMap = new Map();
    }

    updateContributingSources(receiver, result) {
        this.receiverMap.set(receiver, result);
    }

    getContributingSources(receiver) {
        return this.receiverMap.get(receiver) || [];
    }
}

class RTCRtpReceiverInterceptor {
    constructor(onGetContributingSources) {
        // Store the original getContributingSources method
        const originalGetContributingSources = RTCRtpReceiver.prototype.getContributingSources;
        
        // Replace with our intercepted version
        RTCRtpReceiver.prototype.getContributingSources = function() {
            // Call the original method with proper this binding and arguments
            const result = originalGetContributingSources.apply(this, arguments);
            
            // Call the callback with the receiver, arguments, and the result
            if (onGetContributingSources) {
                onGetContributingSources(this, result, ...arguments);
            }
            
            // Return the original result
            return result;
        };
    }
}

// Websocket client
class WebSocketClient {
  // Message types
  static MESSAGE_TYPES = {
      JSON: 1,
      VIDEO: 2,
      AUDIO: 3,
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
      this.fillerFrameInterval = null;

      this.lastVideoFrame = this.getBlackFrame();
      this.blackVideoFrame = this.getBlackFrame();
      */
  }

  /*
  We no longer need this because we're not using MediaStreamTrackProcessor's
  getBlackFrame() {
    // Create black frame data (I420 format)
    const width = window.initialData.videoFrameWidth, height = window.initialData.videoFrameHeight;
    const yPlaneSize = width * height;
    const uvPlaneSize = (width * height) / 4;

    const frameData = new Uint8Array(yPlaneSize + 2 * uvPlaneSize);
    // Y plane (black = 0)
    frameData.fill(0, 0, yPlaneSize);
    // U and V planes (black = 128)
    frameData.fill(128, yPlaneSize);

    return {width, height, frameData};
  }

  currentVideoStreamIsActive() {
    const result = window.userManager?.deviceForStreamIsActive(window.videoTrackManager?.getStreamIdToSendCached());

    // This avoids a situation where we transition from no video stream to video stream and we send a filler frame from the
    // last time we had a video stream and it's not the same as the current video stream.
    if (!result)
        this.lastVideoFrame = this.blackVideoFrame;

    return result;
  }

  startFillerFrameTimer() {
    if (this.fillerFrameInterval) return; // Don't start if already running
    
    this.fillerFrameInterval = setInterval(() => {
        try {
            const currentTime = performance.now();
            if (currentTime - this.lastVideoFrameTime >= 500 && this.mediaSendingEnabled) {                
                // Fix: Math.floor() the milliseconds before converting to BigInt
                const currentTimeMicros = BigInt(Math.floor(currentTime) * 1000);
                const frameToUse = this.currentVideoStreamIsActive() ? this.lastVideoFrame : this.blackVideoFrame;
                this.sendVideo(currentTimeMicros, '0', frameToUse.width, frameToUse.height, frameToUse.frameData);
            }
        } catch (error) {
            console.error('Error in black frame timer:', error);
        }
    }, 250);
  }

  stopFillerFrameTimer() {
    if (this.fillerFrameInterval) {
        clearInterval(this.fillerFrameInterval);
        this.fillerFrameInterval = null;
    }
  }
  */

  async enableMediaSending() {
    this.mediaSendingEnabled = true;
    await window.styleManager.start();

    // No longer need this because we're not using MediaStreamTrackProcessor's
    //this.startFillerFrameTimer();
  }

  async disableMediaSending() {
    window.styleManager.stop();
    // Give the media recorder a bit of time to send the final data
    await new Promise(resolve => setTimeout(resolve, 2000));
    this.mediaSendingEnabled = false;

    // No longer need this because we're not using MediaStreamTrackProcessor's
    //this.stopFillerFrameTimer();
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
      if (this.ws.readyState !== WebSocket.OPEN) {
          console.error('WebSocket is not connected');
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

  sendEncodedMP4Chunk(encodedMP4Data) {
    if (this.ws.readyState !== WebSocket.OPEN) {
      console.error('WebSocket is not connected for video chunk send', this.ws.readyState);
      return;
    }

    if (!this.mediaSendingEnabled) {
      return;
    }

    try {
      // Create a header with just the message type (4 bytes)
      const headerBuffer = new ArrayBuffer(4);
      const headerView = new DataView(headerBuffer);
      headerView.setInt32(0, WebSocketClient.MESSAGE_TYPES.ENCODED_MP4_CHUNK, true);

      // Create a Blob that combines the header and the MP4 data
      const message = new Blob([headerBuffer, encodedMP4Data]);

      // Send the combined Blob directly
      this.ws.send(message);
    } catch (error) {
      console.error('Error sending WebSocket video chunk:', error);
    }
  }

  sendPerParticipantAudio(participantId, audioData) {
    if (this.ws.readyState !== WebSocket.OPEN) {
      console.error('WebSocket is not connected for per participant audio send', this.ws.readyState);
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
        console.error('Error sending WebSocket audio message:', error);
    }
  }

  sendMixedAudio(timestamp, audioData) {
      if (this.ws.readyState !== WebSocket.OPEN) {
          console.error('WebSocket is not connected for audio send', this.ws.readyState);
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
          console.error('Error sending WebSocket audio message:', error);
      }
  }

  sendVideo(timestamp, streamId, width, height, videoData) {
      if (this.ws.readyState !== WebSocket.OPEN) {
          console.error('WebSocket is not connected for video send', this.ws.readyState);
          return;
      }

      if (!this.mediaSendingEnabled) {
        return;
      }
      
      this.lastVideoFrameTime = performance.now();
      this.lastVideoFrame = {width, height, frameData: videoData};
      
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

// Interceptors

class FetchInterceptor {
    constructor(responseCallback) {
        this.originalFetch = window.fetch;
        this.responseCallback = responseCallback;
        window.fetch = (...args) => this.interceptFetch(...args);
    }

    async interceptFetch(...args) {
        try {
            // Call the original fetch
            const response = await this.originalFetch.apply(window, args);
            
            // Clone the response since it can only be consumed once
            const clonedResponse = response.clone();
            
            // Call the callback with the cloned response
            await this.responseCallback(clonedResponse);
            
            // Return the original response to maintain normal flow
            return response;
        } catch (error) {
            console.error('Error in intercepted fetch:', error);
            throw error;
        }
    }
}
class RTCInterceptor {
    constructor(callbacks) {
        // Store the original RTCPeerConnection
        const originalRTCPeerConnection = window.RTCPeerConnection;
        
        // Store callbacks
        const onPeerConnectionCreate = callbacks.onPeerConnectionCreate || (() => {});
        const onDataChannelCreate = callbacks.onDataChannelCreate || (() => {});
        
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
                onDataChannelCreate(dataChannel, peerConnection);
                return dataChannel;
            };
            
            return peerConnection;
        };
    }
}

// Message type definitions
const messageTypes = [
      {
        name: 'CollectionEvent',
        fields: [
            { name: 'body', fieldNumber: 1, type: 'message', messageType: 'CollectionEventBody' }
        ]
    },
    {
        name: 'CollectionEventBody',
        fields: [
            { name: 'userInfoListWrapperAndChatWrapperWrapper', fieldNumber: 2, type: 'message', messageType: 'UserInfoListWrapperAndChatWrapperWrapper' }
        ]
    },
    {
        name: 'UserInfoListWrapperAndChatWrapperWrapper',
        fields: [
            { name: 'deviceInfoWrapper', fieldNumber: 3, type: 'message', messageType: 'DeviceInfoWrapper' },
            { name: 'userInfoListWrapperAndChatWrapper', fieldNumber: 13, type: 'message', messageType: 'UserInfoListWrapperAndChatWrapper' }
        ]
    },
    {
        name: 'UserInfoListWrapperAndChatWrapper',
        fields: [
            { name: 'userInfoListWrapper', fieldNumber: 1, type: 'message', messageType: 'UserInfoListWrapper' },
            { name: 'chatMessageWrapper', fieldNumber: 4, type: 'message', messageType: 'ChatMessageWrapper', repeated: true }
        ]
    },
    {
        name: 'DeviceInfoWrapper',
        fields: [
            { name: 'deviceOutputInfoList', fieldNumber: 2, type: 'message', messageType: 'DeviceOutputInfoList', repeated: true }
        ]
    },
    {
        name: 'DeviceOutputInfoList',
        fields: [
            { name: 'deviceOutputType', fieldNumber: 2, type: 'varint' }, // Speculating that 1 = audio, 2 = video
            { name: 'streamId', fieldNumber: 4, type: 'string' },
            { name: 'deviceId', fieldNumber: 6, type: 'string' },
            { name: 'deviceOutputStatus', fieldNumber: 10, type: 'message', messageType: 'DeviceOutputStatus' }
        ]
    },
    {
        name: 'DeviceOutputStatus',
        fields: [
            { name: 'disabled', fieldNumber: 1, type: 'varint' }
        ]
    },
    // Existing message types
    {
        name: 'UserInfoListResponse',
        fields: [
            { name: 'userInfoListWrapperWrapper', fieldNumber: 2, type: 'message', messageType: 'UserInfoListWrapperWrapper' }
        ]
    },
    {
        name: 'UserInfoListResponse',
        fields: [
            { name: 'userInfoListWrapperWrapper', fieldNumber: 2, type: 'message', messageType: 'UserInfoListWrapperWrapper' }
        ]
    },
    {
        name: 'UserInfoListWrapperWrapper',
        fields: [
            { name: 'userInfoListWrapper', fieldNumber: 2, type: 'message', messageType: 'UserInfoListWrapper' }
        ]
    },
    {
        name: 'UserEventInfo',
        fields: [
            { name: 'eventNumber', fieldNumber: 1, type: 'varint' } // sequence number for the event
        ]
    },
    {
        name: 'UserInfoListWrapper',
        fields: [
            { name: 'userEventInfo', fieldNumber: 1, type: 'message', messageType: 'UserEventInfo' },
            { name: 'userInfoList', fieldNumber: 2, type: 'message', messageType: 'UserInfoList', repeated: true }
        ]
    },
    {
        name: 'UserInfoList',
        fields: [
            { name: 'deviceId', fieldNumber: 1, type: 'string' },
            { name: 'fullName', fieldNumber: 2, type: 'string' },
            { name: 'profilePicture', fieldNumber: 3, type: 'string' },
            { name: 'status', fieldNumber: 4, type: 'varint' }, // in meeting = 1 vs not in meeting = 6. kicked out = 7?
            { name: 'isCurrentUserString', fieldNumber: 7, type: 'string' }, // Presence of this string indicates that this is the current user. The string itself seems to be a random uuid
            { name: 'displayName', fieldNumber: 29, type: 'string' },
            { name: 'parentDeviceId', fieldNumber: 21, type: 'string' }, // if this is present, then this is a screenshare device. The parentDevice is the person that is sharing
            { name: 'isHost', fieldNumber: 34, type: 'varint' } // Presence of this varint indicates that this is the host
        ]
    },
    {
        name: 'CaptionWrapper',
        fields: [
            { name: 'caption', fieldNumber: 1, type: 'message', messageType: 'Caption' }
        ]
    },
    {
        name: 'Caption',
        fields: [
            { name: 'deviceId', fieldNumber: 1, type: 'string' },
            { name: 'captionId', fieldNumber: 2, type: 'int64' },
            { name: 'version', fieldNumber: 3, type: 'int64' },
            { name: 'isFinal', fieldNumber: 4, type: 'varint' },
            { name: 'text', fieldNumber: 6, type: 'string' },
            { name: 'languageId', fieldNumber: 8, type: 'int64' }
        ]
    },
    {
        name: 'ChatMessageWrapper',
        fields: [
            { name: 'chatMessage', fieldNumber: 2, type: 'message', messageType: 'ChatMessage' }
        ]
    },
    {
        name: 'ChatMessage',
        fields: [
            { name: 'messageId', fieldNumber: 1, type: 'string' },
            { name: 'deviceId', fieldNumber: 2, type: 'string' },
            { name: 'timestamp', fieldNumber: 3, type: 'int64' },
            { name: 'chatMessageContent', fieldNumber: 5, type: 'message', messageType: 'ChatMessageContent' }
        ]
    },
    {
        name: 'ChatMessageContent',
        fields: [
            { name: 'text', fieldNumber: 1, type: 'string' }
        ]
    }
];

// Generic message decoder factory
function createMessageDecoder(messageType) {
    return function decode(reader, length) {
        if (!(reader instanceof protobuf.Reader)) {
            reader = protobuf.Reader.create(reader);
        }

        const end = length === undefined ? reader.len : reader.pos + length;
        const message = {};

        while (reader.pos < end) {
            const tag = reader.uint32();
            const fieldNumber = tag >>> 3;
            
            const field = messageType.fields.find(f => f.fieldNumber === fieldNumber);
            if (!field) {
                reader.skipType(tag & 7);
                continue;
            }

            let value;
            switch (field.type) {
                case 'string':
                    value = reader.string();
                    break;
                case 'int64':
                    value = reader.int64();
                    break;
                case 'varint':
                    value = reader.uint32();
                    break;
                case 'message':
                    value = messageDecoders[field.messageType](reader, reader.uint32());
                    break;
                default:
                    reader.skipType(tag & 7);
                    continue;
            }

            if (field.repeated) {
                if (!message[field.name]) {
                    message[field.name] = [];
                }
                message[field.name].push(value);
            } else {
                message[field.name] = value;
            }
        }

        return message;
    };
}

const ws = new WebSocketClient();
window.ws = ws;
const userManager = new UserManager(ws);
const captionManager = new CaptionManager(ws);
const videoTrackManager = new VideoTrackManager(ws);
const styleManager = new StyleManager();
const receiverManager = new ReceiverManager();
const chatMessageManager = new ChatMessageManager(ws);
let rtpReceiverInterceptor = null;
if (window.initialData.sendPerParticipantAudio) {
    rtpReceiverInterceptor = new RTCRtpReceiverInterceptor((receiver, result, ...args) => {
        receiverManager.updateContributingSources(receiver, result);
    });
}

window.videoTrackManager = videoTrackManager;
window.userManager = userManager;
window.styleManager = styleManager;
window.receiverManager = receiverManager;
window.chatMessageManager = chatMessageManager;
window.sendChatMessage = sendChatMessage;
// Create decoders for all message types
const messageDecoders = {};
messageTypes.forEach(type => {
    messageDecoders[type.name] = createMessageDecoder(type);
});

function base64ToUint8Array(base64) {
    const binaryString = atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes;
}

const syncMeetingSpaceCollectionsUrl = "https://meet.google.com/$rpc/google.rtc.meetings.v1.MeetingSpaceService/SyncMeetingSpaceCollections";
const userMap = new Map();
new FetchInterceptor(async (response) => {
    if (response.url === syncMeetingSpaceCollectionsUrl) {
        const responseText = await response.text();
        const decodedData = base64ToUint8Array(responseText);
        const userInfoListResponse = messageDecoders['UserInfoListResponse'](decodedData);
        const userInfoList = userInfoListResponse.userInfoListWrapperWrapper?.userInfoListWrapper?.userInfoList || [];
        console.log('userInfoList', userInfoList);
        if (userInfoList.length > 0) {
            userManager.newUsersListSynced(userInfoList);
        }
    }
});

const handleCollectionEvent = (event) => {
  const decodedData = pako.inflate(new Uint8Array(event.data));
  //console.log(' handleCollectionEventdecodedData', decodedData);
  // Convert decoded data to base64
  const base64Data = btoa(String.fromCharCode.apply(null, decodedData));
  //console.log('Decoded collection event data (base64):', base64Data);

  const collectionEvent = messageDecoders['CollectionEvent'](decodedData);
  
  const deviceOutputInfoList = collectionEvent.body.userInfoListWrapperAndChatWrapperWrapper?.deviceInfoWrapper?.deviceOutputInfoList;
  if (deviceOutputInfoList) {
    userManager.updateDeviceOutputs(deviceOutputInfoList);
  }

  const chatMessageWrapper = collectionEvent.body.userInfoListWrapperAndChatWrapperWrapper?.userInfoListWrapperAndChatWrapper?.chatMessageWrapper;
  if (chatMessageWrapper) {
    for (const chatMessage of chatMessageWrapper) {
        window.chatMessageManager?.handleChatMessage(chatMessage);
    }
  }

  //console.log('deviceOutputInfoList', JSON.stringify(collectionEvent.body.userInfoListWrapperAndChatWrapperWrapper?.deviceInfoWrapper?.deviceOutputInfoList));
  //console.log('usermap', userMap.allUsersMap);
  //console.log('userInfoList And Event', collectionEvent.body.userInfoListWrapperAndChatWrapperWrapper.userInfoListWrapperAndChatWrapper.userInfoListWrapper);
  const userInfoList = collectionEvent.body.userInfoListWrapperAndChatWrapperWrapper.userInfoListWrapperAndChatWrapper.userInfoListWrapper?.userInfoList || [];
  console.log('userInfoList in collection event', userInfoList);
  // This event is triggered when a single user joins (or leaves) the meeting
  // generally this array only contains a single user
  // we can't tell whether the event is a join or leave event, so we'll assume it's a join
  // if it's a leave, then we'll pick it up from the periodic call to syncMeetingSpaceCollections
  // so there will be a lag of roughly a minute for leave events
  for (const user of userInfoList) {
    userManager.singleUserSynced(user);
  }
};

// the stream ID, not the track id in the TRACK appears in the payload of the protobuf message somewhere

const handleCaptionEvent = (event) => {
  const decodedData = new Uint8Array(event.data);
  const captionWrapper = messageDecoders['CaptionWrapper'](decodedData);
  const caption = captionWrapper.caption;
  captionManager.singleCaptionSynced(caption);
}

const handleMediaDirectorEvent = (event) => {
  console.log('handleMediaDirectorEvent', event);
  const decodedData = new Uint8Array(event.data);
  //console.log(' handleCollectionEventdecodedData', decodedData);
  // Convert decoded data to base64
  const base64Data = btoa(String.fromCharCode.apply(null, decodedData));
  console.log('Decoded media director event data (base64):', base64Data);
}

const handleVideoTrack = async (event) => {  
  try {
    // Create processor to get raw frames
    const processor = new MediaStreamTrackProcessor({ track: event.track });
    const generator = new MediaStreamTrackGenerator({ kind: 'video' });
    
    // Add track ended listener
    event.track.addEventListener('ended', () => {
        console.log('Video track ended:', event.track.id);
        videoTrackManager.deleteVideoTrack(event.track);
    });
    
    // Get readable stream of video frames
    const readable = processor.readable;
    const writable = generator.writable;

    const firstStreamId = event.streams[0]?.id;

    // Check if of the users who are in the meeting and screensharers
    // if any of them have an associated device output with the first stream ID of this video track
    const isScreenShare = userManager
        .getCurrentUsersInMeetingWhoAreScreenSharing()
        .some(user => firstStreamId && userManager.getDeviceOutput(user.deviceId, DEVICE_OUTPUT_TYPE.VIDEO).streamId === firstStreamId);
    if (firstStreamId) {
        videoTrackManager.upsertVideoTrack(event.track, firstStreamId, isScreenShare);
    }

    // Add frame rate control variables
    const targetFPS = isScreenShare ? 5 : 15;
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
                
                if (firstStreamId && firstStreamId === videoTrackManager.getStreamIdToSendCached()) {
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
                console.error('Error processing frame:', error);
                frame.close();
            }
        },
        flush() {
            console.log('Transform stream flush called');
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
            });
    } catch (error) {
        console.error('Stream pipeline error:', error);
        abortController.abort();
    }

  } catch (error) {
      console.error('Error setting up video interceptor:', error);
  }
};

const handleAudioTrack = async (event) => {
  let lastAudioFormat = null;  // Track last seen format
  
  try {
    // Create processor to get raw frames
    const processor = new MediaStreamTrackProcessor({ track: event.track });
    const generator = new MediaStreamTrackGenerator({ kind: 'audio' });
    
    // Get readable stream of audio frames
    const readable = processor.readable;
    const writable = generator.writable;

    const firstStreamId = event.streams[0]?.id;

    const receiver = event.receiver;

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

                // If the audioData buffer is all zeros, then we don't want to send it
                if (audioData.every(value => value === 0)) {
                    return;
                }

                const contributingSources = receiverManager.getContributingSources(receiver);

                const usersForContributingSourcesWithAudioLevel = contributingSources.map(source => {
                    return {
                        audioLevel: source?.audioLevel || 0, 
                        user: userManager.getUserByStreamId(source.source.toString())
                    }
                }).filter(x => x.user).sort((a, b) => b.audioLevel - a.audioLevel);

                const userForContributingSourceWithLoudestAudio = usersForContributingSourcesWithAudioLevel[0]?.user;


                //console.log('contributingSources', contributingSources);
                //console.log('deviceOutputMap', userManager.deviceOutputMap);
                //console.log('usersForContributingSources', usersForContributingSources);

                // Send audio data through websocket
                if (userForContributingSourceWithLoudestAudio) {
                    const firstUserId = userForContributingSourceWithLoudestAudio?.deviceId;
                    if (firstUserId) {
                        ws.sendPerParticipantAudio(firstUserId, audioData);
                    }
                }
                
                // Pass through the original frame
                controller.enqueue(frame);
            } catch (error) {
                console.error('Error processing frame:', error);
                frame.close();
            }
        },
        flush() {
            console.log('Transform stream flush called');
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
            });
    } catch (error) {
        console.error('Stream pipeline error:', error);
        abortController.abort();
    }

  } catch (error) {
      console.error('Error setting up audio interceptor:', error);
  }
};

new RTCInterceptor({
    onPeerConnectionCreate: (peerConnection) => {
        console.log('New RTCPeerConnection created:', peerConnection);
        peerConnection.addEventListener('datachannel', (event) => {
            console.log('datachannel', event);
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
            console.log('New track:', {
                trackId: event.track.id,
                streams: event.streams,
                streamIds: event.streams.map(stream => stream.id),
                // Get any msid information
                transceiver: event.transceiver,
                // Get the RTP parameters which might contain stream IDs
                rtpParameters: event.transceiver?.sender.getParameters()
            });
            if (event.track.kind === 'audio') {
                handleAudioTrack(event);
            }
            if (event.track.kind === 'video') {
                handleVideoTrack(event);
            }
        });
        */

        // Log the signaling state changes
        peerConnection.addEventListener('signalingstatechange', () => {
            console.log('Signaling State:', peerConnection.signalingState);
        });

        // Log the SDP being exchanged
        const originalSetLocalDescription = peerConnection.setLocalDescription;
        peerConnection.setLocalDescription = function(description) {
            console.log('Local SDP:', description);
            return originalSetLocalDescription.apply(this, arguments);
        };

        const originalSetRemoteDescription = peerConnection.setRemoteDescription;
        peerConnection.setRemoteDescription = function(description) {
            console.log('Remote SDP:', description);
            return originalSetRemoteDescription.apply(this, arguments);
        };

        // Log ICE candidates
        peerConnection.addEventListener('icecandidate', (event) => {
            if (event.candidate) {
                console.log('ICE Candidate:', event.candidate);
            }
        });
    },
    onDataChannelCreate: (dataChannel, peerConnection) => {
        console.log('New DataChannel created:', dataChannel);
        console.log('On PeerConnection:', peerConnection);
        console.log('Channel label:', dataChannel.label);

        //if (dataChannel.label === 'collections') {
          //  dataChannel.addEventListener("message", (event) => {
         //       console.log('collectionsevent', event)
        //    });
        //}


      if (dataChannel.label === 'media-director') {
        dataChannel.addEventListener("message", (mediaDirectorEvent) => {
            handleMediaDirectorEvent(mediaDirectorEvent);
        });
      }

       if (dataChannel.label === 'captions' && window.initialData.collectCaptions) {
            dataChannel.addEventListener("message", (captionEvent) => {
                handleCaptionEvent(captionEvent);
            });
        }
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

function clickLanguageOption(languageCode) {
    // Find the element with data-value attribute matching the language code
    const languageElement = document.querySelector(`li[data-value="${languageCode}"]`);

    // Check if the element exists
    if (languageElement) {
        // Click the element
        languageElement.click();
        return true;
    } else {
        return false;
    }
}

async function turnOnCamera() {
    // Click camera button to turn it on
    let cameraButton = null;
    const numAttempts = 30;
    for (let i = 0; i < numAttempts; i++) {
        cameraButton = document.querySelector('button[aria-label="Turn on camera"]') || document.querySelector('div[aria-label="Turn on camera"]');
        if (cameraButton) {
            break;
        }
        window.ws.sendJson({
            type: 'Error',
            message: 'Camera button not found in turnOnCamera, but will try again'
        });
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
        window.ws.sendJson({
            type: 'Error',
            message: 'Camera button not found in turnOnCamera'
        });
    }
}

function turnOnMic() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Turn on microphone"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }
}

function turnOffMic() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector('button[aria-label="Turn off microphone"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }
}

function turnOnMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Turn on microphone"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn on camera"]');
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOffMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector('button[aria-label="Turn off microphone"]');
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector('button[aria-label="Turn off camera"]');
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
        this.videoSoundSource = null;
        this.botOutputVideoElementCaptureStream = null;

        // For outputting image
        this.botOutputCanvasElement = null;
        this.botOutputCanvasElementCaptureStream = null;
        this.lastImageBytes = null;
        
        // For outputting audio
        this.audioContextForBotOutput = null;
        this.gainNode = null;
        this.destination = null;
        this.botOutputAudioTrack = null;

        // For outputting a stream
        this.botOutputMediaStream = null;
        this.botOutputPeerConnection = null;
    }

    connectVideoSourceToAudioContext() {
        if (this.botOutputVideoElement && this.audioContextForBotOutput && !this.videoSoundSource) {
            this.videoSoundSource = this.audioContextForBotOutput.createMediaElementSource(this.botOutputVideoElement);
            this.videoSoundSource.connect(this.gainNode);
        }
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

    playVideo(videoUrl) {
        // If camera or mic are on, turn them off
        turnOffMicAndCamera();

        this.addBotOutputVideoElement(videoUrl);

        // Add event listener to wait until the video starts playing
        this.botOutputVideoElement.addEventListener('playing', () => {
            console.log("Video has started playing, turning on mic and camera");

            this.botOutputVideoElementCaptureStream = this.botOutputVideoElement.captureStream();

            turnOnMicAndCamera();
        }, { once: true });
    }

    isVideoPlaying() {
        return !!this.botOutputVideoElement;
    }

    addBotOutputVideoElement(url) {
        // Disconnect previous video source if it exists
        if (this.videoSoundSource) {
            this.videoSoundSource.disconnect();
            this.videoSoundSource = null;
        }
    
        // Remove any existing video element
        if (this.botOutputVideoElement) {
            this.botOutputVideoElement.remove();
        }
    
        // Create new video element
        this.botOutputVideoElement = document.createElement('video');
        this.botOutputVideoElement.style.display = 'none';
        this.botOutputVideoElement.src = url;
        this.botOutputVideoElement.crossOrigin = 'anonymous';
        this.botOutputVideoElement.loop = false;
        this.botOutputVideoElement.autoplay = true;
        this.botOutputVideoElement.muted = false;
        // Clean up when video ends
        this.botOutputVideoElement.addEventListener('ended', () => {
            turnOffMicAndCamera();
            if (this.videoSoundSource) {
                this.videoSoundSource.disconnect();
                this.videoSoundSource = null;
            }
            this.botOutputVideoElement.remove();
            this.botOutputVideoElement = null;
            this.botOutputVideoElementCaptureStream = null;

            // If we were displaying an image, turn the camera back on
            if (this.botOutputCanvasElementCaptureStream) {
                this.botOutputCanvasElementCaptureStream = null;
                // Resend last image in 1 second
                if (this.lastImageBytes) {
                    setTimeout(() => {
                        this.displayImage(this.lastImageBytes);
                    }, 1000);
                }
            }
        });
    
        document.body.appendChild(this.botOutputVideoElement);
    }

    displayImage(imageBytes) {
        try {
            // Wait for the image to be loaded onto the canvas
            return this.writeImageToBotOutputCanvas(imageBytes)
                .then(async () => {
                // If the stream is already broadcasting, don't do anything
                if (this.botOutputCanvasElementCaptureStream)
                {
                    console.log("Stream already broadcasting, skipping");
                    return;
                }

                // Now that the image is loaded, capture the stream and turn on camera
                this.lastImageBytes = imageBytes;
                this.botOutputCanvasElementCaptureStream = this.botOutputCanvasElement.captureStream(1);
                await turnOnCamera();
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
}

const botOutputManager = new BotOutputManager();
window.botOutputManager = botOutputManager;

navigator.mediaDevices.getUserMedia = function(constraints) {
    return _getUserMedia.call(navigator.mediaDevices, constraints)
      .then(originalStream => {
        console.log("Intercepted getUserMedia:", constraints);
  
        // Stop any original tracks so we don't actually capture real mic/cam
        originalStream.getTracks().forEach(t => t.stop());
  
        // Create a new MediaStream to return
        const newStream = new MediaStream();
          
        if (constraints.video && botOutputManager.botOutputVideoElementCaptureStream) {
            console.log("Adding video track", botOutputManager.botOutputVideoElementCaptureStream.getVideoTracks()[0]);
            newStream.addTrack(botOutputManager.botOutputVideoElementCaptureStream.getVideoTracks()[0]);
        }
        // Video is prioritized over canvas
        else {
            if (constraints.video && botOutputManager.botOutputCanvasElementCaptureStream) {
                console.log("Adding canvas track", botOutputManager.botOutputCanvasElementCaptureStream.getVideoTracks()[0]);
                newStream.addTrack(botOutputManager.botOutputCanvasElementCaptureStream.getVideoTracks()[0]);
            }
         }
         if (constraints.video && botOutputManager.botOutputMediaStream) {
            console.log("Adding botOutputMediaStream", botOutputManager.botOutputMediaStream.getVideoTracks()[0]);
            newStream.addTrack(botOutputManager.botOutputMediaStream.getVideoTracks()[0]);
        }

        // Audio sending not supported yet
        
        // If audio is requested, add our fake audio track
        if (constraints.audio) {  // Only create once
            botOutputManager.initializeBotOutputAudioTrack();
            newStream.addTrack(botOutputManager.botOutputAudioTrack);
        }  

        // Video sending not supported yet
        if (botOutputManager.botOutputVideoElementCaptureStream) {
            botOutputManager.connectVideoSourceToAudioContext();
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