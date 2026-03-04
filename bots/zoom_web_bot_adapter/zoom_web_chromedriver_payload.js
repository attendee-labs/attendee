class ParticipantSpeechStartStopManager {
    constructor() {
        // Only one active speaker at a time
        this.activeSpeaker = null;
    }

    sendSpeechStartStopEvent(participantId, isSpeechStart, timestamp) {
        window.ws?.sendJson({
            type: 'ParticipantSpeechStartStopEvent',
            participantId: participantId.toString(),
            isSpeechStart: isSpeechStart,
            timestamp: timestamp
        });
    }

    addActiveSpeaker(speakerId) {
        if (this.activeSpeaker === speakerId) {
            return;
        }
        if (this.activeSpeaker) {
            this.sendSpeechStartStopEvent(this.activeSpeaker, false, Date.now());
        }
        this.activeSpeaker = speakerId;
        this.sendSpeechStartStopEvent(this.activeSpeaker, true, Date.now());
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

    window.ws.sendJson({
        type: 'AudioTrackStarted',
        trackId: event.track.id
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

// Style manager
class StyleManager {
    constructor() {
        this.meetingAudioStream = null;
        this.audioStreams = []
    }

    addAudioStream(audioStream) {
        this.audioStreams.push(audioStream);
    }

    async start() {
        console.log('StyleManager start');

        // This code is just grabbing a unified audio stream

        // Retrieve all <audio> elements on the page
        const audioElements = document.querySelectorAll('audio');

        this.audioContext = new AudioContext({ sampleRate: 48000 });

        // Combine the audioStreams we've accumulated with anything from audioElements in the DOM.
        const audioStreamTracks = this.audioStreams.map(stream => {
            return stream.getAudioTracks()[0];
        })
        const audioElementTracks = Array.from(audioElements).map(audioElement => {
            return audioElement.srcObject.getAudioTracks()[0];
        });
        this.audioTracks = audioStreamTracks.concat(audioElementTracks);

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

        this.meetingAudioStream = destination.stream;

        if (this.meetingAudioStream.getAudioTracks().length == 0)
        {
            console.log("this.meetingAudioStream.getAudioTracks() had length 0")
            return;
        }

        if (initialData.sendPerParticipantAudio)
            handleAudioTrack({track: this.meetingAudioStream.getAudioTracks()[0]});  
    }
    
    getMeetingAudioStream() {
        return this.meetingAudioStream;
    }

    async stop() {
        console.log('StyleManager stop');
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
    }

    async enableMediaSending() {
        this.mediaSendingEnabled = true;
        await window.styleManager.start();
    }

    async disableMediaSending() {
        window.styleManager.stop();
        // Give the media recorder a bit of time to send the final data
        await new Promise(resolve => setTimeout(resolve, 2000));
        this.mediaSendingEnabled = false;
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
}

class UserManager {
    constructor(ws) {
        this.allUsersMap = new Map();
        this.currentUsersMap = new Map();
        this.deviceOutputMap = new Map();

        this.ws = ws;
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

    convertUser(zoomUser) {
        return {
            deviceId: zoomUser.userId.toString(),
            displayName: zoomUser.userName,
            fullName: zoomUser.userName,
            profile: '',
            status: zoomUser.state,
            isHost: zoomUser.isHost,
            humanized_status: zoomUser.state === "active" ? "in_meeting" : "not_in_meeting",
            isCurrentUser: zoomUser.self
        };
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

// This code intercepts the connect method on the AudioNode class
// When something is connected to the speaker the underlying track is added to our styleManager
// so that it can be aggregated into a stream representing the meeting audio
(() => {
    const origConnect = AudioNode.prototype.connect;
  
    AudioNode.prototype.connect = function(target, ...rest) {

      // Only intercept connections directly to the speakers. The target !== window.botOutputManager?.getAudioContextDestination() condition is to avoid capturing the bots output 
      if (target instanceof AudioDestinationNode && target !== window.botOutputManager?.getAudioContextDestination()) {
        const ctx = this.context;
        // Create a single tee per context
        if (!ctx.__captureTee) {
        try{
          const tee = ctx.createGain();
          const tap = ctx.createMediaStreamDestination();
          origConnect.call(tee, ctx.destination); // keep normal playback
          origConnect.call(tee, tap);             // capture
          ctx.__captureTee = { tee, tap };
          const capturedStream = tap.stream;
          if (capturedStream)
            window.styleManager.addAudioStream(capturedStream);
        }
        catch (error) {
            console.error('Error in AudioNodeInterceptor:', error);
        }
        }
  
        // Reroute to the tee instead of the destination
        return origConnect.call(this, ctx.__captureTee.tee, ...rest);
      }
  
      return origConnect.call(this, target, ...rest);
    };
  })();

const ws = new WebSocketClient();
window.ws = ws;
const dominantSpeakerManager = new DominantSpeakerManager();
window.dominantSpeakerManager = dominantSpeakerManager;
const styleManager = new StyleManager();
window.styleManager = styleManager;
const userManager = new UserManager(ws);
window.userManager = userManager;
const participantSpeechStartStopManager = new ParticipantSpeechStartStopManager();
window.participantSpeechStartStopManager = participantSpeechStartStopManager;


const turnOnCameraArialLabel = "start my video"
const turnOffCameraArialLabel = "stop my video"
const turnOnMicArialLabel = "unmute my microphone"
const turnOffMicArialLabel = "mute my microphone"
const turnOnScreenshareArialLabel = "Share Screen"
const turnOffScreenshareClass = "sharer-button--stop"

async function turnOnCamera() {
    // Click camera button to turn it on
    let cameraButton = null;
    const numAttempts = 30;
    for (let i = 0; i < numAttempts; i++) {
        cameraButton = document.querySelector(`button[aria-label="${turnOnCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnCameraArialLabel}"]`);
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
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }
}

function turnOffMic() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }
}

function turnOnMicAndCamera() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector(`button[aria-label="${turnOnCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnCameraArialLabel}"]`);
    if (cameraButton) {
        console.log("Clicking the camera button to turn it on");
        cameraButton.click();
    } else {
        console.log("Camera button not found");
    }
}

function turnOffMicAndCamera() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it off
    const cameraButton = document.querySelector(`button[aria-label="${turnOffCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffCameraArialLabel}"]`);
    if (cameraButton) {
        console.log("Clicking the camera button to turn it off");
        cameraButton.click();
    } else {
        console.log("Camera off button not found");
    }
}

function turnOnMicAndScreenshare() {
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOnMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it on");
        microphoneButton.click();
    } else {
        console.log("Microphone button not found");
    }

    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[aria-label="${turnOnScreenshareArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnScreenshareArialLabel}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
    }
}

function turnOffMicAndScreenshare() {
    // Click microphone button to turn it off
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`.${turnOffScreenshareClass}`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it off");
        screenshareButton.click();
    } else {
        console.log("Screenshare off button not found");
    }
}

function turnOnScreenshare() {
    // Click screenshare button to turn it on
    const screenshareButton = document.querySelector(`button[aria-label="${turnOnScreenshareArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOnScreenshareArialLabel}"]`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it on");
        screenshareButton.click();
    } else {
        console.log("Screenshare button not found");
    }
}

function turnOffScreenshare() {
    // Click screenshare button to turn it off
    const screenshareButton = document.querySelector(`.${turnOffScreenshareClass}`);
    if (screenshareButton) {
        console.log("Clicking the screenshare button to turn it off");
        screenshareButton.click();
    } else {
        console.log("Screenshare off button not found");
    }
}

// BotOutputManager is defined in shared_chromedriver_payload.js

botOutputManager = new BotOutputManager({
    turnOnWebcam: turnOnCamera,
    turnOffWebcam: () => {
        console.log("Turning off webcam");
    },
    turnOnScreenshare: turnOnScreenshare,
    turnOffScreenshare: turnOffScreenshare,
    turnOnMic: turnOnMic,
    turnOffMic: turnOffMic,
    callOriginalGetUserMedia: true,
});

window.botOutputManager = botOutputManager;
