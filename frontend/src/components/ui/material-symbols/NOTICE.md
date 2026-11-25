# Material Symbols

Copyright Google LLC. Licensed under the Apache License, Version 2.0; the full
licence is in `LICENSE` alongside this notice. The gallery links to both files,
which are shipped in the production build.

Source: https://github.com/google/material-design-icons
Revision: `10ecc14790612ca8fd164e3f4ffe9c16fc50d5b7`

The bundled vectors are Material Symbols **Outlined**, optical size 24, weight
400, unfilled, from `symbols/web/<symbol>/materialsymbolsoutlined/<symbol>_24px.svg`.
Only the SVG wrapper was replaced; the path geometry is unchanged. No icon font,
external sprite or runtime request to Google is used.

| Dashboard name | Material Symbol |
| --- | --- |
| menu | more_vert |
| back | chevron_left |
| forward | chevron_right |
| close | close |
| expand | expand_more |
| collapse | expand_less |
| page-up | arrow_upward |
| page-down | arrow_downward |
| jump | low_priority |
| start | vertical_align_top |
| end | vertical_align_bottom |
| settings | settings |
| add | add |
| remove | remove |
| waiting | front_hand |
| working | pending |
| idle | pause_circle |
| error | error |
| running | play_circle |
| stopped | stop_circle |
| booting | power_settings_new |
| autostart | rocket_launch |
| job | terminal |
| success | check_circle |
| failure | cancel |

## Component API

Import `Icon` from `components/ui/Icon.vue`. `name` is a typed dashboard name
from the table above. `size` is `small`, `medium` (default) or `large`, using
`--md-icon-small`, `--md-icon-medium` and `--md-icon-large` (1, 1.5 and 2rem).
The vectors are filled with `currentColor`, the surrounding text colour, in
every theme and display profile: colour belongs to the theme (ADR 0007), and
the e-ink theme's text tokens are already black.

Supply `label="Settings"` for a standalone meaningful image. Omit `label` for a
decorative icon, including one inside a button that already has an accessible
name. Icons never take keyboard focus. Agent states should still have visible
text labels; the icon is not a substitute for the state name.
