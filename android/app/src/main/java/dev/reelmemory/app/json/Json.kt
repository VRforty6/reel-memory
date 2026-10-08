package dev.reelmemory.app.json

/**
 * Minimal pure-JVM JSON model + parser.
 *
 * Why this exists: the parsers under `net/` and `auth/` are documented as
 * "pure JVM logic (no Android APIs)" and are covered by JVM unit tests, but
 * they used `org.json`, which is an Android framework API. On a developer
 * machine / CI the Android `android.jar` only contains stubs, so every
 * `JSONObject(...)` call throws `RuntimeException("Stub!")` and the whole
 * parsing test suite fails. On a real device `org.json` happens to work, but
 * depending on a framework class in code that is meant to be platform-free
 * is fragile and untestable.
 *
 * [JsonObject]/[JsonArray] expose the small subset of the `org.json` API the
 * app actually uses (`optString/optLong/optInt/optDouble/optBoolean/isNull/
 * optObject/optArray/keys/put/getString/getLong/length`), with identical
 * coercion semantics, so call sites change only by type name. Behaviour on a
 * real device is unchanged: valid backend JSON parses to the same values.
 *
 * The parser is a small recursive-descent implementation: objects, arrays,
 * strings (with escapes incl. `\uXXXX`), numbers (integral values become
 * [Long], fractional ones [Double]), `true`/`false`/`null`. Malformed input
 * throws [IllegalArgumentException], which the existing `try/catch` blocks
 * in the parsers already translate into `null`/fallbacks.
 */
class JsonObject {
    private val map = LinkedHashMap<String, Any?>()

    constructor()

    constructor(text: String) : this() {
        val parsed = parseValue(Parser(text)) as? Map<*, *>
            ?: throw IllegalArgumentException("Not a JSON object")
        for ((k, v) in parsed) map[k as String] = v
    }

    internal constructor(values: Map<String, Any?>) : this() {
        map.putAll(values)
    }

    /** Missing key (or JSON null) -> `""`, mirroring `org.json`. */
    fun optString(name: String): String = optString(name, "")

    /**
     * Missing key (or JSON null) -> [fallback]. The result is non-null whenever
     * [fallback] is non-null, mirroring `org.json`'s platform type. For a
     * nullable fallback use [optStringOrNull].
     */
    fun optString(name: String, fallback: String): String {
        val v = map[name] ?: return fallback
        return scalarToString(v)
    }

    /** Missing key (or JSON null) -> `null`. */
    fun optStringOrNull(name: String): String? {
        val v = map[name] ?: return null
        return scalarToString(v)
    }

    fun optLong(name: String, fallback: Long = 0L): Long {
        val v = map[name] ?: return fallback
        return when (v) {
            is Number -> v.toLong()
            is String -> v.toLongOrNull() ?: fallback
            else -> fallback
        }
    }

    fun optInt(name: String, fallback: Int = 0): Int {
        val v = map[name] ?: return fallback
        return when (v) {
            is Number -> v.toInt()
            is String -> v.toIntOrNull() ?: fallback
            else -> fallback
        }
    }

    fun optDouble(name: String, fallback: Double = Double.NaN): Double {
        val v = map[name] ?: return fallback
        return when (v) {
            is Number -> v.toDouble()
            is String -> v.toDoubleOrNull() ?: fallback
            else -> fallback
        }
    }

    fun optBoolean(name: String, fallback: Boolean = false): Boolean {
        val v = map[name] ?: return fallback
        return when (v) {
            is Boolean -> v
            is String -> when {
                v.equals("true", ignoreCase = true) -> true
                v.equals("false", ignoreCase = true) -> false
                else -> fallback
            }
            else -> fallback
        }
    }

    fun isNull(name: String): Boolean = map[name] == null

    fun optObject(name: String): JsonObject? = (map[name] as? Map<String, Any?>)?.let { JsonObject(it) }

    fun optArray(name: String): JsonArray? = (map[name] as? List<Any?>)?.let { JsonArray(it) }

    fun keys(): Iterator<String> = map.keys.iterator()

    fun put(name: String, value: Any?): JsonObject {
        map[name] = value
        return this
    }

    fun getString(name: String): String {
        val v = map[name] ?: throw NoSuchElementException("Missing key: $name")
        return scalarToString(v)
    }

    fun getLong(name: String): Long {
        val v = map[name] ?: throw NoSuchElementException("Missing key: $name")
        return when (v) {
            is Number -> v.toLong()
            is String -> v.toLongOrNull()
                ?: throw IllegalArgumentException("Not a number: $name")
            else -> throw IllegalArgumentException("Not a number: $name")
        }
    }

    override fun toString(): String = buildString {
        append('{')
        map.entries.forEachIndexed { i, (k, v) ->
            if (i > 0) append(',')
            appendQuoted(k)
            append(':')
            appendValue(v)
        }
        append('}')
    }

    private fun scalarToString(v: Any?): String = when (v) {
        is String -> v
        is Map<*, *> -> JsonObject(v as Map<String, Any?>).toString()
        is List<*> -> JsonArray(v as List<Any?>).toString()
        else -> v.toString()
    }
}

class JsonArray {
    private val list = ArrayList<Any?>()

    constructor()

    constructor(text: String) : this() {
        val parsed = parseValue(Parser(text)) as? List<*>
            ?: throw IllegalArgumentException("Not a JSON array")
        list.addAll(parsed)
    }

    internal constructor(values: List<Any?>) : this() {
        list.addAll(values)
    }

    fun length(): Int = list.size

    fun optObject(index: Int): JsonObject? =
        (list.getOrNull(index) as? Map<String, Any?>)?.let { JsonObject(it) }

    fun optString(index: Int, fallback: String = ""): String =
        list.getOrNull(index)?.let {
            when (it) {
                is String -> it
                else -> it.toString()
            }
        } ?: fallback

    fun getObject(index: Int): JsonObject =
        optObject(index) ?: throw IndexOutOfBoundsException("Not an object at index $index")

    override fun toString(): String = buildString {
        append('[')
        list.forEachIndexed { i, v ->
            if (i > 0) append(',')
            appendValue(v)
        }
        append(']')
    }
}

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

private class Parser(val text: String) {
    var pos = 0

    fun error(msg: String): Nothing =
        throw IllegalArgumentException("$msg at position $pos in: ${text.take(60)}")

    fun skipWs() {
        while (pos < text.length && text[pos].isWhitespace()) pos++
    }

    fun expect(c: Char) {
        skipWs()
        if (pos >= text.length || text[pos] != c) error("Expected '$c'")
        pos++
    }

    fun peek(): Char {
        skipWs()
        if (pos >= text.length) error("Unexpected end of input")
        return text[pos]
    }
}

private fun parseValue(p: Parser): Any? {
    return when (val c = p.peek()) {
        '{' -> parseObject(p)
        '[' -> parseArray(p)
        '"' -> parseString(p)
        't' -> parseLiteral(p, "true", true)
        'f' -> parseLiteral(p, "false", false)
        'n' -> parseLiteral(p, "null", null)
        '-', in '0'..'9' -> parseNumber(p)
        else -> p.error("Unexpected character '$c'")
    }
}

private fun parseObject(p: Parser): Map<String, Any?> {
    val out = LinkedHashMap<String, Any?>()
    p.expect('{')
    if (p.peek() == '}') {
        p.pos++
        return out
    }
    while (true) {
        p.skipWs()
        if (p.peek() != '"') p.error("Expected string key")
        val key = parseString(p)
        p.expect(':')
        out[key] = parseValue(p)
        p.skipWs()
        when (p.peek()) {
            ',' -> p.pos++
            '}' -> {
                p.pos++
                return out
            }
            else -> p.error("Expected ',' or '}'")
        }
    }
}

private fun parseArray(p: Parser): List<Any?> {
    val out = ArrayList<Any?>()
    p.expect('[')
    if (p.peek() == ']') {
        p.pos++
        return out
    }
    while (true) {
        out += parseValue(p)
        p.skipWs()
        when (p.peek()) {
            ',' -> p.pos++
            ']' -> {
                p.pos++
                return out
            }
            else -> p.error("Expected ',' or ']'")
        }
    }
}

private fun parseString(p: Parser): String {
    val t = p.text
    p.expect('"')
    val sb = StringBuilder()
    while (true) {
        if (p.pos >= t.length) p.error("Unterminated string")
        val c = t[p.pos++]
        when (c) {
            '"' -> return sb.toString()
            '\\' -> {
                if (p.pos >= t.length) p.error("Unterminated escape")
                when (val e = t[p.pos++]) {
                    '"' -> sb.append('"')
                    '\\' -> sb.append('\\')
                    '/' -> sb.append('/')
                    'b' -> sb.append('\b')
                    'f' -> sb.append('\u000C')
                    'n' -> sb.append('\n')
                    'r' -> sb.append('\r')
                    't' -> sb.append('\t')
                    'u' -> {
                        if (p.pos + 4 > t.length) p.error("Bad unicode escape")
                        val hex = t.substring(p.pos, p.pos + 4)
                        val code = hex.toIntOrNull(16) ?: p.error("Bad unicode escape")
                        p.pos += 4
                        sb.append(code.toChar())
                    }
                    else -> p.error("Bad escape '\\$e'")
                }
            }
            else -> sb.append(c)
        }
    }
}

private fun parseLiteral(p: Parser, literal: String, value: Any?): Any? {
    if (!p.text.startsWith(literal, p.pos)) p.error("Expected '$literal'")
    p.pos += literal.length
    return value
}

private fun parseNumber(p: Parser): Number {
    val t = p.text
    val start = p.pos
    if (p.pos < t.length && t[p.pos] == '-') p.pos++
    while (p.pos < t.length && t[p.pos].isDigit()) p.pos++
    var isDouble = false
    if (p.pos < t.length && t[p.pos] == '.') {
        isDouble = true
        p.pos++
        while (p.pos < t.length && t[p.pos].isDigit()) p.pos++
    }
    if (p.pos < t.length && (t[p.pos] == 'e' || t[p.pos] == 'E')) {
        isDouble = true
        p.pos++
        if (p.pos < t.length && (t[p.pos] == '+' || t[p.pos] == '-')) p.pos++
        while (p.pos < t.length && t[p.pos].isDigit()) p.pos++
    }
    val raw = t.substring(start, p.pos)
    if (raw.isEmpty() || raw == "-") p.error("Bad number")
    return if (isDouble) {
        raw.toDoubleOrNull() ?: p.error("Bad number")
    } else {
        raw.toLongOrNull() ?: raw.toDoubleOrNull() ?: p.error("Bad number")
    }
}

// ---------------------------------------------------------------------------
// Serializer
// ---------------------------------------------------------------------------

private fun StringBuilder.appendQuoted(s: String) {
    append('"')
    for (c in s) {
        when (c) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            '\b' -> append("\\b")
            '\u000C' -> append("\\f")
            else -> if (c < ' ') append("\\u%04x".format(c.code)) else append(c)
        }
    }
    append('"')
}

private fun StringBuilder.appendValue(v: Any?) {
    when (v) {
        null -> append("null")
        is String -> appendQuoted(v)
        is Boolean -> append(if (v) "true" else "false")
        is Long, is Int, is Short, is Byte -> append(v.toString())
        is Double -> append(if (v.isFinite()) v.toString() else "null")
        is Float -> append(if (v.isFinite()) v.toString() else "null")
        is Map<*, *> -> append(JsonObject(v as Map<String, Any?>).toString())
        is List<*> -> append(JsonArray(v as List<Any?>).toString())
        is JsonObject -> append(v.toString())
        is JsonArray -> append(v.toString())
        else -> appendQuoted(v.toString())
    }
}
