# Live demo video script (about 4 min 30 s, 5 min maximum)

The launch video sells the idea. This one proves it: a real laptop, a real room, real objects, and the
real app answering in real time. Judges should finish it believing nothing was faked.

**Format:** screen recording of the web app full frame, with a phone filming the room as a
picture-in-picture in one corner (or side by side). Keep the system clock visible on the screen
recording. Mark the key moments **"Real time · no cuts"** on screen and do not cut inside them. If a
wait runs long, speed it up and label it **"2×"** rather than cutting.

The labels in quotes below are the exact text the app shows.

## Setup (10 minutes, before recording)

1. **Room:** bright, even light. Laptop on a stand or a shelf, camera tilted down so it sees a desk
   area, a shelf area, a bare spot that you will *not* mark as a place (a chair seat or a side table),
   and the edge of the door or walkway.
2. **Props:** keys on a keyring, wallet, glasses case, pill bottle, and one book with a clear title.
   Matte, distinct objects at least 3 cm across in the picture work best. Keep them off camera.
3. **App:** open the web app and create a fresh account just for the recording, so **My home**
   starts with an empty memory (not the Demo home). To wipe an existing home instead, run
   `python scripts/reset_memory.py --yes --household <home id>`.
4. **Do a full dry run once**, then wipe the memory again. Close other tabs, mute notifications. If
   the light changed since setup, press **Camera → Start over** so it relearns the empty room.
5. **Recorders:** screen recorder at 1080p with system audio on (so the spoken answers are heard);
   phone at 1080p on a small tripod filming the room and the laptop together.
6. **Caregiver email:** make sure the carer email on the routine is an inbox you can show on screen.

## Shot list

| # | Time | Picture | What happens on screen | Voice-over |
|---|---|---|---|---|
| 1 | 0:00–0:15 | Team on camera, room behind you. | — | "Hi, we're team iCamFind. Everything you're about to see is live: a normal laptop, a normal room, no edits in the important parts." |
| 2 | 0:15–0:45 | Screen. Click **"Use this computer's camera"**. Allow access. Wizard: "Keep the camera still", then "Show me your places". Drag boxes and name them **Desk** and **Shelf**, and mark the **Door**. **Done**, toast "Your camera is on. Put something down to try it." | Overlay: *OpenCV 5.0 WebAssembly, running in this browser tab.* | "Setup is one click and three boxes. No install, no account tokens, no app store. OpenCV 5 runs right here in the browser, and the video never leaves this laptop. Only a small photo of each object goes to AWS." |
| 3 | 0:45–1:15 | **Real time · no cuts.** Split screen. Put the keys on the desk, pull your hand away. Put the book on the shelf. Put the wallet and the pill bottle on the desk. | **Things** tab fills in: "Put down" for each, with a photo, the place and the time. The book is named from its cover. | "I put the keys down, and as soon as my hand is gone, about a second and a half later, it's remembered: keys, on the desk, with a photo. Claude on Amazon Bedrock names each photo; it even reads the book's title. Watch the clock: that's real time." |
| 4 | 1:15–1:35 | **Real time.** Move the wallet from the desk to the shelf. | Things shows **"Moved"** desk → shelf, same item, same photo history. | "Move something, and it knows it's the same wallet, not a new one. That's ORB feature matching and colour histograms re-identifying it." |
| 5 | 1:35–2:05 | **Real time · no cuts.** **Ask** tab. Press **"Speak your question"** and say "Where are my keys?" | Live trace "How I found this": **Checked memory**, **Looked with the camera** (fresh crop), **Answer**. Answer card with the camera picture, keys circled. The laptop reads the answer aloud. | "Now just ask. It doesn't simply recall. It makes the camera look again right now, then shows me the picture with my keys circled." |
| 6 | 2:05–2:45 | **Real time · no cuts.** Off to the side, pick up the keys and put them on the bare spot you did **not** mark as a place. Ask again: "Where are my keys?" | Trace: **Checked memory**, **Looked with the camera** (empty desk), **Scanned the room**, **Updated memory**, **Answer**: "Found it…" with the new spot circled. | "Memory says desk. But the camera sees an empty desk, so the agent decides to search the whole room, finds the keys with feature matching, and corrects its own memory. What the camera sees decides what the agent does next. That's the agentic part." |
| 7 | 2:45–3:00 | **Real time.** Phone shot: walk toward the door. Wallet is still on the shelf. | Laptop speaks: "Wait — your wallet on the shelf is still here." The alert appears in **Alerts**. | "Heading out without your wallet? It tells you at the door, instantly, on the device." |
| 8 | 3:00–3:30 | Screen. **Routines → Add a routine**: Thing to watch **pill bottle**, from 20:00 until 20:30 (or a window around now). **Save routine.** Press **"Check now"**. Status "Reminder given" and the laptop speaks a reminder. Press **"Check now"** again. Status "Carer asked", approval card **"Waiting for your OK"** with **"Yes, tell them"** / **"Don't send"**. Click **"Yes, tell them"**, toast "Done. The carer has been told." Cut to the inbox: the email arrives. | — | "For carers: routines. Pills not taken? A gentle spoken reminder first. Only if that's ignored does it ask a carer, and a person approves before anyone is contacted. The model can never send that on its own; the rule is in code." |
| 9 | 3:30–3:55 | `docs/architecture.png`, then `docs/agent_workflow.png`, highlight boxes as you name them. | — | "Under the hood: OpenCV 5 in the browser and in Python for CCTV, with dual background models, gradient analysis and ORB re-identification. On AWS: Cognito, API Gateway, Graviton Lambdas, DynamoDB, S3 with Glacier so memory never runs out, EventBridge, SNS, and Claude on Bedrock kept in Australia." |
| 10 | 3:55–4:15 | Results table from `docs/EVALUATION.md` and `docs/BROWSER_PARITY.md`. | — | "Measured, not claimed: 0.98 F1 on our reproducible benchmark, every place correct, about 1.5 seconds to memory, and the browser engine matches the Python engine exactly. It struggles with tiny, transparent or hidden objects, and we say so." |
| 11 | 4:15–4:30 | Switch the Home selector to **Demo**. Then the team. | The shared Demo home with its cloud replay camera. | "Try it yourself today in our Demo home. iCamFind: memory you can ask." |

## Tips that make or break it

- **Hands out fast.** An object is committed only after the hand has left and the scene is still.
  Put it down, pull your hand fully out of frame, wait two seconds.
- **Shot 6 needs the unmarked spot.** If you move the keys onto another marked place, the camera
  notices the move by itself ("Moved"), and the agent has nothing to search for. That's good too, but
  it's not the scan.
- **Door alert:** the walk must cross the box you marked as the door for about half a second. It
  fires once every 45 seconds at most, so don't trigger it during rehearsal right before the take.
- **Only essentials trigger the door alert.** Keys, wallet, phone, glasses and pills count
  automatically. Star anything else in **Things** to mark it important.
- **Say questions clearly** or type them; typing is just as real for the trace.

## Backup plan

If the live camera misbehaves, run the Python engine on a recorded clip as the camera:
`python -m icamfind run --source clips/demo.mp4`. Everything downstream, including the agent's live
checks, works the same. Say on screen that it's a recorded clip.
