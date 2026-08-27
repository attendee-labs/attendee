(() => {
    if (window.LiveKitMediaStreamReceiver) {
      return;
    }
  
    const livekit = window.LivekitClient;
  
    if (!livekit?.Room || !livekit?.RoomEvent) {
      throw new Error(
        "LiveKit SDK is not loaded. Paste livekit-client.umd.min.js before this script."
      );
    }
  
    const { Room, RoomEvent } = livekit;
  
    const sendJson = (payload) => window.ws?.sendJson(payload);
  
    const describeParticipant = (participant) => ({
      identity: participant.identity,
      sid: participant.sid,
      name: participant.name,
      metadata: participant.metadata,
      attributes: participant.attributes ?? null,
      isLocal: participant.isLocal === true,
      publications: Array.from(participant.trackPublications.values()).map(
        (publication) => ({
          sid: publication.trackSid ?? publication.sid,
          kind: publication.kind,
          source: publication.source,
          muted: publication.isMuted,
          subscribed: publication.isSubscribed,
        })
      ),
    });
  
    /*
     * Wraps one LiveKit room connection and exposes the remote participant's
     * audio/video as a single MediaStream. Track arrival is observed through
     * an internal EventTarget so that waiting code can be written as a plain
     * async loop instead of subscription callbacks.
     */
    class LiveKitConnection {
      constructor(room, matchParticipantOnPublishOnBehalf) {
        this.room = room;
        this.stream = new MediaStream();
        this.selectedParticipantIdentity = null;
        this.matchParticipantOnPublishOnBehalf = matchParticipantOnPublishOnBehalf;
  
        // The output contains at most one audio and one video track.
        this.outputTrackByKind = new Map();
  
        this.trackEvents = new EventTarget();
  
        room.on(RoomEvent.TrackSubscribed, (track, publication, participant) =>
          this.addRemoteTrack(track, publication, participant)
        );
  
        room.on(RoomEvent.TrackUnsubscribed, (track, publication, participant) =>
          this.removeRemoteTrack(track, publication, participant)
        );
  
        room.on(RoomEvent.Disconnected, () => {
          console.info("[LiveKit receiver] Room disconnected");
        });
      }
  
      /*
       * A LiveKit agent publishing for someone else carries that person's
       * identity in "lk.publish_on_behalf" rather than in its own identity,
       * so which field identifies the participant depends on the room setup.
       */
      participantMatchKey(participant) {
        if (this.matchParticipantOnPublishOnBehalf) {
          return participant.attributes?.["lk.publish_on_behalf"] ?? null;
        }

        return participant.identity ?? null;
      }

      acceptsParticipant(participant) {
        const matchKey = this.participantMatchKey(participant);

        if (matchKey === null) {
          return false;
        }

        // When no identity was requested, latch onto the first publisher seen.
        if (this.selectedParticipantIdentity === null) {
          this.selectedParticipantIdentity = matchKey;
        }

        return matchKey === this.selectedParticipantIdentity;
      }
  
      addRemoteTrack(remoteTrack, publication, participant) {
        const mediaTrack = remoteTrack.mediaStreamTrack;
  
        const isUsableKind =
          mediaTrack?.kind === "audio" || mediaTrack?.kind === "video";
  
        if (!this.acceptsParticipant(participant) || !isUsableKind) {
          sendJson({
            type: "LiveKitTrackNotAccepted",
            participantIdentity: participant.identity,
            publicationSid: publication.trackSid ?? publication.sid,
            kind: mediaTrack?.kind ?? null,
            id: mediaTrack?.id ?? null,
          });
          return;
        }
  
        const previousTrack = this.outputTrackByKind.get(mediaTrack.kind);
  
        if (previousTrack === mediaTrack) {
          return;
        }
  
        // Replace the previous track of the same kind.
        if (previousTrack) {
          this.stream.removeTrack(previousTrack);
        }
  
        this.outputTrackByKind.set(mediaTrack.kind, mediaTrack);
        this.stream.addTrack(mediaTrack);
  
        console.info("[LiveKit receiver] Added track", {
          participantIdentity: participant.identity,
          publicationSid: publication.trackSid ?? publication.sid,
          kind: mediaTrack.kind,
          id: mediaTrack.id,
        });
  
        sendJson({
          type: "LiveKitTrackAdded",
          participantIdentity: participant.identity,
          publicationSid: publication.trackSid ?? publication.sid,
          kind: mediaTrack.kind,
          id: mediaTrack.id,
        });
  
        this.notifyTrackChange();
      }
  
      removeRemoteTrack(remoteTrack, publication, participant) {
        const mediaTrack = remoteTrack.mediaStreamTrack;
  
        if (!mediaTrack) {
          return;
        }
  
        if (this.outputTrackByKind.get(mediaTrack.kind) === mediaTrack) {
          this.outputTrackByKind.delete(mediaTrack.kind);
          this.stream.removeTrack(mediaTrack);
          this.notifyTrackChange();
        }
  
        console.info("[LiveKit receiver] Removed track", {
          participantIdentity: participant.identity,
          publicationSid: publication.trackSid ?? publication.sid,
          kind: mediaTrack.kind,
          id: mediaTrack.id,
        });
      }
  
      notifyTrackChange() {
        this.trackEvents.dispatchEvent(new Event("trackchange"));
      }
  
      hasLiveTracks(requiredKinds) {
        return requiredKinds.every((kind) =>
          this.stream
            .getTracks()
            .some((track) => track.kind === kind && track.readyState === "live")
        );
      }
  
      /*
       * Resolves on the next track change, or after timeoutMs — whichever
       * comes first. Never rejects; the caller re-checks the deadline.
       * MediaStream's own addtrack/removetrack events cannot be used here:
       * they only fire for user-agent-initiated changes, not for our own
       * addTrack()/removeTrack() calls.
       */
      nextTrackChangeOrTimeout(timeoutMs) {
        return new Promise((resolve) => {
          const settle = () => {
            this.trackEvents.removeEventListener("trackchange", settle);
            clearTimeout(timer);
            resolve();
          };
  
          const timer = setTimeout(settle, timeoutMs);
          this.trackEvents.addEventListener("trackchange", settle);
        });
      }
  
      async waitForTrackKinds(requiredKinds, timeoutMs) {
        const deadline = Date.now() + timeoutMs;
  
        while (!this.hasLiveTracks(requiredKinds)) {
          const remainingMs = deadline - Date.now();
  
          if (remainingMs <= 0) {
            const presentKinds =
              this.stream.getTracks().map((track) => track.kind).join(", ") ||
              "none";
  
            const message =
              `Timed out waiting for LiveKit tracks: ` +
              `${requiredKinds.join(", ")}. Present: ${presentKinds}`;
  
            sendJson({
              type: "LiveKitTrackWaitTimedOut",
              error: message,
              requiredKinds: requiredKinds,
              presentKinds: presentKinds,
              timeoutMs: timeoutMs,
            });
  
            throw new Error(message);
          }
  
          await this.nextTrackChangeOrTimeout(remainingMs);
        }
      }
  
      reportParticipants() {
        sendJson({
          type: "LiveKitRoomParticipants",
          roomName: this.room.name,
          localParticipant: this.room.localParticipant
            ? describeParticipant(this.room.localParticipant)
            : null,
          remoteParticipants: Array.from(
            this.room.remoteParticipants.values()
          ).map(describeParticipant),
        });
      }
  
      /*
       * Usually TrackSubscribed handles everything. Scan existing publications
       * too, in case subscription completed around the same time as
       * room.connect().
       */
      collectExistingTracks() {
        for (const participant of this.room.remoteParticipants.values()) {
          for (const publication of participant.trackPublications.values()) {
            if (publication.track) {
              this.addRemoteTrack(publication.track, publication, participant);
            }
          }
        }
      }
  
      async close() {
        // Remove the tracks from our wrapper MediaStream, but do not call
        // MediaStreamTrack.stop(). LiveKit owns the remote tracks.
        for (const track of this.stream.getTracks()) {
          this.stream.removeTrack(track);
        }
  
        try {
          await this.room.disconnect();
        } catch (error) {
          console.warn("[LiveKit receiver] Disconnect failed", error);
        }
      }
    }
  
    let activeConnection = null;
  
    async function disconnect() {
      const connection = activeConnection;
      activeConnection = null;
  
      window.__liveKitRoom = null;
      window.__liveKitMediaStream = null;
  
      if (connection) {
        await connection.close();
      }
    }
  
    async function connect({
      url,
      token,
  
      // Strongly recommended when the room can contain multiple publishers.
      // When omitted, the first remote publisher received is selected.
      participantIdentity = null,

      // Match participantIdentity against the publisher's
      // "lk.publish_on_behalf" attribute instead of its own identity.
      matchParticipantOnPublishOnBehalf = true,

      waitForAudio = true,
      waitForVideo = true,
      timeoutMs = 20_000,
    }) {
      if (typeof url !== "string" || !url) {
        throw new TypeError("url must be a non-empty LiveKit WebSocket URL");
      }
  
      if (typeof token !== "string" || !token) {
        throw new TypeError("token must be a non-empty participant token");
      }
  
      await disconnect();
  
      const room = new Room({
        /*
         * We consume RemoteTrack.mediaStreamTrack directly instead of
         * attaching video to an HTMLVideoElement. Adaptive stream is
         * therefore deliberately disabled.
         */
        adaptiveStream: false,
      });
  
      const connection = new LiveKitConnection(
        room,
        matchParticipantOnPublishOnBehalf
      );
      connection.selectedParticipantIdentity = participantIdentity;
  
      activeConnection = connection;
  
      // Expose these immediately for debugging and downstream use.
      window.__liveKitRoom = room;
      window.__liveKitMediaStream = connection.stream;
  
      try {
        await room.connect(url, token, { autoSubscribe: true });
  
        connection.reportParticipants();
        connection.collectExistingTracks();
  
        const requiredKinds = [
          ...(waitForAudio ? ["audio"] : []),
          ...(waitForVideo ? ["video"] : []),
        ];
  
        if (requiredKinds.length > 0) {
          await connection.waitForTrackKinds(requiredKinds, timeoutMs);
        }
  
        console.info("[LiveKit receiver] MediaStream ready", {
          roomName: room.name,
          selectedParticipantIdentity: connection.selectedParticipantIdentity,
          tracks: connection.stream.getTracks().map((track) => ({
            kind: track.kind,
            id: track.id,
            readyState: track.readyState,
            muted: track.muted,
          })),
        });
  
        return connection.stream;
      } catch (error) {
        console.error("[LiveKit receiver] Connection failed", error);
  
        sendJson({
          type: "LiveKitConnectionFailed",
          error: error.message,
        });
  
        await disconnect();
        throw error;
      }
    }
  
    window.LiveKitMediaStreamReceiver = Object.freeze({
      connect,
      disconnect,
  
      get room() {
        return activeConnection?.room ?? null;
      },
  
      get stream() {
        return activeConnection?.stream ?? null;
      },
  
      get selectedParticipantIdentity() {
        return activeConnection?.selectedParticipantIdentity ?? null;
      },
    });
  })();


  async function streamRoomSyncSourceParticipant() {
    const livekitConfig =
      window.initialData.roomSyncSourceParticipantConfiguration.livekit;

    // The source participant is identified by exactly one of `identity` or
    // `publish_on_behalf`. When `publish_on_behalf` is set we match against the
    // publisher's "lk.publish_on_behalf" attribute rather than its own identity.
    const matchParticipantOnPublishOnBehalf =
      livekitConfig.publish_on_behalf != null;
    const participantIdentity = matchParticipantOnPublishOnBehalf
      ? livekitConfig.publish_on_behalf
      : livekitConfig.identity;

    const mediaStream = await window.LiveKitMediaStreamReceiver.connect({
        url: `ws://localhost:${window.initialData.websocketPort}`,
        token: livekitConfig.token,
        participantIdentity: participantIdentity,
        matchParticipantOnPublishOnBehalf: matchParticipantOnPublishOnBehalf,
        // Audio-only participants are allowed; we handle video ourselves below.
        waitForVideo: false,
        });
    
    // Give a real video track a short grace period to arrive, then fall back
    // to a locally generated black video track so the combined stream always
    // has both audio and video.
    let videoTrack = await waitForVideoTrack(mediaStream, 5_000);
   
    if (!videoTrack) {
      videoTrack = createBlackVideoTrack();
      mediaStream.addTrack(videoTrack);
      console.log(
        "[LiveKit receiver] No remote video track; using black video track:",
        videoTrack.id
      );
      window.ws?.sendJson({
        type: 'LiveKitVideoTrackNotAvailable',
        trackId: videoTrack.id,
      });
    }
   
    // Route the LiveKit media stream through the bot's webcam instead of
    // rendering it into an embedded video element on the page.
    window.botOutputManager.setBotOutputMediaStream(mediaStream);
    await window.botOutputManager.playBotOutputMediaStream("webcam");
   
    console.log("LiveKit media stream routed to bot webcam:", mediaStream);
   
    // Polls the stream for a video track until timeoutMs elapses.
    // Resolves with the track, or null if none appeared in time.
    function waitForVideoTrack(stream, timeoutMs) {
      return new Promise((resolve) => {
        const deadline = Date.now() + timeoutMs;
   
        const poll = () => {
          const track = stream.getVideoTracks()[0];
   
          if (track) {
            resolve(track);
          } else if (Date.now() >= deadline) {
            resolve(null);
          } else {
            setTimeout(poll, 250);
          }
        };
   
        poll();
      });
    }
   
    // Creates a video track that continuously renders black frames.
    function createBlackVideoTrack(width = 1280, height = 720, fps = 5) {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
   
      const ctx = canvas.getContext("2d");
   
      const draw = () => {
        ctx.fillStyle = "#000000";
        ctx.fillRect(0, 0, width, height);
      };
   
      // Draw once immediately, then keep drawing so captureStream keeps
      // producing frames (some browsers stop emitting frames from a static
      // canvas otherwise).
      draw();
      setInterval(draw, 1000 / fps);
   
      return canvas.captureStream(fps).getVideoTracks()[0];
    }
  }

  window.streamRoomSyncSourceParticipant = streamRoomSyncSourceParticipant;