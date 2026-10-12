// Click-to-load YouTube for news articles (scripts/news_site.py renders the
// markup). Nothing is fetched from YouTube until the reader presses play;
// then the button becomes a youtube-nocookie player. Without JavaScript the
// thumbnail stays and the "Watch on YouTube" link still works.
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
  button.replaceWith(frame);
});
