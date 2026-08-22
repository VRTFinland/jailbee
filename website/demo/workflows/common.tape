# Shared look for every workflow tape. `Source`d as the first line of each, so
# the videos are visually one set. The values match the retired 1.0 tapes,
# which were tuned against website/assets/style.css.
Set Shell bash
Set FontFamily "IBM Plex Mono"
Set FontSize 18
Set Width 1200
Set Padding 28
Set Theme { "background": "#0b0c0e", "foreground": "#e8e6e3", "cursor": "#f0a92b", "black": "#14161a", "red": "#e06c6c", "green": "#74c47c", "yellow": "#f0a92b", "blue": "#a2a7b0", "magenta": "#a2a7b0", "cyan": "#a2a7b0", "white": "#e8e6e3" }
Set TypingSpeed 45ms
# Real commands take real time. The default 5s WaitTimeout is far too short for
# `jailbee new`, and a Wait that times out fails the entire render.
Set WaitTimeout 180s

# Height is deliberately NOT set here: each tape sizes it to its own content.
# A frame that is two thirds empty is the mistake the retired new.tape
# documented and fixed with a comment; do the same per tape.
