(() => {
  const ORIGINAL_GET_USER_MEDIA =
    navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

  const UPSTREAM_URL = "http://localhost:8000/offer_meeting_audio";

  let pc = null;
  let virtualAudioTrack = null;
  let virtualMicPromise = null;

  function showErrorOnDom(errorMsg) {
    const errorDiv = document.createElement('div');
    errorDiv.id = 'attendee-audio-error';
    errorDiv.textContent = errorMsg;
    Object.assign(errorDiv.style, {
      position: 'fixed',
      top: '20px',
      left: '50%',
      transform: 'translateX(-50%)',
      background: '#d32f2f',
      color: 'white',
      padding: '12px 24px',
      borderRadius: '4px',
      fontFamily: 'system-ui, sans-serif',
      fontSize: '14px',
      zIndex: '999999',
      boxShadow: '0 2px 8px rgba(0,0,0,0.3)'
    });
    document.body.appendChild(errorDiv);
  }

  async function ensureVirtualMicTrack() {
    if (virtualAudioTrack && virtualAudioTrack.readyState === "live") {
      return virtualAudioTrack;
    }
    if (virtualMicPromise) {
      return virtualMicPromise;
    }

    virtualMicPromise = (async () => {
      pc = new RTCPeerConnection();

      // We only receive audio from upstream
      pc.addTransceiver("audio", { direction: "recvonly" });

      const remoteAudioStream = await new Promise(async (resolve, reject) => {
        let resolved = false;
        
        // Set a timeout to alert if remote mediastream is not received
        const timeout = setTimeout(() => {
          if (!resolved) {
            resolved = true;
            const errorMsg = 'Failed to receive remote audio stream within 10 seconds';
            showErrorOnDom(errorMsg);
            reject(new Error(errorMsg));
          }
        }, 10000); // 10 second timeout

        pc.addEventListener("track", (event) => {
          if (resolved) return;
          if (event.track.kind === "audio") {
            resolved = true;
            clearTimeout(timeout); // Clear the timeout since we got the track
            const stream =
              event.streams && event.streams[0]
                ? event.streams[0]
                : new MediaStream([event.track]);
            resolve(stream);
          }
        });

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const res = await fetch(UPSTREAM_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
          }),
        });

        if (!res.ok) {
          const t = await res.text().catch(() => "");
          const errorMsg = "Upstream audio error: " + res.status + (t ? " " + t : "");
          showErrorOnDom(errorMsg);
          reject(new Error(errorMsg));
          return;
        }

        const answer = await res.json();
        await pc.setRemoteDescription(answer);
      });

      const tracks = remoteAudioStream.getAudioTracks();
      if (!tracks.length) {
        throw new Error("No audio track in remote upstream stream");
      }

      virtualAudioTrack = tracks[0];
      return virtualAudioTrack;
    })();

    try {
      const track = await virtualMicPromise;
      return track;
    } catch (e) {
      console.error("Failed to set up virtual mic:", e);
      virtualMicPromise = null;
      throw e;
    }
  }

  function parseConstraints(constraints) {
    // Normalize what the caller requested
    let wantAudio = false;
    let wantVideo = false;
    let rawConstraints = constraints;

    if (constraints === undefined) {
      // Default some apps rely on: audio only
      wantAudio = true;
      wantVideo = false;
      rawConstraints = { audio: true };
    } else if (typeof constraints === "boolean") {
      wantAudio = !!constraints;
      wantVideo = false;
      rawConstraints = { audio: constraints };
    } else if (typeof constraints === "object" && constraints !== null) {
      if ("audio" in constraints && constraints.audio !== false) {
        wantAudio = true;
      }
      if ("video" in constraints && constraints.video !== false) {
        wantVideo = true;
      }
    }

    return { wantAudio, wantVideo, rawConstraints };
  }

  navigator.mediaDevices.getUserMedia = async function interceptedGetUserMedia(
    constraints
  ) {
    const { wantAudio, wantVideo, rawConstraints } =
      parseConstraints(constraints);

    // If they didn't ask for audio, just pass through.
    if (!wantAudio) {
      return ORIGINAL_GET_USER_MEDIA(rawConstraints);
    }

    // Ensure our virtual mic is ready
    const upstreamTrack = await ensureVirtualMicTrack();

    // Build the stream we return to the page
    const outStream = new MediaStream();

    // Use a clone so the page calling stop() on its track
    // is less likely to interfere with our underlying source.
    const audioTrack =
      typeof upstreamTrack.clone === "function"
        ? upstreamTrack.clone()
        : upstreamTrack;
    outStream.addTrack(audioTrack);

    if (wantVideo) {
      // Ask the real getUserMedia for video only, no audio
      let videoOnlyConstraints;
      if (typeof rawConstraints === "object" && rawConstraints !== null) {
        videoOnlyConstraints = { ...rawConstraints, audio: false, video: rawConstraints.video || true };
      } else {
        videoOnlyConstraints = { video: true, audio: false };
      }

      const realVideoStream = await ORIGINAL_GET_USER_MEDIA(
        videoOnlyConstraints
      );
      realVideoStream.getVideoTracks().forEach((t) => outStream.addTrack(t));
    }

    return outStream;
  };

  // Add microphone audio playback one second after DOM loads
  window.addEventListener('DOMContentLoaded', () => {
    setTimeout(async () => {
      try {
        // Create and add audio element to the page
        const microphoneAudio = document.createElement('audio');
        microphoneAudio.id = 'microphoneAudioInjectedByAttendeeWebsiteStreamer';
        microphoneAudio.autoplay = true;
        microphoneAudio.muted = true;
        document.body.appendChild(microphoneAudio);
  
        // Get microphone stream
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  
        // Connect stream to audio element for playback
        microphoneAudio.srcObject = stream;
  
        // Attempt to play the audio
        microphoneAudio.play().then(() => {
          console.log('Microphone audio playing');
        }).catch(e => {
          console.error('Autoplay prevented by browser:', e);
          alert('Autoplay prevented by browser. Click to start audio.');
        });
      } catch (error) {
        console.error('Error setting up microphone audio:', error);
      }
    }, 1000); // Wait 1 second after DOM loads
  });
})();
