ZoomMtg.preLoadWasm()
ZoomMtg.prepareWebSDK()

var zoomInitialData = window.zoomInitialData;

var authEndpoint = '';
var sdkKey = zoomInitialData.sdkKey;
var meetingNumber = zoomInitialData.meetingNumber;
var passWord = zoomInitialData.meetingPassword;
var role = 0;
var userName = initialData.botName;
var userEmail = zoomInitialData.webinarUserEmail || '';
var registrantToken = '';
var recordingToken = zoomInitialData.joinToken || zoomInitialData.appPrivilegeToken;
var zakToken = zoomInitialData.zakToken;
var onBehalfToken = zoomInitialData.onBehalfToken;
var leaveUrl = 'https://zoom.us';
var userEnteredMeeting = false;
var latestJoinSpeedLevel = null;
var userEncounteredOnBehalfTokenUserNotInMeetingError = false;
var userEncounteredGenericJoinError = false;
var recordingPermissionGranted = false;
var madeInitialRequestForRecordingPermission = false;
var sentSaveCaptionNotAllowed = false;

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

function userHasEnteredMeeting() {
    return userEnteredMeeting;
}

window.userHasEnteredMeeting = userHasEnteredMeeting;

function userIsInWaitingRoom() {
    return latestJoinSpeedLevel == 6;
}

window.userIsInWaitingRoom = userIsInWaitingRoom;

function userHasEncounteredOnBehalfTokenUserNotInMeetingError() {
    return userEncounteredOnBehalfTokenUserNotInMeetingError;
}

window.userHasEncounteredOnBehalfTokenUserNotInMeetingError = userHasEncounteredOnBehalfTokenUserNotInMeetingError;

function userHasEncounteredGenericJoinError() {
    return userEncounteredGenericJoinError;
}

window.userHasEncounteredGenericJoinError = userHasEncounteredGenericJoinError;

function startMeeting(signature) {

  document.getElementById('zmmtg-root').style.display = 'block'

    const defaultView = window.initialData.recordingView === 'gallery_view' ? 'gallery' : 'speaker';

    ZoomMtg.init({
        leaveUrl: leaveUrl,
        patchJsMedia: true,
        leaveOnPageUnload: true,
        disableZoomLogo: true,
        disablePreview: true,
        enableWaitingRoomPreview: false,
        defaultView: defaultView,
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
            obfToken: onBehalfToken,
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

                if (isGenericJoinError(error?.errorCode))
                {
                    userEncounteredGenericJoinError = true;
                    return;
                }

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
            if (window.initialData.recordParticipantSpeechStartStopEvents)
                window.participantSpeechStartStopManager?.addActiveSpeaker(activeSpeaker.userId);
        }
        // Use active speaker events to determine if we are silent or not
        window.ws.sendJson({
            type: 'SilenceStatus',
            isSilent: false
        });
    });

    ZoomMtg.inMeetingServiceListener('onJoinSpeed', function (data) {
        console.log('onJoinSpeed', data);
        latestJoinSpeedLevel = data.level;
        // This means that the user was initially in the waiting room
        if (data.level == 6) {
            console.log('onJoinSpeed: level 6, user was in waiting room');
        }
        // This means the user left the waiting room
        if (data.level == 7) {
            console.log('onJoinSpeed: level 7, user left waiting room');
        }
        //joinMeeting
        // Level 13 means "user start join audio" which means we actually got into the meeting and are out of the waiting room
        if (data.level == 13)
        {
            userEnteredMeeting = true;
            // Avoids race condition where it's not actually ready to send yet
            setTimeout(() => {
                window.ws.sendJson({
                    type: 'ChatStatusChange',
                    change: 'ready_to_send'
                });
            }, 500);
        }
    });

    ZoomMtg.inMeetingServiceListener('onMeetingStatus', function (data) {
        console.log('onMeetingStatus', data);

        // Purely for logging purposes, doesn't trigger anything
        window.ws?.sendJson({
            type: 'ZoomWebMeetingStatusChange',
            statusChangeData: data,
        });

        // The 4017 code indicates that we are not able to enter the meeting because the OBF token user is not present
        if (data.errorCode == 4017)
        {
            userEncounteredOnBehalfTokenUserNotInMeetingError = true;
            console.log('handleJoinFailureFromConsoleIntercept: user encountered onbehalf token user not in meeting error');
            return;
        }

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

    /* Disabled because we are using the redux interceptor instead
    ZoomMtg.inMeetingServiceListener('onReceiveTranscriptionMsg', function (item) {
        console.log('onReceiveTranscriptionMsg', item);

        if (item === 'Save caption is not allowed!' && !sentSaveCaptionNotAllowed) {
            window.ws.sendJson({
                type: 'ClosedCaptionStatusChange',
                change: 'save_caption_not_allowed'
            });
            sentSaveCaptionNotAllowed = true;
            return;
        }
        
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
    */

    ZoomMtg.inMeetingServiceListener('onReceiveChatMsg', function (chatMessage) {
        console.log('onReceiveChatMsg', chatMessage);

        try {
            window.ws.sendJson({
                type: 'ChatMessage',
                message_uuid: chatMessage.content.messageId,
                participant_uuid: chatMessage.senderId.toString(),
                timestamp: Math.floor(parseInt(chatMessage.content.t) / 1000),
                text: chatMessage.content.text,
                to_bot: isChatMessageToBot(chatMessage),
                additional_data: getChatMessageThreadInfo(chatMessage.content.messageId),
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
        requestPermissionToRecordIfUserIsHost(dataWithState);
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
        requestPermissionToRecordIfUserIsHost(dataWithState);
    });



    ZoomMtg.inMeetingServiceListener('onMediaCapturePermissionChange', function (permissionChange) {
        console.log('onMediaCapturePermissionChange', permissionChange);

        if (permissionChange.allow)
        {
            ZoomMtg.mediaCapture({record: "start", success: (success) => {
                console.log('mediaCapture success', success);
                onRecordingPermissionGranted();
            }, error: (error) => {
                console.log('mediaCapture error', error);
            }});
        }

        if (permissionChange.allow === false)
        {
            recordingPermissionGranted = false;
            window.ws.sendJson({
                type: 'RecordingPermissionChange',
                change: 'denied'
            });
        }        
    });
}

let chatMessageThreadInfoLookupFailureCount = 0;

// The SDK's onReceiveChatMsg payload doesn't include thread info, but the chat item in redux does.
// The SDK writes the chat item to redux before firing onReceiveChatMsg, so we can look it up synchronously.
function getChatMessageThreadInfo(messageId) {
    try {
        const meetingChat = window.__reduxStore?.getState()?.newChat?.meetingChat;
        const chatItem = Array.isArray(meetingChat) && messageId ? meetingChat.find((item) => item.msgId === messageId) : null;
        if (!chatItem) {
            chatMessageThreadInfoLookupFailureCount++;
            if (chatMessageThreadInfoLookupFailureCount % 50 === 1) {
                window.ws?.sendJson({
                    type: 'ChatMessageThreadInfoLookupFailed',
                    reduxStoreFound: !!window.__reduxStore,
                    failureCount: chatMessageThreadInfoLookupFailureCount,
                });
            }
            return {};
        }

        // Match the native SDK: is_thread means a top-level message (one that can be replied to), is_comment means a reply,
        // and thread_id is the id of the thread's top-level message, so a top-level message's thread_id is its own id.
        // Note that redux's isThread means the opposite of the native SDK's IsThread: it's true for replies.
        if (!chatItem.isThread || !chatItem.mainMsgId)
            return { is_thread: true, is_comment: false, thread_id: chatItem.msgId };

        // mainMsgId is the parent's xmppMsgId (or msgId as a fallback). Map it back to the parent's msgId,
        // since that's what we record as the parent's message_uuid.
        const parentItem = meetingChat.find((item) => item.xmppMsgId === chatItem.mainMsgId || item.msgId === chatItem.mainMsgId);
        return { is_thread: false, is_comment: true, thread_id: parentItem?.msgId || chatItem.mainMsgId };
    } catch (error) {
        window.ws?.sendJson({
            type: 'ChatMessageThreadInfoLookupError',
            error: error.message,
        });
        return {};
    }
}

// The onReceiveChatMsg payload's receiverId is the receiving user's id for a private message, and a built-in id
// (0 is everyone, 1 is all panelists, 4 and 6 are the waiting room) otherwise. Zoom only delivers a private message
// to its sender and receiver, so a message is to the bot exactly when its receiver is the bot.
function isChatMessageToBot(chatMessage) {
    try {
        const botUserId = window.__reduxStore?.getState()?.meeting?.currentUser?.userId;
        return botUserId !== undefined && botUserId !== null && String(chatMessage.receiverId) === String(botUserId);
    } catch (error) {
        return false;
    }
}

function isGenericJoinError(code) {
    return code == 1 && !userEnteredMeeting;
}

function handleJoinFailureFromConsoleIntercept(code, reason) {
    // Hacky way to determine if we are being rejected because the onbehalf token user is not in the meeting
    // There currently seems to be no specific error code for this.
    if (code == 1 && onBehalfToken && !userEnteredMeeting)
    {
        userEncounteredOnBehalfTokenUserNotInMeetingError = true;
        console.log('handleJoinFailureFromConsoleIntercept: user encountered onbehalf token user not in meeting error');
        return;
    }

    if (isGenericJoinError(code))
    {
        userEncounteredGenericJoinError = true;
        return;
    }

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

// ZoomMtg.sendChat can't reply in a thread, but the SDK's internal chat send thunk (the one the reply box in the UI uses)
// takes a replyTo argument. We get at it through webpack's module registry and dispatch it on the redux store.
let zoomWebpackRequire = null;
let zoomChatSendThunkCreator = null;

function getZoomWebpackRequire() {
    if (zoomWebpackRequire)
        return zoomWebpackRequire;
    const chunkGlobal = window.webpackChunk_zoom_webclientjs;
    if (!chunkGlobal)
        return null;
    // Pushing a chunk with an unknown id makes the webpack runtime call our callback with __webpack_require__
    chunkGlobal.push([['attendee_webpack_require_probe'], {}, (require) => { zoomWebpackRequire = require; }]);
    return zoomWebpackRequire;
}

function isZoomChatSendThunkCreator(fn) {
    if (typeof fn !== 'function' || fn.length !== 3)
        return false;
    const source = fn.toString();
    return source.includes('replyTo') && source.includes('WS_CONF_CHAT_REQ');
}

// Finds an export of one of the Zoom web client's webpack modules. Tries the module id and export name from the SDK version
// we currently pin first, then searches every module whose factory source contains all of factorySourceMarkers, so this
// keeps working if module ids / export names change in an SDK upgrade.
function findZoomModuleExport(pinnedModuleId, pinnedExportName, factorySourceMarkers, isMatch) {
    const require = getZoomWebpackRequire();
    if (!require?.m)
        return null;

    try {
        if (require.m[pinnedModuleId] && isMatch(require(pinnedModuleId)?.[pinnedExportName]))
            return require(pinnedModuleId)[pinnedExportName];
    } catch (error) {}

    for (const moduleId of Object.keys(require.m)) {
        const factorySource = require.m[moduleId].toString();
        if (!factorySourceMarkers.every((marker) => factorySource.includes(marker)))
            continue;
        try {
            const moduleExports = require(moduleId);
            for (const exportName of Object.keys(moduleExports)) {
                if (isMatch(moduleExports[exportName]))
                    return moduleExports[exportName];
            }
        } catch (error) {}
    }
    return null;
}

function getZoomChatSendThunkCreator() {
    // Pinned SDK version: module 17984, export dX
    if (!zoomChatSendThunkCreator)
        zoomChatSendThunkCreator = findZoomModuleExport(17984, 'dX', ['replyTo', 'WS_CONF_CHAT_REQ'], isZoomChatSendThunkCreator);
    return zoomChatSendThunkCreator;
}

// Built-in chat receivers (see ZoomMtg.sendChat). Zoom doesn't support thread replies to waiting room chats.
const ZOOM_CHAT_RECEIVER_WAITING_ROOM_USERS = 4;
const ZOOM_CHAT_RECEIVER_WAITING_ROOM_TO_HOSTS = 6;

function sendThreadedChatMessage(text, threadId) {
    const store = window.__reduxStore;
    if (!store)
        return 'Redux store not found';
    const meetingChat = store.getState()?.newChat?.meetingChat;
    if (!Array.isArray(meetingChat))
        return 'Chat list not found in redux';

    let parentItem = meetingChat.find((item) => item.msgId === threadId || item.xmppMsgId === threadId);
    // If we were given a reply's id, reply in the thread it belongs to
    if (parentItem?.isThread && parentItem.mainMsgId)
        parentItem = meetingChat.find((item) => item.xmppMsgId === parentItem.mainMsgId || item.msgId === parentItem.mainMsgId);
    if (!parentItem)
        return 'Thread parent message not found';

    // Mirrors how the Zoom UI picks the receiver for a reply: same audience as the parent message
    if (parentItem.isSilentModeUser || parentItem.receiverId === ZOOM_CHAT_RECEIVER_WAITING_ROOM_USERS || parentItem.receiverId === ZOOM_CHAT_RECEIVER_WAITING_ROOM_TO_HOSTS)
        return 'Thread replies are not supported for waiting room chats';
    let receiverId = parentItem.receiverId;
    if (parentItem.isShowPrivately && !parentItem.isMyMessage)
        receiverId = parentItem.senderId;
    if (receiverId === undefined || receiverId === null)
        return 'Could not determine receiver for thread reply';

    const createSendThunk = getZoomChatSendThunkCreator();
    if (!createSendThunk)
        return 'Zoom chat send function not found';

    store.dispatch(createSendThunk(text.trimEnd(), receiverId, {
        mainMsgId: parentItem.xmppMsgId || parentItem.msgId,
        mainMsgTime: parentItem.t,
        owner: parentItem.ownerRes,
    }));
    return null;
}

function sendChatMessage(text, to_user_uuid, thread_id) {
    if (thread_id) {
        let error;
        try {
            error = sendThreadedChatMessage(text, thread_id);
        } catch (e) {
            error = e.message;
        }
        if (!error)
            return;
        // Don't drop the message, send it without a thread instead
        window.ws.sendJson({
            type: 'sendThreadedChatMessageError',
            error: error,
            thread_id: thread_id,
        });
    }

    ZoomMtg.sendChat({
        message: text,
        userId: to_user_uuid ? parseInt(to_user_uuid, 10) : 0,
        success: (success) => {
            console.log('sendChatMessage success', success);
        },
        error: (error) => {
            console.log('sendChatMessage error', error);
            window.ws.sendJson({
                type: 'sendChatMessageError',
                error: JSON.stringify(error)
            });
        }
    });
}

function changeGalleryViewPage(changeToNextPage) {
    /*
    HTML looks like this:
    <button type="button" class="gallery-video-container__switch-button gallery-video-container__switch-button--back"><div class="gallery-video-container__arrow gallery-video-container__arrow--back"></div><div class="gallery-video-container__pagination">1/5</div></button>
    <button type="button" class="gallery-video-container__switch-button" style="right: 0px;"><div class="gallery-video-container__arrow gallery-video-container__arrow--next"></div><div class="gallery-video-container__pagination">1/5</div></button>
    */
    try {
        let button;
        if (changeToNextPage) {
            // Find the next button (the one without the --back modifier)
            button = document.querySelector('.gallery-video-container__switch-button:not(.gallery-video-container__switch-button--back)');
        } else {
            // Find the back/previous button
            button = document.querySelector('.gallery-video-container__switch-button--back');
        }
        
        if (button) {
            button.click();
        }
    } catch (error) {
        // Do nothing if button cannot be identified
    }
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

// ZoomMtg has no API for chat reactions. The reaction picker in the Zoom UI dispatches the SDK's internal "send vote emoji" thunk,
// sendVoteEmoji(emoji, msgId, addToRecentEmojis), which sends the reaction as a WS_CONF_CHAT_REQ with an encoded emoji xmpp payload.
// The thunk toggles: it removes the reaction if the current user already reacted with that emoji, otherwise it adds it.
let zoomSendVoteEmojiThunkCreator = null;
let zoomMeetingConfig = null;

function isZoomSendVoteEmojiThunkCreator(fn) {
    if (typeof fn !== 'function' || fn.length !== 3)
        return false;
    const source = fn.toString();
    return source.includes('voteMap') && source.includes('encodedEmoji') && source.includes('WS_CONF_CHAT_REQ');
}

function getZoomSendVoteEmojiThunkCreator() {
    // Pinned SDK version: module 90978, export D
    if (!zoomSendVoteEmojiThunkCreator)
        zoomSendVoteEmojiThunkCreator = findZoomModuleExport(90978, 'D', ['voteMap', 'encodedEmoji', 'WS_CONF_CHAT_REQ'], isZoomSendVoteEmojiThunkCreator);
    return zoomSendVoteEmojiThunkCreator;
}

function getZoomMeetingOptions() {
    // Pinned SDK version: module 67954, export Ay (the web client's MeetingConfig)
    if (!zoomMeetingConfig)
        zoomMeetingConfig = findZoomModuleExport(67954, 'Ay', ['isNewJoinFlow', 'MeetingConfig'], (config) => !!config && typeof config === 'object' && 'meetingOptions' in config);
    return zoomMeetingConfig?.meetingOptions || window.MeetingConfig?.meetingOptions || null;
}

// When an account limits chat reactions, the Zoom UI only offers these (compared without skin tone or variation selector)
const ZOOM_LIMITED_CHAT_REACTION_EMOJIS = ['\u{1F44F}', '\u{1F44D}', '\u{1F602}', '\u{1F62E}', '\u{2764}', '\u{1F389}'];

function sendChatMessageReactionOrGetError(messageId, emoji, action) {
    const store = window.__reduxStore;
    if (!store)
        return 'Redux store not found';
    const meetingChat = store.getState()?.newChat?.meetingChat;
    if (!Array.isArray(meetingChat))
        return 'Chat list not found in redux';

    const chatItem = meetingChat.find((item) => item.msgId === messageId || item.xmppMsgId === messageId);
    if (!chatItem)
        return 'Chat message not found';

    const meetingOptions = getZoomMeetingOptions();
    if (meetingOptions?.isEnableMeetingChatReaction === false)
        return 'Chat reactions are disabled in this meeting';
    if (action === 'add' && meetingOptions?.newMeetingChatExperience?.emojiScopeSelectInMeetingChat) {
        const baseEmoji = emoji.replace(/[\u{FE0F}\u{1F3FB}-\u{1F3FF}]/gu, '');
        if (!ZOOM_LIMITED_CHAT_REACTION_EMOJIS.includes(baseEmoji))
            return 'This meeting only allows these chat reactions: \u{1F44F} \u{1F44D} \u{1F602} \u{1F62E} \u{2764}\u{FE0F} \u{1F389}';
    }

    // The thunk toggles, so only dispatch it when it will do what was asked. It keys voteMap by the emoji without zero width characters.
    const voteMapKey = emoji.replace(/[\u200B-\u200D\uFEFF]/g, '');
    const alreadyReacted = !!chatItem.voteMap?.get?.(voteMapKey)?.selfSent;
    if ((action === 'add') === alreadyReacted)
        return null;

    const createSendVoteEmojiThunk = getZoomSendVoteEmojiThunkCreator();
    if (!createSendVoteEmojiThunk)
        return 'Zoom chat reaction function not found';

    // The UI passes the emoji picker's emoji data; the thunk only needs content unless addToRecentEmojis is true
    const result = store.dispatch(createSendVoteEmojiThunk({ content: emoji }, chatItem.xmppMsgId || chatItem.msgId, false));
    // On success the thunk returns the promise for the send. Otherwise it returns nothing (message not found) or the
    // action for the error toast it shows (no permission to chat with the message's audience, or a waiting room chat).
    if (typeof result?.then !== 'function')
        return 'Zoom did not send the reaction';
    result.catch((error) => {
        window.ws?.sendJson({
            type: 'sendChatMessageReactionError',
            error: error?.message || String(error),
            message_id: messageId,
        });
    });
    return null;
}

function sendChatMessageReaction(messageId, emoji, action) {
    let error;
    try {
        error = sendChatMessageReactionOrGetError(messageId, emoji, action);
    } catch (e) {
        error = e.message;
    }
    if (!error)
        return;
    window.ws?.sendJson({
        type: 'sendChatMessageReactionError',
        error: error,
        message_id: messageId,
    });
}

window.sendChatMessageReaction = sendChatMessageReaction;

function askForMediaCapturePermission() {
    madeInitialRequestForRecordingPermission = true;
    // We need to wait a second to ask for permission because of this issue:
    // https://devforum.zoom.us/t/error-in-mediacapturepermission-api-typeerror-cannot-read-properties-of-undefined-reading-caps/96683/6
    setTimeout(() => {
        // Attempt to start capture
        ZoomMtg.mediaCapture({record: "start", success: (success) => {
            // If it succeeds, great, we're done.
            onRecordingPermissionGranted();

        }, error: (error) => {
            // Also try to close the you need to ask for permission modal
            setTimeout(() => {
                closeRequestPermissionModal();
            }, 500);

            // If it fails, we need to ask for permission
            // If this line throws this error 
            // https://devforum.zoom.us/t/web-sdk-typeerror-cannot-read-properties-of-undefined-reading-caps/122712
            // it's because host was not present
            ZoomMtg.mediaCapturePermission({operate: "request", success: (success) => {
                console.log('mediaCapturePermission success', success);
            }, error: (error) => {
                console.log('mediaCapturePermission error', error);
            }});
        }});
    }, 1000);
}

function requestPermissionToRecordIfUserIsHost(data) {
    if (!data.isHost)
        return;
    if (recordingPermissionGranted)
        return;

    setTimeout(() => {
        if (recordingPermissionGranted)
            return;
        if (!madeInitialRequestForRecordingPermission)
            return;
        window.ws?.sendJson({
            type: 'RequestingRecordingPermissionAfterHostJoin',
            userData: data
        });
        // Ask for permission
        ZoomMtg.mediaCapturePermission({operate: "request", success: (success) => {
            console.log('mediaCapturePermission success', success);
        }, error: (error) => {
            console.log('mediaCapturePermission error', error);
        }});
    }, 1000);
}

function onRecordingPermissionGranted() {
    recordingPermissionGranted = true;
    window.ws.sendJson({
        type: 'RecordingPermissionChange',
        change: 'granted'
    });
}

window.askForMediaCapturePermission = askForMediaCapturePermission;