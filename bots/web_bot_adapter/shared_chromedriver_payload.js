// Holds the state of a bot video output stream. We need this class because there are two bot video output streams, one for webcam and one for screenshare.
class BotVideoOutputStream {
    constructor({
        turnOnInput = () => {},
        turnOffInput = () => {},
        ensureMicOn = () => {},
        ensureMicOff = () => {},
        getGainNode = () => {},
        getAudioContext = () => {},
        createSourceAudioTrack = () => {},
    }) {
        this.turnOnInput = turnOnInput;
        this.turnOffInput = turnOffInput;
        this.ensureMicOn = ensureMicOn;
        this.ensureMicOff = ensureMicOff;
        this.getGainNode = getGainNode;
        this.getAudioContext = getAudioContext;
        this.createSourceAudioTrack = createSourceAudioTrack;

        // --- VIDEO SOURCE SETUP (single source canvas) ---
        this.canvas = document.createElement("canvas");
        // Canvas must be 1280x640. Needed to work in Teams.
        this.canvasWidthForImage = 1280;
        this.canvasHeightForImage = 640;
        this.canvas.width = this.canvasWidthForImage;
        this.canvas.height = this.canvasHeightForImage;
        this.canvasCtx = this.canvas.getContext("2d");
        this.imageRedrawInterval = null;

        this.canvasCtx.fillStyle = "black";
        this.canvasCtx.fillRect(0, 0, this.canvas.width, this.canvas.height);

        const sourceVideoStream = this.canvas.captureStream(30); // NEEDS to be 30 or Google Meet complains

        // This is our *source* video track; we will CLONE it for callers.
        const videoTracks = sourceVideoStream.getVideoTracks();
        this.sourceVideoTrack = videoTracks[0] || null;

        this.videoElement = null;
        this.videoRafId = null;
        this.videoAudioSource = null;
    }

    /**
     * Display a PNG image on the virtual webcam.
     *
     * @param {ArrayBuffer|Uint8Array} imageBytes - Raw PNG bytes.
     * @returns {Promise<void>}
     */
    // 3 non-obvious things you need to do to make this work:
    // 1. Image needs to be redrawn on canvas
    // 2. Canvas needs to have a fixed "reasonable" size
    async displayImage(imageBytes) {
        this._stopVideoPlayback(); // Ensure no video is currently drawing

        if (!imageBytes) {
            throw new Error("displayImage: imageBytes is required.");
        }

        let buffer;
        if (imageBytes instanceof ArrayBuffer) {
            buffer = imageBytes;
        } else if (ArrayBuffer.isView(imageBytes)) {
            buffer = imageBytes.buffer;
        } else {
            throw new Error(
                "displayImage: expected ArrayBuffer or TypedArray for imageBytes."
            );
        }

        const blob = new Blob([buffer], { type: "image/png" });
        const url = URL.createObjectURL(blob);
        try {
            const img = await this._loadImage(url);
            this.canvas.width = this.canvasWidthForImage;
            this.canvas.height = this.canvasHeightForImage;
            const imageDrawParams = this.calculateImageDrawParamsForLetterBoxing(img.width, img.height);
            this.canvasCtx.drawImage(img, imageDrawParams.offsetX, imageDrawParams.offsetY, imageDrawParams.width, imageDrawParams.height);
            // Set up an interval that redraws the image every 1000ms. Needed to work in Teams.
            this.imageRedrawInterval = setInterval(() => {
                this.canvasCtx.drawImage(img, imageDrawParams.offsetX, imageDrawParams.offsetY, imageDrawParams.width, imageDrawParams.height);
            }, 1000);
            this.ensureInputOn();

            // Capture last image bytes, so that we can display it again if we play a video
            this.lastImageBytes = imageBytes;
        } finally {
            URL.revokeObjectURL(url);
        }
    }

    _loadImage(url) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = (err) => reject(err);
            img.src = url;
        });
    }

    ensureInputOn() {
        try {
            this.turnOnInput && this.turnOnInput();
        } catch (e) {
            console.error("Error in turnOnInput callback:", e);
        }
    }

    ensureInputOff() {
        try {
            this.turnOffInput && this.turnOffInput();
        } catch (e) {
            console.error("Error in turnOffInput callback:", e);
        }
    }

    isVideoPlaying() {
        return !!this.videoElement && !this.videoElement.paused && !this.videoElement.ended;
    }

    /**
     * Play a video (with audio) through the virtual webcam/mic.
     *
     * @param {string} videoUrl - URL of the video to play.
     * @returns {Promise<void>}
     */
    async playVideo(videoUrl) {
        if (!videoUrl) {
            throw new Error("playVideo: videoUrl is required.");
        }

        this._stopVideoPlayback();
        this._stopImageRedrawInterval();

        if (!this.videoElement) {
            this.videoElement = document.createElement("video");
            this.videoElement.playsInline = true;
        }

        this.videoElement.muted = false;
        this.videoElement.src = videoUrl;
        this.videoElement.loop = false;
        this.videoElement.autoplay = true;
        this.videoElement.crossOrigin = "anonymous";

        if (!this.videoAudioSource) {
            // Create a Web Audio source for the video element
            this.videoAudioSource =
                this.getAudioContext().createMediaElementSource(this.videoElement);
            this.videoAudioSource.connect(this.getGainNode());
            // (Optional) also connect to speakers:
            // this.videoAudioSource.connect(this.audioContext.destination);
        }

        if (this.getAudioContext().state === "suspended") {
            await this.getAudioContext().resume();
        }

        await this.videoElement.play();
        this.ensureInputOn();
        this.ensureMicOn();

        this._startVideoDrawingLoop();

        // Add event listener for when video ends to display the last image
        this.videoEndedHandler = () => {
            // If we had an image, display it again keep the input on if not turn it off
            if (this.lastImageBytes) {
                this.displayImage(this.lastImageBytes);
            }
            else {
                this.ensureInputOff();
            }
        };
        this.videoElement.addEventListener('ended', this.videoEndedHandler);
    }



    _startVideoDrawingLoop() {
        if (!this.videoElement) return;

        let lastDrawTime = 0;
        const drawInterval = 1000 / 15; // ~15fps (60fps / 4)

        const drawFrame = (timestamp) => {
            if (
                !this.videoElement ||
                this.videoElement.paused ||
                this.videoElement.ended
            ) {
                this.videoRafId = null;
                return;
            }

            // Only draw if enough time has passed (throttle to 1/3 of normal rate)
            if (timestamp - lastDrawTime >= drawInterval) {
                // Resize canvas on first valid frame
                const vw = this.videoElement.videoWidth;
                const vh = this.videoElement.videoHeight;
                if (vw && vh && (this.canvas.width !== vw || this.canvas.height !== vh)) {
                    this.canvas.width = vw;
                    this.canvas.height = vh;
                }

                this.canvasCtx.drawImage(
                    this.videoElement,
                    0,
                    0,
                    this.canvas.width,
                    this.canvas.height
                );

                lastDrawTime = timestamp;
            }

            this.videoRafId = requestAnimationFrame(drawFrame);
        };

        this.videoRafId = requestAnimationFrame(drawFrame);
    }

    _stopVideoPlayback() {
        if (this.videoRafId != null) {
            cancelAnimationFrame(this.videoRafId);
            this.videoRafId = null;
        }
        if (this.videoElement) {
            this.videoElement.pause();
            // Remove the ended event listener if it exists
            if (this.videoEndedHandler) {
                this.videoElement.removeEventListener('ended', this.videoEndedHandler);
                this.videoEndedHandler = null;
            }
            // Keep src in case you want to resume later; or clear it:
            // this.videoElement.src = "";
        }
    }

    _stopImageRedrawInterval() {
        if (this.imageRedrawInterval) {
            clearInterval(this.imageRedrawInterval);
            this.imageRedrawInterval = null;
        }
    }



    calculateImageDrawParamsForLetterBoxing(imageWidth, imageHeight) {
        const imgAspect = imageWidth / imageHeight;
        const canvasAspect = this.canvas.width / this.canvas.height;
        
        // Calculate dimensions to fit image within canvas with letterboxing
        let renderWidth, renderHeight, offsetX, offsetY;
        
        if (imgAspect > canvasAspect) {
            // Image is wider than canvas (horizontal letterboxing)
            renderWidth = this.canvas.width;
            renderHeight = this.canvas.width / imgAspect;
            offsetX = 0;
            offsetY = (this.canvas.height - renderHeight) / 2;
        } else {
            // Image is taller than canvas (vertical letterboxing)
            renderHeight = this.canvas.height;
            renderWidth = this.canvas.height * imgAspect;
            offsetX = (this.canvas.width - renderWidth) / 2;
            offsetY = 0;
        }
        
        return {
            offsetX: offsetX,
            offsetY: offsetY,
            width: renderWidth,
            height: renderHeight
        };
    }



    /**
     * Play a MediaStream (e.g. from a WebRTC peer connection) through the
     * virtual webcam and mic.
     *
     * - Video tracks are drawn onto the canvas (same path as playVideo).
     * - Audio tracks, if present, are routed into the same gainNode /
     *   virtual mic pipeline as playPCMAudio / playVideo.
     *
     * @param {MediaStream} mediaStream
     * @returns {Promise<void>}
     */
    async playMediaStream(mediaStream) {
        if (!(mediaStream instanceof MediaStream)) {
            throw new Error("playMediaStream: mediaStream must be a MediaStream.");
        }
        try{

            // Stop any previous video playback and image redraw loop
            this._stopVideoPlayback();
            this._stopImageRedrawInterval();

            if (!this.videoElement) {
                this.videoElement = document.createElement("video");
                this.videoElement.playsInline = true;
            }

            this.videoElement.muted = true;
            // Attach the MediaStream to the video element
            this.videoElement.srcObject = mediaStream;
            this.videoElement.loop = false;
            this.videoElement.autoplay = true;

            ///---- NOT SURE IF WE NEED THIS
            this.createSourceAudioTrack();

            // (Re)wire a MediaStreamAudioSourceNode from the stream into the same gainNode
            if (this.mediaStreamAudioSource) {
                this.mediaStreamAudioSource.disconnect();
            }
            this.mediaStreamAudioSource =
                this.getAudioContext().createMediaStreamSource(mediaStream);
            this.mediaStreamAudioSource.connect(this.getGainNode());
            ///----

            if (this.getAudioContext().state === "suspended") {
                await this.getAudioContext().resume();
            }

            await this.videoElement.play();
            this.ensureInputOn();
            this.ensureMicOn();

            this._startVideoDrawingLoop();
        }
        catch (e) {
            window.ws.sendJson({
                type: 'PLAY_MEDIA_STREAM_ERROR',
                error: e.message
            });
        }
    }

    async stopMediaStream() {
        this._stopVideoPlayback();
        
        // If we had an image, display it again keep the input on if not turn it off
        if (this.lastImageBytes) {
            this.displayImage(this.lastImageBytes);
        }
        else {
            this.ensureInputOff();
        }
    }
}

class BotOutputManager {
    /**
     * @param {Object} callbacks
     * @param {Function} [callbacks.turnOnWebcam]
     * @param {Function} [callbacks.turnOffWebcam]
     * @param {Function} [callbacks.turnOnMic]
     * @param {Function} [callbacks.turnOffMic]
     */
    constructor({
        turnOnWebcam = () => {},
        turnOffWebcam = () => {},
        turnOnScreenshare = () => {},
        turnOffScreenshare = () => {},
        turnOnMic = () => {},
        turnOffMic = () => {},
    } = {}) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error("navigator.mediaDevices.getUserMedia is not available in this context.");
        }

        this.turnOnWebcam = turnOnWebcam;
        this.turnOffWebcam = turnOffWebcam;
        this.turnOnScreenshare = turnOnScreenshare;
        this.turnOffScreenshare = turnOffScreenshare;
        this.turnOnMic = turnOnMic;
        this.turnOffMic = turnOffMic;

        // We don't create the sourceAudioTrack until we need it. Otherwise it will play through the speakers. Not sure why this happens.
        this.sourceAudioTrack = null;

        // ---- AUDIO QUEUE STATE ----
        this.audioQueue = [];
        this.isPlayingAudioQueue = false;
        this.nextPlayTime = 0;
        this.sampleRate = 44100;
        this.numChannels = 1;
        this.turnOffMicTimeout = null;

        this._originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(
            navigator.mediaDevices
        );

        // --- BOT OUTPUT PEER CONNECTION SETUP ---
        this.botOutputPeerConnection = null;
        this.botOutputMediaStream = null;
        this.botOutputMediaStreamOutputDestination = null;
        this.botOutputMediaStreamIsReadyInterval = null;

        // Webcam video output stream
        this.webcamVideoOutputStream = new BotVideoOutputStream({
            turnOnInput: this.turnOnWebcam,
            turnOffInput: this.turnOffWebcam,
            ensureMicOn: () => this.ensureMicOn(),
            ensureMicOff: () => this.ensureMicOff(),
            getGainNode: () => this.gainNode,
            getAudioContext: () => this.audioContext,
            createSourceAudioTrack: () => this._createSourceAudioTrack(),
        });

        this.screenShareVideoOutputStream = new BotVideoOutputStream({
            turnOnInput: this.turnOnScreenshare,
            turnOffInput: this.turnOffScreenshare,
            ensureMicOn: () => this.ensureMicOn(),
            ensureMicOff: () => this.ensureMicOff(),
            getGainNode: () => this.gainNode,
            getAudioContext: () => this.audioContext,
            createSourceAudioTrack: () => this._createSourceAudioTrack(),
        });

        this._installGetUserMediaInterceptor();
        this._installGetDisplayMediaInterceptor();
    }

    _createSourceAudioTrack() {
        if (this.sourceAudioTrack) {
            return;
        }

        // --- AUDIO SOURCE SETUP (single source) ---
        this.audioContext = new AudioContext();
        this.gainNode = this.audioContext.createGain();
        this.audioDestination = this.audioContext.createMediaStreamDestination();

        this.gainNode.gain.value = 1.0;

        this.gainNode.connect(this.audioDestination);
        this.gainNode.connect(this.audioContext.destination); // This causes it to play through the speakers

        // This is our *source* audio track; we will CLONE it for callers.
        const audioTracks = this.audioDestination.stream.getAudioTracks();
        this.sourceAudioTrack = audioTracks[0] || null;
    }

    _installGetUserMediaInterceptor() {
        const self = this;

        navigator.mediaDevices.getUserMedia = async function interceptedGetUserMedia(
            constraints
        ) {
            const needAudio =
                !!(constraints && constraints.audio !== false && constraints.audio != null);
            const needVideo =
                !!(constraints && constraints.video !== false && constraints.video != null);

            // Edge-case: if nothing is requested, just delegate.
            if (!needAudio && !needVideo) {
                return self._originalGetUserMedia(constraints);
            }

            const stream = new MediaStream();

            if (needVideo && self.webcamVideoOutputStream.sourceVideoTrack) {
                // Clone from the source so app-level stop() doesn't kill our source. Otherwise this won't work in Teams.
                const videoClone = self.webcamVideoOutputStream.sourceVideoTrack.clone();
                stream.addTrack(videoClone);
            }

            if (needAudio) {
                // You need to initialize the source audio track here. It will play through the speakers if you initialize it in the constructor.
                self._createSourceAudioTrack();
                const audioClone = self.sourceAudioTrack.clone();
                stream.addTrack(audioClone);
            }

            return stream;
        };
    }

    _installGetDisplayMediaInterceptor() {
        const self = this;

        navigator.mediaDevices.getDisplayMedia = async function interceptedGetDisplayMedia(
            constraints
        ) {
            const needVideo =
                !!(constraints && constraints.video !== false && constraints.video != null);

            // Edge-case: if nothing is requested, just delegate.
            if (!needVideo) {
                return self._originalGetDisplayMedia(constraints);
            }

            const stream = new MediaStream();

            if (needVideo && self.screenShareVideoOutputStream.sourceVideoTrack) {
                // Clone from the source so app-level stop() doesn't kill our source. Otherwise this won't work in Teams.
                const videoClone = self.screenShareVideoOutputStream.sourceVideoTrack.clone();
                stream.addTrack(videoClone);
            }

            return stream;
        };
    }

    ensureMicOn() {
        try {
            this.turnOnMic && this.turnOnMic();
        } catch (e) {
            console.error("Error in turnOnMic callback:", e);
        }
    }

    disableMic() {
        try {
            this.turnOffMic && this.turnOffMic();
        } catch (e) {
            console.error("Error in turnOffMic callback:", e);
        }
    }

    async displayImage(imageBytes) {
        return this.webcamVideoOutputStream.displayImage(imageBytes);
    }

    isVideoPlaying() {
        return this.webcamVideoOutputStream.isVideoPlaying();
    }

    async playVideo(videoUrl) {
        return this.webcamVideoOutputStream.playVideo(videoUrl);
    }

    /**
     * Play raw PCM audio data through the virtual microphone.
     *
     * This version immediately enqueues chunks and lets a queue processor
     * build/schedule AudioBuffers, avoiding per-call scheduling jitter.
     *
     * @param {Int16Array|Float32Array|Array<number>|TypedArray} pcmData
     * @param {number} [sampleRate=44100]
     * @param {number} [numChannels=1]
     */
    async playPCMAudio(pcmData, sampleRate = 44100, numChannels = 1) {
        this._createSourceAudioTrack();
        this.ensureMicOn();

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

        const duration = audioData.length / (numChannels * sampleRate);

        this.audioQueue.push({
            data: audioData,
            duration,
        });

        // If we had a pending mic-off timer, cancel it – new audio is coming
        if (this.turnOffMicTimeout) {
            clearTimeout(this.turnOffMicTimeout);
            this.turnOffMicTimeout = null;
        }

        // Start processing if not already in progress
        if (!this.isPlayingAudioQueue) {
            this._processAudioQueue();
        }
    }

    _processAudioQueue() {
        if (this.audioQueue.length === 0) {
            this.isPlayingAudioQueue = false;
    
            // Delay turning off the mic by 2 seconds, only if queue stays empty
            if (this.turnOffMicTimeout) {
                clearTimeout(this.turnOffMicTimeout);
            }
            this.turnOffMicTimeout = setTimeout(() => {
                if (this.audioQueue.length === 0) {
                    this.disableMic();
                }
            }, 2000);
    
            return;
        }
    
        this.isPlayingAudioQueue = true;
    
        const currentTime = this.audioContext.currentTime;
        if (!this.nextPlayTime || this.nextPlayTime < currentTime) {
            // Catch up if we've fallen behind
            this.nextPlayTime = currentTime;
        }
    
        const { data, duration } = this.audioQueue.shift();
    
        const frames = data.length / this.numChannels;
        const audioBuffer = this.audioContext.createBuffer(
            this.numChannels,
            frames,
            this.sampleRate
        );
    
        if (this.numChannels === 1) {
            const channelData = audioBuffer.getChannelData(0);
            channelData.set(data);
        } else {
            for (let ch = 0; ch < this.numChannels; ch++) {
                const channelData = audioBuffer.getChannelData(ch);
                for (let i = 0; i < frames; i++) {
                    channelData[i] = data[i * this.numChannels + ch];
                }
            }
        }
    
        const source = this.audioContext.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(this.gainNode); // -> gain node -> mic track
    
        source.start(this.nextPlayTime);
        this.nextPlayTime += duration;
    
        // Schedule the next queue processing a bit before the scheduled end
        const timeUntilNextProcessMs =
            (this.nextPlayTime - currentTime) * 1000 * 0.8;
    
        setTimeout(
            () => this._processAudioQueue(),
            Math.max(0, timeUntilNextProcessMs)
        );
    }

    getAudioContextDestination() {
        return this.audioContext?.destination;
    }

    botOutputMediaStreamIsReady() {
        return this.botOutputMediaStream.getVideoTracks().length > 0 && this.botOutputMediaStream.getAudioTracks().length > 0;
    }

    async playBotOutputMediaStream(outputDestination) {
        this.botOutputMediaStreamOutputDestination = outputDestination;

        if (!this.botOutputMediaStreamIsReady()) {
            // Add interval to check if the bot output media stream is ready
            
            if (!this.botOutputMediaStreamIsReadyInterval)
                this.botOutputMediaStreamIsReadyInterval = setInterval(() => {
                    if (this.botOutputMediaStreamIsReady()) {
                        this.playBotOutputMediaStream(this.botOutputMediaStreamOutputDestination);
                    }
                }, 1000);
            return;
        }

        if (this.botOutputMediaStreamIsReadyInterval)
            clearInterval(this.botOutputMediaStreamIsReadyInterval);

        if (outputDestination === "screenshare") {
            return this.screenShareVideoOutputStream.playMediaStream(this.botOutputMediaStream);
        } else {
            return this.webcamVideoOutputStream.playMediaStream(this.botOutputMediaStream);
        }
    }

    async stopBotOutputMediaStream() {
        if (this.botOutputMediaStreamOutputDestination === "screenshare") {
            return this.screenShareVideoOutputStream.stopMediaStream();
        } else {
            return this.webcamVideoOutputStream.stopMediaStream();
        }
    }

    isReadyForWebpageStreamer() {
        return !!window.styleManager.getMeetingAudioStream();
    }

    async getBotOutputPeerConnectionOffer() {
        try
        {
            // 2) Create the RTCPeerConnection
            this.botOutputPeerConnection = new RTCPeerConnection();
        
            // 3) Receive the server's *video* and *audio*
            this.botOutputMediaStream = new MediaStream();
            this.botOutputPeerConnection.ontrack = (ev) => {
                this.botOutputMediaStream.addTrack(ev.track);
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