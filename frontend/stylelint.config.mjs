export default {
    // tokens.css is not ignored: an override below exempts its token
    // declarations only. Migration-only exceptions below are exact paths, never
    // component globs. Remove each with its migration (tickets 08-13); ticket 14
    // removes the list.
    ignoreFiles: [
        "src/views/RepoView.vue", // 09: repository
        "src/views/ContainerView.vue", // 10: container
        "src/views/LoginView.vue", // 12: login and setup
        "src/views/SetupView.vue",
        "src/components/PageGuide.vue", // 13: transcript reader
        "src/components/PageNav.vue",
        "src/components/ToolCall.vue",
        "src/views/SessionView.vue",
        "src/styles/highlight.css",
    ],
    plugins: ["./stylelint/design-tokens.mjs"],
    overrides: [
        {files: ["**/*.vue"], customSyntax: "postcss-html"},
        {
            // The scales and theme palettes define raw values here, and only
            // custom property declarations (the tokens) may be in this file.
            files: ["src/styles/tokens.css"],
            rules: {
                "jailbee/design-tokens": [true, {tokenFile: true}],
                "color-no-hex": null,
                "color-named": null,
                "function-disallowed-list": null,
            },
        },
    ],
    rules: {
        "jailbee/design-tokens": true,
        "color-no-hex": true,
        "color-named": "never",
        "function-disallowed-list": [
            "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color",
        ],
    },
}
