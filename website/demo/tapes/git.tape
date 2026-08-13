# Records scenes/git.sh (measured runtime ~1.7s: 0.4s prompt pause + 0.5s +
# 0.4s bridge-direction line + 0.4s push-summary line) into the three demo
# media formats #demo-git consumes on the page. See ../scenes/README.md for
# what the scene reconstructs and why.
#
# Sleep below is the measured runtime plus a ~2s tail so the clip ends on a
# settled frame instead of holding the last line for the rest of playback.
# Re-measure and adjust if git.sh's timing changes.

Output ../../assets/media/git.webm
Output ../../assets/media/git.mp4

Set Shell bash
Set FontFamily "IBM Plex Mono"
Set FontSize 18
Set Width 1200
# 210, not 750: sized to this scene's own content (5 rows of output plus
# the trailing shell prompt) plus a small margin — a fixed 750 left two
# thirds of the frame empty. See website/assets/style.css's #demo-git
# rule, which must match this exactly.
Set Height 210
Set Padding 28
Set Theme { "background": "#0b0c0e", "foreground": "#e8e6e3", "cursor": "#f0a92b", "black": "#14161a", "red": "#e06c6c", "green": "#74c47c", "yellow": "#f0a92b", "blue": "#a2a7b0", "magenta": "#a2a7b0", "cyan": "#a2a7b0", "white": "#e8e6e3" }
Set TypingSpeed 45ms

# The shell echoes the "../scenes/git.sh" line below once `Show` fires,
# no matter how it's invoked — `scenes/_lib.sh` clears the screen again as
# the first thing the scene itself does, which is what actually keeps
# that echoed invocation off the recorded frame.
Hide
Type "clear" Enter
Type "../scenes/git.sh" Enter
Show

Sleep 4s
