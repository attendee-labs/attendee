/**
 * Webex Bot Adapter - Auto-fill Payload
 * 
 * This script is injected by the Python adapter to provide initial data.
 * The app.js will automatically detect window.webexInitialData and auto-join.
 */

(function() {
    console.log('[Webex Payload] Autofill script loaded');
    console.log('[Webex Payload] Initial data:', window.webexInitialData);
    
    // The app.js will automatically detect window.webexInitialData on DOMContentLoaded
    // and start the auto-join process. We just need to ensure the data is available.
    
    if (!window.webexInitialData) {
        console.error('[Webex Payload] ERROR: window.webexInitialData not found!');
        console.error('[Webex Payload] The Python adapter should have set this before loading this script.');
        
        // Set error status for Python adapter
        window.webexJoinStatus = {
            status: "error",
            message: "Missing webexInitialData - adapter initialization failed",
            type: "initialization_error",
            code: "MISSING_INITIAL_DATA"
        };
    } else {
        console.log('[Webex Payload] ✅ Initial data available - app.js will auto-join');
        console.log('[Webex Payload] Access token length:', window.webexInitialData.accessToken?.length || 0);
        console.log('[Webex Payload] Meeting destination:', window.webexInitialData.meetingDestination);
        console.log('[Webex Payload] WebSocket URL:', window.webexInitialData.websocketUrl);
    }
})();

