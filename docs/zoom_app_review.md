# Getting your Attendee-powered Zoom App Approved
*by Frazer Kearl ([MeetDoris](https://meetdoris.com/))*

## Basic Information

For **Attendee** to function as an anonymous attendee:  
- No Zoom user information is required.  
- No OAuth scopes are required.  
- You can set the redirect URL to your app’s home page (e.g. `/home`, `/dashboard`).  

This makes the application process less stringent than it would otherwise be.

## Embed

- Select **Meeting SDK**.

## Scopes

- `user:read:zak` will always be selected.  
- Description we added:  

> Our app embeds the Zoom Meeting SDK.  
> Zoom’s Marketplace builder automatically adds the `user_zak:read` scope whenever the Meeting SDK feature is enabled.  
>
> We do **not** call the `/users/{id}/token?type=zak` endpoint in our current flow.  
>
> We join meetings with `role = 0` (participant/guest), so a ZAK is never requested or stored.  
>
> No other Zoom REST APIs are used; no additional scopes are requested.  
>
> The scope remains in the manifest only because it is required by the Meeting SDK toggle. It enables future host-start functionality without forcing another review, but it is not exercised in production today.

---

## App Listing

- Your app **does not need to be listed** for Attendee to record external Zoom meetings.  
- Your Zoom app just needs to be **approved**.  
- You still need to fill this section out, along with images.  

Once the app is approved:  
- Users don’t need to take any action to “approve” your software.  
- Once Zoom approves it, it’s good to go.

## Technical Design

### Technology Stack (High-Level Only)
> The form says “describe in detail,” but for basic Attendee you can keep it high-level. You **do not** need to list every library—only the major components.

**Example (edit to match your setup):**
- **Frontend:** React 18.3+, Material UI  
- **Backend:** Python 3.11, Flask  
- **Auth:** Auth0 (OIDC)  
- **Data/Storage:** Azure SQL Database  
- **Hosting:** Azure App Service (backend), Azure Static Web Apps (frontend)  
- **CI/CD & Security:** GitHub Actions, CodeQL SAST  
- **Observability:** Azure Application Insights, centralized logging  
- **Zoom Integration:** Zoom **Meeting SDK (Web)** only; no Zoom REST APIs are used in the Attendee flow

### Architecture Diagram
What we submitted:
![Architecture Diagram Example](https://raw.githubusercontent.com/attendee-labs/attendee/refs/heads/main/static/images/zoom_app_review_architecture_diagram_example.png)

### Application Development
- **Do you have a SSDLC?**  
  - Select **Yes**.  
  - Submit the document detailing your secure development practices (requirements, code reviews, secrets management, dependency scanning, etc.).

- **Does your app undergo SAST?**  
  - Select **Yes**.  
  - We submitted a screenshot of the CodeQL analysis results from our CI/CD pipeline.

- **Does your app periodically undergo 3rd party app testing?**  
  - Select whichever applies.  
  - Not needed for basic Attendee implementation.

### Additional Documents
- Submit whatever you have — the more, the better.  
- SOC 2 or ISO 27001 certs are not required.  
- They’ll refer to excerpts from your privacy policy (you’ll point these out in a later question).

---

## Security

- **Does your app use TLS 1.2 & above?**  
  - Select **Yes** (and make sure it does).

- **Is the integration utilising verification tokens or secret tokens and `z-zm-signature` header?**  
  - No, we’re not using Zoom webhooks with basic Attendee implementation.

- **Does your app collect, store, log, or retain Zoom user data?**  
  - No — hence no OAuth scopes.

---

## Privacy

- **Does your app collect info from under 16s?**  
  - No (include terms to that effect in your T&Cs & privacy policy).

- **Is your app intended for education, healthcare, govt?**  
  - No.

- Provide excerpts from your privacy policy:  
  - Data subject access rights.  
  - Confirmation that users can exercise those rights.

---

## App Submission

Steps:  
1. Verify your domain.  
2. Provide some test credentials (for the usability review).  
3. Submit!

---

## Review Process

### Usability Review
- Your app goes into an approval queue and is assigned a reviewer.  
- They’ll log in and use your app as described.  
- If they encounter issues, you’ll get a “more information required” request.

**Tip:** Ask for a meeting.  
- We had a 30-min call with the reviewer, answered questions, and got approved on the spot.

### Security Review
- They’ll use Burp to intercept and manipulate requests between frontend & server.  
- Ensure privileged UI (like admin controls) has proper **server-side** validation.

If no issues: ✅ You passed.  
Your app is approved and the Attendee Bot can join any external Zoom meeting.