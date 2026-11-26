import stylelint from "stylelint"
import valueParser from "postcss-value-parser"

const ruleName = "jailbee/design-tokens"
const messages = stylelint.utils.ruleMessages(ruleName, {
    expected: (property, value) => `Expected a design token instead of "${value}" in "${property}"`,
    tokenFile: (property) =>
        `Only token declarations belong in tokens.css: move "${property}" to a linted stylesheet (base.css)`,
})

// Properties that take only tokens: any raw number or dimension is wrong.
const spacing = /^(?:(?:margin|padding|inset)(?:-.+)?|(?:row-|column-)?gap|top|right|bottom|left)$/
const borderWidth = /^(?:border(?:-.+)?|outline(?:-width)?)$/
// Properties where a literal px size is wrong (rem, %, vw, unitless are layout).
const sizes = /^(?:min-|max-)?(?:width|height|inline-size|block-size)$/
const pxProperties = new Set(["line-height", "outline-offset", "letter-spacing", "transform", "translate"])
// Sizes and offsets may still be a 1-2px hairline, as border widths may.
const hairlineProperties = /^(?:(?:min-|max-)?(?:width|height|inline-size|block-size)|outline-offset)$/
const motion = /^(?:transition|animation)(?:-.+)?$/
const keywords = new Set([
    "inherit", "initial", "unset", "revert", "revert-layer", "auto", "none",
    "solid", "dashed", "dotted", "double", "hidden", "groove", "ridge", "inset", "outset",
    "currentcolor", "transparent",
])
const number = /^[+-]?(?:\d|\.\d)/
const px = /^[+-]?(?:\d+\.?\d*|\.\d+)px$/i
const time = /^[+-]?(?:\d+\.?\d*|\.\d+)m?s$/i
const zeroTime = /^[+-]?0*\.?0+m?s$/i

/** Each word of a value, skipping var()/env() names but not their fallbacks. */
function* words(nodes) {
    for (const node of nodes) {
        if (node.type === "function") {
            const name = node.value.toLowerCase()
            if (name === "var" || name === "env") {
                const comma = node.nodes.findIndex((part) => part.type === "div" && part.value === ",")
                if (comma !== -1) yield* words(node.nodes.slice(comma + 1))
            } else {
                yield* words(node.nodes)
            }
        } else if (node.type === "word") {
            yield node.value
        }
    }
}

/** The first raw value `property` must not have, or undefined. */
function invalidValue(property, value) {
    const customProperty = property.startsWith("--")
    const checksBorderWidth = borderWidth.test(property) && !/(?:color|style|radius|collapse|spacing)$/.test(property)
    const parsed = valueParser(value).nodes
    if (customProperty || spacing.test(property) || checksBorderWidth || property === "font"
        || property === "font-size" || property.endsWith("radius") || property === "z-index") {
        for (const raw of words(parsed)) {
            const word = raw.toLowerCase()
            // In font shorthand, family names and unitless weights are not
            // sizes. Custom properties may alias tokens, but may not introduce
            // a private scale of raw numbers or dimensions.
            if (property === "font" && !/^[+-]?(?:\d|\.\d).*?(?:[a-z%])$/i.test(word)) continue
            if (customProperty && !number.test(word)) continue
            if (!keywords.has(word) && !["0", "+", "-", "*", "/"].includes(word)
                && !(checksBorderWidth && ["1px", "2px"].includes(word))) return raw
        }
    }
    if (property === "box-shadow") {
        const shadow = value.trim().toLowerCase()
        if (shadow !== "var(--md-elevation-level-2)" && shadow !== "none" && !keywords.has(shadow)) return value.trim()
    }
    if (sizes.test(property) || pxProperties.has(property)) {
        const hairline = hairlineProperties.test(property)
        for (const raw of words(parsed)) {
            if (px.test(raw) && !(hairline && /^[+-]?[12]px$/i.test(raw))) return raw
        }
    }
    if (motion.test(property)) {
        for (const raw of words(parsed)) {
            if (time.test(raw) && !zeroTime.test(raw)) return raw
        }
    }
    return undefined
}

function insideFontFace(decl) {
    for (let node = decl.parent; node; node = node.parent) {
        if (node.type === "atrule" && node.name.toLowerCase() === "font-face") return true
    }
    return false
}

// Parse values, not text: var() fallbacks and nested calc() must not hide raw
// values. With `tokenFile` (tokens.css only), custom property declarations are
// the tokens themselves and take any value, and nothing else may be declared
// there but @font-face descriptors and color-scheme.
export default stylelint.createPlugin(ruleName, (enabled, options) => (root, result) => {
    const valid = stylelint.utils.validateOptions(
        result, ruleName,
        {actual: enabled, possible: [true]},
        {actual: options, possible: {tokenFile: [true, false]}, optional: true},
    )
    if (!valid) return
    const tokenFile = options?.tokenFile === true
    root.walkDecls((decl) => {
        const property = decl.prop.toLowerCase()
        if (tokenFile) {
            if (property.startsWith("--") || property === "color-scheme" || insideFontFace(decl)) return
            stylelint.utils.report({ruleName, result, node: decl, message: messages.tokenFile(property)})
            return
        }
        const invalid = invalidValue(property, decl.value)
        if (invalid) stylelint.utils.report({
            ruleName, result, node: decl,
            message: messages.expected(property, invalid),
            word: invalid,
        })
    })
})
