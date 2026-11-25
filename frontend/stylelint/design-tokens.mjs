import stylelint from "stylelint"
import valueParser from "postcss-value-parser"

const ruleName = "jailbee/design-tokens"
const messages = stylelint.utils.ruleMessages(ruleName, {
    expected: (property, value) => `Expected a design token instead of "${value}" in "${property}"`,
})
const spacing = /^(?:(?:margin|padding|inset)(?:-.+)?|(?:row-|column-)?gap|top|right|bottom|left)$/
const borderWidth = /^(?:border(?:-.+)?|outline(?:-width)?)$/
const keywords = new Set([
    "inherit", "initial", "unset", "revert", "revert-layer", "auto", "none",
    "solid", "dashed", "dotted", "double", "hidden", "groove", "ridge", "inset", "outset",
    "currentcolor", "transparent",
])

// Parse values, not text: var() fallbacks and nested calc() must not hide raw
// dimensions. Widths are special only for borders/outlines, never for spacing.
export default stylelint.createPlugin(ruleName, (enabled) => (root, result) => {
    if (!stylelint.utils.validateOptions(result, ruleName, {actual: enabled, possible: [true]})) return
    root.walkDecls((decl) => {
        const property = decl.prop.toLowerCase()
        const customProperty = property.startsWith("--")
        const checksBorderWidth = borderWidth.test(property) && !/(?:color|style|radius)$/.test(property)
        if (!(customProperty || spacing.test(property) || checksBorderWidth || property === "font" || property === "font-size"
            || property.endsWith("radius") || property === "z-index")) return

        let invalid
        function check(nodes) {
            for (const node of nodes) {
                if (node.type === "function") {
                    const name = node.value.toLowerCase()
                    if (name === "var" || name === "env") {
                        // The first argument is a name; any fallback is still CSS.
                        const comma = node.nodes.findIndex((part) => part.type === "div" && part.value === ",")
                        if (comma !== -1) check(node.nodes.slice(comma + 1))
                    } else {
                        check(node.nodes)
                    }
                } else if (node.type === "word") {
                    const value = node.value.toLowerCase()
                    // In font shorthand, family names and unitless weights are
                    // not sizes. Custom properties may alias tokens, but may not
                    // introduce a private scale of raw numbers or dimensions.
                    if (property === "font" && !/^[+-]?(?:\d|\.\d).*?(?:[a-z%])$/i.test(value)) continue
                    if (customProperty && !/^[+-]?(?:\d|\.\d)/.test(value)) continue
                    if (!keywords.has(value) && !["0", "+", "-", "*", "/"].includes(value)
                        && !(checksBorderWidth && ["1px", "2px"].includes(value))) invalid ??= node.value
                }
            }
        }
        check(valueParser(decl.value).nodes)
        if (invalid) stylelint.utils.report({
            ruleName, result, node: decl,
            message: messages.expected(property, invalid),
            word: invalid,
        })
    })
})
