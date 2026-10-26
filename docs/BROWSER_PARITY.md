# Browser engine parity (OpenCV.js 5.0.0 WebAssembly vs Python OpenCV 5)

Same synthetic clips and scoring rules as `docs/EVALUATION.md`; the browser engine runs with
`placesOnly` off so both see the full frame. Reproduce: `python scripts/browser_parity.py --opencv-js <opencv.js>`.

| Clip | Browser precision | Browser recall | Browser F1 | Identity kept | Python F1 | Browser ms/frame (median, Node) |
|---|---|---|---|---|---|---|
| synthetic_1.mp4 | 1.0 | 1.0 | 1.0 | 4/4 | 1.0 | 49.8 |
| synthetic_2.mp4 | 0.923 | 0.923 | 0.923 | 4/4 | 0.923 | 49.57 |
| synthetic_3.mp4 | 1.0 | 1.0 | 1.0 | 4/4 | 1.0 | 51.74 |
| synthetic_4.mp4 | 1.0 | 1.0 | 1.0 | 4/4 | 1.0 | 50.26 |
| synthetic_5.mp4 | 0.933 | 1.0 | 0.966 | 4/4 | 0.966 | 47.96 |
