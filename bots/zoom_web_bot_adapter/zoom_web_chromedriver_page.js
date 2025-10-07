ZoomMtg.preLoadWasm()
ZoomMtg.prepareWebSDK()

var zoomInitialData = window.zoomInitialData;

var authEndpoint = '';
var sdkKey = zoomInitialData.sdkKey;
var meetingNumber = zoomInitialData.meetingNumber;
var passWord = zoomInitialData.meetingPassword;
var role = 0;
var userName = initialData.botName;
var userEmail = '';
var registrantToken = '';
var recordingToken = zoomInitialData.joinToken || zoomInitialData.appPrivilegeToken;
var zakToken = zoomInitialData.zakToken;
var leaveUrl = 'https://zoom.us';
var userEnteredMeeting = false;

class TranscriptMessageFinalizationManager {
    constructor() {
      this._activeMessages = new Map();  // Map<userId, message>
    }

    sendMessage(message) {
        const messageConverted = {
            deviceId: message.userId.toString(),
            captionId: message.msgId,
            text: message.text ? message.text.replace(/\x00/g, '') : '',
            isFinal: !!message.done
        };
        
        window.ws.sendClosedCaptionUpdate(messageConverted);
    }
  
    addMessage(message) {
        const existingMessageForUser = this._activeMessages.get(message.userId);
        if (existingMessageForUser) {
            if (existingMessageForUser.msgId !== message.msgId) {
                // If there is an existing active message for this user with a different messageId, then we need to finalize the old message
                this.sendMessage({...existingMessageForUser, done: true});
                this._activeMessages.delete(message.userId);
            }
        }
        this._activeMessages.set(message.userId, message);
        this.sendMessage(message);
        if (message.done)
            this._activeMessages.delete(message.userId);
    }
}

const transcriptMessageFinalizationManager = new TranscriptMessageFinalizationManager();

function joinMeeting() {
    const signature = zoomInitialData.signature;
    startMeeting(signature);
}

function startMeeting(signature) {

  document.getElementById('zmmtg-root').style.display = 'block'

    ZoomMtg.init({
        leaveUrl: leaveUrl,
        patchJsMedia: true,
        leaveOnPageUnload: true,
        disableZoomLogo: true,
        disablePreview: true,
        enableWaitingRoomPreview: false,
        //isSupportCC: true,
        //disableJoinAudio: true,
        //isSupportAV: false,
        success: (success) => {
            console.log('startMeeting success');
            console.log(success)

            // Hacky interception of the console.log emitted by the SDK to handle join failure errors
            // There doesn't seem to be any way to get them through the SDK's listeners or callbacks.
            const rawConsoleError = console.log;
            console.log = function firstArgIsMsg(...args) {
                const msg = args[0];
                const code = args[1];
                const reason = args[2];

                try {
                    if (
                        typeof msg === 'string' &&
                        msg.startsWith('join error code:') &&
                        typeof code === 'number' &&
                        typeof reason === 'string'
                    )
                        handleJoinFailureFromConsoleIntercept(code, reason);
                }
                catch (error) {
                }
                // still print through to the real console
                rawConsoleError.apply(console, args);
            };

            ZoomMtg.join({
            signature: signature,
            sdkKey: sdkKey,
            meetingNumber: meetingNumber,
            passWord: passWord,
            userName: userName,
            userEmail: userEmail,
            tk: registrantToken,
            recordingToken: recordingToken,
            zak: zakToken,
            success: (success) => {
                console.log('join success');
                console.log(success);

                /*
                We don't need to do this because user events include the self attribute.
                ZoomMtg.getCurrentUser({
                    success: (currentUser) => {
                        console.log('ZoomMtg.getCurrentUser()', currentUser);
                        currentUser = currentUser.result.currentUser;
                    },
                    error: (error) => {
                        console.log('ZoomMtg.getCurrentUser() error', error);
                    }
                })
                */
            },
            error: (error) => {
                console.log('join error');
                console.log(error);

                window.ws.sendJson({
                    type: 'MeetingStatusChange',
                    change: 'failed_to_join',
                    reason: error
                });
            },
            })
        },
        error: (error) => {
            console.log('startMeeting error');
            console.log(error)
        }
    })

    ZoomMtg.inMeetingServiceListener('onActiveSpeaker', function (data) {
        /*
        [
            {
                "userId": 16778240,
                "userName": "Noah Duncan"
            }
        ]
        */
        for (const activeSpeaker of data) {
            window.dominantSpeakerManager.addCaptionAudioTime(Date.now(), activeSpeaker.userId);
        }
        // Use active speaker events to determine if we are silent or not
        window.ws.sendJson({
            type: 'SilenceStatus',
            isSilent: false
        });
    });

    ZoomMtg.inMeetingServiceListener('onJoinSpeed', function (data) {
        console.log('onJoinSpeed', data);
        // This means that the user was initially in the waiting room
        if (data.level == 6) {
            console.log('onJoinSpeed: level 6, user was in waiting room');
        }
        //joinMeeting
        // Level 13 means "user start join audio" which means we actually got into the meeting and are out of the waiting room
        if (data.level == 13)
        {
            userEnteredMeeting = true;
            window.ws.sendJson({
                type: 'ChatStatusChange',
                change: 'ready_to_send'
            });
        }
    });

    ZoomMtg.inMeetingServiceListener('onMeetingStatus', function (data) {
        console.log('onMeetingStatus', data);

        // 3 means disconnected
        if (data.meetingStatus === 3) {
            // Only send the message if we've got into the meeting
            if (userEnteredMeeting)
                window.ws.sendJson({
                    type: 'MeetingStatusChange',
                    change: 'meeting_ended'
                });
        }
    });

    ZoomMtg.inMeetingServiceListener('onReceiveTranscriptionMsg', function (item) {
        console.log('onReceiveTranscriptionMsg', item);

        if (!item.msgId) {
            window.ws.sendJson({
                type: 'TranscriptMessageError',
                error: 'No msgId',
                item: item
            });
            return;
        }

        if (!window.initialData.collectCaptions)
            return;

        transcriptMessageFinalizationManager.addMessage(item);
    });

    ZoomMtg.inMeetingServiceListener('onReceiveChatMsg', function (chatMessage) {
        console.log('onReceiveChatMsg', chatMessage);

        try {
            window.ws.sendJson({
                type: 'ChatMessage',
                message_uuid: chatMessage.content.messageId,
                participant_uuid: chatMessage.senderId.toString(),
                timestamp: Math.floor(parseInt(chatMessage.content.t) / 1000),
                text: chatMessage.content.text,
            });
        }
        catch (error) {
            window.ws.sendJson({
                type: 'ChatMessageError',
                error: error.message
            });
        }
    });

    ZoomMtg.inMeetingServiceListener('onUserJoin', function (data) {
        console.log('onUserJoin', data);
        if (!data.userId) {
            console.log('onUserJoin: no userId, skipping');
            return;
        }
        const dataWithState = {
            ...data,
            state: 'active'
        }
        window.userManager.singleUserSynced(dataWithState);
    });

    ZoomMtg.inMeetingServiceListener('onUserLeave', function (data) {
        console.log('onUserLeave', data);

        // reasonCode Return the reason the current user left.
        const reasonCode = {
            OTHER: 0, // Other reason.
            HOST_ENDED_MEETING: 1, // Host ended the meeting.
            SELF_LEAVE_FROM_IN_MEETING: 2, // User (self) left from being in the meeting.
            SELF_LEAVE_FROM_WAITING_ROOM: 3, // User (self) left from the waiting room.
            SELF_LEAVE_FROM_WAITING_FOR_HOST_START: 4, // User (self) left from waiting for host to start the meeting.
            MEETING_TRANSFER: 5, // The meeting was transferred to another end to open.
            KICK_OUT_FROM_MEETING: 6, // Removed from meeting by host or co-host.
            KICK_OUT_FROM_WAITING_ROOM: 7, // Removed from waiting room by host or co-host.
            LEAVE_FROM_DISCLAIMER: 8, // User click cancel in disclaimer dialog 
        };

        if (!data.userId) {
            if (data.reasonCode == reasonCode.KICK_OUT_FROM_WAITING_ROOM) {
                window.ws.sendJson({
                    type: 'MeetingStatusChange',
                    change: 'failed_to_join',
                    reason: {
                        method: 'removed_from_waiting_room'
                    }
                });
            }

            if (data.reasonCode == reasonCode.KICK_OUT_FROM_MEETING || data.reasonCode == reasonCode.OTHER || data.reasonCode == reasonCode.HOST_ENDED_MEETING) {
                window.ws.sendJson({
                    type: 'MeetingStatusChange',
                    change: 'removed_from_meeting'
                });
            }
            return;
        }

        const dataWithState = {
            ...data,
            state: 'inactive'
        }
        window.userManager.singleUserSynced(dataWithState);
    });

    ZoomMtg.inMeetingServiceListener('onUserUpdate', function (data) {
        console.log('onUserUpdate', data);
        if (!data.userId) {
            console.log('onUserUpdate: no userId, skipping');
            return;
        }
        const dataWithState = {
            ...data,
            state: 'active'
        }
        window.userManager.singleUserSynced(dataWithState);
    });



    ZoomMtg.inMeetingServiceListener('onMediaCapturePermissionChange', function (permissionChange) {
        console.log('onMediaCapturePermissionChange', permissionChange);

        if (permissionChange.allow)
        {
            ZoomMtg.mediaCapture({record: "start", success: (success) => {
                console.log('mediaCapture success', success);
                window.ws.sendJson({
                    type: 'RecordingPermissionChange',
                    change: 'granted'
                });
            }, error: (error) => {
                console.log('mediaCapture error', error);
            }});
        }

        if (permissionChange.allow === false)
        {
            window.ws.sendJson({
                type: 'RecordingPermissionChange',
                change: 'denied'
            });
        }        
    });
}

function handleJoinFailureFromConsoleIntercept(code, reason) {
    window.ws.sendJson({
        type: 'MeetingStatusChange',
        change: 'failed_to_join',
        reason: {
            errorCode: code,
            errorMessage: reason,
            method: 'join'
        }
    });
}

function leaveMeeting() {
    ZoomMtg.leaveMeeting({});
}

function sendChatMessage(text) {
    ZoomMtg.sendChat({
        message: text,
        success: (success) => {
            console.log('sendChatMessage success', success);
        },
        error: (error) => {
            console.log('sendChatMessage error', error);
        }
    });
}

function closeRequestPermissionModal() {
    try {
        // Find modal with class zm-modal or zm-modal-legacy
        const modals = document.querySelectorAll('div.zm-modal, div.zm-modal-legacy');
        
        for (const modal of modals) {
            // Check if this modal has a descendant with class zm-modal-body-title
            const titleDiv = modal.querySelector('div.zm-modal-body-title');
            
            if (titleDiv && titleDiv.innerText.includes('Permission needed from Meeting Host')) {
                // Found the correct modal, now look for close button within it
                const buttons = modal.querySelectorAll('button');
                
                for (const button of buttons) {
                    if (button.innerText.toLowerCase() === 'close') {
                        console.log('Clicking close button on permission modal');
                        button.click();
                        return;
                    }
                }
                
                console.log('Found permission modal but could not find close button');
                return;
            }
        }
        
        console.log('Permission modal not found');
    }
    catch (error) {
        console.log('closeRequestPermissionModal error', error);
    }
}

window.sendChatMessage = sendChatMessage;

function askForMediaCapturePermission() {
    // We need to wait a second to ask for permission because of this issue:
    // https://devforum.zoom.us/t/error-in-mediacapturepermission-api-typeerror-cannot-read-properties-of-undefined-reading-caps/96683/6
    setTimeout(() => {
        // Attempt to start capture
        ZoomMtg.mediaCapture({record: "start", success: (success) => {
            // If it succeeds, great, we're done.
            window.ws.sendJson({
                type: 'RecordingPermissionChange',
                change: 'granted'
            });

        }, error: (error) => {
            // If it fails, we need to ask for permission
            ZoomMtg.mediaCapturePermission({operate: "request", success: (success) => {
                console.log('mediaCapturePermission success', success);
            }, error: (error) => {
                console.log('mediaCapturePermission error', error);
            }});

            // Also try to close the you need to ask for permission modal
            setTimeout(() => {
                closeRequestPermissionModal();
            }, 500);
        }});
    }, 1000);
}

window.askForMediaCapturePermission = askForMediaCapturePermission;