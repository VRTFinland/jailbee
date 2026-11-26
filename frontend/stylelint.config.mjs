export default {
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
