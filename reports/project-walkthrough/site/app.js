/* afterstage walkthrough — all visualizations are hand-rolled SVG/canvas, no dependencies. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const NS = "http://www.w3.org/2000/svg";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, reduced ? 0 : ms));

  /** Create an SVG element. Colour-ish keys go to style so CSS variables work everywhere. */
  function s(tag, attrs = {}, parent) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      if (["fill", "stroke", "opacity", "font-weight", "font-size", "font-family", "transition"].includes(k)) el.style.setProperty(k, v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v);
    }
    if (parent) parent.appendChild(el);
    return el;
  }
  function h(tag, attrs = {}, parent) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") el.textContent = v;
      else if (k === "html") el.innerHTML = v;
      else if (k === "style") el.style.cssText = v;
      else el.setAttribute(k, v);
    }
    if (parent) parent.appendChild(el);
    return el;
  }
  function rng(seed) { // mulberry32
    return () => {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const STAGES = [
    { id: "analyze", name: "Analyze", color: "var(--s-analyze)", out: "cast.json",
      desc: "Scenes, faces → cast members, vocal stem, transcript and VLM captions. Writes an editable cast bible." },
    { id: "write", name: "Write", color: "var(--s-write)", out: "screenplay.json",
      desc: "A local LLM turns your request into shots: who says what, where, framed how — schema-validated with a repair loop." },
    { id: "voice", name: "Voice", color: "var(--s-voice)", out: "audio/*.wav",
      desc: "Chatterbox Multilingual clones each member's voice and speaks every line, with emotion control." },
    { id: "design", name: "Design", color: "var(--s-design)", out: "keyframes/*.png",
      desc: "Qwen-Image-Edit composes set plates + cast reference crops into one keyframe per shot; ArcFace gates identity." },
    { id: "render", name: "Render", color: "var(--s-render)", out: "shots/*.mp4",
      desc: "InfiniteTalk lip-syncs talk shots in chained windows; Wan2.2 animates b-roll and the bridge out of the source clip." },
    { id: "edit", name: "Edit", color: "var(--s-edit)", out: "final/*.mp4",
      desc: "ffmpeg conforms, concatenates, ducks music under dialogue, burns subtitles + AI badge, tags provenance." },
  ];

  // ------------------------------------------------------------------ theme
  const themeBtn = $("#theme-btn");
  const savedTheme = store.get("as-theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  themeBtn.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("as-theme", next);
    redrawCanvases();
  });

  // ------------------------------------------------------------------ nav + reveal
  const navLinks = $$("#nav a");
  const sections = navLinks.map((a) => $(a.getAttribute("href"))).filter(Boolean);
  const navObs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        navLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
      }
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  sections.forEach((sec) => navObs.observe(sec));
  const revealObs = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); revealObs.unobserve(e.target); }
  }, { threshold: 0.12 });
  $$(".reveal").forEach((el) => revealObs.observe(el));

  // ------------------------------------------------------------------ copy buttons
  $$(".codeblock").forEach((block) => {
    const pre = $("pre", block);
    const btn = h("button", { class: "copy", text: "copy" }, block);
    btn.addEventListener("click", async () => {
      const text = pre.innerText.split("\n").map((l) => l.replace(/\s+#.*$/, "")).join("\n").trim();
      try { await navigator.clipboard.writeText(text); btn.textContent = "copied"; btn.classList.add("done"); }
      catch { btn.textContent = "select + ⌘C"; }
      setTimeout(() => { btn.textContent = "copy"; btn.classList.remove("done"); }, 1500);
    });
  });

  // ------------------------------------------------------------------ checklist
  $$(".check").forEach((row) => {
    const box = $("input", row);
    const key = "as-check-" + row.dataset.id;
    box.checked = store.get(key) === "1";
    row.classList.toggle("done", box.checked);
    box.addEventListener("change", () => { store.set(key, box.checked ? "1" : "0"); row.classList.toggle("done", box.checked); });
  });

  // ================================================================== HERO PIPELINE
  const pipe = { svg: $("#pipeline-svg"), active: 0, playing: !reduced, timer: null, packets: [] };
  function drawPipeline() {
    const svg = pipe.svg;
    svg.innerHTML = "";
    const W = 150, H = 74, y = 60, gap = (1100 - 40 - W * 6) / 5;
    pipe.nodes = [];
    const defs = s("defs", {}, svg);
    const mk = s("marker", { id: "arr", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto" }, defs);
    s("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--ink-3)" }, mk);
    STAGES.forEach((st, i) => {
      const x = 20 + i * (W + gap);
      if (i < 5) {
        s("line", { x1: x + W + 4, y1: y + H / 2, x2: x + W + gap - 6, y2: y + H / 2, stroke: "var(--line)", "stroke-width": 3, "marker-end": "url(#arr)" }, svg);
      }
      const g = s("g", { class: "pnode", tabindex: 0, role: "button", "aria-label": st.name }, svg);
      g.style.cursor = "pointer";
      const rect = s("rect", { x, y, width: W, height: H, rx: 14, fill: "var(--bg-3)", stroke: st.color, "stroke-width": 2 }, g);
      s("circle", { cx: x + 22, cy: y + 24, r: 11, fill: st.color }, g);
      s("text", { x: x + 22, y: y + 28, "text-anchor": "middle", fill: "#fff", "font-size": "12px", "font-weight": 700, text: i + 1 }, g);
      s("text", { x: x + 40, y: y + 29, fill: "var(--ink)", "font-size": "16px", "font-weight": 700, text: st.name }, g);
      s("text", { x: x + 14, y: y + 56, fill: "var(--ink-3)", "font-size": "12px", "font-family": "var(--mono)", text: st.out }, g);
      g.addEventListener("click", () => { selectStage(st.id); $("#pipeline").scrollIntoView(); });
      g.addEventListener("keydown", (e) => { if (e.key === "Enter") g.dispatchEvent(new Event("click")); });
      pipe.nodes.push({ g, rect, x, y, W, H });
    });
    // inputs/outputs labels
    s("text", { x: 20, y: 30, fill: "var(--ink-3)", "font-size": "13px", text: "clip + “make a podcast after the show”" }, svg);
    s("text", { x: 1080, y: 30, fill: "var(--ink-3)", "font-size": "13px", "text-anchor": "end", text: "source clip → bridge → new scenes" }, svg);
    // progress rail
    pipe.rail = s("rect", { x: 20, y: 170, width: 1060, height: 6, rx: 3, fill: "var(--bg-3)" }, svg);
    pipe.fill = s("rect", { x: 20, y: 170, width: 0, height: 6, rx: 3, fill: "var(--accent)", transition: "width .5s ease" }, svg);
    pipe.ticks = STAGES.map((st, i) => s("text", { x: 20 + i * (W + gap) + W / 2, y: 200, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "12px", text: ["~2 min", "~1 min", "~1 min", "~5 min", "hours", "~1 min"][i] }, svg));
    s("text", { x: 550, y: 222, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: "rough wall-clock on a DGX Spark, draft profile, 90 s output — estimates" }, svg);
    pipe.packet = s("circle", { r: 7, cx: -20, cy: y + H / 2, fill: "var(--accent)" }, svg);
    setPipeActive(pipe.active);
  }
  function setPipeActive(i) {
    pipe.active = i;
    pipe.nodes.forEach((n, k) => {
      n.rect.style.fill = k === i ? `color-mix(in srgb, ${STAGES[k].color} 18%, var(--bg-2))` : "var(--bg-3)";
      n.rect.setAttribute("stroke-width", k === i ? 3.5 : 2);
      n.g.style.opacity = k <= i ? 1 : 0.55;
    });
    const n = pipe.nodes[i];
    pipe.fill.setAttribute("width", Math.max(0, n.x + n.W - 20));
    const st = STAGES[i];
    $("#pipe-caption").innerHTML = `<span class="dot" style="background:${st.color}"></span><div><h4>${i + 1} · ${st.name} → <code>${st.out}</code></h4><p>${st.desc}</p></div>`;
    animatePacket(i);
  }
  function animatePacket(i) {
    if (reduced || !pipe.nodes[i + 1]) { pipe.packet.setAttribute("cx", -20); return; }
    const a = pipe.nodes[i], b = pipe.nodes[i + 1];
    const x0 = a.x + a.W, x1 = b.x, t0 = performance.now(), dur = 1400;
    pipe.packet.style.fill = STAGES[i].color;
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      pipe.packet.setAttribute("cx", x0 + (x1 - x0) * e);
      if (p < 1 && pipe.active === i) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function pipeLoop() {
    clearInterval(pipe.timer);
    if (pipe.playing) pipe.timer = setInterval(() => setPipeActive((pipe.active + 1) % STAGES.length), 2800);
  }
  $("#pipe-toggle").addEventListener("click", (e) => {
    pipe.playing = !pipe.playing;
    e.currentTarget.textContent = pipe.playing ? "⏸ Pause" : "▶ Play";
    e.currentTarget.setAttribute("aria-pressed", String(!pipe.playing));
    pipeLoop();
  });
  if (!pipe.playing) $("#pipe-toggle").textContent = "▶ Play";

  // ================================================================== WHY
  async function playWhy() {
    const svg = $("#why-svg");
    svg.innerHTML = "";
    const X0 = 30, X1 = 500, T = 90, sx = (X1 - X0) / T;
    s("text", { x: X0, y: 28, fill: "var(--ink-2)", "font-size": "13px", text: "One video-model generation" }, svg);
    const one = s("rect", { x: X0, y: 40, width: 0, height: 30, rx: 6, fill: "var(--bad)", transition: "width .8s ease" }, svg);
    const oneT = s("text", { x: X0 + 5 * sx + 8, y: 60, fill: "var(--ink-3)", "font-size": "12px", opacity: 0, transition: "opacity .4s", text: "≈ 5 s (81 frames) — then identity drifts" }, svg);
    // axis
    s("line", { x1: X0, y1: 250, x2: X1, y2: 250, stroke: "var(--line)" }, svg);
    for (let t = 0; t <= T; t += 15) {
      s("line", { x1: X0 + t * sx, y1: 246, x2: X0 + t * sx, y2: 254, stroke: "var(--ink-3)" }, svg);
      s("text", { x: X0 + t * sx, y: 270, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: t + "s" }, svg);
    }
    await sleep(200);
    one.setAttribute("width", 5 * sx);
    await sleep(700);
    oneT.style.opacity = 1;
    await sleep(500);
    s("text", { x: X0, y: 112, fill: "var(--ink-2)", "font-size": "13px", text: "afterstage: plan a screenplay, render shot by shot" }, svg);
    const shots = [["bridge", 5], ["broll", 4], ["talk", 7], ["talk", 9], ["broll", 3], ["talk", 6], ["talk", 8], ["broll", 4], ["talk", 10], ["talk", 7], ["broll", 4], ["talk", 8], ["talk", 9], ["broll", 5]];
    const color = { bridge: "var(--s-analyze)", broll: "var(--s-design)", talk: "var(--s-render)" };
    let t = 0;
    for (const [k, d] of shots) {
      const r = s("rect", { x: X0 + t * sx, y: 124, width: Math.max(1, d * sx - 2), height: 30, rx: 5, fill: color[k], opacity: 0, transition: "opacity .3s" }, svg);
      requestAnimationFrame(() => (r.style.opacity = 0.9));
      t += d;
      await sleep(120);
    }
    s("text", { x: X0, y: 180, fill: "var(--ink-3)", "font-size": "12px", text: `${shots.length} shots · ${t} s · every shot conditioned on the same cast references` }, svg);
    const lg = [["bridge", "bridge"], ["broll", "b-roll"], ["talk", "talk (lip-synced)"]];
    lg.forEach(([k, label], i) => {
      s("rect", { x: X0 + i * 140, y: 200, width: 12, height: 12, rx: 3, fill: color[k] }, svg);
      s("text", { x: X0 + i * 140 + 18, y: 211, fill: "var(--ink-3)", "font-size": "12px", text: label }, svg);
    });
  }
  $("#why-replay").addEventListener("click", playWhy);

  // ================================================================== ARCHITECTURE
  const ARCH_FLOWS = {
    analyze: ["e_analysis_disk", "e_vlm"],
    write: ["e_llm", "e_orch_disk"],
    voice: ["e_tts_disk"],
    design: ["e_comfy_req", "e_comfy_disk"],
    render: ["e_comfy_req", "e_comfy_disk", "e_tts_disk"],
    edit: ["e_editor_disk"],
  };
  function drawArch() {
    const svg = $("#arch-svg");
    svg.innerHTML = "";
    const box = (x, y, w, hh, title, sub, color) => {
      s("rect", { x, y, width: w, height: hh, rx: 14, fill: "var(--bg-2)", stroke: color || "var(--line)", "stroke-width": 1.5 }, svg);
      s("text", { x: x + 14, y: y + 26, fill: "var(--ink)", "font-size": "15px", "font-weight": 700, text: title }, svg);
      if (sub) s("text", { x: x + 14, y: y + 44, fill: "var(--ink-3)", "font-size": "12px", text: sub }, svg);
    };
    s("rect", { x: 10, y: 10, width: 1080, height: 450, rx: 18, fill: "var(--bg)", stroke: "var(--ink-3)", "stroke-dasharray": "4 6" }, svg);
    s("text", { x: 26, y: 36, fill: "var(--ink-3)", "font-size": "13px", "font-weight": 700, text: "NVIDIA DGX Spark · GB10 · 128 GB unified memory · aarch64 · CUDA 13" }, svg);
    box(40, 60, 300, 380, "afterstage", "Python process (CLI / web UI)", "var(--accent)");
    const rows = [["CLI · Gradio UI", 110], ["Orchestrator · cache", 170], ["Analysis: scenes · faces · ASR · Demucs", 230], ["TTS: Chatterbox Multilingual", 290], ["Editor: ffmpeg + Pillow", 350]];
    rows.forEach(([t, y]) => {
      s("rect", { x: 56, y, width: 268, height: 46, rx: 9, fill: "var(--bg-3)" }, svg);
      s("text", { x: 70, y: y + 28, fill: "var(--ink-2)", "font-size": "13px", text: t }, svg);
    });
    box(420, 60, 260, 150, "Ollama  :11434", "OpenAI-compatible, loopback", "var(--s-write)");
    s("text", { x: 434, y: 130, fill: "var(--ink-2)", "font-size": "13px", text: "Qwen3-32B — screenplay" }, svg);
    s("text", { x: 434, y: 154, fill: "var(--ink-2)", "font-size": "13px", text: "Qwen3-VL-8B — captions" }, svg);
    s("text", { x: 434, y: 186, fill: "var(--ink-3)", "font-size": "11px", text: "unloaded before diffusion (keep_alive 0)" }, svg);
    box(420, 260, 260, 180, "ComfyUI  :8188", "core nodes · --disable-api-nodes", "var(--s-render)");
    ["Qwen-Image-Edit-2511", "InfiniteTalk · Wan2.1-14B", "Wan2.2-A14B I2V / FLF", "ACE-Step 1.5"].forEach((t, i) =>
      s("text", { x: 434, y: 330 + i * 24, fill: "var(--ink-2)", "font-size": "13px", text: t }, svg));
    box(760, 60, 300, 170, "Disk", null, "var(--s-edit)");
    ["projects/<name>/", "  cast.json · screenplay.json · state.json", "  audio/ keyframes/ shots/ final/", "ComfyUI/models/   ~110 GB weights", "~/.cache/huggingface (offline)"].forEach((t, i) =>
      s("text", { x: 774, y: 112 + i * 22, fill: "var(--ink-2)", "font-size": "11.5px", "font-family": "var(--mono)", text: t }, svg));
    // internet (blocked)
    s("rect", { x: 760, y: 300, width: 300, height: 120, rx: 14, fill: "color-mix(in srgb, var(--bad) 8%, var(--bg-2))", stroke: "var(--bad)", "stroke-dasharray": "5 5" }, svg);
    s("text", { x: 774, y: 330, fill: "var(--bad)", "font-size": "15px", "font-weight": 700, text: "Internet / cloud APIs" }, svg);
    s("text", { x: 774, y: 354, fill: "var(--ink-3)", "font-size": "12px", text: "ensure_local() refuses public endpoints" }, svg);
    s("text", { x: 774, y: 376, fill: "var(--ink-3)", "font-size": "12px", text: "telemetry off · HF_HUB_OFFLINE=1" }, svg);
    s("text", { x: 774, y: 398, fill: "var(--ink-3)", "font-size": "12px", text: "(only setup downloads weights)" }, svg);
    const edges = {
      e_llm: "M324,193 C380,193 380,120 420,120",
      e_vlm: "M324,253 C385,253 385,150 420,150",
      e_comfy_req: "M324,193 C385,193 390,320 420,320",
      e_comfy_disk: "M680,330 C720,330 720,200 760,200",
      e_analysis_disk: "M324,253 C360,253 360,235 400,235 L720,235 C745,235 745,180 760,180",
      e_orch_disk: "M324,193 C360,193 360,235 400,235 L720,235 C745,235 745,150 760,150",
      e_tts_disk: "M324,313 C365,313 365,238 405,238 L720,238 C748,238 748,170 760,170",
      e_editor_disk: "M324,373 C370,373 370,241 410,241 L720,241 C750,241 750,190 760,190",
    };
    for (const [id, d] of Object.entries(edges)) s("path", { id, d, fill: "none", stroke: "var(--line)", "stroke-width": 2 }, svg);
    s("path", { d: "M324,193 C360,193 360,250 400,250 L700,250 C730,250 730,360 760,360", fill: "none", stroke: "var(--bad)", "stroke-width": 1.5, "stroke-dasharray": "3 5", opacity: 0.6 }, svg);
    s("text", { x: 720, y: 302, fill: "var(--bad)", "font-size": "22px", "font-weight": 800, text: "✕" }, svg);
  }
  function highlightArch(stage) {
    const all = new Set(Object.values(ARCH_FLOWS).flat());
    const on = new Set(stage === "all" ? all : ARCH_FLOWS[stage]);
    const color = stage === "all" ? "var(--accent)" : STAGES.find((x) => x.id === stage).color;
    all.forEach((id) => {
      const p = $("#" + id);
      if (!p) return;
      const hot = on.has(id);
      p.classList.toggle("arch-flow", hot);
      p.style.stroke = hot ? color : "var(--line)";
      p.setAttribute("stroke-width", hot ? 3 : 2);
    });
    $$("#arch-buttons button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.stage === stage)));
  }
  [{ id: "all", name: "All flows" }, ...STAGES].forEach((st) => {
    const b = h("button", { class: "btn", "data-stage": st.id, text: st.name }, $("#arch-buttons"));
    b.addEventListener("click", () => highlightArch(st.id));
  });

  // ================================================================== STAGE TABS
  const tabs = $("#stage-tabs");
  const started = {};
  STAGES.forEach((st, i) => {
    const b = h("button", { class: "tab", role: "tab", "aria-selected": "false", "data-stage": st.id, html: `<span class="num" style="background:${st.color}">${i + 1}</span>${st.name}` }, tabs);
    b.addEventListener("click", () => selectStage(st.id));
  });
  function selectStage(id) {
    $$(".tab", tabs).forEach((t) => t.setAttribute("aria-selected", String(t.dataset.stage === id)));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + id));
    if (!started[id]) { started[id] = true; STAGE_VIZ[id] && STAGE_VIZ[id](); }
  }

  // ---------------------------------------------------------------- analyze: clustering
  const cluster = { canvas: $("#cluster-canvas"), points: [], th: 0.45, anim: null };
  const PALETTE = ["#0e9f9a", "#7c4dff", "#e08a00", "#d63d7a", "#2f6fe0", "#2e9e4f", "#8d6e63", "#5c6bc0", "#00897b", "#c0ca33", "#f4511e", "#6d4c41"];
  function genPoints() {
    const r = rng(7);
    const centers = [[110, 90], [280, 70], [440, 120], [190, 230], [400, 250]];
    const counts = [38, 30, 26, 20, 14];
    const pts = [];
    const gauss = () => { let u = 0, v = 0; while (!u) u = r(); while (!v) v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
    centers.forEach(([cx, cy], k) => {
      for (let i = 0; i < counts[k]; i++) pts.push({ x: cx + gauss() * 26, y: cy + gauss() * 22, truth: k, score: 0.6 + r() * 0.4, t: r() });
    });
    for (let i = 0; i < 9; i++) pts.push({ x: 30 + r() * 500, y: 30 + r() * 260, truth: -1, score: 0.3 + r() * 0.3, t: r() }); // crowd faces
    pts.sort((a, b) => a.t - b.t);
    return pts;
  }
  const sim = (a, b) => Math.exp(-((a.x - b.x) ** 2 + (a.y - b.y) ** 2) / (2 * 90 * 90));
  function runClustering(pts, th) {
    const clusters = [];
    const order = [...pts].sort((a, b) => b.score - a.score); // the real code sorts by detector score
    for (const p of order) {
      let best = null, bestSim = th;
      for (const c of clusters) { const v = sim(c, p); if (v > bestSim) { best = c; bestSim = v; } }
      if (!best) clusters.push({ x: p.x, y: p.y, n: 1, members: [p] });
      else { best.members.push(p); best.n++; best.x += (p.x - best.x) / best.n; best.y += (p.y - best.y) / best.n; }
      p.c = clusters.indexOf(best || clusters[clusters.length - 1]);
    }
    return clusters;
  }
  function drawCluster(n) {
    const cv = cluster.canvas, dpr = window.devicePixelRatio || 1, W = 560, H = 320;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = css("--bg");
    ctx.fillRect(0, 0, W, H);
    const shown = cluster.points.slice(0, n);
    const clusters = runClustering(shown, cluster.th);
    clusters.forEach((c, i) => {
      if (c.n < 3) return;
      ctx.beginPath();
      ctx.arc(c.x, c.y, 18 + Math.sqrt(c.n) * 7, 0, Math.PI * 2);
      ctx.strokeStyle = PALETTE[i % PALETTE.length];
      ctx.globalAlpha = 0.35; ctx.lineWidth = 2; ctx.setLineDash([4, 4]); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    });
    shown.forEach((p) => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 4.2, 0, Math.PI * 2);
      ctx.fillStyle = PALETTE[p.c % PALETTE.length];
      ctx.fill();
    });
    const members = clusters.filter((c) => c.n >= 4).length; // ~ min_member_screen_time at 2 fps
    $("#cluster-stats").innerHTML = `<b>${shown.length}</b> detections → <b>${clusters.length}</b> clusters → <b>${members}</b> members kept (≥ 2 s screen time). Ground truth: 5 people + crowd. ` +
      (members > 5 ? "<span style='color:var(--warn)'>Threshold too strict: people are being split.</span>" :
        members < 5 ? "<span style='color:var(--warn)'>Threshold too loose: people are being merged.</span>" :
          "<span style='color:var(--ok)'>Looks right.</span>");
  }
  function playCluster() {
    cancelAnimationFrame(cluster.anim);
    if (!cluster.points.length) cluster.points = genPoints();
    if (reduced) return drawCluster(cluster.points.length);
    let n = 0;
    const tick = () => { n = Math.min(cluster.points.length, n + 2); drawCluster(n); if (n < cluster.points.length) cluster.anim = requestAnimationFrame(tick); };
    tick();
  }
  $("#cluster-th").addEventListener("input", (e) => {
    cluster.th = +e.target.value;
    $("#cluster-th-v").textContent = cluster.th.toFixed(2);
    cancelAnimationFrame(cluster.anim);
    drawCluster(cluster.points.length);
  });
  $("#cluster-play").addEventListener("click", playCluster);

  // ---------------------------------------------------------------- write: repair loop
  async function playRepair() {
    const svg = $("#repair-svg");
    svg.innerHTML = "";
    const node = (x, y, w, t, sub, color) => {
      s("rect", { x, y, width: w, height: 54, rx: 10, fill: "var(--bg-3)", stroke: color, "stroke-width": 2 }, svg);
      s("text", { x: x + w / 2, y: y + 24, "text-anchor": "middle", fill: "var(--ink)", "font-size": "13px", "font-weight": 700, text: t }, svg);
      s("text", { x: x + w / 2, y: y + 42, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: sub }, svg);
    };
    node(10, 20, 150, "Qwen3-32B", "json_schema output", "var(--s-write)");
    node(205, 20, 150, "parse + pydantic", "Screenplay model", "var(--ink-3)");
    node(400, 20, 150, "validate_against", "cast ids · sets · rules", "var(--ink-3)");
    const msg = async (x, y, lines, color, delay = 600) => {
      const g = s("g", { opacity: 0, transition: "opacity .45s" }, svg);
      s("rect", { x, y, width: 268, height: 18 + lines.length * 16, rx: 8, fill: "var(--bg-2)", stroke: color }, g);
      lines.forEach((l, i) => s("text", { x: x + 10, y: y + 18 + i * 16, fill: "var(--ink-2)", "font-size": "11.5px", "font-family": "var(--mono)", text: l }, g));
      requestAnimationFrame(() => (g.style.opacity = 1));
      await sleep(delay);
      return g;
    };
    const arrow = (x1, y1, x2, y2, color) => s("path", { d: `M${x1},${y1} L${x2},${y2}`, stroke: color, "stroke-width": 2, fill: "none", class: "arch-flow" }, svg);
    arrow(160, 47, 205, 47, "var(--s-write)"); arrow(355, 47, 400, 47, "var(--s-write)");
    await msg(10, 90, ['attempt 1:', '{"shots":[{"id":"s3","kind":"talk",', '  "speaker":"m7", "line":"..."}]}'], "var(--ink-3)", 900);
    await msg(286, 90, ["✗ s3: unknown character id 'm7'", "  use one of ['m1','m2','m3','m4']"], "var(--bad)", 900);
    arrow(420, 170, 150, 170, "var(--bad)");
    s("text", { x: 285, y: 188, "text-anchor": "middle", fill: "var(--bad)", "font-size": "11px", text: "errors appended as a new user turn" }, svg);
    await sleep(700);
    await msg(10, 205, ['attempt 2:', '{"shots":[{"id":"s3","kind":"talk",', '  "speaker":"m1", "line":"..."}]}'], "var(--ink-3)", 900);
    await msg(286, 205, ["✓ schema ok   ✓ semantics ok", "→ normalise() → screenplay.json"], "var(--ok)", 400);
    s("text", { x: 280, y: 318, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: "max writer.repair_attempts (3) rounds, then a clear error" }, svg);
  }
  $("#repair-play").addEventListener("click", playRepair);

  // ---------------------------------------------------------------- voice: waveforms
  const EMO = { neutral: 0.5, warm: 0.55, calm: 0.4, excited: 0.8, happy: 0.7, playful: 0.7, emotional: 0.75, sad: 0.6, surprised: 0.8, serious: 0.4, shy: 0.45 };
  const emoBody = $("#emo-table");
  Object.entries(EMO).forEach(([k, v]) => h("tr", { html: `<td>${k}</td><td><div style="display:flex;gap:8px;align-items:center"><div style="height:8px;width:${v * 120}px;background:var(--s-voice);border-radius:4px"></div>${v}</div></td>` }, emoBody));
  const wave = { anim: null };
  function playWave() {
    cancelAnimationFrame(wave.anim);
    const cv = $("#wave-canvas"), dpr = window.devicePixelRatio || 1, W = 560, H = 300;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
    const lines = [
      { who: "m1 · Ari", emo: "excited", text: "안녕하세요 여러분! 방금 무대 끝나고 바로 왔어요.", sec: 4.1 },
      { who: "m2 · Bora", emo: "warm", text: "오늘 팬분들 함성 진짜 대박이었어요.", sec: 3.2 },
      { who: "g1 · MC", emo: "playful", text: "So… who missed the cue in the bridge?", sec: 2.6 },
    ];
    let t = 0;
    const total = reduced ? 1 : 120;
    const draw = () => {
      t++;
      ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, W, H);
      lines.forEach((ln, i) => {
        const y0 = 20 + i * 92;
        ctx.fillStyle = css("--ink"); ctx.font = "600 13px " + css("--sans");
        ctx.fillText(`${ln.who}  ·  ${ln.emo} (${EMO[ln.emo]})`, 10, y0 + 12);
        ctx.fillStyle = css("--ink-3"); ctx.font = "12px " + css("--sans");
        ctx.fillText(ln.text, 10, y0 + 30);
        const maxW = 520, w = (ln.sec / 4.5) * maxW, padW = (0.25 / 4.5) * maxW;
        const prog = Math.min(1, (t / total) * (1 + i * 0.25));
        const r = rng(i + 3), mid = y0 + 60;
        for (let x = 0; x < w * prog; x += 3) {
          const env = Math.abs(Math.sin((x / w) * Math.PI * (5 + i * 2))) * 0.8 + 0.2;
          const a = (6 + r() * 16) * env * (0.6 + EMO[ln.emo] * 0.6);
          ctx.fillStyle = css("--s-voice"); ctx.fillRect(10 + x, mid - a, 2, a * 2);
        }
        if (prog >= 1) {
          ctx.fillStyle = css("--bg-3"); ctx.fillRect(10 + w, mid - 3, padW, 6);
          ctx.fillStyle = css("--ink-3"); ctx.font = "11px " + css("--mono");
          ctx.fillText(`${(ln.sec + 0.25).toFixed(2)} s (+0.25 pad) → audio/s0${i + 3}.wav`, 10 + Math.min(w + padW + 8, 300), mid + 26);
        }
      });
      if (t < total * 1.6) wave.anim = requestAnimationFrame(draw);
    };
    draw();
  }
  $("#wave-play").addEventListener("click", playWave);

  // ---------------------------------------------------------------- design: composition + identity gate
  async function playDesign() {
    const svg = $("#design-svg");
    svg.innerHTML = "";
    const card = (x, y, label, kind, color) => {
      const g = s("g", { opacity: 0, transition: "opacity .4s" }, svg);
      s("rect", { x, y, width: 110, height: 72, rx: 8, fill: "var(--bg-3)", stroke: color, "stroke-width": 1.5 }, g);
      if (kind === "set") {
        s("rect", { x: x + 10, y: y + 40, width: 90, height: 22, rx: 3, fill: "var(--line)" }, g);
        s("circle", { cx: x + 30, cy: y + 22, r: 7, fill: "var(--s-voice)", opacity: 0.7 }, g);
        s("rect", { x: x + 70, y: y + 14, width: 26, height: 20, rx: 3, fill: "var(--s-design)", opacity: 0.6 }, g);
      } else {
        s("circle", { cx: x + 55, cy: y + 26, r: 13, fill: color, opacity: 0.85 }, g);
        s("rect", { x: x + 33, y: y + 42, width: 44, height: 24, rx: 10, fill: color, opacity: 0.6 }, g);
      }
      s("text", { x: x + 55, y: y + 90, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: label }, g);
      requestAnimationFrame(() => (g.style.opacity = 1));
      return g;
    };
    card(10, 10, "image 1 · set plate", "set", "var(--ink-3)"); await sleep(300);
    card(10, 115, "image 2 · Ari (speaker)", "p", "var(--s-analyze)"); await sleep(300);
    card(10, 220, "image 3 · Bora", "p", "var(--s-write)"); await sleep(300);
    [45, 150, 255].forEach((y) => s("path", { d: `M122,${y} C170,${y} 170,160 200,160`, stroke: "var(--ink-3)", fill: "none", "stroke-width": 1.5, class: "arch-flow" }, svg));
    s("rect", { x: 200, y: 128, width: 130, height: 64, rx: 10, fill: "var(--bg-2)", stroke: "var(--s-design)", "stroke-width": 2 }, svg);
    s("text", { x: 265, y: 156, "text-anchor": "middle", fill: "var(--ink)", "font-size": "12.5px", "font-weight": 700, text: "Qwen-Image-Edit" }, svg);
    s("text", { x: 265, y: 174, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: "2511 · 4-step LoRA" }, svg);
    s("path", { d: "M330,160 L370,160", stroke: "var(--s-design)", "stroke-width": 2, class: "arch-flow" }, svg);
    const kf = s("g", {}, svg);
    s("rect", { x: 372, y: 110, width: 176, height: 100, rx: 8, fill: "var(--bg-3)", stroke: "var(--s-design)" }, kf);
    s("rect", { x: 382, y: 170, width: 156, height: 30, rx: 3, fill: "var(--line)" }, kf);
    s("circle", { cx: 430, cy: 145, r: 14, fill: "var(--s-analyze)" }, kf);
    const second = s("circle", { cx: 495, cy: 148, r: 13, fill: "var(--s-write)" }, kf);
    s("text", { x: 460, y: 228, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "11px", text: "keyframes/s04.png" }, svg);
    // gauge
    const gx = 372, gy = 250, gw = 176;
    s("rect", { x: gx, y: gy, width: gw, height: 12, rx: 6, fill: "var(--bg-3)" }, svg);
    s("line", { x1: gx + gw * 0.35, y1: gy - 6, x2: gx + gw * 0.35, y2: gy + 18, stroke: "var(--ink-2)", "stroke-dasharray": "2 2" }, svg);
    s("text", { x: gx + gw * 0.35, y: gy + 32, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "10px", text: "threshold 0.35" }, svg);
    const bar = s("rect", { x: gx, y: gy, width: 0, height: 12, rx: 6, fill: "var(--bad)", transition: "width .7s ease, fill .3s" }, svg);
    const verdict = s("text", { x: gx, y: gy - 10, fill: "var(--bad)", "font-size": "12px", "font-weight": 700, text: "" }, svg);
    await sleep(500);
    bar.setAttribute("width", gw * 0.28); await sleep(800);
    verdict.textContent = "ArcFace min-sim 0.28 ✗ → re-roll (seed+7919)";
    second.style.fill = "var(--warn)";
    await sleep(1200);
    second.style.fill = "var(--s-write)";
    bar.style.fill = "var(--ok)"; bar.setAttribute("width", gw * 0.47);
    verdict.style.fill = "var(--ok)"; verdict.textContent = "try 1: 0.47 ✓ keep";
  }
  $("#design-play").addEventListener("click", playDesign);

  // ---------------------------------------------------------------- render: InfiniteTalk windows
  let K = { talk_fps: 25, talk_chunk: 81, talk_motion: 9, broll_fps: 16 };
  function drawTalk() {
    const sec = +$("#talk-sec").value;
    $("#talk-sec-v").textContent = sec.toFixed(1) + " s";
    const total = Math.ceil(sec * K.talk_fps) + 1;
    const C = K.talk_chunk, M = K.talk_motion;
    const n = total <= C ? 1 : 1 + Math.ceil((total - C) / (C - M));
    const produced = C + (n - 1) * (C - M);
    const svg = $("#talk-svg");
    svg.innerHTML = "";
    const X0 = 70, Wd = 960, maxF = Math.max(produced, 160), sx = Wd / maxF;
    const rowH = Math.min(26, 120 / n);
    s("text", { x: 4, y: 18, fill: "var(--ink-3)", "font-size": "12px", text: "windows" }, svg);
    let acc = 0;
    for (let k = 0; k < n; k++) {
      const start = k === 0 ? 0 : acc - M;
      const y = 8 + k * (rowH + 4);
      const g = s("g", { opacity: 0, transition: `opacity .35s ease ${reduced ? 0 : k * 0.12}s` }, svg);
      if (k > 0) s("rect", { x: X0 + start * sx, y, width: M * sx, height: rowH, rx: 3, fill: "var(--warn)" }, g);
      s("rect", { x: X0 + (start + (k ? M : 0)) * sx, y, width: (C - (k ? M : 0)) * sx - 1, height: rowH, rx: 3, fill: "var(--s-render)", opacity: 0.85 }, g);
      if (rowH >= 14) s("text", { x: X0 + start * sx + 6, y: y + rowH / 2 + 4, fill: "#fff", "font-size": "11px", "font-weight": 600, text: `#${k + 1}  frames ${start}–${start + C - 1}` }, g);
      requestAnimationFrame(() => (g.style.opacity = 1));
      acc = k === 0 ? C : acc + (C - M);
    }
    const ay = 160;
    s("text", { x: 4, y: ay + 13, fill: "var(--ink-3)", "font-size": "12px", text: "audio" }, svg);
    s("rect", { x: X0, y: ay, width: total * sx, height: 16, rx: 3, fill: "var(--ink-3)", opacity: 0.55 }, svg);
    if (produced > total) {
      s("rect", { x: X0 + total * sx, y: ay, width: (produced - total) * sx, height: 16, rx: 3, fill: "none", stroke: "var(--ink-3)", "stroke-dasharray": "3 3" }, svg);
      s("text", { x: X0 + produced * sx, y: ay + 30, "text-anchor": "end", fill: "var(--ink-3)", "font-size": "10.5px", text: "padded by the node, trimmed by -shortest" }, svg);
    }
    for (let f = 0; f <= maxF; f += 50) {
      s("text", { x: X0 + f * sx, y: 205, "text-anchor": "middle", fill: "var(--ink-3)", "font-size": "10px", text: f }, svg);
    }
    $("#talk-summary").innerHTML = `→ <b>${n}</b> window${n > 1 ? "s" : ""} · ${total} frames needed · ${produced} rendered · ${n > 1 ? (n - 1) * M + " context frames trimmed" : "no chaining"}`;
  }
  $("#talk-sec").addEventListener("input", drawTalk);

  // ---------------------------------------------------------------- render: Wan2.2 experts
  let expertMode = "draft";
  function drawExperts() {
    const svg = $("#expert-svg");
    svg.innerHTML = "";
    const steps = expertMode === "draft" ? 4 : 20, split = steps / 2;
    const X0 = 20, Wd = 1020, bw = Wd / steps;
    for (let i = 0; i < steps; i++) {
      const hi = i < split;
      const r = s("rect", { x: X0 + i * bw + 2, y: 30, width: bw - 4, height: 44, rx: 6, fill: hi ? "var(--s-write)" : "var(--s-analyze)", opacity: 0.25, transition: `opacity .3s ease ${reduced ? 0 : i * (1.2 / steps)}s` }, svg);
      requestAnimationFrame(() => (r.style.opacity = 0.9));
      if (steps <= 4 || i % 2 === 0) s("text", { x: X0 + i * bw + bw / 2, y: 57, "text-anchor": "middle", fill: "#fff", "font-size": "12px", "font-weight": 700, text: i + 1 }, svg);
    }
    s("text", { x: X0, y: 20, fill: "var(--s-write)", "font-size": "12.5px", "font-weight": 700, text: "high-noise expert — layout & motion (add_noise, leftover noise returned)" }, svg);
    s("text", { x: X0 + Wd, y: 20, "text-anchor": "end", fill: "var(--s-analyze)", "font-size": "12.5px", "font-weight": 700, text: "low-noise expert — detail" }, svg);
    let d = `M${X0},90`;
    for (let i = 0; i <= 40; i++) { const t = i / 40; d += ` L${X0 + t * Wd},${90 + 40 * (1 - Math.pow(1 - t, 0.7))}`; }
    s("path", { d, fill: "none", stroke: "var(--ink-3)", "stroke-width": 1.5 }, svg);
    s("text", { x: X0 + 6, y: 142, fill: "var(--ink-3)", "font-size": "11px", text: expertMode === "draft" ? "noise level ↓   ·  CFG 1.0 + lightx2v 4-step LoRAs  ·  81 frames @16 fps" : "noise level ↓   ·  CFG 3.5, no LoRA  ·  81 frames @16 fps" }, svg);
  }
  $("#expert-draft").addEventListener("click", () => { expertMode = "draft"; $("#expert-draft").setAttribute("aria-pressed", "true"); $("#expert-final").setAttribute("aria-pressed", "false"); drawExperts(); });
  $("#expert-final").addEventListener("click", () => { expertMode = "final"; $("#expert-final").setAttribute("aria-pressed", "true"); $("#expert-draft").setAttribute("aria-pressed", "false"); drawExperts(); });

  // ---------------------------------------------------------------- edit: timeline
  const TL = [["source", 6, "source"], ["s01", 5, "bridge"], ["s02", 4, "broll"], ["s03", 4.4, "talk"], ["s04", 5.4, "talk"], ["s05", 4, "talk"], ["s06", 4.6, "talk"], ["s07", 4, "broll"]];
  const KC = { source: "var(--ink-3)", bridge: "var(--s-analyze)", broll: "var(--s-design)", talk: "var(--s-render)" };
  let tlAnim = null;
  function drawTimeline(play) {
    cancelAnimationFrame(tlAnim);
    const svg = $("#timeline-svg");
    svg.innerHTML = "";
    const total = TL.reduce((a, x) => a + x[1], 0), X0 = 78, Wd = 470, sx = Wd / total;
    const tracks = ["video", "dialogue", "music", "subtitles", "AI badge"];
    tracks.forEach((t, i) => s("text", { x: 4, y: 40 + i * 52, fill: "var(--ink-3)", "font-size": "12px", text: t }, svg));
    let t = 0;
    const dlg = [];
    TL.forEach(([id, d, k]) => {
      s("rect", { x: X0 + t * sx, y: 22, width: d * sx - 1.5, height: 30, rx: 4, fill: KC[k], opacity: 0.85 }, svg);
      s("text", { x: X0 + t * sx + 3, y: 41, fill: "#fff", "font-size": "10px", text: id === "source" ? "source" : id }, svg);
      if (k === "talk") {
        dlg.push([t + 0.05, t + d - 0.3]);
        s("rect", { x: X0 + (t + 0.05) * sx, y: 76, width: (d - 0.35) * sx, height: 24, rx: 4, fill: "var(--s-voice)", opacity: 0.8 }, svg);
        s("rect", { x: X0 + (t + 0.05) * sx, y: 180, width: (d - 0.15) * sx, height: 20, rx: 4, fill: "var(--ink-2)", opacity: 0.6 }, svg);
      }
      t += d;
    });
    const src = TL[0][1];
    s("rect", { x: X0 + src * sx, y: 230, width: (total - src) * sx, height: 18, rx: 4, fill: "var(--bad)", opacity: 0.55 }, svg);
    // music envelope with ducking
    let d = `M${X0 + src * sx},150`;
    for (let x = src; x <= total; x += 0.1) {
      const fadeIn = Math.min(1, (x - src) / 1.5), fadeOut = Math.min(1, (total - x) / 2.5);
      const ducked = dlg.some(([a, b]) => x >= a && x <= b);
      const lvl = 0.18 * fadeIn * fadeOut * (ducked ? 0.35 : 1);
      d += ` L${X0 + x * sx},${150 - lvl * 150}`;
    }
    s("path", { d, fill: "none", stroke: "var(--s-edit)", "stroke-width": 2 }, svg);
    s("text", { x: X0 + src * sx + 4, y: 162, fill: "var(--ink-3)", "font-size": "10px", text: "fade in · ducked under dialogue (sidechaincompress) · fade out" }, svg);
    const head = s("line", { x1: X0, y1: 14, x2: X0, y2: 256, stroke: "var(--accent)", "stroke-width": 2 }, svg);
    const label = s("text", { x: X0, y: 280, fill: "var(--accent)", "font-size": "11px", "text-anchor": "middle", text: "0.0 s" }, svg);
    s("text", { x: X0 + Wd, y: 296, "text-anchor": "end", fill: "var(--ink-3)", "font-size": "11px", text: `${total.toFixed(1)} s (dry-run layout)` }, svg);
    if (!play || reduced) return;
    const t0 = performance.now(), dur = 9000;
    const step = (now) => {
      const p = ((now - t0) % dur) / dur, x = X0 + p * Wd;
      head.setAttribute("x1", x); head.setAttribute("x2", x);
      label.setAttribute("x", x); label.textContent = (p * total).toFixed(1) + " s";
      if (now - t0 < dur * 2) tlAnim = requestAnimationFrame(step);
    };
    tlAnim = requestAnimationFrame(step);
  }
  $("#timeline-play").addEventListener("click", () => drawTimeline(true));

  const STAGE_VIZ = {
    analyze: playCluster,
    write: playRepair,
    voice: playWave,
    design: playDesign,
    render: () => { drawTalk(); drawExperts(); },
    edit: () => drawTimeline(true),
  };

  // ================================================================== GRAPH VIEWER
  const CAT = [
    [/Loader|PatchLoader/, "loader", "var(--s-write)"],
    [/TextEncode|Conditioning|ReferenceLatent|AudioEncoderEncode/, "conditioning", "var(--s-voice)"],
    [/Sampler|Scheduler|Noise|Guider|ModelSampling|CFGNorm/, "sampling", "var(--s-render)"],
    [/Wan|InfiniteTalk|EmptySD3|EmptyAce|ImageScale/, "model op", "var(--s-design)"],
    [/Decode|Batch|FromBatch/, "decode / frames", "var(--s-analyze)"],
    [/Load|Save/, "I/O", "var(--s-edit)"],
  ];
  const catOf = (ct) => CAT.find(([re]) => re.test(ct)) || [null, "other", "var(--ink-3)"];
  function drawGraph(key, data) {
    const { title, prompt } = data.graphs[key];
    $("#graph-title").textContent = title + ` — ${Object.keys(prompt).length} nodes`;
    $$("#graph-buttons button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.key === key)));
    const ids = Object.keys(prompt);
    const deps = {};
    ids.forEach((id) => { deps[id] = Object.values(prompt[id].inputs).filter((v) => Array.isArray(v) && v.length === 2 && typeof v[0] === "string").map((v) => v[0]); });
    const depth = {};
    const dfs = (id) => { if (depth[id] !== undefined) return depth[id]; depth[id] = 0; depth[id] = deps[id].length ? 1 + Math.max(...deps[id].map(dfs)) : 0; return depth[id]; };
    ids.forEach(dfs);
    const cols = {};
    ids.forEach((id) => (cols[depth[id]] = cols[depth[id]] || []).push(id));
    const NW = 176, NH = 40, CX = 214, RY = 54;
    const pos = {};
    let maxRows = 0;
    Object.entries(cols).forEach(([d, list]) => { maxRows = Math.max(maxRows, list.length); list.forEach((id, i) => (pos[id] = { x: 16 + d * CX, y: 16 + i * RY })); });
    const W = 32 + (Math.max(...Object.keys(cols).map(Number)) + 1) * CX, H = 32 + maxRows * RY;
    const svg = $("#graph-svg");
    svg.innerHTML = "";
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    const edges = [];
    ids.forEach((id) => {
      Object.entries(prompt[id].inputs).forEach(([name, v]) => {
        if (!(Array.isArray(v) && v.length === 2 && typeof v[0] === "string")) return;
        const a = pos[v[0]], b = pos[id];
        const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
        const p = s("path", { class: "gedge", d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, stroke: catOf(prompt[v[0]].class_type)[2] }, svg);
        edges.push({ p, from: v[0], to: id });
      });
    });
    ids.forEach((id) => {
      const n = prompt[id], [, , color] = catOf(n.class_type);
      const g = s("g", { class: "gnode", transform: `translate(${pos[id].x},${pos[id].y})` }, svg);
      const isOut = n._meta && n._meta.title === "OUTPUT";
      s("rect", { width: NW, height: NH, rx: 8, fill: `color-mix(in srgb, ${color} 16%, var(--bg-2))`, stroke: color, "stroke-width": isOut ? 3 : 1.2 }, g);
      const label = n.class_type.length > 26 ? n.class_type.slice(0, 25) + "…" : n.class_type;
      s("text", { x: 9, y: 17, fill: "var(--ink)", "font-weight": 600, text: label }, g);
      const hint = Object.entries(n.inputs).find(([k, v]) => typeof v === "string" && /safetensors|png|wav/.test(v));
      s("text", { x: 9, y: 32, fill: "var(--ink-3)", text: `#${id}${isOut ? " · OUTPUT" : ""}${hint ? " · " + String(hint[1]).slice(0, 22) : ""}` }, g);
      g.addEventListener("click", () => {
        edges.forEach((e) => e.p.classList.toggle("hot", e.from === id || e.to === id));
        $("#insp-title").textContent = `#${id} ${n.class_type}`;
        $("#insp-body").textContent = Object.entries(n.inputs).map(([k, v]) =>
          Array.isArray(v) && v.length === 2 && typeof v[0] === "string" ? `${k}: → #${v[0]}.${v[1]} (${prompt[v[0]].class_type})` : `${k}: ${JSON.stringify(v)}`).join("\n");
      });
    });
  }

  // ================================================================== CACHE DEMO
  const SHOTS = [["s01", "bridge"], ["s02", "broll"], ["s03", "talk", "m1"], ["s04", "talk", "m2"], ["s05", "talk", "m1"], ["s06", "talk", "m2"], ["s07", "broll"]];
  const ROWS = ["voice", "keyframe", "shot", "conform"];
  const SCEN = {
    first: ["First run", "Nothing cached: every artifact is produced once.", () => ({ all: true })],
    again: ["Run again", "Identical fingerprints everywhere → zero work; only the final mux re-runs (it's cheap).", () => ({})],
    line: ["Edit line in s04", "The line is part of s04's voice fingerprint → new wav → its size/mtime change s04's video fingerprint → re-conform. The keyframe doesn't depend on the line, so it's kept.", () => ({ voice: ["s04"], shot: ["s04"], conform: ["s04"], music: true })],
    vsample: ["New voice sample for m2", "Every line spoken by m2 (s04, s06) re-voices; their videos follow because the audio file changed. m1's shots are untouched.", () => ({ voice: ["s04", "s06"], shot: ["s04", "s06"], conform: ["s04", "s06"], music: true })],
    reroll: ["Re-roll keyframe s05", "`afterstage design --shot s05` forces that key; the new still changes s05's video fingerprint.", () => ({ keyframe: ["s05"], shot: ["s05"], conform: ["s05"] })],
    set: ["Change the set description", "The set plate changes → every keyframe that used it as image 1 → every shot. Voices are kept: they don't depend on visuals.", () => ({ set: true, keyframe: SHOTS.map((x) => x[0]), shot: SHOTS.map((x) => x[0]), conform: SHOTS.map((x) => x[0]), music: true })],
    profile: ["Switch to final profile", "Profile params are in every visual fingerprint (and the music's), not in the TTS one → all pictures and video re-render at 720p, voices are reused.", () => ({ set: true, keyframe: SHOTS.map((x) => x[0]), shot: SHOTS.map((x) => x[0]), conform: SHOTS.map((x) => x[0]), music: true })],
  };
  function drawCache(key) {
    const grid = $("#cache-grid");
    grid.innerHTML = "";
    const res = SCEN[key][2]();
    h("div", { class: "h", text: "" }, grid);
    SHOTS.forEach(([id, kind]) => h("div", { class: "h", html: `${id}<br><span class="muted">${kind}</span>` }, grid));
    ROWS.forEach((row) => {
      h("div", { class: "rowlabel", text: row + "/" }, grid);
      SHOTS.forEach(([id, kind]) => {
        const na = row === "voice" && kind !== "talk";
        const redo = !na && (res.all || (res[row] || []).includes(id));
        h("div", { class: `cell ${na ? "na" : redo ? "redo" : "cached"}`, text: na ? "—" : redo ? "↻" : "✓" }, grid);
      });
    });
    h("div", { class: "rowlabel", text: "global" }, grid);
    [["set/studio", res.all || res.set], ["music/bed", res.all || res.music], ["final cut", true]].forEach(([label, redo]) =>
      h("div", { class: `cell ${redo ? "redo" : "cached"}`, style: "grid-column: span 2; font-family: var(--mono)", text: `${redo ? "↻" : "✓"} ${label}` }, grid));
    $("#cache-note").textContent = SCEN[key][1];
    $$("#cache-buttons button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.key === key)));
  }
  Object.entries(SCEN).forEach(([k, [label]]) => {
    const b = h("button", { class: "btn", "data-key": k, text: label }, $("#cache-buttons"));
    b.addEventListener("click", () => drawCache(k));
  });

  // ================================================================== MEMORY
  function drawMemory(models, mode) {
    const gb = (keys) => keys.reduce((a, k) => a + (models[k] ? models[k].gb : 0), 0);
    const rows = [
      ["Analyze", [["Qwen3-VL 8B", 6.1, "var(--s-write)"], ["Whisper v3-turbo", 1.6, "var(--s-analyze)"], ["Demucs + InsightFace", 0.6, "var(--s-analyze)"]]],
      ["Write", [["Qwen3-32B (Q4)", 20, "var(--s-write)"]]],
      ["Voice", [["Chatterbox", 3.2, "var(--s-voice)"]]],
      ["Design", [["Qwen-Image-Edit-2511", gb(["keyframe_unet", "keyframe_lora", "keyframe_vae"]), "var(--s-design)"], ["Qwen2.5-VL encoder", gb(["keyframe_clip"]), "var(--s-design)"]]],
      ["Render · talk", [["Wan2.1-14B + InfiniteTalk", gb(["talk_unet", "talk_patch", "talk_lora", "talk_vae", "talk_audio_encoder"]), "var(--s-render)"], ["UMT5-XXL", gb(["wan_clip"]), "var(--s-render)"]]],
      ["Render · b-roll", [["Wan2.2 high+low experts", gb(["broll_unet_high", "broll_unet_low", "broll_lora_high", "broll_lora_low", "wan_vae"]), "var(--s-render)"], ["UMT5-XXL", gb(["wan_clip"]), "var(--s-render)"]]],
      ["Music", [["ACE-Step 1.5", gb(["music_unet", "music_vae", "music_clip1", "music_clip2"]), "var(--s-edit)"]]],
    ];
    const cap = 128;
    const viz = $("#mem-viz");
    viz.innerHTML = "";
    const scale = mode === "all" ? 160 : 140;
    const list = mode === "all"
      ? [["Everything at once", rows.flatMap(([, segs]) => segs).filter((x, i, a) => a.findIndex((y) => y[0] === x[0]) === i)]]
      : rows;
    list.forEach(([label, segs]) => {
      const row = h("div", { class: "mem-row" }, viz);
      h("div", { text: label }, row);
      const track = h("div", { class: "mem-track" }, row);
      let left = 0;
      const total = segs.reduce((a, x) => a + x[1], 0);
      segs.forEach(([name, v, color], i) => {
        const seg = h("div", { class: "mem-seg", title: `${name}: ${v.toFixed(1)} GB`, style: `left:0;width:0;background:${color};opacity:${0.95 - i * 0.18}` }, track);
        const l = (left / scale) * 100, w = (v / scale) * 100;
        requestAnimationFrame(() => requestAnimationFrame(() => { seg.style.left = l + "%"; seg.style.width = w + "%"; }));
        left += v;
      });
      h("div", { class: "mem-cap", style: `left:${(cap / scale) * 100}%`, title: "128 GB" }, track);
      h("div", { class: "muted", style: `text-align:right;color:${total > cap * 0.8 ? "var(--bad)" : "var(--ink-3)"}`, text: total.toFixed(0) + " GB" }, row);
    });
    h("p", { class: "muted", style: "margin-top:12px", html: mode === "all"
      ? "Weights alone approach the 128 GB line (red) before activations, latents and the OS — which is why stages unload what they're done with."
      : "Weights only (activations/latents add several GB per stage). Each stage fits with ample headroom; ComfyUI <code>--highvram</code> keeps the current stage's models resident between shots." }, viz);
  }

  // ================================================================== DATA-DRIVEN BITS
  function redrawCanvases() {
    if (started.analyze) drawCluster(cluster.points.length);
    if (started.voice) playWave();
  }

  async function boot() {
    drawPipeline(); pipeLoop();
    drawArch(); highlightArch("analyze");
    playWhy();
    selectStage("analyze");
    drawCache("line");
    let data = null;
    try {
      const r = await fetch("data/project.json");
      data = await r.json();
    } catch (e) {
      console.warn("data/project.json unavailable (open via the serve script)", e);
    }
    if (!data) {
      $("#graph-title").textContent = "Graph data unavailable — serve the site with script/serve.sh instead of opening the file directly.";
      return;
    }
    K = data.constants;
    if (started.render) drawTalk();
    $("#stat-loc").textContent = data.loc.reduce((a, x) => a + x.lines, 0).toLocaleString();
    $("#stat-tests").textContent = data.tests.length;
    $("#stat-gb").textContent = Math.round(Object.values(data.models).reduce((a, m) => a + m.gb, 0));
    // graphs
    Object.keys(data.graphs).forEach((k) => {
      const b = h("button", { class: "btn", "data-key": k, text: k }, $("#graph-buttons"));
      b.addEventListener("click", () => drawGraph(k, data));
    });
    CAT.forEach(([, name, color]) => h("span", { html: `<i style="background:${color}"></i>${name}` }, $("#graph-legend")));
    h("span", { html: `<i style="background:transparent;border:2px solid var(--ink-2)"></i>thick border = OUTPUT node` }, $("#graph-legend"));
    drawGraph("talk", data);
    // memory
    let memMode = "seq";
    drawMemory(data.models, memMode);
    $("#mem-seq").addEventListener("click", () => { memMode = "seq"; $("#mem-seq").setAttribute("aria-pressed", "true"); $("#mem-all").setAttribute("aria-pressed", "false"); drawMemory(data.models, memMode); });
    $("#mem-all").addEventListener("click", () => { memMode = "all"; $("#mem-all").setAttribute("aria-pressed", "true"); $("#mem-seq").setAttribute("aria-pressed", "false"); drawMemory(data.models, memMode); });
    // tests
    const byFile = {};
    data.tests.forEach((t) => { const [f, name] = t.split("::"); (byFile[f] = byFile[f] || []).push(name); });
    Object.entries(byFile).forEach(([f, names]) => h("li", { html: `<code>${f}</code> — ${names.map((n) => n.replace(/^test_/, "").replace(/_/g, " ")).join(" · ")}` }, $("#test-list")));
    // loc
    const max = Math.max(...data.loc.map((x) => x.lines));
    data.loc.sort((a, b) => b.lines - a.lines).forEach((x, i) => {
      const row = h("div", { class: "loc-row" }, $("#loc-viz"));
      h("span", { text: x.file.replace("afterstage/", "") }, row);
      h("div", { class: "loc-bar", style: `width:${(x.lines / max) * 100}%;animation-delay:${i * 40}ms` }, row);
      h("span", { class: "muted", text: x.lines }, row);
    });
  }
  boot();
})();
