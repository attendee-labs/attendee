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
                isCurrentUser: user.isCurrentUser
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
                isCurrentUser: user.isCurrentUser
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
      // Only intercept connections directly to the speakers
      if (target instanceof AudioDestinationNode) {
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


const turnOnCameraArialLabel = "start my video"
const turnOffCameraArialLabel = "stop my video"
const turnOnMicArialLabel = "unmute my microphone"
const turnOffMicArialLabel = "mute my microphone"

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
    // Click microphone button to turn it on
    const microphoneButton = document.querySelector(`button[aria-label="${turnOffMicArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffMicArialLabel}"]`);
    if (microphoneButton) {
        console.log("Clicking the microphone button to turn it off");
        microphoneButton.click();
    } else {
        console.log("Microphone off button not found");
    }

    // Click camera button to turn it on
    const cameraButton = document.querySelector(`button[aria-label="${turnOffCameraArialLabel}"]`) || document.querySelector(`div[aria-label="${turnOffCameraArialLabel}"]`);
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