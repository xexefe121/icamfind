# Launch video credits

- **Video:** `video/iCamFind_launch.mp4` (1920x1080, 30 fps, 2:25), built reproducibly by `video/build.py`.
- **Voice-over:** Amazon Polly, generative engine, voice "Stephen" (ap-southeast-2), from `video/vo.py`.
- **Music:** original piano/pad bed synthesized in code (`synth_music` in `video/build.py`). No third-party music.
- **Footage:** iCamFind's own web app, from screenshots taken by the automated UI test suite
  (`tests/ui/screenshots/`), plus our architecture diagram and results. No stock footage was used.
- **Fonts:** Segoe UI and Georgia (Windows system fonts), used to render the stills.

# CCTV demo video credits

- **Video:** `video/demo2/iCamFind_demo_cctv.mp4` (1920x1080, 30 fps, 1:58), edited by `video/demo2/build_demo.py`.
- **Camera footage:** free stock clips from [Pexels](https://www.pexels.com/license/) (free to use and modify),
  graded to look like home security cameras by `video/demo2/make_feed.py`:
  [7986194](https://www.pexels.com/video/a-person-getting-keys-on-the-table-7986194/) (hall table, played in reverse
  for the put-down), [7986201](https://www.pexels.com/video/woman-going-out-of-the-door-7986201/) (hallway),
  [12803301](https://www.pexels.com/video/putting-cellphone-on-dresser-12803301/) (bedroom),
  [4994521](https://www.pexels.com/video/mobile-phone-and-eye-glasses-layout-on-the-table-4994521/) (kitchen table).
  The video says on its end card that the footage is stock.
- **What is real:** the hall-table clip was the webcam of the real web app (`video/demo2/record_ui.py`, Edge with
  the clip as its camera), served by `video/demo2/local_stack.py` against the deployed backend: OpenCV 5 in the
  browser, DynamoDB, S3, Claude on Bedrock naming the keys and the agent Lambda answering. Boxes on the CCTV shots come
  from the same browser engine run on the clips (`video/demo2/detect_broll.py`); the bedroom shot is left unnamed
  because Claude Haiku misread the phone.
- **Voice-over:** Amazon Polly, generative engine, voice "Stephen" (ap-southeast-2), from `video/demo2/vo_demo.py`.
- **Music:** the same code-synthesized bed as the launch video.
