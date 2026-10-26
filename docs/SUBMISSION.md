# Devpost submission text (paste into the form)

**Project name:** iCamFind

**Tagline:** Memory you can ask: an OpenCV 5 camera that remembers where you put things, looks
again before it answers, and looks after routines.

**Featured path:** Agentic Vision (also entered for the overall prizes).

## Inspiration

Losing keys, glasses or a wallet is annoying for most people. For someone living with dementia or
ADHD it can derail a day, and for their carers it means countless "have you seen my...?" calls.
Phone trackers only cover the few items that have a tag. A camera already sees everything, but
nothing turned what it sees into a memory you can ask.

## What it does

- **Remembers** every time an object is put down, picked up or moved, with zone, time and a photo,
  using OpenCV 5 on a home device (webcam or existing CCTV).
- **Answers** "Where are my keys?" by voice or text. Before answering, the agent makes the camera
  look again. If the keys are not where memory says, it scans the whole room, finds them and
  corrects its own memory.
- **Reminds** you on device when you walk to the door while essentials are still on the table.
- **Looks after routines** like evening medication. If the pill bottle wasn't picked up in its
  window, the agent confirms with the camera and speaks a reminder. Only if that is ignored does
  it ask a human to approve notifying the caregiver.
- **Explains itself:** every perception, decision and action step, with camera crops, appears in
  the app as "How I found this".

## How we built it

- **OpenCV 5 on the edge:**
  - a dual-background static-object detector: a short-term MOG2 model for motion, plus a long-term
    reference background with a time-based evidence accumulator;
  - morphology and connected components, with fragment grouping;
  - a global lighting-change guard;
  - a Sobel boundary-gradient "ghost" test that tells *placed* apart from *taken away*;
  - YOLOX-S from the OpenCV Model Zoo through `cv.dnn`, for masking people and as a label hint;
  - re-identification of moved objects with ORB + Lowe ratio test, HSV histograms and shape;
  - live verification by patch matching and NCC, and room search by ORB + RANSAC homography plus
    multi-scale template matching;
  - `pointPolygonTest` zones and a `getPerspectiveTransform` floor map.
- **Local-first:** a JSON journal and crops are written before any network call; a retry queue
  syncs them later. People are pixelated in every image that leaves the device.
- **AWS (one SAM/CloudFormation stack, Sydney region):**
  - API Gateway HTTP API (token auth, throttling) serving the API and the web app;
  - Python 3.12 Lambdas on Graviton arm64 (api, agent, routine scheduler);
  - DynamoDB single-table memory with a GSI, point-in-time recovery and encryption;
  - S3 crops tiering to Glacier Instant Retrieval, for unbounded memory at low cost;
  - EventBridge for routine sweeps;
  - SNS (KMS) for caregiver email;
  - CloudWatch JSON logs, X-Ray, and an error alarm;
  - Amazon Bedrock with Claude Haiku 4.5 on the Australian `au.` inference profile, so data stays
    in Australia, with Sonnet 4.6 as the client-side fallback.
- **Agent:** a manual tool-use loop. Its tools are backed by the device's OpenCV routines through a
  DynamoDB command queue, and the device returns fresh crops that the model sees as images.
  Caregiver notifications are gated by policy in code, not left to the model.
- **Accounts:** Amazon Cognito (hosted UI, PKCE) with an API Gateway JWT authorizer. Data is
  partitioned by household, every user also sees a shared Demo home fed by a cloud replay camera
  (Graviton EC2), and per-camera device tokens are stored as hashes.
- **Browser camera:** any laptop or phone can be the camera. The same scene-memory pipeline runs in
  the browser on OpenCV 5.0 WebAssembly, so video never leaves the device. Setup is: allow the
  camera, then drag boxes over your places.
- **"Here it is":** answers circle the object on the camera picture, and the live view pulses it.
- **Web app:** plain HTML/JS, no build step. Voice input and read-aloud, large-text and
  high-contrast modes, fully keyboard operable.

## Evaluation

- **Synthetic benchmark (reproducible, 5 seeds, 65 ground-truth events):**
  - precision 0.970, recall 0.985, **F1 0.977**;
  - **zone accuracy 100%**;
  - mean latency **1.47 s** from the hand leaving to the event;
  - **identity kept for 20/20 objects across all their put-down/pick-up/move events** (10/10 moves
    detected as moves);
  - 23-38 ms per frame on a laptop CPU.
- **Ablation:** shows what each component contributes. See docs/ABLATION.md.
- **Live end-to-end traces (docs/evidence):** recorded against the deployed stack. Examples:
  "memory said picked up, then scanned the room, found it on the desk (0.99), then corrected
  memory"; and "routine missed, then reminder, then caregiver approval requested".
- **Browser engine parity:** the OpenCV 5.0 WebAssembly engine gives identical scores to the
  Python pipeline on all 5 clips (docs/BROWSER_PARITY.md).
- **Live cloud test suite:** 49/49 checks against the deployed stack (scripts/cloud_e2e.py) cover
  auth, tenant isolation, the demo camera, Claude naming, the agent's verify, then scan, then
  update loop, routine reminder and escalation to caregiver approval, and pairing codes.
- **Web app end-to-end suite:** Playwright drives headless Microsoft Edge with a fake webcam
  (tests/ui) through sign-in, the camera wizard, place drawing, asking, the "Here it is" view,
  Things, Routines, pairing, reload/resume, sign-out, accessibility checks and mobile layout.
- **Failure cases:** listed in docs/EVALUATION.md and the report.

## Challenges we ran into

- Telling "an object appeared" apart from "an object that was always there was taken". We solved
  it with boundary-gradient ownership.
- One object splitting into several detections; grouping evidence before committing fixed it.
- Keeping answers current rather than recalled. That led to the live verify-then-scan design.
- Bedrock model access on a new AWS account. Everything is configurable by environment variable,
  with a client-side fallback.

## Accomplishments we're proud of

The agent's behaviour truly depends on what the camera sees, and it's honest about uncertainty and
privacy. It is also measured: a reproducible benchmark, an ablation, and real traces.

## What we learned

Classic computer vision (background models, gradients, features) is still the right tool for
precise "what changed where", and a language model is the right tool for naming things and
deciding what to do next. The combination is far better than either alone.

## What's next

Multi-camera homes with cross-view re-ID, a small on-device open-vocabulary model, a
caregiver mobile app, and a pilot with an occupational therapist.

## Built with

opencv, python, numpy, amazon-web-services, aws-lambda, amazon-api-gateway, amazon-dynamodb,
amazon-s3, amazon-bedrock, claude, amazon-eventbridge, amazon-sns, amazon-cloudwatch,
aws-cloudformation, javascript, html, css

## Testing instructions for judges (put in the private judging notes)

1. Open the live app (link below) and choose **Sign in or create an account**. Use the judge account
   `judges@icamfind.example` (password in the private notes), or sign up with your own email.
2. Switch the **Home** selector to **Demo home**. Its camera is a Graviton EC2 instance running our
   OpenCV 5 pipeline on a recorded room around the clock. Ask "Where is the wallet?" and watch the
   agent check memory, make the camera look again, and scan the room.
3. In **My home**, use **Cameras → Add a camera** to connect your own webcam. Run the command it
   shows after `pip install -r requirements.txt` (see README). That home is private to your account.
4. Reproduce our numbers: `python -m icamfind benchmark --no-dnn` and `python scripts/ablation.py`.

## Links

- Code: (repository URL)
- Live app: https://o91wekq4k6.execute-api.ap-southeast-2.amazonaws.com/ (sign up, or use the judge
  account from the private judging notes)
- Video: (YouTube unlisted URL)
