/**
 * Webex Bot Meeting Recorder with Multistream
 * Records individual participant audio streams and sends to WebSocket
 */

// ============================================================================
// CONFIGURATION
// ============================================================================
const BOT_NAME = "Wasel.ai"; // Change this to match your bot's display name

// ============================================================================
// GLOBALS
// ============================================================================
let webex;
let meeting;
let websocket = null;
let websocketWrapper = null; // WebSocket protocol wrapper
let isRecording = false;
let globalAudioMediaGroup;

// Video display tracking
const remoteVideos = new Map(); // Map<participantId, { tile, video, info, remoteMedia, memberName, memberId }>
const memberIdToRemoteMedia = new Map(); // memberId -> remoteMedia for video
let currentScreenShareRemoteMedia = null;
let currentLayoutId = "AllEqual";

// Audio recording tracking
const audioStreamRecorders = new Map(); // Map<streamId, { recorder, stream, csi, index }>
const streamIndexToMemberId = new Map(); // Map<streamIndex, memberId> - fallback correlation
let currentActiveSpeakers = new Set(); // Currently speaking memberIds
const memberIdToAudioCSI = new Map(); // Map<memberId, csi> - CSI correlation
let multistreamListenersSetup = false; // Prevent duplicate listener registration

// Enhanced correlation tracking
const streamSlotAudioActivity = new Map(); // Map<slotIndex, { hasActivity, lastActivityTime, amplitude, memberHistory, activityHistory }>
const memberToSlotHistory = new Map(); // Map<memberId, Map<slotIndex, { count, lastTime, confidence }>>
const slotToMemberMap = new Map(); // Map<slotIndex, { memberId, memberName, confidence, lastUpdated, method }>

// Legacy tracking (for backward compatibility)
const audioSlotToMemberId = new Map(); // Map<slotIndex, memberId>
const memberIdToSlotIndex = new Map(); // Map<memberId, slotIndex>
let nextAvailableSlot = 0;

// Transcription
let transcriptionEnabled = false;
const transcriptionToMemberId = new Map();
let lastTranscriptionPayload = null;

// UI tracking
const activeSpeakers = new Map(); // Map<memberId, { name, isSpeaking, lastSpokeAt }>

// Participant tracking for UsersUpdate
const participantsMap = new Map(); // Map<memberId, { uuid, name, is_host, is_self, status }>

// Meeting end deduplication flag - prevents sending multiple meeting_ended messages
let meetingEndedSent = false;
let botAdmittedToMeeting = false; // Tracks whether the bot was admitted into the meeting (past lobby)

// Utterance configuration
const UTTERANCE_CONFIG = {
  SILENCE_THRESHOLD_MS: 3000,
  MAX_UTTERANCE_DURATION_MS: 300000,
  SILENCE_DETECTION_INTERVAL_MS: 100,
};

const CORRELATION_CONFIG = {
  ACTIVITY_THRESHOLD: 5, // Audio amplitude threshold
  CORRELATION_DELAY_MS: 300, // Wait for audio to arrive after speaker event
  CONFIDENCE_THRESHOLD: 0.6, // Minimum confidence to use correlation
  SLOT_STICKY_DURATION_MS: 10000, // How long speakers tend to stay in same slot
  ACTIVITY_HISTORY_SIZE: 20, // Number of activity checks to keep
};

// ============================================================================
// CENTRALIZED MEETING END HANDLER
// ============================================================================

/**
 * Centralized handler for meeting end/leave events.
 * Ensures cleanup happens exactly once and sends exactly one meeting_ended
 * or removed_from_meeting message to the Python adapter via WebSocket.
 *
 * @param {string} reason - The reason for the meeting end (for logging)
 * @param {string} changeType - "meeting_ended" or "removed_from_meeting"
 */
function handleMeetingEnd(reason, changeType = "meeting_ended") {
  if (meetingEndedSent) {
    debugLog(`Meeting end already handled, ignoring duplicate (reason: ${reason}, type: ${changeType})`);
    return;
  }
  meetingEndedSent = true;

  debugLog(`🔴 Meeting ending: ${reason} (type: ${changeType})`);

  window.webexJoinStatus = {
    status: changeType === "removed_from_meeting" ? "REMOVED" : "MEETING_ENDED",
    message: reason,
    type: "info",
    code: changeType === "removed_from_meeting" ? "REMOVED" : "MEETING_STOPPED",
  };

  // Step 1: Stop all audio stream recorders
  audioStreamRecorders.forEach((recorderData, streamId) => {
    stopStreamRecording(streamId);
  });
  audioStreamRecorders.clear();

  // Step 2: Disable media sending
  if (websocketWrapper) {
    websocketWrapper.disableMediaSending();
  }

  // Step 3: Notify Python adapter
  if (websocketWrapper) {
    websocketWrapper.sendMeetingStatusChange(changeType);
    debugLog(`📤 Sent ${changeType} to Python adapter`);
  }

  // Step 4: Reset UI (only if elements exist - in headless mode they may not)
  if (participantsGrid) participantsGrid.innerHTML = "";
  if (meetingScreen) meetingScreen.classList.remove("active");
  if (loginScreen) loginScreen.style.display = "flex";
  if (btnJoin) {
    btnJoin.disabled = false;
    btnJoin.textContent = "Join Meeting";
  }
}

// ============================================================================
// ENHANCED AUDIO ACTIVITY TRACKING & CORRELATION (from recording.js)
// ============================================================================

/**
 * Initialize audio activity tracking for all stream slots
 */
function initializeStreamActivityTracking() {
  for (let i = 0; i < 3; i++) {
    streamSlotAudioActivity.set(i, {
      hasActivity: false,
      lastActivityTime: null,
      amplitude: 0,
      memberHistory: [], // Track which members were in this slot over time
      activityHistory: [], // Track amplitude over time
    });
  }
}

/**
 * Update audio activity for a specific stream slot
 */
function updateStreamActivity(slotIndex, amplitude) {
  const activity = streamSlotAudioActivity.get(slotIndex);
  if (!activity) return;

  const hasActivity = amplitude > CORRELATION_CONFIG.ACTIVITY_THRESHOLD;

  activity.amplitude = amplitude;
  activity.hasActivity = hasActivity;

  if (hasActivity) {
    activity.lastActivityTime = Date.now();
  }

  // Keep activity history
  activity.activityHistory.push({
    timestamp: Date.now(),
    amplitude: amplitude,
    hasActivity: hasActivity,
  });

  // Limit history size
  if (
    activity.activityHistory.length > CORRELATION_CONFIG.ACTIVITY_HISTORY_SIZE
  ) {
    activity.activityHistory.shift();
  }

  streamSlotAudioActivity.set(slotIndex, activity);
}

/**
 * Get currently active stream slots (those with audio)
 */
function getActiveSlotsWithAudio() {
  const activeSlots = new Set();
  const now = Date.now();

  streamSlotAudioActivity.forEach((activity, slotIndex) => {
    // Consider slot active if it had audio in the last 500ms
    if (activity.lastActivityTime && now - activity.lastActivityTime < 500) {
      activeSlots.add(slotIndex);
    }
  });

  return activeSlots;
}

/**
 * Update the mapping of a slot to a member with confidence tracking
 */
function updateSlotToMemberMapping(
  slotIndex,
  memberId,
  memberName,
  confidence,
  method,
) {
  const timestamp = Date.now();

  slotToMemberMap.set(slotIndex, {
    memberId,
    memberName,
    confidence,
    lastUpdated: timestamp,
    method,
  });

  // Update member history for this slot
  const activity = streamSlotAudioActivity.get(slotIndex);
  if (activity) {
    activity.memberHistory.push({
      memberId,
      memberName,
      timestamp,
      confidence,
    });
    if (activity.memberHistory.length > 10) {
      activity.memberHistory.shift();
    }
  }

  // Update member-to-slot history
  if (!memberToSlotHistory.has(memberId)) {
    memberToSlotHistory.set(memberId, new Map());
  }
  const slotHistory = memberToSlotHistory.get(memberId);
  const existing = slotHistory.get(slotIndex) || {
    count: 0,
    lastTime: 0,
    confidence: 0,
  };
  slotHistory.set(slotIndex, {
    count: existing.count + 1,
    lastTime: timestamp,
    confidence: Math.max(existing.confidence, confidence),
  });
}

/**
 * Get historical score for a member-slot pair
 */
function getMemberSlotHistoryScore(memberId, slotIndex) {
  const history = memberToSlotHistory.get(memberId);
  if (!history) return 0;

  const slotData = history.get(slotIndex);
  if (!slotData) return 0;

  const timeSinceLast = Date.now() - slotData.lastTime;
  const recency = Math.max(
    0,
    1 - timeSinceLast / CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS,
  );
  const frequency = Math.min(1, slotData.count / 5); // Normalize to 5 assignments

  return slotData.confidence * 0.5 + recency * 0.3 + frequency * 0.2;
}

/**
 * Get most likely slot for a member based on history
 */
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

/**
 * Get recent activity score for a slot
 */
function getRecentSlotActivityScore(slotIndex) {
  const activity = streamSlotAudioActivity.get(slotIndex);
  if (!activity || !activity.activityHistory.length) return 0;

  const recentHistory = activity.activityHistory.slice(-5); // Last 5 checks
  const activeCount = recentHistory.filter((h) => h.hasActivity).length;
  return activeCount / recentHistory.length;
}

/**
 * Find an available slot, avoiding already assigned slots
 */
function findAvailableSlot(assignedSlots = new Set()) {
  for (let i = 0; i < 3; i++) {
    if (!assignedSlots.has(i)) {
      const mapping = slotToMemberMap.get(i);
      if (
        !mapping ||
        Date.now() - mapping.lastUpdated >
        CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS
      ) {
        return i;
      }
    }
  }
  // All slots taken, return least recently updated
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

/**
 * Get member info for slot with confidence check
 */
function getMemberForSlot(slotIndex) {
  const mapping = slotToMemberMap.get(slotIndex);
  if (!mapping) {
    // Fallback to legacy mapping
    const fallbackMemberId = audioSlotToMemberId.get(slotIndex);
    if (fallbackMemberId) {
      const member = getMemberById(fallbackMemberId);
      return {
        memberId: fallbackMemberId,
        memberName: member?.name || "Unknown",
        confidence: 0.5,
        lastUpdated: Date.now(),
        method: "fallback_legacy",
      };
    }
    return null;
  }

  // Check if mapping is stale (>20 seconds)
  const age = Date.now() - mapping.lastUpdated;
  if (age > CORRELATION_CONFIG.SLOT_STICKY_DURATION_MS * 2) {
    return mapping; // Still return it as fallback
  }

  return mapping;
}

/**
 * Correlate active speakers to audio stream slots using multiple signals
 */
function correlateActiveSpeakersToSlots(memberIds) {
  if (!memberIds || memberIds.length === 0) return;

  const activeSlotsWithAudio = getActiveSlotsWithAudio();
  const timestamp = Date.now();

  // CASE 1: Single speaker - Simple case
  if (memberIds.length === 1) {
    const memberId = memberIds[0];
    const member = getMemberById(memberId);
    const memberName = member?.name || "Unknown";

    // Find which slot has audio
    if (activeSlotsWithAudio.size === 1) {
      const [slotIndex] = activeSlotsWithAudio;
      updateSlotToMemberMapping(
        slotIndex,
        memberId,
        memberName,
        0.95,
        "single_speaker_single_slot",
      );
      streamIndexToMemberId.set(slotIndex, memberId);
      return;
    }

    // Check history
    const historicalSlot = getMostLikelySlotForMember(memberId);
    if (historicalSlot !== null) {
      updateSlotToMemberMapping(
        historicalSlot,
        memberId,
        memberName,
        0.7,
        "single_speaker_historical",
      );
      streamIndexToMemberId.set(historicalSlot, memberId);
      return;
    }

    // Fallback: assign to first available slot
    const firstAvailableSlot = findAvailableSlot();
    updateSlotToMemberMapping(
      firstAvailableSlot,
      memberId,
      memberName,
      0.5,
      "single_speaker_fallback",
    );
    streamIndexToMemberId.set(firstAvailableSlot, memberId);
    return;
  }

  // CASE 2: Multiple speakers - Complex correlation
  const correlationScores = new Map();

  memberIds.forEach((memberId) => {
    const member = getMemberById(memberId);
    const scores = new Map();

    // Score each slot for this member
    for (let slotIndex = 0; slotIndex < 3; slotIndex++) {
      let score = 0;

      // Factor 1: Current audio activity in slot (40%)
      if (activeSlotsWithAudio.has(slotIndex)) {
        const activity = streamSlotAudioActivity.get(slotIndex);
        score += 0.4 * (activity.amplitude / 100);
      }

      // Factor 2: Historical assignment (40%)
      const historicalScore = getMemberSlotHistoryScore(memberId, slotIndex);
      score += 0.4 * historicalScore;

      // Factor 3: Recent activity in slot (20%)
      const recentScore = getRecentSlotActivityScore(slotIndex);
      score += 0.2 * recentScore;

      // Penalty: If slot is already strongly assigned to someone else
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

  // Greedy assignment
  const assignedSlots = new Set();
  const assignedMembers = new Set();

  // Sort all possible (member, slot) pairs by score
  const allPairs = [];
  correlationScores.forEach((scores, memberId) => {
    scores.forEach((score, slotIndex) => {
      allPairs.push({ memberId, slotIndex, score });
    });
  });
  allPairs.sort((a, b) => b.score - a.score);

  // Assign greedily
  for (const pair of allPairs) {
    if (
      assignedSlots.has(pair.slotIndex) ||
      assignedMembers.has(pair.memberId)
    ) {
      continue;
    }

    const member = getMemberById(pair.memberId);
    const memberName = member?.name || "Unknown";
    const confidence = Math.min(pair.score, 1.0);

    if (confidence >= CORRELATION_CONFIG.CONFIDENCE_THRESHOLD) {
      updateSlotToMemberMapping(
        pair.slotIndex,
        pair.memberId,
        memberName,
        confidence,
        "multi_speaker_scored",
      );
      streamIndexToMemberId.set(pair.slotIndex, pair.memberId);
      assignedSlots.add(pair.slotIndex);
      assignedMembers.add(pair.memberId);
    }
  }

  // Handle any unassigned speakers
  memberIds.forEach((memberId) => {
    if (!assignedMembers.has(memberId)) {
      const member = getMemberById(memberId);
      const memberName = member?.name || "Unknown";
      const fallbackSlot = findAvailableSlot(assignedSlots);
      updateSlotToMemberMapping(
        fallbackSlot,
        memberId,
        memberName,
        0.4,
        "multi_speaker_fallback",
      );
      streamIndexToMemberId.set(fallbackSlot, memberId);
      assignedSlots.add(fallbackSlot);
    }
  });
}

// ============================================================================
// DOM ELEMENTS
// ============================================================================
const loginScreen = document.getElementById("login-screen");
const meetingScreen = document.getElementById("meeting-screen");
const accessTokenElm = document.getElementById("access-token");
const meetingDestinationElm = document.getElementById("meeting-destination");
const meetingPasswordElm = document.getElementById("meeting-password");
const websocketUrlElm = document.getElementById("websocket-url");
const btnJoin = document.getElementById("btn-join");
const btnLeave = document.getElementById("btn-leave");
const btnMute = document.getElementById("btn-mute");
const btnVideo = document.getElementById("btn-video");
const btnToggleDebug = document.getElementById("btn-toggle-debug");
const loginStatus = document.getElementById("login-status");
const participantsGrid = document.getElementById("participants-grid");
const screenshareView = document.getElementById("screenshare-view");
const screenshareVideo = document.getElementById("screenshare-video");
const speakersList = document.getElementById("speakers-list");
const debugPanel = document.getElementById("debug-panel");
const debugLogElm = document.getElementById("debug-log");
const meetingTitle = document.getElementById("meeting-title");
const participantCount = document.getElementById("participant-count");
const recordingIndicator = document.getElementById("recording-indicator");

// ============================================================================
// WEBSOCKET PROTOCOL WRAPPER
// ============================================================================
class WebSocketProtocol {
  static MESSAGE_TYPES = {
    JSON: 1,
    VIDEO: 2,
    AUDIO: 3,
    ENCODED_MP4_CHUNK: 4,
    PER_PARTICIPANT_AUDIO: 5,
  };

  constructor(websocket) {
    this.ws = websocket;
    this.mediaSendingEnabled = false;
  }

  // Expose readyState for Python adapter compatibility checks
  get readyState() {
    return this.ws ? this.ws.readyState : WebSocket.CLOSED;
  }

  enableMediaSending() {
    this.mediaSendingEnabled = true;
    debugLog("✅ Media sending enabled");
  }

  disableMediaSending() {
    this.mediaSendingEnabled = false;
    debugLog("⚠️ Media sending disabled");

    // Stop all audio stream recordings
    audioStreamRecorders.forEach((recorderData, streamId) => {
      stopStreamRecording(streamId);
    });
    audioStreamRecorders.clear();
  }

  sendJson(data) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      debugLog("❌ Cannot send JSON - WebSocket not connected");
      return;
    }

    try {
      const jsonString = JSON.stringify(data);
      const jsonBytes = new TextEncoder().encode(jsonString);
      const message = new Uint8Array(4 + jsonBytes.length);
      new DataView(message.buffer).setInt32(0, WebSocketProtocol.MESSAGE_TYPES.JSON, true);
      message.set(jsonBytes, 4);
      this.ws.send(message.buffer);
    } catch (error) {
      debugLog("❌ Error sending JSON via WebSocket", { error: error.message });
    }
  }

  sendMeetingStatusChange(change) {
    this.sendJson({
      type: "MeetingStatusChange",
      change: change,
    });
    debugLog(`📤 Sent MeetingStatusChange: ${change}`);
  }

  sendUsersUpdate(newUsers = [], removedUsers = [], updatedUsers = []) {
    if (!this.mediaSendingEnabled) return;

    this.sendJson({
      type: "UsersUpdate",
      newUsers: newUsers,
      removedUsers: removedUsers,
      updatedUsers: updatedUsers,
    });
  }

  sendPerParticipantAudio(participantId, audioData) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      debugLog("❌ Cannot send per-participant audio - WebSocket not connected");
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
      const message = new Uint8Array(4 + 1 + participantIdBytes.length + audioData.byteLength);
      const dataView = new DataView(message.buffer);

      // Set message type (5 for PER_PARTICIPANT_AUDIO)
      dataView.setInt32(0, WebSocketProtocol.MESSAGE_TYPES.PER_PARTICIPANT_AUDIO, true);

      // Set participantId length as uint8 (1 byte)
      dataView.setUint8(4, participantIdBytes.length);

      // Copy participantId bytes
      message.set(participantIdBytes, 5);

      // Copy audio data after type, length and participantId
      message.set(new Uint8Array(audioData), 5 + participantIdBytes.length);

      // Send the binary message
      this.ws.send(message.buffer);
    } catch (error) {
      debugLog("❌ Error sending per-participant audio", { error: error.message });
    }
  }

  sendCaptionUpdate(caption) {
    if (!this.mediaSendingEnabled) return;

    this.sendJson({
      type: "CaptionUpdate",
      caption: caption,
    });
  }

  sendError(errorMessage, details = {}) {
    this.sendJson({
      type: "Error",
      message: errorMessage,
      ...details,
    });
  }

  sendSilenceStatus(isSilent) {
    if (!this.mediaSendingEnabled) return;

    this.sendJson({
      type: "SilenceStatus",
      isSilent: isSilent,
    });
  }

  sendChatStatusChange(change) {
    this.sendJson({
      type: "ChatStatusChange",
      change: change,
    });
  }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================
function showLoginStatus(message, type = "info") {
  if (loginStatus) {
    loginStatus.textContent = message;
    loginStatus.className = `status-msg ${type} show`;
  }
  debugLog(`[${type.toUpperCase()}] ${message}`);
}

function debugLog(message, data = null) {
  const timestamp = new Date().toLocaleTimeString();

  let logText = `[${timestamp}] ${message}`;
  if (data) {
    logText += ` | ${JSON.stringify(data, null, 2)}`;
  }

  // Update DOM debug panel if available (may not exist in headless mode)
  if (debugLogElm) {
    const entry = document.createElement("div");
    entry.className = "debug-entry";
    entry.textContent = logText;

    if (
      message.toLowerCase().includes("error") ||
      message.toLowerCase().includes("failed")
    ) {
      entry.classList.add("error");
    } else if (
      message.toLowerCase().includes("success") ||
      message.toLowerCase().includes("joined")
    ) {
      entry.classList.add("success");
    }

    debugLogElm.insertBefore(entry, debugLogElm.firstChild);

    // Keep only last 100 entries
    while (debugLogElm.children.length > 100) {
      debugLogElm.removeChild(debugLogElm.lastChild);
    }
  }

  console.log(logText, data || "");
}

function updateParticipantCount() {
  const count = remoteVideos.size;
  if (participantCount) participantCount.textContent = `${count} participant${count !== 1 ? "s" : ""}`;
  if (participantsGrid) participantsGrid.setAttribute("data-count", Math.min(count, 9));
}

function connectWebSocket() {
  const wsUrl = (websocketUrlElm && websocketUrlElm.value) ? websocketUrlElm.value.trim() : "";
  if (!wsUrl) {
    debugLog("⚠️ No WebSocket URL provided, recording locally only");
    return;
  }

  try {
    debugLog("🔌 Connecting to WebSocket...", { url: wsUrl });
    websocket = new WebSocket(wsUrl);
    websocketWrapper = new WebSocketProtocol(websocket);

    // CRITICAL: Expose websocketWrapper globally as window.ws
    // The Python WebBotAdapter framework calls:
    //   window.ws?.enableMediaSending()
    //   window.ws?.disableMediaSending()
    window.ws = websocketWrapper;

    websocket.onopen = () => {
      debugLog("✅ WebSocket CONNECTED and READY", {
        readyState: websocket.readyState,
        url: wsUrl,
      });
      isRecording = true;
      if (recordingIndicator) {
        recordingIndicator.classList.add("active");
      }

      // Enable media sending once connected
      websocketWrapper.enableMediaSending();
    };

    websocket.onerror = (error) => {
      debugLog("❌ WebSocket error", { error: error.message || "Unknown error" });
    };

    websocket.onclose = () => {
      debugLog("🔌 WebSocket disconnected");
      isRecording = false;
      if (recordingIndicator) {
        recordingIndicator.classList.remove("active");
      }
      websocket = null;
      websocketWrapper = null;
      window.ws = null;
    };

    websocket.onmessage = (event) => {
      debugLog("📨 WebSocket message received", { data: event.data });
    };
  } catch (error) {
    debugLog("❌ Failed to connect WebSocket", { error: error.message });
  }
}

async function initializeWebex() {
  const token = (accessTokenElm && accessTokenElm.value) ? accessTokenElm.value.trim() : "";

  if (!token) {
    showLoginStatus("Please enter an access token", "error");
    throw new Error("No access token provided");
  }

  try {
    showLoginStatus("Initializing Webex SDK...", "info");
    debugLog("Initializing Webex SDK");

    webex = window.Webex.init({
      credentials: {
        access_token: token,
      },
      config: {
        logger: {
          level: "info",
        },
        meetings: {
          reconnection: {
            enabled: true,
          },
          enableRtx: true,
          experimental: {
            enableUnifiedMeetings: true,
            enableMultistream: true,
          },
        },
      },
    });

    await new Promise((resolve, reject) => {
      webex.once("ready", resolve);
      setTimeout(() => reject(new Error("SDK initialization timeout")), 15000);
    });

    debugLog("Webex SDK ready");

    // Register with meetings service
    await webex.meetings.register();
    debugLog("Registered with Webex meetings service");

    // Listen for meeting:removed at the SDK level - this fires when the meeting
    // object is completely removed from the SDK's internal collection (e.g., host
    // ends the meeting, or the meeting is cleaned up). This is a critical backup
    // detection mechanism that works even if meeting-instance events don't fire.
    webex.meetings.on("meeting:removed", (removedMeeting) => {
      debugLog("⚠️ meeting:removed event from webex.meetings", {
        removedMeetingId: removedMeeting?.id,
        currentMeetingId: meeting?.id,
      });
      // Only handle if this is our current meeting
      if (meeting && removedMeeting && removedMeeting.id === meeting.id) {
        handleMeetingEnd("Meeting removed from SDK (host ended or meeting cleaned up)", "meeting_ended");
        meeting = null;
      }
    });

    showLoginStatus("SDK initialized successfully!", "success");
    return true;
  } catch (error) {
    debugLog("Failed to initialize Webex SDK", { error: error.message });
    showLoginStatus("Failed to initialize: " + error.message, "error");
    throw error;
  }
}

function createParticipantTile(participantId, memberName, remoteMedia) {
  const tile = document.createElement("div");
  tile.className = "participant-tile";
  tile.id = `participant-${participantId}`;
  tile.setAttribute("data-no-video", "true");
  tile.setAttribute("data-member-id", participantId);

  // Video element
  const video = document.createElement("video");
  video.autoplay = true;
  video.playsInline = true;
  video.muted = true;

  // No video placeholder
  const placeholder = document.createElement("div");
  placeholder.className = "no-video-placeholder";

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = (memberName || "U").charAt(0).toUpperCase();

  placeholder.appendChild(avatar);

  // Participant info overlay
  const info = document.createElement("div");
  info.className = "participant-info";
  info.innerHTML = `
        <svg class="mic-icon mic-active" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
            <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
        </svg>
        <span class="participant-name">${memberName || "Unknown"}</span>
    `;

  tile.appendChild(video);
  tile.appendChild(placeholder);
  tile.appendChild(info);

  return { tile, video, info };
}

function addParticipant(participantId, memberName, remoteMedia) {
  if (remoteVideos.has(participantId)) {
    return; // Already exists
  }

  const { tile, video, info } = createParticipantTile(
    participantId,
    memberName,
    remoteMedia,
  );
  participantsGrid.appendChild(tile);

  remoteVideos.set(participantId, {
    tile,
    video,
    info,
    remoteMedia,
    memberName,
    memberId: participantId,
  });

  updateParticipantCount();
  debugLog(`Added participant: ${memberName}`, { participantId });
}

function removeParticipant(participantId) {
  const participant = remoteVideos.get(participantId);
  if (!participant) return;

  participant.tile.remove();
  remoteVideos.delete(participantId);

  updateParticipantCount();
  debugLog(`Removed participant`, { participantId });
}

function updateParticipantVideo(participantId, stream, sourceState) {
  const participant = remoteVideos.get(participantId);
  if (!participant) return;

  if (sourceState === "live" && stream) {
    participant.srcObject = stream;
    participant.tile.setAttribute("data-no-video", "false");
  } else {
    participant.video.srcObject = null;
    participant.tile.setAttribute("data-no-video", "true");
  }
}

function assignMemberToAudioSlot(memberId, memberName) {
  // Check if member already has a slot
  if (memberIdToSlotIndex.has(memberId)) {
    return memberIdToSlotIndex.get(memberId);
  }

  // Find an available slot (0, 1, or 2)
  let assignedSlot = null;

  // First, try to find an empty slot
  for (let slot = 0; slot < 3; slot++) {
    if (!audioSlotToMemberId.has(slot)) {
      assignedSlot = slot;
      break;
    }
  }

  // If no empty slot, check if any assigned members are no longer speaking
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

  // If still no slot, use round-robin
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

function setupMultistreamListeners() {
  // Prevent duplicate listener registration
  if (multistreamListenersSetup) {
    debugLog(
      "⚠️ Multistream listeners already set up, skipping duplicate registration",
    );
    return;
  }
  multistreamListenersSetup = true;
  debugLog("🎯 Setting up multistream listeners");

  // Initialize enhanced correlation tracking
  initializeStreamActivityTracking();

  // Remote Audio Streams (for recording)
  meeting.on("media:remoteAudio:created", (audioMediaGroup) => {
    debugLog("🎧 Remote audio created", {
      count: audioMediaGroup.getRemoteMedia().length,
    });

    globalAudioMediaGroup = audioMediaGroup;
    const remoteMediaArray = audioMediaGroup.getRemoteMedia();

    // Immediately start recording all audio streams
    remoteMediaArray.forEach((remoteMedia, index) => {
      const csi = remoteMedia.csi;

      debugLog(`🔧 Setting up audio slot ${index}`, {
        csi: csi !== undefined && csi !== null ? csi : "undefined",
        memberId: remoteMedia.memberId || "N/A",
        sourceState: remoteMedia.sourceState,
        hasStream: !!remoteMedia.stream,
      });

      // Try to map CSI to member immediately if available
      if (csi !== undefined && csi !== null) {
        try {
          const member = meeting.members.findMemberByCsi(csi);
          if (member && member.id) {
            memberIdToAudioCSI.set(member.id, csi);
            debugLog("✅ Initial CSI mapping", {
              csi,
              memberId: member.id,
              memberName: member.name,
              streamIndex: index,
            });
          }
        } catch (error) {
          debugLog("⚠️ Could not map CSI initially", {
            csi,
            error: error.message,
          });
        }
      }

      // Attach to HTML audio element for playback
      const audioElement = document.getElementsByClassName(
        "multistream-remote-audio",
      )[index];
      if (audioElement && remoteMedia.stream) {
        audioElement.srcObject = remoteMedia.stream;
      }

      // Start recording immediately if stream is available
      if (remoteMedia.stream) {
        startStreamRecording(
          remoteMedia.stream,
          remoteMedia.csi,
          index,
          remoteMedia,
        );
      }

      // Listen for sourceUpdate in case stream becomes available later or CSI changes
      remoteMedia.on("sourceUpdate", (data) => {
        const updatedCsi = remoteMedia.csi;

        debugLog("🔄 Audio source updated", {
          streamIndex: index,
          csi:
            updatedCsi !== undefined && updatedCsi !== null
              ? updatedCsi
              : "undefined",
          state: data?.state || remoteMedia.sourceState,
          hasStream: !!remoteMedia.stream,
        });

        // Update CSI mapping if CSI is now available
        if (updatedCsi !== undefined && updatedCsi !== null) {
          try {
            const member = meeting.members.findMemberByCsi(updatedCsi);
            if (member && member.id) {
              memberIdToAudioCSI.set(member.id, updatedCsi);
              debugLog("✅ Updated CSI mapping from sourceUpdate", {
                csi: updatedCsi,
                memberId: member.id,
                memberName: member.name,
              });
            }
          } catch (error) {
            debugLog("⚠️ Could not map updated CSI", { csi: updatedCsi });
          }
        }

        // Update HTML audio element
        const audioElement = document.getElementsByClassName(
          "multistream-remote-audio",
        )[index];
        if (audioElement && remoteMedia.stream) {
          audioElement.srcObject = remoteMedia.stream;
        }

        // If stream is available and we're not recording it yet, start recording
        if (remoteMedia.stream) {
          const streamId = remoteMedia.stream.id;
          if (!audioStreamRecorders.has(streamId)) {
            debugLog("🎙️ Starting recording from sourceUpdate", {
              csi: updatedCsi,
              state: data?.state,
            });
            startStreamRecording(
              remoteMedia.stream,
              updatedCsi,
              index,
              remoteMedia,
            );
          }
        }

        // Don't stop on 'no source' - keep recording if audio track is still active
      });

      // Only stop when the remoteMedia is completely stopped
      remoteMedia.on("stopped", () => {
        debugLog("🛑 RemoteMedia stopped, stopping recording", {
          csi: remoteMedia.csi,
          streamIndex: index,
        });
        stopStreamRecording(remoteMedia.stream?.id);
      });
    });

    debugLog("✅ Audio recording initialized for all streams");
  });

  // Remote Video Layout Changes (for video display)
  meeting.on(
    "media:remoteVideo:layoutChanged",
    ({
      layoutId,
      activeSpeakerVideoPanes,
      memberVideoPanes,
      screenShareVideo,
    }) => {
      currentLayoutId = layoutId || currentLayoutId;
      debugLog("Video layout changed", {
        layoutId: currentLayoutId,
        activeSpeakers: Object.keys(activeSpeakerVideoPanes || {}).length,
        memberPanes: Object.keys(memberVideoPanes || {}).length,
        hasScreenShare: !!screenShareVideo,
      });

      for (const [groupId, group] of Object.entries(activeSpeakerVideoPanes)) {
        group
          .getRemoteMedia()
          .forEach((remoteMedia, index) =>
            processNewVideoPane(meeting, groupId, index, remoteMedia),
          );
      }
      // Unsubscribe from previous screen share RemoteMedia
      if (currentScreenShareRemoteMedia) {
        try {
          //currentScreenShareRemoteMedia.off('sourceUpdate');
          //currentScreenShareRemoteMedia.off('stopped');
        } catch (e) {
          /* no-op */
        }
        currentScreenShareRemoteMedia = null;
      }

      function updateScreenShareView(remoteMedia) {
        if (!remoteMedia) {
          screenshareVideo.srcObject = null;
          screenshareView.classList.remove("active");
          return;
        }
        if (remoteMedia.sourceState === "live" && remoteMedia.stream) {
          screenshareVideo.srcObject = remoteMedia.stream;
          screenshareView.classList.add("active");
          debugLog("Screen share active");
        } else {
          screenshareVideo.srcObject = null;
          screenshareView.classList.remove("active");
        }
      }

      // Handle screen share (RemoteMedia object – listen for sourceUpdate/stopped)
      if (screenShareVideo && typeof screenShareVideo.on === "function") {
        currentScreenShareRemoteMedia = screenShareVideo;
        updateScreenShareView(screenShareVideo);
        screenShareVideo.on("sourceUpdate", () => {
          debugLog("Screen share source updated", {
            sourceState: screenShareVideo.sourceState,
          });
          updateScreenShareView(screenShareVideo);
        });
        screenShareVideo.on("stopped", () => {
          debugLog("Screen share stopped");
          currentScreenShareRemoteMedia = null;
          screenshareVideo.srcObject = null;
          screenshareView.classList.remove("active");
        });
      } else {
        updateScreenShareView(screenShareVideo);
      }

      // Build memberId -> remoteMedia from panes (used for roster grid and for screen-share strip)
      memberIdToRemoteMedia.clear();
      function addOrPreferLive(memberId, remoteMedia) {
        if (!memberId) return;
        const existing = memberIdToRemoteMedia.get(memberId);
        const preferThis = existing
          ? remoteMedia.sourceState === "live" &&
          existing.sourceState !== "live"
          : true;
        if (preferThis) {
          memberIdToRemoteMedia.set(memberId, remoteMedia);
        }
      }
      if (activeSpeakerVideoPanes) {
        for (const [groupId, group] of Object.entries(
          activeSpeakerVideoPanes,
        )) {
          group.getRemoteMedia().forEach((remoteMedia) => {
            addOrPreferLive(remoteMedia.memberId, remoteMedia);
          });
        }
      }
      if (memberVideoPanes) {
        for (const [paneId, remoteMedia] of Object.entries(memberVideoPanes)) {
          addOrPreferLive(remoteMedia.memberId, remoteMedia);
        }
      }

      const isScreenShareLayout =
        currentLayoutId === "ScreenShareView" || !!screenShareVideo;

      detachParticipantRemoteMediaListeners();
      participantsGrid.innerHTML = "";
      remoteVideos.clear();

      if (isScreenShareLayout) {
        // Screen share: strip of participants from panes only (names + speaking)
        memberIdToRemoteMedia.forEach((remoteMedia, memberId) => {
          const member = getMemberById(memberId);
          const memberName = member?.name || "Unknown Participant";
          addParticipant(memberId, memberName, remoteMedia);
          updateParticipantVideo(
            memberId,
            remoteMedia.stream,
            remoteMedia.sourceState,
          );
          if (typeof remoteMedia.on === "function") {
            remoteMedia.on("sourceUpdate", () => {
              updateParticipantVideo(
                memberId,
                remoteMedia.stream,
                remoteMedia.sourceState,
              );
            });
            remoteMedia.on("stopped", () => {
              removeParticipant(memberId);
            });
          }
        });
        participantsGrid.classList.add("screenshare-strip");
        debugLog("Screen share strip: participants from panes", {
          count: memberIdToRemoteMedia.size,
        });
      } else {
        // No screen share: grid from roster (all in-meeting participants), link media by memberId
        refreshParticipantGridFromRoster();
        debugLog("Roster grid: all in-meeting participants", {
          count: getRosterMembersInMeeting().length,
        });
      }

      const speakingMemberIds = Array.from(activeSpeakers.entries())
        .filter(([, s]) => s.isSpeaking)
        .map(([id]) => id);
      highlightSpeakingParticipants(speakingMemberIds);
    },
  );

  // Active speaker changes (for UI highlighting and audio slot assignment)
  meeting.on("media:activeSpeakerChanged", async ({ memberIds }) => {
    if (!memberIds || !Array.isArray(memberIds)) return;

    // Update currentActiveSpeakers set
    const previousActiveSpeakers = new Set(currentActiveSpeakers);
    currentActiveSpeakers = new Set(memberIds);

    // Send silence status to Python adapter
    if (websocketWrapper) {
      websocketWrapper.sendSilenceStatus(memberIds.length === 0);
    }

    // Wait for audio to propagate to streams
    await new Promise((resolve) =>
      setTimeout(resolve, CORRELATION_CONFIG.CORRELATION_DELAY_MS),
    );

    // Run enhanced correlation algorithm
    correlateActiveSpeakersToSlots(memberIds);

    // Also update legacy tracking for backward compatibility
    memberIds.forEach((memberId) => {
      const member = getMemberById(memberId);
      const memberName = member?.name || "Unknown";
      assignMemberToAudioSlot(memberId, memberName);
    });

    // Update active speaker tracking for UI
    memberIds.forEach((memberId) => {
      if (!activeSpeakers.has(memberId)) {
        const member = getMemberById(memberId);
        activeSpeakers.set(memberId, {
          memberId,
          name: member?.name || "Unknown",
          isSpeaking: true,
          lastSpokeAt: new Date(),
        });
      } else {
        const speaker = activeSpeakers.get(memberId);
        speaker.isSpeaking = true;
        speaker.lastSpokeAt = new Date();
      }
    });

    // Mark others as not speaking
    activeSpeakers.forEach((speaker, memberId) => {
      if (!memberIds.includes(memberId)) {
        speaker.isSpeaking = false;
      }
    });

    updateSpeakersUI();
    highlightSpeakingParticipants(memberIds);
  });

  // Source count changes
  meeting.on(
    "media:remoteVideoSourceCountChanged",
    ({ numTotalSource, numLiveSources }) => {
      debugLog("Video sources changed", {
        total: numTotalSource,
        live: numLiveSources,
      });
    },
  );

  meeting.on(
    "media:remoteAudioSourceCountChanged",
    ({ numTotalSource, numLiveSources }) => {
      debugLog("Audio sources changed", {
        total: numTotalSource,
        live: numLiveSources,
      });

      if (globalAudioMediaGroup) {
        globalAudioMediaGroup.getRemoteMedia().forEach((remoteMedia, index) => {
          // Start recording all streams with audio, regardless of state
          if (remoteMedia.stream) {
            const streamId = remoteMedia.stream.id;
            if (!audioStreamRecorders.has(streamId)) {
              debugLog("🎙️ Starting recording from source count change", {
                csi: remoteMedia.csi,
                streamIndex: index,
              });
              startStreamRecording(
                remoteMedia.stream,
                remoteMedia.csi,
                index,
                remoteMedia,
              );
            }
          }
        });
      }
    },
  );

  // Transcription events (for orgs with Webex Assistant enabled)
  meeting.on("meeting:receiveTranscription:started", (payload) => {
    transcriptionEnabled = true;
    debugLog("✅ Transcription STARTED", {
      captionLanguages: payload.captionLanguages,
      spokenLanguages: payload.spokenLanguages,
    });
    debugLog("📝 Will use transcription API for participant correlation");
  });

  meeting.on("meeting:caption-received", (payload) => {
    lastTranscriptionPayload = payload;

    debugLog("📝 Caption received", {
      captionCount: payload.captions?.length || 0,
      payload: JSON.stringify(payload).substring(0, 200),
    });

    if (payload.captions && Array.isArray(payload.captions)) {
      payload.captions.forEach((caption) => {
        const { personId, text, timestamp, isFinal } = caption;

        // Try to find member by personId
        let memberId = transcriptionToMemberId.get(personId);

        if (!memberId) {
          // Try to find member in roster by matching personId
          const members = getRosterMembersInMeeting();
          const member = members.find((m) => m.id === personId);

          if (member) {
            memberId = member.id;
            transcriptionToMemberId.set(personId, memberId);

            // Also ensure they have an audio slot if they're speaking
            if (isFinal) {
              assignMemberToAudioSlot(memberId, member.name);
            }

            debugLog("📝 Transcription correlation established", {
              personId,
              memberId,
              memberName: member.name,
              text: text?.substring(0, 50),
              slot: getSlotForMember(memberId),
            });
          } else {
            debugLog("⚠️ Transcription personId not found in roster", {
              personId,
              text: text?.substring(0, 50),
            });
          }
        }

        if (isFinal && memberId) {
          const member = getMemberById(memberId);
          const slot = getSlotForMember(memberId);
          debugLog("📝 Final transcription", {
            memberId,
            memberName: member?.name || "Unknown",
            text: text?.substring(0, 100),
            timestamp,
            audioSlot: slot !== null ? slot : "not_assigned",
          });

          // Send caption to Python adapter via WebSocket
          if (websocketWrapper) {
            websocketWrapper.sendCaptionUpdate({
              participant_uuid: memberId,
              participant_full_name: member?.name || "Unknown",
              text: text,
              timestamp_ms: timestamp || Date.now(),
              is_final: isFinal,
            });
          }
        }
      });
    }
  });

  meeting.on("meeting:receiveTranscription:stopped", () => {
    transcriptionEnabled = false;
    debugLog("❌ Transcription STOPPED");
    debugLog("⚠️ Falling back to active speaker correlation only");
  });
}

function startStreamRecording(stream, csi, index, remoteMedia = null) {
  if (!stream) return;

  const streamId = stream.id;

  // Check if already recording this stream
  if (audioStreamRecorders.has(streamId)) {
    return;
  }

  try {
    const audioTracks = stream.getAudioTracks();
    if (audioTracks.length === 0) {
      return;
    }

    debugLog(`[Audio] Starting continuous streaming for slot ${index}, CSI: ${csi}`);

    // Create audio context for amplitude detection (used for correlation)
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
      const average =
        dataArray.reduce((sum, value) => sum + value, 0) / bufferLength;
      updateStreamActivity(index, average);
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
            duration: frame.duration,
          };

          if (!lastAudioFormat || JSON.stringify(currentFormat) !== JSON.stringify(lastAudioFormat)) {
            lastAudioFormat = currentFormat;
            if (websocketWrapper) {
              websocketWrapper.sendJson({
                type: "AudioFormatUpdate",
                format: currentFormat,
              });
            }
          }

          // Update activity detection
          hasAudioActivity();

          // Skip silent frames (all zeros)
          let isAllZeros = true;
          for (let i = 0; i < audioData.length; i++) {
            if (audioData[i] !== 0) {
              isAllZeros = false;
              break;
            }
          }
          if (isAllZeros) {
            frame.close();
            return;
          }

          // Determine participant ID using correlation
          let participantId = null;
          const slotMapping = getMemberForSlot(index);

          if (slotMapping && slotMapping.confidence >= CORRELATION_CONFIG.CONFIDENCE_THRESHOLD) {
            participantId = slotMapping.memberId;
          } else if (streamIndexToMemberId.has(index)) {
            participantId = streamIndexToMemberId.get(index);
          }

          // Send per-participant audio if we have a participant ID and WebSocket is ready
          if (participantId && websocketWrapper && websocketWrapper.mediaSendingEnabled) {
            websocketWrapper.sendPerParticipantAudio(participantId, audioData.buffer);
          }

          frame.close();
        } catch (error) {
          console.error("[Audio] Error processing frame:", error);
          try { frame.close(); } catch (e) { /* ignore */ }
        }
      },
      flush() {
        debugLog("[Audio] Transform stream flush called for slot " + index);
      },
    });

    // Start the pipeline
    const abortController = new AbortController();
    readable
      .pipeThrough(transformStream)
      .pipeTo(new WritableStream(), { signal: abortController.signal })
      .catch((error) => {
        if (error.name !== "AbortError") {
          console.error("[Audio] Pipeline error:", error);
        }
      });

    // Store recorder reference with cleanup data
    audioStreamRecorders.set(streamId, {
      stream: stream,
      csi: csi,
      index: index,
      remoteMedia: remoteMedia,
      audioContext: audioContext,
      abortController: abortController,
    });

    debugLog(`[Audio] Stream recording started for slot ${index}`);
  } catch (error) {
    debugLog("❌ Failed to start stream recording", { error: error.message });
  }
}

function stopStreamRecording(streamId) {
  if (!streamId) return;

  const recorderData = audioStreamRecorders.get(streamId);
  if (!recorderData) return;

  try {
    // Abort the streaming pipeline
    if (recorderData.abortController) {
      recorderData.abortController.abort();
    }

    // Close audio context
    if (recorderData.audioContext) {
      recorderData.audioContext.close();
    }

    audioStreamRecorders.delete(streamId);
    debugLog("[Audio] Stopped stream recording", { streamId, csi: recorderData.csi });
  } catch (error) {
    debugLog("[Audio] Error stopping stream recording", { error: error.message });
    audioStreamRecorders.delete(streamId);
  }
}

function processNewVideoPane(meeting, paneGroupId, paneId, remoteMedia) {
  // Store the remoteMedia for this memberId so we can access their info later
  if (remoteMedia.memberId) {
    memberIdToRemoteMedia.set(remoteMedia.memberId, remoteMedia);
    debugLog("Tracked video pane", {
      memberId: remoteMedia.memberId,
      paneGroupId,
      paneId,
      sourceState: remoteMedia.sourceState,
    });
  }

  // Setup listener for source changes
  // remoteMedia.on('sourceUpdate', (data) => {
  //     if (data.memberId) {
  //         memberIdToRemoteMedia.set(data.memberId, remoteMedia);
  //         debugLog('Video source updated', {
  //             memberId: data.memberId,
  //             state: data.state
  //         });
  //     }
  // });

  // Handle when remote media stops
  remoteMedia.on("stopped", () => {
    if (remoteMedia.memberId) {
      memberIdToRemoteMedia.delete(remoteMedia.memberId);
      debugLog("Video pane stopped", { memberId: remoteMedia.memberId });
    }
  });
}

function getMemberById(memberId) {
  if (!meeting || !meeting.members) return null;
  const members = meeting.members.membersCollection.members;
  return members[memberId] || null;
}

/** Get all participants currently in the meeting (roster), regardless of speaking status. */
function getRosterMembersInMeeting() {
  if (!meeting || !meeting.members) return [];
  const collection = meeting.members.membersCollection;
  const members = collection.members || collection.getAll?.() || {};
  const list = Array.isArray(members) ? members : Object.values(members);
  return list.filter((m) => m && m.isInMeeting === true);
}

/** Detach sourceUpdate/stopped from all participants' remoteMedia before clearing the grid. */
function detachParticipantRemoteMediaListeners() {
  remoteVideos.forEach((p) => {
    if (p.remoteMedia && typeof p.remoteMedia.off === "function") {
      try {
        // p.remoteMedia.off('sourceUpdate');
        // p.remoteMedia.off('stopped');
      } catch (e) {
        /* no-op */
      }
    }
  });
}

function refreshParticipantGridFromRoster(rosterOverride) {
  if (!meeting) return;
  const isScreenShareLayout =
    currentLayoutId === "ScreenShareView" || !!currentScreenShareRemoteMedia;
  if (isScreenShareLayout) return;

  detachParticipantRemoteMediaListeners();
  participantsGrid.innerHTML = "";
  remoteVideos.clear();

  const roster = Array.isArray(rosterOverride)
    ? rosterOverride.filter((m) => m && m.isInMeeting === true)
    : getRosterMembersInMeeting();
  debugLog("Roster grid refresh", { count: roster.length });

  roster.forEach((member) => {
    const memberId = member.id;
    const memberName = member.name || "Unknown Participant";
    const remoteMedia = memberIdToRemoteMedia.get(memberId) || null;

    addParticipant(memberId, memberName, remoteMedia);
    updateParticipantVideo(
      memberId,
      remoteMedia?.stream ?? null,
      remoteMedia?.sourceState ?? "no source",
    );

    if (remoteMedia && typeof remoteMedia.on === "function") {
      // remoteMedia.on('sourceUpdate', () => {
      //     updateParticipantVideo(memberId, remoteMedia.stream, remoteMedia.sourceState);
      // });
      remoteMedia.on("stopped", () => {
        removeParticipant(memberId);
      });
    }
  });

  updateParticipantCount();
  participantsGrid.classList.remove("screenshare-strip");
  const speakingMemberIds = Array.from(activeSpeakers.entries())
    .filter(([, s]) => s.isSpeaking)
    .map(([id]) => id);
  highlightSpeakingParticipants(speakingMemberIds);

  // Force UI repaint so grid update is visible (roster-driven grid requires explicit update)
  requestAnimationFrame(() => {
    participantsGrid.offsetHeight; // reflow
  });
}

function highlightSpeakingParticipants(speakingMemberIds) {
  // Remove all speaking highlights
  document.querySelectorAll(".participant-tile.speaking").forEach((tile) => {
    tile.classList.remove("speaking");
  });

  // Add highlight to current speakers by matching memberId
  remoteVideos.forEach((participant, participantId) => {
    if (speakingMemberIds.includes(participantId)) {
      participant.tile.classList.add("speaking");
    }
  });
}

function updateSpeakersUI() {
  if (activeSpeakers.size === 0) {
    speakersList.innerHTML =
      '<p style="color: #666; text-align: center; padding: 20px;">No active speakers yet</p>';
    return;
  }

  speakersList.innerHTML = "";

  const sortedSpeakers = Array.from(activeSpeakers.entries()).sort(
    (a, b) => b[1].lastSpokeAt - a[1].lastSpokeAt,
  );

  sortedSpeakers.forEach(([memberId, data]) => {
    const item = document.createElement("div");
    item.className = `speaker-item ${data.isSpeaking ? "speaking" : ""}`;
    item.innerHTML = `
            <div class="speaker-name">${data.isSpeaking ? "🔊 " : ""}${data.name}</div>
            <div class="speaker-status">Last spoke: ${data.lastSpokeAt.toLocaleTimeString()}</div>
        `;
    speakersList.appendChild(item);
  });
}

async function joinMeeting() {
  const destination = (meetingDestinationElm && meetingDestinationElm.value)
    ? meetingDestinationElm.value.trim() : "";

  if (!destination) {
    showLoginStatus("Please enter a meeting URL or number", "error");
    window.webexJoinStatus = {
      status: "error",
      message: "No meeting URL provided",
      type: "validation_error",
      code: "NO_MEETING_URL",
    };
    return;
  }

  try {
    // Reset meeting end state for the new meeting session
    meetingEndedSent = false;
    botAdmittedToMeeting = false;

    if (btnJoin) {
      btnJoin.disabled = true;
      btnJoin.textContent = "Joining...";
    }

    window.webexJoinStatus = {
      status: "initializing",
      message: "Initializing Webex SDK",
      type: "info",
      code: null,
    };

    // Initialize SDK
    await initializeWebex();

    // Connect WebSocket for recording
    connectWebSocket();

    window.webexJoinStatus = {
      status: "joining",
      message: "Creating meeting",
      type: "info",
      code: null,
    };

    // Create meeting
    showLoginStatus("Creating meeting...", "info");
    debugLog("Creating meeting", { destination });

    meeting = await webex.meetings.create(destination);
    debugLog("Meeting created", { id: meeting.id });

    // Setup meeting event listeners BEFORE joining
    setupMeetingListeners();

    // Define media configuration (will be used after admission)
    const remoteMediaManagerConfig = {
      audio: {
        numOfActiveSpeakerStreams: 3,
        numOfScreenShareStreams: 1,
      },
      video: {
        preferLiveVideo: true,
        initialLayoutId: "AllEqual",
        layouts: {
          AllEqual: {
            activeSpeakerVideoPaneGroups: [
              { id: "main", numPanes: 9, size: "best", priority: 255 },
            ],
          },
          ScreenShareView: {
            screenShareVideo: { size: "best" },
            activeSpeakerVideoPaneGroups: [
              {
                id: "thumbnails",
                numPanes: 8,
                size: "thumbnail",
                priority: 255,
              },
            ],
          },
        },
      },
    };

    // Monitor bot status in members list to detect rejection
    let botAdmitted = false;
    meeting.members.on("members:update", (payload) => {
      const delta = payload.delta || {};
      const updated = delta.updated || [];
      const removed = delta.removed || [];

      // Check for bot in updated members
      updated.forEach((member) => {
        if (
          member.name &&
          member.name.toLowerCase().includes(BOT_NAME.toLowerCase()) &&
          member.isSelf
        ) {
          debugLog("🤖 Bot status update", {
            name: member.name,
            status: member.status,
            isInLobby: member.isInLobby,
            isInMeeting: member.isInMeeting,
            state: member.state,
          });

          // If bot is NOT in meeting and NOT in lobby = REJECTED
          // This happens when host denies the join request
          if (!member.isInMeeting && !member.isInLobby && !botAdmitted) {
            debugLog(
              "❌ Bot rejected from lobby (not in meeting/lobby anymore)",
            );
            showLoginStatus("❌ Join request denied by host", "error");

            // Return to login screen
            meetingScreen.classList.remove("active");
            loginScreen.style.display = "flex";
            btnJoin.disabled = false;
            btnJoin.textContent = "Join Meeting";

            // Clean up
            if (meeting) {
              meeting
                .leave()
                .catch((e) => debugLog("Error leaving", { error: e.message }));
              meeting = null;
            }
          }

          // If bot successfully joined meeting from lobby
          if (member.isInMeeting && !botAdmitted) {
            debugLog(
              "✅ Bot admitted to meeting (detected via members:update)",
            );
            botAdmitted = true;
            botAdmittedToMeeting = true;
          }
        }
      });

      // Check for bot in removed members (kicked/rejected)
      removed.forEach((member) => {
        if (
          member.name &&
          member.name.toLowerCase().includes(BOT_NAME.toLowerCase()) &&
          member.isSelf
        ) {
          debugLog("❌ Bot removed from members list", {
            name: member.name,
            wasInLobby: meeting.isInLobby,
          });

          if (meeting.isInLobby || !botAdmitted) {
            showLoginStatus("❌ Join request denied", "error");

            // Return to login screen
            meetingScreen.classList.remove("active");
            loginScreen.style.display = "flex";
            btnJoin.disabled = false;
            btnJoin.textContent = "Join Meeting";

            meeting = null;
          }
        }
      });
    });

    // CRITICAL: Setup lobby admission handler BEFORE joining
    meeting.on("meeting:self:guestAdmitted", async () => {
      debugLog("✅ Admitted from lobby, adding media now");
      showLoginStatus("Admitted! Adding media...", "info");
      botAdmitted = true;
      botAdmittedToMeeting = true;

      window.webexJoinStatus = {
        status: "IN_MEETING",
        message: "Admitted to meeting, adding media",
        type: "success",
        code: 200,
      };

      // Notify Python that bot was admitted
      if (websocketWrapper) {
        websocketWrapper.sendMeetingStatusChange("joined");
      }

      try {
        // Setup multistream listeners BEFORE adding media (so events are captured)
        setupMultistreamListeners();

        // Respect disableIncomingVideo from adapter config
        const disableVideo = (window.webexInitialData && window.webexInitialData.disableIncomingVideo) ||
          (window.initialData && window.initialData.disableIncomingVideo) || false;

        // Add media AFTER being admitted
        const mediaOptions = {
          receiveAudio: true,
          receiveVideo: !disableVideo,
          sendAudio: false,
          sendVideo: false,
          receiveTranscription: true,
          remoteMediaManagerConfig,
        };

        await meeting.addMedia(mediaOptions);
        debugLog("✅ Media added successfully", { disableVideo });

        showLoginStatus("Joined meeting successfully!", "success");

        // Switch to meeting screen
        if (loginScreen) loginScreen.style.display = "none";
        if (meetingScreen) meetingScreen.classList.add("active");
        if (meetingTitle) meetingTitle.textContent = meeting.title || "Meeting";

        // Notify that chat is ready (Webex supports chat immediately after joining)
        if (websocketWrapper) {
          websocketWrapper.sendChatStatusChange("ready_to_send");
        }
      } catch (mediaError) {
        debugLog("❌ Failed to add media after admission", {
          error: mediaError.message,
        });
        showLoginStatus("Failed to add media: " + mediaError.message, "error");

        window.webexJoinStatus = {
          status: "FAILED_TO_JOIN",
          message: `Failed to add media: ${mediaError.message}`,
          type: "error",
          code: "MEDIA_ERROR",
        };

        if (websocketWrapper) {
          websocketWrapper.sendMeetingStatusChange("failed_to_join");
        }
      }
    });

    // Setup lobby waiting handler
    meeting.on("meeting:self:lobbyWaiting", (payload) => {
      debugLog("🚪 Bot waiting in lobby for admission", payload);
      showLoginStatus("⏳ Waiting in lobby - host will admit you...", "info");
    });

    // Handle rejection from lobby
    meeting.on("meeting:self:guestDenied", async (payload) => {
      debugLog("❌ Bot rejected from lobby", payload);
      showLoginStatus("❌ Join request denied by host", "error");

      window.webexJoinStatus = {
        status: "FAILED_TO_JOIN",
        message: "Join request denied by host",
        type: "error",
        code: "GUEST_DENIED",
      };

      // Notify Python adapter
      if (websocketWrapper) {
        websocketWrapper.sendMeetingStatusChange("failed_to_join");
        websocketWrapper.sendError("Join request denied by host", { reason: "guest_denied" });
      }

      // Clean up and return to login screen
      try {
        await meeting.leave();
      } catch (leaveError) {
        debugLog("Error leaving after rejection", {
          error: leaveError.message,
        });
      }

      if (meetingScreen) meetingScreen.classList.remove("active");
      if (loginScreen) loginScreen.style.display = "flex";
      if (btnJoin) {
        btnJoin.disabled = false;
        btnJoin.textContent = "Join Meeting";
      }
      meeting = null;
    });

    // Handle removal from lobby/meeting
    meeting.on("meeting:removedParticipant", (payload) => {
      debugLog("❌ Bot removed from meeting/lobby", payload);
      // Check if it's the bot that was removed (isSelf or name match)
      const botName = (window.webexInitialData && window.webexInitialData.botName) ||
        (window.initialData && window.initialData.botName) ||
        BOT_NAME;
      const isSelf = (payload.participant && payload.participant.isSelf) ||
        (payload.participant && payload.participant.name === botName);
      if (isSelf) {
        showLoginStatus("❌ Removed from meeting/lobby", "error");
        handleMeetingEnd("Bot removed from meeting/lobby", "removed_from_meeting");
        meeting = null;
      }
    });

    // Handle lobby left event (when bot leaves lobby for any reason)
    meeting.on("meeting:self:lobbyLeft", (payload) => {
      debugLog("🚪 Bot left lobby", payload);
      const reason = payload?.reason || "unknown";

      if (reason === "DENIED" || reason === "REJECTED") {
        showLoginStatus("❌ Join request denied", "error");

        window.webexJoinStatus = {
          status: "FAILED_TO_JOIN",
          message: `Join request ${reason.toLowerCase()}`,
          type: "error",
          code: reason,
        };

        if (websocketWrapper) {
          websocketWrapper.sendMeetingStatusChange("failed_to_join");
          websocketWrapper.sendError("Join request denied", { reason });
        }

        // Clean up
        if (meetingScreen) meetingScreen.classList.remove("active");
        if (loginScreen) loginScreen.style.display = "flex";
        if (btnJoin) {
          btnJoin.disabled = false;
          btnJoin.textContent = "Join Meeting";
        }

        if (meeting) {
          meeting
            .leave()
            .catch((e) => debugLog("Error leaving", { error: e.message }));
          meeting = null;
        }
      }
    });

    meeting.on("meeting:self:requestedToJoin", (payload) => {
      debugLog("📞 Requested to join meeting", payload);
    });

    // Handle meeting state changes - covers BOTH lobby and in-meeting scenarios
    meeting.on("meeting:stateChange", (payload) => {
      debugLog("Meeting state changed", {
        from: payload.from,
        to: payload.to,
        current: meeting ? meeting.state : "no meeting",
        isInLobby: meeting ? meeting.isInLobby : "no meeting",
        botAdmitted: botAdmittedToMeeting,
      });

      if (!meeting) return;

      if (payload.to === "INACTIVE" || payload.to === "LEFT") {
        if (meeting.isInLobby) {
          // Case 1: Meeting became inactive/left while in lobby (likely rejected/denied)
          debugLog("⚠️ Meeting became inactive while in lobby (likely rejected)");
          showLoginStatus("❌ Unable to join meeting", "error");

          if (websocketWrapper) {
            websocketWrapper.sendMeetingStatusChange("failed_to_join");
          }

          if (meetingScreen) meetingScreen.classList.remove("active");
          if (loginScreen) loginScreen.style.display = "flex";
          if (btnJoin) {
            btnJoin.disabled = false;
            btnJoin.textContent = "Join Meeting";
          }
          meeting = null;
        } else if (botAdmittedToMeeting) {
          // Case 2: Bot was admitted and in the meeting, but meeting transitioned to
          // INACTIVE/LEFT. This means the host ended the meeting, or the bot was
          // disconnected. This is a CRITICAL detection path.
          debugLog("⚠️ Meeting became INACTIVE/LEFT while bot was in meeting (host ended or disconnected)");
          handleMeetingEnd(`Meeting state changed to ${payload.to} (host ended or disconnected)`, "meeting_ended");
          meeting = null;
        }
      }
    });

    // STEP 1: Join WITHOUT media first
    showLoginStatus("Joining meeting...", "info");
    debugLog(
      "Joining meeting without media (will add after admission if needed)",
    );

    window.webexJoinStatus = {
      status: "joining",
      message: "Joining meeting",
      type: "info",
      code: null,
    };

    await meeting.join({
      pin: (meetingPasswordElm && meetingPasswordElm.value) ? meetingPasswordElm.value : undefined,
      moderator: false,
      moveToResource: false,
      enableMultistream: true, // CRITICAL: Enable multistream
    });

    // STEP 2: Check if already joined (no lobby) and add media immediately
    if (meeting.state === "JOINED" && !meeting.isInLobby) {
      debugLog("✅ Joined directly (no lobby), adding media now");
      botAdmitted = true;
      botAdmittedToMeeting = true;

      window.webexJoinStatus = {
        status: "IN_MEETING",
        message: "Joined meeting directly (no lobby)",
        type: "success",
        code: 200,
      };

      // Notify Python adapter that bot joined
      if (websocketWrapper) {
        websocketWrapper.sendMeetingStatusChange("joined");
      }

      // Setup multistream listeners BEFORE adding media (so events are captured)
      setupMultistreamListeners();

      // Respect disableIncomingVideo from adapter config
      const disableVideo = (window.webexInitialData && window.webexInitialData.disableIncomingVideo) ||
        (window.initialData && window.initialData.disableIncomingVideo) || false;

      const mediaOptions = {
        receiveAudio: true,
        receiveVideo: !disableVideo,
        sendAudio: false,
        sendVideo: false,
        receiveTranscription: true,
        remoteMediaManagerConfig,
      };

      await meeting.addMedia(mediaOptions);
      debugLog("✅ Media added successfully", { disableVideo });

      showLoginStatus("Joined meeting successfully!", "success");

      // Switch to meeting screen
      if (loginScreen) loginScreen.style.display = "none";
      if (meetingScreen) meetingScreen.classList.add("active");
      if (meetingTitle) meetingTitle.textContent = meeting.title || "Meeting";

      // Notify that chat is ready
      if (websocketWrapper) {
        websocketWrapper.sendChatStatusChange("ready_to_send");
      }
    } else if (meeting.isInLobby) {
      debugLog(
        "⏳ In lobby, waiting for admission (media will be added after)",
      );
      showLoginStatus("⏳ Waiting in lobby...", "info");

      window.webexJoinStatus = {
        status: "WAITING_IN_LOBBY",
        message: "Waiting in lobby for host to admit",
        type: "info",
        code: null,
      };

      // Periodically check bot status while in lobby
      const lobbyCheckInterval = setInterval(() => {
        if (!meeting || !meeting.isInLobby || botAdmitted) {
          clearInterval(lobbyCheckInterval);
          return;
        }

        // Find self member in meeting
        const selfMember = meeting.members.selfMember;
        if (selfMember) {
          debugLog("🔍 Checking bot lobby status", {
            name: selfMember.name,
            isInLobby: selfMember.isInLobby,
            isInMeeting: selfMember.isInMeeting,
            status: selfMember.status,
          });

          // If bot is no longer in lobby and not in meeting = rejected
          if (!selfMember.isInLobby && !selfMember.isInMeeting) {
            debugLog("❌ Bot rejected (no longer in lobby or meeting)");
            showLoginStatus("❌ Join request denied", "error");

            clearInterval(lobbyCheckInterval);

            // Return to login screen
            if (meetingScreen) meetingScreen.classList.remove("active");
            if (loginScreen) loginScreen.style.display = "flex";
            if (btnJoin) {
              btnJoin.disabled = false;
              btnJoin.textContent = "Join Meeting";
            }

            meeting
              .leave()
              .catch((e) => debugLog("Error leaving", { error: e.message }));
            meeting = null;
          }
        }
      }, 2000); // Check every 2 seconds

      // The meeting:self:guestAdmitted handler will add media and switch UI
    }
  } catch (error) {
    debugLog("Failed to join meeting", {
      error: error.message,
      stack: error.stack,
    });
    showLoginStatus("Failed to join: " + error.message, "error");

    window.webexJoinStatus = {
      status: "FAILED_TO_JOIN",
      message: `Failed to join: ${error.message}`,
      type: "error",
      code: error.code || "JOIN_ERROR",
    };

    // Notify Python adapter of failure
    if (websocketWrapper) {
      websocketWrapper.sendMeetingStatusChange("failed_to_join");
      websocketWrapper.sendError("Failed to join meeting", {
        error: error.message,
        code: error.code
      });
    }

    if (btnJoin) {
      btnJoin.disabled = false;
      btnJoin.textContent = "Join Meeting";
    }

    // Clean up meeting object on error
    if (meeting) {
      try {
        await meeting.leave();
      } catch (leaveError) {
        debugLog("Error during cleanup", { error: leaveError.message });
      }
      meeting = null;
    }
  }
}

function setupMeetingListeners() {
  meeting.on("meeting:reconnectionSuccess", () => {
    debugLog("Meeting reconnected successfully");
  });

  // Reconnection failed permanently - treat as meeting end
  meeting.on("meeting:reconnectionFailure", () => {
    debugLog("⚠️ Meeting reconnection failed permanently");
    handleMeetingEnd("Meeting reconnection failed permanently", "meeting_ended");
    meeting = null;
  });

  // Also handle the older event name (some SDK versions use "Failed" instead of "Failure")
  meeting.on("meeting:reconnectionFailed", () => {
    debugLog("⚠️ Meeting reconnection failed (legacy event)");
    handleMeetingEnd("Meeting reconnection failed", "meeting_ended");
    meeting = null;
  });

  // Meeting stopped event - fires when the host ends the meeting for everyone.
  // This is one of the most reliable events for detecting host-initiated meeting end.
  meeting.on("meeting:stopped", (reason) => {
    debugLog("🔴 meeting:stopped event fired", { reason });
    handleMeetingEnd(`Meeting stopped: ${reason || "host ended"}`, "meeting_ended");
    meeting = null;
  });

  // Self left meeting - fires when the bot leaves the meeting (e.g., via meeting.leave())
  meeting.on("meeting:self:left", () => {
    debugLog("🔴 meeting:self:left event fired");
    handleMeetingEnd("Bot left meeting (self:left)", "meeting_ended");
    meeting = null;
  });

  // Media stopped event - fires when all media streams are terminated.
  // This is a backup indicator that the meeting has ended.
  meeting.on("media:stopped", () => {
    debugLog("⚠️ media:stopped event fired - media streams terminated");
    // Only treat as meeting end if the bot was admitted (media:stopped can also fire
    // during normal media renegotiation, so we only act on it if it's unexpected)
    if (botAdmittedToMeeting && !meetingEndedSent) {
      debugLog("Media stopped while bot was in meeting - treating as meeting end");
      handleMeetingEnd("Media stopped (meeting likely ended)", "meeting_ended");
      meeting = null;
    }
  });

  meeting.on("meeting:stoppedSharingRemote", () => {
    debugLog("Remote screen share stopped");
    if (screenshareVideo) screenshareVideo.srcObject = null;
    if (screenshareView) screenshareView.classList.remove("active");
    if (
      meeting &&
      meeting.remoteMediaManager &&
      typeof meeting.remoteMediaManager.setLayout === "function"
    ) {
      meeting.remoteMediaManager.setLayout("AllEqual").catch((err) => {
        debugLog("setLayout(AllEqual) error", { error: err?.message });
      });
    }
  });

  meeting.on("meeting:startedSharingRemote", () => {
    debugLog("Remote screen share started");
    if (
      meeting &&
      meeting.remoteMediaManager &&
      typeof meeting.remoteMediaManager.setLayout === "function"
    ) {
      meeting.remoteMediaManager
        .setLayout("ScreenShareView")
        .then(() => {
          debugLog("Switched to ScreenShareView layout");
        })
        .catch((err) => {
          debugLog("setLayout(ScreenShareView) error", { error: err?.message });
        });
    }
  });

  // Roster updates: keep grid in sync with all in-meeting participants (join/leave/status)
  meeting.members.on("members:update", (payload) => {
    // Skip processing if meeting has ended
    if (meetingEndedSent) return;

    const full = payload.full || [];
    const delta = payload.delta || {};

    // Convert full to array if it's an object
    const fullArray = Array.isArray(full) ? full : Object.values(full);

    debugLog("Members updated", {
      fullCount: fullArray.length,
      added: (delta.added || []).length,
      updated: (delta.updated || []).length,
      removed: (delta.removed || []).length,
    });

    // Track previous participants
    const previousParticipantIds = new Set(participantsMap.keys());
    const currentParticipantIds = new Set();

    const newUsers = [];
    const updatedUsers = [];
    const removedUsers = [];

    // Determine bot name for isCurrentUser detection
    const botName = (window.webexInitialData && window.webexInitialData.botName) ||
      (window.initialData && window.initialData.botName) ||
      BOT_NAME;

    // Process all current members - format must match framework expectations
    // The Python WebBotAdapter expects: deviceId, fullName, isCurrentUser, isHost, humanized_status, active
    fullArray.forEach((member) => {
      if (!member || !member.id) return;

      const memberId = member.id;
      currentParticipantIds.add(memberId);

      const participantInfo = {
        deviceId: memberId,
        fullName: member.name || "Unknown",
        displayName: member.name || "Unknown",
        isCurrentUser: member.isSelf || (member.name && member.name === botName),
        isHost: member.isHost || false,
        status: member.isInMeeting ? 1 : 6,
        humanized_status: member.isInMeeting ? "in_meeting" : "not_in_meeting",
        meetingId: meeting?.id || null,
      };

      // Check if new or updated
      if (!previousParticipantIds.has(memberId)) {
        newUsers.push(participantInfo);
      } else {
        const existing = participantsMap.get(memberId);
        // Check if anything changed
        if (JSON.stringify(existing) !== JSON.stringify(participantInfo)) {
          updatedUsers.push(participantInfo);
        }
      }

      participantsMap.set(memberId, participantInfo);
    });

    // Find removed participants
    previousParticipantIds.forEach((memberId) => {
      if (!currentParticipantIds.has(memberId)) {
        const removedInfo = participantsMap.get(memberId);
        if (removedInfo) {
          removedUsers.push({
            ...removedInfo,
            status: 6,
            humanized_status: "not_in_meeting",
          });
        }
        participantsMap.delete(memberId);
      }
    });

    // Send user updates to Python via standard protocol
    if (websocketWrapper) {
      if (newUsers.length > 0 || removedUsers.length > 0 || updatedUsers.length > 0) {
        websocketWrapper.sendUsersUpdate(newUsers, removedUsers, updatedUsers);
        debugLog("📤 Sent UsersUpdate", {
          new: newUsers.length,
          removed: removedUsers.length,
          updated: updatedUsers.length,
        });
      }
    }

    // When no one is sharing, grid is roster-driven; refresh UI with event roster so it updates immediately
    if (
      currentLayoutId !== "ScreenShareView" &&
      !currentScreenShareRemoteMedia
    ) {
      refreshParticipantGridFromRoster(fullArray);
    }
  });
}

async function leaveMeeting() {
  if (!meeting) {
    debugLog("leaveMeeting called but no meeting exists");
    // Even without a meeting, send meeting_ended if not yet sent (edge case)
    handleMeetingEnd("leaveMeeting called (no meeting object)", "meeting_ended");
    return;
  }

  try {
    debugLog("Leaving meeting (leaveMeeting called)");

    // Step 1: Send meeting_ended via centralized handler FIRST, before SDK leave.
    // This ensures the Python adapter gets the notification even if meeting.leave() fails.
    // The centralized handler handles deduplication, so if meeting:self:left or
    // meeting:stopped also fires, the duplicate will be safely ignored.
    handleMeetingEnd("Bot leaving meeting (manual leave)", "meeting_ended");

    // Step 2: Clear all tracking maps
    memberIdToAudioCSI.clear();
    memberIdToRemoteMedia.clear();
    activeSpeakers.clear();
    remoteVideos.clear();
    currentActiveSpeakers.clear();
    streamIndexToMemberId.clear();
    streamSlotAudioActivity.clear();
    memberToSlotHistory.clear();
    slotToMemberMap.clear();
    audioSlotToMemberId.clear();
    memberIdToSlotIndex.clear();
    transcriptionToMemberId.clear();
    globalAudioMediaGroup = null;

    // Step 3: Leave the meeting via SDK
    try {
      await meeting.leave();
      debugLog("Left meeting via SDK");
    } catch (leaveError) {
      debugLog("Error leaving meeting via SDK (may already be disconnected)", { error: leaveError.message });
    }

    // Step 4: Wait a moment for WebSocket buffers to flush
    await new Promise(resolve => setTimeout(resolve, 2000));

    debugLog("Left meeting successfully");

    if (speakersList) {
      speakersList.innerHTML =
        '<p style="color: #666; text-align: center; padding: 20px;">No active speakers yet</p>';
    }

    meeting = null;
  } catch (error) {
    debugLog("Error leaving meeting", { error: error.message });
    // Ensure meeting_ended is sent even on error
    handleMeetingEnd("Error during leaveMeeting: " + error.message, "meeting_ended");
    meeting = null;
  }
}

// Expose leaveMeeting globally so Python adapter can call it
window.leaveMeeting = leaveMeeting;

if (btnJoin) btnJoin.addEventListener("click", joinMeeting);
if (btnLeave) btnLeave.addEventListener("click", leaveMeeting);

if (btnToggleDebug) {
  btnToggleDebug.addEventListener("click", () => {
    if (debugPanel) debugPanel.classList.toggle("show");
  });
}

if (btnMute) {
  btnMute.addEventListener("click", () => {
    btnMute.classList.toggle("active");
  });
}
if (btnVideo) {
  btnVideo.addEventListener("click", () => {
    btnVideo.classList.toggle("active");
  });
}

// ============================================================================
// INITIALIZATION
// ============================================================================

// Initialize webexJoinStatus for Python adapter monitoring
window.webexJoinStatus = {
  status: "initializing",
  message: "Page loaded, waiting for initialization",
  type: "info",
  code: null,
};

// Auto-join if webexInitialData is provided (from Python adapter)
document.addEventListener("DOMContentLoaded", async () => {
  debugLog("Application loaded and ready");
  debugLog("Multistream support enabled for individual participant streams");

  // Check if we have initial data from Python adapter
  if (window.webexInitialData) {
    debugLog("🤖 Auto-join mode detected - initializing from Python adapter", window.webexInitialData);

    window.webexJoinStatus = {
      status: "auto_joining",
      message: "Auto-joining from Python adapter",
      type: "info",
      code: null,
    };

    // Auto-fill the form fields
    if (accessTokenElm) accessTokenElm.value = window.webexInitialData.accessToken || "";
    if (meetingDestinationElm) meetingDestinationElm.value = window.webexInitialData.meetingDestination || "";
    if (meetingPasswordElm) meetingPasswordElm.value = window.webexInitialData.meetingPassword || "";

    // Determine WebSocket URL: prefer webexInitialData, then initialData.websocketPort
    const wsPort = window.webexInitialData.websocketPort ||
      (window.initialData && window.initialData.websocketPort) || 8766;
    const wsUrl = window.webexInitialData.websocketUrl || `ws://localhost:${wsPort}`;
    if (websocketUrlElm) websocketUrlElm.value = wsUrl;

    // Auto-connect WebSocket first
    connectWebSocket();

    // Wait for WebSocket to connect (with timeout)
    const wsConnectStart = Date.now();
    while ((!websocket || websocket.readyState !== WebSocket.OPEN) && Date.now() - wsConnectStart < 5000) {
      await new Promise(resolve => setTimeout(resolve, 200));
    }

    if (!websocket || websocket.readyState !== WebSocket.OPEN) {
      debugLog("⚠️ WebSocket not connected after 5s, proceeding anyway");
    }

    // Auto-join the meeting
    try {
      await joinMeeting();
    } catch (error) {
      debugLog("❌ Auto-join failed", { error: error.message });
      window.webexJoinStatus = {
        status: "FAILED_TO_JOIN",
        message: error.message,
        type: "error",
        code: error.code || "AUTO_JOIN_FAILED",
      };
    }
  } else {
    debugLog("📋 Manual mode - waiting for user input");
  }
});