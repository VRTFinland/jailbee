# Web dashboard design system: Material 3 tokens, headless behaviour, bundled assets

Refines [ADR 0004](0004-web-dashboard-stack.md). The web dashboard's look is one design system written as code: design tokens with Material 3 role names (`--md-*` in `frontend/src/styles/tokens.css`), a small set of UI primitives built only from those tokens, and the `/ui` gallery that shows both under every theme and display profile. See #167.

- **Material 3, without shadows.** The design language is Material 3 with tonal surfaces and tonal elevation only: no shadow elevation and no ripple. Light and dark are the tonal palettes of one blue seed. Hover and pressed states are Material 3 state layers. State transitions last at most 150 ms in the normal display profile.
- **A headless behaviour library is allowed; a styled component library is still not.** Dialogs, menus and selects may take focus trapping, keyboard navigation, ARIA and dismissal from a headless library (Reka UI). It is used only inside the UI primitives, never by views, and it brings no visual styling. ADR 0004's rejection of styled libraries such as Vuetify or PrimeVue stands.
- **The font and icons are bundled.** Inter (variable, latin subset) and the Material Symbols icons the dashboard uses are built into the app. No third-party font host, icon font or CDN: the dashboard renders the same on every device and makes no request to another host.
- **The e-ink display profile overrides the design language.** The e-ink theme stays black on white with hairline borders, underlined links, no tints, no shadows and no animation; only the shape scale carries over. Under `prefers-reduced-motion`, nothing moves either.

## Considered options

- **Keep ADR 0004's own tokens and let each view style its controls.** Rejected. Without shared primitives every view invented its own buttons, rows and dialogs, and the #164 reader chrome drifted from its mockups because the look never reached the implementer.
- **A styled Material library (Vuetify).** Rejected for the reasons in ADR 0004: its shadows, ripples and animations fight the e-ink profile.
- **A refined version of the previous look, or one close to the terminal dashboard.** Compared on identical mockups and not chosen.
- **Fonts and icons from Google Fonts.** Rejected. A third-party host leaks dashboard use and fails offline.

## Consequences

- Old token names stay as aliases of the `--md-*` tokens until every view is migrated; then they are removed.
- Stylelint rejects raw colours, font sizes, radii, z-indexes and spacing outside the token file, so the tokens are the only easy way.
- The gallery is the visual reference: its screenshots are compared in the e2e suite.
- The built app grows by the font file (about 50 kB) and the icon components.
