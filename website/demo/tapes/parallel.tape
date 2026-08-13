# Records scenes/parallel.sh (measured runtime ~0.9s: 0.4s prompt pause +
# 0.5s, then an instant `cat` of generated/ls.txt) into the three demo media
# formats #demo-parallel consumes on the page. See ../scenes/README.md for
# what the scene reconstructs and why.
#
# Sleep below is the measured runtime plus a ~2s tail so the clip ends on a
# settled frame instead of holding the last line for the rest of playback.
# Re-measure and adjust if parallel.sh's timing changes.

Output ../../assets/media/parallel.webm
Output ../../assets/media/parallel.mp4

Set Shell bash
Set FontFamily "IBM Plex Mono"
Set FontSize 18
Set Width 1200
# 300, not 750: sized to this scene's own content (the command line, the
# 8-line table, and the trailing shell prompt) plus a small margin — a
# fixed 750 left two thirds of the frame empty. See
# website/assets/style.css's #demo-parallel rule, which must match this
# exactly.
Set Height 300
Set Padding 28
Set Theme { "background": "#0b0c0e", "foreground": "#e8e6e3", "cursor": "#f0a92b", "black": "#14161a", "red": "#e06c6c", "green": "#74c47c", "yellow": "#f0a92b", "blue": "#a2a7b0", "magenta": "#a2a7b0", "cyan": "#a2a7b0", "white": "#e8e6e3" }
Set TypingSpeed 45ms

# The shell echoes the "../scenes/parallel.sh" line below once `Show`
# fires, no matter how it's invoked — `scenes/_lib.sh` clears the screen
# again as the first thing the scene itself does, which is what actually
# keeps that echoed invocation off the recorded frame.
Hide
Type "clear" Enter
Type "../scenes/parallel.sh" Enter
Show

Sleep 3s
