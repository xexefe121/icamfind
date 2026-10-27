"""Voice-over for the CCTV demo video, one file per scene (Amazon Polly generative, Sydney region).

    python video/demo2/vo_demo.py [voice]      -> video/demo2/vo/sceneN.mp3 + lengths.json
"""

import json
import subprocess
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
B = "<break time='{}ms'/>"
SCENES = {
    1: f"Every home already has the cameras. {B.format(300)} What they don't have, {B.format(200)} is a memory you can ask.",
    2: f"For someone living with dementia, {B.format(200)} the hardest part of the morning is often a simple question. {B.format(300)} Where did I put it?",
    3: f"Setting up iCamFind takes one box. {B.format(300)} Open the app, pick the camera, {B.format(200)} and draw around the place where things get put down. {B.format(300)} OpenCV 5 runs right inside the browser, {B.format(200)} so the video never leaves the home.",
    4: f"Keys go down. {B.format(250)} The hand moves away, {B.format(200)} and a moment later they're remembered, {B.format(200)} with a photo and the place. {B.format(300)} Claude on Amazon Bedrock gives each thing an everyday name.",
    5: f"Every camera in the home works the same way. {B.format(250)} Something put down on the nightstand is noticed, {B.format(200)} the moment the hand moves away.",
    6: f"Later, just ask. {B.format(300)} The agent doesn't trust memory alone. {B.format(200)} It makes the camera look again, right now, {B.format(200)} and circles the answer on the live picture.",
    7: f"And when something has gone, {B.format(200)} it says so honestly. {B.format(300)} The camera sees an empty table, {B.format(200)} the agent searches the room, {B.format(200)} and tells you when the keys were last seen, and when they were picked up.",
    8: f"Under the hood: OpenCV 5 in WebAssembly and Python, {B.format(150)} dual background models, gradient tests and ORB re-identification. {B.format(300)} On AWS: Cognito, API Gateway, Graviton Lambdas, DynamoDB and S3. {B.format(200)} And Claude on Bedrock, kept in Australia, as the agent that decides when to look again.",
    9: f"iCamFind. {B.format(400)} Memory you can ask.",
}


def main() -> None:
    voice = sys.argv[1] if len(sys.argv) > 1 else "Stephen"
    out = HERE / "vo"
    out.mkdir(exist_ok=True)
    polly = boto3.client("polly", region_name="ap-southeast-2")
    lengths = {}
    for n, text in SCENES.items():
        mp3 = out / f"scene{n}.mp3"
        res = polly.synthesize_speech(Text=f"<speak>{text}</speak>", TextType="ssml", VoiceId=voice,
                                      Engine="generative", OutputFormat="mp3", SampleRate="24000")
        mp3.write_bytes(res["AudioStream"].read())
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(mp3)], capture_output=True, text=True).stdout.strip())
        lengths[n] = round(dur, 2)
    (out / "lengths.json").write_text(json.dumps(lengths, indent=1))
    print(lengths)


if __name__ == "__main__":
    main()
