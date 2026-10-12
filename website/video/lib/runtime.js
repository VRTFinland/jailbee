// Scene runtime for website/video/render.py.
//
// render.py opens a scene as `scene.html?duration=…&speech=…&clip=…&format=…`
// and steps it frame by frame: `JB.seek(t)` puts every animation on the page
// at scene time `t` (seconds), then the page is screenshotted. Nothing runs on
// wall-clock time, so a frame is the same however slow the machine is.
//
// A scene registers its animations with `JB.scene(fn)`; `fn` gets the runtime
// and builds them with `JB.anim`. Times are seconds on the scene clock;
// `JB.at(x)` maps a fraction of the narration (0 = first word, 1 = last) to
// one, so animations follow the voice when the text is edited.
(() => {
  const q = new URLSearchParams(location.search);
  const num = (name, fallback) => {
    const value = Number(q.get(name));
    return Number.isFinite(value) && q.has(name) ? value : fallback;
  };
  const builders = [];
  const JB = {
    duration: num("duration", 6),
    speech: num("speech", 0.6),
    clip: num("clip", 4.8),
    format: q.get("format") || "main",
    params: JSON.parse(q.get("params") || "{}"),
    isReady: false,
    scene(fn) {
      builders.push(fn);
    },
    at(fraction) {
      return JB.speech + JB.clip * fraction;
    },
    // Animate `el` through `keyframes` between `from` and `to` (scene seconds).
    anim(el, keyframes, from, to, easing = "cubic-bezier(.2,.7,.2,1)") {
      const targets = typeof el === "string" ? document.querySelectorAll(el) : [el];
      for (const target of targets) {
        const animation = target.animate(keyframes, {
          delay: from * 1000,
          duration: Math.max(1, (to - from) * 1000),
          fill: "both",
          easing,
        });
        animation.pause();
      }
    },
    seek(t) {
      for (const animation of document.getAnimations()) {
        animation.pause();
        animation.currentTime = t * 1000;
      }
    },
  };
  window.JB = JB;
  document.documentElement.dataset.format = JB.format;

  window.addEventListener("load", async () => {
    await document.fonts.ready;
    await Promise.all(
      [...document.images].map((img) => (img.decode ? img.decode().catch(() => {}) : null)),
    );
    for (const fn of builders) fn(JB);
    JB.seek(0);
    JB.isReady = true;
  });
})();
