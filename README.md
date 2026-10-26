# iCamFind

**Camera-based visual memory for people who misplace things, and the people who care for them.**

Put your keys down, walk away, and later ask *"Where are my keys?"*. iCamFind answers with the
place, the time and a photo, after it has had the camera look again to make sure the keys are
still there. It also catches *"Did I take my pills?"* moments, and it says *"Wait, your keys are
still on the desk"* when you head for the door without them.

Built for the **OpenCV AI Competition 2026, powered by AWS**: OpenCV 5 does the perception on the
home device, and AWS holds the long-term memory, the agent and the web app. It is entered for the
overall prizes and the **Agentic Vision Award**.

![Architecture](docs/architecture.svg)

| | |
|---|---|
| Technical report | [docs/REPORT.md](docs/REPORT.md) |
| Evaluation (reproducible) | [docs/EVALUATION.md](docs/EVALUATION.md), [docs/ABLATION.md](docs/ABLATION.md) |
| Agent workflow + traces | [docs/agent_workflow.svg](docs/agent_workflow.svg), [docs/evidence/](docs/evidence/) |
| API contract | [docs/API.md](docs/API.md) |
| Demo video script | [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) |

## How it works (60-second version)

1. **On the device (OpenCV 5):** a dual-background static-object detector notices when something
   is put down, picked up or moved. A short-term MOG2 model tracks motion and a long-term reference
   background holds memory. People are masked out using the YOLOX detector from the OpenCV Model
   Zoo, run through `cv.dnn`. Boundary-gradient analysis tells "placed" apart from "taken away".
   ORB features, colour histograms and shape re-identify an object after it moves. Each event is
   journaled locally first, so memory survives offline periods.
2. **In AWS:** events and privacy-filtered crops go to DynamoDB and S3. Crops tier to Glacier
   Instant Retrieval, so memory grows without limit at low cost. Claude Haiku 4.5 on Amazon
   Bedrock (Australian inference profile, so data stays in Australia) gives each crop an everyday
   name like "house keys".
3. **The agent:** questions and routine checks run as a perception, decision, action loop. The
   agent reads memory, then makes the camera **look again**:
   - `verify_object_live` checks the remembered spot by patch matching;
   - if the spot is empty, `scan_room_for_object` searches the whole frame with ORB + RANSAC;
   - it corrects memory, speaks reminders, and asks a caregiver for approval before any
     notification goes out.

   Every step shows up in the app as "How I found this".

## Repository layout

```
icamfind/edge/      OpenCV 5 pipeline: scene memory, detector, re-ID, zones, privacy, uplink, CLI app
icamfind/eval/      synthetic benchmark generator, scorer, annotation tool, report writer
cloud/src/          Lambda functions (api, agent, scheduler), DynamoDB/S3 store, Bedrock client, web app
cloud/template.yaml SAM/CloudFormation stack (HTTP API, Lambda arm64, DynamoDB, S3, SNS, EventBridge, alarms)
scripts/            deploy.py, get_models.py, e2e.py, ablation.py, reset_memory.py
tests/              pytest suite (scene behaviour, scorer, cloud handlers)
docs/               report, evaluation, ablation, diagrams, agent traces, API, demo script
```

## Quick start

Requirements: Python 3.10 to 3.12, a webcam or RTSP camera, and the AWS CLI v2 configured for an
account with Amazon Bedrock access to Claude Haiku 4.5. You don't need SAM CLI or Docker.

```bash
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/get_models.py                          # OpenCV Zoo YOLOX-S, SHA-256 verified
```

### 1. Deploy the cloud (about 3 minutes)

```bash
python scripts/deploy.py --region ap-southeast-2 [--caregiver-email you@example.com]
```

This bundles the Lambda code, uploads it with `aws cloudformation package`, deploys
`cloud/template.yaml`, and writes `.icamfind.env` with the web-app URL and a randomly generated
access token. Keep that file private. If you pass `--caregiver-email`, confirm the SNS subscription
email that arrives.

### 2. Set up the camera

```bash
cp config.example.json config.json
python -m icamfind calibrate      # click polygons for desk / shelf / door, press m for the room map
python -m icamfind run            # preview window; q quits, r re-learns the background
```

A CCTV stream works as well: `python -m icamfind run --source rtsp://user:pass@camera/stream`.
A recorded clip can stand in for a camera: `--source clips/demo.mp4`.
Without `.icamfind.env` the device runs **local-only**, still remembering and alerting.

### 3. Use it

Open the URL from `.icamfind.env` and enter the access token. You can also open `<url>/#token=<token>`
once; the app stores the token and removes it from the address bar. Then:
- put an object down, wait about 2 seconds after your hand leaves, and watch it appear;
- ask "Where is my wallet?" by voice or text;
- add a routine (object + time window) and press **Check now** to see the reminder and escalation flow.

## Reproduce the evaluation

```bash
python -m icamfind benchmark --no-dnn      # 5 seeded synthetic clips -> docs/EVALUATION.md
python scripts/ablation.py                 # component ablation -> docs/ABLATION.md
python -m pytest -q                        # unit tests
python -m icamfind record --out clips/real1.mp4 --seconds 180   # real footage
python -m icamfind annotate --video clips/real1.mp4             # label ground truth
python -m icamfind eval --video clips/real1.mp4 --truth clips/real1.truth.json
python scripts/e2e.py ask "Where are my keys?"                  # live agent trace -> docs/evidence/
```

## Privacy and responsible use

- People are masked from object detection and **pixelated in every image that leaves the device**
  (crops, snapshots, live checks). Full frames and video are never uploaded.
- Claude runs on Amazon Bedrock with the `au.` geographic inference profile. Bedrock does not store
  prompts or outputs and does not use them for training.
- Caregiver notifications go through a policy enforced in code. Either a human approves them in the
  app, or a spoken reminder went unanswered past a grace period. The model cannot send email on its own.
- It's a memory aid, not a safety or medical device. Answers include when the object was last seen,
  and say when the camera is offline.

See the "Responsible use" section of [docs/REPORT.md](docs/REPORT.md) for limitations and failure modes.

## License

MIT for the code in this repository. The YOLOX-S weights come from the OpenCV Model Zoo (Apache-2.0).
