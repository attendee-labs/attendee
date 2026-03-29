# WASEL Extensions & Integrations Guide

This guide outlines the architectural principles for extending the upstream `meetings-bots` open-source project. Our primary goal is to **maximize custom functionality** while **minimizing integration friction** with upstream updates.

## 1. The Core Philosophy: Layering over Modification
When integrating a new feature, platform, or capability (like Webex, OAuth2 Proxy, custom transcription, etc.), **do not rewrite upstream code**. Instead, build your integration as a separate, isolated component and attach minimal "hooks" to the upstream codebase.

This ensures that running `git rebase` or pulling the latest upstream commits results in zero or trivial merge conflicts.

## 2. Dependency Isolation (The Custom App Pattern)
Any new core feature must reside in a dedicated Django app, cleanly separated from the original `bots` application.

* **DO NOT** add massive WASEL-specific API endpoints to `bots/views.py`.
* **DO**: Create `wasel_bots/` or an equivalent dedicated Django app.
* **DO**: Store all custom serializers, adapters, static files, models, and celery tasks inside this new directory.

For example, our Webex integration is heavily encapsulated inside `wasel_bots/adapters/webex_bot_adapter/`, meaning upstream changes to Zoom or Teams logic will never conflict with our Webex logic.

## 3. The Hooking Strategy
When upstream logic needs to be overridden, employ the "Hook Strategy". Limit changes in upstream files to one or two lines pointing to your custom logic.

**Bad Practice (Upstream `bots/bot_controller.py`)**:
```python
def get_bot_adapter(self):
    if self.meeting_type == MeetingTypes.TEAMS:
        # 100 lines of custom logic added directly here
        pass
    if self.meeting_type == MeetingTypes.WEBEX:
        # 200 lines of custom logic added directly here
        pass
```

**Good Practice (Upstream `bots/bot_controller.py`)**:
```python
def get_bot_adapter(self):
    if self.meeting_type == MeetingTypes.WEBEX:
        # WASEL CUSTOMIZATION HOOK
        from wasel_bots.utils.webex_controller_hooks import get_webex_bot_adapter
        return get_webex_bot_adapter(self)
```
This isolates the heavy lifting to files WASEL completely owns, so if the upstream fork restructures `bot_controller.py`, resolving the merge conflict is just replacing one line.

## 4. Extending Models
When the `Bot` or `Project` model needs tracking for new properties:

* **DO NOT** bloat `bots/models.py` with hardcoded columns unless strictly necessary.
* **DO**: Use `OneToOneField` or `ForeignKey` models inside `wasel_bots/models.py`.

If you absolutely must mutate the core `Bot` payload (e.g., adding a new Enum for `MeetingTypes`), comment the addition distinctly (e.g., `# WASEL CUSTOMIZATION`) and leave the base schemas entirely intact.

## 5. Front-End Assets & Static Resolvers
Custom WebRTC browser payloads (like headless Javascript) should live in `wasel_bots/static/`.

 When extending upstream Python static servers (like `ThreadingHTTPServer`):
* Avoid hardcoded relative path traversal like `os.path.join("..", "adapters")`, which assume standard directory structures.
* Always use dynamically constructed **absolute paths** to access cross-directory files to ensure they load universally without depending on the exact execution location:
```python
os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_custom_payload.js")
```

## 6. Integrating Proxies & Middleware
For edge-layer integrations (like the upcoming **OAuth2 Proxy**):
1. **Never enforce logic directly on upstream views.** 
2. Use **Django Middleware**. Create a custom middleware in `wasel_bots/middleware.py` intercepting proxy headers natively.
3. Manage exceptions using `.env` toggles (e.g. `ENABLE_OAUTH2_PROXY=True`) so developers can run the base `meetings-bots` locally without custom infrastructure dependencies failing the server load.

## 7. Committing and Rebasing Workflow
When upgrading the base project:
1. `git fetch upstream`
2. `git rebase upstream/main`
3. Resolve the 3-4 minor hooks (the only conflicts you'll likely encounter).
4. Run integration tests (Webex, custom URLs, etc.) to ensure APIs didn't deprecate a crucial argument (like keyword parameters changing).
