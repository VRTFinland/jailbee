# Web dashboard design system: Material 3 tokens, headless behaviour, bundled assets

Refines [ADR 0004](0004-web-dashboard-stack.md). The web dashboard's look is one design system written as code: design tokens with Material 3 role names (`--md-*` in `frontend/src/styles/tokens.css`), a small set of UI primitives built only from those tokens, and the `/ui` gallery that shows both under every theme and display profile. See [#167](https://github.com/VRTFinland/jailbee/issues/167).

- **Material 3, primarily tonal elevation.** The design language is Material 3 with tonal surfaces and no ripple. After user testing (2026-11-26), menu-family surfaces (Menu, Select and quick preferences) use shared level-2 shadow elevation and extra-small corners to separate them from the page; all other surfaces stay shadow-free. Light and dark are the tonal palettes of one blue seed. Hover and pressed states are Material 3 state layers. State transitions last at most 150 ms; drawer entry and its scrim fade use 280 ms emphasized motion. E-ink and reduced motion override these as described below.
- **Token names never hide their value.** Spacing tokens are named by their px size at the default root (`--md-space-16` is 1rem), not by an index.
- **A headless behaviour library is allowed; a styled component library is still not.** Dialogs, menus and selects may take focus trapping, keyboard navigation, ARIA and dismissal from a headless library (Reka UI). It is used only inside the UI primitives, never by views, and it brings no visual styling. ADR 0004's rejection of styled libraries such as Vuetify or PrimeVue stands.
- **The font and icons are bundled.** Inter (variable, latin subset) and the Material Symbols icons the dashboard uses are built into the app. No third-party font host, icon font or CDN: the dashboard renders the same on every device and makes no request to another host.
- **E-ink overrides the design language, split between theme and display profile.** The e-ink *theme* sets the colours: black on white, hairline borders, underlined links, no tints; only the shape scale carries over. The e-ink *display profile* sets the behaviour: no animation or transitions, page turns instead of scrolling, no shadows or dimming backdrops. Each is chosen on its own, so the light theme with the e-ink profile is valid. Under `prefers-reduced-motion`, nothing moves either.
- **A theme or profile difference is a token, or a rule scoped without `:root`.** Custom properties inherit from the nearest `[data-theme]` or `[data-profile]`, so a gallery panel shows its own theme and profile whatever the page's; a rule that cannot be a token is written `[data-profile="eink"] .x`, never `:root[data-profile="eink"] .x`.

## Considered options

- **Keep ADR 0004's own tokens and let each view style its controls.** Rejected. Without shared primitives every view invented its own buttons, rows and dialogs, and the #164 reader chrome drifted from its mockups because the look never reached the implementer.
- **A styled Material library (Vuetify).** Rejected for the reasons in ADR 0004: its shadows, ripples and animations fight the e-ink profile.
- **A refined version of the previous look, or one close to the terminal dashboard.** Compared on identical mockups and not chosen.
- **Fonts and icons from Google Fonts.** Rejected. A third-party host leaks dashboard use and fails offline.

## Consequences

- Old token names stay as aliases of the `--md-*` tokens until every view is migrated; then they are removed.
- The tokens are to be the only easy way: Stylelint is to reject raw colours, font sizes, radii, z-indexes and spacing outside the token file.
- The gallery is the visual reference; its screenshots are to be compared in the e2e suite.
- The built app grows by the font file (about 50 kB) and, once added, the icon components.
- The token file, the gallery and Inter landed first; the primitives, icons, headless library, lint and screenshot comparison follow in the later #167 tickets.
