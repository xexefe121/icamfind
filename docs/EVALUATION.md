# Evaluation results (synthetic benchmark)

Generated 2026-10-26 21:12 with OpenCV 5.0.0 on Windows AMD64 / Python 3.11.9.
Reproduce: `python -m icamfind benchmark --no-dnn` (deterministic seeds).

| Clip | Truth events | Predicted | Precision | Recall | F1 | Zone acc. | Mean latency (s) | Identity kept | Rebaselines | ms/frame (median) |
|---|---|---|---|---|---|---|---|---|---|---|
| synthetic_1.mp4 | 13 | 13 | 1.0 | 1.0 | 1.0 | 1.0 | 1.46 | 4/4 | 1 | 22.62 |
| synthetic_2.mp4 | 13 | 13 | 0.923 | 0.923 | 0.923 | 1.0 | 1.51 | 4/4 | 1 | 38.31 |
| synthetic_3.mp4 | 13 | 13 | 1.0 | 1.0 | 1.0 | 1.0 | 1.47 | 4/4 | 1 | 28.24 |
| synthetic_4.mp4 | 12 | 12 | 1.0 | 1.0 | 1.0 | 1.0 | 1.44 | 4/4 | 1 | 29.81 |
| synthetic_5.mp4 | 14 | 15 | 0.933 | 1.0 | 0.966 | 1.0 | 1.46 | 4/4 | 1 | 33.72 |
| **All** | **65** | **66** | **0.970** | **0.985** | **0.977** | **1.0** | **1.468** | | | |

## Recall by event type

| Type | Truth | Detected | Recall |
|---|---|---|---|
| placed | 30 | 30 | 1.0 |
| removed | 25 | 24 | 0.96 |
| moved | 10 | 10 | 1.0 |

## Failure cases

- **Missed** in synthetic_2.mp4: removed `remote` @ desk (t=40.0 s)
- **False positive** in synthetic_2.mp4: placed `object` @ desk (t=41.5 s)
- **False positive** in synthetic_5.mp4: removed `object` @ side table (t=77.5 s)
