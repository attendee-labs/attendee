// Owns the websocket connection back to the Python adapter. Messages use the wire format shared with the other
// web bots: a 4 byte little-endian message type followed by the payload.
class WebSocketClient {
    static MESSAGE_TYPES = {
        JSON: 1,
        AUDIO: 3,
        PER_PARTICIPANT_AUDIO: 5,
    };

    constructor() {
        this.ws = new WebSocket(`ws://localhost:${window.initialData.websocketPort}`);
        this.ws.binaryType = 'arraybuffer';
        this.mediaSendingEnabled = false;
        // The page can report events before the socket is open, so they wait here until it is
        this.pendingJson = [];
        this.ws.onopen = () => this.pendingJson.splice(0).forEach((data) => this.sendJson(data));
        this.ws.onerror = (error) => console.error('WebSocket Error:', error);
    }

    enableMediaSending() {
        this.mediaSendingEnabled = true;
    }

    disableMediaSending() {
        this.mediaSendingEnabled = false;
    }

    sendBinary(messageType, ...parts) {
        const message = new Uint8Array(4 + parts.reduce((length, part) => length + part.byteLength, 0));
        new DataView(message.buffer).setInt32(0, messageType, true);
        let offset = 4;
        for (const part of parts) {
            message.set(new Uint8Array(part.buffer ?? part, part.byteOffset ?? 0, part.byteLength), offset);
            offset += part.byteLength;
        }
        this.ws.send(message.buffer);
    }

    sendJson(data) {
        if (this.ws.readyState !== WebSocket.OPEN) {
            this.pendingJson.push(data);
            return;
        }
        this.sendBinary(WebSocketClient.MESSAGE_TYPES.JSON, new TextEncoder().encode(JSON.stringify(data)));
    }

    // audioData is float32 PCM
    sendMixedAudio(audioData) {
        if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) return;
        this.sendBinary(WebSocketClient.MESSAGE_TYPES.AUDIO, audioData);
    }

    sendPerParticipantAudio(participantId, audioData) {
        if (this.ws.readyState !== WebSocket.OPEN || !this.mediaSendingEnabled) return;
        const idBytes = new TextEncoder().encode(participantId);
        this.sendBinary(WebSocketClient.MESSAGE_TYPES.PER_PARTICIPANT_AUDIO, Uint8Array.of(idBytes.length), idBytes, audioData);
    }
}

window.ws = new WebSocketClient();
