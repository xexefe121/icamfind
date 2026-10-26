# Ablation study (synthetic benchmark, 5 seeds)

Each row disables one part of the scene-memory pipeline. Event matching rules are the same as in `docs/EVALUATION.md` (type + time window; moved counts as placed).

| Variant | Predicted events | Precision | Recall | F1 | Identity kept across moves | Mean latency (s) |
|---|---|---|---|---|---|---|
| Full pipeline | 66 | 0.97 | 0.985 | 0.977 | 20/20 | 1.47 |
| No lighting guard (no re-baseline on global change) | 66 | 0.97 | 0.985 | 0.977 | 20/20 | 1.47 |
| No ghost test (every change treated as 'placed') | 66 | 0.909 | 0.923 | 0.916 | 20/20 | 1.46 |
| No re-identification (moves become new objects) | 66 | 0.909 | 0.923 | 0.916 | 10/20 | 1.46 |
| Short stability window (0.4 s instead of 2 s) | 102 | 0.559 | 0.877 | 0.683 | 17/20 | -0.16 |
