/*
 * iCamFind browser camera: the scene-memory pipeline of icamfind/edge, running on OpenCV 5.0.0
 * compiled to WebAssembly. Video never leaves the device; only event crops, occasional snapshots
 * and live-check crops are uploaded (same JSON as the Python edge app, see docs/API.md).
 *
 * The file has two layers:
 *   * a DOM-free core (Appearance, RoomGeometry, SceneMemory) that is a faithful port of
 *     scene.py / appearance.py / geometry.py and also runs in Node for validation
 *     (scripts/web_engine_bench.js);
 *   * a browser Engine that captures frames from a <video>, talks to the device API, answers live
 *     checks, draws the overlay and persists memory in localStorage (contract: docs/WEB_CAMERA.md).
 *
 * Frames inside the core are 3-channel RGB Mats. Python works on BGR; every operation used here is
 * channel-order invariant once the matching conversions are used (RGB2HSV / RGB2GRAY), so the
 * thresholds carry over unchanged.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ICamEdge = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const OPENCV_URL = "https://cdn.jsdelivr.net/npm/@techstark/opencv-js@5.0.0-release.1/dist/opencv.js";
  const OPENCV_SRI = "sha384-16eddoZUbhJFP+dmyqNO/+vH8UdDOg8MJo+zi1NSy2clHxpKiQaoSQWtQnPHzYvc";

  // Mirrors icamfind/edge/config.py; max_object_area is lower in the browser because a laptop camera
  // usually also sees the person sitting in front of it.
  const DEFAULTS = {
    stableSeconds: 2.0,
    minObjectArea: 0.0012, // fraction of frame area
    maxObjectArea: 0.12,
    longDiffThreshold: 28,
    lightingChangeFraction: 0.35,
    placesOnly: false, // core default = Python behaviour; the browser Engine turns it on
    essentials: ["keys", "wallet", "phone", "glasses", "pill", "medication"],
  };
  const REID_WINDOW = 12 * 3600;
  const REID_THRESHOLD = 0.6;
  const PERSON_HOLD = 1.5;

  function newId(prefix) {
    let hex = "";
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      const b = new Uint8Array(5);
      crypto.getRandomValues(b);
      hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    } else {
      for (let i = 0; i < 10; i++) hex += Math.floor(Math.random() * 16).toString(16);
    }
    return `${prefix}_${hex}`;
  }

  function intersection(a, b) {
    const ix = Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]));
    const iy = Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]));
    return ix * iy;
  }

  function intersects(a, b) {
    return intersection(a, b) > 0;
  }

  function del(...mats) {
    for (const m of mats) if (m && !m.isDeleted()) m.delete();
  }

  /**
   * Dense (continuous) copy of a Mat. In OpenCV.js 5.0.0, clone() of an ROI keeps the parent's row
   * step, so linear `.data` access reads the wrong pixels; copyTo into a fresh Mat is always dense.
   */
  function dense(mat) {
    const out = new mat.constructor();
    mat.copyTo(out);
    return out;
  }

  // ===================================================================== appearance.py

  /**
   * Appearance models used to re-identify moved objects and verify them on request.
   * Three OpenCV signals are fused: ORB + Hamming ratio-test matching (texture), HSV hue/saturation
   * histogram correlation (colour) and normalised cross-correlation (exact-position checks).
   */
  class Appearance {
    constructor(cv) {
      this.cv = cv;
      // nfeatures=300, scaleFactor=1.2, nlevels=6, edgeThreshold=9, firstLevel=0, WTA_K=2, HARRIS, patchSize=15, fast=20
      this.orb = new cv.ORB(300, 1.2, 6, 9, 0, 2, cv.ORB_HARRIS_SCORE, 15, 20);
      this.matcher = new cv.BFMatcher(cv.NORM_HAMMING, false);
    }

    dispose() {
      del(this.orb, this.matcher);
    }

    _upscale(patch, minSide = 64) {
      const cv = this.cv;
      const s = Math.max(1.0, minSide / Math.max(1, Math.min(patch.rows, patch.cols)));
      const out = new cv.Mat();
      if (s > 1) cv.resize(patch, out, new cv.Size(0, 0), s, s, cv.INTER_CUBIC);
      else patch.copyTo(out);
      return out;
    }

    /** {hist: Mat, desc: Mat, kps: [[x,y]], size: [w,h]} - free with freeSig(). */
    signature(patch, mask = null) {
      const cv = this.cv;
      const up = this._upscale(patch);
      let m = new cv.Mat();
      if (mask) cv.resize(mask, m, new cv.Size(up.cols, up.rows), 0, 0, cv.INTER_NEAREST);
      const hsv = new cv.Mat();
      cv.cvtColor(up, hsv, cv.COLOR_RGB2HSV);
      const mv = new cv.MatVector();
      mv.push_back(hsv);
      const hist = new cv.Mat();
      cv.calcHist(mv, [0, 1], m, hist, [24, 16], [0, 180, 0, 256], false);
      cv.normalize(hist, hist, 1.0, 0.0, cv.NORM_L1);
      const gray = new cv.Mat();
      cv.cvtColor(up, gray, cv.COLOR_RGB2GRAY);
      const kv = new cv.KeyPointVector();
      const desc = new cv.Mat();
      this.orb.detectAndCompute(gray, m, kv, desc, false);
      const kps = [];
      for (let i = 0; i < kv.size(); i++) {
        const p = kv.get(i).pt;
        kps.push([p.x, p.y]);
      }
      const sig = { hist, desc, kps, size: [up.cols, up.rows] };
      del(up, m, hsv, gray);
      mv.delete();
      kv.delete();
      return sig;
    }

    freeSig(sig) {
      if (sig) del(sig.hist, sig.desc);
    }

    _goodMatches(qDesc, tDesc) {
      const vv = new this.cv.DMatchVectorVector();
      this.matcher.knnMatch(qDesc, tDesc, vv, 2);
      const good = [];
      for (let i = 0; i < vv.size(); i++) {
        const p = vv.get(i);
        if (p.size() === 2) {
          const a = p.get(0);
          const b = p.get(1);
          if (a.distance < 0.75 * b.distance) good.push(a);
        }
        p.delete();
      }
      vv.delete();
      return good;
    }

    orbScore(a, b) {
      if (!a.desc || !b.desc || a.desc.rows < 4 || b.desc.rows < 4) return 0.0;
      const good = this._goodMatches(a.desc, b.desc);
      return Math.min(1.0, good.length / Math.max(8.0, 0.25 * Math.min(a.desc.rows, b.desc.rows)));
    }

    histScore(a, b) {
      return Math.max(0.0, this.cv.compareHist(a.hist, b.hist, this.cv.HISTCMP_CORREL));
    }

    /** Fused identity similarity in [0, 1]. */
    similarity(a, b) {
      const h = this.histScore(a, b);
      const o = this.orbScore(a, b);
      const ar = a.size[0] / Math.max(1, a.size[1]);
      const br = b.size[0] / Math.max(1, b.size[1]);
      const shape = Math.min(ar, br) / Math.max(ar, br);
      return 0.5 * h + 0.35 * o + 0.15 * shape;
    }

    /** Zero-mean normalised cross-correlation between two same-size RGB patches. */
    ncc(a, b) {
      const cv = this.cv;
      if (a.rows !== b.rows || a.cols !== b.cols || a.rows * a.cols === 0) return 0.0;
      const ga = new cv.Mat();
      const gb = new cv.Mat();
      cv.cvtColor(a, ga, cv.COLOR_RGB2GRAY);
      cv.cvtColor(b, gb, cv.COLOR_RGB2GRAY);
      const da = ga.data;
      const db = gb.data;
      const n = da.length;
      let ma = 0;
      let mb = 0;
      for (let i = 0; i < n; i++) {
        ma += da[i];
        mb += db[i];
      }
      ma /= n;
      mb /= n;
      let sab = 0;
      let saa = 0;
      let sbb = 0;
      for (let i = 0; i < n; i++) {
        const x = da[i] - ma;
        const y = db[i] - mb;
        sab += x * y;
        saa += x * x;
        sbb += y * y;
      }
      del(ga, gb);
      const denom = Math.sqrt(saa * sbb);
      if (denom < 1e-6) {
        // Flat patches: compare mean intensity instead.
        return 1.0 - Math.min(1.0, Math.abs(meanOf(this.cv, a) - meanOf(this.cv, b)) / 40.0);
      }
      return sab / denom;
    }

    /** How well the current patch matches a stored reference at the same location, in [0, 1]. */
    patchMatch(current, reference) {
      // ROIs are not dense in memory; work on continuous copies for the pixel loops.
      const cur = current.isContinuous() ? current : dense(current);
      const ref = reference.isContinuous() ? reference : dense(reference);
      const n = (this.ncc(cur, ref) + 1) / 2;
      let diff = 0;
      if (cur.rows === ref.rows && cur.cols === ref.cols) {
        const a = cur.data;
        const b = ref.data;
        const len = cur.rows * cur.cols * cur.channels();
        let s = 0;
        for (let i = 0; i < len; i++) s += Math.abs(a[i] - b[i]);
        diff = s / len / 255.0;
      }
      if (cur !== current) cur.delete();
      if (ref !== reference) ref.delete();
      return 0.6 * n + 0.4 * (1 - Math.min(1.0, diff * 4));
    }

    /**
     * Find a stored object appearance anywhere in the frame: ORB matching + RANSAC homography first
     * (rotation and scale), then multi-scale normalised template matching. Returns {box, score}.
     */
    locate(template, frame, sig, searchMask = null) {
      const cv = this.cv;
      let bestBox = null;
      let bestScore = 0.0;
      const ownSig = !sig;
      sig = sig || this.signature(template);
      const gray = new cv.Mat();
      cv.cvtColor(frame, gray, cv.COLOR_RGB2GRAY);
      const none = new cv.Mat();
      const kv = new cv.KeyPointVector();
      const desc = new cv.Mat();
      this.orb.detectAndCompute(gray, searchMask || none, kv, desc, false);
      if (sig.desc && sig.desc.rows >= 8 && desc.rows >= 8) {
        const good = this._goodMatches(sig.desc, desc);
        if (good.length >= 8) {
          const src = [];
          const dst = [];
          for (const m of good) {
            src.push(sig.kps[m.queryIdx][0], sig.kps[m.queryIdx][1]);
            const p = kv.get(m.trainIdx).pt;
            dst.push(p.x, p.y);
          }
          const srcM = cv.matFromArray(good.length, 1, cv.CV_32FC2, src);
          const dstM = cv.matFromArray(good.length, 1, cv.CV_32FC2, dst);
          const inl = new cv.Mat();
          const H = cv.findHomography(srcM, dstM, cv.RANSAC, 5.0, inl);
          const nInl = inl.rows ? cv.countNonZero(inl) : 0;
          if (H.rows === 3 && nInl >= 6) {
            const [tw, th] = sig.size;
            const corners = cv.matFromArray(4, 1, cv.CV_32FC2, [0, 0, tw, 0, tw, th, 0, th]);
            const warped = new cv.Mat();
            cv.perspectiveTransform(corners, warped, H);
            const r = cv.boundingRect(warped);
            if (r.width > 4 && r.height > 4) {
              bestBox = [Math.max(0, r.x), Math.max(0, r.y), r.width, r.height];
              bestScore = Math.min(1.0, nInl / 20.0);
            }
            del(corners, warped);
          }
          del(srcM, dstM, inl, H);
        }
      }
      kv.delete();
      del(desc);

      const tgray = new cv.Mat();
      cv.cvtColor(template, tgray, cv.COLOR_RGB2GRAY);
      for (const scale of [0.8, 1.0, 1.25]) {
        const t = new cv.Mat();
        cv.resize(tgray, t, new cv.Size(0, 0), scale, scale, cv.INTER_LINEAR);
        if (t.rows >= gray.rows || t.cols >= gray.cols || Math.min(t.rows, t.cols) < 6) {
          t.delete();
          continue;
        }
        const res = new cv.Mat();
        cv.matchTemplate(gray, t, res, cv.TM_CCOEFF_NORMED);
        let mm;
        if (searchMask) {
          const valid = searchMask.roi(new cv.Rect(0, 0, res.cols, res.rows));
          mm = cv.minMaxLoc(res, valid);
          valid.delete();
        } else {
          mm = cv.minMaxLoc(res);
        }
        const score = mm.maxVal;
        if (score > bestScore && score > 0.6) {
          bestBox = [mm.maxLoc.x, mm.maxLoc.y, t.cols, t.rows];
          bestScore = score;
        }
        del(t, res);
      }
      del(tgray, gray, none);
      if (ownSig) this.freeSig(sig);
      return { box: bestBox, score: bestScore };
    }
  }

  function meanOf(cv, mat) {
    const m = cv.mean(mat);
    const c = mat.channels();
    let s = 0;
    for (let i = 0; i < c; i++) s += m[i];
    return s / c;
  }

  // ===================================================================== geometry.py

  /** Named zones (polygons in processing coordinates) and point-in-zone rules. */
  class RoomGeometry {
    constructor(cv, zones = [], doorZone = null) {
      this.cv = cv;
      this.zones = (zones || []).filter((z) => z && Array.isArray(z.polygon) && z.polygon.length >= 3);
      this.doorZone = doorZone || null;
      this.polys = this.zones.map((z) =>
        cv.matFromArray(z.polygon.length, 1, cv.CV_32FC2, z.polygon.flat().map(Number)));
    }

    dispose() {
      del(...this.polys);
      this.polys = [];
    }

    zoneAt(point, excludeDoor = false) {
      let best = "unzoned";
      let bestDist = -1e9;
      this.zones.forEach((zone, i) => {
        if (excludeDoor && zone.name === this.doorZone) return;
        const d = this.cv.pointPolygonTest(this.polys[i], new this.cv.Point(point[0], point[1]), true);
        if (d >= 0 && d > bestDist) {
          best = zone.name;
          bestDist = d;
        }
      });
      return best;
    }

    zoneForBox(box) {
      const [x, y, w, h] = box;
      // Objects rest on surfaces: use the bottom-centre, fall back to the centre.
      let zone = this.zoneAt([x + w / 2, y + h * 0.9], true);
      if (zone === "unzoned") zone = this.zoneAt([x + w / 2, y + h / 2], true);
      return zone;
    }

    inDoorZone(box) {
      if (!this.doorZone) return false;
      const [x, y, w, h] = box;
      return this.zoneAt([x + w / 2, y + h]) === this.doorZone;
    }

    toMap() {
      return null; // the browser camera has no floor-plan calibration
    }

    /** uint8 mask (0/255) of the given zones rasterised at width x height, or null if none. */
    mask(width, height, which) {
      const cv = this.cv;
      const zs = this.zones.filter(which);
      if (!zs.length) return null;
      const m = cv.Mat.zeros(height, width, cv.CV_8UC1);
      const pts = new cv.MatVector();
      const owned = zs.map((z) => cv.matFromArray(z.polygon.length, 1, cv.CV_32SC2,
        z.polygon.flat().map((v) => Math.round(Number(v)))));
      for (const p of owned) pts.push_back(p);
      cv.fillPoly(m, pts, new cv.Scalar(255));
      del(...owned);
      pts.delete();
      return m;
    }
  }

  // ===================================================================== privacy.py

  /** Crop a box with context margin; returns {mat (clone), offset}. */
  function cropWithMargin(cv, frame, box, margin = 0.35, minSide = 48) {
    const [x, y, w, h] = box;
    const m = Math.floor(Math.max(w, h) * margin);
    const sidePad = Math.max(0, Math.floor((minSide - Math.max(w, h)) / 2));
    const x0 = Math.max(0, x - m - sidePad);
    const y0 = Math.max(0, y - m - sidePad);
    const x1 = Math.min(frame.cols, x + w + m + sidePad);
    const y1 = Math.min(frame.rows, y + h + m + sidePad);
    const roi = frame.roi(new cv.Rect(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0)));
    const mat = dense(roi);
    roi.delete();
    return { mat, offset: [x0, y0] };
  }

  function clipBox(box, w, h) {
    const x = Math.max(0, Math.min(w - 1, box[0]));
    const y = Math.max(0, Math.min(h - 1, box[1]));
    return [x, y, Math.max(1, Math.min(box[2], w - x)), Math.max(1, Math.min(box[3], h - y))];
  }

  /** Rectangle grown by m pixels and clipped to a w x h frame (plain object accepted by Mat.roi). */
  function growRect(r, m, w, h) {
    const x0 = Math.max(0, r.x - m);
    const y0 = Math.max(0, r.y - m);
    const x1 = Math.min(w, r.x + r.width + m);
    const y1 = Math.min(h, r.y + r.height + m);
    return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
  }

  function roiClone(cv, mat, box) {
    const r = mat.roi(new cv.Rect(box[0], box[1], box[2], box[3]));
    const c = dense(r);
    r.delete();
    return c;
  }

  // ===================================================================== scene.py

  /**
   * Scene memory: detects when objects are put down, picked up or moved (port of scene.py).
   *
   * Dual-background static-object detection: a short-term MOG2 model gives "what is moving now",
   * a long-term float reference background gives "what differs from memory". Pixels that differ,
   * are not moving and not covered by a person accumulate time-based evidence; a region that stays
   * stable for stableSeconds with no motion nearby is committed as an event and absorbed.
   */
  class SceneMemory {
    constructor(cv, cfg = {}, geometry = null) {
      this.cv = cv;
      this.cfg = Object.assign({}, DEFAULTS, cfg);
      this.geometry = geometry || new RoomGeometry(cv, [], null);
      this.app = new Appearance(cv);
      this.short = new cv.BackgroundSubtractorMOG2(40, 20, false);
      this.bgLong = null; // CV_32FC3
      this.acc = null; // Float32Array, seconds of stable evidence per pixel
      this.objects = new Map();
      this.persons = []; // [{box, t}] - no person detector in the browser; kept for parity
      this.startedAt = null;
      this.lastT = null;
      this.lightingSince = null;
      this.motionFraction = 0.0;
      this.moving = null; // uint8 0/255 motion mask of the last frame (valid until the next process())
      this.width = 0;
      this.height = 0;
      this.placesMask = null;
      this._placesDirty = true;
      this.stats = { frames: 0, rebaselines: 0, events: 0 };
      this.k3 = cv.getStructuringElement(cv.MORPH_ELLIPSE, new cv.Size(3, 3));
      this.k7 = cv.getStructuringElement(cv.MORPH_ELLIPSE, new cv.Size(7, 7));
      this.k15 = cv.getStructuringElement(cv.MORPH_ELLIPSE, new cv.Size(15, 15));
    }

    setGeometry(geometry) {
      if (this.geometry && this.geometry !== geometry) this.geometry.dispose();
      this.geometry = geometry;
      this._placesDirty = true;
    }

    get learning() {
      return this.startedAt === null || this.lastT === null || this.lastT - this.startedAt < 1.5;
    }

    personBoxes(t, hold = PERSON_HOLD) {
      return this.persons.filter((p) => t - p.t < hold).map((p) => p.box);
    }

    resetBackground() {
      del(this.bgLong);
      this.bgLong = null;
    }

    _ensurePlaces() {
      if (!this._placesDirty || !this.width) return;
      del(this.placesMask);
      this.placesMask = null;
      if (this.cfg.placesOnly) {
        // Only the places the user marked (desk, shelf...) can produce objects; the door zone is
        // used for the door-motion alert only.
        const door = this.geometry.doorZone;
        this.placesMask = this.geometry.mask(this.width, this.height, (z) => z.name !== door);
      }
      this._placesDirty = false;
    }

    // ------------------------------------------------------------------ main loop

    /** Process one RGB frame at time t (seconds). Returns events; each carries a `crop` Mat to delete. */
    process(frame, t) {
      const cv = this.cv;
      this.stats.frames += 1;
      const smooth = new cv.Mat();
      cv.GaussianBlur(frame, smooth, new cv.Size(5, 5), 0);
      if (this.bgLong === null || this.width !== frame.cols || this.height !== frame.rows) {
        del(this.bgLong);
        this.width = frame.cols;
        this.height = frame.rows;
        this._placesDirty = true;
        this.bgLong = new cv.Mat();
        smooth.convertTo(this.bgLong, cv.CV_32FC3);
        this.acc = new Float32Array(this.width * this.height);
        this.startedAt = this.lastT = t;
        const fg0 = new cv.Mat();
        this.short.apply(smooth, fg0, -1);
        del(fg0, smooth);
        return [];
      }
      this._ensurePlaces();
      const dt = Math.min(0.5, Math.max(0.0, t - this.lastT));
      this.lastT = t;
      const N = this.width * this.height;

      // Short-term model: "moving right now".
      const fg = new cv.Mat();
      this.short.apply(smooth, fg, -1);
      cv.morphologyEx(fg, fg, cv.MORPH_OPEN, this.k3);
      if (!this.moving || this.moving.rows !== this.height || this.moving.cols !== this.width) {
        del(this.moving);
        this.moving = new cv.Mat(this.height, this.width, cv.CV_8UC1);
      }
      this.moving.setTo(new cv.Scalar(0));
      if (cv.countNonZero(fg)) {
        // A 15x15 dilation only reaches 7 px, so dilating the foreground's bounding box (plus margin)
        // gives exactly the full-frame result at a fraction of the cost in WebAssembly.
        const r = growRect(cv.boundingRect(fg), 8, this.width, this.height);
        const src = fg.roi(r);
        const dst = this.moving.roi(r);
        cv.dilate(src, dst, this.k15);
        del(src, dst);
      }
      fg.delete();
      this.motionFraction = cv.countNonZero(this.moving) / N;

      // Long-term memory: "different from what I remember".
      const bgU8 = new cv.Mat();
      cv.convertScaleAbs(this.bgLong, bgU8);
      const diff3 = new cv.Mat();
      cv.absdiff(smooth, bgU8, diff3);
      const ch = new cv.MatVector();
      cv.split(diff3, ch);
      const c0 = ch.get(0); // each get() returns a wrapper that must be deleted
      const c1 = ch.get(1);
      const c2 = ch.get(2);
      const diff = new cv.Mat();
      cv.max(c0, c1, diff);
      cv.max(diff, c2, diff);
      del(c0, c1, c2);
      ch.delete();
      const differs = new cv.Mat();
      cv.threshold(diff, differs, this.cfg.longDiffThreshold, 255, cv.THRESH_BINARY);
      del(diff3, diff, bgU8);

      const smoothF = new cv.Mat();
      if (t - this.startedAt < 1.5) {
        // Let the short-term model learn the scene before trusting it.
        smooth.convertTo(smoothF, cv.CV_32FC3);
        cv.addWeighted(this.bgLong, 0.7, smoothF, 0.3, 0, this.bgLong);
        del(smoothF, differs, smooth);
        return [];
      }

      if (this._handleGlobalChange(smooth, differs, t)) {
        del(smoothF, differs, smooth);
        return [];
      }

      // Person boxes (none in the browser) also count as occlusion.
      let personMask = null;
      const pboxes = this.personBoxes(t);
      if (pboxes.length) {
        personMask = cv.Mat.zeros(this.height, this.width, cv.CV_8UC1);
        for (const [x, y, w, h] of pboxes) {
          const px = Math.floor(w * 0.15);
          const py = Math.floor(h * 0.1);
          cv.rectangle(personMask, new cv.Point(x - px, y - py), new cv.Point(x + w + px, y + h + py),
            new cv.Scalar(255), -1);
        }
      }

      const quiet = new cv.Mat(this.height, this.width, cv.CV_8UC1);
      const evidence = new cv.Mat(this.height, this.width, cv.CV_8UC1);
      // Views into the WASM heap are taken after the last allocation: heap growth detaches old views.
      const dd = differs.data;
      const mv = this.moving.data;
      const pm = personMask ? personMask.data : null;
      const places = this.placesMask ? this.placesMask.data : null;
      const acc = this.acc;
      const qd = quiet.data;
      const ed = evidence.data;
      const twoDt = 2 * dt;
      const W = this.width;
      let ex0 = W;
      let ey0 = this.height;
      let ex1 = -1;
      let ey1 = -1;
      for (let i = 0; i < N; i++) {
        // Outside the marked places (browser placesOnly) everything is treated as background.
        const d = dd[i] !== 0 && (places === null || places[i] !== 0);
        const occ = mv[i] !== 0 || (pm !== null && pm[i] !== 0);
        if (d) {
          if (!occ) acc[i] += dt;
        } else {
          const v = acc[i] - twoDt;
          acc[i] = v > 0 ? v : 0;
        }
        qd[i] = !d && !occ ? 255 : 0;
        if (acc[i] > 0.25) {
          ed[i] = 255;
          const y = (i / W) | 0;
          const x = i - y * W;
          if (x < ex0) ex0 = x;
          if (x > ex1) ex1 = x;
          if (y < ey0) ey0 = y;
          if (y > ey1) ey1 = y;
        } else {
          ed[i] = 0;
        }
      }
      del(personMask, differs);

      // Slow drift (lighting through the day) only where nothing is happening.
      smooth.convertTo(smoothF, cv.CV_32FC3);
      const blended = new cv.Mat();
      cv.addWeighted(this.bgLong, 0.998, smoothF, 0.002, 0, blended);
      blended.copyTo(this.bgLong, quiet);
      del(blended, quiet);

      const events = [];
      if (ex1 < 0) {
        del(evidence, smoothF, smooth);
        return events; // nothing is building up evidence
      }

      // Group evidence into candidate regions (closing joins fragments of one object) and commit a
      // region only once nearly all of it is stable, so one object yields one event. Morphology runs
      // on the evidence bounding box plus a 16 px margin, which is identical to the full frame for a
      // 15x15 closing (it reaches 7 px) and much cheaper in WebAssembly.
      const roi = growRect({ x: ex0, y: ey0, width: ex1 - ex0 + 1, height: ey1 - ey0 + 1 }, 16, W, this.height);
      const evRoi = evidence.roi(roi);
      const building = new cv.Mat();
      cv.morphologyEx(evRoi, building, cv.MORPH_CLOSE, this.k15);
      cv.morphologyEx(building, building, cv.MORPH_OPEN, this.k3);
      evRoi.delete();
      const labels = new cv.Mat();
      const stats = new cv.Mat();
      const cents = new cv.Mat();
      const n = cv.connectedComponentsWithStats(building, labels, stats, cents, 8, cv.CV_32S);
      const RW = roi.width;
      const areaTotal = N;
      const stable = this.cfg.stableSeconds;
      for (let c = 1; c < n; c++) {
        const lbx = stats.intAt(c, 0);
        const lby = stats.intAt(c, 1);
        const bx = lbx + roi.x;
        const by = lby + roi.y;
        const bw = stats.intAt(c, 2);
        const bh = stats.intAt(c, 3);
        let pix = 0;
        let ready = 0;
        let maxAcc = 0;
        let ld = labels.data32S;
        let edv = evidence.data;
        for (let r = 0; r < bh; r++) {
          for (let x = 0, i = (by + r) * W + bx, li = (lby + r) * RW + lbx; x < bw; x++, i++, li++) {
            if (ld[li] === c && edv[i] !== 0) {
              pix++;
              const a = acc[i];
              if (a >= stable) ready++;
              if (a > maxAcc) maxAcc = a;
            }
          }
        }
        if (!pix) continue;
        if (ready / pix < 0.85 && maxAcc < 2.5 * stable) continue; // still settling

        const comp = cv.Mat.zeros(this.height, W, cv.CV_8UC1);
        const cd = comp.data;
        ld = labels.data32S;
        edv = evidence.data;
        for (let r = 0; r < bh; r++) {
          for (let x = 0, i = (by + r) * W + bx, li = (lby + r) * RW + lbx; x < bw; x++, i++, li++) {
            if (ld[li] === c && edv[i] !== 0) cd[i] = 255;
          }
        }
        cv.morphologyEx(comp, comp, cv.MORPH_CLOSE, this.k7);
        const area = cv.countNonZero(comp);
        const rect = cv.boundingRect(comp);
        const box = [rect.x, rect.y, rect.width, rect.height];
        if (area < this.cfg.minObjectArea * areaTotal) {
          if (area < 0.25 * this.cfg.minObjectArea * areaTotal) this._absorb(smoothF, comp); // sensor noise
          comp.delete();
          continue;
        }
        if (area > this.cfg.maxObjectArea * areaTotal) {
          this._absorb(smoothF, comp); // furniture moved or camera bumped: not an object
          comp.delete();
          continue;
        }
        if (!this._isQuiet(box, t)) {
          comp.delete();
          continue; // a hand may still be on it; wait
        }
        for (const ev of this._explain(frame, comp, box, t)) events.push(ev);
        this._absorb(smoothF, comp);
        comp.delete();
      }
      del(building, labels, stats, cents, evidence, smoothF, smooth);
      this.stats.events += events.length;
      return events;
    }

    // ------------------------------------------------------------------ helpers

    _handleGlobalChange(smooth, differs, t) {
      const changed = this.cv.countNonZero(differs) / (this.width * this.height);
      if (changed > this.cfg.lightingChangeFraction && this.motionFraction < 0.5) {
        if (this.lightingSince === null) {
          this.lightingSince = t;
        } else if (t - this.lightingSince > 1.0) {
          // Lights switched on/off or auto-exposure jump: re-learn instead of hallucinating events.
          smooth.convertTo(this.bgLong, this.cv.CV_32FC3);
          this.acc.fill(0);
          this.lightingSince = null;
          this.stats.rebaselines += 1;
        }
        return true;
      }
      this.lightingSince = null;
      return false;
    }

    _isQuiet(box, t) {
      const [x, y, w, h] = box;
      const m = 12;
      const y0 = Math.max(0, y - m);
      const y1 = Math.min(this.height, y + h + m);
      const x0 = Math.max(0, x - m);
      const x1 = Math.min(this.width, x + w + m);
      const r = this.moving.roi(new this.cv.Rect(x0, y0, x1 - x0, y1 - y0));
      const frac = this.cv.countNonZero(r) / ((x1 - x0) * (y1 - y0));
      r.delete();
      if (frac > 0.02) return false;
      const grown = [x - m, y - m, w + 2 * m, h + 2 * m];
      return !this.personBoxes(t, 1.0).some((p) => intersects(grown, p));
    }

    _absorb(smoothF, comp) {
      const grown = new this.cv.Mat();
      this.cv.dilate(comp, grown, this.k7);
      smoothF.copyTo(this.bgLong, grown);
      const g = grown.data;
      const acc = this.acc;
      for (let i = 0; i < g.length; i++) if (g[i] !== 0) acc[i] = 0;
      grown.delete();
    }

    _boundaryGradients(cur, under, mask) {
      const cv = this.cv;
      const ring = new cv.Mat();
      cv.morphologyEx(mask, ring, cv.MORPH_GRADIENT, this.k3);
      if (!cv.countNonZero(ring)) {
        ring.delete();
        return [0.0, 0.0];
      }
      const grad = (img) => {
        const g = new cv.Mat();
        const gx = new cv.Mat();
        const gy = new cv.Mat();
        const mag = new cv.Mat();
        cv.cvtColor(img, g, cv.COLOR_RGB2GRAY);
        cv.Sobel(g, gx, cv.CV_32F, 1, 0, 3);
        cv.Sobel(g, gy, cv.CV_32F, 0, 1, 3);
        cv.magnitude(gx, gy, mag);
        const v = cv.mean(mag, ring)[0];
        del(g, gx, gy, mag);
        return v;
      };
      const out = [grad(cur), grad(under)];
      ring.delete();
      return out;
    }

    _explain(frame, comp, box, t) {
      const cv = this.cv;
      const [x, y, w, h] = box;
      const bgU8 = new cv.Mat();
      cv.convertScaleAbs(this.bgLong, bgU8);
      const cur = roiClone(cv, frame, box);
      const under = roiClone(cv, bgU8, box);
      const mask = roiClone(cv, comp, box);
      const events = [];

      // 1) Was a remembered object picked up?
      const consumed = cv.Mat.zeros(mask.rows, mask.cols, cv.CV_8UC1);
      for (const obj of this._overlapping(box)) {
        const [ox, oy, ow, oh] = obj.box;
        if (ox < 0 || oy < 0 || ox + ow > this.width || oy + oh > this.height) continue;
        if (ow !== obj.under.cols || oh !== obj.under.rows) continue;
        const now = frame.roi(new cv.Rect(ox, oy, ow, oh));
        const sUnder = this.app.patchMatch(now, obj.under);
        const sObj = this.app.patchMatch(now, obj.appearance);
        now.delete();
        if (sUnder > sObj + 0.03) {
          events.push(this._removed(obj, t, frame, sUnder - sObj));
          cv.rectangle(consumed, new cv.Point(ox - x, oy - y), new cv.Point(ox - x + ow, oy - y + oh),
            new cv.Scalar(255), -1);
        }
      }
      const notConsumed = new cv.Mat();
      cv.bitwise_not(consumed, notConsumed);
      const remaining = new cv.Mat();
      cv.bitwise_and(mask, notConsumed, remaining);
      const remSum = cv.countNonZero(remaining);
      const maskSum = cv.countNonZero(mask);
      del(consumed, notConsumed, remaining);
      if (remSum < 0.3 * maskSum) {
        // Picked up, or a change on a remembered object that is still there (lighting).
        del(cur, under, mask, bgU8);
        return events;
      }

      // 2) New edges belong to the current frame (placed) or to the old background (ghost)?
      const [gCur, gUnder] = this._boundaryGradients(cur, under, mask);
      if (gCur >= 0.85 * gUnder) events.push(this._placed(frame, cur, under, mask, box, t));
      else events.push(this._ghostRemoved(bgU8, cur, under, mask, box, t));
      bgU8.delete();
      return events; // cur/under/mask are now owned by a tracked object
    }

    _overlapping(box) {
      const out = [];
      for (const obj of this.objects.values()) {
        if (obj.status !== "present") continue;
        const inter = intersection(box, obj.box);
        if (inter > 0.3 * obj.box[2] * obj.box[3] || inter > 0.3 * box[2] * box[3]) out.push(obj);
      }
      return out;
    }

    _isEssential(label) {
      const l = String(label || "").toLowerCase();
      return this.cfg.essentials.some((e) => l.includes(String(e).toLowerCase()));
    }

    _event(type, obj, t, box, crop, confidence, extra = {}) {
      return Object.assign({
        id: newId("ev"), type, object_id: obj.id, ts: t, zone: obj.zone, bbox: box.map((v) => Math.round(v)),
        map_xy: null, label: obj.label, local_label: obj.local_label, confidence: Math.round(confidence * 1000) / 1000,
        note: "", essential: !!obj.essential, crop: crop.mat, crop_offset: crop.offset,
      }, extra);
    }

    _freeObject(obj) {
      del(obj.appearance, obj.under, obj.mask);
      this.app.freeSig(obj.sig);
    }

    _placed(frame, cur, under, mask, box, t) {
      const sig = this.app.signature(cur, mask);
      const zone = this.geometry.zoneForBox(box);
      let best = null;
      let bestScore = 0.0;
      for (const obj of this.objects.values()) {
        if (obj.status === "removed" && obj.removedAt && t - obj.removedAt < REID_WINDOW) {
          const score = this.app.similarity(sig, obj.sig);
          if (score > bestScore) {
            best = obj;
            bestScore = score;
          }
        }
      }
      const crop = cropWithMargin(this.cv, frame, box);
      if (best !== null && bestScore >= REID_THRESHOLD) {
        const fromZone = best.zone;
        this._freeObject(best);
        Object.assign(best, {
          box, zone, appearance: cur, under, mask, sig, status: "present", removedAt: null, lastSeen: t,
        });
        const moved = fromZone !== zone;
        return this._event(moved ? "moved" : "placed", best, t, box, crop, bestScore, Object.assign(
          { note: `re-identified (similarity ${bestScore.toFixed(2)})` + (moved ? "" : ", put back") },
          moved ? { from_zone: fromZone } : {}));
      }
      const obj = {
        id: newId("obj"), label: "object", local_label: "object", confidence: 0, box, zone, status: "present",
        firstSeen: t, lastSeen: t, appearance: cur, under, mask, sig, essential: false, removedAt: null,
      };
      this.objects.set(obj.id, obj);
      return this._event("placed", obj, t, box, crop, 0);
    }

    _removed(obj, t, frame, margin) {
      obj.status = "removed";
      obj.removedAt = t;
      obj.lastSeen = t;
      const crop = cropWithMargin(this.cv, frame, obj.box);
      return this._event("removed", obj, t, obj.box, crop, Math.min(1.0, 0.5 + margin), { note: "picked up" });
    }

    /** An object that was already there when memory started has been taken away. */
    _ghostRemoved(bgU8, cur, under, mask, box, t) {
      const obj = {
        id: newId("obj"), label: "object", local_label: "object", confidence: 0, box,
        zone: this.geometry.zoneForBox(box), status: "removed", firstSeen: t, lastSeen: t,
        appearance: under, under: cur, mask, essential: false, removedAt: t,
      };
      obj.sig = this.app.signature(obj.appearance, mask);
      this.objects.set(obj.id, obj);
      const crop = cropWithMargin(this.cv, bgU8, box);
      return this._event("removed", obj, t, box, crop, 0,
        { note: "pre-existing object taken (seen in remembered background)" });
    }

    // ------------------------------------------------------------------ live perception tools

    verify(objectId, frame, t) {
      const obj = this.objects.get(objectId);
      if (!obj) return { ok: false, error: `unknown object ${objectId}` };
      const box = clipBox(obj.box, frame.cols, frame.rows);
      const now = roiClone(this.cv, frame, box);
      const occluded = this.personBoxes(t, 1.0).some((p) => intersects(obj.box, p));
      const sObj = this.app.patchMatch(now, obj.appearance);
      const sUnder = this.app.patchMatch(now, obj.under);
      now.delete();
      const present = sObj > Math.max(0.6, sUnder);
      if (present) obj.lastSeen = t;
      return {
        ok: true, object_id: objectId, present, occluded,
        match_object: Math.round(sObj * 1000) / 1000, match_empty_surface: Math.round(sUnder * 1000) / 1000,
        zone: obj.zone, bbox: obj.box.map((v) => Math.round(v)),
      };
    }

    scan(objectId, frame, t) {
      const obj = this.objects.get(objectId);
      if (!obj) return { ok: false, error: `unknown object ${objectId}` };
      let mask = null;
      const pboxes = this.personBoxes(t, 1.0);
      if (pboxes.length) {
        mask = new this.cv.Mat(frame.rows, frame.cols, this.cv.CV_8UC1, new this.cv.Scalar(255));
        for (const [x, y, w, h] of pboxes) {
          this.cv.rectangle(mask, new this.cv.Point(x, y), new this.cv.Point(x + w, y + h), new this.cv.Scalar(0), -1);
        }
      }
      const { box, score } = this.app.locate(obj.appearance, frame, obj.sig, mask);
      del(mask);
      if (box === null || score < 0.5) {
        return { ok: true, object_id: objectId, found: false, score: Math.round(score * 1000) / 1000 };
      }
      return {
        ok: true, object_id: objectId, found: true, score: Math.round(score * 1000) / 1000,
        zone: this.geometry.zoneForBox(box), bbox: box.map((v) => Math.round(v)), map_xy: null,
      };
    }

    /** After a successful scan, move the remembered object to where it was found. */
    adoptFound(objectId, box, frame, t) {
      const cv = this.cv;
      const obj = this.objects.get(objectId);
      if (!obj) return null;
      box = clipBox(box.map((v) => Math.round(v)), frame.cols, frame.rows);
      const fromZone = obj.zone;
      this._freeObject(obj);
      obj.box = box;
      obj.zone = this.geometry.zoneForBox(box);
      obj.appearance = roiClone(cv, frame, box);
      obj.mask = new cv.Mat(box[3], box[2], cv.CV_8UC1, new cv.Scalar(255));
      obj.under = dense(obj.appearance); // unknown; refreshed on next pick-up/put-down
      obj.sig = this.app.signature(obj.appearance, obj.mask);
      obj.status = "present";
      obj.removedAt = null;
      obj.lastSeen = t;
      const crop = cropWithMargin(cv, frame, box);
      const moved = fromZone !== obj.zone;
      return this._event(moved ? "moved" : "placed", obj, t, box, crop, 0.7,
        Object.assign({ note: "found by camera scan" }, moved ? { from_zone: fromZone } : {}));
    }

    // ------------------------------------------------------------------ persistence + overlay helpers

    meta(obj) {
      return {
        id: obj.id, label: obj.label, local_label: obj.local_label, confidence: obj.confidence, box: obj.box,
        zone: obj.zone, status: obj.status, first_seen: obj.firstSeen, last_seen: obj.lastSeen,
        essential: !!obj.essential, removed_at: obj.removedAt,
      };
    }

    /** Re-create a remembered object from persisted metadata and RGB/gray Mats (ownership taken). */
    importObject(meta, appearance, under, mask) {
      const obj = {
        id: meta.id, label: meta.label || "object", local_label: meta.local_label || "object",
        confidence: meta.confidence || 0, box: meta.box, zone: meta.zone || "unzoned", status: meta.status || "present",
        firstSeen: meta.first_seen, lastSeen: meta.last_seen, essential: !!meta.essential,
        removedAt: meta.removed_at || null, appearance, under, mask,
      };
      obj.sig = this.app.signature(appearance, mask);
      const old = this.objects.get(obj.id);
      if (old) this._freeObject(old);
      this.objects.set(obj.id, obj);
      return obj;
    }

    /** Bounding boxes of regions currently building evidence (for the overlay). */
    settlingBoxes(minSeconds = 0.3) {
      if (!this.acc || !this.width) return [];
      const cv = this.cv;
      const m = new cv.Mat(this.height, this.width, cv.CV_8UC1);
      const d = m.data;
      const acc = this.acc;
      for (let i = 0; i < d.length; i++) d[i] = acc[i] > minSeconds ? 255 : 0;
      const contours = new cv.MatVector();
      const hier = new cv.Mat();
      cv.findContours(m, contours, hier, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
      const out = [];
      for (let i = 0; i < contours.size(); i++) {
        const c = contours.get(i);
        const r = cv.boundingRect(c);
        if (r.width * r.height >= 36) out.push([r.x, r.y, r.width, r.height]);
        c.delete();
      }
      contours.delete();
      del(hier, m);
      return out;
    }

    dispose() {
      for (const obj of this.objects.values()) this._freeObject(obj);
      this.objects.clear();
      del(this.bgLong, this.moving, this.placesMask, this.k3, this.k7, this.k15, this.short);
      this.app.dispose();
      if (this.geometry) this.geometry.dispose();
    }
  }

  // ===================================================================== browser layer

  let loadPromise = null;

  /** Inject opencv.js (SRI-pinned) and wait for the WASM runtime; idempotent. */
  function load() {
    if (loadPromise) return loadPromise;
    loadPromise = new Promise((resolve, reject) => {
      const ready = async () => {
        try {
          let cv = self.cv;
          if (cv instanceof Promise) cv = await cv;
          else if (!cv || !cv.Mat) {
            await new Promise((r) => {
              const prev = cv && cv.onRuntimeInitialized;
              cv.onRuntimeInitialized = () => {
                if (prev) prev();
                r();
              };
            });
            cv = self.cv;
          }
          self.cv = cv;
          resolve(cv);
        } catch (err) {
          reject(err);
        }
      };
      if (typeof self !== "undefined" && self.cv && (self.cv.Mat || self.cv instanceof Promise)) {
        ready();
        return;
      }
      const s = document.createElement("script");
      s.src = OPENCV_URL;
      s.integrity = OPENCV_SRI;
      s.crossOrigin = "anonymous";
      s.async = true;
      s.onload = ready;
      s.onerror = () => {
        loadPromise = null;
        reject(new Error("Could not load OpenCV (check the internet connection)."));
      };
      document.head.appendChild(s);
    });
    return loadPromise;
  }

  function hashString(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(16);
  }

  function lsGet(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function lsSet(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  }

  /** RGB or gray Mat -> data URL via a canvas (OpenCV.js has no imencode). */
  function matToDataUrl(cv, mat, type = "image/jpeg", quality = 0.85, maxSide = 0) {
    let src = mat;
    let scaled = null;
    if (maxSide && Math.max(mat.rows, mat.cols) > maxSide) {
      const s = maxSide / Math.max(mat.rows, mat.cols);
      scaled = new cv.Mat();
      cv.resize(mat, scaled, new cv.Size(Math.max(1, Math.round(mat.cols * s)), Math.max(1, Math.round(mat.rows * s))),
        0, 0, cv.INTER_AREA);
      src = scaled;
    }
    const rgba = new cv.Mat();
    cv.cvtColor(src, rgba, src.channels() === 1 ? cv.COLOR_GRAY2RGBA : cv.COLOR_RGB2RGBA);
    const canvas = document.createElement("canvas");
    canvas.width = rgba.cols;
    canvas.height = rgba.rows;
    canvas.getContext("2d").putImageData(new ImageData(new Uint8ClampedArray(rgba.data), rgba.cols, rgba.rows), 0, 0);
    del(rgba, scaled);
    return canvas.toDataURL(type, quality);
  }

  function dataUrlToMat(cv, url, gray) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const c = document.createElement("canvas");
        c.width = img.naturalWidth;
        c.height = img.naturalHeight;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0);
        const rgba = cv.matFromImageData(ctx.getImageData(0, 0, c.width, c.height));
        const out = new cv.Mat();
        cv.cvtColor(rgba, out, gray ? cv.COLOR_RGBA2GRAY : cv.COLOR_RGBA2RGB);
        rgba.delete();
        resolve(out);
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  const b64 = (dataUrl) => dataUrl.slice(dataUrl.indexOf(",") + 1);

  /** Browser camera engine (see docs/WEB_CAMERA.md). */
  class Engine {
    constructor(opts) {
      this.opts = Object.assign({ apiBase: "", zones: [], doorZone: null, essentials: DEFAULTS.essentials,
        placesOnly: true, targetFps: 8 }, opts || {});
      this.video = this.opts.video;
      this.overlay = this.opts.overlay || null;
      this.key = `icamfind.edge.${this.opts.deviceId || hashString(this.opts.deviceToken || "local")}`;
      this.cv = null;
      this.core = null;
      this.running = false;
      this.frame = null; // current RGB frame (valid during a tick)
      this.lastFrameUrl = null;
      this.commands = [];
      this.queue = this._loadQueue();
      this.sending = false;
      this.backoff = 1000;
      this.timers = [];
      this.fps = 0;
      this._lastTick = 0;
      this._lastHeartbeat = 0;
      this._lastSnapshot = 0;
      this._snapshotRequested = true;
      this._doorSince = null;
      this._lastDoorAlert = 0;
      this._saveTimer = null;
      this._statusAt = 0;
      this._capture = null;
      this._frameSize = [640, 480];
      this.doorMask = null;
    }

    get objects() {
      if (!this.core) return [];
      return [...this.core.objects.values()].map((o) => ({ id: o.id, label: o.label, zone: o.zone, status: o.status,
        box: o.box.slice(), essential: !!o.essential }));
    }

    get frameSize() {
      return this._frameSize.slice();
    }

    _status(state, message = "") {
      const s = { state, fps: Math.round(this.fps * 10) / 10,
        objects: this.core ? [...this.core.objects.values()].filter((o) => o.status === "present").length : 0, message };
      this.lastStatus = s;
      if (this.opts.onStatus) {
        try {
          this.opts.onStatus(s);
        } catch (e) { /* host callback errors must not stop the camera */ }
      }
    }

    async _fetch(path, opts = {}) {
      const res = await fetch(`${this.opts.apiBase}${path}`, {
        method: opts.method || "GET",
        headers: Object.assign({ "x-icamfind-device-token": this.opts.deviceToken || "" },
          opts.body !== undefined ? { "content-type": "application/json" } : {}),
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json().catch(() => ({}));
    }

    async start() {
      if (this.running) return;
      this.running = true;
      this._status("loading", "Loading OpenCV…");
      try {
        this.cv = await load();
      } catch (err) {
        this.running = false;
        this._status("error", err.message || String(err));
        return;
      }
      const cv = this.cv;
      if (!this.core) {
        const geometry = new RoomGeometry(cv, this.opts.zones, this.opts.doorZone);
        this.core = new SceneMemory(cv, { essentials: this.opts.essentials, placesOnly: this.opts.placesOnly }, geometry);
        await this._restore();
      }
      this._capture = document.createElement("canvas");
      this._status("learning", "Learning the room…");
      this.timers.push(setInterval(() => this._pollCommands(), 2000));
      this.timers.push(setInterval(() => this._drainQueue(), 1500));
      this._tick();
    }

    stop() {
      this.running = false;
      this.timers.forEach((t) => clearInterval(t));
      this.timers = [];
      clearTimeout(this._tickTimer);
      this._save();
      if (this.core) {
        this.core.dispose();
        this.core = null;
      }
      del(this.doorMask);
      this.doorMask = null;
      this._status("paused", "Camera paused");
    }

    setZones(zones, doorZone) {
      this.opts.zones = zones || [];
      this.opts.doorZone = doorZone || null;
      if (this.core) this.core.setGeometry(new RoomGeometry(this.cv, this.opts.zones, this.opts.doorZone));
      del(this.doorMask);
      this.doorMask = null; // rebuilt on the next frame
      this._lastHeartbeat = 0;
    }

    resetBackground() {
      if (this.core) this.core.resetBackground();
      this._status("learning", "Learning the room…");
    }

    snapshotDataUrl() {
      return this.lastFrameUrl;
    }

    // ------------------------------------------------------------------ frame loop

    _grab() {
      const v = this.video;
      if (!v || v.readyState < 2 || !v.videoWidth) return null;
      const W = 640;
      const H = Math.max(2, Math.round((v.videoHeight / v.videoWidth) * W / 2) * 2);
      this._frameSize = [W, H];
      const c = this._capture;
      if (c.width !== W || c.height !== H) {
        c.width = W;
        c.height = H;
      }
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(v, 0, 0, W, H);
      const rgba = this.cv.matFromImageData(ctx.getImageData(0, 0, W, H));
      const rgb = new this.cv.Mat();
      this.cv.cvtColor(rgba, rgb, this.cv.COLOR_RGBA2RGB);
      rgba.delete();
      return rgb;
    }

    _tick() {
      if (!this.running) return;
      const started = performance.now();
      let frame = null;
      try {
        frame = this._grab();
        if (frame) {
          const t = Date.now() / 1000;
          if (this._lastTick) this.fps = 0.8 * this.fps + 0.2 * (1000 / Math.max(1, started - this._lastTick));
          this._lastTick = started;
          this.frame = frame;
          for (const ev of this.core.process(frame, t)) this._publish(ev);
          this._doorCheck(t);
          while (this.commands.length) this._runCommand(this.commands.shift(), t);
          if (t - this._lastHeartbeat > 20) this._heartbeat(t);
          this._draw();
          if (started - this._statusAt > 1000) {
            this._statusAt = started;
            this._status(this.core.learning ? "learning" : "watching",
              this.core.learning ? "Learning the room…" : "Watching for things being put down");
          }
        }
      } catch (err) {
        this._status("error", (err && err.message) || String(err));
      } finally {
        this.frame = null;
        del(frame);
      }
      const elapsed = performance.now() - started;
      this._tickTimer = setTimeout(() => this._tick(), Math.max(0, 1000 / this.opts.targetFps - elapsed));
    }

    _publish(ev) {
      const cv = this.cv;
      const crop = matToDataUrl(cv, ev.crop, "image/jpeg", 0.88, 320);
      del(ev.crop);
      const payload = {};
      for (const k of ["id", "type", "object_id", "ts", "zone", "bbox", "map_xy", "label", "local_label", "confidence",
        "note", "essential", "from_zone"]) {
        if (ev[k] !== undefined) payload[k] = ev[k];
      }
      this.queue.push({ event: payload, crop_b64: b64(crop) });
      while (this.queue.length > 40) this.queue.shift(); // cap memory/localStorage use
      this._saveQueue();
      this._drainQueue();
      this._scheduleSave();
      if (this.opts.onEvent) {
        try {
          this.opts.onEvent({ type: ev.type, label: ev.label, zone: ev.zone, from_zone: ev.from_zone, ts: ev.ts,
            object_id: ev.object_id, cropDataUrl: crop });
        } catch (e) { /* host callback errors must not stop the camera */ }
      }
    }

    _snapshot(maxSide = 960) {
      if (!this.frame) return null;
      const url = matToDataUrl(this.cv, this.frame, "image/jpeg", 0.8, maxSide);
      this.lastFrameUrl = url;
      return url;
    }

    _heartbeat(t) {
      this._lastHeartbeat = t;
      const body = { zones: this.opts.zones, door_zone: this.opts.doorZone, frame_size: this._frameSize, map: null };
      if (this._snapshotRequested || t - this._lastSnapshot > 300) {
        const url = this._snapshot();
        if (url) {
          body.snapshot_b64 = b64(url);
          this._lastSnapshot = t;
          this._snapshotRequested = false;
        }
      } else if (this.frame) {
        this.lastFrameUrl = matToDataUrl(this.cv, this.frame, "image/jpeg", 0.7, 640);
      }
      this._fetch("api/device/heartbeat", { method: "POST", body }).catch(() => {});
    }

    async _pollCommands() {
      if (!this.running || !this.opts.deviceToken) return;
      try {
        const res = await this._fetch("api/device/commands");
        for (const c of res.commands || []) this.commands.push(c);
      } catch { /* offline: try again next tick */ }
    }

    _runCommand(cmd, t) {
      const cv = this.cv;
      const args = cmd.args || {};
      let result;
      let image = null;
      if (cmd.kind === "verify_object") {
        result = this.core.verify(args.object_id || "", this.frame, t);
        if (result.ok) {
          const obj = this.core.objects.get(args.object_id);
          const crop = cropWithMargin(cv, this.frame, clipBox(obj.box, this.frame.cols, this.frame.rows), 0.8);
          image = matToDataUrl(cv, crop.mat, "image/jpeg", 0.85, 360);
          crop.mat.delete();
        }
      } else if (cmd.kind === "scan_for_object") {
        result = this.core.scan(args.object_id || "", this.frame, t);
        if (result.found) {
          const ev = this.core.adoptFound(args.object_id, result.bbox, this.frame, t);
          if (ev) this._publish(ev);
          const crop = cropWithMargin(cv, this.frame, clipBox(result.bbox, this.frame.cols, this.frame.rows), 0.8);
          image = matToDataUrl(cv, crop.mat, "image/jpeg", 0.85, 360);
          crop.mat.delete();
        } else {
          image = this._snapshot();
        }
      } else if (cmd.kind === "snapshot") {
        this._snapshotRequested = true;
        this._heartbeat(t);
        image = this.lastFrameUrl;
        result = { ok: true, frame_size: this._frameSize };
      } else if (cmd.kind === "speak") {
        this._say(String(args.text || "").slice(0, 300));
        result = { ok: true, spoken: true };
      } else {
        result = { ok: false, error: `unknown command ${cmd.kind}` };
      }
      const body = { result };
      if (image) body.crop_b64 = b64(image);
      this._fetch(`api/device/commands/${encodeURIComponent(cmd.id)}/result`, { method: "POST", body }).catch(() => {});
    }

    _say(text) {
      try {
        if (typeof speechSynthesis !== "undefined" && text) {
          const u = new SpeechSynthesisUtterance(text);
          u.rate = 0.95;
          speechSynthesis.speak(u);
        }
      } catch { /* speech is best effort */ }
    }

    _doorCheck(t) {
      const cv = this.cv;
      if (!this.opts.doorZone || !this.core.moving) return;
      if (!this.doorMask) {
        const door = this.opts.doorZone;
        this.doorMask = this.core.geometry.mask(this._frameSize[0], this._frameSize[1], (z) => z.name === door);
        if (!this.doorMask) return;
        this._doorArea = cv.countNonZero(this.doorMask);
      }
      if (!this._doorArea || this.core.learning) return;
      const both = new cv.Mat();
      cv.bitwise_and(this.core.moving, this.doorMask, both);
      const frac = cv.countNonZero(both) / this._doorArea;
      both.delete();
      // Without a person detector, someone at the door = significant motion inside the door zone.
      if (frac <= 0.08) {
        this._doorSince = null;
        return;
      }
      if (this._doorSince === null) this._doorSince = t;
      if (t - this._doorSince < 0.5 || t - this._lastDoorAlert < 45) return;
      const left = [...this.core.objects.values()].filter((o) => o.status === "present" && o.essential);
      if (!left.length) return;
      this._lastDoorAlert = t;
      const names = left.slice(0, 3).map((o) => `your ${o.label} on the ${o.zone}`).join(", ");
      const message = `Wait — ${names} ${left.length === 1 ? "is" : "are"} still here.`;
      this._say(message);
      this._fetch("api/device/alerts", { method: "POST",
        body: { kind: "door_essentials", message, objects: left.map((o) => o.id), ts: t } }).catch(() => {});
      if (this.opts.onAlert) {
        try {
          this.opts.onAlert({ kind: "door_essentials", message });
        } catch (e) { /* ignore */ }
      }
    }

    // ------------------------------------------------------------------ uplink queue

    _loadQueue() {
      try {
        return JSON.parse(lsGet(`${this.key}.queue`) || "[]");
      } catch {
        return [];
      }
    }

    _saveQueue() {
      if (!lsSet(`${this.key}.queue`, JSON.stringify(this.queue))) {
        lsSet(`${this.key}.queue`, JSON.stringify(this.queue.map((q) => ({ event: q.event })))); // quota: drop crops
      }
    }

    async _drainQueue() {
      if (this.sending || !this.queue.length || !this.opts.deviceToken) return;
      this.sending = true;
      try {
        while (this.queue.length) {
          const item = this.queue[0];
          const res = await this._fetch("api/device/events", { method: "POST", body: item });
          this.queue.shift();
          this._saveQueue();
          const obj = res && res.object;
          if (obj && obj.id && this.core && this.core.objects.has(obj.id)) {
            const local = this.core.objects.get(obj.id);
            if (obj.label) local.label = obj.label;
            local.essential = !!obj.essential || this.core._isEssential(local.label);
            this._scheduleSave();
          }
          this.backoff = 1000;
        }
      } catch {
        this.backoff = Math.min(30000, this.backoff * 2);
        setTimeout(() => this._drainQueue(), this.backoff);
      } finally {
        this.sending = false;
      }
    }

    // ------------------------------------------------------------------ persistence

    _scheduleSave() {
      clearTimeout(this._saveTimer);
      this._saveTimer = setTimeout(() => this._save(), 2000);
    }

    _save() {
      if (!this.core || !this.cv) return;
      try {
        const objs = [...this.core.objects.values()].sort((a, b) => (b.lastSeen || 0) - (a.lastSeen || 0)).slice(0, 60);
        const data = objs.map((o) => ({
          meta: this.core.meta(o),
          app: matToDataUrl(this.cv, o.appearance, "image/png"),
          under: matToDataUrl(this.cv, o.under, "image/png"),
          mask: matToDataUrl(this.cv, o.mask, "image/png"),
        }));
        lsSet(this.key, JSON.stringify({ v: 1, objects: data }));
      } catch { /* storage full or unavailable: memory still lives in the cloud */ }
    }

    async _restore() {
      let saved = null;
      try {
        saved = JSON.parse(lsGet(this.key) || "null");
      } catch {
        saved = null;
      }
      if (!saved || !Array.isArray(saved.objects)) return;
      for (const item of saved.objects) {
        try {
          const [app, under, mask] = await Promise.all([dataUrlToMat(this.cv, item.app, false),
            dataUrlToMat(this.cv, item.under, false), dataUrlToMat(this.cv, item.mask, true)]);
          this.core.importObject(item.meta, app, under, mask);
        } catch { /* skip a damaged entry */ }
      }
    }

    // ------------------------------------------------------------------ overlay

    _draw() {
      const c = this.overlay;
      if (!c) return;
      const dpr = (typeof devicePixelRatio !== "undefined" && devicePixelRatio) || 1;
      const cw = Math.max(1, Math.round((c.clientWidth || c.width) * dpr));
      const ch = Math.max(1, Math.round((c.clientHeight || c.height) * dpr));
      if (c.width !== cw || c.height !== ch) {
        c.width = cw;
        c.height = ch;
      }
      const ctx = c.getContext("2d");
      ctx.clearRect(0, 0, cw, ch);
      const [W, H] = this._frameSize;
      const sx = cw / W;
      const sy = ch / H;
      ctx.lineWidth = 2 * dpr;
      ctx.font = `${Math.round(13 * dpr)}px system-ui, sans-serif`;
      for (const z of this.opts.zones || []) {
        if (!z.polygon || z.polygon.length < 3) continue;
        const door = z.name === this.opts.doorZone;
        ctx.strokeStyle = door ? "#f59e0b" : "#22d3ee";
        ctx.fillStyle = door ? "rgba(245,158,11,0.10)" : "rgba(34,211,238,0.08)";
        ctx.beginPath();
        z.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy)));
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        const [lx, ly] = z.polygon[0];
        ctx.fillStyle = ctx.strokeStyle;
        ctx.fillText(z.name, lx * sx + 4 * dpr, ly * sy + 16 * dpr);
      }
      ctx.setLineDash([6 * dpr, 4 * dpr]);
      ctx.strokeStyle = "#facc15";
      for (const [x, y, w, h] of this.core.settlingBoxes()) ctx.strokeRect(x * sx, y * sy, w * sx, h * sy);
      ctx.setLineDash([]);
      for (const o of this.core.objects.values()) {
        if (o.status !== "present") continue;
        const [x, y, w, h] = o.box;
        ctx.strokeStyle = o.essential ? "#fbbf24" : "#4ade80";
        ctx.strokeRect(x * sx, y * sy, w * sx, h * sy);
        const label = `${o.label}`;
        const tw = ctx.measureText(label).width + 8 * dpr;
        ctx.fillStyle = "rgba(0,0,0,0.6)";
        ctx.fillRect(x * sx, y * sy - 18 * dpr, tw, 18 * dpr);
        ctx.fillStyle = "#fff";
        ctx.fillText(label, x * sx + 4 * dpr, y * sy - 5 * dpr);
      }
    }
  }

  function create(opts) {
    return new Engine(opts);
  }

  return { load, create, SceneMemory, RoomGeometry, Appearance, cropWithMargin, DEFAULTS, Engine };
});
