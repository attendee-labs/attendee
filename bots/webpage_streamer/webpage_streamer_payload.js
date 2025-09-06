(() => {
  // ---------- Config ----------
  const HUM_FREQ_HZ = 150;
  const HUM_GAIN    = 0.02;

  // ---------- Audio graph & virtual mic management ----------
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = new AC({ latencyHint: "interactive" });

  let currentCleanup = null;          // function to tear down current virtual source
  let currentVirtualTrack = null;     // MediaStreamTrack served as the "mic"

  const resumeCtx = () => { if (ctx.state === "suspended") ctx.resume().catch(()=>{}); };
  ["pointerdown","keydown","click","touchstart"].forEach(ev =>
    window.addEventListener(ev, resumeCtx, { capture: true })
  );

  function setVirtualMicTrackFromNodeChain(connectFn, label = "Virtual Microphone") {
    // Tear down old source
    if (typeof currentCleanup === "function") { try { currentCleanup(); } catch {} }
    currentCleanup = null;

    const dest = ctx.createMediaStreamDestination();
    const cleanup = connectFn(dest);

    const track = dest.stream.getAudioTracks()[0];
    try { Object.defineProperty(track, "label", { value: label, configurable: true }); } catch {}
    currentVirtualTrack = track;
    currentCleanup = () => {
      try { track.stop(); } catch {}
      try { cleanup && cleanup(); } catch {}
    };
    resumeCtx();
  }

  function setVirtualMicToHum() {
    setVirtualMicTrackFromNodeChain((dest) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = HUM_FREQ_HZ;
      gain.gain.value = HUM_GAIN;
      osc.connect(gain).connect(dest);
      try { osc.start(); } catch {}
      return () => { try { osc.stop(); } catch {} try { osc.disconnect(); gain.disconnect(); } catch {} };
    }, "Virtual Microphone (Hum)");
  }

  function setVirtualMicFromStream(stream) {
    // Bridge remote stream -> WebAudio -> MediaStreamDestination so it behaves like a local mic
    setVirtualMicTrackFromNodeChain((dest) => {
      const src = ctx.createMediaStreamSource(stream);
      const gain = ctx.createGain();
      gain.gain.value = 1.0;
      src.connect(gain).connect(dest);

      // If the remote audio track ends, fall back to hum
      const t = stream.getAudioTracks()[0];
      const onEnded = () => setVirtualMicToHum();
      if (t) t.addEventListener("ended", onEnded, { once: true });

      return () => {
        try { src.disconnect(); gain.disconnect(); } catch {}
        if (t) try { t.removeEventListener("ended", onEnded); } catch {}
      };
    }, "Virtual Microphone (Bridged)");
  }

  // Expose a tiny API so other code can update the virtual mic source
  window.__setVirtualMicFromStream = setVirtualMicFromStream;

  // Boot with hum so early calls to gUM succeed
  setVirtualMicToHum();

  // ---------- getUserMedia override (instance + prototype) ----------
  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia) return;

  const OrigProtoGUM = MediaDevices.prototype.getUserMedia;

  function wants(constraints) {
    return {
      audio: !!(constraints && (constraints.audio === true || typeof constraints.audio === "object")),
      video: !!(constraints && (constraints.video === true || typeof constraints.video === "object")),
    };
  }

  // Some apps pass picky constraints (deviceId, sampleRate). We ignore those for the virtual mic.
  function stripAudioConstraints(c) {
    if (c === true || c == null) return true;
    // keep it an object but drop properties we can't satisfy
    const out = { ...c };
    delete out.deviceId;
    delete out.sampleRate;
    delete out.channelCount;
    delete out.autoGainControl;
    delete out.echoCancellation;
    delete out.noiseSuppression;
    return out; // ignored anyway for our virtual source
  }

  async function humGUM(constraints = {}) {
    const need = wants(constraints);
    if (!need.audio) {
      // No audio requested -> pass through completely
      return OrigProtoGUM.call(md, constraints);
    }

    // Merge video (real) + audio (virtual)
    let out = new MediaStream();

    // Handle video if requested
    if (need.video) {
      const videoConstraints = constraints.video === undefined ? true : constraints.video;
      const realVideo = await OrigProtoGUM.call(md, { video: videoConstraints, audio: false });
      realVideo.getVideoTracks().forEach(t => out.addTrack(t));
    }

    // Provide the current virtual mic. Clone so if the app stops its track,
    // it won't kill the shared source.
    const virtual = currentVirtualTrack ? currentVirtualTrack.clone()
                                        : (setVirtualMicToHum(), currentVirtualTrack.clone());

    // Best-effort apply constraints (usually a no-op for virtual track)
    try {
      const audioConstraints = stripAudioConstraints(constraints.audio);
      if (audioConstraints && typeof audioConstraints === "object") {
        await virtual.applyConstraints(audioConstraints);
      }
    } catch {}

    out.addTrack(virtual);
    return out;
  }

  Object.defineProperty(MediaDevices.prototype, "getUserMedia", {
    value: humGUM, writable: true, configurable: true
  });
  navigator.mediaDevices.getUserMedia = humGUM.bind(navigator.mediaDevices);

  // Optional: legacy shims
  ["getUserMedia", "webkitGetUserMedia", "mozGetUserMedia"].forEach(fnName => {
    const legacy = navigator[fnName];
    if (typeof legacy === "function") {
      navigator[fnName] = function(constraints, success, failure) {
        navigator.mediaDevices.getUserMedia(constraints).then(success, failure);
      };
    } else {
      navigator[fnName] = function(constraints, success, failure) {
        navigator.mediaDevices.getUserMedia(constraints).then(success, failure);
      };
    }
  });

  // ---------- Your WebRTC fetcher: pipe remote audio into the virtual mic ----------
  async function startReceivingMeetingAudio() {
    const pc = new RTCPeerConnection();

    // Collect remote audio
    const ms = new MediaStream();
    pc.ontrack = (ev) => {
      ms.addTrack(ev.track);
      if (ms.getAudioTracks().length > 0) {
        // >>> This line makes the remote stream BECOME the microphone <<<
        window.__setVirtualMicFromStream(ms);
      }
    };

    pc.addTransceiver("audio", { direction: "recvonly" });

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const res = await fetch("http://localhost:8000/offer_meeting_audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
    });

    if (!res.ok) {
      const t = await res.text();
      alert("No upstream audio yet (or error): " + res.status + " " + t);
      return;
    }

    const answer = await res.json();
    await pc.setRemoteDescription(answer);
  }

  // Kick it off (adjust timing as you like)
  setTimeout(() => { startReceivingMeetingAudio(); }, 1000);
})();
