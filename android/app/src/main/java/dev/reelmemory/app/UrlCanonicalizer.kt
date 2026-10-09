package dev.reelmemory.app

import java.net.URI
import java.net.URISyntaxException

/**
 * URL canonicalization, mirroring the backend rules in
 * backend `app/capture/canonicalize.py` (PRD FR-CAP-002/003, SEC-001):
 *
 * - Host allowlist: instagram.com, www.instagram.com, m.instagram.com
 * - Supported paths: /reel/<shortcode>, /reels/<shortcode>, /p/<shortcode>
 * - Canonical form preserves kind: /reel/<shortcode> or /p/<shortcode> (query stripped)
 * - Instagram is the first SourceAdapter; this is intentionally the only one for v1.
 */
object UrlCanonicalizer {

    /** Hosts we accept (backend SEC-001 allowlist). */
    val allowedHosts: Set<String> =
        setOf("instagram.com", "www.instagram.com", "m.instagram.com")

    private val pathKinds = setOf("reel", "reels", "p")

    /** Instagram shortcodes are URL-safe base64-ish tokens. */
    private val shortcodeRegex = Regex("^[A-Za-z0-9_-]{2,128}$")

    sealed class Result {
        /** Canonical representation of the shared URL. */
        data class Ok(
            val platform: String,          // "instagram"
            val platformItemId: String,    // shortcode — canonical source identity
            val canonicalUrl: String,      // query params stripped
            val originalUrl: String        // exactly what the user shared
        ) : Result()

        /** Reason for rejection; codes match backend FailureCode names. */
        data class Err(val code: String, val message: String) : Result()
    }

    fun canonicalize(rawUrl: String?): Result {
        val raw = (rawUrl ?: "").trim()
        if (raw.isEmpty()) return Result.Err("INVALID_URL", "empty URL")

        // Pure-JVM parsing (java.net.URI): no Android dependency, unit-testable.
        val uri = try {
            URI(if (raw.contains("://")) raw else "https://$raw")
        } catch (e: URISyntaxException) {
            return Result.Err("INVALID_URL", "malformed URL: $raw")
        } catch (e: IllegalArgumentException) {
            return Result.Err("INVALID_URL", "malformed URL: $raw")
        }

        val scheme = (uri.scheme ?: "").lowercase()
        if (scheme != "http" && scheme != "https") {
            return Result.Err("INVALID_URL", "unsupported scheme in URL: $raw")
        }

        val host = (uri.host ?: "").lowercase()
        if (host.isEmpty() || !Regex("^[a-z0-9.-]+\$").matches(host)) {
            return Result.Err("INVALID_URL", "malformed host in URL: $raw")
        }
        if (host !in allowedHosts) {
            // SEC-001: never fetch non-allowlisted hosts.
            return Result.Err("UNSUPPORTED_SOURCE", "host not allowlisted: $host")
        }

        val parts = (uri.path ?: "").split("/").filter { it.isNotEmpty() }
        if (parts.size != 2 || parts[0].lowercase() !in pathKinds) {
            return Result.Err(
                "INVALID_URL",
                "expected /reel/<shortcode>, /reels/<shortcode> or /p/<shortcode>: $raw"
            )
        }
        val shortcode = parts[1]
        if (!shortcodeRegex.matches(shortcode)) {
            return Result.Err("INVALID_URL", "invalid shortcode: $shortcode")
        }

        // Preserve post-vs-reel so the backend can choose the correct source actor.
        // /reels/ normalizes to /reel/; /p/ remains /p/.
        val canonicalKind = if (parts[0].lowercase() == "p") "p" else "reel"
        val canonical = "https://instagram.com/$canonicalKind/$shortcode"
        return Result.Ok(
            platform = "instagram",
            platformItemId = shortcode,
            canonicalUrl = canonical,
            originalUrl = raw
        )
    }
}

/**
 * Extracts candidate URLs from shared text (e.g. an Instagram share message
 * usually contains exactly one instagram.com link, sometimes with surrounding text).
 */
object UrlExtractor {
    private val urlRegex = Regex("""https?://[^\s<>"']+""")

    fun extract(text: String?): List<String> {
        if (text.isNullOrBlank()) return emptyList()
        return urlRegex.findAll(text)
            .map { it.value.trimEnd('.', ',', ';', '!', '?', ')', ']') }
            .filter { it.isNotEmpty() }
            .distinct()
            .toList()
    }
}
