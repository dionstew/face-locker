# Recognition and liveness commissioning

## Pipeline

The worker loads YuNet and SFace once, then starts the selected camera only for preview,
scanning, or enrollment. Pi ribbon capture uses Picamera2; local webcams and HTTP/RTSP streams
use OpenCV in a separate reader process. It captures fresh BGR frames, requires exactly one face, checks face size, brightness,
and Laplacian sharpness, aligns landmarks, and produces a normalized SFace embedding.

Compare embeddings by cosine similarity. The best score for each person is the maximum
over their templates; the best person's score must exceed `match_threshold`, and the margin
over the second-best person must exceed `match_margin`. Unknown/ambiguous faces are rejected.
Scores are similarities, **not identity probabilities**.

An accepted face must match the same identity for `consistent_frames` consecutive valid
observations. Missing/poor-quality faces reset the sequence. An explicit session lasts at most
`scan_timeout`, and successful matching waits at least 1.5 seconds after tapping Start. One
successful session produces at most one command. A new tap is required for another session.

Enrollment requires a valid administrator session and collects `enrollment_samples` samples
at least 0.8 seconds apart. Samples must match the first sample, and matching an already-active
identity prevents duplicate enrollment. If a PAD model is present, enrollment also requires
liveness. Without PAD, administrator-supervised enrollment is available for observation mode.

## PAD model adapter contract

No PAD weights are bundled. Select a licensed model and validate its accuracy and preprocessing
against its author's documentation before enabling actual access. Many models have different
crop/normalization conventions and **cannot be substituted directly** into this adapter.

The implemented adapter (`PiVision.analyze`) requires:

- ONNX supported by your OpenCV DNN build, one input and one output.
- Input is the detected face bounding-box crop; no extra context is included.
- Resize to `pad_size × pad_size` (default 128), BGR → RGB.
- Float32 NCHW tensor `[1,3,H,W]`, values scaled by `1/255`, no mean subtraction.
- Output flattens to exactly two finite scores.
- `pad_logits: true`: apply softmax to logits. Otherwise output must already be probabilities
  in `[0,1]` summing to one.
- `pad_real_index` selects the live/real class (0 or 1). Live score must be at least
  `pad_threshold` for every observation in the accepted identity sequence.

If your selected model expects expanded crops, mean/std normalization, additional inputs,
or a temporal sequence, implement that documented preprocessing in `PiVision` first. Do not
guess the class ordering. Invalid PAD output faults the scan and never produces an unlock.

## Measurement before deployment

Measure genuine acceptance and impostor false acceptance using the full enrolled gallery,
including different lighting, distances, glasses, and expected user appearances. Test prints,
phone photographs/video replay, and other relevant presentation attacks separately. Consecutive
matching frames reduce instability but are not a liveness check. Validate PAD false rejection
and attack acceptance on the actual camera; the application does not claim certified resistance.

The provided similarity and PAD thresholds are starting configuration values only. No measured
Raspberry Pi 4 latency or deployment accuracy is implied. Use the external controller's own
activation deadline so a stalled camera process cannot keep a solenoid energized.
