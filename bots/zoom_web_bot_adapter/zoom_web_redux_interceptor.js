(() => {
    if (window.__reduxStoreSnifferInstalled) return;
    window.__reduxStoreSnifferInstalled = true;
  
    const STORE_PROP = "__reduxStore";
    const EVENT_NAME = "__reduxStoreFound";
  
    function isReduxStore(x) {
      if (
        !x ||
        typeof x.getState !== "function" ||
        typeof x.dispatch !== "function" ||
        typeof x.subscribe !== "function"
      ) {
        return false;
      }
      try {
        const state = x.getState();
        return state && "meeting" in state;
      } catch (_) {
        return false;
      }
    }
  
    function exposeStore(store) {
      if (window[STORE_PROP] === store) return;
      try {
        Object.defineProperty(window, STORE_PROP, {
          value: store,
          configurable: true,
          enumerable: false,
          writable: false,
        });
      } catch (_) {
        window[STORE_PROP] = store;
      }
  
      // Optional: keep a snapshot updated (can be heavy if state is huge)
      // window.__reduxState = store.getState();
      // store.subscribe(() => { window.__reduxState = store.getState(); });
  
      window.dispatchEvent(new Event(EVENT_NAME));
    }
  
    function getReactInternalFromNode(node) {
      if (!node) return null;
  
      // React 16/17 root container
      if (node._reactRootContainer && node._reactRootContainer._internalRoot) {
        return node._reactRootContainer._internalRoot;
      }
  
      // React attaches internals on DOM nodes with these keys
      const keys = Object.keys(node);
      for (let i = 0; i < keys.length; i++) {
        const k = keys[i];
        if (k.startsWith("__reactContainer$") || k.startsWith("__reactFiber$")) {
          return node[k];
        }
      }
      return null;
    }
  
    function normalizeToFiber(nodeOrRoot) {
      if (!nodeOrRoot) return null;
  
      // FiberRoot has .current
      if (nodeOrRoot.current && nodeOrRoot.current.memoizedProps !== undefined) {
        return nodeOrRoot.current;
      }
  
      // FiberNode typically has .memoizedProps / .return / .child
      if (nodeOrRoot.memoizedProps !== undefined || nodeOrRoot.child || nodeOrRoot.return) {
        return nodeOrRoot;
      }
  
      return null;
    }
  
    function rootFromFiber(fiber) {
      let n = fiber;
      const seen = new Set();
      while (n && n.return && !seen.has(n.return)) {
        seen.add(n);
        n = n.return;
      }
      return n || fiber;
    }
  
    function findStoreStartingAtFiber(fiber) {
      const start = rootFromFiber(fiber);
      const stack = [start];
      const seen = new Set();
      let steps = 0;
      const MAX_STEPS = 100000;
  
      while (stack.length && steps < MAX_STEPS) {
        const f = stack.pop();
        steps++;
        if (!f || seen.has(f)) continue;
        seen.add(f);
  
        const mp = f.memoizedProps;
        if (mp && isReduxStore(mp.store)) return mp.store;
  
        const pp = f.pendingProps;
        if (pp && isReduxStore(pp.store)) return pp.store;
  
        // DFS
        if (f.sibling) stack.push(f.sibling);
        if (f.child) stack.push(f.child);
      }
      return null;
    }
  
    function candidateRootElements() {
      const sels = ["#root", "#app", "#__next", "#gatsby-focus-wrapper", "[data-reactroot]"];
      const out = [];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el) out.push(el);
      }
      if (document.body) out.push(document.body);
      return out;
    }
  
    function scanForStoreOnce() {
      if (window[STORE_PROP] && isReduxStore(window[STORE_PROP])) return true;
  
      // 1) Try common roots first
      const roots = candidateRootElements();
      for (const el of roots) {
        const internal = getReactInternalFromNode(el);
        const fiber = normalizeToFiber(internal);
        if (fiber) {
          const store = findStoreStartingAtFiber(fiber);
          if (store) {
            exposeStore(store);
            return true;
          }
        }
      }
  
      // 2) Limited DOM scan fallback (avoid heavy full-page scans)
      const walker = document.createTreeWalker(
        document.documentElement || document,
        NodeFilter.SHOW_ELEMENT
      );
      let count = 0;
      const MAX_NODES = 2000;
  
      while (walker.nextNode() && count < MAX_NODES) {
        count++;
        const el = walker.currentNode;
        const internal = getReactInternalFromNode(el);
        const fiber = normalizeToFiber(internal);
        if (!fiber) continue;
  
        const store = findStoreStartingAtFiber(fiber);
        if (store) {
          exposeStore(store);
          return true;
        }
      }
  
      return false;
    }
  
    function startWatching() {
      // Try immediately (in case of very fast mount/hydration)
      if (scanForStoreOnce()) return;
  
      // Watch DOM mutations to retry cheaply as app mounts
      const mo = new MutationObserver(() => {
        if (scanForStoreOnce()) mo.disconnect();
      });
  
      const target = document.documentElement || document;
      try {
        mo.observe(target, { childList: true, subtree: true });
      } catch (_) {}
  
      // Poll indefinitely at 1s intervals until the store is found
      const timer = setInterval(() => {
        if (scanForStoreOnce()) {
          clearInterval(timer);
          try { mo.disconnect(); } catch (_) {}
        }
      }, 1000);
    }
  
    // Promise helper for Selenium
    window.__waitForReduxStore = function __waitForReduxStore(timeoutMs = 10000) {
      if (window[STORE_PROP] && isReduxStore(window[STORE_PROP])) {
        return Promise.resolve(window[STORE_PROP]);
      }
      return new Promise((resolve, reject) => {
        const t = setTimeout(() => {
          window.removeEventListener(EVENT_NAME, onFound);
          reject(new Error("Timed out waiting for Redux store"));
        }, timeoutMs);
  
        function onFound() {
          if (window[STORE_PROP] && isReduxStore(window[STORE_PROP])) {
            clearTimeout(t);
            window.removeEventListener(EVENT_NAME, onFound);
            resolve(window[STORE_PROP]);
          }
        }
  
        window.addEventListener(EVENT_NAME, onFound);
        // Kick an extra scan right away
        scanForStoreOnce();
      });
    };
  
    setTimeout(startWatching, 1000);
  })();

class LiveTranscriptListWatcher {
    constructor() {
      this.currentMessageMap = new Map(); // msgId -> message data
    }

    processLiveTranscriptChange(newLTMessage) {
      if (!newLTMessage || typeof newLTMessage !== 'object') return;

      if (!window.initialData?.collectCaptions) return;

      const next = new Map(Object.entries(newLTMessage));
      const prev = this.currentMessageMap;

      for (const [msgId, message] of next) {
        const old = prev.get(msgId);

        // Only forward to finalization manager if new or changed
        if (!old || old.text !== message.text || old.done !== message.done) {
          if (message.user?.userId != null) {
            // It's defined in zoom_web_chromedriver_page.js. Ugly.
            transcriptMessageFinalizationManager.addMessage({
              userId: message.user.userId,
              msgId: message.msgId,
              text: message.text,
              done: message.done,
            });
          }
        }
      }

      this.currentMessageMap = next;
    }
}

const liveTranscriptListWatcher = new LiveTranscriptListWatcher();
window.liveTranscriptListWatcher = liveTranscriptListWatcher;

function onReduxStoreFound() {
    const store = window.__reduxStore;
    if (!store) return;
  
    store.subscribe(() => {
      const next = store.getState();
      window.liveTranscriptListWatcher.processLiveTranscriptChange(next.newLiveTranscription?.newLTMessage || {});
    });
  }
  
  if (window.__reduxStore) {
    onReduxStoreFound();
  } else {
    window.addEventListener("__reduxStoreFound", onReduxStoreFound, { once: true });
  }