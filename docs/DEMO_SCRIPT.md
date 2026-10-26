# Demo video script (5 minutes maximum)

The rules ask for: the team, the application working, its architecture, and its principal results.
Record at 1080p. Use a screen recorder for the web app and a phone or second camera for the room.
Keep the room bright and evenly lit, and put the camera high, looking down at a desk, a shelf and
the door.

## Before recording (10 minutes)

1. Open the web app and sign in. Create your own account; the shared Demo home is fine for
   rehearsal, but record in **My home**.
2. In **My home**, go to **Cameras → Add a camera** ("Living room") and copy the PowerShell command.
3. `python -m icamfind calibrate`. Draw zones named `desk`, `shelf`, `side table` and `door`,
   and mark `door` as the exit.
4. Paste the command from step 2. It runs `python -m icamfind run` with your camera's token.
   Keep the web app open next to it, with large-text mode on.
5. Props: keys on a keyring, wallet, glasses case, pill bottle. Matte, distinct objects at least
   about 3 cm across in the image work best.
6. Star the keys and wallet as essentials, or let Claude flag them automatically.

Optional shot: switch the Home selector to **Demo home** to show that anyone can try iCamFind
online. A cloud replay camera feeds it around the clock.

## Shot list

| Time | Shot | What to say (suggested) |
|---|---|---|
| 0:00-0:20 | Team on camera | "We're team iCamFind. For people living with dementia or ADHD, and for their carers, 'where did I put it?' is a daily frustration. We built a camera that remembers for you." (If you quote a statistic here, cite its source on screen.) |
| 0:20-0:50 | Put keys on desk, wallet on shelf. Cut to the app: both appear with photos, zones and names ("house keys"). | "OpenCV 5 runs on this laptop. It notices objects being put down, waits until your hand leaves, then sends only a small, privacy-filtered photo to AWS." |
| 0:50-1:30 | Ask by voice: "Where are my keys?" Show the live trace: Checked memory, Looked with the camera (crop), Answer. Read-aloud plays. | "The agent doesn't just recall. It makes the camera look again right now, so the answer is current." |
| 1:30-2:10 | Pick the keys up off camera and put them on the side table. Ask again. The trace shows Looked with the camera (empty surface), Scanned the room, found on side table, Updated memory. | "Memory said desk, but the camera saw an empty desk, so the agent scanned the whole room with feature matching, found the keys and fixed its own memory. Visual evidence changes what it does next." |
| 2:10-2:40 | Walk to the door with the wallet still on the shelf. Laptop speaks: "Wait, your wallet on the shelf is still here." The alert appears in the app. | "Leave without your essentials and it reminds you, on device, instantly." |
| 2:40-3:20 | Routines panel: "Evening medication: pill bottle 20:00-20:30". Press Check now twice. The first run gives a spoken reminder. The second creates a caregiver approval card. Click Approve and the email arrives. | "For caregivers: routines. A missed medication gets a gentle reminder first. Only if that is ignored does it ask a human to approve notifying the caregiver. The model can't email anyone on its own; that policy is enforced in code." |
| 3:20-4:00 | Architecture diagram (docs/architecture.svg), then agent workflow diagram. | "On device: dual background models, people masking with the OpenCV Model Zoo detector through cv.dnn, a boundary-gradient test, and ORB re-identification. On AWS: API Gateway, Graviton Lambdas, DynamoDB, S3 with Glacier tiering for unlimited memory, EventBridge, SNS, and Claude Haiku 4.5 on Bedrock pinned to Australia." |
| 4:00-4:40 | docs/EVALUATION.md and ABLATION.md tables, plus one real-clip result if recorded. | "On our reproducible benchmark: 0.98 F1, 100% zone accuracy, about 1.5 seconds from hand-away to memory, and every moved object kept its identity. The ablation shows what each component adds. We also show where it fails: tiny or transparent objects, and objects hidden by other objects." |
| 4:40-5:00 | Team, the app on a phone | "iCamFind: memory you can ask. Thanks!" |

## Backup plan

If the live camera misbehaves during recording, replay a recorded clip as the camera:
`python -m icamfind run --source clips/demo.mp4`. Everything downstream, including the agent's live
checks, works the same against the clip.
