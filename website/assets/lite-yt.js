// Click-to-load YouTube, for news articles (scripts/news_site.py renders the
// markup) and the landing page's #demos. Nothing is fetched from YouTube
// until the reader presses play; then the button becomes a youtube-nocookie
// player. Without JavaScript the thumbnail stays and the "Watch on YouTube"
// link still works.
document.addEventListener("click", (event) => {
  const button = event.target.closest(".yt__play");
  if (!button) return;
  const id = button.closest(".yt").dataset.ytId;
  const frame = document.createElement("iframe");
  frame.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0`;
  frame.title = button.getAttribute("aria-label") || "YouTube video";
  frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  frame.allowFullscreen = true;
  frame.className = "yt__frame";
  frame.ytButton = button;
  button.replaceWith(frame);
});

// A player left running in a panel a tab switch hid keeps playing where
// nobody can see it. Put the button back, which also stops it.
// The panels fade (--tabs-fade, 0.8s in style.css) and `visibility` flips
// only at the end of it, so look once the fade is over.
document.addEventListener("change", (event) => {
  if (!event.target.classList.contains("tabs__radio")) return;
  setTimeout(() => {
    for (const frame of document.querySelectorAll(".yt__frame")) {
      // Hidden either way: #demos hides panels with `visibility`, keeping
      // their height; elsewhere a hidden ancestor would be `display: none`.
      const hidden = frame.offsetParent === null || getComputedStyle(frame).visibility !== "visible";
      if (hidden && frame.ytButton) frame.replaceWith(frame.ytButton);
    }
  }, 900);
});
