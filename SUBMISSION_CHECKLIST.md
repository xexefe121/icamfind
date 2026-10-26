# Morning checklist: submit by 4:45 pm AEST (11:45 pm PT, Oct 26)

Everything below the line is already done and tested. These are the steps only a person can do.

## 1. Try it yourself (10 min)
- Open https://o91wekq4k6.execute-api.ap-southeast-2.amazonaws.com/ and create your own account (or
  use the judge account: `judges@icamfind.example`, password = `ICAMFIND_TEST_PASSWORD` in
  `.icamfind.env`).
- **Camera tab → Use this computer's camera.** Allow access, drag boxes over Desk / Shelf / Door,
  then Finish. Put a spoon or your keys on the desk, take your hand away, and wait about 2 s; a
  "Remembered: …" message appears.
- **Ask tab:** "Where are my keys?" The answer shows the camera picture with the keys circled.
- **Home switcher → Demo home:** ask "Where is the plate?". A cloud replay camera feeds this home.

## 2. Record the video (≤ 5 min)
Follow `docs/DEMO_SCRIPT.md` (shot list + voice-over). Upload to YouTube as **Unlisted**.

## 3. Put the code where judges can see it (pick one)
- Public GitHub repo (recommended):
  ```bash
  gh repo create icamfind --public --source . --push
  ```
- Or upload `iCamFind-code.zip` (built by `python scripts/make_zip.py`) to the Devpost form.

## 4. Fill in Devpost
- Paste the sections from `docs/SUBMISSION.md`.
- Images: `docs/architecture.png`, `docs/agent_workflow.png`, plus screenshots from the app
  (`tests/ui/screenshots/`).
- Links: repo URL, app URL, video URL.
- Private judging notes: the judge account email and password (from `.icamfind.env`) and the
  "Testing instructions for judges" block from `docs/SUBMISSION.md`.
- Opt in to the **Agentic Vision Award**.

## 5. After the deadline
- Delete or rotate the AWS access key that was pasted into chat (IAM → `cli-admin` → Security
  credentials).
- Keep the stack running through judging (Oct 27 – Nov 9). It costs roughly $0.60/day (mostly the demo camera server) plus a few
  cents per question. When judging is over, run `aws cloudformation delete-stack --stack-name icamfind`.

---

## Already done and verified (overnight)
- Live cloud test suite (`python scripts/cloud_e2e.py`): see `docs/evidence/cloud_e2e_results.json`.
- UI end-to-end tests in headless Edge with a fake webcam (`tests/ui/`), with screenshots.
- Accuracy: `docs/EVALUATION.md`, `docs/ABLATION.md`, `docs/BROWSER_PARITY.md`.
- Unit tests: `python -m pytest -q`.
