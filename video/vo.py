"""Synthesize the voice-over, one file per scene, with Amazon Polly (Sydney region)."""
import json
import subprocess
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).parent
SCENES = {
    1: "There's a moment we all know. <break time='400ms'/> You put something down, <break time='250ms'/> and the world moves on without it. <break time='500ms'/> For most of us, it's a small annoyance. <break time='400ms'/> For someone living with dementia, <break time='300ms'/> it's the moment the day starts to slip away.",
    2: "Carers hear the same question, again and again. <break time='300ms'/> Where did I put it? <break time='600ms'/> But it was never really about the keys. <break time='400ms'/> It's about independence. <break time='300ms'/> Dignity. <break time='300ms'/> Trusting your own mind.",
    3: "So we built something that remembers for you. <break time='500ms'/> This is iCamFind.",
    4: "Point any laptop or phone at the places you put things. <break time='300ms'/> Draw them once. The desk, the shelf, the door. <break time='300ms'/> That's the whole setup. <break time='400ms'/> OpenCV 5 runs right inside the browser, watching for the moment something is put down, and the hand moves away. <break time='400ms'/> The video never leaves the room. <break time='250ms'/> Only a small photo of the object does.",
    5: "Later, just ask. <break time='400ms'/> iCamFind doesn't simply recall. <break time='250ms'/> It looks again, right now, and circles the answer on the camera picture. <break time='500ms'/> And if the keys aren't where they were? <break time='300ms'/> It searches the whole room, finds them, and corrects its own memory.",
    6: "Heading out without your wallet? <break time='250ms'/> It tells you at the door. <break time='400ms'/> Evening pills still untouched? <break time='250ms'/> A gentle reminder first. <break time='300ms'/> Only if that's ignored does it ask a carer, <break time='250ms'/> and a person always approves before anyone is contacted.",
    7: "Under the hood: OpenCV 5, in Python and in WebAssembly, with dual background models, gradient analysis and ORB re-identification. <break time='400ms'/> On AWS: Cognito, API Gateway, Graviton Lambdas, DynamoDB, and S3 with Glacier, so memory never runs out. <break time='300ms'/> And Claude on Amazon Bedrock, kept in Australia, as the agent that decides when to look again. <break time='400ms'/> Anyone can try it today, in our shared Demo home.",
    8: "Measured, not claimed. <break time='400ms'/> An F1 score of zero point nine eight on our reproducible benchmark. <break time='250ms'/> Every place correct. <break time='250ms'/> About a second and a half from hand-away to memory. <break time='300ms'/> And a browser engine that matches the Python engine, exactly.",
    9: "Because the most precious thing to lose isn't your keys. <break time='500ms'/> It's the feeling that you can still find your way. <break time='800ms'/> iCamFind. <break time='400ms'/> Memory you can ask.",
}


def synth(voice: str, engine: str, out_dir: Path, only=None) -> dict:
    polly = boto3.client("polly", region_name="ap-southeast-2")
    out_dir.mkdir(parents=True, exist_ok=True)
    lengths = {}
    for n, text in SCENES.items():
        if only and n not in only:
            continue
        mp3 = out_dir / f"scene{n}.mp3"
        res = polly.synthesize_speech(Text=f"<speak>{text}</speak>", TextType="ssml", VoiceId=voice, Engine=engine,
                                      OutputFormat="mp3", SampleRate="24000")
        mp3.write_bytes(res["AudioStream"].read())
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(mp3)], capture_output=True, text=True).stdout.strip())
        lengths[n] = round(dur, 2)
    (out_dir / "lengths.json").write_text(json.dumps(lengths, indent=1))
    return lengths


if __name__ == "__main__":
    voice = sys.argv[1] if len(sys.argv) > 1 else "Matthew"
    engine = sys.argv[2] if len(sys.argv) > 2 else "generative"
    out = HERE / "vo" if len(sys.argv) <= 3 else Path(sys.argv[3])
    only = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else None
    print(synth(voice, engine, out, only))
