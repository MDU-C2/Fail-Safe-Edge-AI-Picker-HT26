# GPT-6 Astra dashboard assistant

Open **Assistant** in the cup dashboard to type a message. Open the separate **Settings** tab to configure the API key or pair a phone. The assistant and named-cup features run on the MacBook. GPT-6 Astra handles language and real saved-cup visual matching through the same API client and settings. YOLO remains responsible for cup detection.

Use direct `--gateway` mode, or the combined `--device-config` dashboard with its Mac entry configured as `backend: "local"`. The combined dashboard shows **Processing device** above the tabs. Select MacBook to use Assistant; select Pi or Lynx for their ordinary picking controls. Assistant is disabled during switching and while Pi/Lynx is selected. Older configurations that use a separate Mac HTTP service also keep Assistant disabled. No assistant model or credentials are deployed to Pi or Lynx. See [device setup](DEVICE_DASHBOARD.md).

## Test without a gateway

From this folder:

```sh
python capture_and_detect.py --web --demo --port 8087
```

Open `http://localhost:8087`, click **Scan** in **Pick cups**, then open **Assistant**.

- With no key, scene answers are explicitly labelled **Scripted demo (not GPT)**. Try “Pick the white cup” or “Which cups are available?” Exact saved-cup commands are labelled separately and also run locally.
- On the Mac, open **Settings**, paste an OpenAI API key, then use **Save settings** and **Test connection**. This uses the real `gpt-6-astra` model with the synthetic scene, while robot operations remain simulated. The sample illustration is represented by explicit sample attributes; actual camera scans include their bounded PNG/JPEG image when available.
- A picking request automatically prepares a review inside **Assistant**. Review the selected cups and grid destinations, then click its confirmation button once. The language model never confirms movement; there is no second confirmation in Pick cups.

For a scene-dependent request, Assistant takes one camera-only scan when the current photo is missing or unusable. You do not need to press Scan first. `Take a photo of the workspace` explicitly refreshes the image, without an Astra call. The resulting photo appears in Assistant. Scanning does not move the robot, clear recovery or replace an existing pick/control preview; finish or cancel a preview first. Camera work has a 45-second wait limit and is never automatically retried.

Cup numbers can change between scans. If a numbered request needs a new photo, Assistant shows the new photo and asks you to check the number again, without selecting a cup. `Remember cup 2 as …` deliberately uses the displayed photo. A saved-name pick reuses the current usable snapshot; it scans only when no usable snapshot exists.

Give a complete request each time: conversation history is displayed for reference but is not submitted as context. A suggestion expires after two minutes and is invalidated by a new scan, device change, recovery/stop state or changed movement settings.

To try naming in the demo, send `Remember cup 2 as Kasun's cup`, click **Save cup reference**, then send `Pick Kasun's cup`. Matching prepares an inline review automatically; use its preview button to simulate the sequence. The demo uses scripted sample identities, not real visual recognition. Its isolated references disappear when the demo server closes.

## Supported requests

The assistant can explain the recorded scene, match a description to a detected cup, and prepare a selected-cup or all-available-cups preview. It can suggest groups by appearance. **Grouping does not change placement destinations or execute a sorting routine**; placement keeps the existing grid order. Requests for a subset of several cups should be made one cup at a time.

Home, gripper controls, Stop, calibration and recovery use their existing dashboard controls. The language model cannot send arbitrary positions, alter motion parameters, confirm actions or clear reservations. The operator's confirmation button inside Assistant uses the same guarded confirmation as the manual picker. A visual answer is an interpretation of a saved observation; picking uses the reviewed snapshot without another capture.

## Remember and find a named cup

After scanning, use a displayed cup number to save a reference:

- `Remember cup 2 as Kasun's cup` or `Save cup 2 as Kasun's cup` proposes a name and reference photo. Click **Save cup reference** to save it; this sends no movement command.
- `Saved cups` lists the selected picker's references.
- `Pick Kasun's cup` or `Pick saved Kasun's cup` chooses an existing reference, then compares it with the current snapshot's cups. Inspect the matched cup, reference photo and destination in the inline **Assistant** review, then confirm there once.

Exact save/list commands and selecting an existing saved name run locally. Saving a reference sends no movement or paid API request. More flexible wording uses GPT-6 Astra to propose a name or choose a supplied saved reference.

Requesting a real saved-cup pick asks Astra to compare the reference photo with
the current usable snapshot before showing the inline review. If no usable
snapshot exists, the app takes one scan. This requires the configured API key.
An ambiguous or missing match prevents a preview. Confirmation checks the
reference identity against that same saved snapshot and sends no new camera
request. Demo matching remains scripted and makes no model calls for comparisons.

In camera mode, the name and cropped reference photo are stored in the picker's `saved_cups.json` under its output directory and survive restart when that directory is retained. They are independent of the API key and chat history. Names are unique, start with a letter, and allow up to 48 characters using ASCII letters, digits, spaces, apostrophes, underscores and hyphens. Straight and curly apostrophes are treated as the same spelling. The library holds up to 32 references. Use **Forget** in **Pick cups → Saved cup references** before replacing an existing name.

Start the dashboard directly on the Mac with your normal `--gateway` arguments, then configure Astra in **Settings**. The dashboard no longer accepts `--llm`, `--llm-model` or `--ollama-url`; it makes no local language-model calls or fallback requests. The old command box accepts fixed operator commands; flexible requests belong in **Assistant**. The existing detector, placement checks, motion plan and movement confirmation remain in the picking workflow.

Named Assistant actions are available on the direct or in-process managed MacBook picker. References stay in its existing capture directory and survive switching away and back. They are not copied to Pi or Lynx. Device changes invalidate scans and pending Assistant answers; nothing is automatically picked after switching back.

## Provider and cost

The integration uses OpenAI directly, with a fixed HTTPS API origin and model `gpt-6-astra`. A ChatGPT subscription does not supply API credit. Obtain an API key through your OpenAI API account; never put it in Git or a chat message.

At implementation time, OpenAI lists Astra at $10 per million input tokens and $50 per million output tokens. Flex is half those token prices and can be slower or unavailable. Standard is the default; Low reasoning limits unnecessary reasoning work. Messages interpreted by GPT, **Test connection**, and real saved-cup visual checks can incur API charges. Exact saving/listing requests stay local. Images and reasoning affect usage. There is no continuous upload or automatic paid retry. See [OpenAI pricing](https://developers.openai.com/api/docs/pricing) and [Astra's model page](https://developers.openai.com/api/docs/models/gpt-6-astra).

Requests have bounded timeouts and share one client, which prevents overlapping API requests. A timed-out request may still have incurred provider usage; a late result is discarded. Stop stays available independently of the assistant. The saved snapshot has no age expiry. Movement confirmation still expires after 60 seconds.

## Key and data handling

Keys stay in server memory by default and disappear when the dashboard stops. **Remember on this Mac** explicitly saves to an owner-only file outside Git (by default `~/.config/cup-picker/assistant.json`, or `--assistant-settings`). **Forget key** removes the saved file and memory key. Keys are never returned in status, placed in browser storage or sent to a selected execution device.

When a message needs GPT interpretation, the request shares that message, a bounded description of the current scan/grid state, saved-cup names and IDs, and an available PNG/JPEG scene image with OpenAI. Real named-cup matching also sends the saved reference photo and bounded images of the current cup candidates. References remain stored in the Mac's library; API credentials are never placed in that library. Exact saving/listing requests do not send this context to OpenAI. The app requests `store: false` for Responses calls; provider retention still follows your OpenAI account's data policy.

## Phone and real-device use

See [Phone access](PHONE_ACCESS.md) for text-only access over HTTP on a trusted local network. No certificate installation is needed. The Mac and paired phone share one controller and one in-memory key. The phone address and pairing code are shown in **Settings** on the Mac. Phone reads and commands require pairing, and writes retain origin and CSRF checks.

When phone HTTP access is enabled, API credential changes are accepted only through the Mac's loopback address (`localhost` or `127.0.0.1`). Add or forget the key in **Settings** on the Mac. Paired phones can send text requests using the configured key; the key itself is never sent to the phone. HTTP does not encrypt local-network traffic, so use a trusted network. Requests from the Mac to OpenAI still use HTTPS.

When the gateway is available, restart using the direct or combined launcher and retain the phone-access arguments. Omit `--demo` and the removed local-LLM flags; start in dry-run mode to check a real scene. Enable `--live` only when ready to operate the robot. Close another controller before using the same robot. The combined dashboard uses the existing Pi/Lynx services; this update does not deploy new remote software. Their configured services must be reachable for switching.

## Validation

The API protocol, Astra visual comparison and credential storage tests use mocked provider responses. Coordinator and HTTP tests exercise stale scenes and libraries, Mac-only capability checks, named references, preview-only execution, pairing and CSRF. Named-cup workflow tests use synthetic images or scripted sample identities. Browser tests cover desktop and 360 px layouts and an actual gateway-free demo workflow. Real Astra access, physical-phone access, real visual matching and robot execution require subsequent testing with the user's API account and equipment.
